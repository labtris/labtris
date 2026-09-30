Labtris 0.11.0 — Ultra Ethernet on a real wire, and several things
that had never actually run.

`uet-ref` packages the Ultra Ethernet Consortium's own reference
provider (MIT): SES with tagged and untagged messages and RMA, PDS,
TSS with AES-GCM, all four delivery modes, and a partial
implementation of UET congestion control. It needs no hardware — a
raw Ethernet socket on an ordinary veth is enough. UET is its own
transport on IP protocol 253, and the reference Wireshark dissector
ships alongside, so a capture decodes 103 fields instead of reading
`Unknown (253)`.

Five AI-fabric labs now arrive bundled and wired on first boot: a UET
pair, an RDMA pair, a bmv2 switch running the UEC packet-trimming
program, a link with eight priority classes, and a two-spine
two-leaf fabric with four UET endpoints. Every image they need pulls
from a public registry.

### Three things that were broken and are not any more

**P4 never ran.** The bmv2 node used `p4lang/behavioral-model`, which
ships no compiler at all, so `p4c-bm2-ss` was "command not found" on
every start. A fallback kept the container alive, so Labtris reported
the node `running` while the switch was dead. The command also passed
`--no-p4`, which tells simple_switch to start *without* a program,
and never passed `-i`, so it had no ports either. All three fixed;
`simple_switch_CLI` now shows the program's own match table.

**Link impairment never applied** on any install with a current
pyroute2. The tbf and red calls passed `parent=None` for a root
qdisc; 0.8 puts that through an integer conversion and raises
"required argument is not an integer". Rate, ECN and PFC all failed
with that one opaque message and nothing in the log.

**The container shipped none of the runtime packaging data** — no
demo pods, no dissector, no P4 built-ins — so a compose install was a
materially different product from an ISO install, and P4 could not
work there at all.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.11.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.11.0-amd64.iso.part-* > labtris-0.11.0-amd64.iso
sha256sum -c labtris-0.11.0-amd64.sha256
```

Upgrading from 0.10.0: `cd /opt/labtris && git pull && sudo
systemctl restart labtris-api`. No migration.

## Ultra Ethernet node kinds

| | `ue-sim` | `ue-stack` |
|---|---|---|
| **What it is** | Kaima Lab's ns-3 3.44 UEC simulator | New PoC userspace daemon in this repo |
| **Traffic** | Simulated inside the container's ns-3 process | Real UDP frames on the container's veth |
| **Determinism** | Full | Real-network |
| **Best for** | Protocol validation, benchmarks | Real-packet labs, tcpdump traces, integration with real Linux tools |

Both drop onto the canvas from the palette. Build each with the
Dockerfiles in `packaging/dockerfiles/` — see the READMEs for exact
commands. Not pre-built by the labtris installer (the `ue-sim`
image is a ~2 GB ns-3 build; shipping the Dockerfile lets you pin
the upstream commit).

### `ue-sim` — packaged from github.com/kaima2022/UE-Sim

- Ubuntu 22.04 + GCC 10+, ns-3 3.44, UE-Sim compiled release-profile
- `labtris node exec <id> -- ns3 run soft-ue-e2e-concepts` fires
  the end-to-end walkthrough
- SES + PDS + PDC implementations from the paper

### `ue-stack` — new in this repo, first cut

The real userspace stack the user asked for. Lands the shape:

- `ue_stack/packet.py` — SES + PDS + PDC header pack/unpack
- `ue_stack/pdc.py` — Ipdc (fire-and-forget, working end-to-end) +
  Tpdc (reliable with ACK+RTO, wire format works, state machine
  is a stub)
- `ue_stack/daemon.py` — async UDP daemon on port 4791
- `uestack` CLI — `uestack listen`, `uestack send <host>:<port> <msg>`

Two-node smoke test:

```
node-a $ uestack listen
node-b $ uestack send 10.0.0.10:4791 "hello ue"
```

Explicitly a PoC: not spec-interop-ready, no CC, no packet
spraying, TPDC state machine is minimal. What's stub, what's real,
and the roadmap to full spec conformance are all in
[`docs/design/ue-stack-poc.mdx`](https://docs.labtris.com/design/ue-stack-poc)
— the shape is the load-bearing piece, everything above the wire
format stays when the spec pinning happens.

## New: FreeRADIUS container profile

`freeradius/freeradius-server:latest` as a first-class node kind
for AAA-shaped labs — 802.1X / WPA2-Enterprise experiments,
network-device management auth, VPN concentrator testing. Listens
on UDP 1812 (auth) + 1813 (accounting). Ships with a default
clients.conf accepting localhost with secret `testing123`; edit
`/etc/raddb/clients.conf` inside the container to add your NAS.

## Verified

- `ue_stack` unit tests pass end-to-end: header round-trip,
  IPDC sequence advancement, TPDC pending-ACK bookkeeping, and a
  real-packet loopback send/receive.
- Container profiles `ue-sim`, `ue-stack`, `freeradius` render in
  the palette catalog with correct cache-status probes.

## What's next

`docs/design/ue-stack-poc.mdx` outlines Phases J1–J4 for `ue-stack`
(spec-pin wire format, CC algorithm, packet spraying, real-NIC
interop). Volunteers welcome.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
