# `uet-ref` — the UEC reference UET provider

The Ultra Ethernet Consortium's own reference implementation of Ultra
Ethernet Transport, packaged as a Labtris node kind.

Upstream: [github.com/ultraethernet/uet-ref-prov](https://github.com/ultraethernet/uet-ref-prov)
(MIT). Spec: [UE Specification 1.0.2](https://ultraethernet.org/wp-content/uploads/sites/20/2026/01/UE-Specification-1.0.2-1.pdf)
(public, CC BY-ND 4.0).

## Why this and not `ue-stack`

`ue-stack` in this repo is a proof of concept that puts a UET-shaped
header on the wire. It has no congestion control, no multipath, and its
reliable context is not reachable from any running configuration. Put it
on a fabric and there is nothing to measure.

This image has SES (tagged and untagged messages, RMA read and write),
PDS in two implementations, TSS with AES-GCM, all four delivery modes
(RUD, ROD, RUDI, UUD), and a partial implementation of UET Network
Signal Congestion Control. It is the one to use for anything where the
answer is a number.

## Build

    docker build -t labtris/uet-ref:latest packaging/dockerfiles/uet-ref/

Builds libfabric 1.20.1 from source, then the provider at a pinned
commit. Bump `UET_REF_COMMIT` to move upstream. Expect a long first
build — libfabric is most of it.

## Run a pair

Drop two `uet-ref` nodes on one segment, give them addresses, then:

    # node A
    UET_IFNAME=eth0 uet server 10.0.0.1
    # node B
    UET_IFNAME=eth0 uet client 10.0.0.1

`uet` uses a raw Ethernet socket (`NET_RAW`, already in the base caps).
`uet_xdp` defaults to the XDP shim and loads an eBPF program, which is
why the profile adds `BPF` and `SYS_ADMIN`; `UET_NIC_SHIM=rawsock`
forces the socket path.

UET is **its own transport on protocol number 253**, not UDP. It will
not collide with RoCEv2 on 4791 the way `ue-stack` does.

## Reading it on the wire

tshark has no built-in UET dissector. Upstream's Lua one is baked in at
`/usr/share/uet/uet.lua`:

    ln -sf /usr/share/uet/uet.lua ~/.config/wireshark/plugins/uet.lua

`labtris-network-skills/packet_analysis/uet.yaml` is written against its
field names — `uet.pds.type`, `uet.entropy.entropy`, `uet.pds.nack_code`
and the rest.

## Upstream's stated gaps

Worth knowing before you draw conclusions from a run:

- **Multi-path packet delivery is not fully supported.** This is the
  interesting one — spraying across paths is what UET is for, and the
  `uet.entropy.entropy` field is where you would see it. Developing it
  needs a real multi-path fabric, which `labtris lab generate` builds.
- No key exchange; one static Secure Domain with fixed keys.
- The XDP path still copies to and from the umem buffers.
- Upstream prioritises clarity and feature coverage over performance,
  so absolute throughput is not the number to quote.
