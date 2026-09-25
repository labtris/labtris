#!/usr/bin/env bash
#
# Smoke-test a Labtris install against the three checked-in demo pods.
#
#   ./packaging/smoke-test.sh                       # all three pods
#   ./packaging/smoke-test.sh --pod spine-leaf      # just one
#   ./packaging/smoke-test.sh --keep                # leave the labs behind
#   ./packaging/smoke-test.sh --url http://host:8081
#
# What it proves, in order: the API answers and authenticates; a pod loads;
# every node starts; the generated FRR config installs; and BGP EVPN
# sessions actually reach Established. That last one is the point — a lab
# whose nodes are all "running" tells you Docker works, not that the fabric
# converged.
#
# What it deliberately does NOT do: ping between hosts. The topology
# generator writes a BGP underlay but no addressing plan, so there are no
# host addresses to ping. Control-plane convergence is the honest ceiling.
#
# Auth: set LABTRIS_PASSWORD, or LABTRIS_TOKEN to skip the login round-trip.
# Pods are read off the SERVER's filesystem by default; when running this
# from somewhere else, set LABTRIS_POD_REMOTE=1 to upload them instead.
#
# Needs curl and jq. Deliberately bash 3.2 compatible so it runs from a
# macOS laptop against a remote host, not just on the Labtris box.
#
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)

API=${LABTRIS_API_URL:-http://127.0.0.1:8081}
USERNAME=${LABTRIS_USER:-admin}
PASSWORD=${LABTRIS_PASSWORD:-}
TOKEN=${LABTRIS_TOKEN:-}
POD_DIR=${LABTRIS_POD_DIR:-$ROOT/packaging/demo-pods}
POD_REMOTE=${LABTRIS_POD_REMOTE:-0}
KEEP=0
ONLY=""
# FRR establishes in a couple of seconds on a quiet host; the slack is for
# a loaded one, and for the apt-less cold start of a freshly loaded pod.
START_TIMEOUT=${START_TIMEOUT:-120}
BGP_TIMEOUT=${BGP_TIMEOUT:-90}

while [ $# -gt 0 ]; do
  case "$1" in
    --url)     API=$2; shift 2 ;;
    --pod)     ONLY=$2; shift 2 ;;
    --pod-dir) POD_DIR=$2; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    # Print the header block as help. awk rather than a hardcoded line
    # range so the two cannot drift, and `sub` rather than sed's `\?`,
    # which is a GNU extension that BSD sed silently ignores.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

API=${API%/}
V=$API/api/v1

# ---------------------------------------------------------------- output --

if [ -t 1 ]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi

FAILURES=""      # newline-separated; bash 3.2 has no nameref-friendly arrays here
FAILURE_COUNT=0
CREATED_LABS=""  # space-separated lab ids

step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() {
  printf '  %s✗%s %s\n' "$R" "$N" "$*"
  FAILURES="$FAILURES$*"$'\n'
  FAILURE_COUNT=$((FAILURE_COUNT + 1))
}
die()  { printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"; }

# ------------------------------------------------------------------- http --

# api METHOD PATH [JSON_BODY] — prints the response body, then a newline,
# then the HTTP status. The status rides along in the output on purpose:
# callers invoke this through $( ), which is a subshell, so a global set
# in here would never reach them. Split with rbody/rstatus.
api() {
  local method=$1 path=$2 body=${3:-}
  set -- -sS -X "$method" -w '\n%{http_code}' -H 'Accept: application/json'
  if [ -n "$TOKEN" ]; then
    set -- "$@" -H "Authorization: Bearer $TOKEN"
  fi
  if [ -n "$body" ]; then
    set -- "$@" -H 'Content-Type: application/json' -d "$body"
  fi
  # 000 is curl's own "never got a response"; callers treat it as a failure
  # like any other non-2xx rather than dying deep inside a subshell.
  curl "$@" "$V$path" 2>/dev/null || printf '\n000'
}

rbody()   { printf '%s' "${1%$'\n'*}"; }
rstatus() { printf '%s' "${1##*$'\n'}"; }
is_ok()   { case "$1" in 2[0-9][0-9]) return 0 ;; *) return 1 ;; esac; }

api_ok() {
  local resp status body detail
  resp=$(api "$@")
  status=$(rstatus "$resp")
  body=$(rbody "$resp")
  if ! is_ok "$status"; then
    detail=$(printf '%s' "$body" | jq -r '.detail // .message // empty' 2>/dev/null || true)
    if [ "$status" = "000" ]; then
      die "cannot reach $V$2 — is the API up at $API?"
    fi
    die "$1 $2 returned HTTP $status${detail:+ — $detail}"
  fi
  printf '%s' "$body"
}

# ------------------------------------------------------------------- pods --

pod_file() {
  case "$1" in
    spine-leaf)     echo "spine-leaf-2x2-evpn.pod.tar.gz" ;;
    rail-optimised) echo "rail-optimised-2x2.pod.tar.gz" ;;
    fat-tree)       echo "fat-tree-k2.pod.tar.gz" ;;
    *)              return 1 ;;
  esac
}

