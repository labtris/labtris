# Security

## Reporting a vulnerability

Email **contact@labtris.com**. Please do not open a public issue for
anything exploitable — a public report is readable by everyone running
Labtris, including people who have not had a chance to upgrade.

Include what you need to make it reproducible: the version, how Labtris was
installed (one-liner, compose or ISO), and the steps. A proof of concept is
welcome but not required.

Expect a first reply within a few days. This is a small project and there is
no paid security team behind it, so please read that as a genuine estimate
rather than a service commitment.

## What is in scope

Labtris runs untrusted-ish workloads on purpose — container images and VM
disks the operator chose — and gives the browser a lot of reach into the
host. Things worth reporting:

* Anything that lets a lab node reach the host beyond its namespace, or
  reach another lab it was not wired to.
* Anything that lets an unauthenticated request act as an authenticated one,
  or a normal user act as an admin.
* Command injection through node options, template fields, lab imports or
  pod archives.
* Secrets appearing where they should not: the database password, the session
  secret, or a node's configuration in a log, an API response or a pod export.

## What is not a vulnerability

These are documented behaviours rather than bugs. If one of them surprised
you, that is worth an issue about the documentation.

* **`labtris-netd` runs as root.** It is the only process that touches
  netlink and nftables. That is the design, and its API surface is the thing
  to scrutinise rather than the fact of it.
* **The API can start processes on the host.** Creating a QEMU node runs
  QEMU. `qemu_allow_extra_args` is off by default precisely because it turns
  node options into unrestricted arguments to that process; turning it on is
  a decision to trust whoever can create nodes.
* **A privileged container node can affect the host.** Labtris will run the
  image you ask it to. Node profiles that mount `/dev/infiniband` or add
  capabilities do what they say.
* **Reaching Labtris means controlling Labtris.** Bind it to a private
  address or a VPN. The compose file defaults to `127.0.0.1` for that reason
  — before the first-run wizard completes there is no admin account, so an
  instance published on `0.0.0.0` can be claimed by whoever finds it first.

## Supported versions

The latest release. There are no backports to older tags — fixes go into the
next release, and upgrading is a tag bump or re-running the installer.
