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
            notes="Extra caps SYS_ADMIN, NET_BIND_SERVICE, SYS_NICE "
                  "on top of the base NET_ADMIN + NET_RAW. Zebra will "
                  "not come up otherwise.",
        ),
        ContainerImage(id="haproxy", label="HAProxy", image="haproxy:alpine"),
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
