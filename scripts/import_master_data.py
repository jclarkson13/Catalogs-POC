#!/usr/bin/env python3
"""Import a CST product data export into master/products.csv.

Maps the raw export's column names (which don't match master/products.csv's
schema, and in a couple of cases are mislabeled relative to their actual
content) onto the fields generate_catalog.py expects. See docs/reference.md's
"General" notes for why master/products.csv previously came from
sample catalogs rather than a direct data export.

Accepts either a .csv export or a .pkl/.pickle pandas DataFrame snapshot
(e.g. a DynamoDB table dump) of the same 112-column shape - the two US/EU
price fields (gbp, eu_list_price) are stored as plain strings in both.

Usage: python3 scripts/import_master_data.py <input.csv|input.pkl> [--requested-by X] [--dry-run]
"""
import argparse
import ast
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER_CSV = ROOT / "master" / "products.csv"
AUDIT_DIR = Path(os.environ["CATALOG_AUDIT_DIR"]) if os.environ.get("CATALOG_AUDIT_DIR") else ROOT / "audit"  # override: agents in worktrees log to the main checkout

MASTER_COLUMNS = [
    "sku", "manufacturer", "short_description", "long_description",
    "list_price_usd", "list_price_eur", "list_price_gbp",
    "list_price_usd_future", "list_price_eur_future", "list_price_gbp_future",
    "currency", "uom", "size", "size_unit", "unspsc",
    "assays", "shipping_conditions", "storage_temperature",
    "product_classification", "msds_url", "product_url", "image_url",
    "cas_number",
]

SIZE_PATTERN = re.compile(r"^([\d.]+)\s*(.*)$")
CAS_PATTERN = re.compile(r"\d{2,7}-\d{2}-\d")
WHITESPACE_RUN = re.compile(r"\s+")


def parse_dict_field(raw):
    if isinstance(raw, dict):
        return raw
    raw = (raw or "").strip()
    if not raw:
        return {}
    return ast.literal_eval(raw)


