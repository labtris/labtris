# rdma-host container image

Ubuntu 24.04 + `rdma-core` + `perftest` (ib_send_bw / ib_write_bw)
+ `iproute2 iputils-ping tcpdump`, all baked in at build time so
the container starts in seconds without needing internet access
inside the lab bridge.

## Build

```bash
docker build \
  -t labtris/rdma-host:latest \
  packaging/dockerfiles/rdma-host/
```

## Status: blocked by the kernel, not by Labtris

Measured on Ubuntu 24.04, kernel 6.8.0-139-generic, September 2026. Read
this before spending an afternoon on it.

**Soft-RoCE between two containers does not work on this kernel.** The
cause is not Docker and not Labtris — it is `rdma_rxe`'s handling of
network namespaces, and it reproduces with no containers involved at all:

```bash
rdma system set netns exclusive        # only settable when init is the
                                       # ONLY netns, i.e. at boot
ip netns add t
ip link add vh type veth peer name vc && ip link set vc netns t
ip netns exec t rdma link add rxe_ns type rxe netdev vc   # rc=0
ip netns exec t rdma link show                            # EMPTY
rdma link show                                            # rxe_ns is HERE
```

The device is created **on the host** and is invisible inside the
namespace that asked for it, even though `rdma system show` reports
`netns exclusive` both inside and out. A second namespace then cannot
create its own, because the name is effectively global:

```
rdma_rxe: rxe_newlink: failed to add eth0
rxe0: rxe_newlink: already configured on eth0
```

In the default `shared` mode the device is created on the host by design.
Verbs then work — see below — but the transmit path does a route lookup
in the *host's* namespace, which has no route to the lab subnet, so
nothing reaches the wire. Confirmed by capturing on the node's own
`eth1` during an `ib_send_bw` run: the TCP out-of-band exchange on port
18515 is there, and not one UDP packet on 4791.

### What does work, in shared mode

Everything up to the transfer, which is why the image is still worth
having:

* `rdma link add <name> type rxe netdev eth1` — ACTIVE, bound to the
  node's veth.
* `/dev/infiniband/uverbsN` appears per device, and Labtris bind-mounts
  that directory into `rdma-host` nodes, so a node sees each device as it
  is created.
* `ibv_devinfo` inside a node lists the HCAs.
* `ib_send_bw` connects and exchanges RoCEv2 IPv4 GIDs
  (`::ffff:10.88.0.1`).

### A newer kernel does not fix it — tested

An earlier version of this file suggested trying a newer kernel, on the
theory that rxe's namespace handling had improved since 6.8. It has not.

Tested in a QEMU guest on **Ubuntu 26.04.1 LTS, kernel 7.0.0-31**, same
experiment, same result: devices create and report ACTIVE bound to the
right netdev, `ibv_devinfo` lists them from inside each namespace,
`ib_send_bw` connects and exchanges RoCEv2 IPv4 GIDs, and then no
bandwidth row ever appears and no UDP 4791 packet reaches the wire.

Two things 7.0 does differently, neither of which helps:

* In `shared` mode a device IS now visible from inside the namespace that
  created it, showing its own netdev. On 6.8 the namespace saw nothing.
* `rdma system set netns exclusive` still fails while any other network
  namespace exists, which is the practical blocker. On the Labtris host
  that is simply the running containers — every one holds a netns, so the
  setting can only go through with the whole stack stopped. In a bare
  Ubuntu 26.04 VM with no containers at all it still failed, and there
  the culprit was `polkitd`: 26.04 hardens `polkit.service` with
  `PrivateNetwork=yes`, so the authorization daemon sits in its own empty
  network namespace. (24.04 does not — `PrivateNetwork=no` there.)
  Stopping polkit lets the setting through, after which rxe still places
  the device on the host and a second namespace's add fails with ENFILE
  ("Too many open files in system").

`siw` (SoftiWARP) was tried as an alternative transport, with and without
`-R`. Same outcome.

### Every combination tried

| Kernel | RDMA netns mode | Namespaces via | Result |
|---|---|---|---|
| 6.8.0-139 | shared | Docker | verbs work, zero packets on the wire |
| 6.8.0-139 | exclusive | Docker | `/sys/class/infiniband/` empty, no verbs |
| 6.8.0-139 | exclusive | `ip netns` | device created on the host instead |
| 7.0.0-31 | shared | `ip netns` | device visible in the namespace, zero packets |
| 7.0.0-31 | exclusive | `ip netns` | device on the host; second add `ENFILE` |
| 7.0.0-31 | shared | Docker (Labtris) | verbs work, zero packets on the wire |
| 7.0.0-31 | exclusive | Docker (Labtris) | device on the host; second add `ENFILE` |

