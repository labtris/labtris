#!/usr/bin/env bash
#
# Is this Labtris actually working?
#
#   labtris-health            # check everything, exit 0 if healthy
#   labtris-health --quiet    # print only failures
#
# Written for the five minutes after an upgrade, which is when "the unit is
# active" and "the product works" diverge. systemctl will happily report
# labtris-api active while every request 500s on a migration that did not
# finish, and nginx reloads cleanly with a certificate it cannot read.
#
# Exit status is the point: 0 healthy, 1 something is broken. Safe in a cron
# or a CI step.
#
# Read-only. It starts nothing, restarts nothing and writes nothing.
set -uo pipefail

PREFIX=${LABTRIS_PREFIX:-/opt/labtris}
CONFDIR=${CONFDIR:-/etc/labtris}
QUIET=0
FAIL=0
WARN=0

[ "${1:-}" = "--quiet" ] && QUIET=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

G='\033[0;32m'; R='\033[1;31m'; Y='\033[1;33m'; D='\033[0;90m'; N='\033[0m'
ok()   { [ "$QUIET" = 1 ] || printf "  ${G}ok${N}    %-26s %s\n" "$1" "${2:-}"; }
bad()  { printf "  ${R}FAIL${N}  %-26s %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
note() { printf "  ${Y}warn${N}  %-26s %s\n" "$1" "${2:-}"; WARN=$((WARN+1)); }
hdr()  { [ "$QUIET" = 1 ] || printf "\n${D}%s${N}\n" "$1"; }

# ------------------------------------------------------------------ identity
hdr "Install"
if [ -f "$PREFIX/pyproject.toml" ]; then
  VER=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$PREFIX/pyproject.toml" | head -1)
  REF=$(git -C "$PREFIX" -c "safe.directory=$PREFIX" rev-parse --short HEAD 2>/dev/null)
  ok "version" "${VER:-unknown} ${REF:+($REF)}"
else
  bad "version" "no $PREFIX/pyproject.toml — is LABTRIS_PREFIX right?"
fi

# ------------------------------------------------------------------ services
hdr "Services"
for u in labtris-api labtris-netd postgresql nginx; do
  if systemctl is-active --quiet "$u" 2>/dev/null; then
    ok "$u" "active"
  else
    bad "$u" "$(systemctl is-active "$u" 2>/dev/null || echo 'not found')"
  fi
done
# guacd is what the VNC and RDP console tabs connect through. Absent on a
# 26.04 install where Ubuntu no longer packages it, which is a documented
# gap rather than a broken install — hence warn, not fail.
if systemctl is-active --quiet guacd 2>/dev/null; then
  ok "guacd" "active (VNC/RDP consoles)"
else
  note "guacd" "not active — VNC and RDP console tabs will fail at connect"
fi

# --------------------------------------------------------------------- ports
hdr "Listeners"
# The API itself, behind nginx. If this is down nothing else matters, so it
# is checked directly rather than only through the proxy.
API=$(curl -s -o /dev/null -m 8 -w '%{http_code}' http://127.0.0.1:8080/ 2>/dev/null)
case "$API" in
  200|30?|401|403) ok "api 127.0.0.1:8080" "HTTP $API" ;;
  000)             bad "api 127.0.0.1:8080" "no answer — the app is not listening" ;;
  5??)             bad "api 127.0.0.1:8080" "HTTP $API — running but erroring" ;;
  *)               note "api 127.0.0.1:8080" "HTTP $API" ;;
esac

P443=$(curl -sk -o /dev/null -m 8 -w '%{http_code}' https://127.0.0.1/ 2>/dev/null)
case "$P443" in
  000) bad "https :443" "no answer" ;;
  5??) bad "https :443" "HTTP $P443" ;;
  *)   ok  "https :443" "HTTP $P443" ;;
esac

P80=$(curl -s -o /dev/null -m 8 -w '%{http_code}' http://127.0.0.1/ 2>/dev/null)
R80=$(curl -s -o /dev/null -m 8 -w '%{redirect_url}' http://127.0.0.1/ 2>/dev/null)
case "$P80" in
  30?) ok  "http :80" "$P80 -> ${R80:-?}" ;;
  000) bad "http :80" "no answer" ;;
  *)   note "http :80" "HTTP $P80 (expected a redirect to 443)" ;;
esac

P8081=$(curl -s -o /dev/null -m 8 -w '%{http_code}' http://127.0.0.1:8081/ 2>/dev/null)
case "$P8081" in
  000) note "http :8081" "no answer — tunnels forwarding this port will fail" ;;
  5??) bad  "http :8081" "HTTP $P8081" ;;
  *)   ok   "http :8081" "HTTP $P8081" ;;
esac

