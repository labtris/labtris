#!/usr/bin/env bash
#
# Install Labtris on Ubuntu 24.04 (26.04: use the container install — see
# get.sh for why). Idempotent: safe to re-run over an existing
# install, which is what makes it usable both as the ISO's late-command and as
# the upgrade path on a machine somebody already provisioned by hand.
#
# This is the executable form of docs/reference/install-from-source.mdx.
# Where the two disagree the script wins, because it is the one that gets run.
#
#   sudo ./packaging/install-labtris.sh [--source DIR] [--branch NAME]
#   sudo ./packaging/install-labtris.sh --finalise      (see below)
#
# TWO PHASES, and the split is not cosmetic.
#
# An ISO's late-commands run under `curtin in-target`, which is a chroot: the
# packages are installed into a filesystem that is not running. systemd is not
# PID 1 there, so `systemctl --now` cannot reach the bus and fails, and there
# is no running Postgres to create a database in. The first version of this
# script did all of that inline and died at the first systemctl, after apt had
# succeeded — an install that looked fine until you noticed /opt/labtris was
# never created.
#
# So: everything that only touches the filesystem happens now, and everything
# that needs a live machine is deferred to a first-boot unit that re-invokes
# this script with --finalise. On an already-booted host both phases run back
# to back and the split is invisible.
#
set -euo pipefail

LABTRIS_USER=${LABTRIS_USER:-labtris}
PREFIX=${PREFIX:-/opt/labtris}
CONFDIR=${CONFDIR:-/etc/labtris}
REPO=${REPO:-https://github.com/labtris/labtris.git}
BRANCH=${BRANCH:-main}
SOURCE=""
MODE=install
#: Set when the web UI could not be produced, so the closing summary can say
#: so rather than printing a URL that serves nothing but JSON.
UI_MISSING=0
#: Directories of packages carried on the install media, if any.
DEBS=${DEBS:-}
WHEELS=${WHEELS:-}
#: Set by the ISO's late-command. Authoritative: never start anything.
STAGED=${STAGED:-0}

while [ $# -gt 0 ]; do
  case "$1" in
    --source)   SOURCE=$2; shift 2 ;;
    --branch)   BRANCH=$2; shift 2 ;;
    --repo)     REPO=$2; shift 2 ;;
    --finalise|--finalize) MODE=finalise; shift ;;
    --staged)   STAGED=1; shift ;;
    --debs)     DEBS=$2; shift 2 ;;
    --wheels)   WHEELS=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

#: Whether phase two can run here and now.
#:
#: The obvious test — [ -d /run/systemd/system ] — is wrong in exactly the
#: place it matters. `curtin in-target` bind-mounts the live installer's /run
#: into the target, so that directory exists and belongs to the installer, not
#: to the system being built. It reported "systemd is running", finalise() ran,
#: and `systemctl enable --now` wrote its symlinks into the target while trying
#: to *start* services on the installer. The enable half succeeded and the
#: start half failed, which is why the second attempt got all the way to the
#: last block before dying.
#:
#: So: an explicit flag from the caller first, because the ISO knows perfectly
#: well that it is staging an image and should not have to be guessed at. The
#: chroot probe is the fallback for anyone running this by hand somewhere odd.
should_finalise_now() {
  [ "$STAGED" = 1 ] && return 1
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    systemd-detect-virt --chroot >/dev/null 2>&1 && return 1
  fi
  [ -d /run/systemd/system ] || return 1
  return 0
}

