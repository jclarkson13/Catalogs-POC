# Baseline Catalog Reconciliation

Ground-truth reconciliation of every `config/customers/*.json` catalog against the **STEP Catalog Configuration Baseline Inventory** (Google Sheet: `https://docs.google.com/spreadsheets/d/1PkjRwHSFORF0NNOJc7_iYnSX4rg40QBu0g7NwcLv1UU/edit?gid=1801121141#gid=1801121141`), sourced from 3 validation emails dated 2026-09-18: NEB/Benelux (21 entries), Current Price (113), Future Price (100) — 234 total. Matching is by **exact `display_name` string match** against the baseline's `Configuration` column, not fuzzy text similarity — an earlier pass tried loose token matching (false positives) and strict substring matching (199 false negatives from the "Current/Future Price" merged `source_sample` convention) before settling on this exact-match approach against a rebuilt ground truth.

**Status as of last reconciliation run: 234/234 baseline entries matched, 0 duplicates, 4 documented non-baseline exceptions, 2 customers added after the baseline snapshot (see below).**

**Fidelity status (2026-10-02):** all 236 customers match their real file exactly (`reports/fidelity/latest.json`, 04:29 UTC): the 30,604 known-gap cells (27,457 coverage gaps + 3,147 accepted residuals) were copied into `master/real_cell_snapshot.csv` by user decision.

`Erasmus_MC_OTHERS` and `Erasmus_MC_REMOVE` (from the raw NEB validation email) are excluded from the 234-entry baseline count — both are empty placeholder files with zero data rows, confirmed during the original reconciliation.

## How to re-run this reconciliation

1. Fetch the sheet: `mcp__claude_ai_Google_Drive__read_file_content` with fileId `1PkjRwHSFORF0NNOJc7_iYnSX4rg40QBu0g7NwcLv1UU` (the `[merged] Combined baseline inventory` table).
2. For every `config/customers/*.json`, read `display_name` and `template_ref`.
3. Match each baseline `Configuration` string to a customer by **exact** `display_name` equality (not substring/fuzzy — see the memory note above for why).
4. Anything unmatched on either side is a real gap or a real duplicate — investigate before assuming it's a false positive.

## Customers added after the 2026-09-18 baseline snapshot

The baseline sheet is a fixed snapshot from validation emails dated 2026-09-18. A customer added to `incoming/Catalogs/` afterward is correctly absent from the sheet — that's not a gap, just a snapshot that predates the addition. Tracked here instead so this doc stays a complete picture of everything under `config/customers/`, not just what's in the original 234.

| Region | Configuration | Customer slug | Template | Added |
| --- | --- | --- | --- | --- |
| DE | DE - Uniklinikum Erlangen Current Price | `de-uniklinikum-erlangen` | `de-uniklinikum-erlangen` | 2026-09-24 |
| DE | DE - Uniklinikum Erlangen Future Price | `de-uniklinikum-erlangen-future` | `de-uniklinikum-erlangen` | 2026-09-24 |

Real per-file quirk (verified against `incoming/Catalogs/Current Price/` and `.../Future Price/` before modeling it): the Future file's `ContractPrice` always equals `BasePrice` (0% effective discount) even though `PriceModifier` still literally reads `20` — the same "Future reverts to full list price" pattern already confirmed for `de-helf-future`, `eu-imba-future`, and `eu-oms-future`. Modeled as `discount_percentage: 20` (Current) / `0` (Future) with `PriceModifier` a template-level literal constant, not derived from `discount` — see the template's own `description` field.

This customer's real source file is the first semicolon-delimited `.csv` this project fully replicates the delimiter for — `write_csv()` and the template's `delimiter` field were added 2026-09-24 for this. Two pre-existing customers (`universitat-heidelberg`, `unimarket`) also have real semicolon-delimited source files but are still generated as comma-delimited CSV, predating this fix — a known, flagged inconsistency, not fixed as part of this change (out of scope; ask if you want those two aligned as well).

## Fidelity audit history

Per-template gap notes, the long-tail sample audit and the float-rounding sweep moved to [`docs/archive/fidelity-audit-history.md`](archive/fidelity-audit-history.md) (superseded: all 236 customers match as of 2026-10-02).

## Current Price (113)

