#!/usr/bin/env python3
"""FastAPI backend for the Catalog Workshop sandbox.

Browse customers, templates, catalog history, master data, fidelity reports
and the audit log; edit safe customer-config fields; generate catalogs; run
long operations (regenerate all, fidelity scoreboard) as background jobs
(see jobs.py). No auth yet - see _requested_by().
"""
import csv
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
ROOT = WEBAPP_DIR.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scripts.generate_catalog import (
    AUDIT_DIR,
    CATALOGS_DIR,
    CHECK_REGISTRY,
    CUSTOMERS_DIR,
    TEMPLATES_DIR,
    append_audit,
    audit_source,
    customer_exclusions,
    customer_snapshot,
    generate,
    load_customer,
    load_master,
    load_template,
    master_row_flags,
)
# Read-only reuse of the scoreboard's own definitions (report location and
# what counts as a "wrong column") so the UI never disagrees with latest.md.
# Running the scoreboard is a background job (jobs.py) that shells out to the
# script itself.
from scripts.fidelity_scoreboard import REPORT_DIR as FIDELITY_REPORT_DIR, wrong_columns

from webapp.backend.jobs import JobConflict, JobRunner
from scripts import customer_config as cc

FIDELITY_REPORT = FIDELITY_REPORT_DIR / "latest.json"
FIDELITY_MISSING = (
    "No fidelity report found at reports/fidelity/latest.json. "
    "Use \"Run fidelity scoreboard\" on the Fidelity tab, or run "
    "`python3 scripts/fidelity_scoreboard.py all` from Claude Code, to create one."
)
# Per-column SKU lists can hold up to 2,000 SKUs each; the UI only needs a
# sample, so /fidelity/{slug} trims them to keep the response small.
FIDELITY_SKU_SAMPLE = 25

MASTER_SEARCH_FIELDS = ("sku", "manufacturer", "short_description", "cas_number")


def _duplicate_skus(rows):
    seen, dupes = set(), set()
    for r in rows:
        sku = (r.get("sku") or "").strip()
        if not sku:
            continue
        (dupes if sku in seen else seen).add(sku)
    return dupes


def _row_flags(row, duplicate_skus):
    flags = master_row_flags(row)
    sku = (row.get("sku") or "").strip()
    if sku and sku in duplicate_skus:
        flags = flags + ["duplicate SKU"]
    return flags


def _filter_master_rows(rows, search, flagged_only, duplicate_skus):
    if search:
        q = search.lower()
        rows = [r for r in rows if any(q in (r.get(f) or "").lower() for f in MASTER_SEARCH_FIELDS)]
    if flagged_only:
        rows = [r for r in rows if _row_flags(r, duplicate_skus)]
    return rows

app = FastAPI(title="Catalog Workshop API", version="0.1.0")

# Stale-code guard (added 2026-10-02). The server imports generate_catalog.py
# once, so editing it (or this backend) leaves the running process on the old
# code - a new template builtin then fails every Preview/Generate with a bare
# 500. Record the code files' mtimes at startup; when any changed, in-process
# generates (Preview, Generate, regenerate/refresh jobs) are refused with a
# clear "restart the server" message and the page shows a banner. Scoreboard
# jobs run the script as a subprocess, so they always use current code. No
# auto-reload: a reload would kill a running background job.
_CODE_FILES = sorted(
    [ROOT / "scripts" / "generate_catalog.py", ROOT / "scripts" / "fidelity_scoreboard.py"]
    + list((ROOT / "webapp" / "backend").glob("*.py"))
)
_CODE_MTIMES = {p: p.stat().st_mtime for p in _CODE_FILES}
_SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _changed_code_files():
    changed = []
    for p, mtime in _CODE_MTIMES.items():
        try:
            if p.stat().st_mtime != mtime:
                changed.append(str(p.relative_to(ROOT)))
        except FileNotFoundError:
            changed.append(str(p.relative_to(ROOT)))
    return changed


def _refuse_if_code_stale():
    changed = _changed_code_files()
    if changed:
        raise HTTPException(status_code=409, detail={
            "message": (
                "The server is running old code: " + ", ".join(changed) + " changed after it started. "
                "Restart the web server before generating (Preview/Generate would use the old code)."
            ),
            "stale_code": changed,
        })


