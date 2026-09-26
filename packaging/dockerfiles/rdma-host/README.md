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

So this is not a kernel-version problem and not something Labtris can fix
from above. Two Linux network namespaces doing soft-RDMA to each other
does not work with the in-tree soft transports as they stand.

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
