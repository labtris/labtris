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

## Status: verbs work, the data path does not

Measured on Ubuntu 24.04, kernel 6.8.0-139, September 2026. Everything up
to and including an established RDMA connection works; the transfer
itself does not. Read this before spending an afternoon on it.

What was verified working:

* `rdma link add <name> type rxe netdev eth1` inside a node — ACTIVE,
  bound to the node's own veth.
* `/dev/infiniband/uverbsN` appears on the host per device, and Labtris
  bind-mounts that directory into `rdma-host` nodes, so the nodes see
  each device as it is created.
* `ibv_devinfo` inside a node lists the HCAs.
* `ib_send_bw` connects, exchanges RoCEv2 IPv4 GIDs (`::ffff:10.88.0.1`)
  and prints the results header.

Where it stops: **zero RoCE packets reach the wire.** `tcpdump -i <lab
bridge> udp port 4791` during a run captures nothing, and the benchmark
never prints a result row.

The cause is the RDMA network-namespace mode. In the default `shared`
mode an rxe device created from inside a container lives in the *init*
namespace while its netdev is in the container's, and the transmit path
does not survive that split.

The obvious fix does not work on this kernel. `rdma system set netns
exclusive` can only be set when init is the only network namespace —
i.e. at boot, before Docker starts — and once set, `rdma link add` inside
a container fails:

```
rdma_rxe: rxe_newlink: failed to add eth0
error: Too many open files in system
```

with `/sys/class/infiniband/` empty inside the node, so `ibv_devinfo`
finds nothing even when a device did get created. Remounting sysfs needs
`CAP_SYS_ADMIN` and did not help.

So this image is a working RDMA *toolbox* — the tools are there, the
device appears, verbs enumerate — and not yet a working RDMA *fabric*.
Treat it as the starting point for that work.

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
