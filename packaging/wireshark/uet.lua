-- UltraEthernet wireshark dissector
--
-- Copyright Keysight Technologies 2024
--
-- Copyright (c) 2025, Broadcom. All rights reserved. The term
-- Broadcom refers to Broadcom Limited and/or its subsidiaries.
--
-- To use this dissector, make it available in the wireshark plugins
-- directory, for example:
--
-- ln -sf $(pwd)/uet.lua ~/.config/wireshark/plugins/

p_uet = Proto("uet", "UltraEthernet Transport")
p_uet.prefs["ip_proto"] = Pref.uint("IP protocol#", 253, "IP protocol number for UET")
p_uet.prefs["track"] = Pref.bool("Track requests and responses", true,
	"Enable to track request/response/ACK packet relationships (at the expense of memory and/or CPU usage)")

-- LIST
local UET_EXPECTED = 0  -- 0x0
local UET_OVERFLOW = 1  -- 0x1
local LIST_STR_MAP = {
	[UET_EXPECTED] = "UET_EXPECTED",
	[UET_OVERFLOW] = "UET_OVERFLOW",
}
local LIST_DESC_MAP = {
	[UET_EXPECTED] = "EXPECTED - Operation matched the expected list",
	[UET_OVERFLOW] = "OVERFLOW - Operation was buffered in the overflow buffer space and tracked an unexpected header",
}

-- PDS_NEXT_HDR
local UET_HDR_NONE                = 0  -- 0x0
local UET_HDR_REQUEST_SMALL       = 1  -- 0x1
local UET_HDR_REQUEST_MEDIUM      = 2  -- 0x2
local UET_HDR_REQUEST_STD         = 3  -- 0x3
local UET_HDR_RESPONSE            = 4  -- 0x4
local UET_HDR_RESPONSE_DATA       = 5  -- 0x5
local UET_HDR_RESPONSE_DATA_SMALL = 6  -- 0x6
local PDS_NEXT_HDR_STR_MAP = {
	[UET_HDR_NONE]                = "UET_HDR_NONE",
	[UET_HDR_REQUEST_MEDIUM]      = "UET_HDR_REQUEST_MEDIUM",
	[UET_HDR_REQUEST_SMALL]       = "UET_HDR_REQUEST_SMALL",
	[UET_HDR_REQUEST_STD]         = "UET_HDR_REQUEST_STD",
	[UET_HDR_RESPONSE]            = "UET_HDR_RESPONSE",
	[UET_HDR_RESPONSE_DATA]       = "UET_HDR_RESPONSE_DATA",
	[UET_HDR_RESPONSE_DATA_SMALL] = "UET_HDR_RESPONSE_DATA_SMALL",
}
local PDS_NEXT_HDR_DESC_MAP = {
	[UET_HDR_NONE]                = "NONE",
	[UET_HDR_REQUEST_MEDIUM]      = "REQUEST_MEDIUM",
	[UET_HDR_REQUEST_SMALL]       = "REQUEST_SMALL",
	[UET_HDR_REQUEST_STD]         = "REQUEST_STD",
	[UET_HDR_RESPONSE]            = "RESPONSE",
	[UET_HDR_RESPONSE_DATA]       = "RESPONSE_DATA",
	[UET_HDR_RESPONSE_DATA_SMALL] = "RESPONSE_DATA_SMALL",
}

-- PDS_CTRL_TYPE
local UET_PDS_CTRL_TYPE_NOP         = 0x0
local UET_PDS_CTRL_TYPE_ACK_REQ     = 0x1
local UET_PDS_CTRL_TYPE_CLEAR       = 0x2
local UET_PDS_CTRL_TYPE_CLEAR_REQ   = 0x3
local UET_PDS_CTRL_TYPE_CLOSE       = 0x4
local UET_PDS_CTRL_TYPE_CLOSE_REQ   = 0x5
local UET_PDS_CTRL_TYPE_PROBE       = 0x6
local UET_PDS_CTRL_TYPE_CREDIT      = 0x7
local UET_PDS_CTRL_TYPE_CREDIT_REQ  = 0x8
local UET_PDS_CTRL_TYPE_NEGOTIATION = 0x9
local PDS_CTRL_TYPE_STR_MAP = {
	[UET_PDS_CTRL_TYPE_NOP]         = "UET_CTRL_TYPE_NOP",
	[UET_PDS_CTRL_TYPE_ACK_REQ]     = "UET_CTRL_TYPE_ACK_REQ",
	[UET_PDS_CTRL_TYPE_CLEAR]       = "UET_CTRL_TYPE_CLEAR",
	[UET_PDS_CTRL_TYPE_CLEAR_REQ]   = "UET_CTRL_TYPE_CLEAR_REQ",
	[UET_PDS_CTRL_TYPE_CLOSE]       = "UET_CTRL_TYPE_CLOSE",
	[UET_PDS_CTRL_TYPE_CLOSE_REQ]   = "UET_CTRL_TYPE_CLOSE_REQ",
	[UET_PDS_CTRL_TYPE_PROBE]       = "UET_CTRL_TYPE_PROBE",
	[UET_PDS_CTRL_TYPE_CREDIT]      = "UET_CTRL_TYPE_CREDIT",
	[UET_PDS_CTRL_TYPE_CREDIT_REQ]  = "UET_CTRL_TYPE_CREDIT_REQ",
	[UET_PDS_CTRL_TYPE_NEGOTIATION] = "UET_CTRL_TYPE_NEGOTIATION",
}
local PDS_CTRL_TYPE_DESC_MAP = {
	[UET_PDS_CTRL_TYPE_NOP]         = "CTRL_TYPE_NOP",
	[UET_PDS_CTRL_TYPE_ACK_REQ]     = "CTRL_TYPE_ACK_REQ",
	[UET_PDS_CTRL_TYPE_CLEAR]       = "CTRL_TYPE_CLEAR",
	[UET_PDS_CTRL_TYPE_CLEAR_REQ]   = "CTRL_TYPE_CLEAR_REQ",
	[UET_PDS_CTRL_TYPE_CLOSE]       = "CTRL_TYPE_CLOSE",
	[UET_PDS_CTRL_TYPE_CLOSE_REQ]   = "CTRL_TYPE_CLOSE_REQ",
	[UET_PDS_CTRL_TYPE_PROBE]       = "CTRL_TYPE_PROBE",
	[UET_PDS_CTRL_TYPE_CREDIT]      = "CTRL_TYPE_CREDIT",
	[UET_PDS_CTRL_TYPE_CREDIT_REQ]  = "CTRL_TYPE_CREDIT_REQ",
	[UET_PDS_CTRL_TYPE_NEGOTIATION] = "CTRL_TYPE_NEGOTIATION",
}

