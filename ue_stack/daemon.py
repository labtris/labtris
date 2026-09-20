"""Async UDP daemon: send + receive UET packets on a local port.

Not the real UEC transport — real UET rides directly on Ethernet /
IP as its own protocol. UDP is the PoC vehicle so `ue_stack` runs
without kernel privileges anywhere the process has a socket, and
Labtris's veths carry it as ordinary L4 traffic. Swapping to a raw
socket + own IP protocol number is a small change once the wire
format is spec-pinned.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable

from ue_stack.packet import OpCode, PdcType, UePacket
from ue_stack.pdc import Ipdc, Tpdc

logger = logging.getLogger(__name__)

#: Placeholder port. UEC 1.0 pins a UDP port for its own transport in
#: the spec; if/when a public number is documented, update here.
DEFAULT_PORT = 4791  # borrows RoCEv2's for now


class UeStackDaemon:
    """One daemon per host, bound to one local UDP port. Owns the
    context table (pdcs) keyed by (peer, pdc_id)."""

    def __init__(self, bind: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        self.bind = bind
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._peer_addr: dict[int, tuple[str, int]] = {}  # pdc_id → (host, port)
        self._on_receive: Callable[[tuple[str, int], UePacket], None] | None = None
        # Active reliable contexts, by pdc_id. Referenced by on_ack()
        # so an incoming ACK matches back to the correct pending queue.
        self._tpdcs: dict[int, Tpdc] = {}

    async def start(self, on_receive: Callable[[tuple[str, int], UePacket], None]) -> None:
        loop = asyncio.get_event_loop()
        self._on_receive = on_receive
        transport, _proto = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(self),
            local_addr=(self.bind, self.port),
        )
        self._transport = transport
        logger.info("ue_stack listening on %s:%s", self.bind, self.port)

    def register_tpdc(self, pdc: Tpdc) -> None:
        self._tpdcs[pdc.pdc_id] = pdc

    def sock_send_to(self, host: str, port: int) -> Callable[[bytes], "asyncio.Future[None]"]:
        """Return a send-callable bound to one peer, matching the
        signature PdcBase.send() expects."""
        transport = self._transport
        if transport is None:
            raise RuntimeError("daemon not started")

        async def _send(data: bytes) -> None:
            transport.sendto(data, (host, port))
        return _send

    def _dispatch(self, addr: tuple[str, int], pkt: UePacket) -> None:
        # ACKs for reliable contexts feed back to the sender's pending
        # table. Data packets go to the on_receive handler.
        if pkt.ses.op == OpCode.ACK and pkt.pds.pdc_type == PdcType.TPDC:
            tpdc = self._tpdcs.get(pkt.pds.dst_pdc_id)
            if tpdc is not None:
                tpdc.on_ack(pkt.pds.psn)
            return
        if self._on_receive is not None:
            try:
                self._on_receive(addr, pkt)
            except Exception:  # noqa: BLE001 — a handler bug must not stop the daemon
                logger.exception("on_receive bug on %r", pkt)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, daemon: UeStackDaemon) -> None:
        self.daemon = daemon

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            pkt = UePacket.unpack(data)
        except Exception:  # noqa: BLE001 — junk on the port must not crash us
            logger.warning("un-unpackable packet from %s (%d bytes)", addr, len(data))
            return
        self.daemon._dispatch(addr, pkt)
