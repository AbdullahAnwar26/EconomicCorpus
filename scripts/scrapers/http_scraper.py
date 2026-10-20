from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.utilities.file_utils import ensure_dir
from scripts.utilities.hash_utils import sha256_file
from scripts.utilities.logging_utils import get_logger


logger = get_logger("http_scraper")


class HTTPScraper:

    def __init__(self, timeout=60):
        self.timeout = timeout

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": (
                "EconomicCorpusResearchBot/1.0 "
                "(research data collection)"
            )
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        reraise=True,
    )
    def download(self, url, output_path):

        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        temp_path = output_path.with_suffix(
            output_path.suffix + ".part"
        )

        logger.info(f"Downloading: {url}")

        with self.session.get(
            url,
            stream=True,
            timeout=self.timeout
        ) as response:

            response.raise_for_status()

            with temp_path.open("wb") as f:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:
                        f.write(chunk)

        temp_path.replace(output_path)

        checksum = sha256_file(output_path)

        logger.info(
            f"Downloaded: {output_path} | SHA256: {checksum}"
        )

        return {
            "url": url,
            "path": str(output_path),
            "sha256": checksum,
            "status": "success"
        }