-- PDS_TYPE
local TYPE_UET_SEC    = 1  -- 0x1
local TYPE_RUD_REQ    = 2  -- 0x2
local TYPE_ROD_REQ    = 3  -- 0x3
local TYPE_RUDI_REQ   = 4  -- 0x4
local TYPE_RUDI_RESP  = 5  -- 0x5
local TYPE_UUD_REQ    = 6  -- 0x6
local TYPE_ACK        = 7  -- 0x7
local TYPE_ACK_CC     = 8  -- 0x8
local TYPE_ACK_CCX    = 9  -- 0x9
local TYPE_NACK       = 10  -- 0xa
local TYPE_CTRL       = 11  -- 0xb
local TYPE_NACK_CCX   = 12  -- 0xc
local TYPE_RUD_CC_REQ = 13  -- 0xd
local TYPE_ROD_CC_REQ = 14  -- 0xe
local PDS_TYPE_STR_MAP = {
	[TYPE_ACK]        = "TYPE_ACK",
	[TYPE_ACK_CC]     = "TYPE_ACK_CC",
	[TYPE_ACK_CCX]    = "TYPE_ACK_CCX",
	[TYPE_CTRL]       = "TYPE_CTRL",
	[TYPE_NACK]       = "TYPE_NACK",
	[TYPE_NACK_CCX]   = "TYPE_NACK_CCX",
	[TYPE_ROD_REQ]    = "TYPE_ROD_REQ",
	[TYPE_ROD_CC_REQ] = "TYPE_ROD_CC_REQ",
	[TYPE_RUDI_REQ]   = "TYPE_RUDI_REQ",
	[TYPE_RUDI_RESP]  = "TYPE_RUDI_RESP",
	[TYPE_RUD_REQ]    = "TYPE_RUD_REQ",
	[TYPE_RUD_CC_REQ] = "TYPE_RUD_CC_REQ",
	[TYPE_UET_SEC]    = "TYPE_UET_SEC",
	[TYPE_UUD_REQ]    = "TYPE_UUD_REQ",
}
local PDS_TYPE_DESC_MAP = {
	[TYPE_ACK]        = "ACK - Acknowledge",
	[TYPE_ACK_CC]     = "ACK_CC - ACK w/ Congestion Control",
	[TYPE_ACK_CCX]    = "ACK_CCX - ACK w/ Congestion Control Extended ",
	[TYPE_CTRL]       = "CTRL - Control",
	[TYPE_NACK]       = "NACK - Negative acknowledge",
	[TYPE_NACK_CCX]   = "NACK_CCX - Negative acknowledge",
	[TYPE_ROD_REQ]    = "ROD_REQ - ROD Request",
	[TYPE_ROD_CC_REQ] = "ROD_CC_REQ - ROD Request w/ Congestion Controll state",
	[TYPE_RUDI_REQ]   = "RUDI_REQ - RUDI Request",
	[TYPE_RUDI_RESP]  = "RUDI_RESP - RUDI Response",
	[TYPE_RUD_REQ]    = "RUD_REQ - RUD Request",
	[TYPE_RUD_CC_REQ] = "RUD_REQ - RUD Request w/ Congestion Control state",
	[TYPE_UET_SEC]    = "UET_SEC - UET Encryption header",
	[TYPE_UUD_REQ]    = "UUD_REQ - UUD Request",
}

-- NACK_CODE
local UET_NACK_NONE              = 0x00
local UET_NACK_TRIMMED           = 0x01
local UET_NACK_TRIMMED_LAST_HOP  = 0x02
local UET_NACK_TRIMMED_ACK       = 0x03
local UET_NACK_NO_PDC_AVAIL      = 0x04
local UET_NACK_NO_CCC_AVAIL      = 0x05
local UET_NACK_NO_BITMAP         = 0x06
local UET_NACK_NO_PKT_BUF        = 0x07
local UET_NACK_NO_GTD_DEL_AVAIL  = 0x08
local UET_NACK_NO_SES_MSG_AVAIL  = 0x09
local UET_NACK_NO_RESOURCE       = 0x0a
local UET_NACK_PSN_OOR_WINDOW    = 0x0b
local UET_NACK_ROD_OOO           = 0x0d
local UET_NACK_INV_DPDCID        = 0x0e
local UET_NACK_PDC_HDR_MISMATCH  = 0x0f
local UET_NACK_CLOSING           = 0x10
local UET_NACK_CLOSING_IN_ERR    = 0x11
local UET_NACK_PKT_NOT_RCVD      = 0x12
local UET_NACK_GTD_RESP_UNAVAIL  = 0x13
local UET_NACK_ACK_WITH_DATA     = 0x14
local UET_NACK_INVALID_SYN       = 0x15
local UET_NACK_PDC_MODE_MISMATCH = 0x16
local UET_NACK_NEW_START_PSN     = 0x17
local UET_NACK_RCVD_SES_PROCG    = 0x18
local UET_NACK_UNEXP_EVENT       = 0x19
local UET_NACK_RCVR_INFER_LESS   = 0x1a
local UET_NACK_EXP_NACK_NORMAL   = 0xfd
local UET_NACK_EXP_NACK_ERR      = 0xfe
local UET_NACK_EXP_NACK_FATAL    = 0xff
local PDS_NACK_CODE_STR_MAP = {
	[UET_NACK_NONE]             = "NACK_NONE",
	[UET_NACK_TRIMMED]          = "NACK_TRIMMED",
	[UET_NACK_TRIMMED_LAST_HOP] = "NACK_TRIMMED_LAST_HOP",
	[UET_NACK_TRIMMED_ACK]      = "NACK_TRIMMED_ACK",
	[UET_NACK_NO_PDC_AVAIL]     = "NACK_NO_PDC_AVAIL",
	[UET_NACK_NO_CCC_AVAIL]     = "NACK_NO_CCC_AVAIL",
	[UET_NACK_NO_BITMAP]        = "NACK_NO_BITMAP",
	[UET_NACK_NO_PKT_BUF]       = "NACK_NO_PKT_BUF",
	[UET_NACK_NO_GTD_DEL_AVAIL] = "NACK_NO_GTD_DEL_AVAIL",
	[UET_NACK_NO_SES_MSG_AVAIL] = "NACK_NO_SES_MSG_AVAIL",
	[UET_NACK_NO_RESOURCE]      = "NACK_NO_RESOURCE",
	[UET_NACK_PSN_OOR_WINDOW]   = "NACK_PSN_OOR_WINDOW",
	[UET_NACK_ROD_OOO]          = "NACK_ROD_OOO",
	[UET_NACK_INV_DPDCID]       = "NACK_INV_DPDCID",
	[UET_NACK_PDC_HDR_MISMATCH] = "NACK_PDC_HDR_MISMATCH",
	[UET_NACK_CLOSING]          = "NACK_CLOSING",
	[UET_NACK_CLOSING_IN_ERR]   = "NACK_CLOSING_IN_ERR",
	[UET_NACK_PKT_NOT_RCVD]     = "NACK_PKT_NOT_RCtVD",
	[UET_NACK_GTD_RESP_UNAVAIL] = "NACK_GTD_RESP_UNAVAIL",
	[UET_NACK_ACK_WITH_DATA]    = "NACK_ACK_WITH_DATA",
	[UET_NACK_INVALID_SYN]      = "NACK_INVALID_SYN",
	[UET_NACK_PDC_MODE_MISMATCH]= "NACK_PDC_MODE_MISMATCH",
	[UET_NACK_NEW_START_PSN]    = "NACK_NEW_START_PSN",
	[UET_NACK_RCVD_SES_PROCG]   = "NACK_RCVD_SES_PROCG",
	[UET_NACK_UNEXP_EVENT]      = "NACK_UNEXP_EVENT",
	[UET_NACK_RCVR_INFER_LESS]  = "NACK_RCVR_INFER_LESS",
	[UET_NACK_EXP_NACK_NORMAL]  = "NACK_EXP_NACK_NORMAL",
	[UET_NACK_EXP_NACK_ERR]     = "NACK_EXP_NACK_ERR",
	[UET_NACK_EXP_NACK_FATAL]   = "NACK_EXP_NACK_FATAL",
}
local PDS_NACK_CODE_DESC_MAP = {
	[UET_NACK_NONE]             = "NONE",
	[UET_NACK_TRIMMED]          = "TRIMMED",
	[UET_NACK_TRIMMED_LAST_HOP] = "TRIMMED_LAST_HOP",
	[UET_NACK_TRIMMED_ACK]      = "TRIMMED_ACK",
	[UET_NACK_NO_PDC_AVAIL]     = "NO_PDC_AVAIL",
	[UET_NACK_NO_CCC_AVAIL]     = "NO_CCC_AVAIL",
	[UET_NACK_NO_BITMAP]        = "NO_BITMAP",
	[UET_NACK_NO_PKT_BUF]       = "NO_PKT_BUF",
	[UET_NACK_NO_GTD_DEL_AVAIL] = "NO_GTD_DEL_AVAIL",
	[UET_NACK_NO_SES_MSG_AVAIL] = "NO_SES_MSG_AVAIL",
	[UET_NACK_NO_RESOURCE]      = "NO_RESOURCE",
	[UET_NACK_PSN_OOR_WINDOW]   = "PSN_OOR_WINDOW",
	[UET_NACK_ROD_OOO]          = "ROD_OOO",
	[UET_NACK_INV_DPDCID]       = "INV_DPDCID",
	[UET_NACK_PDC_HDR_MISMATCH] = "PDC_HDR_MISMATCH",
	[UET_NACK_CLOSING]          = "CLOSING",
	[UET_NACK_CLOSING_IN_ERR]   = "CLOSING_IN_ERR",
	[UET_NACK_PKT_NOT_RCVD]     = "PKT_NOT_RCVD",
	[UET_NACK_GTD_RESP_UNAVAIL] = "GTD_RESP_UNAVAIL",
	[UET_NACK_ACK_WITH_DATA]    = "ACK_WITH_DATA",
	[UET_NACK_INVALID_SYN]      = "INVALID_SYN",
	[UET_NACK_PDC_MODE_MISMATCH]= "PDC_MODE_MISMATCH",
	[UET_NACK_NEW_START_PSN]    = "NEW_START_PSN",
	[UET_NACK_RCVD_SES_PROCG]   = "RCVD_SES_PROCG",
	[UET_NACK_UNEXP_EVENT]      = "UNEXP_EVENT",
	[UET_NACK_RCVR_INFER_LESS]  = "RCVR_INFER_LESS",
	[UET_NACK_EXP_NACK_NORMAL]  = "EXP_NACK_NORMAL",
	[UET_NACK_EXP_NACK_ERR]     = "EXP_NACK_ERR",
	[UET_NACK_EXP_NACK_FATAL]   = "EXP_NACK_FATAL",
}

