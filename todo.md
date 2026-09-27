# todo — labtris core

Living roadmap. Delete items as they land in `git log`.

Sibling roadmaps:
- **labtris-aws** (private) — 148 AWS services: 24 verified real, ~24 persistent-state, ~99 façade
- **labtris-network-skills** — assistant content: protocol rules, fault playbooks, device command sets, prompts. MIT; started 2026-09-27
- `labtris-azure`, `labtris-gcp`, `labtris-oci`, `labtris-saas` — skeletons only

---

## Assistant parity with GNS3 Copilot

GNS3 3.1 ships an AI copilot and an MCP service. Surveyed on 2026-09-27
against the `3.1` branch of `GNS3/gns3-server` and
`docs.gns3.com/docs-3.1-en`, not the changelog. The honest position: they
have **~50 MCP tool handlers** to our 46, and nine copilot features
documented as implemented. Competing on "we have an assistant" is a
losing position — they have more people on it. What follows is what would
have to be true to stop being *behind*, which is a different and smaller
goal than being ahead.

Their MCP is weighted to managing a lab **as a document** — projects
(14 handlers), symbols (6), drawings (5), images (5), templates (5),
snapshots (4). Ours is weighted to operating a **running** lab —
`console_exec`, `node_logs`, `capture_read`, `link_stats`, `impair_link`,
the `vnc_*` family, `lab_generate`, `lab_snapshot`. That is a real
difference in shape today and not a moat; it is where each project spent
the last six months.

Ordered by what we would actually gain, not by their list order.

- [ ] **Skills repository.** Theirs loads skills, prompts and security
      config from an external git repo at startup, so content updates
      without redeploying the server. We have `SYSTEM` as a hardcoded
      constant in `agent.py` — a user cannot change the prompt at all.
      This is the one to do first: it is the cheapest, it unblocks the
      next two, and it is the thing already wanted under the "NetworkLLM
      / configurable system prompts" heading.

      Looked at what is actually in `GNS3/gns3-skills` (142 files,
      last touched 2026-09-15, **GPL-3.0**):

      | Directory | Files | What they are |
      |---|---|---|
      | `packet_analysis/` | 61 | one YAML per protocol — bgp, ospf, arp, bfd, dhcp… |
      | `injection/` | 51 | one YAML per fault family — bgp_issues, dhcp_snooping_dai_issues… |
      | `device/` | 11 | per-vendor command sets — cisco_iol, cisco_xr, plus bgp/ospf |
      | `prompts/` | 4 | lab_automation_assistant, teaching_assistant, troubleshooting_injection |
      | `config/` | 1 | the security configuration |

      A protocol skill is small and declarative — `bgp.yaml` is a name, a
      `display_filter`, and a list of `tshark_field` entries each with a
      label and a description. That is the whole mechanism: the model gets
      told which fields matter for a protocol and how to filter for them.
      Nothing clever, and it works because the content is curated rather
      than because the loader is.

      Two consequences for us. The format is worth copying and the content
      is **not ours to take** — GPL-3.0 against our MIT core and a
      commercial cloud half, so a Labtris skills repo starts empty and
      gets written, or points at theirs as an optional external source and
      keeps the licences apart. And the shape argues for doing this first:
      112 of their 142 files are packet-analysis and injection rules, so
      the skills loader *is* most of those two items rather than a
      prerequisite to them.

- [ ] **Protocol-oriented packet analysis.** Theirs stores tshark fields,
      display filters and check rules as YAML in the skills repo, then has
      the assistant diagnose a live capture against them. We have
      `capture_start` / `capture_read` returning raw pcap and a Wireshark
      GUI — the bytes, with no interpretation. The gap is the rule layer,
      not the capture, and it lands naturally on top of the skills repo.

