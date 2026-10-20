from pathlib import Path

RAW_ROOT = Path("data/raw")
PROCESSED_ROOT = Path("data/processed")
DOWNLOAD_ROOT = Path("data/downloads")


def source_raw_dir(source_id):
    path = RAW_ROOT / source_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_processed_dir(source_id):
    path = PROCESSED_ROOT / source_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_dir(source_id):
    path = DOWNLOAD_ROOT / source_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_storage_paths(source_id):
    return {
        "raw": source_raw_dir(source_id),
        "processed": source_processed_dir(source_id),
        "downloads": download_dir(source_id),
    }
