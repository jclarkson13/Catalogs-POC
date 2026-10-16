"""Audited customer-config changes from the command line (business-user requests
through Claude Code). Every change writes an audit entry with old and new
values, the same `update_customer_config` action the web UI writes (plus
`create_customer`), so "what was it before, who changed it, when?" is answered
from the audit log alone. Nothing is generated: each command prints the
dry-run/generate command to run next.

    python3 scripts/customer_config.py show <slug>
    python3 scripts/customer_config.py history <slug>
    python3 scripts/customer_config.py set <slug> [--discount N] [--valid-from YYYY-MM-DD] [--valid-until YYYY-MM-DD]
    python3 scripts/customer_config.py create <new-slug> --like <existing-slug> --display-name "..." \
        [--discount N] [--valid-from ...] [--valid-until ...] [--customer-id ID] [--sales-region US|DE|UK|EU|NL]
    python3 scripts/customer_config.py exclude <slug> SKU [SKU ...] --reason "..."
    python3 scripts/customer_config.py include <slug> SKU [SKU ...]

Common options: --requested-by NAME (default $USER; record the business user
who asked), --request "their words / reason", --with-future (also apply set /
exclude / include to the -future sibling), --dry-run (show, change nothing),
--source business|engineering (audit source, default business).

Exclusions go to the customer's OWN list, config/exclusions/<slug>.csv, never
to a shared overrides.excluded_skus_lookup file (those are shared by many
customers; `show` lists who shares them).
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.generate_catalog import (  # noqa: E402
    AUDIT_DIR, CUSTOMERS_DIR, EXCLUSIONS_DIR, TEMPLATES_DIR, append_audit, audit_source, customer_exclusions,
    load_customer, load_master,
)

FILE_MAP = ROOT / "config" / "fidelity_file_map.json"
FIELDS = {"discount": "discount_percentage", "valid_from": "price_valid_from", "valid_until": "price_valid_until"}
class ConfigError(ValueError):
    """A refused change (bad value, unknown SKU) - message is user-facing."""


# Identity fields that belong to the customer being copied, not the new one.
CUSTOMER_SPECIFIC = ("customer_id", "step_classification_id", "display_name")


def cfg_path(slug):
    return CUSTOMERS_DIR / f"{slug}.json"


def sibling(slug):
    s = slug[: -len("-future")] if slug.endswith("-future") else f"{slug}-future"
    return s if cfg_path(s).exists() else None


def _set_top_level(text, key, value):
    """Replace one top-level key's value in JSON text in place (any value
    type), keeping the rest of the file byte-for-byte; or None if the key
    isn't found at top level."""
    dec = json.JSONDecoder()
    for m in re.finditer(r'\n([ \t]*)"' + re.escape(key) + r'"\s*:\s*', text):
        start = m.end()
        try:
            _, end = dec.raw_decode(text, start)
        except ValueError:
            continue
        indent = m.group(1)
        dumped = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
        candidate = text[:start] + dumped + text[end:]
        try:
            if json.loads(candidate).get(key) == value:
                return candidate
        except ValueError:
            pass
    return None