| Region | Baseline Configuration | Customer slug | Template |
| --- | --- | --- | --- |
| DE | DE - Bayer Current Price | `bayer-de` | `de-bmecat-bayer` |
| DE | DE - Boehringer Forschungsinstitut Austria Current Price | `boehringer-forschungsinstitut-austria` | `de-boehringer-austria` |
| DE | DE - Boehringer Pharma Germany Current Price | `boehringer-pharma-germany` | `de-boehringer-pharma` |
| DE | DE - Boehringer RCV Austria Current Price | `boehringer-rcv-austria` | `de-boehringer-austria` |
| DE | DE - Charite Current Price | `charite` | `de-charite-eclass` |
| DE | DE - Coupa Marketplace Current Price | `coupa-marketplace-de` | `coupa-marketplace` |
| DE | DE - Curevac SE Current Price Export | `curevac-se` | `curevac-se` |
| DE | DE - DKFZ Current Price | `dkfz` | `dkfz-bmecat` |
| DE | DE - Evotec Current Price | `evotec` | `evotec-coupa-style` |
| DE | DE - FSKK Current Price | `fskk` | `fskk-staffelpreis` |
| DE | DE - Fisher Scientific Products Current Price | `fisher-scientific-de` | `fisher-scientific-regional` |
| DE | DE - GHX Europe Catalog | `de-ghx-europe-catalog` | `de-ghx-europe-catalog` |
| DE | DE - HELF Current Price | `de-helf` | `de-helf` |
| DE | DE - Helmholtz Current Price | `helmholtz` | `sap-gdsn-price-de` |
| DE | DE - Max Planck Current Price | `max-planck` | `max-planck-sap-tiered` |
| DE | DE - Max Planck Products | `max-planck-products` | `max-planck-products` |
| DE | DE - Max Planck Products Current Price Mercateo | `max-planck-mercateo` | `sap-gdsn-price-de` |
| DE | DE - Max Planck eCLASS Export | `max-planck-eclass` | `max-planck-eclass` |
| DE | DE - Merck KGaA Current Price | `merck-kgaa` | `merck-kgaa-de` |
| DE | DE - RPTU Kaiserlautern Current Price | `rptu-kaiserlautern` | `rptu-kaiserlautern` |
| DE | DE - Uni Bonn Current Price | `uni-bonn` | `sap-gdsn-price-de` |
| DE | DE - Uni Gottingen Current Price | `uni-gottingen` | `sap-gdsn-price-de` |
| DE | DE - Uniklinikum Köln Current Price | `uniklinikum-koeln` | `uniklinikum-koeln` |
| DE | DE - Universitat Heidelberg Current Price | `universitat-heidelberg` | `universitat-heidelberg` |
| DE | DE - VWR Current Price | `vwr-de` | `vwr-de-mel` |
| EU | EU - Institut für Molekulare Biotechnologie GmbH (IMBA) Current Price | `eu-imba` | `eu-imba` |
| EU | EU - OMS Current Price | `eu-oms` | `eu-oms` |
| EU | EU - Zageno Current Price | `eu-zageno` | `eu-zageno` |
| EU | Europe Current Price Export | `europe-price-export` | `europe-price-export` |
| UK | UK - Amici Catalog Current Price | `amici` | `amici-catalog` |
| UK | UK - Coupa Marketplace Current Price | `coupa-marketplace-uk` | `coupa-marketplace` |
| UK | UK - Fisher Scientific Products Current Price | `fisher-scientific-uk` | `fisher-scientific-regional` |
| UK | UK - One Advanced Current Price | `one-advanced` | `one-advanced-network` |
| UK | UK - One Advanced Products | `one-advanced-products` | `one-advanced-products` |
| UK | UK - Sciquest Current Price | `uk-sciquest-network` | `uk-sciquest-network` |
| UK | UK - Sciquest Products | `sciquest-products-uk` | `sciquest-products-uk` |
| UK | UK - University of Dundee Current Price | `uni-dundee` | `uni-dundee` |
| UK | UK - University of Edinburgh Current Price | `uni-edinburgh` | `uni-edinburgh` |
| UK | UK - University of Hull Current Price | `uni-hull` | `uni-hull` |
| UK | UK - University of Nottingham Current Price | `uni-nottingham` | `uni-nottingham` |
| UK | UK - University of Oxford Current Price | `uni-oxford` | `uni-oxford` |
| UK | UK - University of Strathclyde Current Price | `uni-strathclyde` | `uni-strathclyde` |
| UK | UK - VWR Current Price | `vwr-uk` | `vwr-uk-mel` |
| UK | UK - Vertex LabViva Current Price | `vertex-labviva-uk` | `vertex-labviva-uk` |
| UK | UK - Zageno Current Price | `uk-zageno` | `uk-zageno` |
| US | US - Abbott Abbvie HubWoo Current Price | `abbvie-hubwoo` | `punchout-tiered` |
| US | US - Ad-Hoc catalog Current Price | `adhoc-master` | `adhoc-full` |
| US | US - Albany Medical Center Current Price | `albany-medical-center` | `hospital-minimal` |
| US | US - Ariba 0% Discount (Market Rate) Current Price | `ariba-0pct-cif` | `ariba-tiered-txt` |
| US | US - Ariba 05% Discount Current Price | `ariba-5pct-cif` | `ariba-tiered-txt` |
| US | US - Ariba 05% Discount Current Price EXCEL | `ariba-5pct` | `ariba-tiered` |
| US | US - Ariba 11% Discount Current Price | `ariba-11pct-cif` | `ariba-tiered-txt` |
| US | US - Ariba 11% Discount Current Price EXCEL | `ariba-11pct` | `ariba-tiered` |
| US | US - Ariba Current Price CSV | `ariba-current-csv` | `ariba-current-csv` |
| US | US - Ariba Proxy | `ariba-proxy-cif` | `ariba-proxy-txt` |
| US | US - Ariba Proxy EXCEL | `ariba-proxy` | `ariba-proxy` |
| US | US - Bio Products Inc Current Price | `bio-products-inc` | `distributor-discount` |
| US | US - Bioresco Univ of Maryland Current Price | `bioresco-univ-maryland` | `bioresco-univ-maryland` |
| US | US - Cincinnati Childrens Hospital Current Price | `cincinnati-childrens` | `cincinnati-childrens` |
| US | US - Coupa Current Price | `coupa-csv-us` | `coupa-csv-us` |
| US | US - Coupa Marketplace Current Price | `coupa-marketplace-us` | `coupa-marketplace` |
| US | US - Dana Farber Cancer Institute (Main) Current Price | `dana-farber` | `dana-farber` |
| US | US - Fisher Current Price | `fisher-us` | `fisher-us` |
| US | US - Fisher Regulatory Info | `fisher-regulatory-info` | `fisher-regulatory-info` |
| US | US - GSS Current Price | `gss` | `gss` |
| US | US - Gene X - Press Current Price | `gene-x-press` | `distributor-synonyms` |
| US | US - Genentech Current Price | `genentech` | `genentech` |
| US | US - Genentech Products | `genentech-products` | `genentech-products` |
| US | US - Genetica Y Tecnologia Spa Current Price | `genetica-y-tecnologia` | `distributor-synonyms` |
| US | US - Gladstone Institute Current Price | `gladstone-institute` | `distributor-staggered-gladstone` |
| US | US - Google Merchant Center Current Price | `google-merchant-center` | `google-merchant-center` |
| US | US - Intramalls NIH Current Price | `intramalls-network` | `intramalls-network` |
| US | US - Johns Hopkins University Core Store Current Price | `jhu-core-store` | `jhu-core-store` |
| US | US - Joslin Current Price | `joslin` | `joslin` |
| US | US - LSDS Current Price | `lsds` | `distributor-staggered-lsds` |
| US | US - La Jolla Institute for Immunology Current Price | `la-jolla-immunology` | `la-jolla-immunology` |
| US | US - LabViva Current Price | `labviva-network` | `labviva-network` |
| US | US - LabViva UCOP Current Price | `labviva-ucop-network` | `labviva-ucop-network` |
| US | US - Labequim SA DE CV Current Price | `labequim` | `distributor-synonyms` |
| US | US - Mass General Brigham Current Price | `mass-general-brigham` | `mass-general-brigham` |
| US | US - Mayo Clinic Current Price | `mayo-clinic` | `mayo-clinic` |
| US | US - Michigan State Univ BMB Store Current Price | `michigan-state-bmb` | `michigan-state-bmb` |
| US | US - Migliore Current Price | `migliore` | `distributor-synonyms` |
| US | US - NYU School of Medicine Current Price | `nyu-school-of-medicine` | `nyu-school-of-medicine` |
| US | US - Neta Scientific Current Price | `neta-scientific` | `neta-scientific` |
| US | US - New England Biolabs Canada Current Price | `new-england-biolabs-canada` | `new-england-biolabs-canada` |
| US | US - Ohio State Univ Freezer Acct (MBSC) Current Price | `ohio-state-freezer` | `ohio-state-freezer` |
| US | US - PMV PHARMA INC Current Price | `pmv-pharma` | `pmv-pharma` |
| US | US - Partners Current Price | `partners` | `partners` |
| US | US - Pfizer Current Price | `pfizer` | `pfizer` |
| US | US - Quartzy Current Price | `quartzy` | `quartzy` |
| US | US - Quimica Valaner S.A. de C.V Current Price | `quimica-valaner` | `distributor-synonyms` |
| US | US - Sanofi Genzyme Current Price | `sanofi-genzyme` | `sanofi-genzyme` |
| US | US - Sciquest Current Price | `sciquest-network` | `network-multi-institution` |
| US | US - Sciquest Level 2 PunchOut Proxy | `sciquest-level2-proxy` | `sciquest-level2-proxy` |
| US | US - Sciquest Products | `sciquest-products-us` | `sciquest-products-us` |
| US | US - Sellex Inc Current Price | `sellex` | `distributor-synonyms` |
| US | US - Stanford University Products | `stanford-products` | `stanford-products` |
| US | US - StonyBrook University Freezer Current Price | `stonybrook-freezer` | `stonybrook-freezer` |
| US | US - Suministros Clinicos Isla SAS Current Price | `suministros-clinicos-isla` | `distributor-synonyms` |
| US | US - Tanirel S.A Current Price | `tanirel` | `distributor-synonyms` |
| US | US - Temple University Health System Current Price | `temple-university-health-system` | `temple-university-health-system` |
| US | US - UAMS Workday Current Price | `uams-workday` | `uams-workday` |
| US | US - UCLA Perfect Commerce Current Price | `ucla-perfect-commerce` | `ucla-perfect-commerce` |
| US | US - UCSD Current Price | `ucsd` | `ucsd` |
| US | US - UniMarket Current Price | `unimarket` | `unimarket` |
| US | US - Uniscience Do Brasil Current Price | `uniscience-do-brasil` | `distributor-synonyms` |
| US | US - Univ Of Tennessee Level 2 PunchOut Products | `univ-tennessee` | `univ-tennessee` |
| US | US - University of Pennsylvania Cell Center Current Price | `upenn-cell-center` | `upenn-cell-center` |
| US | US - VWR Current Price | `vwr-network-us` | `vwr-network-us` |
| US | US - Virginia Commenwealth University Supply Center Current Price | `vcu-supply-center` | `vcu-supply-center` |
| US | US - Whitehead Institute Current Price | `whitehead-institute` | `whitehead-institute` |
| US | US - Zageno Current Price | `zageno` | `marketplace-multicurrency` |

