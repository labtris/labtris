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
# cc_sim is the congestion-control test app: N senders onto one receiver,
# with link speed, RTT, queue size and drop/ECN/trim thresholds set as
# parameters rather than inherited from whatever host you happen to be
# on. It measures per-sender throughput to show utilisation and fairness,
# and generates no network traffic. This is the build target that gives
# performance numbers meaning anything outside this machine.
RUN make && make xdp && make strict-core \
 && make -C crypto/test && make -C tests/util
# cc_sim is built separately with INCS overridden. Upstream's rule for
# its objects passes only $(INCS) — not $(LF_LOCAL_HDRS) — while
# cc/uet_cc_st.c includes ofi_list.h, which lives in
# ./libfabric_headers/include. So `make cc_sim` fails out of the box
# with "ofi_list.h: No such file or directory". Adding the two paths
# upstream already defines is the whole fix; worth sending back.
RUN make cc_sim \
      INCS="-I. -I./util -I./imp_shim -I./nic_shim -I./crypto -I./libfabric_headers -I./libfabric_headers/include"

FROM base AS runtime
RUN apt-get -y install --no-install-recommends \
      libnuma1 libuuid1 libxdp1 libnl-3-200 libnl-route-3-200 \
      iputils-ping net-tools iproute2 \
      `# uet_test.sh calls sudo unconditionally, even as root` \
      sudo \
      `# not upstream's: an operator shelled in wants to see the wire` \
      tcpdump \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build-libfabric /stage /
# Everything lands under /opt/uet, which is also the working directory,
# because scripts/uet_test.sh runs `./uet` from the cwd rather than from
# PATH. Symlinks into /usr/bin keep the short forms working too.
COPY --from=build-uet /src/uet/uet          /opt/uet/uet
COPY --from=build-uet /src/uet/uet_xdp      /opt/uet/uet_xdp
COPY --from=build-uet /src/uet/uet_cc_sim   /opt/uet/uet_cc_sim
COPY --from=build-uet /src/uet/scripts/     /opt/uet/scripts/
COPY --from=build-uet /src/uet/crypto/test/ /opt/uet/crypto-test/
COPY --from=build-uet /src/uet/tests/util/  /opt/uet/tests-util/
RUN for b in uet uet_xdp uet_cc_sim; do ln -sf /opt/uet/$b /usr/bin/$b; done \
 && chmod +x /opt/uet/scripts/* 2>/dev/null || true
# scripts/uet_test.sh walks up from the cwd looking for a directory named
# libfabric* that contains src/.libs, and uses it for LD_LIBRARY_PATH. An
# installed libfabric does not satisfy that, so the script dies with
# "libfabric directory not found" before running a single test. Recreate
# the shape it looks for: from /opt/uet it walks to /opt and finds this.
COPY --from=build-libfabric /src/libfabric/src/.libs/ /opt/libfabric-1.20.1/src/.libs/
# The binaries link the provider as shared objects, and upstream's own
# Dockerfile copies only the executables — so `uet` in that image dies
# with "libuet_fabric.so: cannot open shared object file". Their README
# works around it with LD_LIBRARY_PATH pointing into the build tree,
# which a shipped container does not have. Install the libraries
# properly instead.
COPY --from=build-uet /src/uet/libuet_fabric.so /usr/lib/
COPY --from=build-uet /src/uet/libuet_verbs.so  /usr/lib/
COPY --from=build-uet /src/uet/libxdpuet.so     /usr/lib/
# The eBPF kernel object the XDP shim loads at runtime. uet_xdp is built
# with -DXDP_PROG=uet_xdp_kern.o, an unqualified name, so it is looked up
# relative to the working directory: keep the CMD's WORKDIR next to it.
COPY --from=build-uet /src/uet/uet_xdp_kern.o   /opt/uet/
# The official Wireshark dissector. UET has no built-in tshark support,
# so ship it here and tell people where it is — labtris-network-skills'
# packet_analysis/uet.yaml is written against these field names.
COPY --from=build-uet /src/uet/dissector/uet.lua /usr/share/uet/uet.lua

RUN ldconfig && fi_info > /dev/null && /opt/uet/uet 2>&1 | head -1 || true

WORKDIR /opt/uet
CMD ["/bin/bash", "-lc", "echo 'uet-ref — the UEC reference provider. Everything upstream ships is here:'; echo '  pair      UET_IFNAME=eth0 UET_PDS=pds uet server|client <test> <peer>'; echo '  tests     rma sync_rma untag tag tag_any_src unexp_untag unexp_tag'; echo '            defer_send defer_tag defer_tag_any_src atomic sync_atomic uud'; echo '  sweep     scripts/uet_test.sh client|server eth0 <peer> <test> <pds> [shim]'; echo '            pds: sng pds pds_direct pds_cluster pds_cluster_ssi pds_server_ssi'; echo '                 pds_cluster_key_rot pds_cluster_1rtt pds_cluster_churn'; echo '                 pds_cluster_interop   |   shim: rawsock xdp'; echo '  cc        uet_cc_sim <n-senders>        # incast fairness, no traffic'; echo '  crypto    crypto-test/crypto_test      # AES, GCM, CMAC, KDF vs NIST vectors'; echo '  bitmap    tests-util/bitmap_test'; echo '  dissector /usr/share/uet/uet.lua'; echo '  NOTE      UET_PDS defaults to sng. RUDI and UUD need UET_PDS=pds.'; exec sleep infinity"]
