Labtris 0.8.0 — Phase F: the DB pool leak that surfaced during long
AI-assistant chats is fixed, generated spine-leaf fabrics now come up
with a working BGP EVPN underlay in one call, and links can mark ECN
under queue pressure — the last primitive needed to prototype DCTCP
/ DCQCN / Ultra Ethernet congestion control end to end with the
`ecn.p4` bmv2 built-in from 0.7.0.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.8.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.8.0-amd64.iso.part-* > labtris-0.8.0-amd64.iso
sha256sum -c labtris-0.8.0-amd64.sha256
```

Upgrading from 0.7.0: `cd /opt/labtris && git pull && sudo systemctl
restart labtris-api`. No migration.

## Fixes

**QueuePool exhaustion after hours of use.** `routers/ai.py`'s
WebSocket chat wrapped an entire agent turn in a single
`async with SessionLocal()`, holding a pooled connection for minutes
across every yield point. N concurrent chats + hung disconnects
progressively exhausted the 15-slot pool; new requests timed out
with the SQLAlchemy TimeoutError users hit in 0.6.0 / 0.7.0. Fix
narrows the session scope: `_lab_context` (one query up front) and
the LlmUnavailable fallback (local_plan) each open their own
short-lived session; the streaming agent loop itself needs no DB.

Belt-and-suspenders: `pool_recycle=1800` on the engine, so any
session that escapes a future audit is reclaimed inside 30 min
instead of sitting in the pool until systemd restarts the process.
Base pool_size stays at 5 and overflow at 10 — a workload needing
more than 15 concurrent sessions is a leak worth finding, not a
pool worth growing.

## New

**BGP EVPN startup-config generation.** The topology generator's
`--with-bgp-evpn` flag on spine-leaf now writes a working FRR
underlay into each spine's and leaf's `Node.startup_config`:
zebra + bgpd, loopback router-id, unnumbered eBGP between every
spine-leaf pair (spine AS 65000, leaves AS 65001+), L2VPN EVPN
address family with `advertise-all-vni` for overlay discovery.

The generated `startup_config` is a self-installing shell script:

```
labtris node push <id>                          # writes /config/startup-config
labtris node exec <id> -- sh /config/startup-config
                                                 # applies + reloads watchfrr
```

Only spine-leaf gets EVPN configs in this pass. rail-optimised and
fat-tree remain topology-only; both are less useful without an
addressing plan, so BGP for them waits for a per-pattern
addressing story (Phase G).

**ECN marking as a first-class link primitive.** `ImpairSpec` grows
`ecn` / `ecn_min_bytes` / `ecn_max_bytes`. When enabled, `tc.set`
installs a RED qdisc below netem+tbf that marks the ECN CE bit on
packets whose queue-avg is in [min, max) and drops only above max.
Composes with the `ecn.p4` bmv2 built-in from 0.7.0 for end-to-end
DCTCP / DCQCN / Ultra Ethernet CC prototyping — the switch marks,
the fabric marks, the receiver echoes, the sender slows.

New `datacenter` preset alongside `lan` / `wifi` / `3g` / `satellite`
/ `lossy-wan`: 25 Gbps rate + RED with a 50-150 KB ECN band, shaped
closer to a real DC leaf-spine hop than the LAN preset's 2 ms.

Full PFC / ETS / DCBX pause-frame primitives are deferred; they need
OVS or an eBPF pause-signal path per priority queue. Marking-based
ECN is the first-order signal every DC CC algorithm reads, and
covers most of the actual research load.

## Deferred (with a design note)

**Multipath / LinkGroup** — parallel links between the same node pair,
for dual-homed hosts and full-BW spine uplinks. See
`docs/design/multipath-deferred` for why it did not ship in this
phase and the plan when it lands.

## Verified

- QueuePool timeouts no longer reproduce after the fix; API restart
  clears cleanly, health probes return 401 as expected.
- `labtris lab generate --pattern spine-leaf --spines 2 --leaves 2
  --with-bgp-evpn`: 4 nodes with valid FRR configs including
  correct AS numbers (65000 spine, 65001/65002 leaves), router-ids
  (10.0.0.1..10.0.0.102), and eBGP peer sessions on each spine's
  eth0..eth(N-1).
- `tc.set` accepts `ecn: true` + `ecn_min_bytes: 50000` +
  `ecn_max_bytes: 150000` without netlink errors.

## Bring your own images

Unchanged. Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
