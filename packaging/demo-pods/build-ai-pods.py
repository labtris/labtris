#!/usr/bin/env python3
"""Build the AI-fabric demo pods.

Every pod here arrives **wired**. That matters: `_clone_topology` in
routers/labs.py builds networks from the payload's `links` and ignores
both `interfaces[].network_id` and the `networks` array, so a pod with
nodes and no links lands on the canvas as loose boxes. The older
`rdma-uet-demo` pod has exactly that shape — 4 nodes, 0 links, half its
interfaces unattached — which is why it is superseded here.

Pods are cold: they carry no image. Every node image is now
public: alpine, frr and bmv2 from Docker Hub, uet-ref and rdma-host from
ghcr.io/labtris where CI publishes them. A *hot* pod can carry
`nodes/<id>/image.tar` and the loader will `docker load` it, which is how
you move a local build onto a machine with no registry — see
`--with-image`.

    ./build-ai-pods.py            # write every pod into this directory
    ./build-ai-pods.py --list     # just show what would be written
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The FRR installer is imported rather than copied. These pods are the only
# ones that arrive *configured* — the older ones land wired but with an empty
# frr.conf and a description telling you to set up the underlay yourself —
# and a second copy of that script would drift from the generator's within a
# release.
_spec = importlib.util.spec_from_file_location(
    "_frr_templates", HERE.parent.parent / "labtris_api" / "frr_templates.py"
)
_frr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_frr)
_FRR_DAEMONS_EVPN = _frr._FRR_DAEMONS_EVPN
_frr_installer = _frr._frr_installer
_seed_files_from_installer = _frr.seed_files_from_installer

# Deterministic ids. They are remapped on load, so they only have to be
# internally consistent and 26 chars of Crockford-ish base32.
def _id(tag: str, n: int) -> str:
    body = f"{tag}{n:04d}".upper().ljust(21, "0")
    return ("01" + body)[:26]


def node(
    nid: str,
    name: str,
    image: str,
    ifaces: int = 1,
    env: dict[str, str] | None = None,
    opts: dict[str, object] | None = None,
    iface_scheme: str = "eth",
    startup_config: str | None = None,
) -> dict:
    return {
        "cmd": None,
        "console": {},
        "cpu_limit": None,
        "env": env or {},
        "id": nid,
        "iface_scheme": iface_scheme,
        "image": image,
        "interfaces": [
            {
                "host_ifname": None,
                "id": f"{nid[:-2]}I{i}",
                "idx": i,
                "mac": None,
                "name": f"{'eth' if iface_scheme == 'eth' else 's'}{i}",
                "network_id": None,
                "node_id": nid,
                "reserved_ip": None,
                "trunk_vids": None,
                "vlan_id": None,
                "vlan_mode": None,
            }
            for i in range(ifaces)
        ],
        "lab_id": "",
        "last_error": None,
        "name": name,
        "nic_model": None,
        # An FRR node carries its config twice: as the installer script, for
        # `lab configure` and for runtimes with no seeding, and as the files
        # themselves so they can be written in before the container starts.
        # Seeded, `frrinit.sh start` reads a daemons file that already says
        # bgpd=yes — the pod converges on boot instead of after a push, an
        # exec and a restart.
        "opts": {
            **(opts or {}),
            **(
                {"seed_files": _seed_files_from_installer(startup_config)}
                if startup_config and _seed_files_from_installer(startup_config)
                else {}
            ),
        },
        "paused": False,
        "qemu_opts": {},
        "ram_mb": None,
        "runtime": "docker",
        "runtime_ref": None,
        "startup_config": startup_config,
        "state": "defined",
        "style": {},
    }


def link(
    lid: str,
    a: str,
    b: str,
    impair: dict | None = None,
    impair_ba: dict | None = None,
) -> dict:
    """`impair` applies both ways unless `impair_ba` overrides the return.

    Separate directions because a real inter-site hop is not symmetric —
    different paths, different queues — and a demo that pretends otherwise
    teaches the wrong thing about the one tier where it matters most.
    """
    return {
        "id": lid,
        "lab_id": "",
        "a_iface_id": a,
        "b_iface_id": b,
        "network_id": None,
        "admin_up": True,
        "impair_ab": impair,
        "impair_ba": impair_ba if impair_ba is not None else impair,
    }


def iface(nid: str, i: int = 0) -> str:
    return f"{nid[:-2]}I{i}"


def _project_version() -> str:
    """The version in pyproject.toml, or "unknown".

    Informational metadata only, so a missing or unreadable pyproject is
    not worth raising over — a pod that builds is more useful than one that
    fails over a string nothing gates on.
    """
    import re
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    try:
        text = (root / "pyproject.toml").read_text()
    except OSError:
        return "unknown"
    m = re.search(r'^version *= *"([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def pod(name: str, description: str, nodes: list[dict], links: list[dict],
        geometry: dict[str, dict], hooks: str | None = None) -> dict:
    lab_id = _id("LAB", abs(hash(name)) % 9999)
    for n in nodes:
        n["lab_id"] = lab_id
    for l in links:
        l["lab_id"] = lab_id
    out = {
        "format": "labtris-lab-v1",
        "geometry": {"nodes": geometry, "links": {},
                     "view": {"x": 80, "y": 40, "k": 1}},
        "lab": {
            "active_configset": None,
            "description": description,
            "folder": "",
            "id": lab_id,
            "links": links,
            "locked": False,
            "name": name,
            "networks": [],
            "nodes": nodes,
        },
    }
    if hooks:
        out["_hooks"] = hooks
    return out


def manifest(p: dict, mode: str = "cold") -> dict:
    nodes = p["lab"]["nodes"]
    return {
        "format": "labtris-pod-v1",
        # Informational only — the loader does not gate on it. Read from
        # pyproject rather than written here, which is what the comment
        # claimed before the string was left at 0.12.0 through a version
        # bump: a pod built today should not claim a release it predates.
        "labtris_version": _project_version(),
        "pod_id": _id("POD", abs(hash(p["lab"]["name"])) % 9999),
        "lab_id": p["lab"]["id"],
        "lab_name": p["lab"]["name"],
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "node_count": len(nodes),
        "nodes": [
            {"id": n["id"], "name": n["name"], "runtime": "docker",
             "image": n["image"], "hot_image_tag": None,
             "disk_status": "never_started"}
            for n in nodes
        ],
        "has_hooks": False,
        "hot_snapshot_tag": None,
    }


def write(p: dict, out: Path) -> int:
    man = manifest(p)
    man["has_hooks"] = bool(p.get("_hooks"))
    with tarfile.open(out, "w:gz") as tar:
        if p.get("_hooks"):
            raw = p["_hooks"].encode()
            ti = tarfile.TarInfo("hooks.yml")
            ti.size = len(raw)
            ti.mtime = 0
            tar.addfile(ti, BytesIO(raw))
        for fname, obj in (("lab.json", p), ("snapshot.json", man),
                           ("templates.json", [])):
            raw = json.dumps(obj, indent=2).encode()
            ti = tarfile.TarInfo(fname)
            ti.size = len(raw)
            ti.mtime = 0
            tar.addfile(ti, BytesIO(raw))
    return out.stat().st_size


# --------------------------------------------------------------------
# The labs
# --------------------------------------------------------------------

def uet_pair() -> dict:
    a, b = _id("UETA", 1), _id("UETB", 2)
    return pod(
        "uet-pair",
        "Two UEC reference-provider endpoints on one link. Real Ultra "
        "Ethernet on IP protocol 253 over kernel veths. "
        "`UET_IFNAME=eth0 UET_PDS=pds uet server rma <peer>` on one, "
        "`uet client rma <peer>` on the other. 13 tests x 10 PDS configs "
        "x 2 NIC shims via scripts/uet_test.sh.",
        [node(a, "uet-a", "ghcr.io/labtris/uet-ref:latest",
              env={"UET_IFNAME": "eth0", "UET_PDS": "pds"}),
         node(b, "uet-b", "ghcr.io/labtris/uet-ref:latest",
              env={"UET_IFNAME": "eth0", "UET_PDS": "pds"})],
        [link(_id("UETL", 1), iface(a), iface(b))],
        {a: {"x": 120, "y": 180}, b: {"x": 520, "y": 180}},
    )


def rdma_pair() -> dict:
    a, b = _id("RDMA", 1), _id("RDMB", 2)
    return pod(
        "rdma-pair",
        "Soft-RoCE between two hosts. `rdma link add rxe0 type rxe "
        "netdev eth0`, then `ib_send_bw -d rxe0 -x 1 <peer>`. GID index 1 "
        "is RoCEv2; index 0 is IPv6 link-local RoCEv1 and will not route. "
        "Needs Linux 7.1+ on the HOST — before that rdma_rxe bound UDP "
        "4791 in the init netns only and a lab node moves no data.",
        [node(a, "rdma-a", "ghcr.io/labtris/rdma-host:latest"),
         node(b, "rdma-b", "ghcr.io/labtris/rdma-host:latest")],
        [link(_id("RDML", 1), iface(a), iface(b))],
        {a: {"x": 120, "y": 180}, b: {"x": 520, "y": 180}},
        # The one lab here that can start cleanly and then do nothing.
        # soft-RoCE needs a 7.1+ host kernel and no distribution ships one,
        # so the likely first experience is a transfer that connects and
        # moves zero bytes with nothing to read. A container shares the
        # host kernel, so `uname -r` inside a node reports the host's — the
        # check costs nothing and turns a silent dead end into a sentence.
        hooks="""ready_when: all_nodes_running