## Future Price (100)

| Region | Baseline Configuration | Customer slug | Template |
| --- | --- | --- | --- |
| DE | DE - Bayer Future Price | `bayer-de-future` | `de-bmecat-bayer` |
| DE | DE - Boehringer Forschungsinstitut Austria Future Price | `boehringer-forschungsinstitut-austria-future` | `de-boehringer-austria` |
| DE | DE - Boehringer Pharma Germany Future Price | `boehringer-pharma-germany-future` | `de-boehringer-pharma` |
| DE | DE - Boehringer RCV Austria Future Price | `boehringer-rcv-austria-future` | `de-boehringer-austria` |
| DE | DE - Charite Future Price | `charite-future` | `de-charite-eclass` |
| DE | DE - Coupa Marketplace Future Price | `coupa-marketplace-de-future` | `coupa-marketplace` |
| DE | DE - Curevac SE Future Price Export | `curevac-se-future` | `curevac-se` |
| DE | DE - DKFZ Future Price | `dkfz-future` | `dkfz-bmecat` |
| DE | DE - Evotec Future Price | `evotec-future` | `evotec-coupa-style` |
| DE | DE - FSKK Future Price | `fskk-future` | `fskk-staffelpreis` |
| DE | DE - Fisher Scientific Products Future Price | `fisher-scientific-de-future` | `fisher-scientific-regional` |
| DE | DE - HELF Future Price | `de-helf-future` | `de-helf` |
| DE | DE - Helmholtz Future Price | `helmholtz-future` | `sap-gdsn-price-de` |
| DE | DE - Max Planck Future Price | `max-planck-future` | `max-planck-sap-tiered` |
| DE | DE - Max Planck Products Future Price Mercateo | `max-planck-mercateo-future` | `sap-gdsn-price-de` |
| DE | DE - Merck KGaA Future Price | `merck-kgaa-future` | `merck-kgaa-de` |
| DE | DE - RPTU Kaiserlautern Future Price | `rptu-kaiserlautern-future` | `rptu-kaiserlautern` |
| DE | DE - Uni Bonn Future Price | `uni-bonn-future` | `sap-gdsn-price-de` |
| DE | DE - Uni Gottingen Future Price | `uni-gottingen-future` | `sap-gdsn-price-de` |
| DE | DE - Uniklinikum Köln Future Price | `uniklinikum-koeln-future` | `uniklinikum-koeln` |
| DE | DE - Universitat Heidelberg Future Price | `universitat-heidelberg-future` | `universitat-heidelberg` |
| DE | DE - VWR Future Price | `vwr-de-future` | `vwr-de-mel` |
| EU | EU - Institut für Molekulare Biotechnologie GmbH (IMBA) Future Price | `eu-imba-future` | `eu-imba` |
| EU | EU - OMS Future Price | `eu-oms-future` | `eu-oms` |
| EU | EU - Zageno Future Price | `eu-zageno-future` | `eu-zageno` |
| EU | Europe Future Price Export | `europe-price-export-future` | `europe-price-export` |
| UK | UK - Amici Catalog Future Price | `amici-future` | `amici-catalog` |
| UK | UK - Coupa Marketplace Future Price | `coupa-marketplace-uk-future` | `coupa-marketplace` |
| UK | UK - Fisher Scientific Products Future Price | `fisher-scientific-uk-future` | `fisher-scientific-regional` |
| UK | UK - One Advanced Future Price | `one-advanced-future` | `one-advanced-network` |
| UK | UK - Sciquest Future Price | `uk-sciquest-network-future` | `uk-sciquest-network` |
| UK | UK - University of Dundee Future Price | `uni-dundee-future` | `uni-dundee` |
| UK | UK - University of Edinburgh Future Price | `uni-edinburgh-future` | `uni-edinburgh` |
| UK | UK - University of Hull Future Price | `uni-hull-future` | `uni-hull` |
| UK | UK - University of Nottingham Future Price | `uni-nottingham-future` | `uni-nottingham` |
| UK | UK - University of Oxford Future Price | `uni-oxford-future` | `uni-oxford` |
| UK | UK - University of Strathclyde Future Price | `uni-strathclyde-future` | `uni-strathclyde` |
| UK | UK - VWR Future Price | `vwr-uk-future` | `vwr-uk-mel` |
| UK | UK - Vertex LabViva Future Price | `vertex-labviva-uk-future` | `vertex-labviva-uk` |
| UK | UK - Zageno Future Price | `uk-zageno-future` | `uk-zageno` |
| US | US - Abbott Abbvie HubWoo Future Price | `abbvie-hubwoo-future` | `punchout-tiered` |
| US | US - Ad-Hoc catalog Future Price | `adhoc-master-future` | `adhoc-full` |
| US | US - Albany Medical Center Future Price | `albany-medical-center-future` | `hospital-minimal` |
| US | US - Ariba 0% Discount (Market Rate) Future Price | `ariba-0pct-cif-future` | `ariba-tiered-txt` |
| US | US - Ariba 05% Discount Future Price | `ariba-5pct-cif-future` | `ariba-tiered-txt` |
| US | US - Ariba 05% Discount Future Price EXCEL | `ariba-5pct-future` | `ariba-tiered` |
| US | US - Ariba 11% Discount Future Price | `ariba-11pct-cif-future` | `ariba-tiered-txt` |
| US | US - Ariba 11% Discount Future Price EXCEL | `ariba-11pct-future` | `ariba-tiered` |
| US | US - Ariba Future Price CSV | `ariba-current-csv-future` | `ariba-current-csv` |
| US | US - Bio Products Inc Future Price | `bio-products-inc-future` | `distributor-discount` |
| US | US - Bioresco Univ of Maryland Future Price | `bioresco-univ-maryland-future` | `bioresco-univ-maryland` |
| US | US - Cincinnati Childrens Hospital Future Price | `cincinnati-childrens-future` | `cincinnati-childrens` |
| US | US - Coupa Future Price | `coupa-csv-us-future` | `coupa-csv-us` |
| US | US - Coupa Marketplace Future Price | `coupa-marketplace-us-future` | `coupa-marketplace` |
| US | US - Dana Farber Cancer Institute (Main) Future Price | `dana-farber-future` | `dana-farber` |
| US | US - Fisher Future Price | `fisher-us-future` | `fisher-us` |
| US | US - GSS Future Price | `gss-future` | `gss` |
| US | US - Gene X - Press Future Price | `gene-x-press-future` | `distributor-synonyms` |
| US | US - Genentech Future Price | `genentech-future` | `genentech` |
| US | US - Genetica Y Tecnologia Spa Future Price | `genetica-y-tecnologia-future` | `distributor-synonyms` |
| US | US - Gladstone Institute Future Price | `gladstone-institute-future` | `distributor-staggered-gladstone` |
| US | US - Google Merchant Center Future Price | `google-merchant-center-future` | `google-merchant-center` |
| US | US - Intramalls NIH Future Price | `intramalls-network-future` | `intramalls-network` |
| US | US - Johns Hopkins University Core Store Future Price | `jhu-core-store-future` | `jhu-core-store` |
| US | US - Joslin Future Price | `joslin-future` | `joslin` |
| US | US - LSDS Future Price | `lsds-future` | `distributor-staggered-lsds` |
| US | US - La Jolla Institute for Immunology Future Price | `la-jolla-immunology-future` | `la-jolla-immunology` |
| US | US - LabViva Future Price | `labviva-network-future` | `labviva-network` |
| US | US - LabViva UCOP Future Price | `labviva-ucop-network-future` | `labviva-ucop-network` |
| US | US - Labequim SA DE CV Future Price | `labequim-future` | `distributor-synonyms` |
| US | US - Mass General Brigham Future Price | `mass-general-brigham-future` | `mass-general-brigham` |
| US | US - Mayo Clinic Future Price | `mayo-clinic-future` | `mayo-clinic` |
| US | US - Michigan State Univ BMB Store Future Price | `michigan-state-bmb-future` | `michigan-state-bmb` |
| US | US - Migliore Future Price | `migliore-future` | `distributor-synonyms` |
| US | US - NYU School of Medicine Future Price | `nyu-school-of-medicine-future` | `nyu-school-of-medicine` |
| US | US - Neta Scientific Future Price | `neta-scientific-future` | `neta-scientific` |
| US | US - New England Biolabs Canada Future Price | `new-england-biolabs-canada-future` | `new-england-biolabs-canada` |
| US | US - Ohio State Univ Freezer Acct (MBSC) Future Price | `ohio-state-freezer-future` | `ohio-state-freezer` |
| US | US - PMV PHARMA INC Future Price | `pmv-pharma-future` | `pmv-pharma` |
| US | US - Partners Future Price | `partners-future` | `partners` |
| US | US - Pfizer Future Price | `pfizer-future` | `pfizer` |
| US | US - Quartzy Future Price | `quartzy-future` | `quartzy` |
| US | US - Quimica Valaner S.A. de C.V Future Price | `quimica-valaner-future` | `distributor-synonyms` |
| US | US - Sanofi Genzyme Future Price | `sanofi-genzyme-future` | `sanofi-genzyme` |
| US | US - Sciquest Future Price | `sciquest-network-future` | `network-multi-institution` |
| US | US - Sellex Inc Future Price | `sellex-future` | `distributor-synonyms` |
| US | US - StonyBrook University Freezer Future Price | `stonybrook-freezer-future` | `stonybrook-freezer` |
| US | US - Suministros Clinicos Isla SAS Future Price | `suministros-clinicos-isla-future` | `distributor-synonyms` |
| US | US - Tanirel S.A Future Price | `tanirel-future` | `distributor-synonyms` |
| US | US - Temple University Health System Future Price | `temple-university-health-system-future` | `temple-university-health-system` |
| US | US - UAMS Workday Future Price | `uams-workday-future` | `uams-workday` |
| US | US - UCLA Perfect Commerce Future Price | `ucla-perfect-commerce-future` | `ucla-perfect-commerce` |
| US | US - UCSD Future Price | `ucsd-future` | `ucsd` |
| US | US - UniMarket Future Price | `unimarket-future` | `unimarket` |
| US | US - Uniscience Do Brasil Future Price | `uniscience-do-brasil-future` | `distributor-synonyms` |
| US | US - University of Pennsylvania Cell Center Future Price | `upenn-cell-center-future` | `upenn-cell-center` |
| US | US - VWR Future Price | `vwr-network-us-future` | `vwr-network-us` |
| US | US - Virginia Commenwealth University Supply Center Future Price | `vcu-supply-center-future` | `vcu-supply-center` |
| US | US - Whitehead Institute Future Price | `whitehead-institute-future` | `whitehead-institute` |
| US | US - Zageno Future Price ⚠️ *(baseline status: Fail)* | `zageno-future` | `zageno-us-future` |

