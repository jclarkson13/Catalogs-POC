"""Minimal in-process background job runner for the web UI.

Long operations (regenerate every customer, run the fidelity scoreboard) used
to be driven from the browser, so they stopped when the tab closed. Here they
run in a server-side thread instead, and the browser only polls for progress.

Deliberately small:
- One job at a time. Both job types are heavy on an 8 GB machine, so a second
  start is rejected (JobConflict -> HTTP 409) rather than queued.
- State (the current job and the last MAX_HISTORY finished ones) is persisted
  to a small JSON file (STATE_FILE, gitignored), rewritten atomically on every
  state change. Jobs are NOT resumed after a server restart: on startup any
  job that was queued/running is marked `interrupted` (see _load_state), an
  interrupted scoreboard gets the same `failed` audit entry a cancel writes,
  and an interrupted regenerate records which customers finished. A scoreboard
  subprocess (own process group) left running by the dead server is stopped.
- No generation/scoring logic here: `regenerate` calls generate() from
  scripts/generate_catalog.py, `scoreboard` shells out to
  scripts/fidelity_scoreboard.py exactly as a terminal user would.
"""
import ast
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from scripts.generate_catalog import ROOT, append_audit, audit_timestamp, generate

MAX_HISTORY = 20
# Per-customer errors/warnings are summarised, not stored in full: a blocked
# customer can have thousands of messages.
MAX_MESSAGES_PER_ITEM = 3
OUTPUT_TAIL_LINES = 40

STATE_FILE = ROOT / "webapp" / "backend" / "jobs_state.json"
INTERRUPTED_NOTE = "server restarted"

SCOREBOARD_SCRIPT = ROOT / "scripts" / "fidelity_scoreboard.py"
PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\] (\S+): (\S+)(?: \((\d+) unexplained\))?(?: (.*))?$")
REPORT_RE = re.compile(r"^Report: (reports/fidelity/\S+)\.md$")

ACTIVE_STATUSES = ("queued", "running")


class JobConflict(Exception):
    def __init__(self, running):
        self.running = running
        super().__init__(
            f"A {running['type']} job is already running (started {running['started_at']} by "
            f"{running['requested_by']}, {running['done']}/{running['total'] or '?'} done). "
            "Only one background job runs at a time - wait for it to finish or cancel it."
        )


