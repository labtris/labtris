"""Every mutation is recorded, with who or what made it.

The motivation is the assistant. Once a model can exec into a node, rewrite a
config or delete a lab, "what happened to my lab" stops being a compliance
question and becomes a debugging one — and the first thing anyone asks is
whether they did it or the AI did.
"""

from __future__ import annotations

import ulid
from httpx import AsyncClient

from labtris_api import audit


async def test_a_mutation_is_recorded_and_a_read_is_not(client: AsyncClient) -> None:
    """Listing labs is not interesting, there are orders of magnitude more of
    them, and a log nobody can scan is a log nobody reads."""
    name = f"audit-{ulid.new().str[-8:].lower()}"
    created = (await client.post("/api/v1/labs", json={"name": name})).json()
    await client.get("/api/v1/labs")

    r = await client.get("/api/v1/audit", params={"limit": 50})
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]

    # Matched on route rather than lab_id: a create has no lab_id, because the
    # row did not exist when the request arrived and its id is only in the
    # response. Route, actor and time still say who made a lab and when.
    creates = [e for e in entries if e["route"] == "/api/v1/labs" and e["method"] == "POST"]
    assert creates, "the lab creation was not recorded"
    assert all(e["method"] != "GET" for e in entries), "a read was recorded"

    # The delete DOES carry the lab, because the id is in the path. And a
    # refused one is recorded too — a log that only holds what worked cannot
    # answer "why did nothing happen when I clicked that".
    await client.delete(f"/api/v1/labs/{created['id']}")
    missing = await client.delete(f"/api/v1/labs/{'0' * 26}")
    assert missing.status_code >= 400, missing.text

    after = (await client.get("/api/v1/audit", params={"limit": 30})).json()["entries"]
    assert any(
        e["method"] == "DELETE" and e["lab_id"] == created["id"] for e in after
    ), "the delete did not record which lab"
    assert any(
        e["status"] >= 400 for e in after
    ), "the refused delete was not recorded"


async def test_the_actor_and_how_they_came_are_recorded(client: AsyncClient) -> None:
    name = f"audit-{ulid.new().str[-8:].lower()}"
    created = (await client.post("/api/v1/labs", json={"name": name})).json()

    got = (await client.get("/api/v1/audit", params={"limit": 20})).json()["entries"]
    entry = next(e for e in got if e["route"] == "/api/v1/labs" and e["method"] == "POST")

    assert entry["via"] in ("human", "assistant")
    assert entry["summary"], "an entry with no summary is one nobody will read"
    assert entry["duration_ms"] is not None
    await client.delete(f"/api/v1/labs/{created['id']}")


async def test_the_summary_groups_by_route_not_path() -> None:
    """A thousand node starts should read as one kind of thing."""
    s = audit.summarise("POST", "/api/v1/nodes/{node_id}/start", 200)

    assert "{node_id}" in s
    assert "created" in s


def test_a_refusal_says_so_in_the_summary() -> None:
    assert "refused (403)" in audit.summarise("DELETE", "/api/v1/labs/{lab_id}", 403)


def test_secrets_are_never_stored() -> None:
    """An audit entry exists to say what happened, not to become the next
    place a credential leaks from."""
    out = audit.redact(
        {
            "name": "lab",
            "password": "hunter2",
            "api_token": "cfat_live",
            "Authorization": "Bearer x",
            "nested_secret_key": "s3cr3t",
        }
    )

    assert out["name"] == "lab"
    for k in ("password", "api_token", "Authorization", "nested_secret_key"):
        assert out[k] == "«redacted»", k
    assert "hunter2" not in str(out)
    assert "cfat_live" not in str(out)


def test_bulk_is_summarised_rather_than_stored() -> None:
    """A topology import or a startup config is tens of kilobytes, and a
    hundred of those is a table nobody can query."""
    out = audit.redact({"content": "x" * 50_000, "nodes": list(range(400))})

    assert len(out["content"]) < 300
    assert "50000 chars" in out["content"]
    assert out["nodes"] == "[400 items]"


