from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pymupdf
from bs4 import BeautifulSoup
from lxml import etree


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw"
PROCESSED_ROOT = ROOT / "data" / "processed"
SOURCE_STATUS = ROOT / "reports" / "source_status.json"
INVENTORY = ROOT / "reports" / "source_inventory.json"
ACQUISITION_MANIFEST = ROOT / "reports" / "acquisition_manifest.jsonl"
LEGACY_MANIFESTS = (
    RAW_ROOT / "crawled" / "openstax_org" / "manifest.json",
    RAW_ROOT / "crawled" / "www_econlib_org" / "manifest.json",
    RAW_ROOT / "crawled" / "www_library_dartmouth_edu" / "manifest.json",
    RAW_ROOT / "crawled" / "www_marxists_org" / "manifest.json",
)
CORPUS_JSONL = PROCESSED_ROOT / "normalized_corpus.jsonl"
EXTRACTION_MANIFEST = PROCESSED_ROOT / "extraction_manifest.jsonl"
LOG_PATH = ROOT / "logs" / "extraction.log"
MIN_TEXT_CHARS = 150

logger = logging.getLogger("economic_extraction")

ELIGIBLE_SOURCE_IDS = {
    "economic_source_001",
    "economic_source_004",
    "economic_source_005",
    "economic_source_008",
    "economic_source_010",
    "economic_source_014",
    "economic_source_015",
    "economic_source_016",
    "economic_source_017",
    "economic_source_019",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "http" and parsed.netloc.lower() == "openstax.org" else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme, fragment=""))


def sniff_format(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(32).lstrip()
    suffix = path.suffix.lower()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        try:
            with zipfile.ZipFile(path) as archive:
                if "mimetype" in archive.namelist() or "META-INF/container.xml" in archive.namelist():
                    return "epub"
        except zipfile.BadZipFile:
            return "invalid_zip"
        return "zip"
    if suffix == ".json" or head.startswith((b"{", b"[")):
        return "json"
    if suffix == ".xml" or head.startswith(b"<?xml"):
        return "xml"
    if head.lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "html"
    if suffix in {".html", ".htm", ".xhtml"}:
        return "html"
    if suffix == ".pdf":
        return "invalid_pdf"
    if suffix == ".epub":
        return "invalid_epub"
    return "unsupported"


def _text_from_soup(soup: BeautifulSoup) -> tuple[str, str]:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    for node in soup.find_all(
        ["script", "style", "noscript", "svg", "nav", "footer", "form", "button", "iframe"]
    ):
        node.decompose()

    for selector in (
        "[role=navigation]", "[role=contentinfo]", "[aria-label*=navigation i]",
        ".site-header", ".site-footer", ".navbar", ".breadcrumb", ".breadcrumbs",
        ".sidebar", ".menu", ".toolbar",
    ):
        for node in soup.select(selector):
            node.decompose()

    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    if main is None:
        main = soup.body or soup

    paragraphs = []
    for node in main.find_all(
        ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "dt", "dd",
         "pre", "figcaption", "td", "th"]
    ):
        text = " ".join(node.stripped_strings)
        if text and (not paragraphs or text != paragraphs[-1]):
            paragraphs.append(text)

    text = "\\n\\n".join(line for line in paragraphs if line).strip()

    # Fallback for pages whose meaningful text is outside the selected tags.
    if len(text) < MIN_TEXT_CHARS:
        text = " ".join(main.stripped_strings).strip()

    return unescape(title), text

def extract_html_bytes(data: bytes) -> tuple[str, str]:
    soup = BeautifulSoup(data, "lxml")
    return _text_from_soup(soup)


def flatten_json(value, prefix: str = "") -> list[str]:
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = f"{prefix}.{key}" if prefix else str(key)
            result.extend(flatten_json(item, label))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(flatten_json(item, f"{prefix}[{index}]"))
    elif isinstance(value, (str, int, float, bool)) and str(value).strip():
        result.append(f"{prefix}: {value}" if prefix else str(value))
    return result


