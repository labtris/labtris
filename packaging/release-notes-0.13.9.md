Labtris 0.13.9 — hot copies work on labs that are running.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## Why four disks failed to flatten

Not disk space. `qemu-img` cannot read the disk of a node whose lab is
**started** on the EVE-NG box:

```
qemu-img: Could not open '…/virtioa.qcow2': Failed to get shared "write" lock
Is another process using the image?
```

Which is exactly why some nodes in a lab succeeded and others did not — the
running ones were locked. Verified against a live VM rather than inferred.

0.13.8 then made it worse by guessing out loud. It printed "out of space on
the EVE-NG box, or qemu-img missing" and discarded `qemu-img`'s own message,
so the one line that explained it never reached you.

## What happens now

The real error is printed. And when it is a lock, the copy is retried
read-only:

```
  that node's lab is running on the EVE-NG box, so its disk is locked.
  Retrying read-only, which gives a crash-consistent copy — as if the
  machine lost power. Stop the lab there and re-run for a clean one.
  copied while running — treat the result as crash-consistent
```

`qemu-img -U` reads a disk another process holds. The result is
crash-consistent: the guest's filesystem is as it would be after losing
power, so a journalled filesystem replays on boot and anything unflushed is
gone. For a router whose configuration was saved, that is almost always fine.
For one with unsaved changes in memory, it is not.

**Stop the lab in EVE-NG and re-run if you want a clean copy.** The warning
says so at the point it matters, rather than leaving you to find out when a
guest boots into a repair prompt.
