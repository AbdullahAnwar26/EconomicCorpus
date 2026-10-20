from pathlib import Path
from scripts.utilities.hash_utils import sha256_file


def validate_file(path, expected_sha256=None):
    path = Path(path)

    if not path.exists():
        return {
            "valid": False,
            "reason": "file_not_found"
        }

    if not path.is_file():
        return {
            "valid": False,
            "reason": "not_a_file"
        }

    if path.stat().st_size == 0:
        return {
            "valid": False,
            "reason": "empty_file"
        }

    actual_sha256 = sha256_file(path)

    if expected_sha256 and actual_sha256 != expected_sha256:
        return {
            "valid": False,
            "reason": "sha256_mismatch",
            "expected_sha256": expected_sha256,
            "actual_sha256": actual_sha256
        }

    return {
        "valid": True,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": actual_sha256
    }