def _xml_text(path: Path) -> tuple[str, str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()
    title = ""
    for element in root.iter():
        if etree.QName(element).localname.lower() in {"title", "name"} and element.text and element.text.strip():
            title = element.text.strip()
            break
    def walk(element, parents: tuple[str, ...] = ()) -> list[str]:
        if not isinstance(element.tag, str):
            return []
        name = etree.QName(element).localname
        current = (*parents, name)
        own_text = " ".join((element.text or "").split())
        lines = [f"{'/'.join(current)}: {own_text}"] if own_text else []
        for child in element:
            lines.extend(walk(child, current))
        return lines

    lines = walk(root)
    return title, "\n".join(lines)


def _epub_chapters(path: Path) -> tuple[str, list[dict]]:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"EPUB archive contains corrupt member: {bad}")
        container = etree.fromstring(archive.read("META-INF/container.xml"))
        opf_path = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not opf_path:
            raise ValueError("EPUB container does not identify a package document")
        package_path = opf_path[0]
        package = etree.fromstring(archive.read(package_path))
        title_nodes = package.xpath("//*[local-name()='metadata']/*[local-name()='title']")
        title = " ".join(" ".join(title_nodes[0].itertext()).split()) if title_nodes else ""
        manifest = {
            node.get("id"): node.get("href")
            for node in package.xpath("//*[local-name()='manifest']/*[local-name()='item']")
            if node.get("id") and node.get("href")
        }
        chapters = []
        opf_dir = Path(package_path).parent
        for itemref in package.xpath("//*[local-name()='spine']/*[local-name()='itemref']"):
            href = manifest.get(itemref.get("idref"))
            if not href:
                continue
            member = (opf_dir / href).as_posix()
            try:
                data = archive.read(member)
            except KeyError as exc:
                raise ValueError(f"EPUB spine item is missing: {member}") from exc
            chapter_title, text = extract_html_bytes(data)
            if text.strip():
                chapters.append({"title": chapter_title, "path": member, "text": text})
        if not chapters:
            raise ValueError("EPUB contains no readable spine chapters")
        return title, chapters


def extract_file(path: Path, fmt: str) -> dict:
    if fmt == "html":
        title, text = extract_html_bytes(path.read_bytes())
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"HTML yielded only {len(text)} readable characters")
        return {"title": title, "content": text, "pages": None}
    if fmt == "pdf":
        if path.stat().st_size < 256:
            raise ValueError("PDF file is too small")
        try:
            document = pymupdf.open(path)
        except (pymupdf.FileDataError, RuntimeError) as exc:
            raise ValueError(f"Could not open PDF: {exc}") from exc
        try:
            if len(document) == 0:
                raise ValueError("PDF has no pages")
            pages = [
                {"page": number, "text": page.get_text("text").strip()}
                for number, page in enumerate(document, start=1)
            ]
            text_length = sum(len(page["text"]) for page in pages)
            if text_length < MIN_TEXT_CHARS:
                raise ValueError(f"PDF yielded only {text_length} text characters across {len(pages)} pages")
            title = (document.metadata or {}).get("title") or ""
            return {"title": title.strip(), "content": "\n\n".join(p["text"] for p in pages), "pages": pages}
        finally:
            document.close()
    if fmt == "xml":
        title, text = _xml_text(path)
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"XML yielded only {len(text)} readable characters")
        return {"title": title, "content": text, "pages": None}
    if fmt == "json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        text = "\n".join(flatten_json(value))
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"JSON yielded only {len(text)} meaningful text characters")
        title = value.get("title", "") if isinstance(value, dict) else ""
        return {"title": str(title).strip(), "content": text, "structured_data": value, "pages": None}
    if fmt == "epub":
        title, chapters = _epub_chapters(path)
        text = "\n\n".join(chapter["text"] for chapter in chapters)
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError(f"EPUB yielded only {len(text)} readable characters")
        return {"title": title, "content": text, "chapters": chapters, "pages": None}
    raise ValueError(f"Unsupported or invalid source file format: {fmt}")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        raise ValueError(f"Expected a JSON object or array in {path}")
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.error("Malformed JSONL at %s:%d", path, number)
    return rows


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())


