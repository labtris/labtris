"""Every third-party import must be a declared dependency.

PyJWT was imported by auth.py from the moment authentication landed and was
never listed in pyproject.toml. It arrived on the development machine as a
side effect of something else, so every test passed, the app ran, and the
first genuinely clean install died on `import jwt` — during migrations, on a
machine nobody could reach, with the failure surfacing as a systemd unit that
would not start.

The failure mode is what makes this worth a test: an undeclared dependency is
invisible exactly where you develop and fatal exactly where you deploy.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ("labtris_api", "labtris_netd", "labtris_mcp")

#: Import name to distribution name, for the ones that differ.
DISTRIBUTION = {
    "jwt": "pyjwt",
    "yaml": "pyyaml",
    "ulid": "ulid-py",
    "pydantic_settings": "pydantic-settings",
}


def _declared() -> set[str]:
    """Distribution names pinned in pyproject, normalised to lowercase."""
    names = set()
    for line in (ROOT / "pyproject.toml").read_text().splitlines():
        line = line.strip().strip('",')
        if line and line[0].isalpha() and ("==" in line or ">=" in line):
            names.add(line.split("[")[0].split("=")[0].split(">")[0].lower())
    return names


def _imported() -> dict[str, str]:
    """Top-level third-party module -> the first file importing it."""
    found: dict[str, str] = {}
    for package in PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module.split(".")[0]]
                else:
                    continue
                for module in modules:
                    if module in sys.stdlib_module_names or module.startswith("labtris"):
                        continue
                    found.setdefault(module, str(path.relative_to(ROOT)))
    return found


@pytest.mark.parametrize(("module", "source"), sorted(_imported().items()))
def test_every_third_party_import_is_declared(module: str, source: str) -> None:
    distribution = DISTRIBUTION.get(module, module).lower()

    assert distribution in _declared(), (
        f"{source} imports {module!r}, which needs {distribution!r} in "
        "pyproject.toml. It may already be installed here by accident; a clean "
        "install will fail."
    )


def test_pyjwt_specifically_is_pinned() -> None:
    """The one that got away, kept as a named regression rather than trusting
    the general check to keep covering it."""
    assert "pyjwt" in _declared()


def test_the_application_can_actually_be_constructed() -> None:
    """The check the import scan cannot do.

    python-multipart is never imported by our code — FastAPI reaches for it
    when a route declares UploadFile, and raises at app construction if it is
    absent. So a scan of import statements finds nothing wrong, every test
    passes in an environment where it happens to be installed, and a clean
    machine answers 502.

    Building the app exercises every route's dependency graph, which is where
    that class of failure lives. It is still only as honest as the environment
    it runs in: the real proof is installing into an empty venv, which is what
    the ISO does on every boot."""
    from labtris_api.main import create_app

    app = create_app()

    assert app.routes, "the app was built with no routes"


def test_upload_routes_have_their_parser() -> None:
    """backup.py takes an UploadFile. Without python-multipart that route
    cannot be built at all, and it takes the whole app down with it."""
    assert "python-multipart" in _declared()
