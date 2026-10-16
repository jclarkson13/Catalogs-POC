"""Retire master/real_cell_snapshot.csv rows that master data now reproduces.

The snapshot holds cells COPIED from real catalog files where no source
reproduced them (see build_real_cell_snapshot.py). After a fresh CST export /
master import, many of those cells - above all the coverage-gap cells for SKUs
that were missing from cst_data_2.csv - should come from master data again.

For every customer that has snapshot rows, this rebuilds its rows with the
snapshot switched off (generate_catalog.SNAPSHOT_DISABLED) and compares each
snapshot cell with what the column's own fallback expression now produces, as
the catalog would write it (value, plus number-vs-text for xlsx). A row is
retired only when every customer sharing its display_name reproduces it
exactly; anything uncertain (generate error, unknown column, no such SKU) is
kept. Then records the master fingerprint the snapshot was reviewed against in
master/real_cell_snapshot.meta.json - which silences the generator's
"snapshot review" warning until master/*.csv changes again - and writes one
`retire_snapshot_rows` audit entry.

    python3 scripts/retire_snapshot_rows.py [--dry-run] [--workers 2] [--requested-by X] [--request "..."]

Typical flow after a new export: import_master_data.py (and rebuild the
lookups), retire_snapshot_rows.py, then a full refresh (generate all +
fidelity_scoreboard all --from-catalogs) to confirm nothing regressed. Rows
for display_names no customer uses any more are reported as orphans and
dropped only with --drop-orphans.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import generate_catalog as g  # noqa: E402

SNAPSHOT = ROOT / "master" / "real_cell_snapshot.csv"
META = ROOT / "master" / "real_cell_snapshot.meta.json"


def load_snapshot():
    with open(SNAPSHOT, newline="", encoding="utf-8") as f:
        return {r["key"]: (r["value"], r.get("type") or "") for r in csv.DictReader(f)}


def column_key(template, label):
    """Snapshot column label -> the build_rows() key for that column, or None."""
    cols = template["columns"]
    if label.endswith("]") and " [col " in label:
        i = int(label.rsplit(" [col ", 1)[1][:-1]) - 1
        return cols[i].get("key", cols[i]["header"]) if 0 <= i < len(cols) else None
    for c in cols:
        if c["header"] == label:
            return c.get("key", c["header"])
    return None


def reproduces(gen, value, kind, typed):
    """Would the catalog write exactly the snapshot cell?"""
    if kind in ("int", "float"):
        return isinstance(gen, (int, float)) and not isinstance(gen, bool) and float(gen) == float(value)
    if gen is None:
        gen = ""
    if typed and not isinstance(gen, str):  # xlsx: a number cell where the real cell is text
        return False
    return str(gen) == value


def check_slug(args):
    """Worker: (slug, set of snapshot keys this customer reproduces, error)."""
    slug, cells = args
    g.SNAPSHOT_DISABLED = True
    try:
        customer = g.load_customer(slug)
        template = g.load_template(customer["template_ref"])
        _, rows = g.prepare_rows(customer, template, slug)
    except (Exception, SystemExit) as e:
        return slug, set(), f"{type(e).__name__}: {e}"
    sku_key = column_key(template, template["sku_column"])
    by_sku = {r.get(sku_key): r for r in rows}
    typed = customer.get("file_type", "xlsx") in ("xlsx", "cif")
    ok = set()
    for key, (label, sku, value, kind) in cells.items():
        ck = column_key(template, label)
        row = by_sku.get(sku)
        if ck is None or row is None or ck not in row:
            continue
        if reproduces(row[ck], value, kind, typed):
            ok.add(key)
    return slug, ok, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--drop-orphans", action="store_true")
    ap.add_argument("--requested-by", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--request", default="")
    args = ap.parse_args()

    snap = load_snapshot()
    names = defaultdict(list)  # display_name -> slugs
    for p in sorted(g.CUSTOMERS_DIR.glob("*.json")):
        names[json.loads(p.read_text())["display_name"]].append(p.stem)

    per_name, orphans = defaultdict(dict), []
    for key, (value, kind) in snap.items():
        name, label, sku = key.split("|", 2) if key.count("|") == 2 else (None, None, None)
        if name is None:  # a "|" inside a column label: split from both ends
            name, rest = key.split("|", 1)
            label, sku = rest.rsplit("|", 1)
        if name not in names:
            orphans.append(key)
            continue
        per_name[name][key] = (label, sku, value, kind)

    jobs = [(slug, cells) for name, cells in per_name.items() for slug in names[name]]
    print(f"{len(snap)} snapshot rows, {len(per_name)} display names, {len(jobs)} customers to rebuild "
          f"(snapshot off), {len(orphans)} orphan rows", flush=True)
    ok_by_slug, errors = {}, {}
    with ProcessPoolExecutor(args.workers) as ex:
        for i, (slug, ok, err) in enumerate(ex.map(check_slug, jobs), 1):
            ok_by_slug[slug] = ok
            if err:
                errors[slug] = err
            print(f"[{i}/{len(jobs)}] {slug}: {len(ok)} reproducible" + (f" ERROR {err}" if err else ""), flush=True)

    retired = []
    for name, cells in per_name.items():
        slugs = names[name]
        for key in cells:
            if all(key in ok_by_slug.get(s, ()) for s in slugs):
                retired.append(key)
    if args.drop_orphans:
        retired += orphans
    by_col = defaultdict(int)
    for key in retired:
        by_col["|".join(key.split("|")[:2])] += 1

    print(f"\n{len(retired)} of {len(snap)} rows reproducible -> retire; {len(snap) - len(retired)} kept")
    for k, n in sorted(by_col.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  {n:6d}  {k}")
    if orphans and not args.drop_orphans:
        print(f"{len(orphans)} orphan rows kept (no customer has that display_name; --drop-orphans removes them)")
    if errors:
        print(f"{len(errors)} customers failed to build - their rows were kept: {', '.join(sorted(errors))}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    keep = {k: v for k, v in snap.items() if k not in set(retired)}
    with open(SNAPSHOT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "value", "type"])
        for k in sorted(keep):
            w.writerow([k, *keep[k]])
    g.SNAPSHOT_DISABLED = False
    meta = {
        "reviewed_at": g.audit_timestamp(),
        "master_fingerprint": g.master_fingerprint(),
        "rows_before": len(snap), "rows_after": len(keep), "retired": len(retired),
        "_note": "Written by scripts/retire_snapshot_rows.py; generate_catalog.check_snapshot_review() "
                 "warns when master/*.csv no longer matches master_fingerprint.",
    }
    META.write_text(json.dumps(meta, indent=1) + "\n")
    g.append_audit(
        "real_cell_snapshot", args.requested_by, args.request or "retire reproducible snapshot rows",
        "failed" if errors else "success",
        {"rows_before": len(snap), "rows_after": len(keep), "retired": len(retired),
         "retired_by_column": dict(by_col), "errors": [f"{s}: {e}" for s, e in sorted(errors.items())],
         "warnings": [f"{len(orphans)} orphan rows kept"] if orphans and not args.drop_orphans else []},
        action="retire_snapshot_rows",
    )
    print(f"wrote {SNAPSHOT.relative_to(ROOT)} ({len(keep)} rows) and {META.relative_to(ROOT)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
