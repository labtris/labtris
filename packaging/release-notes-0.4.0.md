Labtris 0.4.0 — the first public release.

Self-hosted network emulation on real Linux networking: veth pairs, bridges,
netlink and nftables, with Docker containers and QEMU virtual machines as
nodes, wired on a canvas in the browser.

## Install

**From the ISO.** Download every `labtris-0.4.0-amd64.iso.part-*` file and join
them — GitHub refuses release assets of 2 GiB or more, and this image is 3.7 GB
because every package it installs is on the disc:

```
cat labtris-0.4.0-amd64.iso.part-* > labtris-0.4.0-amd64.iso
sha256sum -c labtris-0.4.0-amd64.sha256      # macOS: shasum -a 256 -c
sudo dd if=labtris-0.4.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The checksum is of the finished image, so it verifies the join as well as the
download. Do not skip it — a truncated part boots and then fails partway
through the install.

**On an existing Ubuntu 24.04 host:**

```
curl -fsSL https://labtris.com/install | sudo bash
```

## What is in it

**Nodes.** Docker containers start in about a second; QEMU guests take as long
as that OS takes to boot, get real savevm/loadvm snapshots, and run under KVM
where the host has it. Save a configured appliance as a reusable template —
the disk is flattened into a new image rather than committed back over the one
every other node shares.

**Networking.** Point-to-point links, internal bridges, NAT segments with DHCP
and static reservations, cloud networks onto a host NIC, and VXLAN segments
that stretch a topology across several hosts. Bridges can be made VLAN-aware —
802.1Q or QinQ, with real access and trunk ports. Links take netem delay,
jitter, loss, reordering and rate limits, per direction.

**Consoles.** Container shells over a real PTY, QEMU serial from the first byte
of boot, VNC and RDP through Guacamole. Terminals dock, split, or pop out.

**Seeing what happens.** Packet capture on any link with a BPF filter,
Wireshark itself streamed from the server, live guest addressing on the canvas,
DHCP leases, and NAT sessions including what the outside sees each flow as.

**API and assistant.** Everything the interface does goes over the same REST
API. An MCP server exposes it to any Model Context Protocol client, and the
built-in assistant runs in your browser with your own key — the server never
sees it, and its tools run as you.

## Verified

The image was installed on a VM with **no network interface at all**
(`-nic none`), the only honest test of an offline installer: 413 packages and
42 wheels from the disc, zero apt failures, 16 migrations, every service
active, interface answering 200.

The `curl | bash` path was then tested on a clean Ubuntu 24.04 and produces the
same result — same services, same schema, same interface.

## Bring your own images

Labtris redistributes no vendor software. Docker images come from whichever
registry you point at; QEMU images are yours to obtain. The GNS3 registry's
appliance definitions import natively, which fills in the RAM, NIC model, disk
bus and console type each one needs.

## Known limits

- **Labtris does not configure your guests.** It builds the topology and starts
  them; addressing and configuration inside a node are yours.
- **Accounts are local** — no LDAP, AD or RADIUS.
- **No per-user quotas and no audit log.**
- **Vendor container kinds** (SR Linux, cEOS, XRd) are not first-class yet. A
  containerlab topology imports its shape and wiring; those nodes arrive as
  labelled placeholders.
- **x86-64 only.**
- **Do not expose the port to the internet.** It reaches the Docker socket and a
  root daemon. Create the first account before the machine goes anywhere — until
  one exists, the API is open.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
