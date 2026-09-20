from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any

from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkError

from labtris_netd.protocol import IFNAME_RE
from labtris_netd.verbs import NetdFault


def _pfc_apply_via_cli(
    ifname: str,
    netem: object,
    rate: object,
    pfc_priorities: list[int],
    ecn_min: int,
    ecn_max: int,
) -> None:
    """Shell out to `tc` to install prio + per-band red on `ifname`.

    pyroute2's prio encoder rejects both list and bytes for `priomap`
    with an unhelpful 'required argument is not an integer'. tc(8) is
    the canonical way to configure prio anyway — it's part of iproute2,
    on every host running netd, and stable across pyroute2 versions."""
    if netem and rate:
        parent, handle, major = "parent 2:0", "3:0", 3
    elif netem or rate:
        parent, handle, major = "parent 1:0", "2:0", 2
    else:
        parent, handle, major = "root", "1:0", 1
    subprocess.run(
        [
            "tc", "qdisc", "add", "dev", ifname, *parent.split(),
            "handle", handle, "prio",
            "bands", "8",
            "priomap", "0", "1", "2", "3", "4", "5", "6", "7",
            "0", "0", "0", "0", "0", "0", "0", "0",
        ],
        check=True, capture_output=True,
    )
    for band in pfc_priorities:
        if not (0 <= band < 8):
            continue
        try:
            subprocess.run(
                [
                    "tc", "qdisc", "add", "dev", ifname,
                    "parent", f"{major}:{band + 1}",
                    "handle", f"{major}{band + 1}:0",
                    "red",
                    "limit", str(ecn_max * 4),
                    "min", str(ecn_min),
                    "max", str(ecn_max),
                    "avpkt", "1000",
                    "burst", str(max((2 * ecn_min + ecn_max) // 3000, 1)),
                    "probability", "0.02",
                    "ecn",
                ],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            # A band that failed does not tear down the whole spec —
            # `tc qdisc show` will surface the partial attach.
            pass


CLONE_NEWNET = 0x40000000
_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _kbit_to_bytes(kbit: Any) -> int:
    """tbf speaks bytes per second; the UI and the API speak kbit."""
    return max(1, int(int(kbit) * 1000 / 8))


@lru_cache(maxsize=1)
def _ticks_per_usec() -> float:
    """From /proc/net/psched, because it is a property of this kernel.

    The four values are (t2us, us2t, 1000000, clock_res); ticks per microsecond
    is t2us/us2t — 1000/64 = 15.625 on the usual build."""
    try:
        parts = Path("/proc/net/psched").read_text().split()
        t2us = int(parts[0], 16)
        us2t = int(parts[1], 16)
        if t2us and us2t:
            return t2us / us2t
    except (OSError, ValueError, IndexError):
        pass
    return 15.625


def _nested_attr(opts: Any, name: str) -> dict[str, Any] | None:
    """Pull one named attribute out of a qdisc's nested attribute list."""
    for key, value in getattr(opts, "get", lambda *_: None)("attrs") or []:
        if key == name and isinstance(value, dict):
            return value
    return None


def _map_nl(exc: NetlinkError, name: str) -> NetdFault:
    code = exc.code
    if code in (17,):  # EEXIST
        return NetdFault("EEXIST", f"{name} exists")
    if code in (2, 19):  # ENOENT / ENODEV
        return NetdFault("ENOENT", f"{name} not found")
    if code in (16,):  # EBUSY
        return NetdFault("EBUSY", f"{name} busy")
    if code in (40,):  # ELOOP
        return NetdFault(
            "ELOOP",
            f"{name} cannot be enslaved here: the kernel refuses a bridge inside "
            "a bridge, and refuses any attachment that would make a loop",
        )
    if code in (1, 13):
        return NetdFault("EPERM", str(exc))
    return NetdFault("EINTERNAL", str(exc))


# Link types we probe for in host_capabilities(). The vxlan parameters are the
# same shape vxlan_create() uses, so a kernel that accepts the probe accepts a
# real overlay endpoint too.
_PROBE_LINKS: dict[str, dict[str, Any]] = {
    "bridge": {},
    "dummy": {},
    "vxlan": {"vxlan_id": 1, "vxlan_group": "127.0.0.1", "vxlan_port": 4789},
}

# Probing creates and deletes real devices, so do it once per netd process.
_CAPABILITIES: dict[str, Any] | None = None


def _render_management_netplan(
    mode: str,
    interface: str,
    address: str | None,
    gateway: str | None,
    dns: list[str],
    kind: str = "nic",
) -> str:
    """Return the yaml text a management config renders to. Pure — no
    filesystem, no netlink — so it's unit-testable without a live host.

    `kind` is 'nic' (write under `ethernets:`) or 'bridge' (write under
    `bridges:` — the bridge is presumed already-defined by another netplan
    file, e.g. the labtris-cloud drop-in; this file only sets its address
    so it does not fight over the bridge's structure).

    Deliberately hand-written strings rather than yaml.dump: the file is
    short, the shape matters exactly, and a rogue key would silently take
    effect on the next boot.
    """
    if kind not in ("nic", "bridge"):
        raise ValueError(f"kind must be 'nic' or 'bridge', not {kind!r}")
    top_key = "ethernets" if kind == "nic" else "bridges"
    lines = [
        "# Written by Labtris. Do not edit by hand — the UI overwrites this file.",
        "network:",
        "  version: 2",
        f"  {top_key}:",
        f"    {interface}:",
    ]
    if mode == "dhcp":
        lines += [
            "      dhcp4: true",
            "      dhcp6: false",
        ]
    else:
        if not address or not gateway:
            raise ValueError("manual mode requires address and gateway")
        lines += [
            "      dhcp4: false",
            "      dhcp6: false",
            f"      addresses: [{address}]",
            "      routes:",
            "        - to: default",
            f"          via: {gateway}",
        ]
        if dns:
            lines += [
                "      nameservers:",
                "        addresses: [" + ", ".join(dns) + "]",
            ]
    return "\n".join(lines) + "\n"


# Regex for names Labtris allocates for uplink bridges: br1, br2, br99. br0
# is reserved for the earlier pre-bridge shape (management + first cloud),
# so the auto-allocator here starts at 1. Not a security check — a shape
# check that lets uplink_probe distinguish "labtris-managed uplink bridge"
# from "some other host bridge we shouldn't touch".
_UPLINK_BRIDGE_RE = re.compile(r"^br([1-9][0-9]*)$")


def _render_uplink_bridges_netplan(pairs: list[tuple[str, str]]) -> str:
    """Return the yaml text for a labtris uplink-bridges netplan file.

    `pairs` is `[(nic_name, bridge_name), …]` — one host NIC per bridge,
    each bridge unaddressed (Labtris does not put an IP on an uplink; that
    is the management pane's job on a separate bridge if you want it).
    """
    if not pairs:
        # An empty apply is legitimate — it removes all uplink bridges.
        # Emit an inert-but-valid file rather than deleting the file so
        # /etc/netplan/ stays predictable and cloud-init has no reason to
        # re-write anything.
        return (
            "# Written by Labtris. Uplink bridges configured from the UI.\n"
            "# Currently: none.\n"
            "network:\n"
            "  version: 2\n"
        )

    seen: set[str] = set()
    for nic, br in pairs:
        if nic in seen:
            raise ValueError(f"nic {nic!r} appears more than once")
        seen.add(nic)

    lines = [
        "# Written by Labtris. Uplink bridges configured from the UI.",
        "# Do not edit by hand — the UI overwrites this file.",
        "network:",
        "  version: 2",
        "  ethernets:",
    ]
    for nic, _br in pairs:
        lines += [
            f"    {nic}:",
            "      dhcp4: false",
            "      dhcp6: false",
        ]
    lines.append("  bridges:")
    for nic, br in pairs:
        lines += [
            f"    {br}:",
            f"      interfaces: [{nic}]",
            "      dhcp4: false",
            "      dhcp6: false",
            "      parameters:",
            "        stp: false",
            "        forward-delay: 0",
        ]
    return "\n".join(lines) + "\n"


def _apply_netplan_write(
    cfg_path: Path,
    content: str,
    neutralise_cloud_init: bool = True,
) -> dict[str, Any]:
    """Write a netplan drop-in, optionally neutralise cloud-init's file,
    then run `netplan apply`. Shared by configure_management and
    configure_uplinks.

    `neutralise_cloud_init` is a bool because both callers want the same
    thing today; leaving it as a param keeps the option open for a future
    caller that doesn't (e.g. a debug apply).
    """
    cfg_path.write_text(content)
    # netplan refuses world-readable configs since 0.106; enforce the
    # tighter perms so `netplan apply` doesn't warn.
    cfg_path.chmod(0o600)

    touched: list[str] = [str(cfg_path)]
    if neutralise_cloud_init:
        cloud_init = Path("/etc/netplan/50-cloud-init.yaml")
        if cloud_init.exists():
            existing = cloud_init.read_text()
            if 'name: "enx-none"' not in existing:
                inert = (
                    "# Neutralised by Labtris: management and uplink configs\n"
                    "# live in /etc/netplan/60-labtris-*.yaml instead.\n"
                    "network:\n"
                    "  version: 2\n"
                    "  ethernets:\n"
                    "    any-ethernet-inert:\n"
                    "      match:\n"
                    '        name: "enx-none"\n'
                    "      optional: true\n"
                    "      dhcp4: false\n"
                )
                cloud_init.write_text(inert)
                cloud_init.chmod(0o600)
                touched.append(str(cloud_init))

    proc = subprocess.run(
        ["/usr/sbin/netplan", "apply"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise NetdFault(
            "EINTERNAL",
            f"netplan apply failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}",
        )
    return {"files_written": touched, "netplan_output": (proc.stdout or "").strip()}


class PyrouteNet:
    def bridge_create(self, name: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("add", ifname=name, kind="bridge")
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
            idx = _lookup(ipr, name)
            return {"index": idx}

    def bridge_delete(self, name: str) -> dict[str, Any]:
        return self.iface_delete(name)

    def bridge_list(self) -> dict[str, Any]:
        bridges: list[dict[str, Any]] = []
        with IPRoute() as ipr:
            for link in ipr.get_links():
                ifname = link.get_attr("IFLA_IFNAME")
                if not ifname or not IFNAME_RE.match(ifname):
                    continue
                info = link.get_attr("IFLA_LINKINFO")
                kind = None
                if info is not None:
                    kind = info.get_attr("IFLA_INFO_KIND") if hasattr(info, "get_attr") else None
                    if kind is None and isinstance(info, list):
                        for item in info:
                            if (
                                isinstance(item, tuple | list)
                                and item
                                and item[0] == "IFLA_INFO_KIND"
                            ):
                                kind = item[1]
                if kind == "bridge":
                    bridges.append({"name": ifname, "index": int(link["index"])})
        return {"bridges": bridges}

    def tap_create(self, name: str, owner_uid: int) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("add", ifname=name, kind="tuntap", mode="tap", uid=owner_uid)
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
            return {"index": _lookup(ipr, name)}

    def tap_delete(self, name: str) -> dict[str, Any]:
        return self.iface_delete(name)

    def iface_attach(self, name: str, bridge: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("set", index=_lookup(ipr, name), master=_lookup(ipr, bridge))
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {}

    def iface_detach(self, name: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("set", index=_lookup(ipr, name), master=0)
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {}

    def iface_set_state(self, name: str, up: bool) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("set", index=_lookup(ipr, name), state="up" if up else "down")
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {}

    def iface_delete(self, name: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("del", index=_lookup(ipr, name))
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {}

    def veth_create(self, name: str, peer: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("add", ifname=name, kind="veth", peer={"ifname": peer})
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
            return {"index": _lookup(ipr, name), "peer_index": _lookup(ipr, peer)}

    def netns_move(
        self,
        name: str,
        pid: int,
        rename_to: str | None = None,
        mac: str | None = None,
        up: bool = True,
    ) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                ipr.link("set", index=_lookup(ipr, name), net_ns_pid=pid)
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        if rename_to or mac is not None or up:
            _in_netns(pid, lambda: _finish_move(name, rename_to, mac, up))
        return {}

    def tc_set(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        self.tc_clear(name)
        delay = int(spec.get("delay_ms") or 0)
        jitter = int(spec.get("jitter_ms") or 0)
        loss = float(spec.get("loss_pct") or 0)
        reorder = float(spec.get("reorder_pct") or 0)
        duplicate = float(spec.get("duplicate_pct") or 0)
        corrupt = float(spec.get("corrupt_pct") or 0)
        rate = spec.get("rate_kbit")
        # Phase F3: opt-in ECN CE marking under queue pressure. When
        # `ecn: true` and (delay or rate) is set, the shaping qdisc is
        # netem-then-RED — RED marks ECN CE above `ecn_min_bytes` up to
        # `ecn_max_bytes`, drops at max (or tail-drops if ECN is off).
        # DCTCP / DCQCN / Ultra Ethernet CC all read this signal; the
        # ecn.p4 built-in on a bmv2 switch produces it upstream, this
        # gets it on plain shaped Linux links.
        ecn = bool(spec.get("ecn") or False)
        ecn_min = int(spec.get("ecn_min_bytes") or 50_000)
        ecn_max = int(spec.get("ecn_max_bytes") or max(ecn_min * 3, 150_000))
        # Phase H: DCB per-priority queueing. When pfc=true, install
        # `prio` root (8 bands, priomap 0..7 → bands 0..7) and attach a
        # RED qdisc with ECN on each band named in pfc_priorities.
        # Priorities not in the list get the default pfifo. This gives
        # the queueing effect real 802.1Qbb PFC produces — class
        # isolation + early congestion signalling on the "lossless"
        # bands — without emitting wire-level PAUSE frames. Real pause
        # frames need XDP-side generation and are Phase I. When rate or
        # netem are also set they stack ABOVE prio (they act on the
        # aggregate), so a pfc lab with a rate cap is `tbf → prio →
        # (red|pfifo)` per band. See docs/design/pfc-implementation.mdx.
        pfc = bool(spec.get("pfc") or False)
        pfc_priorities: list[int] = list(spec.get("pfc_priorities") or [])
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            netem = delay or jitter or loss or reorder or duplicate or corrupt
            try:
                if netem:
                    kw: dict[str, Any] = {"index": idx, "handle": "1:0"}
                    if delay or jitter:
                        kw["delay"] = delay * 1000
                        if jitter:
                            kw["jitter"] = jitter * 1000
                    if loss:
                        kw["loss"] = loss
                    if duplicate:
                        kw["duplicate"] = duplicate
                    # pyroute2 names these prob_reorder / prob_corrupt. Passing
                    # "reorder" and "corrupt" was silently ignored — the qdisc
                    # was created without them and nothing said so, so two of
                    # the six impairment knobs did nothing at all.
                    if reorder:
                        kw["prob_reorder"] = reorder
                    if corrupt:
                        kw["prob_corrupt"] = corrupt
                    ipr.tc("add", "netem", **kw)
                if rate:
                    parent = "1:0" if netem else None
                    ipr.tc(
                        "add",
                        "tbf",
                        index=idx,
                        handle="2:0" if netem else "1:0",
                        parent=parent,
                        # tbf's rate is BYTES per second, so a kbit figure has
                        # to be divided by 8. Passing kbit*1000 straight in ran
                        # every shaped link at eight times the requested
                        # bandwidth — a "3g" profile behaving like broadband,
                        # which is invisible unless you measure it.
                        rate=_kbit_to_bytes(rate),
                        burst=16000,
                        limit=max(_kbit_to_bytes(rate) // 4, 16000),
                    )
                if ecn and not pfc:
                    # RED as leaf qdisc: marks ECN when qavg is in
                    # [ecn_min, ecn_max), drops above. Sits under
                    # netem+tbf if either exists, else attaches root.
                    # Skipped when pfc is also set — pfc installs its
                    # own per-band REDs and a root-level RED would
                    # collide with prio's placement.
                    handle = "3:0" if (netem or rate) else "1:0"
                    parent = (
                        "2:0" if (netem and rate)
                        else "1:0" if (netem or rate)
                        else None
                    )
                    ipr.tc(
                        "add",
                        "red",
                        index=idx,
                        handle=handle,
                        parent=parent,
                        limit=ecn_max * 4,
                        min=ecn_min,
                        max=ecn_max,
                        avpkt=1000,
                        burst=max((2 * ecn_min + ecn_max) // 3000, 1),
                        probability=0.02,
                        ecn=True,
                    )
                if pfc:
                    # pyroute2's tc() for prio has a fragile encoder
                    # for the priomap arg — passing either list or
                    # bytes raises "required argument is not an
                    # integer" from deep in the netlink packer, with
                    # no useful hint. Fall back to the tc(8) CLI: it's
                    # already on every host running netd (part of the
                    # iproute2 package), spec-exact behaviour, no
                    # pyroute2-version quirks to test around.
                    _pfc_apply_via_cli(
                        name, netem, rate, pfc_priorities,
                        ecn_min, ecn_max,
                    )
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        # Best-effort ethtool PAUSE. Outside the netlink try because
        # ethtool talks over ioctl, not netlink, and veth EOPNOTSUPP is
        # not an error to raise — just a fact worth logging.
        if pfc:
            try:
                import subprocess as _sub
                r = _sub.run(
                    ["ethtool", "-A", name, "rx", "on", "tx", "on"],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    import logging as _log
                    _log.getLogger("labtris.netd").info(
                        "ethtool -A on %s: %s (expected on veth; "
                        "see docs/design/pfc-implementation)",
                        name, (r.stderr or "").strip()[:120],
                    )
            except (FileNotFoundError, OSError):
                pass
        return {"applied": spec}

    def iface_counters(self, names: list[str]) -> dict[str, Any]:
        """IFLA_STATS64 for every named interface, in one netlink dump.

        Called from the per-link traffic overlay: the browser polls one
        lab-wide endpoint every ~2 s, that endpoint asks netd for every
        endpoint interface's counters in one call, netd walks a single
        `ipr.get_links()` result. A canvas with 100 links needs one
        round-trip.

        Missing interfaces get `{"exists": false}` so a link whose tap
        was just wiped does not surface as a zero — the UI can dim the
        label and leave the last known value visible.
        """
        import time

        out: dict[str, Any] = {}
        as_of = time.time()
        with IPRoute() as ipr:
            by_name = {
                link.get_attr("IFLA_IFNAME"): link
                for link in ipr.get_links()
                if link.get_attr("IFLA_IFNAME")
            }
            for name in names:
                link = by_name.get(name)
                if link is None:
                    out[name] = {"exists": False}
                    continue
                # IFLA_STATS64 is a nested struct (rtnl_link_stats64):
                # rx_packets, tx_packets, rx_bytes, tx_bytes, rx_errors,
                # tx_errors, rx_dropped, tx_dropped, multicast, collisions,
                # + a bunch of others. pyroute2 exposes it as a dict.
                stats = link.get_attr("IFLA_STATS64") or link.get_attr("IFLA_STATS") or {}
                out[name] = {
                    "exists": True,
                    "rx_bytes": int(stats.get("rx_bytes", 0)),
                    "tx_bytes": int(stats.get("tx_bytes", 0)),
                    "rx_packets": int(stats.get("rx_packets", 0)),
                    "tx_packets": int(stats.get("tx_packets", 0)),
                    "rx_dropped": int(stats.get("rx_dropped", 0)),
                    "tx_dropped": int(stats.get("tx_dropped", 0)),
                    "rx_errors": int(stats.get("rx_errors", 0)),
                    "tx_errors": int(stats.get("tx_errors", 0)),
                    "as_of": as_of,
                }
        return {"counters": out}

    def iface_inspect(self, names: list[str]) -> dict[str, Any]:
        """What each named device actually is right now.

        Setting up a dataplane is only half of it — nothing until now could
        ask whether the result matches the topology that asked for it. A tap
        that exists but is down, or is on the wrong bridge, produces a lab
        that looks correct and does not pass traffic."""
        out: dict[str, Any] = {}
        with IPRoute() as ipr:
            by_index = {link["index"]: link for link in ipr.get_links()}
            by_name = {
                link.get_attr("IFLA_IFNAME"): link
                for link in by_index.values()
                if link.get_attr("IFLA_IFNAME")
            }
            for name in names:
                link = by_name.get(name)
                if link is None:
                    out[name] = {"exists": False}
                    continue
                master_idx = link.get_attr("IFLA_MASTER")
                master = by_index.get(master_idx) if master_idx else None
                info = link.get_attr("IFLA_LINKINFO")
                out[name] = {
                    "exists": True,
                    "index": link["index"],
                    "up": bool(link["flags"] & 1),
                    # Admin-up with no carrier is the signature of a tap whose
                    # other end nobody opened — i.e. a stopped guest.
                    "carrier": link.get_attr("IFLA_CARRIER") == 1,
                    "master": master.get_attr("IFLA_IFNAME") if master is not None else None,
                    "kind": info.get_attr("IFLA_INFO_KIND") if info is not None else None,
                    "mtu": link.get_attr("IFLA_MTU"),
                }
        return {"interfaces": out}

    def tc_read(self, name: str) -> dict[str, Any]:
        """The impairment actually in the kernel, in the units tc_set takes.

        Reported back in the same shape it was given so intent and reality can
        be compared directly rather than through a unit conversion nobody
        checks."""
        spec: dict[str, Any] = {}
        with IPRoute() as ipr:
            try:
                idx = _lookup(ipr, name)
            except NetdFault:
                return {"exists": False, "spec": {}}
            for qdisc in ipr.get_qdiscs(index=idx):
                kind = qdisc.get_attr("TCA_KIND")
                opts = qdisc.get_attr("TCA_OPTIONS")
                if opts is None:
                    continue
                if kind == "netem":
                    # Times come back in scheduler ticks, not microseconds —
                    # 15.625 of them per microsecond on a typical kernel, which
                    # is why a 120ms delay reads as 1875000. The rate is the
                    # kernel's to state, so ask it rather than assume.
                    ticks = _ticks_per_usec()
                    delay = opts.get("delay") or 0
                    jitter = opts.get("jitter") or 0
                    if delay:
                        spec["delay_ms"] = round(delay / ticks / 1000)
                    if jitter:
                        spec["jitter_ms"] = round(jitter / ticks / 1000)
                    # Probabilities are 32-bit fractions of 1.
                    for key, out_key in (("loss", "loss_pct"), ("duplicate", "duplicate_pct")):
                        value = opts.get(key)
                        if value:
                            spec[out_key] = round(100 * value / (2**32), 3)
                    # reorder and corrupt hang off nested attributes instead.
                    for attr, field, out_key in (
                        ("TCA_NETEM_REORDER", "prob_reorder", "reorder_pct"),
                        ("TCA_NETEM_CORRUPT", "prob_corrupt", "corrupt_pct"),
                    ):
                        nested = _nested_attr(opts, attr)
                        value = (nested or {}).get(field)
                        if value:
                            spec[out_key] = round(100 * value / (2**32), 3)
                elif kind == "tbf":
                    parms = _nested_attr(opts, "TCA_TBF_PARMS") or {}
                    rate = parms.get("rate")
                    if rate:
                        spec["rate_kbit"] = round(rate * 8 / 1000)
        return {"exists": True, "spec": spec}

    def tc_clear(self, name: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                idx = _lookup(ipr, name)
            except NetdFault:
                return {}
            for handle in ("2:0", "1:0"):
                try:
                    ipr.tc("del", index=idx, handle=handle)
                except Exception:
                    pass
        return {}

    def addr_replace(self, name: str, address: str, prefix: int) -> dict[str, Any]:
        """Put the gateway address on a lab bridge, replacing any it has.

        Replace rather than add: a NAT network that is edited keeps one
        gateway, and an accumulated second address on the bridge would answer
        ARP for a subnet nothing routes to.
        """
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            for existing in ipr.get_addr(index=idx):
                old = existing.get_attr("IFA_ADDRESS")
                if old:
                    try:
                        ipr.addr("del", index=idx, address=old,
                                 prefixlen=existing["prefixlen"])
                    except NetlinkError:
                        pass
            try:
                ipr.addr("add", index=idx, address=address, prefixlen=prefix)
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
            ipr.link("set", index=idx, state="up")
        return {"address": f"{address}/{prefix}"}

    def addr_flush(self, name: str) -> dict[str, Any]:
        with IPRoute() as ipr:
            try:
                idx = _lookup(ipr, name)
            except NetdFault:
                return {}
            for existing in ipr.get_addr(index=idx):
                old = existing.get_attr("IFA_ADDRESS")
                if old:
                    try:
                        ipr.addr("del", index=idx, address=old,
                                 prefixlen=existing["prefixlen"])
                    except NetlinkError:
                        pass
        return {}

    def nat_enable(self, subnet: str, bridge: str) -> dict[str, Any]:
        """Masquerade this subnet on the way out, and let it be forwarded.

        Its own table, so tearing a NAT network down cannot disturb whatever
        else the host has in nftables — a lab tool that flushes the machine's
        firewall is a lab tool nobody runs twice.
        """
        _nft_table_ensure()
        # Register the bridge so the masquerade below can exclude it. Without
        # this, traffic from one lab segment to *another* lab segment is
        # masqueraded too — `ip daddr != subnet` only excludes this network's
        # own prefix. A capture would then show the host's address instead of
        # the real source, which in a tool people use to learn routing is worse
        # than not having NAT at all.
        _nft("add", "element", "inet", _NFT_TABLE, _NFT_BRIDGES, "{", bridge, "}")
        _nft(
            "add", "rule", "inet", _NFT_TABLE, "postrouting",
            "ip", "saddr", subnet, "ip", "daddr", "!=", subnet,
            "oifname", "!=", f"@{_NFT_BRIDGES}",
            "counter", "masquerade",
        )
        # Traffic between guests on the same bridge is already switched; these
        # two allow the routed path in and out of it.
        _nft("add", "rule", "inet", _NFT_TABLE, "forward", "iifname", bridge, "counter", "accept")
        _nft("add", "rule", "inet", _NFT_TABLE, "forward", "oifname", bridge, "counter", "accept")
        # Routing between the bridge and the outside is the whole point of a
        # NAT network, and it is off by default on most hosts.
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
        return {"subnet": subnet}

    def nat_disable(self, subnet: str, bridge: str) -> dict[str, Any]:
        """Remove only this network's rules, by matching on their text.

        nftables handles are stable but not known to us across a netd restart,
        so the rules are found by listing the table and matching the subnet and
        bridge that identify them. Nothing outside our own table is touched.
        """
        listing = _nft_list()
        for handle, text in listing:
            if subnet in text or f'"{bridge}"' in text:
                _nft("delete", "rule", "inet", _NFT_TABLE, _chain_of(text), "handle", str(handle))
        try:
            _nft("delete", "element", "inet", _NFT_TABLE, _NFT_BRIDGES, "{", bridge, "}")
        except NetdFault:
            # Already gone is the normal case when a network is torn down twice.
            pass
        return {}

    def bridge_vlan_aware(self, name: str, on: bool, ethertype: int) -> dict[str, Any]:
        """Turn a plain bridge into a VLAN-filtering one — EVE-NG's smart bridge.

        Without filtering a Linux bridge forwards tagged frames unchanged and
        ignores the tags, so a "trunk" works by accident and an "access port"
        does not exist. With it the bridge is a switch: ports have a PVID, tags
        are enforced, and 802.1ad gives the outer tag for QinQ.
        """
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            ipr.link(
                "set",
                index=idx,
                kind="bridge",
                br_vlan_filtering=1 if on else 0,
                br_vlan_protocol=ethertype,
            )
        return {"vlan_filtering": on, "ethertype": ethertype}

    def bridge_port_vlan(
        self, name: str, pvid: int | None, tagged: list[int], untagged: list[int]
    ) -> dict[str, Any]:
        """Set one port's VLAN membership.

        An access port is a PVID plus that VLAN untagged. A trunk is a list of
        tagged VLANs and usually no PVID. Existing membership is cleared first,
        including the kernel's default VLAN 1, which otherwise leaves every
        "access port on VLAN 20" also carrying untagged VLAN 1 traffic.
        """
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            for existing in _port_vlans(ipr, idx):
                try:
                    ipr.vlan_filter("del", index=idx, vlan_info={"vid": existing})
                except NetlinkError:
                    pass
            for vid in tagged:
                ipr.vlan_filter("add", index=idx, vlan_info={"vid": vid})
            for vid in untagged:
                flags = 4  # BRIDGE_VLAN_INFO_UNTAGGED
                if pvid == vid:
                    flags |= 2  # BRIDGE_VLAN_INFO_PVID
                ipr.vlan_filter("add", index=idx, vlan_info={"vid": vid, "flags": flags})
        return {"pvid": pvid, "tagged": tagged, "untagged": untagged}

    def dhcp_start(
        self,
        key: str,
        bridge: str,
        gateway: str,
        prefix: int,
        first: str,
        last: str,
        dns: str | None,
        hosts: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return _dhcp.start(key, bridge, gateway, prefix, first, last, dns, hosts)

    def nat_sessions(self, subnet: str) -> dict[str, Any]:
        """Flows currently tracked through this NAT.

        The `conntrack` tool, not /proc/net/nf_conntrack. That file looked like
        the dependency-free option and is not one: Ubuntu 24.04 — the platform
        the installer targets — builds its kernel without
        CONFIG_NF_CONNTRACK_PROCFS, so the file never appears no matter how
        much traffic is tracked. The proc read is kept as a fallback because
        kernels that do expose it are one less process to spawn.
        """
        proc = Path("/proc/net/nf_conntrack")
        if proc.exists():
            try:
                return {"sessions": _conntrack_for(proc.read_text(), subnet)}
            except OSError:
                pass

        tool = shutil.which("conntrack")
        if not tool:
            return {
                "sessions": [],
                "note": "conntrack is not installed, and this kernel does not expose "
                "/proc/net/nf_conntrack",
            }
        run = subprocess.run(
            [tool, "-L", "-f", "ipv4"], capture_output=True, text=True, timeout=10
        )
        # conntrack writes its summary to stderr and the table to stdout; a
        # non-zero exit with rows on stdout is normal when the table is large.
        if run.returncode != 0 and not run.stdout:
            return {"sessions": [], "note": run.stderr.strip()[:200] or "conntrack failed"}
        return {"sessions": _conntrack_for(run.stdout, subnet)}

    def dhcp_stop(self, key: str) -> dict[str, Any]:
        return _dhcp.stop(key)

    def dhcp_leases(self, key: str) -> dict[str, Any]:
        return _dhcp.leases(key)

    def capture_start(self, name: str, bpf: str) -> dict[str, Any]:
        return _captures.start(name, bpf)

    def capture_stop(self, name: str) -> dict[str, Any]:
        return _captures.stop(name)

    def capture_read(self, name: str) -> dict[str, Any]:
        return _captures.read(name)

    def hook_ping(
        self, pid: int, target: str, count: int, timeout_s: int
    ) -> dict[str, Any]:
        """Ping `target` from the netns of PID; return rc/stdout/stderr.

        `nsenter -t PID -n` puts us in the container's network namespace
        without paying for a full nsenter-into-mount-and-user; ping runs
        with whatever it inherits from netd (root, which is what /bin/ping
        wants for -W to work without setuid gymnastics)."""
        proc = subprocess.run(
            [
                "nsenter",
                "-t",
                str(pid),
                "-n",
                "/bin/ping",
                "-c",
                str(count),
                "-W",
                str(timeout_s),
                target,
            ],
            capture_output=True,
            text=True,
            # Wall-clock cap independent of ping's own per-packet -W, so a
            # target that black-holes every packet can't hold this call
            # open past count*(timeout+1) seconds.
            timeout=count * (timeout_s + 1) + 5,
        )
        return {
            "rc": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    def hook_http(self, pid: int, url: str, timeout_s: int) -> dict[str, Any]:
        """GET `url` from the netns of PID and report the HTTP status.

        curl is asked for the status code alone (`-w '%{http_code}'`) so
        the body never comes back to us — hooks read pass/fail from the
        status, not the body. Follows redirects (-L) because a hook that
        wanted the endpoint after redirects would otherwise need one hook
        per hop."""
        proc = subprocess.run(
            [
                "nsenter",
                "-t",
                str(pid),
                "-n",
                "/usr/bin/curl",
                "-sSL",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                str(timeout_s),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,
        )
        try:
            status = int(proc.stdout.strip() or "0")
        except ValueError:
            status = 0
        return {
            "rc": proc.returncode,
            "http_status": status,
            "stderr": proc.stderr,
        }

    def host_tune(self, sysctls: dict[str, str]) -> dict[str, Any]:
        applied: dict[str, str] = {}
        for key, value in sysctls.items():
            if not _SYSCTL_RE.match(key):
                raise NetdFault("EINVAL", f"illegal sysctl {key}")
            path = Path("/proc/sys") / key.replace(".", "/")
            if not path.exists():
                continue
            path.write_text(str(value) + "\n")
            applied[key] = value
        return {"applied": applied}

    def host_tuning_read(self, keys: list[str]) -> dict[str, Any]:
        out: dict[str, str | None] = {}
        for key in keys:
            path = Path("/proc/sys") / key.replace(".", "/")
            out[key] = path.read_text().strip() if path.exists() else None
        ksm = Path("/sys/kernel/mm/ksm/pages_sharing")
        out["ksm.pages_sharing"] = ksm.read_text().strip() if ksm.exists() else None
        return {"values": out}

    def management_probe(self) -> dict[str, Any]:
        """What the UI needs to render its Management network pane sensibly.

        Reports the interfaces that could carry the management address (a
        bare NIC or a host-owned bridge — but not docker0, and not
        Labtris-owned devices) plus what the box is currently on. If a box
        has been pre-bridged (ens160 → br0), the pane offers br0 as the
        target because that's where the IP actually lives; picking ens160
        instead would write a config the running bridge stack shadows."""
        nics: list[dict[str, Any]] = []
        default_ifs, default_srcs = self._default_route_state()
        with IPRoute() as ipr:
            addrs: dict[int, list[str]] = {}
            for addr in ipr.get_addr():
                ip = addr.get_attr("IFA_ADDRESS")
                if ip:
                    addrs.setdefault(addr["index"], []).append(f"{ip}/{addr['prefixlen']}")
            for link in ipr.get_links():
                name = link.get_attr("IFLA_IFNAME")
                if not name or name == "lo" or IFNAME_RE.match(name):
                    continue
                info = link.get_attr("IFLA_LINKINFO")
                kind = info.get_attr("IFLA_INFO_KIND") if info is not None else None
                enslaved = link.get_attr("IFLA_MASTER") is not None
                # Candidate targets: a bare NIC (no kind), or a host bridge
                # (kind == "bridge") that is not docker0. Everything else
                # (veths, macvlans, Labtris devices, docker0) skipped.
                if kind is None:
                    entry_kind = "nic"
                elif kind == "bridge" and name != "docker0":
                    entry_kind = "bridge"
                else:
                    continue
                # A NIC enslaved into a bridge isn't independently addressable —
                # its address (if any) lives on the bridge. Offer the bridge
                # to the user, not the enslaved NIC.
                if entry_kind == "nic" and enslaved:
                    continue
                nics.append({
                    "name": name,
                    "kind": entry_kind,
                    "addresses": addrs.get(link["index"], []),
                    "carries_default_route": name in default_ifs,
                })

        current: dict[str, Any] = {
            "mode": "unknown",
            "interface": None,
            "kind": None,
            "address": None,
            "gateway": None,
            "dns": [],
        }
        # The interface that carries the default route is the management one.
        # Its addresses become the current address (if only one) or the one
        # that sources the default route (if several).
        for nic in nics:
            if nic["carries_default_route"] and nic["addresses"]:
                current["interface"] = nic["name"]
                current["kind"] = nic["kind"]
                # Prefer the address that sources the default route; fall
                # back to the first.
                for a in nic["addresses"]:
                    if a.split("/")[0] in default_srcs:
                        current["address"] = a
                        break
                if not current["address"]:
                    current["address"] = nic["addresses"][0]
                break

        # Gateway from /proc/net/route — the first default route.
        try:
            with open("/proc/net/route") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "00000000":
                        gw_hex = parts[2]
                        # gw is a little-endian hex IP
                        gw = ".".join(
                            str(int(gw_hex[i:i + 2], 16)) for i in (6, 4, 2, 0)
                        )
                        current["gateway"] = gw
                        break
        except OSError:
            pass

        # DNS from /etc/resolv.conf — best-effort. systemd-resolved's
        # 127.0.0.53 stub is dropped so the pane shows upstream servers.
        dns: list[str] = []
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                if line.startswith("nameserver "):
                    ns = line.split()[1]
                    if ns not in ("127.0.0.53",):
                        dns.append(ns)
        except OSError:
            pass
        current["dns"] = dns

        # If the current netplan drop-in exists and matches, report mode.
        cfg_path = Path("/etc/netplan/60-labtris-management.yaml")
        if cfg_path.exists():
            try:
                if "dhcp4: true" in cfg_path.read_text():
                    current["mode"] = "dhcp"
                elif "dhcp4: false" in cfg_path.read_text():
                    current["mode"] = "manual"
            except OSError:
                pass

        return {"interfaces": nics, "current": current}

    def configure_management(
        self,
        mode: str,
        interface: str,
        address: str | None,
        gateway: str | None,
        dns: list[str],
        kind: str = "nic",
    ) -> dict[str, Any]:
        """Write a netplan drop-in for the management NIC and apply it.

        Two files touched: `60-labtris-management.yaml` is written from the
        supplied config, and if `50-cloud-init.yaml` exists it is rewritten
        to an inert placeholder so cloud-init's `en*` glob does not race
        against our explicit interface entry. That race is what put the box
        at 10.124.133.93 instead of the intended IP in an earlier fix.

        `netplan apply` runs synchronously; on some kernels / DHCP servers
        the return can precede the interface actually acquiring an address.
        The API layer should not treat this as ground truth for reachability
        — the browser confirms that by reloading itself at the new URL."""
        if mode not in ("dhcp", "manual"):
            raise NetdFault("EINVAL", f"mode must be dhcp or manual, not {mode!r}")

        # Refuse an interface that's already enslaved — the config would be
        # accepted by netplan but shadowed by the bridge, and the admin
        # would be looking at an address that never appears. This is the
        # gap the Uplink bridges pane opens: br1 exists and owns ens192,
        # then someone picks ens192 in management. Point them at br1.
        if kind == "nic":
            with IPRoute() as ipr:
                idxs = ipr.link_lookup(ifname=interface)
                if idxs:
                    master_idx = ipr.get_links(idxs[0])[0].get_attr("IFLA_MASTER")
                    if master_idx is not None:
                        master_name = _ifname_of(ipr, master_idx)
                        raise NetdFault(
                            "EINVAL",
                            f"{interface} is enslaved to {master_name!r}. "
                            f"Pick {master_name!r} as the target if you meant "
                            "to configure the bridge's address.",
                        )

        try:
            rendered = _render_management_netplan(
                mode, interface, address, gateway, dns, kind=kind
            )
        except ValueError as exc:
            raise NetdFault("EINVAL", str(exc)) from exc

        result = _apply_netplan_write(
            Path("/etc/netplan/60-labtris-management.yaml"), rendered
        )
        return {
            "applied": {
                "mode": mode,
                "interface": interface,
                "address": address,
                "gateway": gateway,
                "dns": dns,
            },
            **result,
        }

    def uplink_probe(self) -> dict[str, Any]:
        """What the Uplink bridges pane needs to render.

        Two lists come back. `eligible_nics` is the physical NICs a new
        uplink bridge could be built from: bare (no LINKINFO kind), not
        already enslaved, and not the NIC that carries the default route
        (that one is management; making it an uplink strands the box).

        `existing_bridges` is the labtris-owned uplink bridges — those
        named `br1`, `br2`, `br3`, … — with the NIC each one is holding.
        This is name-only recognition; a bridge someone else created and
        happens to have called `br1` will show up here, and that's OK: the
        write flow will refuse to co-own it via the netplan file which is
        a promise of exclusivity.
        """
        eligible: list[dict[str, Any]] = []
        existing: list[dict[str, Any]] = []
        default_ifs, _ = self._default_route_state()
        with IPRoute() as ipr:
            by_index = {link["index"]: link for link in ipr.get_links()}
            for link in by_index.values():
                name = link.get_attr("IFLA_IFNAME")
                if not name or name == "lo" or IFNAME_RE.match(name):
                    continue
                info = link.get_attr("IFLA_LINKINFO")
                kind = info.get_attr("IFLA_INFO_KIND") if info is not None else None
                master_idx = link.get_attr("IFLA_MASTER")
                if kind is None:
                    if master_idx is not None:
                        continue  # already a bridge port; not eligible
                    if name in default_ifs:
                        continue  # management NIC; refuse
                    eligible.append({"name": name})
                elif kind == "bridge" and _UPLINK_BRIDGE_RE.match(name):
                    # Which NIC is currently enslaved to this uplink bridge?
                    slave = None
                    for other in by_index.values():
                        if other.get_attr("IFLA_MASTER") == link["index"]:
                            slave_name = other.get_attr("IFLA_IFNAME")
                            other_info = other.get_attr("IFLA_LINKINFO")
                            # Report only the physical NIC slave, not
                            # lab veths hanging off the bridge.
                            other_kind = (
                                other_info.get_attr("IFLA_INFO_KIND")
                                if other_info is not None
                                else None
                            )
                            if other_kind is None and slave_name:
                                slave = slave_name
                                break
                    existing.append({"name": name, "nic": slave})
        # Sort so the UI shows a stable order regardless of kernel
        # enumeration.
        eligible.sort(key=lambda i: i["name"])
        existing.sort(key=lambda i: i["name"])
        return {"eligible_nics": eligible, "existing_bridges": existing}

    def configure_uplinks(self, pairs: list[dict[str, str]]) -> dict[str, Any]:
        """Write the uplink-bridges netplan file and apply it.

        `pairs` is `[{"nic": "ens192"}, {"nic": "ens224"}]`. Bridge names
        are allocated server-side (br1, br2, br3, …) — the client picks
        NICs, not names, so there's no way for two admins racing on the
        UI to argue about which NIC gets which bridge.

        Empty list is legal — it writes an inert netplan file and removes
        every previously-allocated uplink bridge. The caller (API layer)
        should have already checked that no lab network still references
        the bridges that are about to disappear.
        """
        # Assign br1, br2, … in the order NICs were supplied. Deterministic
        # per input, but not stable across two different-input applies —
        # applying [{nic: ens192}, {nic: ens224}] then [{nic: ens224}] gets
        # ens224=br2 on the first apply and ens224=br1 on the second, and
        # any Cloud network that referenced br2 would need updating. The
        # API layer refuses removal while references exist, so a "shift"
        # like this is a two-step choreography (delete Cloud, apply, add
        # Cloud) — the naming is fine.
        seen: set[str] = set()
        assigned: list[tuple[str, str]] = []
        for i, entry in enumerate(pairs, start=1):
            nic = entry.get("nic") if isinstance(entry, dict) else None
            if not isinstance(nic, str):
                raise NetdFault("EINVAL", "each pair needs 'nic': <string>")
            if nic in seen:
                raise NetdFault("EINVAL", f"nic {nic!r} appears more than once")
            seen.add(nic)
            assigned.append((nic, f"br{i}"))

        try:
            rendered = _render_uplink_bridges_netplan(assigned)
        except ValueError as exc:
            raise NetdFault("EINVAL", str(exc)) from exc

        result = _apply_netplan_write(
            Path("/etc/netplan/60-labtris-cloud.yaml"), rendered
        )
        return {
            "applied": [
                {"nic": nic, "bridge": br} for nic, br in assigned
            ],
            **result,
        }

    def host_capabilities(self) -> dict[str, Any]:
        """What this kernel can actually do, probed rather than assumed.

        Whether a device type exists is not something /proc or /sys will tell
        you — the module may be built in, absent, or present but blocked. The
        only honest answer is to try to create one and delete it again, so
        that is what this does, once per netd process. It exists because the
        VXLAN overlay silently looks like a bug when the real answer is
        "this kernel has no vxlan device type" (ENOTSUP on link add)."""
        global _CAPABILITIES
        if _CAPABILITIES is not None:
            return _CAPABILITIES
        links: dict[str, Any] = {}
        with IPRoute() as ipr:
            for kind, kwargs in _PROBE_LINKS.items():
                name = f"pnlprobe{len(links)}"
                try:
                    ipr.link("add", ifname=name, kind=kind, **kwargs)
                except NetlinkError as exc:
                    links[kind] = {"supported": False, "errno": exc.code, "error": str(exc)}
                    continue
                links[kind] = {"supported": True}
                try:
                    ipr.link("del", index=_lookup(ipr, name))
                except (NetlinkError, NetdFault):
                    pass
        _CAPABILITIES = {
            "links": links,
            "tuntap": Path("/dev/net/tun").exists(),
            "tools": {
                tool: shutil.which(tool) is not None for tool in ("tcpdump", "ip", "tc", "qemu-img")
            },
        }
        return _CAPABILITIES

    def host_interfaces(self) -> dict[str, Any]:
        """The host's real NICs and OS-owned bridges, for backing a `cloud`
        network.

        Two shapes reach the picker: a bare NIC that Labtris will enslave into
        a fresh bridge (the EVE-NG-installer-not-run case), and a bridge the OS
        already put together (EVE-NG's `pnet0`, netplan's `br0` on a
        pre-bridged management NIC) which Labtris will attach lab veths to
        directly, without touching enslavement. That second shape is marked
        `reusable=True` — the API's create path branches on it.

        Lab-owned devices are filtered out (they match IFNAME_RE), as is
        loopback. Each entry carries the addresses it holds and whether the
        default route's source lives on it, so the UI can warn before the
        user picks the wrong one."""
        default_ifs, default_src_addrs = self._default_route_state()

        out: list[dict[str, Any]] = []
        with IPRoute() as ipr:
            addrs: dict[int, list[str]] = {}
            for addr in ipr.get_addr():
                ip = addr.get_attr("IFA_ADDRESS")
                if ip:
                    addrs.setdefault(addr["index"], []).append(f"{ip}/{addr['prefixlen']}")
            for link in ipr.get_links():
                name = link.get_attr("IFLA_IFNAME")
                if not name or name == "lo" or IFNAME_RE.match(name):
                    continue
                info = link.get_attr("IFLA_LINKINFO")
                kind = info.get_attr("IFLA_INFO_KIND") if info is not None else None
                enslaved = link.get_attr("IFLA_MASTER") is not None
                iface_addrs = addrs.get(link["index"], [])
                # A bridge is fine to *reuse* (attach veths to) but never fine
                # to *enslave* into another bridge — the kernel returns ELOOP.
                # docker0 is the one bridge Labtris must never touch.
                # A NIC already held by a non-Labtris bridge is unusable
                # standalone: pick the bridge holding it instead.
                reason = None
                reusable = False
                if kind == "bridge":
                    if name == "docker0":
                        reason = "Docker's bridge — Labtris will not touch it"
                    else:
                        reusable = True
                elif enslaved:
                    reason = "already enslaved to a bridge — pick that bridge instead"
                has_default_route_addr = any(
                    a.split("/")[0] in default_src_addrs for a in iface_addrs
                )
                out.append(
                    {
                        "name": name,
                        "index": link["index"],
                        "kind": kind,
                        "up": link["flags"] & 1 == 1,
                        "enslaved": enslaved,
                        "addresses": iface_addrs,
                        "default_route": name in default_ifs,
                        # True if this interface (usually a bridge) holds the
                        # address the default route is sourced from. Enslaving
                        # a NIC that carries the default route breaks the
                        # host; reusing a bridge that carries it does not,
                        # but the UI still wants to prompt.
                        "has_default_route_addr": has_default_route_addr,
                        "reusable": reusable,
                        "usable": reason is None,
                        "unusable_reason": reason,
                    }
                )
        return {"interfaces": sorted(out, key=lambda i: i["name"])}

    def _default_route_ifaces(self) -> set[str]:
        names, _ = self._default_route_state()
        return names

    def _default_route_state(self) -> tuple[set[str], set[str]]:
        """Names of interfaces carrying a default route, and source addresses
        the kernel is currently using to reach the default gateway. The second
        is what distinguishes "this bridge holds the management IP" from
        "some other bridge" — enslaving the management NIC is dangerous;
        reusing a bridge that already owns the IP is not, but the UI still
        wants to prompt in both cases."""
        names: set[str] = set()
        try:
            with open("/proc/net/route") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "00000000":
                        names.add(parts[0])
        except OSError:
            pass
        srcs: set[str] = set()
        try:
            with IPRoute() as ipr:
                for route in ipr.get_routes(family=2):  # AF_INET
                    if route.get_attr("RTA_DST") is None:
                        # default route: no destination attribute
                        src = route.get_attr("RTA_PREFSRC")
                        if src:
                            srcs.add(src)
        except Exception:  # noqa: BLE001 — route probe is advisory
            pass
        return names, srcs

    def inspect_bridge(self, name: str) -> dict[str, Any]:
        """Point-lookup counterpart to host_interfaces for the reuse path.
        The API asks this before committing a cloud row that names an existing
        OS bridge, so we can refuse a name that isn't actually a bridge (or
        isn't a Docker exception, etc.) with a specific error rather than the
        first failed netlink call downstream. Returns `{exists, kind, up,
        addresses, member_count, has_default_route_addr}` for whatever `name`
        resolves to. `exists=False` means no interface by that name at all;
        `kind != "bridge"` means the name resolves but isn't a bridge."""
        _, default_src_addrs = self._default_route_state()
        with IPRoute() as ipr:
            idxs = ipr.link_lookup(ifname=name)
            if not idxs:
                return {"exists": False}
            link = ipr.get_links(idxs[0])[0]
            info = link.get_attr("IFLA_LINKINFO")
            kind = info.get_attr("IFLA_INFO_KIND") if info is not None else None
            iface_addrs = []
            for addr in ipr.get_addr(index=idxs[0]):
                ip = addr.get_attr("IFA_ADDRESS")
                if ip:
                    iface_addrs.append(f"{ip}/{addr['prefixlen']}")
            member_count = sum(
                1 for other in ipr.get_links() if other.get_attr("IFLA_MASTER") == idxs[0]
            )
        return {
            "exists": True,
            "kind": kind,
            "up": link["flags"] & 1 == 1,
            "addresses": iface_addrs,
            "member_count": member_count,
            "has_default_route_addr": any(
                a.split("/")[0] in default_src_addrs for a in iface_addrs
            ),
        }

    def cloud_attach(self, name: str, bridge: str, force: bool) -> dict[str, Any]:
        """Enslave one of the host's own NICs to a lab bridge — the `cloud`
        network, EVE-NG's pnet: the lab's L2 segment and the outside world
        become the same broadcast domain.

        This is genuinely dangerous on the wrong interface. Enslaving a NIC
        moves its traffic to the bridge, and the host's address does not follow
        it, so doing this to the interface carrying the default route takes the
        machine off the network — including whatever session issued the
        request. Refused unless the caller says explicitly that it meant it."""
        with IPRoute() as ipr:
            info = ipr.get_links(_lookup(ipr, name))[0].get_attr("IFLA_LINKINFO")
            if info is not None and info.get_attr("IFLA_INFO_KIND") == "bridge":
                # force does not help here: the kernel will not nest bridges,
                # and the raw failure (ELOOP) reads like a filesystem error.
                raise NetdFault(
                    "EINVAL",
                    f"{name} is itself a bridge, so it cannot be enslaved to {bridge}. "
                    "A cloud network needs a physical NIC or a veth — pick the "
                    "interface that carries the traffic, not a bridge over it.",
                )
        if not force and name in self._default_route_ifaces():
            raise NetdFault(
                "EPERM",
                f"{name} carries this host's default route; enslaving it to {bridge} "
                "would take the host off the network. Pass force to override.",
            )
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            # A NIC can only be enslaved to one bridge, so a second attach
            # *moves* it — silently cutting the uplink out from under whoever
            # had it, who keeps a bridge that looks connected and goes nowhere.
            current = ipr.get_links(idx)[0].get_attr("IFLA_MASTER")
            if current is not None and not force:
                held = _ifname_of(ipr, current)
                if held != bridge:
                    raise NetdFault(
                        "EBUSY",
                        f"{name} is already enslaved to {held}; detach it first "
                        "rather than moving it and breaking that bridge's uplink",
                    )
            master = _lookup(ipr, bridge)
            try:
                ipr.link("set", index=idx, master=master)
                ipr.link("set", index=idx, state="up")
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {"index": idx, "master": master}

    def cloud_detach(self, name: str) -> dict[str, Any]:
        """Give a host NIC back: release it from whatever bridge holds it."""
        with IPRoute() as ipr:
            idx = _lookup(ipr, name)
            try:
                ipr.link("set", index=idx, master=0)
            except NetlinkError as exc:
                raise _map_nl(exc, name) from exc
        return {"index": idx}

    def vxlan_create(
        self, name: str, vni: int, remote: str, local: str | None, dstport: int
    ) -> dict[str, Any]:
        """Extend an L2 segment to a peer host — F6's overlay primitive
        (`docs/04-scaling.md`, containerlab's `tools vxlan`). Requires the
        kernel's vxlan device type; on hosts without it (some sandboxed /
        minimal kernels build in only bridge+veth+tuntap) this raises
        ENOTSUP rather than silently no-op'ing."""
        kwargs: dict[str, Any] = {
            "ifname": name,
            "kind": "vxlan",
            "vxlan_id": vni,
            "vxlan_group": remote,
            "vxlan_port": dstport,
        }
        if local:
            kwargs["vxlan_local"] = local
        with IPRoute() as ipr:
            try:
                ipr.link("add", **kwargs)
            except NetlinkError as exc:
                if exc.code in (95, 524):  # EOPNOTSUPP / ENOTSUPP
                    raise NetdFault(
                        "ENOTSUP", f"vxlan device type unsupported by this kernel: {exc}"
                    ) from exc
                raise _map_nl(exc, name) from exc
            idx = _lookup(ipr, name)
            ipr.link("set", index=idx, state="up")
            return {"index": idx}

    def vxlan_delete(self, name: str) -> dict[str, Any]:
        return self.iface_delete(name)


#: Our own table. Everything NAT-related lives here so tearing a lab network
#: down is a delete of rules we created, never a flush of the host's firewall.
def _in_subnet(addr: str, subnet: str) -> bool:
    """Is this dotted-quad inside this CIDR? Written out rather than using
    ipaddress so netd keeps no import it does not already need for netlink."""
    try:
        net, bits = subnet.split("/")
        mask = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF
        pack = lambda a: sum(int(p) << sh for p, sh in zip(a.split("."), (24, 16, 8, 0)))
        return (pack(addr) & mask) == (pack(net) & mask)
    except (ValueError, AttributeError):
        return False


def _conntrack_for(raw: str, subnet: str) -> list[dict[str, Any]]:
    """Parse /proc/net/nf_conntrack down to the flows out of one lab subnet.

    A line carries the original tuple and then the reply tuple. For a
    masqueraded flow the reply's destination is the address the host rewrote it
    to, which is the one piece of the story a capture inside the lab cannot
    show — so it is what makes this view worth having.
    """
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        proto = next(
            (p for p in parts[:4] if p in ("tcp", "udp", "icmp", "sctp", "dccp", "gre")),
            "",
        )
        fields: list[dict[str, str]] = [{}, {}]
        which = 0
        for token in parts:
            key, _, value = token.partition("=")
            if not value:
                continue
            if key in fields[which]:
                which = 1          # the second src= starts the reply tuple
            if which < 2:
                fields[which][key] = value
        orig, reply = fields
        src = orig.get("src")
        if not src or not _in_subnet(src, subnet):
            continue
        out.append(
            {
                "proto": proto,
                "state": next((p for p in parts if p.isupper() and len(p) > 3), ""),
                "src": src,
                "sport": orig.get("sport", ""),
                "dst": orig.get("dst", ""),
                "dport": orig.get("dport", ""),
                #: What the outside sees this flow as.
                "translated": reply.get("dst", ""),
            }
        )
    return out


_NFT_TABLE = "labtris"
#: The lab's own bridges. Traffic leaving through one of these is going to
#: another lab segment, not to the outside, and must not be rewritten.
_NFT_BRIDGES = "labtris_bridges"


def _nft(*args: str) -> str:
    nft = shutil.which("nft")
    if not nft:
        raise NetdFault("ENOENT", "nft is not installed — NAT networks need nftables")
    proc = subprocess.run([nft, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise NetdFault("EINVAL", f"nft {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _nft_table_ensure() -> None:
    # `add` is idempotent for tables and chains; `create` would fail the second
    # time and there is no natural "first" caller to own creation.
    _nft("add", "table", "inet", _NFT_TABLE)
    _nft(
        "add", "set", "inet", _NFT_TABLE, _NFT_BRIDGES,
        "{", "type", "ifname", ";", "}",
    )
    _nft(
        "add", "chain", "inet", _NFT_TABLE, "postrouting",
        "{", "type", "nat", "hook", "postrouting", "priority", "100", ";", "}",
    )
    # Priority 0 with a policy of accept: this chain only adds accepts for our
    # own bridges, and must not become the thing that decides the host's
    # default forwarding stance.
    _nft(
        "add", "chain", "inet", _NFT_TABLE, "forward",
        "{", "type", "filter", "hook", "forward", "priority", "0", ";", "policy", "accept", ";", "}",
    )


def _nft_list() -> list[tuple[int, str]]:
    """Every rule in our table as (handle, text), for targeted deletion."""
    out: list[tuple[int, str]] = []
    try:
        listing = _nft("-a", "list", "table", "inet", _NFT_TABLE)
    except NetdFault:
        return out
    for line in listing.splitlines():
        if "# handle " not in line:
            continue
        try:
            handle = int(line.rsplit("# handle ", 1)[1].strip())
        except (IndexError, ValueError):
            continue
        out.append((handle, line))
    return out


def _chain_of(rule_line: str) -> str:
    """Which chain a listed rule belongs to, inferred from what it does."""
    return "postrouting" if "masquerade" in rule_line else "forward"


def _port_vlans(ipr: Any, idx: int) -> list[int]:
    """VLAN ids currently configured on a bridge port."""
    vids: list[int] = []
    try:
        for link in ipr.get_vlans(index=idx):
            info = link.get_attr("IFLA_AF_SPEC")
            if info is None:
                continue
            for entry in info.get_attrs("IFLA_BRIDGE_VLAN_INFO"):
                vid = entry["vid"] if isinstance(entry, dict) else getattr(entry, "vid", None)
                if vid:
                    vids.append(int(vid))
    except Exception:
        # Listing failing must not stop us configuring the port: worst case a
        # stale VLAN survives, which the caller can see and fix.
        return []
    return vids


_SYSCTL_RE = re.compile(r"^[a-z0-9_.]+$")


class _Captures:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, subprocess.Popen[str]] = {}
        self._bufs: dict[str, deque[str]] = {}

    def start(self, name: str, bpf: str) -> dict[str, Any]:
        tcpdump = shutil.which("tcpdump")
        if not tcpdump:
            raise NetdFault("ENOENT", "tcpdump is not installed")
        self.stop(name)
        cmd = [tcpdump, "-i", name, "-nn", "-l", "-e", "-tt"]
        if bpf.strip():
            cmd.extend(bpf.split())
        buf: deque[str] = deque(maxlen=500)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                buf.append(line.rstrip())

        threading.Thread(target=_reader, daemon=True).start()
        with self._lock:
            self._jobs[name] = proc
            self._bufs[name] = buf
        return {"started": name}

    def stop(self, name: str) -> dict[str, Any]:
        with self._lock:
            proc = self._jobs.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        return {}

    def read(self, name: str) -> dict[str, Any]:
        with self._lock:
            buf = self._bufs.get(name)
            lines = list(buf) if buf is not None else []
            if buf is not None:
                buf.clear()
        return {"lines": lines}


_captures = _Captures()

class _Dhcp:
    """One dnsmasq per NAT network, supervised the way captures are.

    dnsmasq rather than a hand-rolled server because DHCP is a protocol with
    a long tail — relay agents, option 121, lease persistence — and getting it
    subtly wrong shows up as a guest that boots without an address once a week.

    Bound to the bridge with --interface and --bind-interfaces, so it answers
    on that segment and nowhere else. A NAT network on a host that already runs
    a DHCP server on its LAN must not start answering that LAN's requests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, subprocess.Popen[str]] = {}
        self._dir = Path("/run/labtris/dhcp")

    def start(
        self,
        key: str,
        bridge: str,
        gateway: str,
        prefix: int,
        first: str,
        last: str,
        dns: str | None,
        hosts: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        dnsmasq = shutil.which("dnsmasq")
        if not dnsmasq:
            raise NetdFault("ENOENT", "dnsmasq is not installed")
        self.stop(key)
        self._dir.mkdir(parents=True, exist_ok=True)
        leases = self._dir / f"{key}.leases"
        netmask = _prefix_to_mask(prefix)
        cmd = [
            dnsmasq,
            "--keep-in-foreground",
            "--conf-file=/dev/null",
            f"--interface={bridge}",
            # Without both of these dnsmasq listens on the wildcard address and
            # will answer DHCP for every segment on the host, including the
            # one the host itself is managed over.
            "--bind-interfaces",
            "--except-interface=lo",
            f"--listen-address={gateway}",
            f"--dhcp-range={first},{last},{netmask},12h",
            f"--dhcp-option=option:router,{gateway}",
            f"--dhcp-leasefile={leases}",
            # No /etc/hosts, no /etc/resolv.conf, no upstream unless we say so:
            # this serves one lab segment, it is not the host's resolver.
            "--no-hosts",
            "--no-resolv",
            "--log-facility=-",
        ]
        cmd.append(f"--dhcp-option=option:dns-server,{dns or gateway}")
        if dns:
            cmd.append(f"--server={dns}")
        # Reservations. A lab where r1 is 10.0.0.1 every time is worth a great
        # deal more than one where it is whatever the pool handed out, and the
        # MAC is already known because we allocated it.
        for host in hosts or []:
            cmd.append(f"--dhcp-host={host['mac']},{host['ip']}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        # A dnsmasq that dies on a bad range should be reported now, not
        # discovered by a guest that never gets an address.
        try:
            proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            pass
        else:
            out = (proc.stdout.read() if proc.stdout else "").strip()
            raise NetdFault("EINVAL", f"dnsmasq exited immediately: {out[:300]}")
        with self._lock:
            self._jobs[key] = proc
        return {"started": key, "pid": proc.pid}

    def stop(self, key: str) -> dict[str, Any]:
        with self._lock:
            proc = self._jobs.pop(key, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        leases = self._dir / f"{key}.leases"
        leases.unlink(missing_ok=True)
        return {}

    def leases(self, key: str) -> dict[str, Any]:
        """What dnsmasq has actually handed out, straight from its lease file."""
        path = self._dir / f"{key}.leases"
        rows: list[dict[str, str]] = []
        try:
            for line in path.read_text().splitlines():
                parts = line.split()
                # expiry mac ip hostname client-id
                if len(parts) >= 4:
                    rows.append({"mac": parts[1], "ip": parts[2], "name": parts[3]})
        except OSError:
            pass
        return {"leases": rows}


def _prefix_to_mask(prefix: int) -> str:
    bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return ".".join(str((bits >> shift) & 0xFF) for shift in (24, 16, 8, 0))


_dhcp = _Dhcp()



def _ifname_of(ipr: IPRoute, index: int) -> str:
    links = ipr.get_links(index)
    name = links[0].get_attr("IFLA_IFNAME") if links else None
    return str(name) if name else f"index {index}"


def _lookup(ipr: IPRoute, name: str) -> int:
    found = ipr.link_lookup(ifname=name)
    if not found:
        raise NetdFault("ENOENT", f"{name} not found")
    return int(found[0])


def _finish_move(name: str, rename_to: str | None, mac: str | None, up: bool) -> None:
    with IPRoute() as ipr:
        idx = _lookup(ipr, name)
        kwargs: dict[str, Any] = {"index": idx}
        if rename_to:
            kwargs["ifname"] = rename_to
        if mac:
            kwargs["address"] = mac.lower()
        if len(kwargs) > 1:
            ipr.link("set", **kwargs)
        if up:
            ipr.link("set", index=_lookup(ipr, rename_to or name), state="up")


def _in_netns(pid: int, fn: Any) -> None:
    nsfd = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
    self_fd = os.open("/proc/self/ns/net", os.O_RDONLY)
    try:
        if _libc.setns(nsfd, CLONE_NEWNET) != 0:
            err = ctypes.get_errno()
            raise NetdFault("EINTERNAL", f"setns failed: {os.strerror(err)}")
        try:
            fn()
        finally:
            if _libc.setns(self_fd, CLONE_NEWNET) != 0:
                err = ctypes.get_errno()
                raise NetdFault("EINTERNAL", f"setns restore failed: {os.strerror(err)}")
    finally:
        os.close(nsfd)
        os.close(self_fd)
