Labtris 0.9.0 — Phase G: the LinkGroup multipath schema, BGP EVPN
configs on all three fabric patterns, a soft-RoCE host preset that
stands in for Ultra Ethernet until upstream lands, a stable PFC
schema hint reserved for the real DCB runtime, and three curated
demo pods you can load without building a topology yourself.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.9.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.9.0-amd64.iso.part-* > labtris-0.9.0-amd64.iso
sha256sum -c labtris-0.9.0-amd64.sha256
```

Upgrading from 0.8.0: `cd /opt/labtris && git pull && sudo alembic
upgrade head && sudo systemctl restart labtris-api`. Migration
adds a `link_groups` table + nullable `links.group_id` and drops
the UNIQUE constraints on `links.a_iface_id` / `links.b_iface_id`
(see below).

## New

**LinkGroup multipath schema.** Parallel Links between the same node
pair are now first-class shapes on the canvas — Alembic 0019 adds a
`link_groups` table + nullable `Link.group_id` FK + drops the UNIQUE
constraints that blocked parallel wires. New router:

```
POST   /api/v1/labs/{id}/link-groups   {name, a_node_id, b_node_id, count, hash_policy?}
GET    /api/v1/labs/{id}/link-groups
DELETE /api/v1/link-groups/{id}
```

`hash_policy` defaults to `layer3+4`. Real runtime ECMP (Linux bond
or eBPF hash) is future work — today the guest's control plane
(FRR + `bgp bestpath as-path multipath-relax` etc.) does the
balancing across the parallel uplinks, which is what real DC
fabrics do anyway. The load-bearing piece is the schema: dual-
homed hosts, 4×100G spine uplinks, and every other real-DC shape
were structurally impossible before this migration.

**BGP EVPN configs on rail-optimised + fat-tree.** F2 shipped this
for spine-leaf; G1 extends it to the other two patterns. Rail-
optimised gets per-rail iBGP islands (AS 64700 + rail, router-id
10.<rail>.0.1 for spine, 10.<rail>.<h>.1 for hosts). Fat-tree gets
the Al-Fares AS layout: cores at AS 65000, each pod at AS 65100+
pod, unnumbered eBGP up + unnumbered iBGP within the pod.
`labtris lab generate --pattern fat-tree --k 2 --with-bgp-evpn`
produces a working 7-node fabric with FRR configs pre-loaded on
every node.

**RDMA host container profile.** New `rdma-host` node kind in the
palette: Ubuntu + rdma-core + perftest, with `rdma link add rxe0
type rxe netdev eth0` in the first-boot script. Closest working
stand-in for Ultra Ethernet's userspace RDMA verbs today (soft-
RoCE / rxe). When a public UET stack lands upstream, a `uet-host`
preset drops in next to this one with the same shape.

**Three curated demo pods.** `packaging/demo-pods/`:
- `spine-leaf-2x2-evpn.pod.tar.gz` — 2 spines × 2 leaves × 1 host
- `rail-optimised-2x2.pod.tar.gz` — 2 rails × 2 hosts
- `fat-tree-k2.pod.tar.gz` — 7-node Al-Fares k=2

Each is <2.5 KB (cold snapshot of all-Docker topology → topology
manifest + startup_configs, no bytes). Load with
`labtris lab load <path>` for a working fabric in one command.

**PFC schema stub.** `ImpairSpec` gains `pfc: bool` +
`pfc_priorities: list[int]`. tc.set accepts them, logs "pfc
requested but not implemented", and installs only netem+tbf+red.
Real 802.1Qbb pause frames need OVS or an eBPF per-priority pause
path — a Phase H project. Ships the stable schema shape today so a
lab spec that names `pfc: true` does not need changing when the
runtime lands.

## Non-goals still tracked

- **Real 802.1Qbb PFC pause frames** — needs OVS or eBPF per-priority
  pause path. Schema is stable (see above); runtime is Phase H.
- **Linux-bond runtime for LinkGroup** — kernel-side ECMP without a
  routing daemon. Today the guest's own control plane does the
  balancing.
- **Ultra Ethernet preset** — no public UET stack exists yet.
  `rdma-host` is the stand-in until upstream lands.

## Verified

- `labtris lab generate --pattern fat-tree --k 2 --with-bgp-evpn`
  produces core/agg/edge nodes with correct FRR configs (AS 65000
  core, AS 65101 pod-1, AS 65102 pod-2, correct up/down eBGP).
- `POST /labs/{id}/link-groups` with count=3 between two nodes
  creates 3 member Links; `GET /labs/{id}/link-groups` lists them;
  `DELETE /link-groups/{id}` cascades to member Links.
- Three demo pods cold-snapshot cleanly (2 KB each), extract to
  valid `snapshot.json` + `lab.json` + `templates.json` +
  per-node dirs.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
