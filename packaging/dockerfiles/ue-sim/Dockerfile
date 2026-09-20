# UE-Sim as a first-class Labtris node.
#
# Builds Kaima Lab's UE-Sim (github.com/kaima2022/UE-Sim) — an ns-3
# 3.44 fork implementing the UEC/UET SES + PDS + PDC sublayers. Ships
# an image whose default state is "cd /ue-sim && ./ns3 --help" ready;
# `labtris node exec <id> -- ./ns3 run soft-ue` fires the throughput
# stress test end-to-end.
#
# Pinning to a specific commit of UE-Sim so a labtris upgrade does not
# silently pull in a moving upstream. Bump COMMIT to update.

FROM ubuntu:22.04

ARG UE_SIM_COMMIT=main
ENV DEBIAN_FRONTEND=noninteractive

# UE-Sim needs GCC 10+, CMake 3.13+, ns-3 build deps. Ubuntu 22.04's
# defaults cover it. Add curl + git for the clone and iproute2/tcpdump
# so an operator shelled in can debug traffic on the container's veth.
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      build-essential cmake git curl ca-certificates \
      python3 python3-pip \
      iproute2 iputils-ping tcpdump \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch ${UE_SIM_COMMIT} \
      https://github.com/kaima2022/UE-Sim.git ue-sim \
 || git clone --depth 1 https://github.com/kaima2022/UE-Sim.git ue-sim

WORKDIR /opt/ue-sim
# Configure + build. ns-3's own script (./ns3) handles CMake; use the
# release profile for a leaner container (~40% smaller than debug).
RUN ./ns3 configure --build-profile=release --enable-examples \
 && ./ns3 build

# Symlink for convenience: `labtris node exec <id> -- ns3 …` works
# without cd'ing.
RUN ln -s /opt/ue-sim/ns3 /usr/local/bin/ns3

# Advertise every scratch program on shell login for discoverability.
RUN printf '%s\n' \
    '# UE-Sim node — Kaima Lab UE-Sim on ns-3 3.44' \
    '# Try:' \
    '#   ns3 run soft-ue-e2e-concepts   # end-to-end walkthrough' \
    '#   ns3 run soft-ue                # throughput stress test' \
    '#   ns3 run first-soft-ue          # smallest example' \
    '# Full list: ls /opt/ue-sim/scratch /opt/ue-sim/examples' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

WORKDIR /opt/ue-sim

# PID 1 as bash-supervised tail so `frrinit.sh restart`-style ops on
# other nodes have a companion — the container stays up regardless of
# whether an ns-3 sim is running. Same shape the FRR profile uses.
CMD ["/bin/bash", "-c", "cat /etc/motd; exec tail -f /dev/null"]