def write_config(slug, data, old_text=None):
    """Write a config, keeping the file's own formatting: each changed
    top-level field is replaced in place (verified by re-parsing); new keys
    or anything unverifiable fall back to a full indent=2 rewrite."""
    path = cfg_path(slug)
    if old_text is not None:
        text, old = old_text, json.loads(old_text)
        ok = set(old) == set(data)
        for k, v in data.items():
            if not ok:
                break
            if old.get(k) != v:
                text = _set_top_level(text, k, v)
                ok = text is not None
        if ok and json.loads(text) == data:
            path.write_text(text, encoding="utf-8")
            return
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def validate(cfg):
    errors = []
    d = cfg.get("discount_percentage")
    if not isinstance(d, (int, float)) or not 0 <= d <= 100:
        errors.append(f"discount_percentage must be a number between 0 and 100 (got {d!r})")
    for name, pct in (cfg.get("category_discounts") or {}).items():
        if not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            errors.append(f"category_discounts.{name} must be between 0 and 100 (got {pct!r})")
    for name, inst in (cfg.get("institutions") or {}).items():
        pct = (inst or {}).get("discount_percentage")
        if pct is not None and (not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
            errors.append(f"institutions.{name}.discount_percentage must be between 0 and 100 (got {pct!r})")
    try:
        f, u = date.fromisoformat(cfg["price_valid_from"]), date.fromisoformat(cfg["price_valid_until"])
        if f >= u:
            errors.append(f"price_valid_from {f} must be before price_valid_until {u}")
    except (KeyError, ValueError) as e:
        errors.append(f"price_valid_from/until must be YYYY-MM-DD dates ({e})")
    return errors


def next_steps(slugs):
    print("\nNext: preview, then generate (records the generate in the audit log too):")
    for s in slugs:
        print(f"  python3 scripts/generate_catalog.py {s} --dry-run")
        print(f"  python3 scripts/generate_catalog.py {s} --requested-by \"<name>\" --request \"<reason>\"")


def shared_users(lookup_name):
    out = []
    for p in sorted(CUSTOMERS_DIR.glob("*.json")):
        v = json.loads(p.read_text()).get("overrides", {}).get("excluded_skus_lookup")
        if v == lookup_name or (isinstance(v, list) and lookup_name in v):
            out.append(p.stem)
    return out


def cmd_show(a):
    c = load_customer(a.slug)
    print(f"{c.get('display_name')} ({a.slug})")
    for k in ("template_ref", "file_type", "price_period", "discount_percentage", "category_discounts",
              "price_valid_from", "price_valid_until", "sales_region", "customer_id", "step_classification_id"):
        if k in c:
            print(f"  {k}: {c[k]}")
    if c.get("institutions"):
        print(f"  institutions: {len(c['institutions'])} with their own discounts")
    sib = sibling(a.slug)
    if sib:
        s = load_customer(sib)
        print(f"  sibling: {sib} ({s.get('price_period') or 'current'}, {s.get('discount_percentage')}%, "
              f"{s.get('price_valid_from')}..{s.get('price_valid_until')})")
    shared = c.get("overrides", {}).get("excluded_skus_lookup")
    for name in ([shared] if isinstance(shared, str) else shared or []):
        users = shared_users(name)
        print(f"  shared exclusion list {name}: used by {len(users)} customers - do not edit for one customer")
    own = customer_exclusions(a.slug)
    print(f"  own exclusions (config/exclusions/{a.slug}.csv): {len(own)}"
          + (f" - {', '.join(sorted(own)[:10])}" if own else ""))


def history(slug):
    """{business: [entries], engineering: {count, latest}, commits: [str]} -
    business changes listed (config changes and business generates),
    engineering runs only counted. Shared by the CLI and the web UI."""
    business, eng = [], []
    for f in sorted(Path(AUDIT_DIR).glob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("customer") != slug:
                    continue
                if audit_source(e) != "business":
                    eng.append(e)
                    continue
                detail = e.get("detail") if e.get("action") != "generate_catalog" else {
                    "rows_written": (e.get("detail") or {}).get("rows_written")}
                business.append({k: e.get(k) for k in ("timestamp", "who", "action", "outcome", "request")}
                                | {"detail": detail})
    log = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%as %h %an: %s", "--", str(cfg_path(slug))],
                         capture_output=True, text=True).stdout.splitlines()
    return {"business": business,
            "engineering": {"count": len(eng), "latest": eng[-1]["timestamp"] if eng else None},
            "commits": log}


def cmd_history(a):
    h = history(a.slug)
    print(f"History for {a.slug} (audit log; business changes listed, engineering runs counted):")
    for e in h["business"]:
        print(f"  {e['timestamp'][:16]} {e.get('who')}: {e.get('action')} ({e.get('outcome')}) "
              f"{json.dumps(e['detail'], ensure_ascii=False)[:180]}  ({(e.get('request') or '')[:80]})")
    if not h["business"]:
        print("  no business changes recorded")
    if h["engineering"]["count"]:
        print(f"  + {h['engineering']['count']} engineering runs (refreshes, scoreboard, cleanup), "
              f"latest {h['engineering']['latest'][:10]}")
    print(f"Commits touching the config file ({len(h['commits'])}; edits made outside these tools only show here):")
    for line in h["commits"][:15]:
        print(f"  {line[:150]}")

def cmd_set(a):
    targets = [a.slug] + ([sibling(a.slug)] if a.with_future and sibling(a.slug) else [])
    changed = []
    for slug in targets:
        text = cfg_path(slug).read_text(encoding="utf-8")
        cfg = json.loads(text)
        new = dict(cfg)
        for opt, field in FIELDS.items():
            v = getattr(a, opt)
            if v is not None:
                new[field] = v
        diff = {k: {"old": cfg.get(k), "new": new[k]} for k in FIELDS.values() if cfg.get(k) != new.get(k)}
        if not diff:
            print(f"{slug}: no change")
            continue
        errors = validate(new)
        if errors:
            sys.exit(f"{slug}: refused - " + "; ".join(errors))
        print(f"{slug}: " + ", ".join(f"{k} {v['old']!r} -> {v['new']!r}" for k, v in diff.items()))
        if a.dry_run:
            continue
        write_config(slug, new, text)
        append_audit(slug, a.requested_by, a.request or "config change via customer_config.py", "success",
                     diff, action="update_customer_config", source=a.source)
        changed.append(slug)
    if a.dry_run:
        print("dry run: nothing changed")
    elif changed:
        next_steps(changed)


def create_customer(slug, *, like, display_name, requested_by, request="", discount=None, valid_from=None,
                    valid_until=None, customer_id=None, sales_region=None, source="business", dry_run=False):
    """Create config/customers/<slug>.json as a copy of `like` (its own IDs and
    notes dropped), map it to no real file, audit `create_customer`. Returns
    (config, notes). Shared by the CLI and the web UI; raises ConfigError."""
    if cfg_path(slug).exists():
        raise ConfigError(f"{slug} already exists")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug or ""):
        raise ConfigError("the new id must be lowercase letters/digits/hyphens, e.g. northwind-biosciences")
    if not (display_name or "").strip():
        raise ConfigError("a display name is required")
    base = load_customer(like)
    new = {k: v for k, v in base.items() if not k.startswith("_note") and k not in CUSTOMER_SPECIFIC}
    new = {"display_name": display_name.strip(), **new}
    for field, v in (("discount_percentage", discount), ("price_valid_from", valid_from),
                     ("price_valid_until", valid_until)):
        if v is not None:
            new[field] = v
    if customer_id:
        new["customer_id"] = customer_id
    if sales_region:
        new["sales_region"] = sales_region
    new["_note"] = (f"Created {date.today()} as a copy of {like} (template {base['template_ref']}), "
                    f"requested by {requested_by}. No real reference file yet.")
    errors = validate(new)
    if errors:
        raise ConfigError("; ".join(errors))
    notes = []
    missing = [k for k in ("customer_id", "step_classification_id") if k in base and k not in new]
    if missing:
        notes.append(f"not copied (belong to {like}): {', '.join(missing)} - add the new customer's own IDs when known")
    if dry_run:
        return new, notes
    write_config(slug, new)
    fm = json.loads(FILE_MAP.read_text()) if FILE_MAP.exists() else {}
    fm.setdefault(slug, None)  # no real file: scored "no_real_file", never fuzzy-matched to another file
    FILE_MAP.write_text(json.dumps(fm, indent=1, ensure_ascii=False) + "\n")
    append_audit(slug, requested_by, request or f"new customer like {like}", "success",
                 {"like": like, "config": {k: new[k] for k in ("display_name", "template_ref", "discount_percentage",
                                                               "price_valid_from", "price_valid_until") if k in new}},
                 action="create_customer", source=source)
    return new, notes


