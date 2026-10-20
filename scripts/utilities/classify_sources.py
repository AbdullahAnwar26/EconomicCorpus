import json
from pathlib import Path

INPUT = Path("reports/source_verification.json")
OUTPUT = Path("reports/source_classification.json")

with INPUT.open("r", encoding="utf-8-sig") as f:
    data = json.load(f)

classified = []

for source in data["sources"]:
    item = dict(source)

    url = source["url"].lower()
    source_type = source.get("source_type", "unknown")

    if url.endswith(".pdf") or "application/pdf" in source_type:
        content_type = "pdf"
    elif source_type == "html":
        content_type = "html"
    else:
        content_type = "web_page"

    item["content_type"] = content_type

    if not source["verified"]:
        item["scrape_status"] = "needs_review"
    elif content_type == "pdf":
        item["scrape_status"] = "pdf_download"
    else:
        item["scrape_status"] = "web_crawl"

    classified.append(item)

OUTPUT.write_text(
    json.dumps(
        {
            "total_sources": len(classified),
            "sources": classified
        },
        indent=2,
        ensure_ascii=False
    ),
    encoding="utf-8"
)

print(f"Classified: {len(classified)} sources")
print(f"Saved: {OUTPUT}")
