from __future__ import annotations

import io
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api import backup, gdrive
from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.main import create_app


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def client():
    if not await _can_db():
        pytest.skip("Postgres is not available (start with `make dev-db`)")
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await engine.dispose()
    await db_engine.dispose()


async def test_a_backup_round_trips_a_lab_with_several_links(client: AsyncClient) -> None:
    """Restore is the half nobody tests until they need it.

    Several links on purpose: the generated name for a link's implicit network
    used the *head* of a ULID, which is the millisecond timestamp, so two links
    created in the same instant collided on (lab_id, name) and the restore
    failed on its own export."""
    name = f"bk-{ulid.new().str[-8:].lower()}"
    lab = (await client.post("/api/v1/labs", json={"name": name})).json()
    nodes = []
    for i in range(4):
        r = await client.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": f"n{i}", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}, {}]},
        )
        nodes.append(r.json())
    for a, b in ((0, 1), (1, 2), (2, 3)):
        r = await client.post(
            f"/api/v1/labs/{lab['id']}/links",
            json={"a_iface_id": nodes[a]["interfaces"][1]["id"],
                  "b_iface_id": nodes[b]["interfaces"][0]["id"]},
        )
        assert r.status_code == 201, r.text

    blob = (await client.get("/api/v1/backup")).content
    manifest = backup.read_manifest(blob)
    assert manifest["format"] == backup.ARCHIVE_VERSION
    assert name in [entry["name"] for entry in manifest["labs"]]
    assert any(p["lab"]["name"] == name for p in backup.labs_in(blob))

    # Restore the way it is actually used: the lab is gone, and the archive is
    # what brings it back. Deliberately not mode=clone — the archive holds
    # every lab on the instance, so cloning it here would duplicate the whole
    # database on each run, which is the very bug this file now guards.
    await client.delete(f"/api/v1/labs/{lab['id']}")

    r = await client.post(
        "/api/v1/backup/restore",
        files={"file": ("labtris-backup.tar.gz", io.BytesIO(blob), "application/gzip")},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert not out["failed"], out["failed"]
    assert name in out["restored"], f"our lab was not restored: {out['restored']}"

    labs = (await client.get("/api/v1/labs")).json()
    back = next(x for x in labs if x["name"] == name)
    detail = (await client.get(f"/api/v1/labs/{back['id']}")).json()
    assert len(detail["nodes"]) == 4
    assert len(detail["links"]) == 3, "every link must survive the round trip"

    await client.delete(f"/api/v1/labs/{back['id']}")


async def test_restore_rejects_something_that_is_not_a_backup(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/backup/restore",
        files={"file": ("nope.tar.gz", io.BytesIO(b"not an archive"), "application/gzip")},
    )
    assert r.status_code == 400
    assert "not a labtris backup" in r.json()["error"]["message"]


async def test_settings_can_be_stored_and_env_pins_win(client: AsyncClient, monkeypatch) -> None:
    """An env var is the floor: the UI must not be able to store something that
    a restart would silently overrule."""
    before = (await client.get("/api/v1/settings")).json()["settings"]
    assert {s["key"] for s in before} >= {"llm_base_url", "llm_model", "qemu_accel"}

    r = await client.patch(
        "/api/v1/settings", json={"values": {"llm_model": "claude-sonnet-4", "llm_max_steps": 5}}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["changed"]) == {"llm_model", "llm_max_steps"}
    assert settings.llm_model == "claude-sonnet-4", "a live setting should apply immediately"

    monkeypatch.setenv("LABTRIS_QEMU_ACCEL", "tcg")
    r = await client.patch("/api/v1/settings", json={"values": {"qemu_accel": "kvm"}})
    assert r.json()["refused_pinned_by_env"] == ["qemu_accel"]
    assert r.json()["changed"] == []

    now = {s["key"]: s for s in (await client.get("/api/v1/settings")).json()["settings"]}
    assert now["qemu_accel"]["editable"] is False
    assert now["qemu_accel"]["source"] == "env"
    assert now["llm_api_key"]["value"] == "", "a secret must never be sent back to the browser"

    await client.patch("/api/v1/settings", json={"values": {"llm_model": "gpt-4o-mini"}})


async def test_a_setting_needing_a_restart_says_so(client: AsyncClient) -> None:
    r = await client.patch("/api/v1/settings", json={"values": {"qemu_vm_dir": "/tmp/labtris-vms"}})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] == ["qemu_vm_dir"]
    await client.patch(
        "/api/v1/settings", json={"values": {"qemu_vm_dir": "~/.local/share/labtris/qemu-vms"}}
    )


