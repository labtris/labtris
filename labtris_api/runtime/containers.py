"""The container image catalog.

Docker nodes accept any image reference, and most images need nothing beyond
the two capabilities every node gets. Vendor network operating systems are the
exception: they expect to boot on a real machine, so they mount ramdisks, raise
ulimits and write sysctls before their first process starts.

Granting that to every container would hand host root to anyone who can create
a node. Instead the demands are declared here, per image, and an entry is only
reachable by editing this file — which means shipping code, not clicking a
button. Anything absent from the catalog runs with the unprivileged default.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every docker node gets these. They are what makes a node a network device:
#: NET_ADMIN configures interfaces and routes, NET_RAW opens raw sockets so
#: ping and tcpdump work inside the guest. Neither escapes the container.
BASE_CAPS: tuple[str, ...] = ("NET_ADMIN", "NET_RAW")


@dataclass(frozen=True)
class ContainerImage:
    id: str
    label: str
    image: str
    cmd: list[str] | None = None
    #: Vendor NOSes assume uid 0 and fail in confusing ways without it.
    user: str | None = None
    #: Full host privilege. Set this only for an image that has been observed
    #: to fail without it — see `notes` for what was tried. A privileged
    #: container can reach the host kernel, so `create_node` refuses these for
    #: anyone who is not an admin.
    privileged: bool = False
    #: Extra capabilities on top of BASE_CAPS, for images that need more than
    #: the default but less than everything.
    cap_add: tuple[str, ...] = ()
    source: str = "Docker Hub"
    credentials: str | None = None
    #: Why this image is configured the way it is. Surfaced in the palette so
    #: whoever picks a privileged image can see what they are agreeing to.
    notes: str = ""
    #: Rough time to a usable CLI. Vendor NOSes are minutes, not seconds, and
    #: a node that looks hung for 90s is a support question worth pre-empting.
    boot_seconds: int = 0
    #: What this image calls its own ports. Containers get eth0 from the
    #: netns; a NOS names them its own way. See naming.IFACE_SCHEMES.
    iface_scheme: str = "eth"
    #: Per-instance bind mounts, each `(subdir, container_path)`. The
    #: runtime resolves `subdir` under
    #: `~/.local/share/labtris/node-mounts/<node_id>/<subdir>/` (creating
    #: it if absent) and mounts it at `container_path`. First user: the
    #: bmv2 P4 switch mounts a per-node `/p4` dir carrying `prog.p4`.
    #: Anything else with per-instance user files (a NOS with a startup-
    #: config file baked in, a Wireshark node with dissectors) hangs off
    #: this same mechanism.
    per_node_mounts: tuple[tuple[str, str], ...] = ()

    #: Host paths bind-mounted straight through, each `(host_path,
    #: container_path)`. Unlike per_node_mounts these are shared, not
    #: per-node, and are skipped silently when the host path is absent.
    #:
    #: Only user is /dev/infiniband, which soft-RoCE needs: libibverbs
    #: opens /dev/infiniband/uverbsN to reach a device, and a container
    #: gets a private /dev that never contains it. A bind of the whole
    #: directory rather than a Docker `--device` per node, because the
    #: uverbs node for a given rxe device does not exist until that
    #: device is created — which happens after the container is running.
    host_mounts: tuple[tuple[str, str], ...] = ()
    #: cgroup device rules needed to actually use the above, as
    #: `(type, major, minor, perms)`. A bind mount makes the node visible;
    #: the device cgroup still has to permit opening it.
    device_cgroup_rules: tuple[str, ...] = ()


CONTAINER_CATALOG: dict[str, ContainerImage] = {
    image.id: image
    for image in [
        ContainerImage(
            id="alpine",
            label="Alpine Linux",
            image="alpine:3.20",
            cmd=["sleep", "3600"],
        ),
        ContainerImage(id="nginx", label="Nginx", image="nginx:alpine"),
        ContainerImage(id="redis", label="Redis", image="redis:alpine"),
        ContainerImage(
            id="frr",
            label="FRRouting",
            image="frrouting/frr:v8.4.0",
            # Zebra refuses to start without CAP_SYS_ADMIN — it wants
            # mount namespaces for VRFs, netlink features NET_ADMIN
            # alone does not cover, and unconstrained interface renames.
            # NET_BIND_SERVICE is what lets BGP take port 179 without
            # running as root; SYS_NICE bumps thread priority for
            # rt-affine daemons. This is what FRR's own docker
            # instructions list as the minimum non-privileged set —
            # short of `--privileged`, it's the same shape.
            cap_add=("SYS_ADMIN", "NET_BIND_SERVICE", "SYS_NICE"),
            # Default PID 1 in this image is `watchfrr $(daemon_list)`
            # — a single-process container the way Docker likes them,
            # but it means `frrinit.sh restart` inside the container
            # stops PID 1 and the container dies. The assistant hit
            # this trying to enable `pimd` in /etc/frr/daemons.
            #
            # Two ways to reload daemons without dying:
            #   1. Non-destructive: edit /etc/frr/daemons, then
            #      `pkill -HUP watchfrr` — watchfrr re-reads the
            #      daemon list and starts/stops accordingly, without
            #      restarting itself. Fastest, no traffic drop.
            #   2. Destructive: `frrinit.sh restart` — stops watchfrr
            #      then starts it. Fine if a bash-supervised PID 1
            #      outlives the stop.
            #
            # We take (2)'s safety net so both patterns work: run frr
            # via frrinit.sh, then hand PID 1 to bash blocked on
            # `tail -f /dev/null` (the docker-compose idiom for
            # "sidecar processes only, keep the container alive").
            # The daemon reload guidance stays in `notes` for the
            # assistant to prefer (1) when possible.
            # chown before start, because a seeded /etc/frr arrives owned by
            # root: NodeSpec.seed_files writes a plain tar into the created
            # container and has no business knowing that this image wants
            # uid 100. FRR reads world-readable files either way, but `write
            # memory` and vtysh's own history need the ownership the image
            # ships. A no-op when nothing was seeded.
            cmd=[
                "/bin/bash", "-c",
                "chown -R frr:frr /etc/frr 2>/dev/null || true; "
                "/usr/lib/frr/frrinit.sh start && exec tail -f /dev/null",
            ],
            notes="Extra caps SYS_ADMIN, NET_BIND_SERVICE, SYS_NICE on "
                  "top of the base NET_ADMIN + NET_RAW — Zebra will "
                  "not come up otherwise. To reload daemons after an "
                  "/etc/frr/daemons edit prefer `pkill -HUP watchfrr` "
                  "(no traffic drop, no PID 1 churn); `frrinit.sh "
                  "restart` also works because PID 1 is a bash "
                  "supervisor, but drops the routing plane briefly.",
        ),
        ContainerImage(id="haproxy", label="HAProxy", image="haproxy:alpine"),
        ContainerImage(
            id="freeradius",
            label="FreeRADIUS",
            image="freeradius/freeradius-server:latest",
            source="Docker Hub (freeradius/freeradius-server)",
            notes=(
                "FreeRADIUS AAA server. Listens on UDP 1812 (auth) + "
                "1813 (accounting). Ships with a default clients.conf "
                "accepting localhost with secret 'testing123'; edit "
                "/etc/raddb/clients.conf inside the container to add "
                "your NAS. Useful for network-device auth labs, "
                "802.1X / WPA2-Enterprise experiments, and any lab "
                "that wants a real RADIUS server rather than a stub."
            ),
            boot_seconds=2,
        ),
        ContainerImage(
            id="ubuntu",
            label="Ubuntu",
            image="ubuntu:24.04",
            cmd=["sleep", "infinity"],
        ),
        ContainerImage(
            id="busybox",
            label="BusyBox",
            image="busybox:1.36",
            cmd=["sleep", "3600"],
        ),
        ContainerImage(id="coredns", label="CoreDNS", image="coredns/coredns"),
        # The first vendor NOS here, and the only one that needs no account:
        # Nokia publish it on a public registry under their own EULA, so it
        # can be pulled on demand like any other image.
        #
        # It is privileged because it has to be. Observed on 26.7.2: with
        # NET_ADMIN, NET_RAW, SYS_ADMIN and SYS_RESOURCE — with seccomp and
        # apparmor both unconfined — it still exits, because sr_linux writes
        # net.ipv4.ip_local_port_range and fs.pipe-max-size during boot and
        # docker keeps /proc/sys read-only for every unprivileged container.
        # containerlab runs it privileged for the same reason.
        ContainerImage(
            id="ue-stack",
            label="ue-stack (real userspace UET, PoC)",
            image="labtris/ue-stack:latest",
            source=(
                "Local build — see packaging/dockerfiles/ue-stack/README.md "
                "(source in this repo's ue_stack/ package)"
            ),
            notes=(
                "The `uestack` PoC — real UET wire-format daemon + CLI, "
                "sending real UDP packets over the container's veth. "
                "IPDC (fire-and-forget) works end to end; TPDC (reliable "
                "with ACK+RTO) is a stub. Not spec-interop-ready; "
                "wire-format bit widths follow the UEC 1.0 shape but "
                "exact offsets are TODO. Pair with the ue-sim node "
                "kind (Kaima Lab's ns-3 simulator) for protocol "
                "validation and this one for real-packet labs."
            ),
            boot_seconds=3,
        ),
        ContainerImage(
            id="uet-ref",
            label="UET reference provider (UEC, real CC)",
            image="ghcr.io/labtris/uet-ref:latest",
            # Raw Ethernet sockets need NET_RAW, already in BASE_CAPS.
            # The XDP shim loads an eBPF program, which wants BPF and
            # SYS_ADMIN; `uet` (rawsock only) runs without them, so a
            # lab that only needs the rawsock path costs nothing extra.
            cap_add=("BPF", "SYS_ADMIN", "SYS_RESOURCE"),
            source=(
                "ghcr.io/labtris/uet-ref — built by CI from "
                "packaging/dockerfiles/uet-ref/ (upstream: "
                "github.com/ultraethernet/uet-ref-prov, MIT). Pulls like "
                "any other image; no local build needed."
            ),
            iface_scheme="eth",
            notes=(
                "The Ultra Ethernet Consortium's own reference "
                "implementation of UET, and the one to reach for: SES "
                "with tagged and untagged messages plus RMA read/write, "
                "PDS in two implementations, TSS with AES-GCM, all four "
                "delivery modes (RUD, ROD, RUDI, UUD), and a partial "
                "implementation of UET Network Signal Congestion "
                "Control. Real packets on the container's veth over "
                "protocol 253 — UET's own transport, so no port "
                "collision. `UET_IFNAME=eth0 uet server <ip>` on one "
                "node, `uet client <ip>` on another. Upstream's own "
                "gaps: multi-path delivery is not fully supported, "
                "there is no key exchange, and XDP still copies. The "
                "Wireshark dissector ships at /usr/share/uet/uet.lua — "
                "tshark has no built-in UET support without it."
            ),
            boot_seconds=3,
        ),
        ContainerImage(
            id="uet-htsim",
            label="UET congestion-control simulator (UEC htsim)",
            image="ghcr.io/labtris/uet-htsim:latest",
            source=(
                "ghcr.io/labtris/uet-htsim — built by CI from "
                "packaging/dockerfiles/uet-htsim/ (upstream: "
                "github.com/ultraethernet/uet-htsim, BSD-2-Clause, itself "
                "a fork of Broadcom/csg-htsim)"
            ),
            iface_scheme="eth",
            notes=(
                "The counterpart to uet-ref, not a replacement. uet-ref "
                "puts real UET frames on a real veth, so every sequence "
                "number and NACK code is a genuine protocol fact — but "
                "the link is the host's memory bandwidth, so a throughput "
                "figure from it describes the machine. htsim models "
                "packets instead: nothing touches the wire, but the link "
                "is 800 Gbps because you said so, and incast and fairness "
                "at that rate mean something. uet-ref answers 'is the "
                "protocol correct', this answers 'is the algorithm good'. "
                "Nine congestion-control algorithms to compare — NSCC "
                "(UEC's own, default), DCTCP, DCQCN, NDP, EQDS, RoCE, "
                "PFC, Swift, HPCC — over fat trees, default 3-tier with "
                "12us RTT. Examples in /opt/htsim/sim/datacenter."
            ),
            boot_seconds=3,
        ),
        ContainerImage(
            id="ue-sim",
            label="UE-Sim (Ultra Ethernet simulator on ns-3)",
            image="labtris/ue-sim:latest",
            cap_add=("SYS_ADMIN", "NET_BIND_SERVICE"),
            source=(
                "Local build — see packaging/dockerfiles/ue-sim/README.md "
                "(upstream: github.com/kaima2022/UE-Sim)"
            ),
            iface_scheme="eth",
            notes=(
                "Kaima Lab's UE-Sim — end-to-end Ultra Ethernet "
                "simulation on ns-3 3.44. Discrete-event simulator, "
                "not a real userspace UET stack; the simulated hosts "
                "run inside the container's ns-3 process rather than "
                "on real network interfaces. Use for protocol "
                "validation + CC-algorithm work; use `ue-stack` when "
                "the real userspace daemon lands. Image is a ~2 GB "
                "local build — see packaging/dockerfiles/ue-sim/ "
                "README.md."
            ),
            boot_seconds=3,
        ),
        ContainerImage(
            id="rdma-host",
            label="RDMA host (soft-RoCE + perftest)",
            # Local build: baking rdma-core+perftest at build time
            # removes the runtime dependency on internet inside the
            # container's netns. The old shape (ubuntu:noble + apt-
            # install in cmd) failed silently on every lab whose bridge
            # had no NAT — every user hit it. See
            # packaging/dockerfiles/rdma-host/README.md.
            image="ghcr.io/labtris/rdma-host:latest",
            source=(
                "Local build — see packaging/dockerfiles/rdma-host/"
                "README.md (Dockerfile in this repo)"
            ),
            # 231 is the infiniband_verbs char major (uverbsN), 10 is misc
            # (rdma_cm). Without both rules the bind mount is visible and
            # every open() returns EPERM.
            host_mounts=(("/dev/infiniband", "/dev/infiniband"),),
            device_cgroup_rules=("c 231:* rwm", "c 10:* rwm"),
            notes=(
                "Ubuntu + rdma-core + perftest, all baked in — starts "
                "in ~1 s with `ib_send_bw` / `rdma` on PATH. Host "
                "kernel must load `rdma_rxe` once first: "
                "`sudo modprobe rdma_rxe` on the labtris host, or "
                "`echo rdma_rxe | sudo tee /etc/modules-load.d/rdma_rxe.conf` "
                "for reboots. Closest working stand-in for Ultra "
                "Ethernet's userspace verbs today; the container's "
                "`rdma link add rxe0` is what makes verbs work over the "
                "labtris bridge."
            ),
            boot_seconds=2,
        ),
        ContainerImage(
            id="bmv2",
            label="P4 switch (bmv2)",
            # p4lang/p4c, not p4lang/behavioral-model. The latter ships
            # simple_switch, simple_switch_CLI and simple_switch_grpc and
            # NO compiler at all, so `p4c-bm2-ss` was "command not found"
            # on every start and the fallback below kept the container
            # alive with a dead switch — a node that reported `running`
            # and forwarded nothing. p4c carries both the compiler and
            # simple_switch.
            image="p4lang/p4c:latest",
            cap_add=("SYS_ADMIN", "NET_BIND_SERVICE"),
            cmd=[
                "/bin/bash",
                "-c",
                # Three things this has to get right, each of which was
                # wrong before:
                #
                # 1. Compile. p4c-bm2-ss turns prog.p4 into prog.json.
                # 2. Load it. The old line passed `--no-p4`, which tells
                #    simple_switch to start *without* a program — it
                #    logs "ignoring input config" and forwards nothing.
                # 3. Attach ports. Nothing ever passed `-i`, so the
                #    switch had no interfaces. Labtris hot-plugs the
                #    veths in after the container starts, so the ports
                #    cannot be named at exec time; wait for s1 to appear
                #    and build the list from what is actually there.
                #
                # `--enable-swap` is a *target* option, hence after `--`.
                # With it, simple_switch_CLI can load_new_config_file and
                # swap_configs at runtime.
                (
                    "cd /p4 && "
                    "{ p4c-bm2-ss -o prog.json prog.p4 || "
                    "  { echo 'P4 COMPILE FAILED — switch not started'; "
                    "    exec tail -f /dev/null; }; } && "
                    "for _ in $(seq 1 30); do "
                    "  [ -e /sys/class/net/s1 ] && break; sleep 1; done; "
                    "ARGS=''; "
                    "for f in $(ls /sys/class/net | grep -E '^s[0-9]+$' | sort -V); do "
                    "  ARGS=\"$ARGS -i ${f#s}@$f\"; done; "
                    "echo \"attaching ports:$ARGS\"; "
                    "exec simple_switch --log-console prog.json $ARGS -- --enable-swap"
                ),
            ],
            source="Docker Hub (p4lang/p4c — compiler and simple_switch)",
            iface_scheme="s",
            per_node_mounts=(("p4", "/p4"),),
            notes=(
                "P4 programmable data plane (bmv2 simple_switch). The "
                "container mounts a per-node directory at /p4 that "
                "carries prog.p4 — either one of the curated built-ins "
                "(basic_switch, ecmp, ecn, trim) or a file uploaded via "
                "POST /nodes/{id}/p4. p4c-bm2-ss compiles on start and "
                "the switch runs with --enable-swap, so a program can "
                "also be replaced at runtime through simple_switch_CLI "
                "without restarting the node. A compile failure leaves "
                "the container up with the switch stopped and the reason "
                "in the node log."
            ),
            boot_seconds=5,
        ),
        ContainerImage(
            id="srlinux",
            label="Nokia SR Linux",
            image="ghcr.io/nokia/srlinux:latest",
            cmd=["sudo", "bash", "-c", "/opt/srlinux/bin/sr_linux"],
            user="0",
            privileged=True,
            source="ghcr.io/nokia",
            credentials="admin / NokiaSrl1!",
            iface_scheme="srl",
            notes=(
                "Needs a writable /proc/sys to boot, which only full privilege "
                "grants — a privileged container can reach the host kernel, so "
                "only admins may place one. Emulates a 7220 IXR-D3L."
            ),
            boot_seconds=45,
        ),
    ]
}

#: Nodes carry an image reference, not a catalog id, so the runtime has to
#: match on the reference it is handed.
_BY_REFERENCE: dict[str, ContainerImage] = {i.image: i for i in CONTAINER_CATALOG.values()}


def profile_for(image: str) -> ContainerImage | None:
    """The catalog entry for an image reference, or None for anything BYO."""
    return _BY_REFERENCE.get(image)


def needs_privilege(image: str) -> bool:
    profile = profile_for(image)
    return profile is not None and profile.privileged
