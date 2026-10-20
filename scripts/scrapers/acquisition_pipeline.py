from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import pymupdf
import requests
from bs4 import BeautifulSoup
from requests import Response


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "reports" / "source_inventory.json"
VERIFICATION = ROOT / "reports" / "source_verification.json"
MANIFEST = ROOT / "reports" / "acquisition_manifest.jsonl"
STATE_PATH = ROOT / "reports" / "acquisition_state.json"
RAW_ROOT = ROOT / "data" / "raw"
MAX_ATTEMPTS = 3
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 40
MAX_RESPONSE_BYTES = 200 * 1024 * 1024
MAX_PAGES_PER_SOURCE = 500

logger = logging.getLogger("economic_acquisition")


@dataclass(frozen=True)
class Policy:
    category: str
    action: str
    reason: str


POLICIES = {
    "economic_source_001": Policy(
        "unavailable", "inspect_pdf_links",
        "The old Stanford URL returned 404; inspect only the current URL recorded in the manager verification report for an explicitly exposed full-text file.",
    ),
    "economic_source_002": Policy(
        "readable_online_redistribution_unclear", "inspect_only",
        "Archive.org record is readable, but the manager inventory does not establish redistribution permission.",
    ),
    "economic_source_003": Policy(
        "readable_online_redistribution_unclear", "gutenberg_au_inspect",
        "Gutenberg Australia provides online text, but the inventory does not establish redistribution status for this work.",
    ),
    "economic_source_004": Policy(
        "free_full_text", "gutenberg",
        "Project Gutenberg full-book HTML is exposed for the listed work.",
    ),
    "economic_source_005": Policy(
        "free_full_text", "gutenberg",
        "Project Gutenberg full-book HTML is exposed for the listed work.",
    ),
    "economic_source_006": Policy(
        "unavailable", "blocked",
        "The listed Mises URL returned HTTP 403; do not retry or bypass the access restriction.",
    ),
    "economic_source_007": Policy(
        "unavailable", "blocked",
        "The listed Liberty Fund URL returned HTTP 403; do not retry or bypass the access restriction.",
    ),
    "economic_source_008": Policy(
        "free_full_text", "openstax",
        "The listed OpenStax Principles of Economics 3e pages provide textbook content; restrict discovery to that book.",
    ),
    "economic_source_009": Policy(
        "excluded", "exclude",
        "The manager identifies Princeton Muqaddimah as paid/sample-only.",
    ),
    "economic_source_010": Policy(
        "free_full_text", "mcgill_pdfs",
        "Inspect only textbook PDF links exposed by the manager-listed McGill textbook page.",
    ),
    "economic_source_011": Policy(
        "readable_online_redistribution_unclear", "inspect_only",
        "The MIT-hosted PDF is technically accessible, but the manager inventory does not establish redistribution permission.",
    ),
    "economic_source_012": Policy(
        "readable_online_redistribution_unclear", "inspect_only",
        "Econlib content is readable online; do not repackage the complete work absent clear redistribution permission.",
    ),
    "economic_source_013": Policy(
        "readable_online_redistribution_unclear", "inspect_only",
        "Econlib content is readable online; do not repackage the complete work absent clear redistribution permission.",
    ),
    "economic_source_014": Policy(
        "free_full_text", "gutenberg",
        "Project Gutenberg full-book HTML is exposed for the listed work.",
    ),
    "economic_source_015": Policy(
        "free_full_text", "gutenberg",
        "Project Gutenberg full-book HTML is exposed for the listed work.",
    ),
    "economic_source_016": Policy(
        "free_full_text", "gutenberg",
        "Project Gutenberg full-book HTML is exposed for the listed work.",
    ),
    "economic_source_017": Policy(
        "partial_sample", "existing_sample_only",
        "The manager identifies this as sample chapters, not the complete book; retain verified existing sample material only.",
    ),
    "economic_source_018": Policy(
        "readable_online_redistribution_unclear", "dartmouth_inspect",
        "Inspect the Dartmouth digitization landing page for exposed book resources; do not mistake its catalog page for the book.",
    ),
    "economic_source_019": Policy(
        "free_full_text", "marx_html",
        "Acquire only Capital Volume I text resources exposed under the manager-listed Marxists Internet Archive path.",
    ),
    "economic_source_020": Policy(
        "excluded", "exclude",
        "The manager identifies the McGraw-Hill publisher page as sample/paid material.",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_manifest(item: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        stream.flush()


def read_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    rows = []
    with MANIFEST.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed manifest line %d", line_number)
    return rows


def normalized_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "http" and parsed.netloc.lower() == "openstax.org" else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme, fragment=""))


def wait_for_retry(response: Response | None, attempt: int, sleep=time.sleep) -> None:
    delay = min(2 ** (attempt - 1), 20)
    if response is not None and response.headers.get("Retry-After"):
        retry_after = response.headers["Retry-After"]
        try:
            delay = max(0, int(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                delay = max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    sleep(delay)


def text_metadata(content: bytes) -> tuple[str, int]:
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    text = " ".join(soup.stripped_strings)
    return unescape(title), len(text)


def validate_content(path: Path, content_type: str, expected_kind: str | None = None) -> dict:
    size = path.stat().st_size
    if size < 256:
        raise ValueError(f"Content is too small to be corpus material ({size} bytes)")

    with path.open("rb") as stream:
        prefix = stream.read(16)

    kind = expected_kind
    if prefix.startswith(b"%PDF-"):
        kind = "pdf"
    elif prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        kind = "zip"
    elif kind is None and (
        "html" in content_type.lower()
        or prefix.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml"))
    ):
        kind = "html"

    if kind == "pdf":
        if not prefix.startswith(b"%PDF-"):
            raise ValueError("Response is not a PDF (missing PDF signature)")
        try:
            document = pymupdf.open(path)
            page_count = len(document)
            if page_count < 1:
                raise ValueError("PDF contains no pages")
            first_page = document[0].get_text().strip()
            metadata = document.metadata or {}
            if page_count < 2 and len(first_page) < 100:
                raise ValueError("PDF is too small or empty to be a full-text document")
            return {
                "kind": "pdf",
                "page_count": page_count,
                "title": (metadata.get("title") or "").strip() or None,
                "text_chars": len(first_page),
            }
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f"Invalid PDF: {exc}") from exc

    if kind == "html":
        if not prefix.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body")):
            raise ValueError("Response is not recognizable HTML")
        title, text_chars = text_metadata(path.read_bytes())
        if text_chars < 500:
            raise ValueError(f"HTML has too little readable text ({text_chars} characters)")
        title_lower = title.lower()
        if any(token in title_lower for token in ("access denied", "403 forbidden", "not found")):
            raise ValueError(f"HTML appears to be an error page: {title}")
        return {"kind": "html", "page_count": None, "title": title or None, "text_chars": text_chars}

    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
                names = archive.namelist()
                if bad_member:
                    raise ValueError(f"Corrupt ZIP member: {bad_member}")
                if path.suffix.lower() == ".epub" and "mimetype" not in names:
                    raise ValueError("EPUB archive lacks mimetype entry")
                if not names:
                    raise ValueError("Empty ZIP archive")
            return {"kind": "zip", "page_count": None, "title": None, "text_chars": None}
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid ZIP archive: {exc}") from exc

    raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")


def content_extension(kind: str, url: str) -> str:
    if kind == "pdf":
        return ".pdf"
    if kind == "zip":
        return ".epub" if "epub" in url.lower() else ".zip"
    return ".html"


class AcquisitionRunner:
    def __init__(
        self,
        session: requests.Session | None = None,
        sleep=time.sleep,
        max_pages: int = MAX_PAGES_PER_SOURCE,
        source_limit: int | None = None,
        source_ids: set[str] | None = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "EconomicCorpusResearchBot/1.0 (research data collection; contact: corpus-admin)",
            "Accept": "text/html,application/pdf,application/epub+zip,application/octet-stream;q=0.8",
        })
        self.sleep = sleep
        self.max_pages = max_pages
        self.source_limit = source_limit
        self.source_ids = source_ids
        self.rows = read_manifest()
        self.crawl_state = (
            load_json(STATE_PATH) if STATE_PATH.exists() else {}
        )
        verification = load_json(VERIFICATION)
        self.verified_final_urls = {
            item["source_id"]: item.get("final_url")
            for item in verification.get("sources", [])
            if item.get("final_url")
        }
        self.existing_successes = {
            normalized_url(row.get("source_url", ""))
            for row in self.rows
            if row.get("status") in {"success", "duplicate"}
            and row.get("source_url")
            and row.get("local_path")
            and Path(row["local_path"]).exists()
        }
        self.prior_attempts: dict[str, int] = {}
        self.permanent_failures: set[str] = set()
        for row in self.rows:
            if row.get("status") in {"failed", "blocked"}:
                for value in (row.get("source_url"), row.get("final_url")):
                    if not value:
                        continue
                    key = normalized_url(value)
                    self.prior_attempts[key] = max(
                        self.prior_attempts.get(key, 0), int(row.get("attempts", 0))
                    )
                    if row.get("error", "").startswith(("HTTP 404", "HTTP 400", "HTTP 401", "HTTP 403")):
                        self.permanent_failures.add(key)
        self.hash_index = self._index_existing_hashes()

    def _index_existing_hashes(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        roots = []
        if RAW_ROOT.exists():
            roots.extend(
                path for path in RAW_ROOT.iterdir()
                if path.is_dir() and path.name != "crawled"
            )
            if (RAW_ROOT / "crawled").exists():
                logger.info("Skipping unverified legacy crawl directory in corpus hash index")
        downloads = ROOT / "data" / "downloads"
        if downloads.exists():
            roots.append(downloads)
        for root in roots:
            for path in root.rglob("*"):
                if path.is_file() and not path.name.endswith(".part"):
                    try:
                        index.setdefault(sha256_file(path), path)
                    except OSError as exc:
                        logger.warning("Could not hash existing file %s: %s", path, exc)
        return index

    def _get(self, url: str, source_id: str) -> tuple[Response | None, bytes | None, int, str | None]:
        key = normalized_url(url)
        prior = self.prior_attempts.get(key, 0)
        if key in self.permanent_failures:
            return None, None, prior, "Permanent HTTP failure already recorded; no repeat request made"
        if prior >= MAX_ATTEMPTS:
            return None, None, 0, f"Retry limit already reached ({MAX_ATTEMPTS} attempts)"
        if any(
            row.get("status") == "blocked"
            and normalized_url(row.get("source_url", "")) == key
            for row in self.rows
        ):
            return None, None, prior, "Previously blocked URL; no repeat request made"
        attempt_limit = MAX_ATTEMPTS - prior
        attempts = 0
        last_error = None
        for attempt in range(1, attempt_limit + 1):
            attempts += 1
            response = None
            try:
                response = self.session.get(
                    url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True, stream=True
                )
                if response.status_code == 403:
                    return response, None, prior + attempts, "HTTP 403; access restriction respected"
                if response.status_code == 404:
                    return response, None, prior + attempts, "HTTP 404; resource unavailable"
                if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                    return response, None, prior + attempts, f"HTTP {response.status_code}; request not retried"
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < attempt_limit:
                        wait_for_retry(response, attempt, self.sleep)
                        continue
                    return response, None, prior + attempts, last_error
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError(f"Response exceeds configured limit ({MAX_RESPONSE_BYTES} bytes)")
                    chunks.append(chunk)
                return response, b"".join(chunks), prior + attempts, None
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                if attempt < attempt_limit:
                    wait_for_retry(response, attempt, self.sleep)
            finally:
                if response is not None:
                    response.close()
        return None, None, prior + attempts, last_error or "Request failed"

    def _record(
        self, source: dict, url: str, status: str, *, response: Response | None = None,
        body: bytes | None = None, attempts: int = 0, error: str | None = None,
        notes: str | None = None, forced_kind: str | None = None,
    ) -> dict:
        source_id = source["source_id"]
        item = {
            "source_id": source_id,
            "source_url": url,
            "final_url": response.url if response is not None else None,
            "local_path": None,
            "content_type": response.headers.get("Content-Type", "") if response else "",
            "size_bytes": len(body) if body is not None else None,
            "sha256": None,
            "status": status,
            "attempts": attempts,
            "page_count": None,
            "title": None,
            "collected_at": utc_now(),
            "error": error,
            "notes": notes,
        }
        if body is not None:
            source_dir = RAW_ROOT / source_id
            source_dir.mkdir(parents=True, exist_ok=True)
            content_type = item["content_type"]
            kind = forced_kind or (
                "pdf" if "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")
                else "zip" if "epub" in content_type.lower() or url.lower().split("?")[0].endswith((".epub", ".zip"))
                else "html"
            )
            temp_path = source_dir / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".part")
            try:
                with temp_path.open("wb") as stream:
                    stream.write(body)
                    stream.flush()
                details = validate_content(temp_path, content_type, kind)
                checksum = sha256_file(temp_path)
                item["sha256"] = checksum
                item["page_count"] = details["page_count"]
                item["title"] = details["title"]
                item["notes"] = (notes + "; " if notes else "") + f"validated {details['kind']}; text_chars={details['text_chars']}"
                duplicate_path = self.hash_index.get(checksum)
                if duplicate_path is not None:
                    item["status"] = "duplicate"
                    item["local_path"] = str(duplicate_path.relative_to(ROOT))
                    item["duplicate_of"] = item["local_path"]
                    item["notes"] = (item["notes"] + "; " if item["notes"] else "") + "identical SHA-256 already stored"
                    temp_path.unlink(missing_ok=True)
                else:
                    output_path = source_dir / (
                        re.sub(r"[^A-Za-z0-9._-]+", "_", Path(urlparse(url).path).name)[:100]
                        or f"{checksum[:16]}{content_extension(details['kind'], url)}"
                    )
                    if output_path.exists():
                        output_path = source_dir / f"{output_path.stem}_{checksum[:8]}{output_path.suffix}"
                    temp_path.replace(output_path)
                    item["status"] = "success"
                    item["local_path"] = str(output_path.relative_to(ROOT))
                    self.hash_index[checksum] = output_path
                self.existing_successes.add(normalized_url(url))
            except (OSError, ValueError, RuntimeError) as exc:
                temp_path.unlink(missing_ok=True)
                item["status"] = "failed"
                item["error"] = f"Content validation/save failed: {exc}"
        append_manifest(item)
        self.rows.append(item)
        log_method = logger.info if item["status"] in {"success", "duplicate"} else logger.warning
        log_method("%s %s %s", item["status"].upper(), source_id, url)
        return item

    def _already_recorded(self, url: str) -> bool:
        for row in reversed(self.rows):
            if not self._row_matches_url(row, url):
                continue
            if row.get("status") in {"success", "duplicate"} and row.get("local_path"):
                path = ROOT / row["local_path"]
                if path.exists() and sha256_file(path) == row.get("sha256"):
                    logger.info("SKIPPED verified existing acquisition: %s", url)
                    return True
        return False

    @staticmethod
    def _row_matches_url(row: dict, url: str) -> bool:
        key = normalized_url(url)
        return key in {
            normalized_url(row.get("source_url", "")),
            normalized_url(row.get("final_url", "")),
        }

    def _cached_content(self, url: str) -> tuple[bytes, str] | None:
        for row in reversed(self.rows):
            if not self._row_matches_url(row, url):
                continue
            if row.get("status") not in {"success", "duplicate"} or not row.get("local_path"):
                continue
            path = ROOT / row["local_path"]
            try:
                if path.is_file() and sha256_file(path) == row.get("sha256"):
                    return path.read_bytes(), row.get("final_url") or url
            except OSError as exc:
                logger.warning("Could not read cached page %s: %s", path, exc)
        return None

    def acquire_url(self, source: dict, url: str, *, forced_kind: str | None = None) -> dict | None:
        if self._already_recorded(url):
            return None
        basename = Path(urlparse(url).path).name
        existing = RAW_ROOT / source["source_id"] / basename
        if basename and existing.is_file():
            expected_kind = forced_kind or (
                "pdf" if existing.suffix.lower() == ".pdf"
                else "zip" if existing.suffix.lower() in {".epub", ".zip"}
                else "html"
            )
            try:
                details = validate_content(existing, "application/pdf" if expected_kind == "pdf" else "text/html", expected_kind)
                self._register_existing(source, url, existing, details, "Existing verified resource reused; no download")
                return self.rows[-1]
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Existing resource failed validation %s: %s", existing, exc)
        response, body, attempts, error = self._get(url, source["source_id"])
        if error:
            if response is not None and response.status_code in {401, 403}:
                return self._record(source, url, "blocked", response=response, attempts=attempts, error=error)
            if response is None and "Previously blocked URL" in (error or ""):
                return self._record(source, url, "blocked", attempts=attempts, error=error)
            status = "failed"
            return self._record(source, url, status, response=response, attempts=attempts, error=error)
        return self._record(source, url, "success", response=response, body=body, attempts=attempts, forced_kind=forced_kind)

    def inspect_url(self, source: dict, url: str) -> tuple[Response | None, bytes | None, int, str | None]:
        return self._get(url, source["source_id"])

    def _record_inspection(
        self, source: dict, url: str, response: Response, body: bytes,
        attempts: int, notes: str,
    ) -> dict:
        content_type = response.headers.get("Content-Type", "")
        title = None
        page_count = None
        if "html" in content_type.lower() or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            title, text_chars = text_metadata(body)
            notes = f"{notes}; readable_text_chars={text_chars}"
        elif body.startswith(b"%PDF-"):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
                temporary.write(body)
                temporary_path = Path(temporary.name)
            try:
                details = validate_content(temporary_path, content_type or "application/pdf", "pdf")
                title = details["title"]
                page_count = details["page_count"]
            finally:
                temporary_path.unlink(missing_ok=True)
        row = {
            "source_id": source["source_id"], "source_url": url, "final_url": response.url,
            "local_path": None, "content_type": content_type, "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "status": "inspected",
            "attempts": attempts, "page_count": page_count, "title": title,
            "collected_at": utc_now(), "error": None, "notes": notes,
        }
        append_manifest(row)
        self.rows.append(row)
        return row

    def _save_crawl_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".part")
        temporary.write_text(
            json.dumps(self.crawl_state, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(STATE_PATH)

    def _existing_page(self, source: dict, url: str) -> tuple[bytes, str, Path] | None:
        source_dir = RAW_ROOT / source["source_id"]
        if not source_dir.exists():
            return None
        basename = Path(urlparse(url).path).name
        candidates = []
        if basename:
            candidates.append(source_dir / basename)
        landing_urls = {
            normalized_url(source["url"]),
            normalized_url(self.verified_final_urls.get(source["source_id"], source["url"])),
        }
        if normalized_url(url) in landing_urls:
            candidates.extend(
                source_dir / f"{source['source_id']}{suffix}" for suffix in (".html", ".htm")
            )
        for path in candidates:
            if not path.is_file():
                continue
            try:
                validate_content(path, "text/html", "html")
                final_url = (
                    self.verified_final_urls.get(source["source_id"], url)
                    if normalized_url(url) in landing_urls else url
                )
                return path.read_bytes(), final_url, path
            except (OSError, ValueError, RuntimeError):
                continue
        return None

    @staticmethod
    def _same_host(url: str, start_url: str) -> bool:
        return urlparse(url).netloc.lower() == urlparse(start_url).netloc.lower()

    @staticmethod
    def _page_links(content: bytes, page_url: str, start_url: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(content, "html.parser")
        links = []
        start = urlparse(start_url)
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, anchor["href"]).split("#", 1)[0]
            parsed = urlparse(href)
            path = parsed.path.lower()
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != start.netloc.lower():
                continue
            if path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".mp3", ".mp4")):
                continue
            if any(part in path for part in ("/login", "/account", "/search", "/donate", "/privacy", "/terms")):
                continue
            links.append((href, anchor.get_text(" ", strip=True)))
        return links

    def _inspect_page(self, source: dict, url: str) -> tuple[bytes | None, str | None, list[tuple[str, str]]]:
        cached = self._cached_content(url)
        local = None if cached else self._existing_page(source, url)
        if cached:
            body, final_url = cached
            attempts = 0
            note = "Previously verified acquisition reused for discovery"
        elif local:
            body, final_url, local_path = local
            attempts = 0
            note = f"Existing local page reused for discovery: {local_path.relative_to(ROOT)}"
        else:
            response, body, attempts, error = self.inspect_url(source, url)
            if error:
                row_status = "blocked" if response is not None and response.status_code in {401, 403} else "failed"
                self._record(source, url, row_status, response=response, attempts=attempts, error=error)
                return None, None, []
            assert response is not None and body is not None
            final_url = response.url
            note = "Discovery page fetched; response not saved as corpus text"
        response = requests.Response()
        response.status_code = 200
        response.url = final_url
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        self._record_inspection(source, url, response, body, attempts, note)
        logger.info("INSPECTED %s %s (%d bytes)", source["source_id"], final_url, len(body))
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and not body.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head")):
            return body, final_url, []
        return body, final_url, self._page_links(body, final_url, source["url"])

    def _save_html_page(self, source: dict, url: str, body: bytes, final_url: str) -> dict:
        response = requests.Response()
        response.status_code = 200
        response.url = final_url
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = body
        return self._record(source, url, "success", response=response, body=body, attempts=1, forced_kind="html")

    def _run_gutenberg(self, source: dict) -> None:
        book_id = re.search(r"/ebooks/(\d+)", source["url"])
        if not book_id:
            self._record(source, source["url"], "failed", error="Could not identify Project Gutenberg book ID")
            return
        number = book_id.group(1)
        url = f"https://www.gutenberg.org/cache/epub/{number}/pg{number}-images.html"
        if self._already_recorded(url):
            return
        existing = RAW_ROOT / source["source_id"] / f"pg{number}-images.html"
        if existing.exists():
            try:
                details = validate_content(existing, "text/html", "html")
                self._register_existing(source, url, existing, details, "Existing full-book HTML verified; no download")
                return
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Existing file is not a verified full text %s: %s", existing, exc)
        self.acquire_url(source, url, forced_kind="html")

    def _register_existing(self, source: dict, url: str, path: Path, details: dict, notes: str) -> None:
        checksum = sha256_file(path)
        canonical = self.hash_index.get(checksum)
        duplicate_path = canonical if canonical is not None and canonical.resolve() != path.resolve() else None
        item = {
            "source_id": source["source_id"], "source_url": url, "final_url": url,
            "local_path": str((duplicate_path or path).relative_to(ROOT)), "content_type": "text/html" if details["kind"] == "html" else "application/pdf",
            "size_bytes": path.stat().st_size, "sha256": checksum, "status": "duplicate" if duplicate_path else "success",
            "attempts": 0, "page_count": details.get("page_count"), "title": details.get("title"),
            "collected_at": utc_now(), "error": None,
            "notes": notes + ("; identical SHA-256 already stored" if duplicate_path else ""),
        }
        if duplicate_path:
            item["duplicate_of"] = str(duplicate_path.relative_to(ROOT))
        append_manifest(item)
        self.rows.append(item)
        self.hash_index.setdefault(checksum, path)
        self.existing_successes.add(normalized_url(url))
        logger.info("SUCCESS reused verified file %s", path)

    def _run_openstax(self, source: dict) -> None:
        start_url = source["url"]
        source_id = source["source_id"]
        state = self.crawl_state.get(source_id, {})
        if state.get("start_url") == start_url:
            queue = list(state.get("queue", []))
            seen: set[str] = set(state.get("seen", []))
        else:
            queue = [start_url]
            seen = set()
        queue = [self._canonical_openstax_url(url) for url in queue]
        saved = 0
        processed_this_run = 0
        while queue and processed_this_run < self.max_pages:
            url = queue[0]
            key = normalized_url(url)
            if key in seen:
                queue.pop(0)
                continue
            cached = self._cached_content(url)
            was_cached = cached is not None
            if cached:
                body, final_url = cached
                links = self._page_links(body, final_url, source["url"])
            else:
                body, final_url, links = self._inspect_page(source, url)
            if body is None or final_url is None:
                continue
            parsed = urlparse(final_url)
            if parsed.netloc.lower() != urlparse(start_url).netloc.lower():
                continue
            path = parsed.path.lower()
            if "/books/principles-economics-3e/pages/" not in path:
                continue
            if re.search(r"/pages/\d+-[^/]+/?$", path) and self._is_book_text(body):
                if not was_cached:
                    self._save_html_page(source, url, body, final_url)
                else:
                    logger.info("SKIPPED verified existing OpenStax page %s", url)
                saved += 1
            for next_url, _ in links:
                next_url = self._canonical_openstax_url(next_url)
                next_path = urlparse(next_url).path.lower()
                if "/books/principles-economics-3e/pages/" in next_path and normalized_url(next_url) not in seen and next_url not in queue:
                    queue.append(next_url)
            queue.pop(0)
            seen.add(key)
            processed_this_run += 1
            self.crawl_state[source_id] = {
                "start_url": start_url,
                "queue": queue,
                "seen": sorted(seen),
                "updated_at": utc_now(),
            }
            self._save_crawl_state()
        if queue:
            self.crawl_state[source_id] = {
                "start_url": start_url,
                "queue": queue,
                "seen": sorted(seen),
                "updated_at": utc_now(),
            }
            self._save_crawl_state()
            self._record(
                source,
                start_url,
                "partial_source",
                notes=f"Per-run page limit reached after {processed_this_run} pages; {len(queue)} in-scope links remain",
            )
        else:
            self.crawl_state.pop(source_id, None)
            self._save_crawl_state()
            self._record(
                source,
                start_url,
                "crawl_complete",
                notes=f"Bounded OpenStax book crawl completed; {len(seen)} unique pages inspected",
            )
        if not saved and not any(
            row.get("status") in {"success", "duplicate"}
            and row.get("source_id") == source_id
            and "/books/principles-economics-3e/pages/" in row.get("source_url", "")
            for row in self.rows
        ):
            self._record(
                source,
                start_url,
                "skipped",
                notes="No textbook chapter pages passed content validation during bounded discovery",
            )

    @staticmethod
    def _canonical_openstax_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc.lower() == "openstax.org" and parsed.scheme == "http":
            return parsed._replace(scheme="https").geturl()
        return url

    @staticmethod
    def _is_book_text(body: bytes) -> bool:
        soup = BeautifulSoup(body, "html.parser")
        main = soup.find("main") or soup.find("article") or soup.body or soup
        text = " ".join(main.stripped_strings)
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        return len(text) >= 1500 and "principles of economics 3e" in title

    def _run_pdf_links(self, source: dict, *, require_textbooks: bool = False) -> None:
        body, final_url, links = self._inspect_page(source, source["url"])
        if body is None or final_url is None:
            return
        selected = []
        for url, label in links:
            if not urlparse(url).path.lower().endswith(".pdf"):
                continue
            if not self._same_host(url, source["url"]):
                continue
            terms = ("davidson", "mckinnon", "econometric", "macroeconometric") if require_textbooks else (
                ("elements", "statistical", "learning", "tibshirani", "esl")
                if source["source_id"] == "economic_source_001" else ()
            )
            if terms and not any(term in f"{url} {label}".lower() for term in terms):
                continue
            selected.append(url)
        if not selected:
            logger.info("SKIPPED no legitimate same-domain PDF exposed by %s", source["source_id"])
            self._record(
                source,
                source["url"],
                "skipped",
                notes="No permitted same-domain full-text PDF link was exposed on the source page",
            )
            return
        for url in dict.fromkeys(selected):
            self.acquire_url(source, url, forced_kind="pdf")

    def _run_gutenberg_au_inspect(self, source: dict) -> None:
        body, final_url, links = self._inspect_page(source, source["url"])
        if body is None or final_url is None:
            return
        index_links = [
            url for url, _ in links
            if urlparse(url).path.lower().endswith("/0-index.html")
            and self._same_host(url, source["url"])
        ]
        index_url = index_links[0] if index_links else None
        if index_url is None:
            self._record(
                source, source["url"], "skipped",
                notes="No same-domain full-text index link was exposed by the Gutenberg Australia entry page",
            )
            return
        index_body, index_final, chapter_links = self._inspect_page(source, index_url)
        if index_body is None or index_final is None:
            return
        chapters = [
            url for url, label in chapter_links
            if re.search(r"/chap\d+[a-z]?\.html$", urlparse(url).path.lower())
            and self._same_host(url, source["url"])
            and ("chapter" in label.lower() or label.lower().startswith("book "))
        ]
        for chapter_url in list(dict.fromkeys(chapters))[:2]:
            self._inspect_page(source, chapter_url)

    def _run_dartmouth_inspect(self, source: dict) -> None:
        local = self._existing_page(source, source["url"])
        if local:
            body, final_url, local_path = local
            attempts = 0
            note = f"Existing local page reused for digitization discovery: {local_path.relative_to(ROOT)}"
        else:
            response, body, attempts, error = self.inspect_url(source, source["url"])
            if error:
                status = "blocked" if response is None or response.status_code in {401, 403} else "failed"
                self._record(source, source["url"], status, response=response, attempts=attempts, error=error)
                return
            assert response is not None and body is not None
            final_url = response.url
            note = "Dartmouth landing page inspected; content not saved"
        soup = BeautifulSoup(body, "html.parser")
        resources = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(final_url, anchor["href"])
            text = anchor.get_text(" ", strip=True)
            parsed = urlparse(href)
            if parsed.netloc.lower() != "collections.dartmouth.edu":
                continue
            if "/meadows/" not in parsed.path.lower():
                continue
            if parsed.path.lower().endswith((".pdf", ".epub", ".mobi", ".html")):
                resources.append({"label": text, "url": href})
        item = {
            "source_id": source["source_id"], "source_url": source["url"], "final_url": final_url,
            "local_path": None, "content_type": "text/html",
            "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "status": "inspected", "attempts": attempts, "page_count": None,
            "title": soup.title.get_text(" ", strip=True) if soup.title else None,
            "collected_at": utc_now(), "error": None,
            "notes": note + "; digitized HTML/PDF/EPUB/MOBI links found but not fetched or saved because redistribution status is unclear",
            "discovered_resources": resources,
        }
        append_manifest(item)
        self.rows.append(item)
        logger.info("INSPECTED %s; digitized resources exposed=%d (not downloaded)", source["source_id"], len(resources))

    def _run_marx(self, source: dict) -> None:
        start_url = source["url"]
        body, final_url, links = self._inspect_page(source, start_url)
        if body is None or final_url is None:
            return
        queue = []
        discovered = 0
        pdf_links = []
        for url, label in links:
            parsed = urlparse(url)
            path = parsed.path.lower()
            if parsed.netloc.lower() != urlparse(start_url).netloc.lower():
                continue
            if path.endswith(".pdf"):
                if path.startswith("/archive/marx/works/1867-c1/") or (
                    "pdf version" in label.lower() and "capital-volume-i" in path
                ):
                    pdf_links.append(url)
                continue
            if not path.startswith("/archive/marx/works/1867-c1/"):
                continue
            if path.endswith((".htm", ".html")) and (
                re.search(r"/(ch\d+|[a-z][^/]*\.htm)$", path) or "chapter" in label.lower()
            ):
                queue.append(url)
        for pdf_url in dict.fromkeys(pdf_links):
            result = self.acquire_url(source, pdf_url, forced_kind="pdf")
            full_pdf = result and result.get("status") in {"success", "duplicate"} and result.get("page_count")
            if not full_pdf:
                full_pdf = any(
                    normalized_url(row.get("source_url", "")) == normalized_url(pdf_url)
                    and row.get("status") in {"success", "duplicate"}
                    and row.get("page_count")
                    for row in self.rows
                )
            if full_pdf:
                logger.info("SKIPPED separate HTML chapters; full-text PDF is already verified")
                return
            if result and result.get("status") in {"success", "duplicate"}:
                discovered += 1
        seen = set()
        for url in dict.fromkeys(queue):
            if len(seen) >= self.max_pages:
                break
            if normalized_url(url) in seen:
                continue
            seen.add(normalized_url(url))
            cached = self._cached_content(url)
            was_cached = cached is not None
            if cached:
                page, page_url = cached
            else:
                page, page_url, _ = self._inspect_page(source, url)
            if page and page_url and self._is_marx_text(page):
                if not was_cached:
                    self._save_html_page(source, url, page, page_url)
                else:
                    logger.info("SKIPPED verified existing Capital chapter %s", url)
                discovered += 1
        if not discovered:
            self._record(
                source,
                start_url,
                "skipped",
                notes="No legitimate Capital Volume I full-text resource passed link and content checks",
            )

    @staticmethod
    def _is_marx_text(body: bytes) -> bool:
        soup = BeautifulSoup(body, "html.parser")
        text = " ".join(soup.stripped_strings)
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        return len(text) >= 1200 and ("capital" in title or "marx" in title)

    def _run_inspect_only(self, source: dict) -> None:
        if any(
            row.get("status") == "inspected"
            and normalized_url(row.get("source_url", "")) == normalized_url(source["url"])
            for row in self.rows
        ):
            logger.info("SKIPPED previously inspected source %s", source["source_id"])
            return
        local_html = self._existing_page(source, source["url"])
        local_pdf = next((RAW_ROOT / source["source_id"]).glob("*.pdf"), None)
        if local_html:
            body, final_url, local_path = local_html
            response = requests.Response()
            response.status_code = 200
            response.url = final_url
            response.headers["Content-Type"] = "text/html"
            self._record_inspection(
                source, source["url"], response, body, 0,
                f"Inspection only; no content saved; existing page reused from {local_path.relative_to(ROOT)}",
            )
            logger.info("SKIPPED content storage by policy %s", source["source_id"])
            return
        if local_pdf:
            try:
                details = validate_content(local_pdf, "application/pdf", "pdf")
                body = local_pdf.read_bytes()
                response = requests.Response()
                response.status_code = 200
                response.url = source["url"]
                response.headers["Content-Type"] = "application/pdf"
                self._record_inspection(
                    source, source["url"], response, body, 0,
                    f"Inspection only; no content saved; existing PDF reused from {local_pdf.relative_to(ROOT)}; pages={details['page_count']}",
                )
                logger.info("SKIPPED PDF storage by policy %s", source["source_id"])
                return
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Existing PDF could not be inspected for %s: %s", source["source_id"], exc)
        response, body, attempts, error = self.inspect_url(source, source["url"])
        if error:
            status = "blocked" if response is None or response.status_code in {401, 403} else "failed"
            self._record(source, source["url"], status, response=response, attempts=attempts, error=error)
            return
        assert response is not None and body is not None
        self._record_inspection(
            source, source["url"], response, body, attempts,
            "Inspection only; no content saved",
        )
        logger.info("SKIPPED content storage by policy %s", source["source_id"])

    def _run_source(self, source: dict, verification: dict) -> None:
        policy = POLICIES.get(source["source_id"])
        if policy is None:
            self._record(source, source["url"], "skipped", error="No approved source-specific acquisition policy")
            return
        if policy.action == "exclude":
            item = self._record(source, source["url"], "excluded", notes=policy.reason)
            item["error"] = policy.reason
            return
        if policy.action == "blocked":
            self._record(source, source["url"], "blocked", error=policy.reason, notes="Prior source_verification.json records HTTP 403; no new request made")
            return
        logger.info("Processing %s (%s)", source["source_id"], policy.category)
        if policy.action == "gutenberg":
            self._run_gutenberg(source)
        elif policy.action == "openstax":
            self._run_openstax(source)
        elif policy.action == "mcgill_pdfs":
            self._run_pdf_links(source, require_textbooks=True)
        elif policy.action == "gutenberg_au_inspect":
            self._run_gutenberg_au_inspect(source)
        elif policy.action == "dartmouth_inspect":
            self._run_dartmouth_inspect(source)
        elif policy.action == "marx_html":
            self._run_marx(source)
        elif policy.action == "inspect_pdf_links":
            inspected_url = verification.get(source["source_id"], {}).get("final_url")
            if inspected_url:
                adjusted = dict(source)
                adjusted["url"] = inspected_url
                self._run_pdf_links(adjusted)
            else:
                self._record(source, source["url"], "failed", error="No current manager-verified Stanford URL available")
        elif policy.action == "inspect_only":
            if policy.category == "partial_sample":
                self._register_existing_sample(source)
            else:
                self._run_inspect_only(source)
        elif policy.action == "existing_sample_only":
            self._register_existing_sample(source)

    def _register_existing_sample(self, source: dict) -> None:
        candidates = list((RAW_ROOT / source["source_id"]).glob("*.pdf"))
        if not candidates:
            self._record(source, source["url"], "partial", notes="Sample-only source; no existing sample file found")
            return
        for path in candidates:
            existing_url = normalized_url(source["url"])
            if self._already_recorded(existing_url):
                return
            try:
                details = validate_content(path, "application/pdf", "pdf")
            except (OSError, ValueError, RuntimeError) as exc:
                self._record(source, source["url"], "failed", error=f"Existing sample PDF invalid: {exc}")
                continue
            checksum = sha256_file(path)
            row = {
                "source_id": source["source_id"], "source_url": source["url"], "final_url": source["url"],
                "local_path": str(path.relative_to(ROOT)), "content_type": "application/pdf",
                "size_bytes": path.stat().st_size, "sha256": checksum, "status": "partial",
                "attempts": 0, "page_count": details.get("page_count"), "title": details.get("title"),
                "collected_at": utc_now(), "error": None,
                "notes": "Existing validated sample chapters; not a complete book",
            }
            self.hash_index.setdefault(checksum, path)
            self.existing_successes.add(existing_url)
            self.rows.append(row)
            append_manifest(row)
            logger.info("PARTIAL sample reused %s", path)

    def run(self) -> dict:
        inventory = load_json(INVENTORY)
        verification_data = load_json(VERIFICATION)
        verification = {item["source_id"]: item for item in verification_data.get("sources", [])}
        sources = inventory.get("sources", [])
        if self.source_ids is not None:
            sources = [source for source in sources if source["source_id"] in self.source_ids]
        if self.source_limit is not None:
            sources = sources[:self.source_limit]
        logger.info("Acquisition started for %d manager sources", len(sources))
        for source in sources:
            self._run_source(source, verification)
        return write_reports(inventory.get("sources", []), self.rows)


