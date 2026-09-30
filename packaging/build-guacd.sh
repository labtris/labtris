#!/usr/bin/env bash
#
# Build guacd from the Apache source release and drop a .deb in
# packaging/guacd-debs/, for bundling onto the ISO and installing natively.
#
#   ./packaging/build-guacd.sh                       # this machine's arch
#   ARCH=amd64 ./packaging/build-guacd.sh            # what the ISO ships
#   GUAC_VERSION=1.6.0 ./packaging/build-guacd.sh
#
# WHY: Ubuntu 26.04 ships no guacd at all (noble's 1.3.0-1.3ubuntu1 was the
# last), so the native install there has dead VNC and RDP console tabs. This
# is one of the two things blocking 26.04 in get.sh; the other is the missing
# python3.12-venv, which this does not address.
#
# The tarball is fetched and unpacked HERE, on the host, rather than in the
# container. Unpacking inside a linux/amd64 container on an arm64 machine
# fails on every file with "Cannot open: Function not implemented" — qemu-user
# does not implement the syscalls GNU tar reaches for. Doing it host-side also
# puts the checksum check somewhere its failure is visible rather than buried
# in a build log.
set -euo pipefail

GUAC_VERSION=${GUAC_VERSION:-1.6.0}
ARCH=${ARCH:-$(uname -m)}
case "$ARCH" in
  x86_64|amd64) PLATFORM=linux/amd64 ;;
  aarch64|arm64) PLATFORM=linux/arm64 ;;
  *) echo "unsupported ARCH=$ARCH" >&2; exit 1 ;;
esac

# Pinned, not fetched alongside the tarball. A checksum downloaded from the
# same host as the file it describes verifies transport, not authenticity —
# anyone who can substitute one can substitute both. This value was read from
# downloads.apache.org on 2026-09-30 and belongs in review when it changes.
SHA256_1_6_0=8bc45675da96d7b6f39728160181e3d4ff3c08f460f6d26de5805b642bf13f2b

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK="$ROOT/packaging/dockerfiles/guacd"
OUT="$ROOT/packaging/guacd-debs"
TARBALL="guacamole-server-${GUAC_VERSION}.tar.gz"
CACHE="${TMPDIR:-/tmp}/labtris-guacd"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$CACHE" "$OUT"

say "Fetching guacamole-server ${GUAC_VERSION}"
if [ ! -s "$CACHE/$TARBALL" ]; then
  curl -fL --progress-bar -o "$CACHE/$TARBALL.part" \
    "https://downloads.apache.org/guacamole/${GUAC_VERSION}/source/${TARBALL}"
  mv "$CACHE/$TARBALL.part" "$CACHE/$TARBALL"
else
  say "  cached"
fi

say "Verifying checksum"
if [ "$GUAC_VERSION" = "1.6.0" ]; then
  WANT=$SHA256_1_6_0
else
  # No pin for a version this script has not been reviewed against. Fetching
  # it is better than skipping the check, and the warning says what it is
  # and is not worth.
  say "  no pinned digest for ${GUAC_VERSION} — fetching upstream's"
  say "  this checks transport only, not authenticity. Pin it before relying on it."
  WANT=$(curl -fsSL "https://downloads.apache.org/guacamole/${GUAC_VERSION}/source/${TARBALL}.sha256" | awk '{print $1}')
fi
[ -n "$WANT" ] || die "no expected digest"
if command -v sha256sum >/dev/null 2>&1; then
  GOT=$(sha256sum "$CACHE/$TARBALL" | awk '{print $1}')
else
  GOT=$(shasum -a 256 "$CACHE/$TARBALL" | awk '{print $1}')
fi
[ "$GOT" = "$WANT" ] || { rm -f "$CACHE/$TARBALL"; die "checksum mismatch
       expected $WANT
       got      $GOT
     The cached copy has been deleted; re-run to fetch again."; }
say "  $GOT"

say "Unpacking on the host (see the note at the top)"
rm -rf "$WORK/src"
mkdir -p "$WORK/src"
tar xzf "$CACHE/$TARBALL" -C "$WORK/src" --strip-components=1
[ -f "$WORK/src/configure" ] || die "no configure script in the unpacked tree"

say "Building for $PLATFORM (this is slow under emulation)"
docker build --platform "$PLATFORM" \
  --build-arg "GUAC_VERSION=$GUAC_VERSION" \
  --target artifact --output "type=local,dest=$OUT" \
  "$WORK"

# The build tree is an unpacked third-party release; leaving it behind would
# put ~30 MB of someone else's source under packaging/ where it looks vendored.
rm -rf "$WORK/src"

say "Done"
ls -lh "$OUT"/*.deb
