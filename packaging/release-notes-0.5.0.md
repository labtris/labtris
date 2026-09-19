Labtris 0.5.0 — the release that lets a Claude Code / Cursor / MCP
client drive a graphical guest the same way it drives a serial one,
plus the fixes that surfaced the first week 0.4.0 met real users.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.5.0-amd64.iso.part-*` on the release
page, joined and checksummed the same way as 0.4.0.

Upgrading from 0.4.0: `cd /opt/labtris && git pull && sudo systemctl
restart labtris-api`. Existing running QEMU VMs need to be stopped and
started once to gain a QMP socket (mouse support depends on it); a
clear error names the fix if you try to click before restarting.

## New

**VNC assistant tools.** Three new MCP tools and three new REST
endpoints make a graphical guest first-class for the assistant loop:

- `vnc_screenshot` returns a PNG of the framebuffer. Optional
  `region=x,y,w,h` crop and `grid=N` overlay (a light grid with axis
  labels) so the model can pick coordinates without guessing.
- `vnc_type` types text or sends raw HMP `sendkey` combos, and
  returns the after-screenshot in the same response — one turn per
  action instead of two. Set `screenshot=false` when firing a burst
  of keys where only the final frame matters.
- `vnc_mouse` moves and clicks in framebuffer pixel coordinates.
  Absolute positioning via QMP `input-send-event` — no relative
  `mouse_move` guessing.

Together they close the "there is a login prompt but no serial getty"
gap — Ubuntu Desktop, vendor appliance wizards, PC BIOS/GRUB menus.
The `console_exec` tool description learns to steer the model to VNC
tools when a QEMU node returns nothing on serial.

**Prefetch a QEMU image without spawning a node.** `POST
/api/v1/images/pull` kicks off download+extract+convert in the
background and returns immediately with a status snapshot;
`/images/status` is the polling target. Same machinery on three
surfaces:

- `labtris-image pull <id>` in the CLI shows a live progress bar.
- `image_pull` / `image_status` MCP tools for the assistant.
- A **Pull now** button next to any uncached image in the palette;
  the existing progress bar renders in place.

Before, first-time-use of a 2 GB image meant an eight-minute stall
inside a node start.

## Fixes

**QEMU 8.2 bootindex.** The `-drive if=<bus>` shortcut form rejects
`bootindex` on this QEMU (`Block format 'qcow2' does not support the
option 'bootindex'`). virtio and ide now build the split form —
`-drive if=none` + `-device virtio-blk-pci|ide-hd,drive=…,bootindex=1`
— which matches how the sata branch already worked. Fixes VMs that
refused to boot on Ubuntu 24.04.

**Container consoles draw their prompt.** Bash writes its prompt to
*stderr*, and the wrapper had `exec bash -i 2>/dev/null` to hide
bash-not-found errors on sh-only images. That redirect was silencing
every prompt. The container console has been a real PTY for a while
(cd persists, Ctrl-C interrupts, top redraws) — it now looks like one
on connect too.

**console_exec accepts empty commands.** A bare `"\n"` wakes a getty
stuck at `login:`. The old validator refused it and the assistant
gave up configuring Ubuntu VMs.

**Docker console menu label.** Was "Shell" on Docker and "Console" on
QEMU. Two names for the same feature made people ask for a Docker
console. Both are "Console" now.

## Verified

Every VNC tool exercised on real QEMU 8.2.2: screenshot returns a
real 1280×800 PNG with the `-f png` variant of `screendump`, keys
land via `sendkey`, and mouse clicks land pixel-accurately via QMP
`input-send-event`. Image prefetch exercised end-to-end with cirros
(20 MB): API returns 202, status polls show 0% → 41% → cached, CLI
progress line renders and finishes with the on-disk size.

## Bring your own images

Unchanged from 0.4.0. Labtris redistributes no vendor software; the
GNS3 registry's 228 appliance definitions import natively.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
