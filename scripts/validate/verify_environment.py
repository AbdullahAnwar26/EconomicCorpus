from pathlib import Path
import sys

REQUIRED_PATHS = [
    "config/sources.json",
    "config/sources.template.json",
    "scripts/sources/source_registry.py",
    "scripts/sources/job_registry.py",
    "scripts/scrapers/http_scraper.py",
    "scripts/scrapers/browser_scraper.py",
    "scripts/extract/extract_html.py",
    "scripts/extract/extract_pdf.py",
    "scripts/extract/extract_json.py",
    "scripts/extract/extract_xml.py",
    "scripts/normalize/normalize_data.py",
    "scripts/validate/validate_downloads.py",
    "scripts/validate/validate_corpus.py",
    "scripts/utilities/file_utils.py",
    "scripts/utilities/hash_utils.py",
    "scripts/utilities/logging_utils.py",
    "scripts/utilities/download_manifest.py",
    "scripts/utilities/storage_utils.py",
    "run_pipeline.py",
    "requirements.txt",
]


def main():
    print("=" * 60)
    print("ECONOMIC CORPUS ENVIRONMENT CHECK")
    print("=" * 60)

    missing = []

    for item in REQUIRED_PATHS:
        path = Path(item)

        if path.exists():
            print(f"[OK]      {item}")
        else:
            print(f"[MISSING] {item}")
            missing.append(item)

    print("-" * 60)

    if missing:
        print(f"FAILED: {len(missing)} required files missing")
        sys.exit(1)

    print("PASSED: All required files are present")
    print("=" * 60)


if __name__ == "__main__":
    main()
