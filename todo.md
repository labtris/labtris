# todo — labtris core

Living roadmap. Delete items as they land in `git log`.

Sibling roadmaps:
- **[labstack-aws/TODO.md](https://github.com/labtris/labstack-aws/blob/master/TODO.md)** — 141 AWS services (12 verified real, 6 real-backing, ~18 persistent, ~99 façade)
- `labstack-azure`, `labstack-gcp`, `labstack-oci`, `labstack-saas` — skeletons only

---

## In flight (uncommitted, on this checkout)

The `git status` on this branch has a full Phase K1/K2/K3 landing sitting
un-pushed. Split into three focused PRs before merging:

- [ ] **PR-A · Phase K1 SSH proxy** — asyncssh server, JWT-as-password + pubkey auth
  - `labtris_api/ssh_proxy.py`, `labtris_api/routers/ssh_endpoints.py`,
    `labtris_api/routers/ssh_keys.py`, `labtris_api/models.py` (+SshKey),
    `migrations/versions/0020_ssh_keys.py`, `tests/unit/test_ssh_proxy.py`,
    `docs/vendor-cli-ssh-proxy.mdx`
  - Verify on `.77`: `sshpass -p $JWT ssh -p 2222 <node>@10.124.133.77`
- [ ] **PR-B · Phase K2 aws-CLI-shape** — profiles, `--output`, `--query`
  - `labtris_cli/profiles.py`, `labtris_cli/query.py`, `labtris_cli/format.py`,
    `labtris_cli/__main__.py`, `labtris_cli/auth.py`, `labtris_cli/node.py`,
    `docs/cli-profiles.mdx`
  - Verify: `labtris --profile home nodes list --output json --query '[?state==\`running\`].name'`
- [ ] **PR-C · Phase K3 RESTCONF** — `/restconf` sub-app, `ietf-interfaces` model
  - `labtris_api/restconf/`, `labtris_api/main.py`, `labtris_api/config.py`,
    `docs/restconf-api.mdx`
  - Verify: `curl -H 'accept: application/yang-data+json' -H "authorization: Bearer $TOK" .../restconf/data/ietf-interfaces:interfaces-state/interface=Gi0-0`
- [ ] **PR-D · Plugin loader** — `labtris_api/plugins.py`, entry-point discovery
  - Verify: `pip install labstack-aws` → api restart → `_aws/` mount works
- [ ] **PR-E · Docs** — the 4 `.mdx` files + `docs/mint.json` menu entries

---

## Recently landed

- [x] **Docker-pull for labtris core** (`a8e020a`, 2026-09-26)
  - `Dockerfile`, `docker-compose.yml`, `packaging/docker/entrypoint.sh`,
    `.github/workflows/docker-image.yml`
  - `ghcr.io/labtris/labtris:latest` publishes on push to main
  - **Follow-up:** flip GHCR package visibility to public (org owner action)
  - **Follow-up:** multi-arch (add `linux/arm64` to buildx) — someone needs to ask first
- [x] **rdma-host build-time image** (`3645159`)
- [x] **Palette Docker Pull-now progress** (`6229a64`)
- [x] **Ultra Ethernet suite** (`0ecf5f4`)
- [x] **MCP setup docs** (`066c9c5`)

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
      runner, or Firecracker-in-CI, or just document "manually verified on .77"
- [ ] **Migration round-trip test** — every `alembic upgrade` also has
      a working `downgrade`. Some don't today.

---

## Docs

- [ ] **"Migrate from GNS3" walkthrough** — the single biggest user
      acquisition vector; every network engineer has a GNS3 install
- [ ] **"Migrate from EVE-NG" walkthrough** — same
- [ ] **"LocalStack side-by-side" page** — head-to-head with LocalStack
      showing what runs, what doesn't
