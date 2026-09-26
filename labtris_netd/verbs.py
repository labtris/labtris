from __future__ import annotations

from typing import Any, Protocol

from labtris_netd.protocol import IFNAME_RE

VERBS = frozenset(
    {
        "ping",
        "bridge.create",
        "bridge.delete",
        "bridge.list",
        "ovs.available",
        "ovs.bridge_create",
        "ovs.bridge_delete",
        "tap.create",
        "tap.delete",
        "iface.attach",
        "iface.detach",
        "iface.set_state",
        "iface.delete",
        "veth.create",
        "netns.move",
        "netns.links",
        "iface.inspect",
        "tc.set",
        "tc.read",
        "tc.clear",
        "capture.start",
        "capture.stop",
        "capture.read",
        "host.tune",
        "host.tuning_read",
        "host.capabilities",
        "host.interfaces",
        "host.inspect_bridge",
        "host.management_probe",
        "host.configure_management",
        "host.uplink_probe",
        "host.configure_uplinks",
        "cloud.attach",
        "cloud.detach",
        "vxlan.create",
        "vxlan.delete",
        "addr.replace",
        "addr.flush",
        "nat.enable",
        "nat.disable",
        "bridge.vlan_aware",
        "bridge.port_vlan",
        "dhcp.start",
        "dhcp.stop",
        "dhcp.leases",
        "nat.sessions",
        # Lab-scoped ready hooks (labtris_api/runtime/hooks.py). Both run
        # inside a target container's netns via nsenter, keyed on the
        # container PID the caller supplied.
        "hook.ping",
        "hook.http",
        # Batched IFLA_STATS64 read for a list of interfaces. Used by
        # the per-link traffic overlay on the canvas — one call per
        # lab poll, not one per link.
        "iface.counters",
    }
)

MAC_RE = __import__("re").compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
IPV4_RE = __import__("re").compile(r"^(\d{1,3}\.){3}\d{1,3}$")


