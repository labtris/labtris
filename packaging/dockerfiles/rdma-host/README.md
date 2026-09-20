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
labtris node exec <rdma-a> -- rdma link show
labtris node exec <rdma-a> -- ib_send_bw -d rxe0        # server, blocks
# in another shell:
labtris node exec <rdma-b> -- ib_send_bw -d rxe0 <a-ip>  # client
```

If `rdma link show` says `state ACTIVE`, RDMA verbs are live between
the two nodes. Latency will be much higher than a real ConnectX; the
correctness is what matters.

## Upgrade from the pre-0.12.0 shape

The previous shape used `ubuntu:noble` with an apt-install in the
container's cmd. It failed silently on any lab without internet in
the container's netns (which is most of them). Old rdma-host nodes
will keep working (they're `ubuntu:noble` containers that never got
their tools installed); replace them by dragging a fresh `RDMA
host` chip from the palette — the palette now points at
`labtris/rdma-host:latest`.
