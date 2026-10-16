"""What changed between two versions of one customer's catalog?

    python3 scripts/compare_versions.py <customer-slug> [--from DATE] [--to DATE] [--list] [--out FILE.csv]

Picks two files from catalogs/<slug>/ (kept as a rolling history, see
generate_catalog.CATALOG_HISTORY_KEEP): by default the newest and the one
before it. --from/--to take a date (YYYY-MM-DD) or a filename; a date picks
that day's file, or the newest one before it ("the one they got at the start
of last month"). --list shows the available versions.

Reports, in business terms: rows added/removed (by SKU), columns added/removed
or moved, how many products changed per column (with examples), price changes
(up/down, largest moves) for the template's price columns, cell-type changes
(e.g. number -> text), and the context - every audit entry for this customer
between the two dates, plus commits touching its config or template - so the
answer can say *why*, not just *what*. --out writes every changed cell
(sku, column, old, new) to a CSV. Read-only; nothing is generated or audited.
"""
import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.compare_catalog import compare  # noqa: E402
from scripts.generate_catalog import (  # noqa: E402
    AUDIT_DIR, CATALOGS_DIR, CUSTOMERS_DIR, TEMPLATES_DIR, audit_source, load_customer, load_template,
    output_extension,
)

EXAMPLES = 3


def versions(slug, customer):
    d = CATALOGS_DIR / slug
    return sorted(d.glob(f"*.{output_extension(customer)}")) if d.exists() else []


def file_date(p):
    return p.stem.rsplit("_", 1)[-1]


def pick(files, spec, default):
    if spec is None:
        return default
    for p in files:
        if p.name == spec:
            return p
    older = [p for p in files if file_date(p) <= spec]
    if not older:
        sys.exit(f"No version on or before {spec}; available: {', '.join(file_date(p) for p in files)}")
    return older[-1]


def num(v):
    try:
        return Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def audit_between(slug, start, end):
    """Audit entries for the customer from the day after `start` through `end` (dates)."""
    out = []
    for f in sorted(Path(AUDIT_DIR).glob("*.jsonl")):
        if not (start < f.stem <= end):
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("customer") == slug:
                    out.append(e)
    return out


def commits_between(paths, start, end):
    cmd = ["git", "-C", str(ROOT), "log", "--format=%as %h %s", f"--since={start} 23:59:59",
           f"--until={end} 23:59:59", "--", *map(str, paths)]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return []


