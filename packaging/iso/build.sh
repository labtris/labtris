#!/usr/bin/env bash
#
# Build the Labtris installer ISO.
#
# Takes Ubuntu's official server ISO, injects an autoinstall answer file, the
# Labtris source and the branding, and repacks it so it boots and installs
# unattended on BIOS and UEFI alike.
#
#   ./packaging/iso/build.sh                    # build with defaults
#   RELEASE=24.04.3 ./packaging/iso/build.sh    # a different point release
#   OUT=/tmp/x.iso ./packaging/iso/build.sh     # somewhere else
#
# Re-runnable: the upstream ISO is cached and checksum-verified, so a second
# build only redoes the parts that are ours.
#
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)

# Pinned rather than "latest": a build that silently changes base image between
# runs is not reproducible, and 26.04 will want its own testing pass.
RELEASE=${RELEASE:-24.04.3}
ARCH=${ARCH:-amd64}
BASE_ISO_NAME="ubuntu-${RELEASE}-live-server-${ARCH}.iso"
# Where the image comes from. releases.ubuntu.com is authoritative but is
# rate-limited hard from some networks — measured at 1.6 MB/s from eu-west-1
# against 40 MB/s from a mirror, which is two hours against ninety seconds.
MIRROR=${MIRROR:-https://releases.ubuntu.com/${RELEASE%.*}}
# Checksums always come from Ubuntu itself, whatever MIRROR is set to. That is
# the point: it is what makes using a faster third-party mirror safe rather
# than a matter of trusting whoever runs it.
SUMS_URL=${SUMS_URL:-https://releases.ubuntu.com/${RELEASE%.*}/SHA256SUMS}
CACHE=${CACHE:-$ROOT/.cache/iso}
WORK=${WORK:-$ROOT/.cache/iso-work}
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)
VERSION=${VERSION:-dev}
OUT=${OUT:-$ROOT/dist/labtris-${VERSION}-${ARCH}.iso}
LABEL=${LABEL:-LABTRIS}
#: Ubuntu codename matching RELEASE, used for the clean base the package set
#: is resolved against. Derived from RELEASE rather than defaulted
#: separately: the two were independent before, so `RELEASE=26.04.1
#: ./build.sh` would have fetched a resolute ISO and then resolved its
#: package set against noble, which fails in ways that look like a broken
#: mirror rather than a wrong codename.
case "${RELEASE%.*}" in
  24.04) SUITE_DEFAULT=noble ;;
  24.10) SUITE_DEFAULT=oracular ;;
  25.04) SUITE_DEFAULT=plucky ;;
  25.10) SUITE_DEFAULT=questing ;;
  26.04) SUITE_DEFAULT=resolute ;;
  *)     SUITE_DEFAULT="" ;;
