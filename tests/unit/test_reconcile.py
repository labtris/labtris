"""Startup reconciliation: make the database agree with the machine.

The interesting cases cannot be arranged by hand on a live host — a node that
says running with nothing behind it, a reservation whose owner was deleted —
so these build the state directly and check what reconcile() does with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.models import IfnameRegistry, Lab, MacRegistry, Node
from labtris_api.reconcile import reconcile


@asynccontextmanager
async def db() -> AsyncIterator[AsyncSession]:
    """A session and engine owned by one test, so nothing crosses event loops."""
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def test_an_orphaned_reservation_is_released() -> None:
    """Deleting a node used to leave its device names reserved forever, so the
    pool only ever shrank. Anything whose owner no longer exists is free."""
    async with db() as session:
        session.add(IfnameRegistry(host_ifname="vgone0001", kind="veth",
                                   owner_id="01NOSUCHOWNER000000000001"))
        await session.commit()

        result = await reconcile(session)

        assert result["ifnames_released"] >= 1
        left = (
            await session.execute(
                select(IfnameRegistry).where(IfnameRegistry.host_ifname == "vgone0001")
            )
        ).scalar_one_or_none()
        assert left is None


async def test_a_reservation_with_a_live_owner_is_left_alone() -> None:
    """The sweep must not free names still in use — that would eventually hand
    one device name to two labs."""
    async with db() as session:
        lab = Lab(id="01RECONCILELAB0000000001", name="reconcile-keep")
        session.add(lab)
        await session.flush()
        session.add(IfnameRegistry(host_ifname="bkeep0001", kind="bridge", owner_id=lab.id))
        await session.commit()
        try:
            await reconcile(session)

            kept = (
                await session.execute(
                    select(IfnameRegistry).where(IfnameRegistry.host_ifname == "bkeep0001")
                )
            ).scalar_one_or_none()
            assert kept is not None, "released a reservation whose owner still exists"
        finally:
            await session.execute(
                IfnameRegistry.__table__.delete().where(
                    IfnameRegistry.host_ifname == "bkeep0001"
                )
            )
            await session.execute(Lab.__table__.delete().where(Lab.id == lab.id))
            await session.commit()


async def test_an_orphaned_mac_is_released() -> None:
    async with db() as session:
        session.add(MacRegistry(mac="02:00:de:ad:be:ef", owner_id="01NOSUCHIFACE00000000001"))
        await session.commit()

        result = await reconcile(session)

        assert result["macs_released"] >= 1


async def test_a_node_that_only_thinks_it_is_running_is_corrected() -> None:
    """A host reboot takes every process with it while the rows go on claiming
    the lab is up — healthy in the UI, unable to pass a packet.

    Marked stopped rather than restarted: silently booting someone's VMs
    because the machine rebooted is a surprising amount of work to begin
    unasked, and on this host each one is minutes of emulation."""
    async with db() as session:
        lab = Lab(id="01RECONCILELAB0000000002", name="reconcile-stale")
        session.add(lab)
        await session.flush()
        node = Node(
            id="01RECONCILENODE000000001", lab_id=lab.id, name="ghost",
            runtime="qemu", image="cirros", state="running",
            runtime_ref="/nonexistent/path/that/cannot/be/running",
        )
        session.add(node)
        await session.commit()
        try:
            result = await reconcile(session)

            assert "ghost" in result["marked_stopped"]
            await session.refresh(node)
            assert node.state == "stopped"
        finally:
            await session.execute(Node.__table__.delete().where(Node.id == node.id))
            await session.execute(Lab.__table__.delete().where(Lab.id == lab.id))
            await session.commit()