def write_reports(sources: list[dict], rows: list[dict]) -> dict:
    categories = {source["source_id"]: POLICIES.get(source["source_id"]) for source in sources}
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row.get("source_id", ""), []).append(row)
    statuses = []
    for source in sources:
        source_id = source["source_id"]
        policy = categories[source_id]
        records = by_source.get(source_id, [])
        successful = [item for item in records if item.get("status") in {"success", "duplicate"} and item.get("local_path")]
        partials = [item for item in records if item.get("status") == "partial" and item.get("local_path")]
        latest = records[-1] if records else {}
        if policy.category == "excluded":
            status = "excluded"
        elif policy.category == "unavailable" and policy.action == "blocked":
            status = "blocked"
        elif max(
            (i for i, item in enumerate(records) if item.get("status") == "partial_source"),
            default=-1,
        ) > max(
            (i for i, item in enumerate(records) if item.get("status") == "crawl_complete"),
            default=-1,
        ):
            status = "partial"
        elif policy.category == "readable_online_redistribution_unclear" and any(
            item.get("status") == "inspected" for item in records
        ):
            status = "readable_online_redistribution_unclear"
        elif successful and any(item.get("status") in {"failed", "blocked"} for item in records):
            status = "partial"
        elif successful:
            status = "success"
        elif partials:
            status = "partial_sample"
        elif any(item.get("status") == "failed" for item in records):
            status = "failed"
        elif any(item.get("status") == "blocked" for item in records):
            status = "blocked"
        else:
            status = "not_acquired" if policy.category == "free_full_text" else policy.category
        statuses.append({
            "source_id": source_id, "source_url": source["url"],
            "category": policy.category, "status": status, "reason": policy.reason,
            "files": successful + partials, "latest_error": latest.get("error"),
        })

    full_ids = {item["source_id"] for item in statuses if item["status"] == "success"}
    partial_ids = {item["source_id"] for item in statuses if item["status"] in {"partial_sample", "partial"}}
    blocked_ids = {item["source_id"] for item in statuses if item["status"] == "blocked"}
    failed_ids = {item["source_id"] for item in statuses if item["status"] == "failed"}
    excluded_ids = {item["source_id"] for item in statuses if item["status"] == "excluded"}
    unique_files = {}
    for row in rows:
        if row.get("status") in {"success", "partial", "duplicate"} and row.get("sha256") and row.get("local_path"):
            unique_files.setdefault(row["sha256"], row)
    duplicates = [row for row in rows if row.get("status") == "duplicate"]
    landing_urls = {
        source["source_id"]: normalized_url(source["url"]) for source in sources
    }
    landing_hashes: dict[str, dict[str, dict]] = {}
    for row in rows:
        if (
            row.get("status") == "inspected"
            and row.get("sha256")
            and normalized_url(row.get("source_url", "")) == landing_urls.get(row.get("source_id"))
        ):
            landing_hashes.setdefault(row["sha256"], {})[row.get("source_url", "")] = {
                    "source_id": row.get("source_id"),
                    "source_url": row.get("source_url"),
                    "title": row.get("title"),
                    "sha256": row.get("sha256"),
                    "notes": "Landing/inspection response only; not corpus text",
                }
    landing_duplicates = [
        list(group.values()) for group in landing_hashes.values() if len(group) > 1
    ]
    summary = {
        "generated_at": utc_now(),
        "total_manager_sources": len(sources),
        "successful_full_text_sources": len(full_ids),
        "partial_sample_sources": len(partial_ids),
        "blocked_sources": len(blocked_ids),
        "failed_sources": len(failed_ids),
        "excluded_sources": len(excluded_ids),
        "duplicates": len(duplicates) + len(landing_duplicates),
        "total_files": len(unique_files),
        "total_bytes": sum(int(row.get("size_bytes") or 0) for row in unique_files.values()),
        "source_counts": {
            "successful_full_text_ids": sorted(full_ids),
            "partial_sample_ids": sorted(partial_ids),
            "blocked_ids": sorted(blocked_ids),
            "failed_ids": sorted(failed_ids),
            "excluded_ids": sorted(excluded_ids),
        },
    }
    reports = ROOT / "reports"
    (reports / "acquisition_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (reports / "source_status.json").write_text(json.dumps(statuses, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failure_map = {}
    for row in rows:
        if row.get("status") not in {"failed", "blocked"}:
            continue
        item = {
            key: row.get(key)
            for key in ("source_id", "source_url", "final_url", "status", "attempts", "error", "notes")
        }
        key = (item["source_id"], item["source_url"], item["status"])
        if key not in failure_map or (item["attempts"] or 0) > (failure_map[key]["attempts"] or 0):
            failure_map[key] = item
    failure_rows = list(failure_map.values())
    recorded_failures = {
        (item.get("source_id"), item.get("source_url"), item.get("status"))
        for item in failure_rows
    }
    for item in statuses:
        if item["status"] in {"failed", "blocked"} and (
            item["source_id"], item["source_url"], item["status"]
        ) not in recorded_failures:
            failure_rows.append({
                "source_id": item["source_id"], "source_url": item["source_url"],
                "final_url": None, "status": item["status"], "attempts": 0,
                "error": item.get("latest_error"), "notes": item["reason"],
            })
    (reports / "source_failures.json").write_text(
        json.dumps(failure_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (reports / "source_exclusions.json").write_text(json.dumps([item for item in statuses if item["status"] == "excluded"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    duplicate_report = {
        "content_duplicates": duplicates,
        "landing_page_duplicates": landing_duplicates,
    }
    (reports / "duplicate_report.json").write_text(json.dumps(duplicate_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def configure_logging() -> None:
    log_path = ROOT / "logs" / "acquisition.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Acquire only approved manager-listed economic sources.")
    parser.add_argument("--limit", type=int, help="Process only the first N inventory sources (for a small run).")
    parser.add_argument("--source-ids", nargs="+", help="Process only these manager inventory source IDs.")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_SOURCE)
    args = parser.parse_args(argv)
    configure_logging()
    summary = AcquisitionRunner(
        max_pages=args.max_pages,
        source_limit=args.limit,
        source_ids=set(args.source_ids) if args.source_ids else None,
    ).run()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
