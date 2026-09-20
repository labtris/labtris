"""Sanity tests for the ue_stack wire format + PDC state machines."""

from __future__ import annotations

import asyncio

import pytest

from ue_stack.daemon import UeStackDaemon
from ue_stack.packet import (
    HEADER_BYTES, OpCode, PdcHeader, PdcType, PdsHeader, SesHeader, UePacket,
)
from ue_stack.pdc import Ipdc, Tpdc


def test_headers_round_trip():
    p = UePacket(
        ses=SesHeader(op=OpCode.WRITE, flags=0b11, msn=42, txn_id=7, dst_endpoint=99),
        pds=PdsHeader(pdc_type=PdcType.TPDC, psn=123, dst_pdc_id=1),
        pdc=PdcHeader(credits=8, priority=3),
        payload=b"hello",
    )
    b = p.pack()
    assert len(b) == HEADER_BYTES + len(p.payload)
    r = UePacket.unpack(b)
    assert r.ses.op == OpCode.WRITE
    assert r.ses.msn == 42
    assert r.ses.txn_id == 7
    assert r.ses.dst_endpoint == 99
    assert r.pds.pdc_type == PdcType.TPDC
    assert r.pds.psn == 123
    assert r.pdc.credits == 8
    assert r.pdc.priority == 3
    assert r.payload == b"hello"


def test_ipdc_advances_sequence():
    ipdc = Ipdc(pdc_id=1, local_endpoint=1, dst_endpoint=2)
    sent = []

    async def sock(data: bytes) -> None:
        sent.append(UePacket.unpack(data))

    async def go():
        for _ in range(3):
            await ipdc.send(sock, OpCode.SEND, b"x")

    asyncio.run(go())
    assert [p.pds.psn for p in sent] == [1, 2, 3]
    assert [p.ses.msn for p in sent] == [1, 2, 3]
    assert all(p.pds.pdc_type == PdcType.IPDC for p in sent)


def test_tpdc_tracks_pending_and_ack_clears():
    tpdc = Tpdc(pdc_id=42, local_endpoint=1, dst_endpoint=2)

    async def sock(_data: bytes) -> None:
        pass

    async def go():
        p1 = await tpdc.send(sock, OpCode.WRITE, b"a")
        p2 = await tpdc.send(sock, OpCode.WRITE, b"b")
        assert set(tpdc._pending) == {p1.pds.psn, p2.pds.psn}
        assert tpdc.on_ack(p1.pds.psn) is True
        assert tpdc.on_ack(p1.pds.psn) is False  # already cleared
        assert set(tpdc._pending) == {p2.pds.psn}

    asyncio.run(go())


@pytest.mark.anonymous
def test_end_to_end_ipdc_over_loopback():
    """Two daemons on ephemeral ports on loopback — sender's IPDC
    packet arrives on the receiver's on_receive."""

    received: list[UePacket] = []

    async def go():
        rx = UeStackDaemon(port=0)
        await rx.start(lambda addr, pkt: received.append(pkt))
        assert rx._transport is not None
        rx_port = rx._transport.get_extra_info("sockname")[1]

        tx = UeStackDaemon(port=0)
        await tx.start(lambda *_a: None)
        ipdc = Ipdc(pdc_id=1, local_endpoint=1, dst_endpoint=2)
        await ipdc.send(
            tx.sock_send_to("127.0.0.1", rx_port),
            OpCode.SEND,
            b"hello ue",
        )
        # Yield the loop a couple of ticks so the datagram lands.
        for _ in range(5):
            await asyncio.sleep(0.02)
            if received:
                break

        rx.close()
        tx.close()

    asyncio.run(go())
    assert len(received) == 1
    assert received[0].payload == b"hello ue"