class JobRunner:
    def __init__(self, state_path=STATE_FILE):
        self._lock = threading.Lock()
        self._current = None          # the queued/running job, if any
        self._history = deque(maxlen=MAX_HISTORY)  # finished jobs, newest first
        self._state_path = state_path  # None = memory only (tests)
        self._shutting_down = False
        self._load_state()

    # ---- public API ----
    def start(self, job_type, params, requested_by):
        if job_type not in ("regenerate", "scoreboard", "refresh"):
            raise ValueError(f"Unknown job type '{job_type}' (expected 'regenerate', 'scoreboard' or 'refresh')")
        with self._lock:
            if self._current is not None:
                raise JobConflict(self._public(self._current))
            job = {
                "id": uuid.uuid4().hex[:12],
                "type": job_type,
                "status": "queued",
                "params": params,
                "requested_by": requested_by,
                "created_at": audit_timestamp(),
                "started_at": None,
                "finished_at": None,
                "total": len(params["slugs"]) if job_type in ("regenerate", "refresh") else 0,
                "done": 0,
                "current": None,
                "items": [],
                "summary": {},
                "errors": [],
                "cancel_requested": False,
                "_started_mono": None,
                "_cancel": threading.Event(),
                "_proc": None,
            }
            if job_type in ("scoreboard", "refresh"):
                job.update(report=None, output_tail=[], pid=None)
            if job_type == "refresh":
                # Two phases: 1 = regenerate (reuses _run_regenerate's fields),
                # 2 = scoreboard (reuses _run_scoreboard_subprocess's fields).
                # phase1_items/phase1_summary snapshot phase 1's results before
                # they're reset for phase 2's progress tracking.
                job.update(phase=1, phase_count=2, phase1_items=[], phase1_summary={})
            self._current = job
            self._persist()
        target = {
            "regenerate": self._run_regenerate,
            "scoreboard": self._run_scoreboard,
            "refresh": self._run_refresh,
        }[job_type]
        threading.Thread(target=self._run, args=(job, target), name=f"job-{job['id']}", daemon=True).start()
        return self.get(job["id"])

    def get(self, job_id):
        with self._lock:
            for job in self._all():
                if job["id"] == job_id:
                    return self._public(job)
        return None

    def current(self):
        """The queued/running job (public view), or None."""
        with self._lock:
            return self._public(self._current) if self._current else None

    def list(self):
        with self._lock:
            return {
                "current": self._public(self._current) if self._current else None,
                "recent": [self._public(j) for j in self._history],
            }

    def cancel(self, job_id):
        """Returns the job, or None if unknown. Cancelling a finished job is a no-op."""
        with self._lock:
            job = next((j for j in self._all() if j["id"] == job_id), None)
            if job is None:
                return None
            if job["status"] in ACTIVE_STATUSES:
                job["cancel_requested"] = True
                job["_cancel"].set()
                proc = job["_proc"]
                self._persist()
            else:
                proc = None
        if proc is not None:
            # Don't block the request on the subprocess's shutdown grace period.
            threading.Thread(target=_terminate, args=(proc,), daemon=True).start()
        return self.get(job_id)

    def shutdown(self):
        """Server shutdown: stop a scoreboard subprocess (it runs in its own
        process group, so it would otherwise outlive the server) and freeze the
        state file as-is. The job stays queued/running on disk, so the next
        startup marks it interrupted and writes its audit entry."""
        with self._lock:
            self._shutting_down = True
            proc = self._current["_proc"] if self._current else None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    # ---- persistence ----
    def _persist(self):
        """Write current + history to the state file atomically. Call with
        self._lock held (so snapshots are written in order)."""
        if self._state_path is None:
            return
        state = {
            "saved_at": audit_timestamp(),
            "current": self._serialize(self._current) if self._current else None,
            "recent": [self._serialize(j) for j in self._history],
        }
        tmp = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except OSError as e:  # never let bookkeeping break a running job
            print(f"jobs: could not save {self._state_path}: {e}", file=sys.stderr)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _serialize(self, job):
        out = self._public(job)
        out["pid"] = job.get("pid")
        return out

    @staticmethod
    def _revive(d):
        """A job dict read back from the state file, with its internal fields."""
        job = dict(d)
        job["_saved_elapsed"] = job.pop("elapsed_seconds", 0) or 0
        job["_started_mono"] = None
        job["_cancel"] = threading.Event()
        job["_proc"] = None
        for k, default in (("items", []), ("summary", {}), ("errors", []), ("params", {}),
                           ("done", 0), ("total", 0), ("cancel_requested", False),
                           ("phase1_items", []), ("phase1_summary", {})):
            if job.get(k) is None:
                job[k] = default
        return job

    def _load_state(self):
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            current = state.get("current")
            recent = state.get("recent") or []
        except (OSError, ValueError, AttributeError) as e:
            print(f"jobs: ignoring unreadable {self._state_path}: {e}", file=sys.stderr)
            return
        history = [self._revive(d) for d in recent if isinstance(d, dict) and d.get("id")]
        # Defensive: interrupt anything active wherever it is in the file.
        active = [self._revive(current)] if isinstance(current, dict) and current.get("id") else []
        active += [j for j in history if j.get("status") in ACTIVE_STATUSES]
        history = [j for j in history if j.get("status") not in ACTIVE_STATUSES]
        for job in active:
            self._interrupt(job)
        with self._lock:
            self._history = deque(active + history, maxlen=MAX_HISTORY)
            self._current = None
            if active:
                self._persist()

    def _interrupt(self, job):
        """Mark a job found queued/running at startup as interrupted."""
        job["status"] = "interrupted"
        job["current"] = None
        job["finished_at"] = audit_timestamp()
        job["errors"].append(INTERRUPTED_NOTE)
        slugs = job["params"].get("slugs") or []
        if job["type"] == "regenerate":
            finished = [i["slug"] for i in job["items"]]
            done = set(finished)
            job["finished_slugs"] = finished
            job["not_run_slugs"] = [s for s in slugs if s not in done]
            job["errors"].append(
                f"stopped after {len(finished)} of {job['total']} customers; "
                f"{len(job['not_run_slugs'])} not run (jobs are not resumed automatically)"
            )
        elif job["type"] == "refresh" and job.get("phase") == 1:
            # Interrupted during phase 1 (regenerate): phase 2 never started.
            finished = [i["slug"] for i in job["items"]]
            done = set(finished)
            job["finished_slugs"] = finished
            job["not_run_slugs"] = [s for s in slugs if s not in done]
            job["errors"].append(
                f"stopped during phase 1 (regenerate) after {len(finished)} of {job['total']} customers; "
                f"{len(job['not_run_slugs'])} not run, phase 2 (scoreboard) never started "
                "(jobs are not resumed automatically)"
            )
        elif job["type"] in ("scoreboard", "refresh"):
            # A plain scoreboard job, or refresh's phase 2.
            request_text = "web UI scoreboard job" if job["type"] == "scoreboard" else "web UI refresh job"
            if _stop_orphan_scoreboard(job.get("pid")):
                job["errors"].append(f"terminated the orphaned scoreboard subprocess (pid {job['pid']})")
            if job.get("report") is None:
                job["errors"].append("no report written")
                # Same entry a cancel writes: the script never reached its own.
                try:
                    append_audit(
                        "all" if not slugs else ",".join(slugs)[:200], job["requested_by"],
                        request_text, "failed",
                        {"errors": list(job["errors"]), "scored_before_stop": job["done"], "warnings": []},
                        action="fidelity_scoreboard",
                    )
                except Exception as e:
                    job["errors"].append(f"could not write audit entry: {e}")
        job["pid"] = None

    # ---- internals ----
    def _all(self):
        return ([self._current] if self._current else []) + list(self._history)

    @staticmethod
    def _public(job):
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["items"] = list(job["items"])
        out["summary"] = dict(job["summary"])
        out["errors"] = list(job["errors"])
        if "output_tail" in job:
            out["output_tail"] = list(job["output_tail"])
        out.pop("pid", None)
        if job["_started_mono"] is not None:
            end = job.get("_finished_mono") or time.monotonic()
            out["elapsed_seconds"] = round(end - job["_started_mono"], 1)
        else:
            out["elapsed_seconds"] = job.get("_saved_elapsed", 0)  # loaded from the state file
        return out

    def _run(self, job, target):
        with self._lock:
            job["status"] = "running"
            job["started_at"] = audit_timestamp()
            job["_started_mono"] = time.monotonic()
            self._persist()
        try:
            target(job)
            final = "cancelled" if job["_cancel"].is_set() else ("failed" if job["errors"] else "succeeded")
        except Exception as e:  # a bug in the runner itself - record it, never leave the job "running"
            job["errors"].append(f"job crashed: {type(e).__name__}: {e}")
            final = "failed"
        with self._lock:
            if self._shutting_down:
                return  # leave it running on disk; next startup marks it interrupted
            job["status"] = final
            job["current"] = None
            job["finished_at"] = audit_timestamp()
            job["_finished_mono"] = time.monotonic()
            job["_proc"] = None
            self._history.appendleft(job)
            self._current = None
            self._persist()

    def _run_regenerate(self, job):
        p = job["params"]
        note = p.get("note") or "bulk regenerate via web UI job"
        counts = {"ok": 0, "blocked": 0, "error": 0}
        for slug in p["slugs"]:
            if job["_cancel"].is_set():
                break
            with self._lock:
                job["current"] = slug
            try:
                r = generate(slug, job["requested_by"], note, p.get("dry_run", False))
                item = {
                    "slug": slug,
                    "outcome": "ok" if not r["errors"] else "blocked",
                    "rows": r["rows"],
                    "warning_count": len(r["warnings"]),
                    "error_count": len(r["errors"]),
                    "errors": r["errors"][:MAX_MESSAGES_PER_ITEM],
                    "file_path": r.get("file_path"),
                }
            except (Exception, SystemExit) as e:  # expression errors raise SystemExit
                msg = f"{type(e).__name__}: {e}"
                # generate() only audits outcomes it returns; record crashes too.
                try:
                    append_audit(slug, job["requested_by"], note, "failed",
                                 {"errors": [f"exception during generate: {msg}"], "warnings": []})
                except Exception:
                    pass
                item = {"slug": slug, "outcome": "error", "rows": 0, "warning_count": 0,
                        "error_count": 1, "errors": [msg], "file_path": None}
            with self._lock:
                job["items"].append(item)
                counts[item["outcome"]] += 1
                job["summary"] = dict(counts)
                job["done"] += 1
                self._persist()

    def _run_scoreboard(self, job):
        slugs = job["params"]["slugs"]
        self._run_scoreboard_subprocess(job, slugs, from_catalogs=False, request_text="web UI scoreboard job")

    def _run_refresh(self, job):
        """Phase 1: regenerate every customer (reuses _run_regenerate as-is -
        same job fields as a plain 'regenerate' job). Phase 2: fidelity
        scoreboard over everything just written (--from-catalogs skips
        rebuilding every catalog a second time). Cancelling during phase 1
        stops after the current customer and skips phase 2 entirely; cancelling
        during phase 2 terminates the scoreboard subprocess, same as a plain
        scoreboard job."""
        with self._lock:
            job["phase"] = 1
            self._persist()
        self._run_regenerate(job)
        with self._lock:
            # Snapshot phase 1's results before they're reset for phase 2's
            # own progress tracking (done/total/items/summary are shared
            # fields between _run_regenerate and _run_scoreboard_subprocess).
            job["phase1_items"] = list(job["items"])
            job["phase1_summary"] = dict(job["summary"])
            cancelled = job["_cancel"].is_set()
            if not cancelled:
                job["phase"] = 2
                job["items"] = []
                job["summary"] = {}
                job["done"] = 0
                job["total"] = 0
                job["current"] = None
            self._persist()
        if cancelled:
            job["errors"].append("cancelled during phase 1 (regenerate); phase 2 (scoreboard) skipped")
            return
        self._run_scoreboard_subprocess(job, None, from_catalogs=True, request_text="web UI refresh job")

    def _run_scoreboard_subprocess(self, job, slugs, from_catalogs, request_text):
        cmd = [sys.executable, "-u", str(SCOREBOARD_SCRIPT), *(slugs or ["all"]),
               "--requested-by", job["requested_by"], "--request", request_text]
        if from_catalogs:
            cmd.append("--from-catalogs")
        # Own process group, so cancel can stop the ProcessPoolExecutor workers too.
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with self._lock:
            job["_proc"] = proc
            job["pid"] = proc.pid
            job["current"] = "starting (indexing real files)"
            self._persist()
        if job["_cancel"].is_set():  # cancelled between start() and Popen
            _terminate(proc)
        counts = {}
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with self._lock:
                job["output_tail"].append(line)
                del job["output_tail"][:-OUTPUT_TAIL_LINES]
                m = PROGRESS_RE.match(line)
                if m:
                    i, n, slug, status, unexplained, err = m.groups()
                    job["done"], job["total"] = int(i), int(n)
                    job["current"] = f"scored {slug}"
                    job["items"].append({"slug": slug, "status": status,
                                         "unexplained_cells": int(unexplained or 0), "error": err})
                    counts[status] = counts.get(status, 0) + 1
                    job["summary"] = dict(counts)
                    self._persist()
                elif (rm := REPORT_RE.match(line)):
                    job["report"] = rm.group(1) + ".json"
                    self._persist()
                elif line.startswith("{") and line.endswith("}"):
                    # Final status counts, incl. no_real_file customers that
                    # never get a progress line.
                    try:
                        job["summary"] = ast.literal_eval(line)
                    except (ValueError, SyntaxError):
                        pass
        rc = proc.wait()
        if self._shutting_down:
            return  # killed by server shutdown: startup handles errors + audit
        if job["_cancel"].is_set():
            job["errors"].append("cancelled; scoreboard subprocess terminated (no report written)")
        elif job["report"] is None:
            job["errors"].append(f"fidelity_scoreboard.py exited with code {rc} without writing a report")
        elif rc != 0:
            job["errors"].append(f"scoreboard finished with errors for some customers (exit code {rc})")
        # The script writes its own audit entry once it reaches the report
        # step; if it was killed or crashed first, record the attempt here.
        if job["report"] is None:
            append_audit(
                "all" if not slugs else ",".join(slugs)[:200], job["requested_by"], request_text,
                "failed", {"errors": list(job["errors"]), "scored_before_stop": job["done"], "warnings": []},
                action="fidelity_scoreboard",
            )


def _terminate(proc, grace=10):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _stop_orphan_scoreboard(pid):
    """If a scoreboard subprocess recorded in the state file is still alive
    (it runs in its own process group, so it can outlive a killed server),
    terminate its group. Checks the command line first so a recycled pid is
    never touched. Returns True if something was signalled."""
    if not pid:
        return False
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    if str(SCOREBOARD_SCRIPT) not in cmd or "web UI scoreboard job" not in cmd:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False