hooks:
  - name: "host kernel is 7.1+ (below this, soft-RoCE moves no data)"
    kind: command
    node: rdma-a
    command: "sh -c 'k=$(uname -r); maj=${k%%.*}; r=${k#*.}; min=${r%%.*};
              if [ \\"$maj\\" -gt 7 ] || { [ \\"$maj\\" -eq 7 ] && [ \\"$min\\" -ge 1 ]; };
              then echo \\"kernel $k supports soft-RoCE\\";
              else echo \\"kernel $k is too old — rdma_rxe gained its
              per-namespace UDP 4791 socket in 7.1. ib_send_bw will connect
              and transfer nothing. Install a mainline 7.x kernel.\\"; exit 1; fi'"
    expect_rc: 0
    expect_stdout_regex: "supports soft-RoCE"
    timeout_s: 15
""",
    )


def p4_trim() -> dict:
    sw = _id("P4SW", 1)
    h1, h2 = _id("P4H1", 2), _id("P4H2", 3)
    return pod(
        "p4-trim",
        "A bmv2 switch running the UEC packet-trimming primitive between "
        "two hosts. Over TRIM_THRESHOLD queue depth the switch keeps 64 "
        "bytes of headers, marks DSCP 63 and drops the payload, so the "
        "receiver learns about congestion immediately instead of after a "
        "timeout. Swap the program with PUT /nodes/{id}/p4 and restart "
        "that node — p4c-bm2-ss compiles at start, so the running switch "
        "keeps its old program until then.",
        [node(sw, "p4-switch", "p4lang/p4c:latest", ifaces=2,
              opts={"p4_program": "trim"}, iface_scheme="s"),
         node(h1, "host-1", "alpine:3.20"),
         node(h2, "host-2", "alpine:3.20")],
        [link(_id("P4L1", 1), iface(h1), iface(sw, 0)),
         link(_id("P4L2", 2), iface(h2), iface(sw, 1))],
        {h1: {"x": 80, "y": 90}, sw: {"x": 360, "y": 180},
         h2: {"x": 80, "y": 280}},
    )


def pfc_classes() -> dict:
    a, b = _id("PFCA", 1), _id("PFCB", 2)
    impair = {
        "delay_ms": 0, "jitter_ms": 0, "loss_pct": 0, "rate_kbit": 100_000,
        "reorder_pct": 0, "duplicate_pct": 0, "corrupt_pct": 0,
        "ecn": True, "ecn_min_bytes": 50_000, "ecn_max_bytes": 150_000,
        "pfc": True, "pfc_priorities": [0, 1, 2, 3, 4, 5, 6, 7],
    }
    return pod(
        "pfc-classes",
        "One shaped link with eight priority bands and per-band RED/ECN. "
        "Flood one class and watch the others keep forwarding: that is "
        "the isolation the queueing buys. 802.1Qbb PAUSE emission is NOT "
        "implemented, so this is priority queueing with ECN marking, not "
        "a lossless fabric. tc qdisc show on the host side of the link "
        "shows `prio bands 8` with a `red ... ecn` under each band.",
        [node(a, "class-a", "alpine:3.20"),
         node(b, "class-b", "alpine:3.20")],
        [link(_id("PFCL", 1), iface(a), iface(b), impair=impair)],
        {a: {"x": 120, "y": 180}, b: {"x": 520, "y": 180}},
    )


def ai_fabric() -> dict:
    """2 spines, 2 leaves, 4 UET hosts — the smallest shape with more than
    one equal-cost path between any two hosts, which is the precondition
    for packet spraying to mean anything."""
    s1, s2 = _id("FBS1", 1), _id("FBS2", 2)
    l1, l2 = _id("FBL1", 3), _id("FBL2", 4)
    hosts = [_id("FBH", 10 + i) for i in range(4)]
    nodes = [
        node(s1, "spine-1", "frrouting/frr:v8.4.0", ifaces=2),
        node(s2, "spine-2", "frrouting/frr:v8.4.0", ifaces=2),
        node(l1, "leaf-1", "frrouting/frr:v8.4.0", ifaces=4),
        node(l2, "leaf-2", "frrouting/frr:v8.4.0", ifaces=4),
    ] + [
        node(h, f"uet-{i+1}", "ghcr.io/labtris/uet-ref:latest",
             env={"UET_IFNAME": "eth0", "UET_PDS": "pds"})
        for i, h in enumerate(hosts)
    ]
    links = [
        link(_id("FBX", 1), iface(l1, 0), iface(s1, 0)),
        link(_id("FBX", 2), iface(l1, 1), iface(s2, 0)),
        link(_id("FBX", 3), iface(l2, 0), iface(s1, 1)),
        link(_id("FBX", 4), iface(l2, 1), iface(s2, 1)),
        link(_id("FBX", 5), iface(hosts[0]), iface(l1, 2)),
        link(_id("FBX", 6), iface(hosts[1]), iface(l1, 3)),
        link(_id("FBX", 7), iface(hosts[2]), iface(l2, 2)),
        link(_id("FBX", 8), iface(hosts[3]), iface(l2, 3)),
    ]
    geo = {
        s1: {"x": 220, "y": 60}, s2: {"x": 520, "y": 60},
        l1: {"x": 180, "y": 220}, l2: {"x": 560, "y": 220},
        hosts[0]: {"x": 60, "y": 380}, hosts[1]: {"x": 260, "y": 380},
        hosts[2]: {"x": 460, "y": 380}, hosts[3]: {"x": 660, "y": 380},
    }
    return pod(
        "ai-fabric-uet",
        "Two spines, two leaves, four Ultra Ethernet endpoints. Every "
        "leaf reaches every spine, so there are two equal-cost paths "
        "between any pair of hosts — the precondition for packet "
        "spraying to be observable. Watch uet.entropy.entropy across a "
        "capture: one value for every packet means no spraying, which is "
        "upstream's stated gap and the most useful thing this shape "
        "currently shows. Leaves and spines are FRR and need an underlay "
        "configured; the hosts are ready as they are.",
        nodes, links, geo,
    )


def fw_vwire() -> dict:
    """A container firewall inline on a virtual wire — the DPU/IoT shape.

    Not in SEED: this is a development lab, not something every install
    should find on its canvas.

    Both hosts sit in one subnet and do not know the firewall is there.
    It has two interfaces, no IP on the data path, and bridges between
    them — which is how a DPU or an inline gateway actually inserts, and
    means a function can be added or removed without re-addressing
    anything.

    Filter with nftables' `bridge` family rather than br_netfilter. The
    latter needs net.bridge.bridge-nf-call-iptables on the *host*, so a
    lab depending on it filters on one machine and silently forwards
    everything on another.

        fw:   ip link add br0 type bridge
              ip link set eth0 master br0; ip link set eth1 master br0
              ip link set br0 up
              nft add table bridge filter
              nft add chain bridge filter forward '{ type filter hook forward priority 0; }'
              nft add rule bridge filter forward tcp dport 23 drop
    """
    a, b = _id("FWHA", 1), _id("FWHB", 2)
    fw = _id("FWFW", 3)
    return pod(
        "fw-vwire",
        "A firewall container inline on a virtual wire. Two hosts in one "
        "subnet, a two-interface bridging node between them with no IP on "
        "the data path — the shape a BlueField DPU or an inline IoT "
        "gateway takes. Filter with nftables' bridge family; br_netfilter "
        "depends on a host sysctl and fails silently where it is not set.",
        # All three on the same image: the hosts want socat and tcpdump to
        # be a usable test rig, and a listener backgrounded from busybox sh
        # does not survive a console exec, so alpine hosts could not hold a
        # port open long enough to prove a rule.
        [node(a, "host-a", "ghcr.io/labtris/fw:latest"),
         node(fw, "fw", "ghcr.io/labtris/fw:latest", ifaces=2),
         node(b, "host-b", "ghcr.io/labtris/fw:latest")],
        [link(_id("FWL", 1), iface(a), iface(fw, 0)),
         link(_id("FWL", 2), iface(fw, 1), iface(b))],
        {a: {"x": 80, "y": 180}, fw: {"x": 360, "y": 180},
         b: {"x": 640, "y": 180}},
    )


def _bgp(hostname: str, asn: int, router_id: str, peers: list[str],
         extra: str = "") -> str:
    """Unnumbered eBGP on the named interfaces, redistributing connected.

    Unnumbered because it is what makes these labs start working the moment
    they boot: no addressing plan to apply by hand before anything routes.
    """
    nbrs = "\n".join(
        f" neighbor {p} interface remote-as external" for p in peers
    )
    acts = "\n".join(f"  neighbor {p} activate" for p in peers)
    return f"""frr defaults datacenter
