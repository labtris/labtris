"""One place the version comes from.

It was written out by hand in four: pyproject, the FastAPI app, the MCP server
banner and web/package.json. Three of them were wrong the moment the fourth was
bumped, and the one people actually read — the version the API reports, and the
one the ISO is named after — is the one that silently stayed behind.

Read from installed package metadata, falling back to parsing pyproject for a
source checkout that was never `pip install`ed. The fallback matters: the ISO
build and the handbook both run straight from a working tree.
"""

from __future__ import annotations

import re
from pathlib import Path


def _from_pyproject() -> str | None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
    except OSError:
        return None
    return match.group(1) if match else None


def _resolve() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("labtris")
    except PackageNotFoundError:
        return _from_pyproject() or "0.0.0+unknown"
    except Exception:  # noqa: BLE001 - a broken metadata store must not stop the app
        return _from_pyproject() or "0.0.0+unknown"


__version__ = _resolve()
