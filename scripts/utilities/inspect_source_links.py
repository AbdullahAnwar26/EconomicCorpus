from pathlib import Path
from bs4 import BeautifulSoup

for path in Path("data/raw").glob("*/**/*.html"):
    soup = BeautifulSoup(
        path.read_text(encoding="utf-8", errors="replace"),
        "lxml"
    )

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if href:
            links.append((text, href))

    print("=" * 70)
    print(path)
    print(f"Links found: {len(links)}")

    for text, href in links[:30]:
        print(f"  {text[:80]} -> {href}")
