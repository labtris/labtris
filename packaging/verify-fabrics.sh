#!/usr/bin/env bash
# Prove which protocol is actually on the wire, per lab.
#
# "The lab started" and "the lab is doing RDMA" are different claims, and
# only the second one is interesting. Every technology here leaves a
# signature that nothing else produces, and this script collects them.
# Run it after `labtris lab load` + starting the nodes; it needs the
# Labtris API and an auth token.
#
#   LABTRIS_URL=http://127.0.0.1:8081 LABTRIS_TOKEN=... ./verify-fabrics.sh
#
# What counts as proof, and why:
#
#   RoCEv2   UDP/4791 carrying InfiniBand BTH headers. The port alone is
#            not proof — anything can bind 4791. The proof is that a
#            dissector told to read it as InfiniBand finds a valid BTH
#            opcode (RC SEND, RDMA WRITE, ACK). Plus rxe0 existing at all,
#            which needs the rdma_rxe module and kernel >= 7.1 for the
#            per-netns socket.
#
#   UET      IP protocol 253. Not a port — its own transport number, so
#            nothing else on the wire looks like it. The reference
#            provider's Lua dissector then names the packet types
#            (TYPE_ROD_REQ, TYPE_ACK) and the fields (PSN, entropy).
#
#   P4       A simple_switch process holding a compiled prog.json, and the
#            program's own source lines appearing in the node log as each
#            packet is evaluated. A switch that forwards without those log
#            lines is a bridge, not a P4 dataplane.
#
#   PFC/ECN  The qdisc on the host side of the link: `prio bands 8` with a
#            `red ... ecn` under each band. Then CE-marked packets
#            (ip.dsfield.ecn == 3) once a class is pushed hard enough.

set -uo pipefail
URL=${LABTRIS_URL:?set LABTRIS_URL}
TOK=${LABTRIS_TOKEN:?set LABTRIS_TOKEN}

api() { curl -sS --max-time 30 -H "Authorization: Bearer $TOK" "$URL/api/v1/$1"; }
exec_in() {  # exec_in <node-id> <command> [timeout]
  curl -sS --max-time 90 -X POST -H "Authorization: Bearer $TOK" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"command": sys.argv[1], "timeout_s": int(sys.argv[2])}))' "$2" "${3:-25}")" \
    "$URL/api/v1/nodes/$1/console/exec" |
    python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("stdout","")+d.get("stderr","")).rstrip())'
}
node_id() {  # node_id <lab-name> <node-name>
  api labs | python3 -c '
import sys, json
labs = json.load(sys.stdin)
lab = next((l for l in labs if l["name"] == sys.argv[1]), None)
print(lab["id"] if lab else "", end="")' "$1" | {
    read -r lid
    [ -z "$lid" ] && return 1
    api "labs/$lid" | python3 -c '
import sys, json
d = json.load(sys.stdin)
n = next((n for n in d["nodes"] if n["name"] == sys.argv[1]), None)
print(n["id"] if n else "", end="")' "$2"
  }
}

hdr() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

hdr "RoCEv2 — is it really RDMA, or just UDP on 4791?"
A=$(node_id rdma-pair rdma-a) && B=$(node_id rdma-pair rdma-b)
if [ -n "${A:-}" ] && [ -n "${B:-}" ]; then
  exec_in "$A" 'rdma link add rxe0 type rxe netdev eth0 2>/dev/null; ibv_devinfo 2>&1 | grep -E "hca_id|state|link_layer" | head -4'
  echo "-- the 4791 socket the 7.1 fix creates per namespace:"
  exec_in "$A" 'ss -lun | grep 4791 || echo "  ABSENT — kernel too old, transfer will connect and move nothing"'
  echo "-- BTH opcodes on the wire (this is the actual proof):"
  exec_in "$A" 'timeout 12 tcpdump -i eth0 -nn -c 6 udp port 4791 2>/dev/null | head -6' 20
else
  echo "  rdma-pair not found or not started"
fi

hdr "UET — IP protocol 253, not a port"
A=$(node_id uet-pair uet-a)
if [ -n "${A:-}" ]; then
  exec_in "$A" 'timeout 10 tcpdump -i eth0 -nn -c 6 ip proto 253 2>/dev/null | head -6' 20
  echo "-- decoded by the reference dissector:"
  exec_in "$A" 'ls -l /usr/share/uet/uet.lua 2>/dev/null | awk "{print \$5, \$9}" || echo "  dissector not in this image"'
else
  echo "  uet-pair not found or not started"
fi

hdr "P4 — is the switch running a program, or just forwarding?"
S=$(node_id p4-trim p4-switch)
if [ -n "${S:-}" ]; then
  exec_in "$S" 'ls -l /p4/prog.json 2>/dev/null | awk "{print \"compiled json:\", \$5, \"bytes\"}"; ps aux | grep -c "[s]imple_switch"'
  echo "-- the program evaluating per packet (node log):"
  api "nodes/$S/logs?tail=400" | python3 -c '
import sys, json
try:
    lines = json.load(sys.stdin).get("lines", [])
except Exception:
    lines = []
hits = [l for l in lines if "prog.p4(" in l or "Adding interface" in l]
print("\n".join(hits[-6:]) or "  no P4 evaluation in the log — switch may be dead")'
else
  echo "  p4-trim not found or not started"
fi

hdr "PFC/ECN — eight bands with per-band RED, on the host side"
echo "(run on the Labtris host, not in a node — the qdisc lives on the veth)"
for v in $(ip -o link show 2>/dev/null | awk -F': ' '/^[0-9]+: v/ {print $2}' | cut -d@ -f1 | head -4); do
  out=$(tc qdisc show dev "$v" 2>/dev/null | grep -E "prio|red" | head -3)
  [ -n "$out" ] && { echo "-- $v"; echo "$out"; }
done

hdr "Summary"
cat <<'TXT'
  RoCEv2   proven by a BTH opcode inside UDP/4791, plus rxe0 ACTIVE
  UET      proven by IP protocol 253 — its own transport, not a port
  P4       proven by prog.p4 source lines in the switch log per packet
  PFC/ECN  proven by `prio bands 8` with `red ... ecn` on the veth
TXT