def source_url_in_scope(source_id: str, url: str | None, path: Path) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    p = parsed.path.lower()
    if source_id == "economic_source_001":
        return (
            parsed.netloc.lower() == "hastie.su.domains"
            and path.suffix.lower() == ".pdf"
            and path.name.lower() == "eslii_print12_toc.pdf"
        )
    if source_id in {"economic_source_004", "economic_source_005", "economic_source_014", "economic_source_015", "economic_source_016"}:
        match = re.search(r"/cache/epub/(\d+)/pg\1-[^/]+\.html$", p)
        return match is not None
    if source_id == "economic_source_008":
        return (
            parsed.netloc.lower() == "openstax.org"
            and "/books/principles-economics-3e/pages/" in p
            and re.search(r"/pages/(?:\d+-[^/]+|[a-e]-[^/]+)/?$", p) is not None
        )
    if source_id == "economic_source_010":
        return (
            parsed.netloc.lower() == "russell-davidson.research.mcgill.ca"
            and p.startswith("/textbooks/")
            and path.suffix.lower() == ".pdf"
            and path.name.lower() != "eie-davidson-mackinnon-2021.pdf"
        )
    if source_id == "economic_source_017":
        return path.suffix.lower() == ".pdf" and "samplechapters" in p
    if source_id == "economic_source_019":
        return (
            parsed.netloc.lower() == "www.marxists.org"
            and p.startswith("/archive/marx/works/1867-c1/")
            and (
                p.endswith(".pdf") and "capital-volume-i.pdf" in p
                or p.endswith((".htm", ".html"))
            )
        )
    return False


def legacy_source_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.lower()
    if (
        parsed.netloc.lower() == "openstax.org"
        and "/books/principles-economics-3e/pages/" in path
    ):
        return "economic_source_008"
    if (
        parsed.netloc.lower() == "www.marxists.org"
        and path.startswith("/archive/marx/works/1867-c1/")
        and path.endswith((".htm", ".html"))
    ):
        return "economic_source_019"
    return None


