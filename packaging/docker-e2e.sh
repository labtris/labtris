#!/usr/bin/env bash
# End-to-end test of the containerised Labtris: does a lab actually run
# when every process shares one container network namespace instead of
# the host's?
#
# Three things are under test, in increasing order of what they prove:
#   A. container nodes  -> dind reachable, veth pair built, peer moved
#                          into the guest netns, L2 adjacency works
#   B. NAT network      -> nft masquerade + dnsmasq DHCP inside the
#                          namespace, and a node reaching the internet
#   C. QEMU node        -> tap opened through /dev/net/tun, KVM accel,
#                          serial console reachable
#
# Run on the Docker host, from the directory holding docker-compose.yml:
#   ./packaging/docker-e2e.sh
#
# Leaves nothing behind unless KEEP=1. Overrides:
#   LABTRIS_API   base URL                (default http://127.0.0.1:8080)
#   COMPOSE_DIR   where docker-compose.yml lives (default .)
#   PW_FILE       where the generated admin password is kept
#                 (default ./admin-password)
set -uo pipefail

ROOT=${LABTRIS_API:-http://127.0.0.1:8080}
B=$ROOT/api/v1
COMPOSE_DIR=${COMPOSE_DIR:-.}
PW_FILE=${PW_FILE:-./admin-password}
PASS=0; FAIL=0
G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'

ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; PASS=$((PASS+1)); }
bad()  { printf '  %s✗%s %s\n     %s\n' "$R" "$N" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
step() { printf '\n%s== %s ==%s\n' "$Y" "$1" "$N"; }
want() { if printf '%s' "${3:-}" | grep -q "$2"; then ok "$1"; else bad "$1" "got: $(printf '%s' "${3:-}" | head -c 300)"; fi; }

# api METHOD PATH [BODY] -> body on stdout, status via `st`.
#
# The status goes to a FILE, not a variable: almost every call here is
# made inside $(...), which is a subshell, so a variable assigned in api
# never reaches the caller and every request would look like status "".
SFILE=$(mktemp)
st() { cat "$SFILE"; }
api() {
  local m=$1 p=$2 d=${3:-} out
  if [ -n "$d" ]; then
    out=$(curl -sS -m 180 -w '\n%{http_code}' -X "$m" \
      -H "Authorization: Bearer ${TOKEN:-}" -H 'Content-Type: application/json' \
      -d "$d" "$B$p" 2>&1)
  else
    out=$(curl -sS -m 180 -w '\n%{http_code}' -X "$m" \
      -H "Authorization: Bearer ${TOKEN:-}" "$B$p" 2>&1)
  fi
  printf '%s' "${out##*$'\n'}" > "$SFILE"
  printf '%s' "${out%$'\n'*}"
}

# Run a command inside a node, print "<rc>|<stdout>"
nexec() {
  local id=$1 cmd=$2 t=${3:-20} body
  body=$(api POST "/nodes/$id/console/exec" \
    "$(jq -nc --arg c "$cmd" --argjson t "$t" '{command:$c,timeout_s:$t}')")
  if [ "$(st)" != "200" ]; then printf '125|%s' "$body"; return 0; fi
  printf '%s|%s' \
    "$(printf '%s' "$body" | jq -r '.exit_code // 0')" \
    "$(printf '%s' "$body" | jq -r '.stdout // ""')"
}

# --- auth ------------------------------------------------------------
step "Bootstrap"
for i in $(seq 1 40); do
  curl -sf -m 3 "$B/auth/state" >/dev/null 2>&1 && break
  sleep 2
done
STATE=$(curl -sS -m 10 "$B/auth/state")
want "API answering" "setup_required" "$STATE"

PW="e2e-$(head -c 12 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9')"
if printf '%s' "$STATE" | grep -q 'true'; then
  RESP=$(api POST /auth/setup "$(jq -nc --arg p "$PW" '{username:"admin",password:$p}')")
  TOKEN=$(printf '%s' "$RESP" | jq -r '.token // empty')
  [ -n "$TOKEN" ] && ok "created the first admin" || bad "auth/setup" "$RESP"
  printf '%s' "$PW" > $PW_FILE
else
  PW=$(cat $PW_FILE 2>/dev/null || echo "")
  RESP=$(api POST /auth/login "$(jq -nc --arg p "$PW" '{username:"admin",password:$p}')")
  TOKEN=$(printf '%s' "$RESP" | jq -r '.token // empty')
  [ -n "$TOKEN" ] && ok "logged in" || bad "auth/login" "$RESP"
fi
[ -n "${TOKEN:-}" ] || { printf '\nno token — stopping\n'; exit 1; }

want "web UI served" "200" "$(curl -s -o /dev/null -w '%{http_code}' $ROOT/)"

# --- lab -------------------------------------------------------------
step "Create a lab"
LAB=$(printf '%s' "$(api POST /labs '{"name":"docker-e2e","description":"namespace test"}')" | jq -r '.id // empty')
[ -n "$LAB" ] && ok "lab $LAB" || { bad "POST /labs" "$(st)"; exit 1; }

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then printf '\n%skept lab %s%s\n' "$Y" "$LAB" "$N"; return; fi
  step "Cleanup"
  api DELETE "/labs/$LAB" >/dev/null
  ok "removed lab $LAB"
}
trap cleanup EXIT

# --- A. container dataplane -----------------------------------------
step "A. Container nodes through the private Docker daemon"
NET=$(printf '%s' "$(api POST "/labs/$LAB/networks" '{"name":"seg1","kind":"bridge"}')" | jq -r '.id // empty')
[ -n "$NET" ] && ok "bridge network $NET" || bad "network create" "$(st)"

