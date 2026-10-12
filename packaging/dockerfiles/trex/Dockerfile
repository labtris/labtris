# trex — Cisco TRex traffic generator, stateless, software mode on af_packet.
#
# TRex is a DPDK application. On real hardware it binds a NIC to vfio and
# runs it at line rate; in a Labtris container node there is no PCI NIC,
# only a veth, so it runs with `--software` and DPDK's af_packet PMD. That
# path is kernel-bound: expect single-digit Mpps per core, not the 100 Gbps
# the tool is famous for. What it keeps is the rest of TRex — the
# stateless Python API, per-stream statistics, latency streams, pcap
# replay — so you can measure a DUT (VPP, FRR, a vendor NOS) in the lab
# and say exactly what rate it forwarded and what it dropped, which is the
# question a lab can answer. Absolute throughput of a software NIC is not.
#
# TREX_URL may point at a local file in the build context (the Hetzner
# build does this: the release server rejects that network) or at
# trex-tgn.cisco.com, the upstream download (CI).
#
# Upstream: github.com/cisco-system-traffic-generator/trex-core, Apache-2.0.

# Stage 1 unpacks and trims. The release tarball is 280 MB and unpacks to
# 1.2 GB, most of it debug and profiled binaries, a Mellanox PMD in four
# flavours, the ASTF/BP simulators and the EMU service — none of which a
# software-mode generator on a veth can use. Doing this in its own stage
# keeps the full tree out of the final image's layers (an ADD layer stays
# in the image however much a later RUN deletes).
FROM ubuntu:22.04 AS unpack

ARG TREX_VERSION=v3.08
ARG TREX_URL=https://trex-tgn.cisco.com/trex/release/${TREX_VERSION}.tar.gz

# ADD extracts a local tarball on its own but downloads a URL as a file,
# so the next step takes whichever shape it finds.
ADD ${TREX_URL} /tmp/trex-src/
RUN mkdir -p /opt/trex \
 && if ls /tmp/trex-src/*.tar.gz >/dev/null 2>&1; then \
      tar -xzf /tmp/trex-src/*.tar.gz -C /opt/trex --strip-components=1; \
    else \
      mv /tmp/trex-src/v*/* /opt/trex/; \
    fi \
 && cd /opt/trex \
 && rm -rf *-debug* _*-o *-o so/x86_64/*-debug* so/x86_64/*-o.so so/*-debug* so/*-o.so \
           so/_libmlx5-64.so so/ppc64le \
           bp-sim-64 astf-sim astf-sim-utl stl-sim avl exp cap2 \
           emu trex_emu trex-emu trex_client_*.tar.gz bird x710_ddp ko \
           master_daemon.py \
           run_regression run_functional_tests doc_process.py

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      python3 python3-distutils \
      iproute2 iputils-ping pciutils netbase \
      libnuma1 libpcap0.8 zlib1g libssl3 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=unpack /opt/trex /opt/trex
RUN ln -s /opt/trex/t-rex-64 /usr/local/bin/t-rex-64

COPY trex_cfg.yaml /etc/trex_cfg.yaml
COPY udp-throughput.py /opt/trex/labtris/udp-throughput.py
COPY run-trex.sh /usr/local/bin/run-trex
RUN chmod +x /usr/local/bin/run-trex

RUN printf '%s\n' \
    '# trex — Cisco TRex, stateless, software mode (af_packet on eth0/eth1).' \
    '# run-trex               starts the server on eth0+eth1 (edit /etc/trex_cfg.yaml for other ports)' \
    '# python3 /opt/trex/labtris/udp-throughput.py --rate 500kpps --duration 20' \
    '#                        UDP 64B both directions, prints tx/rx pps, Gbps, drops' \
    '# /opt/trex/trex-console  the interactive console (server must be running)' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

WORKDIR /opt/trex
CMD ["/bin/bash", "-c", "cat /etc/motd; exec tail -f /dev/null"]
