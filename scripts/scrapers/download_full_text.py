from pathlib import Path
import requests
from urllib.parse import urljoin

jobs = {
    "economic_source_003": [
        "https://gutenberg.net.au/ebooks03/0300071h/0-index.html"
    ],
    "economic_source_004": [
        "https://www.gutenberg.org/cache/epub/30107/pg30107-images.html"
    ],
    "economic_source_005": [
        "https://www.gutenberg.org/cache/epub/38194/pg38194-images.html"
    ],
    "economic_source_010": [
        "https://russell-davidson.research.mcgill.ca/EIE-davidson-mackinnon-2021.pdf",
        "https://russell-davidson.research.mcgill.ca/ETM-davidson-mackinnon-2021.pdf"
    ],
    "economic_source_014": [
        "https://www.gutenberg.org/cache/epub/15776/pg15776-images.html"
    ],
    "economic_source_015": [
        "https://www.gutenberg.org/cache/epub/3300/pg3300-images.html"
    ],
    "economic_source_016": [
        "https://www.gutenberg.org/cache/epub/33310/pg33310-images.html"
    ],
    "economic_source_019": [
        "https://www.marxists.org/archive/marx/works/1867-c1/download/pdf/Capital-Volume-I.pdf"
    ]
}

session = requests.Session()
session.headers.update({
    "User-Agent": "EconomicCorpusResearchBot/1.0 (research data collection)"
})

for source_id, urls in jobs.items():
    output_dir = Path("data/raw") / source_id
    output_dir.mkdir(parents=True, exist_ok=True)

    for url in urls:
        print(f"Downloading: {url}")

        response = session.get(
            url,
            timeout=(20, 120),
            allow_redirects=True
        )
        response.raise_for_status()

        filename = Path(url.split("?")[0]).name or "download"
        output = output_dir / filename
        output.write_bytes(response.content)

        print(f"  SAVED: {output}")
        print(f"  SIZE: {len(response.content):,} bytes")

print()
print("DIRECT FULL-TEXT DOWNLOADS COMPLETE")
