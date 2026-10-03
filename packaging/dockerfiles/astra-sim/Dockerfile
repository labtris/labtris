# astra-sim — the scale-up tier, modelled rather than emulated.
#
# /ai-fabrics names four networks and calls scale-up a deliberate gap: it is
# memory-semantic over its own PHY, and UALink has no open implementation to
# package, so a node claiming to speak it would be a guess with no reference
# to check against. That is still true — the UALink 1.0 spec is public and
# silicon is being built, but there is nothing to run.
#
# What the page said was worth building instead is a behavioural model of a
# scale-up domain: rack-scale topology, switched against fully connected,
# what a link failure costs, and the question that actually matters
# downstream — when does traffic leave the rack and land on the scale-out
# fabric. ASTRA-sim is exactly that, and it already exists.
#
# It models a hierarchy of dimensions, the first being the scale-up network
# (what NVLink or UALink occupies in a real rack) and the ones above it the
# scale-out fabric. You give it a collective and a topology; it tells you how
# long the collective takes and where the time went. Nothing touches a wire.
#
#   uet-ref    is the protocol correct?
#   uet-htsim  is the algorithm good?
#   astra-sim  how long does this collective take on this rack?
#
# Three different questions. A lab is only useful if it is clear which one it
# is answering, so this node answers the third and says so.
#
# LICENCE — the reason only one backend is built.
#
# ASTRA-sim is MIT, and so are the pieces this image needs: the analytical
# network backend (MIT), the analytical memory backend (MIT), Chakra
# (Apache-2.0), fmt (MIT) and spdlog (MIT). Its ns-3 backend is a GPL-2.0
# fork and is NOT initialised here. That is the same call that replaced the
# old ue-sim node, which packaged a GPL-2.0 ns-3 fork, with uet-htsim.
# Submodules are initialised by name rather than with --recursive precisely
# so that adding a backend upstream cannot quietly pull copyleft into this
# image.
#
# Upstream: github.com/astra-sim/astra-sim. Pinned by commit so a rebuild
# does not silently change what an experiment measures — bump
# ASTRA_SIM_COMMIT to move.

FROM debian:bookworm-slim AS build
ENV DEBIAN_FRONTEND=noninteractive
ARG ASTRA_SIM_COMMIT=master

RUN apt-get -y update \
 && apt-get -y install --no-install-recommends \
      build-essential cmake git ca-certificates ninja-build \
      libboost-dev libboost-program-options-dev protobuf-compiler \
      libprotobuf-dev python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/astra-sim/astra-sim.git . \
 && git checkout --detach "${ASTRA_SIM_COMMIT}"

# Named, not --recursive. extern/network_backend/ns-3 is GPL-2.0 and stays
# uninitialised; nothing in this image links against it.
RUN git submodule update --init --depth 1 \
      extern/graph_frontend/chakra \
      extern/network_backend/analytical \
      extern/remote_memory_backend/analytical \
      extern/helper/fmt \
      extern/helper/spdlog \
 && test ! -e extern/network_backend/ns-3/CMakeLists.txt \
    || (echo "ns-3 backend was initialised; refusing to build" >&2 && exit 1)

# build.sh compiles Chakra's protobuf schema first, then cmake-builds into
# build/astra_analytical/build — which is what the runtime stage copies.
RUN ./build/astra_analytical/build.sh

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -y update \
 && apt-get -y install --no-install-recommends \
      libprotobuf32 libboost-program-options1.74.0 python3 iproute2 \
 && rm -rf /var/lib/apt/lists/*

# Upstream's own layout, so upstream's run scripts and docs apply verbatim:
# they resolve PROJECT_DIR from their own location and expect the binary at
# build/astra_analytical/build/bin.
COPY --from=build /src/build/astra_analytical/build /opt/astra-sim/build/astra_analytical/build
COPY --from=build /src/examples /opt/astra-sim/examples
COPY --from=build /src/inputs /opt/astra-sim/inputs
COPY --from=build /src/LICENSE /opt/astra-sim/LICENSE

# Those scripts each call build.sh before running. The binary is already
# built and this image has no compiler, so the call becomes a line that says
# so rather than a confusing failure half way through an example.
RUN printf '%s\n' '#!/bin/sh' \
      'echo "[labtris] already built at image build time; skipping compile."' \
    > /opt/astra-sim/build/astra_analytical/build.sh \
 && chmod +x /opt/astra-sim/build/astra_analytical/build.sh

# The ns-3 and htsim run scripts go: neither backend is in this image, and a
# script that cannot work is worse than a missing one — it looks like a bug
# in the node rather than a deliberate exclusion.
RUN rm -rf /opt/astra-sim/examples/run_scripts/ns3 \
           /opt/astra-sim/examples/run_scripts/htsim

WORKDIR /opt/astra-sim
# A simulator node has nothing to listen on; it exists to be exec'd into and
# run. Same shape as uet-htsim.
CMD ["sleep", "infinity"]