-- REQ_OPCODE
local UET_NO_OP              = 0  -- 0x0
local UET_WRITE              = 1  -- 0x1
local UET_READ               = 2  -- 0x2
local UET_ATOMIC             = 3  -- 0x3
local UET_FETCHING_ATOMIC    = 4  -- 0x4
local UET_SEND               = 5  -- 0x5
local UET_RENDEZVOUS_SEND    = 6  -- 0x6
local UET_DATAGRAM_SEND      = 7  -- 0x7
local UET_DEFERRABLE_SEND    = 8  -- 0x8
local UET_TAGGED_SEND        = 9  -- 0x9
local UET_RENDEZVOUS_TSEND   = 10  -- 0xa
local UET_DEFERRABLE_TSEND   = 11  -- 0xb
local UET_DEFERRABLE_RTR     = 12  -- 0xc
local UET_TSEND_ATOMIC       = 13  -- 0xd
local UET_TSEND_FETCH_ATOMIC = 14  -- 0xe
local UET_MSG_ERROR          = 15  -- 0xf
local UET_OP_EXTENDED        = 63  -- 0x3f
local REQ_OPCODE_STR_MAP = {
	[UET_ATOMIC]             = "UET_ATOMIC",
	[UET_DATAGRAM_SEND]      = "UET_DATAGRAM_SEND",
	[UET_DEFERRABLE_RTR]     = "UET_DEFERRABLE_RTR",
	[UET_DEFERRABLE_SEND]    = "UET_DEFERRABLE_SEND",
	[UET_DEFERRABLE_TSEND]   = "UET_DEFERRABLE_TSEND",
	[UET_FETCHING_ATOMIC]    = "UET_FETCHING_ATOMIC",
	[UET_MSG_ERROR]          = "UET_MSG_ERROR",
	[UET_NO_OP]              = "UET_NO_OP",
	[UET_OP_EXTENDED]        = "UET_OP_EXTENDED",
	[UET_READ]               = "UET_READ",
	[UET_RENDEZVOUS_SEND]    = "UET_RENDEZVOUS_SEND",
	[UET_RENDEZVOUS_TSEND]   = "UET_RENDEZVOUS_TSEND",
	[UET_SEND]               = "UET_SEND",
	[UET_TAGGED_SEND]        = "UET_TAGGED_SEND",
	[UET_TSEND_ATOMIC]       = "UET_TSEND_ATOMIC",
	[UET_TSEND_FETCH_ATOMIC] = "UET_TSEND_FETCH_ATOMIC",
	[UET_WRITE]              = "UET_WRITE",
}
local REQ_OPCODE_DESC_MAP = {
	[UET_ATOMIC]             = "ATOMIC",
	[UET_DATAGRAM_SEND]      = "DATAGRAM_SEND - Only legal when used with UUD PDS type.",
	[UET_DEFERRABLE_RTR]     = "DEFERRABLE_RTR - A deferred send is ready to restart",
	[UET_DEFERRABLE_SEND]    = "DEFERRABLE_SEND - A send operation where the payload transfer may be deferred by the target.",
	[UET_DEFERRABLE_TSEND]   = "DEFERRABLE_TSEND - A deferrable version of the tagged send",
	[UET_FETCHING_ATOMIC]    = "FETCHING_ATOMIC - Includes compare and swap",
	[UET_MSG_ERROR]          = "MSG_ERROR - Used to terminate an in-progress message ID. Can be sent as an early final packet of a message for an in-flight message that encounters an error.",
	[UET_NO_OP]              = "NO_OP - May or may not be needed/useful. It always seems to wind up being included.",
	[UET_OP_EXTENDED]        = "OP_EXTENDED - Reserved for opcode space expansion",
	[UET_READ]               = "READ - RMA Read",
	[UET_RENDEZVOUS_SEND]    = "RENDEZVOUS_SEND - Incorporated to allow Send over ROD (for ordering) with bulk payload over RUD.",
	[UET_RENDEZVOUS_TSEND]   = "RENDEZVOUS_TSEND - A rendezvous version of the tagged send.",
	[UET_SEND]               = "SEND - (non-matching) send operation",
	[UET_TAGGED_SEND]        = "TAGGED_SEND - A tagged send operation using match bits for buffer selection.",
	[UET_TSEND_ATOMIC]       = "TSEND_ATOMIC - Atomic operations with tagged send addressing semantics.",
	[UET_TSEND_FETCH_ATOMIC] = "TSEND_FETCH_ATOMIC - Fetching atomic operations (including compare and swap) using tagged send addressing semantics",
	[UET_WRITE]              = "WRITE - RMA Write",
}

