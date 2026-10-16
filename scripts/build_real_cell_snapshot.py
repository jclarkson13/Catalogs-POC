"""Build master/real_cell_snapshot.csv: cells COPIED from real catalog files
where no source reproduces them (POC decisions 2026-10-01, see CLAUDE.md
"Standing rules"). Applied to every column by generate_catalog.build_rows().

- SDS links / hazard codes, ~24 SKUs: SDS reissued after the 2026-09-14
  cst_data_2.csv export, plus per-file SDS choices (blank where we emit a link
  and vice versa).
- sciquest-products-us Long Description (176 SKUs: real file has a different
  PRODUCTBACKGROUND version), Species Reactivity (13: different species
  order), Molecular Weight (+ Details) (76518S/T: real blank).
- amici-catalog Hazards (7305C/V1 EUH430, SDS reissue) and Restrictions
  ('Duty Free Ethanol' on 12 SKUs; no source field identifies them), Shelflife
  (3 SKUs where the source's shelf life changed).
- 2026-10-01 single-column batch: 10754* family's raw <p> names (bioresco,
  nyu, sanofi), the same 3 shelf lives (eu-zageno, vwr), species lists that
  differ from the source (one-advanced, pmv), quartzy 90595* CAS trailing \n.
- europe-price-export EU CLP (EUR) 5748P2 = '690' (real whole-number string;
  the scoreboard calls it number_format, so these 2 rows were added by hand -
  the merge keeps them).
- coupa-marketplace: CATEGORY1 (3 SKUs re-categorized upstream), INTRASTAT_CODE
  (3305S/T blank in the real files), UK DESCRIPTION_SHORT (7970C/V1, 8117S).
- sap-gdsn-price-de helmholtz PRICE_AMOUNT (first of two same-named columns):
  622 half-cent prices the real file rounds down (39 list prices; no rule -
  upstream float behaviour). Added by hand (duplicate header), merge keeps them.
- sap-gdsn-price-de-mercateo FVALUE2 (FNAME2 = GHSSIGNAL): G/A signal word,
  not derivable - H314 rows appear as both G and A.
- neta-scientific Status (46 SKUs whose On Hold flag changed since the real file)
  and CAS Number (44712*/90595* blank in the real file).
- bipp-articles Buom 9303S; basf-mycatalogcloud-oci ClassificationID (130 NEB
  SKUs whose real eclass differs from neb_data.pkl) and MimeSource (T2110L/V).
Only the (customer, column, SKU) cells a fidelity report lists as unexplained
are copied; everything else keeps its normal expression. Frozen: rerun after
a newer CST export only if diffs reappear, and drop rows the export fixes.

- 2026-10-02 `--known-gaps`: every remaining known-gap cell (user decision:
  copy them all). For each customer in the report, compares the real file
  with its current catalogs/ file and copies each differing cell (value or
  cell type) whose SKU is a coverage gap (absent from cst_data_2.csv - the
  105 SKUs added after that export) or an accepted residual
  (config/fidelity_accepted.json) for that column. Duplicate headers are
  keyed "HEADER [col N]". Since 2026-10-02 the generator applies snapshot
  cells to every column (build_rows), so no template edit is needed.

Key = "display_name|column|sku" -> value, plus `type` ('' = text/blank,
'int'/'float' = real number cell, converted back by the builtin). Per customer, not per template:
customers sharing a template disagree (coupa-marketplace DE vs UK pick
different SDS links; uni-bonn kept the old links helmholtz replaced). Usage:
    python3 scripts/build_real_cell_snapshot.py reports/fidelity/<a>.json [<b>.json ...]
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from compare_catalog import compare, read_table  # noqa: E402

OUT = ROOT / "master" / "real_cell_snapshot.csv"
COLUMNS = {
    ("amici-catalog", "URL to SDS"),
    ("coupa-marketplace", "DATASHEET"),
    ("eu-zageno", "Hazard codes"),
    ("eu-zageno", "Resource URL"),
    ("sap-gdsn-price-de", "MIME_SOURCE8"),
    ("sap-gdsn-price-de-mercateo", "MIME_SOURCE8"),
    ("sap-gdsn-price-de-mercateo", "FVALUE3"),
    ("max-planck-products", "Sicherheitsdatenblatt/URL"),
    ("max-planck-products", "H-Sätze"),
    ("sciquest-products-uk", "MSDS URL"),
    ("vertex-labviva-uk", "Document"),
    ("vwr-de-mel", "MSDS"),
    ("vwr-uk-mel", "MSDS"),
    ("sciquest-products-us", "Long Description"),
    ("sciquest-products-us", "Species Reactivity"),
    ("sciquest-products-us", "Molecular Weight"),
    ("sciquest-products-us", "Molecular Weight Details"),
    ("amici-catalog", "Hazards"),
    ("amici-catalog", "Restrictions"),
    ("amici-catalog", "Shelflife"),
    ("bioresco-univ-maryland", "Product Description"),
    ("nyu-school-of-medicine", "Short Description"),
    ("nyu-school-of-medicine", "Long Description"),
    ("sanofi-genzyme", "Name*"),
    ("sanofi-genzyme", "Description*"),
    ("eu-zageno", "Shelf Life"),
    ("vwr-de-mel", "Maximum Shelf Life"),
    ("vwr-uk-mel", "Maximum Shelf Life"),
    ("one-advanced-products", "Specifications"),
    ("pmv-pharma", "Species Reactivity"),
    ("quartzy", "CAS Number"),
    ("pmv-pharma", "CAS Number"),
    ("europe-price-export", "EU CLP (EUR)"),
    ("coupa-marketplace", "CATEGORY1"),
    ("coupa-marketplace", "INTRASTAT_CODE"),
    ("coupa-marketplace", "DESCRIPTION_SHORT"),
    ("max-planck-products", "CAS Number"),
    ("sap-gdsn-price-de-mercateo", "FVALUE2"),
    ("neta-scientific", "Status"),
    ("neta-scientific", "CAS Number"),
    ("bipp-articles", "Buom"),
    ("basf-mycatalogcloud-oci", "Classification.1.ClassificationID"),
    ("basf-mycatalogcloud-oci", "Mime.1.MimeSource"),
    ("evotec-coupa-style", "Supplier Item Link to Safety Data Sheet"),
    ("google-merchant-center", "species"),
    ("google-merchant-center", "specificity"),
    ("google-merchant-center", "product description"),
    ("google-merchant-center", "Molecular Weight (kDa)"),
}


def load_existing():
    if not OUT.exists():
        return {}
    with open(OUT, newline="", encoding="utf-8") as f:
        return {r["key"]: (r["value"], r.get("type") or "") for r in csv.DictReader(f)}


def write_snapshot(merged):
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "value", "type"])
        for k in sorted(merged):
            w.writerow([k, *merged[k]])


def known_gaps(report):
    from fidelity_scoreboard import accepted_residuals, cst2_skus
    from generate_catalog import CATALOGS_DIR, load_customer, load_template, output_extension
    gaps = cst2_skus()
    acc = accepted_residuals()
    existing = load_existing()
    new, unwrapped = {}, set()
    for r in json.load(open(report))["results"]:
        cols = r.get("columns") or {}
        if not any(v.get("coverage_gap") or v.get("accepted") for v in cols.values()):
            continue
        slug = r["slug"]
        customer = load_customer(slug)
        tpl = load_template(customer["template_ref"])
        files = sorted((CATALOGS_DIR / slug).glob(f"*.{output_extension(customer)}"))
        headerless = (tpl.get("csv_format") or {}).get("header_row", True) is False
        hdr = [c["header"] for c in tpl["columns"]] if headerless else None
        real = str(ROOT / r["real_file"])
        res = compare(real, str(files[-1]), tpl.get("sku_column"), header=hdr,
                      delimiter=tpl.get("delimiter"), sheet=tpl.get("sheet_name"))
        rh, rrows = read_table(real, sheet=tpl.get("sheet_name"), sku=tpl.get("sku_column"),
                               header=hdr, delimiter=tpl.get("delimiter"))
        si = rh.index(tpl.get("sku_column") or rh[0])
        rmap = {row[si][0]: row for row in rrows}
        targets = {}
        for label in set(res["value_diffs"]) | set(res["type_diff_skus"]):
            known = acc.get((tpl["template_name"], label), frozenset())
            skus = {k for k, _, _ in res["value_diffs"].get(label, [])}
            for ks in res["type_diff_skus"].get(label, {}).values():
                skus.update(ks)
            skus = {k for k in skus if k in known or k in gaps}
            if skus:
                targets[label] = skus
        for label, skus in targets.items():
            if label.endswith("]") and " [col " in label:
                ci = int(label.rsplit(" [col ", 1)[1][:-1]) - 1
            else:
                ci = rh.index(label)
            expr = tpl["columns"][ci]["expression"] if ci < len(tpl["columns"]) and tpl["columns"][ci]["header"] == rh[ci] else None
            if expr is None:
                expr = next(c["expression"] for c in tpl["columns"] if c["header"] == label)
            for sku in skus:
                v, t = rmap[sku][ci] if ci < len(rmap[sku]) else ("", "blank")
                key = f"{customer['display_name']}|{label}|{sku}"
                val = (v, t if t in ("int", "float") else "")
                if key in new and new[key] != val:
                    print("CONFLICT", key, new[key], val)
                    sys.exit(1)
                new[key] = val
        print(f"{slug}: {sum(len(s) for s in targets.values())} cells")
    write_snapshot({**existing, **new})
    print(f"{len(new)} known-gap cells -> {OUT.relative_to(ROOT)}; {len(existing)} existing rows kept")


def main(reports):
    results = {}
    for rep in reports:  # later reports win per customer
        for r in json.load(open(rep))["results"]:
            results[r["slug"]] = r
    values = {}  # key -> {value: [slugs]}
    # Merge: keep existing rows (once a snapshot applies, its cells are no
    # longer unexplained, so a rebuild from a newer report would drop them).
    existing = {}
    if OUT.exists():
        with open(OUT, newline="", encoding="utf-8") as f:
            existing = {r["key"]: (r["value"], r.get("type") or "") for r in csv.DictReader(f)}
    for slug, r in sorted(results.items()):
        wanted = {c: set(v.get("unexplained_skus") or []) for c, v in (r["columns"] or {}).items()
                  if v and (r["template"], c) in COLUMNS}
        if not any(wanted.values()):
            continue
        tpl = json.load(open(ROOT / "config" / "templates" / f"{r['template']}.json"))
        sku_col = tpl["sku_column"]
        name = json.load(open(ROOT / "config" / "customers" / f"{slug}.json"))["display_name"]
        header, rows = read_table(str(ROOT / r["real_file"]), sku=sku_col, delimiter=tpl.get("delimiter"))
        si = header.index(sku_col)
        for col, skus in wanted.items():
            ci = header.index(col)
            for row in rows:
                sku = row[si][0] if si < len(row) else ""
                if sku in skus:
                    val = row[ci][0] if ci < len(row) and row[ci][0] is not None else ""
                    values.setdefault(f"{name}|{col}|{sku}", {}).setdefault(str(val), []).append(slug)
    conflicts = {k: v for k, v in values.items() if len(v) > 1}
    if conflicts:
        for k, v in conflicts.items():
            print("CONFLICT", k, v)
        sys.exit(1)
    write_snapshot({**existing, **{k: (next(iter(v)), "") for k, v in values.items()}})
    print(f"{len(values)} new/updated + {len(set(existing) - set(values))} kept -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--known-gaps"]:
        known_gaps(sys.argv[2])
    else:
        main(sys.argv[1:])