esac
SUITE=${SUITE:-$SUITE_DEFAULT}
APT_MIRROR=${APT_MIRROR:-http://archive.ubuntu.com/ubuntu/}

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Validated here rather than beside the SUITE assignment above, because
# these call die() and it is not defined until this line.
[ -n "$SUITE" ] || die "no codename known for Ubuntu ${RELEASE%.*}; set SUITE= explicitly"

# 26.04 is not buildable yet, and the reason is not the ISO machinery.
#
# Guacamole USED to be half of this and no longer is: packaging/build-guacd.sh
# builds guacd 1.6.0 from the Apache release tarball for resolute, and
# install-labtris.sh installs that .deb when the release has no guacd of its
# own. What it cannot build is the RDP plugin — 1.6.0 does not compile against
# FreeRDP 3, the only FreeRDP resolute has — so a 26.04 image would have VNC,
# SSH and telnet consoles and no RDP.
#
# What still blocks it outright is Python: packages.txt needs python3.12 and
# python3.12-venv, and resolute has the interpreter but NOT the venv package,
# so the virtualenv step fails. That is a pyproject pin (>=3.12,<3.13) to
# lift and a test pass on a newer interpreter, not a packaging fix.
#
# Left as an explicit refusal rather than a build that fails an hour in on an
# apt resolve.
if [ "${RELEASE%.*}" = "26.04" ] && [ "${FORCE_SUITE:-0}" != 1 ]; then
  die "Ubuntu 26.04 ISOs are not buildable yet: packages.txt needs
       python3.12-venv, which resolute does not package (python3.12 itself is
       there, so installing that alone will not help).
       guacd is no longer a blocker — packaging/build-guacd.sh builds it,
       though without RDP, which needs FreeRDP 2.
       Use the container install on 26.04. FORCE_SUITE=1 to try anyway."
fi

# Everything this script starts dies with it. Without this, interrupting a
# build can leave its curl running: the orphan keeps writing the same partial
# file a later run is also writing, and the two interleave into an image that
# passes every check except the checksum, 3 GB later.
#
# Scoped to interruption, and to this script's own children. The first version
# was `kill 0` on EXIT, which signals the whole process group — so a *successful*
# build killed whatever invoked it, and handed back 144 (SIGURG) instead of 0.
# Running standalone that is invisible, because there is nothing else in the
# group; release.sh calling this was where it surfaced.
interrupted() {
  trap - INT TERM
  pkill -P $$ 2>/dev/null || true
  exit 130
}
trap interrupted INT TERM

# ---------------------------------------------------------------- dependencies
missing=""
for tool in xorriso curl sed; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
# 7z reads the ISO without needing a loop mount, which a container or an
# unprivileged CI runner cannot do.
command -v 7z >/dev/null 2>&1 || command -v 7zz >/dev/null 2>&1 || missing="$missing p7zip-full"
if [ -n "$missing" ]; then
  die "missing:$missing
  Debian/Ubuntu:  sudo apt-get install -y xorriso p7zip-full curl
  macOS:          brew install xorriso p7zip"
fi
SEVENZ=$(command -v 7z || command -v 7zz)

# ------------------------------------------------------------------- base ISO
mkdir -p "$CACHE"
BASE_ISO="$CACHE/$BASE_ISO_NAME"

# One build at a time. Two runs sharing a cache directory fight over the same
# partial download and produce a corrupt image rather than an error.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$CACHE/.build.lock"
  flock -n 9 || die "another build is already running (lock: $CACHE/.build.lock)"
fi
if [ ! -s "$BASE_ISO" ]; then
  say "Fetching $BASE_ISO_NAME (~3 GB, cached for next time)"
  # A progress bar redrawing into a log file writes tens of thousands of lines
  # and buries everything else, so it is only asked for on a terminal.
  if [ -t 1 ]; then PROGRESS=--progress-bar; else PROGRESS=--no-progress-meter; fi
  # No -C -: a partial file left by an interrupted run cannot be told apart
  # from a complete one, and resuming onto a truncated tail produces an image
  # that fails its checksum 3 GB later. Starting over costs ninety seconds on
  # a decent mirror and removes a whole class of confusing failure.
  rm -f "$BASE_ISO.part"
  curl -fL $PROGRESS -o "$BASE_ISO.part" "$MIRROR/$BASE_ISO_NAME"
  mv "$BASE_ISO.part" "$BASE_ISO"
else
  say "Using cached $BASE_ISO_NAME"
fi

say "Verifying checksum"
# Against Ubuntu's published SHA256SUMS. An ISO truncated by a dropped
# connection extracts far enough to look fine and produces a machine that
# fails to boot, which is a bad place to discover it.
SUMS=$(curl -fsSL "$SUMS_URL") || die "could not fetch $SUMS_URL"
WANT=$(printf '%s\n' "$SUMS" | awk -v f="*$BASE_ISO_NAME" '$2==f {print $1}')
[ -n "$WANT" ] || die "$BASE_ISO_NAME is not listed in $SUMS_URL"
if command -v sha256sum >/dev/null 2>&1; then
  GOT=$(sha256sum "$BASE_ISO" | awk '{print $1}')
else
  GOT=$(shasum -a 256 "$BASE_ISO" | awk '{print $1}')
fi
if [ "$WANT" != "$GOT" ]; then
  # Removed rather than left in place: a cached file that fails its checksum
  # is worthless, and leaving it means the next run fails the same way for
  # someone who has forgotten why.
  rm -f "$BASE_ISO"
  die "checksum mismatch — the cached image was corrupt and has been deleted.
  Re-run to download it again.
    expected $WANT
    got      $GOT"
fi

# ------------------------------------------------------------------- stage
# Only what we add is staged on disk. The Ubuntu filesystem is never extracted
# or rebuilt: xorriso copies the source image and applies our changes to it,
# which is both faster than unpacking 3 GB and the reason the boot record
# survives — it is replayed from the original rather than reconstructed.
say "Staging additions"
rm -rf "$WORK"
mkdir -p "$WORK/add/nocloud" "$WORK/add/labtris" "$WORK/add/labtris-overlay" "$WORK/grub"

# An optional SSH key for the installed machine, so a built ISO can be
# driven without a console. Off unless asked for: SSH_AUTHORIZED_KEY=...
# or SSH_AUTHORIZED_KEY_FILE=~/.ssh/id_ed25519.pub. Nothing is baked in by
# default — an ISO that shipped somebody's key would be a back door, and
# an ISO that shipped a *generated* key would be worse, because everyone
# would have the same one.
#
# This exists because testing the ISO means logging into the machine it
# builds, and the only account is one whose password must be changed on
# first use — which a script cannot do and an automated test should not.
prepare_autoinstall() {
  local src=$1 dst=$2 key=""
  if [ -n "${SSH_AUTHORIZED_KEY_FILE:-}" ]; then
    [ -r "$SSH_AUTHORIZED_KEY_FILE" ] || die "SSH_AUTHORIZED_KEY_FILE unreadable: $SSH_AUTHORIZED_KEY_FILE"
    key=$(head -1 "$SSH_AUTHORIZED_KEY_FILE")
  elif [ -n "${SSH_AUTHORIZED_KEY:-}" ]; then
    key=$SSH_AUTHORIZED_KEY
  fi
  if [ -z "$key" ]; then
    install -m 0644 "$src" "$dst"
    return
  fi
  case $key in
    ssh-*|ecdsa-*|sk-*) : ;;
    *) die "SSH_AUTHORIZED_KEY does not look like a public key" ;;
  esac
  # authorized-keys sits under the same `ssh:` block that already sets
  # install-server and allow-pw, so this only adds a key — it does not
  # change how passwords behave.
  # Two injections, both needed. The key alone is not enough: the overlay
  # ships a unit that runs `chage -d 0 labtris-admin` on first boot, and an
  # expired password makes PAM refuse every session — including one
  # authenticated by key, with "Password change required but no TTY
  # available". So an image built for unattended access also pre-creates
  # the marker file that unit already guards itself with, which leaves the
  # unit in place and simply satisfied. Passwords are unchanged; what
  # changes is that the account is not aged out from under the key.
  #
  # `touch`, not `: >`: a colon-space inside an unquoted YAML scalar makes
  # the rest a mapping, and the `>` that follows is then read as a folded
  # block indicator. The file parses as nonsense and the install dies.

  # Third injection, and the one to think about before using this mode.
  # A key that logs in and can then do nothing privileged is half a
  # feature: installing a kernel, restarting a unit or reading a root-only
  # log all need root, and every one of those is why an image gets
  # automated in the first place. So a keyed build also drops a sudoers
  # file granting NOPASSWD to the one admin account.
  #
  # The security shape, stated plainly: this only ever happens when the
  # operator passed a key at build time. A default ISO is untouched — no
  # key, no sudoers file. Password authentication is unchanged, so an
  # interactive operator is prompted exactly as before. The blast radius
  # is one account on an image the operator deliberately built for
  # unattended use, and the key is the thing protecting it. Do not use
  # this mode for an image that will be handed to anyone else.
  #
  # echo, not printf: a first attempt used `printf %s\\n` and the escape
  # did not survive Python, awk, YAML and sh in series — the installed
  # file read `NOPASSWD:ALLn`, sudo rejected it as a malformed path, and
  # the machine had a sudoers drop-in that did nothing. Verify the file
  # content, not just that the command ran.
  awk -v k="$key" '
    { print }
    /^  ssh:$/ { print "    authorized-keys:"; print "      - " k }
    /labtris-expire-admin\.service$/ {
      print "    - curtin in-target --target=/target -- sh -c '"'"'mkdir -p /var/lib/labtris && touch /var/lib/labtris/admin-expired'"'"'"
      print "    - curtin in-target --target=/target -- sh -c '"'"'echo \"labtris-admin ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/90-labtris-automation; chmod 0440 /etc/sudoers.d/90-labtris-automation'"'"'"
    }
  ' "$src" > "$dst"
  chmod 0644 "$dst"
  say "autoinstall carries an SSH key for ${key##* }"
}

