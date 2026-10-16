# Catalog Workshop

A proof of concept: using Claude Code as a B2B catalog management system. Business users ask in plain language, and Claude changes customer setups, generates catalogs and explains them. It's a **local sandbox** that mirrors production's "Generic Catalog Export" (STEP → SQS → Lambda → S3; see the pim-docs MCP). It never writes to AWS or STEP.

**Status (2026-10-02):** 236 customer setups on 111 templates. All 236 match their real production files (`reports/fidelity/latest.json`). CIFF output is deferred. Detailed reference: [`docs/reference.md`](docs/reference.md). Decision history: [`docs/audit-decisions.md`](docs/audit-decisions.md). Business-user guide: [`docs/business-user-playbook.md`](docs/business-user-playbook.md). Baseline reconciliation: [`docs/baseline-reconciliation.md`](docs/baseline-reconciliation.md).

## Requirements (from the project brief)
1. Audit everything in `audit/{date}.jsonl`: who ran what, when, and the outcome, including failures.
2. Generated catalogs go in `catalogs/{customer-slug}/`, one folder per customer.
3. Catalog formats are remembered as templates (`config/templates/`), and customer setups, including their persisted discounts, as configs (`config/customers/`).
4. File types: csv, xlsx, CIF txt. CIFF is deferred.
5. Quality and consistency checks always run. They're developed with the user (see `docs/reference.md`, "Quality checks").

## Layout
- `master/products.csv` (CST, from the 2026-09-02 export) and `products_neb.csv` (NEB) hold product data. Other `master/*.csv` files are `lookup()` tables, mostly from `incoming/cst_data_2.csv` (112 raw columns). `master/real_cell_snapshot.csv` holds cells copied from real files (see below).
- `config/templates/*.json`: `columns` (header + Python expression over `row`/`discount`/`customer`), `sku_column`, `price_columns`, optional keys.
- `config/customers/*.json`: `template_ref`, `discount_percentage`, `price_valid_from`/`until`, `file_type`, `overrides`. A 2027 price list is a `-future` sibling config (`price_period: future`).
- `config/exclusions/<slug>.csv`: a customer's own excluded SKUs. Shared exclusion lists in `master/` are used by many customers, so never edit them for one customer.
- `incoming/` (gitignored) holds the real production files (`incoming/Catalogs/{Current Price,Future Price,NEB}/`) and raw exports.
- `catalogs/` (gitignored) keeps the 12 newest versions per customer. `reports/fidelity/latest.*` is tracked.

## Everyday commands
| Task | Command |
|---|---|
| Show / change a customer setup (audited, old → new) | `python3 scripts/customer_config.py show / history / set / create / exclude / include …` |
| Preview, then generate a catalog | `python3 scripts/generate_catalog.py <slug> --dry-run`, then without `--dry-run`, with `--requested-by "<person>" --request "<why>"` |
| What changed between two versions | `python3 scripts/compare_versions.py <slug> [--from DATE]` |
| Compare with the real file | `python3 scripts/fidelity_scoreboard.py <slug>` |
| Full refresh (about 1 h) | `python3 scripts/generate_catalog.py all`, then `python3 scripts/fidelity_scoreboard.py all --from-catalogs` |
| New CST export | `import_master_data.py` → rebuild lookups → `retire_snapshot_rows.py` → full refresh |
| Web UI | `PATH="$HOME/Library/Python/3.14/bin:$PATH" uvicorn webapp.backend.main:app --port 8000`. **Restart it after any edit to `scripts/generate_catalog.py` or the backend**: it refuses to generate on stale code. |

Always pass the business user's name as `--requested-by` and their reason as `--request`. Engineering work (refreshes, fixes nobody requested) uses `--source engineering` (the default for `all` runs and the scoreboard). Nothing is sent to customers: report the file path.

## Standing rules
- **Replicate real files exactly, quirks included.** That covers stale labels, `00AE` entities, header/content mismatches and odd formatting. Check `incoming/` before "fixing" any difference. Never fabricate values. Leave a cell blank only when the real file is blank there.
- **Real-file snapshots are a fallback.** When no source reproduces a real cell, copy it into `master/real_cell_snapshot.csv` (`scripts/build_real_cell_snapshot.py`). The generator applies those cells to every column automatically. They're frozen: retire them after a new export.
- **Audit with the full population, never samples.** Compare value and cell type (xlsx text vs number), header order, the row (SKU) set per customer, and the bytes of CSV/TXT files. The checklist is in `docs/reference.md`.
- **Prices use Decimal math** via the `price_for_rounded*` / `price_text` builtins. Rounding mode is per template.
- **Current and Future are separate configs.** "From January 1" usually means the `-future` sibling only. Check both.
- **Ask before acting on ambiguous requests**, e.g. "next year", "German" (some DE setups are Austrian), or "update" (rebuild, new prices or new discounts?).
- **Machine limits (8 GB RAM):** run one heavy job at a time. Never run parallel full refreshes. Worktree agents run single customers or `--workers 1`.

## Parallel agents (git worktrees)
Reset the worktree to `main` if it's behind (agents start stale). Symlink `incoming/` from the main checkout. Set `CATALOG_AUDIT_DIR` to the main checkout's `audit/` unless the work is a throwaway test. Split work by template group.

## Known limits
- No authentication: `requested_by` is whatever name is given.
- No record of which file was sent to a customer.
- Intended business changes (a new discount, a removed SKU) show up as fidelity differences from the reference file.
- Any edit to `generate_catalog.py` makes every catalog stale for the scoreboard.
