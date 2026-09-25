# syntax=docker/dockerfile:1.7
#
# Labtris core — API + netd + web UI in one image.
#
# Runs api or netd depending on argv[0]:
#   docker run ... ghcr.io/labtris/labtris:latest api    # default
#   docker run ... ghcr.io/labtris/labtris:latest netd
#   docker run ... ghcr.io/labtris/labtris:latest cli    # -> labtris CLI
#
# The compose file wires api + netd + postgres together. Standalone
# `docker run` is fine for the api container as long as an external
# database URL is supplied via LABTRIS_DATABASE_URL and netd is either
# skipped (no network plumbing) or reachable at LABTRIS_NETD_SOCKET.

# ---- 1. Build wheels stage ------------------------------------------------
FROM python:3.12-slim AS wheelhouse

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY labtris_api ./labtris_api
COPY labtris_cli ./labtris_cli
COPY labtris_client ./labtris_client
COPY labtris_mcp ./labtris_mcp
COPY labtris_netd ./labtris_netd
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /wheels/


# ---- 2. Runtime stage -----------------------------------------------------
FROM python:3.12-slim AS runtime

# System packages the Labtris ISO installs (see docs/reference/install-from-source.mdx).
# We keep the list tight — qemu + docker-cli + tcpdump are the load-bearing ones;
# guacd is optional (only needed for VNC/RDP consoles) and can be added later.
RUN apt-get update && apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils \
      docker.io \
      iproute2 iptables \
      tcpdump p7zip-full \
      curl ca-certificates \
      libpq5 \
      openssh-client \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Copy wheels + install
COPY --from=wheelhouse /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
        'uvicorn[standard]' asyncpg alembic \
 && rm -rf /tmp/wheels /root/.cache

# Ship alembic config + migrations at a stable location so the
# entrypoint can `alembic upgrade head` on boot.
COPY alembic.ini /opt/labtris/alembic.ini
COPY migrations  /opt/labtris/migrations

# Non-root user for the API (matches the ISO's `labtris` user).
# netd itself still needs root for netlink; the compose file overrides
# `user:` for that container.
RUN groupadd --system --gid 999 labtris \
 && useradd  --system --uid 999 --gid labtris --home-dir /opt/labtris labtris \
 && mkdir -p /opt/labtris /run/labtris /var/lib/labtris \
 && chown labtris:labtris /opt/labtris /var/lib/labtris

# Entrypoint dispatch.
COPY packaging/docker/entrypoint.sh /usr/local/bin/labtris-entrypoint
RUN chmod +x /usr/local/bin/labtris-entrypoint

# API defaults
ENV LABTRIS_DATABASE_URL="" \
    LABTRIS_NETD_SOCKET="/run/labtris/netd.sock" \
    LABTRIS_QEMU_ACCEL="kvm" \
    LABTRIS_LISTEN_HOST="0.0.0.0" \
    LABTRIS_LISTEN_PORT="8080"

EXPOSE 8080 2222
VOLUME ["/var/lib/labtris", "/run/labtris"]

ENTRYPOINT ["/usr/local/bin/labtris-entrypoint"]
CMD ["api"]
