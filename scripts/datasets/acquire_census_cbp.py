from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath

import requests

from scripts.datasets.structured_ingest import append_jsonl, read_jsonl


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw"
RAW_DIR = RAW_ROOT / "structured" / "us_census_cbp"
PROCESSED_DIR = ROOT / "data" / "processed" / "structured" / "us_census_cbp"
ZIP_PATH = RAW_DIR / "cbp23co.zip"
JSONL_PATH = PROCESSED_DIR / "cbp23co.jsonl"
MANIFEST = ROOT / "reports" / "census_cbp_acquisition_manifest.jsonl"
STATE_PATH = ROOT / "reports" / "census_cbp_acquisition_state.json"
VALIDATION_PATH = ROOT / "reports" / "census_cbp_validation.json"
SUMMARY_PATH = ROOT / "reports" / "census_cbp_summary.json"
FAILURES_PATH = ROOT / "reports" / "census_cbp_failures.json"
SOURCE_URL = "https://www2.census.gov/programs-surveys/cbp/datasets/2023/cbp23co.zip"
DATASET_ID = "us_census_cbp_county_2023"
MAX_ATTEMPTS = 3
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
RIGHTS_APPROVED = False
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
USER_AGENT = "EconomicCorpusResearchBot/1.0 (public economic statistics research)"
TERMS_URL = "https://www.census.gov/data/developers/about/terms-of-service.html"
RELEASE_URL = "https://www.census.gov/data/datasets/2023/econ/cbp/2023-cbp.html"
logger = logging.getLogger("economic_corpus.census_cbp")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


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
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return delay


def validate_archive(path: Path) -> tuple[str, list[str]]:
    if path.stat().st_size < 100:
        raise ValueError(f"Archive is unexpectedly small ({path.stat().st_size} bytes)")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError("Downloaded file is not a valid ZIP archive") from exc
    with archive:
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"ZIP CRC check failed for {corrupt}")
        candidates = []
        for name in archive.namelist():
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Unsafe path in archive: {name}")
            if member.suffix.lower() in {".csv", ".txt"} and not name.endswith("/"):
                candidates.append(name)
        if len(candidates) != 1:
            raise ValueError(f"Expected one CBP data table in archive, found {len(candidates)}")
        return candidates[0], archive.namelist()


