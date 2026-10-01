Labtris 0.13.0 — HTTPS by default, Ubuntu 26.04, and a real upgrade path.

Most of this release came out of running the previous one on a clean machine
and writing down what actually went wrong.

## HTTPS by default

The interface served plain HTTP on port 8081. It now serves **443 with TLS**,
with **80 redirecting** to it. The certificate is self-signed and generated once
at install; replace the two files in `/etc/labtris/tls` to install your own and
nothing regenerates them.

Self-signed rather than ACME because the common case is a lab box on a private
address with no public DNS name, where ACME cannot issue at all. Your browser
will warn once.

**8081 is still there**, still plain HTTP. It is the port every `ssh -L` example
and QEMU hostfwd forwards, and over a tunnel to `127.0.0.1` a certificate for
the real hostname would not match anyway. Both ports reach the same instance.

## Upgrading, as one command

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

It goes to the latest **release** rather than tracking `main`, dumps the
database before it migrates anything, and tells you whether the result is
actually serving — printing the journal command, the previous commit and the
path to the dump if it is not. Labs, pods, images and the database carry
across.

From this release it is installed as `sudo labtris-upgrade`.

## A health check worth running

```
labtris-health
```

Services, all three listeners, whether 80 really redirects, the certificate's
expiry and SANs, the database **and whether the schema matches what the code
expects**, the UET dissector, the kernel floor for soft-RoCE, Docker and
`/dev/kvm`. Exits 0 or 1, so it works in cron or CI, and it writes nothing.

`labtris-doctor` reports host facts and still does. This answers a different
question: systemd will report `labtris-api` active while every request 500s on
a migration that did not finish.

## Ubuntu 26.04

The native installer and the ISO accept 26.04. Two things were in the way:

* **Python.** Resolute packages no `python3.12` in any form. The package list
  now asks for the distribution's own `python3` — 3.12 on noble, 3.14 on
  resolute — and the tree accepts `>=3.12,<3.15`. Five dependencies needed
  newer pins for 3.14 wheels: `asyncpg`, `PyYAML`, `Pillow`, `pydantic` and
  `SQLAlchemy`.
* **Guacamole.** Ubuntu stopped packaging `guacd` after noble, so Labtris now
  builds it from the Apache release tarball.

**One gap on 26.04: there is no RDP console.** Guacamole 1.6.0 does not compile
against FreeRDP 3, which is the only FreeRDP resolute ships. VNC, SSH and telnet
consoles work, and QEMU consoles are VNC — this only affects connecting to a
Windows guest over RDP. Use 24.04 or the container install for that.

## Demo labs now arrive on an upgrade

They were seeded only by the ISO's first boot, which meant a shell install never
got them at all and an upgrade never got the ones added since. Both are fixed.

The record now tracks **which** pods a machine has been offered rather than
whether it has ever been seeded, so a new lab reaches an existing install while
a lab you deleted on purpose stays deleted.

## Also

* A firewall node kind and an inline virtual-wire lab for developing a DPU or
  IoT container firewall without the hardware.
* `uet-htsim`, the UEC simulator, for questions `uet-ref` cannot answer.
* `get.sh` and the install summary report the host kernel, since soft-RoCE
  needs Linux 7.1+ and no distribution ships one — below that the RDMA lab
  starts and moves no data.
* `SECURITY.md`, and a contact address that is not a GitHub issue.

## Upgrading from 0.12.0 or earlier

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

Tested from a real 0.11.0 install: upgraded with its existing labs intact and a
restorable dump in `/var/backups/labtris`.

Note that this changes what nginx listens on. If something else on the host owns
80 or 443, nginx will not start — the upgrade's health check catches it and
says so, but the instance is down until the conflict is resolved.
