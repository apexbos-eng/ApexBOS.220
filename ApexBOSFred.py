#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApexBOSFred.py - Clean-Room 450 MHz EOT / HOT (AAR S-9152) SDR Demodulator & Decoder
Copyright (c) 2026 AB. Licensed under the MIT License.

Clean-room Python/NumPy implementation of North American railroad End-of-Train (EOT / "FRED",
457.9375 MHz) and Head-of-Train (HOT, 452.9375 MHz) 1200 baud coherent FFSK/MSK (1200/1800 Hz)
+ AAR S-9152 74-bit BCH(63, 45) telemetry decoding.

Designed to run on a dedicated SDR alongside ApexBOSGrabber (220 MHz PTC) and
ApexBOSLatch (900 MHz ATCS), publishing unified telemetry into:
  - ApexBOSFlux / DispatcherEngine / Layout Packet Scroller (ZMQ PUB :18301)
  - ApexBOSSpy protocol inspector
  - ApexBOSChroma RF spectrum & waterfall (gated XPUB :18399, REP control :18398)

Disclaimer: Amateur railfan / RF research reference tool ONLY. Not for operational railway use.
"""

import argparse
import collections
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import zmq
except ImportError:
    zmq = None

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import ApexBOSGrabber as grabber
import ApexBOSLatch as latch_mod
import config_loader
from version import VERSION, __version__

# ---------------------------------------------------------------------------
# AAR S-9152 EOT / HOT RF & Protocol Constants
# ---------------------------------------------------------------------------
DEFAULT_FRED_ZMQ_PORT = 18301
DEFAULT_FRED_CHROMA_PORT = 18399
DEFAULT_FRED_CTRL_PORT = 18398

FRED_BAUD_RATE = 1200
FRED_DEMOD_RATE = 48000             # Exact 40 audio samples per 1200 baud FFSK bit
FRED_NARROW_SAMPLE_RATE = 240000    # Optional low RTL2832U rate (28.8 MHz / 120 = 5 * 48 kHz)
FRED_DEFAULT_SAMPLE_RATE = 960000   # Narrowest alias-free R820T/RTL2832U rate (20 * 48 kHz; avoids 300k IF aliasing & adds +6dB decimation gain)
FRED_CHANNEL_BW_HZ = 12500          # Standard 12.5 kHz narrowband channel width (+/- 6.25 kHz) for EOT/HOT/DPU/RLC
FRED_SPS = FRED_DEMOD_RATE // FRED_BAUD_RATE  # 40
FRED_TONE_LOW_HZ = 1200.0           # 1.0 full cycle per bit period
FRED_TONE_HIGH_HZ = 1800.0          # 1.5 cycles per bit period

FRED_CHANNELS: Dict[int, Tuple[int, str, str]] = {
    1:  (457937500, "EOT",  "EOT -> HOT (457.93750 MHz)"),
    2:  (452937500, "HOT",  "HOT -> EOT (452.93750 MHz)"),
    3:  (452925000, "DPU1", "DPU Ch 1 Lead TX (452.92500 MHz)"),
    4:  (452950000, "DPU2", "DPU Ch 2 Lead TX (452.95000 MHz)"),
    5:  (457925000, "DPU3", "DPU Ch 3 Remote TX (457.92500 MHz)"),
    6:  (457950000, "DPU4", "DPU Ch 4 Remote TX (457.95000 MHz)"),
    7:  (452931250, "RLC1", "RLC Ch 1 Narrowband (452.93125 MHz)"),
    8:  (452943750, "RLC2", "RLC Ch 2 Narrowband (452.94375 MHz)"),
    9:  (452956250, "RLC3", "RLC Ch 3 Narrowband (452.95625 MHz)"),
    10: (452962500, "RLC4", "RLC Ch 4 Narrowband (452.96250 MHz)"),
    11: (452968750, "RLC5", "RLC Ch 5 Narrowband (452.96875 MHz)"),
}

FRED_457_GROUP = [1, 5, 6]                       # EOT + DPU Ch 3/4 (25 kHz span -> 1 RTL-SDR)
FRED_452_GROUP = [2, 3, 4, 7, 8, 9, 10, 11]      # HOT + DPU Ch 1/2 + RLC Ch 1-5 (43.75 kHz span -> 1 RTL-SDR)
FRED_DPU_GROUP = [3, 4, 5, 6]
FRED_RLC_GROUP = [7, 8, 9, 10, 11]
FRED_ALL_GROUP = list(FRED_CHANNELS.keys())

FRED_CHANNEL_ALIASES: Dict[str, int] = {
    "eot": 1, "hot": 2,
    "dpu1": 3, "dpu-1": 3, "dpu2": 4, "dpu-2": 4,
    "dpu3": 5, "dpu-3": 5, "dpu4": 6, "dpu-4": 6,
    "rlc1": 7, "rlc-1": 7, "rcl1": 7,
    "rlc2": 8, "rlc-2": 8, "rcl2": 8,
    "rlc3": 9, "rlc-3": 9, "rcl3": 9,
    "rlc4": 10, "rlc-4": 10, "rcl4": 10,
    "rlc5": 11, "rlc-5": 11, "rcl5": 11,
}

# DPU (Locotrol) V.23 AFSK tones & HDLC constants
DPU_V23_MARK_HZ = 1300.0
DPU_V23_SPACE_HZ = 2100.0
DPU_V23_BAUD = 1200
DPU_FSK_BAUD = 4800
RLC_FSK_BAUD = 2400

DPU_MSG_TYPES = {
    0x01: "CMD",
    0x02: "STATUS",
    0x03: "COMM_CHK",
    0x04: "BRAKE",
    0x05: "LINK",
    0x06: "ALARM",
}

# Full DSU / Locotrol DPU Protocol Constants
DSU_PACKET_TYPES = {0: "CM", 1: "ST", 2: "LK", 3: "LR"}
DSU_PACKET_TYPE_NAMES = {"CM": "Command", "ST": "Status", "LK": "Link", "LR": "LinkReply"}
DSU_ORIGINS = {0: "LD", 1: "RM"}
DSU_REPEAT_CODES = {0: "DI", 1: "RP", 2: "RO", 3: "DI"}
DSU_REVERSERS = {0: "CT", 1: "FO", 2: "RE", 3: "CT"}
DSU_ALARM_BITS = [
    (0x01, "EMR"),   # Emergency
    (0x02, "PEN"),   # Penalty
    (0x04, "PCS"),   # Power Cutout Switch open
    (0x08, "BPRS"),  # Uncommanded BP rise
    (0x10, "BPDR"),  # Uncommanded BP drop
]

# Default RLC (Remote Control Locomotive) field value tables (overridable via config/rlc_rules.json)
RLC_DEFAULT_FRAME_TYPES = {
    0: "OCU->LCU CTRL",
    1: "LCU->OCU STAT",
    2: "LINK",
    3: "LINK_ACK",
}
RLC_DEFAULT_SPEED_STEPS = {
    0: "STOP",
    1: "COAST",
    2: "1mph",
    3: "4mph",
    4: "7mph",
    5: "10mph",
    6: "15mph",
    7: "15mph+",
}
RLC_DEFAULT_REVERSERS = {
    0: "N",
    1: "F",
    2: "R",
    3: "N",
}

def fred_channel_kind(ch: int) -> str:
    if ch in (1, 2):
        return "eot" if ch == 1 else "hot"
    if ch in (3, 4, 5, 6):
        return "dpu"
    if ch in (7, 8, 9, 10, 11):
        return "rlc"
    return "eot"

# Standard AAR S-9152 74-bit frame: 11-bit Barker frame sync + 45 data bits + 18 BCH checkbits
FRED_SYNC_LEN = 11
FRED_SYNC_WORD = 0b11100010010  # Exact AAR S-9152 / PyEOT 11-bit Barker frame sync (0x712; 01001000111 LSB-first)
FRED_SYNC_BITS = np.array([(FRED_SYNC_WORD >> (FRED_SYNC_LEN - 1 - i)) & 1 for i in range(FRED_SYNC_LEN)], dtype=np.int8)
FRED_SYNC_BIPOLAR = (FRED_SYNC_BITS * 2 - 1).astype(np.float32)

# Exact AAR S-9152 BCH(63, 45) parameters (Rear-to-Front EOT: degree-18 generator & 18-bit XOR mask)
FRED_BCH_DEGREE = 18
FRED_BCH_POLY = 0b1111001101000001111       # 0x79A0F (degree 18)
FRED_BCH_CIPHER_MASK = 0b101011011101110000 # 0x2B770 (18-bit XOR mask on checkbits)
FRED_CODEWORD_BITS = 63
FRED_DATA_BITS = 45
FRED_FRAME_BITS = FRED_SYNC_LEN + FRED_CODEWORD_BITS  # 74 bits

# Exact AAR S-5701 / S-9152 Front-to-Rear HOT (452.9375 MHz) parameters:
#   - 24-bit Frame Sync: 1000 1111 0001 0001 0010 1001 (0x8F1129 MSB-first -> 0x9488F1 LSB-first over air)
#   - 3 repeated 63-bit BCH(63, 30) blocks (30 info bits + 33 check bits), each followed by 1 odd-parity bit
HOT_SYNC_LEN = 24
HOT_SYNC_WORD_SPEC = 0x8F1129   # 1000 1111 0001 0001 0010 1001 (MSB-to-LSB spec order)
HOT_SYNC_WORD_LSB = 0x9488F1    # 1001 0100 1000 1000 1111 0001 (LSB-first arrival order over air)
HOT_SYNC_LSB_BIPOLAR = np.array(
    [1.0 if ((HOT_SYNC_WORD_LSB >> (HOT_SYNC_LEN - 1 - i)) & 1) else -1.0 for i in range(HOT_SYNC_LEN)],
    dtype=np.float32,
)
HOT_SYNC_SPEC_BIPOLAR = np.array(
    [1.0 if ((HOT_SYNC_WORD_SPEC >> (HOT_SYNC_LEN - 1 - i)) & 1) else -1.0 for i in range(HOT_SYNC_LEN)],
    dtype=np.float32,
)
HOT_BCH_DEGREE = 33
HOT_DATA_BITS = 30
HOT_CODEWORD_BITS = 63
HOT_BLOCK_STRIDE_BITS = 64  # 63-bit BCH(63,30) block + 1 odd-parity bit
HOT_FRAME_MIN_BITS = HOT_SYNC_LEN + HOT_CODEWORD_BITS  # 87 bits (24 sync + 1st 63-bit block)
# Exact S-5701 degree-33 generator polynomial:
#   g(x) = x^33+x^32+x^30+x^29+x^28+x^27+x^26+x^23+x^22+x^20+x^15+x^14+x^13+x^11+x^9+x^8+x^6+x^5+x^2+x+1
HOT_BCH_POLY_S5701 = 0x37CD0EB67
HOT_BCH_POLY_REV = 0x39B5C2CFB  # Bit-reversed companion for LSB-first transmitted checkbits
HOT_BCH_CHECK_MASK = (1 << HOT_BCH_DEGREE) - 1
HOT_CMD_STATUS_REQ = 0x55       # 01010101: Status update request from front unit
HOT_CMD_EMERGENCY = 0xAA        # 10101010: Emergency brake application from front unit

BATTERY_COND_LABELS = {
    3: "OK",
    2: "Low",
    1: "Very Low",
    0: "Not Monitored",
}


def fred_channel_frequency(ch: int) -> int:
    if ch in FRED_CHANNELS:
        return FRED_CHANNELS[ch][0]
    raise ValueError(f"Unknown FRED channel {ch} (1=EOT 457.9375 MHz, 2=HOT 452.9375 MHz)")


# ---------------------------------------------------------------------------
# Clean-Room AAR S-5701 / S-9152 BCH(63, 45) Encoder & Syndrome Error Corrector
# ---------------------------------------------------------------------------
def _reverse_bits(val: int, width: int) -> int:
    v = int(val)
    out = 0
    for _ in range(int(width)):
        out = (out << 1) | (v & 1)
        v >>= 1
    return out


def _bch63_rem(val: int) -> int:
    rem = int(val)
    top = 1 << FRED_BCH_DEGREE
    for shift in range(rem.bit_length() - 1, FRED_BCH_DEGREE - 1, -1):
        if (rem >> shift) & 1:
            rem ^= (FRED_BCH_POLY << (shift - FRED_BCH_DEGREE))
    return rem & (top - 1)


def fred_bch_encode(data45: int) -> int:
    """
    Encodes 45 data bits into a 63-bit AAR S-5701 / S-9152 codeword:
    computes 18-bit BCH(63,45) remainder over the bit-reversed 45-bit data block
    with generator 0x79A0F and XORs the 18 checkbits with cipher key 0x2B770.
    """
    d = int(data45) & ((1 << FRED_DATA_BITS) - 1)
    d_rev = _reverse_bits(d, FRED_DATA_BITS)
    parity = _bch63_rem(d_rev << FRED_BCH_DEGREE)
    masked_parity = parity ^ FRED_BCH_CIPHER_MASK
    return (d << FRED_BCH_DEGREE) | masked_parity


def _build_bch63_syndrome_table() -> Dict[int, Tuple[int, int]]:
    """Precomputes 0/1/2-bit error masks for 63-bit AAR S-9152 codewords (~2,017 entries -> O(1) lookup)."""
    table: Dict[int, Tuple[int, int]] = {0: (0, 0)}
    for b1 in range(FRED_CODEWORD_BITS):
        m1 = 1 << b1
        syn = _bch63_rem(m1)
        if syn not in table:
            table[syn] = (m1, 1)
    for b1 in range(FRED_CODEWORD_BITS):
        m1 = 1 << b1
        for b2 in range(b1 + 1, FRED_CODEWORD_BITS):
            mask = m1 | (1 << b2)
            syn = _bch63_rem(mask)
            if syn not in table:
                table[syn] = (mask, 2)
    return table


_FRED_SYNDROME_TABLE = _build_bch63_syndrome_table()


def fred_bch_decode(codeword63: int, max_errors: int = 2) -> Tuple[Optional[int], int]:
    """
    Decodes a 63-bit AAR S-5701 / S-9152 codeword:
      - Unmasks the 18-bit checkbit cipher key (0x2B770)
      - Reverses the 45-bit data block per AAR S-5701 BCH polynomial convention
      - Corrects up to `max_errors` bits via O(1) syndrome lookup
    Returns (data45, num_errors) or (None, -1).
    """
    cw = int(codeword63) & ((1 << FRED_CODEWORD_BITS) - 1)
    data_rx = (cw >> FRED_BCH_DEGREE) & ((1 << FRED_DATA_BITS) - 1)
    chk_rx = (cw & ((1 << FRED_BCH_DEGREE) - 1)) ^ FRED_BCH_CIPHER_MASK
    cw_bch = (_reverse_bits(data_rx, FRED_DATA_BITS) << FRED_BCH_DEGREE) | chk_rx
    syn = _bch63_rem(cw_bch)
    entry = _FRED_SYNDROME_TABLE.get(syn)
    if entry is None:
        return None, -1
    err_mask, n_err = entry
    if n_err > max_errors:
        return None, -1
    corrected_bch = cw_bch ^ err_mask
    data_corr_rev = (corrected_bch >> FRED_BCH_DEGREE) & ((1 << FRED_DATA_BITS) - 1)
    return _reverse_bits(data_corr_rev, FRED_DATA_BITS), n_err


# ---------------------------------------------------------------------------
# Clean-Room AAR S-5701 / S-9152 Front-to-Rear HOT BCH(63, 30) Codec & Parser
# ---------------------------------------------------------------------------
def _bch33_rem(val: int, poly: int = HOT_BCH_POLY_REV) -> int:
    rem = int(val)
    top = 1 << HOT_BCH_DEGREE
    for shift in range(rem.bit_length() - 1, HOT_BCH_DEGREE - 1, -1):
        if (rem >> shift) & 1:
            rem ^= (poly << (shift - HOT_BCH_DEGREE))
    return rem & (top - 1)


def _build_hot_bch30_syndrome_table(poly: int) -> Dict[int, Tuple[int, int]]:
    """Precomputes 0/1/2-bit error masks for 63-bit AAR S-5701 HOT BCH(63,30) codewords (2,017 entries -> O(1))."""
    table: Dict[int, Tuple[int, int]] = {0: (0, 0)}
    for b1 in range(HOT_CODEWORD_BITS):
        m1 = 1 << b1
        syn = _bch33_rem(m1, poly)
        if syn not in table:
            table[syn] = (m1, 1)
    for b1 in range(HOT_CODEWORD_BITS):
        m1 = 1 << b1
        for b2 in range(b1 + 1, HOT_CODEWORD_BITS):
            mask = m1 | (1 << b2)
            syn = _bch33_rem(mask, poly)
            if syn not in table:
                table[syn] = (mask, 2)
    return table


_HOT_SYNDROME_TABLE_REV = _build_hot_bch30_syndrome_table(HOT_BCH_POLY_REV)
_HOT_SYNDROME_TABLE_DIRECT = _build_hot_bch30_syndrome_table(HOT_BCH_POLY_S5701)


def hot_bch30_encode(data30: int, invert_check: bool = False) -> int:
    """
    Encodes 30 Front-to-Rear HOT information bits into a 63-bit AAR S-5701 BCH(63, 30) codeword:
      30 info bits + 33 BCH check bits (transmitted LSB-first, matching S-5701 hardware LFSR).
    """
    d = int(data30) & ((1 << HOT_DATA_BITS) - 1)
    d_rev = _reverse_bits(d, HOT_DATA_BITS)
    parity = _bch33_rem(d_rev << HOT_BCH_DEGREE, HOT_BCH_POLY_REV)
    if invert_check:
        parity ^= HOT_BCH_CHECK_MASK
    return (d << HOT_BCH_DEGREE) | parity


def hot_bch30_decode(codeword63: int, max_errors: int = 2) -> Tuple[Optional[int], int]:
    """
    Decodes a 63-bit AAR S-5701 Front-to-Rear HOT BCH(63, 30) codeword (30 info + 33 check bits).
    Supports both S-5701 LSB-first checkbits (exact hardware convention) and unreversed checkbits,
    as well as unmasked or bitwise-inverted checkbits, with O(1) syndrome error correction up to `max_errors`.
    With 33 check bits (8.59e9 syndromes), false-accept probability is < 10^-6 even at t=2.
    """
    cw = int(codeword63) & ((1 << HOT_CODEWORD_BITS) - 1)
    data_rx = (cw >> HOT_BCH_DEGREE) & ((1 << HOT_DATA_BITS) - 1)
    chk_raw = cw & HOT_BCH_CHECK_MASK
    data_rev = _reverse_bits(data_rx, HOT_DATA_BITS)

    # 1. Primary: Exact S-5701 LSB-first checkbits (equivalent to rev(data30) mod HOT_BCH_POLY_REV)
    for mask in (0, HOT_BCH_CHECK_MASK):
        cw_bch = (data_rev << HOT_BCH_DEGREE) | (chk_raw ^ mask)
        syn = _bch33_rem(cw_bch, HOT_BCH_POLY_REV)
        entry = _HOT_SYNDROME_TABLE_REV.get(syn)
        if entry is not None and entry[1] <= max_errors:
            corr = cw_bch ^ entry[0]
            d_corr_rev = (corr >> HOT_BCH_DEGREE) & ((1 << HOT_DATA_BITS) - 1)
            return _reverse_bits(d_corr_rev, HOT_DATA_BITS), entry[1]

    # 2. Secondary: Direct MSB-first checkbits (data30 << 33 | chk mod HOT_BCH_POLY_S5701)
    for mask in (0, HOT_BCH_CHECK_MASK):
        cw_bch = (data_rx << HOT_BCH_DEGREE) | (chk_raw ^ mask)
        syn = _bch33_rem(cw_bch, HOT_BCH_POLY_S5701)
        entry = _HOT_SYNDROME_TABLE_DIRECT.get(syn)
        if entry is not None and entry[1] <= max_errors:
            corr = cw_bch ^ entry[0]
            d_corr = (corr >> HOT_BCH_DEGREE) & ((1 << HOT_DATA_BITS) - 1)
            return d_corr, entry[1]

    return None, -1


def pack_hot_data30(
    unit_id: int,
    command: int = HOT_CMD_STATUS_REQ,
    chaining: int = 0b11,
    msg_type: int = 0b000,
) -> int:
    """
    Packs AAR S-5701 Front-to-Rear HOT 30-bit information block (arrival order MSB->LSB):
      [29:28] (bits 0..1)   chaining (2b, always 11 for single-block front-to-rear)
      [27:25] (bits 2..4)   msg_type (3b, always 000 for front-to-rear command)
      [24:8]  (bits 5..21)  unit_id (17b, LSB-first, 00001..99999)
      [7:0]   (bits 22..29) command (8b, LSB-first: 0x55=Status Req, 0xAA=Emergency)
    """
    d30 = (
        ((int(chaining) & 0x3) << 28)
        | ((int(msg_type) & 0x7) << 25)
        | (_reverse_bits(int(unit_id) & 0x1FFFF, 17) << 8)
        | _reverse_bits(int(command) & 0xFF, 8)
    )
    return d30 & ((1 << HOT_DATA_BITS) - 1)


def parse_hot_data30(data30: int, channel_id: int = 2) -> Dict[str, Any]:
    """Unpacks a 30-bit AAR S-5701 Front-to-Rear HOT data block into structured HOT telemetry."""
    d = int(data30) & ((1 << HOT_DATA_BITS) - 1)
    chaining = (d >> 28) & 0x3
    msg_type = (d >> 25) & 0x7
    unit_id = _reverse_bits((d >> 8) & 0x1FFFF, 17)
    cmd_byte = _reverse_bits(d & 0xFF, 8)
    emergency = (cmd_byte == HOT_CMD_EMERGENCY)
    is_status_req = (cmd_byte == HOT_CMD_STATUS_REQ)
    if emergency:
        cmd_str = "EMERGENCY"
        type_name = "HOT_EMERG"
    elif is_status_req:
        cmd_str = "STATUS"
        type_name = "HOT_CMD"
    else:
        cmd_str = f"CMD:0x{cmd_byte:02X}"
        type_name = "HOT_CMD"
    hex_str = f"{d:08X}"
    scroll = f"[HOT #{unit_id:05d}] Cmd:{cmd_str} | Ch:{channel_id}"
    return {
        "protocol": "hot",
        "type_name": type_name,
        "chaining": chaining,
        "msg_type": msg_type,
        "unit_id": unit_id,
        "node_id": unit_id,
        "command": cmd_byte,
        "command_str": cmd_str,
        "armed": not emergency,
        "emergency": emergency,
        "bch_type": "BCH(63,30)",
        "hex": hex_str,
        "_node_id_": unit_id,
        "_scroll_ptc_": scroll,
        "_type_": type_name,
        "_sequence_": None,
        "telemetry": {
            "wiu_id": f"{unit_id:05d}",
            "src": f"HOT_{unit_id:05d}",
            "command": cmd_str,
            "emergency": emergency,
        },
    }


# ---------------------------------------------------------------------------
# AAR S-5701 / S-9152 45-Bit EOT / HOT Telemetry Packing & Parsing (LSB-first)
# ---------------------------------------------------------------------------
def pack_fred_data45(
    unit_id: int,
    pressure_psi: int = 89,
    motion: bool = True,
    marker_light: bool = True,
    marker_batt_ok: bool = True,
    battery_cond: int = 3,
    battery_pct: int = 95,
    valve_ok: bool = True,
    turbine_ok: bool = True,
    armed: bool = True,
    emergency: bool = False,
    is_hot: bool = False,
    chaining: int = 0,
) -> int:
    """
    Packs AAR S-5701 / S-9152 EOT/HOT telemetry fields into 45 data bits (data_block[0:45],
    where bit 44 is transmitted first after the 11-bit frame sync):
      [44:43] (bits 0..1)   chaining (2b)
      [42:41] (bits 2..3)   batt_cond (2b, LSB-first)
      [40:38] (bits 4..6)   msg_type (3b: 0=Normal EOT, 4=HOT, 5=HOT_EMERG)
      [37:21] (bits 7..23)  unit_id (17b, LSB-first, 00001..99999)
      [20:14] (bits 24..30) pressure_psi (7b, LSB-first, 0..127)
      [13:7]  (bits 31..37) batt_charge (7b, LSB-first, 0..127 -> 0..100%)
      [6]     (bit 38)      spare / emergency flag
      [5]     (bit 39)      valve_ckt_stat (valve_ok)
      [4]     (bit 40)      conf_ind (armed)
      [3]     (bit 41)      turbine_ok
      [2]     (bit 42)      motion
      [1]     (bit 43)      mkr_batt (marker_batt_ok)
      [0]     (bit 44)      mkr_light (marker_light)
    """
    msg_type = (5 if emergency else 4) if is_hot else (2 if armed else 0)
    batt_raw = max(0, min(127, int(round(max(0, min(100, int(battery_pct))) * 127.0 / 100.0))))
    d45 = (
        ((int(chaining) & 0x3) << 43)
        | (_reverse_bits(int(battery_cond) & 0x3, 2) << 41)
        | ((int(msg_type) & 0x7) << 38)
        | (_reverse_bits(int(unit_id) & 0x1FFFF, 17) << 21)
        | (_reverse_bits(max(0, min(127, int(pressure_psi))) & 0x7F, 7) << 14)
        | (_reverse_bits(batt_raw & 0x7F, 7) << 7)
        | ((1 if emergency else 0) << 6)
        | ((1 if valve_ok else 0) << 5)
        | ((1 if armed else 0) << 4)
        | ((1 if turbine_ok else 0) << 3)
        | ((1 if motion else 0) << 2)
        | ((1 if marker_batt_ok else 0) << 1)
        | (1 if marker_light else 0)
    )
    return d45 & ((1 << FRED_DATA_BITS) - 1)


def parse_fred_data45(data45: int, channel_id: int = 1) -> Dict[str, Any]:
    """Unpacks 45-bit AAR S-5701 / S-9152 data word (LSB-first fields) into EOT / HOT telemetry."""
    d = int(data45) & ((1 << FRED_DATA_BITS) - 1)
    chaining = (d >> 43) & 0x3
    batt_cond_code = _reverse_bits((d >> 41) & 0x3, 2)
    msg_type = (d >> 38) & 0x7
    unit_id = _reverse_bits((d >> 21) & 0x1FFFF, 17)
    pressure_psi = _reverse_bits((d >> 14) & 0x7F, 7)
    batt_raw = _reverse_bits((d >> 7) & 0x7F, 7)
    battery_pct = min(100, int(round(batt_raw * 100.0 / 127.0)))
    spare_emerg = bool((d >> 6) & 1)
    valve_ok = bool((d >> 5) & 1)
    armed = bool((d >> 4) & 1)
    turbine_ok = bool((d >> 3) & 1)
    motion = bool((d >> 2) & 1)
    marker_batt_ok = bool((d >> 1) & 1)
    marker_light = bool(d & 1)
    emergency = spare_emerg or (msg_type == 5)

    is_hot = (channel_id == 2) or (channel_id != 1 and msg_type in (4, 5))
    proto = "hot" if is_hot else "eot"
    batt_lbl = BATTERY_COND_LABELS.get(batt_cond_code, "OK")
    hex_str = f"{d:012X}"

    if is_hot:
        cmd_str = "EMERGENCY" if emergency else ("ARM" if armed else "STATUS")
        scroll = f"[HOT #{unit_id:05d}] Cmd:{cmd_str} | Ch:{channel_id}"
        type_name = "HOT_EMERG" if emergency else "HOT_CMD"
    else:
        mov_str = "MOVING" if motion else "STOPPED"
        mkr_str = "ON" if marker_light else "OFF"
        val_str = "OK" if valve_ok else "FLT"
        arm_str = "Arm" if armed else "Disarm"
        scroll = (
            f"[EOT #{unit_id:05d}] {pressure_psi} PSI | {mov_str} | "
            f"Mkr:{mkr_str} | Batt:{batt_lbl}({battery_pct}%) | Valve:{val_str} | {arm_str}"
        )
        type_name = "EOT_EMERG" if emergency else "EOT_STATUS"

    return {
        "protocol": proto,
        "type_name": type_name,
        "chaining": chaining,
        "msg_type": msg_type,
        "unit_id": unit_id,
        "node_id": unit_id,
        "pressure_psi": pressure_psi,
        "motion": motion,
        "marker_light": marker_light,
        "marker_batt_ok": marker_batt_ok,
        "battery_cond": batt_lbl,
        "battery_pct": battery_pct,
        "valve_ok": valve_ok,
        "turbine_ok": turbine_ok,
        "armed": armed,
        "emergency": emergency,
        "hex": hex_str,
        "_node_id_": unit_id,
        "_scroll_ptc_": scroll,
        "_type_": type_name,
        "_sequence_": None,
        "telemetry": {
            "wiu_id": f"{unit_id:05d}",
            "src": f"{proto.upper()}_{unit_id:05d}",
            "pressure_psi": pressure_psi,
            "motion": motion,
            "marker_light": marker_light,
            "battery_cond": batt_lbl,
            "battery_pct": battery_pct,
            "armed": armed,
            "emergency": emergency,
        },
    }


# ---------------------------------------------------------------------------
# Clean-Room Coherent 1200/1800 Hz FFSK-over-NFM RF Modulator (--mock & Tests)
# ---------------------------------------------------------------------------
def generate_fred_ffsk_iq(
    codeword63: int,
    sample_rate: int = 960000,
    center_freq: int = 457937500,
    channel_freq: int = 457937500,
    fm_dev_hz: float = 3000.0,
    amp: float = 48.0,
    noise_std: float = 2.5,
    pad_samples: int = 6000,
    sample_format: str = "u8",
    seed: int = 457,
) -> bytes:
    """
    Synthesizes realistic 450 MHz AAR S-9152 EOT/HOT RF IQ bytes (u8 or s16):
      preamble + 11-bit sync + 63-bit BCH codeword -> coherent 1200/1800 Hz FFSK audio
      -> Narrowband FM RF modulation at `channel_freq`.
    """
    bits: List[int] = [1, 0] * 12  # 24-bit bit-sync preamble
    for i in range(FRED_SYNC_LEN):
        bits.append((FRED_SYNC_WORD >> (FRED_SYNC_LEN - 1 - i)) & 1)
    for i in range(FRED_CODEWORD_BITS):
        bits.append((codeword63 >> (FRED_CODEWORD_BITS - 1 - i)) & 1)
    bits.extend([0, 1])

    sps_rf = float(sample_rate) / float(FRED_BAUD_RATE)
    n_burst = int(round(len(bits) * sps_rf))
    n_total = pad_samples + n_burst + pad_samples

    rng = np.random.default_rng(seed)
    sig = (rng.normal(0.0, noise_std, n_total) + 1j * rng.normal(0.0, noise_std, n_total)).astype(np.complex64)

    # Continuous-phase 1200 Hz (bit=1) / 1800 Hz (bit=0) FFSK audio subcarrier
    bit_idx = np.minimum((np.arange(n_burst) / sps_rf).astype(np.int32), len(bits) - 1)
    bit_arr = np.array(bits, dtype=np.int8)[bit_idx]
    tone_f = np.where(bit_arr == 1, FRED_TONE_LOW_HZ, FRED_TONE_HIGH_HZ)
    audio_phase = 2.0 * math.pi * np.cumsum(tone_f) / float(sample_rate)
    audio = np.sin(audio_phase)

    # NFM modulate audio onto RF channel offset
    f_offset = float(channel_freq - center_freq)
    inst_f = f_offset + audio * float(fm_dev_hz)
    rf_phase = 2.0 * math.pi * np.cumsum(inst_f) / float(sample_rate)
    sig[pad_samples:pad_samples + n_burst] += (amp * np.exp(1j * rf_phase)).astype(np.complex64)

    if sample_format == "s16":
        scale = 32768.0 / 127.5
        out = np.empty(n_total * 2, dtype="<i2")
        out[0::2] = np.clip(np.round(sig.real * scale), -32768, 32767).astype("<i2")
        out[1::2] = np.clip(np.round(sig.imag * scale), -32768, 32767).astype("<i2")
        return out.tobytes()
    out_u8 = np.empty(n_total * 2, dtype=np.uint8)
    out_u8[0::2] = np.clip(np.round(sig.real + 127.5), 0, 255).astype(np.uint8)
    out_u8[1::2] = np.clip(np.round(sig.imag + 127.5), 0, 255).astype(np.uint8)
    return out_u8.tobytes()


def generate_hot_s5701_iq(
    codeword63: int,
    sample_rate: int = 960000,
    center_freq: int = 452946875,
    channel_freq: int = 452937500,
    fm_dev_hz: float = 3000.0,
    amp: float = 48.0,
    noise_std: float = 2.5,
    pad_samples: int = 6000,
    repeat_blocks: int = 3,
    sample_format: str = "u8",
    seed: int = 452,
) -> bytes:
    """
    Synthesizes an authentic AAR S-5701 Front-to-Rear HOT (452.9375 MHz) RF IQ burst:
      bit-sync preamble + 24-bit frame sync (0x9488F1 LSB-first) + 3 repeated 63-bit BCH(63,30)
      blocks (each followed by 1 odd-parity bit) -> coherent 1200/1800 Hz FFSK over NFM.
    """
    bits: List[int] = [1, 0] * 14  # 28-bit bit-sync preamble
    for i in range(HOT_SYNC_LEN):
        bits.append((HOT_SYNC_WORD_LSB >> (HOT_SYNC_LEN - 1 - i)) & 1)
    cw_bits = [(int(codeword63) >> (HOT_CODEWORD_BITS - 1 - i)) & 1 for i in range(HOT_CODEWORD_BITS)]
    odd_par = 1 - (sum(cw_bits) & 1)
    for _ in range(max(1, int(repeat_blocks))):
        bits.extend(cw_bits)
        bits.append(odd_par)
    bits.extend([0, 1])

    sps_rf = float(sample_rate) / float(FRED_BAUD_RATE)
    n_burst = int(round(len(bits) * sps_rf))
    n_total = pad_samples + n_burst + pad_samples

    rng = np.random.default_rng(seed)
    sig = (rng.normal(0.0, noise_std, n_total) + 1j * rng.normal(0.0, noise_std, n_total)).astype(np.complex64)

    bit_idx = np.minimum((np.arange(n_burst) / sps_rf).astype(np.int32), len(bits) - 1)
    bit_arr = np.array(bits, dtype=np.int8)[bit_idx]
    tone_f = np.where(bit_arr == 1, FRED_TONE_LOW_HZ, FRED_TONE_HIGH_HZ)
    audio_phase = 2.0 * math.pi * np.cumsum(tone_f) / float(sample_rate)
    audio = np.sin(audio_phase)

    f_offset = float(channel_freq - center_freq)
    inst_f = f_offset + audio * float(fm_dev_hz)
    rf_phase = 2.0 * math.pi * np.cumsum(inst_f) / float(sample_rate)
    sig[pad_samples:pad_samples + n_burst] += (amp * np.exp(1j * rf_phase)).astype(np.complex64)

    if sample_format == "s16":
        scale = 32768.0 / 127.5
        out = np.empty(n_total * 2, dtype="<i2")
        out[0::2] = np.clip(np.round(sig.real * scale), -32768, 32767).astype("<i2")
        out[1::2] = np.clip(np.round(sig.imag * scale), -32768, 32767).astype("<i2")
        return out.tobytes()
    out_u8 = np.empty(n_total * 2, dtype=np.uint8)
    out_u8[0::2] = np.clip(np.round(sig.real + 127.5), 0, 255).astype(np.uint8)
    out_u8[1::2] = np.clip(np.round(sig.imag + 127.5), 0, 255).astype(np.uint8)
    return out_u8.tobytes()


# ---------------------------------------------------------------------------
# Clean-Room EOT/HOT 1200 Baud Coherent FFSK/MSK Demodulator
# ---------------------------------------------------------------------------
class FredChannelDemodulator:
    """
    Stateful per-channel 450 MHz EOT/HOT demodulator:
      1. Mixes wideband IQ to baseband and decimates to 48,000 S/s (40 audio samples/bit).
      2. Quadrature NFM discriminator -> 48 kHz audio baseband.
      3. Sliding quadrature matched-filter energy detector at 1200 Hz vs 1800 Hz.
      4. Multi-phase clock recovery + 11-bit sync correlation + BCH(63, 45) t=2 FEC.
    """

    def __init__(self, input_rate: int, channel_id: int, freq_hz: int, max_bch_errors: int = 1):
        self.input_rate = int(input_rate)
        self.channel_id = int(channel_id)
        self.freq_hz = int(freq_hz)
        self.max_bch_errors = int(max_bch_errors)

        self.dec_factor = max(1, int(round(self.input_rate / float(FRED_DEMOD_RATE))))
        self.actual_demod_rate = float(self.input_rate) / float(self.dec_factor)
        self.sps = max(8, int(round(self.actual_demod_rate / float(FRED_BAUD_RATE))))

        # Two-stage sharp 12.5 kHz NFM channel filter:
        #   Stage 1: Anti-alias decimator (input_rate -> 48 kS/s)
        #   Stage 2: Sharp 48 kS/s channel filter (4.8 kHz cutoff, 65 taps -> >55 dB rejection at 8.5 kHz, >75 dB at 12.5 kHz)
        cutoff = min(0.45, (FRED_CHANNEL_BW_HZ * 0.5) / float(self.input_rate))
        taps = min(255, max(129, 10 * self.dec_factor + 1))
        self.lpf = grabber.WindowedSincFilter(self.dec_factor, cutoff, taps)
        self.ch_lpf = grabber.WindowedSincFilter(1, min(0.45, 4800.0 / float(self.actual_demod_rate)), 65)
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.audio_tail = np.zeros(0, dtype=np.float32)
        self._recent_hashes: collections.deque = collections.deque(maxlen=64)
        self._noise_floor_db: Optional[float] = None
        self._prev_chunk_active: bool = False

        # Precompute 1-symbol quadrature matched-filter templates for 1200 Hz & 1800 Hz
        t_sym = np.arange(self.sps, dtype=np.float32) / float(self.actual_demod_rate)
        self._t1200 = np.exp(-2j * math.pi * FRED_TONE_LOW_HZ * t_sym).astype(np.complex64)[::-1]
        self._t1800 = np.exp(-2j * math.pi * FRED_TONE_HIGH_HZ * t_sym).astype(np.complex64)[::-1]

    def reset(self):
        self.lpf.reset()
        self.ch_lpf.reset()
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.audio_tail = np.zeros(0, dtype=np.float32)
        self._noise_floor_db = None
        self._prev_chunk_active = False

    def process_iq(
        self,
        iq_complex: np.ndarray,
        center_freq: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        n = len(iq_complex)
        if n < self.dec_factor * 4:
            return [], -100.0

        # 1. DDC + Two-stage sharp 12.5 kHz NFM channel filter -> 48 kS/s
        df = float(self.freq_hz - center_freq)
        dt = 1.0 / float(self.input_rate)
        idx = np.arange(n, dtype=np.float64)
        lo = np.exp(-2j * math.pi * (self.phase_acc + df * idx * dt)).astype(np.complex64)
        self.phase_acc = float((self.phase_acc + df * n * dt) % 1.0)

        bb = self.ch_lpf.process(self.lpf.process(iq_complex * lo))
        if len(bb) < self.sps * 8:
            return [], -100.0

        ch_rssi = grabber.compute_signal_power_dbfs(bb)

        # Persistent running noise-floor carrier squelch (handles weak distant EOTs AND 150-250ms bursts filling chunks)
        self._noise_floor_db, peak_blk_db = _update_channel_noise_floor(self._noise_floor_db, bb, self.actual_demod_rate)
        burst_wins = _detect_rf_burst_windows(
            bb,
            self.actual_demod_rate,
            snr_thresh_db=4.5,
            min_dur_ms=12.0,
            noise_floor_override_db=self._noise_floor_db,
        )
        cur_active = bool(burst_wins) or (peak_blk_db >= self._noise_floor_db + 4.5) or (float(ch_rssi) > -38.0)
        has_rf_energy = cur_active or self._prev_chunk_active
        self._prev_chunk_active = cur_active

        # 2. Quadrature NFM discriminator -> audio
        ext = np.empty(len(bb) + 1, dtype=np.complex64)
        ext[0] = self.prev_sample
        ext[1:] = bb
        self.prev_sample = bb[-1]
        audio = np.angle(ext[1:] * np.conj(ext[:-1])).astype(np.float32)

        full_audio = np.concatenate([self.audio_tail, audio]) if len(self.audio_tail) > 0 else audio
        tail_bits = (HOT_FRAME_MIN_BITS + 32) if self.channel_id == 2 else (FRED_FRAME_BITS + 16)
        keep_tail = self.sps * tail_bits
        self.audio_tail = full_audio[-keep_tail:].copy() if len(full_audio) > keep_tail else full_audio.copy()

        if (not has_rf_energy) or len(full_audio) < self.sps * (FRED_FRAME_BITS + 2):
            self.last_sync_count = 0
            self.last_fec_good = 0
            self.last_fec_bad = 0
            self.last_bursts = 0
            return [], ch_rssi

        # Exact 2-symbol moving-average DC blocker:
        # 2*sps spans EXACTLY 2 cycles of 1200 Hz AND 3 cycles of 1800 Hz -> 0 dB tone loss,
        # complete immunity to +/-4 kHz carrier frequency offset & burst key-up DC steps!
        dc_win = 2 * self.sps
        if len(full_audio) > dc_win:
            csum = np.empty(len(full_audio) + 1, dtype=np.float64)
            csum[0] = 0.0
            np.cumsum(full_audio, out=csum[1:])
            half_w = dc_win // 2
            idx_a = np.arange(len(full_audio))
            lo_i = np.clip(idx_a - half_w, 0, len(full_audio) - dc_win)
            hi_i = lo_i + dc_win
            local_dc = ((csum[hi_i] - csum[lo_i]) / float(dc_win)).astype(np.float32)
            audio_ac = full_audio - local_dc
        else:
            audio_ac = full_audio - float(np.mean(full_audio))

        # 3. Sliding coherent quadrature matched-filter energy for 1200 Hz vs 1800 Hz
        c1200 = np.convolve(audio_ac, self._t1200, mode="valid")
        c1800 = np.convolve(audio_ac, self._t1800, mode="valid")
        soft_metric = (c1200.real * c1200.real + c1200.imag * c1200.imag) - (
            c1800.real * c1800.real + c1800.imag * c1800.imag
        )

        # 4. Sweep symbol phases:
        #    - Ch 2 (HOT 452.9375 MHz): Authentic AAR S-5701 24-bit Frame Sync (0x9488F1) + 3-block BCH(63,30) decoder
        #      (plus 11-bit BCH(63,45) fallback for legacy/synthetic HOT frames).
        #    - Ch 1 (EOT 457.9375 MHz): Unchanged 11-bit Barker sync (0x712) + BCH(63,45) decoder.
        packets: List[Dict[str, Any]] = []
        sync_hits_chunk = 0
        seen_pos: Set[int] = set()
        phase_step = max(1, self.sps // 16)
        best_snr = max((w[3] for w in burst_wins), default=max(0.0, float(peak_blk_db - self._noise_floor_db)))
        hot_bch_max_err = min(2, max(1, self.max_bch_errors + 1))
        for ph in range(0, self.sps, phase_step):
            syms = soft_metric[ph::self.sps]
            if len(syms) < FRED_FRAME_BITS:
                continue
            hard_pos = (syms >= 0.0).astype(np.int8)
            bipolar = (hard_pos * 2 - 1).astype(np.float32)

            # --- Ch 2 ONLY: AAR S-5701 Front-to-Rear HOT (24-bit sync + 3 repeated BCH(63,30) blocks) ---
            if self.channel_id == 2 and len(syms) >= HOT_FRAME_MIN_BITS:
                for hot_sync_vec in (HOT_SYNC_LSB_BIPOLAR, HOT_SYNC_SPEC_BIPOLAR):
                    h_corr = np.correlate(bipolar, hot_sync_vec, mode="valid")
                    h_hits = np.where(np.abs(h_corr) >= (HOT_SYNC_LEN - 2))[0]
                    for h_idx in h_hits:
                        h_i = int(h_idx)
                        if h_i + HOT_FRAME_MIN_BITS > len(hard_pos):
                            continue
                        pos_bucket = h_i // 4
                        if pos_bucket not in seen_pos:
                            seen_pos.add(pos_bucket)
                            sync_hits_chunk += 1
                        inverted = h_corr[h_i] < 0.0
                        # Check up to all 3 repeated 63-bit blocks (each separated by 1 odd-parity bit = 64-bit stride)
                        for blk_idx in range(3):
                            b_start = h_i + HOT_SYNC_LEN + blk_idx * HOT_BLOCK_STRIDE_BITS
                            b_end = b_start + HOT_CODEWORD_BITS
                            if b_end > len(hard_pos):
                                break
                            cw_bits = (1 - hard_pos[b_start:b_end]) if inverted else hard_pos[b_start:b_end]
                            cw_val = 0
                            for b in cw_bits:
                                cw_val = (cw_val << 1) | int(b)
                            data30, n_err = hot_bch30_decode(cw_val, max_errors=hot_bch_max_err)
                            if data30 is None:
                                continue
                            parsed = parse_hot_data30(data30, channel_id=self.channel_id)
                            # AAR S-5701 front-to-rear: msg_type == 000 & 5-digit unit ID 00001..99999
                            if parsed.get("msg_type", 0) != 0 or not (1 <= int(parsed.get("unit_id", 0)) <= 99999):
                                continue
                            sig_key = ("HOT30", parsed["hex"], h_i // 8)
                            if sig_key in self._recent_hashes:
                                break
                            self._recent_hashes.append(sig_key)

                            s0 = ph + h_i * self.sps
                            s1 = min(len(full_audio), s0 + HOT_FRAME_MIN_BITS * self.sps)
                            est_df_hz = float(np.mean(full_audio[s0:s1]) * self.actual_demod_rate / (2.0 * math.pi)) if s1 > s0 else 0.0
                            est_df_hz = max(-5500.0, min(5500.0, est_df_hz))

                            parsed.update({
                                "channel": self.channel_id,
                                "freq_hz": self.freq_hz,
                                "freq_offset_hz": round(est_df_hz, 1),
                                "rssi": round(float(ch_rssi), 2),
                                "rssi_dbfs": round(float(ch_rssi), 2),
                                "snr_db": round(float(best_snr), 1),
                                "bch_errors": n_err,
                                "hot_block": blk_idx + 1,
                                "crc_ok": True,
                                "rate": "1200",
                                "timestamp": datetime.now().isoformat(),
                            })
                            packets.append(parsed)
                            break  # Decoded one of the 3 repeated blocks for this sync hit

            corr = np.correlate(bipolar, FRED_SYNC_BIPOLAR, mode="valid")
            # Require EXACT 11-bit Barker frame sync (0 bit errors -> 12x lower noise false-positive rate)
            hit_indices = np.where(np.abs(corr) >= FRED_SYNC_LEN)[0]
            for h_idx in hit_indices:
                h_i = int(h_idx)
                if h_i + FRED_FRAME_BITS > len(hard_pos):
                    continue
                # Verify alternating bit-sync preamble (101010...) immediately preceding frame sync
                if h_i >= 10:
                    pre = hard_pos[h_i - 10:h_i]
                    if int(np.count_nonzero(pre[1:] != pre[:-1])) < 5:
                        continue
                pos_bucket = h_i // 4
                if pos_bucket not in seen_pos:
                    seen_pos.add(pos_bucket)
                    sync_hits_chunk += 1
                inverted = corr[h_i] < 0.0
                cw_bits = (
                    1 - hard_pos[h_i + FRED_SYNC_LEN:h_i + FRED_FRAME_BITS]
                    if inverted
                    else hard_pos[h_i + FRED_SYNC_LEN:h_i + FRED_FRAME_BITS]
                )
                cw_val = 0
                for b in cw_bits:
                    cw_val = (cw_val << 1) | int(b)
                data45, n_err = fred_bch_decode(cw_val, max_errors=self.max_bch_errors)
                if data45 is None:
                    continue

                parsed = parse_fred_data45(data45, channel_id=self.channel_id)
                # AAR S-5701 / S-9152 EOT unit IDs are strictly 5 decimal digits (00001..99999)
                if not (1 <= int(parsed.get("unit_id", 0)) <= 99999):
                    continue
                sig_key = (parsed["hex"], pos_bucket)
                if sig_key in self._recent_hashes:
                    continue
                self._recent_hashes.append(sig_key)

                s0 = ph + h_i * self.sps
                s1 = min(len(full_audio), s0 + FRED_FRAME_BITS * self.sps)
                est_df_hz = float(np.mean(full_audio[s0:s1]) * self.actual_demod_rate / (2.0 * math.pi)) if s1 > s0 else 0.0

                parsed.update({
                    "channel": self.channel_id,
                    "freq_hz": self.freq_hz,
                    "freq_offset_hz": round(est_df_hz, 1),
                    "rssi": round(float(ch_rssi), 2),
                    "rssi_dbfs": round(float(ch_rssi), 2),
                    "snr_db": round(float(best_snr), 1),
                    "bch_errors": n_err,
                    "crc_ok": True,
                    "rate": "1200",
                    "timestamp": datetime.now().isoformat(),
                })
                packets.append(parsed)

        self.last_sync_count = max(sync_hits_chunk, len(packets))
        self.last_fec_good = len(packets)
        self.last_fec_bad = max(0, self.last_sync_count - self.last_fec_good)
        self.last_bursts = self.last_sync_count if self.last_sync_count > 0 else (1 if ch_rssi > -45.0 else 0)
        return packets, ch_rssi


# ---------------------------------------------------------------------------
# Clean-Room HDLC + CRC-16-CCITT Framing (DPU Locotrol & RLC Telemetry)
# ---------------------------------------------------------------------------
def hdlc_crc16_ccitt(data: bytes) -> int:
    """Computes standard HDLC FCS (CRC-16-CCITT / X.25, poly 0x8408 reflected, init 0xFFFF, xorout 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return (crc ^ 0xFFFF) & 0xFFFF


def hdlc_encode_frame(payload: bytes) -> List[int]:
    """Encodes bytes into HDLC bitstream with 0x7E flags, LSB-first bits, CRC-16 FCS, and zero-bit stuffing."""
    fcs = hdlc_crc16_ccitt(payload)
    body = bytes(payload) + bytes([fcs & 0xFF, (fcs >> 8) & 0xFF])
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits: List[int] = list(flag) * 2
    ones = 0
    for b in body:
        for i in range(8):
            bit = (b >> i) & 1
            bits.append(bit)
            if bit:
                ones += 1
                if ones == 5:
                    bits.append(0)
                    ones = 0
            else:
                ones = 0
    bits.extend(flag)
    return bits


def hdlc_extract_frames(bits: np.ndarray, min_len: int = 4, max_len: int = 96) -> Tuple[List[bytes], int]:
    """
    Extracts valid HDLC CRC-16-CCITT frames strictly between 0x7E flags (zero flag leakage).
    Returns (valid_payloads, total_flag_delimited_candidates).
    """
    b_arr = np.asarray(bits, dtype=np.int8) & 1
    n = len(b_arr)
    if n < 24:
        return [], 0

    # Locate all 0x7E (01111110) flag start indices
    flag_starts: List[int] = []
    shift = 0
    for i in range(n):
        shift = ((shift << 1) | int(b_arr[i])) & 0xFF
        if i >= 7 and shift == 0x7E:
            flag_starts.append(i - 7)

    if len(flag_starts) < 2:
        return [], 0

    valid: List[bytes] = []
    candidates = 0
    for idx in range(len(flag_starts) - 1):
        seg_start = flag_starts[idx] + 8
        seg_end = flag_starts[idx + 1]
        if seg_end - seg_start < (min_len + 2) * 8:
            continue
        if seg_end - seg_start > (max_len + 8) * 10:
            continue

        # Zero-bit unstuffing
        unstuffed: List[int] = []
        ones = 0
        aborted = False
        for i in range(seg_start, seg_end):
            b = int(b_arr[i])
            if ones == 5:
                if b == 0:
                    ones = 0
                    continue
                aborted = True
                break
            unstuffed.append(b)
            ones = (ones + 1) if b == 1 else 0

        if aborted or len(unstuffed) < (min_len + 2) * 8 or (len(unstuffed) % 8) != 0:
            continue

        raw_bytes = bytearray()
        for j in range(0, len(unstuffed), 8):
            val = 0
            for k in range(8):
                val |= (unstuffed[j + k] << k)
            raw_bytes.append(val)

        if len(raw_bytes) < (min_len + 2) or len(raw_bytes) > (max_len + 2):
            continue
        candidates += 1
        body = bytes(raw_bytes[:-2])
        rx_fcs = raw_bytes[-2] | (raw_bytes[-1] << 8)
        if hdlc_crc16_ccitt(body) == rx_fcs:
            valid.append(body)

    return valid, candidates


def encode_dsu_power(power_val: Any) -> int:
    """Encodes a DSU Power/Throttle/Dynamic-Brake value into a compact byte."""
    if isinstance(power_val, int):
        return max(0, min(255, power_val))
    s = str(power_val or "IDLE").strip().upper()
    if s in ("IDLE", "0", "N0"):
        return 0x00
    if s == "IDLMD":
        return 0x09
    if s == "SETOT":
        return 0x0A
    if s == "BSTUP":
        return 0x0B
    if s.startswith("N") and s[1:].isdigit():
        n = int(s[1:])
        return max(1, min(8, n))
    if s.startswith("B") or s.startswith("DB"):
        num_s = s[2:] if s.startswith("DB") else s[1:]
        try:
            db = max(0.0, min(8.0, float(num_s)))
            # Use 0.25-step table 0x10..0x30 when exact quarter-step, else 0.10-step 0x80..0xD0
            q = round(db * 4.0)
            if abs(db - q * 0.25) < 1e-4:
                return 0x10 + int(q)
            return 0x80 + min(80, int(round(db * 10.0)))
        except ValueError:
            return 0x0B
    return 0x00


def decode_dsu_power(code: Any) -> str:
    """Decodes a DSU Power byte or string into canonical IDLE/IDLMD/SETOT/N1..N8/BSTUP/B0.00..B8.00."""
    if isinstance(code, str):
        s = code.strip().upper()
        if s in ("IDLE", "IDLMD", "SETOT", "BSTUP"):
            return s
        if s.startswith("N") and s[1:].isdigit() and 1 <= int(s[1:]) <= 8:
            return f"N{int(s[1:])}"
        if s.startswith("B") or s.startswith("DB"):
            num_s = s[2:] if s.startswith("DB") else s[1:]
            try:
                db = max(0.0, min(8.0, float(num_s)))
                return f"B{db:.2f}"
            except ValueError:
                return s
        return s or "IDLE"
    try:
        v = int(code) & 0xFF
    except (ValueError, TypeError):
        return "IDLE"
    if v == 0x00:
        return "IDLE"
    if 1 <= v <= 8:
        return f"N{v}"
    if v == 0x09:
        return "IDLMD"
    if v == 0x0A:
        return "SETOT"
    if v == 0x0B:
        return "BSTUP"
    if 0x10 <= v <= 0x30:
        db = (v - 0x10) * 0.25
        return f"B{db:.2f}"
    if 0x80 <= v <= 0xD0:
        db = min(8.0, (v - 0x80) * 0.10)
        return f"B{db:.2f}"
    return f"N{v & 0x07}" if (v & 0x07) else "IDLE"


def _encode_dsu_bpred(val: Any) -> int:
    if val is None:
        return 0x00
    s = str(val).strip().upper()
    if s in ("REL", "RELEASE", "0"):
        return 0x00
    if s in ("EMER", "EMERGENCY"):
        return 0xFF
    try:
        return max(1, min(254, int(round(float(s)))))
    except ValueError:
        return 0x00


def _decode_dsu_bpred(val: Any) -> Any:
    if isinstance(val, str):
        s = val.strip().upper()
        if s in ("REL", "RELEASE"):
            return "REL"
        if s in ("EMER", "EMERGENCY"):
            return "EMER"
        try:
            f = float(s)
            return int(f) if f.is_integer() else round(f, 1)
        except ValueError:
            return s or "REL"
    try:
        v = int(val) & 0xFF
    except (ValueError, TypeError):
        return "REL"
    if v == 0x00:
        return "REL"
    if v == 0xFF:
        return "EMER"
    return v


def _encode_dsu_ib(val: Any) -> int:
    if val is None:
        return 0
    s = str(val).strip().upper()
    if s in ("B", "BAIL", "BAILOFF", "BAIL-OFF"):
        return 0xFD
    if s == "FULL":
        return 0xFE
    if s in ("EMER", "EMERGENCY"):
        return 0xFF
    try:
        return max(0, min(252, int(round(float(s)))))
    except ValueError:
        return 0


def _decode_dsu_ib(val: Any) -> Any:
    if isinstance(val, str):
        s = val.strip().upper()
        if s in ("B", "BAIL", "BAILOFF", "BAIL-OFF"):
            return "B"
        if s == "FULL":
            return "FULL"
        if s in ("EMER", "EMERGENCY"):
            return "EMER"
        try:
            f = float(s)
            return int(f) if f.is_integer() else round(f, 1)
        except ValueError:
            return s or 0
    try:
        v = int(val) & 0xFF
    except (ValueError, TypeError):
        return 0
    if v == 0xFD:
        return "B"
    if v == 0xFE:
        return "FULL"
    if v == 0xFF:
        return "EMER"
    return v


def pack_dsu_payload(
    addr: int,
    trid: int = 1,
    tp: str = "CM",
    origin: Optional[str] = None,
    rp: str = "DI",
    seq: int = 0,
    nrm: int = 1,
    power: Any = "IDLE",
    rev: str = "CT",
    mtr: int = 0,
    bvin: int = 0,
    sand: int = 0,
    prk: int = 0,
    bpred: Any = "REL",
    ib: Any = 0,
    bp: int = 90,
    er: int = 90,
    mr: int = 138,
    bc: int = 0,
    af: int = 0,
    tre: int = 0,
    alarms: Optional[List[str]] = None,
) -> bytes:
    """
    Packs a full GE Locotrol / DSU binary payload (10 bytes for CM/LK/LR, 14 bytes for ST)
    containing all header/routing, power/reverser/control bits, CM brake commands, and ST telemetry/alarms.
    """
    tp_u = str(tp or "CM").strip().upper()
    tp_code = {"CM": 0, "ST": 1, "LK": 2, "LR": 3}.get(tp_u, 0)
    if origin is None:
        or_u = "RM" if tp_u in ("ST", "LR") else "LD"
    else:
        or_u = str(origin).strip().upper()
    or_code = 1 if or_u == "RM" else 0
    rp_code = {"DI": 0, "RP": 1, "RO": 2}.get(str(rp or "DI").strip().upper(), 0)
    nrm_c = max(0, min(5, int(nrm)))
    seq_c = int(seq) & 0x1F
    rev_code = {"CT": 0, "FO": 1, "RE": 2}.get(str(rev or "CT").strip().upper(), 0)

    b0 = (int(addr) >> 8) & 0xFF
    b1 = int(addr) & 0xFF
    b2 = int(trid) & 0xFF
    b3 = ((tp_code & 0x03) << 6) | ((or_code & 0x01) << 5) | ((rp_code & 0x03) << 3) | (nrm_c & 0x07)
    b4 = ((seq_c & 0x1F) << 3) | ((rev_code & 0x03) << 1)
    b5 = encode_dsu_power(power) & 0xFF
    b6 = (
        (1 if int(mtr) else 0)
        | ((1 if int(bvin) else 0) << 1)
        | ((1 if int(sand) else 0) << 2)
        | ((1 if int(prk) else 0) << 3)
    )

    if tp_u == "ST":
        al_mask = 0
        al_set = {str(a).strip().upper() for a in (alarms or []) if str(a).strip()}
        for bit_m, name in DSU_ALARM_BITS:
            if name in al_set:
                al_mask |= bit_m
        tre_b = max(-128, min(127, int(round(float(tre))))) & 0xFF
        return bytes([
            b0, b1, b2, b3, b4, b5, b6,
            max(0, min(255, int(bp))),
            max(0, min(255, int(er))),
            max(0, min(255, int(mr))),
            max(0, min(255, int(bc))),
            max(0, min(255, int(af))),
            tre_b,
            al_mask,
        ])

    return bytes([
        b0, b1, b2, b3, b4, b5, b6,
        _encode_dsu_bpred(bpred),
        _encode_dsu_ib(ib),
        0x00,
    ])


DSU_DEFAULT_LABELS: Dict[str, Any] = {
    "packet_types": {"CM": "CMD", "ST": "STAT", "LK": "LINK", "LR": "LINK-ACK"},
    "repeat_codes": {"DI": "Direct", "RP": "Repeat", "RO": "Offboard"},
    "reversers": {"CT": "Cntr", "FO": "Fwd", "RE": "Rev"},
    "power_modes": {
        "IDLE": "Idle",
        "IDLMD": "IdleMode",
        "SETOT": "Set-Out",
        "BSTUP": "DynSetup",
        "notch_prefix": "Notch ",
        "dyn_prefix": "DynBrk ",
    },
    "brake_states": {"REL": "Rel", "EMER": "EMER", "B": "Bail", "FULL": "FULL"},
    "flags": {"MTR": "Motoring", "BVIN": "Valve In", "Sand": "Sand", "PRK": "ParkBrk"},
    "alarms": {"EMR": "EMR", "PEN": "PEN", "PCS": "PCS", "BPRS": "BPRS", "BPDR": "BPDR"},
}

DSU_DEFAULT_SHOW_FIELDS: Dict[str, bool] = {
    "show_trid": True,
    "show_repeat": True,
    "show_nrm": True,
    "show_seq": True,
    "show_flags": True,
    "show_inactive_flags": False,
    "show_brakes": True,
    "show_pressures": True,
    "show_effort": True,
    "show_alarms": True,
    "show_hex": False,
}


def _humanize_dsu_power(power: str, power_labels: Dict[str, str]) -> str:
    s = str(power or "IDLE").strip().upper()
    if s in power_labels:
        return str(power_labels[s])
    if s.startswith("N") and s[1:].isdigit():
        return f"{power_labels.get('notch_prefix', 'Notch ')}{int(s[1:])}"
    if s.startswith("B") and len(s) > 1:
        return f"{power_labels.get('dyn_prefix', 'DynBrk ')}{s[1:]}"
    return s


def _format_dsu_scroll_line(pkt: Dict[str, Any], rules_cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    Formats a human-readable, concise single-line _scroll_ptc_ summary for any DSU/DPU packet.
    Optionally customized via config/dpu_rules.json (labels, show_fields, units, scroll_formats).
    """
    cfg = rules_cfg if isinstance(rules_cfg, dict) else {}
    raw_labels = cfg.get("labels") if isinstance(cfg.get("labels"), dict) else {}
    show_cfg = dict(DSU_DEFAULT_SHOW_FIELDS)
    if isinstance(cfg.get("show_fields"), dict):
        for k, v in cfg["show_fields"].items():
            show_cfg[str(k)] = bool(v)

    def _lbl_map(key: str) -> Dict[str, str]:
        base = dict(DSU_DEFAULT_LABELS[key])
        if isinstance(raw_labels.get(key), dict):
            for k, v in raw_labels[key].items():
                base[str(k)] = str(v)
        return base

    tp_map = _lbl_map("packet_types")
    rp_map = _lbl_map("repeat_codes")
    rev_map = _lbl_map("reversers")
    pwr_map = _lbl_map("power_modes")
    brk_map = _lbl_map("brake_states")
    flg_map = _lbl_map("flags")
    alm_map = _lbl_map("alarms")

    ch_num = pkt.get("dpu_channel", pkt.get("channel", 1))
    tp = str(pkt.get("TP") or "CM").upper()
    tp_lbl = tp_map.get(tp, tp)
    origin = str(pkt.get("OR") or ("RM" if pkt.get("role") == "REMOTE" else "LD")).upper()
    role = "REMOTE" if origin == "RM" else "LEAD"
    rp = str(pkt.get("RP") or "DI").upper()
    rp_lbl = rp_map.get(rp, rp)
    addr = int(pkt.get("ADDR", pkt.get("loco_id", 0)) or 0)
    trid = int(pkt.get("TRID", 0) or 0)
    seq = int(pkt.get("SEQ", 0) or 0)
    nrm = int(pkt.get("NRM", 0) or 0)
    raw_hex = str(pkt.get("hex") or "")

    units_map = cfg.get("units") if isinstance(cfg.get("units"), dict) else {}
    unit_cfg = units_map.get(str(addr)) or units_map.get(f"0x{addr:04X}") or {}
    unit_name = str(unit_cfg.get("name") or "") if isinstance(unit_cfg, dict) else ""
    unit_label = f"#{addr}({unit_name})" if unit_name else f"#{addr}"

    pwr = str(pkt.get("Power") or pkt.get("throttle") or "IDLE")
    pwr_lbl = _humanize_dsu_power(pwr, pwr_map)
    rev = str(pkt.get("REV") or "CT")
    rev_lbl = rev_map.get(rev, rev)

    mtr = int(pkt.get("MTR", 0) or 0)
    bvin = int(pkt.get("BVIN", 0) or 0)
    sand = int(pkt.get("Sand", 0) or 0)
    prk = int(pkt.get("PRK", 0) or 0)
    active_flags: List[str] = []
    if mtr:
        active_flags.append(flg_map.get("MTR", "Motoring"))
    if bvin:
        active_flags.append(flg_map.get("BVIN", "Valve In"))
    elif show_cfg.get("show_inactive_flags"):
        active_flags.append("Valve Out")
    if sand:
        active_flags.append(flg_map.get("Sand", "Sand"))
    if prk:
        active_flags.append(flg_map.get("PRK", "ParkBrk"))
    flags_str = f"[{', '.join(active_flags)}]" if (show_cfg.get("show_flags", True) and active_flags) else ""

    bpred = pkt.get("BPRED", "REL")
    ib = pkt.get("IB", 0)
    bpred_lbl = f"-{bpred}psi" if isinstance(bpred, (int, float)) else brk_map.get(str(bpred).upper(), str(bpred))
    ib_lbl = f"{ib}psi" if isinstance(ib, (int, float)) else brk_map.get(str(ib).upper(), str(ib))

    bp = pkt.get("BP", pkt.get("pressure_psi", 0))
    er = pkt.get("ER", 0)
    mr = pkt.get("MR", 0)
    bc = pkt.get("BC", 0)
    af = pkt.get("AF", 0)
    tre = pkt.get("TRE", 0)
    tre_s = f"{int(tre):+d}" if isinstance(tre, (int, float)) else str(tre)
    raw_alarms = pkt.get("AL") or []
    mapped_alarms = [alm_map.get(str(a).upper(), str(a).upper()) for a in raw_alarms]
    alarms_str = ",".join(mapped_alarms)
    alarm_section = f" | ALARM:{alarms_str}" if (show_cfg.get("show_alarms", True) and alarms_str) else ""

    link_action = f"UNLINK (0 rem)" if (tp == "LK" and nrm == 0) else (f"LINK ({nrm} rem)" if tp == "LK" else "Handshake OK")

    # Optional user-supplied custom template in config/dpu_rules.json -> scroll_formats[TP]
    scroll_fmts = cfg.get("scroll_formats") if isinstance(cfg.get("scroll_formats"), dict) else {}
    custom_tpl = scroll_fmts.get(tp) or scroll_fmts.get("*")
    if isinstance(custom_tpl, str) and custom_tpl.strip():
        try:
            rendered = custom_tpl.format(
                ch=ch_num, role=role, tp=tp, tp_label=tp_lbl, origin=origin,
                rp=rp, rp_label=rp_lbl, addr=addr, unit_name=unit_name, unit_label=unit_label,
                trid=trid, seq=seq, nrm=nrm, power=pwr, power_label=pwr_lbl,
                rev=rev, rev_label=rev_lbl, flags=flags_str,
                mtr=mtr, bvin=bvin, sand=sand, prk=prk,
                bpred=bpred, bpred_label=bpred_lbl, ib=ib, ib_label=ib_lbl,
                bp=bp, er=er, mr=mr, bc=bc, af=af, tre=tre_s,
                alarms=alarms_str or "NONE", alarm_section=alarm_section,
                link_action=link_action, hex=raw_hex,
            )
            return " ".join(rendered.split())
        except Exception:
            pass

    # Built-in human-readable layout honoring show_fields toggles
    meta_bits: List[str] = []
    if show_cfg.get("show_trid", True):
        meta_bits.append(f"Tr:{trid}")
    if show_cfg.get("show_repeat", True):
        meta_bits.append(rp_lbl)
    if show_cfg.get("show_nrm", True) and tp != "LK":
        meta_bits.append(f"{nrm} rem")
    meta_grp = f" ({', '.join(meta_bits)})" if meta_bits else ""
    hdr = f"[DPU Ch{ch_num} {role}] {tp_lbl} {unit_label}{meta_grp}"
    seq_tail = f" | Seq:{seq}" if show_cfg.get("show_seq", True) else ""
    hex_tail = f" | {raw_hex}" if (show_cfg.get("show_hex", False) and raw_hex) else ""

    if tp == "LK":
        return f"{hdr} | {link_action}{seq_tail}{hex_tail}"
    if tp == "LR":
        return f"{hdr} | Handshake OK{seq_tail}{hex_tail}"

    ctrl_core = f"{pwr_lbl} {rev_lbl}" + (f" {flags_str}" if flags_str else "")
    if tp == "CM":
        brk_part = f" | BP:{bpred_lbl} Ind:{ib_lbl}" if show_cfg.get("show_brakes", True) else ""
        return f"{hdr} | {ctrl_core}{brk_part}{seq_tail}{hex_tail}"

    # ST (Remote Status)
    telem_parts: List[str] = []
    if show_cfg.get("show_pressures", True):
        telem_parts.append(f"BP:{bp} ER:{er} MR:{mr} BC:{bc} psi")
    if show_cfg.get("show_effort", True):
        telem_parts.append(f"Flow:{af} Eff:{tre_s}")
    telem_str = f" | {' '.join(telem_parts)}" if telem_parts else ""
    return f"{hdr} | {ctrl_core}{telem_str}{alarm_section}{seq_tail}{hex_tail}"


class DsuPacketDecoder:
    """
    Full DSU / GE Locotrol Distributed Power packet decoder for ApexBOSEngine,
    ApexBOSFred, and ApexBOSSpy. Decodes all header/routing fields (ADDR, TRID,
    TP, OR, RP, SEQ, NRM), Power/REV/boolean control bits (MTR, BVIN, Sand, PRK),
    CM brake commands (BPRED, IB), and ST telemetry/alarms (BP, ER, MR, BC, AF, TRE, AL).
    Optionally loads `config/dpu_rules.json` (hot-reloading on edits) so users can
    customize displayed fields, friendly unit names, labels, and scroll format strings,
    falling back seamlessly to built-in decoding when `dpu_rules.json` is absent.
    Backwards-compatible with legacy 8-byte pack_dpu_payload() frames.
    """

    def __init__(self, config_dir: Optional[str] = None, rules_path: Optional[str] = None):
        self.base_dir = PROJECT_ROOT
        self.config_dir = os.path.abspath(config_dir) if config_dir else os.path.join(self.base_dir, "config")
        self.explicit_rules_path = os.path.abspath(rules_path) if rules_path else None
        self._lock = threading.Lock()
        self._loaded_path: Optional[str] = None
        self._loaded_mtime: Optional[float] = None
        self._rules_obj: Any = None
        self._packet_parser: Any = None
        self._last_check_ts: float = 0.0
        self.reload(force=True)

    def _candidate_paths(self) -> List[str]:
        cands: List[str] = []
        if self.explicit_rules_path:
            cands.append(self.explicit_rules_path)
        if self.config_dir:
            cands.append(os.path.join(self.config_dir, "dpu_rules.json"))
        cands.append(os.path.join(self.base_dir, "config", "dpu_rules.json"))
        cands.append(os.path.join(os.getcwd(), "config", "dpu_rules.json"))
        seen: Set[str] = set()
        out: List[str] = []
        for p in cands:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                out.append(ap)
        return out

    def reload(self, force: bool = False) -> bool:
        """Checks for `dpu_rules.json` creation/modification/removal and reloads if changed."""
        now = time.monotonic()
        if not force and (now - self._last_check_ts) < 1.0:
            return False
        self._last_check_ts = now

        found_path: Optional[str] = None
        found_mtime: Optional[float] = None
        for p in self._candidate_paths():
            if os.path.isfile(p):
                try:
                    found_mtime = os.path.getmtime(p)
                    found_path = p
                    break
                except OSError:
                    pass

        with self._lock:
            if not force and found_path == self._loaded_path and found_mtime == self._loaded_mtime:
                return False

            self._loaded_path = found_path
            self._loaded_mtime = found_mtime
            self._rules_obj = None
            self._packet_parser = None

            if not found_path:
                return True

            try:
                with open(found_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._rules_obj = data
                if isinstance(data, list):
                    try:
                        from parse_packets import PacketParser
                        self._packet_parser = PacketParser(data)
                    except Exception:
                        self._packet_parser = None
                return True
            except Exception:
                self._rules_obj = None
                self._packet_parser = None
                return False

    @property
    def has_custom_rules(self) -> bool:
        with self._lock:
            return self._rules_obj is not None

    @property
    def loaded_path(self) -> Optional[str]:
        with self._lock:
            return self._loaded_path

    def decode(self, pkt_or_bytes: Any, channel_id: int = 3, mod_label: str = "V23-1200") -> Dict[str, Any]:
        self.reload(force=False)
        if isinstance(pkt_or_bytes, (bytes, bytearray)):
            return self._decode_binary(bytes(pkt_or_bytes), channel_id=channel_id, mod_label=mod_label)

        out = dict(pkt_or_bytes or {})
        ch_id = int(out.get("channel") or channel_id or 3)
        ch_num = int(out.get("dpu_channel") or (max(1, ch_id - 2) if ch_id in (3, 4, 5, 6) else ch_id))
        mod_s = str(out.get("modulation") or mod_label or "V23-1200")

        # If structured DSU fields are not yet decoded and raw hex is present, decode binary first
        has_dsu_fields = any(k in out for k in ("TP", "TRID", "BPRED", "AL", "REV", "Power", "NRM"))
        raw_hex = str(out.get("hex") or out.get("raw_hex") or "").strip().upper()
        if not has_dsu_fields and raw_hex:
            try:
                raw_b = bytes.fromhex(raw_hex)
                if len(raw_b) >= 8:
                    dec = self._decode_binary(raw_b, channel_id=ch_id, mod_label=mod_s)
                    # Preserve transport metadata (rssi, freq_hz, timestamp, etc.)
                    for k, v in out.items():
                        if k in ("channel", "freq_hz", "freq_offset_hz", "rssi", "rssi_dbfs", "snr_db", "rate", "timestamp"):
                            dec[k] = v
                    return dec
            except ValueError:
                pass

        # Normalize structured DSU dict (supports uppercase or lowercase field names)
        def _g(*names, default=None):
            for n in names:
                if n in out and out[n] is not None:
                    return out[n]
            return default

        tp = str(_g("TP", "tp", "pkt_type", default=("ST" if str(out.get("role", "")).upper() == "REMOTE" else "CM"))).strip().upper()
        if tp not in DSU_PACKET_TYPE_NAMES:
            tp = {"CMD": "CM", "COMMAND": "CM", "STATUS": "ST", "LINK": "LK", "LINK_REPLY": "LR"}.get(tp, "CM")
        origin = str(_g("OR", "origin", default=("RM" if tp in ("ST", "LR") or str(out.get("role", "")).upper() == "REMOTE" else "LD"))).strip().upper()
        origin = "RM" if origin in ("RM", "REMOTE") else "LD"
        rp = str(_g("RP", "rp", "repeat_code", default="DI")).strip().upper()
        if rp not in ("DI", "RP", "RO"):
            rp = "DI"
        addr = int(_g("ADDR", "addr", "loco_id", "unit_id", default=0) or 0) & 0xFFFF
        trid = int(_g("TRID", "trid", "train_id", default=0) or 0) & 0xFF
        seq = int(_g("SEQ", "seq", "_sequence_", default=0) or 0) & 0x1F
        nrm = max(0, min(5, int(_g("NRM", "nrm", "num_remotes", default=1 if tp != "LK" else 0) or 0)))

        power = decode_dsu_power(_g("Power", "power", "throttle", default="IDLE"))
        rev = str(_g("REV", "rev", "reverser", default="CT")).strip().upper()
        rev = {"C": "CT", "CENTER": "CT", "CENTERED": "CT", "F": "FO", "FWD": "FO", "FORWARD": "FO", "R": "RE", "REV": "RE", "REVERSE": "RE"}.get(rev, rev if rev in ("CT", "FO", "RE") else "CT")

        mtr = 1 if int(bool(_g("MTR", "mtr", "motoring", default=(1 if power.startswith("N") else 0)))) else 0
        bvin = 1 if int(bool(_g("BVIN", "bvin", "brake_valve_cut_in", default=0))) else 0
        sand = 1 if int(bool(_g("Sand", "sand", "SAND", "manual_sand", default=0))) else 0
        prk = 1 if int(bool(_g("PRK", "prk", "parking_brake", default=0))) else 0

        role = "REMOTE" if origin == "RM" else "LEAD"
        out.update({
            "protocol": "dpu",
            "dsu_decoded": True,
            "type_name": f"DPU_{tp}",
            "role": role,
            "dpu_channel": ch_num,
            "ADDR": addr,
            "TRID": trid,
            "TP": tp,
            "OR": origin,
            "RP": rp,
            "SEQ": seq,
            "NRM": nrm,
            "Power": power,
            "REV": rev,
            "MTR": mtr,
            "BVIN": bvin,
            "Sand": sand,
            "PRK": prk,
            "loco_id": int(_g("loco_id", default=addr) or addr),
            "unit_id": addr,
            "node_id": addr,
            "_node_id_": addr,
            "throttle": power,
            "modulation": mod_s,
            "_type_": f"DPU_{tp}",
            "_sequence_": seq,
        })

        if tp == "CM":
            out["BPRED"] = _decode_dsu_bpred(_g("BPRED", "bpred", default="REL"))
            out["IB"] = _decode_dsu_ib(_g("IB", "ib", default=0))
            if "pressure_psi" not in out:
                out["pressure_psi"] = out["BPRED"] if isinstance(out["BPRED"], (int, float)) else 0
        elif tp == "ST":
            bp = int(_g("BP", "bp", "pressure_psi", default=90) or 0)
            er = int(_g("ER", "er", default=bp) or 0)
            mr = int(_g("MR", "mr", default=138) or 0)
            bc = int(_g("BC", "bc", default=0) or 0)
            af = int(_g("AF", "af", default=0) or 0)
            tre = int(_g("TRE", "tre", default=0) or 0)
            raw_al = _g("AL", "al", "alarms", default=[])
            if isinstance(raw_al, str):
                al_list = [a.strip().upper() for a in raw_al.split(",") if a.strip() and a.strip().upper() != "NONE"]
            elif isinstance(raw_al, (list, tuple, set)):
                al_list = [str(a).strip().upper() for a in raw_al if str(a).strip()]
            else:
                al_list = []
            out.update({
                "BP": bp, "ER": er, "MR": mr, "BC": bc, "AF": af, "TRE": tre, "AL": al_list,
                "pressure_psi": bp,
            })
        elif tp == "LK":
            out["link_action"] = "UNLINK" if nrm == 0 else "LINK"

        with self._lock:
            rules_obj = self._rules_obj
            pkt_parser = self._packet_parser

        rules_source = "default"
        custom_scroll_override: Optional[str] = None
        if isinstance(rules_obj, list) and pkt_parser is not None:
            try:
                raw_b = b""
                if raw_hex:
                    try:
                        cand_b = bytes.fromhex(raw_hex)
                        if len(cand_b) >= 10:
                            raw_b = cand_b
                    except ValueError:
                        raw_b = b""
                if not raw_b:
                    # Canonicalize legacy 8-byte / structured DSU dicts into binary DSU layout for parse_packets.py
                    raw_b = pack_dsu_payload(
                        addr=addr, trid=trid, tp=tp, origin=origin, rp=rp, seq=seq, nrm=nrm,
                        power=power, rev=rev, mtr=mtr, bvin=bvin, sand=sand, prk=prk,
                        bpred=out.get("BPRED", "REL"), ib=out.get("IB", 0),
                        bp=int(out.get("BP", out.get("pressure_psi", 90)) or 0),
                        er=int(out.get("ER", 90) or 0),
                        mr=int(out.get("MR", 138) or 0),
                        bc=int(out.get("BC", 0) or 0),
                        af=int(out.get("AF", 0) or 0),
                        tre=int(out.get("TRE", 0) or 0),
                        alarms=out.get("AL") or [],
                    )
                res = pkt_parser.parse(raw_b)
                if isinstance(res, dict) and res.get("is_valid"):
                    pdata = res.get("data", res)
                    if isinstance(pdata, dict):
                        for k, v in pdata.items():
                            if k not in ("protocol", "channel", "rssi", "freq_hz") and not k.startswith("_rsv_"):
                                out[k] = v
                        if pdata.get("_scroll_ptc_"):
                            sc = str(pdata["_scroll_ptc_"])
                            # Preserve RF channel badge when standard [DPU LEAD]/[DPU REMOTE] prefix is used
                            if sc.startswith("[DPU LEAD]"):
                                sc = f"[DPU Ch{ch_num} LEAD]" + sc[len("[DPU LEAD]"):]
                            elif sc.startswith("[DPU REMOTE]"):
                                sc = f"[DPU Ch{ch_num} REMOTE]" + sc[len("[DPU REMOTE]"):]
                            custom_scroll_override = sc
                        rules_source = "dpu_rules.json"
            except Exception:
                pass
        elif isinstance(rules_obj, dict):
            rules_source = "dpu_rules.json"
            units_map = rules_obj.get("units") if isinstance(rules_obj.get("units"), dict) else {}
            ucfg = units_map.get(str(addr)) or units_map.get(f"0x{addr:04X}")
            if isinstance(ucfg, dict):
                if ucfg.get("name"):
                    out["unit_name"] = str(ucfg["name"])
                if ucfg.get("railroad"):
                    out["railroad"] = str(ucfg["railroad"])
            hide_tps = {str(t).strip().upper() for t in (rules_obj.get("hide_packet_types") or [])}
            if tp in hide_tps:
                out["_hide_scroller_"] = True

        out["_dpu_rules_source_"] = rules_source
        out["_scroll_ptc_"] = (
            custom_scroll_override
            if custom_scroll_override is not None
            else _format_dsu_scroll_line(out, rules_cfg=rules_obj if isinstance(rules_obj, dict) else None)
        )
        out["telemetry"] = {
            "wiu_id": f"DPU_{addr}",
            "src": f"DPU_{role}_{addr}",
            "ADDR": addr,
            "TRID": trid,
            "TP": tp,
            "OR": origin,
            "RP": rp,
            "SEQ": seq,
            "NRM": nrm,
            "Power": power,
            "REV": rev,
            "throttle": power,
            "pressure_psi": out.get("pressure_psi", 0),
        }
        return out

    def _decode_binary(self, payload: bytes, channel_id: int = 3, mod_label: str = "V23-1200") -> Dict[str, Any]:
        hex_str = payload.hex().upper()
        ch_num = max(1, channel_id - 2) if channel_id in (3, 4, 5, 6) else channel_id

        # Full binary DSU frame (>= 10 bytes, packed via pack_dsu_payload or live Locotrol framing)
        if len(payload) >= 10:
            addr = (payload[0] << 8) | payload[1]
            trid = payload[2]
            b3 = payload[3]
            tp = DSU_PACKET_TYPES.get((b3 >> 6) & 0x03, "CM")
            origin = DSU_ORIGINS.get((b3 >> 5) & 0x01, "LD")
            rp = DSU_REPEAT_CODES.get((b3 >> 3) & 0x03, "DI")
            nrm = min(5, b3 & 0x07)
            b4 = payload[4]
            seq = (b4 >> 3) & 0x1F
            rev = DSU_REVERSERS.get((b4 >> 1) & 0x03, "CT")
            power = decode_dsu_power(payload[5])
            b6 = payload[6]
            mtr = 1 if (b6 & 0x01) else 0
            bvin = 1 if (b6 & 0x02) else 0
            sand = 1 if (b6 & 0x04) else 0
            prk = 1 if (b6 & 0x08) else 0

            base: Dict[str, Any] = {
                "protocol": "dpu",
                "channel": channel_id,
                "dpu_channel": ch_num,
                "ADDR": addr,
                "TRID": trid,
                "TP": tp,
                "OR": origin,
                "RP": rp,
                "SEQ": seq,
                "NRM": nrm,
                "Power": power,
                "REV": rev,
                "MTR": mtr,
                "BVIN": bvin,
                "Sand": sand,
                "PRK": prk,
                "loco_id": addr,
                "modulation": mod_label,
                "hex": hex_str,
            }
            if tp == "CM":
                base["BPRED"] = _decode_dsu_bpred(payload[7])
                base["IB"] = _decode_dsu_ib(payload[8])
            elif tp == "ST":
                base["BP"] = payload[7]
                base["ER"] = payload[8]
                base["MR"] = payload[9]
                base["BC"] = payload[10] if len(payload) >= 11 else 0
                base["AF"] = payload[11] if len(payload) >= 12 else 0
                raw_tre = payload[12] if len(payload) >= 13 else 0
                base["TRE"] = raw_tre - 256 if raw_tre >= 128 else raw_tre
                al_mask = payload[13] if len(payload) >= 14 else 0
                base["AL"] = [name for bit_m, name in DSU_ALARM_BITS if (al_mask & bit_m)]
            return self.decode(base, channel_id=channel_id, mod_label=mod_label)

        # Backwards-compatible legacy 8-byte pack_dpu_payload() framing
        role = "REMOTE" if channel_id in (5, 6) or (len(payload) >= 7 and payload[6] == 1) else "LEAD"
        origin = "RM" if role == "REMOTE" else "LD"
        addr_b = payload[0] if len(payload) >= 1 else 0
        ctrl = payload[1] if len(payload) >= 2 else 0
        msg_type = (ctrl >> 4) & 0x0F
        notch = ctrl & 0x0F
        loco_id = ((payload[2] << 16) | (payload[3] << 8) | payload[4]) if len(payload) >= 5 else addr_b
        brake_psi = (payload[5] & 0x7F) if len(payload) >= 6 else 0
        seq = (payload[7] & 0x1F) if len(payload) >= 8 else (addr_b & 0x1F)
        tp = {0x01: "CM", 0x02: "ST", 0x05: "LK"}.get(msg_type, "ST" if role == "REMOTE" else "CM")
        power = f"N{notch}" if 1 <= notch <= 8 else ("IDLE" if notch == 0 else f"B{min(8, notch - 8):.2f}")
        legacy_d: Dict[str, Any] = {
            "protocol": "dpu",
            "channel": channel_id,
            "dpu_channel": ch_num,
            "role": role,
            "ADDR": loco_id & 0xFFFF,
            "TRID": 0,
            "TP": tp,
            "OR": origin,
            "RP": "DI",
            "SEQ": seq,
            "NRM": 1,
            "Power": power,
            "REV": "FO" if (1 <= notch <= 8) else "CT",
            "MTR": 1 if (1 <= notch <= 8) else 0,
            "BVIN": 1 if brake_psi > 0 else 0,
            "Sand": 0,
            "PRK": 0,
            "loco_id": loco_id,
            "pressure_psi": brake_psi,
            "modulation": mod_label,
            "hex": hex_str,
        }
        if tp == "CM":
            legacy_d["BPRED"] = "REL" if brake_psi >= 85 else max(0, 90 - brake_psi)
            legacy_d["IB"] = 0
        elif tp == "ST":
            legacy_d["BP"] = brake_psi
            legacy_d["ER"] = brake_psi
            legacy_d["MR"] = 138
            legacy_d["BC"] = 0
            legacy_d["AF"] = 0
            legacy_d["TRE"] = notch * 10 if 1 <= notch <= 8 else 0
            legacy_d["AL"] = []
        return self.decode(legacy_d, channel_id=channel_id, mod_label=mod_label)


_SHARED_DSU_DECODER: Optional[DsuPacketDecoder] = None


def decode_dpu_packet(pkt: Any, channel_id: int = 3, mod_label: str = "V23-1200") -> Dict[str, Any]:
    global _SHARED_DSU_DECODER
    if _SHARED_DSU_DECODER is None:
        _SHARED_DSU_DECODER = DsuPacketDecoder()
    return _SHARED_DSU_DECODER.decode(pkt, channel_id=channel_id, mod_label=mod_label)


def pack_dpu_payload(
    loco_id: int,
    msg_type: int = 0x01,
    throttle_notch: int = 4,
    brake_psi: int = 90,
    is_remote: bool = False,
    seq: int = 0,
) -> bytes:
    """Packs a structured Distributed Power (DPU) telemetry/command HDLC payload (8 bytes)."""
    addr = 0x80 | (seq & 0x0F) if is_remote else (seq & 0x0F)
    ctrl = ((msg_type & 0x0F) << 4) | (throttle_notch & 0x0F)
    lid = int(loco_id) & 0xFFFFFF
    return bytes([
        addr & 0xFF,
        ctrl & 0xFF,
        (lid >> 16) & 0xFF,
        (lid >> 8) & 0xFF,
        lid & 0xFF,
        max(0, min(127, int(brake_psi))),
        0x01 if is_remote else 0x00,
        (seq & 0xFF),
    ])


def parse_dpu_payload(payload: bytes, channel_id: int, mod_label: str = "V23-1200") -> Dict[str, Any]:
    """Parses a CRC-verified HDLC DPU/DSU frame into full structured telemetry."""
    return decode_dpu_packet(payload, channel_id=channel_id, mod_label=mod_label)


def generate_dpu_v23_iq(
    payload: bytes,
    sample_rate: int = 960000,
    center_freq: int = 452950000,
    channel_freq: int = 452925000,
    fm_dev_hz: float = 2500.0,
    amp: float = 48.0,
    noise_std: float = 2.0,
    pad_samples: int = 8000,
    sample_format: str = "u8",
    seed: int = 452,
) -> bytes:
    """Synthesizes 452/457 MHz DPU 1200-baud CCITT V.23 AFSK (1300/2100 Hz) over NFM with HDLC CRC-16."""
    bits = [1, 0] * 12 + hdlc_encode_frame(payload) + [0, 1, 1, 1, 1, 1, 1, 0]
    sps_rf = float(sample_rate) / float(DPU_V23_BAUD)
    n_burst = int(round(len(bits) * sps_rf))
    n_total = pad_samples + n_burst + pad_samples

    rng = np.random.default_rng(seed)
    sig = (rng.normal(0.0, noise_std, n_total) + 1j * rng.normal(0.0, noise_std, n_total)).astype(np.complex64)

    bit_idx = np.minimum((np.arange(n_burst) / sps_rf).astype(np.int32), len(bits) - 1)
    tone_f = np.where(np.array(bits, dtype=np.int8)[bit_idx] == 1, DPU_V23_MARK_HZ, DPU_V23_SPACE_HZ)
    audio_phase = 2.0 * math.pi * np.cumsum(tone_f) / float(sample_rate)
    audio = np.sin(audio_phase)

    f_off = float(channel_freq - center_freq)
    rf_phase = 2.0 * math.pi * np.cumsum(f_off + fm_dev_hz * audio) / float(sample_rate)
    sig[pad_samples:pad_samples + n_burst] += (amp * np.exp(1j * rf_phase)).astype(np.complex64)

    if sample_format == "s16":
        scale = 32768.0 / 127.5
        out = np.empty(n_total * 2, dtype="<i2")
        out[0::2] = np.clip(np.round(sig.real * scale), -32768, 32767).astype("<i2")
        out[1::2] = np.clip(np.round(sig.imag * scale), -32768, 32767).astype("<i2")
        return out.tobytes()
    out_u8 = np.empty(n_total * 2, dtype=np.uint8)
    out_u8[0::2] = np.clip(np.round(sig.real + 127.5), 0, 255).astype(np.uint8)
    out_u8[1::2] = np.clip(np.round(sig.imag + 127.5), 0, 255).astype(np.uint8)
    return out_u8.tobytes()


def generate_dpu_fsk_iq(
    payload: bytes,
    sample_rate: int = 960000,
    center_freq: int = 457937500,
    channel_freq: int = 457925000,
    baud_rate: int = DPU_FSK_BAUD,
    fsk_dev_hz: float = 2000.0,
    amp: float = 48.0,
    noise_std: float = 2.0,
    pad_samples: int = 8000,
    sample_format: str = "u8",
    seed: int = 457,
) -> bytes:
    """Synthesizes 452/457 MHz DPU 4800 bps (or RLC 2400 bps) direct 2-FSK HDLC RF IQ."""
    bits = [1, 0] * 12 + hdlc_encode_frame(payload) + [0, 1, 1, 1, 1, 1, 1, 0]
    sps_rf = float(sample_rate) / float(baud_rate)
    n_burst = int(round(len(bits) * sps_rf))
    n_total = pad_samples + n_burst + pad_samples

    rng = np.random.default_rng(seed)
    sig = (rng.normal(0.0, noise_std, n_total) + 1j * rng.normal(0.0, noise_std, n_total)).astype(np.complex64)

    bit_idx = np.minimum((np.arange(n_burst) / sps_rf).astype(np.int32), len(bits) - 1)
    sym = np.array(bits, dtype=np.float64)[bit_idx] * 2.0 - 1.0
    f_off = float(channel_freq - center_freq)
    rf_phase = 2.0 * math.pi * np.cumsum(f_off + sym * fsk_dev_hz) / float(sample_rate)
    sig[pad_samples:pad_samples + n_burst] += (amp * np.exp(1j * rf_phase)).astype(np.complex64)

    if sample_format == "s16":
        scale = 32768.0 / 127.5
        out = np.empty(n_total * 2, dtype="<i2")
        out[0::2] = np.clip(np.round(sig.real * scale), -32768, 32767).astype("<i2")
        out[1::2] = np.clip(np.round(sig.imag * scale), -32768, 32767).astype("<i2")
        return out.tobytes()
    out_u8 = np.empty(n_total * 2, dtype=np.uint8)
    out_u8[0::2] = np.clip(np.round(sig.real + 127.5), 0, 255).astype(np.uint8)
    out_u8[1::2] = np.clip(np.round(sig.imag + 127.5), 0, 255).astype(np.uint8)
    return out_u8.tobytes()


def nrzi_decode(bits: np.ndarray) -> np.ndarray:
    """Decodes HDLC/AX.25 NRZI (0 = transition, 1 = no transition) into NRZ bits."""
    b = np.asarray(bits, dtype=np.int8) & 1
    if len(b) < 2:
        return b.copy()
    return (b[1:] == b[:-1]).astype(np.int8)


def _update_channel_noise_floor(
    prev_nf_db: Optional[float],
    bb: np.ndarray,
    demod_rate: float,
) -> Tuple[float, float]:
    """
    Tracks a persistent per-channel RF noise floor (dBFS) across chunks using 2 ms power blocks.
    Drops quickly toward quiet noise floors and rises very slowly during sustained bursts so
    150-250 ms EOT/DPU bursts filling an entire chunk never mask themselves as noise.
    Returns (noise_floor_db, peak_block_db).
    """
    n = len(bb)
    blk = max(32, int(round(demod_rate * 0.002)))
    n_blk = n // blk
    if n_blk < 2:
        p = grabber.compute_signal_power_dbfs(bb)
        nf = float(p) if prev_nf_db is None else float(prev_nf_db)
        return nf, float(p)
    pwr = np.mean(np.abs(bb[:n_blk * blk].reshape(n_blk, blk)) ** 2, axis=1)
    pwr_db = 10.0 * np.log10(np.maximum(pwr, 1e-12)) - 20.0 * math.log10(127.5)
    p10 = float(np.percentile(pwr_db, 10))
    p95 = float(np.percentile(pwr_db, 95))
    if prev_nf_db is None:
        nf = p10
    elif p10 < prev_nf_db:
        nf = 0.60 * prev_nf_db + 0.40 * p10
    else:
        nf = 0.96 * prev_nf_db + 0.04 * p10
    return float(nf), float(p95)


def _detect_rf_burst_windows(
    bb: np.ndarray,
    demod_rate: float,
    snr_thresh_db: float = 10.0,
    min_dur_ms: float = 15.0,
    noise_floor_override_db: Optional[float] = None,
) -> List[Tuple[int, int, float, float]]:
    """
    Detects active RF carrier bursts inside a decimated complex baseband array `bb`.
    Returns list of (start_idx, end_idx, burst_rssi_dbfs, burst_snr_db).
    """
    n = len(bb)
    blk = max(32, int(round(demod_rate * 0.002)))  # 2 ms energy blocks
    n_blk = n // blk
    if n_blk < 6:
        return []
    pwr = np.mean(np.abs(bb[:n_blk * blk].reshape(n_blk, blk)) ** 2, axis=1)
    pwr_db = 10.0 * np.log10(np.maximum(pwr, 1e-12)) - 20.0 * math.log10(127.5)
    local_nf = float(np.percentile(pwr_db, 10))
    noise_floor_db = min(local_nf, float(noise_floor_override_db)) if noise_floor_override_db is not None else local_nf
    thresh_db = max(noise_floor_db + snr_thresh_db, -88.0)
    active = pwr_db >= thresh_db

    min_blks = max(2, int(round((min_dur_ms / 1000.0) * demod_rate / blk)))
    windows: List[Tuple[int, int, float, float]] = []
    i = 0
    while i < n_blk:
        if not active[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j < n_blk:
            if active[j]:
                gap = 0
            else:
                gap += 1
                if gap > 3:  # Allow up to 6 ms brief fade inside burst
                    break
            j += 1
        end_blk = j - gap
        if (end_blk - i) >= min_blks:
            b_rssi = float(np.median(pwr_db[i:end_blk]))
            b_snr = max(0.0, b_rssi - noise_floor_db)
            s0 = max(0, i * blk - blk)
            s1 = min(n, end_blk * blk + blk)
            windows.append((s0, s1, b_rssi, b_snr))
        i = max(i + 1, j)
    return windows


def _extract_preamble_fsk_payload(
    disc_seg: np.ndarray,
    demod_rate: float,
    baud_candidates: Tuple[int, ...] = (4800, 2400, 1200),
    min_preamble_bits: int = 10,
    min_payload_bytes: int = 4,
    max_payload_bytes: int = 32,
) -> Optional[Tuple[bytes, int]]:
    """
    Locks onto an alternating 101010... bit-sync preamble inside a centered FM discriminator
    burst segment and extracts synchronized payload bytes for proprietary FSK frames
    (Locotrol DPU & Cattron/Wabtec RLC) that do not use X.25 HDLC 0x7E flags.
    Returns (payload_bytes, matched_baud) or None.
    """
    if len(disc_seg) < 64:
        return None
    centered = disc_seg - float(np.median(disc_seg))
    # Reject flat unmodulated carriers (no FSK deviation)
    if float(np.std(centered)) < 0.03:
        return None

    best_res: Optional[Tuple[int, bytes, int]] = None  # (score, payload, baud)
    for baud in baud_candidates:
        sps = max(4, int(round(demod_rate / float(baud))))
        if len(centered) < sps * (min_preamble_bits + min_payload_bytes * 8):
            continue
        kern = np.ones(sps, dtype=np.float32) / float(sps)
        sm = np.convolve(centered, kern, mode="valid")
        step = max(1, sps // 6)
        for ph in range(0, sps, step):
            syms = sm[ph::sps]
            if len(syms) < min_preamble_bits + min_payload_bytes * 8:
                continue
            bits = (syms >= 0.0).astype(np.int8)
            # Find longest alternating run (101010... or 010101...) in the first half of the burst
            trans = (bits[1:] != bits[:-1]).astype(np.int8)
            run_len = 0
            best_run = 0
            best_end = -1
            search_lim = min(len(trans), max(48, len(trans) // 2))
            for k in range(search_lim):
                if trans[k]:
                    run_len += 1
                    if run_len > best_run:
                        best_run = run_len
                        best_end = k + 2  # First bit after alternating preamble
                else:
                    run_len = 0
            if best_run >= min_preamble_bits and best_end > 0:
                rem = bits[best_end:]
                n_bytes = min(max_payload_bytes, len(rem) // 8)
                if n_bytes < min_payload_bytes:
                    continue
                out = bytearray()
                for bi in range(n_bytes):
                    v = 0
                    for k in range(8):
                        v = (v << 1) | int(rem[bi * 8 + k])
                    out.append(v)
                # Ensure payload isn't all 0x00 / 0xFF / 0xAA / 0x55 idle fill
                uniq = set(out)
                if len(uniq) <= 1 or uniq.issubset({0x00, 0xFF, 0xAA, 0x55}):
                    continue
                score = best_run * 10 + n_bytes
                if best_res is None or score > best_res[0]:
                    best_res = (score, bytes(out), baud)

    if best_res is None:
        return None
    return best_res[1], best_res[2]


class DpuChannelDemodulator:
    """
    Clean-room 452/457 MHz Distributed Power (DPU / Locotrol) multi-mode demodulator:
      - Burst-envelope squelch + burst-local carrier DC centering
      - Path A: CCITT V.23 1200-baud AFSK (1300/2100 Hz) + HDLC CRC-16 (NRZ & NRZI)
      - Path B: Coherent FFSK/MSK 1200-baud (1200/1800 Hz) + HDLC CRC-16 (NRZ & NRZI)
      - Path C: Direct 2400 & 4800 bps 2-FSK + HDLC CRC-16 (NRZ & NRZI)
      - Path D: Preamble-locked FSK burst recovery for proprietary Locotrol III/IV/XA frames
    """

    def __init__(self, input_rate: int, channel_id: int, freq_hz: int):
        self.input_rate = int(input_rate)
        self.channel_id = int(channel_id)
        self.freq_hz = int(freq_hz)
        self.decim = max(1, int(round(self.input_rate / float(FRED_DEMOD_RATE))))
        self.actual_demod_rate = float(self.input_rate) / float(self.decim)
        self.sps_v23 = max(8, int(round(self.actual_demod_rate / float(DPU_V23_BAUD))))
        self.sps_2400 = max(6, int(round(self.actual_demod_rate / float(RLC_FSK_BAUD))))
        self.sps_fsk = max(4, int(round(self.actual_demod_rate / float(DPU_FSK_BAUD))))
        # Two-stage sharp 12.5 kHz channel filter (prevents adjacent EOT 457.9375 MHz from bleeding into DPU3/4!)
        cutoff = min(0.45, (FRED_CHANNEL_BW_HZ * 0.5) / float(self.input_rate))
        taps = min(255, max(129, 10 * self.decim + 1))
        self.lpf = grabber.WindowedSincFilter(self.decim, cutoff, taps)
        self.ch_lpf = grabber.WindowedSincFilter(1, min(0.45, 4800.0 / float(self.actual_demod_rate)), 65)
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.audio_tail = np.zeros(0, dtype=np.float32)
        self._recent_hashes: collections.deque = collections.deque(maxlen=64)
        self._noise_floor_db: Optional[float] = None
        self._prev_chunk_active: bool = False

        t = np.arange(self.sps_v23, dtype=np.float32) / float(self.actual_demod_rate)
        self.mark_cos = np.cos(2.0 * math.pi * DPU_V23_MARK_HZ * t).astype(np.float32)
        self.mark_sin = np.sin(2.0 * math.pi * DPU_V23_MARK_HZ * t).astype(np.float32)
        self.space_cos = np.cos(2.0 * math.pi * DPU_V23_SPACE_HZ * t).astype(np.float32)
        self.space_sin = np.sin(2.0 * math.pi * DPU_V23_SPACE_HZ * t).astype(np.float32)
        self.ffsk12_cos = np.cos(2.0 * math.pi * FRED_TONE_LOW_HZ * t).astype(np.float32)
        self.ffsk12_sin = np.sin(2.0 * math.pi * FRED_TONE_LOW_HZ * t).astype(np.float32)
        self.ffsk18_cos = np.cos(2.0 * math.pi * FRED_TONE_HIGH_HZ * t).astype(np.float32)
        self.ffsk18_sin = np.sin(2.0 * math.pi * FRED_TONE_HIGH_HZ * t).astype(np.float32)

        self.last_sync_count = 0
        self.last_fec_good = 0
        self.last_fec_bad = 0
        self.last_bursts = 0

    def _emit_hdlc_candidates(
        self,
        hard_bits: np.ndarray,
        mod_label: str,
        rate_str: str,
        est_df_hz: float,
        ch_rssi: float,
        snr_db: float,
        packets: List[Dict[str, Any]],
    ) -> int:
        cand_total = 0
        # Check NRZ normal, NRZ inverted, and NRZI (polarity-invariant)
        for stream_bits in (hard_bits, 1 - hard_bits, nrzi_decode(hard_bits)):
            frames, cands = hdlc_extract_frames(stream_bits)
            cand_total += cands
            for payload in frames:
                hx = payload.hex().upper()
                if hx in self._recent_hashes:
                    continue
                self._recent_hashes.append(hx)
                pkt = parse_dpu_payload(payload, self.channel_id, mod_label=mod_label)
                pkt.update({
                    "channel": self.channel_id,
                    "freq_hz": self.freq_hz,
                    "freq_offset_hz": round(est_df_hz, 1),
                    "rssi": round(float(ch_rssi), 2),
                    "rssi_dbfs": round(float(ch_rssi), 2),
                    "snr_db": round(float(snr_db), 1),
                    "crc_ok": True,
                    "rate": rate_str,
                    "timestamp": datetime.now().isoformat(),
                })
                packets.append(pkt)
        return cand_total

    def process_iq(self, iq_complex: np.ndarray, center_freq: int) -> Tuple[List[Dict[str, Any]], float]:
        n = len(iq_complex)
        if n == 0:
            return [], -100.0
        df = float(self.freq_hz - center_freq)
        dt = 1.0 / float(self.input_rate)
        idx = np.arange(n, dtype=np.float64)
        lo = np.exp(-2j * math.pi * (self.phase_acc + df * idx * dt)).astype(np.complex64)
        self.phase_acc = float((self.phase_acc + df * n * dt) % 1.0)

        bb = self.ch_lpf.process(self.lpf.process(iq_complex * lo))
        if len(bb) < self.sps_v23 * 16:
            return [], -100.0
        ch_rssi = grabber.compute_signal_power_dbfs(bb)

        ext = np.empty(len(bb) + 1, dtype=np.complex64)
        ext[0] = self.prev_sample
        ext[1:] = bb
        self.prev_sample = bb[-1]
        disc = np.angle(ext[1:] * np.conj(ext[:-1])).astype(np.float32)

        full_disc = np.concatenate([self.audio_tail, disc]) if len(self.audio_tail) > 0 else disc
        keep_tail = self.sps_v23 * 120
        self.audio_tail = full_disc[-keep_tail:].copy() if len(full_disc) > keep_tail else full_disc.copy()

        # Detect RF burst windows with persistent running noise floor
        self._noise_floor_db, peak_blk_db = _update_channel_noise_floor(self._noise_floor_db, bb, self.actual_demod_rate)
        burst_wins = _detect_rf_burst_windows(
            bb,
            self.actual_demod_rate,
            snr_thresh_db=7.5,
            min_dur_ms=15.0,
            noise_floor_override_db=self._noise_floor_db,
        )
        cur_active = bool(burst_wins) or (peak_blk_db >= self._noise_floor_db + 6.5) or (float(ch_rssi) > -38.0)
        has_rf_energy = cur_active or self._prev_chunk_active
        self._prev_chunk_active = cur_active
        if burst_wins:
            b0_top, b1_top, _, _ = max(burst_wins, key=lambda w: w[3])
            seg_top = disc[b0_top:b1_top]
            est_df_hz = float(np.median(seg_top) * self.actual_demod_rate / (2.0 * math.pi)) if len(seg_top) > 0 else 0.0
        else:
            est_df_hz = float(np.median(disc) * self.actual_demod_rate / (2.0 * math.pi))
        est_df_hz = max(-5500.0, min(5500.0, est_df_hz))
        best_snr = max((w[3] for w in burst_wins), default=max(0.0, float(peak_blk_db - self._noise_floor_db)))
        audio_ac = full_disc - float(np.median(full_disc))

        if not has_rf_energy:
            self.last_sync_count = 0
            self.last_fec_good = 0
            self.last_fec_bad = 0
            self.last_bursts = 0
            return [], ch_rssi

        packets: List[Dict[str, Any]] = []
        total_cand = 0

        # Path A: CCITT V.23 1200-baud AFSK (1300/2100 Hz)
        if len(audio_ac) >= self.sps_v23 * 32:
            mc = np.convolve(audio_ac, self.mark_cos[::-1], mode="valid")
            ms = np.convolve(audio_ac, self.mark_sin[::-1], mode="valid")
            sc = np.convolve(audio_ac, self.space_cos[::-1], mode="valid")
            ss = np.convolve(audio_ac, self.space_sin[::-1], mode="valid")
            metric_v23 = (mc * mc + ms * ms) - (sc * sc + ss * ss)
            for ph in range(0, self.sps_v23, 4):
                syms = metric_v23[ph::self.sps_v23]
                if len(syms) >= 40:
                    total_cand += self._emit_hdlc_candidates(
                        (syms >= 0.0).astype(np.int8), "V23-1200", "1200", est_df_hz, ch_rssi, best_snr, packets
                    )

            # Path B: Coherent 1200/1800 Hz FFSK -> HDLC CRC-16 (NRZ & NRZI)
            fc12 = np.convolve(audio_ac, self.ffsk12_cos[::-1], mode="valid")
            fs12 = np.convolve(audio_ac, self.ffsk12_sin[::-1], mode="valid")
            fc18 = np.convolve(audio_ac, self.ffsk18_cos[::-1], mode="valid")
            fs18 = np.convolve(audio_ac, self.ffsk18_sin[::-1], mode="valid")
            metric_ffsk = (fc12 * fc12 + fs12 * fs12) - (fc18 * fc18 + fs18 * fs18)
            for ph in range(0, self.sps_v23, 4):
                syms = metric_ffsk[ph::self.sps_v23]
                if len(syms) >= 40:
                    total_cand += self._emit_hdlc_candidates(
                        (syms >= 0.0).astype(np.int8), "FFSK-1200", "1200", est_df_hz, ch_rssi, best_snr, packets
                    )

        # Path C: Direct 2400 & 4800 bps 2-FSK + HDLC CRC-16 (NRZ & NRZI)
        for mod_lbl, rate_s, sps in (("FSK-4800", "4800", self.sps_fsk), ("FSK-2400", "2400", self.sps_2400)):
            if len(audio_ac) >= sps * 44:
                kern = np.ones(sps, dtype=np.float32) / float(sps)
                sm_fsk = np.convolve(audio_ac, kern, mode="valid")
                step = max(2, sps // 5)
                for ph in range(0, sps, step):
                    syms = sm_fsk[ph::sps]
                    if len(syms) >= 40:
                        total_cand += self._emit_hdlc_candidates(
                            (syms >= 0.0).astype(np.int8), mod_lbl, rate_s, est_df_hz, ch_rssi, best_snr, packets
                        )

        # Path D: Burst-local preamble-locked FSK recovery for non-HDLC Locotrol III/IV/XA bursts
        if not packets and burst_wins:
            for b0, b1, b_rssi, b_snr in burst_wins:
                seg = disc[b0:b1]
                b_df = float(np.median(seg) * self.actual_demod_rate / (2.0 * math.pi)) if len(seg) > 0 else est_df_hz
                # Reject out-of-channel bleed (e.g. adjacent EOT 12.5 kHz away showing up on DPU3/DPU4!)
                if abs(b_df) > 4500.0:
                    continue
                rec = _extract_preamble_fsk_payload(seg, self.actual_demod_rate, baud_candidates=(4800, 2400, 1200))
                if rec is None:
                    continue
                payload, matched_baud = rec
                hx = payload.hex().upper()
                if hx in self._recent_hashes:
                    continue
                self._recent_hashes.append(hx)
                dur_ms = int(round((b1 - b0) * 1000.0 / float(self.actual_demod_rate)))
                ch_num = max(1, self.channel_id - 2) if self.channel_id in (3, 4, 5, 6) else self.channel_id
                role = "REMOTE" if self.channel_id in (5, 6) else "LEAD"
                uid = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
                scroll = (
                    f"[DPU Ch{ch_num} {role}] Burst {dur_ms}ms ({matched_baud}bps SNR:{b_snr:.0f}dB) | {hx[:32]}"
                )
                packets.append({
                    "protocol": "dpu",
                    "type_name": "DPU_BURST",
                    "crc_ok": False,
                    "role": role,
                    "dpu_channel": ch_num,
                    "loco_id": uid,
                    "unit_id": uid,
                    "node_id": uid,
                    "_node_id_": uid,
                    "throttle": "-",
                    "pressure_psi": 0,
                    "modulation": f"FSK-{matched_baud}",
                    "duration_ms": dur_ms,
                    "hex": hx,
                    "channel": self.channel_id,
                    "freq_hz": self.freq_hz,
                    "freq_offset_hz": round(b_df, 1),
                    "rssi": round(float(b_rssi), 2),
                    "rssi_dbfs": round(float(b_rssi), 2),
                    "snr_db": round(float(b_snr), 1),
                    "rate": str(matched_baud),
                    "timestamp": datetime.now().isoformat(),
                    "_scroll_ptc_": scroll,
                    "_type_": "DPU_BURST",
                    "_sequence_": None,
                    "telemetry": {"wiu_id": f"DPU_{uid:04X}", "src": f"DPU_{role}_{uid:04X}", "duration_ms": dur_ms},
                })

        self.last_sync_count = max(len(packets), min(total_cand, len(packets) + 2))
        self.last_fec_good = len(packets)
        self.last_fec_bad = max(0, self.last_sync_count - self.last_fec_good)
        self.last_bursts = max(len(burst_wins), self.last_sync_count)
        return packets, ch_rssi


def parse_rlc_payload(payload: bytes, channel_id: int, crc_ok: bool = True, baud_label: str = "2400") -> Dict[str, Any]:
    """Formats a 452 MHz Remote Locomotive Control (RLC) burst/HDLC frame (default fallback)."""
    hex_str = payload.hex().upper()
    rlc_num = max(1, channel_id - 6) if channel_id in (7, 8, 9, 10, 11) else channel_id
    ocu_id = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
    cmd_b = payload[2] if len(payload) >= 3 else 0
    tag = "HDLC/CRC" if crc_ok else "FSK-SYNC"
    scroll = f"[RLC Ch{rlc_num}] OCU:0x{ocu_id:04X} Cmd:0x{cmd_b:02X} ({tag} {baud_label}bps) | {hex_str}"
    return {
        "protocol": "rlc",
        "type_name": "RLC_FRAME" if crc_ok else "RLC_BURST",
        "rlc_channel": rlc_num,
        "ocu_id": ocu_id,
        "unit_id": ocu_id,
        "node_id": ocu_id,
        "_node_id_": ocu_id,
        "crc_ok": bool(crc_ok),
        "baud_label": str(baud_label),
        "hex": hex_str,
        "_scroll_ptc_": scroll,
        "_type_": "RLC_FRAME" if crc_ok else "RLC_BURST",
        "_sequence_": None,
        "telemetry": {
            "wiu_id": f"RLC_{ocu_id:04X}",
            "src": f"RLC_CH{rlc_num}_{ocu_id:04X}",
            "crc_ok": bool(crc_ok),
        },
    }


def _extract_rlc_field_int(raw_bytes: bytes, spec: Any, default: Optional[int] = None) -> Optional[int]:
    """Extracts an integer field from raw RLC bytes using a rule spec ({byte/bytes, mask, shift})."""
    if not raw_bytes or not isinstance(spec, dict):
        return default
    if "bytes" in spec and isinstance(spec["bytes"], (list, tuple)) and spec["bytes"]:
        idxs = [int(i) for i in spec["bytes"]]
        if max(idxs) >= len(raw_bytes) or min(idxs) < 0:
            return default
        val = 0
        for idx in idxs:
            val = (val << 8) | raw_bytes[idx]
    elif "byte" in spec:
        idx = int(spec["byte"])
        if idx < 0 or idx >= len(raw_bytes):
            return default
        val = raw_bytes[idx]
    else:
        return default

    if "mask" in spec and spec["mask"] is not None:
        m = spec["mask"]
        mask_i = int(m, 0) if isinstance(m, str) else int(m)
        val &= mask_i
    if "shift" in spec and spec["shift"] is not None:
        val >>= int(spec["shift"])
    return int(val)


class RlcRulesDecoder:
    """
    Decodes 452 MHz Remote Control Locomotive (RLC) packets using `config/rlc_rules.json`
    when present (supporting both PacketParser JSON lists and structured RLC field/unit maps),
    or falls back to the built-in default RLC decoding logic when `rlc_rules.json` is absent.
    Hot-reloads automatically if `rlc_rules.json` is created, edited, or deleted.
    """

    def __init__(self, config_dir: Optional[str] = None, rules_path: Optional[str] = None):
        self.base_dir = PROJECT_ROOT
        self.config_dir = os.path.abspath(config_dir) if config_dir else os.path.join(self.base_dir, "config")
        self.explicit_rules_path = os.path.abspath(rules_path) if rules_path else None
        self._lock = threading.Lock()
        self._loaded_path: Optional[str] = None
        self._loaded_mtime: Optional[float] = None
        self._rules_obj: Any = None
        self._packet_parser: Any = None
        self._last_check_ts: float = 0.0
        self.reload(force=True)

    def _candidate_paths(self) -> List[str]:
        cands: List[str] = []
        if self.explicit_rules_path:
            cands.append(self.explicit_rules_path)
        if self.config_dir:
            cands.append(os.path.join(self.config_dir, "rlc_rules.json"))
        cands.append(os.path.join(self.base_dir, "config", "rlc_rules.json"))
        cands.append(os.path.join(os.getcwd(), "config", "rlc_rules.json"))
        seen: Set[str] = set()
        out: List[str] = []
        for p in cands:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                out.append(ap)
        return out

    def reload(self, force: bool = False) -> bool:
        """Checks for `rlc_rules.json` creation/modification/removal and reloads if changed."""
        now = time.monotonic()
        if not force and (now - self._last_check_ts) < 1.0:
            return False
        self._last_check_ts = now

        found_path: Optional[str] = None
        found_mtime: Optional[float] = None
        for p in self._candidate_paths():
            if os.path.isfile(p):
                try:
                    found_mtime = os.path.getmtime(p)
                    found_path = p
                    break
                except OSError:
                    pass

        with self._lock:
            if not force and found_path == self._loaded_path and found_mtime == self._loaded_mtime:
                return False

            self._loaded_path = found_path
            self._loaded_mtime = found_mtime
            self._rules_obj = None
            self._packet_parser = None

            if not found_path:
                return True

            try:
                with open(found_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._rules_obj = data
                if isinstance(data, list):
                    try:
                        from parse_packets import PacketParser
                        self._packet_parser = PacketParser(data)
                    except Exception:
                        self._packet_parser = None
                return True
            except Exception:
                self._rules_obj = None
                self._packet_parser = None
                return False

    @property
    def has_custom_rules(self) -> bool:
        with self._lock:
            return self._rules_obj is not None

    @property
    def loaded_path(self) -> Optional[str]:
        with self._lock:
            return self._loaded_path

    def decode(self, pkt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decodes an RLC packet dict using `config/rlc_rules.json` if present,
        or preserves the existing default RLC decoding logic when missing.
        """
        self.reload(force=False)
        out = dict(pkt or {})
        raw_hex = str(out.get("hex") or out.get("raw_hex") or "").strip().upper()
        try:
            raw_bytes = bytes.fromhex(raw_hex) if raw_hex else b""
        except ValueError:
            raw_bytes = b""

        ch = int(out.get("channel") or 7)
        rlc_num = int(out.get("rlc_channel") or (max(1, ch - 6) if ch in (7, 8, 9, 10, 11) else ch))
        crc_ok = bool(out.get("crc_ok", True))

        with self._lock:
            rules_obj = self._rules_obj
            pkt_parser = self._packet_parser

        # If rlc_rules.json is missing, preserve existing default decoding logic cleanly
        if rules_obj is None:
            out.setdefault("_rlc_rules_source_", "default")
            if "_scroll_ptc_" not in out and raw_bytes:
                fb = parse_rlc_payload(raw_bytes, ch, crc_ok=crc_ok, baud_label=str(out.get("baud_label") or "2400"))
                for k, v in fb.items():
                    out.setdefault(k, v)
            return out

        # Case 1: Standard PacketParser JSON rule list
        if isinstance(rules_obj, list) and pkt_parser is not None and raw_bytes:
            try:
                res = pkt_parser.parse(raw_bytes)
                if isinstance(res, dict) and res.get("is_valid"):
                    pdata = res.get("data", res)
                    if isinstance(pdata, dict):
                        for k, v in pdata.items():
                            if k not in ("protocol", "channel", "rssi", "freq_hz"):
                                out[k] = v
                        out["_rlc_rules_source_"] = "rlc_rules.json"
                        if pdata.get("_scroll_ptc_"):
                            out["_scroll_ptc_"] = str(pdata["_scroll_ptc_"])
                        return out
            except Exception:
                pass
            out["_rlc_rules_source_"] = "default"
            return out

        # Case 2: Structured RLC rules dictionary
        if isinstance(rules_obj, dict):
            fields_cfg = rules_obj.get("fields") if isinstance(rules_obj.get("fields"), dict) else {}
            units_map = rules_obj.get("units") if isinstance(rules_obj.get("units"), dict) else {}

            ft_map = dict(RLC_DEFAULT_FRAME_TYPES)
            if isinstance(rules_obj.get("frame_types"), dict):
                for k, v in rules_obj["frame_types"].items():
                    if str(k).isdigit():
                        ft_map[int(k)] = str(v)

            spd_map = dict(RLC_DEFAULT_SPEED_STEPS)
            if isinstance(rules_obj.get("speed_steps"), dict):
                for k, v in rules_obj["speed_steps"].items():
                    if str(k).isdigit():
                        spd_map[int(k)] = str(v)

            rev_map = dict(RLC_DEFAULT_REVERSERS)
            if isinstance(rules_obj.get("reverser_map"), dict):
                for k, v in rules_obj["reverser_map"].items():
                    if str(k).isdigit():
                        rev_map[int(k)] = str(v)

            default_ocu = ((raw_bytes[0] << 8) | raw_bytes[1]) if len(raw_bytes) >= 2 else int(out.get("unit_id", 0) or 0)
            ocu_id = _extract_rlc_field_int(raw_bytes, fields_cfg.get("ocu_id"), default=default_ocu)
            if ocu_id is None:
                ocu_id = default_ocu
            ocu_hex = f"{ocu_id:04X}"

            unit_cfg = (
                units_map.get(ocu_hex)
                or units_map.get(f"0x{ocu_hex}")
                or units_map.get(str(ocu_id))
                or units_map.get("*")
                or {}
            )
            if not isinstance(unit_cfg, dict):
                unit_cfg = {}

            lcu_raw = _extract_rlc_field_int(raw_bytes, fields_cfg.get("lcu_id"), default=None)
            lcu_str = str(unit_cfg.get("lcu_id") or (f"0x{lcu_raw:04X}" if lcu_raw is not None else ""))
            unit_name = str(unit_cfg.get("name") or "")

            ft_code = _extract_rlc_field_int(
                raw_bytes,
                fields_cfg.get("frame_type", {"byte": 2, "mask": 0xC0, "shift": 6}),
                default=0,
            )
            frame_type = ft_map.get(int(ft_code or 0), f"TYPE_{ft_code}")

            seq_val = _extract_rlc_field_int(
                raw_bytes,
                fields_cfg.get("seq", {"byte": 2, "mask": 0x3F, "shift": 0}),
                default=0,
            )
            spd_code = _extract_rlc_field_int(
                raw_bytes,
                fields_cfg.get("speed", {"byte": 3, "mask": 0x07, "shift": 0}),
                default=None,
            )
            speed_str = spd_map.get(spd_code, str(spd_code)) if spd_code is not None else ""

            rev_code = _extract_rlc_field_int(
                raw_bytes,
                fields_cfg.get("reverser", {"byte": 3, "mask": 0x18, "shift": 3}),
                default=None,
            )
            rev_str = rev_map.get(rev_code, "N") if rev_code is not None else ""

            ib_val = _extract_rlc_field_int(raw_bytes, fields_cfg.get("ind_brake"), default=None)
            ab_val = _extract_rlc_field_int(raw_bytes, fields_cfg.get("auto_brake"), default=None)

            active_flags: List[str] = []
            flags_spec = fields_cfg.get("flags")
            if isinstance(flags_spec, dict) and "byte" in flags_spec and isinstance(flags_spec.get("bits"), dict):
                b_idx = int(flags_spec["byte"])
                if 0 <= b_idx < len(raw_bytes):
                    fb_val = raw_bytes[b_idx]
                    for bit_k, flag_name in flags_spec["bits"].items():
                        try:
                            bit_i = int(bit_k)
                            if (fb_val >> bit_i) & 1:
                                active_flags.append(str(flag_name).upper())
                        except (ValueError, TypeError):
                            pass
            else:
                for fname, def_spec in (
                    ("HORN", {"byte": 3, "mask": 0x20, "shift": 5}),
                    ("BELL", {"byte": 3, "mask": 0x40, "shift": 6}),
                    ("SAND", {"byte": 3, "mask": 0x80, "shift": 7}),
                ):
                    v = _extract_rlc_field_int(raw_bytes, fields_cfg.get(fname.lower(), def_spec), default=0)
                    if v:
                        active_flags.append(fname)

            man_down_v = _extract_rlc_field_int(raw_bytes, fields_cfg.get("man_down"), default=0)
            man_down = bool(man_down_v) or ("MAN-DOWN" in active_flags) or ("TILT" in active_flags)
            if man_down and "MAN-DOWN" not in active_flags:
                active_flags.append("MAN-DOWN")

            crc_tag = "CRC:OK" if crc_ok else "CRC:SYNC"
            ocu_lbl = f"0x{ocu_hex}({unit_name})" if unit_name else f"0x{ocu_hex}"
            route_lbl = f"OCU:{ocu_lbl}->LCU:{lcu_str}" if lcu_str else f"OCU:{ocu_lbl}"

            detail_parts: List[str] = []
            if speed_str:
                detail_parts.append(f"Spd:{speed_str}")
            if rev_str:
                detail_parts.append(f"Rev:{rev_str}")
            if ib_val is not None:
                detail_parts.append(f"IB:{ib_val}")
            if ab_val is not None:
                detail_parts.append(f"AB:{ab_val}")
            if active_flags:
                detail_parts.append(f"[{','.join(active_flags)}]")

            custom_fmt = unit_cfg.get("scroll_format") or rules_obj.get("scroll_format")
            if custom_fmt:
                try:
                    scroll_line = str(custom_fmt).format(
                        channel=rlc_num, frame_type=frame_type, ocu_id=ocu_hex,
                        unit_name=unit_name, lcu_id=lcu_str, seq=seq_val,
                        speed=speed_str, reverser=rev_str, ind_brake=ib_val,
                        auto_brake=ab_val, flags=",".join(active_flags),
                        crc=crc_tag, hex=raw_hex,
                    )
                except Exception:
                    scroll_line = f"[RLC Ch{rlc_num}] {frame_type} {route_lbl} SEQ:{seq_val} | {' '.join(detail_parts)} | {crc_tag}"
            else:
                det_s = f" | {' '.join(detail_parts)}" if detail_parts else ""
                scroll_line = f"[RLC Ch{rlc_num}] {frame_type} {route_lbl} SEQ:{seq_val}{det_s} | {crc_tag}"

            out.update({
                "protocol": "rlc",
                "rlc_channel": rlc_num,
                "frame_type": frame_type,
                "ocu_id": ocu_id,
                "lcu_id": lcu_str,
                "unit_name": unit_name,
                "unit_id": ocu_id,
                "node_id": ocu_id,
                "_node_id_": ocu_id,
                "seq": seq_val,
                "_sequence_": seq_val,
                "speed": speed_str,
                "reverser": rev_str,
                "ind_brake": ib_val,
                "auto_brake": ab_val,
                "flags": active_flags,
                "horn": "HORN" in active_flags,
                "bell": "BELL" in active_flags,
                "sand": "SAND" in active_flags,
                "man_down": man_down,
                "crc_ok": crc_ok,
                "_rlc_rules_source_": "rlc_rules.json",
                "_scroll_ptc_": scroll_line,
            })
            if unit_cfg.get("ctc_id"):
                out["ctc_id"] = str(unit_cfg["ctc_id"])
                out["_ctc_id_"] = str(unit_cfg["ctc_id"])
            return out

        out.setdefault("_rlc_rules_source_", "default")
        return out


_SHARED_RLC_DECODER: Optional[RlcRulesDecoder] = None


def decode_rlc_packet(pkt: Dict[str, Any], config_dir: Optional[str] = None) -> Dict[str, Any]:
    global _SHARED_RLC_DECODER
    if config_dir:
        return RlcRulesDecoder(config_dir=config_dir).decode(pkt)
    if _SHARED_RLC_DECODER is None:
        _SHARED_RLC_DECODER = RlcRulesDecoder()
    return _SHARED_RLC_DECODER.decode(pkt)


class RlcChannelDemodulator:
    """
    Clean-room 452 MHz Remote Locomotive Control (RLC) 12.5 kHz narrowband channelizer & demodulator
    for 452.93125, 452.94375, 452.95625, 452.96250, and 452.96875 MHz:
      - 12.5 kHz channel filter (+/- 6.25 kHz LPF) matching FCC Part 90 narrowband FSK receiver width
      - Path A: 1200 / 2400 / 4800 bps 2-FSK/GFSK + HDLC CRC-16 (NRZ & NRZI)
      - Path B: Burst-envelope squelch + preamble-locked FSK recovery for Cattron/Wabtec RCL bursts
    """

    def __init__(self, input_rate: int, channel_id: int, freq_hz: int):
        self.input_rate = int(input_rate)
        self.channel_id = int(channel_id)
        self.freq_hz = int(freq_hz)
        self.decim = max(1, int(round(self.input_rate / float(FRED_DEMOD_RATE))))
        self.actual_demod_rate = float(self.input_rate) / float(self.decim)
        self.sps_1200 = max(8, int(round(self.actual_demod_rate / float(DPU_V23_BAUD))))
        self.sps_2400 = max(6, int(round(self.actual_demod_rate / float(RLC_FSK_BAUD))))
        self.sps_4800 = max(4, int(round(self.actual_demod_rate / float(DPU_FSK_BAUD))))
        cutoff = min(0.45, (FRED_CHANNEL_BW_HZ * 0.5) / float(self.input_rate))
        taps = min(255, max(129, 10 * self.decim + 1))
        self.lpf = grabber.WindowedSincFilter(self.decim, cutoff, taps)
        self.ch_lpf = grabber.WindowedSincFilter(1, min(0.45, 4800.0 / float(self.actual_demod_rate)), 65)
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.disc_tail = np.zeros(0, dtype=np.float32)
        self._recent_hashes: collections.deque = collections.deque(maxlen=64)
        self._noise_floor_db: Optional[float] = None
        self._prev_chunk_active: bool = False
        self.last_sync_count = 0
        self.last_fec_good = 0
        self.last_fec_bad = 0
        self.last_bursts = 0

    def process_iq(self, iq_complex: np.ndarray, center_freq: int) -> Tuple[List[Dict[str, Any]], float]:
        n = len(iq_complex)
        if n == 0:
            return [], -100.0
        df = float(self.freq_hz - center_freq)
        dt = 1.0 / float(self.input_rate)
        idx = np.arange(n, dtype=np.float64)
        lo = np.exp(-2j * math.pi * (self.phase_acc + df * idx * dt)).astype(np.complex64)
        self.phase_acc = float((self.phase_acc + df * n * dt) % 1.0)

        bb = self.ch_lpf.process(self.lpf.process(iq_complex * lo))
        if len(bb) < self.sps_2400 * 16:
            return [], -100.0
        ch_rssi = grabber.compute_signal_power_dbfs(bb)

        ext = np.empty(len(bb) + 1, dtype=np.complex64)
        ext[0] = self.prev_sample
        ext[1:] = bb
        self.prev_sample = bb[-1]
        disc = np.angle(ext[1:] * np.conj(ext[:-1])).astype(np.float32)

        full_disc = np.concatenate([self.disc_tail, disc]) if len(self.disc_tail) > 0 else disc
        keep_tail = self.sps_2400 * 100
        self.disc_tail = full_disc[-keep_tail:].copy() if len(full_disc) > keep_tail else full_disc.copy()

        self._noise_floor_db, peak_blk_db = _update_channel_noise_floor(self._noise_floor_db, bb, self.actual_demod_rate)
        burst_wins = _detect_rf_burst_windows(
            bb,
            self.actual_demod_rate,
            snr_thresh_db=7.5,
            min_dur_ms=15.0,
            noise_floor_override_db=self._noise_floor_db,
        )
        cur_active = bool(burst_wins) or (peak_blk_db >= self._noise_floor_db + 6.5) or (float(ch_rssi) > -38.0)
        has_rf_energy = cur_active or self._prev_chunk_active
        self._prev_chunk_active = cur_active
        if not has_rf_energy:
            self.last_sync_count = 0
            self.last_fec_good = 0
            self.last_fec_bad = 0
            self.last_bursts = 0
            return [], ch_rssi
        est_df_hz = max(-5500.0, min(5500.0, float(np.median(disc) * self.actual_demod_rate / (2.0 * math.pi))))
        best_snr = max((w[3] for w in burst_wins), default=max(0.0, float(peak_blk_db - self._noise_floor_db)))
        ac = full_disc - float(np.median(full_disc))

        packets: List[Dict[str, Any]] = []
        total_cand = 0

        # Path A: 1200 / 2400 / 4800 bps 2-FSK/GFSK + HDLC CRC-16 (NRZ & NRZI)
        for baud_lbl, sps in (("2400", self.sps_2400), ("4800", self.sps_4800), ("1200", self.sps_1200)):
            if len(ac) < sps * 40:
                continue
            kern = np.ones(sps, dtype=np.float32) / float(sps)
            sm = np.convolve(ac, kern, mode="valid")
            step = max(2, sps // 5)
            for ph in range(0, sps, step):
                syms = sm[ph::sps]
                if len(syms) < 36:
                    continue
                hard = (syms >= 0.0).astype(np.int8)
                for inv_bits in (hard, 1 - hard, nrzi_decode(hard)):
                    frames, cands = hdlc_extract_frames(inv_bits)
                    total_cand += cands
                    for payload in frames:
                        hx = payload.hex().upper()
                        if hx in self._recent_hashes:
                            continue
                        self._recent_hashes.append(hx)
                        pkt = parse_rlc_payload(payload, self.channel_id, crc_ok=True, baud_label=baud_lbl)
                        pkt.update({
                            "channel": self.channel_id,
                            "freq_hz": self.freq_hz,
                            "freq_offset_hz": round(est_df_hz, 1),
                            "rssi": round(float(ch_rssi), 2),
                            "rssi_dbfs": round(float(ch_rssi), 2),
                            "snr_db": round(float(best_snr), 1),
                            "rate": baud_lbl,
                            "timestamp": datetime.now().isoformat(),
                        })
                        packets.append(pkt)

        # Path B: Burst-local preamble-locked narrowband FSK capture (Cattron/Wabtec RCL)
        if not packets and burst_wins:
            for b0, b1, b_rssi, b_snr in burst_wins:
                seg = disc[b0:b1]
                b_df = float(np.median(seg) * self.actual_demod_rate / (2.0 * math.pi)) if len(seg) > 0 else est_df_hz
                if abs(b_df) > 4500.0:
                    continue
                rec = _extract_preamble_fsk_payload(seg, self.actual_demod_rate, baud_candidates=(2400, 4800, 1200))
                if rec is None:
                    continue
                payload, matched_baud = rec
                hx = payload.hex().upper()
                if hx in self._recent_hashes:
                    continue
                self._recent_hashes.append(hx)
                pkt = parse_rlc_payload(payload, self.channel_id, crc_ok=False, baud_label=str(matched_baud))
                pkt.update({
                    "channel": self.channel_id,
                    "freq_hz": self.freq_hz,
                    "freq_offset_hz": round(b_df, 1),
                    "rssi": round(float(b_rssi), 2),
                    "rssi_dbfs": round(float(b_rssi), 2),
                    "snr_db": round(float(b_snr), 1),
                    "rate": str(matched_baud),
                    "duration_ms": int(round((b1 - b0) * 1000.0 / float(self.actual_demod_rate))),
                    "timestamp": datetime.now().isoformat(),
                })
                packets.append(pkt)

        self.last_sync_count = max(len(packets), min(total_cand, len(packets) + 2))
        self.last_fec_good = len(packets)
        self.last_fec_bad = max(0, self.last_sync_count - self.last_fec_good)
        self.last_bursts = max(len(burst_wins), self.last_sync_count)
        return packets, ch_rssi


def build_fred_wire_frames(pkt: Dict[str, Any]) -> Tuple[bytes, List[bytes]]:
    """
    Encodes a decoded EOT/HOT packet into unified ApexBOSFlux/Engine/Spy/Chroma ZMQ multipart frames:
      Frame 0: b'E' + JSON envelope (self-describing across Flux multicast -> Engine & Spy)
      Frame 1: ASCII hex + RSSI
      Frame 2: JSON metadata (for Chroma blips & Spy)
      Frame 3: RSSI tag
    """
    payload_json = json.dumps(pkt, separators=(",", ":")).encode("utf-8")
    wire_frame = b"E" + payload_json
    rssi_val = float(pkt.get("rssi", -75.0))
    hex_str = str(pkt.get("hex") or "")
    hex_frame = f"{hex_str} RSSI:{rssi_val:.1f}".encode("ascii")
    rssi_frame = f"RSSI:{rssi_val:.1f}".encode("ascii")
    return wire_frame, [wire_frame, hex_frame, payload_json, rssi_frame]


# ---------------------------------------------------------------------------
# ApexBOSFred Application Controller (Multi-SDR + Grabber TUI + DPU/RLC + Chroma)
# ---------------------------------------------------------------------------
class ApexBOSFredApp(latch_mod.GrabberStyleDemodMixin):
    """
    Standalone 450 MHz Railroad Telemetry SDR Demodulator & Decoder daemon:
      - EOT / HOT (AAR S-9152 1200-baud FFSK + BCH(63,45)): 457.9375 / 452.9375 MHz
      - DPU Ch 1-4 (V.23 1200 AFSK + 4800 FSK HDLC CRC-16): 452.925, 452.950, 457.925, 457.950 MHz
      - RLC Ch 1-5 (12.5 kHz narrowband FSK HDLC/burst): 452.93125, 452.94375, 452.95625, 452.96250, 452.96875 MHz
    Full Grabber-style TUI, commands, statistics, Auto-PPM, PID Auto-Tuner, and Chroma IQ tap.
    """

    APP_DISPLAY_NAME = "ApexBOSFred"
    FEC_STAGE_LABEL = "BCH / HDLC-CRC Decoded   "
    RECENT_HEADER_LABEL = " Recent Decoded 450 MHz Packets (EOT/HOT BCH(63,45) | DPU/RLC HDLC CRC-16):"

    def __init__(self, args):
        self.args = args
        self.locked_channels: Set[int] = set()
        self.channels = self._parse_channels(args, locked_out=self.locked_channels)
        self.stream_kind = "mock" if getattr(args, "mock", False) else grabber.resolve_sdr_stream_kind(args)
        self.soapy_args = str(getattr(args, "soapy_args", None) or "").strip()
        self.gain_db, self.gain_tenths = grabber.parse_gain_value(args.gain)
        self.ppm = int(args.ppm)
        self.bias_tee = bool(args.bias_tee)
        self.device = int(args.device)
        self.zmq_port = int(args.zmq_port)
        self.chroma_port = int(args.chroma_port)
        self.ctrl_port = int(args.ctrl_port)
        self.sample_format = "u8"
        self.bytes_per_sample = 2
        self._stop_ev = threading.Event()
        self.stream = None
        self.demods: Dict[int, Any] = {}
        self.freqs_by_ch: Dict[int, int] = {}
        self.center_freq = 457943750
        self.sample_rate = FRED_DEFAULT_SAMPLE_RATE

        self._init_grabber_ui_state(args)
        # Auto-PPM is disabled by default for Fred only (Latch & Grabber keep Auto-PPM ON by default)
        self.auto_ppm_enabled = bool(getattr(args, "auto_ppm", False)) and not bool(getattr(args, "no_auto_ppm", False))
        self._reconfigure_channels()

    @staticmethod
    def _parse_single_channel_static(raw_tok: str) -> Tuple[Optional[int], bool]:
        s = str(raw_tok or "").strip()
        if not s:
            return None, False
        is_lock = bool(re.search(r"[\.lLpP]+$", s))
        core = re.sub(r"[\.lLpP]+$", "", s).strip().lower()
        if core in FRED_CHANNEL_ALIASES:
            return FRED_CHANNEL_ALIASES[core], is_lock
        if core.isdigit():
            c = int(core)
            if c in FRED_CHANNELS:
                return c, is_lock
        return None, False

    def _parse_single_channel_token(self, tok: str) -> Tuple[Optional[int], bool]:
        return self._parse_single_channel_static(tok)

    @classmethod
    def _parse_channels(cls, args, locked_out: Optional[Set[int]] = None) -> List[int]:
        if getattr(args, "both", False) or getattr(args, "all_chans", False):
            return list(FRED_ALL_GROUP)
        if getattr(args, "hot", False) or getattr(args, "band_452", False):
            return list(FRED_452_GROUP)
        if getattr(args, "dpu", False):
            return list(FRED_DPU_GROUP)
        if getattr(args, "rlc", False):
            return list(FRED_RLC_GROUP)
        if getattr(args, "eot_only", False):
            return [1]
        chans_arg = getattr(args, "chans", None)
        if chans_arg:
            out: List[int] = []
            for tok in str(chans_arg).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                m_rng = re.match(r"^(\d+)\s*-\s*(\d+)([\.lLpP]*)$", tok)
                if m_rng:
                    a, b = int(m_rng.group(1)), int(m_rng.group(2))
                    is_lk = bool(m_rng.group(3))
                    for c in range(min(a, b), max(a, b) + 1):
                        if c in FRED_CHANNELS:
                            if c not in out:
                                out.append(c)
                            if is_lk and locked_out is not None:
                                locked_out.add(c)
                    continue
                ch, is_lk = cls._parse_single_channel_static(tok)
                if ch is not None:
                    if ch not in out:
                        out.append(ch)
                    if is_lk and locked_out is not None:
                        locked_out.add(ch)
            if out:
                return sorted(out)
        # Default: 457 MHz sub-band (EOT 457.9375 + DPU Ch 3 457.9250 + DPU Ch 4 457.9500 -> 25 kHz span, 1 RTL-SDR)
        return list(FRED_457_GROUP)

    def _channel_short_label(self, ch: int) -> str:
        if ch in FRED_CHANNELS:
            return FRED_CHANNELS[ch][1]
        return f"Ch{ch}"

    def _channel_proto_label(self, ch: int) -> str:
        return fred_channel_kind(ch).upper()

    def _format_recent_detail(self, pkt: Dict[str, Any]) -> str:
        proto = str(pkt.get("protocol") or "").lower()
        if proto == "eot":
            return f"#{pkt.get('unit_id', 0):05d} {pkt.get('pressure_psi', 0)}PSI {'MOV' if pkt.get('motion') else 'STP'} Batt:{pkt.get('battery_pct', 0)}%"
        if proto == "hot":
            return f"#{pkt.get('unit_id', 0):05d} {'EMERG' if pkt.get('emergency') else ('ARM' if pkt.get('armed') else 'STAT')}"
        if proto == "dpu":
            return f"Loco #{pkt.get('loco_id', 0)} {pkt.get('throttle', '')} {pkt.get('pressure_psi', 0)}PSI ({pkt.get('modulation', '')})"
        if proto == "rlc":
            return f"OCU:0x{int(pkt.get('unit_id', 0)):04X} {pkt.get('hex', '')[:16]}"
        return str(pkt.get("_scroll_ptc_") or pkt.get("hex", "")[:24])

    def _reconfigure_channels(self):
        is_rsp = (self.stream_kind == "rsptcp") or (self.stream_kind == "soapy" and "sdrplay" in self.soapy_args.lower())
        is_wideband_sdr = is_rsp or (self.stream_kind == "soapy") or (self.stream_kind == "mock")
        # For RTL-SDR (max usable span ~2.0 MHz), group channels by frequency proximity,
        # pick the largest cluster, and prefer the EOT (457 MHz) group on equal size!
        if not is_wideband_sdr:
            selected, skipped = latch_mod.select_channels_for_sdr_bandwidth(
                self.channels,
                fred_channel_frequency,
                max_span_hz=latch_mod.RTLSDR_MAX_SPAN_HZ,
                prefer_predicate=lambda c: fred_channel_frequency(c) >= 455000000,  # Prefer EOT / 457 MHz group on tie
            )
            if skipped:
                self.channels = selected
                sel_lbls = [self._channel_short_label(c) for c in selected]
                sk_lbls = [self._channel_short_label(c) for c in skipped]
                self.status_banner = f"RTL-SDR span limit: monitoring {sel_lbls} (skipped out-of-band: {sk_lbls})"
            else:
                self.channels = selected

        self.freqs_by_ch = {ch: fred_channel_frequency(ch) for ch in self.channels}
        freqs = list(self.freqs_by_ch.values())
        raw_cf = int(round(0.5 * (min(freqs) + max(freqs)))) if freqs else 457937500
        ch_step_hz = 6250 if any(fred_channel_kind(c) == "rlc" for c in self.channels) else 12500
        self.center_freq = grabber.avoid_channel_center_dc_offset(raw_cf, freqs, channel_spacing_hz=ch_step_hz)
        span = (max(freqs) - min(freqs)) if len(freqs) > 1 else 0
        req_rate = int(getattr(self.args, "rate", 0) or 0)
        if is_rsp:
            min_rate = grabber.DirectRspTcpSource.RSP_MIN_RATE
            needed = int(math.ceil((span + 200000) / 192000.0) * 192000)
            self.sample_rate = max(min_rate, needed, req_rate)
            if self.sample_rate % 192000 != 0:
                self.sample_rate = int(math.ceil(self.sample_rate / 192000.0) * 192000)
        else:
            # 960 kS/s (20 * 48 kHz) is the narrowest alias-free, full-sensitivity R820T/RTL2832U rate:
            # avoids the R820T's ~300-350 kHz minimum analog IF filter aliasing into 240 kS/s and
            # adds +6 dB (1 effective ADC bit) of digital decimation gain. (Users may still pass --rate 240000.)
            if span <= 400000:
                needed = FRED_DEFAULT_SAMPLE_RATE
            else:
                needed = max(960000, int(math.ceil((span + 200000) / 192000.0) * 192000))
            target_rate = req_rate if req_rate > 0 else needed
            if not is_wideband_sdr:
                target_rate = min(target_rate, latch_mod.RTLSDR_MAX_RATE_HZ)
                self.sample_rate = grabber.sanitize_rtlsdr_sample_rate(target_rate)
            else:
                self.sample_rate = grabber.sanitize_rtlsdr_sample_rate(target_rate)
        self._apply_hw_params(freq=self.center_freq)
        self._rebuild_demods()

    def _rebuild_demods(self):
        new_demods: Dict[int, Any] = {}
        for ch in self.channels:
            kind = fred_channel_kind(ch)
            f_hz = self.freqs_by_ch[ch]
            if kind in ("eot", "hot"):
                new_demods[ch] = FredChannelDemodulator(self.sample_rate, ch, f_hz, max_bch_errors=self.max_errors)
            elif kind == "dpu":
                new_demods[ch] = DpuChannelDemodulator(self.sample_rate, ch, f_hz)
            else:
                new_demods[ch] = RlcChannelDemodulator(self.sample_rate, ch, f_hz)
        self.demods = new_demods

    def get_chroma_state_dict(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": VERSION,
            "band": "fred",
            "protocol": "eot",
            "stream_kind": self.stream_kind,
            "center_freq": int(self.center_freq),
            "sample_rate": int(self.sample_rate),
            "base_sample_rate": int(self.sample_rate),
            "sample_format": self.sample_format,
            "wideband": len(self.channels) > 3 and (max(self.freqs_by_ch.values()) - min(self.freqs_by_ch.values())) > 1000000,
            "ppm": int(self.ppm),
            "gain_db": float(self.gain_db),
            "gain_tenths": int(self.gain_tenths),
            "bias_tee": bool(self.bias_tee),
            "auto_ppm": bool(self.auto_ppm_enabled),
            "channels": sorted(self.channels),
            "locked_channels": sorted(self.locked_channels),
            "worker_ports": [self.zmq_port],
            "chroma_port": self.chroma_port,
            "peak_rssi": round(float(self.peak_rssi), 2),
            "total_packets": int(self.total_packets),
        }

    def handle_control_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        cmd = str(req.get("cmd") or "").strip().lower()
        try:
            if cmd in ("get_state", "ping"):
                return self.get_chroma_state_dict()
            elif cmd == "set_ppm":
                self.handle_terminal_input(f"p {int(req['ppm'])}")
                return self.get_chroma_state_dict()
            elif cmd == "set_gain":
                self.handle_terminal_input(f"g {req['gain']}")
                return self.get_chroma_state_dict()
            elif cmd == "set_bias_tee":
                self.handle_terminal_input(f"b {1 if req['bias_tee'] else 0}")
                return self.get_chroma_state_dict()
            elif cmd == "set_auto_ppm":
                self.handle_terminal_input(f"autopm {'on' if req['enabled'] else 'off'}")
                return self.get_chroma_state_dict()
            elif cmd == "add_channel":
                ch = int(req["channel"])
                if ch not in FRED_CHANNELS:
                    return {"ok": False, "error": f"Invalid FRED channel {ch}"}
                self.handle_terminal_input(f"+ {ch}")
                return self.get_chroma_state_dict()
            elif cmd == "remove_channel":
                self.handle_terminal_input(f"- {int(req['channel'])}")
                return self.get_chroma_state_dict()
            elif cmd == "toggle_channel":
                ch = int(req["channel"])
                self.handle_terminal_input(f"- {ch}" if ch in self.channels else f"+ {ch}")
                return self.get_chroma_state_dict()
            elif cmd == "lock_channel":
                ch = int(req["channel"])
                self.handle_terminal_input(f"lock {ch}" if req.get("locked", True) else f"unlock {ch}")
                return self.get_chroma_state_dict()
            return {"ok": False, "error": f"Unknown command: {cmd}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _open_stream(self):
        a = self.args
        if self.stream_kind == "pipe":
            bin_p = grabber.detect_rtl_sdr_executable(a.rtl_sdr_bin)
            return grabber.SubprocessPipeSource(bin_p, self.center_freq, self.sample_rate, self.gain_db, self.ppm, self.device, bias_tee=self.bias_tee)
        if self.stream_kind == "tcp":
            return grabber.DirectRtlTcpSource(a.host, a.port, self.center_freq, self.sample_rate, self.gain_db, self.ppm, bias_tee=self.bias_tee)
        if self.stream_kind == "rtlsdr":
            return grabber.DirectRtlSdrSource(self.center_freq, self.sample_rate, self.gain_db, self.ppm, self.device, bias_tee=self.bias_tee)
        if self.stream_kind == "rsptcp":
            s = grabber.DirectRspTcpSource(
                a.host, a.port, self.center_freq, self.sample_rate, self.gain_db, self.ppm,
                bias_tee=self.bias_tee, extended_16bit=bool(a.rsp_extended),
                lna_state=int(a.rsp_lna_state or 0), antenna=a.rsp_antenna, notch=a.rsp_notch,
                sample_format_override=a.sample_format,
            )
            self.sample_rate = s.sample_rate
            self.sample_format = s.sample_format
            self.bytes_per_sample = s.bytes_per_sample
            self._rebuild_demods()
            return s
        if self.stream_kind == "soapy":
            s = grabber.DirectSoapySdrSource(
                self.center_freq, self.sample_rate, self.gain_db, self.ppm, self.device,
                bias_tee=self.bias_tee, soapy_args=self.soapy_args, lna_state=a.rsp_lna_state, antenna=a.rsp_antenna,
            )
            self.sample_rate = s.sample_rate
            self.sample_format = s.sample_format
            self.bytes_per_sample = s.bytes_per_sample
            self._rebuild_demods()
            return s
        if self.stream_kind == "stdin":
            if a.sample_format == "s16":
                self.sample_format = "s16"
                self.bytes_per_sample = 4
            return grabber.StandardInputStreamSource()
        return None

    def run(self):
        ctx = zmq.Context() if zmq is not None else None
        pub = xpub = rep = None
        chroma_subs = 0
        last_chroma_send = 0.0
        last_render = 0.0

        try:
            if ctx is not None:
                pub = ctx.socket(zmq.PUB)
                pub.setsockopt(zmq.LINGER, 0)
                pub.bind(f"tcp://*:{self.zmq_port}")
                if self.chroma_port > 0:
                    xpub = ctx.socket(zmq.XPUB)
                    xpub.setsockopt(zmq.SNDHWM, 2)
                    xpub.setsockopt(zmq.LINGER, 0)
                    xpub.bind(f"tcp://127.0.0.1:{self.chroma_port}")
                if self.ctrl_port > 0:
                    rep = ctx.socket(zmq.REP)
                    rep.setsockopt(zmq.LINGER, 0)
                    rep.bind(f"tcp://127.0.0.1:{self.ctrl_port}")

            if self.stream_kind != "mock":
                self.stream = self._open_stream()

            if not self.no_tui and sys.stdin and sys.stdin.isatty():
                self.cmd_thread = grabber.TerminalCommandListener(self.handle_terminal_input, self.shutdown_flag, app=self)
                self.cmd_thread.start()
            else:
                ch_desc = ", ".join(self._channel_short_label(c) for c in self.channels)
                print(
                    f"[*] ApexBOSFred ({VERSION}) | 450 MHz EOT/HOT/DPU/RLC | "
                    f"Mode: {self.stream_kind} ({self.sample_format}) | "
                    f"Center: {self.center_freq/1e6:.4f} MHz | Rate: {self.sample_rate/1e6:.3f} MS/s | "
                    f"[{ch_desc}] | ZMQ:{self.zmq_port}"
                )

            chunk_samps = max(16384, int(round(self.sample_rate * 0.12)))
            mock_idx = 0

            while not self._stop_ev.is_set():
                if rep is not None:
                    for _ in range(4):
                        try:
                            raw_req = rep.recv(flags=zmq.NOBLOCK)
                            resp = self.handle_control_request(json.loads(raw_req.decode("utf-8")))
                            rep.send(json.dumps(resp).encode("utf-8"), flags=zmq.NOBLOCK)
                        except Exception:
                            break

                if xpub is not None:
                    while True:
                        try:
                            ev = xpub.recv(flags=zmq.NOBLOCK)
                            if ev and ev[0] == 1:
                                chroma_subs += 1
                            elif ev and ev[0] == 0:
                                chroma_subs = max(0, chroma_subs - 1)
                        except Exception:
                            break

                if self.stream_kind == "mock":
                    ch_m = self.channels[mock_idx % len(self.channels)]
                    kind_m = fred_channel_kind(ch_m)
                    if kind_m == "eot":
                        d45 = pack_fred_data45(
                            41822 + (mock_idx % 5) * 111,
                            pressure_psi=88 + (mock_idx % 3),
                            motion=(mock_idx % 2 == 0),
                            marker_light=True,
                            battery_pct=94,
                            is_hot=False,
                        )
                        raw_chunk = generate_fred_ffsk_iq(
                            fred_bch_encode(d45),
                            sample_rate=self.sample_rate,
                            center_freq=self.center_freq,
                            channel_freq=self.freqs_by_ch[ch_m],
                            seed=457 + mock_idx,
                        )
                    elif kind_m == "hot":
                        d30 = pack_hot_data30(
                            41822 + (mock_idx % 5) * 111,
                            command=HOT_CMD_EMERGENCY if (mock_idx % 7 == 0) else HOT_CMD_STATUS_REQ,
                        )
                        raw_chunk = generate_hot_s5701_iq(
                            hot_bch30_encode(d30),
                            sample_rate=self.sample_rate,
                            center_freq=self.center_freq,
                            channel_freq=self.freqs_by_ch[ch_m],
                            seed=452 + mock_idx,
                        )
                    elif kind_m == "dpu":
                        dp = pack_dpu_payload(
                            loco_id=8412 + (mock_idx % 3),
                            msg_type=0x02 if ch_m in (5, 6) else 0x01,
                            throttle_notch=(mock_idx % 8) + 1,
                            brake_psi=90,
                            is_remote=(ch_m in (5, 6)),
                            seq=mock_idx % 16,
                        )
                        if mock_idx % 2 == 0:
                            raw_chunk = generate_dpu_v23_iq(dp, sample_rate=self.sample_rate, center_freq=self.center_freq, channel_freq=self.freqs_by_ch[ch_m], seed=452 + mock_idx)
                        else:
                            raw_chunk = generate_dpu_fsk_iq(dp, sample_rate=self.sample_rate, center_freq=self.center_freq, channel_freq=self.freqs_by_ch[ch_m], seed=452 + mock_idx)
                    else:
                        rlc_p = bytes([0xA1, 0x05 + (mock_idx % 4), 0x24, 0x80, mock_idx & 0xFF, 0x55])
                        raw_chunk = generate_dpu_fsk_iq(
                            rlc_p, sample_rate=self.sample_rate, center_freq=self.center_freq,
                            channel_freq=self.freqs_by_ch[ch_m], baud_rate=RLC_FSK_BAUD, fsk_dev_hz=1200.0, seed=453 + mock_idx,
                        )
                    mock_idx += 1
                    time.sleep(0.45)
                else:
                    need_bytes = chunk_samps * self.bytes_per_sample
                    raw_chunk = self.stream.read_exact_bytes(need_bytes)
                    if not raw_chunk or len(raw_chunk) < need_bytes:
                        time.sleep(0.01)
                        continue

                n_samps = len(raw_chunk) // self.bytes_per_sample
                self.reader_bytes_read += len(raw_chunk)
                self.reader_samples_read += n_samps
                self.reader_chunks_read += 1

                if self.sample_format == "s16":
                    arr = np.frombuffer(raw_chunk, dtype="<i2").astype(np.float32) * (127.5 / 32768.0)
                else:
                    arr = np.frombuffer(raw_chunk, dtype=np.uint8).astype(np.float32) - 127.5
                iq = arr[0::2] + 1j * arr[1::2]
                self.demod_samples_proc += len(iq)
                self.demod_chunks_proc += 1

                now_t = time.time()
                for ch, demod in list(self.demods.items()):
                    pkts, ch_rssi = demod.process_iq(iq, self.center_freq)
                    self._record_chunk_metrics(
                        ch, ch_rssi,
                        getattr(demod, "last_sync_count", len(pkts)),
                        getattr(demod, "last_fec_good", len(pkts)),
                        getattr(demod, "last_fec_bad", 0),
                        getattr(demod, "last_bursts", 0),
                        now_t,
                    )
                    for pkt in pkts:
                        if self._is_duplicate_packet(ch, pkt.get("hex", ""), now_t):
                            continue
                        self._record_unique_packet(pkt, now_t)
                        _, zmq_frames = build_fred_wire_frames(pkt)
                        if pub is not None:
                            try:
                                pub.send_multipart(zmq_frames, flags=zmq.NOBLOCK)
                            except Exception:
                                pass
                        if self.no_tui or not (sys.stdin and sys.stdin.isatty()):
                            print(f"[{time.strftime('%H:%M:%S')}] {pkt['_scroll_ptc_']} | RSSI:{pkt['rssi']:.1f} dBFS")

                if xpub is not None and chroma_subs > 0 and (now_t - last_chroma_send) >= 0.038:
                    last_chroma_send = now_t
                    max_b = 65536 * self.bytes_per_sample
                    iq_slice = raw_chunk[-max_b:] if len(raw_chunk) > max_b else raw_chunk
                    meta = self.get_chroma_state_dict()
                    meta["timestamp"] = now_t
                    try:
                        xpub.send_multipart([b"IQ", json.dumps(meta).encode("utf-8"), iq_slice], flags=zmq.NOBLOCK)
                    except Exception:
                        pass

                if not self.no_tui and (now_t - last_render) >= 0.35:
                    last_render = now_t
                    self.render_screen(now_t)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_ev.set()
            if self.stream:
                try:
                    self.stream.close()
                except Exception:
                    pass
            for s in (pub, xpub, rep):
                if s is not None:
                    try:
                        s.close(linger=0)
                    except Exception:
                        pass
            if ctx is not None:
                try:
                    ctx.term()
                except Exception:
                    pass


def build_cli_parser() -> argparse.ArgumentParser:
    cfg_ports = config_loader.get_aux_demod_ports().get("fred", {})
    def_dev = int(cfg_ports.get("device", 2))
    def_gain = str(cfg_ports.get("gain", "28.0") or "28.0").strip()
    def_ppm = int(cfg_ports.get("ppm", 0) or 0)
    def_zmq = int(cfg_ports.get("zmq_port", DEFAULT_FRED_ZMQ_PORT))
    def_chroma = int(cfg_ports.get("chroma_port", DEFAULT_FRED_CHROMA_PORT))
    def_ctrl = int(cfg_ports.get("ctrl_port", DEFAULT_FRED_CTRL_PORT))

    p = argparse.ArgumentParser(
        description=f"ApexBOSFred ({VERSION}) - Clean-Room 450 MHz EOT/HOT + DPU + RLC SDR Demodulator"
    )
    p.add_argument("--chans", default=None, help="Channels: mnemonics or numbers (e.g. HOT,DPU1,RLC1,4,7). Default: EOT,DPU3,DPU4")
    p.add_argument("--afc", action="store_true", default=False, help="Enable per-channel AFC at startup (default: OFF)")
    p.add_argument("--eot-only", action="store_true", help="Monitor EOT (457.9375 MHz) only")
    p.add_argument("--hot", "--452", dest="hot", action="store_true", help="Monitor 452 MHz sub-band: HOT + DPU1/2 + RLC1-5 (43.75 kHz span, 1 RTL-SDR)")
    p.add_argument("--dpu", action="store_true", help="Monitor all 4 DPU channels (452.925, 452.950, 457.925, 457.950 MHz; wideband SDR)")
    p.add_argument("--rlc", action="store_true", help="Monitor all 5 RLC channels (452.93125..452.96875 MHz; 1 RTL-SDR)")
    p.add_argument("--both", "--all", dest="both", action="store_true", help="Monitor ALL 11 channels (EOT+HOT+DPU1-4+RLC1-5; wideband SDR >= 6 MS/s)")
    p.add_argument("--device", type=int, default=def_dev, help=f"SDR device index (config/default: {def_dev}, leaving 0=Grabber, 1=Latch)")
    p.add_argument("--rate", type=int, default=0, help="SDR sample rate in S/s (0=auto: 960000 alias-free for 452/457 MHz; optional --rate 240000; auto-scaled >= 5.376M with --both)")
    p.add_argument("--gain", default=def_gain, help=f"Tuner gain in dB (0 or 'auto' for AGC, config/default: {def_gain})")
    p.add_argument("--ppm", type=int, default=def_ppm, help=f"Master oscillator PPM correction (config/default: {def_ppm})")
    p.add_argument("-b", "--bias-tee", dest="bias_tee", type=int, default=0, choices=[0, 1], help="Enable bias tee (default: 0)")
    p.add_argument("-E", "--max-errors", dest="max_errors", type=int, default=1, help="Max BCH(63,45) correctable bit errors (0..2, default: 1)")
    p.add_argument("--dedup-ttl", type=float, default=0.4, help="Duplicate packet suppression window in seconds (default: 0.4)")
    p.add_argument("--auto-tune", action="store_true", help="Enable Lazy PID Auto-Tuner at startup")
    p.add_argument("--auto-ppm", action="store_true", help="Enable automatic 7s PPM drift tracking (OFF by default for Fred)")
    p.add_argument("--no-auto-ppm", action="store_true", help="Disable automatic 7s PPM drift tracking (default for Fred)")
    p.add_argument("--no-tui", action="store_true", help="Run in plain console log mode instead of Grabber-style TUI")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI terminal colors")
    p.add_argument("--zmq-port", type=int, default=def_zmq, help=f"ZMQ PUB output port (default: {def_zmq})")
    p.add_argument("--chroma-port", type=int, default=def_chroma, help=f"Chroma XPUB IQ tap port (default: {def_chroma})")
    p.add_argument("--ctrl-port", type=int, default=def_ctrl, help=f"Chroma REP control port (default: {def_ctrl})")
    p.add_argument("--mock", action="store_true", help="Run with synthetic EOT/HOT/DPU/RLC RF bursts (no SDR needed)")
    # Shared SDR mode flags (identical to ApexBOSGrabber)
    p.add_argument("--pipe", action="store_true")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--rtl-tcp", action="store_true")
    p.add_argument("--rsp-tcp", "--rsptcp", dest="rsp_tcp", action="store_true")
    p.add_argument("--rsp-tcp-bin", default=None)
    p.add_argument("--rsp-16bit", "--rsp-extended", dest="rsp_extended", action="store_true")
    p.add_argument("--rsp-lna-state", type=int, default=0)
    p.add_argument("--rsp-antenna", default=None)
    p.add_argument("--rsp-notch", type=int, default=None)
    p.add_argument("--soapy", "--soapysdr", dest="soapy", action="store_true")
    p.add_argument("--soapy-args", default=None)
    p.add_argument("--rtlsdr", "--pyrtlsdr", dest="rtlsdr", action="store_true")
    p.add_argument("--rtl-sdr-bin", default=None)
    p.add_argument("--rtlsdr-dll-dir", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", "--rtl-port", dest="port", type=int, default=1234)
    p.add_argument("--sample-format", default="auto", choices=["auto", "u8", "s16"])
    p.add_argument("--sdr-mode", default="auto", choices=["auto", "pipe", "tcp", "rtlsdr", "rsptcp", "soapy", "stdin"])
    return p


def main():
    args = build_cli_parser().parse_args()
    app = ApexBOSFredApp(args)
    app.run()


if __name__ == "__main__":
    main()
