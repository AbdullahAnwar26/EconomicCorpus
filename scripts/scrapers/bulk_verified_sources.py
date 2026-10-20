import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

SOURCES = Path("reports/source_verification.json")
OUTPUT_ROOT = Path("data/raw")
MANIFEST = Path("reports/bulk_scrape_manifest.jsonl")

MAX_SOURCES = 16

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def safe_name(text):
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in text
    )[:180]

def build_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": (
            "EconomicCorpusResearchBot/1.0 "
            "(research data collection)"
        )
    })

    return session

def load_existing_hashes():
    hashes = set()

    if not MANIFEST.exists():
        return hashes

    with MANIFEST.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if item.get("status") == "success":
                    hashes.add(item.get("sha256"))
            except json.JSONDecodeError:
                continue

    return hashes

def write_manifest(item):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

def main():
    data = json.loads(
        SOURCES.read_text(encoding="utf-8-sig")
    )

    sources = [
        s for s in data["sources"]
        if s.get("verified") is True
    ][:MAX_SOURCES]

    print("=" * 70)
    print("ECONOMIC CORPUS - VERIFIED SOURCE SCRAPER")
    print("=" * 70)
    print(f"Sources selected: {len(sources)}")

    existing_hashes = load_existing_hashes()
    session = build_session()

    success = 0
    duplicate = 0
    failed = 0

    for index, source in enumerate(sources, start=1):
        source_id = source["source_id"]
        url = source["url"]

        print()
        print(f"[{index}/{len(sources)}] {source_id}")
        print(f"URL: {url}")

        output_dir = OUTPUT_ROOT / source_id
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            response = session.get(
                url,
                timeout=(20, 60),
                allow_redirects=True
            )

            status_code = response.status_code
            print(f"HTTP: {status_code}")

            if status_code >= 400:
                failed += 1
                write_manifest({
                    "source_id": source_id,
                    "url": url,
                    "final_url": response.url,
                    "status": "failed",
                    "http_status": status_code,
                    "error": f"HTTP {status_code}",
                    "timestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                })
                continue

            content = response.content
            checksum = sha256_bytes(content)

            if checksum in existing_hashes:
                duplicate += 1
                print("DUPLICATE: SHA256 already exists")
                write_manifest({
                    "source_id": source_id,
                    "url": url,
                    "final_url": response.url,
                    "sha256": checksum,
                    "status": "duplicate",
                    "http_status": status_code,
                    "timestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                })
                continue

            content_type = (
                response.headers
                .get("content-type", "")
                .lower()
            )

            if "pdf" in content_type or url.lower().endswith(".pdf"):
                extension = ".pdf"
            else:
                extension = ".html"

            filename = safe_name(source_id) + extension
            output_path = output_dir / filename

            output_path.write_bytes(content)

            existing_hashes.add(checksum)

            success += 1

            print(f"SAVED: {output_path}")
            print(f"SIZE: {len(content):,} bytes")
            print(f"SHA256: {checksum}")

            write_manifest({
                "source_id": source_id,
                "url": url,
                "final_url": response.url,
                "path": str(output_path),
                "sha256": checksum,
                "size": len(content),
                "content_type": content_type,
                "status": "success",
                "http_status": status_code,
                "timestamp": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

        except requests.RequestException as exc:
            failed += 1
            print(f"ERROR: {exc}")

            write_manifest({
                "source_id": source_id,
                "url": url,
                "status": "failed",
                "error": str(exc),
                "timestamp": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

    print()
    print("=" * 70)
    print("SCRAPE SUMMARY")
    print("=" * 70)
    print(f"Success:   {success}")
    print(f"Duplicate: {duplicate}")
    print(f"Failed:    {failed}")
    print(f"Manifest:  {MANIFEST}")

if __name__ == "__main__":
    main()
