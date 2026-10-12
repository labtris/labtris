# vpp — FD.io VPP as a lab router

`ghcr.io/labtris/vpp`, built by CI from this directory on
`ligato/vpp-base:26.06-release` (fd.io VPP, Apache-2.0).

VPP owns a NIC through DPDK on hardware. A Labtris container node has no
NIC, only veths, so this image runs VPP with the DPDK plugin off and
attaches each lab link with `create host-interface name ethN` — the
af_packet driver, TPACKET v3 rings, polled by VPP workers. Everything
above the driver is the same VPP graph.

## What it needs

- `IPC_LOCK` on top of the base capabilities. The af_packet rings are
  mlocked; without it `create host-interface` fails with `No such file or
  directory`, which is not what is wrong. The catalog entry carries it.
- Seven cores. `startup.conf` pins main to core 4 and two workers to 5-6,
  so a poll-mode worker never shares a core with TRex's data-plane thread
  (TRex takes 0-2 by default). On a smaller host, lower the numbers in
  `/etc/vpp/startup.conf`; VPP refuses to start on a core it cannot see.
- No hugepages: `buffers { page-size 4K }`.

## The TRex demo

`vpp-router` makes eth0 `10.0.1.1/24`, eth1 `10.0.2.1/24`, routes
`16.0.0.0/8` out eth0 and `48.0.0.0/8` out eth1, and sets both ports to
polling. That is the DUT half of `packaging/demo-pods/vpp-trex.pod.tar.gz`.

## What was measured

Two-container prototype on the 36-core Hetzner host, 64 B UDP, 10 s per
point, TRex offering the same rate on both ports:

| offered, per direction | forwarded | where the loss was |
|---|---|---|
| 0.5 Mpps | 100 % | — |
| 1, 2, 3 Mpps | 100 % | — |
| 4 Mpps | 90 % | veth → VPP ring: VPP's own counters show every frame it received went out |
| 6 Mpps | 90 % | same; TRex achieved 6 Mpps tx |

Two things cost a run before those numbers came out. Interrupt receive
mode (`show hardware` says `interrupt`) overflowed the ring at ~1 Mpps
because the worker slept between bursts; `set interface rx-mode …
polling` fixed that. And VPP's default pinning put the workers on cores
1 and 2, which is where TRex's data-plane and RX threads sit, and the
loss was a flat 8 % at every rate. The pinning in `startup.conf` is the
fix.

So: 3 Mpps per direction, loss-free, through the whole VPP graph, and
`vppctl show runtime` tells you which node took the cycles. The ceiling
is the veth's. The same VPP on a real NIC shows you VPP's.