def cmd_create(a):
    try:
        new, notes = create_customer(a.slug, like=a.like, display_name=a.display_name, requested_by=a.requested_by,
                                     request=a.request, discount=a.discount, valid_from=a.valid_from,
                                     valid_until=a.valid_until, customer_id=a.customer_id,
                                     sales_region=a.sales_region, source=a.source, dry_run=a.dry_run)
    except ConfigError as e:
        sys.exit(f"refused - {e}")
    print(f"create {a.slug} like {a.like}: template {new['template_ref']}, {new['discount_percentage']}%, "
          f"{new['price_valid_from']}..{new['price_valid_until']}, region {new.get('sales_region')}")
    for n in notes:
        print(f"  {n}")
    if a.dry_run:
        print("dry run: nothing created")
        return
    next_steps([a.slug])

def _master_skus():
    skus = {r["sku"] for r in load_master("products.csv")}
    if (ROOT / "master" / "products_neb.csv").exists():
        skus |= {r["sku"] for r in load_master("products_neb.csv")}
    return skus


def change_exclusions(slug, skus, *, include, reason="", requested_by, request="", source="business",
                      dry_run=False):
    """Add (include=False) or remove SKUs from config/exclusions/<slug>.csv,
    audited. Returns the SKUs actually changed. Shared by the CLI and the
    web UI. Raises ConfigError for unknown SKUs or a missing reason."""
    load_customer(slug)
    skus = [s.strip() for s in skus if s and s.strip()]
    if not skus:
        raise ConfigError("no SKUs given")
    if not include:
        if not reason.strip():
            raise ConfigError("a reason is required to exclude products")
        unknown = sorted(set(skus) - _master_skus())
        if unknown:
            raise ConfigError(f"not in master data: {', '.join(unknown)}")
    own = customer_exclusions(slug)
    if include:
        changes = [s for s in skus if s in own]
        for s in changes:
            del own[s]
    else:
        changes = [s for s in dict.fromkeys(skus) if s not in own]
        for s in changes:
            own[s] = {"sku": s, "reason": reason, "requested_by": requested_by,
                      "requested_on": date.today().isoformat()}
    if dry_run or not changes:
        return changes
    EXCLUSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXCLUSIONS_DIR / f"{slug}.csv"
    if own:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["sku", "reason", "requested_by", "requested_on"])
            w.writeheader()
            for s in sorted(own):
                w.writerow({k: own[s].get(k, "") for k in w.fieldnames})
    elif path.exists():
        path.unlink()
    verb = "include again" if include else "exclude"
    detail = {"excluded_skus": {("removed" if include else "added"): changes}}
    if not include:
        detail["reason"] = reason
    append_audit(slug, requested_by, request or f"{verb} {', '.join(changes)}", "success", detail,
                 action="update_customer_config", source=source)
    return changes