class NetOps(Protocol):
    def bridge_create(self, name: str) -> dict[str, Any]: ...
    def bridge_delete(self, name: str) -> dict[str, Any]: ...
    def bridge_list(self) -> dict[str, Any]: ...
    def tap_create(self, name: str, owner_uid: int) -> dict[str, Any]: ...
    def tap_delete(self, name: str) -> dict[str, Any]: ...
    def iface_attach(self, name: str, bridge: str) -> dict[str, Any]: ...
    def iface_detach(self, name: str) -> dict[str, Any]: ...
    def iface_set_state(self, name: str, up: bool) -> dict[str, Any]: ...
    def iface_delete(self, name: str) -> dict[str, Any]: ...
    def veth_create(self, name: str, peer: str) -> dict[str, Any]: ...
    def addr_replace(self, name: str, address: str, prefix: int) -> dict[str, Any]: ...
    def addr_flush(self, name: str) -> dict[str, Any]: ...
    def nat_enable(self, subnet: str, bridge: str) -> dict[str, Any]: ...
    def nat_disable(self, subnet: str, bridge: str) -> dict[str, Any]: ...
    def bridge_vlan_aware(self, name: str, on: bool, ethertype: int) -> dict[str, Any]: ...
    def bridge_port_vlan(
        self, name: str, pvid: int | None, tagged: list[int], untagged: list[int]
    ) -> dict[str, Any]: ...
    def dhcp_start(
        self, key: str, bridge: str, gateway: str, prefix: int,
        first: str, last: str, dns: str | None,
        hosts: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]: ...
    def nat_sessions(self, subnet: str) -> dict[str, Any]: ...
    def dhcp_stop(self, key: str) -> dict[str, Any]: ...
    def dhcp_leases(self, key: str) -> dict[str, Any]: ...
    def netns_move(
        self,
        name: str,
        pid: int,
        rename_to: str | None = None,
        mac: str | None = None,
        up: bool = True,
    ) -> dict[str, Any]: ...
    def netns_links(self, pids: dict[str, int]) -> dict[str, Any]: ...
    def ovs_available(self) -> dict[str, Any]: ...
    def ovs_bridge_create(self, name: str) -> dict[str, Any]: ...
    def ovs_bridge_delete(self, name: str) -> dict[str, Any]: ...
    def tc_set(self, name: str, spec: dict[str, Any]) -> dict[str, Any]: ...
    def tc_read(self, name: str) -> dict[str, Any]: ...
    def iface_inspect(self, names: list[str]) -> dict[str, Any]: ...
    def tc_clear(self, name: str) -> dict[str, Any]: ...
    def capture_start(self, name: str, bpf: str) -> dict[str, Any]: ...
    def capture_stop(self, name: str) -> dict[str, Any]: ...
    def capture_read(self, name: str) -> dict[str, Any]: ...
    def host_tune(self, sysctls: dict[str, str]) -> dict[str, Any]: ...
    def host_tuning_read(self, keys: list[str]) -> dict[str, Any]: ...
    def host_capabilities(self) -> dict[str, Any]: ...
    def host_interfaces(self) -> dict[str, Any]: ...
    def inspect_bridge(self, name: str) -> dict[str, Any]: ...
    def management_probe(self) -> dict[str, Any]: ...
    def configure_management(
        self,
        mode: str,
        interface: str,
        address: str | None,
        gateway: str | None,
        dns: list[str],
        kind: str = "nic",
    ) -> dict[str, Any]: ...
    def uplink_probe(self) -> dict[str, Any]: ...
    def configure_uplinks(self, pairs: list[dict[str, str]]) -> dict[str, Any]: ...
    def cloud_attach(self, name: str, bridge: str, force: bool) -> dict[str, Any]: ...
    def cloud_detach(self, name: str) -> dict[str, Any]: ...
    def vxlan_create(
        self, name: str, vni: int, remote: str, local: str | None, dstport: int
    ) -> dict[str, Any]: ...
    def vxlan_delete(self, name: str) -> dict[str, Any]: ...
    def hook_ping(
        self, pid: int, target: str, count: int, timeout_s: int
    ) -> dict[str, Any]: ...
    def hook_http(self, pid: int, url: str, timeout_s: int) -> dict[str, Any]: ...
    def iface_counters(self, names: list[str]) -> dict[str, Any]: ...


class NetdFault(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# A real kernel interface name, as opposed to a lab-allocated one. Enslaving a
# host NIC to a lab bridge is the one operation that legitimately names an
# interface this daemon did not create, so it gets its own validation rather
# than loosening IFNAME_RE for everything.
HOST_IFNAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,14}$")


def _require_host_ifname(value: Any) -> str:
    if not isinstance(value, str) or not HOST_IFNAME_RE.match(value):
        raise NetdFault("EINVAL", "malformed host interface name")
    if value == "lo":
        raise NetdFault("EINVAL", "refusing to bridge the loopback interface")
    return value


def _require_ifname(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not IFNAME_RE.match(value):
        raise NetdFault("EINVAL", f"malformed {field}")
    return value


#: The kernel wants the ethertype, not the name people use for it. Converting
#: here rather than in net.py keeps it on the validation boundary — and passing
#: the string through got "required argument is not an integer" from netlink,
#: an error naming neither the argument nor what it wanted.
ETHERTYPES = {"802.1Q": 0x8100, "802.1ad": 0x88A8}

CIDR_RE = __import__("re").compile(r"^(\d{1,3}\.){3}\d{1,3}/(3[0-2]|[12]?\d)$")
#: Keys name a dnsmasq instance and become a filename under /run. Restricted so
#: they cannot walk out of that directory.
KEY_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,64}$")


