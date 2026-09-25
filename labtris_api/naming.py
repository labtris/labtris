from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

#: Two shapes, both ours.
#:
#: The old one is a kind letter and eight base36 characters. The new one keeps
#: the kind letter, then says what the device is *for*, then a four-character
#: digest — `vub1-eth0-3f2a` rather than `vjyo3kffo`. Reading `ip link` or a
#: capture no longer means cross-referencing the database to find out which
#: node you are looking at.
#:
#: This pattern is a safety gate, not decoration: netd refuses to touch any
#: device whose name does not match, which is what stops it deleting docker0
#: or a host NIC. So the readable form still has to be unmistakably ours — the
#: mandatory `-xxxx` suffix does that. Docker's `veth48dff4c` has no dash and
#: `br-13b48a89612a` has twelve characters after its one, so neither can be
#: mistaken for a device this daemon owns.
IFNAME_RE = re.compile(r"^(t|b|v|x)([0-9a-z]{8}|[0-9a-z]{1,9}-[0-9a-z]{4})$")
_ALPH = "0123456789abcdefghijklmnopqrstuvwxyz"

#: Linux caps an interface name at 15 characters (IFNAMSIZ - 1), which is the
#: whole reason these are terse rather than descriptive.
IFNAME_MAX = 15
_SLUG_MAX = 9
_UNSAFE = re.compile(r"[^0-9a-z]+")


def slug(hint: str, limit: int = _SLUG_MAX) -> str:
    """A readable fragment: lowercase, alphanumeric, truncated from the end.

    Keeping the head is what makes a truncated name still recognisable — "core"
    survives from "core-router" where the tail would have left "outer"."""
    return _UNSAFE.sub("", (hint or "").lower())[:limit]


def device_hint(owner: str, part: str = "") -> str:
    """Compose "which thing" and "which port" inside the character budget.

    Concatenating and then truncating mangles both — core-rtr + eth0 became
    "rertreth0", which is less use than the hash it replaced. The port is short
    and fully distinguishing, so it is kept whole and the owner takes whatever
    room is left."""
    port = slug(part, 5)
    return slug(owner, max(1, _SLUG_MAX - len(port))) + port


def base36(n: int) -> str:
    if n < 0:
        raise ValueError("base36 of negative")
    if n == 0:
        return "0"
    chars: list[str] = []
    while n:
        n, r = divmod(n, 36)
        chars.append(_ALPH[r])
    return "".join(reversed(chars))


