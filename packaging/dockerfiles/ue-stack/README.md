# ue-stack container image

Real userspace UET wire-format daemon — the `uestack` CLI from this
repo's `ue_stack/` package, packaged so a Labtris palette drop lands
a container that speaks the wire protocol.

## Build

Run from the repo root (build context includes `ue_stack/`):

```bash
docker build \
  -f packaging/dockerfiles/ue-stack/Dockerfile \
  -t labtris/ue-stack:latest \
  .
```

## What ships today

- Real UDP-transported UET frames (headers per `ue_stack.packet`)
- IPDC context — unreliable fire-and-forget, works end to end
- TPDC context — reliable-with-ACK-and-RTO wire format works; the
  full state machine is a stub (fixed RTO, no CC-driven backoff)
- `uestack send` / `uestack listen` for one-shot demos

## What's stub

- Exact spec-conformant bit widths on headers (shape correct, exact
  offsets marked TODO)
- Congestion control (missing)
- Packet spraying (missing — sender picks one path)
- No interop with real UEC silicon yet

See `docs/design/ue-stack-poc.mdx` for what a full stack would take.
