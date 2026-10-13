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
# TREX_URL is the upstream download (CI) or the name of a copy of the
# tarball in the build context (the Hetzner build: the release server
# refuses that network). Either way its SHA-256 must match TREX_SHA256.
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

# The download server (trex-tgn.cisco.com) does not send its intermediate
# certificate, so anything without AIA chasing — BuildKit's ADD, curl on
# Debian — fails "unable to verify the first certificate". The transport
# is not what makes this safe; the hash is: the tarball must match
# TREX_SHA256 or the build stops. A local file (the Hetzner build) is
# checked the same way.
ARG TREX_SHA256=d2ac73627906c4ce01127c1c6954464fab373463966ea9592509446e74972b5f
COPY . /tmp/ctx/
RUN apt-get update -qq && apt-get install -y --no-install-recommends curl ca-certificates >/dev/null \
 && if [ -f "/tmp/ctx/${TREX_URL}" ]; then cp "/tmp/ctx/${TREX_URL}" /tmp/trex.tar.gz; \
    else curl -fsSL --insecure --retry 3 -o /tmp/trex.tar.gz "${TREX_URL}"; fi \
 && echo "${TREX_SHA256}  /tmp/trex.tar.gz" | sha256sum -c - \
 && rm -rf /tmp/ctx \
 && mkdir -p /opt/trex \
 && tar -xzf /tmp/trex.tar.gz -C /opt/trex --strip-components=1 \
 && rm /tmp/trex.tar.gz \
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
COPY trex-up.sh /usr/local/bin/trex-up
RUN chmod +x /usr/local/bin/run-trex /usr/local/bin/trex-up

RUN printf '%s\n' \
    '# trex — Cisco TRex, stateless, software mode (af_packet on eth0/eth1).' \
    '# run-trex               the server, foreground, on eth0+eth1 (edit /etc/trex_cfg.yaml for other ports)' \
    '# trex-up                the same in the background, returns when both ports are bound' \
    '# python3 /opt/trex/labtris/udp-throughput.py --rate 500kpps --duration 20' \
    '#                        UDP 64B both directions, prints tx/rx pps, Gbps, drops' \
    '# /opt/trex/trex-console  the interactive console (server must be running)' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

WORKDIR /opt/trex
CMD ["/bin/bash", "-c", "cat /etc/motd; exec tail -f /dev/null"]
