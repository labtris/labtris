#!/usr/bin/env bash
#
# Install a mainline kernel new enough for soft-RoCE.
#
#   sudo labtris-kernel --check     # what you have, what you need
#   sudo labtris-kernel --install   # fetch and install, then reboot yourself
#
# WHY THIS EXISTS
#
# The RDMA lab needs Linux 7.1 or newer: that is where rdma_rxe gained a
# per-namespace UDP 4791 socket. Below it, a transfer between two lab nodes
# connects, exchanges GIDs and then moves no data, with nothing in any log to
# say why. No distribution ships 7.1 — Ubuntu 24.04 is on 6.8 and 26.04 on
# 7.0 — so every install reported "kernel too old" and offered no way out.
# Saying what is wrong without saying what to do about it is half an answer.
#
# WHAT IT DOES, AND THE HONEST COST
#
# Ubuntu's mainline kernel builds (kernel.ubuntu.com) are the same source
# Canonical builds releases from, packaged as .deb, but they are NOT
# supported: no security backports, no DKMS guarantees, and a kernel that
# regresses on your hardware is yours to debug. That is a reasonable trade on
# a lab box whose job is to run RDMA labs, and a bad one on anything you
# depend on.
#
# It therefore refuses to act without --install, prints what it will fetch,
# and never removes the kernel you are running. If the new one does not boot,
# pick the old one from the GRUB menu.
set -uo pipefail

MAINLINE=https://kernel.ubuntu.com/mainline
WANT_MAJ=7
WANT_MIN=1
MODE=check

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check)   MODE=check; shift ;;
    --install) MODE=install; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1  (try --help)" ;;
  esac
done

# ------------------------------------------------------------ what is running
KREL=$(uname -r)
KMAJ=${KREL%%.*}
case "$KREL" in *.*) _r=${KREL#*.}; KMIN=${_r%%.*} ;; *) KMIN=0 ;; esac
case "$KMAJ" in ''|*[!0-9]*) KMAJ=0 ;; esac
case "$KMIN" in ''|*[!0-9]*) KMIN=0 ;; esac

printf '  %-22s %s\n' "running kernel:" "$KREL"
printf '  %-22s %s\n' "soft-RoCE needs:" ">= ${WANT_MAJ}.${WANT_MIN}"

if [ "$KMAJ" -gt "$WANT_MAJ" ] || { [ "$KMAJ" -eq "$WANT_MAJ" ] && [ "$KMIN" -ge "$WANT_MIN" ]; }; then
  printf '  %-22s %s\n' "verdict:" "already new enough — nothing to do"
  # The module and the socket are the real test, not the version string.
  if lsmod 2>/dev/null | grep -q '^rdma_rxe'; then
    printf '  %-22s %s\n' "rdma_rxe:" "loaded"
  else
    printf '  %-22s %s\n' "rdma_rxe:" "not loaded — modprobe rdma_rxe"
  fi
  exit 0
fi
printf '  %-22s %s\n' "verdict:" "too old — the RDMA lab will move no data"

[ "$(uname -m)" = x86_64 ] || die "mainline builds here are amd64 only; found $(uname -m)"
command -v dpkg >/dev/null 2>&1 || die "not a Debian/Ubuntu system — install a 7.1+ kernel the way your distribution does"

# ------------------------------------------------------- find a mainline build
say "Looking for a mainline build >= ${WANT_MAJ}.${WANT_MIN}"
INDEX=$(curl -fsSL --max-time 30 "$MAINLINE/" 2>/dev/null) \
  || die "could not reach $MAINLINE
       Check the network, or install a kernel by hand."

# Directory names look like v7.1.4/ — take the newest that satisfies the floor
# and is not a release candidate. An -rc on a box meant to measure RDMA
# throughput is the wrong kind of adventurous.
VER=$(printf '%s\n' "$INDEX" |
      sed -n 's/.*href="v\([0-9][0-9.]*\)\/".*/\1/p' |
      grep -vi rc |
      awk -F. -v M="$WANT_MAJ" -v m="$WANT_MIN" \
        '($1>M) || ($1==M && $2>=m) {print}' |
      sort -V | tail -1)
[ -n "$VER" ] || die "no mainline build >= ${WANT_MAJ}.${WANT_MIN} is listed at $MAINLINE"
say "  newest suitable: v$VER"

if [ "$MODE" = check ]; then
  cat <<TXT

  To install it:
      sudo labtris-kernel --install

  Mainline builds are the same source Canonical releases from, packaged as
  .deb, but they are NOT supported: no security backports, and a regression on
  your hardware is yours to debug. Reasonable on a lab box, not on anything
  you depend on.
TXT
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "run as root (sudo labtris-kernel --install)"

# ------------------------------------------------------------------- download
WORK=$(mktemp -d /tmp/labtris-kernel.XXXXXX) || die "could not make a work directory"
trap 'rm -rf "$WORK"' EXIT
say "Fetching v$VER"
LIST=$(curl -fsSL --max-time 30 "$MAINLINE/v$VER/amd64/" 2>/dev/null) \
  || die "could not list $MAINLINE/v$VER/amd64/"

# The generic flavour only, and no -lowlatency, -cloud or debug packages: the
# headers and image are what a lab box needs and the rest is hundreds of MB.
FILES=$(printf '%s\n' "$LIST" |
        sed -n 's/.*href="\(linux-[^"]*\.deb\)".*/\1/p' |
        grep -E 'generic|_all\.deb' |
        grep -v lowlatency | grep -v dbg | sort -u)
[ -n "$FILES" ] || die "no generic .deb packages listed for v$VER"

for f in $FILES; do
  printf '  %s\n' "$f"
  curl -fsSL --max-time 300 -o "$WORK/$f" "$MAINLINE/v$VER/amd64/$f" \
    || die "download failed: $f"
done

# CHECKSUMS is published alongside the builds. Verifying it matters more here
# than almost anywhere else in this repo: this is a kernel, installed as root.
if SUMS=$(curl -fsSL --max-time 30 "$MAINLINE/v$VER/amd64/CHECKSUMS" 2>/dev/null); then
  say "Verifying checksums"
  ( cd "$WORK" && printf '%s\n' "$SUMS" | grep -E '^[0-9a-f]{64} ' > CHECKSUMS.sha256 2>/dev/null
    for f in $FILES; do
      grep -F " $f" CHECKSUMS.sha256 2>/dev/null | sha256sum -c - 2>/dev/null | grep -q OK \
        && printf '  ok    %s\n' "$f" \
        || { printf '  FAIL  %s\n' "$f"; exit 1; }
    done ) || die "a checksum did not match — nothing was installed"
else
  warn "no CHECKSUMS published for v$VER; proceeding unverified is your call"
  warn "stopping instead. Install by hand if you accept that."
  exit 1
fi

say "Installing"
dpkg -i "$WORK"/*.deb || { apt-get -f install -y; die "dpkg reported errors — check the output above"; }

say "Done"
cat <<TXT

  Installed v$VER alongside $KREL. Nothing was removed, so if the new kernel
  does not boot, choose the old one from the GRUB menu.

  Reboot, then confirm what this was for:

      sudo reboot
      uname -r
      sudo modprobe rdma_rxe
      ss -lun | grep 4791        # a per-namespace listener means it works

  Then start the rdma-pair lab. labtris-health will also stop warning.
TXT
