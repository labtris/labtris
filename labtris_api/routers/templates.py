from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import conflict, not_found
from labtris_api.models import Node, Template
from labtris_api.schemas import TemplateOut, TemplatePatch

router = APIRouter(tags=["templates"])


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> list[Template]:
    result = await session.execute(select(Template).order_by(Template.name))
    return list(result.scalars())


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def patch_template(
    template_id: str,
    body: TemplatePatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Template:
    """Update a template's editable fields.

    Scalar fields on the row (name/icon/description) update directly; the
    per-spec knobs (RAM, CPUs, nic-model, disk-bus, iface-scheme, graphical)
    merge into `spec` so unset fields keep their old values. Anything the
    caller didn't send is left alone.
    """
    tmpl = await session.get(Template, template_id)
    if tmpl is None:
        raise not_found(f"template {template_id} not found")

    data = body.model_dump(exclude_unset=True)

    for scalar in ("name", "icon", "description"):
        if scalar in data:
            setattr(tmpl, scalar, data.pop(scalar))

    if data:
        # Spec is JSONB; SQLAlchemy only tracks whole-column replacement, so
        # rebuild the dict and assign back rather than mutating in place.
        spec = dict(tmpl.spec or {})
        for key, value in data.items():
            # qemu_opts is itself a dict — deep-merge rather than replace so
            # setting {"cpu":"host"} does not wipe an existing machine or
            # boot-order override that lives alongside it. Every other
            # value is a scalar and just gets written through.
            if key == "qemu_opts" and isinstance(value, dict):
                spec["qemu_opts"] = {**(spec.get("qemu_opts") or {}), **value}
            else:
                spec[key] = value
        tmpl.spec = spec

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"template name {body.name!r} already exists") from exc

    await session.refresh(tmpl)
    return tmpl


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    """Delete a template, and the image it owns if nothing else needs it.

    A saved QEMU template owns a flattened multi-GB disk that nothing else on
    the host references by name. Deleting the row and leaving the file is a
    slow disk leak nobody attributes to this; deleting the file while a node
    still boots from it breaks that node at its next start. So: refuse while
    nodes use it, and only then remove both.
    """
    from pathlib import Path

    from labtris_api.runtime.qemu import CUSTOM_PREFIX, _custom_path

    tmpl = await session.get(Template, template_id)
    if tmpl is None:
        raise not_found(f"template {template_id} not found")

    image = tmpl.image
    owns_disk = image.startswith(CUSTOM_PREFIX)

    if owns_disk:
        users = (
            await session.execute(select(Node).where(Node.image == image))
        ).scalars().all()
        if users:
            names = ", ".join(sorted(n.name for n in users)[:5])
            more = f" and {len(users) - 5} more" if len(users) > 5 else ""
            raise conflict(
                f"{len(users)} node(s) still use this image ({names}{more}). "
                "Delete them first, or keep the template."
            )

    # Note the companion paths BEFORE the delete so we can ref-count
    # them after the commit — a JSON dict from a deleted row is fine to
    # read from `tmpl.spec` still, but querying "any other template
    # pointing at this path" after the commit gives the true answer.
    bios_path = (tmpl.spec or {}).get("bios")
    cdrom_path = (tmpl.spec or {}).get("cdrom")

    await session.delete(tmpl)
    await session.commit()

    if owns_disk:
        # After the commit: a file removed before it would be gone even if the
        # delete rolled back. Another template may share the image — identical
        # bytes get one file — so only the last one out removes it.
        still = (
            await session.execute(select(Template).where(Template.image == image))
        ).scalars().first()
        if still is None:
            with contextlib.suppress(OSError):
                Path(_custom_path(image[len(CUSTOM_PREFIX) :])).unlink(missing_ok=True)

    # Companion files: same ref-count. A given BIOS or CD-ROM can back
    # more than one template (share the same OVMF-sata.fd across all
    # SATA-boot appliances, or the same NX-OS config CD across every
    # NX-OSv release). Only remove the file when nobody points at it.
    # The `spec->>` JSON accessor is dialect-specific to PostgreSQL,
    # which is Labtris's only supported DB.
    for path_str in (bios_path, cdrom_path):
        if not path_str:
            continue
        p = Path(path_str)
        # Look for any template whose spec.bios or spec.cdrom equals
        # this path. If none, unlink. Uses raw JSONB text-extract, not
        # the ORM (Template.spec is opaque JSONB to SQLAlchemy).
        from sqlalchemy import text

        still_ref = (
            await session.execute(
                text(
                    "SELECT 1 FROM templates "
                    "WHERE spec->>'bios' = :p OR spec->>'cdrom' = :p LIMIT 1"
                ),
                {"p": path_str},
            )
        ).scalar_one_or_none()
        if still_ref is None:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
    return Response(status_code=204)
