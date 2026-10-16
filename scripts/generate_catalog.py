#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER_CSV = ROOT / "master" / "products.csv"
TEMPLATES_DIR = ROOT / "config" / "templates"
CUSTOMERS_DIR = ROOT / "config" / "customers"
CATALOGS_DIR = ROOT / "catalogs"
AUDIT_DIR = Path(os.environ["CATALOG_AUDIT_DIR"]) if os.environ.get("CATALOG_AUDIT_DIR") else ROOT / "audit"  # override: agents in worktrees log to the main checkout

def price_for(row, base_field, customer):
    """Picks a master price field by the customer's price_period ('current',
    the default, or 'future') - e.g. price_for(row, 'list_price_usd', customer)
    reads list_price_usd or list_price_usd_future. Lets one template serve both
    a customer's Current Price and Future Price catalog (two separate customer
    configs, same template_ref) without duplicating every price expression.

    Falls back to the plain base_field when the _future column doesn't exist
    on this row at all (not just blank) - master/products_neb.csv (NEB brand)
    has no *_future price columns in its schema, since NEB has no separate
    future-pricing data. This only matters for a template whose master_source
    spans both products.csv and products_neb.csv and is also used by a
    price_period: "future" customer (e.g. vwr-de-future/vwr-uk-future via
    benelux-vwr-mel) - every CST-brand row still gets its real _future value."""
    period = customer.get("price_period", "current")
    field = f"{base_field}_future" if period == "future" else base_field
    if field not in row:
        field = base_field
    return float(row[field])