def shared_exclusion_lists(slug):
    """[(lookup file, customers using it)] for the customer's shared lists."""
    shared = load_customer(slug).get("overrides", {}).get("excluded_skus_lookup")
    return [(n, shared_users(n)) for n in ([shared] if isinstance(shared, str) else shared or [])]


def cmd_exclude(a, include=False):
    targets = [a.slug] + ([sibling(a.slug)] if a.with_future and sibling(a.slug) else [])
    for slug in targets:
        try:
            changes = change_exclusions(slug, a.skus, include=include, reason=getattr(a, "reason", ""),
                                        requested_by=a.requested_by, request=a.request, source=a.source,
                                        dry_run=a.dry_run)
        except ConfigError as e:
            sys.exit(f"{slug}: refused - {e}")
        verb = "include again" if include else "exclude"
        print(f"{slug}: {verb} {', '.join(changes) or 'nothing (already ' + ('included' if include else 'excluded') + ')'}")
    if a.dry_run:
        print("dry run: nothing changed")
    else:
        if not a.with_future and sibling(a.slug):
            print(f"note: {sibling(a.slug)} (the other price period) was not changed; add --with-future to include it")
        next_steps(targets)


def main():
    ap = argparse.ArgumentParser(description="Audited customer-config changes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--requested-by", default=os.environ.get("USER", "unknown"))
    common.add_argument("--request", default="")
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--with-future", action="store_true")
    common.add_argument("--source", choices=["business", "engineering"], default="business",
                        help="audit source (default business; engineering for config fixes not requested by the business)")
    for name in ("show", "history"):
        p = sub.add_parser(name)
        p.add_argument("slug")
    p = sub.add_parser("set", parents=[common])
    p.add_argument("slug")
    p.add_argument("--discount", type=float)
    p.add_argument("--valid-from", dest="valid_from")
    p.add_argument("--valid-until", dest="valid_until")
    p = sub.add_parser("create", parents=[common])
    p.add_argument("slug")
    p.add_argument("--like", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--discount", type=float)
    p.add_argument("--valid-from", dest="valid_from")
    p.add_argument("--valid-until", dest="valid_until")
    p.add_argument("--customer-id")
    p.add_argument("--sales-region", choices=["US", "DE", "UK", "EU", "NL"])
    for name in ("exclude", "include"):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("slug")
        p.add_argument("skus", nargs="+")
        if name == "exclude":
            p.add_argument("--reason", required=True)
    a = ap.parse_args()
    if getattr(a, "discount", None) is not None and a.discount == int(a.discount):
        a.discount = int(a.discount)
    {"show": cmd_show, "history": cmd_history, "set": cmd_set, "create": cmd_create,
     "exclude": cmd_exclude, "include": lambda x: cmd_exclude(x, include=True)}[a.cmd](a)


if __name__ == "__main__":
    main()
