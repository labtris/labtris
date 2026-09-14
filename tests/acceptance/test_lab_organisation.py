"""Folders, moving, cloning, and who may freeze a lab.

Cloning is where the risk is. A lab is not just rows: its interfaces hold MACs
reserved against a global registry, its networks hold host device names, and
its nodes may hold references to live containers. A copy that duplicated any of
those would look fine in the database and collide the moment both labs ran.
"""

from __future__ import annotations

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app

OWNER = User(id="01FOLDEROWNER00000000001", name="Owner", username="folderowner", role="user")
OTHER = User(id="01FOLDEROTHER00000000001", name="Other", username="folderother", role="user")


@pytest.fixture()
async def clients():
    from labtris_api.db import engine as db_engine
    from labtris_api.models import User as UserRow

    seed_engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(seed_engine, expire_on_commit=False)() as session:
        for who in (OWNER, OTHER):
            if await session.get(UserRow, who.id) is None:
                session.add(
                    UserRow(id=who.id, username=who.username, display_name=who.name,
                            password_hash="scrypt$00$00", role=who.role)
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

    def as_user(user: User) -> AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield as_user
    await engine.dispose()
    await db_engine.dispose()


def _name() -> str:
    return f"org-{ulid.new().str[-8:].lower()}"


async def _lab(as_user, user=OWNER, **body) -> dict:
    async with as_user(user) as c:
        r = await c.post("/api/v1/labs", json={"name": _name(), **body})
        assert r.status_code == 201, r.text
        return r.json()


async def _delete(as_user, *lab_ids, user=OWNER) -> None:
    async with as_user(user) as c:
        for lab_id in lab_ids:
            await c.delete(f"/api/v1/labs/{lab_id}")


async def test_a_lab_can_be_created_in_a_folder(clients) -> None:
    lab = await _lab(clients, folder="/CCNA/Week 1/")

    # Normalised on the way in, so the picker never shows two "CCNA"s.
    assert lab["folder"] == "CCNA/Week 1"
    await _delete(clients, lab["id"])


async def test_moving_a_lab_is_a_folder_patch(clients) -> None:
    lab = await _lab(clients, folder="Drafts")
    async with clients(OWNER) as c:
        r = await c.patch(f"/api/v1/labs/{lab['id']}", json={"folder": "CCNA//Week 2"})

    assert r.status_code == 200, r.text
    assert r.json()["folder"] == "CCNA/Week 2"
    await _delete(clients, lab["id"])


async def test_a_lab_moves_back_to_the_root(clients) -> None:
    """"" is the root, not "unset" — so it has to survive exclude_unset."""
    lab = await _lab(clients, folder="CCNA")
    async with clients(OWNER) as c:
        r = await c.patch(f"/api/v1/labs/{lab['id']}", json={"folder": ""})

    assert r.json()["folder"] == ""
    await _delete(clients, lab["id"])


async def test_listing_filters_to_one_folder_exactly(clients) -> None:
    """Not a prefix match: opening CCNA shows what is in CCNA, and
    CCNA/Week 1 is reached by opening that in turn. A prefix would flatten the
    tree the folders exist to create."""
    top = await _lab(clients, folder="CCNAX")
    nested = await _lab(clients, folder="CCNAX/Week 1")
    async with clients(OWNER) as c:
        listed = (await c.get("/api/v1/labs", params={"folder": "CCNAX"})).json()

    names = [x["name"] for x in listed]
    assert top["name"] in names
    assert nested["name"] not in names
    await _delete(clients, top["id"], nested["id"])


async def test_folders_are_derived_from_labs_not_stored(clients) -> None:
    """So there is no such thing as an empty folder left behind by a move."""
    lab = await _lab(clients, folder="Ephemeral")
    async with clients(OWNER) as c:
        before = (await c.get("/api/v1/folders")).json()["folders"]
        await c.patch(f"/api/v1/labs/{lab['id']}", json={"folder": ""})
        after = (await c.get("/api/v1/folders")).json()["folders"]

    assert "Ephemeral" in [f["path"] for f in before]
    assert "Ephemeral" not in [f["path"] for f in after]
    await _delete(clients, lab["id"])


async def test_a_clone_gets_its_own_macs(clients) -> None:
    """The one that would have been silently wrong: MACs are reserved against
    a global registry, so a copied MAC means two labs that cannot both run."""
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        await c.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "n1", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}, {}]},
        )
        clone = (await c.post(f"/api/v1/labs/{lab['id']}/clone", json={})).json()
        src = (await c.get(f"/api/v1/labs/{lab['id']}")).json()
        dst = (await c.get(f"/api/v1/labs/{clone['id']}")).json()

    src_macs = {i["mac"] for n in src["nodes"] for i in n["interfaces"]}
    dst_macs = {i["mac"] for n in dst["nodes"] for i in n["interfaces"]}

    assert len(dst_macs) == 2
    assert not (src_macs & dst_macs)
    await _delete(clients, lab["id"], clone["id"])


