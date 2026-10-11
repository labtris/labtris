#!/bin/sh
# A 1 MB all-reduce across 16 NPUs arranged as 2 dies × 4 packages × 2
# nodes — scale-in, scale-up and scale-out as the three dimensions of one
# analytical model. See scale-in-up-out.yml for the numbers and what they
# are not.
set -e
ROOT=/opt/astra-sim
HERE=$(dirname "$(realpath "$0")")
exec "$ROOT/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware" \
  --workload-configuration="$ROOT/examples/workload/microbenchmarks/all_reduce/16npus_1MB/all_reduce" \
  --system-configuration="$HERE/scale-in-up-out.json" \
  --remote-memory-configuration="$ROOT/examples/remote_memory/analytical/no_memory_expansion.json" \
  --network-configuration="$HERE/scale-in-up-out.yml"
