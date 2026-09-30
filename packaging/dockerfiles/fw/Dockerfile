# fw — a firewall node with the tools already in it.
#
# Lab links are isolated: a node on one has no route out, which is the
# right default and means `apk add nftables` inside a running lab fails
# with "temporary error" and leaves you with a firewall that cannot
# filter. Same reasoning as rdma-host baking in perftest — a lab that
# needs the internet to become useful is not reproducible.
#
# Shaped for the inline case: a DPU or an IoT gateway sitting between
# host and network, two interfaces, no IP on the datapath, bridging and
# inspecting. That is why ebtables and the nftables *bridge* family
# matter more here than plain iptables.
#
#   ip link add br0 type bridge
#   ip link set eth0 master br0 && ip link set eth1 master br0
#   ip link set br0 up && ip link set eth0 up && ip link set eth1 up
#   nft add table bridge filter
#   nft add chain bridge filter forward '{ type filter hook forward priority 0; }'
#   nft add rule bridge filter forward tcp dport 23 drop
#
# Filter with the bridge family rather than br_netfilter: the latter
# needs net.bridge.bridge-nf-call-iptables set on the *host*, so a lab
# built on it filters on one machine and silently forwards everything on
# another.

FROM alpine:3.20

RUN apk add --no-cache \
      nftables iptables ip6tables ebtables \
      conntrack-tools \
      iproute2 bridge-utils \
      tcpdump \
      socat \
      bind-tools \
 && rm -rf /var/cache/apk/*

CMD ["/bin/sh", "-lc", "echo 'fw node — nftables, iptables, ebtables, conntrack, tcpdump.'; echo; echo '  inline shape: two interfaces, bridge them, no IP on the datapath'; echo '  filter with: nft add table bridge filter'; echo '  NOT br_netfilter — that depends on a host sysctl and fails silently'; exec sleep infinity"]
