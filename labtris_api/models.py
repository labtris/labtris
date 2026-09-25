from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, ENUM, JSONB, MACADDR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


node_state_enum = ENUM(
    "defined",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
    name="node_state",
    create_type=False,
)
network_kind_enum = ENUM(
    "bridge", "cloud", "vxlan", "nat", name="network_kind", create_type=False
)
runtime_kind_enum = ENUM(
    "docker",
    "qemu",
    "containerlab",
    "iol",
    "dynamips",
    name="runtime_kind",
    create_type=False,
)


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Slash-separated path, "" for the root. Organisational only — nothing
    #: about how a lab runs depends on where it sits.
    folder: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The configset most recently applied, or None. Cleared as soon as any
    #: node's config is edited directly, because after that it would be a
    #: claim the lab can no longer support.
    active_configset: Mapped[str | None] = mapped_column(Text, nullable=True)
    configsets: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Ready hooks — YAML canonical form, parsed cache, per-run state. See
    #: labtris_api/runtime/hooks.py. All three nullable because "no hooks
    #: defined" and "hooks defined but never run" are both meaningful, and
    #: defaulting to `{}` would make the runner iterate empty every time
    #: any node in the lab changed state.
    hooks_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    hooks: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    hooks_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    nodes: Mapped[list[Node]] = relationship(
        back_populates="lab", cascade="all, delete-orphan", passive_deletes=True
    )
    networks: Mapped[list[Network]] = relationship(
        back_populates="lab", cascade="all, delete-orphan", passive_deletes=True
    )
    links: Mapped[list[Link]] = relationship(
        back_populates="lab", cascade="all, delete-orphan", passive_deletes=True
    )
    geometry: Mapped[Geometry | None] = relationship(
        back_populates="lab", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )
    # Nullable on purpose: labs created before users existed have no owner,
    # and inventing one would be a lie about who made them.
    owner_id: Mapped[str | None] = mapped_column(
        CHAR(26), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("lab_id", "name"),)

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    lab_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    runtime: Mapped[str] = mapped_column(runtime_kind_enum, nullable=False)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(node_state_enum, nullable=False, default="defined")
    cpu_limit: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    ram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    env: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    cmd: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    runtime_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    style: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Remote-console settings for this node, passed through to guacd: the
    # target a VNC/RDP tunnel should dial and the credentials to use. Empty
    # for a QEMU node, which gets its VNC display from the hypervisor.
    console: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Which emulated NIC QEMU gives this guest. Null takes the image's default.
    nic_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the machine itself is built, as opposed to what runs inside it:
    # chipset, acceleration, boot order, whether it gets a data volume. Kept
    # on the node rather than in the VM directory so it survives a wipe and
    # travels with an export.
    qemu_opts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Runtime-agnostic per-node options bag. First use: P4 program
    #: selection for a bmv2 switch (`{"p4_program": "ecmp"}`). Also the
    #: right home for future per-node knobs that are not qemu-specific
    #: (a container node's hugepage size, a RDMA-capable node's rxe
    #: interface name, custom kernel modules to load at boot). Kept
    #: nullable rather than defaulting to `{}` because a NULL means
    #: "no per-node opts" and lets a code path skip the read entirely.
    opts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    startup_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def iface_scheme(self) -> str:
        """What this node's guest calls its own ports — see naming.IFACE_SCHEMES.

        Derived rather than stored: it is a property of the image, so storing a
        copy on the node would go stale the moment the catalog learned better.
        The import is local because the catalogs sit above this module."""
        from labtris_api.lifecycle import iface_scheme_for

        return iface_scheme_for(self.runtime, self.image)

    lab: Mapped[Lab] = relationship(back_populates="nodes")
    interfaces: Mapped[list[Interface]] = relationship(
        back_populates="node", cascade="all, delete-orphan", order_by="Interface.idx"
    )