def host_ifname(
    kind: Literal["tap", "bridge", "veth", "vxlan"],
    owner_id: str,
    salt: int = 0,
    hint: str = "",
) -> str:
    """A host device name for one owner.

    Deterministic in (owner_id, salt), so a node that stops and starts gets the
    same devices back instead of drifting. `hint` only changes how it reads —
    uniqueness still comes from the digest, and the caller retries with a
    higher salt if the registry says the name is taken.
    """
    prefix = {"tap": "t", "bridge": "b", "veth": "v", "vxlan": "x"}[kind]
    digest = hashlib.blake2b(f"{owner_id}:{salt}".encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big")
    tail = base36(n).rjust(8, "0")
    label = slug(hint)
    name = f"{prefix}{label}-{tail[:4]}" if label else prefix + tail[:8]
    if not IFNAME_RE.match(name) or len(name) > IFNAME_MAX:
        raise ValueError(f"produced illegal ifname {name!r}")
    return name


#: What the *guest* calls its own ports, which is not something we choose — the
#: guest kernel does, from the PCI slot the NIC lands in. So this table is a
#: label, not an instruction: it tells someone reading the topology that the
#: second port of this Ubuntu VM will appear inside it as `ens4`, so they can
#: write a netplan file that matches. Getting it wrong costs an SSH session and
#: a puzzled ten minutes, which is exactly the tax this removes.
@dataclass(frozen=True)
class IfaceScheme:
    id: str
    label: str
    #: `{n}` is substituted with the guest-side number.
    template: str
    #: Where the guest starts counting. Containers start at 0; a QEMU guest's
    #: first virtio NIC lands in a slot systemd renders as ens3.
    start: int = 0
    #: How far apart consecutive ports are. Only VMware needs this, and it
    #: needs it badly: its NICs come out ens192, ens224, ens256.
    step: int = 1
    #: Fixed names for the first N ports, before the template kicks in. Some
    #: appliances name the first port differently from the rest (Palo Alto:
    #: mgmt / eth1/1, eth1/2… ; NX-OS: Mgmt0 / E1/1, E1/2…) — this is EVE's
    #: `eth_name` list. Rendered by literal position; the template's
    #: numbering starts at `start` from where the fixed list runs out, so a
    #: PAN-OS palette drop reads mgmt, eth1/1, eth1/2 rather than mgmt,
    #: eth1/2, eth1/3.
    first_names: tuple[str, ...] = ()

    def name(self, idx: int) -> str:
        if idx < len(self.first_names):
            return self.first_names[idx]
        template_idx = idx - len(self.first_names)
        return self.template.format(n=self.start + template_idx * self.step)


IFACE_SCHEMES: dict[str, IfaceScheme] = {
    s.id: s
    for s in [
        IfaceScheme("eth", "eth0, eth1 — containers and older Linux", "eth{n}"),
        IfaceScheme("ens", "ens3, ens4 — Ubuntu/Debian on QEMU virtio", "ens{n}", start=3),
        IfaceScheme(
            "enp", "enp0s3, enp0s4 — predictable names on some boards", "enp0s{n}", start=3
        ),
        # VMware's VMXNET3 adapters land 32 slots apart, so the numbers a
        # student sees are ens192, ens224, ens256 — not consecutive, and the
        # single most confusing thing about Ubuntu on ESXi.
        IfaceScheme("vmware", "ens192, ens224 — VMware VMXNET3", "ens{n}", start=192, step=32),
        IfaceScheme("srl", "ethernet-1/1 — Nokia SR Linux", "ethernet-1/{n}", start=1),
        IfaceScheme("ios", "GigabitEthernet0/0 — Cisco IOS", "GigabitEthernet0/{n}"),
        # Palo Alto: first port is the out-of-band management NIC; data
        # ports are eth1/1..eth1/N on the single dataplane. Matches EVE's
        # paloalto.yml (eth_name=["mgmt"], eth_format="eth1/{1}").
        IfaceScheme(
            "paloalto",
            "mgmt, eth1/1, eth1/2 — Palo Alto PAN-OS",
            "eth1/{n}",
            start=1,
            first_names=("mgmt",),
        ),
        # Cisco Nexus 9000v: management port is Mgmt0; data ports are
        # E1/1..E1/128. Matches EVE's nxosv9k.yml (eth_name=["Mgmt0"],
        # eth_format="E{1}/{1-128}").
        IfaceScheme(
            "nxos",
            "Mgmt0, E1/1, E1/2 — Cisco Nexus 9000v",
            "E1/{n}",
            start=1,
            first_names=("Mgmt0",),
        ),
        # bmv2 (P4 software switch): ports appear as s1, s2, … so the
        # `simple_switch --interface N@sN …` argument line matches the
        # names the user sees on the canvas. Starts at 1 because bmv2
        # rejects port 0 as reserved.
        IfaceScheme("s", "s1, s2 — bmv2 P4 switch ports", "s{n}", start=1),
        # Cumulus Linux: eth0 is the out-of-band management port and the
        # front-panel switch ports are swp1..swpN. Getting this wrong is
        # especially confusing on Cumulus because its own documentation,
        # `net show interface`, and every FRR example all say swp.
        IfaceScheme(
            "swp",
            "eth0, swp1, swp2 — Cumulus Linux",
            "swp{n}",
            start=1,
            first_names=("eth0",),
        ),
    ]
}

DEFAULT_IFACE_SCHEME = "eth"


def guest_iface_name(scheme: str | None, idx: int) -> str:
    """The name the guest will use for its port at `idx`."""
    return IFACE_SCHEMES.get(scheme or DEFAULT_IFACE_SCHEME, IFACE_SCHEMES["eth"]).name(idx)


#: Folder paths are typed by hand, so they arrive with leading slashes, double
#: slashes, trailing spaces and the occasional "..". None of that is dangerous
#: — a folder is a string on a row, not a filesystem path — but left alone it
#: produces two folders that look identical in a list and sort apart.
_FOLDER_MAX = 200
_FOLDER_DEPTH = 8


def normalise_folder(raw: str | None) -> str:
    """A folder path in one canonical form: "CCNA/Week 1", or "" for the root."""
    parts = [seg.strip() for seg in (raw or "").replace("\\", "/").split("/")]
    # "." and ".." carry no meaning here and would only ever be a typo or an
    # attempt at traversal; dropping them beats rejecting the whole path.
    clean = [seg for seg in parts if seg and seg not in (".", "..")]
    return "/".join(clean[:_FOLDER_DEPTH])[:_FOLDER_MAX]
