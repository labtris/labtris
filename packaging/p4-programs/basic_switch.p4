/* basic_switch.p4 — the smallest useful P4 program for bmv2.
 *
 * L2 forwarding by destination MAC. Every packet's dst MAC is looked
 * up in a table populated at runtime (via simple_switch_CLI or the
 * runtime API); the matching action sets egress_spec to a port. A
 * miss floods to CPU port so the switch never black-holes silently.
 *
 * Kept as thin as possible: parser reads ethernet, control block is
 * one apply(), deparser emits ethernet. Everything else in this
 * directory is a delta on this shape.
 */

#include <core.p4>
#include <v1model.p4>

const bit<9>  CPU_PORT = 255;

/* ---------------------------- headers ------------------------------ */

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers { ethernet_t ethernet; }
struct metadata { }

/* ---------------------------- parser ------------------------------- */

parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        packet.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply { } }

/* ------------------------- ingress control ------------------------- */

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t std) {

    action forward(bit<9> port) { std.egress_spec = port; }
    action flood()              { std.egress_spec = CPU_PORT; }

    table dmac {
        key     = { hdr.ethernet.dstAddr : exact; }
        actions = { forward; flood; }
        size    = 4096;
        default_action = flood();
    }

    apply { dmac.apply(); }
}

/* ------------------------- egress control -------------------------- */

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t std) { apply { } }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply { } }

/* --------------------------- deparser ------------------------------ */

control MyDeparser(packet_out packet, in headers hdr) {
    apply { packet.emit(hdr.ethernet); }
}

V1Switch(MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()) main;
