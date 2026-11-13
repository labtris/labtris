Labtris 0.13.14: links change on running VMs, a VM whose QEMU dies says so, and Ubuntu cloud images boot on the ISO install.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## Links on running VMs

Re-pointing a link on a running VM used to answer 201 and leave the
guest's card on its old bridge, or with no backend at all. The tap now
moves to the new bridge, so the guest sees its cable pulled and plugged
back in, on both chipsets.

An interface added to a running VM is hot-plugged. On `pc` this uses
the main bus. `q35` VMs now boot with spare PCIe slots
(`hotplug_slots`, default 4) so a card can be added and removed there
too. The result is checked in QEMU and on the tap. A change that did
not take answers `409 needs_restart`, with the change saved for the
next start.

Interfaces can be removed: **Remove interface…** in the node's menu, or
`DELETE /nodes/{id}/interfaces/{iface_id}`. A port that still has a
link is refused with `409 conflict`. A q35 card that was there at boot
leaves at the next restart.

The handbook has a new chapter, *Virtual hardware*: chipsets, the
emulated IOMMU, NIC queues, and what can change while a VM runs.

## A VM whose QEMU dies is no longer shown as running

A VM that crashed, was killed or powered itself off stayed `running`
until someone stopped it. Labtris now watches every QEMU it starts, and
those that were already running when the API restarted, and marks the
node stopped within seconds. A crash leaves QEMU's own error in
`last_error`; a guest power-off leaves none. A VM that dies right after
launch, for example on an unknown CPU model, now fails its start with
QEMU's message instead of reporting `running`.

## No silent BIOS drop

A template could point at a BIOS or CD-ROM file that was not there, and
the VM booted without it: SeaBIOS instead of OVMF, with no warning.
Saving such a path now returns 422, and starting a VM whose BIOS file
has gone fails with a message naming the file.

## Ubuntu cloud images on the ISO install

- The ISO and native install lacked `cloud-image-utils`, so Ubuntu cloud
  images booted with no user. It is now installed.
- The cloud-init seed was attached without `media=cdrom`, and QEMU
  exited with "Block node is read-only".
- Two VMs started back to back could get the same VNC display.

## Also in this release

- **Assistant conversations live on the server.** A turn keeps running
  when the tab closes, another tab can follow it and answer its
  approvals, and the next turn sees the history. Only Cancel stops a
  turn.
- **`qemu_opts` at node create** is kept, merged over the template's.
  Unknown options, or `qemu_opts` on a container, return 422.
- **`POST /images/pull`** works for Docker images again; it crashed on
  every one in 0.13.13.
- **The first console command after an API restart reaches the VM.** The
  console reconnects on first use, and the command used to arrive before
  the connection did: it was dropped, and `console/exec` returned nothing
  after its full timeout. It now waits for the console, or says it is not
  connected.

Tested on a 0.13.13 ISO install, and again on a fresh install of this
release's ISO, with Ubuntu 24.04 guests and alpine containers: rewire,
hot-plug and removal on pc and q35 with ping across each change, killed
and powered-off VMs, and an API restart with VMs running.
