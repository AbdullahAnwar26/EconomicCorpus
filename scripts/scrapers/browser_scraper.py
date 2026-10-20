from pathlib import Path
from playwright.sync_api import sync_playwright

from scripts.utilities.file_utils import ensure_dir
from scripts.utilities.hash_utils import sha256_file
from scripts.utilities.logging_utils import get_logger

logger = get_logger("browser_scraper")


class BrowserScraper:

    def __init__(self, timeout=60000, headless=True):
        self.timeout = timeout
        self.headless = headless

    def download(self, url, output_path):
        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        logger.info(f"Browser scraping: {url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless
            )

            page = browser.new_page()
            page.set_default_timeout(self.timeout)

            try:
                page.goto(
                    url,
                    wait_until="networkidle"
                )

                html = page.content()

                output_path.write_text(
                    html,
                    encoding="utf-8"
                )

            finally:
                browser.close()

        checksum = sha256_file(output_path)

        logger.info(
            f"Browser download complete: "
            f"{output_path} | SHA256: {checksum}"
        )

        return {
            "url": url,
            "path": str(output_path),
            "sha256": checksum,
            "status": "success"
        }