## NEB (Benelux) (21)

| Region | Baseline Configuration | Customer slug | Template |
| --- | --- | --- | --- |
| Benelux | Algemeen_Ziekenhuis_Maastricht_Maastricht_Medisch_Centrum | `algemeen-ziekenhuis-maastricht` | `ghx-gdsn-nl-hospital` |
| Benelux | Amsterdam_Medisch_Centrum | `amsterdam-medisch-centrum` | `ghx-gdsn-nl-hospital` |
| Benelux | BASF_Beneering | `basf-beneering` | `basf-mycatalogcloud-oci` |
| Benelux | BIPP_Articles | `bipp-articles` | `bipp-articles` |
| Benelux | BIPP_Prices_Kul | `bipp-kul` | `bioke-catalogue-price` |
| Benelux | BIPP_Prices_List | `bipp-list` | `bioke-catalogue-price` |
| Benelux | Benelux_VWR_Catalog | `benelux-vwr` | `benelux-vwr-mel` |
| Benelux | DSM | `dsm` | `dsm-cif` |
| Benelux | Erasmus_MC_ADD | `erasmus-mc` | `erasmus-mc-contract-line` |
| Benelux | Intervet_Merck_MSD_Ariba_Catalog | `intervet-merck-msd` | `intervet-merck-msd-cif` |
| Benelux | Leids_Universitair_Medisch_Centrum | `leids-umc` | `ghx-gdsn-nl-hospital` |
| Benelux | Netherlands_Kanker_Instituut | `nki-avl` | `ghx-gdsn-nl-hospital` |
| Benelux | Prijzenbestand_Radboud_UMC | `radboud-umc` | `radboud-umc-price-compare` |
| Benelux | Prinses_Maxima_Centrum | `prinses-maxima-centrum` | `ghx-gdsn-nl-hospital` |
| Benelux | Sanquin_prijslijst | `sanquin` | `sanquin-prijslijst` |
| Benelux | UNIVERSITEIT_LEIDEN_GORLAEUS_LABORATORIUM | `leiden-gorlaeus-lab` | `ghx-gdsn-nl-hospital` |
| Benelux | Universitair_Medisch_Centrum_Groningen | `umc-groningen` | `ghx-gdsn-nl-hospital` |
| Benelux | Universitair_Medisch_Centrum_Utrecht | `umc-utrecht` | `ghx-gdsn-nl-hospital` |
| Benelux | Universiteit_Maastricht_Catalog | `universiteit-maastricht` | `universiteit-maastricht-cif` |
| Benelux | Universiteit_Utrecht_Centrale_Goederenontvangst | `utrecht-centrale-goederenontvangst` | `ghx-gdsn-nl-hospital` |
| Benelux | Wageningen_UR | `wageningen-ur` | `wageningen-catalogus` |

## Non-baseline exceptions

Customers whose `display_name` doesn't correspond to any baseline row — each individually confirmed intentional, not a gap:

| Customer slug | display_name | Why it has no baseline row |
| --- | --- | --- |
| `ariba-0pct-future` | Ariba - 0% Discount (Market Rate) (Future Price) | **Removed 2026-09-26** — no real file and no baseline entry (the real 0% files belong to `ariba-0pct-cif-future`). |
| `ariba-0pct` | Ariba - 0% Discount (Market Rate) | **Removed 2026-09-26** — no real file and no baseline entry (the real 0% files belong to `ariba-0pct-cif`). |
| `tufts` | Tufts University | **Removed 2026-09-26** — obsolete sandbox example (category-discount heuristic demo), no confirmed STEP ID and no real file. |
| `univ-tennessee-future` | US - Univ Of Tennessee Level 2 PunchOut Products (Future Price) | **Removed 2026-09-26** — no real Future file and no baseline entry; it had been created only for symmetry with its Current sibling. |

