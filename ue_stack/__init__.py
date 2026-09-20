"""ue_stack — a proof-of-concept userspace Ultra Ethernet Transport stack.

Wire-format shape following the UEC 1.0 spec's three sublayers:

- **SES** (Semantic Sub-layer) — packet.py.SesHeader: transaction id,
  endpoint address, op code, MSN (message sequence number), SOM/EOM.
- **PDS** (Packet Delivery Sub-layer) — packet.py.PdsHeader: PDC type
  selector, PSN (packet sequence number), destination PDC id.
- **PDC** (Packet Delivery Context) — pdc/ subpackage:
    - `ipdc.py` — Immediate PDC: unreliable, low-latency, no ACK.
    - `tpdc.py` — Transactional PDC: reliable with per-packet ACK + RTO
      retransmission. Stub — the state machine + RTO wiring lands
      later; wire format is stable.

Not intended to interop with real UEC hardware. This is a research
starting point: the shape a real UET stack takes, running on real
Linux over real veths, so a fabric researcher can prototype changes
against real packets rather than an ns-3 simulation. When real
hardware ships and interop matters, everything below the CLI stays
the same; the wire-format bit widths get pinned to the exact UEC
spec numbers (this stub uses spec-shaped fields but the exact
offsets are TODO).

Ships alongside UE-Sim (Kaima Lab's ns-3 simulator, packaged as the
`ue-sim` Labtris node kind). Different tools for different jobs:
UE-Sim for deterministic protocol validation + fast iteration;
`ue_stack` for real-packet experiments over a Labtris fabric.
"""

__version__ = "0.1.0"
