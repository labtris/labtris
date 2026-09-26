from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import yaml

from labtris_api.runtime.qemu import QEMU_CATALOG


@dataclass
class PlannedNode:
    name: str
    runtime: str
    image: str
    cmd: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    ifaces: list[str] = field(default_factory=list)
    position: tuple[int, int] | None = None


@dataclass
class PlannedLink:
    a_node: str
    a_iface: str | None
    b_node: str
    b_iface: str | None


@dataclass
class Plan:
    """What we intend to build, before touching the database.

    Parsing and creating are split so a malformed file cannot leave a half-made
    lab behind, and so both importers hand the same shape to one realizer."""

    name: str
    nodes: list[PlannedNode] = field(default_factory=list)
    links: list[PlannedLink] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    #: name -> network kind, for the few segments that are not a plain
    #: Linux bridge. A side-table rather than a richer `networks` element
    #: so that every existing producer and consumer of `networks` is
    #: unaffected; anything absent here is a bridge.
    network_kinds: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(name))[:60] or "node"


# --- containerlab -----------------------------------------------------------

#: containerlab kinds we can run as-is. Everything else is a vendor image that
#: is licensed, registry-gated, or has to be imported by hand — those become a
#: labelled placeholder rather than failing the whole file, because a topology
#: that arrives with 14 of its 17 nodes is worth more than a rejection.
CLAB_KINDS: dict[str, str] = {
    "linux": "docker",
    "docker": "docker",
    "host": "docker",
}

CLAB_VENDOR_NOTE = {
    "nokia_srlinux": "SR Linux needs ghcr.io/nokia/srlinux, which is registry-gated",
    "srl": "SR Linux needs ghcr.io/nokia/srlinux, which is registry-gated",
    "arista_ceos": "cEOS has to be downloaded from Arista and imported by hand",
    "ceos": "cEOS has to be downloaded from Arista and imported by hand",
    "juniper_crpd": "cRPD needs a Juniper licence and a manual docker load",
    "crpd": "cRPD needs a Juniper licence and a manual docker load",
    "vr-sros": "vrnetlab images are built locally from a vendor qcow2",
    "cisco_iol": "IOL images are licensed and not redistributable",
}

#: Kinds that are a shared segment rather than a node.
CLAB_SEGMENTS = {"bridge", "ovs-bridge"}