-- RET_CODE
local RC_NULL                 = 0  -- 0x0
local RC_OK                   = 1  -- 0x1
local RC_BAD_GENERATION       = 2  -- 0x2
local RC_DISABLED             = 3  -- 0x3
local RC_DISABLED_GEN         = 4  -- 0x4
local RC_NO_MATCH             = 5  -- 0x5
local RC_UNSUPPORTED_OP       = 6  -- 0x6
local RC_UNSUPPORTED_SIZE     = 7  -- 0x7
local RC_AT_INVALID           = 8  -- 0x8
local RC_AT_PERM              = 9  -- 0x9
local RC_AT_ATS_ERROR         = 10  -- 0xa
local RC_AT_NO_TRANS          = 11  -- 0xb
local RC_AT_OUT_OF_RANGE      = 12  -- 0xc
local RC_HOST_POISONED        = 13  -- 0xd
local RC_HOST_UNSUCCESS_CMPL  = 14  -- 0xe
local RC_AMO_UNSUPPORTED_OP   = 15  -- 0xf
local RC_AMO_UNSUPPORTED_DT   = 16  -- 0x10
local RC_AMO_UNSUPPORTED_SIZE = 17  -- 0x11
local RC_AMO_UNALIGNED        = 18  -- 0x12
local RC_AMO_FP_NAN           = 19  -- 0x13
local RC_AMO_FP_UNDERFLOW     = 20  -- 0x14
local RC_AMO_FP_OVERFLOW      = 21  -- 0x15
local RC_AMO_FP_INEXACT       = 22  -- 0x16
local RC_PERM_VIOLATION       = 23  -- 0x17
local RC_OP_VIOLATION         = 24  -- 0x18
local RC_BAD_INDEX            = 25  -- 0x19
local RC_BAD_PID              = 26  -- 0x1a
local RC_BAD_JOB_ID           = 27  -- 0x1b
local RC_BAD_MKEY             = 28  -- 0x1c
local RC_BAD_ADDR             = 29  -- 0x1d
local RC_CANCELLED            = 30  -- 0x1e
local RC_UNDELIVERABLE        = 31  -- 0x1f
local RC_UNCOR                = 32  -- 0x20
local RC_UNCOR_TRNSNT         = 33  -- 0x21
local RC_TOO_LONG             = 34  -- 0x22
local RC_INITIATOR_ERR        = 35  -- 0x23
local RC_DROPPED              = 36  -- 0x24
local RET_CODE_STR_MAP = {
	[RC_AMO_FP_INEXACT]       = "RC_AMO_FP_INEXACT",
	[RC_AMO_FP_NAN]           = "RC_AMO_FP_NAN",
	[RC_AMO_FP_OVERFLOW]      = "RC_AMO_FP_OVERFLOW",
	[RC_AMO_FP_UNDERFLOW]     = "RC_AMO_FP_UNDERFLOW",
	[RC_AMO_UNALIGNED]        = "RC_AMO_UNALIGNED",
	[RC_AMO_UNSUPPORTED_DT]   = "RC_AMO_UNSUPPORTED_DT",
	[RC_AMO_UNSUPPORTED_OP]   = "RC_AMO_UNSUPPORTED_OP",
	[RC_AMO_UNSUPPORTED_SIZE] = "RC_AMO_UNSUPPORTED_SIZE",
	[RC_AT_ATS_ERROR]         = "RC_AT_ATS_ERROR",
	[RC_AT_INVALID]           = "RC_AT_INVALID",
	[RC_AT_NO_TRANS]          = "RC_AT_NO_TRANS",
	[RC_AT_OUT_OF_RANGE]      = "RC_AT_OUT_OF_RANGE",
	[RC_AT_PERM]              = "RC_AT_PERM",
	[RC_BAD_ADDR]             = "RC_BAD_ADDR",
	[RC_BAD_GENERATION]       = "RC_BAD_GENERATION",
	[RC_BAD_INDEX]            = "RC_BAD_INDEX",
	[RC_BAD_JOB_ID]           = "RC_BAD_JOB_ID",
	[RC_BAD_MKEY]             = "RC_BAD_MKEY",
	[RC_BAD_PID]              = "RC_BAD_PID",
	[RC_CANCELLED]            = "RC_CANCELLED",
	[RC_DISABLED]             = "RC_DISABLED",
	[RC_DISABLED_GEN]         = "RC_DISABLED_GEN",
	[RC_DROPPED]              = "RC_DROPPED",
	[RC_HOST_POISONED]        = "RC_HOST_POISONED",
	[RC_HOST_UNSUCCESS_CMPL]  = "RC_HOST_UNSUCCESS_CMPL",
	[RC_INITIATOR_ERR]        = "RC_INITIATOR_ERR",
	[RC_NO_MATCH]             = "RC_NO_MATCH",
	[RC_NULL]                 = "RC_NULL",
	[RC_OK]                   = "RC_OK",
	[RC_OP_VIOLATION]         = "RC_OP_VIOLATION",
	[RC_PERM_VIOLATION]       = "RC_PERM_VIOLATION",
	[RC_TOO_LONG]             = "RC_TOO_LONG",
	[RC_UNCOR]                = "RC_UNCOR",
	[RC_UNCOR_TRNSNT]         = "RC_UNCOR_TRNSNT",
	[RC_UNDELIVERABLE]        = "RC_UNDELIVERABLE",
	[RC_UNSUPPORTED_OP]       = "RC_UNSUPPORTED_OP",
	[RC_UNSUPPORTED_SIZE]     = "RC_UNSUPPORTED_SIZE",
}
local RET_CODE_DESC_MAP = {
	[RC_AMO_FP_INEXACT]       = "AMO_FP_INEXACT",
	[RC_AMO_FP_NAN]           = "AMO_FP_NAN",
	[RC_AMO_FP_OVERFLOW]      = "AMO_FP_OVERFLOW",
	[RC_AMO_FP_UNDERFLOW]     = "AMO_FP_UNDERFLOW",
	[RC_AMO_UNALIGNED]        = "AMO_UNALIGNED",
	[RC_AMO_UNSUPPORTED_DT]   = "AMO_UNSUPPORTED_DT",
	[RC_AMO_UNSUPPORTED_OP]   = "AMO_UNSUPPORTED_OP",
	[RC_AMO_UNSUPPORTED_SIZE] = "AMO_UNSUPPORTED_SIZE",
	[RC_AT_ATS_ERROR]         = "AT_ATS_ERROR",
	[RC_AT_INVALID]           = "AT_INVALID",
	[RC_AT_NO_TRANS]          = "AT_NO_TRANS",
	[RC_AT_OUT_OF_RANGE]      = "AT_OUT_OF_RANGE",
	[RC_AT_PERM]              = "AT_PERM",
	[RC_BAD_ADDR]             = "BAD_ADDR",
	[RC_BAD_GENERATION]       = "BAD_GENERATION",
	[RC_BAD_INDEX]            = "BAD_INDEX",
	[RC_BAD_JOB_ID]           = "BAD_JOB_ID",
	[RC_BAD_MKEY]             = "BAD_MKEY",
	[RC_BAD_PID]              = "BAD_PID",
	[RC_CANCELLED]            = "CANCELLED",
	[RC_DISABLED]             = "DISABLED",
	[RC_DISABLED_GEN]         = "DISABLED_GEN",
	[RC_DROPPED]              = "DROPPED",
	[RC_HOST_POISONED]        = "HOST_POISONED",
	[RC_HOST_UNSUCCESS_CMPL]  = "HOST_UNSUCCESS_CMPL",
	[RC_INITIATOR_ERR]        = "INITIATOR_ERR",
	[RC_NO_MATCH]             = "NO_MATCH",
	[RC_NULL]                 = "NULL",
	[RC_OK]                   = "OK",
	[RC_OP_VIOLATION]         = "OP_VIOLATION",
	[RC_PERM_VIOLATION]       = "PERM_VIOLATION",
	[RC_TOO_LONG]             = "TOO_LONG",
	[RC_UNCOR]                = "UNCOR",
	[RC_UNCOR_TRNSNT]         = "UNCOR_TRNSNT",
	[RC_UNDELIVERABLE]        = "UNDELIVERABLE",
	[RC_UNSUPPORTED_OP]       = "UNSUPPORTED_OP",
	[RC_UNSUPPORTED_SIZE]     = "UNSUPPORTED_SIZE",
}

-- RSP_OPCODE
local UET_DEFAULT_RESPONSE = 0  -- 0x0
local UET_RESPONSE         = 1  -- 0x1
local UET_RESPONSE_W_DATA  = 2  -- 0x2
local UET_NO_RESPONSE      = 3  -- 0x3
local RSP_OPCODE_STR_MAP = {
	[UET_DEFAULT_RESPONSE] = "UET_DEFAULT_RESPONSE",
	[UET_NO_RESPONSE]      = "UET_NO_RESPONSE",
	[UET_RESPONSE]         = "UET_RESPONSE",
	[UET_RESPONSE_W_DATA]  = "UET_RESPONSE_W_DATA",
}
local RSP_OPCODE_DESC_MAP = {
	[UET_DEFAULT_RESPONSE] = "DEFAULT_RESPONSE",
	[UET_NO_RESPONSE]      = "NO_RESPONSE",
	[UET_RESPONSE]         = "RESPONSE",
	[UET_RESPONSE_W_DATA]  = "RESPONSE_W_DATA",
}

-- ACK_REQ_CLR_CLS
local UET_ACK_REQ_NO_REQUEST = 0
local UET_ACK_REQ_CLEAR      = 1
local UET_ACK_REQ_CLOSE      = 2
local UET_ACK_REQ_RESERVED   = 3
local UET_ACK_REQ_STR_MAP = {
	[UET_ACK_REQ_NO_REQUEST] = "UET_ACK_REQ_NO_REQUEST",
	[UET_ACK_REQ_CLEAR]      = "UET_ACK_REQ_CLEAR",
	[UET_ACK_REQ_CLOSE]      = "UET_ACK_REQ_CLOSE",
	[UET_ACK_REQ_RESERVED]   = "UET_ACK_REQ_RESERVED",
}
local UET_ACK_REQ_DESC_MAP = {
	[UET_ACK_REQ_NO_REQUEST] = "NO_REQUEST",
	[UET_ACK_REQ_CLEAR]      = "REQ_CLEAR",
	[UET_ACK_REQ_CLOSE]      = "REQ_CLOSE",
	[UET_ACK_REQ_RESERVED]   = "RESERVED",
}

local yes_no = {"Yes", "No"}
local fld = p_uet.fields

