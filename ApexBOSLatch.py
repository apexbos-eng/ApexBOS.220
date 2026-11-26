#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApexBOSLatch.py - Clean-Room 900 MHz ATCS (AAR Spec 200) SDR Demodulator & Decoder
Copyright (c) 2026 AB. Licensed under the MIT License.

Clean-room Python/NumPy implementation of the North American 900 MHz ATCS
4800 bps 2-FSK + BCH(48, 32) / BCH(63, 47) telemetry physical & framing layers.
Designed to run on a dedicated SDR alongside ApexBOSGrabber (220 MHz PTC) and
ApexBOSFred (450 MHz EOT/HOT), publishing unified telemetry into:
  - ApexBOSFlux / DispatcherEngine / Layout Packet Scroller (ZMQ PUB :18201)
  - ApexBOSSpy protocol inspector
  - ApexBOSChroma RF spectrum & waterfall (gated XPUB :18299, REP control :18298)

Disclaimer: Amateur railfan / RF research reference tool ONLY. Not for operational railway use.
"""

import argparse
import collections
import json
import math
import os
import queue
import re
import signal
import socket
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
import config_loader
from version import VERSION, __version__

# ---------------------------------------------------------------------------
# ATCS 900 MHz Band Constants (AAR Specification 200, 12.5 kHz Spacing)
# ---------------------------------------------------------------------------
DEFAULT_LATCH_ZMQ_PORT = 18201
DEFAULT_LATCH_CHROMA_PORT = 18299
DEFAULT_LATCH_CTRL_PORT = 18298

ATCS_BAUD_RATE = 4800
ATCS_DEMOD_RATE = 48000          # Exact 10 samples per 4800 bps symbol
ATCS_SPS = ATCS_DEMOD_RATE // ATCS_BAUD_RATE  # 10

# Pre-FCC-Restructure 900 MHz ATCS Channels (AAR Spec 200):
#   F-1: 935.8875 MHz (BCP) / 896.8875 MHz (MCP)
#   F-2: 935.9375 MHz (BCP) / 896.9375 MHz (MCP)
#   F-3: 935.9875 MHz (BCP) / 896.9875 MHz (MCP)
#   F-4: 936.8875 MHz (BCP) / 897.8875 MHz (MCP)
#   F-5: 936.9375 MHz (BCP) / 897.9375 MHz (MCP)
#   F-6: 936.9875 MHz (BCP) / 897.9875 MHz (MCP)
ATCS_BCP_CHANNELS: Dict[int, Tuple[int, str]] = {
    1: (935887500, "F-1 BCP"),
    2: (935937500, "F-2 BCP"),
    3: (935987500, "F-3 BCP"),
    4: (936887500, "F-4 BCP"),
    5: (936937500, "F-5 BCP"),
    6: (936987500, "F-6 BCP"),
}

ATCS_MCP_CHANNELS: Dict[int, Tuple[int, str]] = {
    11: (896887500, "F-1 MCP"),
    12: (896937500, "F-2 MCP"),
    13: (896987500, "F-3 MCP"),
    14: (897887500, "F-4 MCP"),
    15: (897937500, "F-5 MCP"),
    16: (897987500, "F-6 MCP"),
}

ATCS_ALL_CHANNELS: Dict[int, Tuple[int, str]] = {**ATCS_BCP_CHANNELS, **ATCS_MCP_CHANNELS}

ATCS_CHANNEL_ALIASES: Dict[str, int] = {
    "f-1": 1, "f1": 1, "f-2": 2, "f2": 2, "f-3": 3, "f3": 3,
    "f-4": 4, "f4": 4, "f-5": 5, "f5": 5, "f-6": 6, "f6": 6,
    "bcp1": 1, "bcp-1": 1, "bcp2": 2, "bcp-2": 2, "bcp3": 3, "bcp-3": 3,
    "bcp4": 4, "bcp-4": 4, "bcp5": 5, "bcp-5": 5, "bcp6": 6, "bcp-6": 6,
    "mcp1": 11, "mcp-1": 11, "mcp2": 12, "mcp-2": 12, "mcp3": 13, "mcp-3": 13,
    "mcp4": 14, "mcp-4": 14, "mcp5": 15, "mcp-5": 15, "mcp6": 16, "mcp-6": 16,
    "f-1m": 11, "f-2m": 12, "f-3m": 13, "f-4m": 14, "f-5m": 15, "f-6m": 16,
}

# Max usable RF span for standard RTL-SDR dongles (<= 2.4 MS/s -> ~2.0 MHz clean passband)
RTLSDR_MAX_SPAN_HZ = 2000000
RTLSDR_MAX_RATE_HZ = 2304000


def select_channels_for_sdr_bandwidth(
    channels: List[int],
    freq_lookup_fn,
    max_span_hz: int = RTLSDR_MAX_SPAN_HZ,
    prefer_predicate=None,
) -> Tuple[List[int], List[int]]:
    """
    Groups channels by frequency proximity so every selected channel fits inside `max_span_hz`.
    Picks the largest cluster; on a tie, prefers the cluster containing more channels matching
    `prefer_predicate(ch)` (e.g., MCP for ATCS Latch, EOT/457 MHz for Fred).
    Returns (selected_channels_sorted, skipped_channels_sorted).
    """
    unique = sorted(set(int(c) for c in channels))
    if len(unique) <= 1:
        return unique, []

    by_freq = sorted(unique, key=lambda c: freq_lookup_fn(c))
    if (freq_lookup_fn(by_freq[-1]) - freq_lookup_fn(by_freq[0])) <= max_span_hz:
        return unique, []

    # Sliding-window maximal frequency clusters within max_span_hz
    clusters: List[List[int]] = []
    for i in range(len(by_freq)):
        f0 = freq_lookup_fn(by_freq[i])
        grp = [by_freq[j] for j in range(i, len(by_freq)) if (freq_lookup_fn(by_freq[j]) - f0) <= max_span_hz]
        if grp:
            clusters.append(grp)

    def _score(grp: List[int]) -> Tuple[int, int, int]:
        pref_cnt = sum(1 for c in grp if prefer_predicate(c)) if prefer_predicate else 0
        # Larger group wins -> more preferred-side channels wins -> tighter span wins
        span = freq_lookup_fn(grp[-1]) - freq_lookup_fn(grp[0])
        return (len(grp), pref_cnt, -span)

    best = sorted(max(clusters, key=_score))
    best_set = set(best)
    skipped = sorted(c for c in unique if c not in best_set)
    return best, skipped

# Standard ATCS Spec 200 16-bit frame synchronization word
ATCS_SYNC_WORD = 0x168A
ATCS_SYNC_BITS = np.array([(ATCS_SYNC_WORD >> (15 - i)) & 1 for i in range(16)], dtype=np.int8)
ATCS_SYNC_BIPOLAR = (ATCS_SYNC_BITS * 2 - 1).astype(np.float32)

# Clean-room binary BCH(63, 47) / shortened BCH(48, 32) generator polynomial (degree 16, t=2)
# g(x) = (x^6 + x + 1)(x^6 + x^4 + x^2 + x + 1)(x^4 + x + 1)?
# Standard primitive BCH(63, 45/47) over GF(2^6):
# m1(x) = x^6 + x + 1 (0x43), m3(x) = x^6 + x^4 + x^2 + x + 1 (0x57), (x+1) -> degree 13 or 16.
# Here we use a degree-16 t>=2 systematic BCH/CRC-16 generator over GF(2) (0x11021 * ... -> 0x18005 / 0x11021)
# Specifically: g(x) = (x^6 + x + 1)(x^6 + x^4 + x^2 + x + 1)(x^4 + x^3 + 1) -> degree 16 (0x1B93F),
# which has minimum distance d_min >= 5 over 48-bit shortened codewords (full t=2 error correction!).
ATCS_BCH_DEGREE = 16
ATCS_BCH_POLY = 0x1B93F  # Degree-16 polynomial with d_min >= 5 for 48-bit (32 data + 16 parity) blocks
ATCS_CODEWORD_BITS = 48
ATCS_DATA_BITS = 32

ATCS_MSG_TYPES = {
    0x01: "IND",     # Wayside Indication (tracks/switches/signals)
    0x02: "CTL",     # Control Command (dispatcher -> wayside)
    0x03: "POLL",    # Base Poll / Roll Call
    0x04: "ACK",     # Link Acknowledgement
    0x05: "BEACON",  # Base Station Beacon / ID
    0x06: "LOCO",    # Mobile / Locomotive Report
}


def atcs_channel_frequency(ch: int) -> int:
    """Returns center frequency in Hz for ATCS channel ID (1-6 BCP, 11-16 MCP)."""
    if ch in ATCS_ALL_CHANNELS:
        return ATCS_ALL_CHANNELS[ch][0]
    raise ValueError(f"Unknown ATCS channel {ch} (valid: 1-6 BCP, 11-16 MCP)")


def atcs_channel_direction(ch: int) -> str:
    return "MCP" if ch >= 11 else "BCP"


# ---------------------------------------------------------------------------
# Clean-Room BCH(48, 32) Systematic Encoder & t=2 Syndrome Error Corrector
# ---------------------------------------------------------------------------
def _poly_rem(val: int, poly: int, deg: int) -> int:
    """Computes remainder of binary polynomial division val(x) mod poly(x)."""
    rem = int(val)
    top = 1 << deg
    # Shift poly down from highest active bit
    bit_len = rem.bit_length()
    for shift in range(bit_len - 1, deg - 1, -1):
        if (rem >> shift) & 1:
            rem ^= (poly << (shift - deg))
    return rem & (top - 1)


def atcs_bch_encode(data32: int) -> int:
    """Encodes a 32-bit payload into a systematic 48-bit codeword (32 data MSB + 16 BCH parity LSB)."""
    d = int(data32) & 0xFFFFFFFF
    parity = _poly_rem(d << ATCS_BCH_DEGREE, ATCS_BCH_POLY, ATCS_BCH_DEGREE)
    return (d << ATCS_BCH_DEGREE) | parity


def _build_bch48_syndrome_table() -> Dict[int, Tuple[int, int]]:
    """Precomputes 1-bit and 2-bit error masks for 48-bit BCH codewords (1,176 entries -> O(1) lookup)."""
    table: Dict[int, Tuple[int, int]] = {0: (0, 0)}
    # 1-bit errors (always take priority)
    for b1 in range(ATCS_CODEWORD_BITS):
        mask = 1 << b1
        syn = _poly_rem(mask, ATCS_BCH_POLY, ATCS_BCH_DEGREE)
        if syn not in table:
            table[syn] = (mask, 1)
    # 2-bit errors
    for b1 in range(ATCS_CODEWORD_BITS):
        m1 = 1 << b1
        for b2 in range(b1 + 1, ATCS_CODEWORD_BITS):
            mask = m1 | (1 << b2)
            syn = _poly_rem(mask, ATCS_BCH_POLY, ATCS_BCH_DEGREE)
            if syn not in table:
                table[syn] = (mask, 2)
    return table


_ATCS_SYNDROME_TABLE = _build_bch48_syndrome_table()


def atcs_bch_decode(codeword48: int, max_errors: int = 2) -> Tuple[Optional[int], int]:
    """
    Decodes a 48-bit BCH codeword correcting up to `max_errors` (0, 1, or 2) bit errors.
    Returns (data32, num_errors_corrected) or (None, -1) if uncorrectable.
    """
    cw = int(codeword48) & ((1 << ATCS_CODEWORD_BITS) - 1)
    syn = _poly_rem(cw, ATCS_BCH_POLY, ATCS_BCH_DEGREE)
    entry = _ATCS_SYNDROME_TABLE.get(syn)
    if entry is None:
        return None, -1
    err_mask, n_err = entry
    if n_err > max_errors:
        return None, -1
    corrected = cw ^ err_mask
    return (corrected >> ATCS_BCH_DEGREE) & 0xFFFFFFFF, n_err


# ---------------------------------------------------------------------------
# ATCS Multi-Block Frame Packing & Parsing (Clean-Room Spec 200 Layout)
# ---------------------------------------------------------------------------
def pack_atcs_frame_words(
    mcp_addr: str,
    msg_type: int = 0x01,
    indication_bytes: bytes = b"\x00\x00\x00\x00",
    seq: int = 0,
    is_uplink: bool = False,
) -> List[int]:
    """
    Packs an ATCS wayside/base telemetry message into 3 systematic 48-bit BCH codewords (12 payload bytes):
      Word 0 (32b): [dir:1][msg_type:4][seq:3][rr_code:10][site_hi:14]
      Word 1 (32b): [site_lo:13][sub_code:10][ind_len:4][rsv:5]
      Word 2 (32b): [indication_or_control_bytes:32]
    """
    digits = "".join(c for c in str(mcp_addr) if c.isdigit()).zfill(14)[:14]
    rr_code = int(digits[:3]) & 0x3FF
    site_num = int(digits[3:11]) & 0x7FFFFFF
    sub_code = int(digits[11:14]) & 0x3FF

    w0 = (
        ((1 if is_uplink else 0) << 31)
        | ((int(msg_type) & 0x0F) << 27)
        | ((int(seq) & 0x07) << 24)
        | (rr_code << 14)
        | ((site_num >> 13) & 0x3FFF)
    )
    w1 = ((site_num & 0x1FFF) << 19) | ((sub_code & 0x3FF) << 9) | ((len(indication_bytes) & 0x0F) << 5)
    ind4 = (bytes(indication_bytes) + b"\x00\x00\x00\x00")[:4]
    w2 = int.from_bytes(ind4, "big")
    return [atcs_bch_encode(w0), atcs_bch_encode(w1), atcs_bch_encode(w2)]


def parse_atcs_frame_words(words32: List[int]) -> Dict[str, Any]:
    """Parses 3 decoded 32-bit ATCS data words into structured telemetry fields."""
    if len(words32) < 3:
        return {}
    w0, w1, w2 = words32[0], words32[1], words32[2]
    is_uplink = bool((w0 >> 31) & 1)
    msg_type = (w0 >> 27) & 0x0F
    seq = (w0 >> 24) & 0x07
    rr_code = (w0 >> 14) & 0x3FF
    site_hi = w0 & 0x3FFF
    site_lo = (w1 >> 19) & 0x1FFF
    site_num = (site_hi << 13) | site_lo
    sub_code = (w1 >> 9) & 0x3FF
    ind_bytes = int(w2).to_bytes(4, "big")

    mcp_addr = f"{rr_code % 1000:03d}{site_num % 100000000:08d}{sub_code % 1000:03d}"
    type_name = ATCS_MSG_TYPES.get(msg_type, f"T{msg_type:X}")
    raw_payload = int(w0).to_bytes(4, "big") + int(w1).to_bytes(4, "big") + ind_bytes
    return {
        "protocol": "atcs",
        "direction": "MCP" if is_uplink else "BCP",
        "msg_type": msg_type,
        "type_name": f"ATCS_{type_name}",
        "sequence": seq,
        "rr_code": f"{rr_code % 1000:03d}",
        "mcp_addr": mcp_addr,
        "node_id": int(mcp_addr),
        "indication_hex": ind_bytes.hex().upper(),
        "indication_bits": f"{w2:032b}",
        "hex": raw_payload.hex().upper(),
    }


# ---------------------------------------------------------------------------
# ATCS Rules Decoder (config/atcs_rules.json + Default Spec 200 Fallback)
# ---------------------------------------------------------------------------
def _default_atcs_bit_tokens(ind_hex: str, direction: str = "MCP", msg_type: int = 0x01) -> Tuple[List[str], Dict[int, str]]:
    """
    Derives standard ATCS / CTC code-line bit tokens (K11..K48 for indications,
    C11..C48 for controls) from the 32-bit indication/control word.
    Returns (active_tokens, bit_index_to_token_map).
    """
    prefix = "C" if (str(direction).upper() == "BCP" or int(msg_type) == 0x02) else "K"
    clean = "".join(c for c in str(ind_hex or "") if c in "0123456789abcdefABCDEF")
    if len(clean) % 2 == 1:
        clean = "0" + clean
    try:
        raw = bytes.fromhex(clean) if clean else b""
    except ValueError:
        raw = b""
    active: List[str] = []
    bit_map: Dict[int, str] = {}
    for b_idx, bval in enumerate(raw[:8]):
        for bit_in_byte in range(8):
            flat_idx = b_idx * 8 + bit_in_byte
            tok = f"{prefix}{b_idx + 1}{bit_in_byte + 1}"
            bit_map[flat_idx] = tok
            if (bval >> (7 - bit_in_byte)) & 1:
                active.append(tok)
    return active, bit_map


def _infer_atcs_state_hints(active_tokens: List[str], decoded_labels: List[str], explicit_hints: Dict[str, Any]) -> Dict[str, Any]:
    """Infers track/switch/signal state hints from semantic ATCS labels or explicit state_map entries."""
    hints: Dict[str, Any] = dict(explicit_hints or {})
    all_lbls = [str(x).lower() for x in decoded_labels]
    for lbl in all_lbls:
        if "occupied" not in hints and ("track_ind" in lbl or "approach_ind" in lbl or "occ" in lbl):
            hints["occupied"] = True
        if "switch" not in hints:
            if "_rw_" in lbl or "reverse" in lbl:
                hints["switch"] = "reverse"
            elif "_nw_" in lbl or "normal" in lbl:
                hints["switch"] = "normal"
        if "signal" not in hints:
            if "_go_" in lbl or "clear" in lbl or "green" in lbl:
                hints["signal"] = "green"
            elif "stop" in lbl or "red" in lbl:
                hints["signal"] = "red"
    return hints


class AtcsRulesDecoder:
    """
    Decodes ATCS packets using `config/atcs_rules.json` when present (auto-reloading on file changes),
    and falls back cleanly to standard ATCS Spec 200 code-line decoding (`K11..K48` / `C11..C48`
    and 14-digit MCP address as `ctc_id`) when `atcs_rules.json` is missing.

    Supports both:
      1. Structured ATCS JSON dict (`stations`, `classes`, `bit_map`, `rules`)
      2. ApexBOS `PacketParser` JSON rule list (`[{"type": ..., "name": ...}, ...]`)
    """

    def __init__(self, config_dir: Optional[str] = None, rules_path: Optional[str] = None):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_dir = config_dir
        self.explicit_rules_path = rules_path
        self._lock = threading.RLock()
        self._loaded_path: Optional[str] = None
        self._loaded_mtime: Optional[float] = None
        self._last_check_ts: float = 0.0
        self._rules_obj: Any = None
        self._packet_parser: Any = None
        self.reload(force=True)

    def _candidate_paths(self) -> List[str]:
        cands: List[str] = []
        if self.explicit_rules_path:
            cands.append(self.explicit_rules_path)
        if self.config_dir:
            cands.append(os.path.join(self.config_dir, "atcs_rules.json"))
        cands.append(os.path.join(self.base_dir, "config", "atcs_rules.json"))
        cands.append(os.path.join(os.getcwd(), "config", "atcs_rules.json"))
        seen: Set[str] = set()
        out: List[str] = []
        for p in cands:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                out.append(ap)
        return out

    def reload(self, force: bool = False) -> bool:
        """Checks for `atcs_rules.json` creation/modification/removal and reloads if changed."""
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
        Enriches an ATCS packet dict with decoded CTC ID, CTC tokens, station name,
        state hints, and formatted `_scroll_ptc_` line using `atcs_rules.json` if present
        or default Spec 200 decoding if absent.
        """
        self.reload(force=False)
        out = dict(pkt or {})
        mcp_addr = str(out.get("mcp_addr") or out.get("node_id") or out.get("_node_id_") or "").strip()
        direction = str(out.get("direction") or "MCP").upper()
        msg_type = int(out.get("msg_type", 0x01) or 0x01)
        type_name = str(out.get("type_name") or f"ATCS_{ATCS_MSG_TYPES.get(msg_type, 'IND')}")
        ind_hex = str(out.get("indication_hex") or "")
        ch = out.get("channel", 0)
        bch_err = out.get("bch_errors", 0)

        # 1. Default known Spec 200 decoding (always available as fallback)
        active_tokens, default_bit_map = _default_atcs_bit_tokens(ind_hex, direction=direction, msg_type=msg_type)
        resolved_ctc_id = str(out.get("_ctc_id_") or out.get("ctc_id") or mcp_addr)
        station_name = ""
        decoded_labels: List[str] = []
        explicit_hints: Dict[str, Any] = {}
        custom_scroll: Optional[str] = None
        source = "default"

        with self._lock:
            rules_obj = self._rules_obj
            pkt_parser = self._packet_parser

        # 2. Apply config/atcs_rules.json when present
        if isinstance(rules_obj, list) and pkt_parser is not None:
            raw_hex = str(out.get("hex") or "")
            try:
                raw_bytes = bytes.fromhex(raw_hex) if raw_hex else b""
            except ValueError:
                raw_bytes = b""
            if raw_bytes:
                try:
                    res = pkt_parser.parse(raw_bytes)
                    if isinstance(res, dict) and res.get("is_valid"):
                        pdata = res.get("data", res)
                        if isinstance(pdata, dict):
                            for k, v in pdata.items():
                                if k not in ("protocol", "channel", "rssi", "freq_hz"):
                                    out[k] = v
                            if pdata.get("_ctc_id_") or pdata.get("ctc_id"):
                                resolved_ctc_id = str(pdata.get("_ctc_id_") or pdata.get("ctc_id"))
                            if isinstance(pdata.get("_ctc_tokens_"), (list, tuple, set)):
                                active_tokens = [str(t) for t in pdata["_ctc_tokens_"]]
                            if pdata.get("_scroll_ptc_"):
                                custom_scroll = str(pdata["_scroll_ptc_"])
                            source = "atcs_rules.json"
                except Exception:
                    pass
        elif isinstance(rules_obj, dict):
            source = "atcs_rules.json"
            classes_map = rules_obj.get("classes") if isinstance(rules_obj.get("classes"), dict) else {}
            stations_map = rules_obj.get("stations") if isinstance(rules_obj.get("stations"), dict) else {}
            global_bit_map = rules_obj.get("bit_map") if isinstance(rules_obj.get("bit_map"), dict) else {}
            rules_list = rules_obj.get("rules") if isinstance(rules_obj.get("rules"), list) else []

            # Match station entry (exact mcp_addr, stripped leading zeros, or wildcard "*")
            st_cfg = (
                stations_map.get(mcp_addr)
                or stations_map.get(mcp_addr.lstrip("0"))
                or stations_map.get("*")
            )
            matched_rule: Optional[Dict[str, Any]] = None
            for r in rules_list:
                if not isinstance(r, dict):
                    continue
                r_mcp = str(r.get("mcp_addr") or "").strip()
                r_dir = str(r.get("direction") or "").strip().upper()
                r_type = r.get("msg_type")
                if r_mcp and r_mcp not in ("*", mcp_addr, mcp_addr.lstrip("0")):
                    continue
                if r_dir and r_dir != direction:
                    continue
                if r_type is not None and int(r_type) != msg_type:
                    continue
                matched_rule = r
                break

            # Merge bit index overrides (global bit_map -> station -> matched_rule)
            eff_bit_map: Dict[int, str] = dict(default_bit_map)
            dir_gmap = global_bit_map.get(direction) or global_bit_map.get(direction.lower())
            if isinstance(dir_gmap, dict):
                for k, v in dir_gmap.items():
                    if str(k).isdigit():
                        eff_bit_map[int(k)] = str(v)

            bit_names: Dict[str, str] = {}
            state_map: Dict[str, Any] = {}
            if isinstance(rules_obj.get("state_map"), dict):
                state_map.update(rules_obj["state_map"])

            def _apply_cfg_block(cfg_blk: Optional[Dict[str, Any]]):
                nonlocal station_name, resolved_ctc_id, custom_scroll
                if not isinstance(cfg_blk, dict):
                    return
                if cfg_blk.get("name") or cfg_blk.get("mcp"):
                    station_name = str(cfg_blk.get("name") or cfg_blk.get("mcp"))
                if cfg_blk.get("ctc_id"):
                    resolved_ctc_id = str(cfg_blk["ctc_id"])
                cls_ref = str(cfg_blk.get("class") or cfg_blk.get("signals") or "").lstrip("@").strip()
                if cls_ref and isinstance(classes_map.get(cls_ref), dict):
                    for tk, lbl in classes_map[cls_ref].items():
                        bit_names[str(tk).upper()] = str(lbl)
                sub_bits = (
                    cfg_blk.get("bits")
                    or (cfg_blk.get("controls") if direction == "BCP" else cfg_blk.get("indications"))
                    or cfg_blk.get("indications")
                )
                if isinstance(sub_bits, dict):
                    for k, v in sub_bits.items():
                        if str(k).isdigit():
                            eff_bit_map[int(k)] = str(v)
                if isinstance(cfg_blk.get("bit_names"), dict):
                    for tk, lbl in cfg_blk["bit_names"].items():
                        bit_names[str(tk).upper()] = str(lbl)
                if isinstance(cfg_blk.get("state_map"), dict):
                    state_map.update(cfg_blk["state_map"])
                if cfg_blk.get("scroll_format"):
                    custom_scroll = str(cfg_blk["scroll_format"])

            _apply_cfg_block(st_cfg)
            _apply_cfg_block(matched_rule)

            # Recompute active tokens using effective bit map
            clean_hex = "".join(c for c in ind_hex if c in "0123456789abcdefABCDEF")
            if len(clean_hex) % 2 == 1:
                clean_hex = "0" + clean_hex
            try:
                ind_raw = bytes.fromhex(clean_hex) if clean_hex else b""
            except ValueError:
                ind_raw = b""

            mapped_tokens: List[str] = []
            for b_idx, bval in enumerate(ind_raw[:8]):
                for bit_in_byte in range(8):
                    if (bval >> (7 - bit_in_byte)) & 1:
                        flat_idx = b_idx * 8 + bit_in_byte
                        tok = eff_bit_map.get(flat_idx)
                        if tok and tok not in mapped_tokens:
                            mapped_tokens.append(tok)
            if isinstance((matched_rule or {}).get("tokens"), list):
                for t in matched_rule["tokens"]:
                    if str(t) not in mapped_tokens:
                        mapped_tokens.append(str(t))
            active_tokens = mapped_tokens

            for tok in active_tokens:
                lbl = bit_names.get(tok.upper())
                if lbl and lbl not in decoded_labels:
                    decoded_labels.append(lbl)
                st_hint = state_map.get(tok) or state_map.get(tok.upper())
                if isinstance(st_hint, dict):
                    explicit_hints.update(st_hint)

        state_hints = _infer_atcs_state_hints(active_tokens, decoded_labels, explicit_hints)

        # Combine token list (preserve order, deduplicate) for evaluate_ctc_rules()
        combined_tokens: List[str] = []
        for t in list(active_tokens) + list(decoded_labels):
            if t and t not in combined_tokens:
                combined_tokens.append(t)

        # Build human-readable summary for Spy & Packet Scroller
        tok_pairs: List[str] = []
        for idx_t, tok in enumerate(active_tokens[:8]):
            tok_pairs.append(tok)
        tok_str = ",".join(tok_pairs) if tok_pairs else "NONE"
        lbl_str = f" ({', '.join(decoded_labels[:4])})" if decoded_labels else ""
        short_type = type_name.replace("ATCS_", "")
        site_hdr = f"{station_name} ({resolved_ctc_id})" if station_name else f"MCP {resolved_ctc_id or mcp_addr}"
        if custom_scroll:
            try:
                scroll_line = custom_scroll.format(
                    channel=ch, direction=direction, mcp_addr=mcp_addr,
                    ctc_id=resolved_ctc_id, station=station_name or mcp_addr,
                    type=short_type, tokens=tok_str, labels=", ".join(decoded_labels),
                    indication_hex=ind_hex, bch_errors=bch_err,
                )
            except Exception:
                scroll_line = f"[ATCS {direction} Ch{ch}] {site_hdr} {short_type} [{tok_str}]{lbl_str}"
        else:
            scroll_line = (
                f"[ATCS {direction} Ch{ch}] {site_hdr} {short_type} "
                f"[{tok_str}]{lbl_str} IND:{ind_hex} (BCH:{bch_err})"
            )

        out["ctc_id"] = resolved_ctc_id
        out["_ctc_id_"] = resolved_ctc_id
        out["ctc_tokens"] = list(active_tokens)
        out["_ctc_tokens_"] = combined_tokens
        out["atcs_station"] = station_name
        out["atcs_decoded_labels"] = decoded_labels
        out["atcs_state_hints"] = state_hints
        out["_atcs_rules_source_"] = source
        out["_scroll_ptc_"] = scroll_line
        if "_node_id_" not in out and mcp_addr.isdigit():
            out["_node_id_"] = int(mcp_addr)
        if "_type_" not in out:
            out["_type_"] = type_name
        return out