prepare_autoinstall "$HERE/autoinstall/user-data" "$WORK/add/nocloud/user-data"
install -m 0644 "$HERE/autoinstall/meta-data" "$WORK/add/nocloud/meta-data"
# Subiquity looks here by itself, which is what lets the kernel command line
# stay free of the ds= argument and its semicolon. The nocloud directory above
# is kept as the fallback path for anyone who does pass ds= by hand.
prepare_autoinstall "$HERE/autoinstall/user-data" "$WORK/add/autoinstall.yaml"

# Build the web UI here rather than on the target. It is static output — a
# few hundred kilobytes of JS and CSS — so shipping the result removes Node,
# npm and the NodeSource script from the installed system entirely. That is
# one fewer network dependency and one fewer thing to go wrong at 3am in a
# training room.
if command -v npm >/dev/null 2>&1; then
  say "Building the web UI"
  ( cd "$ROOT/web" && npm install --silent && npm run build --silent )
  [ -d "$ROOT/web/dist" ] || die "npm run build produced no web/dist"
else
  say "  no npm — the ISO will carry no UI, and the API will serve REST only"
fi

# The working tree, minus everything rebuilt on the target. Baking the source
# in is what lets the install run on a network with no route to GitHub, which
# is the normal state of a training room. web/dist is deliberately NOT
# excluded: it is the prebuilt UI, and shipping it is the point.
# Every exclude is anchored with ./ so it matches only at the top level.
# --exclude=dist matches ANY component named dist, which silently took
# web/dist — the prebuilt UI this build exists to ship — along with the ISO
# output directory it was aimed at. The build reported "Building the web UI",
# the files were there, and none of them reached the disc.
# Virtualenvs are found rather than named. The list used to be `./.venv` and
# nothing else, so a second one — a .devvenv holding ruff, mypy and pytest —
# was copied onto the disc in full: 220 MB of tooling nobody installing this
# will ever run, and no way to notice short of listing the image. Any directory
# with a pyvenv.cfg in it is a virtualenv by definition, which is a rule that
# keeps working when the next one is called something else.
VENV_EXCLUDES=""
for cfg in "$ROOT"/*/pyvenv.cfg "$ROOT"/.*/pyvenv.cfg; do
  [ -f "$cfg" ] || continue
  VENV_EXCLUDES="$VENV_EXCLUDES --exclude=./$(basename "$(dirname "$cfg")")"
