import json
from pathlib import Path
import requests

INPUT = Path("reports/source_inventory.json")
OUTPUT = Path("reports/source_verification.json")

with INPUT.open("r", encoding="utf-8-sig") as f:
    data = json.load(f)

results = []

session = requests.Session()
session.headers.update({
    "User-Agent": "EconomicCorpusResearchBot/1.0"
})

for source in data["sources"]:
    url = source["url"]

    result = dict(source)

    try:
        response = session.get(
            url,
            timeout=30,
            allow_redirects=True
        )

        result["http_status"] = response.status_code
        result["final_url"] = response.url
        result["verified"] = response.ok

        if "text/html" in response.headers.get("content-type", "").lower():
            result["source_type"] = "html"
        elif "application/pdf" in response.headers.get("content-type", "").lower():
            result["source_type"] = "pdf"
        else:
            result["source_type"] = response.headers.get(
                "content-type",
                "unknown"
            )

        result["error"] = None

    except requests.RequestException as exc:
        result["http_status"] = None
        result["final_url"] = None
        result["verified"] = False
        result["source_type"] = "unknown"
        result["error"] = str(exc)

    results.append(result)

    print(
        f"[{'OK' if result['verified'] else 'FAIL'}] "
        f"{url} -> {result['http_status']}"
    )

OUTPUT.write_text(
    json.dumps(
        {
            "total_sources": len(results),
            "sources": results
        },
        indent=2,
        ensure_ascii=False
    ),
    encoding="utf-8"
)

print("-" * 60)
print(f"Verified: {sum(x['verified'] for x in results)}")
print(f"Failed: {sum(not x['verified'] for x in results)}")
print(f"Saved: {OUTPUT}")
