cls# EconomicCorpus

## Acquisition

The resumable manager-source acquisition workflow reads `reports/source_inventory.json`,
applies source-specific access and rights policies, and writes verified material under
`data/raw/`. It does not normalize or chunk the collected text.

Run the full manager-source acquisition:

```powershell
.venv\Scripts\python.exe run_pipeline.py
```

Run a bounded test against selected inventory sources:

```powershell
.venv\Scripts\python.exe run_pipeline.py --source-ids economic_source_004 economic_source_008 economic_source_019 --max-pages 3
```

The workflow appends acquisition events to `reports/acquisition_manifest.jsonl` and
updates `acquisition_summary.json`, `source_status.json`, `source_failures.json`,
`source_exclusions.json`, and `duplicate_report.json`. OpenStax crawl checkpoints
are kept in `reports/acquisition_state.json`. Existing verified identical files are
reused by SHA-256; failed URLs are limited to three attempts across runs.
Run details are appended to `logs/acquisition.log`.

## Resumable extraction

Extract existing manager-approved full-text files and explicitly labeled sample
material (no network access):

```powershell
.venv\Scripts\python.exe -m scripts.extract.batch_extract --include-samples
```

Use `--limit 2` for a small local test, `--source-ids economic_source_008` for a
single source, or `--reports-only` to refresh status reports after acquisition.
Successful normalized records are appended to
`data/processed/normalized_corpus.jsonl`; `extraction_manifest.jsonl` tracks file
hashes and resumable outcomes. Rights-unclear and paid/sample-only complete-book
sources are excluded from extraction and listed in `reports/rights_review.csv`.
Batch status is recorded in `reports/extraction_summary.json`,
`reports/extraction_failures.csv`, `reports/final_source_status.csv`, and
`reports/final_corpus_summary.md`.

## Structured public datasets

The document extractor flattens JSON values into prose and should not be used for
economic observations. The separate World Bank WDI pilot preserves the native
observation fields and units in structured JSONL, stores the original API response,
and records request/file hashes in
`reports/public_dataset_acquisition_manifest.jsonl`. The pilot is bounded to one
country, indicator, and year:

```powershell
.venv\Scripts\python.exe -m scripts.datasets.structured_ingest
```

The default pilot requests USA GDP (current US$) for 2021. It writes a structured
validation report and skips the request when the same successful dataset slice is
already present and hash-valid. This does not acquire or imply completeness of the
full WDI dataset. Candidate datasets, access findings, and rights-review items are
listed in `reports/public_dataset_candidates.md` and
`reports/public_dataset_candidates.json`; candidate rights-review items and
acquisition failures are written separately to
`reports/public_dataset_rights_review.csv` and
`reports/public_dataset_failures.json`.