ALL_PODS="spine-leaf rail-optimised fat-tree"

# ---------------------------------------------------------------- preflight --

need curl
need jq

step "Preflight"
printf '  API      %s\n' "$API"
printf '  pods     %s\n' "$POD_DIR"

ORDER=$ALL_PODS
if [ -n "$ONLY" ]; then
  pod_file "$ONLY" >/dev/null || die "unknown pod '$ONLY' — one of: $ALL_PODS"
  ORDER=$ONLY
fi

if [ -z "$TOKEN" ]; then
  [ -n "$PASSWORD" ] || die "set LABTRIS_PASSWORD (or LABTRIS_TOKEN) — this script will not prompt"
  login=$(api_ok POST /auth/login \
    "$(jq -nc --arg u "$USERNAME" --arg p "$PASSWORD" '{username:$u,password:$p}')")
  TOKEN=$(printf '%s' "$login" | jq -r '.token // empty')
  [ -n "$TOKEN" ] || die "login succeeded but returned no token"
  ok "authenticated as $USERNAME"
else
  ok "using LABTRIS_TOKEN from the environment"
fi

api_ok GET /health | jq -e '.' >/dev/null 2>&1 || die "/health did not return JSON"
ok "API healthy"

cleanup() {
  [ -z "$CREATED_LABS" ] && return
  if [ "$KEEP" -eq 1 ]; then
    printf '\n%s labs kept:%s%s\n' "$Y" "$CREATED_LABS" "$N"
    return
  fi
  step "Cleanup"
  for lab in $CREATED_LABS; do
    api DELETE "/labs/$lab" >/dev/null || true
    ok "removed lab $lab"
  done
}
trap cleanup EXIT

# exec_node NODE_ID COMMAND [TIMEOUT] — run a shell command inside a node.
# Prints "<exit_code>|<stdout>" — same subshell reasoning as api(). An exit
# code of 125 means the call itself failed, which is distinct from the
# command running and failing.
exec_node() {
  local id=$1 cmd=$2 t=${3:-15} resp status body rc out
  resp=$(api POST "/nodes/$id/console/exec" \
    "$(jq -nc --arg c "$cmd" --argjson t "$t" '{command:$c,timeout_s:$t}')")
  status=$(rstatus "$resp")
  body=$(rbody "$resp")
  if ! is_ok "$status"; then
    printf '125|'
    return 0
  fi
  rc=$(printf '%s' "$body" | jq -r '.exit_code // 0' 2>/dev/null || echo 0)
  out=$(printf '%s' "$body" | jq -r '.stdout // ""' 2>/dev/null || true)
  printf '%s|%s' "$rc" "$out"
}

exec_rc()  { printf '%s' "${1%%|*}"; }
exec_out() { printf '%s' "${1#*|}"; }

# Count BGP peers in Established state. Walks the document for any object
# carrying a "state" rather than pinning to one FRR version's schema, and
# answers 0 for anything that is not JSON at all.
established_count() {
  printf '%s' "$1" | jq -r '
    [.. | objects | select(has("state")) | .state]
    | map(select(. == "Established")) | length' 2>/dev/null || echo 0
}

run_pod() {
  local key=$1 file path loaded lab_id lab_name node_count detail nodes
  file=$(pod_file "$key")
  path=$POD_DIR/$file

  step "Pod: $key"
  if [ ! -f "$path" ]; then
    fail "$key — archive not found at $path"
    return
  fi

  # The API reads the archive off the SERVER's filesystem. Running this from
  # a laptop means naming a path the server cannot see, so upload instead.
  if [ "$POD_REMOTE" = "1" ]; then
    loaded=$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
      -F "file=@$path" -F "name=smoke-$key-$$" "$V/pods/upload") \
      || { fail "$key — upload failed"; return; }
  else
    local resp status
    resp=$(api POST /pods/load \
      "$(jq -nc --arg p "$path" --arg n "smoke-$key-$$" '{path:$p,name:$n}')")
    status=$(rstatus "$resp")
    loaded=$(rbody "$resp")
    if ! is_ok "$status"; then
      fail "$key — load failed (HTTP $status). Not running on the Labtris host? Re-run with LABTRIS_POD_REMOTE=1 to upload instead."
      return
    fi
  fi

  lab_id=$(printf '%s' "$loaded" | jq -r '.lab_id // empty')
  lab_name=$(printf '%s' "$loaded" | jq -r '.name // empty')
  node_count=$(printf '%s' "$loaded" | jq -r '.node_count // 0')
  [ -n "$lab_id" ] || { fail "$key — load returned no lab_id"; return; }
  CREATED_LABS="$CREATED_LABS $lab_id"
  ok "loaded as '$lab_name' ($node_count nodes)"

  # --- start every node -------------------------------------------------
  detail=$(api_ok GET "/labs/$lab_id")
  nodes=$(printf '%s' "$detail" | jq -c '.nodes[] | {id,name}')

  local started=0 nid nname sresp sstatus
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    nid=$(printf '%s' "$n" | jq -r '.id')
    nname=$(printf '%s' "$n" | jq -r '.name')
    sresp=$(api POST "/nodes/$nid/start")
    sstatus=$(rstatus "$sresp")
    if is_ok "$sstatus"; then
      started=$((started + 1))
    else
      fail "$key — $nname failed to start (HTTP $sstatus)"
    fi
  done <<EOF