def first_cas(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    first = raw.split(",")[0].strip()
    return first if CAS_PATTERN.fullmatch(first) else ""


def map_row(row, warnings, line_no):
    sku = row["sku"].strip()

    size_match = SIZE_PATTERN.match(row["size"].strip())
    size, size_unit = (size_match.group(1), size_match.group(2)) if size_match else ("", row["size"].strip())

    uom_dict = parse_dict_field(row["uom"])
    uom = uom_dict.get("Base_UOM_Txt_Abbrev", "")

    product_url_dict = parse_dict_field(row["product_url"])
    product_url = product_url_dict.get("us", "")

    msds_url = (row["sds_link_us_en"] or "").strip() or (row["SDS_URL_US"] or "").strip()
    if not msds_url:
        warnings.append(f"row {line_no} ({sku}): no msds_url available")

    eu_price_raw = (row.get("eu_list_price") or "").strip()
    if eu_price_raw:
        list_price_eur = f"{float(eu_price_raw):.2f}"
    else:
        list_price_eur = f"{float(row['gbp']):.2f}"
        warnings.append(f"row {line_no} ({sku}): no eu_list_price available, falling back to gbp (US price) for list_price_eur")

    eu_future_raw = (row.get("eu_future_list_price") or "").strip()
    if eu_future_raw:
        list_price_eur_future = f"{float(eu_future_raw):.2f}"
    else:
        list_price_eur_future = f"{float(row['future_gbp']):.2f}"
        warnings.append(f"row {line_no} ({sku}): no eu_future_list_price available, falling back to future_gbp (US future price) for list_price_eur_future")

    uk_price_raw = (row.get("UK_CLP") or "").strip()
    if uk_price_raw:
        list_price_gbp = f"{float(uk_price_raw):.2f}"
    else:
        list_price_gbp = f"{float(row['gbp']):.2f}"
        warnings.append(f"row {line_no} ({sku}): no UK_CLP available, falling back to gbp (US price) for list_price_gbp")

    uk_future_raw = (row.get("UK_Future_CLP") or "").strip()
    if uk_future_raw:
        list_price_gbp_future = f"{float(uk_future_raw):.2f}"
    else:
        list_price_gbp_future = f"{float(row['future_gbp']):.2f}"
        warnings.append(f"row {line_no} ({sku}): no UK_Future_CLP available, falling back to future_gbp (US future price) for list_price_gbp_future")

    return {
        "sku": sku,
        "manufacturer": row["MANUFACTURERNAME"].strip(),
        "short_description": WHITESPACE_RUN.sub(" ", row["PRODUCTSHORTNAME"]).strip(),
        "long_description": row["product_name"].strip(),
        "list_price_usd": f"{float(row['gbp']):.2f}",
        "list_price_eur": list_price_eur,
        "list_price_gbp": list_price_gbp,
        "list_price_usd_future": f"{float(row['future_gbp']):.2f}",
        "list_price_eur_future": list_price_eur_future,
        "list_price_gbp_future": list_price_gbp_future,
        "currency": row["CURRENCYCODE"].strip() or "USD",
        "uom": uom,
        "size": size,
        "size_unit": size_unit,
        "unspsc": row["unspsc_no"].strip(),
        "assays": row["applications"].strip(),
        "shipping_conditions": row["shipping_condition"].strip(),
        "storage_temperature": row["storage_temp"].strip(),
        "product_classification": row["product_type"].strip(),
        "msds_url": msds_url,
        "product_url": product_url,
        "image_url": row["primary_image_url"].strip(),
        "cas_number": first_cas(row["commercial_cas_number"]),
    }


def write_audit(outcome, detail, requested_by, source_path):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    entry = {
        # Local time with explicit offset (see generate_catalog.audit_timestamp).
        "timestamp": datetime.now().astimezone().isoformat(),
        "who": requested_by,
        "action": "import_master_data",
        "customer": None,
        "request": f"import {source_path}",
        "outcome": outcome,
        "detail": detail,
    }
    audit_path = AUDIT_DIR / f"{now.date().isoformat()}.jsonl"
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


DICT_COLUMNS = {"uom", "product_url"}


def iter_pickle_rows(source_path):
    """Yield (line_no, row_dict) from a pandas DataFrame pickle, normalized to
    look like csv.DictReader output: every non-dict field a string (None/NaN
    becomes ''), dict-shaped fields (uom, product_url) left as real dicts so
    parse_dict_field can consume them directly."""
    import pandas as pd

    df = pd.read_pickle(source_path)
    for i, record in enumerate(df.to_dict("records"), start=2):
        row = {}
        for key, value in record.items():
            if key in DICT_COLUMNS:
                row[key] = value if isinstance(value, dict) else {}
            elif value is None or (isinstance(value, float) and math.isnan(value)):
                row[key] = ""
            else:
                row[key] = str(value)
        yield i, row


def iter_csv_rows(source_path):
    with open(source_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            yield i, row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("--requested-by", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import os
    requested_by = args.requested_by or os.environ.get("USER", "unknown")
    source_path = args.input_csv

    warnings = []
    errors = []
    mapped_rows = []
    seen_skus = set()

    is_pickle = source_path.lower().endswith((".pkl", ".pickle"))
    row_iter = iter_pickle_rows(source_path) if is_pickle else iter_csv_rows(source_path)

    for i, row in row_iter:
        sku = row["sku"].strip()
        if not sku:
            errors.append(f"row {i}: blank sku")
            continue
        if sku in seen_skus:
            errors.append(f"row {i}: duplicate sku {sku!r}")
            continue
        seen_skus.add(sku)
        try:
            mapped_rows.append(map_row(row, warnings, i))
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
    print(f"Current master/products.csv has {old_sku_count} rows")
    print(f"Warnings: {len(warnings)}")
    for w in warnings[:20]:
        print(f"  - {w}")
    if len(warnings) > 20:
        print(f"  ... and {len(warnings) - 20} more")

    if args.dry_run:
        print("Dry run — master/products.csv not modified")
        write_audit("dry_run", {"rows_parsed": len(mapped_rows), "warnings": warnings}, requested_by, source_path)
        return

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
