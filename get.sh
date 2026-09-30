#!/usr/bin/env bash
#
# Fetch Labtris and hand off to the real installer.
#
#   curl -fsSL https://labtris.com/install | sudo bash
#
# Everything hard lives in packaging/install-labtris.sh — this script only
# has to get that far. Kept tiny on purpose: a curl-to-bash script that
# is doing anything clever is a script nobody reads before they run it.
#
# Override any of these on the command line:
#
#   sudo LABTRIS_BRANCH=some-topic bash -c "$(curl -fsSL labtris.com/install)"
#   sudo LABTRIS_REPO=https://github.com/labtris/labtris.git bash -c ...
#   sudo LABTRIS_PREFIX=/srv/labtris bash -c ...
#   sudo LABTRIS_FORCE_OS=1 bash -c ...           # try on non-24.04 anyway
#
set -euo pipefail

REPO=${LABTRIS_REPO:-https://github.com/labtris/labtris.git}
BRANCH=${LABTRIS_BRANCH:-main}
PREFIX=${LABTRIS_PREFIX:-/opt/labtris}
FORCE_OS=${LABTRIS_FORCE_OS:-0}

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root (\`sudo bash\`, or pipe into \`sudo bash\`)"

# The native installer targets Ubuntu 24.04 specifically — dependency
# versions are pinned to what noble ships. Refusing loudly rather than
# failing on a missing package six minutes in is the polite thing to do.
#
# 26.04 gets its own message rather than the generic one, because the two
# reasons it cannot work yet are concrete and neither is the user's fault.
# Both re-checked against packages.ubuntu.com on 2026-09-30:
#
#   python3.12-venv  absent from resolute. python3.12 ITSELF IS THERE, which
#                    is why this is worth stating precisely — the obvious
#                    fix, `apt install python3.12`, succeeds and changes
#                    nothing, because packages.txt and the `python3.12 -m
#                    venv` in install-labtris.sh both need the venv package.
#   guacd            absent from resolute; noble's 1.3.0-1.3ubuntu1 is the
#                    last one packaged. The VNC and RDP consoles have
#                    nothing to install.
#
# The container install has neither problem — it carries its own Python and
# pulls guacd as an image — so that is where 26.04 users are pointed.
#
# LABTRIS_FORCE_OS stays. It is the escape hatch, not the gate: removing it
# would leave someone on an untested distribution with no way through at
# all, which is the opposite of opening things up.
if [ "$FORCE_OS" != 1 ]; then
  . /etc/os-release 2>/dev/null || true
  if [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = "26.04" ]; then
    die "Ubuntu 26.04 is supported through the container install, not this one.

       curl -fsSL https://raw.githubusercontent.com/labtris/labtris/main/docker-compose.yml \\
         | docker compose -f - up -d

       Verified on 24.04 and 26.04, and it needs nothing from the host
       but Docker and /dev/kvm.

       The native install cannot work on resolute yet: python3.12-venv is
       not packaged there, so the virtualenv step fails — note that
       python3.12 itself IS available, so installing that alone will not
       help — and guacd is not packaged either, leaving the VNC and RDP
       consoles with nothing to install. The container install carries
       both itself.
       Set LABTRIS_FORCE_OS=1 to attempt it anyway."
  fi
  if [ "${ID:-}" != ubuntu ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    die "this installer wants Ubuntu 24.04; found ${PRETTY_NAME:-unknown}.
       The container install runs on any distribution with Docker:
         https://docs.labtris.com/reference/docker
       Set LABTRIS_FORCE_OS=1 to try anyway (nothing about the rest is
       Ubuntu-specific, but the package set is pinned to noble)."
  fi
  if [ "$(uname -m)" != x86_64 ]; then
    die "amd64 (x86_64) only — found $(uname -m). Set LABTRIS_FORCE_OS=1 to try."
  fi
fi

# Not a gate. Everything in Labtris runs on any kernel this OS ships —
# except soft-RoCE, which needs the per-namespace UDP 4791 socket that
# rdma_rxe gained in 7.1. Saying so here, before a five-minute install,
# beats letting someone discover it when an ib_send_bw moves zero bytes
# and prints no error. The installer repeats it in the closing summary.
KREL=$(uname -r)
case "${KREL%%.*}" in
  ''|*[!0-9]*) : ;;                       # unparseable — say nothing
  *)
    # No dot means no minor: without this a bare "7" parses as 7.7.
    _kmaj=${KREL%%.*}
    case "$KREL" in *.*) _krest=${KREL#*.}; _kmin=${_krest%%.*} ;; *) _kmin=0 ;; esac
    case "$_kmin" in ''|*[!0-9]*) _kmin=0 ;; esac
    if [ "$_kmaj" -lt 7 ] || { [ "$_kmaj" -eq 7 ] && [ "$_kmin" -lt 1 ]; }; then
      say "Note: kernel $KREL is below 7.1, so the RDMA lab will start and
    move no data. Every other lab, Ultra Ethernet included, is unaffected.
    https://labtris.com/install/guide#rdma-kernel"
    fi
    ;;
esac

say "Installing git, curl, ca-certificates (needed to clone)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends git curl ca-certificates

say "Fetching Labtris to $PREFIX (branch: $BRANCH)"
if [ -d "$PREFIX/.git" ]; then
  git -C "$PREFIX" fetch --depth 1 origin "$BRANCH"
  git -C "$PREFIX" checkout -f FETCH_HEAD
else
  # --depth 1: the installer needs the tree, not the history.
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$PREFIX"
fi

INSTALLER="$PREFIX/packaging/install-labtris.sh"
[ -x "$INSTALLER" ] || die "$INSTALLER is missing or not executable — corrupt clone?"

say "Handing off to $INSTALLER"
exec "$INSTALLER" --branch "$BRANCH" --source "$PREFIX"