fld.entropy		= ProtoField.none("uet.entropy",		"Entropy")
fld.entropy_val		= ProtoField.uint16("uet.entropy.entropy",	"Entropy",	base.DEC)
fld.entropy_rsv		= ProtoField.uint16("uet.entropy.rsvd",		"Reserved",	base.DEC)

fld.sec			= ProtoField.none("uet.sec",			"Security")
fld.sec_type		= ProtoField.uint32("uet.sec.type",		"Type",		base.HEX, PDS_TYPE_DESC_MAP, 0xf8000000)
fld.sec_flags		= ProtoField.uint32("uet.sec.flags",		"Flags",	base.HEX, nil, 0x07000000)
fld.sec_flags_sp	= ProtoField.uint32("uet.sec.flags.sp",		"SP",		base.DEC, nil, 0x04000000)
fld.sec_flags_rsv	= ProtoField.uint32("uet.sec.flags.rsv",	"RSV",		base.DEC, nil, 0x02000000)
fld.sec_flags_an	= ProtoField.uint32("uet.sec.flags.an",		"AN",		base.DEC, nil, 0x01000000)
fld.sec_sdi		= ProtoField.uint32("uet.sec.sdi",		"SDI",		base.DEC, nil, 0x00ffffff)
fld.sec_ssi		= ProtoField.uint32("uet.sec.ssi",		"SSI",		base.HEX)
fld.sec_epoch		= ProtoField.uint64("uet.sec.epoch",		"Epoch",	base.HEX, nil, UInt64("0xffff000000000000"))
fld.sec_tsc		= ProtoField.uint64("uet.sec.tsc",		"TSC",		base.HEX, nil, UInt64("0x0000ffffffffffff"))

fld.pds			= ProtoField.none("uet.pds",			"PDS")
fld.pds_type		= ProtoField.uint16("uet.pds.type",		"Type",			base.HEX, PDS_TYPE_DESC_MAP, 0xf800)
fld.pds_next_hdr	= ProtoField.uint16("uet.pds.next_hdr",		"Next Header",		base.HEX, PDS_NEXT_HDR_DESC_MAP, 0x0780)
fld.pds_ctrl_type	= ProtoField.uint16("uet.pds.ctrl_type",	"Control Type",		base.HEX, PDS_CTRL_TYPE_DESC_MAP, 0x0780)
fld.pds_flags		= ProtoField.uint16("uet.pds.flags",		"Flags",		base.HEX, nil, 0x007f)
fld.pds_clr_psn_off	= ProtoField.int16("uet.pds.clear_psn_offset",	"Clear PSN offset",	base.DEC)
fld.pds_psn		= ProtoField.uint32("uet.pds.psn",		"PSN",			base.DEC)
fld.pds_cack_psn	= ProtoField.uint32("uet.pds.cack_psn",		"Cumulative ACK PSN",	base.DEC)
fld.pds_spdcid		= ProtoField.uint16("uet.pds.spdcid",		"Source PDC ID",	base.DEC)
fld.pds_dpdcid		= ProtoField.uint16("uet.pds.dpdcid",		"Destination PDC ID",	base.DEC)
fld.pds_pdc_info	= ProtoField.uint16("uet.pds.pdc_info",		"PDC Info",		base.HEX, nil, 0xf000)
fld.pds_pdc_offset	= ProtoField.uint16("uet.pds.pdc_offset",	"PDC Offset",		base.DEC, nil, 0x0fff)
fld.pds_ack_psn_off	= ProtoField.int16("uet.pds.ack_psn_offset",	"ACK PSN offset",	base.DEC)
fld.pds_cack_psn	= ProtoField.uint32("uet.pds.cack_psn",		"CACK PSN",		base.DEC)
fld.pds_nack_code	= ProtoField.uint8("uet.pds.nack_code",		"NACK Code",		base.DEC, PDS_NACK_CODE_DESC_MAP)
fld.pds_vendor_code	= ProtoField.uint8("uet.pds.vendor_code",	"Vendor Code",		base.DEC)
fld.pds_nack_psn	= ProtoField.uint32("uet.pds.nack_psn",		"NACK PSN",		base.DEC)
fld.pds_nack_payload	= ProtoField.uint32("uet.pds.nack_payload",	"NACK Payload",		base.DEC)
fld.pds_probe_opaque	= ProtoField.uint16("uet.pds.probe_opaque",	"Probe Opaque",		base.DEC)
fld.pds_reserved	= ProtoField.uint16("uet.pds.reserved",		"Reserved",		base.DEC)
fld.pds_ctrl_payload	= ProtoField.uint16("uet.pds.ctrl_payload",	"Control Payload",	base.DEC)

-- ROD/RUD request flags
fld.pds_flag_rreq_rsv	= ProtoField.uint16("uet.pds.flags.rreq.rsv",	"RSV",			base.DEC, nil, 0x60)
fld.pds_flag_rreq_retx	= ProtoField.bool("uet.pds.flags.rreq.retx",	"RETX (is retransmit)",	16, yes_no, 0x10)
fld.pds_flag_rreq_ar	= ProtoField.bool("uet.pds.flags.rreq.ar",	"AR (ACK Request)",	16, yes_no, 0x08)
fld.pds_flag_rreq_syn	= ProtoField.bool("uet.pds.flags.rreq.syn",	"SYN",			16, yes_no, 0x04)
fld.pds_flag_rreq_rsv2	= ProtoField.uint16("uet.pds.flags.rreq.rsv2",	"RSV2",			base.DEC, nil, 0x03)

-- ACK flags
fld.pds_flag_ack_rsv		= ProtoField.uint16("uet.pds.flags.ack.rsv",		"RSV",			base.DEC, nil, 0x40)
fld.pds_flag_ack_m		= ProtoField.bool("uet.pds.flags.ack.m",		"M (ECN marked)",	16, yes_no, 0x20)
fld.pds_flag_ack_retx		= ProtoField.bool("uet.pds.flags.ack.retx",		"RETX",			16, yes_no, 0x10)
fld.pds_flag_ack_p		= ProtoField.bool("uet.pds.flags.ack.probe",		"P (Probe)",		16, yes_no, 0x08)
fld.pds_flag_ack_req		= ProtoField.uint16("uet.pds.flags.ack.req",		"REQ",			base.DEC, UET_ACK_REQ_DESC_MAP, 0x06)
fld.pds_flag_ack_rsv2		= ProtoField.uint16("uet.pds.flags.ack.rsv2",		"RSV2",			base.DEC, nil, 0x01)

-- NACK flags
fld.pds_flag_nack_rsv		= ProtoField.uint16("uet.pds.flags.nack.rsv",		"RSV",			base.DEC, nil, 0x40)
fld.pds_flag_nack_m		= ProtoField.bool("uet.pds.flags.nack.m",		"M (ECN marked)",	16, yes_no, 0x20)
fld.pds_flag_nack_retx		= ProtoField.bool("uet.pds.flags.nack.retx",		"RETX",			16, yes_no, 0x10)
fld.pds_flag_nack_nt		= ProtoField.bool("uet.pds.flags.nack.nt",		"NACK Type",		16, yes_no, 0x08)
fld.pds_flag_nack_rsv2		= ProtoField.uint16("uet.pds.flags.nack.rsv2",		"RSV2",			base.DEC, nil, 0x07)

-- CTRL flags
fld.pds_flag_ctrl_rsv		= ProtoField.uint16("uet.pds.flags.ctrl.rsv",		"RSV",			base.DEC, nil, 0x40)
fld.pds_flag_ctrl_isrod		= ProtoField.bool("uet.pds.flags.ctrl.isrod",		"Is ROD",		16, yes_no, 0x20)
fld.pds_flag_ctrl_retx		= ProtoField.bool("uet.pds.flags.ctrl.retx",		"RETX",			16, yes_no, 0x10)
fld.pds_flag_ctrl_ar		= ProtoField.bool("uet.pds.flags.ctrl.ar",		"AR (ACK Request)",	16, yes_no, 0x08)
fld.pds_flag_ctrl_syn		= ProtoField.bool("uet.pds.flags.ctrl.syn",		"SYN",			16, yes_no, 0x04)
fld.pds_flag_ctrl_rsv2		= ProtoField.uint16("uet.pds.flags.ctrl.rsv2",		"RSV2",			base.DEC, nil, 0x03)

