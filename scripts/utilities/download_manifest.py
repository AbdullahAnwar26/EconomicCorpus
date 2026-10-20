import json
from pathlib import Path
from datetime import datetime, timezone

MANIFEST_PATH = Path("reports/download_manifest.json")


def load_manifest():
    if not MANIFEST_PATH.exists():
        return []

    with MANIFEST_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_manifest(entries):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    with MANIFEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def record_download(url, path, sha256, status="success"):
    entries = load_manifest()

    duplicate = any(
        entry.get("sha256") == sha256
        for entry in entries
        if entry.get("status") == "success"
    )

    entry = {
        "url": url,
        "path": str(path),
        "sha256": sha256,
        "status": status,
        "duplicate": duplicate,
        "downloaded_at": datetime.now(timezone.utc).isoformat()
    }

    entries.append(entry)
    save_manifest(entries)

    return entry


def find_by_hash(sha256):
    for entry in load_manifest():
        if entry.get("sha256") == sha256:
            return entry

    return None
