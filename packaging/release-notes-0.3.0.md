Two things you could not do in 0.2.0: draw a topology and see it come up right,
and change your mind after registering an appliance. Both were single fixes
hiding a pile of small ones.

## New since 0.2.0

**Every interface a node knows about now boots with the guest.** QEMU used to
start with `-nic none` and hot-plug each interface after boot — but only for
interfaces already wired to a segment, and only if the guest accepted PCI
hot-plug at all. A port you had added but not yet cabled was invisible inside
the guest, and network appliances that enumerate PCI once at boot (NX-OSv,
PAN-OS, most physical-shape images) ignored the whole idea. Real routers show
every configured interface regardless of whether a cable is present; the port
state reflects the wire. Labtris now behaves the same way — every declared
interface is cold-plugged at boot as a `-netdev tap` + `-device` pair, and
whether the tap is enslaved to a bridge is a separate step handled after
boot. An unwired port shows up in the guest as a NIC with no carrier.

**Admin-down actually unplugs the cable.** It was setting the host tap DOWN
and stopping there — but virtio-net (and e1000) reports carrier from the
netdev backend attachment inside QEMU, not from the L1 state of the host tap,
so the guest kept reporting the port UP no matter what you did on the outside.
Labtris now sends the QMP `set_link` alongside, and the guest's driver raises
the expected link-down interrupt. `Admin up` restores it symmetrically.

**Templates are editable.** Everything set with `labtris-image add` — RAM,
CPUs, NIC model, disk bus, iface scheme, description, the name itself — is
now a pencil ✎ button away in the palette. Change PAN-OS's disk bus to sata,
bump NX-OS to 16 GB RAM, rename an old import — no more delete-and-re-add.
The runtime CPU model (`qemu64` / `host` / `max`) is exposed in the same
modal, because appliances with a modern glibc (PAN-OS 11+, RHEL 9+, Ubuntu
24.04 with a newer libc) panic on `qemu64`'s pre-2010 x86-64 baseline and
need `host` (KVM passthrough) or `max` (software equivalent).

**Assistant survives a refresh.** The conversation used to evaporate on
reload, which was uniquely fragile — every other panel (canvas geometry,
palette state, theme, terminal tabs) persists. Now the chat reloads with
everything intact, up to a 200-bubble rolling cap so localStorage cannot
fill on a long session. A **Clear** button in the ChatPane header wipes the
conversation without touching the lab; the confirm dialog says as much.
The prompt box is a real multi-line textarea that grows with the content:
Enter sends, Shift-Enter inserts a newline, the convention every chat app
has settled on.

**The canvas refreshes when the assistant edits it.** Only lifecycle events
(start/stop/fail) used to publish to the lab WebSocket, so tool calls from
the assistant that added a node or drew a link left the canvas sitting stale
until the next state change. Middleware now watches every successful
POST/PATCH/PUT/DELETE, resolves which lab it touched from the path
parameters, and publishes a topology event — the browser side already
refetches on any WS frame, debounced, so a burst of tool calls turns into
one repaint.

**EVE-NG-shaped disk flags.** Labtris was passing `-drive if=virtio,format=qcow2`
and nothing else. EVE passes `bus=0,unit=0,cache=writeback,aio=threads,discard=unmap,detect-zeroes=unmap`
for every VM — pure performance defaults, but `discard=unmap` in particular
tells the guest the disk supports TRIM, which appliances that probe hardware
read as "thin virtual disk." Without it, PAN-OS's dhpm picks its physical-
hardware personality (PA-HDF) instead of PA-VM. Confirmed by direct
diagnostic: same PAN-OS disk under EVE's flags boots PA-VM, under the old
flags boots PA-HDF.

**Appliance interface names match the appliance.** Two new iface schemes
follow the EVE convention exactly: `paloalto` → mgmt, eth1/1, eth1/2, …;
`nxos` → Mgmt0, E1/1, E1/2, … Because the first port of a real router is
the out-of-band management NIC and the rest are data ports, that shape
belongs in the palette rather than being papered over. Other schemes
(`eth`, `ens`, `enp`, `vmware`, `srl`, `ios`) are unchanged.

**Fix for saved-image nodes:** `POST /nodes/{id}/interfaces` was consulting
only the built-in `QEMU_CATALOG` for the iface scheme, so a saved-image
node whose template said `paloalto` fell through to `eth` for added ports.
Now it consults the template's spec first, matching how create-time
resolution already worked.

**index.html is served no-cache.** The static-file mount used to send no
`Cache-Control` at all, so browsers heuristically cached `index.html` and
kept loading the previous JS bundle after a deploy until someone manually
hard-refreshed. Now `assets/*` (fingerprinted, safe forever) is marked
`immutable` and `index.html` is `no-cache`, the standard hashed-asset SPA
setup.

