# rdma-host — Ubuntu with rdma-core + perftest baked in at build time.
#
# The plain first-boot-apt shape (0.9.0) failed on any lab whose bridge
# had no NAT to the internet — `apt-get update` returned 'Temporary
# failure resolving archive.ubuntu.com' and the container came up as a
# bare Ubuntu with none of the RDMA tools. Every user hit this.
#
# Pre-building with the packages already installed removes the runtime
# dependency on internet. Container starts in ~1 s; `ib_send_bw`,
# `ib_write_bw`, `rdma`, `ibv_devinfo` all present on PATH; the only
# runtime action is `rdma link add rxe0` which needs the host's
# rdma_rxe module loaded once (documented in the notes).

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      rdma-core perftest \
      iproute2 iputils-ping tcpdump \
      libibverbs1 librdmacm1 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# MOTD explaining what's here + how to try it. Written on shell login
# via .bashrc rather than /etc/motd so `docker exec -it` sees it.
RUN printf '%s\n' \
    '# rdma-host — Ubuntu + rdma-core + perftest, soft-RoCE ready.' \
    '# Try:' \
    '#   rdma link add rxe0 type rxe netdev eth0      # requires host modprobe rdma_rxe' \
    '#   rdma link show                               # confirm rxe0 up' \
    '#   ib_send_bw -d rxe0                           # server' \
    '#   ib_send_bw -d rxe0 <peer-ip>                 # client' \
    '#   ibv_devinfo                                  # local RDMA devices' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

# Runtime is small: try adding the rxe device on eth0, hand PID 1 to
# bash-tail so the container stays alive regardless of whether the rxe
# attach succeeded (host may not have loaded rdma_rxe yet).
CMD ["/bin/bash", "-c", \
     "rdma link add rxe0 type rxe netdev eth0 2>/dev/null || true; " \
     "cat /etc/motd; " \
     "exec tail -f /dev/null"]
