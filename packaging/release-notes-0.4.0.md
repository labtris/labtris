A batch of security fixes, an appliance shape 0.3.0 couldn't boot, and the
end of a class of "signed in but not really" bugs.

## New since 0.3.0

**Template companion files: BIOS, CD-ROM and per-template qemu args.** Some
appliances need more than a disk image to boot. NX-OSv 9000 was the shove:
it wants a SATA-boot-capable OVMF variant (`OVMF-sata.fd`) as `-bios`, its
1.3 GB `cdrom.iso` mounted every boot for the initial config schema, and
`bootindex=1` on the disk so the firmware picks the right thing. Rather
than hard-coding NX-OS, Labtris now takes an optional BIOS blob, CD-ROM
blob and free-form `qemu_extra_args` list per template. Content-addressed
storage under `~/.cache/labtris/qemu-bios/` and `qemu-cdrom/`, uploaded
once and referenced by many templates, ref-counted on delete. Upload
through the palette's edit-template modal, `POST /api/v1/images/companion`,
or `labtris-image companion add`. NX-OSv 9000, cat9kv, vJunos-EVO, uccx,
ise — anything with a fixed set of extra QEMU flags — becomes a
palette-only configuration instead of a code change.

**NX-OSv 9000 actually boots.** Two independent problems. The disk bus:
the GNS3 registry says `sata`, QEMU's `-drive if=` refuses `sata`, and
Labtris was silently rewriting it to `ide` — which loses `bootindex=1`
and the AHCI HBA. Now for `disk_bus: sata` we build the real
split-device form (`-device ich9-ahci` + `-drive if=none` + `-device
ide-hd,bus=ahciN.0,bootindex=1`). The boot timeout: `_await_monitor` was
capped at 10 s and NX-OSv loads a big BIOS before the monitor comes
alive; raised to 60 s so a slow-loading appliance is not killed for
looking hung. With the companion files above and both of these,
NX-OSv reaches the login prompt.

**Disk bus and graphical propagate from the template.** A saved-image
node whose template said `sata` was falling through to `virtio` at
launch, because `qemu.start()` was reading the built-in catalog rather
than the template's spec. Same story for `graphical: true` (which
switches the console from serial to VNC). Both now come from the
template.

**Kernel-Samepage-Merging tuning stays on the doc pages.** No code
change, but the section in `docs/04-scaling.md` is now grounded in the
Ubuntu defaults directly, so a fresh install can be walked to the
measured `~8.5:1` dedup ratio with copy-paste sysctls, KSM tunables
and hugepage settings.

## Security fixes

**No more synthetic admin.** In development, a missing session cookie
was resolving to a fabricated admin user, which is convenient for
`curl` and catastrophic for a machine on any network. Anonymous
requests now get a 401. `LABTRIS_DEV_ANON=1` re-enables the old
behaviour explicitly, for local scripts that want it.

**WebSockets require the session cookie.** Console, canvas, assistant
and lab-state WS handlers were accepting the upgrade without
authentication and only checking a per-connection permission after —
which for the read-only broadcast channels was no check at all.
Every WS route now runs the same auth as the REST side, so an
unauthenticated attach fails at the handshake.

**The browser reacts to a 401.** A stale cookie used to render the
shell first and then let API calls silently fail. Now the very first
`/auth/whoami` deciding "not signed in" clears local state and
routes to `/login` before any panel mounts.

**Assistant WebSocket mints its own bearer.** The AI tool loop was
calling API endpoints with `token=None`, hitting the new WS auth,
and reporting "sign in to continue" to the model instead of running
the action. It now issues a short-lived token bound to the calling
user for the loop's own callbacks.

## Users and admin

**Settings → Users.** Add and remove accounts from the UI, list who
exists, promote to admin. Two roles (`admin` and `user`) — admins
can do everything and see everyone's labs, users only see their
own. The first account created is admin.

## Consoles

**No more `\r\n` per keystroke.** The QEMU console WebSocket was
appending CRLF to every frame going out to the browser, corrupting
the terminal for anything more sophisticated than `echo`. Fixed:
bytes go through raw.

