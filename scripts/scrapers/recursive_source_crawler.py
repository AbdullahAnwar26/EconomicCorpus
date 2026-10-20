from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
import hashlib
import json
import time

START_URLS = [
    "https://openstax.org/books/principles-economics-3e/pages/1-introduction",
    "https://www.econlib.org/library/Buchanan/buchCv3.html",
    "https://www.econlib.org/library/Mill/mlP.html",
    "https://www.marxists.org/archive/marx/works/1867-c1/",
    "https://www.library.dartmouth.edu/digital/digital-collections/limits-growth"
]

MAX_PAGES_PER_SOURCE = 500

session = requests.Session()
session.headers.update({
    "User-Agent": "EconomicCorpusResearchBot/1.0 (research data collection)"
})

def safe_name(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16] + ".html"

def useful_link(url, start_url):
    parsed = urlparse(url)
    start = urlparse(start_url)

    if parsed.scheme not in ("http", "https"):
        return False

    if parsed.netloc != start.netloc:
        return False

    path = parsed.path.lower()

    # Skip obvious navigation/assets
    blocked = (
        ".css", ".js", ".png", ".jpg", ".jpeg",
        ".gif", ".svg", ".ico", ".woff", ".woff2",
        ".zip", ".mp3", ".mp4", ".xml"
    )

    if path.endswith(blocked):
        return False

    # Skip common utility/account/search URLs
    blocked_words = (
        "/search", "/login", "/account",
        "/privacy", "/terms", "/contact",
        "/about", "/donate"
    )

    if any(word in path for word in blocked_words):
        return False

    return True

for start_url in START_URLS:
    domain = urlparse(start_url).netloc
    source_name = domain.replace(".", "_")
    output_dir = Path("data/raw/crawled") / source_name
    output_dir.mkdir(parents=True, exist_ok=True)

    queue = [start_url]
    seen = set()
    manifest = []

    print()
    print("=" * 70)
    print(f"SOURCE: {start_url}")
    print("=" * 70)

    while queue and len(seen) < MAX_PAGES_PER_SOURCE:
        url = queue.pop(0)

        if url in seen:
            continue

        seen.add(url)

        try:
            r = session.get(url, timeout=(20, 60))
            r.raise_for_status()

            content_type = r.headers.get("content-type", "").lower()

            if "text/html" not in content_type:
                continue

            output = output_dir / safe_name(url)
            output.write_bytes(r.content)

            manifest.append({
                "url": url,
                "path": str(output),
                "sha256": hashlib.sha256(r.content).hexdigest()
            })

            print(
                f"[{len(seen):03}] "
                f"{r.status_code} "
                f"{len(r.content):,} bytes | "
                f"queue={len(queue)}"
            )

            soup = BeautifulSoup(r.content, "lxml")

            for a in soup.find_all("a", href=True):
                next_url = urljoin(url, a["href"]).split("#")[0]

                if useful_link(next_url, start_url):
                    if next_url not in seen and next_url not in queue:
                        queue.append(next_url)

            time.sleep(0.2)

        except Exception as e:
            print(f"[FAILED] {url} -> {e}")

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"Completed: {len(manifest)} HTML pages")

print()
print("RECURSIVE CONTENT CRAWL COMPLETE")