@app.get("/server/status")
def server_status():
    """{started_at, stale_code: [changed files]} - polled by the page for its banner."""
    return {"started_at": _SERVER_STARTED_AT, "stale_code": _changed_code_files()}


# No auth layer yet (single local user). Mirrors the CLI's own default for
# --requested-by. Once this is exposed beyond one person, this must come
# from a verified server-side session instead, never from client input.
def _requested_by():
    return os.environ.get("USER", "unknown")


# Business users may edit these config values through the UI. Everything
# else — especially a template's `columns`/`expression` fields, which are
# eval()'d against master data — stays read-only and is a Claude Code /
# engineer change, not a web-form one.
EDITABLE_CUSTOMER_FIELDS = {
    "discount_percentage",
    "category_discounts",
    "institutions",
    "price_valid_from",
    "price_valid_until",
    "overrides",
}


class CustomerUpdate(BaseModel):
    discount_percentage: float | None = None
    category_discounts: dict[str, float] | None = None
    institutions: dict | None = None
    price_valid_from: str | None = None
    price_valid_until: str | None = None
    overrides: dict | None = None
    apply_to_sibling: bool = False  # also apply discounts/overrides to the other price period


class GenerateRequest(BaseModel):
    dry_run: bool = False
    note: str = ""


class JobRequest(BaseModel):
    type: str
    slugs: list[str] | None = None   # default: every customer
    dry_run: bool = False            # regenerate only
    note: str = ""                   # regenerate only


def _customer_slugs():
    return sorted(p.stem for p in CUSTOMERS_DIR.glob("*.json"))


def _template_names():
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.json"))


