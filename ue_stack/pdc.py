"""PDC (Packet Delivery Context) state machines.

Two shipped:

- **IPDC** — Immediate, unreliable. `send()` builds one UET packet and
  hands it to the daemon; `receive()` dispatches on the ses.op.
  No ACK, no retransmit — matches UEC's low-latency delay-sensitive
  class.
- **TPDC** — Transactional, reliable. Send tracks per-PSN pending state,
  waits for ACK matching that PSN, retransmits after RTO. Stub in
  this PoC — the state-machine table is real; the RTO backoff and
  selective-retransmit paths are minimal.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ue_stack.packet import (
    OpCode, PdcHeader, PdcType, PdsHeader, SesHeader, UePacket,
)


class PdcBase:
    """One PDC per (local endpoint, peer) pair. Owns the sequence
    counters that make ordering + retransmit possible."""

    def __init__(self, pdc_id: int, local_endpoint: int, dst_endpoint: int) -> None:
        self.pdc_id = pdc_id
        self.local_endpoint = local_endpoint
        self.dst_endpoint = dst_endpoint
        self._txn_gen = itertools.count(1)
        self._psn_gen = itertools.count(1)
        self._msn_gen = itertools.count(1)


class Ipdc(PdcBase):
    """Unreliable fire-and-forget context. `send()` returns as soon as
    the packet is on the wire; no acknowledgement is expected."""

    type = PdcType.IPDC

    async def send(
        self,
        sock_send: Callable[[bytes], Awaitable[None]],
        op: OpCode,
        payload: bytes,
        priority: int = 3,
    ) -> UePacket:
        psn = next(self._psn_gen)
        msn = next(self._msn_gen)
        pkt = UePacket(
            ses=SesHeader(
                op=op,
                flags=0b11,  # SOM + EOM — single-packet message.
                msn=msn,
                txn_id=next(self._txn_gen),
                dst_endpoint=self.dst_endpoint,
            ),
            pds=PdsHeader(
                pdc_type=self.type, psn=psn, dst_pdc_id=self.pdc_id,
            ),
            pdc=PdcHeader(credits=0, priority=priority),
            payload=payload,
        )
        await sock_send(pkt.pack())
        return pkt


@dataclass
class _PendingAck:
    packet: UePacket
    sent_at: float
    retries: int = 0


class Tpdc(PdcBase):
    """Reliable context with per-packet ACK + RTO retransmit.

    Stub: RTO is a fixed 500 ms with 3 retries; selective NACK is
    accepted but not yet used to drive fast retransmit. Production
    UET runs a CC-driven RTO estimator and reacts to NACK; both belong
    in a real implementation."""

    type = PdcType.TPDC

    def __init__(self, *args: Any, rto_seconds: float = 0.5, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.rto_seconds = rto_seconds
        self._pending: dict[int, _PendingAck] = {}

    async def send(
        self,
        sock_send: Callable[[bytes], Awaitable[None]],
        op: OpCode,
        payload: bytes,
        priority: int = 3,
    ) -> UePacket:
        psn = next(self._psn_gen)
        msn = next(self._msn_gen)
        pkt = UePacket(
            ses=SesHeader(
                op=op, flags=0b11, msn=msn,
                txn_id=next(self._txn_gen),
                dst_endpoint=self.dst_endpoint,
            ),
            pds=PdsHeader(
                pdc_type=self.type, psn=psn, dst_pdc_id=self.pdc_id,
            ),
            pdc=PdcHeader(credits=0, priority=priority),
            payload=payload,
        )
        await sock_send(pkt.pack())
        self._pending[psn] = _PendingAck(pkt, time.monotonic())
        return pkt

    def on_ack(self, psn: int) -> bool:
        """Called by the daemon when an ACK matching one of our
        pending PSNs arrives. Returns whether the ACK matched."""
        return self._pending.pop(psn, None) is not None

    async def rto_sweep(
        self, sock_send: Callable[[bytes], Awaitable[None]], max_retries: int = 3,
    ) -> list[int]:
        """One tick of the RTO retransmit loop. Callers should invoke
        every ~rto_seconds/2. Returns the PSNs given up on (retries
        exhausted)."""
        now = time.monotonic()
        dropped: list[int] = []
        for psn, entry in list(self._pending.items()):
            if now - entry.sent_at < self.rto_seconds:
                continue
            if entry.retries >= max_retries:
                self._pending.pop(psn)
                dropped.append(psn)
                continue
            entry.retries += 1
            entry.sent_at = now
            await sock_send(entry.packet.pack())
        return dropped
