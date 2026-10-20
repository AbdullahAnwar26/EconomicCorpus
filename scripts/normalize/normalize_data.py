import json
from datetime import datetime, timezone


def normalize_record(
    source_id,
    source_url,
    title,
    content,
    record_type="document",
    metadata=None
):
    return {
        "source_id": source_id,
        "source_url": source_url,
        "title": title,
        "record_type": record_type,
        "content": content,
        "metadata": metadata or {},
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": "1.0"
    }


def save_normalized(records, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open(
        "w",
        encoding="utf-8"
    ) as f:
        for record in records:
            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False
                ) + "\n"
            )
