# Demo pods

Three curated cold-snapshot pods you can load and inspect without
building a topology by hand. Each was `labtris lab generate`d on a
running instance, cold-snapshotted, and checked in here.

| Pod | Nodes | Shape |
|---|---|---|
| `spine-leaf-2x2-evpn.pod.tar.gz` | 6 | 2 spines × 2 leaves × 1 host, FRR everywhere, BGP EVPN configs pre-populated in `Node.startup_config` |
| `rail-optimised-2x2.pod.tar.gz` | 6 | 2 rails × 2 hosts, per-rail iBGP island (AS 64701 / 64702) |
| `fat-tree-k2.pod.tar.gz` | 7 | k=2 Al-Fares fat tree: 1 core + 2 pods × (1 agg + 1 edge) + 2 hosts, per-pod eBGP up to core |

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

**Known-bad: the fat-tree pod does not converge.** spine-leaf and
rail-optimised both reach Established on every session. On fat-tree the
AS numbering and neighbour discovery are right — core-1 sees
`agg-1-1(eth0) AS 65101` and `agg-2-1(eth1) AS 65102` over IPv6
link-local — but the sessions sit in Idle after exchanging OPENs. Being
chased; the pod is shipped because it is still a valid topology to load
and inspect.

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