Every cell reached the same place. In `shared` the device is on the host
by design, so the transmit path resolves the peer in the host's namespace
and nothing leaves. In `exclusive` the device is *still* placed on the
host — `rdma system show` reports exclusive from inside the container,
and the device lands outside it anyway — so the second namespace collides
on the name and gets `ENFILE`.

Setting `exclusive` at all needs init to be the only network namespace.
On 26.04 that means stopping `polkit` as well as every container, because
`polkit.service` is hardened with `PrivateNetwork=yes` there. Doing all
of that, and confirming `netns exclusive` from inside the node, still
produced the row above.

So this is not a kernel-version problem and not something Labtris can fix
from above. Two Linux network namespaces doing soft-RDMA to each other
does not work with the in-tree soft transports as they stand.

### Someone is already fixing this upstream — do not fix it here

`rdma_rxe` simply did not support network namespaces. That is not a
misconfiguration on our side, and it is being addressed by an active
patch series on linux-rdma: *"RDMA/rxe: Add the support that rxe works
in net namespace"*, at v7 by March 2026, with a stated goal of landing
netns support for 7.0. It makes every rxe device exclusive to its own
namespace, creates and tears down the UDP 4791 socket per namespace, and
ships a selftest that runs exactly the command that fails here
(`ip netns exec net0 rdma link add rxe0 type rxe netdev ...`).

Ubuntu 26.04's 7.0.0-31 does **not** carry it yet. One check settles it:

```bash
ss -lunp | grep 4791
```

A kernel with the series gives each namespace its own listener. This one
has a single global `0.0.0.0:4791` in init, which is precisely why a
device created from inside a container ends up on the host and nothing
transmits.

**So there is nothing for Labtris to implement.** When the series reaches
a shipping kernel, re-run the test — the Labtris side is already done and
verified: `/dev/infiniband` is passed into `rdma-host` nodes, device
names are per-node, and the RoCEv2 GID index is documented. Carrying a
kernel patch ourselves would trade a working `curl | bash` story for
"run our kernel", which is a bad trade for a self-hosted product.

One thing to expect when it lands: the series puts every rxe device in
exclusive mode per namespace, and `rdma system set netns exclusive`
requires init to be the only namespace when set — on 26.04 that means
stopping `polkit` too. That is a boot-order note for the installer, not a
blocker.

### What this image is for, then

Learning the RDMA userspace: `rdma link`, `ibv_devinfo`, the verbs API,
what a queue pair and a GID index are, and reading RoCEv2 addressing.
All of that works. Running an actual RDMA transfer between two lab nodes
does not, and nothing on the Labtris side is what is stopping it.

For a lab that puts real, inspectable frames on a real wire today, use
the `ue-stack` nodes instead — see
`packaging/demo-pods/rdma-uet-demo.pod.tar.gz`.

## Host prerequisite: rdma_rxe

The container tries `rdma link add rxe0 type rxe netdev eth0` on
start. That needs the host kernel to have loaded the `rdma_rxe`
module. Run once on the labtris host:

```bash
sudo modprobe rdma_rxe
```

Survives reboots via /etc/modules-load.d:

```bash
echo rdma_rxe | sudo tee /etc/modules-load.d/rdma_rxe.conf
```

## Quick test — two nodes on a bridge

```bash
# per node — give each its OWN device name. rxe0 is global in the default
# netns-shared mode, so the second node to use that name collides.
labtris node exec <rdma-a> -- rdma link add rxe_a type rxe netdev eth1
labtris node exec <rdma-b> -- rdma link add rxe_b type rxe netdev eth1
labtris node exec <rdma-a> -- rdma link show      # expect state ACTIVE
labtris node exec <rdma-a> -- ibv_devinfo -l      # expect both HCAs

# -x 1, not -x 0: index 0 is the IPv6 link-local RoCEv1 GID. Index 1 is
# RoCE v2 over IPv4, which is what you want. Check with
#   cat /sys/class/infiniband/<dev>/ports/1/gid_attrs/types/1
labtris node exec <rdma-a> -- ib_send_bw -d rxe_a -x 1        # server
labtris node exec <rdma-b> -- ib_send_bw -d rxe_b -x 1 <a-ip> # client
```

`rdma link show` reporting ACTIVE and `ibv_devinfo` listing the HCAs both
work. The benchmark will connect, exchange GIDs and then produce no
result — see the status section above.

## Upgrade from the pre-0.12.0 shape

The previous shape used `ubuntu:noble` with an apt-install in the
container's cmd. It failed silently on any lab without internet in
the container's netns (which is most of them). Old rdma-host nodes
will keep working (they're `ubuntu:noble` containers that never got
their tools installed); replace them by dragging a fresh `RDMA
host` chip from the palette — the palette now points at
`labtris/rdma-host:latest`.
