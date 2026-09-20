Labtris 0.7.0 — Phase E: the LLM-native network lab grows a
programmable data plane, live per-link stats on the canvas, an
AI-fabric topology generator, and the palette gets a Pull-now button
for Docker images. The pieces you need to prototype RoCE, Ultra
Ethernet, and any other AI-fabric shape without leaving the browser.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.7.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.7.0-amd64.iso.part-* > labtris-0.7.0-amd64.iso
sha256sum -c labtris-0.7.0-amd64.sha256
```

Upgrading from 0.6.0: `cd /opt/labtris && git pull && sudo alembic
upgrade head && sudo systemctl restart labtris-api`. The migration
adds one nullable JSONB column (`Node.opts`) — no data touched.

## New

### P4 (bmv2) programmable switch as a first-class node kind

Drag `⌥ P4 switch (bmv2)` onto the canvas and it comes up with
`basic_switch.p4` compiled and forwarding. Swap the program per-node:
pick one of four curated built-ins, or upload your own `.p4`.

Curated programs in `packaging/p4-programs/`:
- **basic_switch** — L2 forwarding by dst MAC (baseline)
- **ecmp** — per-packet 5-tuple hash ECMP (packet-spraying primitive)
- **ecn** — mark ECN CE when the egress queue depth crosses a threshold
- **trim** — Ultra Ethernet-style packet trimming under queue pressure

Node inspector grows a "P4 program" section — dropdown + upload +
live state. CLI: `labtris node p4 {show,builtins,set,upload,clear}`.
MCP: `p4_builtins`, `node_p4_show`, `node_p4_set_builtin`,
`node_p4_clear`.

Under the hood, each bmv2 node carries a per-instance bind mount at
`/p4` — new generic `per_node_mounts` mechanism on `ContainerImage`,
usable by any future node kind that wants a per-instance file dir.
`Node.opts JSONB` (Alembic 0018) is the runtime-agnostic sibling to
`qemu_opts` and holds `{p4_source, p4_program}`.

Full page: [docs.labtris.com/p4](https://docs.labtris.com/p4).

### Live per-link traffic stats on the canvas

Click the new **⇋ Traffic** toggle in the canvas control row. Every
link gets a small midpoint label showing `rx ↔ tx` in short-form bps
(12k, 4.2M, 1.1G). Quiet links (< 1 kbps) stay uncluttered.

Powered by a new `iface.counters` netd verb that reads IFLA_STATS64
for a list of interfaces in one netlink dump, plus a runtime-side
rate cache that derives bps/pps/dps from adjacent snapshots. A canvas
with 100 links needs one netd round-trip per 2-second poll.

New endpoints:
- `GET /api/v1/links/{id}/stats` — one link, both endpoints
- `GET /api/v1/labs/{id}/link-stats` — one lab, every link, batched

MCP tools `link_stats` and `lab_link_stats` — the assistant can now
answer "what is going through this link right now?" with real
numbers instead of opening a capture.

### AI-fabric topology generator

Three patterns, one call:

```
labtris lab generate <lab-id> --pattern spine-leaf \
  --spines 2 --leaves 4 --hosts-per-leaf 8 \
  --spine-kind bmv2 --p4-program ecmp
```

- **spine-leaf** — N spines × M leaves × K hosts-per-leaf (full CLOS)
- **rail-optimised** — R rails × H hosts-per-rail, one spine per rail
  (the GPU-cluster shape)
- **fat-tree** — k-ary Al-Fares 2008 (core, aggregation, edge, hosts)

Every generated Node/Link/Network row is the same shape a hand-drawn
lab produces — no hidden "generated" flag. After generation you can
delete a node, edit a link, add a wire. `POST
/api/v1/labs/{id}/generate` + `labtris lab generate` CLI + MCP
`lab_generate` tool.

Full page: [docs.labtris.com/fabrics](https://docs.labtris.com/fabrics).

### Palette: Pull-now button for Docker images

Docker images with no local cache used to stall the first spawn on a
silent multi-minute registry pull. Now every catalog docker image
shows its cache status in the palette (`✓` for cached, "not on this
host" for uncached), with a **Pull now** button that fires a
background pull and flips to `✓` when done. Same UX pattern the QEMU
Pull-now button has had since 0.5.0.

The old "already starting" error is also fixed. Used to say "a
first-time qemu image is downloaded and converted before boot" for
any node — now it names the actual runtime and points at the
right progress surface.

## Fixes

**Wires no longer disappear behind node boxes.** The `<svg class="wires">`
layer sat in document order alongside the `.node` divs; without an
explicit z-index, the divs stacked on top and any wire crossing a
node vanished. `z-index: 2` + `pointer-events: none` on the wires
container lifts the connectivity above the bodies where it belongs.
Node clicks still land because the SVG root is now transparent to
pointer events and `.link-hit` opts back in on the stroke only.

**Segment-wire dashes read as a line, not spotty dots.** Was
`stroke-dasharray: 1 5` — one pixel on, five off — which on a long
span read as broken cable. Now `3 3` (short even dashes) so the
segment-vs-direct distinction survives but the wire looks like a
wire.

## Positioning

The site copy and README now lead with "the LLM-native network lab"
and carry a founder-story `## Why this exists` block explaining the
project's actual motivation: developing new networking features on
one's own infrastructure and fine-tuning LLM models on the specific
task of reading a running network. `docs/design/*` gains no new
notes; every position claim lives on the two front doors people
actually read.

## Verified end-to-end on a real instance

- `labtris node p4 set <id> --builtin ecmp` copies ecmp.p4 into the
  per-node mount dir and updates `Node.opts` to `{p4_source: "builtin",
  p4_program: "ecmp"}`.
- `docker` cache probe surfaces 5 uncached images with Pull-now
  buttons; bmv2 (previously pulled during E1 verify) reports cached.
- `iface.counters` returns real IFLA_STATS64 for two links in the
  canvas; second poll 3 s later shows rx_bytes climbed 683861 →
  684020 (~50 B/s FRR chatter, real numbers).
- `labtris lab generate <id> --pattern spine-leaf --spines 2
  --leaves 3 --hosts-per-leaf 1` produces 8 nodes, 9 links, 9
  networks with the correct wiring and naming.

## Non-goals for this release

- **BGP EVPN startup-config generation** on generated fabrics — flag
  accepted, wiring not yet written.
- **Cross-rail wiring for rail-optimised** — the shape UEC argues for
  improving; modelling only one rail keeps the pattern honest.
- **Dual-homed hosts in spine-leaf** — needs multipath, deferred to
  Phase F.
- **Ultra Ethernet reference stack** — tracking upstream; ship a
  preset in a week when a public UET stack lands.

## Bring your own images

Unchanged from 0.6.0. Labtris redistributes no vendor software.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
