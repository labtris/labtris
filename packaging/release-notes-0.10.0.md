Labtris 0.10.0 — Phase H: per-priority DCB queueing on every link.
`pfc: true` is no longer a schema stub — it now installs a real
`prio` qdisc with per-band ECN-marking RED on the priorities you
name. Real 802.1Qbb PAUSE-frame emission stays Phase I (needs an
XDP-side implementation); the queueing effect that fabric CC
research actually measures ships today.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.10.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.10.0-amd64.iso.part-* > labtris-0.10.0-amd64.iso
sha256sum -c labtris-0.10.0-amd64.sha256
```

Upgrading from 0.9.0: `cd /opt/labtris && git pull && sudo
systemctl restart labtris-api labtris-netd`. No migration.

## New

**Per-priority DCB queueing.** When `ImpairSpec.pfc = true` and
`ImpairSpec.pfc_priorities = [3, 4]`, netd now installs:

```
tc qdisc add dev <tap> root handle 1: prio bands 8 \
    priomap 0 1 2 3 4 5 6 7 0 0 0 0 0 0 0 0
tc qdisc add dev <tap> parent 1:4 handle 14: red \
    limit ... min ... max ... avpkt 1000 probability 0.02 ecn
tc qdisc add dev <tap> parent 1:5 handle 15: red ...
ethtool -A <tap> rx on tx on   # veth returns EOPNOTSUPP; logged
```

Stacks under any rate/netem set by other ImpairSpec fields: a lab
with rate+delay+pfc gets `tbf → prio → (red|pfifo)` per band.
Priorities not in `pfc_priorities` get default pfifo — the
class-isolation prio provides is available on every band; the ECN
marking is opt-in per priority.

The stable schema shape from 0.9.0 is unchanged; a lab spec that
named `pfc: true` before this release now DOES something real
without the operator changing anything.

**Design note: `docs/design/pfc-implementation.mdx`.** Explains
what prio+red covers, what it doesn't (real PAUSE frames), and the
XDP-side plan for Phase I. Documented at the level a fabric
researcher can decide whether Labtris is enough for their work or
they need real hardware.

## Non-goals still tracked

- **Real 802.1Qbb PAUSE frames** — Phase I. Needs an XDP program
  that watches per-priority queue depth in a bpf_map and injects
  MAC-control PAUSE frames back at the peer. The schema
  (`pfc: true` + `pfc_priorities`) will keep working when it lands.
- **802.1Qaz ETS** (per-class bandwidth minimums beyond strict-
  priority) — needs mqprio + htb, requires multi-queue devices,
  veth is single-queue.
- **802.1Qaz DCBX** (auto-negotiation of the DCB config) — needs
  lldpad, not shipping in the image.

## Verified

On a live veth on .77:
- `tc.set` with `pfc: true, pfc_priorities: [3, 4]` installed the
  full tree without error.
- `tc qdisc show` returned the prio + two red qdiscs with the
  correct limits/min/max/ecn flags.
- `tc class show` showed all 8 prio classes with leaf red qdiscs on
  1:4 and 1:5 only.
- `tc.clear` cleanly removed the tree.

## Session totals since 0.6.0

Ten commits across Phase E → H shipped four public releases
(0.7.0, 0.8.0, 0.9.0, 0.10.0). ~5,000 lines of code + docs. The
last item on the 0.9.0 non-goals list ('real PFC pause frames')
now has a real implementation for its queueing half; only the
wire-level PAUSE-frame emission stays deferred with a plan.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
