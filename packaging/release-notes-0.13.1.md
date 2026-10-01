Labtris 0.13.1 — the demo labs actually arrive, and a way to get the kernel.

A fix release. **If you took 0.13.0, upgrade again** — the bundled AI-fabric
labs did not load on it.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## The demo labs now load on an upgrade

0.13.0 installed and enabled the seeding unit, said so, and then never started
it. The guard deciding whether to start it evaluated false moments after the
unit had been installed, and the error was suppressed — so the labs were
missing and nothing in the log said why.

Seeding itself was never broken. It now tests for the unit file rather than
parsing `systemctl` output, reloads systemd before starting, and prints the
failure and the manual command if the start does not work.

After upgrading you should see `uet-pair`, `rdma-pair`, `p4-trim`,
`pfc-classes` and `ai-fabric-uet` alongside whatever you already had. Labs you
deleted on purpose stay deleted — the record tracks which pods a machine has
been offered, not whether it has ever been seeded.

## A kernel you can actually get

Every install has been reporting that the kernel is too old for soft-RoCE and
offering nothing to do about it. That is half an answer.

```
sudo labtris-kernel --check     # what you have, what you need
sudo labtris-kernel --install   # fetch, verify checksums, install
```

It finds the newest Ubuntu mainline build at or above 7.1 (7.2.6 today),
verifies the published checksums, and installs the generic flavour **alongside**
your running kernel — nothing is removed, so the old one is still in the GRUB
menu if the new one misbehaves.

Mainline builds are the same source Canonical releases from, packaged as
`.deb`, but they are **not supported**: no security backports, and a regression
on your hardware is yours to debug. That is a fair trade on a lab box and a bad
one on anything you depend on, so it refuses to act without `--install`.

After a reboot:

```
uname -r
sudo modprobe rdma_rxe
ss -lun | grep 4791        # a per-namespace listener means soft-RoCE works
```

## Also

* The stale-command cleanup works again. The three operator commands had grown
  into one `if/elif/else` chain where `labtris-upgrade`'s cleanup only ran when
  `labtris-health` was also missing, so a dangling command could survive an
  upgrade to an older release.
* `labtris-health` and the install summary now name `labtris-kernel` instead of
  only reporting that the kernel is too old.

## Everything from 0.13.0

HTTPS on 443 with 80 redirecting to it, `labtris-upgrade`, `labtris-health`,
Ubuntu 26.04 support and guacd built from source all arrived in 0.13.0 and are
in this release. See those notes for the detail — including that 26.04 has no
RDP console, and that moving to 443 will stop nginx on a host already using
80 or 443.