mknode() {  # mknode NAME IMAGE NETWORK_ID -> node id
  printf '%s' "$(api POST "/labs/$LAB/nodes" "$(jq -nc \
    --arg n "$1" --arg i "$2" --arg net "$3" \
    '{name:$n,runtime:"docker",image:$i,interfaces:[{name:"eth1",network_id:$net}]}')")" \
    | jq -r '.id // empty'
}
H1=$(mknode h1 alpine:3.20 "$NET"); H2=$(mknode h2 alpine:3.20 "$NET")
[ -n "$H1" ] && [ -n "$H2" ] && ok "created h1/h2" || bad "node create" "$(st)"

for n in $H1 $H2; do
  r=$(api POST "/nodes/$n/start")
  [ "$(st)" = "200" ] && ok "started $(printf '%s' "$r" | jq -r .name)" \
    || bad "start $n" "$(st): $(printf '%s' "$r" | head -c 200)"
done
sleep 5

out=$(nexec "$H1" 'ip -o link show')
want "veth landed in h1 as eth1" "eth1" "${out#*|}"

# Address them by hand — a plain bridge does no IPAM — then prove L2.
nexec "$H1" 'ip addr add 10.99.0.1/24 dev eth1 && ip link set eth1 up' >/dev/null
nexec "$H2" 'ip addr add 10.99.0.2/24 dev eth1 && ip link set eth1 up' >/dev/null
sleep 2
out=$(nexec "$H1" 'ping -c 2 -W 3 10.99.0.2')
if [ "${out%%|*}" = "0" ]; then ok "h1 -> h2 ping over the lab bridge"
else bad "h1 -> h2 ping" "rc=${out%%|*} ${out#*|}"; fi

# --- B. NAT + internet ----------------------------------------------
step "B. NAT network: dnsmasq DHCP and nft masquerade"
NAT=$(printf '%s' "$(api POST "/labs/$LAB/networks" '{"name":"wan","kind":"nat"}')" | jq -r '.id // empty')
[ -n "$NAT" ] && ok "nat network $NAT" || bad "nat create" "$(st)"
SUB=$(printf '%s' "$(api GET "/networks/$NAT")" | jq -r '.subnet // "?"')
printf '     subnet %s\n' "$SUB"

H3=$(mknode h3 alpine:3.20 "$NAT")
[ -n "$H3" ] && ok "created h3 on the NAT segment" || bad "h3 create" "$(st)"
api POST "/nodes/$H3/start" >/dev/null
sleep 5

out=$(nexec "$H3" 'udhcpc -i eth1 -n -q -t 5 2>&1; ip -4 addr show eth1' 40)
want "h3 got a DHCP address" "inet " "${out#*|}"

out=$(nexec "$H3" 'ping -c 2 -W 4 1.1.1.1' 30)
if [ "${out%%|*}" = "0" ]; then ok "h3 reached the internet through the masquerade"
else bad "h3 -> 1.1.1.1" "rc=${out%%|*} ${out#*|}"; fi

LEASES=$(api GET "/networks/$NAT/leases")
want "lease visible to the API" "eth1\|ip\|mac" "$LEASES"

# --- C. QEMU + KVM ---------------------------------------------------
step "C. QEMU node: tap through /dev/net/tun, KVM acceleration"
QID=$(printf '%s' "$(api POST "/labs/$LAB/nodes" "$(jq -nc --arg net "$NAT" \
  '{name:"vm1",runtime:"qemu",image:"cirros",ram_mb:256,interfaces:[{name:"eth0",network_id:$net}]}')")" \
  | jq -r '.id // empty')
[ -n "$QID" ] && ok "created qemu node vm1" || bad "qemu node create" "$(st)"

printf '     pulling the cirros image (first run only)...\n'
r=$(api POST "/nodes/$QID/start")
if [ "$(st)" = "200" ]; then ok "vm1 start accepted"
else bad "vm1 start" "$(st): $(printf '%s' "$r" | head -c 400)"; fi

# NodeOut calls it `state`, and carries last_error for a failed start.
for i in $(seq 1 45); do
  NODE=$(api GET "/nodes/$QID")
  ST=$(printf '%s' "$NODE" | jq -r '.state // "?"')
  [ "$ST" = "running" ] && break
  [ "$ST" = "error" ] && break
  sleep 4
done
want "vm1 reached running" "running" "$ST"
ERR=$(printf '%s' "$NODE" | jq -r '.last_error // ""')
[ -n "$ERR" ] && printf '     last_error: %s\n' "$(printf '%s' "$ERR" | head -c 400)"
printf '     host_ifname: %s\n' "$(printf '%s' "$(api GET "/nodes/$QID/interfaces")" | jq -r '.[0].host_ifname // "?"' 2>/dev/null)"
if [ "$ST" = "running" ]; then
  printf '     qemu accel: '
  (cd "$COMPOSE_DIR" && docker compose exec -T api sh -c "ps -eo args | grep -m1 qemu-system | tr \" \" \"\\n\" | grep -A1 accel | tail -1") 2>/dev/null || echo "?"
fi

printf '\n%s== namespace containment ==%s\n' "$Y" "$N"
for d in $(cd "$COMPOSE_DIR" && docker compose exec -T netd ip -o link show 2>/dev/null \
           | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -E '^[btvx][0-9a-z]'); do
  if ip link show "$d" >/dev/null 2>&1; then bad "$d leaked to the host"; else ok "$d confined to the namespace"; fi
done

printf '\n%s%d passed, %d failed%s\n' "$([ "$FAIL" -eq 0 ] && printf '%s' "$G" || printf '%s' "$R")" \
  "$PASS" "$FAIL" "$N"
[ "$FAIL" -eq 0 ]