async def test_a_clone_is_defined_not_running(clients) -> None:
    """A copied runtime ref would point the clone's stop button at the
    original's container."""
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        await c.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "n1", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}]},
        )
        clone = (await c.post(f"/api/v1/labs/{lab['id']}/clone", json={})).json()
        dst = (await c.get(f"/api/v1/labs/{clone['id']}")).json()

    assert [n["state"] for n in dst["nodes"]] == ["defined"]
    assert all(n["runtime_ref"] is None for n in dst["nodes"])
    await _delete(clients, lab["id"], clone["id"])


async def test_a_clone_keeps_the_topology(clients) -> None:
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        for n in ("a", "b"):
            await c.post(
                f"/api/v1/labs/{lab['id']}/nodes",
                json={"name": n, "runtime": "docker", "image": "alpine:3.20",
                      "interfaces": [{}]},
            )
        src = (await c.get(f"/api/v1/labs/{lab['id']}")).json()
        a, b = src["nodes"][0]["interfaces"][0], src["nodes"][1]["interfaces"][0]
        await c.post(f"/api/v1/labs/{lab['id']}/links",
                     json={"a_iface_id": a["id"], "b_iface_id": b["id"]})
        clone = (await c.post(f"/api/v1/labs/{lab['id']}/clone", json={})).json()
        dst = (await c.get(f"/api/v1/labs/{clone['id']}")).json()

    assert sorted(n["name"] for n in dst["nodes"]) == ["a", "b"]
    assert len(dst["links"]) == 1
    # The link must join the *clone's* interfaces, not reach back into the original.
    clone_ifaces = {i["id"] for n in dst["nodes"] for i in n["interfaces"]}
    assert dst["links"][0]["a_iface_id"] in clone_ifaces
    assert dst["links"][0]["b_iface_id"] in clone_ifaces
    await _delete(clients, lab["id"], clone["id"])


async def test_a_clone_belongs_to_whoever_made_it(clients) -> None:
    """Cloning someone's lab to learn from it must not enlarge their estate."""
    lab = await _lab(clients, user=OWNER)
    async with clients(OTHER) as c:
        clone = (await c.post(f"/api/v1/labs/{lab['id']}/clone", json={})).json()
        listing = (await c.get("/api/v1/labs", params={"mine": True})).json()

    assert clone["name"] in [x["name"] for x in listing]
    await _delete(clients, clone["id"], user=OTHER)
    await _delete(clients, lab["id"])


async def test_a_clone_can_be_named_and_placed(clients) -> None:
    lab = await _lab(clients, folder="CCNA")
    wanted = _name()
    async with clients(OWNER) as c:
        clone = (
            await c.post(f"/api/v1/labs/{lab['id']}/clone",
                         json={"name": wanted, "folder": "/CCNA/Attempt 2/"})
        ).json()

    assert clone["name"] == wanted
    assert clone["folder"] == "CCNA/Attempt 2"
    await _delete(clients, lab["id"], clone["id"])


async def test_only_the_owner_may_lock_a_lab(clients) -> None:
    """A lock nobody but the locker can undo is worse than a delete — there is
    no way back without an admin. #64 gated destruction and missed this."""
    lab = await _lab(clients, user=OWNER)
    async with clients(OTHER) as c:
        r = await c.post(f"/api/v1/labs/{lab['id']}/lock")

    assert r.status_code == 403, r.text
    await _delete(clients, lab["id"])


async def test_the_owner_can_lock_and_unlock(clients) -> None:
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        locked = await c.post(f"/api/v1/labs/{lab['id']}/lock")
        unlocked = await c.post(f"/api/v1/labs/{lab['id']}/unlock")

    assert locked.json() == {"locked": True}
    assert unlocked.json() == {"locked": False}
    await _delete(clients, lab["id"])


