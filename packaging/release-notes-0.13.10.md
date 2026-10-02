Labtris 0.13.10 — dragging a wire from a node handle onto a port no longer 422s.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## The link that silently refused to be made

Two gestures on the canvas produce a wire:

* **Pill → pill.** Start on an interface, release on another interface.
  The code sets `wiring = { from: iface }`, with `wiring.from.id` set
  to the interface ULID.
* **Handle → pill.** Start on the node-level handle (a shortcut for
  "pick a port for me"), release on an interface. The code sets
  `wiring = { fromNode: node, from: {x, y} }` — the `from` is the
  rubber-band's anchor in canvas coordinates, and has no `id`.

`endWire(iface)` assumed the first shape and passed `wiring.from.id`
straight into `POST /api/v1/labs/<id>/links` as the `a_iface_id`. In
the handle→pill case that id was `undefined`, which `JSON.stringify`
drops, so the server received `{ b_iface_id: "01M…" }`, missed the
required `a_iface_id`, and replied 422. The toast surfaced "field
required" with no obvious cause, and the gesture that failed looked
visually identical to one that worked.

`endWire` now detects a handle-origin wiring (`wiring.fromNode` is
set), finds the node that owns the released-on interface, and routes
through the picker so the user chooses the a-side explicitly. The old
pill→pill path is unchanged.

## And a round of generator and startup fixes that landed with it

Not reasons on their own to upgrade; worth knowing if any of them
have bitten you.

* `generate --with-bgp-evpn` configured zero sessions on the generated
  FRR daemons — the daemons file ran its own options as if they were
  commands. Fixed; EVPN sessions now establish.
* `advertise-all-vni` was emitted into router configs with no VNI
  and no tenants to carry, which produced a warning on every FRR
  reload. Now only written when there is at least one tenant VNI.
* The overlay generator raised the MTU on one end of every uplink
  and left the other end at the default. Fixed; symmetric.
* `apply_configs` restarted FRR unconditionally even when nothing in
  the configuration had changed. On a converged fabric that meant
  BGP sessions all dropped simultaneously. Only restarts when the
  rendered config actually differs.
* `vtysh` printed a "no config file" warning on every single command
  on the generated router nodes. The file was there — vtysh was
  invoked without `-c`. Fixed.
* A fat tree is k pods; the generator was laying it out as four rows
  across half a million pixels. Pod layout restored.
* The fat-tree cap was hitting the ceiling again at 1344 nodes.
  Raised; see `labtris lab generate --topology fat-tree` for the
  current bound.
* `Start all` ran sequentially; past ~3267 nodes the browser gave up
  before the backend finished. Now dispatches in parallel with a
  worker cap.
* The database pool was smaller than the real workload. Verified
  under a 2000-node start, pool resized.
* KSM diagnostics said "KSM on" and omitted the two numbers that
  actually matter — pages shared and KSM-savings MB.
* `labtris-user` crashed on an import — the worst place for it to
  die, because that is where you discover a bad lab.
* "Changing a password had no route anywhere in the product." Now
  surfaced in the user menu.
* The right-click RAM/vCPU menu did nothing when the Inspector was
  closed. Fixed.
* The Inspector reopen handle appeared where it was NOT closed from.
  Fixed.
* Added a supported way back into an instance you are locked out of
  (`labtris admin unlock` on the host).

## Upgrade

The usual way:

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

For the Docker image path (first-class since 0.13.9's docker-pull
work):

```
docker pull ghcr.io/labtris/labtris:0.13.10
docker compose up -d
```
