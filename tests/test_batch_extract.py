import tempfile
import unittest
import zipfile
from pathlib import Path

import pymupdf

from scripts.extract.batch_extract import (
    canonical_url,
    extract_file,
    extract_html_bytes,
    flatten_json,
    load_jsonl,
    legacy_source_id,
    sniff_format,
    source_url_in_scope,
)


class BatchExtractTests(unittest.TestCase):
    def test_html_removes_navigation_scripts_and_footer(self):
        data = b"""<!doctype html><html><head><title>Book</title>
        <script>tracking noise</script></head><body><nav>menu noise</nav>
        <main><h1>Chapter One</h1><p>Economics text.</p></main>
        <footer>footer noise</footer></body></html>"""
        title, text = extract_html_bytes(data)
        self.assertEqual(title, "Book")
        self.assertIn("Chapter One", text)
        self.assertIn("Economics text.", text)
        self.assertNotIn("menu noise", text)
        self.assertNotIn("tracking noise", text)
        self.assertNotIn("footer noise", text)

    def test_html_extracts_definition_list_terms(self):
        data = b"""<html><head><title>Key Terms</title></head><body>
        <main class="page-content"><h1>Key Terms</h1><dl>
        <dt>gross domestic product</dt><dd>the market value of final goods and services.</dd>
        </dl></main></body></html>"""
        title, text = extract_html_bytes(data)
        self.assertEqual(title, "Key Terms")
        self.assertIn("gross domestic product", text)
        self.assertIn("market value of final goods", text)

    def test_pdf_extraction_preserves_page_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pdf"
            doc = pymupdf.open()
            for text in [
                "First economics page. " * 12,
                "Second economics page. " * 12,
            ]:
                page = doc.new_page()
                page.insert_text((72, 72), text)
            doc.save(path)
            doc.close()
            self.assertEqual(sniff_format(path), "pdf")
            extracted = extract_file(path, "pdf")
            self.assertEqual([page["page"] for page in extracted["pages"]], [1, 2])
            self.assertIn("First economics page", extracted["pages"][0]["text"])
            self.assertIn("Second economics page", extracted["pages"][1]["text"])

    def test_json_flattener_keeps_keys_and_values(self):
        lines = flatten_json({"title": "GDP", "sections": [{"text": "Output and income"}]})
        self.assertIn("title: GDP", lines)
        self.assertIn("sections[0].text: Output and income", lines)

    def test_xml_extraction_keeps_element_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.xml"
            path.write_text(
                "<book><title>Economic History</title><chapter><p>"
                + "Trade and growth. " * 15
                + "</p></chapter></book>",
                encoding="utf-8",
            )
            extracted = extract_file(path, "xml")
            self.assertIn("Economic History", extracted["title"])
            self.assertIn("Trade and growth.", extracted["content"])

    def test_epub_spine_is_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.epub"
            container = b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
            <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>"""
            package = b"""<package xmlns="http://www.idpf.org/2007/opf">
            <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Economics</dc:title></metadata>
            <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
            <spine><itemref idref="chapter"/></spine></package>"""
            chapter = b"<html><head><title>Chapter</title></head><body><main><p>" + (
                b"Economics and markets. " * 12
            ) + b"</p></main></body></html>"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr("META-INF/container.xml", container)
                archive.writestr("OEBPS/content.opf", package)
                archive.writestr("OEBPS/chapter.xhtml", chapter)
            self.assertEqual(sniff_format(path), "epub")
            extracted = extract_file(path, "epub")
            self.assertEqual(extracted["title"], "Economics")
            self.assertEqual(len(extracted["chapters"]), 1)
            self.assertIn("Economics and markets", extracted["content"])

    def test_scope_rejects_landing_pages_and_unrelated_books(self):
        path = Path("pg30107-images.html")
        self.assertTrue(source_url_in_scope(
            "economic_source_004",
            "https://www.gutenberg.org/cache/epub/30107/pg30107-images.html",
            path,
        ))
        self.assertFalse(source_url_in_scope(
            "economic_source_004", "https://www.gutenberg.org/ebooks/30107", path
        ))
        self.assertFalse(source_url_in_scope(
            "economic_source_008",
            "https://openstax.org/books/biology-2e/pages/1-introduction",
            Path("1-introduction"),
        ))
        self.assertFalse(source_url_in_scope(
            "economic_source_010",
            "https://russell-davidson.research.mcgill.ca/textbooks/EIE-davidson-mackinnon-2021.pdf",
            Path("EIE-davidson-mackinnon-2021.pdf"),
        ))
        self.assertTrue(source_url_in_scope(
            "economic_source_019",
            "https://www.marxists.org/archive/marx/works/1867-c1/ch01.htm",
            Path("ch01.htm"),
        ))

    def test_legacy_url_scope_maps_only_approved_book_domains(self):
        self.assertEqual(
            legacy_source_id("https://www.marxists.org/archive/marx/works/1867-c1/ch01.htm"),
            "economic_source_019",
        )
        self.assertEqual(
            legacy_source_id("https://openstax.org/books/principles-economics-3e/pages/1-introduction"),
            "economic_source_008",
        )
        self.assertIsNone(legacy_source_id("https://www.econlib.org/"))

    def test_openstax_http_url_canonicalizes_to_https(self):
        self.assertEqual(
            canonical_url("http://openstax.org/books/principles-economics-3e/pages/1-introduction#main"),
            "https://openstax.org/books/principles-economics-3e/pages/1-introduction",
        )

    def test_json_array_legacy_manifest_is_read_as_json_not_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('[{"url":"https://example.org/book"}]', encoding="utf-8")
            self.assertEqual(
                load_jsonl(path),
                [{"url": "https://example.org/book"}],
            )


if __name__ == "__main__":
    unittest.main()
