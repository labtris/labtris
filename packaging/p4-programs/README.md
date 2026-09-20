# Curated P4 programs

These are the built-in programs a `bmv2` Labtris node can run without
uploading anything. Pick one at spawn time (or via `labtris node p4
set <id> --builtin <name>`); the node's runtime bind-mounts this
directory into the container at `/p4`, compiles `prog.p4` with
`p4c-bm2-ss`, and runs `simple_switch` against the result.

Every program targets the v1model architecture that ships with bmv2.
Kept small on purpose — these are references you read, not production
data planes. When you want something real, upload your own `.p4` via
`labtris node p4 upload <id> <path>` and the mount switches to it.

| File | What it demonstrates | Fabric research angle |
|---|---|---|
| `basic_switch.p4` | L2 forwarding by destination MAC. | The baseline every other program is measured against — no ECMP, no marking, no drops beyond what the buffer forces. |
| `ecmp.p4` | Per-packet ECMP across a small port set, hashing the 5-tuple. | The building block of every scale-out fabric. Compare per-flow vs per-packet by watching the capture pane on the two output links. |
| `ecn.p4` | Mark ECN CE (Congestion Experienced) when the egress queue crosses a threshold. | Foundation of DCTCP, DCQCN, Ultra Ethernet CC. Enable a hook that graphs queue depth over time and you have a CC test bench. |
| `trim.p4` | Under queue-depth pressure, truncate the packet body past the L4 header instead of dropping it whole. | The Ultra Ethernet "trimming" primitive — the receiver learns immediately that a packet was congested, without waiting on a timer. |

## Compile them yourself

You do not need Labtris to try one — the same `.p4` compiles anywhere
`p4c` runs:

```
docker run --rm -v $PWD:/p4 -w /p4 p4lang/p4c \
  p4c-bm2-ss -o prog.json basic_switch.p4
```

## Adding one

Drop a new `.p4` in this directory, add a row to the table above, and
submit a PR. Anything larger than "reads as one concept" belongs in
your own repo instead — Labtris uploads a per-node program in one
call, so a custom P4 program does not need to live here to be usable.

## Non-goals

- **Not production data planes.** These are one-concept reference
  programs, kept small so a first-time P4 reader can hold each one
  in their head.
- **Not a P4 tutorial.** The p4.org tutorials are excellent and
  already exist; these programs assume you have read them.
- **Not tied to any specific vendor pipeline.** Everything targets
  v1model (bmv2). If you want to compile for a Tofino / Silicon One /
  Trident target, keep the same shape but change the architecture
  include.
