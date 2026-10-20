import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from scripts.scrapers import acquisition_pipeline as acquisition


class FakeResponse:
    def __init__(self, status_code, body=b"", headers=None, url="https://example.org/item"):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

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


class AcquisitionPipelineTests(unittest.TestCase):
    def test_sha256_matches_known_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content.txt"
            path.write_bytes(b"economic corpus")
            self.assertEqual(
                acquisition.sha256_file(path),
                "539bfe1e625e5761d2fd11d2a57e6553a7f3c1e8c9357d231d65cec38598758e",
            )

    def test_html_body_disguised_as_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "error.pdf"
            path.write_bytes(b"%PDF-" + b"<html>" + b"error page " * 100)
            with self.assertRaisesRegex(ValueError, "Invalid PDF"):
                acquisition.validate_content(path, "application/pdf", "pdf")

    def test_duplicate_hash_keeps_one_physical_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data" / "raw"
            existing = raw / "older_source" / "content.html"
            existing.parent.mkdir(parents=True)
            body = b"<html><title>Book</title><body>" + b"economics text " * 100 + b"</body></html>"
            existing.write_bytes(body)
            response = FakeResponse(200, body, {"Content-Type": "text/html"}, "https://example.org/book")
            with patch.object(acquisition, "ROOT", root), patch.object(acquisition, "RAW_ROOT", raw), patch.object(
                acquisition, "MANIFEST", root / "reports" / "acquisition_manifest.jsonl"
            ):
                runner = acquisition.AcquisitionRunner(session=FakeSession([]), sleep=lambda _: None)
                row = runner._record(
                    {"source_id": "economic_source_test"},
                    "https://example.org/book",
                    "success",
                    response=response,
                    body=body,
                    attempts=1,
                    forced_kind="html",
                )
            self.assertEqual(row["status"], "duplicate")
            self.assertEqual(row["local_path"], str(existing.relative_to(root)))
            self.assertEqual(
                list((raw / "economic_source_test").glob("*")),
                [],
            )

    def test_retries_transient_status_with_exponential_backoff(self):
        session = FakeSession([
            FakeResponse(503, headers={}),
            FakeResponse(503, headers={}),
            FakeResponse(200, b"done", {"Content-Type": "text/plain"}),
        ])
        delays = []
        with tempfile.TemporaryDirectory() as directory, patch.object(
            acquisition, "ROOT", Path(directory)
        ), patch.object(acquisition, "RAW_ROOT", Path(directory) / "data" / "raw"):
            runner = acquisition.AcquisitionRunner(session=session, sleep=delays.append)
            response, body, attempts, error = runner._get("https://example.org/item", "source")
        self.assertIsNone(error)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"done")
        self.assertEqual(attempts, 3)
        self.assertEqual(session.calls, 3)
        self.assertEqual(delays, [1, 2])

    def test_retry_after_header_is_respected(self):
        session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, b"done", {"Content-Type": "text/plain"}),
        ])
        delays = []
        with tempfile.TemporaryDirectory() as directory, patch.object(
            acquisition, "ROOT", Path(directory)
        ), patch.object(acquisition, "RAW_ROOT", Path(directory) / "data" / "raw"):
            runner = acquisition.AcquisitionRunner(session=session, sleep=delays.append)
            _, body, attempts, error = runner._get("https://example.org/item", "source")
        self.assertIsNone(error)
        self.assertEqual(body, b"done")
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [7])


if __name__ == "__main__":
    unittest.main()