# --------------------------------------------------------------- phase two
# Everything that needs a live machine: running services, a reachable
# database, a systemd bus to talk to.
finalise() {
  say "Starting services"
  systemctl enable --now postgresql docker

  say "Database"
  local db_pass
  db_pass=$(cat "$CONFDIR/db-password")
  # Wait for Postgres: `systemctl --now` returns once the unit is active, which
  # on first boot can still be before the socket accepts connections.
  local tries=0
  until sudo -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -gt 30 ] && { echo "postgres did not become ready" >&2; exit 1; }
    sleep 2
  done
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='labtris'" | grep -q 1 \
    || sudo -u postgres psql -qc "CREATE ROLE labtris LOGIN PASSWORD '$db_pass'"
  sudo -u postgres psql -qc "ALTER ROLE labtris PASSWORD '$db_pass'"
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='labtris'" | grep -q 1 \
    || sudo -u postgres createdb -O labtris labtris

  say "Schema"
  # Run from $PREFIX, not from wherever systemd started us. alembic resolves
  # script_location relative to the working directory rather than to the -c
  # file, so `migrations` became `/migrations` and it failed with "Path doesn't
  # exist" — invisible during development, where alembic is always run from
  # inside the checkout. A subshell so the rest of finalise() is unaffected.
  (
    cd "$PREFIX" || exit 1
    env "$(grep -h '^LABTRIS_DATABASE_URL=' "$CONFDIR/labtris.env")" \
      "$PREFIX/.venv/bin/alembic" -c "$PREFIX/alembic.ini" upgrade head
  )

  say "Labtris services"
  install -d -o "$LABTRIS_USER" -g "$LABTRIS_USER" /run/labtris
  systemctl daemon-reload
  systemctl enable --now labtris-ksm labtris-netd labtris-api guacd

  say "Reverse proxy"
  nginx -t && systemctl reload nginx

  # Run it now rather than waiting for a reboot. On an upgrade nobody
  # reboots, and "the new labs arrive next time you restart the machine" is
  # indistinguishable from "they never arrived".
  # A file test, not `systemctl list-unit-files | grep`. That guard silently
  # evaluated false on a real 0.12 -> 0.13 upgrade — the unit had just been
  # installed and enabled in phase one, and the block still did not run — and
  # `|| true` on the start meant nothing said so. The labs did not appear and
  # the log showed no reason. Whether the cause was systemctl's output format,
  # a pager or the bus does not matter: the unit file either exists or it does
  # not, and that is what this now asks.
  if [ -f /etc/systemd/system/labtris-seed-pods.service ]; then
    say "Demo labs"
    # The file may have been written moments ago in phase one, so systemd has
    # to be told before `start` can resolve the name.
    systemctl daemon-reload
    if systemctl start labtris-seed-pods.service 2>/tmp/seed-start.err; then
      journalctl -u labtris-seed-pods -n 20 --no-pager 2>/dev/null |
        sed -n 's/.*sh\[[0-9]*\]: \(loaded\|skipped\|failed\):/  \1:/p' | tail -3
    else
      say "  could not start labtris-seed-pods:"
      sed 's/^/    /' /tmp/seed-start.err 2>/dev/null | head -3
      say "  the demo labs are not loaded. Run it by hand:"
      say "    sudo systemctl start labtris-seed-pods"
    fi
    rm -f /tmp/seed-start.err
  else
    say "  no seed unit — demo labs not loaded"
  fi

  # Print (and save) the one page every operator wants to read once and
  # then find again in a week. A file at 0600 in /root because the DB
  # password path is on it — nothing secret in it directly, but everything
  # pointing at things that are.
  local ip version summary krel kmaj kmin krest rdma_note
  ip=$(hostname -I | awk '{print $1}')
  # soft-RoCE needs the per-namespace UDP 4791 socket rdma_rxe gained in
  # Linux 7.1. Below that a transfer connects, exchanges GIDs and moves
  # nothing, with no error anywhere — so the number is worth printing
  # even when it is fine, to save the reader checking.
  krel=$(uname -r)
  # `${krel#*.}` returns the whole string when there is no dot, which
  # would read a bare "7" as 7.7 and call it supported. Hence the case.
  kmaj=${krel%%.*}
  case "$krel" in *.*) krest=${krel#*.}; kmin=${krest%%.*} ;; *) kmin=0 ;; esac
  case "$kmaj" in ''|*[!0-9]*) kmaj=0 ;; esac
  case "$kmin" in ''|*[!0-9]*) kmin=0 ;; esac
  if [ "$kmaj" -gt 7 ] || { [ "$kmaj" -eq 7 ] && [ "$kmin" -ge 1 ]; }; then
    rdma_note="kernel $krel — soft-RoCE supported"
  else
    rdma_note="kernel $krel is below 7.1 — run: sudo labtris-kernel --install"
  fi
  version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$PREFIX/pyproject.toml" | head -1)
  summary=/root/labtris-install-summary.txt
  {
    echo "Labtris ${version:-installed}."
    echo
    if [ "${UI_MISSING:-0}" = "1" ]; then
      printf '  %-20s %s\n' "Web UI:"        "NOT INSTALLED — REST API only"
      printf '  %-20s %s\n' ""               "cd $PREFIX/web && npm install && npm run build"
    else
      printf '  %-20s %s\n' "Web UI:"        "https://${ip}/"
      printf '  %-20s %s\n' ""               "(self-signed — your browser warns once)"
      printf '  %-20s %s\n' "Also on:"       "http://${ip}:8081  (plain HTTP, handy over an ssh -L tunnel)"
      printf '  %-20s %s\n' "First login:"   "create it at that URL"
    fi
    printf '  %-20s %s\n' "Config:"          "$CONFDIR/labtris.env"
    printf '  %-20s %s\n' "DB password:"     "$CONFDIR/db-password"
    printf '  %-20s %s\n' "Service user:"    "$LABTRIS_USER"
    printf '  %-20s %s\n' "Prefix:"          "$PREFIX"
    printf '  %-20s %s\n' "Health check:"    "labtris-health"
    printf '  %-20s %s\n' "Upgrade:"         "sudo labtris-upgrade"
    # Reported rather than warned about, and reported for THIS machine.
    # It is the one bundled lab with a host prerequisite, and the ISO
    # route shares this summary — which is the case where the reader had
    # no say over the kernel at all.
    printf '  %-20s %s\n' "RDMA labs:"       "$rdma_note"
    printf '  %-20s %s\n' "Logs:"            "journalctl -u labtris-api -u labtris-netd -f"
    printf '  %-20s %s\n' "Restart:"         "systemctl restart labtris-api labtris-netd"
    echo
    printf '  %-20s %s\n' "Docs:"            "https://docs.labtris.com"
    printf '  %-20s %s\n' "Source:"          "$REPO"
    echo
    echo "This summary is also saved at $summary (0600)."
  } | tee "$summary"
  chmod 0600 "$summary"
}

