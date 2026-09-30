# uet-htsim — UEC's packet-level simulator for Ultra Ethernet congestion
# control.
#
# The counterpart to the uet-ref node, not a replacement for it. uet-ref
# puts real UET frames on a real veth: every sequence number, NACK code
# and trimming event is a genuine protocol fact, but the link is your
# host's memory bandwidth, so a throughput figure from it describes the
# machine rather than the fabric. htsim is the mirror image — not one
# real packet exists, but the link is 800 Gbps because you said so, and
# incast behaviour and fairness at that rate mean something.
#
#   uet-ref   is the protocol correct?
#   uet-htsim is the algorithm good?
#
# Nine congestion-control algorithms to compare against, which is the
# reason this replaces the ue-sim node: that packaged an ns-3 fork under
# GPL-2.0 with one algorithm and no upstream activity since April.
# This is the consortium's own, BSD-2-Clause, and it is what UEC uses to
# evaluate NSCC before standardising it.
#
#   NSCC (default) · DCTCP · DCQCN · NDP · EQDS · RoCE · PFC · Swift · HPCC
#
# Upstream is github.com/ultraethernet/uet-htsim, itself a fork of
# Broadcom/csg-htsim. Pinned by commit so a rebuild does not silently
# change what an experiment measures — bump UET_HTSIM_COMMIT to move.

FROM debian:bookworm-slim AS build
ENV DEBIAN_FRONTEND=noninteractive
ARG UET_HTSIM_COMMIT=65ea315

RUN apt-get -y update \
 && apt-get -y install --no-install-recommends \
      build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/ultraethernet/uet-htsim.git . \
 && git checkout --detach "${UET_HTSIM_COMMIT}"

# The project builds from sim/, not the repo root.
WORKDIR /src/htsim/sim
RUN cmake -S . -B build && cmake --build build --parallel "$(nproc)"

FROM debian:bookworm-slim AS runtime
ENV DEBIAN_FRONTEND=noninteractive
# python3 and gnuplot are not optional extras: the datacenter examples
# generate their connection matrices and plot their results with them,
# and an image that can run a simulation but not its own examples is a
# poor first contact.
RUN apt-get -y update \
 && apt-get -y install --no-install-recommends \
      python3 python3-matplotlib python3-pandas gnuplot-nox bash \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/htsim /opt/htsim
ENV PATH="/opt/htsim/sim/build:/opt/htsim/sim/datacenter:${PATH}"
WORKDIR /opt/htsim/sim/datacenter

CMD ["/bin/bash", "-lc", "echo 'uet-htsim — packet-level UET congestion-control simulator.'; echo; echo '  examples: /opt/htsim/sim/datacenter'; echo '  binaries: /opt/htsim/sim/build'; echo '  algorithms: NSCC (default) DCTCP DCQCN NDP EQDS RoCE PFC Swift HPCC'; echo '  default topology: 3-tier fat tree, 12us RTT'; echo; echo 'Nothing here touches the wire — it models packets. Use the uet-ref'; echo 'node when you need real frames on a real veth.'; exec sleep infinity"]
