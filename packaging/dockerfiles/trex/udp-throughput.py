#!/usr/bin/env python3
"""Push UDP through the DUT in both directions and report what came back.

port 0 (eth0) sends 16.0.0.0/8 -> 48.0.0.0/8, port 1 (eth1) the reverse,
so a DUT routing 48/8 out its second link and 16/8 out its first forwards
every packet. Forwarded = what arrived on the other port; dropped = the
difference.

    python3 udp-throughput.py --rate 500kpps --duration 10 --size 64
    python3 udp-throughput.py --rate 3mpps --one-way

Counters are the ports' own (opackets/ipackets from the data-plane core),
not per-stream flow stats: in software mode flow stats are counted on the
RX core, which loses track above ~150 kpps and reports a drop that never
happened on the wire. The rate is what TRex *offers*; read 'tx achieved'
before reading the drop line, because above ~4 Mpps per direction this
generator on a veth is the limit, not the DUT.
"""
import argparse
import sys
import time

sys.path.insert(0, "/opt/trex/automation/trex_control_plane/interactive")
from trex.stl.api import (  # noqa: E402
    IP, UDP, Ether, STLClient, STLPktBuilder, STLStream, STLTXCont,
)


def _pps(s: str) -> float:
    s = s.lower()
    for suf, mul in (("mpps", 1e6), ("kpps", 1e3), ("pps", 1)):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * mul
    return float(s)


def stream(src: str, dst: str, size: int, rate: str) -> STLStream:
    pkt = Ether() / IP(src=src, dst=dst) / UDP(sport=1025, dport=12)
    pkt = pkt / (b"x" * max(0, size - len(pkt) - 4))  # 4 = FCS
    mode = (STLTXCont(percentage=float(rate[:-1])) if rate.endswith("%")
            else STLTXCont(pps=_pps(rate)))
    return STLStream(packet=STLPktBuilder(pkt=pkt), mode=mode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="127.0.0.1")
    ap.add_argument("--rate", default="500kpps",
                    help="per direction: Nkpps, Nmpps, Npps or N%%")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--size", type=int, default=64, help="frame size incl. FCS")
    ap.add_argument("--one-way", action="store_true", help="port 0 -> 1 only")
    a = ap.parse_args()

    tx_ports = [0] if a.one_way else [0, 1]
    c = STLClient(server=a.server)
    c.connect()
    try:
        c.reset(ports=[0, 1])
        c.add_streams(stream("16.0.0.1", "48.0.0.1", a.size, a.rate), ports=[0])
        if not a.one_way:
            c.add_streams(stream("48.0.0.1", "16.0.0.1", a.size, a.rate), ports=[1])
        c.clear_stats()
        c.start(ports=tx_ports, duration=a.duration)
        c.wait_on_traffic(ports=tx_ports)
        time.sleep(1)  # let the last burst land before reading
        st = c.get_stats()
    finally:
        c.disconnect()

    bits = a.size * 8
    rows = [("eth0 -> DUT -> eth1", 0, 1)]
    if not a.one_way:
        rows.append(("eth1 -> DUT -> eth0", 1, 0))
    print(f"frame {a.size} B, offered {a.rate} per direction, {a.duration:.0f} s")
    total_tx = total_drop = 0
    for label, tx_port, rx_port in rows:
        tx = st[tx_port]["opackets"]
        rx = st[rx_port]["ipackets"]
        drop = max(0, tx - rx)
        total_tx += tx
        total_drop += drop
        print(label)
        print(f"  tx achieved  {tx/a.duration/1e6:8.3f} Mpps  "
              f"{tx/a.duration*bits/1e9:7.3f} Gbps  ({tx} pkts)")
        print(f"  rx forwarded {rx/a.duration/1e6:8.3f} Mpps  "
              f"{rx/a.duration*bits/1e9:7.3f} Gbps  ({rx} pkts)")
        print(f"  dropped      {drop:8d} pkts  {100.0*drop/tx if tx else 0:6.2f} %")
    pct = 100.0 * total_drop / total_tx if total_tx else 0.0
    print(f"total dropped {total_drop} pkts ({pct:.2f} %) of {total_tx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