if [ "$MODE" = finalise ]; then
  finalise
  exit 0
fi

# --------------------------------------------------------------- phase one
say "Packages"
export DEBIAN_FRONTEND=noninteractive
# Answer wireshark-common's setuid question before it is asked. That question
# is the whole feature: saying yes puts cap_net_admin,cap_net_raw on dumpcap
# and restricts it to the `wireshark` group. Without it Wireshark starts and
# then shows no interfaces at all, which looks like a Labtris bug and is not.
echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections
# Tolerated: on a machine with no route out this fails, and with the packages
# on the media there is nothing it needed to do anyway. Without the guard,
# set -e turns "no internet" into a failed install that reports a stale index
# rather than the thing that actually matters.
apt-get update -qq || say "  apt update failed — continuing with what is on the media"
# Kept in step with docs/reference/install-from-source.mdx §1. p7zip is not
# optional: the osboxes
# images ship as .7z and the QEMU backend extracts before converting.
# libguac-client-rdp0 is separate from guacd — without it RDP tunnels fine and
# then fails at connect, because guacd only advertises installed plugins.
PACKAGES=$(grep -vE '^\s*(#|$)' "${PKGLIST:-$(dirname "$0")/packages.txt}" | tr '\n' ' ')

# Register the media as an apt source, rather than handing apt a list of files.
#
# Passing loose .deb paths does not work: apt resolves against its index, which
# on a freshly installed target is whatever the ISO's base image shipped —
# older than the packages we bundled. It ignores the newer files sitting in
# front of it and goes to the network for versions it recognises.
#
# As a repository with a Packages index, the bundled versions become candidates
# the solver prefers, and nothing is fetched. trusted=yes because the packages
# came off the same disc as this script; signing them would mean shipping a key
# to verify a disc the machine already booted from.
if [ -n "$DEBS" ] && [ -s "$DEBS/Packages.gz" ]; then
  say "  using $(ls "$DEBS"/*.deb 2>/dev/null | wc -l) packages from the media"
  echo "deb [trusted=yes] file:$DEBS ./" > /etc/apt/sources.list.d/labtris-local.list
  apt-get update -qq -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/labtris-local.list \
                     -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0 \
    || say "  could not read the local repository index"
