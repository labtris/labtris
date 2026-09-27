# uet-ref — the UEC reference Ultra Ethernet Transport provider.
#
# Builds github.com/ultraethernet/uet-ref-prov, the Ultra Ethernet
# Consortium's own reference implementation of UET (MIT). Unlike our
# ue-stack PoC this is the real thing: SES with tagged and untagged
# messages and RMA read/write, PDS in two implementations, TSS with
# AES-GCM, all four delivery modes (RUD, ROD, RUDI, UUD), and a partial
# implementation of UET Network Signal Congestion Control.
#
# It needs no special hardware — a raw Ethernet socket or XDP on an
# ordinary veth is enough, which is exactly what a Labtris segment
# provides. UET is its own transport on protocol number 253, so it does
# not collide with anything else on the wire.
#
# Two binaries land in PATH:
#   uet      — rawsock NIC shim only
#   uet_xdp  — rawsock + XDP, XDP by default (UET_NIC_SHIM overrides)
#
# Run a pair on one segment:
#   node A:  UET_IFNAME=eth0 uet server <A-ip>
#   node B:  UET_IFNAME=eth0 uet client <A-ip>
#
# Their stated gaps, worth knowing before you measure anything:
# multi-path packet delivery is not fully supported, there is no key
# exchange (one static Secure Domain), and the XDP path still copies
# to and from the umem buffers.
#
# Staged after upstream's own Dockerfile, with the source cloned at a
# pinned commit instead of copied from the build context, so this file
# builds standalone. Bump COMMIT to update.

FROM debian:bookworm-slim AS base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -y update && apt-get -y dist-upgrade

FROM base AS build-env
RUN apt-get -y install --no-install-recommends \
      build-essential gcc-multilib autoconf automake libtool \
      libnl-3-dev libnl-route-3-dev libnuma-dev libudev-dev libxdp-dev \
      clang uuid-dev pkg-config python-is-python3 \
      git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# libfabric is the API UEC chose for UET; the provider is a libfabric
# provider, so it builds against the real thing rather than a stub.
FROM build-env AS build-libfabric
ARG LIBFABRIC_VERSION=1.20.1
WORKDIR /src
RUN curl -fsSL -o libfabric.tar.bz2 \
      "https://github.com/ofiwg/libfabric/releases/download/v${LIBFABRIC_VERSION}/libfabric-${LIBFABRIC_VERSION}.tar.bz2" \
 && tar --no-same-owner -jxf libfabric.tar.bz2 \
 && ln -s "libfabric-${LIBFABRIC_VERSION}" libfabric
WORKDIR /src/libfabric
RUN autoreconf && ./configure && make -j "$(nproc)" && make install DESTDIR=/stage

FROM build-libfabric AS build-uet
ARG UET_REF_COMMIT=4b48a991da67542bcec4a6fd012528b528645612
WORKDIR /src
RUN git clone https://github.com/ultraethernet/uet-ref-prov.git uet \
 && git -C uet checkout --detach "${UET_REF_COMMIT}"
WORKDIR /src/uet
RUN make && make xdp

FROM base AS runtime
RUN apt-get -y install --no-install-recommends \
      libnuma1 libuuid1 libxdp1 libnl-3-200 libnl-route-3-200 \
      iputils-ping net-tools iproute2 \
      `# not upstream's: an operator shelled in wants to see the wire` \
      tcpdump \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build-libfabric /stage /
RUN ldconfig && fi_info > /dev/null
COPY --from=build-uet /src/uet/uet      /usr/bin/uet
COPY --from=build-uet /src/uet/uet_xdp  /usr/bin/uet_xdp
# The official Wireshark dissector. UET has no built-in tshark support,
# so ship it here and tell people where it is — labtris-network-skills'
# packet_analysis/uet.yaml is written against these field names.
COPY --from=build-uet /src/uet/dissector/uet.lua /usr/share/uet/uet.lua

CMD ["/bin/bash", "-lc", "echo 'uet-ref node. UET_IFNAME=<iface> uet server|client <ip>. Dissector: /usr/share/uet/uet.lua'; exec sleep infinity"]
