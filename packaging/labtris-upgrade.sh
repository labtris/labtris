#!/usr/bin/env bash
#
# Upgrade an existing Labtris install.
#
#   sudo labtris-upgrade              # to the latest release
#   sudo labtris-upgrade --check      # say what would happen, change nothing
#   sudo labtris-upgrade --to v0.12.0 # a specific release
#   sudo labtris-upgrade --to main    # the development branch
#
# WHY THIS EXISTS, when re-running the installer already upgrades
#
# It does, and that is the engine here. But `curl … | sudo bash` has three
# sharp edges that matter more on a machine with labs on it than on a fresh
# one, and this covers them:
#
#   1. It tracks `main`. Someone typing "upgrade" means "move to the latest
#      tested release", not "move to whatever landed an hour ago". This
#      resolves the newest release tag and uses that.
#   2. It migrates the database with no backup. `alembic upgrade head` is
#      not reversible, and a failed migration on someone's lab history is
#      the worst outcome this script can have. So: pg_dump first, every
#      time, and print where it went.
#   3. It does not say whether the result works. A silent exit after
#      restarting a unit that then died is not an upgrade.
#
# Nothing here touches /var/lib/labtris — labs, pods, images and the
# database are the things being carried across, not replaced.
set -uo pipefail

PREFIX=${LABTRIS_PREFIX:-/opt/labtris}
CONFDIR=${CONFDIR:-/etc/labtris}
REPO=${LABTRIS_REPO:-https://github.com/labtris/labtris.git}
BACKUP_DIR=${LABTRIS_BACKUP_DIR:-/var/backups/labtris}
TARGET=""
CHECK_ONLY=0

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --to)     TARGET=${2:?--to needs a tag or branch}; shift 2 ;;
    --check)  CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1  (try --help)" ;;
  esac
done

# ---------------------------------------------------------------- what is here
# Docker and native need completely different handling, and guessing wrong
# would be destructive — so detect, and refuse rather than assume.
INSTALL_KIND=""
if [ -d "$PREFIX/.git" ] || [ -f "$CONFDIR/labtris.env" ]; then
  INSTALL_KIND=native
elif command -v docker >/dev/null 2>&1 &&
     docker ps --format '{{.Image}}' 2>/dev/null |
       grep -qE '(^|/)labtris/labtris(:|$)|ghcr\.io/labtris/labtris'; then
  # The APPLICATION image specifically, not anything with "labtris" in the
  # name. A Labtris host runs lab nodes from labtris/uet-ref, labtris/fw,
  # labtris/rdma-host and so on, and a loose grep matches those — so a
  # machine with a running lab and no container deployment was being told
  # it had one. Matching labtris/labtris excludes every node image.
  INSTALL_KIND=docker
fi

case "$INSTALL_KIND" in
  docker)
    say "This is a container install"
    cat <<'TXT'
  Upgrading a compose deployment is a tag bump, not a code update, and it
  has to run from the directory holding your compose file so it picks up
  your own ports, volumes and overrides:

      docker compose pull && docker compose up -d

  Your volumes are untouched by that. The API container runs the same
  migrations on start that this script would run by hand.
TXT
    exit 0
    ;;
  native) : ;;
  *) die "no Labtris install found.
       Looked for $PREFIX/.git and $CONFDIR/labtris.env (native), and a
       running container built from a labtris image.
       If yours is somewhere else: LABTRIS_PREFIX=/path sudo labtris-upgrade" ;;
esac

[ "$(id -u)" -eq 0 ] || die "run as root (sudo labtris-upgrade)"
[ -d "$PREFIX/.git" ] || die "$PREFIX is not a git checkout, so there is nothing to
       update in place. Re-run the installer instead:
         curl -fsSL https://labtris.com/install | sudo bash"

# ------------------------------------------------------------------- versions
current_version() {
  sed -n 's/^version *= *"\(.*\)"/\1/p' "$PREFIX/pyproject.toml" 2>/dev/null | head -1
}
# -c safe.directory: install-labtris.sh chowns $PREFIX to the labtris service
# user, and this runs as root, so git refuses with "detected dubious
# ownership in repository" and every command fails. Passing it per-invocation
# rather than telling the operator to run
# `git config --global --add safe.directory /opt/labtris` — that edits root's
# global config to work around something this script already knows about, and
# it would persist long after the upgrade.
GIT=(git -C "$PREFIX" -c "safe.directory=$PREFIX")

current_ref() { "${GIT[@]}" rev-parse --short HEAD 2>/dev/null; }

# The newest release tag, from the API rather than `git tag`: a --depth 1
# clone has no tags to sort, and `git tag | tail` sorts lexically anyway,
# which puts v0.9.0 after v0.12.0 and has already caused one wrong answer.
latest_release() {
  curl -fsSL --max-time 20 \
    https://api.github.com/repos/labtris/labtris/releases/latest 2>/dev/null |
    sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1
}

CUR_VER=$(current_version)
CUR_REF=$(current_ref)

if [ -z "$TARGET" ]; then
  say "Finding the latest release"
  TARGET=$(latest_release)
  if [ -z "$TARGET" ]; then
    die "could not reach the GitHub API to find the latest release.
       Name one explicitly:  sudo labtris-upgrade --to v0.12.0
       Or take the development branch:  sudo labtris-upgrade --to main"
  fi
  say "  latest release is $TARGET"
fi