elif [ -n "$DEBS" ]; then
  say "  $DEBS has no Packages.gz — it is a directory of files, not a repository"
fi

# Only ask the network for what is genuinely absent.
#
# The previous version called apt unconditionally afterwards "to confirm
# nothing is missing". Offline that is fatal rather than reassuring: with no
# package index apt cannot resolve a name at all, so `apt-get install nginx`
# fails with "Unable to locate package" even though nginx is installed and
# running. A complete set of media packages must mean no network call, not a
# network call that happens to be redundant.
# Guacamole is not in every release, and where it is absent we carry our own.
#
# Ubuntu stopped packaging guacd after noble: 26.04 has no guacd,
# libguac-client-vnc0 or libguac-client-rdp0 at all. Leaving them in the list
# there makes apt fail the whole install on "Unable to locate package", and
# dropping them silently gives a machine whose VNC console fails at connect
# with nothing explaining why.
#
# So: ask apt whether it has a candidate, and if not, use the .deb that
# packaging/build-guacd.sh builds from the Apache release tarball. That
# package Provides the libguac-client-* names, so one file covers all three.
#
# Checked per-release rather than by version number — when Ubuntu packages it
# again this reverts to the archive copy with no edit here.
GUAC_PKGS="guacd libguac-client-vnc0 libguac-client-rdp0"
if ! apt-cache policy guacd 2>/dev/null | grep -q 'Candidate: [0-9]'; then
  LOCAL_GUACD=""
  for d in "$DEBS" "$(dirname "$0")/guacd-debs"; do
    [ -n "$d" ] || continue
    for f in "$d"/guacd_*_"$(dpkg --print-architecture)".deb; do
      [ -f "$f" ] && LOCAL_GUACD="$f"
    done
  done
  if [ -n "$LOCAL_GUACD" ]; then
    say "  no guacd in this release — installing $(basename "$LOCAL_GUACD")"
    # Let apt resolve its dependencies rather than dpkg -i, which would
    # install it and leave the libraries missing.
    # No `|| die` — this file defines only say(), and set -e already aborts
    # here. guacd is not decoration: without it every VNC and RDP console
    # fails at connect, so stopping is right.
    apt-get install -y --no-install-recommends "$LOCAL_GUACD"
  else
    say "  WARNING: no guacd in this release and no bundled .deb for $(dpkg --print-architecture)."
    say "           VNC and RDP consoles will fail at connect. Build one with:"
    say "             ./packaging/build-guacd.sh"
  fi
  # Either way they must leave the apt list, or the fetch below fails on names
  # this release does not have.
  for g in $GUAC_PKGS; do
    PACKAGES=$(printf '%s' "$PACKAGES" | tr ' ' '\n' | grep -vx "$g" | tr '\n' ' ')
  done
fi

MISSING=""
for pkg in $PACKAGES; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
  say " fetching$MISSING"
  # shellcheck disable=SC2086
  apt-get install -y --no-install-recommends $MISSING