- [ ] **Fault injection as a workflow.** Theirs analyses the topology,
      picks a fault appropriate to the protocols in use, injects it, and
      writes up what it did — for troubleshooting practice. We have every
      primitive (`impair_link` per direction, `iface_set_state`,
      `stop_node`, P4 program swap) and none of the workflow. Worth having
      for the teaching case, and our primitives are richer than theirs
      once it exists.

- [ ] **Context-window management.** Theirs counts tokens with tiktoken,
      offers conservative/balanced/aggressive trimming, and injects
      current topology into the system prompt before each call. We pass
      history untrimmed, so a long session eventually fails against a
      small-context model. The topology injection is the more interesting
      half: it is why their assistant knows the lab without spending a
      tool call on it.

- [ ] **Command security.** Theirs layers checks to stop the model running
      commands that damage the lab. We have `CONFIRM_TOOLS` — destructive
      *tools* need confirmation — but nothing inspects what goes into
      `console_exec`, which is a shell. An allow/deny layer on command
      content, configurable per instance, is the gap.

- [ ] **Per-user and per-group model config.** Theirs is an API with
      inheritance. Ours is two global settings, `llm_base_url` and
      `llm_api_key`. On a shared instance that means one key for everyone,
      which is wrong for a teaching box.

- [ ] **Multi-vendor device drivers.** Theirs uses Netmiko and Nornir with
      per-vendor drivers and device-type detection, so the assistant can
      configure a Huawei box without being told how. We have raw
      `console_exec` and interface-name schemes. This is the largest of
      the nine and the least urgent for us: it pays off against a catalogue
      of vendor NOSes, which is exactly the area we already say is our gap.

Already covered, listed so nobody re-derives it: **node-control tools**
(`add_node`, `start_node`, `stop_node`, `connect_nodes`) and a **chat
API** (the pane, plus `/api/v1/labs/{id}/ai/ws`).

### Later: emit real PFC PAUSE frames

- [ ] **802.1Qbb PAUSE emission.** We ship piece one of PFC — eight
      strict-priority bands with per-class RED and ECN marking, which is
      what makes RoCEv2 usable. We do not ship piece two: emitting a
      MAC-control PAUSE frame (opcode 0x0101) when a class crosses a
      threshold, which is what makes "lossless" literally true. See
      `docs/design/pfc-implementation.mdx`; it needs an XDP program on the
      tap tracking per-priority depth in a bpf_map, or OVS, which we now
      have as a network kind and did not when that note was written.

      Not urgent. ECN is the signal every congestion-control algorithm we
      care about actually reads — DCTCP, DCQCN, UET — and explicit
      backpressure changes the shape of the lesson rather than enabling
      it. Worth doing when someone wants to *see* the PAUSE frames rather
      than infer the mechanism.

      Reading them already works: Wireshark dissects MAC Control as
      `macc`, with `macc.cbfc.pause_time.c0`..`c7` per priority, and
      `labtris-network-skills/packet_analysis/pfc.yaml` is written against
      those fields. So the day we emit them, the analysis side is done.

### What not to chase

The assistant is not where this product wins and should not be the
second section on the home page. Nothing in the nine above touches the
two things neither GNS3 nor containerlab nor CML has any answer to: an
agent that can build and test an **application** against 24 real AWS
services on the same box, and the AI-fabric layer underneath it — P4
swapped under a running lab, RoCE, Ultra Ethernet on the wire. Parity
work is defensive. Spend the rest there.

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
      `aws` CLI (labtris-aws) appear in the labtris canvas as
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
      from labtris-aws compiles to real VyOS config on the in-path
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

## Labtris family (sibling plugins)

Each is a separate repo under `github.com/labtris/`:

- [ ] **labtris-azure** — ARM API surface, at least Storage/Cosmos/AKS
- [ ] **labtris-gcp** — Compute/Storage/BigQuery/GKE
- [ ] **labtris-oci** — Compute/Object Storage
- [ ] **labtris-saas** — wrap [WonderTwin](https://github.com/WonderTwin-AI/wondertwin)
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
