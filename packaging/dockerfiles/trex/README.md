# trex — Cisco TRex, stateless, software mode

`ghcr.io/labtris/trex`, built by CI from this directory. Cisco TRex v3.08
(Apache-2.0) from `trex-tgn.cisco.com`; the Dockerfile's `TREX_URL` can
point at a local copy of the tarball because that server refuses some
hosting networks (it refused Hetzner).

TRex is a DPDK application. On hardware it binds a NIC and drives it at
line rate. On a veth it runs `--software` with DPDK's af_packet PMD — one
data-plane core, no offloads, no hugepages — and keeps everything else:
the stateless Python API, the console, per-port statistics, latency
streams, pcap replay. `eth0` and `eth1` are ports 0 and 1.

## What it needs

- `IPC_LOCK`. The PMD mlocks its mmap'd rings; without it the probe says
  `call to mmap failed on AF_PACKET socket … Resource temporarily
  unavailable`. Nothing else: no `SYS_ADMIN`, no vfio, no hugepages.
- The ring size in `/etc/trex_cfg.yaml`. A bare netdev name gets 512
  frames per ring and loses packets at ~150 kpps, because the one
  data-plane core polls receive between its own transmit bursts. The
  `--vdev` form sets 8192-frame rings; with it the generator receives
  3 Mpps per port without loss.
- Three cores (master, data-plane, RX; cores 0-2 by default). Software
  mode allows one data-plane core per port pair, so `-c` cannot help.

## Using it

```
run-trex                                   # server, foreground; log to a file if you background it
python3 /opt/trex/labtris/udp-throughput.py --rate 1mpps --duration 10
/opt/trex/trex-console                     # the interactive console
```

`udp-throughput.py` sends 64 B UDP from port 0 to `48.0.0.0/8` and from
port 1 to `16.0.0.0/8`, and prints, per direction, what it sent, what
came back on the other port, and the difference. The counters are the
ports' own. Per-stream flow statistics are counted on the RX core in
software mode and go wrong above ~150 kpps, reporting a drop the wire
never saw; the first version of this script used them.

## What the numbers mean

On the 36-core Hetzner host the generator achieved 4 Mpps per direction
and received 3 without loss. Below that, the drop line is the DUT's.
Above it, it is the generator's. What a run tells you is what the device
between the two links did with the packets it was handed — a fact about
the DUT's behaviour, at the rate a software port carries. A throughput
figure from this image describes the host. The DUT's own ceiling is a
question for a NIC.