printf '\n  %-22s %s\n' "installed:" "${CUR_VER:-unknown} (${CUR_REF:-?})"
printf '  %-22s %s\n'   "target:"    "$TARGET"
printf '  %-22s %s\n\n' "prefix:"    "$PREFIX"

if [ "$CHECK_ONLY" = 1 ]; then
  say "--check given, so nothing was changed"
  exit 0
fi

# ------------------------------------------------------------------ db backup
# Before the migration, not after. Always, not on a flag: the one time
# someone would have wanted it is the time they did not pass it.
say "Backing up the database"
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
DUMP="$BACKUP_DIR/labtris-${CUR_VER:-pre}-$STAMP.sql.gz"
DBURL=$(grep -h '^LABTRIS_DATABASE_URL=' "$CONFDIR/labtris.env" 2>/dev/null | cut -d= -f2-)
if [ -z "$DBURL" ]; then
  warn "no LABTRIS_DATABASE_URL in $CONFDIR/labtris.env — skipping the dump"
  warn "an upgrade without a backup is one you cannot walk back from"
else
  # postgresql+asyncpg:// is SQLAlchemy's dialect, not a libpq URL.
  PG_URL=${DBURL/postgresql+asyncpg:/postgresql:}
  if sudo -u postgres pg_dump "$PG_URL" 2>/dev/null | gzip > "$DUMP"; then
    say "  $DUMP ($(du -h "$DUMP" | cut -f1))"
  elif pg_dump "$PG_URL" 2>/dev/null | gzip > "$DUMP"; then
    say "  $DUMP ($(du -h "$DUMP" | cut -f1))"
  else
    rm -f "$DUMP"
    die "pg_dump failed, so the upgrade stopped before touching anything.
       Restore confidence first, or force past this with:
         LABTRIS_BACKUP_DIR=/dev/null is NOT a way to skip it — fix pg_dump."
  fi
fi

# ---------------------------------------------------------------------- update
say "Fetching $TARGET"
"${GIT[@]}" fetch --depth 1 origin "$TARGET" \
  || die "could not fetch '$TARGET'. Is it a real tag or branch?"
"${GIT[@]}" checkout -f FETCH_HEAD \
  || die "checkout failed — $PREFIX may have local modifications"

NEW_VER=$(current_version)
say "Running the installer ($NEW_VER)"
# The installer is the engine: it reinstalls dependencies, runs
# `alembic upgrade head`, reloads systemd and restarts the units. Passing
# --source keeps it working from this checkout rather than re-cloning.
"$PREFIX/packaging/install-labtris.sh" --source "$PREFIX" \
  || die "the installer failed. The database dump is at:
         ${DUMP:-<none taken>}"

# ----------------------------------------------------------------- did it work
say "Checking it came up"
sleep 5

# Prefer the real health check when the tree has one — it looks at the
# schema revision, the certificate and the listeners, not just whether a
# unit is active. Two definitions of "healthy" drift; this keeps one.
HEALTH="$PREFIX/packaging/labtris-health.sh"
if [ -x "$HEALTH" ]; then
  if "$HEALTH"; then
    say "Upgraded ${CUR_VER:-unknown} -> $NEW_VER"
    printf '  %-20s %s\n' "db backup:"     "${DUMP:-none taken}"
    printf '  %-20s %s\n' "previous code:" "git -C $PREFIX -c safe.directory=$PREFIX checkout -f $CUR_REF"
    exit 0
  fi
  warn "upgraded to $NEW_VER, but the health check failed."
  cat <<TXT

  To go back, the previous commit and the dump are both kept:
      git -C $PREFIX -c safe.directory=$PREFIX checkout -f $CUR_REF
      $PREFIX/packaging/install-labtris.sh --source $PREFIX
      gunzip -c ${DUMP:-<none>} | sudo -u postgres psql
TXT
  exit 1
fi

# Fallback for a target release that predates labtris-health.
FAILED=""
for u in labtris-api labtris-netd; do
  if systemctl is-active --quiet "$u"; then
    printf '  %-20s active\n' "$u"
  else
    printf '  %-20s NOT ACTIVE\n' "$u"
    FAILED="$FAILED $u"
  fi
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
CODE=$(curl -sk -o /dev/null -m 10 -w '%{http_code}' "https://127.0.0.1:8443/" 2>/dev/null)
[ "$CODE" = "000" ] && CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:8081/" 2>/dev/null)
printf '  %-20s HTTP %s\n' "web interface" "${CODE:-no answer}"

if [ -n "$FAILED" ] || [ "${CODE:-000}" = "000" ]; then
  warn "upgraded to $NEW_VER, but it is not serving."
  cat <<TXT

  Look here first:
      journalctl -u labtris-api -n 50 --no-pager
      systemctl status$FAILED

  To go back, the previous commit and the dump are both kept:
      git -C $PREFIX -c safe.directory=$PREFIX checkout -f $CUR_REF
      $PREFIX/packaging/install-labtris.sh --source $PREFIX
      gunzip -c ${DUMP:-<none>} | sudo -u postgres psql
TXT
  exit 1
fi

say "Upgraded ${CUR_VER:-unknown} -> $NEW_VER"
printf '  %-20s %s\n' "web interface:" "https://${IP}:8443"
printf '  %-20s %s\n' "db backup:"     "${DUMP:-none taken}"
printf '  %-20s %s\n' "previous code:" "git -C $PREFIX -c safe.directory=$PREFIX checkout -f $CUR_REF"