done
[ -n "$VENV_EXCLUDES" ] && say "  excluding virtualenvs:$VENV_EXCLUDES"

# shellcheck disable=SC2086 - the excludes are separate words on purpose
tar -C "$ROOT" \
    --exclude=./.git --exclude=./.venv --exclude=./node_modules \
    --exclude=./.cache --exclude=./dist --exclude=__pycache__ \
    $VENV_EXCLUDES \
    -cf - . | tar -C "$WORK/add/labtris" -xf -

[ -f "$WORK/add/labtris/web/dist/index.html" ] || [ ! -d "$ROOT/web/dist" ] \
  || die "web/dist was built but did not reach the staging tree — check the tar excludes"

cp -a "$HERE/overlay/." "$WORK/add/labtris-overlay/"

# ---------------------------------------------------------------- packages
# Carry the apt packages on the disc so the install does not depend on a
# working route to archive.ubuntu.com. A training room without internet is the
# normal case; discovering that halfway through an unattended install is not a
# good way to find out.
#
# Skipped when the build host is not Ubuntu, or when BUNDLE_DEBS=0: the
# downloaded packages must match the target's release and architecture, and a
# macOS build host cannot produce them at all.
DEBS_DIR="$WORK/add/labtris-debs"
mkdir -p "$DEBS_DIR"
if [ "${BUNDLE_DEBS:-1}" = "1" ] && command -v debootstrap >/dev/null 2>&1; then
  say "Resolving packages against a clean $SUITE"
  # Computed inside a minimal chroot of the target's own release, not on this
  # machine.
  #
  # The first attempt walked `apt-cache depends --recurse` on the build host
  # and produced 441 packages that were confidently wrong: it missed the whole
  # libguac subtree, because libguac-client-vnc0 is a transitional name for
  # libguac-client-vnc0t64 and the walk stopped at the virtual package, and it
  # missed the ghostscript chain for similar reasons. Everything installed
  # fine online — apt quietly fetched the gaps — and failed offline.
  #
  # Asking a clean system "what would you install?" cannot make that mistake,
  # because it is the same question the target will ask.
  BASE="$CACHE/base-$SUITE"
  if [ ! -x "$BASE/bin/true" ]; then
    say "  building a minimal $SUITE base (cached for later builds)"
    rm -rf "$BASE"
    debootstrap --variant=minbase --include=apt "$SUITE" "$BASE" "$APT_MIRROR" >/dev/null 2>&1 \
      || die "debootstrap failed — cannot resolve packages without a clean base"
  fi
  cat > "$BASE/etc/apt/sources.list" <<EOF
