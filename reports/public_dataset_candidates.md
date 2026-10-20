# Public economic dataset expansion plan

Audit date: 2026-10-09

This is a ranked candidate plan, not authorization to download full datasets. The
document-oriented pipeline supports HTML, PDF, XML, JSON, and EPUB, but its JSON
extractor flattens keys and values into prose. It is not suitable for preserving
economic observations, dimensions, units, or missing-value semantics. The existing
acquisition path has three-attempt retry handling, `Retry-After`, SHA-256 file
deduplication, temporary files, and source checkpoints. It is not dataset-slice
aware. The existing normalized document schema uses `record_type: document` and
text content.

Pre-pilot disk baseline (all files below `data/`, including manifests): 2,385 files,
403,091,576 bytes. The pilot added one 302-byte raw API response and one structured
JSONL observation file. Existing reports separately record 312 acquisition-manifest
files and 588 manager-source-mapped local files; those figures have different
scopes and must not be added together. Existing normalized document counts remain
in `extraction_summary.json`; structured observation records will be reported
separately.

## Ranked candidates

| Rank | Dataset / provider | Format and coverage | Approximate size / cadence | Terms and access assessment | Decision |
|---:|---|---|---|---|---|
| 1 | World Development Indicators — [World Bank](https://databank.worldbank.org/source/world-development-indicators) | Indicators API JSON/XML; global economy, indicator, and time dimensions | Bounded responses are small; full bulk size unverified. Indicator-specific release/update dates. | CC BY 4.0 is the World Bank-produced open-data default, but indicator metadata cites external producers whose individual reuse terms are not exposed. Manual rights clearance is needed. | API schema/paging preflight passed; proposed batch is **held pending rights clearance**. |
| 2 | WEO / IFS — [IMF Data](https://www.imf.org/en/Data) | Official downloads and SDMX 2.1/3.0; global macroeconomic series | Medium to large; WEO is released twice yearly; exact file sizes unverified. | Dataset-level redistribution terms need confirmation. IMF API docs direct interactive API exploration to a signed-in portal; no authenticated access is assumed. | High potential; verify terms and unauthenticated endpoints before acquisition. |
| 3 | NIPA / Regional / International — [BEA](https://www.bea.gov/data) | JSON/XML API and table exports; U.S. national, regional, and international statistics | Medium across selected tables; table-specific quarterly/annual cadence; bulk size unverified. | API key required. Use only an authorized locally configured key; confirm table-level reuse notices. | High relevance; validate terms and metadata before an API batch. |
| 4 | County Business Patterns / Economic Census — [U.S. Census](https://www.census.gov/data/data-tools/cbp.html) | API JSON and official bulk tables; U.S. county/industry detail | Potentially large; annual CBP and roughly five-year Economic Census; 2023 county ZIP URL identified, but the archive was not downloaded. | Census terms permit API use and prohibit re-identification; whether they cover reuse/redistribution of the separate ZIP remains unresolved. | Skipped: bulk-ZIP acquisition is blocked pending dataset-specific terms; no data request started. |
| 5 | OECD statistical datasets — [OECD Data Explorer](https://data-explorer.oecd.org/) | SDMX-CSV/JSON; international comparative series | Large and dataset-specific; many periodicities. | Dataset-specific reuse terms need review. A public SDMX request returned HTTP 403 in this audit; no retry or workaround was attempted. | Medium; no automated request until access/terms are resolved. |
| 6 | FRED / ALFRED — [Federal Reserve Bank of St. Louis](https://fred.stlouisfed.org/) | API JSON/XML and series CSV; U.S. and international series vary | Large across series/vintages; daily through annual cadence. | API terms warn series may be third-party owned. Copyright-marked series require contacting the data owner before use beyond personal use; an API key is required. | Medium only after per-series rights screening; do not redistribute restricted series. |
| 7 | CPI / employment / productivity — [BLS](https://www.bls.gov/data/) | Public API JSON and downloadable tables; U.S. national and subnational | Medium/large by series; monthly, quarterly, or annual. | Review series notes and terms. Official documentation fetch returned HTTP 403 in this audit; no data request was attempted. | Medium; no workaround or alternative host. |
| 8 | National Accounts / PLFS — [MoSPI, India](https://mospi.gov.in/) | Release files vary; machine-readable CSV/XLS availability needs confirmation | Medium/large by release; periodic or annual. | Dataset-level terms not verified. | Medium; verify rights and stable machine-readable files. |
| 9 | Database on Indian Economy — [RBI DBIE](https://dbie.rbi.org.in/) | Portal tables/downloads; formats and API not confirmed | Potentially large; series-specific daily-to-annual cadence. | Redistribution terms unclear. Portal request failed at transport/DNS level; no retries or alternate route. | Medium; manual review before retry/collection. |
| 10 | National Accounts / SDG — [UN Statistics Division](https://unstats.un.org/) | Official exports and APIs vary; global country coverage | Large, dataset-dependent; update cadence varies. | Source-specific rights and attribution required. | Low until a concrete dataset, endpoint, and license are identified. |

The machine-readable dataset details and verification notes are in
[`public_dataset_candidates.json`](./public_dataset_candidates.json).

## WDI proposed batch and preflight

The proposed first expansion is 7 indicators x USA and India x 2020–2022
(maximum 42 country-indicator-year observations). The test used GDP for both
countries and all three years with `per_page=5`: HTTP 200, 6 observations over
2 pages, 1,426 response bytes, six unique country-year keys, and no missing
values. Returned indicator IDs matched the request. Pagination totals matched
the fetched pages.

The API returned empty `unit` fields, so the ingestor must retain those fields
unchanged and preserve indicator names and source notes as semantic descriptions.
Indicator metadata requests succeeded for all seven codes. Named upstream sources
include OECD/national statistics and central banks, IMF, ILO, UN, Eurostat, and
World Bank estimates. The Indicators API does not expose those upstream licenses.
The batch is therefore held pending confirmation that the WDI license covers the
cited inputs. No expansion records were acquired, and the original one-observation
USA GDP 2021 pilot remains unchanged.

See [`wdi_batch_preflight_validation.json`](./wdi_batch_preflight_validation.json)
and [`wdi_indicator_metadata_review.json`](./wdi_indicator_metadata_review.json)
for the full preflight and per-indicator source metadata. The current ingestor is
still single-country/single-year and ignores response pagination; batch support
and tests have not yet been implemented.

## Minimal structured pilot

The new independent ingestor saves the raw API response and normalized observations
as separate, hash-indexed files. Each observation is retained as a structured
object (including indicator, country, ISO code, date, value, unit, observation
status, and decimal precision); it is not flattened into document text. The
request is canonicalized and persisted in a separate structured acquisition
manifest. Re-running the same successful request verifies both local hashes and
skips the network.

The pilot default is one country, one indicator, one year, with a hard response
size and record limit. It is explicitly not a claim of complete WDI coverage. No
large archive was started. After running the pilot, review
`public_dataset_validation.json` and the recorded dataset license metadata before
choosing the next bounded batch.

Candidate licensing/action items are also recorded in
`public_dataset_rights_review.csv`; request failures are kept separately in
`public_dataset_failures.json`.
