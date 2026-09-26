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


# ---- 1b. Build the SPA ----------------------------------------------------
# web/dist is a build artifact and is not in the tree, so without this
# stage the image has no UI at all: main.py mounts the SPA only `if
# web_dist.is_dir()`, so a missing directory means every page 404s with
# nothing in the log. The runtime stage points LABTRIS_WEB_DIST at what
# this produces.
FROM node:20-slim AS webbuild

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


# ---- 2. Runtime stage -----------------------------------------------------
FROM python:3.12-slim AS runtime

# System packages the Labtris ISO installs (see docs/reference/install-from-source.mdx).
# We keep the list tight — qemu + docker-cli + tcpdump are the load-bearing ones;
# guacd is optional (only needed for VNC/RDP consoles) and can be added later.
# Every binary the code actually shells out to. The original list was
# qemu + docker.io + tcpdump and was missing most of the dataplane:
# without nft and dnsmasq a NAT network cannot be brought up at all
# (labtris_netd/net.py installs an nftables masquerade and starts one
# dnsmasq per network), which is the path a lab node uses to reach the
# internet. Verified against the call sites, not guessed.
#
# docker-cli, not docker.io: Debian's docker.io ships dockerd and
# docker-init but NOT the `docker` client, and the client is the only
# part needed here — labtris_api/pods.py shells out to `docker save`
# because aiodocker has no binding for it. The daemon comes from the
# dind service in docker-compose.yml.
RUN apt-get update && apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils \
      cloud-image-utils \
      docker-cli \
      iproute2 iptables nftables \
      dnsmasq-base conntrack ethtool iputils-ping \
      tcpdump p7zip-full \
      curl ca-certificates \
      libpq5 \
      openssh-client \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# The Wireshark GUI feature (labtris_api/wireshark.py) runs a real
# Xvfb + wireshark + x11vnc and tunnels it through guacd. That is ~400MB
# of X11 for something most installs never open, so it is opt-in:
#   docker build --build-arg WITH_WIRESHARK=true .
# Packet capture itself does not need this — it uses tcpdump, above.
ARG WITH_WIRESHARK=false
RUN if [ "$WITH_WIRESHARK" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc wireshark-qt \
      && rm -rf /var/lib/apt/lists/* /var/cache/apt/*; \
    fi

# Copy wheels + install
COPY --from=wheelhouse /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
        'uvicorn[standard]' asyncpg alembic \
 && rm -rf /tmp/wheels /root/.cache

# Ship alembic config + migrations at a stable location so the
# entrypoint can `alembic upgrade head` on boot.
COPY alembic.ini /opt/labtris/alembic.ini
COPY migrations  /opt/labtris/migrations

# The built SPA. FastAPI serves it directly — there is no separate
# frontend container.
COPY --from=webbuild /web/dist /opt/labtris/web/dist

# Non-root user for the API (matches the ISO's `labtris` user).
# netd itself still needs root for netlink; the compose file overrides
# `user:` for that container.
# The uid is allocated rather than pinned to 999: dnsmasq-base, which the
# NAT dataplane needs, already takes 999 in this base image and useradd
# fails the build outright on the collision. Nothing depends on the
# number — netd is told which uid should own a tap at runtime, via
# tap.create's owner_uid, rather than assuming one.
RUN groupadd --system labtris \
 && useradd  --system --gid labtris --home-dir /opt/labtris labtris \
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
    LABTRIS_LISTEN_PORT="8080" \
    LABTRIS_WEB_DIST="/opt/labtris/web/dist"

# Every path the settings derive from $HOME, pinned under the declared
# volume instead. There is no USER instruction here, so the api runs as
# root with HOME=/root, and the defaults in labtris_api/config.py
# (~/.cache/labtris/qemu-images, ~/.local/share/labtris/qemu-vms,
# ~/.local/share/labtris/pods) would put every downloaded base image,
# every node disk overlay and every saved pod in the container's
# writable layer — discarded on the next `docker pull`.
#
# qemu-bios/ and qemu-cdrom/ are resolved relative to the image cache's
# PARENT (see _bios_dir/_cdrom_dir in labtris_api/runtime/qemu.py), so
# they follow from LABTRIS_QEMU_IMAGE_CACHE_DIR and need no var of their
# own. node-data/ likewise follows LABTRIS_QEMU_VM_DIR's parent.
ENV LABTRIS_QEMU_IMAGE_CACHE_DIR="/var/lib/labtris/qemu-images" \
    LABTRIS_QEMU_VM_DIR="/var/lib/labtris/qemu-vms" \
    LABTRIS_POD_DIR="/var/lib/labtris/pods" \
    LABTRIS_NODE_MOUNTS_DIR="/var/lib/labtris/node-mounts" \
    LABTRIS_SESSION_SECRET_PATH="/var/lib/labtris/session-secret"

EXPOSE 8080 2222
VOLUME ["/var/lib/labtris", "/run/labtris"]

ENTRYPOINT ["/usr/local/bin/labtris-entrypoint"]
CMD ["api"]