**Resize frames stop leaking into the guest.** After removing the
CRLF wrapper, `{"resize": ...}` JSON messages from the browser
started echoing into the guest's serial. Now filtered like the
container-shell path already does.

**VNC on same-tab reconnect.** A second Guacamole client attaching to
the same VNC session was being refused (Guacamole default is
exclusive), producing a blank frame or a stuck "tunnel 519" error.
Labtris now spawns with `share=force-shared`, and the browser flushes
the previous tunnel before opening a fresh one.

## Assistant

**No cap on tool calls per turn.** The old 20-tool-call ceiling was
being hit by mid-complexity multi-step plans and looked like the
model giving up. The cap is now a 500-call runaway backstop, not a
per-task limit — bounded to catch loops but not to constrain work.

**Every tool call renders its own bubble.** Long turns used to fold
all tool activity into one final bubble at the end. Now each call
appears as it happens, with the tool name and either its result or
its error. A failing call renders as an error bubble rather than
disappearing.

## Documentation

The handbook and the top-level docs no longer name EVE-NG on the
user-facing pages. Design docs (`01-findings`, `02-features`,
`03-architecture`, `05-phase1-spec`, `eve_api.txt`) still do — that
is where the comparison belongs.

## Install

**The ISO is attached in parts.** GitHub refuses any release asset of 2 GiB
or more, and this image is ~3.2 GB because every package it installs is on
the disc. Download `labtris-0.4.0-amd64.iso.part-*`, then join them (the
`*` glob sorts correctly) and verify with the checksum of the finished
image in one flow:

```
cat labtris-0.4.0-amd64.iso.part-* > labtris-0.4.0-amd64.iso
sha256sum -c labtris-0.4.0-amd64.sha256      # macOS: shasum -a 256 -c
sudo dd if=labtris-0.4.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The install is unattended and takes six to ten minutes. Every Debian
package, every Python wheel and the prebuilt interface are **on the disc**
— it fetches nothing. When it finishes, the console prints the address to
open.

## Upgrading from 0.3.0

The API is compatible; the DB migrates on first start of the new service.
On an existing 0.3.0 install:

```
cd /opt/labtris && git pull && systemctl restart labtris-api labtris-netd
```

For a fresh install use the ISO.

**Existing templates keep working.** The new companion-file fields
(`bios`, `cdrom`, `qemu_extra_args`) default to unset, so a template
that boots today boots the same way after the upgrade. NX-OSv is
the case where you want to turn them on — see the handbook page on
nodes and images.

## What is in it

**Nodes.** Docker containers and QEMU virtual machines. Containers start
in a second; VMs take as long as that OS takes to boot, get real
`savevm`/`loadvm` snapshots, and run under KVM where the host has it.
Appliances can carry a companion BIOS, a CD-ROM and free-form QEMU
flags per template.

**Networking.** Point-to-point links, internal bridges, NAT segments with
a DHCP server and static reservations, cloud networks onto a host NIC (or
onto a host-owned bridge like the netplan `br0`), and VXLAN segments that
stretch a topology across several hosts. Bridges can be made VLAN-aware —
802.1Q or QinQ, with real access and trunk ports. Links take `netem`
delay, jitter, loss, reordering and rate limits, per direction.
Admin-down actually unplugs the cable.

**Consoles.** Container shells over a real PTY. QEMU serial consoles from
the first byte of boot, without CRLF or resize-frame contamination. VNC
and RDP through Guacamole for graphical guests, with force-shared display
so a same-tab reconnect works. Terminals dock, split side by side, or
pop out into their own window.

**Users.** Two roles, add and remove from Settings, admin-only. Every
API and WebSocket path authenticates.

**Assistant.** An MCP-fluent chat pane that can drive the lab. Tool calls
render as they happen; up to 500 per turn; each call carries a fresh
bearer bound to the signed-in user.

**Import.** Legacy `.unl` XML, containerlab `.clab.yml`, and this app's
own exports.
