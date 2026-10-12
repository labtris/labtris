#!/bin/sh
# The TRex demo's DUT config: eth0 and eth1 become VPP ports, each with
# an address on its link, and the two /8s TRex sends from are routed out
# the opposite port. Static ARP for the TRex side so no packet waits on a
# resolution in the middle of a run (TRex answers ARP too; this just makes
# the first second count).
#
#   eth0  10.0.1.1/24   peer 10.0.1.2 (TRex port 0)   16.0.0.0/8 lives here
#   eth1  10.0.2.1/24   peer 10.0.2.2 (TRex port 1)   48.0.0.0/8 lives here
set -e
for i in 1 2 3 4 5 6 7 8 9 10; do vppctl show version >/dev/null 2>&1 && break; sleep 1; done
mac() { cat /sys/class/net/$1/address; }
vppctl create host-interface name eth0
vppctl create host-interface name eth1
vppctl set interface state host-eth0 up
vppctl set interface state host-eth1 up
# Polling, not interrupt: a worker that sleeps between bursts leaves the
# af_packet ring to overflow at a few hundred kpps; polling costs a core
# each and forwards 3x more before the first drop (measured, see README).
vppctl set interface rx-mode host-eth0 polling
vppctl set interface rx-mode host-eth1 polling
vppctl set interface ip address host-eth0 10.0.1.1/24
vppctl set interface ip address host-eth1 10.0.2.1/24
vppctl ip route add 16.0.0.0/8 via 10.0.1.2 host-eth0
vppctl ip route add 48.0.0.0/8 via 10.0.2.2 host-eth1
vppctl show interface address