class Network(Base):
    __tablename__ = "networks"
    __table_args__ = (
        UniqueConstraint("lab_id", "name"),
        CheckConstraint("(kind = 'cloud') = (cloud_ref IS NOT NULL)", name="cloud_ref_ck"),
    )

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    lab_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(network_kind_enum, nullable=False, default="bridge")
    # Not unique any more: two `cloud` networks may share the same host
    # bridge (netplan br0, EVE-NG's pnet0). Enforcement lives in the API
    # path, which allows sharing only when the target is an OS-owned bridge
    # and refuses NIC-sharing (kernel: one master per interface).
    host_ifname: Mapped[str | None] = mapped_column(Text, nullable=True)
    cloud_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    vni: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: NAT networks only: the segment, the address the host answers on, and
    #: the pool. dhcp_first being NULL is how a NAT network says it hands out
    #: nothing — the gateway still routes, guests just address themselves.
    subnet: Mapped[str | None] = mapped_column(Text, nullable=True)
    gateway: Mapped[str | None] = mapped_column(Text, nullable=True)
    dhcp_first: Mapped[str | None] = mapped_column(Text, nullable=True)
    dhcp_last: Mapped[str | None] = mapped_column(Text, nullable=True)
    dns_server: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: A VLAN-filtering bridge is a switch; a plain one forwards tags without
    #: understanding them, which looks like a working trunk and is not one.
    vlan_aware: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vlan_proto: Mapped[str] = mapped_column(Text, nullable=False, default="802.1Q")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lab: Mapped[Lab] = relationship(back_populates="networks")
    interfaces: Mapped[list[Interface]] = relationship(back_populates="network")
    hosts: Mapped[list[NetworkHost]] = relationship(
        back_populates="network", cascade="all, delete-orphan"
    )


