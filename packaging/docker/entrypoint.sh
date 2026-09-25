#!/usr/bin/env bash
# Dispatch to the right long-running process based on argv[0].
# Kept tiny on purpose — s6/supervisor adds surface area we don't need
# when compose gives each process its own container.
set -euo pipefail

cmd="${1:-api}"
shift || true

case "$cmd" in
  api)
    # Best-effort migration on boot. Non-fatal if the DB isn't reachable
    # yet (compose's depends_on handles ordering, but a fresh volume
    # still races).
    if [[ -n "${LABTRIS_DATABASE_URL:-}" && -f /opt/labtris/alembic.ini ]]; then
      ( cd /opt/labtris && alembic upgrade head 2>&1 | tail -5 ) \
        || echo "labtris: alembic upgrade skipped (db not ready?)"
    fi
    exec uvicorn labtris_api.main:app \
      --host "${LABTRIS_LISTEN_HOST:-0.0.0.0}" \
      --port "${LABTRIS_LISTEN_PORT:-8080}" \
      "$@"
    ;;

  netd)
    # netd is the only process that touches netlink. Requires the
    # container to run privileged (or with CAP_NET_ADMIN + host netns).
    mkdir -p /run/labtris
    exec python -m labtris_netd \
      --socket "${LABTRIS_NETD_SOCKET:-/run/labtris/netd.sock}" \
      "$@"
    ;;

  cli)
    exec labtris "$@"
    ;;

  bash|sh)
    exec "$cmd" "$@"
    ;;

  *)
    # Pass anything else straight through — supports arbitrary
    # `docker run ... labtris <any-command>`.
    exec "$cmd" "$@"
    ;;
esac
