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

REPO=${LABTRIS_REPO:-https://github.com/rajeshgangam/my-own-pnetlab.git}
BRANCH=${LABTRIS_BRANCH:-main}
PREFIX=${LABTRIS_PREFIX:-/opt/labtris}
FORCE_OS=${LABTRIS_FORCE_OS:-0}

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root (\`sudo bash\`, or pipe into \`sudo bash\`)"

# The installer targets Ubuntu 24.04 specifically — dependency versions are
# pinned to what noble ships. Refusing loudly rather than failing on a
# missing package six minutes in is the polite thing to do.
if [ "$FORCE_OS" != 1 ]; then
  . /etc/os-release 2>/dev/null || true
  if [ "${ID:-}" != ubuntu ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    die "this installer wants Ubuntu 24.04; found ${PRETTY_NAME:-unknown}.
       Set LABTRIS_FORCE_OS=1 to try anyway (nothing about the rest is
       Ubuntu-specific, but the package set is pinned to noble)."
  fi
  if [ "$(uname -m)" != x86_64 ]; then
    die "amd64 (x86_64) only — found $(uname -m). Set LABTRIS_FORCE_OS=1 to try."
  fi
fi

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
