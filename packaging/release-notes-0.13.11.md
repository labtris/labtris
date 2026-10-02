Labtris 0.13.11 — a generated EVPN fabric that actually routes, and 931 nodes on one box.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

This release is mostly one story. A k=14 fat tree — 931 nodes, 2,058 links,
every switch running FRR with BGP EVPN and every host in one VXLAN segment —
was generated, started and audited on a single machine. Getting it to work
meant fixing six things, and each one was hiding the next.

## The generated EVPN fabric did not route

`--with-bgp-evpn` produced configs that established sessions perfectly and
carried nothing.

**The daemons file ran its own options as shell commands.** The template had
`zebra_options=  "-A 127.0.0.1 -s 90000000"`, and in shell `VAR=  "x"` assigns
VAR empty and then executes the quoted string. FRR started fine with both
daemons running *no options at all*; the only symptom was two lines of stderr
during a config apply nobody reads. Losing `-s 90000000` leaves zebra on the
default netlink receive buffer, which is the one thing a fabric with tens of
thousands of interfaces cannot afford.

**Nothing applied the config.** Generating it was one command; applying it was
`push` then `exec` per node, by hand. On 3,267 nodes that is 6,534 operations,
so in practice the fabric came up with the image's stock `bgpd=no`. There is
now an `apply_configs` task that does it in waves, with ordering, a memory
floor and continue-on-error.

**No config advertised anything.** All seven builders — fat-tree core, agg and
edge, spine, leaf, rail-spine, rail-host — wrote a loopback, BGP neighbours and
an `l2vpn evpn` block, and not one had an `address-family ipv4 unicast`.
Sessions came up regardless, because `frr defaults datacenter` activates peers,
so every switch reported 22 established peers and `RIB entries 0`. A fabric
that peers perfectly and carries no routes looks, from every summary command,
almost exactly like one that works.

**`advertise-all-vni` had no VNI to advertise.** FRR learns VNIs from the
kernel, so with no vxlan device in a bridge it correctly advertises nothing.
Edges now build the bridge, the VXLAN device and the port membership before
FRR starts, and hosts get an address so the overlay has tenants to carry.

Result, audited across all 931 nodes in 69 seconds: every switch uniform,
**686 of 686 hosts** reach a host in another pod, and **182 of 182** ordered
pod pairs ping at 0% loss.

## Applying config no longer melts a converged fabric

The config apply ended in an unconditional `frrinit.sh restart`. That is
correct on a cold node and catastrophic on a converged one: one edge restart
touches roughly 253 adjacencies, and 242 edges at sixteen at a time put about
4,000 adjacency events in flight — all 24 cores pinned, guest load average
4,044, and `sshd` starved off a box whose API was still answering in under a
second.

A restart is only required when the daemon *set* changes, which is true on the
first apply and false afterwards. It now compares and reloads instead, running
`frr-reload.py` to apply only the difference. Verified on a live node: a second
apply leaves `bgpd`'s process start time unchanged.

## Memory deduplication now covers container nodes

KSM has always been enabled and tuned here, citing a measured 8.5:1. That
figure came from fifteen QEMU **VMs**, and it only ever applied to VMs: KSM
merges only memory a process marks mergeable, QEMU does that for guest RAM,
and nothing does it for a container. On a container host KSM was on, scanning
25× harder than stock, and merging **zero pages** — while diagnostics reported
"on", because every knob genuinely was set.

`install-labtris.sh` now opts the container runtime in (`MemoryKSM=yes`,
version-guarded, systemd 254+ and Linux 6.4+). On the 931-node fabric that is
0 → several GB of real saving. Diagnostics shows the ratio, the saving and
which node kinds are covered rather than a bare "on".

## Starting a large lab

- **Start all runs in waves** — `batch` nodes at a time, `stagger_ms` apart,
  ordered by interface count so a leaf is not configured before its spine.
  Sequential left 23 of 24 cores idle; flat out buries the container runtime.
- **A pace selector in the interface.** The old checkbox said "Start one at a
  time" and had not been true since waves landed. Quick / Paced / Gentle / One
  at a time, each showing its numbers.
- **A memory floor.** `min_free_mb`, default 2048. Starting a fabric larger
  than the host used to run until the OOM killer chose a victim, which might
  be Postgres rather than a node.
- **Failures report why.** A counted failure used to point at a `last_error`
  that was often empty.

## Kernel limits now ship

The limits a large lab needs were previously discovered by hitting them. Both
fail in ways that name something else:

| | stock | now |
|---|---|---|
| `fs.inotify.max_user_instances` | 128 | 8192 |
| `net.*.neigh.default.gc_thresh3` | 1024 | 65536 |

The first surfaces as `failed to create shim task` and reads as a container
runtime problem. The second logs `neighbour: arp_cache: neighbor table
overflow` and silently drops resolution, so a fabric half-converges and reads
as a routing bug. The installer also adds the hostname to `/etc/hosts` when it
does not resolve — without it every `sudo` pays a DNS timeout, which is how a
loaded box comes to feel broken for no visible reason.

## Topology and interface

- **The fat-tree ceiling is 24** (4,176 nodes). k=16 was 1,344 and the host
  count is `k³/4`, so a larger k was the only way up.
- **Fat trees lay out as pods.** At k=22 the old four-row layout was 532,400 px
  wide against 800 tall — a 665:1 aspect ratio where every node is sub-pixel.
  Each pod is now a block with its hosts stacked under their own switch, which
  also turns a third of all links into short vertical segments. k=22 goes from
  532,400 × 800 to 12,000 × 14,800.
- **Users and passwords in the interface.** There was no endpoint and no
  control for changing a password — the only route was editing Postgres by
  hand. Your own change requires the current password; an admin resetting
  someone else's does not.
- **`labtris-user` ran under the wrong interpreter** and died on
  `ModuleNotFoundError: No module named 'sqlalchemy'`. It is the documented way
  back into an instance nobody can log into, and it had been broken since it
  shipped.
- **The connection pool** was 5 + 10 with a comment asserting that needing more
  meant a leak. A wave of starting nodes plus the interface polling diagnostics
  needs more; the interface was getting 500s and nginx was turning them into
  502s. Now 20 + 30, settable.

## Known limits

Starting 3,267 nodes works but takes hours: the start path is single-threaded,
at roughly 5.6 seconds a node, with `uvicorn` saturating one core while 23 sit
idle. More cores do not help a saturated thread. One flat VNI across 2,662
hosts also makes every ARP a fabric-wide flood; a VNI per pod is the right
shape. Both are next, and both need better observability first — which is the
following release.