_SHARED_ATCS_DECODER: Optional[AtcsRulesDecoder] = None


def decode_atcs_packet(pkt: Dict[str, Any], config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Convenience wrapper using a shared auto-reloading AtcsRulesDecoder instance."""
    global _SHARED_ATCS_DECODER
    if config_dir:
        return AtcsRulesDecoder(config_dir=config_dir).decode(pkt)
    if _SHARED_ATCS_DECODER is None:
        _SHARED_ATCS_DECODER = AtcsRulesDecoder()
    return _SHARED_ATCS_DECODER.decode(pkt)


# ---------------------------------------------------------------------------
# Clean-Room 4800 bps 2-FSK RF Modulator (for --mock & Unit Testing)
# ---------------------------------------------------------------------------
def generate_atcs_fsk_iq(
    words48: List[int],
    sample_rate: int = 1920000,
    center_freq: int = 936437500,
    channel_freq: int = 935887500,
    fsk_dev_hz: float = 2400.0,
    amp: float = 45.0,
    noise_std: float = 2.5,
    pad_samples: int = 8000,
    sample_format: str = "u8",
    seed: int = 900,
) -> bytes:
    """
    Synthesizes realistic 900 MHz ATCS 4800 bps 2-FSK RF IQ bytes (u8 or s16)
    containing alternating preamble + 16-bit sync word + BCH(48, 32) codewords.
    """
    bits: List[int] = [1, 0] * 16  # 32-bit clock recovery preamble
    for i in range(16):
        bits.append((ATCS_SYNC_WORD >> (15 - i)) & 1)
    for cw in words48:
        for i in range(ATCS_CODEWORD_BITS):
            bits.append((cw >> (ATCS_CODEWORD_BITS - 1 - i)) & 1)
    bits.extend([0, 1, 0, 1])

    sps_rf = float(sample_rate) / float(ATCS_BAUD_RATE)
    n_burst = int(round(len(bits) * sps_rf))
    n_total = pad_samples + n_burst + pad_samples

    rng = np.random.default_rng(seed)
    sig = (rng.normal(0.0, noise_std, n_total) + 1j * rng.normal(0.0, noise_std, n_total)).astype(np.complex64)

    # Instantaneous frequency trajectory for burst
    bit_idx = np.minimum((np.arange(n_burst) / sps_rf).astype(np.int32), len(bits) - 1)
    sym = np.array(bits, dtype=np.float64)[bit_idx] * 2.0 - 1.0  # -1 or +1
    f_offset = float(channel_freq - center_freq)
    inst_f = f_offset + sym * float(fsk_dev_hz)
    phase = 2.0 * math.pi * np.cumsum(inst_f) / float(sample_rate)
    burst = (amp * np.exp(1j * phase)).astype(np.complex64)
    sig[pad_samples:pad_samples + n_burst] += burst

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
# Clean-Room ATCS 900 MHz Channelizer & 4800 bps 2-FSK Demodulator
# ---------------------------------------------------------------------------
class AtcsChannelDemodulator:
    """
    Stateful per-channel 900 MHz ATCS 4800 bps 2-FSK demodulator:
      1. Mixes wideband IQ to baseband and decimates to 48,000 S/s (10 sps).
      2. Quadrature FM discriminator with DC/AFC centering.
      3. Multi-phase matched-filter symbol slicing (both polarities).
      4. 16-bit sync word correlation + 3-block BCH(48, 32) FEC decoding.
    """

    def __init__(self, input_rate: int, channel_id: int, freq_hz: int, max_bch_errors: int = 2):
        self.input_rate = int(input_rate)
        self.channel_id = int(channel_id)
        self.freq_hz = int(freq_hz)
        self.max_bch_errors = int(max_bch_errors)

        self.dec_factor = max(1, int(round(self.input_rate / float(ATCS_DEMOD_RATE))))
        self.actual_demod_rate = float(self.input_rate) / float(self.dec_factor)
        self.sps = max(2, int(round(self.actual_demod_rate / float(ATCS_BAUD_RATE))))

        # Channel low-pass filter (+/- 5.5 kHz passband for 12.5 kHz ATCS channel)
        cutoff = min(0.45, 5500.0 / float(self.input_rate))
        taps = min(255, max(33, 8 * self.dec_factor + 1))
        self.lpf = grabber.WindowedSincFilter(self.dec_factor, cutoff, taps)
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.disc_tail = np.zeros(0, dtype=np.float32)
        self._recent_hashes: collections.deque = collections.deque(maxlen=64)

    def reset(self):
        self.lpf.reset()
        self.phase_acc = 0.0
        self.prev_sample = np.complex64(1.0 + 0.0j)
        self.disc_tail = np.zeros(0, dtype=np.float32)

    def process_iq(
        self,
        iq_complex: np.ndarray,
        center_freq: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Processes complex baseband IQ and returns (decoded_packets, channel_rssi_dbfs)."""
        n = len(iq_complex)
        if n < self.dec_factor * 4:
            return [], -100.0

        # 1. Frequency shift to channel baseband
        df = float(self.freq_hz - center_freq)
        dt = 1.0 / float(self.input_rate)
        idx = np.arange(n, dtype=np.float64)
        lo = np.exp(-2j * math.pi * (self.phase_acc + df * idx * dt)).astype(np.complex64)
        self.phase_acc = float((self.phase_acc + df * n * dt) % 1.0)

        bb = self.lpf.process(iq_complex * lo)
        if len(bb) < self.sps * 20:
            return [], -100.0

        ch_rssi = grabber.compute_signal_power_dbfs(bb)

        # 2. Quadrature FM discriminator
        ext = np.empty(len(bb) + 1, dtype=np.complex64)
        ext[0] = self.prev_sample
        ext[1:] = bb
        self.prev_sample = bb[-1]
        prod = ext[1:] * np.conj(ext[:-1])
        disc = np.angle(prod).astype(np.float32)

        # Retain overlap tail so bursts spanning chunk boundaries decode intact
        full_disc = np.concatenate([self.disc_tail, disc]) if len(self.disc_tail) > 0 else disc
        keep_tail = self.sps * (16 + 3 * ATCS_CODEWORD_BITS + 8)
        self.disc_tail = full_disc[-keep_tail:].copy() if len(full_disc) > keep_tail else full_disc.copy()

        # 3. Matched integrate-and-dump over 1 symbol period
        kernel = np.ones(self.sps, dtype=np.float32) / float(self.sps)
        if len(full_disc) < len(kernel) + 16 * self.sps:
            return [], ch_rssi
        smoothed = np.convolve(full_disc, kernel, mode="valid")
        # Remove residual carrier frequency offset (DC bias in discriminator)
        smoothed = smoothed - float(np.median(smoothed))

        frame_bits_needed = 16 + 3 * ATCS_CODEWORD_BITS  # 160 bits
        packets: List[Dict[str, Any]] = []
        sync_hits_chunk = 0
        fec_good_chunk = 0
        fec_bad_chunk = 0
        seen_sync_pos: Set[int] = set()

        # 4. Sweep symbol clock phases (0..sps-1) and check both FSK polarities
        for ph in range(self.sps):
            syms = smoothed[ph::self.sps]
            if len(syms) < frame_bits_needed:
                continue
            hard_pos = (syms >= 0.0).astype(np.int8)
            bipolar = (hard_pos * 2 - 1).astype(np.float32)

            # Fast 16-bit sync word correlation (matches normal +16 or inverted -16)
            corr = np.correlate(bipolar, ATCS_SYNC_BIPOLAR, mode="valid")
            # Allow up to 1 bit error in 16-bit sync word (|corr| >= 14)
            hit_indices = np.where(np.abs(corr) >= 14.0)[0]
            for h_idx in hit_indices:
                h_i = int(h_idx)
                if h_i + frame_bits_needed > len(hard_pos):
                    continue
                pos_bucket = h_i // 4
                is_new_sync = pos_bucket not in seen_sync_pos
                if is_new_sync:
                    seen_sync_pos.add(pos_bucket)
                    sync_hits_chunk += 1

                inverted = corr[h_i] < 0.0
                frame_bits = (1 - hard_pos[h_i + 16:h_i + frame_bits_needed]) if inverted else hard_pos[h_i + 16:h_i + frame_bits_needed]

                # Decode 3 consecutive BCH(48, 32) codewords
                words32: List[int] = []
                total_bch_err = 0
                ok = True
                for w_idx in range(3):
                    seg = frame_bits[w_idx * ATCS_CODEWORD_BITS:(w_idx + 1) * ATCS_CODEWORD_BITS]
                    cw_val = 0
                    for b in seg:
                        cw_val = (cw_val << 1) | int(b)
                    d32, n_err = atcs_bch_decode(cw_val, max_errors=self.max_bch_errors)
                    if d32 is None:
                        ok = False
                        break
                    words32.append(d32)
                    total_bch_err += n_err

                if not ok:
                    if is_new_sync:
                        fec_bad_chunk += 1
                    continue

                parsed = parse_atcs_frame_words(words32)
                if not parsed:
                    continue
                # Deduplicate across adjacent clock phases
                sig_key = (parsed["hex"], pos_bucket)
                if sig_key in self._recent_hashes:
                    continue
                self._recent_hashes.append(sig_key)
                fec_good_chunk += 1

                # Estimate burst carrier offset (Hz) from discriminator DC mean over frame
                s0 = ph + h_i * self.sps
                s1 = min(len(full_disc), s0 + frame_bits_needed * self.sps)
                est_df_hz = float(np.mean(full_disc[s0:s1]) * self.actual_demod_rate / (2.0 * math.pi)) if s1 > s0 else 0.0

                parsed.update({
                    "channel": self.channel_id,
                    "freq_hz": self.freq_hz,
                    "freq_offset_hz": round(est_df_hz, 1),
                    "rssi": round(float(ch_rssi), 2),
                    "rssi_dbfs": round(float(ch_rssi), 2),
                    "snr_db": round(max(0.0, float(ch_rssi) + 55.0), 1),
                    "bch_errors": total_bch_err,
                    "rate": "4800",
                    "timestamp": datetime.now().isoformat(),
                })
                parsed["_scroll_ptc_"] = (
                    f"[ATCS {parsed['direction']} Ch{self.channel_id}] "
                    f"MCP {parsed['mcp_addr']} {parsed['type_name'].replace('ATCS_', '')} "
                    f"IND:{parsed['indication_hex']} (BCH:{total_bch_err})"
                )
                parsed["_node_id_"] = parsed["node_id"]
                parsed["_type_"] = parsed["type_name"]
                parsed["_sequence_"] = parsed["sequence"]
                parsed["telemetry"] = {
                    "wiu_id": parsed["mcp_addr"],
                    "src": parsed["mcp_addr"],
                    "type_name": parsed["type_name"],
                    "indication_hex": parsed["indication_hex"],
                }
                packets.append(parsed)

        self.last_sync_count = max(sync_hits_chunk, len(packets))
        self.last_fec_good = len(packets)
        self.last_fec_bad = max(0, self.last_sync_count - self.last_fec_good)
        self.last_bursts = self.last_sync_count if self.last_sync_count > 0 else (1 if ch_rssi > -45.0 else 0)
        return packets, ch_rssi


def build_atcs_wire_frames(pkt: Dict[str, Any]) -> Tuple[bytes, List[bytes]]:
    """
    Encodes a decoded ATCS packet into unified ApexBOSFlux/Engine/Spy/Chroma ZMQ multipart frames:
      Frame 0: b'A' + JSON envelope (self-describing across Flux multicast -> Engine & Spy)
      Frame 1: ASCII hex + RSSI
      Frame 2: JSON metadata (for Chroma blips & Spy)
      Frame 3: RSSI tag
    """
    payload_json = json.dumps(pkt, separators=(",", ":")).encode("utf-8")
    wire_frame = b"A" + payload_json
    rssi_val = float(pkt.get("rssi", -75.0))
    hex_str = str(pkt.get("hex") or "")
    hex_frame = f"{hex_str} RSSI:{rssi_val:.1f}".encode("ascii")
    rssi_frame = f"RSSI:{rssi_val:.1f}".encode("ascii")
    return wire_frame, [wire_frame, hex_frame, payload_json, rssi_frame]


# ---------------------------------------------------------------------------
# Shared Grabber-Style Interactive TUI, Commands & Statistics Mixin
# (Used identically by both ApexBOSLatch and ApexBOSFred)
# ---------------------------------------------------------------------------
class GrabberStyleDemodMixin:
    """
    Provides the full ApexBOSGrabber terminal experience for auxiliary SDR demodulators:
      - 80-col ANSI TUI with 3-row block Total Demod Failure Rate & PID Auto-Tuner status
      - I/Q flow, Bursts, Peak RSSI (60s window + ADC clipping warning), Pkts/m (Unique/Raw)
      - Auto-PPM (7s check, ±100 Hz target), Lazy PID Auto-Tuner, Channel AFC, Dedup TTL
      - Live per-channel statistics table & 6-row Recent Decoded Packets feed
      - Cross-platform non-blocking Command > prompt supporting all Grabber commands:
          p <ppm> | g <gain|auto> | b <0|1> | e <err> | autopm <on|off> |
          tune <on|off|status|interval|bounds> | afc <on|off|limit> | dedup <sec> |
          + <ch> | - <ch> | lock <ch> | unlock <ch> | chans | reset | help | q
    """

    APP_DISPLAY_NAME = "ApexBOSDemod"
    FEC_STAGE_LABEL = "BCH FEC Decoded          "
    RECENT_HEADER_LABEL = " Recent Decoded Packets:"

    def _init_grabber_ui_state(self, args):
        self.start_time = time.time()
        self.shutdown_flag = self._stop_ev
        self.use_color = getattr(grabber, "enable_ansi_colors", lambda: True)() and (not getattr(args, "no_color", False))
        self.no_tui = bool(getattr(args, "no_tui", False))
        self.rate_auto_scaled = False

        # Tuning / Control state
        self.base_ppm = int(self.ppm)
        self.base_gain_db = float(self.gain_db)
        self.base_gain_tenths = int(self.gain_tenths)
        self.gain = int(self.gain_tenths)
        self.max_errors = max(0, min(2, int(getattr(args, "max_errors", 2) or 2)))
        self.auto_ppm_enabled = not bool(getattr(args, "no_auto_ppm", False))
        self.last_auto_ppm_time = self.start_time
        self.afc_enabled = bool(getattr(args, "afc", False))
        self.afc_limit_hz = float(getattr(args, "afc_limit", 1500.0) or 1500.0)
        self.dedup_ttl = max(0.0, float(getattr(args, "dedup_ttl", 0.4) if getattr(args, "dedup_ttl", None) is not None else 0.4))
        self.locked_channels: Set[int] = set(getattr(self, "locked_channels", set()))

        self.auto_tuner = grabber.LazyPidAutoTuner(
            base_ppm=self.base_ppm,
            base_gain_tenths=self.base_gain_tenths,
            enabled=bool(getattr(args, "auto_tune", False)),
        )

        # Flow & sliding window stats
        self.reader_samples_read = 0
        self.reader_bytes_read = 0
        self.reader_chunks_read = 0
        self.demod_samples_proc = 0
        self.demod_chunks_proc = 0
        self.total_bursts = 0
        self.total_packets = 0
        self.total_dropped = 0
        self.life_sync = 0
        self.life_fec_good = 0
        self.life_fec_bad = 0

        self.peak_rssi = -100.0
        self.peak_rssi_window: collections.deque = collections.deque()
        self.packet_timestamps: collections.deque = collections.deque()
        self.rolling_deltas: collections.deque = collections.deque()  # (ts, sync, fec_g, fec_b)
        self.df_window: collections.deque = collections.deque()       # (ts, df_hz)
        self.channel_freq_offsets: Dict[int, float] = {}
        self.channel_last_seen: Dict[int, float] = {ch: self.start_time for ch in self.channels}
        self.channel_last_activity: Dict[int, float] = {}
        self.channel_stats: Dict[int, Dict[str, Any]] = {}
        for ch in self.channels:
            self._ensure_channel_stat(ch)

        self._dedup_cache: Dict[Tuple[int, str], float] = {}
        self.recent_events: collections.deque = collections.deque(maxlen=12)
        self.status_banner = "Listening... Type 'help' or '?' for commands."
        self.cmd_thread = None

    def _ensure_channel_stat(self, ch: int) -> Dict[str, Any]:
        if ch not in self.channel_stats:
            self.channel_stats[ch] = {
                "sync": 0,
                "good": 0,
                "bad": 0,
                "packets": 0,
                "rssi": -100.0,
                "df_hz": 0.0,
                "pkt_times": collections.deque(),
                "raw_times": collections.deque(),
            }
        return self.channel_stats[ch]

    def colorize(self, code: str, text: str) -> str:
        if self.use_color and code:
            return f"{code}{text}{grabber.COLOR_RESET}"
        return text

    def _clear_sliding_windows(self):
        self.peak_rssi_window.clear()
        self.peak_rssi = -100.0
        self.rolling_deltas.clear()
        self.df_window.clear()

    def get_sliding_window_average_df(self, current_time: float, window_sec: float = 60.0) -> Optional[float]:
        cutoff = current_time - window_sec
        while self.df_window and self.df_window[0][0] < cutoff:
            self.df_window.popleft()
        if not self.df_window:
            return None
        return sum(x[1] for x in self.df_window) / float(len(self.df_window))

    def _apply_hw_params(self, freq=None, ppm=None, gain=None, bias_tee=None):
        if self.stream is not None:
            try:
                self.stream.update_hardware_params(freq=freq, ppm=ppm, gain=gain, bias_tee=bias_tee)
            except Exception:
                pass

    def _is_duplicate_packet(self, ch: int, hex_str: str, now_t: float) -> bool:
        if self.dedup_ttl <= 0.0:
            return False
        key = (int(ch), str(hex_str or ""))
        prev = self._dedup_cache.get(key)
        if prev is not None and (now_t - prev) < self.dedup_ttl:
            return True
        self._dedup_cache[key] = now_t
        if len(self._dedup_cache) > 512:
            cutoff = now_t - max(2.0, self.dedup_ttl * 2.0)
            self._dedup_cache = {k: v for k, v in self._dedup_cache.items() if v >= cutoff}
        return False

    def _record_chunk_metrics(self, ch: int, ch_rssi: float, sync_d: int, good_d: int, bad_d: int, bursts_d: int, now_t: float):
        st = self._ensure_channel_stat(ch)
        st["rssi"] = float(ch_rssi)
        st["sync"] += int(sync_d)
        st["good"] += int(good_d)
        st["bad"] += int(bad_d)
        for _ in range(int(good_d)):
            st["raw_times"].append(now_t)
        self.total_bursts += int(bursts_d)
        self.life_sync += int(sync_d)
        self.life_fec_good += int(good_d)
        self.life_fec_bad += int(bad_d)
        if sync_d > 0 or good_d > 0 or bad_d > 0:
            self.rolling_deltas.append((now_t, int(sync_d), int(good_d), int(bad_d)))
        if ch_rssi > -99.5:
            self.peak_rssi_window.append((now_t, float(ch_rssi)))
            if ch_rssi > self.peak_rssi:
                self.peak_rssi = float(ch_rssi)

    def _record_unique_packet(self, pkt: Dict[str, Any], now_t: float):
        ch = int(pkt.get("channel") or 0)
        self.total_packets += 1
        self.packet_timestamps.append(now_t)
        self.channel_last_activity[ch] = now_t
        self.channel_last_seen[ch] = now_t
        st = self._ensure_channel_stat(ch)
        st["packets"] += 1
        st["pkt_times"].append(now_t)

        df_val = pkt.get("freq_offset_hz")
        if df_val is not None:
            df_f = float(df_val)
            self.df_window.append((now_t, df_f))
            st["df_hz"] = df_f
            if self.afc_enabled:
                prev_afc = self.channel_freq_offsets.get(ch, 0.0)
                clamped = max(-self.afc_limit_hz, min(self.afc_limit_hz, 0.7 * prev_afc + 0.3 * df_f))
                self.channel_freq_offsets[ch] = clamped

        self.recent_events.append({
            "time_str": time.strftime("%H:%M:%S", time.localtime(now_t)),
            "channel": ch,
            "ch_label": self._channel_short_label(ch),
            "rate": str(pkt.get("rate") or ""),
            "rssi": float(pkt.get("rssi", -100.0)),
            "snr": float(pkt.get("snr_db", 0.0)),
            "freq_offset_hz": df_val,
            "desc": str(pkt.get("type_name") or pkt.get("protocol") or "PKT").upper()[:10],
            "detail": self._format_recent_detail(pkt),
        })

    def _prune_and_run_auto_controllers(self, now_t: float):
        win_sec = grabber.SLIDING_WINDOW_SECONDS
        cutoff = now_t - win_sec
        while self.packet_timestamps and self.packet_timestamps[0] < cutoff:
            self.packet_timestamps.popleft()
        while self.rolling_deltas and self.rolling_deltas[0][0] < cutoff:
            self.rolling_deltas.popleft()
        while self.peak_rssi_window and self.peak_rssi_window[0][0] < cutoff:
            self.peak_rssi_window.popleft()
        self.peak_rssi = max((x[1] for x in self.peak_rssi_window), default=-100.0)
        for st in self.channel_stats.values():
            while st["pkt_times"] and st["pkt_times"][0] < cutoff:
                st["pkt_times"].popleft()
            while st["raw_times"] and st["raw_times"][0] < cutoff:
                st["raw_times"].popleft()

        # 1. Auto-PPM (matches Grabber: 7s interval, ±100 Hz deadband)
        if self.auto_ppm_enabled and (now_t - self.last_auto_ppm_time) >= 7.0:
            self.last_auto_ppm_time = now_t
            avg_df = self.get_sliding_window_average_df(now_t, window_sec=30.0)
            if avg_df is not None and abs(avg_df) > 100.0:
                step = -1 if avg_df > 100.0 else 1
                new_ppm = max(-150, min(150, self.ppm + step))
                if new_ppm != self.ppm:
                    self.ppm = new_ppm
                    self.base_ppm = new_ppm
                    self.auto_tuner.set_base_ppm(new_ppm)
                    self._apply_hw_params(ppm=self.ppm)
                    self.df_window.clear()
                    self.status_banner = f"Auto-PPM adjusted PPM -> {self.ppm} (avg Δf was {avg_df:+.1f} Hz)"

        # 2. Lazy PID Auto-Tuner (matches Grabber: drives short-window failure rate -> 0%)
        if self.auto_tuner.enabled:
            short_cut = now_t - grabber.SHORT_SLIDING_WINDOW_SECONDS
            short_sync = sum(d[1] for d in self.rolling_deltas if d[0] >= short_cut)
            short_good = sum(d[2] for d in self.rolling_deltas if d[0] >= short_cut)
            if short_sync > 0:
                fail_pct = max(0.0, 100.0 - (short_good / float(short_sync) * 100.0))
                res = self.auto_tuner.evaluate(now_t, fail_pct, short_sync)
                if res:
                    new_ppm, new_g_tenths = res
                    changed = False
                    if new_ppm != self.ppm:
                        self.ppm = new_ppm
                        self._apply_hw_params(ppm=self.ppm)
                        changed = True
                    if new_g_tenths != self.gain_tenths and self.base_gain_tenths > 0:
                        self.gain_tenths = new_g_tenths
                        self.gain_db = new_g_tenths / 10.0
                        self.gain = new_g_tenths
                        self._apply_hw_params(gain=self.gain_db)
                        changed = True
                    if changed:
                        self.status_banner = f"Auto-Tune PID: {self.auto_tuner.last_action_desc}"

    def _split_status_banner(self, banner: str, max_len: int = 68) -> Tuple[str, str]:
        if len(banner) <= max_len:
            return banner, ""
        pos = banner.rfind(" ", 0, max_len)
        if pos > 10:
            return banner[:pos].strip(), banner[pos:].strip()
        return banner[:max_len].strip(), banner[max_len:].strip()

    def _channel_short_label(self, ch: int) -> str:
        return str(ch)

    def _channel_proto_label(self, ch: int) -> str:
        return "DATA"

    def _format_recent_detail(self, pkt: Dict[str, Any]) -> str:
        return str(pkt.get("_scroll_ptc_") or pkt.get("hex", "")[:24])

    def _parse_single_channel_token(self, tok: str) -> Tuple[Optional[int], bool]:
        raise NotImplementedError

    def _reconfigure_channels(self):
        raise NotImplementedError

    def handle_terminal_input(self, command_line: str):
        """Processes Grabber-identical live interactive commands."""
        raw_cmd = (command_line or "").strip()
        if not raw_cmd:
            return
        normalized = raw_cmd.replace("=", " ").replace(":", " ")
        tokens = normalized.split()
        if not tokens:
            return
        cmd = tokens[0].lower()

        # Attached syntax support: "+4", "-2", "p-5", "g28", "b1", "e1", "dedup0.5"
        if cmd.startswith("+") and len(cmd) > 1:
            tokens = ["+", cmd[1:]] + tokens[1:]
            cmd = "+"
        elif cmd.startswith("-") and len(cmd) > 1:
            tokens = ["-", cmd[1:]] + tokens[1:]
            cmd = "-"
        elif cmd.startswith("p") and len(cmd) > 1 and (cmd[1:].isdigit() or (cmd[1] == "-" and cmd[2:].isdigit())):
            tokens = ["p", cmd[1:]] + tokens[1:]
            cmd = "p"
        elif cmd.startswith("g") and len(cmd) > 1 and (cmd[1:].replace(".", "", 1).isdigit() or cmd[1:].lower() in ("auto", "agc")):
            tokens = ["g", cmd[1:]] + tokens[1:]
            cmd = "g"
        elif cmd.startswith("b") and len(cmd) > 1 and cmd[1:].lower() in ("0", "1", "on", "off", "true", "false"):
            tokens = ["b", cmd[1:]] + tokens[1:]
            cmd = "b"
        elif cmd.startswith("e") and len(cmd) > 1 and cmd[1:].isdigit():
            tokens = ["e", cmd[1:]] + tokens[1:]
            cmd = "e"
        elif cmd.startswith("dedup") and len(cmd) > 5 and cmd[5:].replace(".", "", 1).isdigit():
            tokens = ["dedup", cmd[5:]] + tokens[1:]
            cmd = "dedup"

        if cmd in ("p", "ppm") and len(tokens) > 1:
            try:
                new_ppm = max(-150, min(150, int(tokens[1])))
                self.ppm = new_ppm
                self.base_ppm = new_ppm
                self.auto_tuner.set_base_ppm(new_ppm)
                self._apply_hw_params(ppm=self.ppm)
                self._clear_sliding_windows()
                self.status_banner = f"PPM adjusted to {self.ppm} (window reset)"
            except ValueError:
                self.status_banner = f"Invalid PPM value: '{tokens[1]}'"

        elif cmd in ("g", "gain") and len(tokens) > 1:
            try:
                self.gain_db, self.gain_tenths = grabber.parse_gain_value(tokens[1])
                self.gain = self.gain_tenths
                self.base_gain_db = self.gain_db
                self.base_gain_tenths = self.gain_tenths
                self.auto_tuner.set_base_gain_tenths(self.gain_tenths)
                self._apply_hw_params(gain=self.gain_db if self.gain_tenths > 0 else "auto")
                self._clear_sliding_windows()
                g_lbl = f"{self.gain_db:.1f} dB" if self.gain_tenths > 0 else "AGC"
                self.status_banner = f"Gain adjusted to {g_lbl} (window reset)"
            except ValueError:
                self.status_banner = f"Invalid Gain value: '{tokens[1]}'"

        elif cmd in ("b", "bias") and len(tokens) > 1:
            arg = tokens[1].lower()
            if arg in ("1", "on", "true", "yes", "enable"):
                self.bias_tee = True
            elif arg in ("0", "off", "false", "no", "disable"):
                self.bias_tee = False
            else:
                self.status_banner = f"Invalid bias value: '{tokens[1]}' (use 1/0 or on/off)"
                return
            self._apply_hw_params(bias_tee=self.bias_tee)
            self._clear_sliding_windows()
            self.status_banner = f"Bias tee {'enabled (ON)' if self.bias_tee else 'disabled (OFF)'} (window reset)"

        elif cmd in ("+", "add") and len(tokens) > 1:
            raw_items = " ".join(tokens[1:]).replace(",", " ").split()
            added, locked = [], []
            now_t = time.time()
            for item in raw_items:
                ch, is_lock = self._parse_single_channel_token(item)
                if ch is None:
                    continue
                if is_lock:
                    self.locked_channels.add(ch)
                    locked.append(ch)
                if ch not in self.channels:
                    self.channels.append(ch)
                    self.channel_last_seen[ch] = now_t
                    self._ensure_channel_stat(ch)
                    added.append(ch)
            if added:
                self.channels = sorted(set(self.channels))
                self._reconfigure_channels()
                self._clear_sliding_windows()
                self.status_banner = f"Added channel(s): {[self._channel_short_label(c) for c in added]}"
            elif locked:
                self.status_banner = f"Locked channel(s): {[self._channel_short_label(c) for c in locked]}"
            else:
                self.status_banner = "Invalid or already active channel(s)"

        elif cmd in ("-", "del", "remove") and len(tokens) > 1:
            raw_items = " ".join(tokens[1:]).replace(",", " ").split()
            del_chs = []
            for item in raw_items:
                ch, _ = self._parse_single_channel_token(item)
                if ch is not None and ch in self.channels:
                    del_chs.append(ch)
            rem = [c for c in self.channels if c not in del_chs]
            if not rem and self.channels:
                rem = [self.channels[0]]
                del_chs = [c for c in del_chs if c != rem[0]]
            if del_chs:
                self.channels = sorted(rem)
                for c in del_chs:
                    self.locked_channels.discard(c)
                    self.channel_freq_offsets.pop(c, None)
                self._reconfigure_channels()
                self._clear_sliding_windows()
                self.status_banner = f"Removed channel(s): {[self._channel_short_label(c) for c in del_chs]}"
            else:
                self.status_banner = "No matching removable channel(s)"

        elif cmd in ("lock", "pin") and len(tokens) > 1:
            nums = []
            for item in " ".join(tokens[1:]).replace(",", " ").split():
                ch, _ = self._parse_single_channel_token(item)
                if ch is not None:
                    self.locked_channels.add(ch)
                    nums.append(ch)
                    if ch not in self.channels:
                        self.channels = sorted(self.channels + [ch])
                        self._ensure_channel_stat(ch)
            if nums:
                self._reconfigure_channels()
                self.status_banner = f"Locked channel(s): {[self._channel_short_label(c) for c in nums]}"
            else:
                self.status_banner = "Invalid channel to lock"

        elif cmd in ("unlock", "unpin") and len(tokens) > 1:
            nums = []
            for item in " ".join(tokens[1:]).replace(",", " ").split():
                ch, _ = self._parse_single_channel_token(item)
                if ch is not None:
                    self.locked_channels.discard(ch)
                    nums.append(ch)
            self.status_banner = f"Unlocked channel(s): {[self._channel_short_label(c) for c in nums]}" if nums else "Invalid channel to unlock"

        elif cmd in ("c", "ch", "chans", "channels"):
            now_t = time.time()
            parts = []
            for c in self.channels:
                age = max(0, int(now_t - (self.channel_last_activity.get(c) or self.channel_last_seen.get(c, self.start_time))))
                lk = "." if c in self.locked_channels else ""
                parts.append(f"{self._channel_short_label(c)}{lk} ({age}s ago)")
            self.status_banner = f"Channels ({len(self.channels)}): {', '.join(parts)}"

        elif cmd in ("e", "-e", "err", "errors", "max-errors", "max_errors"):
            if len(tokens) > 1:
                try:
                    new_err = max(0, min(2, int(tokens[1])))
                    self.max_errors = new_err
                    for d in self.demods.values():
                        if hasattr(d, "max_bch_errors"):
                            d.max_bch_errors = new_err
                    self.status_banner = f"Max FEC/BCH bit errors (-E) set to {new_err}"
                except ValueError:
                    self.status_banner = f"Invalid error threshold: '{tokens[1]}'"
            else:
                self.status_banner = f"Current max FEC/BCH bit errors (-E): {self.max_errors} (use 'e <0-2>')"

        elif cmd in ("autopm", "auto-ppm", "appm", "ppm-afc"):
            sub = tokens[1].lower() if len(tokens) > 1 else ("off" if self.auto_ppm_enabled else "on")
            if sub in ("1", "on", "true", "yes", "enable", "start"):
                self.auto_ppm_enabled = True
                self.last_auto_ppm_time = time.time()
                self.status_banner = "Auto-PPM enabled (target: |Δf| < 100 Hz, 7s interval)"
            elif sub in ("0", "off", "false", "no", "disable", "stop"):
                self.auto_ppm_enabled = False
                self.status_banner = "Auto-PPM disabled"
            elif sub in ("status", "info"):
                avg_df = self.get_sliding_window_average_df(time.time())
                avg_s = f"avg Δf: {avg_df:+.1f} Hz" if avg_df is not None else "waiting for packets"
                self.status_banner = f"Auto-PPM: {'ON' if self.auto_ppm_enabled else 'OFF'} ({avg_s}, 7s check)"
            else:
                self.status_banner = f"Invalid option '{tokens[1]}'. Use 'autopm on' or 'autopm off'"

        elif cmd in ("tune", "pid", "autotune", "auto-tune"):
            sub = tokens[1].lower() if len(tokens) > 1 else ("off" if self.auto_tuner.enabled else "on")
            if sub in ("1", "on", "true", "enable", "start"):
                self.auto_tuner.enabled = True
                g_lbl = f"{self.base_gain_db:.1f} dB" if self.base_gain_tenths > 0 else "AGC"
                self.status_banner = f"Auto-Tune PID enabled (Base PPM: {self.base_ppm}, Base Gain: {g_lbl})"
            elif sub in ("0", "off", "false", "disable", "stop"):
                self.auto_tuner.enabled = False
                self.auto_tuner.reset_offsets()
                self.ppm = self.base_ppm
                self.gain_tenths = self.base_gain_tenths
                self.gain_db = self.base_gain_db
                self.gain = self.gain_tenths
                self._apply_hw_params(ppm=self.ppm, gain=self.gain_db if self.gain_tenths > 0 else "auto")
                self._clear_sliding_windows()
                self.status_banner = f"Auto-Tune PID disabled (restored Base PPM: {self.base_ppm})"
            elif sub in ("status", "info"):
                p_v, i_v, d_v = self.auto_tuner.get_pid_terms()
                self.status_banner = (
                    f"Auto-Tune PID: {'ON' if self.auto_tuner.enabled else 'OFF'} | "
                    f"PID [P:{p_v:+.2f}, I:{i_v:+.2f}, D:{d_v:+.2f}] | {self.auto_tuner.last_action_desc}"
                )
            elif sub in ("speed", "rate", "interval", "int") and len(tokens) > 2:
                try:
                    self.auto_tuner.eval_interval = max(1.0, float(tokens[2]))
                    self.status_banner = f"Auto-Tune PID interval set to {self.auto_tuner.eval_interval:.1f}s"
                except ValueError:
                    self.status_banner = f"Invalid tune interval '{tokens[2]}'"
            elif sub in ("window", "bound", "bounds", "limit", "limits") and len(tokens) > 3:
                try:
                    self.auto_tuner.max_offset_ppm = abs(int(tokens[2]))
                    self.auto_tuner.max_offset_gain_tenths = abs(int(tokens[3]))
                    self.status_banner = f"Auto-Tune PID bounds: ±{self.auto_tuner.max_offset_ppm} PPM, ±{self.auto_tuner.max_offset_gain_tenths/10.0:.1f} dB"
                except ValueError:
                    self.status_banner = "Invalid tune bounds parameters"
            else:
                self.status_banner = "Use 'tune on', 'tune off', 'tune status', or 'tune interval <sec>'"

        elif cmd in ("afc", "chafc", "freq", "freq-corr"):
            sub = tokens[1].lower() if len(tokens) > 1 else ("off" if self.afc_enabled else "on")
            if sub in ("1", "on", "true", "yes", "enable", "start"):
                self.afc_enabled = True
                self.status_banner = f"Channel AFC enabled (window: ±{self.afc_limit_hz:.0f} Hz)"
            elif sub in ("0", "off", "false", "no", "disable", "stop"):
                self.afc_enabled = False
                self.channel_freq_offsets.clear()
                self.status_banner = "Channel AFC disabled (offsets reset to 0 Hz)"
            elif sub in ("limit", "window", "max", "win", "cap") and len(tokens) > 2:
                try:
                    self.afc_limit_hz = abs(float(tokens[2]))
                    self.status_banner = f"Channel AFC window set to ±{self.afc_limit_hz:.0f} Hz"
                except ValueError:
                    self.status_banner = f"Invalid AFC limit '{tokens[2]}'"
            elif sub in ("status", "info"):
                self.status_banner = f"Channel AFC: {'ON' if self.afc_enabled else 'OFF'} (±{self.afc_limit_hz:.0f} Hz, {len(self.channel_freq_offsets)} ch)"
            else:
                self.status_banner = "Use 'afc on', 'afc off', or 'afc limit <hz>'"

        elif cmd in ("dedup", "dup"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("status", "info"):
                    self.status_banner = f"Dedup Window: {self.dedup_ttl:.2f}s"
                else:
                    val_s = tokens[2] if sub in ("window", "ttl", "sec") and len(tokens) > 2 else tokens[1]
                    try:
                        self.dedup_ttl = max(0.0, float(val_s))
                        self.status_banner = f"Dedup window size set to {self.dedup_ttl:.2f}s"
                    except ValueError:
                        self.status_banner = f"Invalid dedup window '{val_s}'"
            else:
                self.status_banner = f"Dedup Window: {self.dedup_ttl:.2f}s (use 'dedup <sec>')"

        elif cmd in ("reset", "clr", "clear"):
            self._clear_sliding_windows()
            self.status_banner = "Reset sliding windows & peak RSSI."

        elif cmd in ("q", "quit", "exit"):
            self.shutdown_flag.set()

        elif cmd in ("?", "h", "help"):
            self.status_banner = (
                "Commands: p <ppm> | g <gain|auto> | b <0|1> | e <err> | autopm <on|off> | "
                "tune <on|off> | afc <on|off> | dedup <sec> | + <ch> | - <ch> | lock/unlock <ch> | chans | reset | q"
            )
        else:
            self.status_banner = f"Unknown command '{raw_cmd}'. Type 'help' for available commands."

    def build_render_lines(self, current_time: float) -> List[str]:
        """Builds the exact Grabber-style 80-col TUI lines (testable headlessly)."""
        C_CYAN = grabber.COLOR_CYAN
        C_BOLD = grabber.COLOR_BOLD
        C_DIM = grabber.COLOR_DIM
        C_WHITE = grabber.COLOR_WHITE
        C_GREEN = grabber.COLOR_GREEN
        C_YELLOW = grabber.COLOR_YELLOW
        C_RED = grabber.COLOR_RED
        C_MAG = grabber.COLOR_MAGENTA
        C_RST = grabber.COLOR_RESET

        self._prune_and_run_auto_controllers(current_time)

        overall_ppm = len(self.packet_timestamps)
        win_sync = sum(d[1] for d in self.rolling_deltas)
        win_fec_g = sum(d[2] for d in self.rolling_deltas)
        win_fec_b = sum(d[3] for d in self.rolling_deltas)

        short_cut = current_time - grabber.SHORT_SLIDING_WINDOW_SECONDS
        short_sync = sum(d[1] for d in self.rolling_deltas if d[0] >= short_cut)
        short_fec_g = sum(d[2] for d in self.rolling_deltas if d[0] >= short_cut)
        short_matched_pct = (short_fec_g / short_sync * 100.0) if short_sync > 0 else 0.0
        short_fail_pct = max(0.0, 100.0 - short_matched_pct) if short_sync > 0 else None

        win_matched_pct = (win_fec_g / win_sync * 100.0) if win_sync > 0 else 0.0
        win_fail_pct = max(0.0, 100.0 - win_matched_pct) if win_sync > 0 else None

        fec_tot = win_fec_g + win_fec_b
        fec_bad_in_good = (win_fec_b / win_fec_g * 100.0) if win_fec_g > 0 else (100.0 if win_fec_b > 0 else 0.0)
        fec_err_rate = (win_fec_b / fec_tot * 100.0) if fec_tot > 0 else 0.0

        is_clipping = (self.peak_rssi >= 0.0)
        gain_str = f"{self.gain_db:.1f} dB" if self.gain_db > 0 else "AGC"
        gain_disp = self.colorize(C_RED + C_BOLD, f"{gain_str} [CLIP: GAIN HIGH]") if is_clipping else self.colorize(C_WHITE, gain_str)

        elapsed_sec = int(max(0.0, current_time - self.start_time))
        elapsed_str = f"{elapsed_sec // 3600:02d}:{(elapsed_sec % 3600) // 60:02d}:{elapsed_sec % 60:02d}"

        lines: List[str] = []
        lines.append(self.colorize(C_CYAN + C_BOLD, "=" * 80))
        lines.append(
            self.colorize(C_CYAN + C_BOLD, f" {self.APP_DISPLAY_NAME} ({VERSION}) ")
            + self.colorize(C_DIM, "| Elapsed: ") + self.colorize(C_WHITE + C_BOLD, elapsed_str)
            + self.colorize(C_DIM, " | SDR: ") + self.colorize(C_WHITE + C_BOLD, f"{self.stream_kind.upper()}/{self.sample_format}")
            + self.colorize(C_DIM, " | Rate: ") + self.colorize(C_WHITE, f"{self.sample_rate} S/s")
            + self.colorize(C_DIM, " | Gain: ") + gain_disp
            + self.colorize(C_DIM, " | Bias: ") + self.colorize(C_GREEN if self.bias_tee else C_DIM, "ON" if self.bias_tee else "OFF")
            + self.colorize(C_DIM, " | PPM: ") + self.colorize(C_WHITE, f"{self.ppm}")
            + self.colorize(C_DIM, " | Err: ") + self.colorize(C_YELLOW + C_BOLD, str(self.max_errors))
            + self.colorize(C_DIM, " | Center: ") + self.colorize(C_GREEN, f"{self.center_freq / 1e6:.4f} MHz")
        )
        lines.append(self.colorize(C_CYAN + C_BOLD, "=" * 80))

        lines.append(
            f" I/Q Flow: Reader {self.colorize(C_WHITE + C_BOLD, f'{self.reader_samples_read:,}')} S "
            f"({self.reader_bytes_read / (1024*1024):.2f} MB, {self.reader_chunks_read} chunks) | "
            f"Demod: {self.colorize(C_GREEN + C_BOLD, f'{self.demod_samples_proc:,}')} S ({self.demod_chunks_proc} banks) | "
            f"ZMQ:{self.zmq_port} Chroma:{self.chroma_port}/{self.ctrl_port}"
        )
        rssi_color = (C_RED + C_BOLD) if is_clipping else (C_MAG + C_BOLD)
        rssi_str = f"{self.peak_rssi:.1f} dBFS [CLIPPING: Gain too high!]" if is_clipping else f"{self.peak_rssi:.1f} dBFS"
        lines.append(
            f" Bursts: {self.colorize(C_WHITE + C_BOLD, str(self.total_bursts))}  "
            f"Peak RSSI: {self.colorize(rssi_color, rssi_str)}  "
            f"Packets: {self.colorize(C_GREEN + C_BOLD, str(self.total_packets))}  "
            f"PPM: {self.colorize(C_GREEN + C_BOLD, str(overall_ppm))} ({self.colorize(C_CYAN, f'Raw: {win_fec_g:,}')})  "
            f"Dropped: {self.colorize(C_RED if self.total_dropped else C_DIM, str(self.total_dropped))}"
        )
        if is_clipping:
            lines.append(self.colorize(C_RED + C_BOLD, " [!] ADC CLIPPING DETECTED (Peak RSSI >= 0.0 dBFS): Use 'g <gain>' to reduce."))

        ch_disp = ", ".join(
            f"{self._channel_short_label(c)}{'.' if c in self.locked_channels else ''}" for c in self.channels
        )
        if len(ch_disp) > 58:
            ch_disp = ch_disp[:55] + "..."
        lk_tag = f", {len(self.locked_channels)} locked." if self.locked_channels else ""
        lines.append(f" Channels ({len(self.channels)} active{lk_tag}): {self.colorize(C_CYAN, ch_disp)}")

        if self.afc_enabled:
            offs = [
                f"{self._channel_short_label(c)}:{self.channel_freq_offsets[c]:+.0f}Hz"
                for c in sorted(self.channel_freq_offsets)
                if c in self.channels and abs(self.channel_freq_offsets[c]) >= 1.0
            ]
            off_s = ", ".join(offs)[:55] if offs else "0 Hz (tracking)"
            lines.append(f" Channel AFC Offsets (Δf) : {self.colorize(C_YELLOW, off_s)}")
        else:
            lines.append(" Channel AFC Offsets (Δf) : " + self.colorize(C_DIM, "[DISABLED - Offsets 0Hz] (use 'afc on')"))

        avg_df = self.get_sliding_window_average_df(current_time)
        if avg_df is not None:
            if abs(avg_df) <= 100.0:
                df_c, df_tag = C_GREEN + C_BOLD, "[OPTIMAL: |Δf| < 100 Hz]"
            elif avg_df > 100.0:
                df_c, df_tag = C_YELLOW + C_BOLD, "[HIGH: Δf > +100 Hz -> PPM -1]"
            else:
                df_c, df_tag = C_CYAN + C_BOLD, "[LOW: Δf < -100 Hz -> PPM +1]"
            df_val_s = f"{avg_df:+.1f} Hz"
        else:
            df_c, df_tag, df_val_s = C_DIM, "[WAITING FOR PACKETS]", "-- Hz"

        ap_badge = self.colorize(C_GREEN + C_BOLD, "ON (7s check)") if self.auto_ppm_enabled else self.colorize(C_DIM, "OFF")
        lines.append(
            f" Avg Packet Freq Offset (Δf) : {self.colorize(df_c, df_val_s)} {self.colorize(df_c, df_tag)} | Auto-PPM: {ap_badge}"
        )
        lines.append(self.colorize(C_DIM, "-" * 80))

        # Pipeline Detection & Decoding Integrity + Auto-Tune PID
        p_v, i_v, d_v = self.auto_tuner.get_pid_terms()
        pid_str = f" [P: {p_v:+.2f}, I: {i_v:+.2f}, D: {d_v:+.2f}]"
        if self.auto_tuner.enabled:
            d_p = self.ppm - self.base_ppm
            d_g = (self.gain_tenths - self.base_gain_tenths) / 10.0
            g_s = f", ΔG: {d_g:+.1f} dB" if self.base_gain_tenths > 0 else " [Gain: AGC]"
            stop_s = " [STOPPED: NO SYNC]" if short_sync == 0 else ""
            tune_badge = self.colorize(C_GREEN + C_BOLD, f"ON (ΔPPM: {d_p:+d}{g_s}){stop_s}") + self.colorize(C_CYAN, pid_str)
        else:
            tune_badge = self.colorize(C_DIM, "OFF") + self.colorize(C_DIM, pid_str)

        lines.append(
            self.colorize(C_BOLD, f" Pipeline Detection & Decoding Integrity ({int(grabber.SLIDING_WINDOW_SECONDS)}s Window) | Auto-Tune PID: ")
            + tune_badge + self.colorize(C_BOLD, ":")
        )

        if self.use_color:
            style = grabber.get_failure_rate_bg_and_tag(short_fail_pct)
            bg_c = style.bg
            fg_c = getattr(style, "fg_contrast", C_WHITE + C_BOLD)
            font = grabber.get_large_block_font()
            rate_str = " -- " if short_fail_pct is None else f"{short_fail_pct:5.1f}%"
            tag_badge = f"{bg_c}{fg_c} {rate_str} [{style.tag}] {C_RST}"
            rows_num = [" ".join(font.get(ch_c, ["   ", "   ", "   "])[r] for ch_c in rate_str) for r in range(3)]
            max_w = max(len(r) for r in rows_num)
            r0_num = f"{bg_c}{fg_c} {rows_num[0].ljust(max_w)} {C_RST}"
            r1_num = f"{bg_c}{fg_c} {rows_num[1].ljust(max_w)} {C_RST}"
            r2_num = f"{bg_c}{fg_c} {rows_num[2].ljust(max_w)} {C_RST}"
            r0_txt = f"{self.colorize(C_BOLD, 'Total Demod Failure Rate:')} {tag_badge}"
            if short_sync > 0:
                r1_txt = (
                    f"{int(grabber.SHORT_SLIDING_WINDOW_SECONDS)}s Short Window: "
                    f"{self.colorize(C_WHITE + C_BOLD, f'{short_fec_g:,}/{short_sync:,}')} Good/Sync "
                    f"({self.colorize(C_GREEN if short_matched_pct >= 90 else C_YELLOW, f'{short_matched_pct:5.1f}% match')})"
                )
            else:
                r1_txt = f"{int(grabber.SHORT_SLIDING_WINDOW_SECONDS)}s Short Window: {self.colorize(C_DIM, '0/0 Good/Sync (--.-% match)')}"
            if win_sync > 0:
                r2_txt = f"60s Total Window: {self.colorize(C_WHITE, f'{win_fail_pct:5.1f}% fail')} ({win_fec_g:,}/{win_sync:,}) | {self.colorize(C_DIM, 'PID Setpoint: 0.0%')}"
            else:
                r2_txt = f"60s Total Window: {self.colorize(C_DIM, '--.-% fail (0/0)')} | {self.colorize(C_DIM, 'PID: STOPPED (NO SYNC)')}"
            lines.extend([f"  {r0_num}  {r0_txt}", f"  {r1_num}  {r1_txt}", f"  {r2_num}  {r2_txt}"])
        else:
            r_s = "--" if short_fail_pct is None else f"{short_fail_pct:5.1f}%"
            lines.append(
                f"  Total Demod Failure Rate : [{r_s}] ({int(grabber.SHORT_SLIDING_WINDOW_SECONDS)}s: {short_fec_g:,}/{short_sync:,} | 60s: {win_fec_g:,}/{win_sync:,})"
            )

        lines.append(
            f"  Packet Sync Signatures   : {self.colorize(C_WHITE + C_BOLD, f'{win_sync:,}')} detected (window)  |  "
            f"Lifetime: {self.colorize(C_DIM, f'{self.life_sync:,}')}"
        )
        lines.append(
            f"  {self.FEC_STAGE_LABEL}: Good: {self.colorize(C_GREEN, f'{win_fec_g:,}')}  | "
            f"Failures: {self.colorize(C_RED if win_fec_b else C_DIM, f'{win_fec_b:,}')}  | "
            f"Bad in Good: {self.colorize(C_YELLOW if fec_bad_in_good > 5 else C_GREEN, f'{fec_bad_in_good:5.1f}%')} "
            f"({fec_err_rate:4.1f}% failure rate)"
        )
        lines.append(
            f"  Packets per Minute Rate  : Unique: {self.colorize(C_GREEN + C_BOLD, f'{overall_ppm:,}')} Pkts/m (dedup)  |  "
            f"Raw Demod: {self.colorize(C_CYAN + C_BOLD, f'{win_fec_g:,}')} Pkts/m (pre-dedup)"
        )
        lines.append(
            f"  Lifetime Unique Packets  : {self.colorize(C_WHITE + C_BOLD, f'{self.total_packets:,}')} (dedup window: {self.dedup_ttl:.2f}s)"
        )
        lines.append(self.colorize(C_DIM, "-" * 80))

        # Per-Channel Statistics Table
        table_hdr = " Ch       | Freq (MHz)  | Proto | Sync    | Good/Bad      | RSSI   | Pkts/m (U/R) |"
        lines.append(self.colorize(C_BOLD, table_hdr))
        for ch in self.channels:
            st = self._ensure_channel_stat(ch)
            lbl = f"{self._channel_short_label(ch)}{'.' if ch in self.locked_channels else ''}"
            freq_s = f"{self.freqs_by_ch.get(ch, 0) / 1e6:9.5f}"
            proto_s = self._channel_proto_label(ch)[:5]
            sync_s = f"{st['sync']:,}"
            gb_s = f"{st['good']:,}/{st['bad']:,}"
            rssi_s = f"{st['rssi']:6.1f}"
            pm_s = f"{len(st['pkt_times']):,} / {len(st['raw_times']):,}"
            lines.append(
                f" {lbl:<8} | {freq_s:<11} | {proto_s:<5} | {sync_s:<7} | {gb_s:<13} | {rssi_s} | {pm_s:<12} |"
            )
        lines.append(self.colorize(C_DIM, "-" * len(table_hdr)))

        # Recent Decoded Packets (6 pre-allocated rows for stable display)
        lines.append(self.colorize(C_BOLD, self.RECENT_HEADER_LABEL))
        recent = list(self.recent_events)[-6:]
        if not recent:
            lines.append(self.colorize(C_DIM, "   (Listening for RF bursts across monitored channels...)"))
            for _ in range(5):
                lines.append("")
        else:
            for ev in recent:
                df_v = ev.get("freq_offset_hz")
                df_s = f"Δf:{df_v:+.0f}Hz" if df_v is not None else "Δf:--Hz"
                desc_pad = f"{ev['desc']:<9s}"
                lines.append(
                    f"   [{ev['time_str']}] {ev['ch_label']:<6s} ({ev['rate']:>4s}) | "
                    f"RSSI:{ev['rssi']:5.1f} SNR:{ev['snr']:4.1f} {df_s:>10s} | "
                    f"{self.colorize(C_GREEN, desc_pad)} | {self.colorize(C_YELLOW, ev['detail'][:44])}"
                )
            for _ in range(6 - len(recent)):
                lines.append("")

        lines.append(self.colorize(C_DIM, "-" * 80))
        p1, p2 = self._split_status_banner(self.status_banner, max_len=68)
        lines.append(f" Status: {self.colorize(C_WHITE, p1)}")
        if p2:
            lines.append(f"         {self.colorize(C_WHITE, p2)}")
        lines.append(
            self.colorize(C_DIM, " Controls: 'p <ppm>', 'g <gain|auto>', 'b <0|1>', 'e <err>', 'autopm <on|off>', 'tune <on|off>'")
        )
        lines.append(
            self.colorize(C_DIM, "           'afc <on|off>', 'dedup <sec>', '+ <ch>', '- <ch>', 'lock/unlock', 'chans', 'q' to quit")
        )
        return lines

    def render_screen(self, current_time: float):
        lines = self.build_render_lines(current_time)
        if self.use_color:
            sys.stdout.write("\033[H\033[2J")
        else:
            try:
                os.system("cls" if os.name == "nt" else "clear")
            except Exception:
                sys.stdout.write("\n" * 2)
        cur_cmd = self.cmd_thread.get_current_buffer() if self.cmd_thread else ""
        sys.stdout.write("\n".join(lines) + f"\n Command > {cur_cmd}")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# ApexBOSLatch Application Controller (Multi-SDR Ready + Grabber TUI + Chroma Tap)
# ---------------------------------------------------------------------------
class ApexBOSLatchApp(GrabberStyleDemodMixin):
    """
    Standalone 900 MHz ATCS SDR Demodulator & Decoder daemon with full Grabber-style
    interactive TUI, commands, Auto-PPM, PID Auto-Tuner, statistics, and Chroma IQ tap.
    """

    APP_DISPLAY_NAME = "ApexBOSLatch"
    FEC_STAGE_LABEL = "BCH(48,32) FEC Decoded   "
    RECENT_HEADER_LABEL = " Recent Decoded ATCS Packets (4800 bps 2-FSK + BCH(48,32) verified):"

    def __init__(self, args):
        self.args = args
        self.locked_channels: Set[int] = set()
        self.channels = self._parse_channels(args.chans, uplink=bool(args.uplink), locked_out=self.locked_channels)
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
        self.demods: Dict[int, AtcsChannelDemodulator] = {}
        self.atcs_decoder = AtcsRulesDecoder(config_dir=os.path.join(PROJECT_ROOT, "config"))
        self.freqs_by_ch: Dict[int, int] = {}
        self.center_freq = 936437500
        self.sample_rate = 1920000

        self._init_grabber_ui_state(args)
        self._reconfigure_channels()

    @staticmethod
    def _parse_single_channel_static(raw_tok: str, uplink: bool = False) -> Tuple[Optional[int], bool]:
        s = str(raw_tok or "").strip()
        if not s:
            return None, False
        is_lock = bool(re.search(r"[\.lLpP]+$", s))
        core = re.sub(r"[\.lLpP]+$", "", s).strip().lower()
        if core in ATCS_CHANNEL_ALIASES:
            ch = ATCS_CHANNEL_ALIASES[core]
            if uplink and 1 <= ch <= 6:
                ch += 10
            return ch, is_lock
        if core.isdigit():
            c = int(core)
            if c in ATCS_ALL_CHANNELS:
                return c, is_lock
        return None, False

    def _parse_single_channel_token(self, tok: str) -> Tuple[Optional[int], bool]:
        return self._parse_single_channel_static(tok, uplink=bool(getattr(self.args, "uplink", False)))

    @classmethod
    def _parse_channels(cls, chans_arg: Optional[str], uplink: bool = False, locked_out: Optional[Set[int]] = None) -> List[int]:
        if not chans_arg:
            return list(ATCS_MCP_CHANNELS.keys()) if uplink else list(ATCS_BCP_CHANNELS.keys())
        out: List[int] = []
        for tok in str(chans_arg).split(","):
            tok = tok.strip()
            if not tok:
                continue
            m_rng = re.match(r"^([a-zA-Z]*-?\d+)\s*-\s*([a-zA-Z]*-?\d+)([\.lLpP]*)$", tok)
            if m_rng and not (tok.lower() in ATCS_CHANNEL_ALIASES):
                c1, _ = cls._parse_single_channel_static(m_rng.group(1), uplink=uplink)
                c2, _ = cls._parse_single_channel_static(m_rng.group(2), uplink=uplink)
                if c1 is not None and c2 is not None:
                    is_lk = bool(m_rng.group(3))
                    for c in range(min(c1, c2), max(c1, c2) + 1):
                        if c in ATCS_ALL_CHANNELS:
                            if c not in out:
                                out.append(c)
                            if is_lk and locked_out is not None:
                                locked_out.add(c)
                    continue
            ch, is_lk = cls._parse_single_channel_static(tok, uplink=uplink)
            if ch is not None:
                if ch not in out:
                    out.append(ch)
                if is_lk and locked_out is not None:
                    locked_out.add(ch)
        return sorted(out) or (list(ATCS_MCP_CHANNELS.keys()) if uplink else list(ATCS_BCP_CHANNELS.keys()))

    def _channel_short_label(self, ch: int) -> str:
        if ch in ATCS_ALL_CHANNELS:
            return ATCS_ALL_CHANNELS[ch][1]
        return f"Ch{ch}"

    def _channel_proto_label(self, ch: int) -> str:
        return "MCP" if ch >= 11 else "BCP"

    def _format_recent_detail(self, pkt: Dict[str, Any]) -> str:
        st = pkt.get("atcs_station") or pkt.get("ctc_id") or pkt.get("mcp_addr", "")
        toks = pkt.get("ctc_tokens") or []
        tok_s = ",".join(toks[:4]) if toks else pkt.get("indication_hex", "")
        return f"{st} [{tok_s}]"

    def _reconfigure_channels(self):
        is_rsp = (self.stream_kind == "rsptcp") or (self.stream_kind == "soapy" and "sdrplay" in self.soapy_args.lower())
        is_wideband_sdr = is_rsp or (self.stream_kind == "soapy")
        # BCP (935 MHz) & MCP (896 MHz) are 39 MHz apart: always cluster within SDR span, preferring MCP on tie
        max_span = 8000000 if is_wideband_sdr else RTLSDR_MAX_SPAN_HZ
        selected, skipped = select_channels_for_sdr_bandwidth(
            self.channels,
            atcs_channel_frequency,
            max_span_hz=max_span,
            prefer_predicate=lambda c: int(c) >= 11,  # Prefer MCP side on tie
        )
        if skipped:
            self.channels = selected
            sel_lbls = [self._channel_short_label(c) for c in selected]
            sk_lbls = [self._channel_short_label(c) for c in skipped]
            self.status_banner = f"SDR span limit: monitoring {sel_lbls} (skipped out-of-band: {sk_lbls})"
        else:
            self.channels = selected

        self.freqs_by_ch = {ch: atcs_channel_frequency(ch) for ch in self.channels}
        freqs = list(self.freqs_by_ch.values())
        raw_cf = int(round(0.5 * (min(freqs) + max(freqs)))) if freqs else 936437500
        self.center_freq = grabber.avoid_channel_center_dc_offset(raw_cf, freqs, channel_spacing_hz=25000)
        span = (max(freqs) - min(freqs)) if len(freqs) > 1 else 0
        min_rate = grabber.DirectRspTcpSource.RSP_MIN_RATE if is_rsp else 960000
        needed = int(math.ceil((span + 120000) / 192000.0) * 192000)
        req_rate = int(getattr(self.args, "rate", 0) or 0)
        self.sample_rate = max(min_rate, needed, req_rate)
        if not is_wideband_sdr and self.stream_kind != "mock":
            self.sample_rate = min(self.sample_rate, RTLSDR_MAX_RATE_HZ)
        if self.sample_rate % 192000 != 0:
            self.sample_rate = int(math.ceil(self.sample_rate / 192000.0) * 192000)
        self._apply_hw_params(freq=self.center_freq)
        self._rebuild_demods()

    def _rebuild_demods(self):
        self.demods = {
            ch: AtcsChannelDemodulator(self.sample_rate, ch, self.freqs_by_ch[ch], max_bch_errors=self.max_errors)
            for ch in self.channels
        }

    def get_chroma_state_dict(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": VERSION,
            "band": "atcs",
            "protocol": "atcs",
            "stream_kind": self.stream_kind,
            "center_freq": int(self.center_freq),
            "sample_rate": int(self.sample_rate),
            "base_sample_rate": int(self.sample_rate),
            "sample_format": self.sample_format,
            "wideband": False,
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
                if ch not in ATCS_ALL_CHANNELS:
                    return {"ok": False, "error": f"Invalid ATCS channel {ch}"}
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
                print(
                    f"[*] ApexBOSLatch ({VERSION}) | 900 MHz ATCS F-1..F-6 4800 bps FSK | "
                    f"Mode: {self.stream_kind} ({self.sample_format}) | "
                    f"Center: {self.center_freq/1e6:.4f} MHz | Rate: {self.sample_rate/1e6:.3f} MS/s | "
                    f"Ch: {[self._channel_short_label(c) for c in self.channels]} | ZMQ:{self.zmq_port}"
                )

            chunk_samps = max(65536, int(round(self.sample_rate * 0.08)))
            mock_seq = 0
            mock_sites = [
                (1, "80210000101400", 0x01, b"\x5A\x3C\x01\xF0"),
                (4, "02204500210800", 0x01, b"\xC3\x10\x44\x02"),
                (2, "12508800304200", 0x02, b"\x0F\xA0\x12\x34"),
            ]

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
                    ch_m, mcp_m, mt_m, ind_m = mock_sites[mock_seq % len(mock_sites)]
                    if ch_m not in self.freqs_by_ch:
                        ch_m = self.channels[0]
                    words = pack_atcs_frame_words(mcp_m, msg_type=mt_m, indication_bytes=ind_m, seq=mock_seq % 8, is_uplink=(ch_m >= 11))
                    raw_chunk = generate_atcs_fsk_iq(
                        words,
                        sample_rate=self.sample_rate,
                        center_freq=self.center_freq,
                        channel_freq=self.freqs_by_ch[ch_m],
                        seed=900 + mock_seq,
                    )
                    mock_seq += 1
                    time.sleep(0.35)
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
                        pkt = self.atcs_decoder.decode(pkt)
                        if self._is_duplicate_packet(ch, pkt.get("hex", ""), now_t):
                            continue
                        self._record_unique_packet(pkt, now_t)
                        _, zmq_frames = build_atcs_wire_frames(pkt)
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
    cfg_ports = config_loader.get_aux_demod_ports().get("atcs", {})
    def_dev = int(cfg_ports.get("device", 1))
    def_gain = str(cfg_ports.get("gain", "28.0") or "28.0").strip()
    def_ppm = int(cfg_ports.get("ppm", 0) or 0)
    def_zmq = int(cfg_ports.get("zmq_port", DEFAULT_LATCH_ZMQ_PORT))
    def_chroma = int(cfg_ports.get("chroma_port", DEFAULT_LATCH_CHROMA_PORT))
    def_ctrl = int(cfg_ports.get("ctrl_port", DEFAULT_LATCH_CTRL_PORT))

    p = argparse.ArgumentParser(
        description=f"ApexBOSLatch ({VERSION}) - Clean-Room 900 MHz ATCS (Pre-FCC F-1..F-6) SDR Demodulator"
    )
    p.add_argument("--chans", default=None, help="ATCS channels: BCP1-BCP6, MCP1-MCP6, F-1..F-6, 1-6 (BCP), 11-16 (MCP)")
    p.add_argument("--uplink", action="store_true", help="Default to 896/897 MHz MCP uplink channels (MCP1-MCP6)")
    p.add_argument("--afc", action="store_true", default=False, help="Enable per-channel AFC at startup (default: OFF)")
    p.add_argument("--device", type=int, default=def_dev, help=f"SDR device index (config/default: {def_dev}, leaving 0 for PTC Grabber)")
    p.add_argument("--rate", type=int, default=1920000, help="SDR sample rate in S/s (default: 1920000; auto-clamped >= 2.112M for SDRplay)")
    p.add_argument("--gain", default=def_gain, help=f"Tuner gain in dB (0 or 'auto' for AGC, config/default: {def_gain})")
    p.add_argument("--ppm", type=int, default=def_ppm, help=f"Master oscillator PPM correction (config/default: {def_ppm})")
    p.add_argument("-b", "--bias-tee", dest="bias_tee", type=int, default=0, choices=[0, 1], help="Enable bias tee (default: 0)")
    p.add_argument("-E", "--max-errors", dest="max_errors", type=int, default=2, help="Max BCH(48,32) correctable bit errors (0..2, default: 2)")
    p.add_argument("--dedup-ttl", type=float, default=0.4, help="Duplicate packet suppression window in seconds (default: 0.4)")
    p.add_argument("--auto-tune", action="store_true", help="Enable Lazy PID Auto-Tuner at startup")
    p.add_argument("--no-auto-ppm", action="store_true", help="Disable automatic 7s PPM drift tracking")
    p.add_argument("--no-tui", action="store_true", help="Run in plain console log mode instead of Grabber-style TUI")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI terminal colors")
    p.add_argument("--zmq-port", type=int, default=def_zmq, help=f"ZMQ PUB output port (default: {def_zmq})")
    p.add_argument("--chroma-port", type=int, default=def_chroma, help=f"Chroma XPUB IQ tap port (default: {def_chroma})")
    p.add_argument("--ctrl-port", type=int, default=def_ctrl, help=f"Chroma REP control port (default: {def_ctrl})")
    p.add_argument("--mock", action="store_true", help="Run with synthetic 935 MHz ATCS 4800 bps FSK bursts (no SDR needed)")
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
    app = ApexBOSLatchApp(args)
    app.run()


if __name__ == "__main__":
    main()