-- RUDI flags
fld.pds_flag_rudi_rsv		= ProtoField.uint16("uet.pds.flags.rudi.rsv",		"RSV",			base.DEC, nil, 0x40)
fld.pds_flag_rudi_m		= ProtoField.bool("uet.pds.flags.rudi.m",		"M (ECN marked)",	16, yes_no, 0x20)
fld.pds_flag_rudi_retx		= ProtoField.bool("uet.pds.flags.rudi.retx",		"RETX",			16, yes_no, 0x10)
fld.pds_flag_rudi_rsv2		= ProtoField.uint16("uet.pds.flags.rudi.rsv2",		"RSV2",			base.DEC, nil, 0x0f)

-- SES_REQ
fld.ses_req			= ProtoField.none("uet.ses.req",			"SES request")
fld.ses_req_rsv			= ProtoField.uint8("uet.ses.req.rsv",			"Reserved",			base.HEX, nil, 0xc0)
fld.ses_req_opcode		= ProtoField.uint8("uet.ses.req.opcode",		"Opcode",			base.HEX, REQ_OPCODE_DESC_MAP, 0x3f)
fld.ses_req_ver			= ProtoField.uint8("uet.ses.req.version",		"Version",			base.HEX, nil, 0xc0)
fld.ses_req_flag_dc		= ProtoField.bool("uet.ses.req.flags.dc",		"Delivery Complete (DC)",	8, yes_no, 0x20)
fld.ses_req_flag_ie		= ProtoField.bool("uet.ses.req.flags.ie",		"Initiator Error (IE)",		8, yes_no, 0x10)
fld.ses_req_flag_relative	= ProtoField.bool("uet.ses.req.flags.relative",		"Relative",			8, yes_no, 0x08)
fld.ses_req_flag_hd		= ProtoField.bool("uet.ses.req.flags.hd",		"Header Data (HD)",		8, yes_no, 0x04)
fld.ses_req_flag_eom		= ProtoField.bool("uet.ses.req.flags.eom",		"End of Message (EOM)",		8, yes_no, 0x02)
fld.ses_req_flag_som		= ProtoField.bool("uet.ses.req.flags.som",		"Start of Message (SOM)",	8, yes_no, 0x01)
fld.ses_ri_gen			= ProtoField.uint8("uet.ses.ri_gen",			"Resource Index Generation",	base.DEC)
fld.ses_job_id			= ProtoField.uint24("uet.ses.job_id",			"Job ID",			base.DEC)
fld.ses_req_rsv2		= ProtoField.uint16("uet.ses.req.rsv2",			"Reserved",			base.HEX, nil, 0xf000)
fld.ses_index			= ProtoField.uint16("uet.ses.index",			"Index",			base.DEC, nil, 0x0fff)
fld.ses_req_rsv3		= ProtoField.uint16("uet.ses.req.rsv3",			"Reserved",			base.HEX, nil, 0xf000)
fld.ses_req_pidonfep		= ProtoField.uint16("uet.ses.req.pid_on_fep",		"PID on FEP",			base.DEC, nil, 0x0fff)
fld.ses_msgid			= ProtoField.uint16("uet.ses.message_id",		"Message ID",			base.DEC)
fld.ses_req_buff_offs		= ProtoField.uint64("uet.ses.req.buffer_offset",	"Buffer offset",		base.DEC_HEX)
fld.ses_req_initiator		= ProtoField.uint32("uet.ses.req.initiator",		"Initiator",			base.HEX)
fld.ses_req_match_bits		= ProtoField.uint64("uet.ses.req.match_bits",		"Match bits",			base.HEX)
fld.ses_req_hdr_data		= ProtoField.uint64("uet.ses.req.header_data",		"Header data",			base.HEX)
fld.ses_req_len			= ProtoField.uint64("uet.ses.req.len",			"Request length",		base.DEC_HEX)
fld.ses_req_msg_offset		= ProtoField.uint32("uet.ses.req.msg_offset",		"Message offset",		base.DEC_HEX)
fld.ses_req_payload_len_rsvd	= ProtoField.uint32("uet.ses.req.payload_len_rsvd",	"Reserved",			base.HEX,     nil, 0xffffc000)
fld.ses_req_payload_len		= ProtoField.uint32("uet.ses.req.payload_len",		"Payload length",		base.DEC_HEX, nil, 0x00003fff)

-- SES_RESP
fld.ses_resp			= ProtoField.none("uet.ses.resp",			"SES response")
fld.ses_resp_data		= ProtoField.none("uet.ses.resp_data",			"SES response w/ data")
fld.ses_resp_list		= ProtoField.uint8("uet.ses.resp.list",			"List",				base.DEC, LIST_DESC_MAP, 0xc0)
fld.ses_resp_opcode		= ProtoField.uint8("uet.ses.resp.opcode",		"Opcode",			base.HEX, RSP_OPCODE_DESC_MAP, 0x3f)
fld.ses_resp_ver		= ProtoField.uint8("uet.ses.resp.version",		"Version",			base.DEC, nil, 0xc0)
fld.ses_resp_retcode		= ProtoField.uint8("uet.ses.resp.retcode",		"Return code",			base.HEX, RET_CODE_DESC_MAP, 0x3f)
-- ses_msgid
-- ses_ri_gen
-- ses_job_id
fld.ses_resp_rreq_msgid		= ProtoField.uint16("uet.ses.resp.req_message_id",	"Read Request Message ID",	base.DEC)
fld.ses_resp_mod_len		= ProtoField.uint32("uet.ses.resp.mod_length",		"Modified length",		base.DEC_HEX)
fld.ses_resp_msg_offset		= ProtoField.uint32("uet.ses.resp.msg_offset",		"Message offset",		base.DEC_HEX)
-- TODO?
fld.ses_resp_rsvd		= ProtoField.uint16("uet.ses.resp.reserved",		"Reserved",			base.HEX,     nil, 0xc000)
fld.ses_resp_payload_len	= ProtoField.uint16("uet.ses.resp.payload_len",		"Payload length",		base.DEC_HEX, nil, 0x3fff)

fld.payload	= ProtoField.bytes("uet.payload",	"Payload")
fld.crc		= ProtoField.bytes("uet.crc",		"CRC")
fld.icv		= ProtoField.bytes("uet.icv",		"ICV")

fld.pds_req_in	= ProtoField.framenum("uet.pds.request_in",	"Request in",	base.NONE, frametype.REQUEST)
fld.pds_ack_in	= ProtoField.framenum("uet.pds.ack_in",		"ACK in",	base.NONE, frametype.ACK)
fld.ses_req_in	= ProtoField.framenum("uet.ses.request_in",	"Request in",	base.NONE, frametype.REQUEST)
fld.ses_resp_in	= ProtoField.framenum("uet.ses.response_in",	"Response in",	base.NONE, frametype.RESPONSE)

local pds_req_map = {} -- <IP, PDCID, PSN> -> framenum
local pds_frame_info = {} -- framenum -> table
local ses_req_map = {} -- <IP, MSG_ID> -> framenum

function track_key(net_addr, pdcid_tvb, psn)
	-- TODO: anything more efficient than strings?
	return tostring(net_addr) .. "--" .. tostring(pdcid_tvb:uint()) .. "--" .. tostring(psn)
end

function ses_track_key(net_addr, msg_id)
	-- TODO: anything more efficient than strings?
	return tostring(net_addr) .. "--" .. tostring(msg_id:uint())
end

local udp_src = Field.new("udp.srcport")
local ip_proto = Field.new("ip.proto")

