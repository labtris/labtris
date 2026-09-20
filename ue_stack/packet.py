"""SES + PDS header pack/unpack for the UET wire format.

The bit-widths and field ordering here follow the UEC 1.0 spec's
described shape; the exact-to-the-bit offsets are marked TODO with a
placeholder width. This gets a real packet on the wire that a
paired ue_stack instance can decode; interop with real UEC silicon
is out of scope for the PoC and will require binding to the shipped
spec numbers.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class PdcType(IntEnum):
    """Which delivery context a packet belongs to.

    IPDC — unreliable fire-and-forget, low-latency delay-sensitive.
    TPDC — reliable with ACK + RTO retransmission.

    Additional context types (RUDD, etc.) live in the full spec;
    stub covers the two that carry the bulk of AI-fabric traffic.
    """

    IPDC = 1
    TPDC = 2


class OpCode(IntEnum):
    """SES-layer operation codes. Small subset for the PoC — the full
    UEC spec has more (atomic add, compare-and-swap, put_immediate,
    …). Mapping to real values happens when the spec's OpCode table
    is pinned in code."""

    WRITE = 0x01     # sender wants receiver to store payload
    READ = 0x02      # sender wants payload back
    SEND = 0x03      # send message with no rendezvous
    ACK = 0x10       # explicit ACK for a TPDC PSN
    NACK = 0x11      # selective retransmit request


# Wire format:
#   [SES 16 B][PDS 12 B][PDC 4 B][payload …]
#
# Byte layouts (network byte order):
#   SES (16 B): op(1) | flags(1) | msn(4) | txn_id(4) | dst_ep(4) | reserved(2)
#   PDS (12 B): pdc_type(1) | reserved(1) | psn(4) | dst_pdc_id(4) | reserved(2)
#   PDC  (4 B): variant-specific — for IPDC/TPDC it's a small
#               credit + priority field (2 B) + reserved(2)

_SES = struct.Struct("!BBIIIH")
_PDS = struct.Struct("!BBIIH")
_PDC = struct.Struct("!HH")


@dataclass
class SesHeader:
    op: OpCode
    #: bit 0: SOM (start of message), bit 1: EOM (end of message).
    flags: int
    msn: int
    txn_id: int
    dst_endpoint: int
    reserved: int = 0

    def pack(self) -> bytes:
        return _SES.pack(
            int(self.op), self.flags, self.msn, self.txn_id,
            self.dst_endpoint, self.reserved,
        )

    @classmethod
    def unpack(cls, b: bytes) -> tuple["SesHeader", bytes]:
        op, flags, msn, txn, ep, res = _SES.unpack_from(b, 0)
        return (
            cls(OpCode(op), flags, msn, txn, ep, res),
            b[_SES.size:],
        )


@dataclass
class PdsHeader:
    pdc_type: PdcType
    psn: int
    dst_pdc_id: int
    reserved: int = 0
    _reserved8: int = 0

    def pack(self) -> bytes:
        return _PDS.pack(
            int(self.pdc_type), self._reserved8,
            self.psn, self.dst_pdc_id, self.reserved,
        )

    @classmethod
    def unpack(cls, b: bytes) -> tuple["PdsHeader", bytes]:
        pdc_type, _r8, psn, pdc_id, res = _PDS.unpack_from(b, 0)
        return (
            cls(PdcType(pdc_type), psn, pdc_id, res, _r8),
            b[_PDS.size:],
        )


@dataclass
class PdcHeader:
    credits: int = 0
    priority: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        return _PDC.pack((self.credits & 0xff) << 8 | (self.priority & 0xff), self.reserved)

    @classmethod
    def unpack(cls, b: bytes) -> tuple["PdcHeader", bytes]:
        prio_creds, res = _PDC.unpack_from(b, 0)
        return (
            cls(credits=(prio_creds >> 8) & 0xff, priority=prio_creds & 0xff, reserved=res),
            b[_PDC.size:],
        )


@dataclass
class UePacket:
    """One UET packet: SES + PDS + PDC + payload."""

    ses: SesHeader
    pds: PdsHeader
    pdc: PdcHeader
    payload: bytes = b""

    def pack(self) -> bytes:
        return self.ses.pack() + self.pds.pack() + self.pdc.pack() + self.payload

    @classmethod
    def unpack(cls, b: bytes) -> "UePacket":
        ses, r1 = SesHeader.unpack(b)
        pds, r2 = PdsHeader.unpack(r1)
        pdc, r3 = PdcHeader.unpack(r2)
        return cls(ses=ses, pds=pds, pdc=pdc, payload=r3)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"UePacket(op={self.ses.op.name} msn={self.ses.msn} "
            f"txn={self.ses.txn_id} pdc={self.pds.pdc_type.name} "
            f"psn={self.pds.psn} payload={len(self.payload)}B)"
        )


HEADER_BYTES = _SES.size + _PDS.size + _PDC.size


def summarize(pkt: dict[str, Any] | bytes) -> str:
    """One-line human view of a packet, from either bytes or a dict."""
    p = UePacket.unpack(pkt) if isinstance(pkt, (bytes, bytearray)) else pkt
    return repr(p)
