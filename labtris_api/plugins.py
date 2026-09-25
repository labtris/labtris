"""Plugin loader — third-party packages hook into labtris-api at startup.

Any package that declares an entry point under `labtris.plugins` is
discovered here and given a chance to mount its own routers, register
its own runtime primitives, or subscribe to lifecycle events. This is
how `labtris_cloudcompat` (Phase L) and every future cloud/vendor
adapter attaches without labtris core needing to import them.

Contract for a plugin:

    # In the plugin's pyproject.toml:
    [project.entry-points."labtris.plugins"]
    cloudcompat = "labtris_cloudcompat.plugin:register"

    # In labtris_cloudcompat/plugin.py:
    def register(app):
        \"\"\"Called once at API startup with the FastAPI app.\"\"\"
        from labtris_cloudcompat.aws import router
        app.include_router(router)

Design choices:
* Plugins are loaded synchronously at app-create time (not lifespan), so
  any router they mount is visible in the OpenAPI schema and the SPA
  redirects don't shadow their URLs. Anything that needs the DB / event
  loop / other lifespan resources hooks into the lifespan itself, not
  register().
* Failures are logged and skipped. A broken plugin must not stop labtris-
  api booting — this is the same contract we hold for hooks / images /
  ssh_proxy today.
* Plugins can be disabled by env var LABTRIS_PLUGINS_DISABLED (comma-
  separated list of entry-point names), for the "one plugin is causing a
  boot loop and I need the API up now" case.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def load_plugins(app: Any) -> None:
    """Discover every registered labtris plugin and call its `register(app)`.

    Order across plugins is the order Python's importlib.metadata yields
    them; plugins that need to run before/after another one should either
    document their preferred load order in the top-level docs or subscribe
    to a lifespan hook instead of relying on registration order.
    """
    disabled = {
        name.strip() for name in os.environ.get("LABTRIS_PLUGINS_DISABLED", "").split(",") if name.strip()
    }
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return

    try:
        eps = list(entry_points(group="labtris.plugins"))
    except TypeError:
        # Python 3.9 compat: entry_points() with group kwarg was added in 3.10.
        eps = list(entry_points().get("labtris.plugins", []))  # type: ignore[attr-defined]

    for ep in eps:
        if ep.name in disabled:
            logger.info("plugin.disabled", name=ep.name)
            continue
        try:
            plugin_register = ep.load()
            plugin_register(app)
            logger.info("plugin.registered", name=ep.name, module=ep.value)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plugin.registration_failed",
                name=ep.name,
                module=ep.value,
                error=str(exc),
            )