deb $APT_MIRROR $SUITE main universe
deb $APT_MIRROR $SUITE-updates main universe
deb http://security.ubuntu.com/ubuntu $SUITE-security main universe
EOF
  cp /etc/resolv.conf "$BASE/etc/resolv.conf" 2>/dev/null || true

  # Prove the mirror is usable before committing to a ~350 MB download.
  # archive.ubuntu.com rate-limits some networks to the point of stalling: from
  # this project's own build host it measured 0 KB/s while de.archive.ubuntu.com
  # served 1.2 MB/s. apt gives no hint when that happens — the build simply sits
  # in "Resolving packages" for hours looking like it is working, which is worse
  # than failing. Checked here so the message names the fix.
  MIRROR_SPEED=$(curl -s -o /dev/null -w '%{speed_download}' --max-time 15 \
      "${APT_MIRROR%/}/dists/$SUITE/Release" 2>/dev/null || echo 0)
  MIRROR_KBS=$(awk -v s="${MIRROR_SPEED:-0}" 'BEGIN{printf "%d", s/1024}')
  if [ "$MIRROR_KBS" -lt 50 ]; then
    die "$APT_MIRROR is serving at ${MIRROR_KBS} KB/s — too slow to bundle 350 MB.
  This is usually archive.ubuntu.com rate-limiting this network. Re-run against
  a mirror, for example:

      APT_MIRROR=http://de.archive.ubuntu.com/ubuntu/ ./packaging/iso/build.sh

  Packages are verified by apt's own signatures, so a third-party mirror is as
  safe as the archive."
  fi
  say "  mirror $APT_MIRROR at ${MIRROR_KBS} KB/s"
  rm -rf "$BASE/debs"; mkdir -p "$BASE/debs/partial"
  PKGS=$(grep -vE '^\s*(#|$)' "$ROOT/packaging/packages.txt" | tr '\n' ' ')
  # shellcheck disable=SC2086
  chroot "$BASE" env DEBIAN_FRONTEND=noninteractive sh -c "
      apt-get update -qq
      apt-get install -y --no-install-recommends --download-only \
        -o Dir::Cache::archives=/debs $PKGS" >/dev/null 2>&1 \
    || die "could not resolve the package set in a clean $SUITE"
  find "$BASE/debs" -name '*.deb' -exec cp {} "$DEBS_DIR/" \;
  rm -rf "$BASE/debs"

  # A pile of .deb files is not a package source: apt resolves against its
  # index, and the target's is older than this host's. Without an index it
  # ignores the newer files in front of it and goes to the network.
  command -v dpkg-scanpackages >/dev/null 2>&1 \
    || die "dpkg-scanpackages is missing (apt-get install dpkg-dev) — without an
  index the bundled packages are unusable and the install silently falls back
  to the network"
  ( cd "$DEBS_DIR" && dpkg-scanpackages . /dev/null 2>/dev/null | gzip -9c > Packages.gz )
  say "  $(find "$DEBS_DIR" -name '*.deb' | wc -l) packages, $(du -sh "$DEBS_DIR" | cut -f1), indexed"
else
  say "  skipping package bundling — the install will fetch them over the network"
fi

