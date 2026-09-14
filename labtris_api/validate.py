"""Does the running lab match the lab that was asked for?

Starting a topology and having a working topology are different claims, and
until now only the first was ever made. These checks compare intent — the rows
in the database — against reality, which for a lab is the dataplane on the
host: the taps that should exist, the bridges they should be on, and the
impairment that should be applied.

Deliberately host-side. Asking a guest whether it can reach its neighbour
needs the guest's cooperation, a login, and a shell; asking the kernel whether
two taps are up on the same bridge needs none of that and is true even for a
guest that has not finished booting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.models import Interface, Lab, Link, Network, Node
from labtris_api.netd_client import NetdError, netd


@dataclass
class Check:
    """One assertion about the lab, and what was found instead."""

    name: str
    subject: str
    passed: bool
    detail: str
    #: Anything a person would want to see to judge the result themselves.
    evidence: dict[str, Any] = field(default_factory=dict)


def _close(a: Any, b: Any, tolerance: float = 0.05) -> bool:
    """Impairment read back from the kernel is quantised, so compare loosely."""
    try:
        x, y = float(a or 0), float(b or 0)
    except (TypeError, ValueError):
        return bool(a == b)
    if x == y:
        return True
    return bool(abs(x - y) <= max(1.0, abs(y) * tolerance))


async def validate_lab(session: AsyncSession, lab: Lab) -> dict[str, Any]:
    checks: list[Check] = []

    nodes = list(
        (
            await session.execute(
                select(Node)
                .options(selectinload(Node.interfaces))
                .where(Node.lab_id == lab.id)
            )
        )
        .scalars()
        .unique()
    )
    networks = list(
        (await session.execute(select(Network).where(Network.lab_id == lab.id))).scalars()
    )
    links = list(
        (await session.execute(select(Link).where(Link.lab_id == lab.id))).scalars()
    )

    running = [n for n in nodes if n.state == "running"]

    # Every device this lab believes it owns, asked for in one round trip.
    wanted: list[str] = []
    for net in networks:
        if net.host_ifname:
            wanted.append(net.host_ifname)
    for node in running:
        for iface in node.interfaces:
            if iface.host_ifname:
                wanted.append(iface.host_ifname)

    live: dict[str, Any] = {}
    if wanted:
        try:
            live = (await netd.call("iface.inspect", {"names": sorted(set(wanted))}))[
                "interfaces"
            ]
        except NetdError as exc:
            checks.append(
                Check(
                    "netd reachable",
                    "host",
                    False,
                    f"cannot inspect the dataplane: {exc.message}",
                )
            )
            return _summarise(lab, checks)

    for net in networks:
        if not net.host_ifname:
            checks.append(
                Check(
                    "segment has a bridge",
                    net.name,
                    not any(
                        iface.network_id == net.id for node in running for iface in node.interfaces
                    ),
                    "no bridge yet — nothing running is attached to it",
                )
            )
            continue
        found = live.get(net.host_ifname) or {}
        checks.append(
            Check(
                "segment has a bridge",
                net.name,
                bool(found.get("exists")) and found.get("kind") == "bridge",
                f"{net.host_ifname} present and up"
                if found.get("exists")
                else f"{net.host_ifname} is missing from the host",
                {"device": net.host_ifname, **found},
            )
        )

    for node in running:
        for iface in node.interfaces:
            if not iface.host_ifname:
                checks.append(
                    Check(
                        "interface is realised",
                        f"{node.name}:{iface.name}",
                        False,
                        "running node has an interface with no host device",
                    )
                )
                continue
            found = live.get(iface.host_ifname) or {}
            ok = bool(found.get("exists")) and bool(found.get("up"))
            checks.append(
                Check(
                    "interface is realised",
                    f"{node.name}:{iface.name}",
                    ok,
                    f"{iface.host_ifname} up"
                    if ok
                    else f"{iface.host_ifname} "
                    + ("is down" if found.get("exists") else "does not exist"),
                    {"device": iface.host_ifname, **found},
                )
            )
            if iface.network_id and found.get("exists"):
                match = [n for n in networks if n.id == iface.network_id]
                expected = match[0].host_ifname if match else None
                actual = found.get("master")
                checks.append(
                    Check(
                        "interface is on the right segment",
                        f"{node.name}:{iface.name}",
                        bool(expected) and actual == expected,
                        f"on {actual}" if actual == expected
                        else f"expected {expected}, found {actual or 'no bridge'}",
                        {"expected": expected, "actual": actual},
                    )
                )

    for link in links:
        a = await session.get(Interface, link.a_iface_id)
        b = await session.get(Interface, link.b_iface_id)
        if a is None or b is None or not a.host_ifname or not b.host_ifname:
            continue
        la = live.get(a.host_ifname) or {}
        lb = live.get(b.host_ifname) or {}
        if not (la.get("exists") and lb.get("exists")):
            continue
        same = la.get("master") and la.get("master") == lb.get("master")
        checks.append(
            Check(
                "link ends share a segment",
                f"{a.host_ifname} ↔ {b.host_ifname}",
                bool(same),
                f"both on {la.get('master')}"
                if same
                else f"{la.get('master')} vs {lb.get('master')} — traffic cannot cross",
                {"a": la.get("master"), "b": lb.get("master")},
            )
        )
        # Impairment: what the kernel is applying versus what the lab stored.
        for side, iface, spec in (("A→B", a, link.impair_ab), ("B→A", b, link.impair_ba)):
            wanted_spec = {k: v for k, v in (spec or {}).items() if v}
            try:
                applied = (await netd.call("tc.read", {"name": iface.host_ifname}))["spec"]
            except NetdError as exc:
                checks.append(
                    Check("impairment is applied", f"{side} {iface.host_ifname}", False,
                          f"could not read tc: {exc.message}")
                )
                continue
            mismatched = {
                k: {"wanted": v, "applied": applied.get(k)}
                for k, v in wanted_spec.items()
                if not _close(applied.get(k), v)
            }

            extra = {k: v for k, v in applied.items() if k not in wanted_spec}
            checks.append(
                Check(
                    "impairment is applied",
                    f"{side} {iface.host_ifname}",
                    not mismatched and not extra,
                    "matches the lab"
                    if not mismatched and not extra
                    else _impair_detail(mismatched, extra),
                    {"wanted": wanted_spec, "applied": applied},
                )
            )

    return _summarise(lab, checks)


def _impair_detail(mismatched: dict[str, Any], extra: dict[str, Any]) -> str:
    parts = []
    if mismatched:
        parts.append(f"differs: {mismatched}")
    if extra:
        parts.append(f"applied but not asked for: {extra}")
    return "; ".join(parts)


def _summarise(lab: Lab, checks: list[Check]) -> dict[str, Any]:
    failed = [c for c in checks if not c.passed]
    return {
        "lab_id": lab.id,
        "lab": lab.name,
        "ok": not failed,
        "total": len(checks),
        "failed": len(failed),
        "checks": [asdict(c) for c in checks],
    }


def render(result: dict[str, Any]) -> str:
    passed = result["total"] - result["failed"]
    lines = [f"{result['lab']}: {passed}/{result['total']} checks passed"]
    for c in result["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']} · {c['subject']} — {c['detail']}")
    return "\n".join(lines)
