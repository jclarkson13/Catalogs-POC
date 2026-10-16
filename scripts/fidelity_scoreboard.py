#!/usr/bin/env python3
"""Fidelity scoreboard: regenerate customers' catalogs into a temp dir and
compare each against its real source file under incoming/Catalogs/.

Nothing is written to catalogs/; one summary audit entry is appended
(action "fidelity_scoreboard"). Quality checks are not run - this measures
fidelity to the real file only; use generate_catalog.py for checks.

Each value difference is labelled with a likely cause, so only novel ones
need a human look:
  coverage_gap   CST master SKU absent from incoming/cst_data_2.csv (known class)
  accepted       listed in config/fidelity_accepted.json (documented,
                 unfixable residual: exact template + column + SKU)
  rounding       both numeric, differ by exactly 0.01
  number_format  numerically equal, formatted differently ('614' vs '614.00')
  whitespace     equal once whitespace is collapsed
  other          anything else

Each template lands in exactly one bucket: clean (every customer matches, or
differs only by coverage gaps / accepted residuals) or needs_work (anything
else, including templates with no real file to verify against).

    python3 scripts/fidelity_scoreboard.py all [--workers 3]
    python3 scripts/fidelity_scoreboard.py <slug> [<slug> ...]
    python3 scripts/fidelity_scoreboard.py --template sap-gdsn-price-de
    python3 scripts/fidelity_scoreboard.py all --from-catalogs   # right after a full regenerate

Writes reports/fidelity/<timestamp>.json and reports/fidelity/latest.{json,md};
latest.md includes changes since the previous latest.json.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.compare_catalog import compare  # noqa: E402
from scripts.generate_catalog import (  # noqa: E402
    CATALOGS_DIR, CUSTOMERS_DIR, TEMPLATES_DIR, append_audit, load_customer, load_template,
    output_extension, prepare_rows, write_output,
)

REAL_DIR = ROOT / "incoming" / "Catalogs"
FILE_MAP = ROOT / "config" / "fidelity_file_map.json"
ACCEPTED = ROOT / "config" / "fidelity_accepted.json"
REPORT_DIR = ROOT / "reports" / "fidelity"
CST_DATA_2 = ROOT / "incoming" / "cst_data_2.csv"
CATEGORIES = ("coverage_gap", "accepted", "rounding", "number_format", "whitespace", "other")
UNEXPLAINED_SKU_LIMIT = 2000  # per column; larger lists are summarised by count only
EXAMPLES_PER_COLUMN = 3

_cst2_skus = None


def load_cst2_skus():
    """SKUs that count as coverage gaps: CST master SKUs (master/products.csv)
    absent from cst_data_2.csv. NEB SKUs are never in that CST export, so they
    are not gaps - their differences stay visible."""
    with open(CST_DATA_2, encoding="utf-8-sig", newline="") as f:
        in_cst2 = {r["sku"] for r in csv.DictReader(f)}
    with open(ROOT / "master" / "products.csv", encoding="utf-8-sig", newline="") as f:
        return frozenset(r["sku"] for r in csv.DictReader(f) if r["sku"] not in in_cst2)


def _init_worker(skus):
    # The parent builds the coverage-gap SKU set once and hands it to each worker.
    global _cst2_skus
    _cst2_skus = skus


_accepted = None


def accepted_residuals():
    """{(template, column): set(skus)} from config/fidelity_accepted.json."""
    global _accepted
    if _accepted is None:
        _accepted = {}
        entries = json.loads(ACCEPTED.read_text()).get("entries", []) if ACCEPTED.exists() else []
        for e in entries:
            _accepted.setdefault((e["template"], e["column"]), set()).update(e["skus"])
    return _accepted


def cst2_skus():
    global _cst2_skus
    if _cst2_skus is None:
        _cst2_skus = load_cst2_skus()
    return _cst2_skus


def _norm_name(name):
    # 'DE - Bayer Current Price (3)' -> 'de - bayer current price'
    return re.sub(r"(\s*\(\d+\))+$", "", name).strip().lower()


def real_file_index():
    index = {}
    for p in REAL_DIR.rglob("*"):
        if p.is_file() and not p.name.startswith(("~$", ".")) and p.suffix.lower() in (".xlsx", ".csv", ".txt"):
            index.setdefault(_norm_name(p.stem), []).append(p)
    return index


def find_real_file(slug, customer, index, file_map):
    """(path | None, how). how: 'map', 'display_name', 'none'."""
    if slug in file_map:
        p = file_map[slug]
        return (ROOT / p if p else None), "map"
    cands = index.get(_norm_name(customer.get("display_name", "")), [])
    if not cands:
        return None, "none"
    ext = "." + output_extension(customer)
    same_ext = [p for p in cands if p.suffix.lower() == ext]
    pick = (same_ext or cands)
    return sorted(pick)[0], "display_name"


def _dec(v):
    try:
        return Decimal(str(v).strip().lstrip("$€£"))
    except (InvalidOperation, ValueError):
        return None


def classify(sku, real, gen, accepted=frozenset()):
    if sku in cst2_skus():
        return "coverage_gap"
    if sku in accepted:
        return "accepted"
    a, b = _dec(real), _dec(gen)
    if a is not None and b is not None:
        if a == b:
            return "number_format"
        if abs(a - b) == Decimal("0.01"):
            return "rounding"
    if " ".join(str(real).split()) == " ".join(str(gen).split()):
        return "whitespace"
    return "other"


def fresh_catalog(slug, customer):
    """The customer's newest generated catalog, if nothing that shapes it
    (customer config, template, master/lookup CSVs, the generator) changed
    since it was written; else None, and the caller regenerates."""
    files = sorted((CATALOGS_DIR / slug).glob(f"*.{output_extension(customer)}"))
    if not files:
        return None
    newest = max(files, key=lambda f: f.stat().st_mtime)
    inputs = [CUSTOMERS_DIR / f"{slug}.json", TEMPLATES_DIR / f"{customer['template_ref']}.json",
              ROOT / "scripts" / "generate_catalog.py", *(ROOT / "master").glob("*.csv"),
              ROOT / "config" / "exclusions" / f"{slug}.csv"]
    if newest.stat().st_mtime < max(p.stat().st_mtime for p in inputs if p.exists()):
        return None
    return newest


def score_customer(slug, real_path, from_catalogs=False):
    """Worker: compare a generated catalog against the real file - the fresh
    one in catalogs/ with from_catalogs, else regenerated into a temp dir.
    Never raises."""
    out = {"slug": slug, "real_file": str(real_path.relative_to(ROOT))}
    try:
        customer = load_customer(slug)
        template = load_template(customer["template_ref"])
        out["template"] = template["template_name"]
        with tempfile.TemporaryDirectory() as tmp:
            gen_path = fresh_catalog(slug, customer) if from_catalogs else None
            out["generated_from"] = "catalog" if gen_path else "regenerated"
            if gen_path is None:
                _, rows = prepare_rows(customer, template, slug)
                gen_path = Path(tmp) / f"{slug}.{output_extension(customer)}"
                write_output(rows, template, customer, gen_path)
            headerless = (template.get("csv_format") or {}).get("header_row", True) is False
            res = compare(str(real_path), str(gen_path), template.get("sku_column"),
                          header=[c["header"] for c in template["columns"]] if headerless else None,
                          delimiter=template.get("delimiter"), sheet=template.get("sheet_name"))
    except Exception as e:  # report and move on; one bad config shouldn't stop the run
        out.update(status="error", error=f"{type(e).__name__}: {e}")
        return out

    acc = accepted_residuals()
    tname = template["template_name"]
    columns = {}
    for label, diffs in res["value_diffs"].items():
        counts = dict.fromkeys(CATEGORIES, 0)
        examples, skus = [], []
        col_accepted = acc.get((tname, label), frozenset())
        for sku, rv, gv in diffs:
            cat = classify(sku, rv, gv, col_accepted)
            counts[cat] += 1
            if cat not in ("coverage_gap", "accepted"):
                skus.append(sku)
                if len(examples) < EXAMPLES_PER_COLUMN:
                    examples.append({"sku": sku, "real": str(rv)[:200], "gen": str(gv)[:200], "cause": cat})
        columns[label] = {"total": len(diffs), **counts, "examples": examples,
                          "unexplained_skus": skus if len(skus) <= UNEXPLAINED_SKU_LIMIT else None}

    unexplained = sum(c["total"] - c["coverage_gap"] - c["accepted"] for c in columns.values())
    structural = []
    if res["real_sheets"] and res["gen_sheets"] and len(res["real_sheets"]) != len(res["gen_sheets"]):
        structural.append(f"sheets: real {len(res['real_sheets'])} ({', '.join(res['real_sheets'])}) vs generated {len(res['gen_sheets'])}")
    if res["real_headerless"]:
        structural.append("real file has no header row; generated writes one")
    elif res["header_diffs"]:
        structural.append(f"header: {len(res['header_diffs'])} position(s) differ")
    if res["framing"]:
        structural.append("csv framing differs")
    if res["sku_missing"]:
        structural.append("SKU column missing; rows not aligned")
    if res["only_real"] or res["only_gen"]:
        structural.append(f"rows: {len(res['only_real'])} only in real, {len(res['only_gen'])} only in generated")
    # Cell-type differences on coverage-gap SKUs are that same known gap, not structural.
    gaps = cst2_skus()
    type_diffs = {}
    for label, by_type in res["type_diff_skus"].items():
        col_accepted = acc.get((tname, label), frozenset())
        kept = {f"{rt}->{gt}": sum(1 for k in skus if k not in gaps and k not in col_accepted)
                for (rt, gt), skus in by_type.items()}
        kept = {t: n for t, n in kept.items() if n}
        if kept:
            type_diffs[label] = kept
    if type_diffs:
        structural.append(f"cell types differ in {len(type_diffs)} column(s)")

    out.update(
        status="match" if res["ok"] else ("known_gaps_only" if not unexplained and not structural else "diffs"),
        real_rows=res["real_rows"], gen_rows=res["gen_rows"], common_rows=res["common"],
        real_cols=res["real_cols"], gen_cols=res["gen_cols"],
        header_diffs=res["header_diffs"][:10], framing=bool(res["framing"]),
        only_real=len(res["only_real"]), only_gen=len(res["only_gen"]),
        only_real_examples=res["only_real"][:5], only_gen_examples=res["only_gen"][:5],
        type_diffs=type_diffs,
        columns=columns, unexplained_cells=unexplained, structural=structural,
    )
    return out


def select_slugs(targets, template_name):
    all_slugs = sorted(p.stem for p in CUSTOMERS_DIR.glob("*.json"))
    if template_name:
        return [s for s in all_slugs if load_customer(s).get("template_ref") == template_name]
    if not targets or targets == ["all"]:
        return all_slugs
    unknown = [s for s in targets if s not in all_slugs]
    if unknown:
        raise SystemExit(f"Unknown customer(s): {', '.join(unknown)}")
    return targets


def _rank_key(r):
    order = {"error": 0, "diffs": 1, "no_real_file": 2, "known_gaps_only": 3, "match": 4}
    return (order.get(r["status"], 9), -len(r.get("structural", [])), -r.get("unexplained_cells", 0), r["slug"])


def wrong_columns(r):
    """Column labels with at least one unexplained cell - a better measure of
    remaining work than the cell count, which scales with row count."""
    return [k for k, c in r.get("columns", {}).items() if c["total"] - c["coverage_gap"] - c["accepted"]]


def template_buckets(results):
    """{template: {bucket, customers, statuses, unexplained_cells}}. clean =
    at least one customer verified against a real file, and every verified
    customer is match / known_gaps_only (customers with no real file don't
    count against it); otherwise needs_work - including templates with no
    real file for any customer, and errors."""
    out = {}
    for r in results:
        t = out.setdefault(r.get("template") or "?", {"customers": [], "statuses": {}, "unexplained_cells": 0,
                                                     "wrong_columns": set(), "structural": set()})
        t["customers"].append(r["slug"])
        t["statuses"][r["status"]] = t["statuses"].get(r["status"], 0) + 1
        t["unexplained_cells"] += r.get("unexplained_cells", 0) or 0
        t["wrong_columns"].update(wrong_columns(r))
        t["structural"].update(s.split(":")[0] for s in r.get("structural", []))
    for t in out.values():
        t["wrong_columns"] = sorted(t["wrong_columns"])
        t["structural"] = sorted(t["structural"])
        verified = set(t["statuses"]) - {"no_real_file"}
        t["bucket"] = "clean" if verified and verified <= {"match", "known_gaps_only"} else "needs_work"
    return out


def render_markdown(report, previous):
    results = report["results"]
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [
        f"# Fidelity scoreboard — {report['generated_at']}",
        "",
        f"Customers: {len(results)} · " + " · ".join(f"{k}: {v}" for k, v in sorted(counts.items())),
        "",
        "`known_gaps_only` = every differing cell is a coverage gap (CST SKU absent from `cst_data_2.csv`) "
        "or an accepted residual (`config/fidelity_accepted.json`). "
        "`unexplained` = differing cells that are neither.",
        "",
    ]
    buckets = report.get("templates") or template_buckets(results)
    n_clean = sum(1 for t in buckets.values() if t["bucket"] == "clean")
    lines += [f"## By template — {n_clean} clean, {len(buckets) - n_clean} needs work", "",
              "Ranked by wrong columns (columns with any unexplained cell, across the template's customers), "
              "then structural issues. The cell count scales with row count, so it overstates big catalogs.", "",
              "| Template | Bucket | Customers | Statuses | Wrong columns | Structural | Unexplained cells |",
              "|---|---|---|---|---|---|---|"]
    for name, t in sorted(buckets.items(), key=lambda kv: (kv[1]["bucket"] == "clean", -len(kv[1]["wrong_columns"]),
                                                            -len(kv[1]["structural"]), -kv[1]["unexplained_cells"], kv[0])):
        st = ", ".join(f"{k} {v}" for k, v in sorted(t["statuses"].items()))
        lines.append(f"| {name} | {t['bucket']} | {len(t['customers'])} | {st} | {len(t['wrong_columns'])} | "
                     f"{'; '.join(t['structural'])} | {t['unexplained_cells']} |")
    lines.append("")
    if previous:
        prev = {r["slug"]: r for r in previous.get("results", [])}
        changes = []
        for r in results:
            p = prev.get(r["slug"])
            if not p:
                continue
            if p["status"] != r["status"] or (p.get("unexplained_cells") or 0) != (r.get("unexplained_cells") or 0):
                changes.append(f"| {r['slug']} | {p['status']} ({p.get('unexplained_cells', 0)}) | "
                               f"{r['status']} ({r.get('unexplained_cells', 0)}) |")
        lines += [f"## Changes since {previous.get('generated_at', 'previous run')}", ""]
        lines += (["| Customer | Before (unexplained) | Now (unexplained) |", "|---|---|---|"] + changes
                  if changes else ["No changes."])
        lines.append("")

    lines += ["## By customer", "",
              "| Customer | Template | Status | Wrong columns | Unexplained cells | Structural | Worst columns |",
              "|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=_rank_key):
        worst = sorted(r.get("columns", {}).items(), key=lambda kv: -(kv[1]["total"] - kv[1]["coverage_gap"]))[:3]
        worst_txt = "; ".join(
            f"{k.splitlines()[-1] if k else k}: " + ", ".join(f"{c} {v[c]}" for c in CATEGORIES if v[c])
            for k, v in worst if v["total"] - v["coverage_gap"]
        )
        detail = r.get("error") or "; ".join(r.get("structural", []))
        lines.append(f"| {r['slug']} | {r.get('template', '')} | {r['status']} | {len(wrong_columns(r)) if 'columns' in r else ''} | "
                     f"{r.get('unexplained_cells', '')} | "
                     f"{detail} | {worst_txt} |".replace("\n", " "))
    return "\n".join(lines) + "\n"


REPORTS_KEEP = 30  # the web UI's /fidelity/{slug}/recent reads the newest 30


def prune_reports(keep=REPORTS_KEEP):
    """Keep the newest `keep` timestamped reports (json + md); latest.* is
    never touched. Added 2026-10-02 - every run added a pair and nothing
    removed them."""
    stamps = sorted({p.stem for p in REPORT_DIR.glob("20*.json")})
    for stamp in stamps[:-keep] if keep else stamps:
        for ext in ("json", "md"):
            (REPORT_DIR / f"{stamp}.{ext}").unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="'all' (default) or customer slugs")
    ap.add_argument("--template", help="only customers using this template")
    # 3, not more: write_xlsx holds a whole sheet in memory (the largest real
    # layouts are ~0.5-1 GB per worker) and this machine has 8 GB RAM.
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--from-catalogs", action="store_true",
                    help="compare each customer's newest catalogs/ file instead of regenerating, "
                         "when it's newer than its config, template, master CSVs and the generator "
                         "(use right after a full regenerate); stale or missing ones are regenerated")
    ap.add_argument("--requested-by", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--request", default="fidelity scoreboard run")
    a = ap.parse_args()

    slugs = select_slugs(a.targets, a.template)
    file_map = {k: v for k, v in json.loads(FILE_MAP.read_text()).items() if not k.startswith("_")}
    index = real_file_index()

    results, jobs = [], {}
    for slug in slugs:
        path, how = find_real_file(slug, load_customer(slug), index, file_map)
        if path is None:
            results.append({"slug": slug, "template": load_customer(slug).get("template_ref"),
                            "status": "no_real_file", "mapping": how})
        else:
            jobs[slug] = path

    started = datetime.now(timezone.utc)
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker, initargs=(load_cst2_skus(),)) as pool:
        futures = {pool.submit(score_customer, s, p, a.from_catalogs): s for s, p in jobs.items()}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            print(f"[{i}/{len(jobs)}] {r['slug']}: {r['status']}"
                  + (f" ({r['unexplained_cells']} unexplained)" if r.get("unexplained_cells") else "")
                  + (f" {r['error']}" if r.get("error") else "")
                  + (" [regenerated: catalog stale or missing]" if a.from_catalogs and r.get("generated_from") == "regenerated" else ""), flush=True)

    stamp = started.strftime("%Y-%m-%dT%H%M%SZ")
    report = {"generated_at": started.isoformat(timespec="seconds"), "requested_by": a.requested_by,
              "targets": slugs if a.targets not in ([], ["all"]) or a.template else "all",
              "results": sorted(results, key=_rank_key), "templates": template_buckets(results)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    latest_json = REPORT_DIR / "latest.json"
    previous = json.loads(latest_json.read_text()) if latest_json.exists() else None
    (REPORT_DIR / f"{stamp}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    # Only a full run replaces latest.*, so partial runs don't shrink the baseline.
    full = not a.template and (not a.targets or a.targets == ["all"])
    md = render_markdown(report, previous if full else None)
    (REPORT_DIR / f"{stamp}.md").write_text(md)
    if full:
        latest_json.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        (REPORT_DIR / "latest.md").write_text(md)
    prune_reports()

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    append_audit("all" if full else ",".join(slugs)[:200], a.requested_by, a.request,
                 "failed" if counts.get("error") else "success",
                 {"customers": len(results), "counts": counts, "report": f"reports/fidelity/{stamp}.json",
                  "warnings": []},
                 action="fidelity_scoreboard")
    print(f"\n{counts}\nReport: reports/fidelity/{stamp}.md")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
