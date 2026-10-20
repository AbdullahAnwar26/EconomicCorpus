import re
import json
from pathlib import Path

PDF_TEXT = Path("reports/economic_sources_pdf.txt")
OUTPUT = Path("reports/source_inventory.json")

text = PDF_TEXT.read_text(encoding="utf-8")

urls = sorted(set(re.findall(r"https?://[^\s]+", text)))

sources = []

for i, url in enumerate(urls, start=1):
    sources.append({
        "source_id": f"economic_source_{i:03d}",
        "url": url.rstrip(".,)"),
        "enabled": False,
        "verified": False,
        "source_type": "unknown",
        "access_method": "unknown",
        "notes": "Extracted from manager-provided PDF"
    })

OUTPUT.parent.mkdir(parents=True, exist_ok=True)

OUTPUT.write_text(
    json.dumps(
        {
            "total_sources": len(sources),
            "sources": sources
        },
        indent=2,
        ensure_ascii=False
    ),
    encoding="utf-8"
)

print(f"Extracted URLs: {len(sources)}")
print(f"Saved: {OUTPUT}")
