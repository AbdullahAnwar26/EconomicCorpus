from pathlib import Path
import json

path = Path("reports/source_inventory.json")
data = json.loads(path.read_text(encoding="utf-8-sig"))

for source in data["sources"]:
    if source["source_id"] == "economic_source_009":
        source["url"] = "https://press.princeton.edu/index.php/books/ebook/9780691281971/the-muqaddimah-pdf"

path.write_text(
    json.dumps(data, indent=2, ensure_ascii=False),
    encoding="utf-8"
)

print("economic_source_009 URL repaired.")
print(source["url"])
