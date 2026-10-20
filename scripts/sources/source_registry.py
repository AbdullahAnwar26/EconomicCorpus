import json
from pathlib import Path

SOURCES_PATH = Path("config/sources.json")


def load_sources():
    if not SOURCES_PATH.exists():
        return []

    with SOURCES_PATH.open(
        "r",
        encoding="utf-8-sig"
    ) as f:
        data = json.load(f)

    return data.get("sources", [])


def get_source(source_id):
    for source in load_sources():
        if source.get("source_id") == source_id:
            return source

    return None


def enabled_sources():
    return [
        source
        for source in load_sources()
        if source.get("enabled", True)
    ]


def source_count():
    return len(load_sources())
