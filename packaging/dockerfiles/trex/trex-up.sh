#!/bin/sh
# Start the TRex server in the background and wait until both ports are
# bound. For hooks and scripts; `run-trex` is the foreground form.
# Prints the port line on success; exits 1 with the log's tail if the
# server did not come up in 60 s.
LOG=${TREX_LOG:-/tmp/trex.log}
if pgrep -x _t-rex-64 >/dev/null 2>&1; then
  echo "trex already running"; exit 0
fi
nohup run-trex "$@" > "$LOG" 2>&1 &
i=0
while [ $i -lt 60 ]; do
  if grep -q "Number of ports found: 2" "$LOG" 2>/dev/null; then
    grep "Number of ports found" "$LOG"; exit 0
  fi
  if grep -q "ERROR encountered" "$LOG" 2>/dev/null; then break; fi
  sleep 1; i=$((i+1))
done
tail -20 "$LOG"; exit 1
