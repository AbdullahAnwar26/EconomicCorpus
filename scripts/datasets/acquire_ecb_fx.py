from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

from scripts.datasets.structured_ingest import append_jsonl, read_jsonl, sha256_file


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw"
RAW_DIR = RAW_ROOT / "structured" / "ecb_exchange_rates"
PROCESSED_DIR = ROOT / "data" / "processed" / "structured" / "ecb_exchange_rates"
RAW_PATH = RAW_DIR / "usd_eur_daily_2020_2025.csv"
PROCESSED_PATH = PROCESSED_DIR / "usd_eur_daily_2020_2025.jsonl"
MANIFEST_PATH = ROOT / "reports" / "ecb_fx_acquisition_manifest.jsonl"
STATE_PATH = ROOT / "reports" / "ecb_fx_resume_state.json"
VALIDATION_PATH = ROOT / "reports" / "ecb_fx_validation.json"

DATASET_ID = "ecb_exr_usd_eur_daily_2020_2025"
SERIES_KEY = "EXR.D.USD.EUR.SP00.A"
TERMS_URL = "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html"
API_BASE = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
SOURCE_URL = API_BASE + "?" + urlencode({
    "format": "csvdata",
    "startPeriod": "2020-01-01",
    "endPeriod": "2025-12-31",
})
MAX_ATTEMPTS = 3
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
USER_AGENT = "EconomicCorpusResearchBot/1.0 (public economic statistics research)"
REQUIRED_FIELDS = {"KEY", "TIME_PERIOD", "OBS_VALUE", "CURRENCY", "CURRENCY_DENOM", "UNIT"}
logger = logging.getLogger("economic_corpus.ecb_fx")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if sha256_file(path) == sha256_bytes(content):
            return
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    temp = path.with_name(path.name + ".part")
    try:
        with temp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def retry_delay(response: requests.Response | None, attempt: int) -> float:
    delay = min(2 ** (attempt - 1), 20)
    if response is None:
        return delay
    header = response.headers.get("Retry-After")
    if not header:
        return delay
    try:
        return max(0, float(header))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return delay


def parse_csv(content: bytes, content_type: str) -> tuple[list[str], list[dict[str, str]]]:
    if "csv" not in content_type.lower() and "text/plain" not in content_type.lower():
        raise ValueError(f"Expected CSV content, received {content_type or 'missing content type'}")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"ECB CSV is not UTF-8: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = reader.fieldnames or []
    if not REQUIRED_FIELDS.issubset(fields):
        raise ValueError(f"ECB CSV is missing required fields: {sorted(REQUIRED_FIELDS - set(fields))}")
    records = []
    for index, row in enumerate(reader, start=1):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Malformed CSV row {index}")
        if row["KEY"] != SERIES_KEY or not row["TIME_PERIOD"]:
            raise ValueError(f"Unexpected or incomplete ECB observation at row {index}")
        if row["OBS_VALUE"]:
            try:
                float(row["OBS_VALUE"])
            except ValueError as exc:
                raise ValueError(f"Non-numeric exchange rate at row {index}") from exc
        records.append(row)
        if len(records) > MAX_RECORDS:
            raise ValueError(f"ECB response exceeds the {MAX_RECORDS}-row limit")
    if not records:
        raise ValueError("ECB response contains no observations")
    keys = [(row["KEY"], row["TIME_PERIOD"]) for row in records]
    if len(keys) != len(set(keys)):
        raise ValueError("ECB response contains duplicate series/date observations")
    return fields, records


def existing_raw_by_hash(checksum: str) -> Path | None:
    if not RAW_ROOT.exists():
        return None
    for path in RAW_ROOT.rglob("*"):
        if path.is_file() and not path.name.endswith(".part") and path.name.lower() != "manifest.json":
            try:
                if sha256_file(path) == checksum:
                    return path
            except OSError as exc:
                logger.warning("Could not hash existing file %s: %s", path, exc)
    return None