function p_uet.dissector(buf, pinfo, root)
	pinfo.cols.protocol = p_uet.name

	local fi = nil
	local ses_tree = nil

	local proto_tree = root:add(p_uet, buf:range(0))
	local offset = 0

	-- if UDP is the parent header then no entropy header
	local entropy_len = 0
	if not udp_src() then
		entropy_len = 4
		local entropy_subtree = proto_tree:add(fld.entropy, buf:range(offset, 4))
		entropy_subtree:add(fld.entropy_val, buf(offset, 2))
		offset = offset + 2
		entropy_subtree:add(fld.entropy_rsv, buf(offset, 2))
		offset = offset + 2
	end

	local h_type = buf(offset, 1):bitfield(0, 5)

	if h_type == TYPE_UET_SEC then

		local sec_tree = proto_tree:add(fld.sec, buf:range(offset))
		sec_tree:add(fld.sec_type, buf(offset, 4))
		local flags_tree = sec_tree:add(fld.sec_flags, buf(offset, 4))
		flags_tree:add(fld.sec_flags_sp, buf(offset, 4))
		flags_tree:add(fld.sec_flags_rsv, buf(offset, 4))
		flags_tree:add(fld.sec_flags_an, buf(offset, 4))
		sec_tree:add(fld.sec_sdi, buf(offset, 4))
		local sp_set = buf(offset, 4):bitfield(5, 1) == 1
		offset = offset + 4

		if sp_set then
			sec_tree:add(fld.sec_ssi, buf(offset, 4))
			offset = offset + 4
		end

		sec_tree:add(fld.sec_epoch, buf(offset, 8))
		sec_tree:add(fld.sec_tsc, buf(offset, 8))
		offset = offset + 8

		sec_tree:set_len(offset - entropy_len)

		pinfo.cols.info = PDS_TYPE_STR_MAP[h_type]

		proto_tree:set_len(offset)
		local payload_len = buf:len() - offset

		-- adjust the payload length for the ICV
		payload_len = payload_len - 16

		if payload_len > 0 then
			root:add(fld.payload, buf(offset, payload_len))
		end

		root:add(fld.icv, buf((offset + payload_len), 16))

		return
	end

	local pds_tree = proto_tree:add(fld.pds, buf:range(offset))
	pds_tree:add(fld.pds_type, buf(offset, 2))
	local h_next = buf(offset, 2):bitfield(5, 4)
	local type_str = PDS_TYPE_STR_MAP[h_type]
	local summary = ""
	if type_str ~= nil then
		summary = type_str
	end

	local do_track = p_uet.prefs.track and not pinfo.visited and not pinfo.in_error_pkt
	if do_track then
		pds_frame_info[pinfo.number] = {}
	end

	-- TODO: h_type == TYPE_RUDI_REQ
	-- TODO: h_type == TYPE_RUDI_RESP
	-- TODO: h_type == TYPE_UUD_REQ

	if h_type == TYPE_RUD_REQ or h_type == TYPE_ROD_REQ then

		pds_tree:add(fld.pds_next_hdr, buf(offset, 2))
		local flags_tree = pds_tree:add(fld.pds_flags, buf(offset, 2))
		flags_tree:add(fld.pds_flag_rreq_rsv, buf(offset, 2))
		flags_tree:add(fld.pds_flag_rreq_retx, buf(offset, 2))
		flags_tree:add(fld.pds_flag_rreq_ar, buf(offset, 2))
		flags_tree:add(fld.pds_flag_rreq_syn, buf(offset, 2))
		flags_tree:add(fld.pds_flag_rreq_rsv2, buf(offset, 2))
		local syn_set = buf(offset, 2):bitfield(13, 1) == 1
		offset = offset + 2

		local b_clr_psn_off = buf(offset, 2)
		pds_tree:add(fld.pds_clr_psn_off, b_clr_psn_off)
		-- TODO: display calculated Clear PSN from PSN
		offset = offset + 2

		local b_psn = buf(offset, 4)
		pds_tree:add(fld.pds_psn, b_psn)
		offset = offset + 4

		local b_spdcid = buf(offset, 2)
		pds_tree:add(fld.pds_spdcid, b_spdcid)
		offset = offset + 2

		local b_dpdcid = buf(offset, 2)
		if syn_set then
			pds_tree:add(fld.pds_pdc_info, b_dpdcid)
			pds_tree:add(fld.pds_pdc_offset, b_dpdcid)
		else
			pds_tree:add(fld.pds_dpdcid, b_dpdcid)
		end
		offset = offset + 2

		-- TODO: CC (RUD_CC_REQ, ROD_CC_REQ)

		pds_tree:set_len(offset - entropy_len)

		if do_track then
			local key = track_key(pinfo.net_src, b_spdcid, b_psn:uint())
			pds_req_map[key] = pinfo.number
		end

	end if h_type == TYPE_ACK then

		pds_tree:add(fld.pds_next_hdr, buf(offset, 2))
		local flags_tree = pds_tree:add(fld.pds_flags, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_rsv, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_m, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_retx, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_p, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_req, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_rsv2, buf(offset, 2))
		local probe_set = buf(offset, 2):bitfield(12, 1) == 1
		offset = offset + 2

		local b_ack_psn_off = buf(offset, 2)
		if probe_set then
			pds_tree:add(fld.pds_probe_opaque, b_ack_psn_off)
		else
			pds_tree:add(fld.pds_ack_psn_off, b_ack_psn_off)
			-- TODO: display calculated ACK PSN from CACK PSN
		end
		offset = offset + 2

		local b_cack_psn = buf(offset, 4)
		pds_tree:add(fld.pds_cack_psn, b_cack_psn)
		offset = offset + 4

		local b_spdcid = buf(offset, 2)
		pds_tree:add(fld.pds_spdcid, b_spdcid)
		offset = offset + 2

		local b_dpdcid = buf(offset, 2)
		pds_tree:add(fld.pds_dpdcid, b_dpdcid)
		offset = offset + 2

		pds_tree:set_len(offset - entropy_len)

		-- TODO: CC (ACK_CC, ACK_CCX)

		if do_track and not probe_set then
			local key = track_key(pinfo.net_dst, b_dpdcid,
					      (b_cack_psn:uint() + b_ack_psn_off:int()))
			local req = pds_req_map[key]
			if req ~= nil then
				pds_frame_info[pinfo.number].pds_req_in = req
				pds_frame_info[req].pds_ack_in = pinfo.number
			end
		end

	end if h_type == TYPE_NACK then

		pds_tree:add(fld.pds_next_hdr, buf(offset, 2))
		local flags_tree = pds_tree:add(fld.pds_flags, buf(offset, 2))
		flags_tree:add(fld.pds_flag_nack_rsv, buf(offset, 2))
		flags_tree:add(fld.pds_flag_nack_m, buf(offset, 2))
		flags_tree:add(fld.pds_flag_nack_retx, buf(offset, 2))
		flags_tree:add(fld.pds_flag_nack_nt, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_rsv2, buf(offset, 2))
		offset = offset + 2

		local b_nack_code = buf(offset, 1)
		pds_tree:add(fld.pds_nack_code, b_nack_code)
		offset = offset + 1

		local b_vendor_code = buf(offset, 1)
		pds_tree:add(fld.pds_vendor_code, b_vendor_code)
		offset = offset + 1

		local b_nack_psn = buf(offset, 4)
		pds_tree:add(fld.pds_nack_psn, b_nack_psn)
		offset = offset + 4

		local b_spdcid = buf(offset, 2)
		pds_tree:add(fld.pds_spdcid, b_spdcid)
		offset = offset + 2

		local b_dpdcid = buf(offset, 2)
		pds_tree:add(fld.pds_dpdcid, b_dpdcid)
		offset = offset + 2

		local b_nack_payload = buf(offset, 4)
		pds_tree:add(fld.pds_nack_payload, b_nack_payload)
		offset = offset + 4

		pds_tree:set_len(offset - entropy_len)

		-- TODO: CC (NACK_CCX)

		if do_track then
			local key = track_key(pinfo.net_dst, b_dpdcid, b_nack_psn:uint())
			local req = pds_req_map[key]
			if req ~= nil then
				pds_frame_info[pinfo.number].pds_req_in = req
				pds_frame_info[req].pds_ack_in = pinfo.number
			end
		end

	end if h_type == TYPE_CTRL then

		local ctrl_type = buf(offset, 2):bitfield(5, 4)
		pds_tree:add(fld.pds_ctrl_type, buf(offset, 2))
		local flags_tree = pds_tree:add(fld.pds_flags, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ctrl_rsv, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ctrl_isrod, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ctrl_retx, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ctrl_ar, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ctrl_syn, buf(offset, 2))
		flags_tree:add(fld.pds_flag_ack_rsv2, buf(offset, 2))
		local syn_set = buf(offset, 2):bitfield(13, 1) == 1
		offset = offset + 2

		local b_probe_opaque = buf(offset, 2)
		if ctrl_type == UET_PDS_CTRL_TYPE_PROBE then
			pds_tree:add(fld.pds_probe_opaque, b_probe_opaque)
		else
			pds_tree:add(fld.pds_reserved, b_probe_opaque)
		end
		offset = offset + 2

		local b_psn = buf(offset, 4)
		pds_tree:add(fld.pds_psn, b_psn)
		offset = offset + 4

		local b_spdcid = buf(offset, 2)
		pds_tree:add(fld.pds_spdcid, b_spdcid)
		offset = offset + 2

		local b_dpdcid = buf(offset, 2)
		if syn_set then
			pds_tree:add(fld.pds_pdc_info, b_dpdcid)
			pds_tree:add(fld.pds_pdc_offset, b_dpdcid)
		else
			pds_tree:add(fld.pds_dpdcid, b_dpdcid)
		end
		offset = offset + 2

		local b_ctrl_payload = buf(offset, 4)
		pds_tree:add(fld.pds_ctrl_payload, b_ctrl_payload)
		offset = offset + 4

		pds_tree:set_len(offset - entropy_len)

		if do_track then
			local key = track_key(pinfo.net_src, b_spdcid, b_psn:uint())
			pds_req_map[key] = pinfo.number
		end

	end

	fi = pds_frame_info[pinfo.number]
	if fi ~= nil then
		if fi.pds_req_in ~= nil then
			pds_tree:add(fld.pds_req_in, fi.pds_req_in):set_generated()
		end
		if fi.ack_in ~= nil then
			pds_tree:add(fld.pds_ack_in, fi.pds_ack_in):set_generated()
		end
	end

	if h_type == TYPE_NACK or h_type == TYPE_CTRL then
		goto done
	end

	if h_next == UET_HDR_REQUEST_STD then

		local opcode = buf(offset, 1):bitfield(2, 6)
		local opcode_str = REQ_OPCODE_STR_MAP[opcode]
		if opcode_str ~= nil then
			summary = summary .. " " .. opcode_str
		end

		local req_tree = proto_tree:add(fld.ses_req, buf(offset, 44))
		ses_tree = req_tree
		req_tree:add(fld.ses_req_rsv, buf(offset, 1))
		req_tree:add(fld.ses_req_opcode, buf(offset, 1))
		req_tree:add(fld.ses_req_ver, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_dc, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_ie, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_relative, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_hd, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_eom, buf(offset+1, 1))
		req_tree:add(fld.ses_req_flag_som, buf(offset+1, 1))
		local is_som = buf(offset+1, 1):bitfield(7, 1)
		local b_msgid = buf(offset+2, 2)
		req_tree:add(fld.ses_msgid, b_msgid)
		req_tree:add(fld.ses_ri_gen, buf(offset+4, 1))
		req_tree:add(fld.ses_job_id, buf(offset+5, 3))
		req_tree:add(fld.ses_req_rsv2, buf(offset+8, 2))
		req_tree:add(fld.ses_req_pidonfep, buf(offset+8, 2))
		req_tree:add(fld.ses_req_rsv3, buf(offset+10, 2))
		req_tree:add(fld.ses_index, buf(offset+10, 2))
		req_tree:add(fld.ses_req_buff_offs, buf(offset+12, 8))
		req_tree:add(fld.ses_req_initiator, buf(offset+20, 4))
		req_tree:add(fld.ses_req_match_bits, buf(offset+24, 8))
		if is_som == 1 then
			req_tree:add(fld.ses_req_hdr_data, buf(offset+32, 8))
		else
			req_tree:add(fld.ses_req_payload_len_rsvd, buf(offset+32, 4))
			req_tree:add(fld.ses_req_payload_len, buf(offset+32, 4))
			req_tree:add(fld.ses_req_msg_offset, buf(offset+36, 4))
		end
		req_tree:add(fld.ses_req_len, buf(offset+40, 4))
		offset = offset + 44

		if do_track then
			local key = ses_track_key(pinfo.net_src, b_msgid)
			ses_req_map[key] = pinfo.number
		end

	end if h_next == UET_HDR_RESPONSE then

		local opcode = buf(offset, 1):bitfield(2, 6)
		local opcode_str = RSP_OPCODE_STR_MAP[opcode]
		if opcode_str ~= nil then
			summary = summary .. " " .. opcode_str
		end
		local resp_tree = proto_tree:add(fld.ses_resp, buf(offset, 12))
		ses_tree = resp_tree
		resp_tree:add(fld.ses_resp_list, buf(offset, 1))
		resp_tree:add(fld.ses_resp_opcode, buf(offset, 1))
		resp_tree:add(fld.ses_resp_ver, buf(offset+1, 1))
		resp_tree:add(fld.ses_resp_retcode, buf(offset+1, 1))
		resp_tree:add(fld.ses_msgid, buf(offset+2, 2))
		resp_tree:add(fld.ses_ri_gen, buf(offset+4, 1))
		resp_tree:add(fld.ses_job_id, buf(offset+5, 3))
		resp_tree:add(fld.ses_resp_mod_len, buf(offset+8, 4))
		offset = offset + 12

	end if h_next == UET_HDR_RESPONSE_DATA then

		local opcode = buf(offset, 1):bitfield(2, 6)
		local opcode_str = RSP_OPCODE_STR_MAP[opcode]
		if opcode_str ~= nil then
			summary = summary .. " " .. opcode_str
		end
		local resp_tree = proto_tree:add(fld.ses_resp_data, buf(offset, 20))
		ses_tree = resp_tree
		resp_tree:add(fld.ses_resp_list, buf(offset, 1))
		resp_tree:add(fld.ses_resp_opcode, buf(offset, 1))
		resp_tree:add(fld.ses_resp_ver, buf(offset+1, 1))
		resp_tree:add(fld.ses_resp_retcode, buf(offset+1, 1))
		resp_tree:add(fld.ses_msgid, buf(offset+2, 2)) -- of itself
		resp_tree:add(fld.ses_ri_gen, buf(offset+4, 1)) -- FIXME: reserved in HDR_RSP_DATA
		resp_tree:add(fld.ses_job_id, buf(offset+5, 3))
		local b_rreq_msgid = buf(offset+8, 2)
		resp_tree:add(fld.ses_resp_rreq_msgid, b_rreq_msgid)
		resp_tree:add(fld.ses_resp_rsvd, buf(offset+10, 2))
		resp_tree:add(fld.ses_resp_payload_len, buf(offset+10, 2))
		resp_tree:add(fld.ses_resp_mod_len, buf(offset+12, 4))
		resp_tree:add(fld.ses_resp_msg_offset, buf(offset+16, 4))
		offset = offset + 20

		if do_track then
			local fkey = ses_track_key(pinfo.net_dst, b_rreq_msgid)
			local fw = ses_req_map[fkey]
			if fw ~= nil then
				pds_frame_info[pinfo.number].ses_req_in = fw
				pds_frame_info[fw].ses_resp_in = pinfo.number
			end
		end

	end

	::done::

	if fi ~= nil and ses_tree ~= nil then
		if fi.ses_req_in ~= nil then
			ses_tree:add(fld.ses_req_in, fi.ses_req_in):set_generated()
		end
		if fi.ses_resp_in ~= nil then
			ses_tree:add(fld.ses_resp_in, fi.ses_resp_in):set_generated()
		end
	end

	if summary ~= "" then
		pinfo.cols.info = summary
	end

	proto_tree:set_len(offset)
	local payload_len = buf:len() - offset

	-- adjust the payload length for the CRC
	payload_len = payload_len - 4

	if payload_len > 0 then
		root:add(fld.payload, buf(offset, payload_len))
	end

	root:add(fld.crc, buf((offset + payload_len), 4))
end

function p_uet:init()
	local ip_table = DissectorTable.get("ip.proto")
	local udp_table = DissectorTable.get("udp.port")
	ip_table:add(p_uet.prefs["ip_proto"], p_uet)
	udp_table:add_for_decode_as(p_uet)
	pds_req_map = {}
	pds_frame_info = {}
	ses_req_map = {}
end

