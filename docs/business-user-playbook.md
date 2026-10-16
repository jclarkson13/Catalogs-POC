# Business-user playbook: working with catalogs through Claude Code

How to ask for catalog changes in plain language, what to expect back, and the
current limits. Based on 7 test requests run 2026-10-02 (details at the end).

## How to ask

Always include:
- **Your name**: it is written to the audit log as who asked.
- **Which customer**, as you know them (for example "Bayer" or "Coupa UK"). Claude maps the name to the setups behind it.
- **When it takes effect**, as a date. Most customers have two setups: a *Current* price list (2026) and a *Future* price list (2027, valid 1 Jan–31 Dec 2027). "From January 1st" means the Future one.
- **Why**: the reason goes into the audit log next to the change.

Claude will ask before acting when a request is ambiguous. Expect questions on "next year", on what "German" covers (some DE setups are Austrian customers), and on whether "update" means rebuild, new prices or new discounts.

## What works today

| Request | Example | What you get |
|---|---|---|
| Change a discount | "Bayer gets 12% from 1 Jan" | Config updated, catalog regenerated, every price checked, audit entries under your name |
| Remove products for one customer | "Coupa UK should no longer get 64668S, 74332T" | Products removed for that customer (Current and Future), with a check that only those rows went |
| Set up a new customer like an existing one | "Northwind, same format as Bio Products, 18%" | New setup and first catalog |
| Who gets a product, at what price | "Which customers get 64668S and at what price?" | A spreadsheet covering every customer, for 2026 and 2027 |
| Check a complaint | "Albany says prices are wrong" | Current discount, its change history, and a comparison with the production file |
| Explain a change | "Why is Zageno's file different from last month?" | `compare_versions.py`: products added/removed, column and price changes, and the logged reasons in between (last 12 generate days are kept) |

Nothing is sent to customers. You get a file path and decide what to send.

## Current limits (as of 2026-10-02)

- **Changing only the 2027 (Future) price list now warns** that it differs from the 2026 one (fixed 2026-10-02). For an intended new deal, that warning is expected.

- **Config changes are audited with old and new values** (fixed 2026-10-02): `scripts/customer_config.py` changes discounts and dates, creates customers like an existing one, and excludes or re-includes SKUs. Changes made any other way only show in git history.
- **Catalog history covers the last 12 generate days per customer** (fixed 2026-10-02). There is still no record of which file was *sent*, so confirm the dates the customer received.
- **Product exclusions are per customer** (fixed 2026-10-02): `customer_config.py exclude` writes the customer's own list. The older shared lists (for example the one used by about 30 UK customers) are never edited for one customer.
- **Intended changes show up as fidelity "differences".** A new discount or a removed product makes the customer differ from the production reference file. That is expected, not an error.
- **Prices come from the latest CST export (2026-09-02).** New list prices need a new export, which affects every customer.
- **Customer identity is not verified.** The name you give is recorded as is.

## Test results (2026-10-02)

| # | Request | Outcome | Main friction |
|---|---|---|---|
| 1 | New customer like Bio Products, 18%, 2027 dates | Done, about 1 min | Config hand-written; creation not audited; 2027 dates with today's list prices (asked) |
| 2 | Bayer 12% from 1 Jan | Done correctly (Future only), every price checked | No audited config tool; cross-period check silent when only Future changes |
| 3 | Remove 2 SKUs for Coupa UK | Done, but needed a generator change | Shared exclusion list (about 30 customers) and only one list per customer; snapshot warning noise |
| 4 | Who gets 64668S, at what price | Done, 134 customers, about 15 min | Needed a custom script; "customer price" column differs per template |
| 5 | Why Zageno's catalog changed | Correct diff, about 10 min | Old file only recoverable from old git history; no delivery record; no version-diff tool |
| 6 | Albany prices wrong, revert | Correct "nothing changed, nothing to revert" | Change history split across audit and git; no field-history or revert tool |
| 7 | "Update the German price lists for next year" | Correctly asked 5 questions, changed nothing | No mapping from business terms to setups |