$nodes
EOF
  ok "issued start for $started node(s)"

  # --- wait for running -------------------------------------------------
  local deadline=$((SECONDS + START_TIMEOUT)) running=0 total=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    detail=$(api_ok GET "/labs/$lab_id")
    running=$(printf '%s' "$detail" | jq '[.nodes[] | select(.state=="running")] | length')
    total=$(printf '%s' "$detail" | jq '.nodes | length')
    [ "$running" -eq "$total" ] && break
    sleep 3
  done
  if [ "$running" -eq "$total" ]; then
    ok "all $total node(s) running"
  else
    fail "$key — only $running/$total nodes reached running within ${START_TIMEOUT}s"
    printf '%s' "$detail" \
      | jq -r '.nodes[] | select(.state!="running") | "      \(.name): \(.state) \(.last_error // "")"'
    return
  fi

  # --- install the generated FRR config ---------------------------------
  # Each generated node carries a self-installing script in startup_config:
  # push writes it to /config/startup-config, running it writes frr.conf and
  # HUPs watchfrr. Plain hosts have no config and answer 409 — that is not a
  # failure, it is what an alpine node looks like.
  local configured=0 frr_nodes="" presp pstatus eres erc
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    nid=$(printf '%s' "$n" | jq -r '.id')
    nname=$(printf '%s' "$n" | jq -r '.name')
    presp=$(api POST "/nodes/$nid/config/push")
    pstatus=$(rstatus "$presp")
    if [ "$pstatus" = "409" ]; then
      continue
    elif ! is_ok "$pstatus"; then
      fail "$key — pushing config to $nname returned HTTP $pstatus"
      continue
    fi
    eres=$(exec_node "$nid" 'sh /config/startup-config' 30)
    erc=$(exec_rc "$eres")
    if [ "$erc" != "0" ]; then
      fail "$key — installing config on $nname exited $erc"
      continue
    fi
    configured=$((configured + 1))
    frr_nodes="$frr_nodes$nid|$nname"$'\n'
  done <<EOF
$nodes
EOF

  if [ "$configured" -eq 0 ]; then
    fail "$key — no node carried a startup-config; this pod may predate BGP generation"
    return
  fi
  ok "installed FRR config on $configured node(s)"

  # --- BGP EVPN convergence ---------------------------------------------
  local bgp_deadline=$((SECONDS + BGP_TIMEOUT)) converged=0 summary="" count
  while [ "$SECONDS" -lt "$bgp_deadline" ]; do
    converged=1
    summary=""
    while IFS= read -r entry; do
      [ -z "$entry" ] && continue
      nid=${entry%%|*}
      nname=${entry#*|}
      eres=$(exec_node "$nid" 'vtysh -c "show bgp l2vpn evpn summary json" 2>/dev/null' 20)
      count=$(established_count "$(exec_out "$eres")")
      [ -z "$count" ] && count=0
      summary="$summary$nname=$count "
      if [ "$count" -lt 1 ]; then
        converged=0
      fi
    done <<EOF
$frr_nodes
EOF
    [ "$converged" -eq 1 ] && break
    sleep 5
  done

  if [ "$converged" -eq 1 ]; then
    ok "BGP EVPN converged — established peers: ${summary% }"
  else
    fail "$key — BGP EVPN did not converge within ${BGP_TIMEOUT}s: ${summary% }"
    local first_id first_name
    first_id=$(printf '%s' "$frr_nodes" | head -1 | cut -d'|' -f1)
    first_name=$(printf '%s' "$frr_nodes" | head -1 | cut -d'|' -f2)
    printf '      last summary from %s:\n' "$first_name"
    exec_out "$(exec_node "$first_id" 'vtysh -c "show bgp l2vpn evpn summary"' 20)" \
      | sed 's/^/      /'
  fi
}

for key in $ORDER; do
  run_pod "$key"
done

# ---------------------------------------------------------------- verdict --

printf '\n'
if [ "$FAILURE_COUNT" -eq 0 ]; then
  printf '%s✓ smoke test passed%s — fabric converged on: %s\n' "$G" "$N" "$ORDER"
  exit 0
fi
printf '%s✗ smoke test failed%s — %d problem(s):\n' "$R" "$N" "$FAILURE_COUNT"
printf '%s' "$FAILURES" | sed 's/^/    - /'
exit 1
