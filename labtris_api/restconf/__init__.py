"""RESTCONF endpoint (Phase K3, RFC 8040 shape).

Labtris speaks RESTCONF on behalf of every managed node. Tools written
for RESTCONF (curl, restconf-client, ncclient's http shim, network
telemetry collectors) work against Labtris without knowing the target is
a lab. First cut ships one YANG model — `ietf-interfaces` (RFC 8343) —
with the operations network engineers use most: read every interface's
counters, and turn an interface up or down.

Design decisions:

* URL shape: `/restconf/data/labtris:nodes/node=<name>/ietf-interfaces:
  interfaces[-state][/interface=<if>]`. RESTCONF assumes the API IS the
  target device; Labtris fronts N devices, so a `labtris:nodes` container
  gives us the per-device scope RFC 8040 doesn't spell out.
* Media type: `application/yang-data+json`. The XML variant of RESTCONF
  is legal but virtually nobody uses it; skipping it keeps the surface
  small. Content-Type on writes is required to be that, but we also
  accept `application/json` for convenience because 90% of curl users
  won't remember to set the yang-data type.
* No YANG library dependency. Every response is hand-rolled JSON in the
  exact keys the model requires. Adding pyang / libyang for one model
  would be premature; the pattern in `serialize.py` makes bolting on
  more models (openconfig-interfaces, ietf-routing, etc.) a ~50-line lift.
"""