**VyOS from ISO in one script.** `packaging/recipes/vyos.sh` boots the
official installer, drives you through `install image` over serial or VNC,
and calls `labtris-image add` at the end. Adds `VYOS_CONSOLE=serial` for
headless installs (drive over SSH, no VNC needed) and `VYOS_VNC_PASSWORD`
for exposed installs (VNC over a trusted lab network).

## Install

**The ISO is attached in parts.** GitHub refuses any release asset of 2 GiB
or more, and this image is 3.5 GB because every package it installs is on
the disc. Download every `labtris-0.3.0-amd64.iso.part-*` file, then join
them — order matters, and the `*` glob sorts correctly:

```
cat labtris-0.3.0-amd64.iso.part-* > labtris-0.3.0-amd64.iso
sha256sum -c labtris-0.3.0-amd64.sha256      # macOS: shasum -a 256 -c
```

The checksum is of the finished image, so it verifies the join as well as
the download. Do not skip it — a truncated part produces an ISO that boots
and then fails partway through the install.

Then write it to a USB stick and boot the machine:

```
sudo dd if=labtris-0.3.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The install is unattended and takes six to ten minutes. Every Debian
package, every Python wheel and the prebuilt interface are **on the disc**
— it fetches nothing. When it finishes, the console prints the address to
open.

Verify the download first:

```
sha256sum -c labtris-0.3.0-amd64.sha256
```

## Upgrading from 0.2.0

The API is compatible; the DB migrates on first start of the new service.
On an existing 0.2.0 install:

```
cd /opt/labtris && git pull && systemctl restart labtris-api labtris-netd
```

For a fresh install use the ISO. Nodes created under 0.2.0 keep their old
interface names (Interface.name is a per-row column); template edits and
new schemes only affect newly-added interfaces. If you want existing
appliance nodes renamed (`ens3` → `mgmt`, etc.) that's a small SQL update;
ask in an issue.

## What is in it

**Nodes.** Docker containers and QEMU virtual machines. Containers start
in a second; VMs take as long as that OS takes to boot, get real
`savevm`/`loadvm` snapshots, and run under KVM where the host has it.

**Networking.** Point-to-point links, internal bridges, NAT segments with
a DHCP server and static reservations, cloud networks onto a host NIC (or
onto a host-owned bridge like the netplan `br0`), and VXLAN segments that
stretch a topology across several hosts. Bridges can be made VLAN-aware —
802.1Q or QinQ, with real access and trunk ports. Links take `netem`
delay, jitter, loss, reordering and rate limits, per direction.
Admin-down actually unplugs the cable.

**Consoles.** Container shells over a real PTY. QEMU serial consoles from
the first byte of boot. VNC and RDP through Guacamole for graphical
guests. Terminals dock, split side by side, or pop out into their own
window.

**Seeing what happens.** Packet capture on any link or segment with a BPF
filter, Wireshark itself streamed from the server, live guest addressing
shown on the canvas, DHCP leases, and live NAT sessions.

**Labs.** Folders, clone, lock, named startup-config sets, import from
EVE-NG `.unl` and containerlab `.clab.yml`, and a one-file backup of
every lab.

**API and assistant.** Everything the interface does goes over the same
REST API, documented at `/docs`. An MCP server with 22 tools lets a model
drive a lab directly. The built-in assistant runs the conversation **in
your browser with your own key** — Labtris never sees it — and its tools
run as you, so it cannot do anything you could not do by hand. The chat
survives a refresh; the canvas refreshes when the assistant edits it.

## Bring your own images

Labtris redistributes no vendor software. Docker images come from whichever
registry you point at; QEMU images are yours to obtain. The GNS3 registry's
228 appliance definitions can be imported to populate the palette. VyOS
has a one-command recipe in `packaging/recipes/vyos.sh`.

## Documentation

The handbook is attached as a PDF, lives in the repository under
`docs/handbook/`, and ships on the installed machine at
`/opt/labtris/docs/handbook`. Ten pages, front to back: install, a first
lab in five minutes, images, networking, consoles, capture and
diagnostics, organising labs, users, the API and the assistant, and
running the server.

## Known limits

- **Labtris does not configure your guests.** It builds the topology and
  starts them; addressing and configuration inside a node are yours.
- **Accounts are local** — no LDAP, AD or RADIUS.
- **No per-user quotas and no audit log.**
- **x86-64 only.**
- **Do not expose the port to the internet.** It reaches the Docker socket
  and a root daemon. Put it on a VPN or a network you control, and create
  the first account before the machine goes anywhere.
- **The server-side assistant path** doesn't thread per-session history
  yet — refresh preserves the visible chat but the model won't remember
  conversational state across turns unless you use the browser-key path
  (Settings → Assistant, your own key).

## Licence

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
