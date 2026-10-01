Labtris 0.13.2 — `labtris-kernel --install` actually installs a kernel.

A fix release. `labtris-kernel --install` shipped broken in 0.13.1: it
downloaded the mainline kernel and then failed partway through, leaving a
half-installed package.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## What was wrong

Not your machine. Ubuntu's mainline `linux-image` maintainer scripts contain,
in both the preinst and the postinst:

```sh
if [ -d /etc/kernel/<phase>.d ] || [ -d /usr/share/kernel/<phase>.d ]; then
    run-parts ... /etc/kernel/<phase>.d /usr/share/kernel/<phase>.d
```

`run-parts` takes exactly one directory — `Usage: run-parts [OPTION]...
DIRECTORY`. Given two it exits with `run-parts: missing operand`, `set -e`
fires, and dpkg stops with the kernel partly unpacked. The guard trips when
*either* directory exists, which is why this hit some machines and not others.

## What it does now

For the duration of the install, `run-parts` is replaced with a wrapper that
splits several directories and calls the real binary for each one that exists,
so the kernel's own hooks still run — `/etc/kernel/postinst.d` is where the
initramfs and GRUB hooks live, and skipping them would produce a kernel with
no initrd and no boot entry. That looks like success and is worse than a clean
failure. The real binary is restored by a trap on `EXIT`, `INT` and `TERM`.

It also installs with `apt-get` rather than `dpkg -i`, because the modules
package depends on `wireless-regdb` and dpkg will not fetch it, and it now
verifies the initrd and the GRUB entry exist instead of trusting a zero exit.

Verified end to end on a 6.8 machine: package configured, `vmlinuz` and
`initrd.img` present, GRUB entries written, `run-parts` restored.

## If 0.13.1 left you with a half-installed kernel

```
sudo dpkg --remove --force-remove-reinstreq linux-image-unsigned-7.2.6-070206-generic
sudo apt-get -f install
```

Then upgrade and try again:

```
sudo labtris-kernel --install
sudo reboot
uname -r
sudo modprobe rdma_rxe
ss -lun | grep 4791        # a per-namespace listener means soft-RoCE works
```

Nothing is ever removed, so your running kernel is untouched and remains in the
GRUB menu.

## Everything from 0.13.1 and 0.13.0

The demo-lab seeding fix, HTTPS on 443 with 80 redirecting to it,
`labtris-upgrade`, `labtris-health`, Ubuntu 26.04 support and guacd built from
source are all in this release. See those notes for the detail — including that
26.04 has no RDP console, and that moving to 443 will stop nginx on a host
already using 80 or 443.
