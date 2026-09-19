# Bootstrap recipes

A **bootstrap** is a small list of `{wait_for, type}` steps a template
carries in its spec. When a node from that template boots for the
first time, Labtris drives the serial console through the sequence:
wait for a pattern to appear, type the response, move to the next
step. The node ends up in a known state (logged in, hostname set,
mgmt IP configured, netconf enabled) without a human intervening.

vrnetlab has one `launch.py` per vendor that does this in Python.
Labtris keeps it as data on the template row instead.

## Shape

```json
{
  "step_timeout_s": 60,
  "steps": [
    {"wait_for": "regex", "type": "text to send\n", "timeout_s": 60},
    ...
  ]
}
```

- `wait_for` — Python regex, matched against the accumulating serial
  buffer. `.` does not match newline; use `[\s\S]` if you need it.
- `type` — literal string, including newlines (`\n` → 0x0a). Passwords
  and hostnames go here. `{name}` is substituted with the node's name;
  `{node_id}` with its ULID.
- `timeout_s` — per-step; defaults to `step_timeout_s` or 60.

## How to attach one to a template

Two ways today:

### From the UI

`Settings → Templates → <your template> → Edit`, then paste JSON into
the "Bootstrap" field. Save. The next node started from this template
runs the bootstrap after it reaches its serial prompt.

### From the API

```
curl -X PATCH http://<host>:8081/api/v1/templates/<template-id> \
  -H 'content-type: application/json' \
  -d '{"spec": {"bootstrap": <the JSON block>}}'
```

## Watching it run

- `GET /api/v1/nodes/<id>/bootstrap` — snapshot: phase (idle / running
  / done / failed), current step, error if any.
- MCP tool `node_bootstrap_status(node_id)` — same thing from an
  assistant.

If the phase stops at `failed`, edit the template's bootstrap block
and `POST /api/v1/nodes/<id>/bootstrap/retry` (or the MCP
`node_bootstrap_retry` tool) to re-run.

## Files here

- [`cirros.json`](cirros.json) — the smallest useful example, and the
  one the tests exercise. Cirros has a `cirros login:` prompt and
  fixed `cirros`/`gocubsgo` credentials. Bootstrap logs in, sets a
  hostname, drops back to a shell prompt.
- [`vendor-router.json`](vendor-router.json) — the shape a classical
  vendor router bootstrap takes: dismiss the setup dialog, log in
  with defaults, enter configuration mode, set hostname + credentials
  + a mgmt interface, commit. Not tied to any specific vendor's
  prompts — use it as a starting point when writing one for your own.