def price_for_rounded(row, base_field, customer, discount):
    """Like price_for(), but computes the discounted price entirely in
    Decimal arithmetic and returns a 2-decimal string - avoids a real bug
    found in sap-gdsn-price-de: 'round(float(x) * (1 - discount/100), 2)'
    (or the equivalent '%.2f' % ...) hits binary floating-point
    representation error on exact-half-cent results (e.g. 191.00 * 0.825 =
    157.575 mathematically, but the float is stored as
    157.57499999999998ish, so naive rounding gives 157.57 when the real
    source file - and correct round-half-even arithmetic on the exact
    decimal value - gives 157.58). Confirmed byte-for-byte correct against
    3 of 4 real DE institution files sharing this template; the 4th
    (Helmholtz) still has a small residual of ~1-cent diffs with no
    deterministic explanation found - see docs/baseline-reconciliation.md."""
    period = customer.get("price_period", "current")
    field = f"{base_field}_future" if period == "future" else base_field
    if field not in row:
        field = base_field
    base = Decimal(str(row[field]))
    disc = Decimal(str(discount))
    result = base * (Decimal("100") - disc) / Decimal("100")
    return str(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def price_for_rounded_half_up(row, base_field, customer, discount):
    """Like price_for_rounded(), but rounds ROUND_HALF_UP instead of
    ROUND_HALF_EVEN. Added for de-boehringer-austria: its real Unit Price
    column disagreed with price_for_rounded() on every exact-half-cent case
    (e.g. 505.00 * 92.5% = 467.125 -> the real file has 467.13, which is
    round-half-UP, not round-half-even's 467.12) - verified byte-for-byte
    correct (0 mismatches) across the full population of both institutions'
    Current Price files once this rounding mode was used instead. Kept as a
    separate function rather than a parameter on price_for_rounded() so
    sap-gdsn-price-de's existing, already-verified call sites can't
    accidentally change behavior."""
    period = customer.get("price_period", "current")
    field = f"{base_field}_future" if period == "future" else base_field
    if field not in row:
        field = base_field
    base = Decimal(str(row[field]))
    disc = Decimal(str(discount))
    result = base * (Decimal("100") - disc) / Decimal("100")
    return str(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


_MONTH_ABBREV = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def format_date_ddmonyy(date_str):
    """Expression builtin: reformats a customer config ISO date
    ('2026-12-31') to fisher-us's real 'DD-MON-YY' text format
    ('31-DEC-26'), e.g. Pricing Expiration Date."""
    year, month, day = date_str.split("-")
    return f"{int(day):02d}-{_MONTH_ABBREV[int(month) - 1]}-{year[2:]}"


def price_text(value, discount=0, half_up=False):
    """Expression builtin: the price_for_rounded() arithmetic on an explicit
    value instead of a price_period-selected master field - for templates
    whose price column reads a fixed field (e.g. float(row['list_price_eur']),
    same value in Current and Future files). Decimal math, 2-decimal TEXT
    string ('415.00'), half-even by default or half-up. Added 2026-09-26 for
    the cell-type sweep: real files store these prices as text, and bare
    float()/round() wrote number cells ('415.00' vs 415)."""
    base = Decimal(str(value))
    result = base * (Decimal("100") - Decimal(str(discount))) / Decimal("100")
    return str(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP if half_up else ROUND_HALF_EVEN))
def price_for_rounded_number(row, base_field, customer, discount, half_up=False):
    """Numeric-cell counterpart to price_for_rounded()/
    price_for_rounded_half_up(): identical Decimal arithmetic (avoids the
    float binary-representation bug on exact-half-cent results, e.g.
    193.5 * 84.0% stored as 162.53999999999996 instead of 162.54), but
    returns a float rather than a 2dp string - for columns whose real file
    writes the price as a NUMBER cell, not TEXT (e.g.
    universiteit-maastricht-cif's Unit Price)."""
    period = customer.get("price_period", "current")
    field = f"{base_field}_future" if period == "future" else base_field
    if field not in row:
        field = base_field
    base = Decimal(str(row[field]))
    disc = Decimal(str(discount))
    result = base * (Decimal("100") - disc) / Decimal("100")
    rounding = ROUND_HALF_UP if half_up else ROUND_HALF_EVEN
    return float(result.quantize(Decimal("0.01"), rounding=rounding))


def clean_boehringer_description(text):
    """Expression builtin for de-boehringer-austria's Item Description/Short
    Name columns: replicates the real per-SKU HTML-tag/entity cleanup found
    in the Boehringer Forschungsinstitut/RCV Austria Current & Future Price
    files - verified byte-for-byte across the full 24,765-row population
    (both institutions, both price periods) except for the SKUs
    incoming/cst_data_2.csv doesn't cover (see the template's own
    description). Every tag - '<sup>'/'</sup>', '<sub>'/'</sub>', '<i>'/
    '</i>', '<p>'/'</p>', the bare self-closing '<R/>', and even the
    pseudo-escaped '<lt/>'/'<gt/>' tokens a handful of SKUs use in place of
    real angle brackets - is replaced with a single space by one generic
    regex, deliberately NOT reassembled into real '<sup>'/'</sup>' tags first
    the way coupa-marketplace's 'uk' mode does: the real Boehringer files
    literally contain the leftover words 'sup'/'/sup' for those SKUs (e.g.
    'PathScan sup 00AE /sup Phospho-Syk...'), not a cleaned symbol - a
    genuine source-file quirk, replicated as-is. Runs of 2+ spaces then
    collapse to one and the ends are trimmed. The bare '®' character
    (tag-wrapped or not) becomes the literal text '00AE' - the same garbled
    HTML-entity-decode artifact already documented for sap-gdsn-price-de's
    'buggy' mode - but note '™' is left untouched here (the real files keep
    the bare unicode character), unlike '®'."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("®", "00AE")
    return re.sub(r" {2,}", " ", text).strip()


def clean_coupa_description_short_uk(text):
    """UK-only variant of clean_coupa_description() for
    coupa-marketplace-uk's DESCRIPTION_SHORT: the real file does NOT
    truncate the fully-cleaned DESCRIPTION_LONG value at 35 chars (that
    works for US/DE, verified 0 mismatches) - it substitutes the
    pseudo-escaped tokens and symbols first (same as clean_coupa_description
    minus the final tag-strip step), truncates THAT to 35 raw chars, and
    only then strips any tag left dangling in the truncated substring.
    Confirmed byte-for-byte against the full population (was previously a
    149/24,764 unexplained residual, documented as accepted without a
    rule - the rule is truncate-then-strip, not clean-then-truncate)."""
    text = text.replace("<lt/>sup<gt/>", "<sup>").replace("<lt/>/sup<gt/>", "</sup>")
    text = text.replace("<sup>®</sup>", "(R)").replace("<sup>™</sup>", "(tm)")
    text = text.replace("®", "(R)").replace("™", "(tm)")
    text = text[:35]
    return re.sub(r"<[^>]+>", "", text)


def clean_coupa_description(text, region="us"):
    """Expression builtin for coupa-marketplace's DESCRIPTION_SHORT/LONG:
    replicates a real, region-specific HTML-entity/tag cleaning pipeline
    applied to master's long_description, verified byte-for-byte across the
    full population for all 3 regions (US/DE/UK) - see coupa-marketplace.json's
    own description for the full byte-level reasoning. Every region first
    replaces the paired '<sup>®</sup>'/'<sup>™</sup>' forms and any remaining
    bare '®'/'™' character with '(R)'/'(tm)' - this alone is the complete
    transform for 'us' (any other tag, e.g. '<i>...</i>' or a literal '<R/>'
    some SKUs already carry, passes through completely untouched). 'de'
    additionally strips every remaining HTML tag, replacing each with a
    single space, then collapses runs of 2+ spaces to one and trims the
    ends. 'uk' first normalizes the pseudo-escaped '<lt/>sup<gt/>'/
    '<lt/>/sup<gt/>' tokens (a small number of SKUs' raw text uses these
    instead of real angle brackets) into real '<sup>'/'</sup>' tags before
    the symbol substitution, then strips every remaining tag with no space
    and no collapse needed - a real asymmetry from 'de's handling of the
    exact same pseudo-escaped input."""
    if region == "uk":
        text = text.replace("<lt/>sup<gt/>", "<sup>").replace("<lt/>/sup<gt/>", "</sup>")
    text = text.replace("<sup>®</sup>", "(R)").replace("<sup>™</sup>", "(tm)")
    text = text.replace("®", "(R)").replace("™", "(tm)")
    if region == "de":
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r" {2,}", " ", text).strip()
    elif region == "uk":
        text = re.sub(r"<[^>]+>", "", text)
    return text


def clean_gladstone_description(text):
    """Expression builtin for distributor-staggered-gladstone's ShortDescription/
    LongDescription (identical in every real row): verified byte-for-byte
    across the full 26,222-row US - Gladstone Institute Current Price
    population. Only '<sup>'/'</sup>', '<sub>'/'</sub>' and '<i>'/'</i>' tags
    are stripped (tag removed, inner text kept) - the pseudo-escaped
    '<lt/>sup<gt/>'/'<lt/>/sup<gt/>' tokens a few SKUs use in place of real
    angle brackets are first normalized to real '<sup>'/'</sup>' tags so they
    get the same treatment. Unlike clean_coupa_description/
    clean_boehringer_description, no entity substitution happens at all here:
    a bare '®'/'™' character passes through completely untouched, and so
    does the bare self-closing '<R/>' tag some SKUs carry (confirmed real:
    'DyLight<R/> 680 Conjugate' appears in the real file exactly as-is, not
    stripped and not decoded) - '<R/>' is deliberately excluded from the
    strip regex for this reason."""
    text = text.replace("<lt/>sup<gt/>", "<sup>").replace("<lt/>/sup<gt/>", "</sup>")
    return re.sub(r"</?(?:sup|sub|i)>", "", text)


def format_neb_size(row):
    """Expression builtin for ghx-gdsn-nl-hospital's 'Inhoud Basiseenheid'
    (Content of Base Unit): master/products_neb.csv's own `size` field is
    itself inconsistently formatted (some values already carry a ',' or '.'
    thousands-style separator, most don't), and the real GHX files apply a
    genuinely odd, real (not fixed) per-character transform rather than
    true numeric reformatting - confirmed byte-for-byte across all 1,617
    NEB rows (modulo a None-vs-'' blank-representation non-issue for rows
    with no size at all): a ',' is replaced with '.', but an existing '.'
    is deleted outright with nothing put back in its place (e.g. real
    master value '0.5' - a genuine decimal quantity - becomes the real
    file's '05', not '0.5' or '5'; '2.500' becomes '2500'; '2,500' becomes
    '2.500'). This only applies to NEB-brand rows - CST-brand rows (which
    do have a 'list_price_usd_future' key, the same brand-detection
    already used by price_for()'s _future fallback; NEB-brand rows lack it
    entirely) pass `size` straight through unchanged, confirmed 0
    mismatches across all 26,222 CST rows under a plain passthrough -
    applying this transform to a CST row's `size` would silently corrupt a
    genuine decimal value like '1.0' into '10'."""
    size = row["size"]
    if "list_price_usd_future" in row:
        return size
    if "," in size:
        return size.replace(",", ".")
    if "." in size:
        return size.replace(".", "")
    return size


def strip_trailing_zeros(s):
    """Expression builtin: strips a trailing '.00'/'0' from a decimal string
    (e.g. price_for_rounded()'s '415.00' -> '415', '157.50' -> '157.5'), for
    distributor-staggered-lsds's Price column - verified byte-for-byte against
    every one of the real US - LSDS Current/Future Price files' Price values,
    all of which are whole-dollar amounts with no fractional cents in the
    current master data, so the fractional-cents behavior (keeping a single
    non-zero decimal digit rather than switching to something like '%g'
    formatting) is inferred, not directly observed - flagged in
    docs/baseline-reconciliation.md as unverified for that case."""
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def clean_ariba_text(text, replace_symbols, strip_commas):
    """Expression builtin for ariba-tiered / ariba-tiered-txt's Item
    Description and Short Name columns: replicates the real Ariba feed's
    text-cleanup on master's long_description, verified byte-for-byte
    against the full population (residuals noted below). Always decodes the
    small number of double-escaped '<lt/>'/'<gt/>' placeholders back to
    '<'/'>' and unwraps (strips, keeping inner text) '<i>', '<sub>', '<sup>'
    and '<p>' tag pairs. Item Description passes replace_symbols=False - it
    leaves a real '®'/'™' character or a bare '<R/>' placeholder untouched
    (residual: 5/26222 SKUs, all one 'PathScan<R/> RP Phospho-Rb' SKU
    family, whose real file left '<p>' tags unstripped too - a genuine
    upstream inconsistency, not reproduced). Short Name passes
    replace_symbols=True - it additionally substitutes '(R)'/'(tm)' for
    those three forms, then the template truncates the result to the real
    file's fixed length (47 for the 0% tier's CIF-derived container, 50 for
    the 5%/11% tiers' EXCEL container) - residual: 194/26222 SKUs (0.7%,
    same set across both tiers/both price periods) where the real file's
    own truncation lands mid-tag or mid-nested-placeholder and the tag
    survives un-stripped; not reproduced - see
    docs/baseline-reconciliation.md. strip_commas additionally removes
    literal commas - true for the 0% tier's container, false for 5%/11%'s
    (confirmed real via full-population diff, not a bug)."""
    text = text.replace("<lt/>", "<").replace("<gt/>", ">")
    for tag in ("<i>", "</i>", "<sub>", "</sub>", "<sup>", "</sup>", "<p>", "</p>"):
        text = text.replace(tag, "")
    if replace_symbols:
        text = text.replace("<R/>", "(R)").replace("®", "(R)").replace("™", "(tm)")
    if strip_commas:
        text = text.replace(",", "")
    return text


def ariba_name(row, strip_commas=False):
    """Symbol-spelled product name for Ariba-family name columns: the
    precomputed product_name_no_symbols variant from cst_data_2.csv
    (master/cst_product_name_variants.csv), falling back to
    clean_ariba_text(long_description, True, ...) for SKUs without one. The
    variant keeps <i>/<sub> tags that clean_ariba_text strips - it explains
    190 of the 194 Short Name rows previously documented as a truncation
    residual (fidelity scoreboard, 2026-09-25)."""
    text = lookup(row["sku"], "cst_product_name_variants.csv", "product_name_no_symbols", "")
    if not text:
        return clean_ariba_text(row["long_description"], True, strip_commas)
    return text.replace(",", "") if strip_commas else text


def cst_product_name(row, raw=False):
    """Product name for the CST-sourced CIF/Ariba Item Description family.
    raw=False (default - curevac-se): cst_data_2.csv's product_name with
    HTML tags stripped, symbols kept literal
    (master/cst_product_name_variants.csv's product_name_no_tags), falling
    back to short_description for the ~105 SKUs cst_data_2.csv lacks.
    raw=True (ariba-proxy(-txt), universiteit-maastricht-cif,
    intervet-merck-msd-cif): the untouched raw field instead - master's
    long_description is that same product_name field verbatim (26,114/26,117
    exact matches; the 3 residuals - 14809S, 27683S, 27683T - are an edited
    <sup>...</sup> in cst_data_2.csv that no real file has; their rows in
    cst_product_name_variants.csv/cst_product_name_with_volume.csv were
    re-derived from master long_description on 2026-10-01), including any
    HTML tag ('<sup>®</sup>') the real file itself leaves un-stripped.
    de-boehringer-pharma is a third variant (strips tags with an inserted
    space and spells out '00AE' for a literal '®' - see
    boehringer_cif_item_name()) and uses neither. Distinct from ariba_name(),
    which returns the symbol-spelled ('(R)'/'(tm)') variant used by Ariba's
    own Short Name column."""
    if raw:
        return row["long_description"] or row["short_description"]
    return lookup(row["sku"], "cst_product_name_variants.csv", "product_name_no_tags", row["short_description"])


def boehringer_cif_item_name(row):
    """de-boehringer-pharma's real, garbled tag-cleanup of the raw product
    name (row['long_description'], cst_data_2.csv's product_name verbatim -
    see cst_product_name()): each opening '<sup>'/'<sub>' tag becomes a
    literal space, its closing tag is deleted outright (e.g.
    'mFluor<sup>™</sup> Violet' -> 'mFluor ™ Violet', not a double space), a
    literal '®' becomes the literal text '00AE' (a real upstream mojibake
    artifact - standing directive is to replicate it, not fix it; see
    docs/audit-decisions.md), and a literal comma is replaced with a space
    (e.g. 'IgG, AP' -> 'IgG  AP', double space - same quirk as
    cst_name_with_suffix's comma_mode='space'). '™' and other characters are
    left untouched. Verified against the real file's full population.
    Exception (2026-10-01): an opening '<sub>' is deleted, not spaced
    ("F(ab')<sub>2</sub>" -> "F(ab')2", 14 SKUs)."""
    text = row["long_description"] or row["short_description"]
    for tag in ("<sup>", "<i>"):
        text = text.replace(tag, " ")
    for tag in ("<sub>", "</sup>", "</sub>", "</i>"):
        text = text.replace(tag, "")
    return text.replace("®", "00AE").replace(",", " ")


def boehringer_cif_short_name(row):
    """de-boehringer-pharma's Short Name: like boehringer_cif_item_name(),
    but BOTH the opening and closing '<sup>'/'<sub>'/'<i>' tags become a
    literal space (Item Description only replaces the opening tag and
    deletes the closing one) - a genuinely different per-column real-file
    cleanup rule, verified against the full population (e.g.
    'PathScan<sup>®</sup> RP...' -> 'PathScan 00AE  RP...', double space,
    vs Item Description's single space for the same SKU)."""
    text = row["long_description"] or row["short_description"]
    for tag in ("<sup>", "</sup>", "<sub>", "</sub>", "<i>", "</i>"):
        text = text.replace(tag, " ")
    return text.replace("®", "00AE").replace(",", " ")


def boehringer_cif_item_description(row):
    """de-boehringer-pharma's Item Description: boehringer_cif_item_name(row)
    plus a test count suffix - ' (<test_count>)', or a lone trailing space
    when there's no test count (cst_marketing_name_details.csv's
    test_count_uni_abbrev) - same real-file quirk as
    cst_name_with_suffix(..., 'blank_space', ...), verified against the full
    population. The comma -> space rule covers the suffix too ('200 assays,
    96 well format' -> '200 assays  96 well format'; 2026-10-01)."""
    name = boehringer_cif_item_name(row)
    test_count = lookup(row["sku"], "cst_marketing_name_details.csv", "test_count_uni_abbrev", "").replace(",", " ")
    return f"{name} ({test_count})" if test_count else f"{name} "


def fisher_us_description(row, short=False):
    """fisher-us Item Description columns (verified 2026-10-01 against all
    26,222 rows of both real files). Base name = product_name_no_symbols
    (fallback: long_description with only the symbols spelled out, tags
    kept), commas and '*' deleted. Long form (1000/250 chars): '<lt/>'/'<gt/>'
    decoded, <sup>/<sub>/<i> tags removed, ' <qty>' appended (fallback size
    with u for \u00b5). short=True (30 chars): no decoding or tag removal,
    no qty. Both then delete the characters < > ' + & ; : and anything
    non-ASCII - so a leftover '<R/>' becomes 'R/', 'H+L' becomes 'HL'."""
    name = lookup(row["sku"], "cst_product_name_variants.csv", "product_name_no_symbols", "") or (
        row["long_description"].replace("<sup>\u00ae</sup>", "(R)").replace("<sup>\u2122</sup>", "(tm)")
        .replace("\u00ae", "(R)").replace("\u2122", "(tm)"))
    name = name.replace(",", "").replace("*", "")
    if short:
        return re.sub(r"[<>'+&;:]|[^\x00-\x7f]", "", name)[:30]
    name = re.sub(r"</?(sup|sub|i)>", "", name.replace("<lt/>", "<").replace("<gt/>", ">"))
    qty = lookup(row["sku"], "cst_marketing_name_details.csv", "qty_mkt_txt_abbrev_units",
                 row["size"] + " " + row["size_unit"].replace("\u00b5", "u"))
    return re.sub(r"[<>'+&;:]|[^\x00-\x7f]", "", name + " " + qty)


def neb_unit_size(row):
    """NEB rows' size text as the real Benelux files print it: neb_data.pkl's
    raw `unit_size` (master/neb_unit_size.csv), whose spacing is inconsistent
    ('145ml', '4000units', '250  \u00b5g', '4X 25 ML') and not derivable from
    products_neb.csv's size/size_unit; falls back to '<size> <size_unit>'.
    Verified 2026-10-01 against universiteit-maastricht/intervet NEB rows."""
    return lookup(row["sku"], "neb_unit_size.csv", "unit_size", "") or f"{row['size']} {row['size_unit']}"


def cst_name_with_suffix(row, suffix_style, comma_mode="keep", raw=False):
    """Assembles the CST-family Item Description/Short Name real-file text:
    cst_product_name(row), with a literal comma kept ('keep'), replaced with
    a space ('space' - de-boehringer-pharma, e.g. 'IgG, AP' -> 'IgG  AP',
    double space) or deleted ('delete' - universiteit-maastricht-cif /
    intervet-merck-msd-cif's Short Name), then one of:
      - None: no suffix (dsm-cif, plain NEB product name)
      - 'size': ' <size> <size_unit>' from master, always appended
        (ariba-proxy(-txt))
      - 'blank_space': ' (<test_count>)', or a lone trailing space when
        there's no test count (de-boehringer-pharma)
      - 'blank_none': ' (<test_count>)', or nothing when blank (curevac-se)
      - 'qty_test_count': ' (<qty>)', or ' (<qty> ( <test_count> ))' when a
        test count exists - extra nested parens/spaces, verified real, not a
        typo (universiteit-maastricht-cif/intervet-merck-msd-cif)
      - 'test_count_then_qty': ' (<qty>)', or ' (<test_count>) (<qty>)' when
        a test count exists - two separate parenthesized groups, test count
        first (radboud-umc-price-compare)
    qty/test_count come from cst_marketing_name_details.csv; NEB rows (no
    CST marketing data) use '<size> <size_unit>' from products_neb.csv. Every variant
    verified against the real file's full population - see
    docs/audit-decisions.md. raw=True uses cst_product_name(row, raw=True)
    (tags left un-stripped) instead of the tags-stripped variant - see that
    function's docstring for which templates need which."""
    name = cst_product_name(row, raw=raw)
    if comma_mode == "space":
        name = name.replace(",", " ")
    elif comma_mode == "delete":
        name = name.replace(",", "")
    if suffix_style is None:
        return name
    if suffix_style == "size":
        return f"{name} {row['size']} {row['size_unit']}"
    test_count = lookup(row["sku"], "cst_marketing_name_details.csv", "test_count_uni_abbrev", "")
    if suffix_style == "blank_space":
        return f"{name} ({test_count})" if test_count else f"{name} "
    if suffix_style == "blank_none":
        return f"{name} ({test_count})" if test_count else name
    if suffix_style == "qty_test_count":
        qty = lookup(row["sku"], "cst_marketing_name_details.csv", "qty_mkt_txt_abbrev_units", "")
        if not qty and row["manufacturer"] == "New England Biolabs":
            qty = neb_unit_size(row)  # NEB rows, e.g. '(4 ml)', '(145ml)'
        if test_count:
            return f"{name} ({qty} ( {test_count} ))"
        return f"{name} ({qty})"
    if suffix_style == "test_count_then_qty":
        qty = lookup(row["sku"], "cst_marketing_name_details.csv", "qty_mkt_txt_abbrev_units", "")
        if not qty and row["manufacturer"] == "New England Biolabs":
            qty = neb_unit_size(row)  # NEB rows, e.g. '(4 ml)', '(145ml)'
        if test_count:
            return f"{name} ({test_count}) ({qty})"
        return f"{name} ({qty})"
    raise ValueError(f"unknown suffix_style {suffix_style!r}")


def sku_leading_digits(sku):
    """Expression builtin for vwr-de-mel/vwr-uk-mel's Short/Long Description
    English columns: both are suffixed with the SKU's leading digit run, not
    the full SKU - e.g. '64668S' -> '64668', '26339SF' -> '26339' (the
    trailing letter suffix, which distinguishes package size/format variants
    of the same base catalog number, is dropped). Verified real across the
    full population; a SKU with no leading digits at all falls back to the
    SKU unchanged (not observed in practice, but avoids returning an empty
    string)."""
    m = re.match(r"^(\d+)", sku)
    return m.group(1) if m else sku


def clean_merck_description(text):
    """Reproduces DE - Merck KGaA's real Item Description/Short Name text
    cleaning of master's long_description (verified byte-for-byte against
    all 24,765 real rows in both Current and Future Price files): every
    HTML-ish tag (<sup>, </sup>, <i>, </i>, <sub>, </sub>, <p>, </p>, the
    bare self-closing <R/>) becomes a single space, runs of 2+ spaces
    collapse to one, ends are trimmed, and any remaining bare '®' becomes
    the literal text '00AE' (the same garbled-entity artifact documented for
    sap-gdsn-price-de/de-boehringer-austria) - '™' is left untouched, a
    confirmed asymmetry matching those other templates' handling of the same
    two symbols."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("®", "00AE")


def clean_pfizer_description(text):
    """Reproduces US - Pfizer's real Item Description text cleaning of
    master's long_description (verified byte-for-byte against all 26,222
    rows of both the Current and Future Price real files): this is NOT a
    blanket tag-strip like clean_ariba_text/clean_merck_description. The
    real file only ever collapses the two EXACT substrings '<sup>®</sup>'
    and '<sup>™</sup>' (tag and symbol together) down to '(R)'/'(tm)', then
    separately substitutes any remaining bare '®'/'™' character (outside
    a <sup> wrapper) for '(R)'/'(tm)' too. Every other tag form is left
    completely untouched, verified real (not a bug) via full-population
    profiling: '<i>...</i>' (107/107 rows keep the tag - species names in
    italics), '<sub>...</sub>' (19/19 rows keep the tag - e.g. F(ab')<sub>2</sub>
    fragment notation), '<p>...</p>', the bare self-closing '<R/>' placeholder
    (25/25 rows outside <sup>, 13/13 inside it, all keep the literal tag - a
    real asymmetry with clean_ariba_text/clean_merck_description, which both
    convert '<R/>' to '(R)'), the pseudo-escaped '<lt/>'/'<gt/>' tokens (6/6
    rows keep them literal, never decoded back to real angle brackets), and
    any other <sup>...</sup> variant whose inner content isn't exactly one of
    the two symbols above (e.g. '<sup>2</sup>', '<sup>+</sup>', '<sup> </sup>',
    '<sup><R/></sup>') - all left with the tag intact. One SKU family
    ('PathScan<R/> RP Phospho-Rb ...', 5 SKUs) has no <sup>/bare-symbol
    content at all, so it passes through this function completely unchanged -
    the same known upstream-inconsistent family clean_ariba_text's own
    docstring documents for ariba-tiered-txt.

    Also aliased as clean_abbvie_description in EXPR_BUILTINS: punchout-tiered
    (abbvie-hubwoo) independently needed the identical exact-substring rule
    for its own Short/Long Description columns (confirmed byte-for-byte, 0
    mismatches across its own 26,222-row population) - a second, unrelated
    template confirmed to share this exact real cleaning convention."""
    text = text.replace("<sup>®</sup>", "(R)").replace("<sup>™</sup>", "(tm)")
    return text.replace("®", "(R)").replace("™", "(tm)")


EXPR_BUILTINS = {
    "round": round, "float": float, "int": int, "str": str, "len": len, "min": min, "max": max,
    "chr": chr, "Decimal": Decimal,
    "price_for": price_for, "price_for_rounded": price_for_rounded,
    "price_for_rounded_half_up": price_for_rounded_half_up,
    "price_text": price_text,
    "price_for_rounded_number": price_for_rounded_number,
    "clean_coupa_description": clean_coupa_description,
    "clean_coupa_description_short_uk": clean_coupa_description_short_uk,
    "format_date_ddmonyy": format_date_ddmonyy,
    "clean_boehringer_description": clean_boehringer_description,
    "clean_gladstone_description": clean_gladstone_description,
    "strip_trailing_zeros": strip_trailing_zeros,
    "clean_ariba_text": clean_ariba_text, "ariba_name": ariba_name,
    "cst_product_name": cst_product_name, "cst_name_with_suffix": cst_name_with_suffix,
    "boehringer_cif_short_name": boehringer_cif_short_name,
    "boehringer_cif_item_description": boehringer_cif_item_description,
    "fisher_us_description": fisher_us_description,
    "neb_unit_size": neb_unit_size,
    "format_neb_size": format_neb_size,
    "sku_leading_digits": sku_leading_digits,
    "leading_digits": sku_leading_digits,
    "clean_merck_description": clean_merck_description,
    "clean_pfizer_description": clean_pfizer_description,
    "clean_abbvie_description": clean_pfizer_description,
}
EXPR_GLOBALS = {"__builtins__": EXPR_BUILTINS}

UNSPSC_PATTERN = re.compile(r"\d{8}")
CAS_PATTERN = re.compile(r"\d{2,7}-\d{2}-\d")

PREVIEW_ROW_LIMIT = 20


_MASTER_CACHE = {}  # filename -> (mtime, rows)


def load_master(filename="products.csv"):
    """Loads a master dataset from master/<filename>. Templates default to
    the CST master (products.csv); a template can set "master_source" to
    point at a different brand's dataset (e.g. products_neb.csv for New
    England Biolabs) as long as it's shaped as the same 19-column schema.
    "master_source" can also be a list of filenames (e.g. the BIPP/Bioke
    Benelux files, which carry both CST and NEB brand SKUs in one real
    catalog) - rows from each are concatenated in list order.

    Cached per filename, keyed on the file's mtime: a plain `generate_catalog.py
    all` run (or the web UI's "Regenerate All Customers") calls this once per
    customer - up to 238 times - and re-parsing the same 26,222-row CSV that
    many times was the entire reason a full run took 10+ minutes. The mtime
    check means an edit to master/products.csv (or a re-import) is picked up
    on the very next call, in the CLI and in the long-lived web server alike -
    this is not a "restart to see changes" cache.

    Callers must treat the returned rows as read-only: they're the same
    list/dicts on every cache hit, not a fresh copy. Nothing in this file or
    webapp/backend/main.py mutates a row in place today (verified) - keep it
    that way, or copy before mutating.
    """
    if isinstance(filename, list):
        rows = []
        for fn in filename:
            rows.extend(load_master(fn))
        return rows
    path = MASTER_CSV.parent / filename
    mtime = path.stat().st_mtime
    cached = _MASTER_CACHE.get(filename)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    _MASTER_CACHE[filename] = (mtime, rows)
    return rows


_LOOKUP_CACHE = {}  # filename -> (mtime, {sku: row})


def load_lookup(filename):
    """Loads a small sku-keyed supplementary CSV from master/<filename> (first
    column is the key, by convention 'sku') - for real per-SKU data that
    exists in a customer's real source file but has no corresponding column
    in master/products.csv (e.g. VWR's customs Commodity Code, extracted
    verbatim from the real Benelux_VWR_Catalog.xlsx, not derivable from any
    other master field). Cached per filename the same way load_master() is.
    Use via the `lookup()` expression builtin, not directly from a template."""
    path = MASTER_CSV.parent / filename
    mtime = path.stat().st_mtime
    cached = _LOOKUP_CACHE.get(filename)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        key_field = reader.fieldnames[0]
        table = {row[key_field]: row for row in reader}
    _LOOKUP_CACHE[filename] = (mtime, table)
    return table


def lookup(sku, filename, column, default=""):
    """Expression builtin: lookup(row['sku'], 'some_lookup.csv', 'column_name').
    Returns `default` (not an error) for a SKU absent from the lookup file -
    matches the real files this replicates, where the same gap is just a
    blank cell, not a rejected row."""
    table = load_lookup(filename)
    entry = table.get(sku)
    return entry.get(column, default) if entry else default


EXPR_BUILTINS["lookup"] = lookup


# Set by scripts/retire_snapshot_rows.py: build_rows() then skips
# real_cell_snapshot.csv cells, to see which rows the normal expressions
# now reproduce.
SNAPSHOT_DISABLED = False


def master_fingerprint():
    """sha256 over every master/*.csv except the snapshot itself - the data
    the snapshot's fallback expressions read. Recorded in
    real_cell_snapshot.meta.json by retire_snapshot_rows.py."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted((ROOT / "master").glob("*.csv")):
        if p.name == "real_cell_snapshot.csv":
            continue
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


_snapshot_review_warning = None


def check_snapshot_review(customer):
    """Warning: master data changed since real_cell_snapshot.csv was last
    reviewed (retire_snapshot_rows.py), so some copied cells may now be
    reproducible - or out of date. Only for customers that have snapshot
    cells; computed once per process."""
    global _snapshot_review_warning
    if not customer_snapshot(customer.get("display_name", "")):
        return []
    if _snapshot_review_warning is None:
        meta_path = ROOT / "master" / "real_cell_snapshot.meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("master_fingerprint") == master_fingerprint():
            _snapshot_review_warning = ""
        else:
            reviewed = meta.get("reviewed_at", "never")
            _snapshot_review_warning = (
                f"master data changed since real_cell_snapshot.csv was last reviewed ({reviewed}); "
                "copied real-file cells may be out of date or now reproducible - run "
                "scripts/retire_snapshot_rows.py"
            )
    return [_snapshot_review_warning] if _snapshot_review_warning else []


def neb_pack_quantity(size):
    """Expression builtin (bipp-articles PurchaseUnit/StockUnit, NEB rows):
    the real BIPP file writes NEB pack quantities as whole-number (int)
    cells derived from master/products_neb.csv's inconsistently formatted
    `size`. Verified across all 1,617 real NEB rows (2026-09-25): blank ->
    1; a thousands-style value ('1.000', '2,500', '10.000') -> the integer
    with the separator removed; any other decimal ('0,5', '1.1', '9,6',
    '6,0', '0,063') -> rounded UP to the next whole number (0.5 -> 1,
    1.1 -> 2, 6.0 -> 6). A value counts as thousands-style when every
    group after the first is exactly 3 digits and the first group doesn't
    start with '0'."""
    size = (size or "").strip()
    if not size:
        return 1
    parts = re.split(r"[.,]", size)
    if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and not parts[0].startswith("0"):
        return int("".join(parts))
    from decimal import ROUND_CEILING
    value = Decimal(size.replace(",", "."))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


EXPR_BUILTINS["neb_pack_quantity"] = neb_pack_quantity


def clean_vwr_long_description(text):
    """Expression builtin (vwr-network-us LongDescription, vwr-de-mel /
    vwr-uk-mel Long Description English): like clean_ariba_text(text, True,
    False) - decode '<lt/>'/'<gt/>', unwrap <i>/<sub>/<sup>, bare '®'/'™' ->
    '(R)'/'(tm)' - except the '<R/>' placeholder and <p>/</p> tags are kept
    raw. Verified 2026-09-25 against every real US/DE/UK VWR row (e.g.
    'PathScan<R/> RP ...', '<p>PathScan<R/> ...</p><p> </p>'), which
    clean_ariba_text turned into '(R)' / stripped."""
    text = text.replace("<lt/>", "<").replace("<gt/>", ">")
    for tag in ("<i>", "</i>", "<sub>", "</sub>", "<sup>", "</sup>"):
        text = text.replace(tag, "")
    return text.replace("®", "(R)").replace("™", "(tm)")


EXPR_BUILTINS["clean_vwr_long_description"] = clean_vwr_long_description


# Shared helpers for expressions repeated across templates (refactor
# 2026-10-02; output byte-identical to the inline expressions they replace).

_GENERIC_SDS_PAGE = "https://www.cellsignal.com/contents/technical/safety-data-sheet-(sds)/resources-safety-data-sheets"


def msds_url_or_blank(row):
    """Blank when master only has the generic SDS resources page, else the
    product's SDS link (14 columns across adhoc/distributor/labviva/sciquest...)."""
    return "" if row["msds_url"] == _GENERIC_SDS_PAGE else row["msds_url"]


def raw_price_gbp(row, customer):
    """Raw GBP list price string from cst_uk_clp.csv (whole numbers stay '614'),
    period-aware, falling back to the 2dp master GBP price."""
    col = "uk_clp_future" if customer.get("price_period") == "future" else "uk_clp"
    return lookup(row["sku"], "cst_uk_clp.csv", col) or price_for_rounded(row, "list_price_gbp", customer, 0)


def raw_price_eur(row, customer):
    """Raw EUR list price string from cst_dkfz_bmecat_fields.csv, period-aware,
    falling back to the 2dp master EUR price."""
    col = "eu_future_list_price" if customer.get("price_period") == "future" else "eu_list_price"
    return lookup(row["sku"], "cst_dkfz_bmecat_fields.csv", col) or price_for_rounded(row, "list_price_eur", customer, 0)


def date_mmddyy(date_str):
    """ISO date -> 'MM/DD/YY' text ('2026-12-31' -> '12/31/26')."""
    return date_str[5:7] + "/" + date_str[8:10] + "/" + date_str[2:4]


def date_mdyyyy(date_str):
    """ISO date -> unpadded 'M/D/YYYY' text ('2026-01-05' -> '1/5/2026')."""
    return str(int(date_str[5:7])) + "/" + str(int(date_str[8:10])) + "/" + date_str[:4]


_PRODUCT_TYPE_UNSPSC = {
    'Monoclonal Antibody': '32160702',
    'Matched Antibody Pair': '32160702',
    'Secondary Antibody': '32160702',
    'FastScan ELISA Kit': '32160605',
    'Polyclonal Antibody': '32160702',
    'ELISA Kit': '32160605',
    'Antibody Sampler Kit': '32160702',
    'Protein Control': '34160400',
    'Growth Factors and Cytokines': '34160400',
    'Antibody Duet': '32160702',
    'Cell Extracts': '32160404',
    'Miscellaneous': '32169090',
    'Primer Set': '32160502',
    'Chemical Modulators': '32169090',
    'Buffer': '32169090',
    'PTMScan': '32161000',
    'Assay Kit': '32161000',
    'Cellular Dyes': '32160906',
    'Cell Extract Kit': '32160401',
    'ELISA Antibody Pair': '32160605',
    'Affinity and Purification Reagents': '32169090',
    'CUT&RUN Kit': '32161000',
    'Detection System Kit': '32161000',
    'siRNA': '34160900',
    'IHC Control Slides': '32160905',
    'IHC Kit': '32160905',
    'Buffer Kit': '32169090',
    'Enzyme': '34160500',
    'MW Marker Kit': '32160403',
    'MW Marker': '32160403',
    'Antibody Cocktail': '32160702',
    'Detection System': '32161000',
    'Avidin and Biotin': '32160404',
    'Protein Control Kit': '34160400',
    'CUT&Tag Kit': '32161000',
    'ChIP Kit': '32161000',
}


def unspsc_for_product_type(row):
    """UNSPSC-style class code by product_classification, blank if unmapped
    (de-helf ECLASS-5.1, max-planck-eclass, the three sap-gdsn REFERENCE_FEATURE_GROUP_ID)."""
    return _PRODUCT_TYPE_UNSPSC.get(row["product_classification"], "")


def ariba_name_with_size(row):
    """ariba_name() + ' ' + marketing size text (cst_marketing_name_details.csv,
    fallback size + unit); 9 name columns in 7 punchout templates."""
    size = lookup(row["sku"], "cst_marketing_name_details.csv", "qty_mkt_txt_abbrev_units", row["size"] + " " + row["size_unit"])
    return ariba_name(row) + " " + size


for _fn in (msds_url_or_blank, raw_price_gbp, raw_price_eur, date_mmddyy, date_mdyyyy,
            unspsc_for_product_type, ariba_name_with_size):
    EXPR_BUILTINS[_fn.__name__] = _fn


def load_customer(slug):
    path = CUSTOMERS_DIR / f"{slug}.json"
    if not path.exists():
        raise SystemExit(f"Unknown customer '{slug}' (no {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def load_template(name):
    path = TEMPLATES_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"Unknown template '{name}' (no {path})")
    return json.loads(path.read_text(encoding="utf-8"))


_COMPILED_EXPR_CACHE = {}  # expression string -> compiled code object


def _compile_expr(expr):
    """eval() re-parses and re-compiles a raw string into bytecode on every
    single call. build_rows() used to hand it the same expression string up
    to 26,222 times per column per customer - across a full run that's the
    dominant cost by far (profiled: ~2s/customer in eval() alone vs ~0.1s to
    load master data). Compiling each unique expression once and reusing the
    code object cuts that repeated parse/compile work out entirely. Cached
    globally (not per-template) since many templates share trivial
    expressions like "row['sku']"."""
    code = _COMPILED_EXPR_CACHE.get(expr)
    if code is None:
        code = compile(expr, "<template-expression>", "eval")
        _COMPILED_EXPR_CACHE[expr] = code
    return code


def snapshot_label(headers, i):
    """Snapshot column label: the header, or "HEADER [col N]" when the header
    repeats in the template (sap-gdsn-price-de's two PRICE_AMOUNT columns) -
    the same labels compare_catalog/the scoreboard use."""
    h = headers[i]
    return f"{h} [col {i + 1}]" if headers.count(h) > 1 else h


_customer_snapshots = {}


def customer_snapshot(display_name):
    """{(column label, sku): value} of real_cell_snapshot.csv rows for one
    customer display_name, typed per the row's `type` column; cached."""
    mtime = (ROOT / "master" / "real_cell_snapshot.csv").stat().st_mtime
    if _customer_snapshots.get(display_name, (None,))[0] != mtime:
        cells = {}
        prefix = display_name + "|"
        for key, entry in load_lookup("real_cell_snapshot.csv").items():
            if not key.startswith(prefix):
                continue
            label, sku = key[len(prefix):].rsplit("|", 1)
            value, kind = entry.get("value", ""), entry.get("type", "")
            cells[(label, sku)] = int(value) if kind == "int" else float(value) if kind == "float" else value
        _customer_snapshots[display_name] = (mtime, cells)
    return _customer_snapshots[display_name][1]


def build_rows(master_rows, template, customer):
    """A column can set "key" to disambiguate a real source file's genuinely
    duplicate header name (e.g. sap-gdsn-price-de's two 'PRICE_AMOUNT'
    columns) - out_row is keyed by that, defaulting to the header text
    itself, so every template without an explicit "key" is unaffected.
    write_xlsx()/write_csv() must be given the matching `keys` list to look
    values back up correctly - see their own docstrings."""
    discount = customer.get("discount_percentage", 0)
    # Resolve each column's compiled code once per generate() call, not once
    # per row - the row loop below only does the actual eval() per cell.
    headers = [col["header"] for col in template["columns"]]
    compiled_columns = [
        (col.get("key", col["header"]), _compile_expr(col["expression"]),
         snapshot_label(headers, i))
        for i, col in enumerate(template["columns"])
    ]
    # Cells copied from the real file (master/real_cell_snapshot.csv) replace
    # the expression's value for this customer - applied to every column, so
    # templates need no per-column wrapper (2026-10-02; replaced 229
    # real_cell_snapshot() wrappers, output identical).
    snapshot = {} if SNAPSHOT_DISABLED else customer_snapshot(customer.get("display_name", ""))
    out_rows = []
    for row in master_rows:
        out_row = {}
        local_vars = {"row": row, "discount": discount, "customer": customer}
        for key, code, label in compiled_columns:
            try:
                out_row[key] = eval(code, EXPR_GLOBALS, local_vars)
            except Exception as e:
                raise SystemExit(f"Expression failed for column '{key}' on SKU {row.get('sku')}: {e}")
            if snapshot:
                cell = snapshot.get((label, row.get("sku")))
                if cell is not None:
                    out_row[key] = cell
        out_rows.append(out_row)
    return out_rows


def check_master_data(master_rows):
    """Integrity checks on the master product list itself — independent of any
    customer/template, since every catalog inherits whatever is wrong here."""
    errors = []
    seen_skus = set()
    for i, row in enumerate(master_rows):
        sku = (row.get("sku") or "").strip()
        if not sku:
            errors.append(f"master row {i}: blank sku")
        elif sku in seen_skus:
            errors.append(f"master row {i}: duplicate sku '{sku}'")
        else:
            seen_skus.add(sku)

        if not (row.get("short_description") or "").strip():
            errors.append(f"master row {i} ({sku}): blank short_description")

        price = row.get("list_price_usd", "")
        try:
            if float(price) <= 0:
                errors.append(f"master row {i} ({sku}): list_price_usd not positive: {price!r}")
        except (TypeError, ValueError):
            errors.append(f"master row {i} ({sku}): non-numeric list_price_usd: {price!r}")

        unspsc = (row.get("unspsc") or "").strip()
        if unspsc and not UNSPSC_PATTERN.fullmatch(unspsc):
            errors.append(f"master row {i} ({sku}): malformed unspsc: {unspsc!r}")

        cas = (row.get("cas_number") or "").strip()
        if cas and not CAS_PATTERN.fullmatch(cas):
            errors.append(f"master row {i} ({sku}): malformed cas_number: {cas!r}")

    return errors


def master_row_flags(row):
    """Per-row master-data issues for the web UI's Master Data browser.

    Mirrors check_master_data's field-level rules (keep the two in sync if
    either changes) but scoped to a single row — duplicate-SKU detection
    needs the full master list, so callers handle that separately.
    """
    flags = []
    if not (row.get("sku") or "").strip():
        flags.append("blank SKU")
    if not (row.get("short_description") or "").strip():
        flags.append("blank description")

    price = row.get("list_price_usd", "")
    try:
        if float(price) <= 0:
            flags.append("price not positive")
    except (TypeError, ValueError):
        flags.append("non-numeric price")

    unspsc = (row.get("unspsc") or "").strip()
    if unspsc and not UNSPSC_PATTERN.fullmatch(unspsc):
        flags.append("malformed UNSPSC")

    cas = (row.get("cas_number") or "").strip()
    if cas and not CAS_PATTERN.fullmatch(cas):
        flags.append("malformed CAS #")

    return flags


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_config_sanity(customer, template):
    """Validates the config/template pairing itself, before any row is built —
    a typo'd column name here would otherwise surface only indirectly (e.g.
    every row's SKU column reading blank)."""
    errors = []
    headers = {c["header"] for c in template["columns"]}

    sku_col = template.get("sku_column")
    if sku_col and sku_col not in headers:
        errors.append(f"template '{template['template_name']}': sku_column '{sku_col}' is not a declared column")

    for col in template.get("price_columns", []):
        if col not in headers:
            errors.append(f"template '{template['template_name']}': price_columns entry '{col}' is not a declared column")

    return errors


def check_price_relationships(rows, template, customer):
    """Cross-column price sanity: tiered pricing should not increase with
    volume, and a 'discounted' price should never exceed its list price —
    and should reconcile with one of the customer's configured discounts,
    which catches a broken template expression rather than just a bad value."""
    errors = []
    headers = [c["header"] for c in template["columns"]]
    sku_col = template.get("sku_column")

    tier_cols = [c for c in ("Price 1", "Price 2", "Price 3") if c in headers]
    has_list_discount = "List Price" in headers and "Discounted Price" in headers
    if len(tier_cols) < 2 and not has_list_discount:
        return errors

    allowed_discounts = set(customer.get("category_discounts", {}).values())
    if "discount_percentage" in customer:
        allowed_discounts.add(customer["discount_percentage"])

    for i, row in enumerate(rows):
        sku = row.get(sku_col) if sku_col else None

        if len(tier_cols) >= 2:
            vals = [_as_float(row.get(c)) for c in tier_cols]
            present = [v for v in vals if v is not None]
            if len(present) >= 2 and any(present[j] < present[j + 1] for j in range(len(present) - 1)):
                errors.append(f"row {i} ({sku}): tiered prices not non-increasing: {dict(zip(tier_cols, vals))}")

        if has_list_discount:
            lp, dp = _as_float(row.get("List Price")), _as_float(row.get("Discounted Price"))
            if lp is not None and dp is not None:
                if dp > lp:
                    errors.append(f"row {i} ({sku}): Discounted Price {dp} exceeds List Price {lp}")
                elif allowed_discounts and lp > 0:
                    implied = 100 * (1 - dp / lp)
                    if not any(abs(implied - d) < 0.5 for d in allowed_discounts):
                        errors.append(
                            f"row {i} ({sku}): Discounted Price implies a {implied:.1f}% discount, which "
                            f"doesn't match any configured discount ({sorted(allowed_discounts)})"
                        )

    return errors


def check_required_fields(rows, template):
    """Blank required fields (heuristically: any column with 'description' in
    its header) pass every other check today but are useless to the buyer.

    A column can opt out via `"allow_blank_description": true` when a real
    source file has been verified to genuinely, legitimately leave that
    specific field blank - e.g. ghx-gdsn-nl-hospital's 'Omschrijving
    Duurzaamheid' (Sustainability Description) is blank in 100% of every
    real row checked, and 'Link Artikelinformatie' (Link Article
    Description) is blank for the same small set of orphaned SKUs (no
    product_url) that other lookup-sourced columns already leave blank.
    Before this flag existed, both columns were forced to a literal 'N/A'
    just to satisfy this check - fabricated data the real files don't
    contain, found by a direct side-by-side comparison against the real
    file (not caught by any prior byte-for-byte column verification, which
    only ever compared populated values, never asked whether a column
    *should* be blank). Use sparingly, only after confirming a genuine
    real-file pattern - this bypasses the one check designed to catch
    exactly this kind of gap."""
    errors = []
    sku_col = template.get("sku_column")
    required_headers = [
        c["header"] for c in template["columns"]
        if "description" in c["header"].lower() and not c.get("allow_blank_description")
    ]
    if not required_headers:
        return errors

    for i, row in enumerate(rows):
        sku = row.get(sku_col) if sku_col else None
        for h in required_headers:
            val = row.get(h)
            if val is None or str(val).strip() == "":
                errors.append(f"row {i} ({sku}): blank required field '{h}'")

    return errors


def _row_count_in_file(path, delimiter=",", header_row=True):
    """`header_row` mirrors a template's csv_format.header_row (see
    write_csv()) - a headerless .csv (ariba-current-csv) mustn't have a
    phantom header subtracted. Blank lines (e.g. that template's real leading
    empty line) are skipped rather than counted as rows. Read as 'utf-8-sig'
    so a BOM-prefixed file (csv_format.bom, see write_csv()) has its BOM
    stripped rather than glued onto the first header field - identical to
    plain 'utf-8' for every file without one."""
    if path.suffix == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            n = sum(1 for r in csv.reader(f, delimiter=delimiter) if r)
            return n - 1 if header_row else n
    if path.suffix == ".xlsx":
        # This only needs a COUNT, not the row values - reading `max_row` off
        # the sheet's <dimension> metadata (populated because every catalog
        # here is written by write_xlsx()/openpyxl itself) is ~500x faster
        # than iterating every cell of every row just to count them, which is
        # what this used to do. Profiled as the actual dominant cost of a
        # full `generate_catalog.py all` run (~5s per customer, ~20 minutes
        # across 238) - far more than eval() or master-CSV parsing.
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True)
        try:
            return max(wb.active.max_row - (1 if header_row else 0), 0)
        finally:
            wb.close()
    return None


def check_row_count_regression(slug, new_row_count, template=None, threshold_pct=10):
    """Non-blocking: compares against the most recently generated catalog for
    this customer, since a large swing usually signals a bad master-data load
    rather than an intentional catalog change.

    Passes the template's own `delimiter` (default ",") through to
    _row_count_in_file() for a .csv template - added 2026-09-24 alongside
    google-merchant-center's rebuild (its real files, and the catalogs this
    generates for them, are semicolon-delimited). Without this, a
    semicolon-delimited .csv's previous output was mis-parsed with the wrong
    delimiter and produced a nonsense row count (every field of every line
    read as one giant unsplit column, inflated further by any embedded
    newline inside an unquoted multi-line cell) - a real generic bug, not
    specific to one template, that happened to go unnoticed until now:
    universitat-heidelberg/unimarket also generate semicolon-delimited output
    (see docs/reference.md's CSV delimiter bullet), but this bug wasn't caught until
    google-merchant-center's audit specifically diffed a regenerated output
    against its own immediately-prior semicolon-delimited run and noticed
    the nonsense row count."""
    warnings = []
    out_dir = CATALOGS_DIR / slug
    if not out_dir.exists():
        return warnings

    existing = sorted(
        (p for p in out_dir.iterdir() if p.is_file() and not p.name.startswith((".", "~$"))),
        key=lambda p: p.stat().st_mtime,
    )
    if not existing:
        return warnings

    last_path = existing[-1]
    delimiter = (template or {}).get("delimiter", ",")
    header_row = (template or {}).get("csv_format", {}).get("header_row", True)
    try:
        prev_row_count = _row_count_in_file(last_path, delimiter=delimiter, header_row=header_row)
    except Exception:
        return warnings
    if not prev_row_count:
        return warnings

    change_pct = 100 * (new_row_count - prev_row_count) / prev_row_count
    if abs(change_pct) >= threshold_pct:
        direction = "dropped" if change_pct < 0 else "increased"
        warnings.append(
            f"row count {direction} {abs(change_pct):.1f}% vs last generated catalog "
            f"({last_path.name}: {prev_row_count} -> {new_row_count} rows)"
        )
    return warnings


def check_category_discount_distribution(rows, template, customer):
    """Non-blocking visibility into a category-discount heuristic (e.g. a
    description-text 'kit' detection): reconstructs which configured discount
    each row's realized price implies and reports the breakdown, without
    duplicating whatever text heuristic the template expression used."""
    warnings = []
    category_discounts = customer.get("category_discounts")
    headers = [c["header"] for c in template["columns"]]
    if not category_discounts or "List Price" not in headers or "Discounted Price" not in headers:
        return warnings

    counts = {cat: 0 for cat in category_discounts}
    unmatched = 0
    for row in rows:
        lp, dp = _as_float(row.get("List Price")), _as_float(row.get("Discounted Price"))
        if not lp or dp is None:
            unmatched += 1
            continue
        implied = 100 * (1 - dp / lp)
        matched = next((cat for cat, pct in category_discounts.items() if abs(implied - pct) < 0.5), None)
        if matched:
            counts[matched] += 1
        else:
            unmatched += 1

    breakdown = ", ".join(f"{cat}: {n}" for cat, n in counts.items())
    if unmatched:
        breakdown += f", unmatched: {unmatched}"
    warnings.append(f"category discount distribution — {breakdown}")
    return warnings


def check_override_coverage(rows, customer):
    """Non-blocking: warns when a SKU-keyed override (e.g. Zageno's regional
    prices) doesn't cover the full row population, so gaps are visible instead
    of silently rendering as blank cells."""
    warnings = []
    regional = customer.get("overrides", {}).get("regional_prices")
    if regional is None:
        return warnings

    total = len(rows)
    covered = len(regional)
    missing = total - covered
    if missing > 0 and total:
        pct = 100 * covered / total
        warnings.append(
            f"overrides.regional_prices covers {covered} of {total} SKUs ({pct:.1f}%); "
            f"{missing} row(s) will have blank regional/contract pricing"
        )
    return warnings


def _sibling_slug(slug):
    """The Current/Future counterpart slug for a customer, per the '-future'
    suffix convention (e.g. bayer-de <-> bayer-de-future) - same convention
    the web UI's sibling-nav link (isFutureSlug/siblingSlug in index.html)
    already uses. Purely a name transform; existence is checked by the caller."""
    if slug.endswith("-future"):
        return slug[: -len("-future")]
    return f"{slug}-future"


def _strip_period_wording(name):
    """Normalizes a display_name for cross-period comparison by removing the
    Current/Future wording however it's phrased in this system - a bare word
    ("DE - HELF Current Price") or a parenthetical suffix
    ("Ariba - 0% Discount (Market Rate) (Future Price)") - so the check
    compares the underlying name, not the period label itself."""
    name = re.sub(r"\(?\b(current|future)\s*price\)?", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(current|future)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\(\s*\)", "", name)
    return re.sub(r"\s{2,}", " ", name).strip(" -")


def check_cross_period_consistency(slug, customer):
    """Non-blocking: a Current/Future pair is meant to be the same negotiated
    contract exported for two price periods - same template and file type,
    same discount terms, contiguous (non-overlapping) validity windows, and
    display names that differ only by the Current/Future wording. Flags drift
    in the customer *configs* themselves.

    Deliberately does NOT compare realized prices or re-litigate known
    per-template quirks (e.g. a template whose expression doesn't actually
    vary by price_period, or a Future file that reuses a genuinely different
    real-world layout) - those are documented, intentional replications of
    real source-file behavior, not config drift. Reported once per pair, from
    the Current side, so generating both siblings in one run doesn't produce
    the same warning twice.

    Since 2026-10-02 it runs from both sides: the business-user workflow test
    changed only bayer-de-future's discount ("12% from 1 January") and got no
    warning, because only the Current side ran the check. Generating both
    siblings in one run now reports a pair's drift on each of them."""
    warnings = []
    sibling_slug = _sibling_slug(slug)
    sibling_path = CUSTOMERS_DIR / f"{sibling_slug}.json"
    if not sibling_path.exists():
        return warnings

    sibling = load_customer(sibling_slug)
    label = f"cross-period vs '{sibling_slug}'"
    current, future = (sibling, customer) if slug.endswith("-future") else (customer, sibling)

    if customer.get("template_ref") != sibling.get("template_ref"):
        warnings.append(
            f"{label}: template_ref differs ({customer.get('template_ref')!r} vs {sibling.get('template_ref')!r})"
        )
    if customer.get("file_type") != sibling.get("file_type"):
        warnings.append(
            f"{label}: file_type differs ({customer.get('file_type')!r} vs {sibling.get('file_type')!r})"
        )
    if customer.get("discount_percentage") != sibling.get("discount_percentage"):
        warnings.append(
            f"{label}: discount_percentage differs "
            f"({customer.get('discount_percentage')!r} vs {sibling.get('discount_percentage')!r})"
        )
    if customer.get("category_discounts") != sibling.get("category_discounts"):
        warnings.append(f"{label}: category_discounts differ")

    cust_inst = customer.get("institutions") or {}
    sib_inst = sibling.get("institutions") or {}
    if set(cust_inst) != set(sib_inst):
        warnings.append(f"{label}: institutions differ ({sorted(set(cust_inst) ^ set(sib_inst))} not shared)")
    else:
        for key, inst in cust_inst.items():
            sib_pct = (sib_inst.get(key) or {}).get("discount_percentage")
            cust_pct = (inst or {}).get("discount_percentage")
            if cust_pct != sib_pct:
                warnings.append(
                    f"{label}: institutions.{key}.discount_percentage differs ({cust_pct!r} vs {sib_pct!r})"
                )

    try:
        cur_until = date.fromisoformat(current["price_valid_until"])
        fut_from = date.fromisoformat(future["price_valid_from"])
        if fut_from < cur_until:
            warnings.append(
                f"{label}: the Future price_valid_from ({fut_from}) is before the Current "
                f"price_valid_until ({cur_until}) - overlapping validity windows"
            )
    except (KeyError, ValueError):
        pass

    cur_name = _strip_period_wording(current.get("display_name") or "")
    fut_name = _strip_period_wording(future.get("display_name") or "")
    if cur_name != fut_name:
        warnings.append(
            f"{label}: display_name doesn't match beyond the Current/Future wording "
            f"({customer.get('display_name')!r} vs {sibling.get('display_name')!r})"
        )

    return warnings


def run_quality_checks(rows, template, customer):
    errors = []

    discount = customer.get("discount_percentage", 0)
    if not (0 <= discount <= 100):
        errors.append(f"discount_percentage {discount} is out of range [0, 100]")

    for category, pct in customer.get("category_discounts", {}).items():
        if not (0 <= pct <= 100):
            errors.append(f"category_discounts.{category} {pct} is out of range [0, 100]")

    for key, institution in customer.get("institutions", {}).items():
        pct = institution.get("discount_percentage")
        if pct is not None and not (0 <= pct <= 100):
            errors.append(f"institutions.{key}.discount_percentage {pct} is out of range [0, 100]")

    try:
        start = date.fromisoformat(customer["price_valid_from"])
        end = date.fromisoformat(customer["price_valid_until"])
        if not start < end:
            errors.append(f"price_valid_from ({start}) is not before price_valid_until ({end})")
    except (KeyError, ValueError) as e:
        errors.append(f"invalid price validity window: {e}")

    sku_col = template.get("sku_column")
    seen_skus = set()
    for i, row in enumerate(rows):
        sku = row.get(sku_col) if sku_col else None
        if sku_col:
            if not sku:
                errors.append(f"row {i}: blank value in SKU column '{sku_col}'")
            elif sku in seen_skus:
                errors.append(f"duplicate SKU '{sku}' in output")
            else:
                seen_skus.add(sku)

        for col in template.get("price_columns", []):
            val = row.get(col)
            if val in ("", None):
                continue
            # Optional template-level "price_prefix" (e.g. dana-farber's
            # literal '$', confirmed on every real List Price/DFCI Price
            # TEXT cell): strip it before the numeric check so the real
            # file's currency-prefixed text can be replicated exactly
            # instead of dropped. Absent by default - no other template
            # is affected.
            prefix = template.get("price_prefix")
            if prefix and isinstance(val, str) and val.startswith(prefix):
                val = val[len(prefix):]
            # Optional template-level "decimal_comma" (eu-oms: real prices
            # are TEXT '510,00'): parse the comma as the decimal separator.
            if template.get("decimal_comma") and isinstance(val, str):
                val = val.replace(",", ".")
            try:
                if float(val) < 0:
                    errors.append(f"row {i} ({sku}): negative price in column '{col}': {val}")
            except (TypeError, ValueError):
                errors.append(f"row {i} ({sku}): non-numeric price in column '{col}': {val!r}")

    return errors


# A browsable inventory of every check the generator runs, independent of
# whether any of them are currently firing for a given customer — kept next
# to the functions it describes so it can't drift out of sync with what the
# code actually does. Consumed by the web UI's Validations tab via GET
# /checks; mirrors the "Quality checks" section of docs/reference.md.
CHECK_REGISTRY = [
    {
        "key": "config_sanity",
        "label": "Config sanity",
        "severity": "blocking",
        "function": "check_config_sanity",
        "description": "sku_column and price_columns must exist among the template's declared columns.",
        "scope": "Every customer, before rows are built.",
    },
    {
        "key": "master_data",
        "label": "Master data integrity",
        "severity": "blocking",
        "function": "check_master_data",
        "description": "No blank/duplicate SKU or description; price numeric and > 0; UNSPSC is 8 digits; CAS # format valid (when present).",
        "scope": "Master product list, once per run.",
    },
    {
        "key": "discount_range",
        "label": "Discount range",
        "severity": "blocking",
        "function": "run_quality_checks",
        "description": "discount_percentage / category_discounts / institutions.*.discount_percentage all in [0, 100].",
        "scope": "Customers with a discount field.",
    },
    {
        "key": "price_validity_window",
        "label": "Price validity window",
        "severity": "blocking",
        "function": "run_quality_checks",
        "description": "price_valid_from must be before price_valid_until.",
        "scope": "Every customer.",
    },
    {
        "key": "output_skus",
        "label": "Output SKUs",
        "severity": "blocking",
        "function": "run_quality_checks",
        "description": "No blank or duplicate SKUs in the generated output.",
        "scope": "Every customer.",
    },
    {
        "key": "price_columns",
        "label": "Price columns",
        "severity": "blocking",
        "function": "run_quality_checks",
        "description": "No negative or non-numeric values in any declared price_columns (after stripping the template's optional \"price_prefix\", e.g. a literal '$'; a template with \"decimal_comma\": true parses '510,00' as 510.00).",
        "scope": "Every template.",
    },
    {
        "key": "price_relationships",
        "label": "Price relationships",
        "severity": "blocking",
        "function": "check_price_relationships",
        "description": "Tiered prices non-increasing; Discounted Price ≤ List Price; implied discount matches a configured discount.",
        "scope": "Tiered or list/discounted-price templates.",
    },
    {
        "key": "required_fields",
        "label": "Required fields",
        "severity": "blocking",
        "function": "check_required_fields",
        "description": "No blank values in any description column, unless that column sets \"allow_blank_description\": true (opt-out for a column verified to be genuinely, legitimately blank in the real source file).",
        "scope": "Templates with a description column.",
    },
    {
        "key": "override_coverage",
        "label": "Override coverage",
        "severity": "warning",
        "function": "check_override_coverage",
        "description": "Flags incomplete SKU-keyed overrides (e.g. regional prices) instead of rendering silently blank.",
        "scope": "Customers with overrides.regional_prices.",
    },
    {
        "key": "row_count_regression",
        "label": "Row-count regression",
        "severity": "warning",
        "function": "check_row_count_regression",
        "description": "Warns on a ≥10% row-count swing vs. the last generated catalog.",
        "scope": "Customers with a prior catalog.",
    },
    {
        "key": "category_discount_distribution",
        "label": "Category discount distribution",
        "severity": "warning",
        "function": "check_category_discount_distribution",
        "description": "Reports row counts per category_discounts bucket, reconstructed from realized prices.",
        "scope": "Customers with category_discounts.",
    },
    {
        "key": "cross_period_consistency",
        "label": "Cross-period consistency",
        "severity": "warning",
        "function": "check_cross_period_consistency",
        "description": (
            "Current/Future sibling pairs (e.g. bayer-de / bayer-de-future) should share the same "
            "template, file type, discount terms, and contiguous validity windows, and differ only "
            "in the Current/Future wording of their display_name."
        ),
        "scope": "Customers with a '-future' sibling config.",
    },
    {
        "key": "snapshot_review",
        "label": "Real-cell snapshot review",
        "severity": "warning",
        "function": "check_snapshot_review",
        "description": (
            "master/*.csv changed since master/real_cell_snapshot.csv was last reviewed by "
            "scripts/retire_snapshot_rows.py, so cells copied from real files may be out of date "
            "or now reproducible from master data."
        ),
        "scope": "Customers with cells in master/real_cell_snapshot.csv.",
    },
]


def write_csv(rows, headers, path, delimiter=",", keys=None, csv_format=None):
    """A template can set "delimiter" (e.g. ";") to match its real source
    file - several real DE/EU CSV exports (Universitat Heidelberg, UniMarket,
    Uniklinikum Erlangen) use semicolons, not commas. Defaults to comma so
    every existing template without a "delimiter" field is unaffected.

    `keys` (same length as `headers`, positional) looks up each row's value
    by a different internal key than the header text actually written - see
    the `keys` note on write_xlsx() for why this exists. Written with a plain
    csv.writer (not DictWriter) since DictWriter can't handle duplicate
    fieldnames.

    `csv_format` (optional template key, added 2026-09-25 for
    ariba-current-csv) replicates real container quirks a plain csv.writer
    can't: {"header_row": bool (default true - false omits the header row
    entirely, the real file has none), "leading_blank_line": bool (default
    false - the real file's first byte is a bare newline before row 1),
    "lineterminator": str (default csv.writer's '\\r\\n'; the real file is
    plain '\\n' throughout, 0 CR bytes), "bom": bool (default false - added
    2026-09-25 for universitat-heidelberg, whose real files start with a
    UTF-8 byte-order mark, bytes EF BB BF; true writes it first via the
    'utf-8-sig' codec)}. Absent means every pre-existing csv template behaves
    exactly as before."""
    keys = keys or headers
    csv_format = csv_format or {}
    lineterminator = csv_format.get("lineterminator", "\r\n")
    encoding = "utf-8-sig" if csv_format.get("bom") else "utf-8"
    with open(path, "w", newline="", encoding=encoding) as f:
        writer = csv.writer(f, delimiter=delimiter, lineterminator=lineterminator)
        if csv_format.get("leading_blank_line"):
            f.write(lineterminator)
        if csv_format.get("header_row", True):
            writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(k, "") for k in keys])


def write_xlsx(rows, headers, path, keys=None, header_format=None, sheet_name=None, header_row=True, extra_sheets=None, leading_sheets=None):
    """`keys` (same length as `headers`, positional) looks up each row's
    value by a different internal key than the header text actually
    written - needed when a template's real source file has a genuinely
    duplicate header name (e.g. sap-gdsn-price-de's two 'PRICE_AMOUNT'
    columns, only the first of which is ever populated in the real files):
    build_rows() keys its dict by `col.get("key", col["header"])`, so the
    duplicate's second occurrence gets its own key and doesn't clobber the
    first's value. Defaults to `headers` itself, so every template without
    an explicit "key" column field is unaffected.

    Header row gets wrap-text alignment and a row height sized to its
    tallest cell's line count - found 2026-09-24 comparing a generated file
    side-by-side against its real source (ghx-gdsn-nl-hospital, whose real
    headers carry embedded '\\n' newlines, e.g. bilingual field names/
    instructions): every prior byte-for-byte verification this project has
    done only ever diffed cell *values*, never formatting, so every xlsx
    template had this same gap - real header cells are wrapped/tall, the
    generated ones were single-line and squished even though the text
    itself was correct.

    `header_format` (from the template's own optional "header_format" key,
    see generate()) opts a specific template into full real-file header
    fidelity beyond the generic wrap+height above - real per-column
    background fill colors and additional header rows (e.g.
    ghx-gdsn-nl-hospital's real files have a second header row of
    Dutch/English fill-in instructions before data starts, which every
    prior version of this generator silently dropped instead of
    replicating), plus real per-column widths. Shape: {"row1_height": float,
    "fills": [hex-ARGB-or-None, ...], "column_widths": [float-or-None, ...]
    (both same length/order as headers), "extra_rows": [{"height": float,
    "values": [str, ...]}]}. All keys optional; absent means "don't touch
    that aspect" so a template can opt into just fills, or just widths, or
    just extra rows. None of this is derivable from the template's column
    list itself (the real colors/widths/instructions aren't a function of
    any master data), so it's supplied as literal template data, extracted
    verbatim from the real file rather than guessed.

    `sheet_name` (from the template's own optional top-level "sheet_name"
    key) sets the workbook's sheet tab title to match the real file's (e.g.
    ghx-gdsn-nl-hospital's real files are all titled "Template NL", not
    openpyxl's default "Sheet") - a separate template key from
    header_format since it's a workbook property, not a header-row one.

    `header_row=False` (the template's csv_format.header_row, also honored
    for .xlsx since 2026-10-01 - uams-workday's real workbook has no header
    row) writes data rows only; header_format is then ignored.

    `extra_sheets` (template key, 2026-10-01): sheets added after the data
    sheet - a name for an empty sheet (bipp-articles' Blad2/Blad3) or
    {"name", "rows"} for static text copied from the real file
    (erasmus-mc-contract-line's column guide and contact sheet)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    keys = keys or headers
    header_format = header_format or {}
    wb = Workbook()
    ws = wb.active
    if sheet_name:
        ws.title = sheet_name
    for extra in leading_sheets or []:
        # Same format as extra_sheets, but placed BEFORE the data sheet
        # (sanquin's 'Template uitleg', wageningen's Gegevens/Stuurrecord).
        sheet = wb.create_sheet(extra["name"] if isinstance(extra, dict) else extra, wb.index(ws))
        for values in (extra.get("rows", []) if isinstance(extra, dict) else []):
            sheet.append(values)
    if leading_sheets:
        wb.active = wb.index(ws)  # row-count checks read the active sheet
    for extra in extra_sheets or []:
        # A name (empty sheet), or {"name": ..., "rows": [[...], ...]} for a
        # static reference sheet copied verbatim from the real file.
        if isinstance(extra, dict):
            sheet = wb.create_sheet(extra["name"])
            for values in extra.get("rows", []):
                sheet.append(values)
        else:
            wb.create_sheet(extra)
    if not header_row:
        for row in rows:
            ws.append([row.get(k, "") for k in keys])
        wb.save(path)
        return
    ws.append(headers)
    wrap_alignment = Alignment(wrap_text=True, vertical="top")
    fills = header_format.get("fills")
    widths = header_format.get("column_widths")
    max_lines = 1
    for i, cell in enumerate(ws[1]):
        if cell.value:
            max_lines = max(max_lines, str(cell.value).count("\n") + 1)
        cell.alignment = wrap_alignment
        if fills and i < len(fills) and fills[i]:
            cell.fill = PatternFill(fill_type="solid", fgColor=fills[i])
        if widths and i < len(widths) and widths[i]:
            ws.column_dimensions[get_column_letter(i + 1)].width = widths[i]
    ws.row_dimensions[1].height = header_format.get("row1_height") or max(15, max_lines * 14)

    for extra_row in header_format.get("extra_rows", []):
        ws.append(extra_row["values"])
        row_idx = ws.max_row
        for cell in ws[row_idx]:
            cell.alignment = wrap_alignment
        if extra_row.get("height"):
            ws.row_dimensions[row_idx].height = extra_row["height"]

    for row in rows:
        ws.append([row.get(k, "") for k in keys])
    wb.save(path)


def write_cif(rows, headers, cif_metadata, path):
    """Writes the CIF_I_V3.0 layout used by DSM.xlsx / Intervet_Merck_MSD_Ariba_Catalog.xlsx /
    Universiteit_Maastricht_Catalog.xlsx: a metadata header block (one [key, value] pair per
    row), then a FIELDNAMES row whose first cell is 'FIELDNAMES: <first header>' with the
    remaining headers in the following cells, then a bare 'DATA' marker row, then the data
    rows in header order. Two metadata values are computed at write time rather than stored
    literally in the template: __ITEM_COUNT__ (row count) and __TIMESTAMP__ (current UTC time,
    formatted per that value's own strftime pattern, e.g. '__TIMESTAMP__:%d-%m-%Y')."""
    from openpyxl import Workbook

    now = datetime.now(timezone.utc)
    wb = Workbook()
    ws = wb.active
    for key, value in cif_metadata:
        if value == "__ITEM_COUNT__":
            value = str(len(rows))
        elif value.startswith("__TIMESTAMP__"):
            fmt = value.split(":", 1)[1] if ":" in value else "%d-%m-%Y"
            value = now.strftime(fmt)
        ws.append([key, value] if value != "" else [key])
    ws.append([f"FIELDNAMES: {headers[0]}"] + list(headers[1:]))
    ws.append(["DATA"])
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    wb.save(path)


def write_cif_txt(rows, headers, cif_metadata, path):
    """Writes a real plaintext CIF_I_V3.0 file - the actual container used by
    the Ariba tiered-discount and proxy feeds (US - Ariba 05%/11%/0% Discount
    ...txt, US - Ariba Proxy.txt), as opposed to write_cif()'s xlsx-wrapped
    approximation used for DSM/Intervet/Universiteit Maastricht. Same
    metadata-block + FIELDNAMES + DATA structure, but written as literal text
    with CSV-quoted data rows (quoting only fields that need it, matching the
    real files) rather than spreadsheet cells. Every real file of this
    container type ends with a literal 'ENDOFDATA' trailer line after the
    last data row - found 2026-09-24 auditing ariba-tiered-txt (confirmed
    across all 6 real Ariba tiered-discount files plus US - Ariba Proxy.txt);
    a generic container-format gap, not specific to one template, so fixed
    here rather than in any one template's columns. Every real file also
    uses plain '\n' line endings throughout (0 '\r' bytes anywhere) -
    csv.writer's default '\r\n' lineterminator (active because the file is
    opened with newline="" so the csv module can control quoting) put a
    stray '\r' before every data row's '\n'; found the same day via a raw
    byte-level check, since the row-by-row field diff used to verify every
    other fix re-splits both sides on line breaks first and can't see a
    line-ending difference."""
    now = datetime.now(timezone.utc)
    with open(path, "w", encoding="utf-8", newline="") as f:
        for key, value in cif_metadata:
            if value == "__ITEM_COUNT__":
                value = str(len(rows))
            elif value.startswith("__TIMESTAMP__"):
                fmt = value.split(":", 1)[1] if ":" in value else "%d-%m-%Y"
                value = now.strftime(fmt)
            f.write(f"{key}: {value}\n" if value != "" else f"{key}\n")
        f.write(f"FIELDNAMES: {','.join(headers)}\n")
        f.write("DATA\n")
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        for row in rows:
            writer.writerow([row.get(h, "") for h in headers])
        f.write("ENDOFDATA\n")


def audit_timestamp():
    """Local wall-clock time with an explicit UTC offset, e.g.
    '2026-09-25T20:26:56.517461-04:00' - unambiguous, and matches the
    operator's clock. (Entries before 2026-09-26 are UTC, '+00:00'; compare
    timestamps as datetimes, never as strings.)"""
    return datetime.now().astimezone().isoformat()


AUDIT_MESSAGE_LIMIT = 20
_ROW_PREFIX = re.compile(r"^(?:master )?row \d+(?: \([^)]*\))?: ")
_TRAILING_VALUE = re.compile(r": (?:'[^']*'|\"[^\"]*\"|\{.*\})$")


def cap_messages_for_audit(detail):
    """Row-level checks emit one message per failing row (a template-wide
    problem can mean 700k+ identical messages - 2026-09-18's audit file hit
    68 MB). Keep the first AUDIT_MESSAGE_LIMIT errors/warnings in the audit
    entry, plus the total and a count per pattern (row prefix stripped, the
    trailing per-row value blanked). The full list is still returned to callers."""
    detail = dict(detail)
    for key in ("errors", "warnings"):
        msgs = detail.get(key) or []
        if len(msgs) <= AUDIT_MESSAGE_LIMIT:
            continue
        patterns = {}
        for m in msgs:
            pat = _ROW_PREFIX.sub("", m)
            if pat != m:
                pat = _TRAILING_VALUE.sub(": '...'", pat)
            patterns[pat] = patterns.get(pat, 0) + 1
        detail[key] = msgs[:AUDIT_MESSAGE_LIMIT]
        detail[f"{key}_total"] = len(msgs)
        detail[f"{key}_by_pattern"] = patterns
    return detail


BUSINESS_ACTIONS = ("update_customer_config", "create_customer")


def audit_source(entry_or_action):
    """'business' (a change or generate done for a business user) or
    'engineering' (refreshes, scoreboard, imports, cleanup). Entries written
    since 2026-10-02 carry `source`; older ones are classified by action:
    config changes are business, everything else engineering (in this sandbox
    every pre-2026-10-02 generate was engineering work)."""
    if isinstance(entry_or_action, dict):
        if entry_or_action.get("source"):
            return entry_or_action["source"]
        entry_or_action = entry_or_action.get("action")
    return "business" if entry_or_action in BUSINESS_ACTIONS else "engineering"


def append_audit(slug, requested_by, request_text, outcome, detail, output_path=None, action="generate_catalog",
                 source=None):
    """`source`: 'business' / 'engineering'; default: env CATALOG_AUDIT_SOURCE,
    else by action (audit_source)."""
    AUDIT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    entry = {
        "timestamp": audit_timestamp(),
        "who": requested_by,
        "action": action,
        "customer": slug,
        "request": request_text,
        "outcome": outcome,
        "source": source or os.environ.get("CATALOG_AUDIT_SOURCE") or audit_source(action),
        "detail": cap_messages_for_audit(detail) if isinstance(detail, dict) else detail,
    }
    if output_path:
        entry["files_written"] = [str(output_path.relative_to(ROOT))]
    with open(AUDIT_DIR / f"{today}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


EXCLUSIONS_DIR = ROOT / "config" / "exclusions"


def customer_exclusions(slug):
    """{sku: row} from config/exclusions/<slug>.csv - this customer's OWN
    excluded SKUs (columns sku, reason, requested_by, requested_on), applied on
    top of any shared overrides.excluded_skus_lookup. Added 2026-10-02 after a
    workflow test: the shared coupa_marketplace_excluded_uk.csv list is used by
    ~30 UK customers, so adding a SKU there for one customer silently drops it
    for all of them. Kept under config/ (business data, not master data, so it
    doesn't trigger the snapshot-review warning). Written by
    scripts/customer_config.py exclude/include."""
    path = EXCLUSIONS_DIR / f"{slug}.csv" if slug else None
    if not path or not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["sku"].strip(): r for r in csv.DictReader(f) if r.get("sku", "").strip()}


def prepare_rows(customer, template, slug=None):
    """(master_rows, catalog rows) for a customer. Shared by generate() and
    scripts/fidelity_scoreboard.py. `slug` enables the customer's own
    config/exclusions/<slug>.csv."""
    master_rows = load_master(template.get("master_source", "products.csv"))
    # overrides.excluded_skus_lookup names a lookup CSV (any column layout -
    # only membership in the file matters) whose SKUs are dropped from this
    # customer's row population entirely, before build_rows() ever sees them.
    # Added for coupa-marketplace-de/-uk: their real files are a genuine
    # subset of the US file's SKU population (~1,457-1,458 fewer rows, mostly
    # V2/V3/V4 multi-unit kit sizes plus 14 high-value cold-chain SKUs with no
    # discoverable common rule) - not a config error, a real per-region
    # catalog-population difference. See docs/baseline-reconciliation.md.
    excluded_file = customer.get("overrides", {}).get("excluded_skus_lookup")
    excluded = set()
    for name in ([excluded_file] if isinstance(excluded_file, str) else excluded_file or []):
        excluded |= set(load_lookup(name))
    excluded |= set(customer_exclusions(slug))
    if excluded:
        master_rows = [row for row in master_rows if row["sku"] not in excluded]
    return master_rows, build_rows(master_rows, template, customer)


def output_extension(customer):
    # CIF_I_V3.0 catalogs are packaged as .xlsx in practice (see DSM.xlsx /
    # Intervet_Merck_MSD_Ariba_Catalog.xlsx / Universiteit_Maastricht_Catalog.xlsx),
    # not as a .cif/.txt file, even though file_type is "cif" for dispatch purposes.
    # "ciftxt" is the real plaintext container (Ariba tiered/proxy feeds).
    ext = customer.get("file_type", "xlsx")
    return "xlsx" if ext == "cif" else "txt" if ext == "ciftxt" else ext


def write_output(rows, template, customer, out_path):
    """Write rows in the customer's file_type. Shared by generate() and
    scripts/fidelity_scoreboard.py (which writes to a temp dir, no catalog)."""
    ext = customer.get("file_type", "xlsx")
    headers = [c["header"] for c in template["columns"]]
    keys = [c.get("key", c["header"]) for c in template["columns"]]
    if ext == "csv":
        write_csv(rows, headers, out_path, delimiter=template.get("delimiter", ","), keys=keys, csv_format=template.get("csv_format"))
    elif ext == "xlsx":
        write_xlsx(rows, headers, out_path, keys=keys, header_format=template.get("header_format"), sheet_name=template.get("sheet_name"),
                   header_row=(template.get("csv_format") or {}).get("header_row", True),
                   extra_sheets=template.get("extra_sheets"), leading_sheets=template.get("leading_sheets"))
    elif ext == "cif":
        write_cif(rows, headers, template.get("cif_metadata", []), out_path)
    elif ext == "ciftxt":
        write_cif_txt(rows, headers, template.get("cif_metadata", []), out_path)
    else:
        raise SystemExit(f"Unsupported file_type '{ext}'")


CATALOG_HISTORY_KEEP = int(os.environ.get("CATALOG_HISTORY_KEEP", "12"))


def prune_older_catalogs(out_dir, keep_path, keep=None):
    """Keeps the customer's newest `keep` catalog files (default
    CATALOG_HISTORY_KEEP = 12, env override) and deletes older ones once a new
    one is written; the deleted names go into that generate's audit detail.
    Files are named {slug}_{YYYY-MM-DD}.ext, so this is one version per
    generate day (a same-day regenerate overwrites that day's file). History
    is what scripts/compare_versions.py diffs ("what changed since last
    month?"). Runs after check_row_count_regression(), which reads the
    previous file. 2026-10-02: first kept only the newest file; changed the
    same day to a rolling history after the workflow tests showed a
    month-over-month question couldn't be answered without it. Up to about
    12 GB at 1 GB per full set."""
    keep = CATALOG_HISTORY_KEEP if keep is None else keep
    files = sorted(p for p in out_dir.iterdir()
                   if p.is_file() and not p.name.startswith((".", "~$")))
    newer_first = [keep_path] + [p for p in reversed(files) if p != keep_path]
    pruned = []
    for p in newer_first[max(keep, 1):]:
        p.unlink()
        pruned.append(p.name)
    return sorted(pruned)


def generate(slug, requested_by, request_text, dry_run, source=None):
    """Runs quality checks and (unless dry_run) writes the catalog + audit entry.

    Returns a result dict rather than printing, so both the CLI and the web
    API can drive the same logic: {ok, dry_run, rows, errors, warnings,
    file_path, template_name, preview_columns, preview_rows}. file_path is
    ROOT-relative, or None if nothing was written. warnings are non-blocking
    (e.g. incomplete override coverage). preview_columns/preview_rows are only
    present when errors is empty, capped at PREVIEW_ROW_LIMIT rows.
    """
    customer = load_customer(slug)
    template = load_template(customer["template_ref"])

    config_errors = check_config_sanity(customer, template)
    if config_errors:
        append_audit(slug, requested_by, request_text, "failed", {"errors": config_errors, "warnings": []}, source=source)
        return {
            "ok": False,
            "dry_run": dry_run,
            "rows": 0,
            "errors": config_errors,
            "warnings": [],
            "file_path": None,
            "template_name": template["template_name"],
        }

    master_rows, rows = prepare_rows(customer, template, slug)
    headers = [c["header"] for c in template["columns"]]

    errors = (
        check_master_data(master_rows)
        + run_quality_checks(rows, template, customer)
        + check_price_relationships(rows, template, customer)
        + check_required_fields(rows, template)
    )
    warnings = (
        check_override_coverage(rows, customer)
        + check_row_count_regression(slug, len(rows), template)
        + check_category_discount_distribution(rows, template, customer)
        + check_cross_period_consistency(slug, customer)
        + check_snapshot_review(customer)
    )

    if errors:
        append_audit(slug, requested_by, request_text, "failed", {"errors": errors, "warnings": warnings},
                     source=source)
        return {
            "ok": False,
            "dry_run": dry_run,
            "rows": len(rows),
            "errors": errors,
            "warnings": warnings,
            "file_path": None,
            "template_name": template["template_name"],
        }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "rows": len(rows),
            "errors": [],
            "warnings": warnings,
            "file_path": None,
            "template_name": template["template_name"],
            "preview_columns": headers,
            "preview_rows": rows[:PREVIEW_ROW_LIMIT],
        }

    out_dir = CATALOGS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    date_stamp = datetime.now(timezone.utc).date().isoformat()
    out_path = out_dir / f"{slug}_{date_stamp}.{output_extension(customer)}"
    write_output(rows, template, customer, out_path)
    pruned = prune_older_catalogs(out_dir, out_path)

    detail = {"rows_written": len(rows), "warnings": warnings}
    if pruned:
        detail["pruned"] = pruned
    append_audit(slug, requested_by, request_text, "success", detail, out_path, source=source)
    return {
        "ok": True,
        "dry_run": False,
        "rows": len(rows),
        "errors": [],
        "warnings": warnings,
        "file_path": str(out_path.relative_to(ROOT)),
        "template_name": template["template_name"],
        "preview_columns": headers,
        "preview_rows": rows[:PREVIEW_ROW_LIMIT],
    }


def main():
    parser = argparse.ArgumentParser(description="Generate a B2B customer catalog from local master data + config.")
    parser.add_argument("customer", help="Customer config slug (filename under config/customers/, without .json), or 'all'")
    parser.add_argument("--requested-by", default=os.environ.get("USER", "unknown"))
    parser.add_argument("--request", default="")
    parser.add_argument("--source", choices=["business", "engineering"],
                        help="audit source; default business for one customer, engineering for 'all'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    slugs = sorted(p.stem for p in CUSTOMERS_DIR.glob("*.json")) if args.customer == "all" else [args.customer]
    source = args.source or ("engineering" if args.customer == "all" else "business")

    ok = True
    crashed = []
    for slug in slugs:
        try:
            result = generate(slug, args.requested_by, args.request, args.dry_run, source=source)
        except (Exception, SystemExit) as e:
            # An expression error raises SystemExit; with 'all' that used to
            # stop the whole run at the first broken customer. Record it like
            # the web UI's regenerate job does and carry on.
            msg = f"{type(e).__name__}: {e}"
            print(f"ERROR: generate failed for '{slug}': {msg}")
            try:
                append_audit(slug, args.requested_by, args.request, "failed",
                             {"errors": [f"exception during generate: {msg}"], "warnings": []}, source=source)
            except Exception as audit_error:
                print(f"  (audit entry not written: {audit_error})")
            crashed.append(slug)
            ok = False
            continue
        if result["errors"]:
            print(f"BLOCKED: {len(result['errors'])} quality check failure(s) for '{slug}':")
            for e in result["errors"]:
                print(f"  - {e}")
        elif result["dry_run"]:
            print(
                f"OK (dry run): {result['rows']} row(s) would be written for '{slug}' "
                f"using template '{result['template_name']}'"
            )
        else:
            print(f"Wrote {result['file_path']} ({result['rows']} rows)")
        for w in result.get("warnings", []):
            print(f"  WARNING: {w}")
        ok = result["ok"] and ok
    if crashed and len(slugs) > 1:
        print(f"{len(crashed)} customer(s) failed with an exception: {', '.join(crashed)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