class BatchExtractor:
    def __init__(
        self,
        source_ids: set[str] | None = None,
        limit: int | None = None,
        sample_only: bool = False,
    ):
        self.source_ids = source_ids
        self.limit = limit
        self.sample_only = sample_only
        self.statuses = json.loads(SOURCE_STATUS.read_text(encoding="utf-8-sig"))
        self.inventory = json.loads(INVENTORY.read_text(encoding="utf-8-sig"))
        self.acquisition_rows = load_jsonl(ACQUISITION_MANIFEST)
        self.legacy_rows = []
        for manifest_path in LEGACY_MANIFESTS:
            self.legacy_rows.extend(load_jsonl(manifest_path))
        self.corpus_rows = load_jsonl(CORPUS_JSONL)
        self.manifest_rows = load_jsonl(EXTRACTION_MANIFEST)
        self.success_by_path_hash = {
            (row.get("source_path"), row.get("source_sha256")): row
            for row in self.manifest_rows
            if row.get("extraction_status") in {"success", "duplicate_content"}
        }
        self.success_by_path_hash.update({
            (row.get("source_path"), row.get("source_sha256")): row
            for row in self.corpus_rows
            if row.get("source_sha256")
        })
        self.corpus_by_hash = {
            row.get("source_sha256"): row
            for row in self.corpus_rows
            if row.get("source_sha256")
        }
        self.processed_records = []

    def _file_urls(self) -> dict[str, str]:
        mapping = {}
        for status in self.statuses:
            for item in status.get("files", []):
                path = item.get("path") or item.get("local_path")
                source_url = item.get("source_url") or item.get("final_url")
                if path and source_url:
                    mapping[str(Path(path))] = source_url
                    mapping[Path(path).as_posix()] = source_url
        for row in self.acquisition_rows:
            path = row.get("local_path")
            if path and row.get("source_url"):
                mapping[str(Path(path))] = row["source_url"]
                mapping[Path(path).as_posix()] = row["source_url"]
        for row in self.legacy_rows:
            path = row.get("path")
            url = row.get("url")
            if path and url:
                mapping[str(Path(path))] = url
                mapping[Path(path).as_posix()] = url
        return mapping

    def candidates(self) -> list[dict]:
        urls = self._file_urls()
        candidates = []
        statuses = {item["source_id"]: item for item in self.statuses}
        inventory = {item["source_id"]: item for item in self.inventory.get("sources", [])}
        for source_id in sorted(ELIGIBLE_SOURCE_IDS):
            if self.source_ids and source_id not in self.source_ids:
                continue
            status = statuses.get(source_id, {})
            if status.get("excluded") or status.get("classification", "").startswith("excluded"):
                continue
            if source_id == "economic_source_017" and not self.sample_only:
                continue
            source_files = {}
            for item in status.get("files", []):
                path = item.get("path") or item.get("local_path")
                if path:
                    source_url = item.get("source_url") or item.get("final_url")
                    source_files[str(Path(path))] = source_url
                    source_files[Path(path).as_posix()] = source_url
            source_dir = RAW_ROOT / source_id
            if source_dir.exists():
                for path in source_dir.rglob("*"):
                    if path.is_file() and not path.name.endswith(".part"):
                        source_files.setdefault(str(path.relative_to(ROOT)), None)
                        source_files.setdefault(path.relative_to(ROOT).as_posix(), None)
            for legacy_row in self.legacy_rows:
                if legacy_source_id(legacy_row.get("url")) != source_id:
                    continue
                path = legacy_row.get("path")
                if path:
                    source_files[str(Path(path))] = legacy_row.get("url")
                    source_files[Path(path).as_posix()] = legacy_row.get("url")
            seen_paths = set()
            for path_key, explicit_url in source_files.items():
                path = Path(path_key)
                if not path.is_absolute():
                    path = ROOT / path
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(RAW_ROOT.resolve())
                except (OSError, ValueError):
                    continue
                if not resolved.is_file() or resolved.name.endswith(".part") or resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                url = explicit_url or urls.get(path_key) or urls.get(str(resolved.relative_to(ROOT))) or urls.get(resolved.relative_to(ROOT).as_posix())
                if not source_url_in_scope(source_id, url, resolved):
                    continue
                fmt = sniff_format(resolved)
                if fmt in {"unsupported", "invalid_zip", "invalid_pdf", "invalid_epub"}:
                    candidates.append({
                        "source_id": source_id, "source_url": url, "path": resolved,
                        "format": fmt, "source_record": inventory.get(source_id, {}),
                    })
                    continue
                candidates.append({
                    "source_id": source_id, "source_url": url, "path": resolved,
                    "format": fmt, "source_record": inventory.get(source_id, {}),
                })
        candidates.sort(key=lambda x: (x["source_id"], str(x["path"]).lower()))
        return candidates[:self.limit] if self.limit is not None else candidates

    def _record_event(self, item: dict, status: str, *, error: str | None = None, **extra) -> dict:
        row = {
            "source_id": item["source_id"],
            "source_url": item.get("source_url"),
            "source_path": item["path"].relative_to(ROOT).as_posix(),
            "source_sha256": item.get("sha256"),
            "file_format": item.get("format"),
            "extraction_status": status,
            "text_length": extra.pop("text_length", None),
            "title": extra.pop("title", None),
            "extracted_at": now_utc(),
            "error": error,
            **extra,
        }
        append_jsonl(EXTRACTION_MANIFEST, row)
        self.manifest_rows.append(row)
        self.processed_records.append(row)
        logger.info("%s %s %s", status.upper(), row["source_id"], row["source_path"])
        return row

    def run(self) -> dict:
        PROCESSED_ROOT.mkdir(parents=True, exist_ok=True)
        items = self.candidates()
        totals = Counter()
        for item in items:
            path = item["path"]
            try:
                item["sha256"] = file_sha256(path)
            except OSError as exc:
                row = self._record_event(item, "failed", error=f"Could not hash source file: {exc}")
                totals[row["extraction_status"]] += 1
                continue
            key = (path.relative_to(ROOT).as_posix(), item["sha256"])
            if key in self.success_by_path_hash:
                totals["skipped_unchanged"] += 1
                logger.info("SKIPPED unchanged %s", key[0])
                continue
            duplicate = self.corpus_by_hash.get(item["sha256"])
            if duplicate:
                row = self._record_event(
                    item, "duplicate_content",
                    title=duplicate.get("title"),
                    text_length=duplicate.get("text_length"),
                    duplicate_of=duplicate.get("source_path"),
                    duplicate_source_id=duplicate.get("source_id"),
                )
                totals[row["extraction_status"]] += 1
                continue
            if item["format"] in {"unsupported", "invalid_zip", "invalid_pdf", "invalid_epub"}:
                row = self._record_event(item, "failed", error=f"Unsupported or invalid file format: {item['format']}")
                totals[row["extraction_status"]] += 1
                continue
            try:
                extracted = extract_file(path, item["format"])
                title = extracted.get("title") or path.stem
                text = extracted["content"]
                if not text.strip():
                    raise ValueError("Extractor returned empty text")
                record = {
                    "source_id": item["source_id"],
                    "source_url": item["source_url"],
                    "canonical_source_url": canonical_url(item["source_url"]),
                    "title": title,
                    "record_type": "sample" if item["source_id"] == "economic_source_017" else "document",
                    "source_path": path.relative_to(ROOT).as_posix(),
                    "source_sha256": item["sha256"],
                    "file_format": item["format"],
                    "extraction_status": "success",
                    "text_length": len(text),
                    "content": text,
                    "metadata": {
                        "pages": extracted.get("pages"),
                        "chapters": extracted.get("chapters"),
                        "structured_data": extracted.get("structured_data"),
                        "sample_only": item["source_id"] == "economic_source_017",
                    },
                    "collected_at": now_utc(),
                    "schema_version": "1.0",
                }
                append_jsonl(CORPUS_JSONL, record)
                self.corpus_rows.append(record)
                self.success_by_path_hash[key] = record
                self.corpus_by_hash[item["sha256"]] = record
                row = self._record_event(item, "success", title=title, text_length=len(text))
                totals[row["extraction_status"]] += 1
            except Exception as exc:
                row = self._record_event(item, "failed", error=f"{type(exc).__name__}: {exc}")
                totals[row["extraction_status"]] += 1
        summary = self._write_reports(items, totals)
        return summary

    def _write_reports(self, items: list[dict], current_counts: Counter) -> dict:
        records = load_jsonl(CORPUS_JSONL)
        manifest = load_jsonl(EXTRACTION_MANIFEST)
        latest_by_path = {}
        for row in manifest:
            latest_by_path[row.get("source_path")] = row
        failures = [
            row for row in latest_by_path.values()
            if row.get("extraction_status") == "failed"
        ]
        failure_path = ROOT / "reports" / "extraction_failures.csv"
        with failure_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "source_id", "source_url", "source_path", "source_sha256",
                "file_format", "error", "extracted_at",
            ])
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in writer.fieldnames} for row in failures)

        rights = [
            row for row in self.statuses
            if row.get("classification") in {
                "readable_online_redistribution_unclear",
                "excluded_paid_sample",
            } or row.get("status") in {"readable_online_redistribution_unclear", "excluded"}
        ]
        rights_path = ROOT / "reports" / "rights_review.csv"
        with rights_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["source_id", "url", "classification", "status", "reason", "resume_required"])
            writer.writeheader()
            for row in rights:
                writer.writerow({
                    "source_id": row.get("source_id"),
                    "url": row.get("url"),
                    "classification": row.get("classification"),
                    "status": row.get("status"),
                    "reason": row.get("classification_reason") or row.get("remaining_work"),
                    "resume_required": row.get("resume_required"),
                })

        # Full per-source rights determination across all 20 manager sources
        # (reports/rights_review.csv above stays narrow/legacy for existing
        # consumers; this is the comprehensive companion the audit relies on).
        extraction_counts_by_source: dict[str, Counter] = defaultdict(Counter)
        for row in latest_by_path.values():
            extraction_counts_by_source[row.get("source_id")][row.get("extraction_status", "unknown")] += 1

        full_rights_path = ROOT / "reports" / "rights_review_full.csv"
        with full_rights_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "source_id", "url", "classification", "status",
                "rights_determination", "accessible", "downloaded",
                "extracted", "complete_coverage", "reason", "resume_required",
            ])
            writer.writeheader()
            for row in self.statuses:
                status = row.get("status")
                classification = row.get("classification")
                if classification == "excluded" or status == "excluded":
                    rights_determination = "paid_or_restricted"
                elif classification == "readable_online_redistribution_unclear":
                    rights_determination = "rights_unclear"
                elif classification == "free_full_text" and status == "success":
                    rights_determination = "rights_verified_public_domain_or_authorized"
                elif status == "partial" or status == "partial_sample":
                    rights_determination = "rights_verified_public_domain_or_authorized_partial"
                else:
                    rights_determination = "unverified"
                extraction_counts = extraction_counts_by_source.get(row["source_id"], Counter())
                writer.writerow({
                    "source_id": row.get("source_id"),
                    "url": row.get("url"),
                    "classification": classification,
                    "status": status,
                    "rights_determination": rights_determination,
                    "accessible": bool(row.get("verified")),
                    "downloaded": bool(row.get("files_downloaded")),
                    "extracted": extraction_counts.get("success", 0) > 0,
                    "complete_coverage": bool(row.get("completed")),
                    "reason": row.get("classification_reason") or row.get("remaining_work"),
                    "resume_required": row.get("resume_required"),
                })

        totals = Counter()
        for row in latest_by_path.values():
            totals[row.get("extraction_status", "unknown")] += 1
        summary = {
            "generated_at": now_utc(),
            "eligible_file_candidates_this_run": len(items),
            "current_run_results": dict(current_counts),
            "manifest_latest_status_counts": dict(totals),
            "unique_normalized_records": len(records),
            "unique_source_files_successfully_extracted": sum(
                1 for row in latest_by_path.values() if row.get("extraction_status") == "success"
            ),
            "duplicate_content_files": sum(
                1 for row in latest_by_path.values() if row.get("extraction_status") == "duplicate_content"
            ),
            "failed_extractions": len(failures),
            "normalized_text_characters": sum(int(row.get("text_length") or 0) for row in records),
            "normalized_jsonl": CORPUS_JSONL.relative_to(ROOT).as_posix(),
            "extraction_manifest": EXTRACTION_MANIFEST.relative_to(ROOT).as_posix(),
            "extraction_failures_csv": failure_path.relative_to(ROOT).as_posix(),
            "rights_review_csv": rights_path.relative_to(ROOT).as_posix(),
            "eligible_source_ids": sorted(ELIGIBLE_SOURCE_IDS),
            "rights_unclear_or_paid_sources_not_extracted": sorted(row["source_id"] for row in rights),
            "note": "Unclear-rights and paid/sample-only complete-work sources were excluded; Cochrane sample pages are retained only as a sample record.",
        }
        (ROOT / "reports" / "extraction_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._write_final_status_reports(summary)
        logger.info("Batch complete: %s", json.dumps(summary, ensure_ascii=False))
        return summary

    def _write_final_status_reports(self, extraction_summary: dict) -> None:
        statuses = json.loads(SOURCE_STATUS.read_text(encoding="utf-8-sig"))
        latest_by_file = {}
        for row in load_jsonl(EXTRACTION_MANIFEST):
            latest_by_file[(row.get("source_id"), row.get("source_path"))] = row
        latest_by_source = {}
        for row in latest_by_file.values():
            latest_by_source.setdefault(row.get("source_id"), Counter())
            latest_by_source[row.get("source_id")][row.get("extraction_status", "unknown")] += 1
        columns = [
            "source_id", "url", "classification", "status", "verified",
            "completed", "partial", "pending", "excluded", "failed",
            "resume_required", "files_downloaded", "bytes_downloaded",
            "pages_downloaded", "pages_failed", "pages_skipped",
            "extraction_success", "extraction_failed", "extraction_duplicates",
            "remaining_work", "error",
        ]
        final_csv = ROOT / "reports" / "final_source_status.csv"
        with final_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for source in statuses:
                extraction_counts = latest_by_source.get(source["source_id"], Counter())
                writer.writerow({
                    **{key: source.get(key) for key in columns},
                    "extraction_success": extraction_counts.get("success", 0),
                    "extraction_failed": extraction_counts.get("failed", 0),
                    "extraction_duplicates": extraction_counts.get("duplicate_content", 0),
                })

        summary = json.loads((ROOT / "reports" / "acquisition_summary.json").read_text(encoding="utf-8-sig"))
        corpus_summary_path = ROOT / "reports" / "final_corpus_summary.md"
        mapped_files = sum(int(source.get("files_downloaded") or 0) for source in statuses)
        mapped_bytes = sum(int(source.get("bytes_downloaded") or 0) for source in statuses)
        mapped_hashes = {
            digest
            for source in statuses
            for digest in source.get("sha256", [])
        }
        structured_summary_path = ROOT / "reports" / "structured_dataset_summary.json"
        structured_summary = (
            json.loads(structured_summary_path.read_text(encoding="utf-8"))
            if structured_summary_path.exists()
            else None
        )
        lines = [
            "# EconomicCorpus status",
            "",
            f"Report generated: {now_utc()}",
            "",
            "## Acquisition",
            "",
            f"- Manager-listed sources: {summary.get('total_manager_sources', len(statuses))}",
            f"- Full-text sources completed in last acquisition summary: {summary.get('successful_full_text_sources', 0)}",
            f"- Partial/sample sources: {summary.get('partial_sample_sources', 0)}",
            f"- Blocked sources: {summary.get('blocked_sources', 0)}",
            f"- Failed sources: {summary.get('failed_sources', 0)}",
            f"- Excluded sources: {summary.get('excluded_sources', 0)}",
            f"- Acquisition-manifest files/bytes: {summary.get('total_files', 0)} / {summary.get('total_bytes', 0)}",
            f"- Manager-source mapped local files/bytes: {mapped_files} / {mapped_bytes}",
            f"- Unique SHA-256 hashes among mapped files: {len(mapped_hashes)}",
            "",
            "## Extraction",
            "",
            f"- Unique normalized records: {extraction_summary['unique_normalized_records']}",
            f"- Successfully extracted source files: {extraction_summary['unique_source_files_successfully_extracted']}",
            f"- Duplicate-content files: {extraction_summary['duplicate_content_files']}",
            f"- Failed extractions: {extraction_summary['failed_extractions']}",
            f"- Extracted text characters: {extraction_summary['normalized_text_characters']}",
            f"- Corpus JSONL: `{extraction_summary['normalized_jsonl']}`",
            "",
            "## Rights and completeness",
            "",
            "Rights-unclear sources are listed in `rights_review.csv` and were not extracted. Paid/sample-only sources were excluded as complete books; permitted sample chapters remain labeled as sample material.",
            "The corpus is not complete while source acquisition or extraction failures remain. See `final_source_status.csv`, `extraction_failures.csv`, and `resume_state.json` before resuming.",
        ]
        if structured_summary:
            lines.extend([
                "",
                "## Structured public datasets",
                "",
                f"- Unique datasets: {structured_summary['unique_datasets']}",
                f"- Unique dataset slices: {structured_summary['unique_dataset_slices']}",
                f"- Downloaded unique files/bytes: {structured_summary['downloaded_unique_files']} / {structured_summary['downloaded_bytes']}",
                f"- Unique structured records: {structured_summary['unique_records']}",
                f"- Missing observation values: {structured_summary['missing_values']}",
                f"- Extracted document-text characters: {extraction_summary['normalized_text_characters']} (structured values remain typed records, not flattened text)",
                "- Structured acquisition manifest: `public_dataset_acquisition_manifest.jsonl`",
                "- Structured validation: `public_dataset_validation.json`",
                "- Public dataset rights review: `public_dataset_rights_review.csv`",
                "- Public dataset failures: `public_dataset_failures.json`",
            ])
        lines.append("")
        corpus_summary_path.write_text("\n".join(lines), encoding="utf-8")


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resumable extraction of existing permitted source files.")
    parser.add_argument("--source-ids", nargs="+", help="Restrict to selected manager source IDs.")
    parser.add_argument("--limit", type=int, help="Process at most N files (for a sample run).")
    parser.add_argument("--include-samples", action="store_true", help="Extract existing sample material with sample labeling.")
    parser.add_argument("--reports-only", action="store_true", help="Refresh reports from current local manifests without extracting.")
    args = parser.parse_args(argv)
    configure_logging()
    runner = BatchExtractor(
        source_ids=set(args.source_ids) if args.source_ids else None,
        limit=args.limit,
        sample_only=args.include_samples,
    )
    summary = (
        runner._write_reports(runner.candidates(), Counter())
        if args.reports_only
        else runner.run()
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
