# Wireshark dissectors shipped with Labtris

Protocols Wireshark cannot decode on its own, so a capture in the UI
shows something more useful than `Unknown (<proto>)`.

## `uet.lua` — Ultra Ethernet Transport

Ultra Ethernet rides its own transport on IP protocol 253, and Wireshark
has no built-in dissector for it. Without this file a UET capture is a
list of undecoded IPv4 frames; with it you get packet types, PSNs, the
per-direction PDC ids, the NACK code table, and the entropy field that
makes packet spraying visible.

Vendored from
[github.com/ultraethernet/uet-ref-prov](https://github.com/ultraethernet/uet-ref-prov)
(`dissector/uet.lua`), MIT, copyright Keysight Technologies and Broadcom.
The header is kept intact, which is what MIT asks for. 103 fields.

`labtris_api/wireshark.py` passes every `.lua` in this directory to
Wireshark with `-X lua_script:`, so dropping another file here is all it
takes to add one.

`labtris-network-skills/packet_analysis/uet.yaml` is written against
these same field names, so the assistant and the human read the same
capture.