class StubGoogle:
    """Stands in for Google's OAuth and Drive endpoints.

    Every real-Google specific — the multipart body shape, the device-flow
    grant type, the Bearer header, the refresh exchange — is asserted here
    against a server that speaks the documented protocol. What this cannot
    prove is that Google agrees with the documentation; that needs a real
    OAuth client, which is why the flow is built to be linked in a minute."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.seen: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def _json(self, payload: object, code: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append((self.path, dict(self.headers)))
                if self.path.startswith("/device"):
                    self._json({"device_code": "DEV-1", "user_code": "ABCD-EFGH",
                                "verification_url": "https://google.com/device", "interval": 5})
                elif self.path.startswith("/token"):
                    form = urllib.parse.parse_qs(raw.decode())
                    if form.get("grant_type", [""])[0].endswith("device_code"):
                        self._json({"refresh_token": "RT-1", "access_token": "AT-1"})
                    else:
                        assert form["grant_type"][0] == "refresh_token"
                        self._json({"access_token": "AT-2"})
                elif self.path.startswith("/upload"):
                    outer.uploads.append((self.headers.get("Authorization", ""), raw))
                    self._json({"id": "FILE-1", "name": "labtris-backup.tar.gz"})
                else:
                    self._json({}, 404)

            def do_GET(self) -> None:
                outer.seen.append((self.path, dict(self.headers)))
                if "alt=media" in self.path:
                    body = outer.uploads[-1][1]
                    blob = body.split(b"\r\n\r\n", 2)[2].rsplit(b"\r\n--", 1)[0]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                else:
                    self._json({"files": [{"id": "FILE-1", "name": "labtris-backup-x.tar.gz",
                                           "size": "123", "createdTime": "2026-01-01T00:00:00Z"}]})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def stub_google(monkeypatch):
    stub = StubGoogle()
    monkeypatch.setattr(gdrive, "OAUTH_DEVICE_URL", f"{stub.base}/device")
    monkeypatch.setattr(gdrive, "OAUTH_TOKEN_URL", f"{stub.base}/token")
    monkeypatch.setattr(gdrive, "UPLOAD_URL", f"{stub.base}/upload")
    monkeypatch.setattr(gdrive, "FILES_URL", f"{stub.base}/files")
    yield stub
    stub.stop()


async def test_the_whole_drive_round_trip(client: AsyncClient, stub_google) -> None:
    """Link, push, list, pull — against a server speaking Google's protocol."""
    assert (await client.get("/api/v1/backup/gdrive/status")).json()["configured"] in (True, False)

    r = await client.post(
        "/api/v1/backup/gdrive/credentials",
        json={"client_id": "cid", "client_secret": "secret"},
    )
    assert r.status_code == 200, r.text

    started = (await client.post("/api/v1/backup/gdrive/link")).json()
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_url"]

    done = (
        await client.post(
            "/api/v1/backup/gdrive/link/complete", json={"device_code": started["device_code"]}
        )
    ).json()
    assert done["linked"] is True
    assert (await client.get("/api/v1/backup/gdrive/status")).json()["linked"] is True

    pushed = (await client.post("/api/v1/backup/gdrive/push")).json()
    assert pushed["id"] == "FILE-1"
    assert pushed["bytes"] > 0
    auth, body = stub_google.uploads[-1]
    assert auth == "Bearer AT-2", "must use a freshly refreshed access token"
    assert b"multipart/related" not in body and b'"name"' in body

    files = (await client.get("/api/v1/backup/gdrive/list")).json()["files"]
    assert files[0]["id"] == "FILE-1"

    pulled = (await client.post("/api/v1/backup/gdrive/pull/FILE-1")).json()
    assert not pulled["failed"], pulled["failed"]
    for name in pulled["restored"]:
        labs = (await client.get("/api/v1/labs")).json()
        match = next((x for x in labs if x["name"] == name), None)
        if match:
            await client.delete(f"/api/v1/labs/{match['id']}")


async def test_drive_refuses_before_it_is_linked(client: AsyncClient, stub_google) -> None:
    from labtris_api.db import SessionLocal
    from labtris_api.models import Setting

    async with SessionLocal() as s:
        row = await s.get(Setting, "gdrive_refresh_token")
        if row is not None:
            await s.delete(row)
            await s.commit()
    r = await client.post("/api/v1/backup/gdrive/push")
    assert r.status_code == 400
    assert "not linked" in r.json()["error"]["message"]


async def test_restoring_an_instances_own_backup_does_not_multiply_its_labs(
    client: AsyncClient,
) -> None:
    """Restore used to be additive with no notion of identity, so restoring an
    instance's own archive onto itself copied every lab. Run it a few times and
    the count doubles each pass — one `demo` became 128 labs named things like
    `demo-1-1-2-1` on a box here. The export carries each lab's id, so a lab
    that is already present is left alone."""
    name = f"idem-{ulid.new().str[-8:].lower()}"
    lab = (await client.post("/api/v1/labs", json={"name": name})).json()
    await client.post(
        f"/api/v1/labs/{lab['id']}/nodes",
        json={"name": "n0", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    blob = (await client.get("/api/v1/backup")).content

    def ours(labs: list[dict[str, object]]) -> list[str]:
        return sorted(x["name"] for x in labs if str(x["name"]).startswith(name))

    before = ours((await client.get("/api/v1/labs")).json())
    assert before == [name]

    for attempt in range(3):
        r = await client.post(
            "/api/v1/backup/restore",
            files={"file": ("labtris-backup.tar.gz", io.BytesIO(blob), "application/gzip")},
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert not out["failed"], out["failed"]
        assert name in out["skipped"], f"pass {attempt}: expected a skip, got {out}"
        assert name not in out["restored"]
        assert ours((await client.get("/api/v1/labs")).json()) == [name], (
            f"pass {attempt} multiplied the lab"
        )

    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_restore_still_recreates_a_lab_that_is_genuinely_gone(
    client: AsyncClient,
) -> None:
    """Skipping what is already here must not turn restore into a no-op — the
    case it exists for is the lab that is missing."""
    name = f"gone-{ulid.new().str[-8:].lower()}"
    lab = (await client.post("/api/v1/labs", json={"name": name})).json()
    await client.post(
        f"/api/v1/labs/{lab['id']}/nodes",
        json={"name": "n0", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    blob = (await client.get("/api/v1/backup")).content
    await client.delete(f"/api/v1/labs/{lab['id']}")

    r = await client.post(
        "/api/v1/backup/restore",
        files={"file": ("labtris-backup.tar.gz", io.BytesIO(blob), "application/gzip")},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert name in out["restored"], out
    back = next(x for x in (await client.get("/api/v1/labs")).json() if x["name"] == name)
    detail = (await client.get(f"/api/v1/labs/{back['id']}")).json()
    assert len(detail["nodes"]) == 1

    await client.delete(f"/api/v1/labs/{back['id']}")
