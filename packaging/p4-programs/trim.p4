/* trim.p4 — packet trimming under egress queue pressure (UEC primitive).
 *
 * Instead of dropping a large packet when the buffer is full, keep
 * only its headers and forward the trimmed shell. The receiver sees
 * "a packet went past that would have arrived if the fabric had had
 * room" — instant congestion signal, no timeout, no cwnd guess.
 * This is one of the primitives Ultra Ethernet defines to replace
 * classical tail-drop under head-of-line pressure.
 *
 * Implementation shape: at egress, if the enqueue depth is over
 * TRIM_THRESHOLD, mark the packet as trimmed (bit in a metadata
 * header) and truncate to L4-header length. The recirculation
 * primitive is not used — we just adjust std.packet_length so the
 * deparser emits fewer bytes. The receiver needs the same trim-aware
 * transport to make sense of the shorter arrivals; this program
 * models the switch side only.
 *
 * A production trim implementation would use extern truncate() (bmv2
 * supports it) and set a DSCP codepoint that survives IP-in-IP.
 * Kept simple here — the point is to be readable, not deployable.
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<19> TRIM_THRESHOLD = 20;
const bit<32> TRIM_KEEP_BYTES = 64;   /* Ethernet + IPv4 + a bit of L4 */

/* Use a DSCP value in the Local/Experimental range to signal a
 * trimmed packet to the receiver. Not a standard codepoint yet —
 * the actual UET codepoint assignment is still moving. */
const bit<6> DSCP_TRIMMED = 63;

/* ---------------------------- headers ------------------------------ */

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<6>  dscp;
    bit<2>  ecn;
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

struct headers { ethernet_t ethernet; ipv4_t ipv4; }
struct metadata { }

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
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply { } }

/* ------------------------- ingress control ------------------------- */

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t std) {

    action forward(bit<9> port) { std.egress_spec = port; }

    table dmac {
        key     = { hdr.ethernet.dstAddr : exact; }
        actions = { forward; NoAction; }
        size    = 4096;
        default_action = NoAction();
    }

    apply { dmac.apply(); }
}

/* ------------------------- egress control -------------------------- */

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t std) {
    apply {
        /* Only trim if the queue is deep AND the packet is IPv4 AND
         * it is large enough that trimming is meaningful. A 128-byte
         * ACK is not worth trimming. */
        if (hdr.ipv4.isValid() &&
            std.enq_qdepth >= TRIM_THRESHOLD &&
            std.packet_length > TRIM_KEEP_BYTES) {
            hdr.ipv4.dscp     = DSCP_TRIMMED;
            hdr.ipv4.totalLen = (bit<16>)TRIM_KEEP_BYTES - 14; /* strip eth */
            truncate(TRIM_KEEP_BYTES);
        }
    }
}

control MyComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {
        update_checksum(hdr.ipv4.isValid(),
            { hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.dscp, hdr.ipv4.ecn,
              hdr.ipv4.totalLen, hdr.ipv4.identification,
              hdr.ipv4.flags, hdr.ipv4.fragOffset,
              hdr.ipv4.ttl, hdr.ipv4.protocol,
              hdr.ipv4.srcAddr, hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

/* --------------------------- deparser ------------------------------ */

control MyDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
    }
}

V1Switch(MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()) main;
