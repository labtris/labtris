"""In-process rate cache for per-link traffic stats.

Netd's `iface.counters` verb returns monotonic byte / packet counters
for a list of interfaces. What the canvas actually wants to render is
`bps` / `pps` — derived from two adjacent snapshots. Rather than have
the browser store history (fragile: state lost on reload, wrong on a
paged view) we cache the last few snapshots per interface here and
compute deltas server-side.

Cache TTL: 60 seconds per snapshot, cap 8 snapshots per interface.
Enough for a rate over a 2-30 second window with a few seconds of
network jitter, small enough to sit in memory for thousands of
interfaces without noticing. LRU-style prune runs on write, not on
read, so a stale entry expires the next time its interface reports.

Wraparound: a 32-bit rx_bytes counter overflows at 4 GB, and the
IFLA_STATS legacy struct still uses 32-bit. `_rate()` treats any
delta that would be negative as "no data" and skips it — the next
snapshot will produce a valid rate. Better a one-tick gap than a
misleadingly-negative rate.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

_CACHE_TTL = 60.0
_MAX_SNAPSHOTS = 8

# ifname -> deque of (as_of, counters_dict) newest at right end
_snapshots: dict[str, deque[tuple[float, dict[str, Any]]]] = defaultdict(
    lambda: deque(maxlen=_MAX_SNAPSHOTS)
)


def record(counters: dict[str, dict[str, Any]]) -> None:
    """Add a batch of `iface.counters` results into the cache."""
    now = time.time()
    for ifname, entry in counters.items():
        if not entry.get("exists"):
            continue
        d = _snapshots[ifname]
        d.append((entry.get("as_of", now), entry))
        _prune(d, now)


def _prune(d: deque[tuple[float, dict[str, Any]]], now: float) -> None:
    while d and now - d[0][0] > _CACHE_TTL:
        d.popleft()


def rate_for(ifname: str) -> dict[str, Any]:
    """Compute bps / pps / dps for one interface from the cached deltas.

    Returns an empty dict when there are fewer than two snapshots (or
    every delta is a wraparound). Never negative — a wraparound flat-
    lines the reported rate rather than reporting a huge negative
    number that would blow up the UI's colour scale."""
    d = _snapshots.get(ifname)
    if not d or len(d) < 2:
        return {}
    oldest_ts, oldest = d[0]
    newest_ts, newest = d[-1]
    dt = newest_ts - oldest_ts
    if dt <= 0:
        return {}
    d_rx = newest["rx_bytes"] - oldest["rx_bytes"]
    d_tx = newest["tx_bytes"] - oldest["tx_bytes"]
    d_rxp = newest["rx_packets"] - oldest["rx_packets"]
    d_txp = newest["tx_packets"] - oldest["tx_packets"]
    d_rxd = newest["rx_dropped"] - oldest["rx_dropped"]
    d_txd = newest["tx_dropped"] - oldest["tx_dropped"]
    if any(x < 0 for x in (d_rx, d_tx, d_rxp, d_txp, d_rxd, d_txd)):
        return {"window_s": dt, "note": "wrap"}
    return {
        "window_s": round(dt, 3),
        "rx_bps": int(d_rx * 8 / dt),
        "tx_bps": int(d_tx * 8 / dt),
        "rx_pps": int(d_rxp / dt),
        "tx_pps": int(d_txp / dt),
        "rx_dps": round(d_rxd / dt, 3),
        "tx_dps": round(d_txd / dt, 3),
    }


def clear() -> None:
    """Wipe the cache. Used from tests + main.py's lifespan reset."""
    _snapshots.clear()