def _catalog_files(slug):
    out_dir = CATALOGS_DIR / slug
    if not out_dir.exists():
        return []
    files = []
    for p in sorted(out_dir.iterdir()):
        if p.name.startswith(".") or p.name.startswith("~$"):
            continue
        if p.is_file():
            stat = p.stat()
            files.append(
                {
                    "filename": p.name,
                    "size_bytes": stat.st_size,
                    "modified": date.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return sorted(files, key=lambda f: f["modified"], reverse=True)


def _all_audit_entries():
    entries = []
    for path in sorted(AUDIT_DIR.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    return entries


def _last_generate_by_slug(entries):
    """Most recent generate_catalog audit entry per customer — the basis for
    the customer list's status badge. This is the last KNOWN state (from the
    last real generate), not a live check: it can go stale if master data
    changes without anyone previewing/generating again."""
    last = {}
    for e in entries:
        if e.get("action") != "generate_catalog":
            continue
        slug = e.get("customer")
        if not slug:
            continue
        if slug not in last or _audit_dt(e) > _audit_dt(last[slug]):
            last[slug] = e
    return last


@app.get("/customers")
def list_customers():
    last_generate = _last_generate_by_slug(_all_audit_entries())

    summaries = []
    for slug in _customer_slugs():
        customer = load_customer(slug)
        catalogs = _catalog_files(slug)

        status = None
        last = last_generate.get(slug)
        if last:
            detail = last.get("detail")
            warnings = detail.get("warnings") or [] if isinstance(detail, dict) else []
            status = {
                "outcome": last.get("outcome"),
                "timestamp": last.get("timestamp"),
                "warning_count": len(warnings),
                "stale": any(w.startswith("row count") for w in warnings),
            }

        summaries.append(
            {
                "slug": slug,
                "display_name": customer.get("display_name", slug),
                "template_ref": customer.get("template_ref"),
                "file_type": customer.get("file_type"),
                "discount_percentage": customer.get("discount_percentage"),
                "category_discounts": customer.get("category_discounts"),
                "sales_region": customer.get("sales_region"),
                "neb": bool(customer.get("neb")),
                "last_generated": catalogs[0]["modified"] if catalogs else None,
                "last_generate_status": status,
            }
        )
    return summaries


@app.get("/customers/{slug}")
def get_customer(slug: str):
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    # The customer's filename is the canonical slug used everywhere else in
    # this API (URLs, /customers list). A customer's own `customer_id` field
    # is not reliably the same value (several pre-existing configs set it to
    # a STEP classification ID instead) - inject the real slug explicitly so
    # frontend code has one thing to trust.
    return {**load_customer(slug), "slug": slug}


# Fields that may be copied to the other price period's config. Validity
# dates never are: Current and Future cover different years.
SIBLING_FIELDS = ("discount_percentage", "category_discounts", "institutions", "overrides")


def _whole_numbers(v):
    """JSON numbers arrive as floats; keep whole numbers as ints (12, not 12.0)."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _whole_numbers(x) for k, x in v.items()}
    return v


def _plan_update(slug, changes):
    """(old_text, new_config, diff) for one config, validated; raises 400."""
    old_text = (CUSTOMERS_DIR / f"{slug}.json").read_text(encoding="utf-8")
    customer = json.loads(old_text)
    diff = {k: {"old": customer.get(k), "new": v} for k, v in changes.items() if customer.get(k) != v}
    customer.update(changes)
    errors = cc.validate(customer)
    if errors:
        raise HTTPException(status_code=400, detail={
            "message": f"Not saved ({slug}): " + "; ".join(errors), "errors": [f"{slug}: {e}" for e in errors]})
    return old_text, customer, diff


@app.patch("/customers/{slug}")
def update_customer(slug: str, update: CustomerUpdate):
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    changes = update.model_dump(exclude_unset=True)
    also_sibling = changes.pop("apply_to_sibling", False)
    changes = {k: _whole_numbers(v) for k, v in changes.items()}
    if not changes:
        raise HTTPException(status_code=400, detail="No editable fields provided")

    # Validate every target before writing any, so a refused sibling leaves
    # both configs untouched. Same validation and in-place write as the CLI.
    plans = [(slug, *_plan_update(slug, changes))]
    sibling = cc.sibling(slug)
    if also_sibling and sibling:
        # Copy only what actually changed here - and for overrides only the
        # changed keys - so the other period keeps its own settings (e.g. a
        # Future file's overrides.label_discount_percentage).
        diff = plans[0][3]
        sib = load_customer(sibling)
        sib_changes = {}
        for k in diff:
            if k not in SIBLING_FIELDS:
                continue
            if k == "overrides":
                old_o, new_o = diff[k]["old"] or {}, diff[k]["new"] or {}
                merged = dict(sib.get("overrides") or {})
                for ok in set(old_o) | set(new_o):
                    if ok not in new_o:
                        merged.pop(ok, None)
                    elif old_o.get(ok) != new_o[ok]:
                        merged[ok] = new_o[ok]
                sib_changes[k] = merged
            else:
                sib_changes[k] = diff[k]["new"]
        if sib_changes:
            plans.append((sibling, *_plan_update(sibling, sib_changes)))
    result = None
    for target, old_text, customer, diff in plans:
        if target == slug:
            result = customer
        if not diff:
            continue
        cc.write_config(target, customer, old_text)
        append_audit(
            target, _requested_by(),
            f"web UI: update config field(s) {', '.join(sorted(diff))}" + (f" (applied from {slug})" if target != slug else ""),
            "success", diff, action="update_customer_config", source="business",
        )
    return {**result, "_updated": [t for t, _, _, d in plans if d]}


class NewCustomer(BaseModel):
    slug: str
    like: str
    display_name: str
    discount_percentage: float | None = None
    price_valid_from: str | None = None
    price_valid_until: str | None = None
    customer_id: str | None = None
    sales_region: str | None = None
    note: str = ""


@app.post("/customers")
def create_customer(body: NewCustomer):
    """New customer as a copy of an existing one (customer_config.create_customer)."""
    if body.like not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{body.like}'")
    try:
        new, notes = cc.create_customer(
            body.slug.strip(), like=body.like, display_name=body.display_name, requested_by=_requested_by(),
            request=body.note, discount=_whole_numbers(body.discount_percentage), valid_from=body.price_valid_from,
            valid_until=body.price_valid_until, customer_id=body.customer_id or None,
            sales_region=body.sales_region or None, source="business")
    except cc.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": body.slug.strip(), "config": new, "notes": notes}


@app.get("/customers/{slug}/history")
def customer_history(slug: str):
    """Business changes (old -> new) and a count of engineering runs, plus the
    config file's commits (customer_config.history)."""
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    return cc.history(slug)


@app.get("/customers/{slug}/snapshot")
def customer_snapshot_cells(slug: str):
    """How many of this customer's cells are copied from the real file
    (master/real_cell_snapshot.csv), per column."""
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    cells = customer_snapshot(load_customer(slug).get("display_name", ""))
    by_col = {}
    for label, _sku in cells:
        by_col[label] = by_col.get(label, 0) + 1
    return {"cells": len(cells), "skus": len({s for _, s in cells}),
            "by_column": dict(sorted(by_col.items(), key=lambda kv: -kv[1]))}


@app.post("/customers/{slug}/generate")
def generate_customer_catalog(slug: str, body: GenerateRequest):
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    _refuse_if_code_stale()
    # A real generate writes catalogs/{slug}/ and prunes older files, which
    # could collide with a running regenerate job (and doubles the memory load
    # while a scoreboard runs). Dry runs write nothing, so they stay allowed.
    if not body.dry_run:
        running = jobs.current()
        if running is not None:
            raise HTTPException(status_code=409, detail={
                "message": (
                    f"A background {running['type']} job is running (started by {running['requested_by']}, "
                    f"{running['done']}/{running['total'] or '?'} done). Generating is paused until it "
                    "finishes or is cancelled; Preview (dry run) still works."
                ),
                "running": running,
            })

    result = generate(slug, _requested_by(), body.note or "generated via web UI", body.dry_run, source="business")
    if result["errors"]:
        raise HTTPException(status_code=422, detail=result)
    return result


@app.get("/customers/{slug}/pending-changes")
def get_pending_changes(slug: str):
    """What would change if this customer were generated right now: config
    edits made since the last successful generate, plus a dry-run summary
    (row-count delta, warnings, blocking errors). Backs the Generate button's
    review-before-you-commit panel; never writes anything itself."""
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")

    entries = [e for e in _all_audit_entries() if e.get("customer") == slug]
    generates = [e for e in entries if e.get("action") == "generate_catalog"]
    successes = [e for e in generates if e.get("outcome") == "success"]
    last_success = max(successes, key=_audit_dt) if successes else None

    # (a) config edits since that last successful generate (or ever, if there
    # was none) - compared as datetimes since the log has mixed UTC offsets.
    config_changes = sorted(
        (
            e for e in entries
            if e.get("action") == "update_customer_config"
            and (last_success is None or _audit_dt(e) > _audit_dt(last_success))
        ),
        key=_audit_dt,
    )

    last_rows = None
    last_file = None
    if last_success:
        detail = last_success.get("detail")
        if isinstance(detail, dict):
            last_rows = detail.get("rows_written")
        files = last_success.get("files_written") or []
        last_file = files[0] if files else None

    # (b) a dry run: row count vs. the last generate's rows_written, plus
    # whatever warnings/errors a real generate would hit right now.
    _refuse_if_code_stale()
    result = generate(slug, _requested_by(), "pending-changes check (dry run)", dry_run=True)
    new_rows = result["rows"]
    delta = delta_pct = None
    if last_rows:
        delta = new_rows - last_rows
        delta_pct = round(100 * delta / last_rows, 1)

    return {
        "slug": slug,
        "config_changes": config_changes,
        # (c) when the last generate happened, and which file it wrote.
        "last_generate": {
            "timestamp": last_success.get("timestamp") if last_success else None,
            "file_path": last_file,
            "rows_written": last_rows,
        },
        "dry_run": {
            "ok": result["ok"],
            "rows": new_rows,
            "errors": result["errors"],
            "warnings": result["warnings"],
        },
        "row_count": {
            "previous": last_rows,
            "new": new_rows,
            "delta": delta,
            "delta_pct": delta_pct,
            "regression": abs(delta_pct) >= 10 if delta_pct is not None else False,
        },
    }


@app.get("/customers/{slug}/catalogs")
def get_customer_catalogs(slug: str):
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    return _catalog_files(slug)


@app.get("/customers/{slug}/compare")
def compare_customer_versions(slug: str, to: str | None = None, from_: str | None = Query(default=None, alias="from")):
    """compare_versions.py's business-terms report for two kept versions
    (default: the newest vs the one before it; `to`/`from` = a filename or
    date). Runs the script as a subprocess so the two workbooks' memory is
    freed afterwards; read-only."""
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    cmd = [sys.executable, str(ROOT / "scripts" / "compare_versions.py"), slug]
    if to:
        cmd += ["--to", to]
    if from_:
        cmd += ["--from", from_]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Comparison took longer than 10 minutes")
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=(proc.stderr.strip().splitlines() or ["compare failed"])[-1])
    return {"report": proc.stdout}


class ExclusionChange(BaseModel):
    skus: list[str]
    action: str  # "exclude" | "include"
    reason: str = ""
    note: str = ""
    apply_to_sibling: bool = False


@app.get("/customers/{slug}/exclusions")
def get_exclusions(slug: str):
    """The customer's own excluded SKUs (config/exclusions/<slug>.csv) plus
    the shared lists it uses and how many customers share each."""
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    own = customer_exclusions(slug)
    return {
        "own": [own[k] for k in sorted(own)],
        "shared": [{"file": name, "customers": len(users)} for name, users in cc.shared_exclusion_lists(slug)],
        "sibling": cc.sibling(slug),
    }


@app.post("/customers/{slug}/exclusions")
def change_customer_exclusions(slug: str, body: ExclusionChange):
    if slug not in _customer_slugs():
        raise HTTPException(status_code=404, detail=f"Unknown customer '{slug}'")
    if body.action not in ("exclude", "include"):
        raise HTTPException(status_code=400, detail="action must be 'exclude' or 'include'")
    targets = [slug] + ([cc.sibling(slug)] if body.apply_to_sibling and cc.sibling(slug) else [])
    try:
        for t in targets:  # validation is identical for both, so a refusal happens on the first
            changed = cc.change_exclusions(
                t, body.skus, include=body.action == "include", reason=body.reason,
                requested_by=_requested_by(), request=body.note or f"web UI: {body.action} {', '.join(body.skus)}",
                source="business")
            if t == slug:
                result = changed
    except cc.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"changed": result, "also": targets[1:], **get_exclusions(slug)}


@app.get("/catalogs/{slug}/{filename}")
def download_catalog(slug: str, filename: str):
    path = CATALOGS_DIR / slug / filename
    try:
        path = path.resolve()
        path.relative_to((CATALOGS_DIR / slug).resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/checks")
def list_checks():
    return CHECK_REGISTRY


@app.get("/templates")
def list_templates():
    return [load_template(name) for name in _template_names()]


@app.get("/templates/{name}")
def get_template(name: str):
    if name not in _template_names():
        raise HTTPException(status_code=404, detail=f"Unknown template '{name}'")
    return load_template(name)


@app.get("/master")
def list_master(
    search: str | None = None,
    flagged_only: bool = False,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
):
    all_rows = load_master(["products.csv", "products_neb.csv"])
    dupes = _duplicate_skus(all_rows)
    rows = _filter_master_rows(all_rows, search, flagged_only, dupes)
    total = len(rows)
    page = [{**r, "flags": _row_flags(r, dupes)} for r in rows[offset : offset + limit]]
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


@app.get("/master/export")
def export_master(search: str | None = None, flagged_only: bool = False):
    all_rows = load_master(["products.csv", "products_neb.csv"])
    dupes = _duplicate_skus(all_rows)
    rows = _filter_master_rows(all_rows, search, flagged_only, dupes)
    if not rows:
        raise HTTPException(status_code=404, detail="No matching products to export")

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    filename = f"master_export_{date.today().isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/master/{sku}")
def get_master_row(sku: str):
    all_rows = load_master(["products.csv", "products_neb.csv"])
    dupes = _duplicate_skus(all_rows)
    for row in all_rows:
        if row.get("sku") == sku:
            return {**row, "flags": _row_flags(row, dupes)}
    raise HTTPException(status_code=404, detail=f"Unknown SKU '{sku}'")


def _audit_dt(e):
    """An audit entry's timestamp as an aware datetime. Entries carry an
    explicit offset ('+00:00' before 2026-09-26, local '-04:00' etc. after),
    so they must be compared as datetimes, not strings."""
    t = e.get("timestamp") or ""
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _audit_local_date(e):
    return _audit_dt(e).astimezone().date().isoformat()


@app.get("/audit")
def get_audit(
    customer: str | None = None,
    outcome: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    limit: int = Query(default=200, le=2000),
):
    # Every entry carries `source` (business/engineering); older entries are
    # classified on read by audit_source().
    entries = [{**e, "source": audit_source(e)} for e in _all_audit_entries()]
    if source:
        entries = [e for e in entries if e["source"] == source]

    if customer:
        entries = [e for e in entries if e.get("customer") == customer]
    if outcome:
        entries = [e for e in entries if e.get("outcome") == outcome]
    if date_from:
        entries = [e for e in entries if _audit_local_date(e) >= date_from]
    if date_to:
        entries = [e for e in entries if _audit_local_date(e) <= date_to]

    entries.sort(key=_audit_dt, reverse=True)
    return entries[:limit]


# ---- Fidelity scoreboard (read-only view of reports/fidelity/latest.json) ----
_fidelity_cache = {"mtime": None, "report": None}


def _load_fidelity_report():
    """latest.json, re-read only when its mtime changes (it's ~1.6 MB).
    Returns None if the scoreboard has never been run."""
    try:
        mtime = FIDELITY_REPORT.stat().st_mtime
    except FileNotFoundError:
        _fidelity_cache.update(mtime=None, report=None)
        return None
    if _fidelity_cache["mtime"] != mtime:
        with open(FIDELITY_REPORT, encoding="utf-8") as f:
            _fidelity_cache.update(mtime=mtime, report=json.load(f))
    return _fidelity_cache["report"]


def _require_fidelity_report():
    report = _load_fidelity_report()
    if report is None:
        raise HTTPException(status_code=404, detail=FIDELITY_MISSING)
    return report


def _template_rank_key(item):
    # Same order as latest.md: needs_work first, then most wrong columns,
    # then most structural issues, then unexplained cells.
    name, t = item
    return (t.get("bucket") == "clean", -len(t.get("wrong_columns", [])),
            -len(t.get("structural", [])), -(t.get("unexplained_cells") or 0), name)


def _customer_fidelity_summary(r):
    wrong = wrong_columns(r)
    return {
        "slug": r["slug"],
        "template": r.get("template"),
        "status": r["status"],
        "real_file": r.get("real_file"),
        "real_rows": r.get("real_rows"),
        "gen_rows": r.get("gen_rows"),
        "wrong_columns": wrong,
        "wrong_column_count": len(wrong),
        "unexplained_cells": r.get("unexplained_cells", 0) or 0,
        "structural": r.get("structural", []),
        "error": r.get("error"),
    }


@app.get("/fidelity/meta")
def get_fidelity_meta():
    """Cheap staleness check for the frontend: just the latest report's
    timestamp, so the page reloads the ~1.6 MB summary only when it changed."""
    report = _require_fidelity_report()
    return {"generated_at": report.get("generated_at")}


@app.get("/fidelity")
def get_fidelity_summary():
    report = _require_fidelity_report()
    results = report.get("results", [])
    templates = [
        {"name": name, **t}
        for name, t in sorted(report.get("templates", {}).items(), key=_template_rank_key)
    ]
    status_counts, bucket_counts = {}, {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    for t in templates:
        bucket_counts[t["bucket"]] = bucket_counts.get(t["bucket"], 0) + 1
    return {
        "generated_at": report.get("generated_at"),
        "requested_by": report.get("requested_by"),
        "targets": report.get("targets"),
        "status_counts": status_counts,
        "bucket_counts": bucket_counts,
        "templates": templates,
        "customers": {r["slug"]: _customer_fidelity_summary(r) for r in results},
    }


@app.get("/fidelity/{slug}")
def get_customer_fidelity(slug: str):
    report = _require_fidelity_report()
    result = next((r for r in report.get("results", []) if r["slug"] == slug), None)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Customer '{slug}' is not in the latest fidelity report")
    return _customer_fidelity_detail(report, result)


# How many timestamped reports /fidelity/{slug}/recent looks through.
FIDELITY_RECENT_SCAN = 30


@app.get("/fidelity/{slug}/recent")
def get_customer_recent_fidelity(slug: str):
    """This customer's result from the NEWEST timestamped report that contains
    it (reports/fidelity/<ts>.json) - e.g. a single-customer "Re-check
    fidelity" run, which by design never updates latest.json. Same shape as
    /fidelity/{slug}, plus `report_file` and `partial` (not a full run)."""
    for path in sorted(FIDELITY_REPORT_DIR.glob("2*.json"), reverse=True)[:FIDELITY_RECENT_SCAN]:
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            continue
        result = next((r for r in report.get("results", []) if r["slug"] == slug), None)
        if result is not None:
            return {
                **_customer_fidelity_detail(report, result),
                "report_file": str(path.relative_to(ROOT)),
                "partial": report.get("targets") != "all",
            }
    raise HTTPException(status_code=404, detail=f"No timestamped fidelity report contains '{slug}'")


def _customer_fidelity_detail(report, result):
    columns = {}
    for label, c in result.get("columns", {}).items():
        skus = c.get("unexplained_skus")
        columns[label] = {
            **{k: v for k, v in c.items() if k != "unexplained_skus"},
            "unexplained": c["total"] - c["coverage_gap"] - c["accepted"],
            # None = more than the scoreboard's per-column limit (count only).
            "unexplained_sku_count": len(skus) if skus is not None else None,
            "unexplained_sku_sample": (skus or [])[:FIDELITY_SKU_SAMPLE],
        }
    return {
        **{k: v for k, v in result.items() if k != "columns"},
        **_customer_fidelity_summary(result),
        "columns": columns,
        "generated_at": report.get("generated_at"),
    }


# ---- Background jobs (regenerate all / fidelity scoreboard, see jobs.py) ----
jobs = JobRunner()  # loads webapp/backend/jobs_state.json; marks leftover active jobs interrupted


@app.on_event("shutdown")
def _stop_jobs_on_shutdown():
    jobs.shutdown()


@app.post("/jobs")
def start_job(body: JobRequest):
    known = _customer_slugs()
    if body.slugs is not None:
        if not body.slugs:
            raise HTTPException(status_code=400, detail="slugs is empty (omit it to run every customer)")
        unknown = sorted(set(body.slugs) - set(known))
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown customer(s): {', '.join(unknown)}")
    if body.type in ("regenerate", "refresh"):
        _refuse_if_code_stale()
    if body.type == "regenerate":
        params = {"slugs": body.slugs or known, "dry_run": body.dry_run, "note": body.note}
    elif body.type == "scoreboard":
        # None = full run ('all'), the only kind that updates latest.json.
        params = {"slugs": body.slugs}
    elif body.type == "refresh":
        # Phase 1 (regenerate) always needs an explicit slug list; phase 2
        # (scoreboard --from-catalogs) always runs 'all' regardless, since a
        # partial refresh wouldn't update latest.json anyway.
        params = {"slugs": body.slugs or known, "note": body.note}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown job type '{body.type}' (expected 'regenerate', 'scoreboard' or 'refresh')")
    try:
        return jobs.start(body.type, params, _requested_by())
    except JobConflict as e:
        raise HTTPException(status_code=409, detail={"message": str(e), "running": e.running})


@app.get("/jobs")
def list_jobs():
    return jobs.list()


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_id}' (only the last 20 finished jobs are kept)")
    return job


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_id}'")
    return job


# Mounted last so it only catches paths none of the API routes above matched.
app.mount("/", StaticFiles(directory=WEBAPP_DIR / "frontend", html=True), name="frontend")
