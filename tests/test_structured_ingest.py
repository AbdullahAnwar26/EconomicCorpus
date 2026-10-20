import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from scripts.datasets import structured_ingest as ingest


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.url = "https://api.worldbank.org/v2/test"
        self.content = json.dumps(payload or []).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)


def sample_payload():
    return [
        {"page": 1, "pages": 1, "per_page": 100, "total": 1, "sourceid": "2", "lastupdated": "2026-10-08"},
        [{
            "indicator": {"id": "NY.GDP.MKTP.CD", "value": "GDP (current US$)"},
            "country": {"id": "US", "value": "United States"},
            "countryiso3code": "USA",
            "date": "2021",
            "value": 23681000000000,
            "unit": "",
            "obs_status": "",
            "decimal": 0,
        }],
    ]


class StructuredIngestTests(unittest.TestCase):
    def test_world_bank_observations_are_validated_without_flattening(self):
        metadata, rows = ingest.validate_payload(
            json.dumps(sample_payload()).encode(), "application/json", 100
        )
        self.assertEqual(metadata["sourceid"], "2")
        self.assertEqual(rows[0]["value"], 23681000000000)
        self.assertEqual(rows[0]["indicator"]["id"], "NY.GDP.MKTP.CD")
        self.assertEqual(rows[0]["date"], "2021")
        self.assertIn("unit", rows[0])

    def test_malformed_and_missing_field_observations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            ingest.validate_payload(b"{}", "application/json", 100)
        payload = sample_payload()
        del payload[1][0]["unit"]
        with self.assertRaisesRegex(ValueError, "missing required"):
            ingest.validate_payload(json.dumps(payload).encode(), "application/json", 100)

    def test_retry_after_is_respected_and_attempts_are_capped(self):
        session = FakeSession([
            FakeResponse(429, headers={"Content-Type": "application/json", "Retry-After": "5"}),
            FakeResponse(200, sample_payload()),
        ])
        delays = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ingest, "MANIFEST", Path(directory) / "manifest.jsonl"), \
             patch.object(ingest, "RAW_ROOT", Path(directory) / "raw"), \
             patch.object(ingest, "STRUCTURED_RAW", Path(directory) / "raw" / "structured"), \
             patch.object(ingest, "STRUCTURED_PROCESSED", Path(directory) / "processed"), \
             patch.object(ingest, "STATUS", Path(directory) / "status.json"), \
             patch.object(ingest, "VALIDATION_REPORT", Path(directory) / "validation.json"):
            runner = ingest.WorldBankIngestor(session=session, sleep=delays.append)
            response, body, attempts, error = runner._get(
                "https://api.worldbank.org/v2/example", attempts_used=0
            )
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [5.0])
        self.assertIsNone(error)
        self.assertIsNotNone(body)
        response.close()

    def test_transient_failures_use_exponential_backoff_and_stop_at_three_attempts(self):
        session = FakeSession([
            FakeResponse(503),
            FakeResponse(503),
            FakeResponse(503),
        ])
        delays = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ingest, "MANIFEST", Path(directory) / "manifest.jsonl"), \
             patch.object(ingest, "RAW_ROOT", Path(directory) / "raw"):
            runner = ingest.WorldBankIngestor(session=session, sleep=delays.append)
            response, body, attempts, error = runner._get(
                "https://api.worldbank.org/v2/example", attempts_used=0
            )
        self.assertEqual(attempts, 3)
        self.assertEqual(session.calls, 3)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(error, "HTTP 503")
        self.assertIsNone(body)
        response.close()

    def test_access_denial_is_not_retried(self):
        response = FakeResponse(403)
        session = FakeSession([response])
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ingest, "RAW_ROOT", Path(directory) / "raw"
        ):
            runner = ingest.WorldBankIngestor(
                session=session, sleep=lambda _: self.fail("must not retry")
            )
            result, body, attempts, error = runner._get(
                "https://api.worldbank.org/v2/example", attempts_used=0
            )
        self.assertEqual(result.status_code, 403)
        self.assertEqual(attempts, 1)
        self.assertEqual(session.calls, 1)
        self.assertIsNone(body)
        self.assertEqual(error, "HTTP 403; not retried")

    def test_acquire_writes_structured_record_and_resume_skips_network(self):
        session = FakeSession([FakeResponse(200, sample_payload())])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ingest, "ROOT", Path(directory)), \
             patch.object(ingest, "RAW_ROOT", Path(directory) / "data" / "raw"), \
             patch.object(ingest, "STRUCTURED_RAW", Path(directory) / "data" / "raw" / "structured" / "wdi"), \
             patch.object(ingest, "STRUCTURED_PROCESSED", Path(directory) / "data" / "processed" / "wdi"), \
             patch.object(ingest, "MANIFEST", Path(directory) / "reports" / "manifest.jsonl"), \
             patch.object(ingest, "STATUS", Path(directory) / "reports" / "status.json"), \
             patch.object(ingest, "VALIDATION_REPORT", Path(directory) / "reports" / "validation.json"), \
             patch.object(ingest, "DATASET_SUMMARY", Path(directory) / "reports" / "dataset-summary.json"), \
             patch.object(ingest, "FAILURES_REPORT", Path(directory) / "reports" / "failures.json"), \
             patch.object(ingest, "RIGHTS_REVIEW_REPORT", Path(directory) / "reports" / "rights.csv"), \
             patch.object(ingest, "CANDIDATES_REPORT", Path(directory) / "reports" / "candidates.json"):
            runner = ingest.WorldBankIngestor(session=session, sleep=lambda _: None)
            result = runner.acquire("USA", "NY.GDP.MKTP.CD", 2021)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["records"], 1)
            stored = json.loads((Path(directory) / result["processed_path"]).read_text(encoding="utf-8"))
            self.assertEqual(stored["record_type"], "economic_observation")
            self.assertEqual(stored["observation"]["value"], 23681000000000)
            self.assertEqual(stored["observation"]["unit"], "")
            second = ingest.WorldBankIngestor(session=FakeSession([]), sleep=lambda _: None)
            resumed = second.acquire("USA", "NY.GDP.MKTP.CD", 2021)
            self.assertEqual(resumed["status"], "skipped_existing")
            self.assertEqual(session.calls, 1)

    def test_identical_existing_raw_file_is_reused_by_hash(self):
        payload = sample_payload()
        body = json.dumps(payload).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "data" / "raw" / "previous" / "same-response.json"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(body)
            session = FakeSession([FakeResponse(200, payload)])
            with patch.object(ingest, "ROOT", root), \
                 patch.object(ingest, "RAW_ROOT", root / "data" / "raw"), \
                 patch.object(ingest, "STRUCTURED_RAW", root / "data" / "raw" / "structured" / "wdi"), \
                 patch.object(ingest, "STRUCTURED_PROCESSED", root / "data" / "processed" / "wdi"), \
                 patch.object(ingest, "MANIFEST", root / "reports" / "manifest.jsonl"), \
                 patch.object(ingest, "STATUS", root / "reports" / "status.json"), \
                 patch.object(ingest, "VALIDATION_REPORT", root / "reports" / "validation.json"), \
                 patch.object(ingest, "DATASET_SUMMARY", root / "reports" / "dataset-summary.json"), \
                 patch.object(ingest, "FAILURES_REPORT", root / "reports" / "failures.json"), \
                 patch.object(ingest, "RIGHTS_REVIEW_REPORT", root / "reports" / "rights.csv"), \
                 patch.object(ingest, "CANDIDATES_REPORT", root / "reports" / "candidates.json"):
                result = ingest.WorldBankIngestor(session=session, sleep=lambda _: None).acquire(
                    "USA", "NY.GDP.MKTP.CD", 2021
                )
            self.assertEqual(result["status"], "duplicate")
            self.assertEqual(result["duplicate_of"], "data/raw/previous/same-response.json")
            self.assertEqual(len(list((root / "data" / "raw").rglob("*.json"))), 1)
            self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
