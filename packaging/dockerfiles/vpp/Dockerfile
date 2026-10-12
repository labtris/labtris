# vpp — FD.io VPP as a lab router, forwarding on af_packet host-interfaces.
#
# VPP is a userspace data plane. On hardware it owns a NIC through DPDK;
# in a Labtris container node its ports are veths, so the DPDK plugin is
# off and each lab link is attached as `create host-interface name ethN`
# (the af_packet driver, mmap'd TPACKET rings, polled by VPP workers).
# That is the same VPP graph — ip4-lookup, ACLs, NAT, VXLAN, SRv6 — with
# the kernel in the packet path at both edges. What you measure is VPP's
# behaviour at the rate a veth carries, which is the question a lab can
# answer; line rate is not.
#
# No hugepages: `buffers { page-size 4K }` lets VPP run on a host that has
# none reserved, at a cost in buffer-pool locality nobody will notice at
# veth rates. Two workers by default; TRex and the kernel want cores too.
#
# Upstream: fd.io VPP, Apache-2.0. Image: ligato/vpp-base (PANTHEON.tech).

ARG VPP_VERSION=26.06-release
FROM ligato/vpp-base:${VPP_VERSION}

COPY startup.conf /etc/vpp/startup.conf
COPY vpp-router.sh /usr/local/bin/vpp-router
RUN chmod +x /usr/local/bin/vpp-router \
 && apt-get update -qq \
 && apt-get install -y --no-install-recommends iproute2 iputils-ping \
 && rm -rf /var/lib/apt/lists/* \
 && printf '%s\n' \
    '# vpp — FD.io VPP 26.06, no DPDK, lab links as af_packet host-interfaces.' \
    '# vppctl show interface            the ports VPP owns' \
    '# vpp-router                       eth0/eth1 as 10.0.1.1 / 10.0.2.1, routes 16/8 and 48/8 (the TRex demo)' \
    '# vppctl create host-interface name eth2 ; vppctl set interface state host-eth2 up' \
    '# vppctl show runtime              per-node vectors/call — where the cycles go' \
    > /etc/motd \
 && printf '\ncat /etc/motd\n' >> /root/.bashrc

# VPP in the foreground is the container; vppctl talks to it over
# /run/vpp/cli.sock. A failed start shows in `docker logs`.
CMD ["/usr/bin/vpp", "-c", "/etc/vpp/startup.conf"]
