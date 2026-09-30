"""
Packages the bundled Developer SDK (operations-manager/sdk/) into a
downloadable zip on demand.

The zip served by GET /v1/sdk/download is built fresh from whatever SDK
source shipped in this image, rather than from a prebuilt artifact someone
could forget to rebuild - so the download always matches the API surface
this running appliance actually exposes.
"""
import io
import re
import zipfile
from pathlib import Path

# operations-manager/app/sdk_packaging.py -> operations-manager/sdk
SDK_SOURCE_DIR = Path(__file__).resolve().parent.parent / "sdk"

_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "dist", "build"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _iter_source_files():
    if not SDK_SOURCE_DIR.exists():
        return
    for path in sorted(SDK_SOURCE_DIR.rglob("*")):
        if path.is_dir():
            continue
        if any(part in _EXCLUDE_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix in _EXCLUDE_SUFFIXES:
            continue
        yield path


def sdk_available() -> bool:
    return SDK_SOURCE_DIR.exists() and any(_iter_source_files())


def sdk_version() -> str:
    """Single source of truth: parse `__version__` out of aom_sdk/__init__.py
    rather than hand-copying it into a second constant that can drift."""
    init_file = SDK_SOURCE_DIR / "aom_sdk" / "__init__.py"
    try:
        text = init_file.read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0.0.0"


def sdk_filename() -> str:
    return f"couchbase-aom-sdk-{sdk_version()}.zip"


def build_sdk_archive() -> bytes:
    """Zip the entire sdk/ source tree (package, examples, README,
    pyproject) into an in-memory archive, rooted at a single top-level
    folder so unzipping doesn't scatter files into the caller's cwd."""
    buffer = io.BytesIO()
    root_name = f"couchbase-aom-sdk-{sdk_version()}"
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _iter_source_files():
            arcname = f"{root_name}/{path.relative_to(SDK_SOURCE_DIR).as_posix()}"
            zf.write(path, arcname)
    return buffer.getvalue()