def main():
    ap = argparse.ArgumentParser(description="What changed between two versions of a customer's catalog.")
    ap.add_argument("customer")
    ap.add_argument("--from", dest="from_", metavar="DATE_OR_FILE")
    ap.add_argument("--to", metavar="DATE_OR_FILE")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", help="write every changed cell to this CSV")
    a = ap.parse_args()

    customer = load_customer(a.customer)
    template = load_template(customer["template_ref"])
    files = versions(a.customer, customer)
    if a.list or len(files) < 2:
        print(f"{customer['display_name']} ({a.customer}): {len(files)} version(s) kept")
        for p in files:
            print(f"  {file_date(p)}  {p.relative_to(ROOT)}")
        if len(files) < 2 and not a.list:
            sys.exit("Need at least two versions to compare (older ones are pruned beyond the rolling history).")
        return

    new = pick(files, a.to, files[-1])
    old = pick([p for p in files if p < new], a.from_, None) if a.from_ else (
        [p for p in files if p < new] or [None])[-1]
    if old is None:
        sys.exit(f"No version older than {new.name}.")

    headerless = (template.get("csv_format") or {}).get("header_row", True) is False
    res = compare(str(old), str(new), template.get("sku_column"),
                  header=[c["header"] for c in template["columns"]] if headerless else None,
                  delimiter=template.get("delimiter"), sheet=template.get("sheet_name"))
    name = lambda h: h.replace("\n", " ").strip()  # noqa: E731

    print(f"# {customer['display_name']} ({a.customer})")
    print(f"Comparing {file_date(old)} ({res['real_rows']} rows) -> {file_date(new)} ({res['gen_rows']} rows)\n")

    # Layout
    if res.get("real_sheets") and res.get("gen_sheets") and res["real_sheets"] != res["gen_sheets"]:
        print(f"Sheets: {res['real_sheets']} -> {res['gen_sheets']}")
    if res["header_diffs"]:
        olds = [r for _, r, _ in res["header_diffs"] if r != "<none>"]
        news = [g for _, _, g in res["header_diffs"] if g != "<none>"]
        added = [h for h in news if h not in olds]
        removed = [h for h in olds if h not in news]
        print(f"Layout: {res['real_cols']} -> {res['gen_cols']} columns")
        if added:
            print(f"  added: {', '.join(name(h) for h in added)}")
        if removed:
            print(f"  removed: {', '.join(name(h) for h in removed)}")
        if not added and not removed:
            print("  same columns, different order")
    else:
        print(f"Layout: unchanged ({res['gen_cols']} columns)")
    if res["framing"]:
        print(f"File framing changed: {res['framing']}")

    # Rows
    added, removed = res["only_gen"], res["only_real"]
    print(f"\nProducts: {len(added)} added, {len(removed)} removed, {res['common']} in both")
    if added:
        print(f"  added: {', '.join(added[:20])}{' ...' if len(added) > 20 else ''}")
    if removed:
        print(f"  removed: {', '.join(removed[:20])}{' ...' if len(removed) > 20 else ''}")

    # Values
    prices = set(template.get("price_columns") or [])
    diffs = res["value_diffs"]
    if not diffs:
        print("\nValues: no changes on products in both versions")
    for label, rows in diffs.items():
        if label.split(" [col ")[0] in prices:
            ups = downs = fmt = other = 0
            moves = []
            for sku, ov, nv in rows:
                o, n = num(ov), num(nv)
                if o is None or n is None:
                    other += 1  # blank <-> value, or non-numeric
                    continue
                if n == o:
                    fmt += 1  # same amount, written differently ('415' -> '415.00')
                    continue
                ups += n > o
                downs += n < o
                if o:
                    moves.append(((n - o) / o * 100, sku, ov, nv))
            moves.sort(key=lambda m: -abs(m[0]))
            print(f"\nPrice '{name(label)}': {ups} up, {downs} down"
                  + (f", {fmt} same amount written differently" if fmt else "")
                  + (f", {other} added/blanked or non-numeric" if other else ""))
            for pct, sku, ov, nv in moves[:EXAMPLES]:
                print(f"  {sku}: {ov} -> {nv} ({pct:+.1f}%)")
    other = [(label, rows) for label, rows in diffs.items() if label.split(" [col ")[0] not in prices]
    if other:
        print(f"\nOther columns changed ({len(other)}):")
        for label, rows in sorted(other, key=lambda kv: -len(kv[1])):
            sku, ov, nv = rows[0]
            print(f"  {name(label)}: {len(rows)} products, e.g. {sku}: {str(ov)[:60]!r} -> {str(nv)[:60]!r}")
    if res["type_diffs"]:
        print("\nCell types changed (looks the same, matters for automated loads):")
        for label, by_type in res["type_diff_skus"].items():
            for (ot, nt), skus in by_type.items():
                print(f"  {name(label)}: {len(skus)} cells {ot} -> {nt}")

    # Why
    start, end = file_date(old), file_date(new)
    entries = audit_between(a.customer, start, end)
    business = [e for e in entries if audit_source(e) == "business"]
    engineering = [e for e in entries if audit_source(e) != "business"]
    print(f"\nBusiness changes for this customer after {start} through {end}:")
    for e in business:
        detail = json.dumps(e.get("detail"), ensure_ascii=False)[:140] if e.get("action") != "generate_catalog" else ""
        print(f"  {e.get('timestamp', '')[:16]} {e.get('who')}: {e.get('action')} ({e.get('outcome')}) "
              f"{e.get('request', '')[:100]} {detail}")
    if not business:
        print("  none")
    if engineering:
        last = engineering[-1]
        print(f"Engineering runs in that window: {len(engineering)} (latest {last.get('timestamp', '')[:10]} "
              f"{last.get('action')}: '{last.get('request', '')[:70]}')")
    commits = commits_between([CUSTOMERS_DIR / f"{a.customer}.json",
                               TEMPLATES_DIR / f"{customer['template_ref']}.json"], start, end)
    if commits:
        print(f"Config/template commits in that window ({len(commits)}):")
        for c in commits[:15]:
            print(f"  {c[:140]}")
    print("Note: master-data imports and generator changes also change catalogs; this lists only "
          "this customer's own audit entries and its config/template commits.")

    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["change", "sku", "column", "old", "new"])
            for sku in added:
                w.writerow(["added", sku, "", "", ""])
            for sku in removed:
                w.writerow(["removed", sku, "", "", ""])
            for label, rows in diffs.items():
                for sku, ov, nv in rows:
                    w.writerow(["changed", sku, name(label), ov, nv])
        print(f"\nEvery change written to {a.out}")


if __name__ == "__main__":
    main()
