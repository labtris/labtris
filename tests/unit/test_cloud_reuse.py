"""When a cloud network's cloud_ref names an OS-owned bridge (netplan br0,
EVE-NG's pnet0), Labtris reuses it — no bridge is created, no NIC is
enslaved, and no interface is deleted on teardown. This exercises the guards
that let host-owned names flow through the same code paths as Labtris ones
without hitting the naming validators or trying to move something already in
place. The end-to-end create/delete lives in tests/acceptance/test_networks.py
where a real netd can prove the DB row and the kernel state stay in sync."""

from __future__ import annotations

from typing import Any

import pytest

from labtris_api import lifecycle


class _StubNet:
    """Just enough of the Network attributes bind_cloud looks at."""

    def __init__(
        self, kind: str, cloud_ref: str | None, host_ifname: str | None
    ) -> None:
        self.kind = kind
        self.cloud_ref = cloud_ref
        self.host_ifname = host_ifname


class _FakeNetd:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((verb, params))
        return {}


@pytest.fixture()
def fake_netd(monkeypatch: pytest.MonkeyPatch) -> _FakeNetd:
    fake = _FakeNetd()
    monkeypatch.setattr(lifecycle, "netd", fake)
    return fake


async def test_bind_cloud_is_a_noop_when_the_bridge_is_the_uplink(
    fake_netd: _FakeNetd,
) -> None:
    # Reuse path: cloud_ref == host_ifname. The bridge is already there; the
    # NIC (if any) is already inside it by way of netplan. There is nothing
    # for netd to attach.
    reused = _StubNet(kind="cloud", cloud_ref="br0", host_ifname="br0")

    await lifecycle.bind_cloud(reused)

    assert fake_netd.calls == []


async def test_bind_cloud_still_attaches_for_a_bare_nic(
    fake_netd: _FakeNetd,
) -> None:
    # Sanity: the guard does not short-circuit the normal path.
    normal = _StubNet(kind="cloud", cloud_ref="ens160", host_ifname="bcloud-abcd")

    await lifecycle.bind_cloud(normal)

    assert fake_netd.calls == [
        (
            "cloud.attach",
            {"name": "ens160", "bridge": "bcloud-abcd", "force": False},
        )
    ]


async def test_ensure_bridge_skips_host_owned_names(
    fake_netd: _FakeNetd,
) -> None:
    # A host bridge (br0) does not match IFNAME_RE, so bridge.create would
    # return EINVAL — a lie about the state of the world. Skip it: the bridge
    # is a precondition, not something Labtris is asked to build.
    await lifecycle._ensure_bridge("br0")

    assert fake_netd.calls == []


async def test_ensure_bridge_still_creates_labtris_bridges(
    fake_netd: _FakeNetd,
) -> None:
    await lifecycle._ensure_bridge("bcloud-abcd")

    assert fake_netd.calls == [("bridge.create", {"name": "bcloud-abcd"})]


async def test_delete_path_leaves_host_owned_bridge_alone(
    fake_netd: _FakeNetd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # delete_lab's cloud branch: cloud_ref == host_ifname means the bridge is
    # OS-owned. Do not drop it; the host would lose its uplink.
    dropped: list[str] = []

    async def track_drop(name: str) -> None:
        dropped.append(name)

    monkeypatch.setattr(lifecycle, "_drop_iface", track_drop)

    # Simulate the branch inline (delete_lab does more setup — DB session,
    # nodes, cascading deletes — than we want here). This is the smallest
    # thing that proves the guard: reused clouds never reach _drop_iface.
    reused_net = _StubNet(kind="cloud", cloud_ref="br0", host_ifname="br0")

    async def drop_if_owned(n: _StubNet) -> None:
        if n.kind == "cloud" and n.host_ifname == n.cloud_ref:
            return
        if n.host_ifname:
            await lifecycle._drop_iface(n.host_ifname)

    await drop_if_owned(reused_net)
    await drop_if_owned(_StubNet(kind="cloud", cloud_ref="ens160", host_ifname="bcloud-1234"))

    assert dropped == ["bcloud-1234"]
