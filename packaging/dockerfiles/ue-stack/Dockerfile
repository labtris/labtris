# ue-stack — real userspace UET wire-format daemon.
#
# The `uestack` CLI (source in the ue_stack/ Python package) packaged
# as its own container so a Labtris palette drop gets an endpoint
# that speaks the real wire format on real veths. Pair with a second
# ue-stack node on the same bridge and `uestack send ↔ uestack listen`
# is a real UET round-trip.
#
# Also useful next to an ns-3 UE-Sim node: use UE-Sim for
# deterministic protocol validation, ue-stack for real-packet
# behaviour on real Linux.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      iproute2 iputils-ping tcpdump \
 && rm -rf /var/lib/apt/lists/*

# Copy the ue_stack package from the labtris source tree. Build
# context is expected to be the repo root:
#   docker build -f packaging/dockerfiles/ue-stack/Dockerfile -t labtris/ue-stack:latest .
COPY ue_stack /opt/ue_stack
COPY pyproject.toml /opt/pyproject.toml
RUN pip install --no-cache-dir --root-user-action=ignore /opt \
 || pip install --no-cache-dir --root-user-action=ignore /opt/ue_stack

RUN printf '%s\n' \
    '# ue-stack node — real UET wire-format daemon.' \
    '# Try:' \
    '#   uestack listen                        # bind default port + print packets' \
    '#   uestack send 10.0.0.2:4791 "hello"    # send one IPDC packet to a peer' \
    '# Source: https://github.com/labtris/labtris/tree/main/ue_stack' \
    > /etc/motd

CMD ["/bin/bash", "-c", "cat /etc/motd; exec tail -f /dev/null"]