# ------------------------------------------------------------------ wheels
# The Python dependencies, so pip needs no route to PyPI either. Downloaded
# with the same interpreter the target runs, because a wheel built for 3.13
# will not install on 3.12 and the failure comes much later than the mistake.
WHEELS_DIR="$WORK/add/labtris-wheels"
mkdir -p "$WHEELS_DIR"
if [ "${BUNDLE_WHEELS:-1}" = "1" ] && command -v python3.12 >/dev/null 2>&1; then
  say "Downloading Python wheels for offline install"
  # setuptools and wheel first: with --no-index pip cannot fetch a build
  # backend, so installing the project itself needs them already present.
  python3.12 -m pip download --quiet --dest "$WHEELS_DIR" \
    setuptools wheel pip >/dev/null 2>&1 || true
  python3.12 -m pip download --quiet --dest "$WHEELS_DIR" "$ROOT" >/dev/null 2>&1 \
    || say "  some wheels could not be fetched — the install will fall back to PyPI"
  say "  $(find "$WHEELS_DIR" -type f | wc -l) wheels, $(du -sh "$WHEELS_DIR" | cut -f1)"
else
  say "  skipping wheel bundling — pip will fetch from PyPI during the install"
fi

# The boot splash is all-or-nothing. A theme whose script calls
# Image("logo.png") on a file that does not exist is not a degraded theme, it
# is a broken one: its initramfs hook fails, update-initramfs returns
# non-zero, and the whole install aborts over a splash screen. So either every
# asset is produced, or the theme is not shipped at all.
PLYMOUTH="$WORK/add/labtris-overlay/usr/share/plymouth/themes/labtris"
if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w 320 -h 320 "$ROOT/web/src/lib/logo-mark.svg" -o "$PLYMOUTH/logo.png"
elif command -v convert >/dev/null 2>&1; then
  convert -background none -resize 320x320 "$ROOT/web/src/lib/logo-mark.svg" "$PLYMOUTH/logo.png"
fi

if [ -s "$PLYMOUTH/logo.png" ]; then
  # One orange pixel, which the theme scales into the progress bar.
  base64 -d > "$PLYMOUTH/bar.png" <<'B64'
iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM
IQAAAABJRU5ErkJggg==
B64
else
  # Checked by testing for the asset rather than for the tool, so a rasteriser
  # that runs and produces nothing is caught too. Writing the remaining assets
  # into a directory this branch has just deleted is how the first attempt at
  # this fix killed the build with no message at all.
  say "  no rsvg-convert or convert — omitting the boot splash entirely"
  rm -rf "$WORK/add/labtris-overlay/usr/share/plymouth"
fi

# ------------------------------------------------------------------ boot menu
say "Branding the boot menu"
# grub.cfg is the only file of Ubuntu's we change, so it is the only one that
# has to come out of the image.
xorriso -osirrox on -indev "$BASE_ISO" \
        -extract /boot/grub/grub.cfg "$WORK/grub/grub.cfg" >/dev/null 2>&1 \
  || die "could not read /boot/grub/grub.cfg from $BASE_ISO_NAME"
chmod u+w "$WORK/grub/grub.cfg"

# `autoinstall` plus a seed on the disc is the whole trigger: Subiquity already
# knows how to run unattended, it just needs to find a datasource. ds=nocloud
# needs the trailing slash or cloud-init reads the last path segment as a file
# name and finds nothing.
# console= twice on purpose. Output goes to every console listed, and the last
# becomes the one userspace treats as /dev/console — so the screen still shows
# the install on a machine with a monitor, and a headless server reached over
# a serial port or a hypervisor's console shows it too. Without this the
# installer is invisible the moment the kernel takes over from GRUB, which is
# exactly when you want to see it.
# No ds=nocloud;s=... here on purpose. That syntax needs a semicolon, GRUB
# treats ; as a command separator, and the escape has to survive a sed
# replacement to reach the file intact — which it did not, producing an ISO
# that booted beautifully and then stopped to ask for a language.
#
# Subiquity reads /autoinstall.yaml from the install media on its own, so the
# argument is unnecessary. Removing it removes the whole class of bug.
KERNEL_ARGS='autoinstall console=tty0 console=ttyS0,115200'
sed -i \
  -e 's|^set timeout=.*|set timeout=5|' \
  -e 's|Try or Install Ubuntu Server|Install Labtris|g' \
  -e 's|Ubuntu Server with the HWE kernel|Install Labtris (HWE kernel)|g' \
  -e "s|---|$KERNEL_ARGS ---|g" \
  "$WORK/grub/grub.cfg"

