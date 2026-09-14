from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.config import settings as env_settings
from labtris_api.db import get_session
from labtris_api.errors import bad_request
from labtris_api.models import Setting
from labtris_api.schemas import SettingsPatch

router = APIRouter(tags=["settings"])

#: Which pane of the settings window a key belongs in. Declared here rather
#: than in the UI so there is one list: a key added to this file appears in the
#: right place without a second edit somewhere else that will be forgotten.
SECTIONS: list[dict[str, str]] = [
    {"id": "general", "label": "General"},
    {"id": "assistant", "label": "Assistant"},
    {"id": "virtualisation", "label": "Virtualisation"},
]

#: What may be changed from the UI, and what changing it costs. Deliberately a
#: whitelist: database_url and netd_socket are how this process reaches its own
#: state, and a browser being able to repoint them is a way to lose a lab, not
#: a feature.
EDITABLE: dict[str, dict[str, Any]] = {
    "llm_base_url": {"section": "assistant","label": "LiteLLM base URL", "kind": "text", "live": True},
    "llm_api_key": {"section": "assistant","label": "LiteLLM API key", "kind": "secret", "live": True},
    "llm_model": {"section": "assistant","label": "Model", "kind": "text", "live": True},
    "llm_max_steps": {"section": "assistant","label": "Tool-call ceiling per turn (runaway backstop)", "kind": "int", "live": True},
    "self_url": {"section": "general","label": "This instance's own URL", "kind": "text", "live": True},
    "qemu_accel": {
        "section": "virtualisation",
        "label": "QEMU acceleration",
        "kind": "choice",
        "choices": ["tcg", "kvm"],
        "live": False,
        "note": "kvm needs /dev/kvm and a host where nested virtualisation actually executes",
    },
    "qemu_image_cache_dir": {
        "section": "virtualisation",
        "label": "Image cache directory",
        "kind": "text",
        "live": False,
        "note": "existing downloads are not moved",
    },
    "qemu_vm_dir": {"section": "virtualisation","label": "VM state directory", "kind": "text", "live": False},
}

SECRET_KEYS = {k for k, v in EDITABLE.items() if v["kind"] == "secret"}


async def overrides(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars()
    return {r.key: r.value for r in rows if r.key in EDITABLE}


async def apply_overrides(session: AsyncSession) -> None:
    """Push stored values onto the live settings object.

    Called on startup and after every write, so a change marked `live` takes
    effect without a restart rather than only being recorded."""
    for key, value in (await overrides(session)).items():
        if EDITABLE[key].get("live") and hasattr(env_settings, key):
            setattr(env_settings, key, value)


@router.get("/settings")
async def read_settings(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Effective configuration, and where each value came from.

    A key pinned by an environment variable is reported as such and marked
    read-only, so the UI does not offer an edit that would be silently
    overridden on the next restart."""
    stored = await overrides(session)
    out = []
    for key, meta in EDITABLE.items():
        env_name = f"LABTRIS_{key.upper()}"
        pinned = env_name in os.environ
        value = getattr(env_settings, key, None)
        out.append(
            {
                "key": key,
                **{k: v for k, v in meta.items() if k != "live"},
                "live": meta.get("live", False),
                "value": "" if key in SECRET_KEYS else value,
                "set": bool(value) if key in SECRET_KEYS else None,
                "source": "env" if pinned else ("stored" if key in stored else "default"),
                "editable": not pinned,
            }
        )
    return {"settings": out, "sections": SECTIONS}


@router.patch("/settings")
async def write_settings(
    body: SettingsPatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    changed, refused = [], []
    for key, value in body.values.items():
        if key not in EDITABLE:
            raise bad_request(f"{key!r} is not a configurable setting")
        if f"LABTRIS_{key.upper()}" in os.environ:
            # Refused rather than stored-and-ignored: a value that looks saved
            # and does nothing is worse than being told no.
            refused.append(key)
            continue
        if EDITABLE[key]["kind"] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise bad_request(f"{key} must be a whole number") from None
        choices = EDITABLE[key].get("choices")
        if choices and value not in choices:
            raise bad_request(f"{key} must be one of {', '.join(choices)}")
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        changed.append(key)
    await session.commit()
    await apply_overrides(session)
    return {
        "changed": changed,
        "refused_pinned_by_env": refused,
        "restart_required": sorted(k for k in changed if not EDITABLE[k].get("live")),
    }