def parse_clab(text: str) -> Plan:
    """A containerlab `.clab.yml` topology.

    Handles the inheritance that makes real files terse: a node takes its kind
    from `topology.defaults`, and its image and command from `topology.kinds`
    unless it overrides them itself."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed containerlab yaml: {exc}") from None
    if not isinstance(doc, dict):
        raise ValueError("containerlab file must be a mapping at the top level")

    topo = doc.get("topology") or {}
    if not isinstance(topo, dict) or "nodes" not in topo:
        raise ValueError("no topology.nodes in this file — is it a containerlab topology?")

    defaults = topo.get("defaults") or {}
    kinds = topo.get("kinds") or {}
    plan = Plan(name=_safe(doc.get("name") or "clab-import"))

    segments: dict[str, str] = {}
    for raw_name, raw in (topo.get("nodes") or {}).items():
        spec = raw if isinstance(raw, dict) else {}
        kind = str(spec.get("kind") or defaults.get("kind") or "linux")
        inherited = kinds.get(kind) or {}
        image = spec.get("image") or inherited.get("image") or defaults.get("image")
        name = _safe(raw_name)

        if kind in CLAB_SEGMENTS:
            # A clab bridge is a segment, not a node — it becomes one here too.
            # `ovs-bridge` used to be flattened to a plain Linux bridge, which
            # quietly dropped the reason someone chose it. It maps to an OVS
            # segment now; if the host has no working Open vSwitch the create
            # fails with a reason, which beats a lab that is subtly not what
            # the file asked for.
            segments[str(raw_name)] = name
            plan.networks.append(name)
            if kind == "ovs-bridge":
                plan.network_kinds[name] = "ovs"
            continue

        runtime = CLAB_KINDS.get(kind)
        if runtime is None or not image:
            note = CLAB_VENDOR_NOTE.get(kind, f"kind {kind!r} has no equivalent here")
            plan.warnings.append(f"{raw_name}: {note} — imported as a placeholder")
            plan.nodes.append(
                PlannedNode(
                    name=name,
                    runtime="docker",
                    image="alpine:3.20",
                    cmd=["sleep", "infinity"],
                    env={"LABTRIS_ORIGINAL_KIND": kind, "LABTRIS_ORIGINAL_IMAGE": str(image or "")},
                )
            )
            continue

        cmd = spec.get("cmd") or inherited.get("cmd")
        plan.nodes.append(
            PlannedNode(
                name=name,
                runtime="docker",
                image=str(image),
                cmd=cmd.split() if isinstance(cmd, str) else cmd,
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            )
        )

    known = {n.name for n in plan.nodes}
    for raw_link in topo.get("links") or []:
        endpoints = (raw_link or {}).get("endpoints") or []
        if len(endpoints) != 2:
            plan.warnings.append(f"skipped a link with {len(endpoints)} endpoints")
            continue
        parsed: list[tuple[str, str | None]] = []
        for ep in endpoints:
            ep_node, _, ep_iface = str(ep).partition(":")
            parsed.append((ep_node, ep_iface or None))
        (a_node, a_if), (b_node, b_if) = parsed

        # An endpoint naming a bridge, the host, or a management network is a
        # segment membership rather than a point-to-point link.
        for end_node, end_iface in ((a_node, a_if), (b_node, b_if)):
            if end_node in segments or end_node in ("host", "mgmt-net", "macvlan"):
                other = b_node if end_node == a_node else a_node
                if end_node in segments:
                    plan.warnings.append(
                        f"{other} joins segment {end_node!r} — attach it on the canvas"
                    )
                else:
                    plan.warnings.append(
                        f"{other}:{end_iface} connected to {end_node!r}, which has no equivalent "
                        "here — use a cloud network bound to a host NIC"
                    )
                break
        else:
            if _safe(a_node) in known and _safe(b_node) in known:
                plan.links.append(PlannedLink(_safe(a_node), a_if, _safe(b_node), b_if))
            else:
                missing = [n for n in (a_node, b_node) if _safe(n) not in known]
                plan.warnings.append(f"link skipped, unknown node(s): {', '.join(missing)}")

    for link in plan.links:
        for end_name, end_iface in ((link.a_node, link.a_iface), (link.b_node, link.b_iface)):
            owner = next(n for n in plan.nodes if n.name == end_name)
            if end_iface and end_iface not in owner.ifaces:
                owner.ifaces.append(end_iface)
    for planned in plan.nodes:
        if not planned.ifaces:
            planned.ifaces.append("eth1")
    return plan


# --- EVE-NG -----------------------------------------------------------------

#: EVE-NG template -> what to run it as here. The QEMU entries are why this is
#: worth revisiting: the importer predates the QEMU backend and mapped every
#: one of these to an Alpine placeholder.
UNL_TEMPLATES: dict[str, tuple[str, str]] = {
    "docker": ("docker", "alpine:3.20"),
    "linux": ("qemu", "ubuntu-24.04"),
    "alpine": ("docker", "alpine:3.20"),
    "ubuntu": ("qemu", "ubuntu-24.04"),
    "debian": ("qemu", "debian-12"),
    "fedora": ("qemu", "fedora-42"),
    "centos": ("qemu", "centos-9-server"),
    "opensuse": ("qemu", "opensuse-leap-15.6"),
    "kali": ("qemu", "kali-2025.2"),
    "mint": ("qemu", "linuxmint-22.1"),
    "arch": ("qemu", "arch-cli"),
    "cirros": ("qemu", "cirros"),
    "frr": ("docker", "frrouting/frr:v8.4.0"),
    "nginx": ("docker", "nginx:alpine"),
}

#: Templates we know are network appliances we cannot run, so the warning can
#: say what it is rather than "unknown".
UNL_APPLIANCES = {
    "iol", "iou", "csr1000v", "csr1000vng", "nxosv9k", "vios", "viosl2", "asav",
    "veos", "vmx", "vsrx", "vqfx", "vyos", "mikrotik", "fortinet", "paloalto",
}


def parse_unl(xml: str, fallback_name: str | None = None) -> Plan:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"malformed .unl xml: {exc}") from None
    topology = root.find("topology")
    topology = topology if topology is not None else root
    plan = Plan(name=_safe(fallback_name or root.get("name") or "unl-import"))

    by_id: dict[str, str] = {}
    for el in topology.findall(".//node"):
        template = str(el.get("template", "")).lower()
        node_id = str(el.get("id", ""))
        name = _safe(el.get("name") or f"n{node_id}")
        by_id[node_id] = name

        runtime, image = UNL_TEMPLATES.get(template, ("", ""))
        env: dict[str, str] = {}
        if not runtime:
            env["LABTRIS_ORIGINAL_TEMPLATE"] = template or "unknown"
            runtime, image = "docker", "alpine:3.20"
            if template in UNL_APPLIANCES:
                plan.warnings.append(
                    f"{name}: {template} is a vendor appliance image — imported as a placeholder"
                )
            else:
                plan.warnings.append(
                    f"{name}: template {template or 'unknown'!r} has no equivalent here "
                    "— imported as a placeholder"
                )
        elif runtime == "qemu" and image not in QEMU_CATALOG:
            plan.warnings.append(f"{name}: {image} is not in the QEMU catalog — used Alpine")
            runtime, image = "docker", "alpine:3.20"

        try:
            position = (int(float(el.get("left", 0))), int(float(el.get("top", 0))))
        except (TypeError, ValueError):
            position = None

        ifaces = [str(i.get("name") or f"eth{n}") for n, i in enumerate(el.findall(".//interface"))]
        plan.nodes.append(
            PlannedNode(
                name=name,
                runtime=runtime,
                image=image,
                cmd=["sleep", "3600"] if runtime == "docker" else None,
                env=env,
                ifaces=ifaces or ["eth1"],
                position=position,
            )
        )

    # EVE-NG expresses a link as two interfaces naming the same network id.
    on_net: dict[str, list[tuple[str, str]]] = {}
    for el in topology.findall(".//node"):
        node_name = by_id.get(str(el.get("id", "")), "")
        for n, iface in enumerate(el.findall(".//interface")):
            net_id = iface.get("network_id")
            if net_id:
                on_net.setdefault(net_id, []).append(
                    (node_name, str(iface.get("name") or f"eth{n}"))
                )
    for net_id, members in on_net.items():
        if len(members) == 2:
            (a_name, a_if), (b_name, b_if) = members
            plan.links.append(PlannedLink(a_name, a_if, b_name, b_if))
        elif len(members) > 2:
            name = _safe(f"net{net_id}")
            plan.networks.append(name)
            plan.warnings.append(
                f"network {net_id} has {len(members)} members — created as a segment named "
                f"{name}; attach its nodes on the canvas"
            )
    return plan


def detect(text: str, filename: str = "") -> str:
    """Which importer a file wants, from its shape rather than its name — a
    topology mailed around loses its extension long before it loses its
    contents."""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return "unl"
    if stripped.startswith("{"):
        return "native"
    if filename.endswith((".yml", ".yaml")) or "topology:" in text:
        return "clab"
    raise ValueError("cannot tell whether this is a .unl, a containerlab topology, or an export")
