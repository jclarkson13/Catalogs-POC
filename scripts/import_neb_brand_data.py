#!/usr/bin/env python3
"""Import a New England Biolabs (NEB-brand) product data snapshot into
master/products_neb.csv.

This is a second, separate master dataset alongside master/products.csv
(which is CST-brand only). It exists because incoming/Catalogs/NEB/DSM.xlsx
turned out to contain New England Biolabs products with zero SKU overlap
with CST's catalog - a different manufacturer's product line distributed
through the same Benelux catalog pipeline, not a CST catalog. Maps onto the
same 19-column shape as master/products.csv so generate_catalog.py's
existing checks/expressions work unchanged against either master source.

Usage: python3 scripts/import_neb_brand_data.py <input.pkl> [--requested-by X] [--dry-run]
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER_CSV = ROOT / "master" / "products_neb.csv"
AUDIT_DIR = Path(os.environ["CATALOG_AUDIT_DIR"]) if os.environ.get("CATALOG_AUDIT_DIR") else ROOT / "audit"  # override: agents in worktrees log to the main checkout

MASTER_COLUMNS = [
    "sku", "manufacturer", "short_description", "long_description",
    "list_price_usd", "list_price_eur", "currency", "uom", "size", "size_unit", "unspsc",
    "assays", "shipping_conditions", "storage_temperature",
    "product_classification", "msds_url", "product_url", "image_url",
    "cas_number",
]

SIZE_PATTERN = re.compile(r"^([\d.,]+)\s*(.*)$")


def to_str(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def map_row(row, line_no):
    sku = to_str(row["sku"]).strip()

    size_match = SIZE_PATTERN.match(to_str(row["unit_size"]).strip())
    size, size_unit = (size_match.group(1), size_match.group(2)) if size_match else ("", to_str(row["unit_size"]).strip())

    price = to_str(row["sale_price"]).strip()

    return {
        "sku": sku,
        "manufacturer": "New England Biolabs",
        "short_description": to_str(row["product_name_short"]).strip(),
        "long_description": to_str(row["product_name_short"]).strip(),
        "list_price_usd": f"{float(price):.2f}",
        "list_price_eur": f"{float(price):.2f}",
        "currency": "USD",
        "uom": to_str(row["uom"]).strip(),
        "size": size,
        "size_unit": size_unit,
        "unspsc": to_str(row["unspsc"]).strip(),
        "assays": "",
        "shipping_conditions": to_str(row["shipping_conditions"]).strip(),
        "storage_temperature": to_str(row["storage_temp"]).strip(),
        "product_classification": to_str(row["product_type"]).strip(),
        "msds_url": "",
        "product_url": to_str(row["manufacturer_url"]).strip(),
        "image_url": "",
        "cas_number": "",
    }


def write_audit(outcome, detail, requested_by, source_path):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    entry = {
        # Local time with explicit offset (see generate_catalog.audit_timestamp).
        "timestamp": datetime.now().astimezone().isoformat(),
        "who": requested_by,
        "action": "import_neb_brand_data",
        "customer": None,
        "request": f"import {source_path}",
        "outcome": outcome,
        "detail": detail,
    }
    audit_path = AUDIT_DIR / f"{now.date().isoformat()}.jsonl"
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_pkl")
    parser.add_argument("--requested-by", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import os
    import pandas as pd

    requested_by = args.requested_by or os.environ.get("USER", "unknown")
    source_path = args.input_pkl

    warnings = []
    errors = []
    mapped_rows = []
    seen_skus = set()

    df = pd.read_pickle(source_path)
    for i, row in enumerate(df.to_dict("records"), start=2):
        sku = to_str(row["sku"]).strip()
        if not sku:
            errors.append(f"row {i}: blank sku")
            continue
        if sku in seen_skus:
            errors.append(f"row {i}: duplicate sku {sku!r}")
            continue
        seen_skus.add(sku)
        try:
            mapped_rows.append(map_row(row, i))
        except Exception as e:
            errors.append(f"row {i} ({sku}): {e}")

    if errors:
        write_audit("failed", {"errors": errors, "warnings": warnings}, requested_by, source_path)
        print(f"FAILED: {len(errors)} error(s), no file written", file=sys.stderr)
        for e in errors[:20]:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    old_sku_count = 0
    if MASTER_CSV.exists():
        with open(MASTER_CSV, newline="", encoding="utf-8") as f:
            old_sku_count = sum(1 for _ in csv.DictReader(f))

    print(f"Parsed {len(mapped_rows)} rows from {source_path}")
    print(f"Current {MASTER_CSV.name} has {old_sku_count} rows")
    print(f"Warnings: {len(warnings)}")

    if args.dry_run:
        print("Dry run — master/products_neb.csv not modified")
        write_audit("dry_run", {"rows_parsed": len(mapped_rows), "warnings": warnings}, requested_by, source_path)
        return

    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_COLUMNS)
        writer.writeheader()
        writer.writerows(mapped_rows)

    print(f"Wrote {len(mapped_rows)} rows to {MASTER_CSV}")
    write_audit(
        "success",
        {"rows_written": len(mapped_rows), "previous_row_count": old_sku_count, "warnings": warnings},
        requested_by,
        source_path,
    )


if __name__ == "__main__":
    main()
