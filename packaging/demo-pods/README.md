# Demo pods

Three curated cold-snapshot pods you can load and inspect without
building a topology by hand. Each was `labtris lab generate`d on a
running instance, cold-snapshotted, and checked in here.

| Pod | Nodes | Shape |
|---|---|---|
| `spine-leaf-2x2-evpn.pod.tar.gz` | 6 | 2 spines × 2 leaves × 1 host, FRR everywhere, BGP EVPN configs pre-populated in `Node.startup_config` |
| `rail-optimised-2x2.pod.tar.gz` | 6 | 2 rails × 2 hosts, per-rail iBGP island (AS 64701 / 64702) |
| `fat-tree-k2.pod.tar.gz` | 7 | k=2 Al-Fares fat tree: 1 core + 2 pods × (1 agg + 1 edge) + 2 hosts, per-pod eBGP up to core |

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