else
  say "  every package is already installed — no network needed"
fi

# Belt and braces: if wireshark-common was already installed, apt will not have
# run its configure step and the preseed above does nothing on its own.
if dpkg -s wireshark-common >/dev/null 2>&1; then
  dpkg-reconfigure -f noninteractive wireshark-common >/dev/null 2>&1 || true
fi
if [ -x /usr/bin/dumpcap ]; then
  say "  dumpcap: $(getcap /usr/bin/dumpcap 2>/dev/null || echo 'no capabilities set')"
fi

say "Service account"
if ! id -u "$LABTRIS_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/$LABTRIS_USER" \
          --shell /usr/sbin/nologin "$LABTRIS_USER"
fi
# The API holds the Docker socket; netd is the only thing that touches netlink.
usermod -aG docker "$LABTRIS_USER"
# /dev/kvm is root:kvm 0660, so hardware acceleration is group membership too.
# Without this QEMU dies at startup with "failed to initialize kvm: Permission
# denied" — on a host that has KVM, which is the one place it matters. It went
# unnoticed for a long time because the development box had no /dev/kvm at all
# and everything silently ran under TCG.
if getent group kvm >/dev/null 2>&1; then
  usermod -aG kvm "$LABTRIS_USER"
fi
# Capture permission comes from group membership, not from root: the API
# launches Wireshark through `sg wireshark`, so the service account has to be
# in that group or every capture session opens onto an empty interface list.
if getent group wireshark >/dev/null 2>&1; then
  usermod -aG wireshark "$LABTRIS_USER"
else
  echo "  no wireshark group — capture will need root, which the API will not use" >&2
fi

say "Code at $PREFIX"
if [ -n "$SOURCE" ] && [ -d "$SOURCE" ] && [ "$SOURCE" -ef "$PREFIX" ]; then
  # Already in place — get.sh clones straight into $PREFIX and then calls this
  # with --source "$PREFIX", so the copy below would tar a directory into
  # itself. GNU tar notices the tree changing under it while the extracting
  # half writes into it, warns "file changed as we read it" for most
  # directories, and exits 1. Under `set -euo pipefail` that ends the install
  # silently: no error text, no services, and /opt/labtris looking plausibly
  # populated because the clone had already put the files there.
  #
  # That is the whole of `curl https://labtris.com/install | sudo bash`, so
  # this path was broken for every user of the documented install command.
  say "  already at $PREFIX — nothing to copy"
elif [ -n "$SOURCE" ]; then
  mkdir -p "$PREFIX"
  # --delete would take .venv with it, so the tree is synced and the venv left.
  # web/dist is NOT excluded. It used to be, from when the UI was built on the
  # target; now the ISO ships it prebuilt so the target needs no Node at all.
  # Excluding it here meant the ISO carried the UI correctly and the installer
  # threw it away on the way to $PREFIX — the API then served JSON and a 404
  # where the interface should be.
  tar -C "$SOURCE" --exclude=./.git --exclude=./.venv --exclude=./node_modules \
      -cf - . | tar -C "$PREFIX" -xf -
elif [ -d "$PREFIX/.git" ]; then
  git -C "$PREFIX" fetch --depth 1 origin "$BRANCH"
  git -C "$PREFIX" checkout -f FETCH_HEAD
else
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$PREFIX"
fi

say "Python environment"
# 3.12 specifically: pyproject pins >=3.12,<3.13 and the dependency set with it.
# Use whatever python3 this release ships, after checking it is one we
# support. Hardcoding python3.12 worked on noble and made resolute
# uninstallable — it has no python3.12 package at all. The check matters
# both ways: too old fails at install with a clear reason instead of at
# import, and too new is refused rather than silently untested.
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || { say "  ERROR: no $PY on PATH"; exit 1; }
PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYVER" in
  3.12|3.13|3.14) say "  using $PY ($PYVER)" ;;
  *) say "  ERROR: Python $PYVER is outside the supported range (3.12 to 3.14)."
     say "         pyproject.toml says requires-python = \">=3.12,<3.15\"."
     say "         Install a supported interpreter and re-run with PYTHON=python3.X"
     exit 1 ;;