def record_metadata(content: bytes, raw_path: Path, raw_hash: str, attempts: int) -> dict:
    content_type = "text/csv"
    fields, rows = parse_csv(content, content_type)
    collected_at = now_utc()
    processed = b"".join(
        (
            json.dumps({
                "record_type": "economic_observation",
                "dataset": {
                    "provider": "European Central Bank",
                    "dataset_id": DATASET_ID,
                    "dataset_name": "ECB reference exchange rate, US dollar/Euro",
                    "series_key": SERIES_KEY,
                    "source_url": SOURCE_URL,
                    "terms_url": TERMS_URL,
                    "reuse_terms": (
                        "ECB website information may be freely used if reproduced accurately "
                        "and the ECB is cited as the source. Any modification must be stated."
                    ),
                    "attribution": (
                        "Source: European Central Bank, ECB reference exchange rate, "
                        "US dollar/Euro, 2.15 pm (C.E.T.); unmodified."
                    ),
                    "raw_path": raw_path.relative_to(ROOT).as_posix(),
                    "raw_sha256": raw_hash,
                    "collected_at": collected_at,
                },
                "observation": row,
            }, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for row in rows
    )
    processed_hash = sha256_bytes(processed)
    atomic_write(PROCESSED_PATH, processed)
    return {
        "generated_at": collected_at,
        "dataset_id": DATASET_ID,
        "dataset_name": "ECB reference exchange rate, US dollar/Euro",
        "series_key": SERIES_KEY,
        "source_url": SOURCE_URL,
        "terms_url": TERMS_URL,
        "reuse_terms": "Free use permitted for ECB website information when accurately reproduced and attributed to the ECB; changes must be stated.",
        "attribution": "Source: European Central Bank, ECB reference exchange rate, US dollar/Euro, 2.15 pm (C.E.T.); unmodified.",
        "license_status": "ECB copyright terms explicitly permit free use subject to attribution, accuracy, and modification disclosure.",
        "raw_path": raw_path.relative_to(ROOT).as_posix(),
        "raw_sha256": raw_hash,
        "raw_size_bytes": len(content),
        "processed_path": PROCESSED_PATH.relative_to(ROOT).as_posix(),
        "processed_sha256": processed_hash,
        "records": len(rows),
        "unique_records": len({(row["KEY"], row["TIME_PERIOD"]) for row in rows}),
        "distinct_series": 1,
        "first_date": rows[0]["TIME_PERIOD"],
        "last_date": rows[-1]["TIME_PERIOD"],
        "missing_values": sum(not value for row in rows for value in row.values()),
        "fields": fields,
        "attempts_total": attempts,
        "status": "success",
        "validation": {
            "content_type": "text/csv",
            "required_fields_present": True,
            "numeric_exchange_rates": True,
            "unique_series_date_keys": True,
            "record_limit": MAX_RECORDS,
        },
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manifest = read_jsonl(MANIFEST_PATH)
    state = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        raw = ROOT / state.get("raw_path", "")
        processed = ROOT / state.get("processed_path", "")
        if (
            state.get("status") == "success"
            and raw.is_file()
            and processed.is_file()
            and sha256_file(raw) == state.get("raw_sha256")
            and sha256_file(processed) == state.get("processed_sha256")
        ):
            result = {**state, "status": "skipped_existing", "attempts_this_run": 0}
            append_jsonl(MANIFEST_PATH, result)
            print(json.dumps({
                "dataset_id": DATASET_ID,
                "status": result["status"],
                "bytes": result["raw_size_bytes"],
                "records": result["unique_records"],
                "raw_sha256": result["raw_sha256"],
            }, indent=2))
            return 0

    attempts_used = max(
        (int(row.get("attempts_total") or 0) for row in manifest if row.get("source_url") == SOURCE_URL),
        default=0,
    )
    if RAW_PATH.is_file():
        content = RAW_PATH.read_bytes()
        if len(content) > MAX_BYTES:
            raise ValueError(f"Existing ECB response exceeds {MAX_BYTES} bytes")
        parse_csv(content, "text/csv")
        result = record_metadata(content, RAW_PATH, sha256_bytes(content), attempts_used)
        result.update({"status": "reused_existing", "attempts_this_run": 0})
        write_state(result)
        VALIDATION_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        append_jsonl(MANIFEST_PATH, result)
        print(json.dumps({
            "dataset_id": DATASET_ID,
            "status": result["status"],
            "bytes": result["raw_size_bytes"],
            "records": result["unique_records"],
            "first_date": result["first_date"],
            "last_date": result["last_date"],
            "raw_sha256": result["raw_sha256"],
        }, indent=2))
        return 0
    if attempts_used >= MAX_ATTEMPTS:
        raise RuntimeError(f"Retry limit reached for ECB series ({MAX_ATTEMPTS} attempts)")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/csv"})
    last_error = None
    for attempt in range(attempts_used + 1, MAX_ATTEMPTS + 1):
        response = None
        state.update({
            "dataset_id": DATASET_ID,
            "source_url": SOURCE_URL,
            "status": "downloading",
            "attempts_total": attempt,
            "updated_at": now_utc(),
        })
        write_state(state)
        try:
            response = session.get(SOURCE_URL, timeout=(10, 60), stream=True)
            if response.status_code in {401, 403, 404}:
                raise RuntimeError(f"HTTP {response.status_code}; not retried")
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(retry_delay(response, attempt))
                    continue
                break
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "csv" not in content_type.lower() and "text/plain" not in content_type.lower():
                raise ValueError(f"Expected CSV content, received {content_type or 'missing content type'}")
            content = bytearray()
            for chunk in response.iter_content(256 * 1024):
                if chunk:
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError(f"ECB response exceeds {MAX_BYTES} bytes")
            body = bytes(content)
            parse_csv(body, content_type)
            raw_hash = sha256_bytes(body)
            duplicate = existing_raw_by_hash(raw_hash)
            raw_target = duplicate or RAW_PATH
            if duplicate is None:
                atomic_write(RAW_PATH, body)
            result = record_metadata(body, raw_target, raw_hash, attempt)
            result.update({
                "final_url": response.url,
                "http_status": response.status_code,
                "attempts_this_run": attempt - attempts_used,
                "duplicate_of": duplicate.relative_to(ROOT).as_posix() if duplicate else None,
            })
            write_state(result)
            VALIDATION_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            append_jsonl(MANIFEST_PATH, result)
            print(json.dumps({
                "dataset_id": DATASET_ID,
                "status": result["status"],
                "bytes": result["raw_size_bytes"],
                "records": result["unique_records"],
                "first_date": result["first_date"],
                "last_date": result["last_date"],
                "raw_sha256": result["raw_sha256"],
            }, indent=2))
            return 0
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= MAX_ATTEMPTS or (isinstance(exc, RuntimeError) and "not retried" in str(exc)):
                break
            time.sleep(retry_delay(response, attempt))
        finally:
            if response is not None:
                response.close()

    failure = {
        "dataset_id": DATASET_ID,
        "source_url": SOURCE_URL,
        "status": "blocked" if last_error and "HTTP 403" in last_error else "failed",
        "attempts_total": max(attempts_used, int(state.get("attempts_total") or 0)),
        "error": last_error or "ECB download failed",
        "updated_at": now_utc(),
    }
    write_state(failure)
    append_jsonl(MANIFEST_PATH, failure)
    logger.error("%s: %s", failure["status"], failure["error"])
    return 1


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_name(STATE_PATH.name + ".part")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, STATE_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
