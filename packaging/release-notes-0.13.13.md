Labtris 0.13.13 — the ISO installs again, the repository is public, and an upgrade no longer stops your labs.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

No token needed: the repository is public again, under the same MIT
licence. `LABTRIS_TOKEN` still works, for installing from a private fork.

## Every 0.13.x ISO failed to install

The unattended install ran to the end of Ubuntu's part and then stopped
at "Python environment" with
`ERROR: no /snap/subiquity/<rev>/usr/bin/python3.10 on PATH`, leaving
the installer at "An error occurred". 0.13.0 changed the installer to
honour `$PYTHON`, so that 26.04 could pick its own interpreter. The ISO
runs that script through `curtin in-target`, which hands it the
installer's environment, and the Ubuntu installer sets `PYTHON` to its
own snap's interpreter, which does not exist inside the system being
installed. The override is now `LABTRIS_PYTHON`. The one-line installer
and upgrades were never affected, since nothing sets `PYTHON` there.

Verified by installing this release's ISO on a fresh 120 GB disk under
QEMU/KVM with UEFI: unattended to the end, every service active, and the
three demo pods (spine-leaf, rail-optimised, fat-tree) loaded and
converged to BGP EVPN Established.

## An upgrade keeps container nodes running, and restarts the VMs

On the compose install, every upgrade stopped every lab node. `labnet`,
the container that owns the shared network namespace, ran the Labtris
image only to sleep, so a new tag recreated it, and recreating it
recreates everything that joins its namespace: `dind` with every lab
container, `netd` with every lab bridge. `labnet` now runs
`alpine:3.20`, so an upgrade recreates only `api` and `netd`. The first
upgrade onto this compose file still restarts container nodes once.

VMs still stop with the `api` container, which is their parent process.
At startup Labtris now waits up to 90 seconds for each runtime to
answer, then starts again every node that was running before, four at a
time. Before, it asked `dind` once while it was still restarting, got no
answer, and left every container node marked running with nothing
behind it. `LABTRIS_RESTORE_RUNNING_NODES=false` keeps the old
behaviour of marking them stopped. The install guide now has the
compose upgrade steps: back up, `docker compose pull`, `up -d`.

## Machine settings on an existing node take effect

`PATCH /nodes/{id}/qemu-options` saved to the database and never
reached the VM's `config.json`, so chipset, CPU model and the rest only
applied to nodes created after the change. Template edits had the same
gap: a node copied the template's options when it was created and never
looked again. Both now apply on the node's next start; an option the
node set itself still wins over the template.

## Also in this release

- **Reboot.** `POST /nodes/{id}/reboot`, `labtris node reboot`, the
  `reboot_node` MCP tool, and the UI's Restart button now use it. It is
  stop then start in one call, with `/stop`'s graceful or force mode.
- **Multi-queue virtio-net with vhost** (`nic_queues`, per node or
  template). Measured between two Ubuntu guests on one link: TCP with
  four streams went from 26.3 to 69.0 Gbit/s, 64-byte UDP from 0.31 to
  0.84 Mpps. Off unless set. The compose file passes `/dev/vhost-net`
  to the api container.
- **Emulated IOMMU** (`viommu` QEMU option) for guests that bind NICs to
  vfio-pci, such as DPDK, VPP or TRex. Needs the q35 chipset and
  `intel_iommu=on` on the guest kernel.
- **vCPU and queue limits** are QEMU's own (255 vCPUs, 256 queues), not
  a fixed 8.
- **VPP and TRex nodes** and the `vpp-trex` demo pod. Its hooks
  configure VPP, start TRex and check that 500 kpps of 64-byte UDP each
  way forwards with no loss.
- **Pods with hooks run them after loading.** The hooks were restored
  but nothing fired until someone triggered them by hand.
- **Generator patterns `scale-above` and `scale-edge`**, and pods for
  both: regions over transit providers, and edge sites on four
  different last miles.
