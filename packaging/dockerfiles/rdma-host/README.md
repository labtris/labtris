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

## Status: working, on kernel 7.1 or newer

Verified end to end in Labtris on 2026-09-27, Ubuntu 26.04.1 with a
mainline **7.2.6** kernel, two `rdma-host` nodes on an ordinary Labtris
bridge:

```
#bytes   #iterations   BW peak[MB/sec]   BW average[MB/sec]   MsgRate[Mpps]
1024     200           94.42             93.74                0.095990
```

That is a real RoCEv2 transfer between two lab nodes, over soft-RoCE, on
a machine with no RDMA hardware in it.

### The kernel is the whole requirement

`rdma_rxe` created its UDP 4791 socket in `init_net` only, so a device
created inside a container was bound to a socket that did not exist
there — the transfer connected, exchanged GIDs, and then silently moved
no packets. That was fixed upstream and **landed in Linux 7.1**:

```
v7.0  rxe_net.c:  static struct rxe_recv_sockets recv_sockets;   /* one global */
                  ip_route_output_key(&init_net, &fl);
v7.1  rxe_net.c:  ip_route_output_key(net, &fl);                 /* caller's net */
                  ip6_dst_lookup_flow(net, rxe_ns_pernet_sk6(net), ...)
```

One command tells you whether a host can do this:

```bash
ss -lun | grep 4791          # a per-namespace listener = 7.1+, good
                             # one global 0.0.0.0:4791 = too old
```

Ubuntu 26.04 ships 7.0 and **cannot** do it. 24.04 ships 6.8 and cannot
either. Install a mainline kernel (kernel.ubuntu.com/mainline, 7.1 or
later) on the Labtris host.

### Leave the netns mode alone

Counter-intuitively, `rdma system set netns exclusive` is **not** needed
and still does not work — devices land on the host and a second one
fails with ENFILE. The default `shared` mode is correct on 7.1+, because
the per-namespace socket is what makes the transmit path work, not the
device-scoping mode. Do not stop polkit, do not stop your containers.

### The recipe

```bash
# once on the host, on a 7.1+ kernel
sudo modprobe rdma_rxe
echo rdma_rxe | sudo tee /etc/modules-load.d/rdma_rxe.conf

# per node — distinct device names, because rxe0 is global under shared mode
labtris node exec <rdma-a> -- rdma link add rxe_a type rxe netdev eth1
labtris node exec <rdma-b> -- rdma link add rxe_b type rxe netdev eth1

# -x 1, not -x 0: index 0 is the IPv6 link-local RoCEv1 GID, index 1 is
# RoCE v2 over IPv4
labtris node exec <rdma-a> -- ib_send_bw -d rxe_a -x 1        # server
labtris node exec <rdma-b> -- ib_send_bw -d rxe_b -x 1 <a-ip> # client
```

Labtris bind-mounts the host's `/dev/infiniband` into `rdma-host` nodes,
so the `uverbs` device for each rxe link appears inside the node as soon
as it is created. Nothing else is needed.

Soft-RoCE is a software device: ~94 MB/s here, nowhere near a real
ConnectX. Correctness is the point — this teaches and tests RDMA code, it
does not benchmark hardware.

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
