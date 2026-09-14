"""The one document to paste into a bug report.

Every section has to survive its subsystem being unavailable — a report that
only works when everything is healthy is a report you can never get when you
need one.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from labtris_api.diagnose import host_facts, render


def test_host_facts_need_nothing_running() -> None:
    """Collected from the filesystem and /proc only, so this half of the
    report is available even when netd, docker and the database are all down.
    """
    facts = host_facts()

    assert facts["labtris_version"]
    assert facts["cpu"]["cores"]
    assert "kvm" in facts
    assert isinstance(facts["disks"], list) and facts["disks"]


def test_kvm_absence_is_explained_not_just_reported() -> None:
    """"kvm: false" tells a user nothing. Why their VMs crawl does."""
    note = host_facts()["kvm"]["note"]

    assert "TCG" in note or "acceleration" in note


def test_render_survives_every_service_being_down() -> None:
    facts = host_facts()
    facts.update(
        {
            "netd": {"reachable": False, "error": "Connection refused"},
            "docker": {"reachable": False, "error": "no socket"},
            "database": {"reachable": False, "error": "password authentication failed"},
            "qemu_processes": 0,
            "logs": {},
        }
    )

    text = render(facts)

    assert "DOWN" in text
    assert "Connection refused" in text
    assert "password authentication failed" in text


def test_drift_between_the_database_and_the_process_table_is_called_out() -> None:
    """A start or stop that did not finish leaves consoles and captures built
    on state that is not true. It should not take a person noticing."""
    facts = host_facts()
    facts.update(
        {
            "database": {"reachable": True, "migration": "0008", "labs": 1, "nodes": 4,
                         "networks": 1, "nodes_running": 4, "nodes_failed": 0},
            "qemu_processes": 1,
            "logs": {},
        }
    )

    assert "DRIFT" in render(facts)


async def test_the_endpoint_serves_both_json_and_paste_ready_text() -> None:
    from labtris_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        as_json = await client.get("/api/v1/system/diagnostics")
        as_text = await client.get("/api/v1/system/diagnostics?fmt=text")

    assert as_json.status_code == 200
    assert as_json.json()["cpu"]["cores"]
    assert as_text.status_code == 200
    assert as_text.headers["content-type"].startswith("text/plain")
    assert "labtris" in as_text.text