grep -q 'autoinstall' "$WORK/grub/grub.cfg" \
  || die "the boot menu was not rewritten — Ubuntu's grub.cfg layout has changed"
# Belt and braces: the argument has to be there, and no stray semicolon may
# creep back in, because GRUB would silently split the line.
grep -q 'autoinstall console=' "$WORK/grub/grub.cfg" \
  || die "the kernel arguments were not applied to the boot menu"
grep -q 'ds=nocloud;' "$WORK/grub/grub.cfg" \
  && die "an unescaped semicolon reached grub.cfg — GRUB would split the line
  and the install would stop to ask for a language"
true

# ------------------------------------------------------------------- repack
say "Building the image"
mkdir -p "$(dirname "$OUT")"
# xorriso refuses an -outdev that already holds data, so a second build would
# fail on the artifact the first one produced. Removing it is right anyway:
# the alternative is a half-overwritten image that looks finished.
rm -f "$OUT"
# -boot_image any replay carries the original's El Torito entries, GRUB2 MBR
# and appended EFI partition across verbatim. Reconstructing those by hand is
# where remastering guides usually produce an image that boots on BIOS and not
# UEFI, or the reverse.
xorriso -indev "$BASE_ISO" -outdev "$OUT" \
        -boot_image any replay \
        -volid "$LABEL" \
        -map "$WORK/add/autoinstall.yaml" /autoinstall.yaml \
        -map "$WORK/add/nocloud"          /nocloud \
        -map "$WORK/add/labtris"          /labtris \
        -map "$WORK/add/labtris-overlay"  /labtris-overlay \
        -map "$WORK/add/labtris-debs"     /labtris-debs \
        -map "$WORK/add/labtris-wheels"   /labtris-wheels \
        -update "$WORK/grub/grub.cfg"     /boot/grub/grub.cfg \
        -commit 2>&1 | grep -aE "FAILURE|ISO image produced|completed successfully" | tail -3

[ -s "$OUT" ] || die "xorriso produced no image at $OUT"

# ------------------------------------------------------------------- verify
# Cheap, and the failure it catches is expensive: an image that lost one of its
# two boot entries installs fine on the machine you tested and not on the next.
say "Verifying the result"
REPORT=$(xorriso -indev "$OUT" -report_el_torito plain 2>/dev/null)
printf '%s\n' "$REPORT" | grep -q 'BIOS' || die "the rebuilt image has no BIOS boot entry"
printf '%s\n' "$REPORT" | grep -q 'UEFI' || die "the rebuilt image has no UEFI boot entry"
xorriso -indev "$OUT" -report_system_area plain 2>/dev/null | grep -q 'GPT' \
  || die "the rebuilt image lost its GPT — UEFI firmware will not boot it"
for path in /nocloud/user-data /labtris/packaging/install-labtris.sh; do
  xorriso -indev "$OUT" -find "$path" >/dev/null 2>&1 \
    || die "$path is missing from the built image"
done
say "  BIOS + UEFI boot entries, GPT and payload all present"

# A checksum of our own output, beside it. The build already verifies the
# checksum of Ubuntu's ISO on the way in; publishing one on the way out is the
# same courtesy to whoever downloads this. Written by the build rather than by
# hand at release time, because a checksum computed later is a checksum of
# whatever file happened to be in dist/ — including a half-finished one.
say "Checksumming"
SUMS="${OUT%.iso}.sha256"
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" ) > "$SUMS"
elif command -v shasum >/dev/null 2>&1; then
  ( cd "$(dirname "$OUT")" && shasum -a 256 "$(basename "$OUT")" ) > "$SUMS"
else
  die "no sha256sum or shasum — cannot checksum the image"
fi
printf '    %s\n' "$(cat "$SUMS")"

say "Built $OUT"
printf '    %s  %s\n' "$(du -h "$OUT" | cut -f1)" "$OUT"
printf '    %s\n' "$SUMS"
cat <<EOF

    Write it to a USB stick:
      sudo dd if=$OUT of=/dev/sdX bs=4M status=progress oflag=sync

    Or try it under QEMU first (no disk is touched):
      ./packaging/iso/test-boot.sh $OUT

    First login: labtris-admin / labtris — the password must be changed.
EOF