hostname {hostname}
log stdout
!
router bgp {asn}
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
 no bgp ebgp-requires-policy
{nbrs}
 !
 address-family ipv4 unicast
  redistribute connected
{acts}
 exit-address-family
{extra}!
"""


def front_end() -> dict:
    """The north-south tier: a border pair, an access switch, three services.

    Small on purpose. The point of this tier is not scale — it is that the
    cluster has a front door, that it is redundant, and that the services
    behind it are named for what they do rather than host-1..3.
    """
    b1, b2 = _id("FEB1", 1), _id("FEB2", 2)
    acc = _id("FEA1", 3)
    svcs = [_id("FES", 10 + i) for i in range(3)]
    names = ["storage", "inference-api", "registry"]

    nodes = [
        node(b1, "border-1", "frrouting/frr:v8.4.0", ifaces=2,
             startup_config=_frr_installer(
                 _FRR_DAEMONS_EVPN,
                 _bgp("border-1", 65001, "10.255.0.1", ["eth0", "eth1"]))),
        node(b2, "border-2", "frrouting/frr:v8.4.0", ifaces=2,
             startup_config=_frr_installer(
                 _FRR_DAEMONS_EVPN,
                 _bgp("border-2", 65002, "10.255.0.2", ["eth0", "eth1"]))),
        node(acc, "access-1", "frrouting/frr:v8.4.0", ifaces=5,
             startup_config=_frr_installer(
                 _FRR_DAEMONS_EVPN,
                 _bgp("access-1", 65010, "10.255.0.10", ["eth0", "eth1"]))),
    ] + [
        node(h, names[i], "alpine:3.20") for i, h in enumerate(svcs)
    ]

    links = [
        link(_id("FEX", 1), iface(b1, 0), iface(b2, 0)),      # border pair
        link(_id("FEX", 2), iface(b1, 1), iface(acc, 0)),
        link(_id("FEX", 3), iface(b2, 1), iface(acc, 1)),
    ] + [
        link(_id("FEX", 10 + i), iface(h), iface(acc, 2 + i))
        for i, h in enumerate(svcs)
    ]

    geo = {
        b1: {"x": 220, "y": 60}, b2: {"x": 520, "y": 60},
        acc: {"x": 370, "y": 220},
        svcs[0]: {"x": 160, "y": 380},
        svcs[1]: {"x": 370, "y": 380},
        svcs[2]: {"x": 580, "y": 380},
    }
    return pod(
        "front-end",
        "The ordinary north-south network, which is the tier everyone "
        "forgets to model: users, storage and inference requests arriving. "
        "Two border routers so one can fail, an access switch, and three "
        "services named for what they do. eBGP is unnumbered and the config "
        "ships with the pod, so it converges on boot rather than after you "
        "write an addressing plan. Pull one border link and watch the "
        "services stay reachable.",
        nodes, links, geo,
    )


def scale_across() -> dict:
    """Two sites joined by a slow hop, slow by different amounts each way.

    The topology is deliberately boring — the inter-site link is the whole
    lesson. A collective that assumes the two directions match is exactly
    what this shape exists to let you break deliberately.
    """
    sites = []
    for sn in (1, 2):
        border = _id(f"SAB{sn}", sn)
        leaf = _id(f"SAL{sn}", 10 + sn)
        hosts = [_id(f"SAH{sn}", 20 + sn * 10 + i) for i in range(2)]
        sites.append((border, leaf, hosts))

    nodes = []
    for i, (border, leaf, hosts) in enumerate(sites, start=1):
        nodes += [
            node(border, f"site{i}-border", "frrouting/frr:v8.4.0", ifaces=2,
                 startup_config=_frr_installer(
                     _FRR_DAEMONS_EVPN,
                     _bgp(f"site{i}-border", 65100 + i, f"10.254.0.{i}",
                          ["eth0", "eth1"]))),
            node(leaf, f"site{i}-leaf", "frrouting/frr:v8.4.0", ifaces=3,
                 startup_config=_frr_installer(
                     _FRR_DAEMONS_EVPN,
                     _bgp(f"site{i}-leaf", 65110 + i, f"10.254.1.{i}",
                          ["eth0"]))),
        ] + [
            node(h, f"site{i}-h{j+1}", "alpine:3.20")
            for j, h in enumerate(hosts)
        ]

    links = []
    for i, (border, leaf, hosts) in enumerate(sites, start=1):
        links.append(link(_id(f"SAX{i}", i), iface(border, 1), iface(leaf, 0)))
        for j, h in enumerate(hosts):
            links.append(
                link(_id(f"SAY{i}", i * 10 + j), iface(h), iface(leaf, 1 + j))
            )

    #: The DCI. 12 ms out, 14 ms back — a metro pair on two different paths.
    #: Asymmetric because real ones are, and because a lab that ships
    #: symmetric impairment quietly teaches that symmetry is the default.
    links.append(
        link(
            _id("SADCI", 99),
            iface(sites[0][0], 0),
            iface(sites[1][0], 0),
            impair={"delay_ms": 12},
            impair_ba={"delay_ms": 14},
        )
    )

    geo = {}
    for i, (border, leaf, hosts) in enumerate(sites):
        x = 120 + i * 440
        geo[border] = {"x": x + 90, "y": 60}
        geo[leaf] = {"x": x + 90, "y": 220}
        geo[hosts[0]] = {"x": x, "y": 380}
        geo[hosts[1]] = {"x": x + 180, "y": 380}

    return pod(
        "scale-across",
        "Two clusters, one workload. Each site is a border and a leaf with "
        "two hosts; the interesting part is the hop between them, which "
        "carries 12 ms one way and 14 ms the other. That asymmetry is the "
        "point — real inter-site paths differ by direction, and anything "
        "that assumes they match will be wrong here in a way you can "
        "measure. Ping across the DCI and compare it with the one-way "
        "figures. Configs ship with the pod and converge on boot.",
        nodes, links, geo,
    )


PODS = {
    "uet-pair": uet_pair,
    "rdma-pair": rdma_pair,
    "p4-trim": p4_trim,
    "pfc-classes": pfc_classes,
    "ai-fabric-uet": ai_fabric,
    "fw-vwire": fw_vwire,
    "front-end": front_end,
    "scale-across": scale_across,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    for name, fn in PODS.items():
        p = fn()
        n, l = len(p["lab"]["nodes"]), len(p["lab"]["links"])
        if args.list:
            print(f"{name:<16} {n} nodes, {l} links")
            continue
        size = write(p, HERE / f"{name}.pod.tar.gz")
        print(f"{name:<16} {n} nodes, {l} links, {size:>6} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