esac
[ -d "$PREFIX/.venv" ] || "$PY" -m venv "$PREFIX/.venv"
if [ -n "$WHEELS" ] && [ -d "$WHEELS" ] && ls "$WHEELS"/* >/dev/null 2>&1; then
  say "  installing from $(ls "$WHEELS" | wc -l) bundled wheels"
  # --no-index: never reach for PyPI. --no-build-isolation: pip cannot fetch a
  # build backend offline, so the backend has to be installed first and then
  # used from the environment rather than from a fresh isolated one.
  "$PREFIX/.venv/bin/pip" install --quiet --no-index --find-links "$WHEELS" \
    setuptools wheel
  "$PREFIX/.venv/bin/pip" install --quiet --no-index --find-links "$WHEELS" \
    --no-build-isolation -e "$PREFIX"
else
  "$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
  "$PREFIX/.venv/bin/pip" install --quiet -e "$PREFIX"
fi

say "Configuration"
mkdir -p "$CONFDIR"
DB_PASS_FILE="$CONFDIR/db-password"
if [ ! -s "$DB_PASS_FILE" ]; then
  # Generated per install. Shipping a known password in an appliance image is
  # how every one of these products ends up with the same credentials.
  (umask 077; head -c 18 /dev/urandom | base64 | tr -d '/+=' > "$DB_PASS_FILE")
fi
DB_PASS=$(cat "$DB_PASS_FILE")

ENV_FILE="$CONFDIR/labtris.env"
if [ ! -s "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# Written by install-labtris.sh. The database URL belongs here rather than in
# whichever shell happened to start the service: a restart that loses it comes
# up against the wrong database and fails every request with a 500.
LABTRIS_DATABASE_URL=postgresql+asyncpg://labtris:$DB_PASS@127.0.0.1:5432/labtris
LABTRIS_NETD_SOCKET=/run/labtris/netd.sock
# Software emulation. Switched to kvm below when /dev/kvm exists — nested virt
# that reports success and then hangs on the first vcpu is why this is not
# simply assumed.
LABTRIS_QEMU_ACCEL=tcg
# Kernel Samepage Merging. Measured 8.5:1 deduplication across fifteen VMs on
# a comparable host — the difference between twenty labs on this machine and a
# hundred and fifty. Stock Ubuntu ships it switched off.
LABTRIS_KSM_RUN=1
LABTRIS_KSM_PAGES_TO_SCAN=1250
LABTRIS_KSM_SLEEP_MS=10
EOF
fi
chmod 0640 "$ENV_FILE"; chown root:"$LABTRIS_USER" "$ENV_FILE" 2>/dev/null || true
# Deliberately checked at finalise time too: an image built on a machine with
# KVM may be booted on one without it, and the reverse.
if [ -e /dev/kvm ]; then
  sed -i 's/^LABTRIS_QEMU_ACCEL=tcg$/LABTRIS_QEMU_ACCEL=kvm/' "$ENV_FILE"
  say "  /dev/kvm present — acceleration set to kvm"
fi

say "Web UI"
if [ -f "$PREFIX/web/dist/index.html" ]; then
  # Already built — by the ISO build, which does it once on the build host so
  # the target needs neither Node nor a route to the npm registry.
  say "  using the prebuilt UI ($(du -sh "$PREFIX/web/dist" | cut -f1))"
elif command -v npm >/dev/null 2>&1; then
  (cd "$PREFIX/web" && npm install --silent && npm run build --silent)
else
  # No prebuilt UI and no npm. This is the `curl | sudo bash` path: web/dist is
  # gitignored, so a git clone never carries it, and nothing has installed Node.
  # Left alone the install "succeeds" and serves REST with no interface at all —
  # which is not the product, and the reader was told to expect a browser.
  #
  # So fetch a toolchain and build it. It costs a couple of minutes and a few
  # hundred MB once; the alternative is an install that looks fine and has no UI.
  say "  no prebuilt UI — installing Node to build it"
  apt-get install -y --no-install-recommends nodejs npm >/dev/null 2>&1 || true
  if command -v npm >/dev/null 2>&1; then
    ( cd "$PREFIX/web" && npm install --silent && npm run build --silent ) || true
  fi
  if [ -f "$PREFIX/web/dist/index.html" ]; then
    say "  built the UI ($(du -sh "$PREFIX/web/dist" | cut -f1))"
  else
    # Loudly, and at the end where it will be read. A warning buried 1500 lines
    # up in apt output is one nobody sees.
    UI_MISSING=1
    echo "  WARNING: could not build the web UI — the API will serve REST only." >&2
    echo "           Install nodejs/npm and run: cd $PREFIX/web && npm install && npm run build" >&2
  fi
fi

# A symlink rather than a copy, so `labtris-upgrade` is always the version
# that shipped with the code now installed. A copy would leave the old
# script in place after an upgrade, which is the one file where being a
# version behind is actively confusing.
# The demo labs. This was previously only wired up by the ISO's
# late-commands, which meant a shell install never got them at all and an
# upgrade never got the ones added since. Enabling it here covers both: the
# unit is a no-op when every bundled pod has already been offered.
say "Demo labs"
if [ -f "$PREFIX/packaging/iso/overlay/etc/systemd/system/labtris-seed-pods.service" ]; then
  install -m 0644 \
    "$PREFIX/packaging/iso/overlay/etc/systemd/system/labtris-seed-pods.service" \
    /etc/systemd/system/labtris-seed-pods.service
  systemctl enable labtris-seed-pods.service >/dev/null 2>&1 \
    && say "  labtris-seed-pods enabled" \
    || say "  could not enable labtris-seed-pods"
else
  say "  unit not in this tree — skipped"
fi

say "Operator commands"
# One block per command, each independent. An earlier version nested these
# into a single if/elif/else chain and the stale-symlink cleanup for
# labtris-upgrade ended up in a branch that only ran when labtris-health was
# ALSO missing — so the dangling-command bug fixed in 635e775 quietly came
# back. Three separate tests cannot do that to each other.
#
# Symlinks rather than copies, so the command is always the one belonging to
# the code now installed. Each removes its own stale link when the target is
# not in this tree: `labtris-upgrade --to <older tag>` can check out a release
# that predates any of these scripts, and a command answering "No such file
# or directory" is worse than one that is honestly absent.
for _cmd in upgrade health kernel; do
  _src="$PREFIX/packaging/labtris-$_cmd.sh"
  _dst="/usr/local/bin/labtris-$_cmd"
  if [ -x "$_src" ]; then
    ln -sf "$_src" "$_dst"
    say "  labtris-$_cmd -> $_src"
  elif [ -L "$_dst" ]; then
    rm -f "$_dst"
    say "  labtris-$_cmd not in this tree — removed the stale symlink"
  fi
done

say "Unit and proxy files"
install -m 0644 "$PREFIX/packaging/systemd/labtris-ksm.service" /etc/systemd/system/
install -m 0644 "$PREFIX/packaging/systemd/labtris-netd.service" /etc/systemd/system/
install -m 0644 "$PREFIX/packaging/systemd/labtris-api.service" /etc/systemd/system/
install -m 0644 "$PREFIX/packaging/nginx/labtris.conf" /etc/nginx/sites-available/labtris
# The proxy body, included by both serving blocks. nginx refuses to start on a
# missing include, so this has to land before the reload — and before the
# site is enabled, or a reload between the two steps fails.
install -d -m 0755 /etc/nginx/snippets
install -m 0644 "$PREFIX/packaging/nginx/snippets/labtris-location.conf" \
  /etc/nginx/snippets/labtris-location.conf
mkdir -p /etc/nginx/sites-enabled
ln -sf /etc/nginx/sites-available/labtris /etc/nginx/sites-enabled/labtris
# Ubuntu's default site is also `default_server` on 80. Two of those on one
# port is a hard nginx failure, not a warning.
rm -f /etc/nginx/sites-enabled/default

# TLS, self-signed, generated once.
#
# Labtris carries session cookies and console streams, so plain HTTP across a
# network was the wrong default — the interface now faces 443 with 80
# redirecting to it. Self-signed rather than ACME because the common case is a
# lab box on a private address with no public DNS name, where ACME cannot
# issue at all. Swap in a real certificate by replacing these two files; the
# paths are what nginx reads and nothing regenerates them once they exist.
#
# The SAN list is what makes a browser accept it after the click-through, and
# what makes `curl --cacert` work for anyone who wants to pin it. Without a
# SAN at all, modern browsers reject the certificate outright regardless of
# the CN — a CN-only certificate is not a working certificate any more.
TLS_DIR=/etc/labtris/tls
if [ -s "$TLS_DIR/labtris.crt" ] && [ -s "$TLS_DIR/labtris.key" ]; then
  say "  TLS: keeping the existing certificate at $TLS_DIR"
else
  say "  TLS: generating a self-signed certificate"
  install -d -m 0750 "$TLS_DIR"
  HOSTN=$(hostname -f 2>/dev/null || hostname)
  SANS="DNS:$HOSTN,DNS:localhost,IP:127.0.0.1"
  for a in $(hostname -I 2>/dev/null); do
    case "$a" in *:*) SANS="$SANS,IP:$a" ;; *) SANS="$SANS,IP:$a" ;; esac
  done
  # 10 years: this is a lab appliance, and an expired certificate on one is a
  # support question rather than a security improvement.
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$TLS_DIR/labtris.key" -out "$TLS_DIR/labtris.crt" \
    -subj "/CN=$HOSTN" -addext "subjectAltName=$SANS" >/dev/null 2>&1 \
    && say "  TLS: $TLS_DIR/labtris.crt ($SANS)" \
    || say "  TLS: openssl failed — nginx will not start on 443. Check: openssl version"
  chmod 0640 "$TLS_DIR/labtris.key" 2>/dev/null || true
  chmod 0644 "$TLS_DIR/labtris.crt" 2>/dev/null || true
fi
chown -R "$LABTRIS_USER:$LABTRIS_USER" "$PREFIX"

# The media directory is deleted after the install, so a sources.list entry
# pointing at it would make every later apt-get update complain.
rm -f /etc/apt/sources.list.d/labtris-local.list

if should_finalise_now; then
  finalise
else
  say "Staging only — startup deferred to first boot"
  # A chroot cannot start anything, so the rest happens once the machine is
  # really running. The unit removes itself afterwards so a reinstall or a
  # second boot does not repeat work that already succeeded.
  cat > /etc/systemd/system/labtris-firstboot.service <<EOF
[Unit]
Description=Complete the Labtris installation on first boot
After=network-online.target postgresql.service docker.service
Wants=network-online.target
ConditionPathExists=$PREFIX/packaging/install-labtris.sh

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$PREFIX/packaging/install-labtris.sh --finalise
ExecStartPost=/usr/bin/systemctl disable labtris-firstboot.service
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  # Symlinked by hand rather than with `systemctl enable`: enabling needs a bus
  # in some systemd versions, and this is exactly the environment without one.
  mkdir -p /etc/systemd/system/multi-user.target.wants
  ln -sf /etc/systemd/system/labtris-firstboot.service \
         /etc/systemd/system/multi-user.target.wants/labtris-firstboot.service
  say "Staged. Labtris will finish installing on the first boot."
fi
