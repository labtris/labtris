Labtris 0.13.3 — bring your EVE-NG labs and images across.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## One command to migrate from EVE-NG

Run it on the Labtris host. It asks for the EVE-NG address, reads what is
there over SSH, and shows you the labs and the images with their sizes:

```
sudo -u labtris labtris-migrate-eveng --dry-run
```

```
Labs (7)
    #  name                             nodes  templates
    1  ccie-rs-lab1                        14  vios, viosl2, veos
    2  junos-basics                         6  vmxvcp, vmxvfp

Images (12, 86.4GB total)
    #  directory                              size
    1  vios-adventerprisek9-m-15.6.2T        1.2GB
```

Pick by number, range (`2-4`), name (`ccie`) or `all`. Choosing a lab works
out which image directories its templates need and copies **only those** — so
trying one 14-node lab moves a few GB rather than the whole library. Then it
registers the images and imports the topology. Drop `--dry-run` to do it.

**It never handles your password.** One SSH connection is opened up front and
ssh does its own authentication, so the password goes from your terminal to
ssh and nowhere near the script. Everything after reuses that connection,
which is why it only asks once.

## Cold and hot

| | `--mode cold` (default) | `--mode hot` |
|---|---|---|
| Topology | yes | yes |
| Base images | yes | yes |
| Each node's configured disk | — | yes |
| Size | small | much larger |

Cold gives you the lab's shape with factory-default devices. Hot gives you the
working disk — the one with the configuration actually applied. Forty hours
into a certification lab, the shape is not what you want back.

Hot is not simply a different file. EVE-NG's runtime disks are qcow2 deltas
whose backing file is an `/opt/unetlab` path that does not exist on your
Labtris host, so a copied delta will not open. Each one is flattened with
`qemu-img convert` on the EVE-NG box first, pulled, and the flattened copy
removed from there afterwards. The plan tells you the size before anything is
copied, and hot falls back to cold — loudly — when no lab on that box has ever
been started, because there is no configured state to bring.

## Also

`labtris-adopt-eveng-images` registers an existing image library on its own,
without the lab import, for the case where the images are what you want.

Both are read-only until you confirm, and `--dry-run` scans and prints the
plan without touching anything. **Try that first** — these paths have been
tested against synthetic trees, not a real EVE-NG install, so the first run on
real data is the one that will find the gaps.

## Everything from 0.13.x

The demo-lab seeding fix, `labtris-kernel`, HTTPS on 443 with 80 redirecting
to it, `labtris-upgrade`, `labtris-health`, Ubuntu 26.04 and guacd built from
source are all in this release.