def _require_ipv4(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IPV4_RE.match(value):
        raise NetdFault("EINVAL", f"malformed {field}")
    if any(int(part) > 255 for part in value.split(".")):
        raise NetdFault("EINVAL", f"malformed {field}")
    return value


def _require_prefix(value: Any) -> int:
    if not isinstance(value, int) or not 0 <= value <= 32:
        raise NetdFault("EINVAL", "prefix must be 0-32")
    return value


def _require_cidr(value: Any) -> str:
    if not isinstance(value, str) or not CIDR_RE.match(value):
        raise NetdFault("EINVAL", "malformed subnet")
    host = value.split("/")[0]
    if any(int(part) > 255 for part in host.split(".")):
        raise NetdFault("EINVAL", "malformed subnet")
    return value


def _require_key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise NetdFault("EINVAL", "malformed key")
    return value


def _require_hosts(value: Any) -> list[dict[str, str]]:
    """DHCP reservations. Each pair becomes a --dhcp-host argument, so a MAC
    that is not a MAC would be read by dnsmasq as something else entirely."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise NetdFault("EINVAL", "hosts must be a list")
    out: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise NetdFault("EINVAL", "each host must be an object")
        mac, ip = entry.get("mac"), entry.get("ip")
        if not isinstance(mac, str) or not MAC_RE.match(mac):
            raise NetdFault("EINVAL", f"malformed reservation MAC {mac!r}")
        out.append({"mac": mac, "ip": _require_ipv4(ip, "reservation ip")})
    return out


def _require_vid(value: Any, field: str) -> int:
    # 0 and 4095 are reserved; the kernel rejects them and the error it gives
    # says nothing about which VLAN was wrong.
    if not isinstance(value, int) or not 1 <= value <= 4094:
        raise NetdFault("EINVAL", f"{field} must be a VLAN id 1-4094")
    return value


def _require_vids(value: Any, field: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise NetdFault("EINVAL", f"{field} must be a list")
    return [_require_vid(v, field) for v in value]


def dispatch(request: dict[str, Any], net: NetOps) -> dict[str, Any]:
    req_id = request.get("id")
    verb = request.get("verb")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _fail(req_id, "EINVAL", "params must be an object")
    if verb not in VERBS:
        return _fail(req_id, "EINVAL", f"unknown verb {verb!r}")
    try:
        result = _call(verb, params, net)
        return {"id": req_id, "ok": True, "result": result}
    except NetdFault as exc:
        return _fail(req_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - a bug in one verb must not kill the connection
        return _fail(req_id, "EINTERNAL", f"{type(exc).__name__}: {exc}")


def _fail(req_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": {"code": code, "message": message}}


def _call(verb: str, params: dict[str, Any], net: NetOps) -> dict[str, Any]:
    if verb == "ping":
        return {"pong": True, "version": "1"}
    if verb == "bridge.create":
        return net.bridge_create(_require_ifname(params.get("name")))
    if verb == "bridge.delete":
        return net.bridge_delete(_require_ifname(params.get("name")))
    if verb == "bridge.list":
        return net.bridge_list()
    if verb == "tap.create":
        uid = params.get("owner_uid")
        if not isinstance(uid, int):
            raise NetdFault("EINVAL", "owner_uid must be int")
        return net.tap_create(_require_ifname(params.get("name")), uid)
    if verb == "tap.delete":
        return net.tap_delete(_require_ifname(params.get("name")))
    if verb == "iface.attach":
        # The bridge may be a Labtris bridge (b…-xxxx) or a host-owned one
        # a `cloud` network is reusing (br0, pnet0). Both are legal targets;
        # the lab-owned name here (`name`) is not.
        return net.iface_attach(
            _require_ifname(params.get("name")),
            _require_host_ifname(params.get("bridge")),
        )
    if verb == "iface.detach":
        return net.iface_detach(_require_ifname(params.get("name")))
    if verb == "iface.set_state":
        up = params.get("up")
        if not isinstance(up, bool):
            raise NetdFault("EINVAL", "up must be bool")
        return net.iface_set_state(_require_ifname(params.get("name")), up)
    if verb == "iface.delete":
        return net.iface_delete(_require_ifname(params.get("name")))
    if verb == "iface.inspect":
        names = params.get("names")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise NetdFault("EINVAL", "names must be a list of strings")
        return net.iface_inspect([_require_ifname(n) for n in names])
    if verb == "iface.counters":
        names = params.get("names")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise NetdFault("EINVAL", "names must be a list of strings")
        return net.iface_counters([_require_ifname(n) for n in names])
    if verb == "addr.replace":
        return net.addr_replace(
            _require_ifname(params.get("name")),
            _require_ipv4(params.get("address"), "address"),
            _require_prefix(params.get("prefix")),
        )
    if verb == "addr.flush":
        return net.addr_flush(_require_ifname(params.get("name")))
    if verb == "nat.enable":
        return net.nat_enable(
            _require_cidr(params.get("subnet")),
            _require_ifname(params.get("bridge"), "bridge"),
        )
    if verb == "nat.disable":
        return net.nat_disable(
            _require_cidr(params.get("subnet")),
            _require_ifname(params.get("bridge"), "bridge"),
        )
    if verb == "bridge.vlan_aware":
        on = params.get("on")
        if not isinstance(on, bool):
            raise NetdFault("EINVAL", "on must be bool")
        proto = params.get("proto", "802.1Q")
        if proto not in ETHERTYPES:
            raise NetdFault("EINVAL", "proto must be 802.1Q or 802.1ad")
        return net.bridge_vlan_aware(
            _require_ifname(params.get("name")), on, ETHERTYPES[proto]
        )
    if verb == "bridge.port_vlan":
        pvid = params.get("pvid")
        if pvid is not None:
            pvid = _require_vid(pvid, "pvid")
        return net.bridge_port_vlan(
            _require_ifname(params.get("name")),
            pvid,
            _require_vids(params.get("tagged"), "tagged"),
            _require_vids(params.get("untagged"), "untagged"),
        )
    if verb == "dhcp.start":
        return net.dhcp_start(
            _require_key(params.get("key")),
            _require_ifname(params.get("bridge"), "bridge"),
            _require_ipv4(params.get("gateway"), "gateway"),
            _require_prefix(params.get("prefix")),
            _require_ipv4(params.get("first"), "first"),
            _require_ipv4(params.get("last"), "last"),
            _require_ipv4(params.get("dns"), "dns") if params.get("dns") else None,
            _require_hosts(params.get("hosts")),
        )
    if verb == "dhcp.stop":
        return net.dhcp_stop(_require_key(params.get("key")))
    if verb == "nat.sessions":
        return net.nat_sessions(_require_cidr(params.get("subnet")))
    if verb == "dhcp.leases":
        return net.dhcp_leases(_require_key(params.get("key")))
    if verb == "tc.read":
        return net.tc_read(_require_ifname(params.get("name")))
    if verb == "veth.create":
        return net.veth_create(
            _require_ifname(params.get("name")),
            _require_ifname(params.get("peer"), "peer"),
        )
    if verb == "ovs.available":
        return net.ovs_available()
    if verb == "ovs.bridge_create":
        return net.ovs_bridge_create(_require_ifname(params.get("name")))
    if verb == "ovs.bridge_delete":
        return net.ovs_bridge_delete(_require_ifname(params.get("name")))
    if verb == "netns.links":
        pids = params.get("pids")
        if not isinstance(pids, dict) or not pids:
            raise NetdFault("EINVAL", "pids must be a non-empty object")
        if len(pids) > 512:
            raise NetdFault("EINVAL", "pids must name at most 512 namespaces")
        clean: dict[str, int] = {}
        for key, value in pids.items():
            if not isinstance(key, str) or not key:
                raise NetdFault("EINVAL", "pids keys must be non-empty strings")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise NetdFault("EINVAL", f"pid for {key!r} must be a positive int")
            clean[key] = value
        return net.netns_links(clean)
    if verb == "netns.move":
        pid = params.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise NetdFault("EINVAL", "pid must be a positive int")
        rename_to = params.get("rename_to")
        if rename_to is not None and (not isinstance(rename_to, str) or not rename_to):
            raise NetdFault("EINVAL", "rename_to malformed")
        mac = params.get("mac")
        if mac is not None and (not isinstance(mac, str) or not MAC_RE.match(mac)):
            raise NetdFault("EINVAL", "mac malformed")
        up = params.get("up", True)
        if not isinstance(up, bool):
            raise NetdFault("EINVAL", "up must be bool")
        return net.netns_move(
            _require_ifname(params.get("name")),
            pid,
            rename_to=rename_to,
            mac=mac,
            up=up,
        )
    if verb == "tc.set":
        spec = params.get("spec")
        if not isinstance(spec, dict):
            raise NetdFault("EINVAL", "spec must be an object")
        return net.tc_set(_require_ifname(params.get("name")), spec)
    if verb == "tc.clear":
        return net.tc_clear(_require_ifname(params.get("name")))
    if verb == "capture.start":
        bpf = params.get("bpf") or ""
        if not isinstance(bpf, str) or any(c in bpf for c in ";|&`$"):
            raise NetdFault("EINVAL", "bpf malformed")
        return net.capture_start(_require_ifname(params.get("name")), bpf)
    if verb == "capture.stop":
        return net.capture_stop(_require_ifname(params.get("name")))
    if verb == "capture.read":
        return net.capture_read(_require_ifname(params.get("name")))
    if verb == "host.tune":
        sysctls = params.get("sysctls")
        if not isinstance(sysctls, dict):
            raise NetdFault("EINVAL", "sysctls must be an object")
        return net.host_tune({str(k): str(v) for k, v in sysctls.items()})
    if verb == "host.tuning_read":
        keys = params.get("keys") or []
        if not isinstance(keys, list):
            raise NetdFault("EINVAL", "keys must be a list")
        return net.host_tuning_read([str(k) for k in keys])
    if verb == "host.capabilities":
        return net.host_capabilities()
    if verb == "host.interfaces":
        return net.host_interfaces()
    if verb == "host.inspect_bridge":
        return net.inspect_bridge(_require_host_ifname(params.get("name")))
    if verb == "host.management_probe":
        return net.management_probe()
    if verb == "host.configure_management":
        mode = params.get("mode")
        if mode not in ("dhcp", "manual"):
            raise NetdFault("EINVAL", "mode must be 'dhcp' or 'manual'")
        interface = _require_host_ifname(params.get("interface"))
        address = params.get("address")
        gateway = params.get("gateway")
        dns_raw = params.get("dns") or []
        if not isinstance(dns_raw, list):
            raise NetdFault("EINVAL", "dns must be a list of IPv4 addresses")
        if mode == "manual":
            if not isinstance(address, str):
                raise NetdFault("EINVAL", "manual mode requires 'address'")
            _require_cidr(address)
            if not isinstance(gateway, str):
                raise NetdFault("EINVAL", "manual mode requires 'gateway'")
            _require_ipv4(gateway, "gateway")
            # Refuse a gateway that isn't inside the address's subnet — the
            # kernel would accept it, but the resulting route is unreachable
            # and takes the box off the network with no useful error.
            import ipaddress

            try:
                net_obj = ipaddress.ip_network(address, strict=False)
                if ipaddress.ip_address(gateway) not in net_obj:
                    raise NetdFault(
                        "EINVAL",
                        f"gateway {gateway} is not inside {address}'s subnet",
                    )
            except ValueError as exc:
                raise NetdFault("EINVAL", f"malformed address/gateway: {exc}") from exc
        dns: list[str] = []
        for entry in dns_raw:
            dns.append(_require_ipv4(entry, "dns"))
        kind = params.get("kind", "nic")
        if kind not in ("nic", "bridge"):
            raise NetdFault("EINVAL", "kind must be 'nic' or 'bridge'")
        return net.configure_management(mode, interface, address, gateway, dns, kind=kind)
    if verb == "host.uplink_probe":
        return net.uplink_probe()
    if verb == "host.configure_uplinks":
        pairs = params.get("pairs") or []
        if not isinstance(pairs, list):
            raise NetdFault("EINVAL", "pairs must be a list")
        clean: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in pairs:
            if not isinstance(entry, dict):
                raise NetdFault("EINVAL", "each pair must be an object")
            nic = _require_host_ifname(entry.get("nic"))
            if nic in seen:
                raise NetdFault("EINVAL", f"nic {nic!r} appears more than once")
            seen.add(nic)
            clean.append({"nic": nic})
        return net.configure_uplinks(clean)
    if verb == "cloud.attach":
        force = params.get("force", False)
        if not isinstance(force, bool):
            raise NetdFault("EINVAL", "force must be bool")
        return net.cloud_attach(
            _require_host_ifname(params.get("name")),
            _require_ifname(params.get("bridge"), "bridge"),
            force,
        )
    if verb == "cloud.detach":
        return net.cloud_detach(_require_host_ifname(params.get("name")))
    if verb == "vxlan.create":
        vni = params.get("vni")
        if not isinstance(vni, int) or not (0 < vni < 2**24):
            raise NetdFault("EINVAL", "vni must be an int in (0, 2**24)")
        remote = params.get("remote")
        if not isinstance(remote, str) or not IPV4_RE.match(remote):
            raise NetdFault("EINVAL", "remote must be an IPv4 address")
        local = params.get("local")
        if local is not None and (not isinstance(local, str) or not IPV4_RE.match(local)):
            raise NetdFault("EINVAL", "local must be an IPv4 address")
        dstport = params.get("dstport", 4789)
        if not isinstance(dstport, int) or not (0 < dstport < 65536):
            raise NetdFault("EINVAL", "dstport must be a valid port")
        return net.vxlan_create(_require_ifname(params.get("name")), vni, remote, local, dstport)
    if verb == "vxlan.delete":
        return net.vxlan_delete(_require_ifname(params.get("name")))
    if verb == "hook.ping":
        pid = params.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise NetdFault("EINVAL", "pid must be a positive int")
        target = params.get("target")
        # No shell metacharacters — target flows into an argv, but a `--`
        # in the wrong place could still surprise ping's own arg parser,
        # so keep it to hostname or IPv4 shape.
        if not isinstance(target, str) or not _TARGET_RE.match(target):
            raise NetdFault("EINVAL", "target must be a hostname or IPv4 address")
        count = params.get("count", 3)
        if not isinstance(count, int) or not (1 <= count <= 20):
            raise NetdFault("EINVAL", "count must be an int in [1,20]")
        timeout = params.get("timeout_s", 5)
        if not isinstance(timeout, int) or not (1 <= timeout <= 60):
            raise NetdFault("EINVAL", "timeout_s must be an int in [1,60]")
        return net.hook_ping(pid, target, count, timeout)
    if verb == "hook.http":
        pid = params.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise NetdFault("EINVAL", "pid must be a positive int")
        url = params.get("url")
        # Only http/https. curl accepts file://, ftp://, gopher:// and
        # others; none of those are what a "hook probes an endpoint"
        # means, and file:// pointed at /etc/shadow inside a netns'd
        # process (which still shares the host fs) is exactly the shape
        # of a bug worth heading off.
        if not isinstance(url, str) or not (
            url.startswith("http://") or url.startswith("https://")
        ):
            raise NetdFault("EINVAL", "url must start with http:// or https://")
        timeout = params.get("timeout_s", 10)
        if not isinstance(timeout, int) or not (1 <= timeout <= 60):
            raise NetdFault("EINVAL", "timeout_s must be an int in [1,60]")
        return net.hook_http(pid, url, timeout)
    raise NetdFault("EINVAL", f"unknown verb {verb!r}")


# Hostname or IPv4. Loose on hostnames intentionally — the ip resolver in
# ping/curl will reject anything real DNS refuses. This regex is only here to
# block shell metacharacters that could survive being handed to subprocess
# with shell=False (which we do), because a malicious `target` should not
# even reach the exec syscall.
_TARGET_RE = __import__("re").compile(r"^[A-Za-z0-9._:\-]{1,253}$")