class Interface(Base):
    __tablename__ = "interfaces"
    __table_args__ = (UniqueConstraint("node_id", "idx"), UniqueConstraint("node_id", "name"))

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    node_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("nodes.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    mac: Mapped[str] = mapped_column(MACADDR, nullable=False)
    host_ifname: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    #: On a VLAN-filtering bridge: "access" with a vlan_id, or "trunk" with a
    #: comma-separated allowed list. NULL means the port is untouched, which on
    #: a plain bridge is the only sensible thing it can be.
    #: A DHCP reservation on this port's NAT network. dnsmasq keys these on
    #: the MAC, which the registry already assigned, so nothing new is needed
    #: to identify the guest.
    reserved_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    vlan_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    vlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trunk_vids: Mapped[str | None] = mapped_column(Text, nullable=True)
    network_id: Mapped[str | None] = mapped_column(
        CHAR(26), ForeignKey("networks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    node: Mapped[Node] = relationship(back_populates="interfaces")
    network: Mapped[Network | None] = relationship(back_populates="interfaces")


class LinkGroup(Base):
    """N parallel Links between the same node pair, grouped for ECMP.

    Each member Link is still a normal two-endpoint p2p wire; the group
    is metadata that says 'treat these together'. The runtime does not
    itself install a multipath route across group members — a guest's
    own control plane (usually FRR + `bgp bestpath as-path multipath-
    relax`) does the balancing across the parallel uplinks. The group
    exists so a lab spec can say 'give me 4x25G between spine-1 and
    leaf-1' declaratively, and so the canvas can render the parallel
    wires as a bundle instead of overlapping single Beziers.
    """

    __tablename__ = "link_groups"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    lab_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Hint for future runtime ECMP wiring. `layer3+4` matches Linux
    #: bond xmit_hash_policy for per-flow ECMP; `per_packet` reserves
    #: a future round-robin mode. Not enforced today; guests do their
    #: own ECMP via routing.
    hash_policy: Mapped[str] = mapped_column(Text, nullable=False, default="layer3+4")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lab: Mapped[Lab] = relationship()


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (CheckConstraint("a_iface_id <> b_iface_id", name="link_ends_distinct"),)

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    lab_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"))
    # UNIQUE was dropped in 0019_link_groups so parallel Links between
    # the same pair (grouped for ECMP) become possible. The check that
    # ungrouped Links stay unique per-interface moves to the API layer
    # in routers/links.create_link — dropping a DB constraint here is
    # deliberate; enforcing per-interface uniqueness in DDL blocks the
    # whole LinkGroup use case for one edge case a router can catch.
    a_iface_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("interfaces.id", ondelete="CASCADE")
    )
    b_iface_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("interfaces.id", ondelete="CASCADE")
    )
    network_id: Mapped[str] = mapped_column(CHAR(26), ForeignKey("networks.id", ondelete="CASCADE"))
    #: Nullable FK to link_groups — a NULL means this is a normal
    #: standalone Link (the shape before 0.9.0).
    group_id: Mapped[str | None] = mapped_column(
        CHAR(26), ForeignKey("link_groups.id", ondelete="CASCADE"), nullable=True
    )
    impair_ab: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    impair_ba: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    admin_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lab: Mapped[Lab] = relationship(back_populates="links")
    a_iface: Mapped[Interface] = relationship(foreign_keys=[a_iface_id])
    b_iface: Mapped[Interface] = relationship(foreign_keys=[b_iface_id])
    network: Mapped[Network] = relationship()
    group: Mapped[LinkGroup | None] = relationship()


class Geometry(Base):
    __tablename__ = "geometry"

    lab_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lab: Mapped[Lab] = relationship(back_populates="geometry")


class IfnameRegistry(Base):
    __tablename__ = "ifname_registry"

    host_ifname: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(CHAR(26), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """Someone who uses this instance.

    Roles are deliberately two: admin and user. Anything finer is a guess
    about how a team works, and a lab tool that makes you model permissions
    before you can build a topology has already lost."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SshKey(Base):
    """OpenSSH-format authorized pubkey for one Labtris user (Phase K1b).

    Checked by labtris_api.ssh_proxy when a client presents a pubkey. The
    fingerprint column is indexed so the auth callback does one row lookup
    per connection, not a full-table scan-and-parse. Unique fingerprint
    also blocks the "someone imported my public key under their account"
    silent-shadow scenario.
    """

    __tablename__ = "ssh_keys"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        CHAR(26),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    key_body: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MacRegistry(Base):
    """Every MAC this instance has handed out.

    A random 46-bit address collides rarely enough to feel safe and often
    enough to ruin a lab when it happens — two NICs on one segment answering
    to the same address is a fault nobody thinks to look for. Reserving makes
    the collision impossible rather than unlikely, and gives deletion
    something to give back."""

    __tablename__ = "mac_registry"

    mac: Mapped[str] = mapped_column(MACADDR, primary_key=True)
    owner_id: Mapped[str] = mapped_column(CHAR(26), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Host(Base):
    """A registered netd endpoint — local or remote. F6's multi-host control
    plane: the API drives each host's netd the same way, over unix (local) or
    TCP+token (remote)."""

    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    underlay_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NetworkHost(Base):
    """One VXLAN endpoint of a `vxlan`-kind Network on one Host."""

    __tablename__ = "network_hosts"

    network_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("networks.id", ondelete="CASCADE"), primary_key=True
    )
    host_id: Mapped[str] = mapped_column(
        CHAR(26), ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True
    )
    vxlan_ifname: Mapped[str | None] = mapped_column(Text, nullable=True)

    network: Mapped[Network] = relationship(back_populates="hosts")
    host: Mapped[Host] = relationship()


class Template(Base):
    """A reusable node template — EVE-NG's 'export node -> template', minus the
    190-file .yml catalog: any node can be saved as one and it feeds the catalog."""

    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    runtime: Mapped[str] = mapped_column(runtime_kind_enum, nullable=False, default="docker")
    image: Mapped[str] = mapped_column(Text, nullable=False)
    cmd: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    env: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    icon: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: How the machine is built, for runtimes where the image reference is not
    #: the whole story. Docker leaves this empty — a reference names immutable
    #: content. A QEMU template saved from a configured node keeps the sizing
    #: and device facts the base catalog would otherwise have supplied, because
    #: its image is now a flattened disk with no catalog entry behind it.
    spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Task(Base):
    """Async job with progress — EVE-NG's `task`/`tasks` queue."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    lab_id: Mapped[str | None] = mapped_column(
        CHAR(26), ForeignKey("labs.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Setting(Base):
    """A runtime override for one config key.

    Env vars stay the floor — this is a layer on top, so a deployment can pin
    something with LABTRIS_* and the UI cannot quietly undo it."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Feedback(Base):
    """One "this is broken" pinned to a spot in the UI.

    The note is what the user typed; `context` is everything that makes it
    reproducible without a conversation — which element, which theme, which
    lab, and what the browser had already logged."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(CHAR(26), primary_key=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
