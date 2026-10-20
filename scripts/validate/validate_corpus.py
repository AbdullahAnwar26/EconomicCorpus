import json
from pathlib import Path


REQUIRED_FIELDS = [
    "source_id",
    "source_url",
    "title",
    "record_type",
    "content",
    "metadata",
    "collected_at",
    "schema_version",
]


def validate_record(record):
    errors = []

    if not isinstance(record, dict):
        return ["Record is not a dictionary"]

    for field in REQUIRED_FIELDS:
        if field not in record:
            errors.append(f"Missing field: {field}")

    if not record.get("source_id"):
        errors.append("Empty source_id")

    if not record.get("title"):
        errors.append("Empty title")

    if not record.get("content"):
        errors.append("Empty content")

    return errors


def validate_jsonl(path):
    path = Path(path)

    total = 0
    valid = 0
    invalid = 0
    errors = []

    with path.open("r", encoding="utf-8-sig") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue

            total += 1

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                invalid += 1
                errors.append(
                    f"Line {line_number}: invalid JSON: {exc}"
                )
                continue

            record_errors = validate_record(record)

            if record_errors:
                invalid += 1
                errors.append(
                    f"Line {line_number}: {record_errors}"
                )
            else:
                valid += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
    }
