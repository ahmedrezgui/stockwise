"""Checksums and atomic JSON/CSV publication for reproducible data snapshots."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def write_bytes(path: Path, content: bytes) -> None:
    """Replace a file only after its complete new content has been written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_manifest(path: Path, value: dict) -> None:
    content = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    write_bytes(path, content.encode("utf-8"))