class CensusCBPIngestor:
    def __init__(self, session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.1",
        })
        self.sleep = sleep
        self.manifest = read_jsonl(MANIFEST)
        self.state = (
            json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if STATE_PATH.exists()
            else {}
        )
        self.archive_path = ZIP_PATH

    def _existing_hash_match(self, checksum: str) -> Path | None:
        if not RAW_ROOT.exists():
            return None
        for path in RAW_ROOT.rglob("*"):
            if not path.is_file() or path.name.endswith(".part") or path.name.lower() == "manifest.json":
                continue
            try:
                if sha256_file(path) == checksum:
                    return path
            except OSError as exc:
                logger.warning("Cannot hash existing file %s: %s", path, exc)
        return None

    def _prior_attempts(self) -> int:
        from_manifest = max(
            (
                int(row.get("attempts_total") or row.get("attempts") or 0)
                for row in self.manifest
                if row.get("source_url") == SOURCE_URL
            ),
            default=0,
        )
        return max(from_manifest, int(self.state.get("attempts_total") or 0))

    def _download(self) -> dict:
        prior = self._prior_attempts()
        if prior >= MAX_ATTEMPTS:
            raise RuntimeError(f"Retry limit reached for CBP archive ({MAX_ATTEMPTS} attempts)")
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        temp = ZIP_PATH.with_name(ZIP_PATH.name + ".part")
        last_error = None
        for attempt in range(prior + 1, MAX_ATTEMPTS + 1):
            response = None
            self.state.update({
                "dataset_id": DATASET_ID,
                "source_url": SOURCE_URL,
                "stage": "downloading",
                "attempts_total": attempt,
                "updated_at": utc_now(),
            })
            write_json_atomic(STATE_PATH, self.state)
            try:
                response = self.session.get(
                    SOURCE_URL,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    allow_redirects=True,
                    stream=True,
                )
                if response.status_code in {401, 403, 404}:
                    raise RuntimeError(f"HTTP {response.status_code}; not retried")
                if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                    raise RuntimeError(f"HTTP {response.status_code}; request not retried")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == MAX_ATTEMPTS:
                        break
                    self.sleep(retry_delay(response, attempt - prior))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if "zip" not in content_type.lower() and "octet-stream" not in content_type.lower():
                    raise ValueError(f"Expected ZIP content type, received {content_type or 'missing'}")
                expected_length = response.headers.get("Content-Length")
                if expected_length and int(expected_length) > MAX_ARCHIVE_BYTES:
                    raise ValueError(f"Archive exceeds {MAX_ARCHIVE_BYTES} byte limit")
                size = 0
                with temp.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ValueError(f"Archive exceeds {MAX_ARCHIVE_BYTES} byte limit")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                validate_archive(temp)
                checksum = sha256_file(temp)
                existing = self._existing_hash_match(checksum)
                if existing is not None:
                    temp.unlink()
                    self.archive_path = existing
                    status = "duplicate"
                    raw_path = existing.relative_to(ROOT).as_posix()
                elif ZIP_PATH.exists():
                    if sha256_file(ZIP_PATH) != checksum:
                        raise FileExistsError(f"Refusing to overwrite existing different archive: {ZIP_PATH}")
                    temp.unlink()
                    self.archive_path = ZIP_PATH
                    status = "reused_existing"
                    raw_path = ZIP_PATH.relative_to(ROOT).as_posix()
                else:
                    os.replace(temp, ZIP_PATH)
                    self.archive_path = ZIP_PATH
                    status = "success"
                    raw_path = ZIP_PATH.relative_to(ROOT).as_posix()
                result = {
                    "dataset_id": DATASET_ID,
                    "source_url": SOURCE_URL,
                    "final_url": response.url,
                    "status": status,
                    "http_status": response.status_code,
                    "attempts_this_run": attempt - prior,
                    "attempts_total": attempt,
                    "raw_path": raw_path,
                    "raw_sha256": checksum,
                    "raw_size_bytes": self.archive_path.stat().st_size,
                    "content_type": content_type,
                    "collected_at": utc_now(),
                }
                self.state.update({
                    **result,
                    "stage": "archive_validated",
                    "updated_at": utc_now(),
                })
                write_json_atomic(STATE_PATH, self.state)
                append_jsonl(MANIFEST, result)
                self.manifest.append(result)
                return result
            except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == MAX_ATTEMPTS or (
                    isinstance(exc, RuntimeError) and "not retried" in str(exc)
                ):
                    break
                self.sleep(retry_delay(response, attempt - prior))
            finally:
                if response is not None:
                    response.close()
        if temp.exists():
            temp.unlink()
        failure = {
            "dataset_id": DATASET_ID,
            "source_url": SOURCE_URL,
            "status": "blocked" if last_error and "HTTP 403" in last_error else "failed",
            "attempts_this_run": max(0, self.state.get("attempts_total", prior) - prior),
            "attempts_total": self.state.get("attempts_total", prior),
            "error": last_error or "Download failed",
            "collected_at": utc_now(),
        }
        self.state.update({**failure, "stage": "download_failed", "updated_at": utc_now()})
        write_json_atomic(STATE_PATH, self.state)
        append_jsonl(MANIFEST, failure)
        self.manifest.append(failure)
        raise RuntimeError(failure["error"])

    def _existing_archive(self) -> dict | None:
        if not self.archive_path.exists():
            return None
        table, members = validate_archive(self.archive_path)
        checksum = sha256_file(self.archive_path)
        return {
            "dataset_id": DATASET_ID,
            "source_url": SOURCE_URL,
            "final_url": SOURCE_URL,
            "status": "reused_existing",
            "attempts_this_run": 0,
            "attempts_total": self._prior_attempts(),
            "raw_path": self.archive_path.relative_to(ROOT).as_posix(),
            "raw_sha256": checksum,
            "raw_size_bytes": self.archive_path.stat().st_size,
            "collected_at": self.state.get("collected_at"),
            "table_member": table,
            "archive_members": members,
        }

    def _extract_records(self, archive_info: dict) -> dict:
        table, members = validate_archive(self.archive_path)
        raw_hash = sha256_file(self.archive_path)
        if JSONL_PATH.exists():
            prior_hash = self.state.get("processed_sha256")
            if prior_hash and sha256_file(JSONL_PATH) == prior_hash and self.state.get("raw_sha256") == raw_hash:
                return self._load_processed_metadata(table, members, raw_hash, "skipped_existing")
            raise FileExistsError(f"Refusing to overwrite unverified or different processed file: {JSONL_PATH}")

        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        temp = JSONL_PATH.with_name(JSONL_PATH.name + ".part")
        temp.unlink(missing_ok=True)
        record_count = 0
        missing_values = 0
        duplicate_rows = 0
        distinct_geo = set()
        years = set()
        fieldnames: list[str] | None = None
        row_digests = set()
        with zipfile.ZipFile(self.archive_path) as archive, archive.open(table) as zipped, \
                io.TextIOWrapper(zipped, encoding="utf-8-sig", newline="") as text, \
                temp.open("w", encoding="utf-8", newline="\n") as output:
            reader = csv.DictReader(text)
            if not reader.fieldnames or any(not name for name in reader.fieldnames):
                raise ValueError("CBP CSV has no valid header")
            fieldnames = list(reader.fieldnames)
            for row in reader:
                if None in row:
                    raise ValueError("CBP CSV row has more values than its header")
                if all(value is None for value in row.values()):
                    continue
                key = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
                if digest in row_digests:
                    duplicate_rows += 1
                row_digests.add(digest)
                geo = row.get("GEO_ID") or row.get("GEOID") or row.get("FIPS")
                if geo:
                    distinct_geo.add(geo)
                year = row.get("YEAR") or row.get("year")
                if year:
                    years.add(year)
                missing_values += sum(value in (None, "") for value in row.values())
                record = {
                    "record_type": "economic_observation",
                    "dataset": {
                        "provider": "U.S. Census Bureau",
                        "dataset_id": DATASET_ID,
                        "dataset_name": "County Business Patterns, 2023, County File",
                        "release_url": RELEASE_URL,
                        "download_url": SOURCE_URL,
                        "terms_url": TERMS_URL,
                        "reuse_basis": "Census Bureau terms permit use of Census data for services and retrieval; this public aggregate CBP file excludes direct identifiers. Do not combine with other data to identify a respondent or establishment.",
                        "attribution": "Source: U.S. Census Bureau, County Business Patterns, 2023. Changes: converted source CSV rows to JSONL without changing field values.",
                        "reference_year": "2023",
                        "geographic_level": "county",
                        "raw_path": archive_info["raw_path"],
                        "raw_sha256": raw_hash,
                    },
                    "observation": row,
                }
                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                record_count += 1
            output.flush()
            os.fsync(output.fileno())
        if record_count == 0:
            temp.unlink(missing_ok=True)
            raise ValueError("CBP archive produced no data records")
        os.replace(temp, JSONL_PATH)
        processed_hash = sha256_file(JSONL_PATH)
        report = {
            "generated_at": utc_now(),
            "dataset_id": DATASET_ID,
            "dataset_name": "County Business Patterns, 2023, County File",
            "source_url": SOURCE_URL,
            "release_url": RELEASE_URL,
            "terms_url": TERMS_URL,
            "license_basis": "Official U.S. Census Bureau statistical release; Census terms permit use of Census data for retrieval/services, subject to strict confidentiality and non-identification restrictions.",
            "license_caveat": "The reviewed terms do not grant permission to identify respondents; do not combine data to re-identify a business or establishment. This summary does not constitute legal advice.",
            "complete_dataset": True,
            "scope": "2023 CBP county-level file from the official release; not other CBP geographic files or years.",
            "archive_member": table,
            "archive_members": members,
            "raw_path": archive_info["raw_path"],
            "raw_size_bytes": self.archive_path.stat().st_size,
            "raw_sha256": raw_hash,
            "processed_path": JSONL_PATH.relative_to(ROOT).as_posix(),
            "processed_size_bytes": JSONL_PATH.stat().st_size,
            "processed_sha256": processed_hash,
            "records": record_count,
            "unique_records": len(row_digests),
            "duplicate_rows": duplicate_rows,
            "distinct_geographic_ids": len(distinct_geo),
            "years": sorted(years),
            "field_count": len(fieldnames or []),
            "fields": fieldnames,
            "missing_cells": missing_values,
            "collected_at": archive_info.get("collected_at") or utc_now(),
            "validation": {
                "zip_crc_passed": True,
                "csv_header_present": bool(fieldnames),
                "rows_match_header": True,
                "records_nonempty": record_count > 0,
                "fields_preserved_as_strings": True,
                "duplicate_rows_detected": duplicate_rows,
            },
        }
        self.state.update({
            **archive_info,
            **report,
            "stage": "complete",
            "updated_at": utc_now(),
        })
        write_json_atomic(STATE_PATH, self.state)
        VALIDATION_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        SUMMARY_PATH.write_text(json.dumps({
            "generated_at": report["generated_at"],
            "dataset_id": DATASET_ID,
            "unique_datasets": 1,
            "downloaded_unique_files": 1,
            "downloaded_bytes": report["raw_size_bytes"],
            "unique_records": report["unique_records"],
            "input_rows": report["records"],
            "duplicate_rows": report["duplicate_rows"],
            "distinct_geographic_ids": report["distinct_geographic_ids"],
            "years": report["years"],
            "fields": report["fields"],
            "missing_cells": report["missing_cells"],
            "normalized_text_characters": 0,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        append_jsonl(MANIFEST, {
            "dataset_id": DATASET_ID,
            "source_url": SOURCE_URL,
            "status": "processed",
            "raw_path": report["raw_path"],
            "raw_sha256": raw_hash,
            "raw_size_bytes": report["raw_size_bytes"],
            "processed_path": report["processed_path"],
            "processed_sha256": processed_hash,
            "records": record_count,
            "unique_records": report["unique_records"],
            "collected_at": report["collected_at"],
        })
        self._update_corpus_summary(report)
        self._write_failures()
        return report

    def _load_processed_metadata(self, table: str, members: list[str], raw_hash: str, status: str) -> dict:
        validation = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
        if validation.get("raw_sha256") != raw_hash or validation.get("processed_sha256") != sha256_file(JSONL_PATH):
            raise ValueError("Existing processed records do not match validated hashes")
        return {**validation, "status": status, "archive_member": table, "archive_members": members}

    def _write_failures(self) -> None:
        failures = [
            row for row in read_jsonl(MANIFEST)
            if row.get("dataset_id") == DATASET_ID and row.get("status") in {"failed", "blocked"}
        ]
        write_json_atomic(FAILURES_PATH, {
            "generated_at": utc_now(),
            "failure_count": len(failures),
            "failures": failures,
        })

    @staticmethod
    def _update_corpus_summary(report: dict) -> None:
        path = ROOT / "reports" / "final_corpus_summary.md"
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        marker = "\n## U.S. Census public datasets\n"
        section = (
            marker
            + "\n"
            + f"- Dataset: County Business Patterns 2023 county file (`{DATASET_ID}`)\n"
            + f"- Downloaded archive: {report['raw_size_bytes']} bytes; SHA-256 `{report['raw_sha256']}`\n"
            + f"- Structured unique records: {report['unique_records']}; duplicate rows: {report['duplicate_rows']}\n"
            + f"- Distinct geographic IDs: {report['distinct_geographic_ids']}; years: {', '.join(report['years'])}\n"
            + f"- Fields preserved: {report['field_count']}; missing cells: {report['missing_cells']}\n"
            + "- Document-text characters contributed: 0 (observations remain structured)\n"
            + "- Census confidentiality restriction: do not identify respondents/establishments, including by combining datasets.\n"
        )
        if marker in content:
            content = content.split(marker, 1)[0].rstrip() + "\n" + section
        else:
            content = content.rstrip() + "\n" + section
        path.write_text(content, encoding="utf-8")

    def run(self) -> dict:
        if not RIGHTS_APPROVED:
            raise RuntimeError(
                "CBP bulk ZIP acquisition is disabled: the reviewed Census terms "
                "address API use, but bulk-ZIP redistribution terms remain unverified"
            )
        if ZIP_PATH.exists():
            self.archive_path = ZIP_PATH
            archive_info = self._existing_archive()
        else:
            archive_info = None
            successful = [
                row for row in self.manifest
                if row.get("dataset_id") == DATASET_ID
                and row.get("status") in {"success", "reused_existing"}
                and row.get("raw_path")
            ]
            for row in reversed(successful):
                candidate = ROOT / row["raw_path"]
                if candidate.is_file() and sha256_file(candidate) == row.get("raw_sha256"):
                    self.archive_path = candidate
                    archive_info = self._existing_archive()
                    break
        if archive_info is None:
            archive_info = self._download()
        if archive_info.get("status") == "reused_existing":
            self.state.update(archive_info)
        try:
            report = self._extract_records(archive_info)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            failure = {
                "dataset_id": DATASET_ID,
                "source_url": SOURCE_URL,
                "status": "failed",
                "stage": "processing",
                "attempts_total": self._prior_attempts(),
                "raw_path": archive_info.get("raw_path"),
                "raw_sha256": archive_info.get("raw_sha256"),
                "error": f"{type(exc).__name__}: {exc}",
                "collected_at": utc_now(),
            }
            self.state.update({**failure, "stage": "processing_failed", "updated_at": utc_now()})
            write_json_atomic(STATE_PATH, self.state)
            append_jsonl(MANIFEST, failure)
            self._write_failures()
            raise
        if report.get("status") == "skipped_existing":
            return report
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire and validate one official U.S. Census CBP 2023 county bulk ZIP."
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        ingestor = CensusCBPIngestor()
        report = ingestor.run()
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        logger.error("CBP acquisition failed: %s", exc)
        if "ingestor" in locals():
            ingestor._write_failures()
        return 1
    print(json.dumps({
        "dataset_id": report["dataset_id"],
        "status": report.get("status", "success"),
        "raw_size_bytes": report["raw_size_bytes"],
        "raw_sha256": report["raw_sha256"],
        "unique_records": report["unique_records"],
        "duplicate_rows": report["duplicate_rows"],
        "distinct_geographic_ids": report["distinct_geographic_ids"],
        "years": report["years"],
        "missing_cells": report["missing_cells"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
