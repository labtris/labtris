# Labtris

Network labs you actually own.

Labtris runs on your own Linux server. It boots real containers and virtual
machines there, wires them together with the kernel's own networking, and gives
you the canvas and the consoles from any browser on the network. No node limit,
no licence server, no account — and the installer needs no internet at all.

Apache-2.0. Self-hosted. Installs from a USB stick onto a machine that has never
been online.

```
┌─ Customer PoCs / edge-lab ───────────── ● 3 running ── 10.0.1.0/24 ─┐
│                                                                     │
│   ┌── n1 ──────────┐              ┌── ubuntu-24.04 ──────────┐      │
│   │ alpine:3.20    │──10.0.1.0/24─│ qemu · virtio · 4 GB     │      │
│   │ eth1 10.0.1.1  │              │ ens3 10.0.2.2            │      │
│   └────────────────┘              └──────────────────────────┘      │
│            │                                                        │
│   ┌── n2 ──┴───────────────┐      ┌── ⇄ nat-out ─────────────┐      │
│   │ eth1 10.0.1.2          │──────│ 10.200.0.0/24 · dhcp     │      │
│   │ eth2 10.0.2.1          │      └──────────────────────────┘      │
│   └────────────────────────┘                                        │
└─────────────────────────────────────────────────────────────────────┘
 n1:~# ping -c 2 10.0.2.2
 64 bytes from 10.0.2.2: seq=0 ttl=63 time=0.312 ms
```

## Install

### One line, on a fresh Ubuntu 24.04 machine

```bash
curl -fsSL https://labtris.com/install | sudo bash
```

Runs about five minutes and finishes with the URL to open, where the
config lives, and where the generated database password sits. The script
is idempotent — re-run it to upgrade. See [Install](https://docs.labtris.com/install)
for the details.

### From the ISO — for bare metal or an air-gapped machine

Every Debian package, every Python wheel and the built interface are on
the ISO, so it needs no network at all.

```bash
sudo dd if=labtris-0.4.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Boot it, and when it finishes open `http://<host>:8081`. First login is
`labtris-admin` / `labtris`, and it will make you change it.

To try the installer without touching a disk:

```bash
./packaging/iso/test-boot.sh dist/labtris-0.4.0-amd64.iso
```

### From source — for development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

make dev-db          # Postgres in Docker (LABTRIS_PG_PORT=55432 if 5432 is taken)
sudo make netd       # the privileged network daemon
make api             # http://localhost:8080
make web             # the interface, on http://localhost:5173
make guacd           # optional: VNC/RDP consoles (apt install guacd)

make test
```

`make web` runs Vite's dev server. To serve the interface from the API instead,
run `make web-build` — `web/dist` is gitignored and only mounted when present,
so it needs rebuilding after each UI change.

Building the installer yourself:

```bash
./packaging/iso/build.sh          # writes dist/labtris-<version>-amd64.iso
```

## What it does

**Nodes.** Docker for anything that ships as an image, QEMU for anything that
ships as a disk. Same canvas, same wiring, same consoles. QEMU nodes get real
`savevm`/`loadvm` snapshots and suspend/resume.

**Consoles.** The guest's real serial port relayed from QEMU — kernel messages
and all — plus a real PTY inside every container (`docker exec -it` with
`cd` that persists, Ctrl-C that interrupts, `top` that redraws), plus VNC and
RDP through Guacamole for graphical guests. Passwords stay hidden because the
guest decides what is echoed, not the page.

**Networking.**

| | |
|---|---|
| Internal bridge | An L2 segment, lab-only |
| NAT | A gateway and masquerading, with DHCP and address reservations |
| Cloud | One of the host's own NICs, enslaved to the lab |
| VXLAN | One segment spanning several hosts |
| VLAN filtering | Real 802.1Q or 802.1ad, with access and trunk ports |
| Impairment | `netem` per direction — delay, jitter, loss, reordering, rate |

**Capture.** `tcpdump` on any link with a BPF filter, or the real Wireshark GUI
running on the server and streamed to your browser.

**Labs.** Folders, clone (with fresh MACs and host device names), lock,
ownership, and several named startup-config sets per lab.

**Ready hooks.** YAML on each lab names what "ready" means for this lab —
ping, HTTP, exec, or a serial regex — and Labtris fires the checks when
every node is up. Lets a lab self-test, and turns a handout into "here is
what should be true; here is what actually is."

**Portable snapshots (pods).** `labtris lab snapshot <id>` writes the whole
lab into a single `.tar.gz` — topology, hooks, per-node disk state, and (in
hot mode) live QEMU memory + committed Docker filesystems. `labtris lab load
<path>` restores it as a new lab on any host. Modeled on LocalStack pods.

**CLI.** `labtris` — nested subcommands (`lab`, `node`, `pod`, `system`),
rich tables for humans, `--format json` for scripts. Same API surface as
the web UI; talks to the server over HTTP with a 7-day JWT stored under
`~/.config/labtris/auth.json`.

**API.** Everything the interface does, the REST API does; the same surface is
exposed over MCP so an agent can drive a lab.

**Density.** Kernel same-page merging is configured and enabled, which is what
makes twenty near-identical guests on one host reasonable rather than
theoretical.

## What is not built yet

Being straight about this saves you an afternoon.

- **Configuration generation.** Labtris draws the links; it does not write
  addresses or bring up OSPF and BGP for you. Guests come up unaddressed. Ready
  hooks can tell you when addressing has converged; they will not do the
  addressing for you.
- **A vendor node catalogue.** Docker and QEMU run whatever you bring, but there
  is no curated list of vendor images. The bootstrap runner is the framework
  for one — vendor recipes drop into `packaging/recipes/bootstrap/*.json` —
  but the list of vendors covered is small.
- **Classroom features.** Shared labs, cohorts, per-user quotas and lab
  instructions are designed but unbuilt. Ownership and lock exist; the class
  layer on top does not.

## Bring your own images

Labtris redistributes no vendor software. Docker images come from whichever
registry you point at; QEMU images are yours to obtain, and the catalogue
records where each one comes from. Anything under a vendor licence stays a
matter between you and that vendor.

## Documentation

**[docs.labtris.com](https://docs.labtris.com)** — the handbook,
searchable, dark-mode, deep-linkable. Ten pages written for someone who
has just arrived: install, a first lab in five minutes, images,
networking, consoles, capture, organising labs, users, the API, and
running the server.

The Markdown that renders the site lives in [`docs/`](docs/), and the
same source builds a single PDF with `make handbook`.

Under `docs/` you will also find:

| Path | Contents |
|---|---|
| [`docs/*.mdx`](docs/) | The handbook chapters — the site is built from these |
| [`docs/api/`](docs/api/) | REST reference and the MCP surface |
| [`docs/reference/`](docs/reference/) | The from-source install path and the ISO build, at length |
| [`docs/design/`](docs/design/) | Findings, features, architecture, scaling, the phase-1 spec |

The design notes read like engineering notes because that is what they
are; the numbers in `scaling` and `findings` came off a live host,
`architecture` is design intent that mostly survived contact with code.

## Layout

```
labtris_api/     the HTTP API, the runtime backends, the lifecycle
labtris_netd/    the only process that touches netlink and nftables, runs as root
labtris_mcp/     the MCP server and the tool definitions
web/             the Svelte interface
migrations/      Alembic schema history
packaging/       the installer, the ISO build, the systemd units
tests/           306 tests: unit, plus acceptance against a real Postgres
```

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
