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
      rdma-core ibverbs-utils perftest \
      iproute2 iputils-ping tcpdump \
      libibverbs1 librdmacm1 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# MOTD explaining what's here + how to try it. Written on shell login
# via .bashrc rather than /etc/motd so `docker exec -it` sees it.
RUN printf '%s\n' \
    '# rdma-host — Ubuntu + rdma-core + perftest, soft-RoCE ready.' \
    '# Try:' \
    '# Host needs: modprobe rdma_rxe' \
    '# RDMA devices are global unless the host is in netns-exclusive mode,' \
    '# so give each node its OWN device name rather than all using rxe0:' \
    '#   rdma link add rxe_\$(hostname) type rxe netdev eth1' \
    '#   rdma link show                               # confirm it is ACTIVE' \
    '#   ibv_devinfo                                  # what verbs can see' \
    '#   ib_send_bw -d rxe_<self>                     # server' \
    '#   ib_send_bw -d rxe_<self> <peer-ip>           # client' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

# Runtime is small: try adding the rxe device on eth0, hand PID 1 to
# bash-tail so the container stays alive regardless of whether the rxe
# attach succeeded (host may not have loaded rdma_rxe yet).
# One string, not several. Docker's exec-form CMD is JSON, and JSON has
# no adjacent-string concatenation — the previous version split the shell
# command across continued lines, which parsed as invalid JSON, so Docker
# silently fell back to shell form and tried to run the literal text
# `[/bin/bash,`. The container exited instantly with
# "/bin/sh: 1: [/bin/bash,: not found" and every rdma-host node failed to
# start. Keep this on one line.
# No automatic `rdma link add` here. It used to try rxe0 on eth0, which is
# wrong twice: eth0 is the management interface, not the lab one, and with
# the host in the default netns-shared mode the name rxe0 is global — so
# the first node to start claims it and every other node's add fails with
# a collision. Creating the device is one line the user runs, named after
# the node, once they know which interface they want it on.
CMD ["/bin/bash", "-c", "cat /etc/motd; exec tail -f /dev/null"]