def test_the_auth_endpoints_are_never_audited() -> None:
    """They carry passwords in the body, and a log that stores them is worse
    than no log. The audit reader is excluded too — reading the log is not an
    event in the log."""
    for path in ("/api/v1/auth/login", "/api/v1/auth/setup",
                 "/api/v1/users/abc/password", "/api/v1/audit"):
        assert audit._SKIP.match(path), path  # noqa: SLF001
    for path in ("/api/v1/labs", "/api/v1/nodes/x/start", "/api/v1/users"):
        assert not audit._SKIP.match(path), path  # noqa: SLF001


def test_the_actor_is_read_from_the_token_not_a_header() -> None:
    """An audit log that can be told who made a write is not an audit log.

    The claim lives inside the signed JWT, so a caller cannot relabel its own
    writes as human — nothing but the server holds the signing secret.
    """
    from labtris_api.auth import issue_token, read_token
    from labtris_api.models import User as UserRow

    row = UserRow(id="01TESTUSER00000000000001", username="alice",
                  role="admin", display_name="Alice")

    human = read_token(issue_token(row))
    assistant = read_token(issue_token(row, via="assistant"))

    assert human["act"] == "human"
    assert assistant["act"] == "assistant"
    assert human["sub"] == assistant["sub"] == row.id


def test_an_old_token_without_the_claim_reads_as_human() -> None:
    """Sessions issued before this existed must keep working, and a missing
    claim is a human session — it predates the assistant being able to act."""
    from labtris_api.auth import User

    assert User(id="x", name="X", username="x", role="user").via == "human"
    assert not User(id="x", name="X", username="x", role="user").is_assistant


async def test_the_app_actually_starts() -> None:
    """Run the lifespan.

    The audit prune task was added with a reference to `asyncio` above a
    function-local `import asyncio` further down the same function — which
    makes the name local for the whole function, so the reference became an
    UnboundLocalError and the app refused to start:

        UnboundLocalError: cannot access local variable 'asyncio'
        ERROR:    Application startup failed. Exiting.

    Every test passed. Nothing in the suite ran the lifespan, so a crash on
    startup was invisible until a real host restarted into a 502 loop.
    """
    from labtris_api.main import create_app, lifespan

    app = create_app()
    async with lifespan(app):
        pass


async def test_the_prune_task_is_started_and_stopped() -> None:
    """A background task nothing cancels keeps uvicorn's shutdown hanging
    until systemd SIGKILLs it, and nginx answers 502 throughout."""
    import asyncio

    from labtris_api.main import create_app, lifespan

    app = create_app()
    async with lifespan(app):
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "audit-prune" in names, names
    await asyncio.sleep(0)
    assert not [
        t for t in asyncio.all_tasks()
        if t.get_name() == "audit-prune" and not t.done() and not t.cancelled()
    ]


async def test_the_summary_counts_actors_rather_than_inferring_them(client: AsyncClient) -> None:
    """by_human was total - by_assistant, which quietly credited people with
    work nobody did: a request that never authenticated is recorded with no
    actor at all, and those landed in the "by people" tile. On the one page
    whose job is saying who did what, an inferred number that can be wrong is
    worse than a missing one."""
    await client.post("/api/v1/labs", json={"name": f"sum-{ulid.new().str[-8:].lower()}"})

    s = (await client.get("/api/v1/audit/summary", params={"since_hours": 24})).json()
    assert s["by_human"] + s["by_assistant"] + s["anonymous"] == s["total"], (
        "the three tiles must partition the total, or the page adds up to "
        "something other than what it says happened"
    )

    #: And the lab just created is attributed to somebody, so by_human is
    #: counting real rows rather than being zero for the wrong reason.
    entries = (await client.get("/api/v1/audit", params={"limit": 50})).json()["entries"]
    mine = next(e for e in entries if e["route"] == "/api/v1/labs" and e["method"] == "POST")
    assert mine["actor"], "a mutation by a signed-in user recorded no actor"
    assert s["by_human"] >= 1
