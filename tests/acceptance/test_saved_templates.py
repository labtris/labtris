"""Saving a configured QEMU node as a template, over the real API.

The unit tests cover the disk work. These cover the promises around it: that a
running node is refused, that the sizing survives into nodes made from the
template, and that deleting a template cannot pull an image out from under a
node still booting from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app

OWNER = User(id="01TMPLOWNER000000000001", name="Owner", username="tmplowner", role="user")


@pytest.fixture()
async def clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from labtris_api.db import engine as db_engine
    from labtris_api.models import User as UserRow
    from labtris_api.runtime import qemu

    # Keep the flattening entirely inside the test's tmp_path: no real qemu-img,
    # no writes to the host's image cache.
    cache = tmp_path / "cache"
    cache.mkdir()
    vms = tmp_path / "vms"
    monkeypatch.setattr(qemu, "_cache_dir", lambda: cache)
    monkeypatch.setattr(qemu, "_vm_dir", lambda node_id: vms / node_id)

    async def fake_run(*args: str, timeout: float | None = None):
        Path(args[-1]).write_bytes(b"flattened-" + args[-2].encode())
        return 0, ""

    monkeypatch.setattr(qemu, "_run", fake_run)

    seed_engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(seed_engine, expire_on_commit=False)() as session:
        if await session.get(UserRow, OWNER.id) is None:
            session.add(
                UserRow(id=OWNER.id, username=OWNER.username, display_name=OWNER.name,
                        password_hash="scrypt$00$00", role=OWNER.role)
            )
            await session.commit()
    await seed_engine.dispose()

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: OWNER

    def client() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield client, vms
    await engine.dispose()
    await db_engine.dispose()


def _name(p: str = "tmpl") -> str:
    return f"{p}-{ulid.new().str[-8:].lower()}"


async def _lab_with_qemu_node(c: AsyncClient, vms: Path, **node) -> tuple[dict, dict]:
    lab = (await c.post("/api/v1/labs", json={"name": _name("lab")})).json()
    body = {"name": "vm1", "runtime": "qemu", "image": "cirros", **node}
    r = await c.post(f"/api/v1/labs/{lab['id']}/nodes", json=body)
    assert r.status_code == 201, r.text
    n = r.json()
    # Stand in for "this node has booted at least once".
    (vms / n["id"]).mkdir(parents=True, exist_ok=True)
    (vms / n["id"] / "disk.qcow2").write_bytes(b"overlay")
    return lab, n


async def test_saving_a_node_records_a_flattened_image_not_the_base(clients) -> None:
    client, vms = clients
    async with client() as c:
        lab, node = await _lab_with_qemu_node(c, vms, ram_mb=4096, nic_model="e1000")

        r = await c.post(f"/api/v1/nodes/{node['id']}/export", json={"name": _name()})
        assert r.status_code == 201, r.text
        tmpl = r.json()

        # The point of the whole feature: not "cirros" again.
        assert tmpl["image"].startswith("custom:")
        assert tmpl["image"] != "cirros"
        assert tmpl["spec"]["from_image"] == "cirros"
        # And the facts a catalog entry would have supplied, which this image
        # no longer has one of.
        assert tmpl["spec"]["ram_mb"] == 4096
        assert tmpl["spec"]["nic_model"] == "e1000"

        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_node_from_the_template_inherits_its_sizing(clients) -> None:
    """Dragging a saved 4 GB appliance onto the canvas must not boot it on the
    256 MB default with a NIC its kernel cannot drive."""
    client, vms = clients
    async with client() as c:
        lab, node = await _lab_with_qemu_node(c, vms, ram_mb=4096, nic_model="e1000")
        tmpl = (
            await c.post(f"/api/v1/nodes/{node['id']}/export", json={"name": _name()})
        ).json()

        lab2 = (await c.post("/api/v1/labs", json={"name": _name("lab")})).json()
        r = await c.post(
            f"/api/v1/labs/{lab2['id']}/nodes",
            json={"name": "restored", "runtime": "qemu", "image": tmpl["image"]},
        )
        assert r.status_code == 201, r.text
        made = r.json()

        assert made["ram_mb"] == 4096
        assert made["nic_model"] == "e1000"

        await c.delete(f"/api/v1/labs/{lab['id']}")
        await c.delete(f"/api/v1/labs/{lab2['id']}")


async def test_an_explicit_size_still_wins(clients) -> None:
    """The template is a default, not a floor — someone may want it smaller."""
    client, vms = clients
    async with client() as c:
        lab, node = await _lab_with_qemu_node(c, vms, ram_mb=4096)
        tmpl = (
            await c.post(f"/api/v1/nodes/{node['id']}/export", json={"name": _name()})
        ).json()

        lab2 = (await c.post("/api/v1/labs", json={"name": _name("lab")})).json()
        made = (
            await c.post(
                f"/api/v1/labs/{lab2['id']}/nodes",
                json={"name": "small", "runtime": "qemu",
                      "image": tmpl["image"], "ram_mb": 1024},
            )
        ).json()
        assert made["ram_mb"] == 1024

        await c.delete(f"/api/v1/labs/{lab['id']}")
        await c.delete(f"/api/v1/labs/{lab2['id']}")


async def test_a_template_in_use_cannot_be_deleted(clients) -> None:
    """Removing the image under a node that still boots from it would break
    that node at its next start, a long way from this action."""
    client, vms = clients
    async with client() as c:
        lab, node = await _lab_with_qemu_node(c, vms)
        tmpl = (
            await c.post(f"/api/v1/nodes/{node['id']}/export", json={"name": _name()})
        ).json()

        lab2 = (await c.post("/api/v1/labs", json={"name": _name("lab")})).json()
        await c.post(
            f"/api/v1/labs/{lab2['id']}/nodes",
            json={"name": "user", "runtime": "qemu", "image": tmpl["image"]},
        )

        r = await c.delete(f"/api/v1/templates/{tmpl['id']}")
        assert r.status_code == 409, r.text
        assert "still use this image" in r.text
        # Named, so the person knows what to go and remove.
        assert "user" in r.text

        # Once the user is gone, it goes — and takes its disk with it.
        await c.delete(f"/api/v1/labs/{lab2['id']}")
        digest = tmpl["image"].split(":", 1)[1]
        assert (await c.delete(f"/api/v1/templates/{tmpl['id']}")).status_code == 204

        from labtris_api.runtime import qemu

        assert not (qemu._cache_dir() / f"custom-{digest}.qcow2").exists()
        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_docker_node_still_saves_as_a_plain_reference(clients) -> None:
    """Flattening is a QEMU concern. A Docker image reference already names
    immutable content, so nothing should be copied."""
    client, vms = clients
    async with client() as c:
        lab = (await c.post("/api/v1/labs", json={"name": _name("lab")})).json()
        node = (
            await c.post(
                f"/api/v1/labs/{lab['id']}/nodes",
                json={"name": "c1", "runtime": "docker", "image": "alpine:3.20"},
            )
        ).json()

        tmpl = (
            await c.post(f"/api/v1/nodes/{node['id']}/export", json={"name": _name()})
        ).json()

        assert tmpl["image"] == "alpine:3.20"
        assert tmpl["spec"] == {}

        await c.delete(f"/api/v1/labs/{lab['id']}")
