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
import json
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent

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
        "opts": opts or {},
        "paused": False,
        "qemu_opts": {},
        "ram_mb": None,
        "runtime": "docker",
        "runtime_ref": None,
        "startup_config": None,
        "state": "defined",
        "style": {},
    }


def link(lid: str, a: str, b: str, impair: dict | None = None) -> dict:
    return {
        "id": lid,
        "lab_id": "",
        "a_iface_id": a,
        "b_iface_id": b,
        "network_id": None,
        "admin_up": True,
        "impair_ab": impair,
        "impair_ba": impair,
    }


def iface(nid: str, i: int = 0) -> str:
    return f"{nid[:-2]}I{i}"


def pod(name: str, description: str, nodes: list[dict], links: list[dict],
        geometry: dict[str, dict]) -> dict:
    lab_id = _id("LAB", abs(hash(name)) % 9999)
    for n in nodes:
        n["lab_id"] = lab_id
    for l in links:
        l["lab_id"] = lab_id
    return {
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


def manifest(p: dict, mode: str = "cold") -> dict:
    nodes = p["lab"]["nodes"]
    return {
        "format": "labtris-pod-v1",
        "labtris_version": "0.11.0",
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
    with tarfile.open(out, "w:gz") as tar:
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


PODS = {
    "uet-pair": uet_pair,
    "rdma-pair": rdma_pair,
    "p4-trim": p4_trim,
    "pfc-classes": pfc_classes,
    "ai-fabric-uet": ai_fabric,
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
