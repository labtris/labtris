Labtris 0.13.12 — deleting a lab with an open console no longer leaks a raw Docker error, and node wipe gets a deep mode.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## Deleting a lab printed a Python repr at you

A console tab open on a container node auto-reconnects its WebSocket
on disconnect. When the lab was being deleted the container had
already stopped but was not yet removed, so the reconnect landed on
`container.exec()` against a stopped container and Docker replied
with 409 "container &lt;id&gt; is not running". `exec_shell` had been
wrapped for this since 0.11; `open_shell` had not. The raw aiodocker
repr surfaced on the delete action, which looked like Labtris had
crashed when it had done the right thing already.

`open_shell` now mirrors `exec_shell`'s handler: 409 becomes a clean
"container is not running — its state has drifted from Labtris"
message that the WebSocket closes on gracefully, 404 becomes
"container no longer exists". The aiodocker client is closed on the
error path, so an fd is not leaked. Nothing to do on your side.

## Node wipe gained a `deep` mode

`POST /api/v1/nodes/{id}/wipe` has done one thing since it was
written: throw the node's writable layer away and keep the node row,
so the next start builds a fresh guest from the shared base image.
For a QEMU node that's the whole story — the overlay is gone, the
boot is fresh.

For a container node the writable layer went too, but anything the
node kept in a *bind mount* was left alone — bmv2's `/p4` program
survived a wipe, so did an FRR `/etc/frr`, so did a saved cumulus
startup. That is sometimes what you want (you wiped the misbehaving
guest, not your P4 program) and sometimes isn't (you really want to
reset this thing to the day it was created).

A `?deep=true` query parameter extends the wipe to the host-side
mounts dir (`~/.local/share/labtris/node-mounts/<node_id>/`). The
next start re-seeds the dir from the profile defaults — bmv2 gets
`basic_switch.p4` back, FRR gets the stock daemons file, etc.

```bash
curl -X POST \
  "http://.../api/v1/nodes/01M.../wipe?deep=true" \
  -H "Authorization: Bearer $TOK"
```

The web wipe button still runs a shallow wipe. A UI toggle for deep
can land in a later release — this one just makes the API path
available, so the CLI and any script can get at it today:

```bash
labtris node wipe <node> --deep        # once labtris_cli ships the flag
# or, in the meantime:
curl -X POST "$BASE/api/v1/nodes/$NID/wipe?deep=true" \
  -H "Authorization: Bearer $TOK"
```

## Upgrade

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

Container state is not touched by the upgrade: a labtris-api restart
reconnects to the running containers by ID. Writable layer, bind
mounts, routing tables and shells inside the containers all survive.
