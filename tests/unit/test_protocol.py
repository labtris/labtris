from __future__ import annotations

from typing import Any

from labtris_netd.protocol import FrameDecoder, encode
from labtris_netd.verbs import dispatch


class FakeNet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def bridge_create(self, name: str) -> dict[str, Any]:
        self.calls.append(("bridge_create", (name,)))
        return {"index": 1}

    def bridge_delete(self, name: str) -> dict[str, Any]:
        self.calls.append(("bridge_delete", (name,)))
        return {}

    def bridge_list(self) -> dict[str, Any]:
        return {"bridges": []}

    def tap_create(self, name: str, owner_uid: int) -> dict[str, Any]:
        self.calls.append(("tap_create", (name, owner_uid)))
        return {"index": 2}

    def tap_delete(self, name: str) -> dict[str, Any]:
        self.calls.append(("tap_delete", (name,)))
        return {}

    def iface_attach(self, name: str, bridge: str) -> dict[str, Any]:
        self.calls.append(("iface_attach", (name, bridge)))
        return {}

    def iface_detach(self, name: str) -> dict[str, Any]:
        self.calls.append(("iface_detach", (name,)))
        return {}

    def iface_set_state(self, name: str, up: bool) -> dict[str, Any]:
        self.calls.append(("iface_set_state", (name, up)))
        return {}

    def iface_delete(self, name: str) -> dict[str, Any]:
        self.calls.append(("iface_delete", (name,)))
        return {}

    def veth_create(self, name: str, peer: str) -> dict[str, Any]:
        self.calls.append(("veth_create", (name, peer)))
        return {"index": 3, "peer_index": 4}

    def netns_move(self, name: str, pid: int, rename_to=None, mac=None, up=True) -> dict[str, Any]:
        self.calls.append(("netns_move", (name, pid, rename_to, mac, up)))
        return {}

    def tc_set(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("tc_set", (name, spec)))
        return {"applied": spec}

    def tc_clear(self, name: str) -> dict[str, Any]:
        self.calls.append(("tc_clear", (name,)))
        return {}

    def capture_start(self, name: str, bpf: str) -> dict[str, Any]:
        self.calls.append(("capture_start", (name, bpf)))
        return {"started": name}

    def capture_stop(self, name: str) -> dict[str, Any]:
        self.calls.append(("capture_stop", (name,)))
        return {}

    def capture_read(self, name: str) -> dict[str, Any]:
        return {"lines": []}

    def host_tune(self, sysctls: dict[str, str]) -> dict[str, Any]:
        return {"applied": sysctls}

    def host_tuning_read(self, keys: list[str]) -> dict[str, Any]:
        return {"values": {k: None for k in keys}}

    def vxlan_create(
        self, name: str, vni: int, remote: str, local: str | None, dstport: int
    ) -> dict[str, Any]:
        self.calls.append(("vxlan_create", (name, vni, remote, local, dstport)))
        return {"index": 5}

    def vxlan_delete(self, name: str) -> dict[str, Any]:
        self.calls.append(("vxlan_delete", (name,)))
        return {}


def test_ndjson_partial_read_split_mid_frame() -> None:
    decoder = FrameDecoder()
    frame = encode({"id": "1", "verb": "ping", "params": {}})
    mid = len(frame) // 2
    assert decoder.feed(frame[:mid]) == []
    got = decoder.feed(frame[mid:])
    assert len(got) == 1
    assert got[0]["verb"] == "ping"


def test_unknown_verb_einval() -> None:
    net = FakeNet()
    reply = dispatch({"id": "x", "verb": "shell.exec", "params": {}}, net)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []


def test_malformed_ifname_no_netlink() -> None:
    net = FakeNet()
    reply = dispatch(
        {"id": "x", "verb": "bridge.create", "params": {"name": "eth0; rm -rf /"}},
        net,
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []


def test_valid_bridge_create_hits_net() -> None:
    net = FakeNet()
    reply = dispatch(
        {"id": "x", "verb": "bridge.create", "params": {"name": "b3f9k2m1x"}},
        net,
    )
    assert reply["ok"] is True
    assert net.calls == [("bridge_create", ("b3f9k2m1x",))]


def test_tc_set_hits_net() -> None:
    net = FakeNet()
    reply = dispatch(
        {"id": "x", "verb": "tc.set", "params": {"name": "t3f9k2m1x", "spec": {"delay_ms": 50}}},
        net,
    )
    assert reply["ok"] is True
    assert net.calls[0][0] == "tc_set"


def test_capture_rejects_shell_in_bpf() -> None:
    net = FakeNet()
    reply = dispatch(
        {
            "id": "x",
            "verb": "capture.start",
            "params": {"name": "t3f9k2m1x", "bpf": "icmp; reboot"},
        },
        net,
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []


def test_host_tune_hits_net() -> None:
    net = FakeNet()
    reply = dispatch(
        {"id": "x", "verb": "host.tune", "params": {"sysctls": {"vm.swappiness": "1"}}},
        net,
    )
    assert reply["ok"] is True


def test_vxlan_create_validates_and_hits_net() -> None:
    net = FakeNet()
    reply = dispatch(
        {
            "id": "x",
            "verb": "vxlan.create",
            "params": {"name": "v3f9k2m1x", "vni": 100, "remote": "10.200.0.2", "dstport": 4789},
        },
        net,
    )
    assert reply["ok"] is True
    assert net.calls[0] == ("vxlan_create", ("v3f9k2m1x", 100, "10.200.0.2", None, 4789))


def test_vxlan_create_rejects_bad_remote() -> None:
    net = FakeNet()
    reply = dispatch(
        {
            "id": "x",
            "verb": "vxlan.create",
            "params": {"name": "v3f9k2m1x", "vni": 100, "remote": "not-an-ip"},
        },
        net,
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []


def test_vxlan_create_rejects_out_of_range_vni() -> None:
    net = FakeNet()
    reply = dispatch(
        {
            "id": "x",
            "verb": "vxlan.create",
            "params": {"name": "v3f9k2m1x", "vni": 2**30, "remote": "10.200.0.2"},
        },
        net,
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "EINVAL"
    assert net.calls == []