async def test_renaming_a_folder_leaves_other_peoples_labs_alone(clients) -> None:
    """Renaming a shared folder must not drag a colleague's work somewhere
    they did not ask for."""
    mine = await _lab(clients, user=OWNER, folder="Shared")
    theirs = await _lab(clients, user=OTHER, folder="Shared")
    async with clients(OWNER) as c:
        result = (await c.post("/api/v1/folders/rename",
                               json={"from": "Shared", "to": "Renamed"})).json()
        after_mine = (await c.get(f"/api/v1/labs/{mine['id']}")).json()
        after_theirs = (await c.get(f"/api/v1/labs/{theirs['id']}")).json()

    assert result["moved"] == 1
    assert after_mine["folder"] == "Renamed"
    assert after_theirs["folder"] == "Shared"
    await _delete(clients, mine["id"])
    await _delete(clients, theirs["id"], user=OTHER)


async def test_the_root_folder_cannot_be_renamed(clients) -> None:
    async with clients(OWNER) as c:
        r = await c.post("/api/v1/folders/rename", json={"from": "", "to": "Everything"})

    assert r.status_code == 400, r.text


async def test_the_lab_list_carries_node_counts(clients) -> None:
    """The lab switcher shows "3 nodes" beside every lab at once. Without the
    counts in the list response the UI has to fetch each lab to label it, which
    is a request per lab on every keystroke of the switcher's filter."""
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        listed = (await c.get("/api/v1/labs")).json()
        empty = next(row for row in listed if row["id"] == lab["id"])
        assert (empty["nodes"], empty["running"]) == (0, 0)

        for name in ("n1", "n2"):
            await c.post(
                f"/api/v1/labs/{lab['id']}/nodes",
                json={"name": name, "runtime": "docker", "image": "alpine:3.20"},
            )
        listed = (await c.get("/api/v1/labs")).json()
        row = next(r for r in listed if r["id"] == lab["id"])

    # Defined, not running: a node that exists is not a node that is up, and
    # the switcher distinguishes them.
    assert row["nodes"] == 2
    assert row["running"] == 0
    await _delete(clients, lab["id"])


async def test_the_addresses_map_answers_for_a_lab_with_nothing_running(clients) -> None:
    """The endpoint 500'd on its first real call because a name it used was
    never imported — which no unit test could catch, since the failure is in
    wiring the route rather than in the parsing it delegates to."""
    lab = await _lab(clients)
    async with clients(OWNER) as c:
        await c.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "n1", "runtime": "docker", "image": "alpine:3.20"},
        )
        resp = await c.get(f"/api/v1/labs/{lab['id']}/addresses")

    assert resp.status_code == 200
    # A defined-but-not-running node is absent rather than present-and-empty:
    # "we did not ask" and "it has none" are different answers.
    assert resp.json() == {}
    await _delete(clients, lab["id"])



def test_the_console_refuses_a_stopped_node_instead_of_failing_at_the_socket() -> None:
    """A stopped QEMU node keeps its runtime_ref: that is the VM's directory,
    not a handle to a live process. Guarding on runtime_ref alone let the
    handler fall through to the serial attach and fail there, and a bare close
    is indistinguishable to the browser from a session that dropped — so it
    reconnected every three seconds, forever, for as long as the tab was open.

    Synchronous and TestClient-based because the async client the rest of this
    file uses has no websocket transport.
    """
    from fastapi.testclient import TestClient

    app = _sign_in_sync(create_app())
    with TestClient(app) as client:
        lab = client.post("/api/v1/labs", json={"name": f"ws-{ulid.new()}"}).json()
        node = client.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "vm1", "runtime": "qemu", "image": "cirros"},
        ).json()
        try:
            with client.websocket_connect(f"/api/v1/nodes/{node['id']}/console/ws") as ws:
                # The reason arrives as text before the close, so the pane has
                # something to show rather than an unexplained disconnection.
                assert "not running" in ws.receive_text()
        finally:
            client.delete(f"/api/v1/labs/{lab['id']}")


def _sign_in_sync(application):
    application.dependency_overrides[get_current_user] = lambda: OWNER
    return application
