# Demo pods

Three curated cold-snapshot pods you can load and inspect without
building a topology by hand. Each was `labtris lab generate`d on a
running instance, cold-snapshotted, and checked in here.

| Pod | Nodes | Shape |
|---|---|---|
| `spine-leaf-2x2-evpn.pod.tar.gz` | 6 | 2 spines × 2 leaves × 1 host, FRR everywhere, BGP EVPN configs pre-populated in `Node.startup_config` |
| `rail-optimised-2x2.pod.tar.gz` | 6 | 2 rails × 2 hosts, per-rail iBGP island (AS 64701 / 64702) |
| `fat-tree-k2.pod.tar.gz` | 7 | k=2 Al-Fares fat tree: 1 core + 2 pods × (1 agg + 1 edge) + 2 hosts, per-pod eBGP up to core |
| `rdma-uet-demo.pod.tar.gz` | 4 | 2 × `rdma-host` and 2 × `ue-stack` on one segment, for the RDMA and Ultra Ethernet rungs |

Regenerated on 0.11.0. The versions before that were built on a 0.5.0
instance, which predates `--with-bgp-evpn` entirely, so they carried no
`startup_config` at all despite this file promising populated configs.

Load one with:

```bash
labtris lab load /path/to/spine-leaf-2x2-evpn.pod.tar.gz \
  --name my-first-fabric
```

Every restored node gets its `startup_config` populated. Bring FRR up
per node with:

```bash
labtris node push <node-id>
labtris node exec <node-id> -- sh /config/startup-config
```

Or just run `packaging/smoke-test.sh`, which does the whole sequence and
then waits for BGP to converge.

All three converge: `packaging/smoke-test.sh` takes each pod from load
to Established on every session.

## What these pods are for

- **Read** — extract with `tar -xzOf <pod>.tar.gz snapshot.json | jq`
  to see the manifest, or `tar -xzOf <pod>.tar.gz lab.json` for the
  topology.
- **Load and start** — the fastest way to see a working spine-leaf on
  your host. Everything is docker + alpine, so RAM is negligible.
- **Adapt** — load, edit the canvas, snapshot again. These are a
  starting point, not immutable.

## Why they're small

Cold snapshots of all-Docker topologies carry no per-node bytes —
the container images (alpine, frrouting/frr) are named in the pod
manifest but their bytes stay in your local Docker cache. That's
why every pod here is a couple of KB even though the running lab
uses ~200 MB per node.

If you want a hot snapshot with running state included, generate
your own and snapshot with `--mode hot`. That produces a much
larger archive that carries the container filesystems + (for QEMU
nodes) live RAM.


## rdma-uet-demo

Four nodes on one bridge: `rdma-a`, `rdma-b` (soft-RoCE hosts) and
`uet-a`, `uet-b` (Ultra Ethernet endpoints).

```bash
labtris lab load packaging/demo-pods/rdma-uet-demo.pod.tar.gz --name rdma-uet
```

Both images are local builds. Build them first, from the repo root:

```bash
docker build -t labtris/rdma-host:latest packaging/dockerfiles/rdma-host/
docker build -f packaging/dockerfiles/ue-stack/Dockerfile -t labtris/ue-stack:latest .
```

### Ultra Ethernet — works today

Address the two UET nodes and send a frame:

```bash
# uet-a
ip addr add 10.99.0.10/24 dev eth1 && ip link set eth1 up
uestack listen

# uet-b
ip addr add 10.99.0.11/24 dev eth1 && ip link set eth1 up
uestack send 10.99.0.10:4791 UETDEMO
```

The listener prints the decoded packet:

```
[1] 10.99.0.11:44554  UePacket(op=SEND msn=1 txn=1 pdc=IPDC psn=1 payload=9B)
    payload: b'hello-uet'
```

And it is a real frame — `tcpdump -i eth1 -nn -U -X udp port 4791` on
`uet-a` shows the UET headers and the payload:

```
IP 10.99.0.11.60883 > 10.99.0.10.4791: UDP, length 39
  0x0030:  0001 0000 0001 0000 0003 0000 5545 5444  ............UETD
  0x0040:  454d 4f                                  EMO
```

### RDMA — needs host setup that is not solved yet

The `rdma-host` nodes start and carry `rdma-core`, `ibverbs-utils` and
`perftest`, but **soft-RoCE does not currently work end to end inside a
Labtris container**, for two reasons found while testing this pod:

1. **The host must be in RDMA netns-exclusive mode.** In the default
   `shared` mode, `rdma link add` run inside a container creates the
   device in the *init* namespace, where the container's verbs library
   cannot see it. `rdma system set netns exclusive` fixes that, but the
   kernel only allows the change while no other namespace holds RDMA
   state — in practice, at boot, before any container starts.
2. **`/dev/infiniband/` does not exist in the container.** Even with the
   device in the right namespace, verbs need the `uverbs` character
   devices, and Docker gives a container a minimal `/dev`. Labtris does
   not pass them through yet.

So this half of the pod is a starting point for that work, not a working
demo. `modprobe rdma_rxe` on the host is necessary but not sufficient.
