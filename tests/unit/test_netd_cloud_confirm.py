"""A forced cloud attach on the management NIC is a hand-over, not a cut-off.

Exercised at the verb layer with a recording fake, which is what lets this
run without pyroute2: the netlink half is three small functions whose job is
"move these addresses and this route", and what is worth pinning here is
that the confirm verb exists, is reachable, validates its input the same way
attach does, and is wired to the right method.
"""

from __future__ import annotations

from typing import Any

from labtris_netd.verbs import dispatch


class RecordingNet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def cloud_attach(self, name: str, bridge: str, force: bool) -> dict[str, Any]:
        self.calls.append(("cloud_attach", (name, bridge, force)))
        return {"index": 7, "master": 9, "confirm_within": 90.0} if force else {"index": 7}

    def cloud_confirm(self, name: str) -> dict[str, Any]:
        self.calls.append(("cloud_confirm", (name,)))
        return {"confirmed": True, "pending": False}

    def cloud_detach(self, name: str) -> dict[str, Any]:
        self.calls.append(("cloud_detach", (name,)))
        return {"index": 7}


def _call(verb: str, params: dict[str, Any], net: RecordingNet) -> dict[str, Any]:
    return dispatch({"id": 1, "verb": verb, "params": params}, net)


def test_confirm_is_a_verb_and_reaches_the_net() -> None:
    net = RecordingNet()
    reply = _call("cloud.confirm", {"name": "enp0s2"}, net)
    assert reply["ok"] is True, reply
    assert reply["result"] == {"confirmed": True, "pending": False}
    assert net.calls == [("cloud_confirm", ("enp0s2",))]


def test_confirm_validates_the_interface_name_like_attach_does() -> None:
    """The name reaches netlink as given, so the same rule applies as for
    attach: a bad name is refused before anything is looked up — and
    refused as a reply, not as an exception that kills the connection."""
    net = RecordingNet()
    reply = _call("cloud.confirm", {"name": "../../etc"}, net)
    assert reply["ok"] is False, reply
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []


def test_a_forced_attach_reports_the_window_it_must_be_confirmed_in() -> None:
    net = RecordingNet()
    reply = _call("cloud.attach", {"name": "enp0s2", "bridge": "b0123abcd", "force": True}, net)
    assert reply["ok"] is True, reply
    assert reply["result"]["confirm_within"] == 90.0
    assert net.calls == [("cloud_attach", ("enp0s2", "b0123abcd", True))]


def test_an_unforced_attach_has_no_window_because_nothing_moved() -> None:
    net = RecordingNet()
    reply = _call("cloud.attach", {"name": "enp0s2", "bridge": "b0123abcd", "force": False}, net)
    assert reply["ok"] is True
    assert "confirm_within" not in reply["result"]
