#!/bin/sh
# Start the TRex server in stateless software mode on the two lab links.
# -i        stateless (interactive) mode, the Python API drives it
# --software  no NIC offloads, af_packet PMD — a veth has no PCI function
# -c N      worker threads; one is plenty for what a veth can carry
# --no-hw-flow-stat  flow stats in software too
exec /opt/trex/t-rex-64 -i --software --no-hw-flow-stat -c "${TREX_CORES:-1}" "$@"
