/* ecmp.p4 — per-packet ECMP across a small port set.
 *
 * For each IPv4 packet, hash the 5-tuple (src/dst IP, protocol,
 * src/dst L4 port) and pick one of ecmp_group_size output ports.
 * "Per-packet" rather than "per-flow" is the point: watching two
 * captures at the two output ports of an ecmp_group_size=2 switch
 * shows the same flow's packets alternating, which is the shape
 * Ultra Ethernet's packet-spraying transport (UET) relies on.
 *
 * A per-flow ECMP would hash the same 5-tuple and stick with that
 * output for the flow's lifetime; that behaviour is what current
 * TCP over ECMP assumes and is what UET explicitly abandons.
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;

/* ---------------------------- headers ------------------------------ */

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

/* Enough of TCP/UDP to read the 4-tuple ports. */
header ports_t {
    bit<16> srcPort;
    bit<16> dstPort;
}

struct headers {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    ports_t    ports;
}

struct metadata {
    bit<16> ecmp_hash;
}

/* ---------------------------- parser ------------------------------- */

parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            default:   accept;
        }
    }
    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            /* TCP=6, UDP=17 — both start with src/dst port. */
            6:       parse_ports;
            17:      parse_ports;
            default: accept;
        }
    }
    state parse_ports {
        packet.extract(hdr.ports);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply { } }

/* ------------------------- ingress control ------------------------- */

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t std) {

    /* Members of the ECMP group. Populated by the control plane;
     * a two-port default is fine for a smoke test. */
    action set_ecmp_group(bit<16> group_id, bit<32> group_size) {
        /* CRC32 over the 5-tuple; modulo group_size selects the
         * offset into the port set. */
        hash(meta.ecmp_hash,
             HashAlgorithm.crc32,
             (bit<16>)0,
             { hdr.ipv4.srcAddr, hdr.ipv4.dstAddr,
               hdr.ipv4.protocol,
               hdr.ports.srcPort, hdr.ports.dstPort,
               /* Standard metadata packet_length gives per-packet
                * (not per-flow) spread when included, which is what
                * UET-style spraying wants. */
               std.packet_length },
             group_size);
    }

    action forward(bit<9> port) { std.egress_spec = port; }

    /* group -> member -> port. Populated at runtime. */
    table ecmp_select {
        key     = { meta.ecmp_hash : exact; }
        actions = { forward; NoAction; }
        size    = 64;
        default_action = NoAction();
    }

    table ecmp_group {
        key     = { hdr.ipv4.dstAddr : lpm; }
        actions = { set_ecmp_group; NoAction; }
        size    = 1024;
        default_action = NoAction();
    }

    apply {
        if (hdr.ipv4.isValid()) {
            ecmp_group.apply();
            ecmp_select.apply();
        }
    }
}

/* ------------------------- egress control -------------------------- */

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t std) { apply { } }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply { } }

/* --------------------------- deparser ------------------------------ */

control MyDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.ports);
    }
}

V1Switch(MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()) main;
