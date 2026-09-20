# UE-Sim container image

Dockerfile for the UE-Sim Labtris node kind (see
`labtris_api/runtime/containers.py` → `ue-sim`). Not built by the
labtris installer — this is a ~2 GB build with a full ns-3 compile,
and shipping the source Dockerfile lets you pin the UE-Sim commit
per your own reproducibility needs rather than tracking a moving
labtris/ue-sim:latest.

## Build

```bash
docker build \
  --build-arg UE_SIM_COMMIT=main \
  -t labtris/ue-sim:latest \
  packaging/dockerfiles/ue-sim/
```

Substitute a specific SHA for reproducibility:

```bash
docker build --build-arg UE_SIM_COMMIT=abc1234 \
  -t labtris/ue-sim:2026-07-14 \
  packaging/dockerfiles/ue-sim/
```

## Point Labtris at your build

If your local tag isn't `labtris/ue-sim:latest`, edit the `image`
field on the `ue-sim` catalog entry in
`labtris_api/runtime/containers.py`. Or push the image to a
registry the labtris host can pull from and use that.

## Try it

Drop a `UE-Sim` chip from the palette onto the canvas → the palette
Pull-now button fetches whatever's tagged locally, or spawn triggers
a pull if it's on a registry.

```bash
labtris node exec <ue-sim-node-id> -- ns3 run soft-ue-e2e-concepts
```

## Upstream

- Repo: https://github.com/kaima2022/UE-Sim
- License: GPL-2
- Institute of Information Engineering + Institute of Microelectronics,
  Chinese Academy of Sciences.
- Cite per the repo's Citation section if you publish results derived
  from this simulator.
