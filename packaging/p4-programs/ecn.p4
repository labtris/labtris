/* ecn.p4 — mark ECN CE when the egress queue crosses a threshold.
 *
 * The foundation of DCTCP, DCQCN, and every RoCE/UET congestion
 * control scheme. Instead of tail-dropping when a buffer fills, the
 * switch stamps the ECN Congestion Experienced bit on packets whose
 * arrival contributes to queue depth; the receiver echoes that back
 * (ECE / CNP) and the sender slows down before any packet is lost.
 *
 * Threshold here is a compile-time constant for legibility. In a
 * production data plane you would want per-priority thresholds,
 * random early marking below the hard threshold, and a per-egress
 * running-average — see RFC 3168 + the DCTCP paper.
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;

/* Mark CE (0b11) when the egress queue depth reaches this many
 * pending units at deparse time. simple_switch reports the queue
 * depth AT ENQUEUE via standard_metadata.enq_qdepth. */
const bit<19> ECN_THRESHOLD = 10;

/* ECN codepoints (RFC 3168): Not-ECT=00, ECT(0)=10, ECT(1)=01, CE=11. */
const bit<2> ECN_NOT_ECT = 0;
const bit<2> ECN_CE      = 3;

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
        /* Only mark packets that the sender has opted into ECN.
         * If ECN=00 (Not-ECT), the sender does not support it and
         * the correct response to congestion is still a drop. */
        if (hdr.ipv4.isValid() &&
            hdr.ipv4.ecn != ECN_NOT_ECT &&
            std.enq_qdepth >= ECN_THRESHOLD) {
            hdr.ipv4.ecn = ECN_CE;
        }
    }
}

/* Recompute the IPv4 checksum because we may have edited the DS/ECN byte. */
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
