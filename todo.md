# todo — labtris core

Living roadmap. Delete items as they land in `git log`.

Sibling roadmaps:
- **[labstack-aws/TODO.md](https://github.com/labtris/labstack-aws/blob/master/TODO.md)** — 141 AWS services (12 verified real, 6 real-backing, ~18 persistent, ~99 façade)
- `labstack-azure`, `labstack-gcp`, `labstack-oci`, `labstack-saas` — skeletons only

---

## In flight

Nothing uncommitted. Phase K1/K2/K3 landed as a single commit
(`3f6e33e`) rather than the five PRs this section used to propose
splitting it into.

Phase K has now been driven end-to-end on a real 0.11.0 instance. All
three surfaces work; what follows is what the verification turned up.

- [ ] **`--query` is global-only, `--output` is not.** `labtris --query
      ... node list` works; `labtris node list --query ...` fails with
      "No such option", because `--query` lives on the root callback
      while `node list` has its own `-o/--output`. Anyone with aws-cli
      habits writes the second form. Either accept `--query` per-command
      or reject `--output` there too — the asymmetry is the problem.
- [ ] **`/restconf` without a trailing slash 404s.** `/restconf/` is
      fine. FastAPI would normally 307 between them; worth a redirect so
      a hand-typed URL works.
- [ ] **SSH proxy rejects `exec` requests.** `ssh <node>@host 'cmd'`
      fails with "exec request failed on channel 0"; only an interactive
      shell works. Reasonable for a console bridge, but it should say so
      on the channel rather than failing opaquely, and
      `docs/vendor-cli-ssh-proxy.mdx` should state it.
- [ ] **SSH proxy startup failure is silent.** `main.py` wraps
      `ssh_proxy.start()` in a bare `except Exception: pass`. Keeping the
      API up is right; swallowing the reason is not — log it.
- [ ] **Pubkey auth is still unverified.** Only JWT-as-password was
      exercised. `labtris_cli/ssh_keys.py` and the `SshKey` model need
      their own pass.

---

## Recently landed

- [x] **Phase K verified** — SSH proxy: JWT-as-password authenticates and
      lands a real shell (`root@leaf-1:/#`); a bad token, an empty
      password and a valid token for a non-existent node are all
      rejected. RESTCONF: `/restconf/` returns the `ietf-restconf` root
      and node-scoped `ietf-interfaces` data in proper YANG shape. CLI:
      JMESPath filtering and projection both work.
- [x] **Phase K** (`3f6e33e`) — SSH proxy (asyncssh + JWT/pubkey),
      aws-CLI-shape CLI (profiles, `--output`, `--query`), RESTCONF
      (`ietf-interfaces`), plugin loader. 29 files, ~3,050 lines.
- [x] **Canvas wire rendering** (`cc0390c`) — wires were clipped at the
      SVG origin, so a generated fabric with nodes at negative
      coordinates lost whole links; anchors also used a hardcoded 176px
      against a 150px card, putting every right-hand wire 26px clear of
      its border. Width now lives once, as `--node-w`.
- [x] **`--with-bgp-evpn` actually converges** (`024a915`) — the
      generated config was correct and bgpd never started: the container
      entrypoint runs `frrinit.sh start` at boot with `bgpd=no`, and
      `start` against a half-running FRR is a no-op. Now restarts.
      Verified: a fresh spine-leaf converges in 10s.
- [x] **Cumulus Linux VX 5.10 in the catalog** (`26ff806`) — plus
      tar.gz extraction (Vagrant `.box` files) and an `swp` iface
      scheme. First real switch NOS in the catalog, and legally clean.
- [x] **`packaging/smoke-test.sh`** (`6308dc5`) — loads the demo pods,
      starts every node, installs the FRR config, waits for BGP EVPN to
      reach Established. Found both bugs above on its first real run.
- [x] **Wire-routing design note** (`f5af34b`) — why generated fabrics
      look tangled, the relevant literature, and the order to fix it in.
- [x] **Hosting docs corrected** (`1d2b9e7`) — labtris.com is a separate
      Next.js repo, nothing here deploys to it, and `get.sh` lives in
      both repos with nothing enforcing the match.
- [x] **Docker-pull for labtris core** (`a8e020a`, 2026-09-26)
  - `Dockerfile`, `docker-compose.yml`, `packaging/docker/entrypoint.sh`,
    `.github/workflows/docker-image.yml`
  - `ghcr.io/labtris/labtris:latest` publishes on push to main
  - **BLOCKED:** the GHCR package is private, so
    `docker pull ghcr.io/labtris/labtris` fails with `unauthorized` for
    everyone — confirmed against the live registry. The workflow has
    published successfully on every push; only visibility is missing.
    Needs an org owner: Packages → labtris → Change visibility → Public.
  - **Follow-up:** multi-arch (add `linux/arm64` to buildx) — someone needs to ask first
- [x] **rdma-host build-time image** (`3645159`)
- [x] **Palette Docker Pull-now progress** (`6229a64`)
- [x] **Ultra Ethernet suite** (`0ecf5f4`)
- [x] **MCP setup docs** (`066c9c5`)

---

## Known-bad

- [ ] **rail spine sometimes shows one peer, not two.** `spine-r2=1`
      where `spine-r1=2` on the same run. Both hosts in the rail are
      configured and the smoke test passes on >=1, so this is a
      convergence-timing question rather than a config one — worth a
      look before anyone quotes rail-optimised timings.

---

## Distribution / packaging

- [ ] **Helm chart** — Kubernetes deploys (`packaging/helm/labtris/`)
- [ ] **Homebrew formula** for `labtris` CLI on macOS
- [ ] **PyPI publish** — `twine upload dist/labtris-*.whl` so
      `pip install labtris` works out of the box (currently source-install only)
- [ ] **Signed releases** — GH release with SHA256SUMS + sigstore signature
- [ ] **Multi-arch docker image** — linux/arm64 alongside amd64

---

## Web UI

- [ ] **Cloud resources as canvas objects** — resources created via
      `aws` CLI (labstack-aws) appear in the labtris canvas as
      first-class objects, not just SDK endpoints. Blocks the "LocalStack
      but visual" pitch.
- [ ] **Console tabs per lab** — currently one tab per node; a lab-level
      grid of consoles matches how real network engineers work.
- [ ] **Packet capture pane in-browser** — dumpcap over WebSocket +
      wireshark-lite JS decoder. `tcpdump -w` download works today; the
      GUI story does not.

---

## Networking / runtime

- [ ] **VyOS firewall compiler** — `authorize-security-group-ingress`
      from labstack-aws compiles to real VyOS config on the in-path
      firewall node. Requires a `firewall` node kind (pluggable, VyOS
      as reference impl), NOT nftables at netd.
- [ ] **NETCONF over SSH subsystem** — bolt onto the K1 asyncssh server
      as a `netconf` subsystem handler; reuses the JWT + pubkey path
- [ ] **gNMI** — needs gRPC transport bootstrap; separate phase
- [ ] **BGP/OSPF looking-glass endpoint** — per-node `show ip bgp
      summary` shortcut for large multi-router labs
- [ ] **QoS presets** — currently HTB per-tap; add ready-made "wan-slow",
      "sat-link", "wifi-lossy" macros

---

## LabStack family (sibling plugins)

Each is a separate repo under `github.com/labtris/`:

- [ ] **labstack-azure** — ARM API surface, at least Storage/Cosmos/AKS
- [ ] **labstack-gcp** — Compute/Storage/BigQuery/GKE
- [ ] **labstack-oci** — Compute/Object Storage
- [ ] **labstack-saas** — wrap [WonderTwin](https://github.com/WonderTwin-AI/wondertwin)
      for Stripe/Twilio/GitHub/Slack twins

---

## Tests / CI

- [ ] **Docker image smoke test in CI** — `docker compose up -d && curl
      /api/v1/system/status` in the workflow (currently manual)
- [ ] **Integration tests against real KVM** — needs a runner with
      `/dev/kvm`; GitHub-hosted runners don't have it. Options: self-hosted
      runner, or Firecracker-in-CI, or just document "manually verified".
      `packaging/smoke-test.sh` is the test; what is missing is somewhere
      to run it. A nested-KVM guest works — verified on the Hetzner box.
- [ ] **Migration round-trip test** — every `alembic upgrade` also has
      a working `downgrade`. Some don't today.

---

## Docs

- [ ] **"Migrate from GNS3" walkthrough** — the single biggest user
      acquisition vector; every network engineer has a GNS3 install
- [ ] **"Migrate from EVE-NG" walkthrough** — same
- [ ] **"LocalStack side-by-side" page** — head-to-head with LocalStack
      showing what runs, what doesn't