# ----------------------------------------------------------------------- tls
hdr "TLS"
CRT=/etc/labtris/tls/labtris.crt
if [ -s "$CRT" ]; then
  if END=$(openssl x509 -in "$CRT" -noout -enddate 2>/dev/null | cut -d= -f2); then
    if openssl x509 -in "$CRT" -noout -checkend 0 >/dev/null 2>&1; then
      SAN=$(openssl x509 -in "$CRT" -noout -ext subjectAltName 2>/dev/null | tail -1 | sed 's/^ *//')
      ok "certificate" "valid until $END"
      [ -n "$SAN" ] && ok "  SANs" "$SAN"
    else
      bad "certificate" "EXPIRED on $END"
    fi
  else
    bad "certificate" "$CRT is not readable as a certificate"
  fi
else
  note "certificate" "no $CRT — 443 cannot serve"
fi

# ---------------------------------------------------------------- data layer
hdr "Data"
DBURL=$(grep -h '^LABTRIS_DATABASE_URL=' "$CONFDIR/labtris.env" 2>/dev/null | cut -d= -f2-)
PG_URL=${DBURL/postgresql+asyncpg:/postgresql:}
if [ -n "$PG_URL" ]; then
  LABS=$(sudo -u postgres psql "$PG_URL" -tAc 'select count(*) from labs' 2>/dev/null ||
         psql "$PG_URL" -tAc 'select count(*) from labs' 2>/dev/null)
  if [ -n "${LABS:-}" ]; then
    ok "database" "reachable, $LABS lab(s)"
  else
    bad "database" "could not query it — a migration may have failed"
  fi
  # A schema behind the code is the classic half-finished upgrade: the app
  # starts, then 500s on the first query touching a new column.
  #
  # Both commands must run FROM $PREFIX. alembic.ini resolves script_location
  # relatively, so invoking it with an absolute -c from elsewhere fails — and
  # the first version of this check then parsed the error text and reported
  # "at 2026-10-01 but code wants FAILED:", a confident lie about a healthy
  # database. Hence the explicit emptiness test rather than trusting awk.
  if [ -x "$PREFIX/.venv/bin/alembic" ]; then
    # First field of the LAST non-empty line. alembic prints the revision
    # last and anything on stdout before it is noise — on this tree that is
    # a plugin registration warning, so taking the FIRST line read back a
    # log timestamp and reported the schema as "at 2026-10-01".
    rev() { awk 'NF {last=$1} END {print last}'; }
    HEAD=$(cd "$PREFIX" && ./.venv/bin/alembic -c alembic.ini heads 2>/dev/null | rev)
    CURR=$(cd "$PREFIX" && env "LABTRIS_DATABASE_URL=$DBURL" \
             ./.venv/bin/alembic -c alembic.ini current 2>/dev/null | rev)
    case "${HEAD:-}:${CURR:-}" in
      :*|*:)  note "schema" "could not read the revision — check alembic by hand" ;;
      *)
        if [ "$HEAD" = "$CURR" ]; then
          ok "schema" "at head ($CURR)"
        else
          bad "schema" "at $CURR but the code wants $HEAD — run the upgrade again"
        fi ;;
    esac
  fi
else
  note "database" "no LABTRIS_DATABASE_URL in $CONFDIR/labtris.env"
fi

# ------------------------------------------------------------------ features
hdr "Features"
if [ -f "$PREFIX/packaging/wireshark/uet.lua" ]; then
  ok "UET dissector" "present"
else
  note "UET dissector" "absent — a UET capture reads 'Unknown (253)'"
fi
# Wireshark disables Lua when it runs as root, so a root-run session decodes
# nothing and says nothing about why.
if command -v wireshark >/dev/null 2>&1; then
  ok "wireshark" "$(command -v wireshark)"
else
  note "wireshark" "not installed — the Wireshark session tab cannot open"
fi
KREL=$(uname -r); KMAJ=${KREL%%.*}
case "$KREL" in *.*) KR=${KREL#*.}; KMIN=${KR%%.*} ;; *) KMIN=0 ;; esac
case "$KMAJ" in ''|*[!0-9]*) KMAJ=0 ;; esac
case "$KMIN" in ''|*[!0-9]*) KMIN=0 ;; esac
if [ "$KMAJ" -gt 7 ] || { [ "$KMAJ" -eq 7 ] && [ "$KMIN" -ge 1 ]; }; then
  ok "kernel" "$KREL — soft-RoCE supported"
else
  note "kernel" "$KREL below 7.1 — sudo labtris-kernel --install"
fi
command -v docker >/dev/null 2>&1 && ok "docker" "$(docker --version 2>/dev/null | cut -d, -f1)" \
                                  || bad "docker" "absent — container nodes cannot start"
[ -e /dev/kvm ] && ok "/dev/kvm" "present" \
                || note "/dev/kvm" "absent — QEMU nodes fall back to emulation"

# -------------------------------------------------------------------- result
echo
if [ "$FAIL" -gt 0 ]; then
  printf "${R}%s check(s) failed${N}%s\n" "$FAIL" "$([ "$WARN" -gt 0 ] && echo ", $WARN warning(s)")"
  echo "  journalctl -u labtris-api -n 50 --no-pager"
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  printf "${Y}healthy, with %s warning(s)${N}\n" "$WARN"
  exit 0
fi
printf "${G}all checks passed${N}\n"
