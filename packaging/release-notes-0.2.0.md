Two things you could not do in 0.1.0: keep a configured appliance, and boot a
guest whose kernel cannot drive a virtio disk.

## New since 0.1.0

**Save a configured guest as a reusable image.** Booting a vendor appliance,
accepting its licence and configuring it is an afternoon's work, and it used to
be an afternoon per lab. Stop the node, **Export as template**, and its disk is
flattened into a standalone image that appears in the palette. Drag it out and
you get the machine as you left it.

It copies rather than committing. EVE-NG merges a node's overlay down into the
base image it came from; Labtris will not, because one base file backs every
node using that image across every lab and user — committing would rewrite
disks under other people's running guests. The saved image also carries its own
sizing and device settings, because it has no catalogue entry behind it to
supply them.

**The disk controller is now the one the image asks for.** It was parsed from
the GNS3 registry, stored, and then ignored: every VM got virtio regardless. A
guest with no virtio-blk driver does not boot and does not say so — QEMU
starts, the console sits at a bootloader or stays blank, and it reads as a
broken host. 67 of the 182 QEMU appliances in the GNS3 registry need IDE.

There is a per-node **Disk controller** override for bring-your-own images, and
Labtris now translates between vocabularies: `-drive if=` refuses `sata`,
`nvme` and `usb`, which the GNS3 registry uses freely, so those are mapped to
the nearest bus that boots instead of producing a VM that dies at launch.

**Smaller:** the registry import endpoint no longer accepts an `apply` flag it
silently ignored, and the version the API reports comes from one place instead
of four.

Labtris builds network labs on **real kernel networking** — veth pairs, Linux
bridges, netlink, nftables. Nothing is simulated, which is why what you learn in
a lab transfers. It is server software: it runs on a Linux machine, boots your
containers and virtual machines there, and everyone else reaches it with a
browser.

## Install

**The ISO is attached in parts.** GitHub refuses any release asset of 2 GiB or
more, and this image is 3.5 GB because every package it installs is on the
disc. Download every `labtris-0.2.0-amd64.iso.part-*` file, then join them —
order matters, and the `*` glob sorts correctly:

```
cat labtris-0.2.0-amd64.iso.part-* > labtris-0.2.0-amd64.iso
sha256sum -c labtris-0.2.0-amd64.sha256      # macOS: shasum -a 256 -c
```

The checksum is of the finished image, so it verifies the join as well as the
download. Do not skip it — a truncated part produces an ISO that boots and then
fails partway through the install.

Then write it to a USB stick and boot the machine:

```
sudo dd if=labtris-0.2.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The install is unattended and takes six to ten minutes. Every Debian package,
every Python wheel and the prebuilt interface are **on the disc** — it fetches
nothing. When it finishes, the console prints the address to open.

This image was verified end to end on a VM given **no network interface at
all** (`-nic none`), which is the only honest way to test an offline install —
anything with a route out will quietly use it. On that machine: 387
packages and 42 wheels installed from the disc with zero apt failures,
15 database migrations applied, `labtris-api`, `labtris-netd`, `labtris-ksm`, nginx,
PostgreSQL and Docker all active, the API answering
`{"status":"ok","netd":true,"db":true,"docker":true}` and the interface
returning 200. It also wrote a DHCP configuration for any ethernet it might
later be given, so a machine installed offline works the moment it is plugged
in.

Verify the download first:

```
sha256sum -c labtris-0.2.0-amd64.sha256
```

## What is in it

**Nodes.** Docker containers and QEMU virtual machines. Containers start in a
second; VMs take as long as that OS takes to boot, get real `savevm`/`loadvm`
snapshots, and run under KVM where the host has it.

**Networking.** Point-to-point links, internal bridges, NAT segments with a DHCP
server and static reservations, cloud networks onto a host NIC, and VXLAN
segments that stretch a topology across several hosts. Bridges can be made
VLAN-aware — 802.1Q or QinQ, with real access and trunk ports — so a VLAN lab
teaches VLANs rather than appearing to work by accident. Links take `netem`
delay, jitter, loss, reordering and rate limits, per direction.

**Consoles.** Container shells over a real PTY, so `vi` and `top` render and
Ctrl-C interrupts. QEMU serial consoles from the first byte of boot. VNC and RDP
through Guacamole for graphical guests. Terminals dock, split side by side, or
pop out into their own window.

**Seeing what happens.** Packet capture on any link or segment with a BPF
filter, Wireshark itself streamed from the server, live guest addressing shown
on the canvas, DHCP leases, and live NAT sessions including what the outside
sees each flow as.

**Labs.** Folders, clone, lock, named startup-config sets for resetting a class
to a known state, import from EVE-NG `.unl` and containerlab `.clab.yml`, and a
one-file backup of every lab.

**API and assistant.** Everything the interface does goes over the same REST
API, documented at `/docs`. An MCP server with 21 tools lets a model drive a lab
directly. The built-in assistant runs the conversation **in your browser with
your own key** — Labtris never sees it — and its tools run as you, so it cannot
do anything you could not do by hand.

## Bring your own images

Labtris redistributes no vendor software. Docker images come from whichever
registry you point at; QEMU images are yours to obtain. The GNS3 registry's 228
appliance definitions can be imported to populate the palette with the correct
RAM, NIC model, disk bus and console type for each — 144 of them are obtainable
without a vendor account.

## Documentation

The handbook is attached as a PDF, lives in the repository under
`docs/handbook/`, and ships on the installed machine at
`/opt/labtris/docs/handbook`. Ten pages, front to back: install, a first lab in
five minutes, images, networking, consoles, capture and diagnostics, organising
labs, users, the API, and running the server.

## Known limits

Stated plainly, because finding out later is worse.

- **Labtris does not configure your guests.** It builds the topology and starts
  them; addressing and configuration inside a node are yours. Generating guest
  configuration is planned.
- **Accounts are local** — no LDAP, AD or RADIUS.
- **No per-user quotas and no audit log.** On a shared box, who booted forty VMs
  is a conversation rather than a control.
- **x86-64 only.**
- **Do not expose the port to the internet.** It reaches the Docker socket and a
  root daemon. Put it on a VPN or a network you control, and create the first
  account before the machine goes anywhere — until one exists, the API is open.

## Licence

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
