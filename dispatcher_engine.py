#!/usr/bin/env python3
# Copyright by AB
# Licensed under the MIT License.
"""
Railroad Dispatcher Display Logic Engine (v26.117.2)
Features:
  - Added label_pos ('top', 'bottom', 'left', 'right') to BaseElement.
  - TCP Layout Server (serves layout dictionary & layout_filename to remote renderers)
  - External PacketParser integration via parse_packets.py
  - Sequence rollover validation & bitwise extraction (_packet_rules_ and _base_)
  - Cached regional signals_NNN.json tables & 'over' token lamp position matching
  - Active Train Tracker & Scroller (Train ID/Name, Speed MPH, Direction, Last Element)
  - Train collision prediction & state-delta-only UDP multicast broadcasting
"""

import argparse
import atexit
import copy
import hashlib
import json
import struct
import zlib
try:
    import lz4.frame as lz4_frame
except ImportError:
    lz4_frame = None

CHUNK_MAGIC_ZLIB = b"ABZ1"
CHUNK_MAGIC_LZ4 = b"ABL1"
CHUNK_HEADER_FMT = "!4sIIHH"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FMT)  # 16 bytes
MAX_UDP_CHUNK_SIZE = 1400  # MTU-safe chunk size: prevents IP packet fragmentation across physical LAN/Wi-Fi
import datetime
import math
import os
import re
import select
import socket
import sys
import tempfile
import threading
import time
from collections import deque
try:
    from dataclasses import dataclass, field, asdict, fields
except ImportError:
    raise ImportError("Python 3.6 requires the 'dataclasses' package. Install it via: pip install dataclasses")
from typing import Dict, List, Optional, Tuple, Any

from parse_packets import PacketParser

RE_CH_RSSI_BYTES = re.compile(rb"^CH(\d{3})(?::RSSI=([+-]?\d+(?:\.\d+)?))?:")
RE_CH_RSSI_STR = re.compile(r"^CH(\d{3})(?::RSSI=([+-]?\d+(?:\.\d+)?))?:")
FIREWALL_HELP_NOTICE = """
Windows Firewall Requirement:
  To ingest UDP multicast packets from ApexBOSFlux or Grabber on Windows,
  Windows Defender Firewall must allow incoming UDP and TCP traffic, IGMP, and multicast routes.
  Run enable_firewall.bat as Administrator, or paste this command into an Administrator PowerShell prompt:
    New-NetFirewallRule -DisplayName "ApexBOS Inbound UDP Multicast" -Direction Inbound -LocalPort 23003,5007 -Protocol UDP -Action Allow -Profile Domain,Private,Public; New-NetFirewallRule -DisplayName "ApexBOS Inbound TCP Services" -Direction Inbound -LocalPort 1234,5008,18000-18175,20101-20175,23009,23010,56781 -Protocol TCP -Action Allow -Profile Domain,Private,Public; New-NetFirewallRule -DisplayName "ApexBOS Inbound IGMP" -Direction Inbound -Protocol 2 -Action Allow -Profile Domain,Private,Public; route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 metric 1
"""
from version import VERSION, __version__
from train_logic import (
    TrainTracker,
    TrainScroller,
    TrainInferenceManager,
    haversine_miles,
    get_signal_gps as resolve_signal_gps,
    get_element_gps_any as resolve_element_gps,
)

def _make_json_serializable(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex().upper()
    if isinstance(obj, dict):
        return {str(k): _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, deque, set)):
        return [_make_json_serializable(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


DEFAULT_SIGNAL_TABLE: Dict[int, str] = {
    0: "top:mg,mid:mg,bot:mg",
    1: "top:mg,mid:mg,bot:mg",
    2: "top:yl",
    3: "top:rd,mid:rd,bot:gr",
    4: "top:yl,mid:fgr",
    5: "top:yl,mid:rd",
    6: "top:rd,mid:yl",
    7: "top:mg,mid:mg,bot:mg",
    8: "top:mg,mid:mg,bot:mg",
    9: "top:mg,mid:mg,bot:mg",
    10: "top:rd,mid:yl,bot:yl",
    11: "top:rd,mid:fyl",
    12: "top:fyl",
    13: "top:mg,mid:mg,bot:mg",
    14: "top:rdi",
    15: "top:bk,mid:bk,bot:bk",
    16: "top:yl,mid:yl",
    17: "top:rd,mid:yl",
    18: "top:rd,mid:gr",
    19: "top:mg,mid:mg,bot:mg",
    20: "top:yl,mid:gr",
    21: "top:mg,mid:mg,bot:mg",
    22: "top:frd",
    23: "top:mg,mid:mg,bot:mg",
    24: "top:gr",
    25: "top:mg,mid:mg,bot:mg",
    26: "top:rd,mid:fyl",
    27: "top:mg,mid:mg,bot:mg",
    28: "top:yl,mid:frd",
    29: "top:mg,mid:mg,bot:mg",
    30: "top:rd,mid:rd,bot:rd",
    31: "top:mg,mid:mg,bot:mg",
    32: "top:gy,mid:gy,bot:gy",
}

SIGNAL_POS_ALIASES: Dict[str, str] = {
    "top": "top", "t": "top", "0": "top",
    "middle": "middle", "mid": "middle", "m": "middle", "1": "middle",
    "bottom": "bottom", "bot": "bottom", "btm": "bottom", "b": "bottom", "2": "bottom",
}

SIGNAL_COLOR_ALIASES: Dict[str, str] = {
    "gr": "green", "gn": "green", "g": "green", "green": "green",
    "fgr": "flashing_green", "f_gr": "flashing_green", "fgn": "flashing_green", "f_gn": "flashing_green", "fg": "flashing_green", "flashing_green": "flashing_green",
    "yl": "yellow", "yw": "yellow", "y": "yellow", "yellow": "yellow",
    "fyl": "flashing_yellow", "f_yl": "flashing_yellow", "fyw": "flashing_yellow", "f_yw": "flashing_yellow", "fy": "flashing_yellow", "flashing_yellow": "flashing_yellow",
    "rd": "red", "r": "red", "red": "red",
    "frd": "flashing_red", "f_rd": "flashing_red", "fr": "flashing_red", "flashing_red": "flashing_red",
    "rdi": "red_intermediate", "rd_i": "red_intermediate", "red_intermediate": "red_intermediate",
    "mg": "magenta", "m": "magenta", "magenta": "magenta",
    "fmg": "flashing_magenta", "f_mg": "flashing_magenta", "fm": "flashing_magenta", "flashing_magenta": "flashing_magenta",
    "bk": "black", "blk": "black", "k": "black", "black": "black",
    "dk": "dark", "dark": "dark",
    "gy": "gray", "gr_y": "gray", "grey": "gray", "gray": "gray",
    "lu": "lunar", "l": "lunar", "lunar": "lunar",
    "flu": "flashing_lunar", "fl": "flashing_lunar", "flashing_lunar": "flashing_lunar",
}


def resolve_signal_lamp_aspect(aspect_str: str, position: str) -> Optional[str]:
    """Resolves a compact ('top:fgr,bot:rd,mid:f_mg') or legacy ('red_over_green') signal aspect string for a given head position."""
    raw = (aspect_str or "").strip()
    if not raw:
        return None
    norm_pos = SIGNAL_POS_ALIASES.get((position or "top").strip().lower(), "top")

    if ":" in raw:
        pos_map: Dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            k_raw, v_raw = part.split(":", 1)
            k_norm = SIGNAL_POS_ALIASES.get(k_raw.strip().lower(), k_raw.strip().lower())
            v_low = v_raw.strip().lower()
            if v_low in SIGNAL_COLOR_ALIASES:
                c_norm = SIGNAL_COLOR_ALIASES[v_low]
            elif v_low.startswith(("f_", "f")) and len(v_low) >= 3:
                sub = v_low[2:] if v_low.startswith("f_") else v_low[1:]
                base_c = SIGNAL_COLOR_ALIASES.get(sub, sub)
                c_norm = f"flashing_{base_c}" if not base_c.startswith("flashing_") else base_c
            else:
                c_norm = v_low
            pos_map[k_norm] = c_norm

        if norm_pos in pos_map:
            return pos_map[norm_pos]
        # If a single-token entry like "top:gr" is evaluated for top, it matched above;
        # if only one head was specified and all heads share it (e.g. "all:mg"), check "all":
        if "all" in pos_map:
            return pos_map["all"]
        return None

    tokens = raw.split("_over_")
    pos_idx = POSITION_INDEX_MAP.get(norm_pos, 0)
    if pos_idx < len(tokens):
        tok = tokens[pos_idx].strip().lower()
        return SIGNAL_COLOR_ALIASES.get(tok, tok)
    return None

POSITION_INDEX_MAP = {"top": 0, "middle": 1, "bottom": 2}

# ATCS/PTC WIU 2-bit switch position indication:
#   01 (1) = NORMAL, 10 (2) = REVERSE, 00 (0) = out of correspondence / in transit, 11 (3) = invalid.
# Maps raw value -> internal alignment (0 = normal, 1 = reverse). Unmapped values keep last alignment.
TURNOUT_VALUE_MAP: Dict[int, int] = {1: 0, 2: 1}
TURNOUT_VALUE_DESC: Dict[int, str] = {0: "OUT OF CORRESPONDENCE", 1: "NORMAL", 2: "REVERSE", 3: "INVALID/FAULT"}
OPPOSITE_DIR = {"E": "W", "W": "E", "N": "S", "S": "N", "NE": "SW", "SW": "NE", "NW": "SE", "SE": "NW"}
COMPATIBLE_DIRS = {
    "E": ("E", "NE", "SE"), "W": ("W", "NW", "SW"),
    "N": ("N", "NE", "NW"), "S": ("S", "SE", "SW"),
    "NE": ("NE", "N", "E"), "NW": ("NW", "N", "W"),
    "SE": ("SE", "S", "E"), "SW": ("SW", "S", "W"),
}

def parse_signal_rules(rule_str: str) -> Tuple[Optional[str], Dict[int, Dict[str, str]]]:
    """
    Parses signal operation overwrite string.
    Supports:
      - RR override: '<RRR>' or 'DEF' / '<DEF>'
      - Individual value overwrite: e.g. 32:"t:flashing_green,m:magenta,b:black", 5:"t:rd,b:gr"
        Supports full color names as well as abbreviations.
    Returns (rr_override, {signal_val: {position: normalized_color}})
    """
    if not rule_str:
        return None, {}

    raw = str(rule_str).strip()
    rr_override = None

    # Check for <RRR> pattern
    m_rr = re.search(r"<([A-Za-z0-9_]+)>", raw)
    if m_rr:
        rr_override = m_rr.group(1).strip()
    elif re.search(r"\bDEF\b|\bDEFAULT\b", raw, re.IGNORECASE):
        rr_override = "DEF"

    value_overrides: Dict[int, Dict[str, str]] = {}
    pattern = r'(\d+)\s*:\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_,:]+))'
    for match in re.finditer(pattern, raw):
        v_num = int(match.group(1))
        content = match.group(2) or match.group(3) or match.group(4) or ""
        head_map: Dict[str, str] = {}
        for part in content.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            k_raw, c_raw = part.split(":", 1)
            k_norm = SIGNAL_POS_ALIASES.get(k_raw.strip().lower(), k_raw.strip().lower())
            c_low = c_raw.strip().lower()
            if c_low in SIGNAL_COLOR_ALIASES:
                c_norm = SIGNAL_COLOR_ALIASES[c_low]
            elif c_low.startswith(("f_", "f")) and len(c_low) >= 2:
                sub = c_low[2:] if c_low.startswith("f_") else c_low[1:]
                base_c = SIGNAL_COLOR_ALIASES.get(sub, sub)
                c_norm = f"flashing_{base_c}" if not base_c.startswith("flashing_") else base_c
            else:
                c_norm = c_low
            head_map[k_norm] = c_norm
        if head_map:
            value_overrides[v_num] = head_map

    return rr_override, value_overrides


def evaluate_ctc_rules(rules_str: str, active_tokens: set) -> bool:
    """
    Evaluates CTC rules string against active packet tokens (case-insensitive).
    Specification: clauses separated by comma (AND).
    Within each clause, terms separated by OR / and/or (OR).
    E.g. 'K11 OR C11,K12 OR C12,K25' -> (K11 or C11) AND (K12 or C12) AND K25
    """
    if not rules_str:
        return False
    clean = rules_str.strip().strip("()")
    if not clean:
        return False
    clauses = [c.strip() for c in clean.split(",") if c.strip()]
    if not clauses:
        return False
    for clause in clauses:
        terms = re.split(r"\s+(?:and/or|or|/)\s+", clause, flags=re.IGNORECASE)
        clause_satisfied = False
        for term in terms:
            t = term.strip().lower()
            if t in active_tokens:
                clause_satisfied = True
                break
        if not clause_satisfied:
            return False
    return True

@dataclass
class BaseElement:
    id: str
    elem_type: str = "element"
    x: int = 0
    y: int = 0
    label: str = ""
    label_pos: str = "top"
    wiu_id: Optional[int] = None
    radio_id: Optional[int] = None
    gps: Optional[Tuple[float, float]] = None
    mile_marker: Optional[float] = None
    ctc_id: str = ""
    ctc_rules: str = ""
    _last_packet_ts: float = field(default=0.0, repr=False)
    _packets_received: int = field(default=0, repr=False)
    _last_raw_hex: str = field(default="", repr=False)
    _last_ctc_ts: float = field(default=0.0, repr=False)
    _last_rssi: Optional[float] = field(default=None, repr=False)
    _rssi_samples: Any = field(default_factory=lambda: deque(maxlen=20), repr=False)

    def add_rssi_sample(self, rssi: Any):
        if not hasattr(self, "_rssi_samples") or self._rssi_samples is None:
            self._rssi_samples = deque(maxlen=20)
        try:
            val = float(rssi)
            self._rssi_samples.append(val)
            self._last_rssi = val
        except (ValueError, TypeError):
            pass

    @property
    def rssi_avg(self) -> Optional[float]:
        samples = getattr(self, "_rssi_samples", None)
        if not samples:
            return self._last_rssi
        return round(sum(samples) / len(samples), 1)

    @property
    def avg_rssi(self) -> Optional[float]:
        return self.rssi_avg

    def update_rssi_from_packet(self, parsed_pkt: dict) -> bool:
        if not parsed_pkt or not isinstance(parsed_pkt, dict):
            return False
        rad_id = getattr(self, "radio_id", None)
        rad_id2 = getattr(self, "radioid", None)
        if self.wiu_id is None and rad_id is None and rad_id2 is None:
            return False

        src_id = parsed_pkt.get("_src_Id_")
        if src_id is None:
            src_id = parsed_pkt.get("_src_id_")
        if src_id is None:
            src_id = parsed_pkt.get("src_id")
        if src_id is None:
            for k, v in parsed_pkt.items():
                if isinstance(k, str) and k.strip().lower() in ("_src_id_", "_src_id", "src_id", "_srcid_"):
                    if v is not None and str(v).strip() != "":
                        src_id = v
                        break
        elif isinstance(src_id, str) and not src_id.strip():
            src_id = None

        matched = False
        if src_id is not None:
            for tid in (self.wiu_id, rad_id, rad_id2):
                if tid is not None and self._wiu_matches(tid, src_id):
                    matched = True
                    break
        else:
            node_id = parsed_pkt.get("_node_id_")
            if node_id is not None:
                if getattr(self, "elem_type", "") == "base":
                    tids = (rad_id, rad_id2, self.wiu_id)
                else:
                    tids = (self.wiu_id, rad_id, rad_id2)
                for tid in tids:
                    if tid is not None and self._wiu_matches(tid, node_id):
                        matched = True
                        break

        if matched:
            rssi = parsed_pkt.get("rssi")
            if rssi is not None:
                self.add_rssi_sample(rssi)
        return matched

    def check_ctc_match(self, parsed_pkt: dict) -> bool:
        pkt_ctc = parsed_pkt.get("_ctc_id_") or parsed_pkt.get("ctc_id")
        if not pkt_ctc and not self.ctc_rules:
            return False

        active_tokens = set()
        if pkt_ctc:
            for tok in re.split(r"[,;\s]+", str(pkt_ctc).strip()):
                if tok:
                    active_tokens.add(tok.lower())

        for k, v in parsed_pkt.items():
            if str(k).startswith("_"):
                if k in ("_tokens_", "_ctc_tokens_") and isinstance(v, (list, tuple, set)):
                    for item in v:
                        active_tokens.add(str(item).lower())
                continue
            if isinstance(v, (list, tuple, set)):
                for item in v:
                    active_tokens.add(str(item).lower())
            elif v in (1, True, "1", "true", "on", "active") or (isinstance(v, int) and v != 0):
                active_tokens.add(str(k).lower())
            elif isinstance(v, str):
                active_tokens.add(v.lower())

        matched = False
        if self.ctc_rules:
            if evaluate_ctc_rules(self.ctc_rules, active_tokens):
                matched = True

        if not matched and pkt_ctc:
            c_val = str(pkt_ctc).strip().lower()
            if self.ctc_id and c_val == self.ctc_id.strip().lower():
                matched = True
            elif not self.ctc_id and c_val == self.id.strip().lower():
                matched = True

        if matched:
            self._last_ctc_ts = float(parsed_pkt.get("timestamp", time.time()))
            return True
        return False

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.check_ctc_match(parsed_pkt)
        self.update_rssi_from_packet(parsed_pkt)
        if self._matches_wiu(parsed_pkt.get("_node_id_")):
            self._last_packet_ts = parsed_pkt.get("timestamp", time.time())
            self._packets_received += 1
            raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
            if raw_hex:
                self._last_raw_hex = str(raw_hex)

    @staticmethod
    def _wiu_matches(wiu_id: Any, node_id: Any) -> bool:
        if wiu_id is None or node_id is None:
            return False
        if wiu_id == node_id:
            return True
        if type(wiu_id) is int and type(node_id) is int:
            return False
        try:
            return int(wiu_id) == int(node_id)
        except (ValueError, TypeError):
            return str(wiu_id).strip().lower() == str(node_id).strip().lower()

    def _matches_wiu(self, node_id: Any) -> bool:
        return self._wiu_matches(self.wiu_id, node_id)

    @staticmethod
    def _extract_packet_rules_and_base(parsed_pkt: dict) -> Optional[Tuple[int, int]]:
        """Safely extracts (pkt_rules_int, base_bits) from a parsed packet, computing base_bits if _base_ is missing."""
        if "_packet_rules_" not in parsed_pkt:
            return None
        raw_rules = parsed_pkt["_packet_rules_"]
        pkt_rules = None
        inferred_bits = None
        if isinstance(raw_rules, int):
            pkt_rules = raw_rules
        elif isinstance(raw_rules, str):
            s = raw_rules.strip()
            if not s:
                return None
            if s.startswith(("0b", "0B", "0x", "0X")):
                try:
                    pkt_rules = int(s, 0)
                except ValueError:
                    return None
            elif all(c in "01" for c in s) and (len(s) > 1 and s.startswith("0") or "_base_" not in parsed_pkt):
                try:
                    pkt_rules = int(s, 2)
                    inferred_bits = len(s)
                except ValueError:
                    return None
            else:
                try:
                    pkt_rules = int(s, 10)
                except ValueError:
                    try:
                        pkt_rules = int(s, 2)
                        inferred_bits = len(s)
                    except ValueError:
                        return None
        else:
            try:
                pkt_rules = int(raw_rules)
            except (ValueError, TypeError):
                return None

        base_bits = None
        if "_base_" in parsed_pkt and parsed_pkt["_base_"] is not None:
            try:
                base_bits = int(parsed_pkt["_base_"])
            except (ValueError, TypeError):
                base_bits = None

        if base_bits is None or base_bits <= 0:
            raw_hex = parsed_pkt.get("raw_hex") or parsed_pkt.get("_raw_hex_") or ""
            clean_hex = re.sub(r"[^0-9a-fA-F]", "", str(raw_hex))
            if len(clean_hex) >= 24 and len(clean_hex) % 2 == 0:
                base_bits = (len(clean_hex) // 2) * 8 - 88
            elif inferred_bits is not None and inferred_bits > 0:
                base_bits = inferred_bits
            elif "pr" in parsed_pkt and isinstance(parsed_pkt["pr"], str) and parsed_pkt["pr"]:
                pr_len = len(parsed_pkt["pr"].strip())
                base_bits = ((pr_len + 7) // 8) * 8
            elif pkt_rules is not None and pkt_rules >= 0:
                blen = max(8, pkt_rules.bit_length())
                base_bits = ((blen + 7) // 8) * 8

        if pkt_rules is None or base_bits is None or base_bits <= 0:
            return None
        return pkt_rules, base_bits

    def to_config_dict(self) -> dict:
        cls = self.__class__
        pub_fields = getattr(cls, "_PUBLIC_FIELD_NAMES", None)
        if pub_fields is None:
            pub_fields = tuple(f.name for f in fields(cls) if not f.name.startswith("_"))
            cls._PUBLIC_FIELD_NAMES = pub_fields
        d = {}
        for k in pub_fields:
            v = getattr(self, k)
            if isinstance(v, dict):
                d[k] = {dk: list(dv) if isinstance(dv, list) else dv for dk, dv in v.items()}
            elif isinstance(v, list):
                d[k] = list(v)
            else:
                d[k] = v
        return d

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = self.to_config_dict()
        d["last_packet_ts"] = self._last_packet_ts
        d["packets_received"] = self._packets_received
        d["raw_hex"] = self._last_raw_hex
        d["last_raw_hex"] = self._last_raw_hex
        d["ctc_id"] = self.ctc_id
        d["ctc_rules"] = self.ctc_rules
        d["ctc_active"] = (time.time() - self._last_ctc_ts < 5.0) if self._last_ctc_ts > 0 else False
        r_avg = self.rssi_avg
        d["last_rssi"] = self._last_rssi
        d["avg_rssi"] = r_avg
        d["rssi_avg"] = r_avg
        return d


@dataclass
class TrackSegment(BaseElement):
    elem_type: str = "track"
    orientation: str = "E-W"
    length_miles: float = 0.5
    neighbors: Dict[str, List[str]] = field(default_factory=dict)
    parallel_track_id: Optional[str] = None
    has_occ_bit: bool = False
    occ_bit_offset: Optional[int] = None

    _hw_occupied: bool = field(default=False, repr=False)
    _inferred_occupied: bool = field(default=False, repr=False)
    _inferred_timestamp: float = field(default=0.0, repr=False)
    _green_path: bool = field(default=False, repr=False)
    _green_path_dir: Optional[str] = field(default=None, repr=False)
    _train_name: Optional[str] = field(default=None, repr=False)
    _train_direction: Optional[str] = field(default=None, repr=False)
    _estimated_speed_mph: Optional[float] = field(default=None, repr=False)
    _collision_warning: bool = field(default=False, repr=False)
    _collision_details: str = field(default="", repr=False)
    _is_dimmed: bool = field(default=False, repr=False)

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.check_ctc_match(parsed_pkt)
        self.update_rssi_from_packet(parsed_pkt)
        if not self._matches_wiu(parsed_pkt.get("_node_id_")):
            return
        self._last_packet_ts = parsed_pkt.get("timestamp", time.time())
        self._packets_received += 1
        raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
        if raw_hex:
            self._last_raw_hex = str(raw_hex)
        if not self.has_occ_bit:
            return
        rb = self._extract_packet_rules_and_base(parsed_pkt)
        if rb is None:
            return

        pkt_rules, base_bits = rb
        bit_no = self.occ_bit_offset

        if bit_no is not None and (base_bits - bit_no - 1) >= 0:
            occup_val = (pkt_rules >> (base_bits - bit_no - 1)) % 2
            self._raw_occ_val = occup_val
            prev_occ = self._hw_occupied
            self._hw_occupied = (occup_val == 1)
            if self._hw_occupied:
                self._inferred_occupied = False
                if not prev_occ:
                    engine.record_train_sighting(self.id, parsed_pkt["timestamp"])
            else:
                self._estimated_speed_mph = None
                self._train_direction = None
                self._train_name = None

    def next_in_direction(self, travel_dir: str, prev_id: Optional[str] = None) -> Tuple[Optional[str], str]:
        if travel_dir in self.neighbors and self.neighbors[travel_dir]:
            raw = self.neighbors[travel_dir]
            cands = [raw] if isinstance(raw, str) else list(raw)
            for cand in cands:
                if cand != prev_id:
                    return cand, travel_dir
        for compat_dir in COMPATIBLE_DIRS.get(travel_dir, (travel_dir,)):
            raw = self.neighbors.get(compat_dir, [])
            cands = [raw] if isinstance(raw, str) else list(raw)
            for cand in cands:
                if cand != prev_id:
                    return cand, compat_dir
        # Fallback for curved tracks (e.g. turning 90 degrees from West to North):
        # find any non-opposite neighbor that is not prev_id
        opp = OPPOSITE_DIR.get(travel_dir)
        for ndir, raw in self.neighbors.items():
            if ndir == opp:
                continue
            cands = [raw] if isinstance(raw, str) else list(raw)
            for cand in cands:
                if cand and cand != prev_id:
                    return cand, ndir
        return None, travel_dir

    @property
    def is_occupied(self) -> bool:
        return self._hw_occupied or self._inferred_occupied

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        color = "WHITE"
        if self._collision_warning or self.is_occupied:
            color = "RED"
        elif self._green_path:
            color = "GREEN"
        if self._collision_warning:
            last_st = f"COLLISION ALERT ({self._collision_details})"
        elif self._hw_occupied:
            last_st = f"OCCUPIED (HW){f' - {self._train_name}' if self._train_name else ''}"
        elif self._inferred_occupied:
            last_st = f"OCCUPIED (INFERRED){f' - {self._train_name}' if self._train_name else ''}"
        elif self._green_path:
            last_st = "CLEAR (GREEN ROUTE)"
        else:
            last_st = "UNOCCUPIED (CLEAR)"
        d.update({
            "hw_occupied": self._hw_occupied, "inferred_occupied": self._inferred_occupied,
            "occupied": self.is_occupied, "green_path": self._green_path, "green_path_dir": self._green_path_dir, "display_color": color,
            "train_name": self._train_name, "train_direction": self._train_direction,
            "estimated_speed_mph": self._estimated_speed_mph, "collision_warning": self._collision_warning,
            "collision_details": self._collision_details, "last_status": last_st,
            "is_dimmed": getattr(self, "_is_dimmed", False),
            "raw_occ_val": getattr(self, "_raw_occ_val", None),
        })
        return d


@dataclass
class Turnout(BaseElement):
    elem_type: str = "turnout"
    common_neighbor: str = ""
    normal_neighbor: str = ""
    reverse_neighbor: str = ""
    orientation: str = "E"
    normal_dir: str = "E"
    reverse_dir: str = "NE"
    cmd_bit_offset: Optional[int] = None
    has_occ_bit: bool = False
    occ_bit_offset: Optional[int] = None
    guarded_by_signals: List[str] = field(default_factory=list)

    _state: int = field(default=0, repr=False)
    _raw_switch_val: Optional[int] = field(default=None, repr=False)
    _out_of_corr: bool = field(default=False, repr=False)
    _raw_occ_val: Optional[int] = field(default=None, repr=False)
    _hw_occupied: bool = field(default=False, repr=False)
    _inferred_occupied: bool = field(default=False, repr=False)
    _inferred_timestamp: float = field(default=0.0, repr=False)
    _green_path: bool = field(default=False, repr=False)
    _green_path_dir: Optional[str] = field(default=None, repr=False)
    _train_name: Optional[str] = field(default=None, repr=False)
    _train_direction: Optional[str] = field(default=None, repr=False)
    _estimated_speed_mph: Optional[float] = field(default=None, repr=False)
    _collision_warning: bool = field(default=False, repr=False)
    _collision_details: str = field(default="", repr=False)
    _is_dimmed: bool = field(default=False, repr=False)

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.check_ctc_match(parsed_pkt)
        self.update_rssi_from_packet(parsed_pkt)
        if not self._matches_wiu(parsed_pkt.get("_node_id_")):
            return
        ts = parsed_pkt.get("timestamp", time.time())
        self._last_packet_ts = ts
        self._packets_received += 1
        raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
        if raw_hex:
            self._last_raw_hex = str(raw_hex)
        for adj_id in (self.common_neighbor, self.normal_neighbor, self.reverse_neighbor):
            if adj_id and hasattr(engine, "elements"):
                adj_el = engine.elements.get(adj_id)
                if adj_el and getattr(adj_el, "wiu_id", None) is None:
                    adj_el._last_packet_ts = ts
        rb = self._extract_packet_rules_and_base(parsed_pkt)
        if rb is None:
            return

        pkt_rules, base_bits = rb

        if self.cmd_bit_offset is not None and (base_bits - self.cmd_bit_offset - 2) >= 0:
            turnout_val = (pkt_rules >> (base_bits - self.cmd_bit_offset - 2)) % 4
            self._raw_switch_val = turnout_val
            mapped = TURNOUT_VALUE_MAP.get(turnout_val)
            if mapped is not None:
                self._state = mapped
                self._out_of_corr = False
            else:
                self._out_of_corr = True

        if self.has_occ_bit and self.occ_bit_offset is not None and (base_bits - self.occ_bit_offset - 1) >= 0:
            occup_val = (pkt_rules >> (base_bits - self.occ_bit_offset - 1)) % 2
            self._raw_occ_val = occup_val
            prev_occ = self._hw_occupied
            self._hw_occupied = (occup_val == 1)
            if self._hw_occupied:
                self._inferred_occupied = False
                if not prev_occ:
                    engine.record_train_sighting(self.id, parsed_pkt["timestamp"])
            else:
                self._estimated_speed_mph = None
                self._train_direction = None
                self._train_name = None

    @property
    def is_occupied(self) -> bool:
        return self._hw_occupied or self._inferred_occupied

    def active_branch_neighbor(self) -> str:
        return self.normal_neighbor if self._state == 0 else self.reverse_neighbor

    def active_branch_dir(self) -> str:
        return self.normal_dir if self._state == 0 else self.reverse_dir

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        if self._collision_warning or self.is_occupied: color = "RED"
        elif self._green_path: color = "GREEN"
        else: color = "WHITE"
        align_str = "STRAIGHT (NORMAL)" if self._state == 0 else "DIVERGING (REVERSE)"
        if self._out_of_corr:
            align_str = f"{TURNOUT_VALUE_DESC.get(self._raw_switch_val, 'UNKNOWN')} (last: {align_str})"
        occ_str = " | OCCUPIED (HW)" if self._hw_occupied else (" | OCCUPIED (INFERRED)" if self._inferred_occupied else (" | GREEN PATH" if self._green_path else ""))
        d.update({
            "state": self._state,
            "alignment": "STRAIGHT" if self._state == 0 else "DIVERGING",
            "raw_switch_val": self._raw_switch_val,
            "switch_desc": TURNOUT_VALUE_DESC.get(self._raw_switch_val, "NO DATA") if self._raw_switch_val is not None else "NO DATA",
            "out_of_correspondence": self._out_of_corr,
            "raw_occ_val": self._raw_occ_val,
            "hw_occupied": self._hw_occupied, "inferred_occupied": self._inferred_occupied,
            "occupied": self.is_occupied, "green_path": self._green_path, "green_path_dir": self._green_path_dir, "display_color": color,
            "train_name": self._train_name, "train_direction": self._train_direction,
            "estimated_speed_mph": self._estimated_speed_mph, "collision_warning": self._collision_warning,
            "collision_details": self._collision_details, "last_status": f"{align_str}{occ_str}",
            "is_dimmed": getattr(self, "_is_dimmed", False),
        })
        return d


@dataclass
class Signal(BaseElement):
    elem_type: str = "signal"
    position: str = "top"
    direction: str = "E"
    protects_element: str = ""
    rear_element: str = ""
    governed_route: Optional[str] = None
    sig_bit_offset: int = 0
    has_occ_bit: bool = False
    occ_bit_offset: Optional[int] = None
    signal_rules: str = ""
    signal_override: str = ""

    _signal_val: int = field(default=32, repr=False)
    _raw_aspect_str: str = field(default="gray", repr=False)
    _lamp_aspect: str = field(default="gray", repr=False)
    _prev_lamp_aspect: str = field(default="gray", repr=False)
    _last_reported_color: Optional[str] = field(default=None, repr=False)
    _dropped_to_red_ts: Optional[float] = field(default=None, repr=False)
    _hw_occupied: bool = field(default=False, repr=False)

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.check_ctc_match(parsed_pkt)
        self.update_rssi_from_packet(parsed_pkt)
        if not self._matches_wiu(parsed_pkt.get("_node_id_")):
            return
        ts = parsed_pkt.get("timestamp", time.time())
        self._last_packet_ts = ts
        self._packets_received += 1
        raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
        if raw_hex:
            self._last_raw_hex = str(raw_hex)
        for adj_id in (self.protects_element, self.rear_element):
            if adj_id and hasattr(engine, "elements"):
                adj_el = engine.elements.get(adj_id)
                if adj_el and getattr(adj_el, "wiu_id", None) is None:
                    adj_el._last_packet_ts = ts
        rb = self._extract_packet_rules_and_base(parsed_pkt)
        if rb is None:
            return

        pkt_rules, base_bits = rb
        bit_no = self.sig_bit_offset

        if (base_bits - bit_no - 5) >= 0:
            signal_val = (pkt_rules >> (base_bits - bit_no - 5)) % 32
            rule_str = self.signal_rules or self.signal_override or ""
            aspect_str = engine.lookup_signal_aspect(self.wiu_id, signal_val, signal_rules=rule_str)
            resolved_lamp = resolve_signal_lamp_aspect(aspect_str, self.position)

            if resolved_lamp is not None:
                was_permissive = self.is_permissive
                self._prev_lamp_aspect = self._lamp_aspect
                self._signal_val = signal_val
                self._raw_aspect_str = aspect_str
                self._lamp_aspect = resolved_lamp.lower()
                c = self.base_color
                if c not in ("GRAY", "DARK", "BLACK"):
                    self._last_reported_color = c

                if was_permissive and self.is_restricted:
                    self._dropped_to_red_ts = parsed_pkt["timestamp"]
                    engine.notify_signal_dropped(self, parsed_pkt["timestamp"])

        if self.has_occ_bit and self.occ_bit_offset is not None and (base_bits - self.occ_bit_offset - 1) >= 0:
            occup_val = (pkt_rules >> (base_bits - self.occ_bit_offset - 1)) % 2
            self._hw_occupied = (occup_val == 1)

    @property
    def is_dark(self) -> bool: return self._lamp_aspect in ("black", "gray", "dark")
    @property
    def is_stop_red(self) -> bool: return self._lamp_aspect in ("red", "red_intermediate")
    @property
    def is_mow_magenta(self) -> bool: return "magenta" in self._lamp_aspect
    @property
    def is_restricted(self) -> bool: return self.is_stop_red or self.is_mow_magenta
    @property
    def is_permissive(self) -> bool: return not self.is_dark and not self.is_restricted
    @property
    def is_flashing(self) -> bool: return self._lamp_aspect.startswith("flashing_")
    @property
    def base_color(self) -> str: return self._lamp_aspect.replace("flashing_", "").replace("_intermediate", "").upper()
    @property
    def last_reported_color(self) -> str: return self._last_reported_color or self.base_color

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        if self.is_dark: status = "DARK"
        elif self.is_stop_red: status = "STOP"
        elif self.is_mow_magenta: status = "MOW_STOP"
        else: status = "GO"
        d.update({
            "signal_val": self._signal_val, "aspect_code": self._signal_val,
            "raw_aspect_str": self._raw_aspect_str, "lamp_aspect": self._lamp_aspect,
            "base_color": self.base_color, "flashing": self.is_flashing,
            "last_reported_color": self.last_reported_color,
            "heads": [self._lamp_aspect.upper()], "status": status, "hw_occupied": self._hw_occupied,
            "last_status": f"{status} ({self._lamp_aspect.upper()} - Code {self._signal_val})",
            "signal_rules": self.signal_rules or self.signal_override,
            "signal_override": self.signal_rules or self.signal_override,
        })
        return d


@dataclass
class Crossing(BaseElement):
    monitored_tracks: List[str] = field(default_factory=list)
    _gate_status: str = field(default="OPEN", repr=False)
    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        d["gate_status"] = self._gate_status
        d["last_status"] = f"GATE {self._gate_status}"
        return d

@dataclass
class BaseRadio(BaseElement):
    channel: int = 1
    radio_id: Optional[int] = None
    _packets_relayed: int = field(default=0, repr=False)
    _config_gps: Optional[Tuple[float, float]] = field(default=None, repr=False)
    transmitted_gps: Optional[Tuple[float, float]] = field(default=None, repr=False)
    channels: List[Any] = field(default_factory=list, repr=False)
    channel_details: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self._config_gps is None and self.gps is not None:
            self._config_gps = tuple(self.gps) if isinstance(self.gps, (list, tuple)) else self.gps
        if self.radio_id is not None:
            try:
                self.radio_id = int(self.radio_id)
            except (ValueError, TypeError):
                pass

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.update_rssi_from_packet(parsed_pkt)
        node_id = parsed_pkt.get("_node_id_")
        ch_val = parsed_pkt.get("channel_meta", parsed_pkt.get("Channel"))
        if ch_val is not None and ch_val == self.channel:
            self._packets_relayed += 1

        matched_radio = False
        target_id = self.radio_id if self.radio_id is not None else self.wiu_id
        if target_id is not None and node_id is not None:
            try:
                matched_radio = (int(target_id) == int(node_id))
            except (ValueError, TypeError):
                matched_radio = (str(target_id) == str(node_id))
        elif target_id is None:
            if ch_val is not None and ch_val == self.channel:
                matched_radio = True
            elif any(k.startswith("chan_") for k in parsed_pkt):
                matched_radio = True

        if matched_radio:
            self._packets_received += 1
            self._last_packet_ts = parsed_pkt.get("timestamp", time.time())
            raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
            if raw_hex:
                self._last_raw_hex = str(raw_hex)

            # Extract 6 channels defined by chan_n in the base packet dictionary
            chan_entries = []
            for k, v in parsed_pkt.items():
                m = re.match(r"^chan_?(\d+)$", k, re.IGNORECASE)
                if m:
                    chan_entries.append((int(m.group(1)), k, v))

            if chan_entries:
                chan_entries.sort(key=lambda item: item[0])
                self.channels = [item[2] for item in chan_entries[:6]]
                self.channel_details = {item[1]: item[2] for item in chan_entries[:6]}
            elif "channels" in parsed_pkt and isinstance(parsed_pkt["channels"], list):
                self.channels = parsed_pkt["channels"][:6]
                self.channel_details = {f"chan_{i}": v for i, v in enumerate(self.channels)}

            # Extract GPS coordinates defined by lat and lon in base packet dictionary
            lat = parsed_pkt.get("lat")
            if lat is None:
                lat = parsed_pkt.get("latitude")
            lon = parsed_pkt.get("lon")
            if lon is None:
                lon = parsed_pkt.get("longitude")

            if lat is not None and lon is not None:
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                    if self._config_gps is None and self.gps is not None and self.transmitted_gps is None:
                        self._config_gps = tuple(self.gps) if isinstance(self.gps, (list, tuple)) else self.gps
                    self.transmitted_gps = (lat_f, lon_f)
                    self.gps = (lat_f, lon_f)
                except (ValueError, TypeError):
                    pass

    def to_config_dict(self) -> dict:
        d = super().to_config_dict()
        if self.transmitted_gps is not None:
            d["gps"] = self._config_gps
        d.pop("channels", None)
        d.pop("channel_details", None)
        d.pop("transmitted_gps", None)
        return d

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict(layout_wiu_ids, rrdata)
        d["radio_id"] = self.radio_id
        d["packets_relayed"] = self._packets_relayed
        if self.transmitted_gps is not None:
            d["gps"] = self.transmitted_gps
            d["transmitted_gps"] = self.transmitted_gps
        d["channels"] = list(self.channels)
        d["channel_details"] = dict(self.channel_details)
        id_str = f" [ID:{self.radio_id}]" if self.radio_id is not None else ""
        d["last_status"] = f"CHANNEL {self.channel} BASE{id_str} ({self._packets_relayed} pkts)"
        return d

@dataclass
class TrackJump(BaseElement):
    letter: str = "A"
    paired_jump_id: str = ""
    direction: str = "E"
    connected_element: str = ""

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        d["last_status"] = f"CONNECTOR [{self.letter}] -> {self.paired_jump_id or 'UNPAIRED'} (Adj: {self.connected_element or 'None'})"
        return d

@dataclass
class DisplayElement(BaseElement):
    extra_info: str = ""
    width: int = 0
    height: int = 0

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict()
        d["last_status"] = f"{self.elem_type.upper()} ({self.label or self.id})"
        return d


@dataclass
class PTCDateTime(BaseElement):
    width: int = 240
    height: int = 50
    last_datetime_str: str = "--- --:--:-- UTC"
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    second: Optional[int] = None
    last_source_wiu: Optional[Any] = None

    def process_packet(self, parsed_pkt: dict, engine: "DispatcherEngine"):
        self.update_rssi_from_packet(parsed_pkt)
        pkt_type = parsed_pkt.get("_type_")
        is_dt = False
        if pkt_type is not None:
            s_type = str(pkt_type).strip()
            if s_type in ("Date/Time", "Date/Time:", "DateTime", "PTC Date/Time") or s_type.startswith("Date/Time"):
                is_dt = True
            elif pkt_type == 6:
                is_dt = True
        elif any(k in parsed_pkt for k in ("year2", "year")) and all(k in parsed_pkt for k in ("mon", "day", "hh", "mm", "ss")):
            is_dt = True

        if not is_dt:
            return

        target_wiu = getattr(self, "wiu_id", None)
        node_id = parsed_pkt.get("_node_id_")
        if target_wiu is not None and node_id is not None:
            try:
                if int(target_wiu) != int(node_id):
                    return
            except (ValueError, TypeError):
                if str(target_wiu) != str(node_id):
                    return

        year_val = parsed_pkt.get("year2", parsed_pkt.get("year"))
        mon_val = parsed_pkt.get("mon", parsed_pkt.get("month"))
        day_val = parsed_pkt.get("day")
        hh_val = parsed_pkt.get("hh", parsed_pkt.get("hour"))
        mm_val = parsed_pkt.get("mm", parsed_pkt.get("minute"))
        ss_val = parsed_pkt.get("ss", parsed_pkt.get("second"))

        dt_str = ""
        if year_val is not None and mon_val is not None and day_val is not None and hh_val is not None and mm_val is not None and ss_val is not None:
            try:
                y = int(year_val)
                if y < 100:
                    y += 2000
                m = int(mon_val)
                d = int(day_val)
                h = int(hh_val)
                mi = int(mm_val)
                s = int(ss_val)
                dt_str = f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d} UTC"
                self.year, self.month, self.day = y, m, d
                self.hour, self.minute, self.second = h, mi, s
            except (ValueError, TypeError):
                pass

        if not dt_str and parsed_pkt.get("_scroll_ptc_"):
            m = re.search(r"(\d{1,2}:\d{2}:\d{2})\s+(\d{1,2}/\d{1,2}/\d{2,4})", str(parsed_pkt["_scroll_ptc_"]))
            if m:
                dt_str = f"{m.group(2)} {m.group(1)} UTC"

        if not dt_str and parsed_pkt.get("datetime"):
            dt_str = str(parsed_pkt["datetime"])

        if dt_str:
            self.last_datetime_str = dt_str

        self._packets_received += 1
        self._last_packet_ts = parsed_pkt.get("timestamp", time.time())
        self.last_source_wiu = node_id
        raw_hex = parsed_pkt.get("raw_hex", parsed_pkt.get("_raw_hex_", ""))
        if raw_hex:
            self._last_raw_hex = str(raw_hex)
        self._active = True

    def to_config_dict(self) -> dict:
        d = super().to_config_dict()
        d["width"] = getattr(self, "width", 240) or 240
        d["height"] = getattr(self, "height", 50) or 50
        return d

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        d = super().to_state_dict(layout_wiu_ids, rrdata)
        d["datetime_str"] = self.last_datetime_str
        d["last_datetime_str"] = self.last_datetime_str
        d["year"] = self.year
        d["month"] = self.month
        d["day"] = self.day
        d["hour"] = self.hour
        d["minute"] = self.minute
        d["second"] = self.second
        d["last_source_wiu"] = self.last_source_wiu
        d["packets_received"] = self._packets_received
        d["last_packet_ts"] = self._last_packet_ts
        d["last_raw_hex"] = getattr(self, "_last_raw_hex", "")
        d["width"] = getattr(self, "width", 240) or 240
        d["height"] = getattr(self, "height", 50) or 50
        d["last_status"] = f"PTC CLOCK: {self.last_datetime_str} ({self._packets_received} pkts)"
        return d

# =====================================================================
# SYSTEM TELEMETRY DISPLAY ELEMENTS
# =====================================================================

class PacketScroller:
    def __init__(self, x: int = 0, y: int = 25, max_len: int = 12, width: int = 580, height: int = 195):
        self.id = "SYS_PACKET_SCROLLER"
        self.elem_type = "packet_scroller"
        self.x, self.y, self.max_len = x, y, max_len
        self.width = int(width) if width and int(width) > 0 else 580
        self.height = int(height) if height and int(height) > 0 else 195
        self.packets: deque = deque(maxlen=max(300, max_len * 20))
        self.start_time: float = time.time()
        self.total_packets: int = 0
        self.packet_times: deque = deque()
        self.channel_totals: Dict[int, int] = {}
        self.channel_times: Dict[int, deque] = {}

    def _prune_old_timestamps(self, now: float):
        while self.packet_times and now - self.packet_times[0] > 60.0:
            self.packet_times.popleft()
        for ch_q in self.channel_times.values():
            while ch_q and now - ch_q[0] > 60.0:
                ch_q.popleft()

    def add_packet(self, parsed_pkt: dict):
        now = float(parsed_pkt.get("timestamp") or time.time())
        scroll_txt = parsed_pkt.get("_scroll_ptc_", "")
        rssi = parsed_pkt.get("rssi", -75.0)
        ch_meta = parsed_pkt.get("channel_meta")
        ch_raw = ch_meta if (ch_meta is not None and ch_meta != 0) else parsed_pkt.get("Channel", ch_meta or 0)
        try:
            ch = int(ch_raw) if ch_raw is not None else 0
        except (ValueError, TypeError):
            ch = 0
        seq = parsed_pkt.get("_sequence_", "-")
        self.total_packets += 1
        self.packet_times.append(now)
        self.channel_totals[ch] = self.channel_totals.get(ch, 0) + 1
        self.channel_times.setdefault(ch, deque()).append(now)
        self._prune_old_timestamps(now)
        self.packets.appendleft(f"[{time.strftime('%H:%M:%S')}] Ch:{ch} Seq:{seq} RSSI:{rssi:.1f}dBm | {scroll_txt}")

    def add_invalid_packet(self, raw_packet: Any, meta: Optional[dict] = None):
        now = time.time()
        meta = meta or {}
        rssi = meta.get("rssi", -75.0)
        ch_raw = meta.get("channel", 0)
        try:
            ch = int(ch_raw) if ch_raw is not None else 0
        except (ValueError, TypeError):
            ch = 0
        if isinstance(raw_packet, (bytes, bytearray)):
            hex_str = bytes(raw_packet).hex().upper()
        elif isinstance(raw_packet, str):
            s = raw_packet.strip()
            hex_str = s.upper() if all(c in "0123456789abcdefABCDEF" for c in s) and s else s.encode("utf-8", errors="ignore").hex().upper()
        else:
            hex_str = str(raw_packet)
        self.total_packets += 1
        self.packet_times.append(now)
        self.channel_totals[ch] = self.channel_totals.get(ch, 0) + 1
        self.channel_times.setdefault(ch, deque()).append(now)
        self._prune_old_timestamps(now)
        self.packets.appendleft(f"[{time.strftime('%H:%M:%S')}] Ch:{ch} RSSI:{rssi:.1f}dBm | INVALID HEX: {hex_str}")

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        now = time.time()
        self._prune_old_timestamps(now)
        elapsed_sec = max(0.0, now - self.start_time)
        el_int = int(elapsed_sec)
        elapsed_str = f"{el_int // 3600:02d}:{(el_int % 3600) // 60:02d}:{el_int % 60:02d}"
        channel_stats = {
            str(ch): {
                "total": self.channel_totals[ch],
                "ppm": len(self.channel_times.get(ch, ())),
            }
            for ch in sorted(self.channel_totals.keys())
        }
        return {
            "id": self.id,
            "elem_type": self.elem_type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "max_len": self.max_len,
            "start_time": self.start_time,
            "elapsed_sec": round(elapsed_sec, 1),
            "elapsed_str": elapsed_str,
            "total_packets": self.total_packets,
            "total_ppm": len(self.packet_times),
            "channel_stats": channel_stats,
            "entries": list(self.packets),
            "packets_received": self.total_packets,
            "last_packet_ts": self.packet_times[-1] if self.packet_times else 0.0,
            "last_status": f"{self.total_packets} pkts ({len(self.packet_times)}/min)",
        }

def format_wiu_display_name(raw_name: Optional[str], fallback_id: Any) -> str:
    """If name is not empty, display the first word for it replacing '_' with spaces.
    E.g. if a name is 'Nevada_CP S1E', returns 'Nevada CP'.
    Otherwise returns fallback_id as string.
    """
    if raw_name:
        s = str(raw_name).strip()
        if s:
            first_word = s.split()[0]
            replaced = first_word.replace("_", " ")
            if replaced:
                return replaced
    return str(fallback_id)


class AddressTable:
    def __init__(self, x: int = 46, y: int = 25, max_len: int = 10, width: int = 520, height: int = 150, dedup_ttl: float = 0.4):
        self.id = "SYS_ADDRESS_TABLE"
        self.elem_type = "address_table"
        self.x, self.y = x, y
        self.max_len = max_len
        self.width = int(width) if width and int(width) > 0 else 520
        self.height = int(height) if height and int(height) > 0 else 150
        self.wius: Dict[int, dict] = {}
        self.dedup_ttl: float = max(0.0, float(dedup_ttl))

    @property
    def total_packets(self) -> int:
        return sum(r.get("packet_count", 0) for r in self.wius.values())

    def check_and_update_sequence(self, parsed_pkt: dict, rrdata: Optional[Dict[str, str]] = None) -> bool:
        wiu_id = parsed_pkt.get("_node_id_")
        if wiu_id is None: return False
        try:
            wiu_id = int(wiu_id)
        except (ValueError, TypeError):
            pass
        seq = parsed_pkt.get("_sequence_")
        now_t = float(parsed_pkt.get("timestamp") or time.time())
        ttl = getattr(self, "dedup_ttl", 0.4)
        if wiu_id in self.wius and seq is not None:
            last_seq = self.wius[wiu_id].get("last_sequence")
            last_t = float(self.wius[wiu_id].get("last_seq_time", self.wius[wiu_id].get("last_seen", 0.0)))
            if last_seq is not None:
                seq_int, last_int = int(seq), int(last_seq)
                if ttl > 0.0:
                    if (now_t - last_t) < ttl:
                        if seq_int == last_int: return False
                        if (seq_int - last_int) % 256 >= 128: return False

        ch_meta = parsed_pkt.get("channel_meta")
        ch = ch_meta if (ch_meta is not None and ch_meta != 0) else parsed_pkt.get("Channel", ch_meta or 0)
        rssi = parsed_pkt.get("rssi", -99.0)
        try:
            rssi_val = float(rssi)
        except (ValueError, TypeError):
            rssi_val = -99.0

        if isinstance(wiu_id, int) and wiu_id > 10000000:
            rr_code = str(wiu_id)[1:4] if len(str(wiu_id)) >= 4 else "DEF"
        elif isinstance(wiu_id, str) and wiu_id.isdigit() and int(wiu_id) > 10000000:
            rr_code = str(wiu_id)[1:4] if len(str(wiu_id)) >= 4 else "DEF"
        else: rr_code = "RADIO"

        # Check for _src_Id_ in parsed_pkt
        src_id = None
        for k, v in parsed_pkt.items():
            if str(k).strip().lower() in ("_src_id_", "_src_id", "src_id", "_srcid_"):
                if v is not None and str(v).strip() != "":
                    try:
                        src_id = int(v)
                    except (ValueError, TypeError):
                        src_id = v
                    break

        if src_id is not None and src_id in self.wius and src_id != wiu_id:
            s_rec = self.wius[src_id]
            s_rec["last_rssi"] = rssi_val
            if "rssi_samples" not in s_rec or not isinstance(s_rec["rssi_samples"], deque):
                s_rec["rssi_samples"] = deque(maxlen=20)
            s_rec["rssi_samples"].append(rssi_val)
            s_rec["avg_rssi"] = round(sum(s_rec["rssi_samples"]) / len(s_rec["rssi_samples"]), 1)
            s_rec["rssi_avg"] = s_rec["avg_rssi"]
            if rssi_val > s_rec.get("high_watermark_rssi", -99.0):
                s_rec["high_watermark_rssi"] = rssi_val

        if wiu_id not in self.wius:
            samples = deque(maxlen=20)
            samples.append(rssi_val)
            avg = round(sum(samples) / len(samples), 1)
            self.wius[wiu_id] = {
                "wiu_id": wiu_id, "rr_code": rr_code,
                "rr_name": (rrdata or {}).get(rr_code, rr_code),
                "channels": {ch}, "high_watermark_rssi": rssi_val,
                "last_rssi": rssi_val, "rssi_samples": samples,
                "avg_rssi": avg, "rssi_avg": avg,
                "last_sequence": seq, "packet_count": 1,
                "last_seen": parsed_pkt.get("timestamp", now_t),
                "last_seq_time": now_t,
            }
        else:
            rec = self.wius[wiu_id]
            rec["rr_name"] = (rrdata or {}).get(rr_code, rr_code)
            rec["channels"].add(ch)
            rec["last_rssi"] = rssi_val
            if rssi_val > rec.get("high_watermark_rssi", -99.0):
                rec["high_watermark_rssi"] = rssi_val
            if "rssi_samples" not in rec or not isinstance(rec["rssi_samples"], deque):
                rec["rssi_samples"] = deque(maxlen=20)
            rec["rssi_samples"].append(rssi_val)
            rec["avg_rssi"] = round(sum(rec["rssi_samples"]) / len(rec["rssi_samples"]), 1)
            rec["rssi_avg"] = rec["avg_rssi"]
            if seq is not None:
                rec["last_sequence"] = seq
                rec["last_seq_time"] = now_t
            rec["packet_count"] += 1
            rec["last_seen"] = parsed_pkt.get("timestamp", now_t)
        return True

    def to_state_dict(
        self,
        layout_wiu_ids: Optional[set] = None,
        rrdata: Optional[Dict[str, str]] = None,
        layout_wiu_names: Optional[Dict[int, str]] = None,
        element_avg_rssi: Optional[dict] = None,
    ) -> dict:
        serialized = {}
        for wid, rec in sorted(
            self.wius.items(),
            key=lambda item: (0, int(item[0])) if str(item[0]).lstrip("-").isdigit() else (1, str(item[0])),
            reverse=True,
        ):
            try:
                wid_int = int(wid)
            except (ValueError, TypeError):
                wid_int = -1
            raw_name = (layout_wiu_names or {}).get(wid_int, "") or rec.get("wiu_name", "")
            disp_name = format_wiu_display_name(raw_name, wid)
            rec_dict = {k: v for k, v in rec.items() if k != "rssi_samples"}
            avg_rssi = rec.get("avg_rssi")
            if element_avg_rssi and (wid_int in element_avg_rssi or str(wid) in element_avg_rssi):
                el_avg = element_avg_rssi.get(wid_int, element_avg_rssi.get(str(wid)))
                if el_avg is not None:
                    avg_rssi = el_avg
            elif avg_rssi is None and "rssi_samples" in rec and rec["rssi_samples"]:
                avg_rssi = round(sum(rec["rssi_samples"]) / len(rec["rssi_samples"]), 1)
            elif avg_rssi is None:
                avg_rssi = rec.get("last_rssi")
            rec_dict["avg_rssi"] = avg_rssi
            rec_dict["rssi_avg"] = avg_rssi
            rec_dict["last_rssi"] = rec.get("last_rssi")
            serialized[str(wid)] = {
                **rec_dict,
                "wiu_name": raw_name,
                "wiu_display_name": disp_name,
                "rr_name": (rrdata or {}).get(rec.get("rr_code", "DEF"), rec.get("rr_name", rec.get("rr_code", "DEF"))),
                "on_layout": wid_int in (layout_wiu_ids or set()),
                "channels": sorted(list(rec["channels"])),
            }
        tot_pkts = sum(r.get("packet_count", 0) for r in self.wius.values())
        last_ts = max((r.get("last_seen", 0.0) for r in self.wius.values()), default=0.0)
        return {
            "id": self.id, "elem_type": self.elem_type, "x": self.x, "y": self.y,
            "width": self.width, "height": self.height, "max_len": self.max_len,
            "total_unique_wius": len(self.wius),
            "wius": serialized, "packets_received": tot_pkts, "last_packet_ts": last_ts,
            "last_status": f"{len(self.wius)} Active WIUs ({tot_pkts} pkts)",
        }


class ChatPanel:
    """Short encrypted text chat element state manager for DispatcherEngine."""
    def __init__(
        self,
        x: int = 76,
        y: int = 22,
        max_len: int = 12,
        width: int = 460,
        height: int = 220,
    ):
        self.id = "SYS_CHAT"
        self.elem_type = "chat"
        self.x = x
        self.y = y
        self.max_len = max_len
        self.width = width
        self.height = height
        self.messages: deque = deque(maxlen=250)
        self._seen_keys: set = set()
        self._local_seq: int = 0
        self.total_messages: int = 0
        self.last_message_ts: float = 0.0

    def add_message(self, msg: dict) -> Optional[dict]:
        import mosfet_client
        if not isinstance(msg, dict):
            return None
        raw_text = msg.get("text", "")
        clean_text = mosfet_client.sanitize_chat_text(raw_text)
        if not clean_text:
            return None

        sender_id = mosfet_client.sanitize_chat_identity(
            msg.get("sender_identity") or msg.get("identity") or msg.get("sender") or "Anonymous"
        ) or "Anonymous"

        try:
            ts = float(msg.get("timestamp") or time.time())
        except (ValueError, TypeError):
            ts = time.time()

        raw_id = msg.get("msg_id") if msg.get("msg_id") is not None else msg.get("id")
        try:
            msg_id = int(raw_id) if raw_id is not None else 0
        except (ValueError, TypeError):
            msg_id = 0

        if msg_id > 0:
            dedup_key = ("id", msg_id, sender_id, clean_text)
        else:
            dedup_key = ("ts", sender_id, int(ts // 2), clean_text)

        bucket_key = ("bucket", sender_id, int(ts // 2), clean_text)
        if dedup_key in self._seen_keys or (msg_id == 0 and bucket_key in self._seen_keys):
            return None

        self._seen_keys.add(dedup_key)
        self._seen_keys.add(bucket_key)
        if len(self._seen_keys) > 1000:
            # Prune oldest arbitrary half when set grows large
            self._seen_keys = set(list(self._seen_keys)[-500:])

        if msg_id <= 0:
            self._local_seq += 1
            msg_id = self._local_seq
        elif msg_id > self._local_seq:
            self._local_seq = msg_id

        raw_targets = msg.get("target_identities")
        targets: List[str] = []
        if isinstance(raw_targets, list):
            for t in raw_targets:
                ct = mosfet_client.sanitize_chat_identity(t)
                if ct and ct not in targets:
                    targets.append(ct)
        for m in mosfet_client.extract_chat_mentions(clean_text):
            if m not in targets:
                targets.append(m)

        dt_obj = datetime.datetime.fromtimestamp(ts)
        time_str = msg.get("time_str") or dt_obj.strftime("%H:%M:%S")
        date_str = msg.get("date_str") or dt_obj.strftime("%Y-%m-%d %H:%M:%S")

        record = {
            "msg_id": msg_id,
            "timestamp": ts,
            "time_str": str(time_str)[:16],
            "date_str": str(date_str)[:32],
            "sender_identity": sender_id,
            "text": clean_text,
            "target_identities": targets[:16],
        }
        self.messages.append(record)
        self.total_messages += 1
        if ts > self.last_message_ts:
            self.last_message_ts = ts
        return record

    def clear(self):
        self.messages.clear()

    def to_state_dict(self, active_identity: str = "") -> dict:
        msg_list = list(self.messages)
        entries = [
            f"[{m.get('time_str', '')}] {m.get('sender_identity', 'Anon')}: {m.get('text', '')}"
            for m in msg_list
        ]
        return {
            "id": self.id,
            "elem_type": self.elem_type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "max_len": self.max_len,
            "active_identity": active_identity,
            "messages": msg_list,
            "entries": entries,
            "total_messages": self.total_messages,
            "packets_received": len(msg_list),
            "last_packet_ts": self.last_message_ts,
            "last_status": f"{len(msg_list)} Chat Msg(s)",
        }

# TrainTracker / TrainScroller imported from train_logic

# =====================================================================
# CORE DISPATCHER LOGIC ENGINE & TCP LAYOUT SERVER
# =====================================================================

class DispatcherEngine:
    def __init__(
        self,
        config: Optional[dict] = None,
        layout_filepath: Optional[str] = None,
        inferred_timeout_sec: float = 8.0,
        mcast_group: str = "224.1.1.1",
        mcast_port: int = 5007,
        layout_server_port: int = 5008,
        time_scale: float = 360.0,
        packet_cache_size: Optional[int] = None,
        train_logic_interval_sec: float = 7.0,
        dedup_ttl: Optional[float] = None,
        unicast_clients: Optional[List[str]] = None,
    ):
        self.version = VERSION
        self.config = config or {}
        if "engine" in self.config and isinstance(self.config["engine"], dict):
            eng_sub = self.config["engine"]
            if "parsers" in eng_sub and "parsers" not in self.config:
                self.config["parsers"] = eng_sub["parsers"]
            if "connections" in eng_sub and "connections" not in self.config:
                self.config["connections"] = eng_sub["connections"]
        self.quiet: bool = bool(self.config.get("quiet", False))
        self.log_fn = self.config.get("log_fn", None)
        self.load_packet_cache_enabled: bool = bool(self.config.get("load_packet_cache", True))
        self.save_packet_cache_enabled: bool = bool(self.config.get("save_packet_cache", True))
        self.config_dir = self.config.get("config_dir", os.getcwd())
        self.inferred_timeout_sec = self.config.get("inferred_timeout_sec", inferred_timeout_sec)
        self.train_logic_interval_sec = float(self.config.get("train_logic_interval_sec", train_logic_interval_sec))
        self.last_train_eval_ts: float = 0.0
        self.enable_train_inference: bool = bool(self.config.get("enable_train_inference", True))
        self.time_scale = self.config.get("time_scale", time_scale)
        chosen_layout = (
            layout_filepath
            or self.config.get("default_layout")
            or self.config.get("layout", "complex_layout.json")
        )
        self.layout_filepath: str = self._resolve_layout_path(chosen_layout)
        self.layout_filename: str = os.path.basename(self.layout_filepath)

        cfg_dedup = (
            dedup_ttl
            if dedup_ttl is not None
            else self.config.get("dedup_ttl", self.config.get("dedup_window", 0.4))
        )
        try:
            self.dedup_ttl: float = max(0.0, float(cfg_dedup))
        except (ValueError, TypeError):
            self.dedup_ttl = 0.4

        self.parsers: List[PacketParser] = []
        self.reload_parsers()

        self.signal_tables_cache: Dict[str, Dict[int, str]] = {}
        self.rrdata: Dict[str, str] = {}
        self.thanks_text: str = ""
        self._orig_element_bindings: Dict[str, dict] = {}
        self.last_packets_by_wiu: Dict[int, dict] = {}
        self.elements: Dict[str, BaseElement] = {}
        self._sorted_element_ids: List[str] = []

        self.scroller_enabled = False
        self.address_table_enabled = False
        self.train_scroller_enabled = False
        self.scroller = PacketScroller(x=0, y=22, max_len=10)
        self.address_table = AddressTable(x=26, y=22, dedup_ttl=self.dedup_ttl)
        self.train_scroller = TrainScroller(x=50, y=22, max_len=10)
        self.chat_enabled = False
        self.chat_panel = ChatPanel(x=76, y=22, max_len=12, width=460, height=220)
        self._chat_lock = threading.Lock()
        self._last_banc_chat_id: int = 0
        self._last_banc_chat_ts: float = 0.0
        conn_cfg = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        raw_cfg_ident = conn_cfg.get("identity") or self.config.get("identity") or ""
        self.chat_self_identity: str = str(raw_cfg_ident).strip() if str(raw_cfg_ident).strip().lower() not in ("", "auto", "none") else ""
        self.chat_self_name: str = ""
        self.chat_contacts: Dict[str, str] = {}
        self._load_local_chat_identities()

        self.recent_sightings: deque = deque(maxlen=25)
        self.active_collisions: List[dict] = []
        self._last_transmitted_signature: str = ""
        self.transmitted_frames_count: int = 0
        self.suppressed_frames_count: int = 0
        self.last_ingest_status: str = "IDLE"
        self.last_parsed_packet: Optional[dict] = None
        self.last_seen_banc_time: float = 0.0

        self.mcast_group = self.config.get("engine_mcast_group", self.config.get("mcast_group", mcast_group))
        self.mcast_port = int(self.config.get("engine_mcast_port", self.config.get("mcast_port", mcast_port)))
        self._mcast_transfer_id: int = 0
        cfg_clients = self.config.get("unicast_clients", [])
        if isinstance(cfg_clients, str):
            cfg_clients = [c.strip() for c in cfg_clients.split(",") if c.strip()]
        self.unicast_clients: List[str] = list(unicast_clients or cfg_clients or [])
        self._client_subscribers: Dict[str, float] = {}
        self._clients_lock = threading.Lock()
        self._tcp_stream_clients: List[socket.socket] = []
        self._tcp_stream_lock = threading.Lock()
        self._egress_interfaces: List[str] = []
        self._last_egress_if_refresh: float = 0.0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except (AttributeError, OSError):
            pass
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * 1048576)
        except (AttributeError, OSError) as ex:
            print(f"Exception: {ex}")
            raise
        try:
            self.sock.setblocking(False)
        except (AttributeError, OSError):
            pass
        self._sock_lock = threading.Lock()

        self.layout_server_host = self.config.get("layout_server_host", "0.0.0.0")
        self.layout_server_port = int(self.config.get("layout_server_port", layout_server_port))
        self._server_thread: Optional[threading.Thread] = None
        self._server_running: bool = False

        # In-memory RAM packet history cache (default 1000, loaded from configuration if present)
        cfg_cache_size = (
            packet_cache_size
            or self.config.get("packet_cache_size")
            or self.config.get("packet_history_limit")
            or self.config.get("packet_cache_limit")
            or 1000
        )
        try:
            self.packet_cache_size: int = max(1, int(cfg_cache_size))
        except (ValueError, TypeError):
            self.packet_cache_size = 1000

        self.packet_cache: deque = deque(maxlen=self.packet_cache_size)
        self.packet_history = self.packet_cache
        self.last_raw_hex: str = ""
        self.raw_packet_logging: bool = False
        self.raw_log_filepath: str = os.path.join(tempfile.gettempdir(), "apexbos_raw_packets.log")
        self.raw_log_count: int = 0
        self._running: bool = True

        # Saved packets with _save_ in dictionary to temporary storage
        self.saved_packets_dir: str = self.config.get("saved_packets_dir", tempfile.gettempdir())
        self.saved_packet_extension: str = self.config.get("saved_packet_extension", "")
        self.saved_packets_count: int = 0
        self.last_saved_packet_file: Optional[str] = None
        self.saved_packets_files: List[str] = []

        self.quiet: bool = bool(self.config.get("quiet", False))
        self.log_fn = self.config.get("log_fn", None)

        # WIU Metadata Tracking (for ApexBOSBanc sync)
        self._wiu_sync_lock = threading.Lock()
        self._tracked_wius: Dict[str, dict] = {}
        self._dirty_wiu_ids: set = set()
        self._wiu_last_report_ts: Dict[str, float] = {}

        # Register exit handler to save packet cache to temporary directory associated with layout
        atexit.register(self._on_process_exit)

        if self.layout_filepath and os.path.exists(self.layout_filepath):
            self.load_layout(self.layout_filepath)
        elif self.load_packet_cache_enabled:
            self.load_packet_cache_from_temp(self.layout_filepath)

        if self.config.get("start_layout_server", True):
            self.start_layout_server()

    def _resolve_layout_path(self, layout_file: Optional[str]) -> str:
        if not layout_file:
            return ""
        if os.path.exists(layout_file):
            return os.path.abspath(layout_file)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        fname = os.path.basename(layout_file)
        candidates = [
            os.path.join(script_dir, layout_file),
            os.path.join(script_dir, "layouts", fname),
            os.path.join(script_dir, "config", fname),
            os.path.join(script_dir, fname),
        ]
        if hasattr(self, "config_dir") and self.config_dir:
            candidates.extend([
                os.path.join(self.config_dir, layout_file),
                os.path.join(self.config_dir, "layouts", fname),
                os.path.join(self.config_dir, "config", fname),
                os.path.join(self.config_dir, fname),
            ])
        cfg_path = self.config.get("config_path") if hasattr(self, "config") and isinstance(self.config, dict) else None
        if cfg_path:
            p_dir = os.path.dirname(os.path.abspath(cfg_path))
            candidates.extend([
                os.path.join(p_dir, layout_file),
                os.path.join(p_dir, "layouts", fname),
                os.path.join(p_dir, "config", fname),
                os.path.join(p_dir, fname),
            ])
        candidates.extend([
            os.path.join(os.getcwd(), layout_file),
            os.path.join(os.getcwd(), "layouts", fname),
            os.path.join(os.getcwd(), "config", fname),
            os.path.join(os.getcwd(), fname),
        ])

        for cand in candidates:
            if os.path.exists(cand):
                return os.path.abspath(cand)

        return os.path.abspath(os.path.join(script_dir, layout_file))

    def _engine_log(self, level: str, msg: str):
        if self.log_fn:
            try:
                self.log_fn(level, msg)
                return
            except Exception:
                pass
        if not self.quiet:
            print(f"[{level}] {msg}" if not msg.startswith("[") else msg)

    def reload_parsers(self):
        msg = ""
        cfgFile = self.config.get("config_path")
        msg = f"| {cfgFile}"
        cfg = []
        if cfgFile is not None and os.path.exists(cfgFile):
            with open(cfgFile, "r", encoding="utf-8") as f: cfg = json.load(f)
            if isinstance(cfg, dict):
                eng_sub = cfg.get("engine", {}) if isinstance(cfg.get("engine"), dict) else {}
                if "parsers" in eng_sub:
                    self.config["parsers"] = eng_sub["parsers"]
                    msg = f"| cfg file: {cfgFile} (engine.parsers) |"
                elif "parsers" in cfg:
                    self.config["parsers"] = cfg["parsers"]
                    msg = f"| cfg file: {cfgFile} |"
        self.parsers.clear()
        search_dirs = [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]
        for rule_entry in self.config.get("parsers", []):
            if isinstance(rule_entry, (dict, list)):
                self.parsers.append(PacketParser(rule_entry))
            elif isinstance(rule_entry, str):
                candidates = [rule_entry] if os.path.isabs(rule_entry) else [os.path.join(d, rule_entry) for d in search_dirs]
                for rule_path in candidates:
                    if os.path.exists(rule_path):
                        try:
                            with open(rule_path, "r", encoding="utf-8") as f:
                                rules_obj = json.load(f)
                            self.parsers.append(PacketParser(rules_obj))
                            break
                        except Exception:
                            pass
        self.signal_tables_cache = {}
        self._load_auxiliary_files()
        return msg

    def reload_all_lookups(self) -> str:
        """Reload parsers, rrdata, thanks.txt, signal tables, and re-evaluate the layout state."""
        msg = self.reload_parsers()
        n_sig = 0
        for d in [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]:
            try:
                n_sig += len([fn for fn in os.listdir(d) if fn.startswith("signals_") and fn.endswith(".json")])
            except OSError:
                pass
        # Re-apply last known packets so signal aspects / turnouts use the fresh lookups
        for pkt in list(self.last_packets_by_wiu.values()):
            for el in self.elements.values():
                if getattr(el, "wiu_id", None) == pkt.get("_node_id_"):
                    try:
                        el.process_packet(pkt, self)
                    except Exception:
                        pass
        self._evaluate_system_logic(time.time())
        self.broadcast_state(self.get_render_state())
        return f"{len(self.parsers)} parser(s), {len(self.rrdata)} railroad(s), {n_sig} signal table file(s), {msg}"

    def _load_auxiliary_files(self):
        search_dirs = [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]
        self.rrdata = {}
        for d in search_dirs:
            rr_path = os.path.join(d, "rrdata.json")
            if os.path.exists(rr_path):
                try:
                    with open(rr_path, "r", encoding="utf-8") as f:
                        raw_rr = json.load(f)
                        if isinstance(raw_rr, dict):
                            self.rrdata = {str(k): str(v) for k, v in raw_rr.items()}
                    break
                except Exception:
                    pass
        self.thanks_text = ""
        for d in search_dirs:
            th_path = os.path.join(d, "thanks.txt")
            if os.path.exists(th_path):
                try:
                    with open(th_path, "r", encoding="utf-8") as f:
                        self.thanks_text = f.read().strip()
                    break
                except Exception:
                    pass

    def _discover_egress_interfaces(self) -> List[str]:
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        for probe_dest in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80), ("10.0.0.1", 80)]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect(probe_dest)
                    pip = probe.getsockname()[0]
                    if pip and not pip.startswith("127.") and pip not in ips:
                        ips.append(pip)
            except Exception:
                pass
        return ips

    def get_egress_interfaces(self) -> List[str]:
        now = time.time()
        if (now - self._last_egress_if_refresh) > 30.0 or not self._egress_interfaces:
            self._egress_interfaces = self._discover_egress_interfaces()
            self._last_egress_if_refresh = now
        return self._egress_interfaces

    def register_client_subscriber(self, client_ip: str):
        if not client_ip or client_ip in ("127.0.0.1", "localhost", "0.0.0.0"):
            return
        with self._clients_lock:
            is_new = client_ip not in self._client_subscribers
            self._client_subscribers[client_ip] = time.time()
        if is_new:
            self._engine_log("INFO", f"Registered remote client subscriber for state updates: {client_ip}:{self.mcast_port}")

    def unregister_client_subscriber(self, client_ip: str):
        if not client_ip:
            return
        with self._clients_lock:
            if client_ip in self._client_subscribers:
                del self._client_subscribers[client_ip]
                self._engine_log("INFO", f"Unregistered remote client subscriber: {client_ip}")

    def start_layout_server(self):
        if self._server_running: return
        self._server_running = True
        self._server_thread = threading.Thread(target=self._layout_server_loop, daemon=True)
        self._server_thread.start()

    def _layout_server_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket = srv
        try:
            srv.bind((self.layout_server_host, self.layout_server_port))
            srv.listen(10)
            srv.settimeout(1.0)
            while self._server_running:
                try:
                    conn, client_addr = srv.accept()
                except socket.timeout: continue
                except OSError: break
                threading.Thread(
                    target=self._handle_layout_client,
                    args=(conn, client_addr),
                    daemon=True,
                ).start()
        except OSError as exc:
            self._engine_log("WARN", f"Layout TCP server on port {self.layout_server_port} could not bind: {exc}")
        finally:
            try: srv.close()
            except OSError: pass

    def _handle_layout_client(self, conn: socket.socket, client_addr: Tuple[str, int]):
        client_ip = client_addr[0] if client_addr else None
        if client_ip:
            self.register_client_subscriber(client_ip)
        try:
            conn.settimeout(3.0)
            req_raw = conn.recv(4096).decode("utf-8", errors="ignore").strip()
            if not req_raw:
                try: conn.close()
                except OSError: pass
                return

            if req_raw.startswith("STREAM_STATE") or req_raw.startswith("SUBSCRIBE_STREAM"):
                if client_ip:
                    self.register_client_subscriber(client_ip)
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except (AttributeError, OSError):
                    pass
                conn.settimeout(5.0)
                # Send immediate first frame snapshot
                try:
                    state = self.get_render_state()
                    raw_json = json.dumps(state).encode("utf-8")
                    if lz4_frame is not None:
                        compressed = lz4_frame.compress(raw_json)
                        magic = CHUNK_MAGIC_LZ4
                    else:
                        compressed = zlib.compress(raw_json, level=1)
                        magic = CHUNK_MAGIC_ZLIB
                    hdr = struct.pack("!I4s", len(compressed), magic)
                    conn.sendall(hdr + compressed)
                except Exception:
                    try: conn.close()
                    except OSError: pass
                    return

                with self._tcp_stream_lock:
                    self._tcp_stream_clients.append(conn)

                self._engine_log("INFO", f"Active real-time TCP state stream connected for {client_ip}:{client_addr[1]}")
                # Monitor client disconnect via select without calling recv() while no data is ready
                try:
                    while self._server_running:
                        r, _, _ = select.select([conn], [], [], 2.0)
                        if r:
                            data = conn.recv(1024)
                            if not data:
                                break
                except Exception:
                    pass
                finally:
                    with self._tcp_stream_lock:
                        if conn in self._tcp_stream_clients:
                            self._tcp_stream_clients.remove(conn)
                    try:
                        conn.close()
                    except OSError:
                        pass
                return

            with conn:
                try:
                    if req_raw.startswith("SUBSCRIBE") or req_raw.startswith("HEARTBEAT") or req_raw.startswith("PING"):
                        if client_ip:
                            self.register_client_subscriber(client_ip)
                        payload = {
                            "ok": True,
                            "registered": client_ip,
                            "mcast_group": self.mcast_group,
                            "mcast_port": self.mcast_port,
                            "current_state": self.get_render_state(),
                        }
                    elif req_raw.startswith("UNSUBSCRIBE"):
                        if client_ip:
                            self.unregister_client_subscriber(client_ip)
                        payload = {"ok": True, "unsubscribed": client_ip}
                    elif req_raw.startswith("SET_TEMP_OVERRIDE "):
                        try:
                            ovr_data = json.loads(req_raw[len("SET_TEMP_OVERRIDE "):].strip())
                            self.apply_temp_override(ovr_data)
                            payload = {"ok": True, "current_state": self.get_render_state()}
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("REVERT_TEMP_OVERRIDE "):
                        try:
                            rev_data = json.loads(req_raw[len("REVERT_TEMP_OVERRIDE "):].strip())
                            self.revert_temp_override(rev_data.get("id"))
                            payload = {"ok": True, "current_state": self.get_render_state()}
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("SAVE_PACKET_HISTORY") or req_raw.startswith("SAVE_HISTORY"):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            target_f = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                            saved_path = self.save_packet_history(target_f)
                            payload = {"ok": True, "saved_path": saved_path, "packet_count": len(self.packet_cache)}
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("TOGGLE_RAW_LOGGING") or req_raw.startswith("TOGGLE_RAW_PACKET_LOGGING"):
                        is_on = self.toggle_raw_packet_logging()
                        payload = {
                            "ok": True,
                            "raw_packet_logging": is_on,
                            "raw_log_filepath": self.raw_log_filepath,
                            "raw_log_count": self.raw_log_count,
                            "current_state": self.get_render_state(),
                        }
                    elif req_raw.startswith("SET_RAW_LOGGING"):
                        parts = req_raw.split(maxsplit=1)
                        flag = True if len(parts) > 1 and parts[1].strip().upper() in ("1", "TRUE", "ON") else False
                        is_on = self.toggle_raw_packet_logging(enabled=flag)
                        payload = {
                            "ok": True,
                            "raw_packet_logging": is_on,
                            "raw_log_filepath": self.raw_log_filepath,
                            "raw_log_count": self.raw_log_count,
                            "current_state": self.get_render_state(),
                        }
                    elif req_raw.startswith("ASSIGN_TRAIN_INFO") or req_raw.startswith("SET_TRAIN_INFO"):
                        try:
                            body = req_raw.split(maxsplit=1)[1].strip()
                            data = json.loads(body)
                            res = self.assign_train_info(data)
                            payload = {"ok": True, "result": res, "current_state": self.get_render_state()}
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("DELETE_TRAIN") or req_raw.startswith("REMOVE_TRAIN"):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            t_arg = parts[1].strip() if len(parts) > 1 else ""
                            if t_arg.startswith("{"):
                                p_data = json.loads(t_arg)
                                t_key = p_data.get("train_key") or p_data.get("train_name")
                            else:
                                t_key = t_arg
                            res = self.delete_train(t_key)
                            payload = {"ok": res.get("ok", False), "result": res, "current_state": self.get_render_state()}
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("GET_SAVED_PACKETS") or req_raw.startswith("GET_SAVED_PACKET_FILES"):
                        payload = {
                            "ok": True,
                            "saved_packets_count": getattr(self, "saved_packets_count", 0),
                            "last_saved_packet_file": getattr(self, "last_saved_packet_file", None),
                            "saved_packets_files": list(getattr(self, "saved_packets_files", [])),
                        }
                    elif req_raw.startswith("GET_PACKET_HISTORY") or req_raw.startswith("GET_PACKET_CACHE"):
                        payload = {
                            "ok": True,
                            "packet_count": len(self.packet_cache),
                            "packet_cache_size": self.packet_cache_size,
                            "packets": list(self.packet_cache),
                        }
                    elif req_raw.startswith("GET_LOCAL_LAYOUTS") or req_raw.startswith("LIST_LOCAL_LAYOUTS"):
                        try:
                            payload = self.get_local_layouts()
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc), "layouts": []}
                    elif req_raw.startswith("LOAD_LOCAL_LAYOUT "):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            lay_name = parts[1].strip() if len(parts) > 1 else ""
                            if lay_name.startswith("{"):
                                try:
                                    j_data = json.loads(lay_name)
                                    lay_name = j_data.get("layout") or j_data.get("name") or j_data.get("layout_name") or lay_name
                                except Exception:
                                    pass
                            payload = self.load_local_layout(lay_name)
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("LOAD_BANC_LAYOUT ") or req_raw.startswith("LOAD_LAYOUT "):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            lay_name = parts[1].strip() if len(parts) > 1 else ""
                            payload = self.load_banc_layout(lay_name)
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("GET_BANC_LAYOUTS") or req_raw.startswith("LIST_BANC_LAYOUTS"):
                        try:
                            payload = self.get_banc_layouts()
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc), "layouts": []}
                    elif req_raw.startswith("SEND_CHAT ") or req_raw.startswith("CHAT_SEND "):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            body = parts[1].strip() if len(parts) > 1 else "{}"
                            if body.startswith("{"):
                                chat_req = json.loads(body)
                            else:
                                chat_req = {"text": body}
                            payload = self.send_chat_message(
                                text=chat_req.get("text", ""),
                                sender=chat_req.get("sender") or chat_req.get("identity"),
                                contacts=chat_req.get("contacts"),
                            )
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("CLEAR_CHAT") or req_raw.startswith("CHAT_CLEAR"):
                        try:
                            payload = self.clear_chat_messages()
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    elif req_raw.startswith("SET_CHAT_IDENTITY_MAP ") or req_raw.startswith("UPDATE_CHAT_CONTACTS "):
                        try:
                            parts = req_raw.split(maxsplit=1)
                            body = parts[1].strip() if len(parts) > 1 else "{}"
                            map_data = json.loads(body) if body.startswith("{") else {}
                            payload = self.set_chat_identity_map(
                                self_identity=map_data.get("self_identity"),
                                self_name=map_data.get("self_name"),
                                contacts=map_data.get("contacts"),
                                persist=bool(map_data.get("persist", True)),
                            )
                        except Exception as exc:
                            payload = {"ok": False, "error": str(exc)}
                    else:
                        payload = {"version": self.version, "layout_filename": self.layout_filename, "thanks_text": self.thanks_text, "rrdata": self.rrdata, "layout": self.get_layout_dict(), "current_state": self.get_render_state()}
                    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                    try:
                        conn.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                except OSError:
                    pass
        except OSError:
            pass

    def _sync_system_panels_from_elements(self):
        for el in self.elements.values():
            if el.elem_type == "packet_scroller":
                self.scroller_enabled = True
                max_r = int(getattr(el, "extra_info", 10)) if str(getattr(el, "extra_info", "")).isdigit() else 10
                self.scroller.x, self.scroller.y = el.x, el.y
                self.scroller.max_len = max_r
                self.scroller.width = getattr(el, "width", 0) or 580
                self.scroller.height = getattr(el, "height", 0) or 195
            elif el.elem_type == "address_table":
                self.address_table_enabled = True
                max_r = int(getattr(el, "extra_info", 10)) if str(getattr(el, "extra_info", "")).isdigit() else 10
                self.address_table.x, self.address_table.y = el.x, el.y
                self.address_table.max_len = max_r
                self.address_table.width = getattr(el, "width", 0) or 450
                self.address_table.height = getattr(el, "height", 0) or 150
            elif el.elem_type == "train_scroller":
                self.train_scroller_enabled = True
                max_r = int(getattr(el, "extra_info", 10)) if str(getattr(el, "extra_info", "")).isdigit() else 10
                self.train_scroller.x, self.train_scroller.y = el.x, el.y
                self.train_scroller.max_len = max_r
                self.train_scroller.width = getattr(el, "width", 0) or 430
                self.train_scroller.height = getattr(el, "height", 0) or 150
            elif el.elem_type == "chat":
                self.chat_enabled = True
                max_r = int(getattr(el, "extra_info", 12)) if str(getattr(el, "extra_info", "")).isdigit() else 12
                self.chat_panel.x, self.chat_panel.y = el.x, el.y
                self.chat_panel.max_len = max_r
                self.chat_panel.width = getattr(el, "width", 0) or 460
                self.chat_panel.height = getattr(el, "height", 0) or 220

    def get_layout_dict(self) -> dict:
        self._sync_system_panels_from_elements()
        data = {
            "version": self.version,
            "layout_filename": self.layout_filename,
            "thanks_text": self.thanks_text,
            "rrdata": self.rrdata,
            "inferred_timeout_sec": self.inferred_timeout_sec,
            "train_logic_interval_sec": self.train_logic_interval_sec,
            "packet_cache_size": self.packet_cache_size,
            "elements": [el.to_config_dict() for el in self.elements.values()],
        }
        if self.scroller_enabled:
            data["scroller"] = {"x": self.scroller.x, "y": self.scroller.y, "max_len": self.scroller.max_len, "width": self.scroller.width, "height": self.scroller.height}
        if self.address_table_enabled:
            data["address_table"] = {"x": self.address_table.x, "y": self.address_table.y, "max_len": self.address_table.max_len, "width": self.address_table.width, "height": self.address_table.height}
        if self.train_scroller_enabled:
            data["train_scroller"] = {"x": self.train_scroller.x, "y": self.train_scroller.y, "max_len": self.train_scroller.max_len, "width": self.train_scroller.width, "height": self.train_scroller.height}
        if getattr(self, "chat_enabled", False):
            data["chat"] = {"x": self.chat_panel.x, "y": self.chat_panel.y, "max_len": self.chat_panel.max_len, "width": self.chat_panel.width, "height": self.chat_panel.height}
        source_info = getattr(self, "source_info", None)
        if source_info is None and isinstance(getattr(self, "raw_layout_data", None), dict):
            source_info = self.raw_layout_data.get("source")
        if source_info is not None:
            data["source"] = copy.deepcopy(source_info)
        return data

    def save_layout(self, filepath: str):
        self.layout_filepath = filepath
        self.layout_filename = os.path.basename(filepath)
        self.config_dir = os.path.dirname(os.path.abspath(filepath)) or os.getcwd()
        self._load_auxiliary_files()
        data = self.get_layout_dict()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_layout_data(self, data: dict, layout_filename: str = "banc_layout.json", config_dir: Optional[str] = None, filepath: Optional[str] = None):
        """Loads a layout directly from an in-memory dictionary (e.g. from Banc Cloud) or pre-parsed layout."""
        # Save cache for the current layout before switching to a new layout
        if self.save_packet_cache_enabled and hasattr(self, "packet_cache") and self.packet_cache and (filepath is None or self.layout_filepath != filepath):
            self.save_packet_cache_to_temp()

        # If raw ITCVL dictionary is passed directly, auto-convert it
        if isinstance(data, dict) and (data.get("fileFormat") == "itcvl" or ("layout" in data and "elements" not in data)):
            try:
                import itcvl_converter
                data = itcvl_converter.convert_itcvl_data(data, layout_filename=layout_filename, save_fragments=False)
            except Exception:
                pass

        self.layout_filepath = filepath or os.path.join(os.getcwd(), layout_filename)
        self.layout_filename = os.path.basename(self.layout_filepath) if self.layout_filepath else layout_filename
        self.config_dir = config_dir or (os.path.dirname(os.path.abspath(self.layout_filepath)) if self.layout_filepath else os.getcwd())
        self._load_auxiliary_files()
        self.raw_layout_data = data
        self.source_info = copy.deepcopy(data.get("source")) if isinstance(data, dict) and "source" in data else None

        # Check for packet_cache_size in layout configuration
        cfg_cache_size = data.get("packet_cache_size") or data.get("packet_history_limit") or data.get("packet_cache_limit")
        if cfg_cache_size is not None:
            try:
                self.set_packet_cache_size(int(cfg_cache_size))
            except (ValueError, TypeError):
                pass

        self.inferred_timeout_sec = data.get("inferred_timeout_sec", self.inferred_timeout_sec)
        self.train_logic_interval_sec = float(data.get("train_logic_interval_sec", self.train_logic_interval_sec))
        self.scroller_enabled = "scroller" in data
        self.address_table_enabled = "address_table" in data
        self.train_scroller_enabled = "train_scroller" in data
        self.chat_enabled = "chat" in data

        sc = data.get("scroller") or {}
        self.scroller = PacketScroller(sc.get("x", 0), sc.get("y", 22), sc.get("max_len", 10), sc.get("width", 580), sc.get("height", 195))
        at = data.get("address_table") or {}
        self.address_table = AddressTable(
            at.get("x", 26),
            at.get("y", 22),
            at.get("max_len", 10),
            at.get("width", 450),
            at.get("height", 150),
            dedup_ttl=getattr(self, "dedup_ttl", 0.4),
        )
        ts = data.get("train_scroller") or {}
        self.train_scroller = TrainScroller(ts.get("x", 50), ts.get("y", 22), ts.get("max_len", 10), ts.get("width", 430), ts.get("height", 150))
        ch = data.get("chat") or {}
        existing_msgs = list(self.chat_panel.messages) if hasattr(self, "chat_panel") else []
        existing_seen = set(self.chat_panel._seen_keys) if hasattr(self, "chat_panel") else set()
        existing_tot = getattr(self.chat_panel, "total_messages", 0) if hasattr(self, "chat_panel") else 0
        existing_ts = getattr(self.chat_panel, "last_message_ts", 0.0) if hasattr(self, "chat_panel") else 0.0
        self.chat_panel = ChatPanel(ch.get("x", 76), ch.get("y", 22), ch.get("max_len", 12), ch.get("width", 460), ch.get("height", 220))
        self.chat_panel.messages.extend(existing_msgs)
        self.chat_panel._seen_keys.update(existing_seen)
        self.chat_panel.total_messages = existing_tot
        self.chat_panel.last_message_ts = existing_ts

        self.elements.clear()
        type_map = {
            "track": TrackSegment, "turnout": Turnout, "signal": Signal, "crossing": Crossing,
            "base": BaseRadio, "track_jump": TrackJump, "ptc_datetime": PTCDateTime,
            "date_time": PTCDateTime, "datetime": PTCDateTime,
            "station": DisplayElement, "label": DisplayElement,
            "bridge": DisplayElement, "diamond": DisplayElement,
            "packet_scroller": DisplayElement, "address_table": DisplayElement, "train_scroller": DisplayElement,
            "chat": DisplayElement,
        }
        for item in data.get("elements", []):
            item_copy = dict(item)
            item_copy.setdefault("label_pos", "top")
            if "radioid" in item_copy and "radio_id" not in item_copy:
                item_copy["radio_id"] = item_copy["radioid"]
            etype = item_copy["elem_type"]
            cls = type_map.get(etype, DisplayElement)
            if item_copy.get("gps"):
                item_copy["gps"] = tuple(item_copy["gps"])
            valid_keys = {f.name for f in fields(cls) if f.init}
            filtered_kwargs = {k: v for k, v in item_copy.items() if k in valid_keys}
            el_obj = cls(**filtered_kwargs)
            self.elements[item_copy["id"]] = el_obj

            if etype == "packet_scroller":
                self.scroller_enabled = True
                max_r = int(el_obj.extra_info) if str(el_obj.extra_info).isdigit() else 10
                self.scroller = PacketScroller(el_obj.x, el_obj.y, max_r, getattr(el_obj, "width", 0) or 580, getattr(el_obj, "height", 0) or 195)
            elif etype == "address_table":
                self.address_table_enabled = True
                max_r = int(el_obj.extra_info) if str(el_obj.extra_info).isdigit() else 10
                self.address_table = AddressTable(
                    el_obj.x,
                    el_obj.y,
                    max_r,
                    getattr(el_obj, "width", 0) or 450,
                    getattr(el_obj, "height", 0) or 150,
                    dedup_ttl=getattr(self, "dedup_ttl", 0.4),
                )
            elif etype == "train_scroller":
                self.train_scroller_enabled = True
                max_r = int(el_obj.extra_info) if str(el_obj.extra_info).isdigit() else 10
                self.train_scroller = TrainScroller(el_obj.x, el_obj.y, max_r, getattr(el_obj, "width", 0) or 430, getattr(el_obj, "height", 0) or 150)
            elif etype == "chat":
                self.chat_enabled = True
                max_r = int(el_obj.extra_info) if str(el_obj.extra_info).isdigit() else 12
                self.chat_panel.x = el_obj.x
                self.chat_panel.y = el_obj.y
                self.chat_panel.max_len = max_r
                self.chat_panel.width = getattr(el_obj, "width", 0) or 460
                self.chat_panel.height = getattr(el_obj, "height", 0) or 220

        self._sorted_element_ids = sorted(self.elements.keys())
        self._refresh_wius_from_layout()

        # Load cached packet history for this layout, if available, upon startup / layout load
        if self.layout_filepath and self.load_packet_cache_enabled:
            self.load_packet_cache_from_temp(self.layout_filepath)

    def load_layout(self, filepath: str):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.load_layout_data(
            data,
            layout_filename=os.path.basename(filepath),
            config_dir=os.path.dirname(os.path.abspath(filepath)) or os.getcwd(),
            filepath=filepath,
        )

    def load_banc_layout(self, layout_name: str, banc_client=None) -> dict:
        """Downloads a layout from Banc Cloud, loads it into the engine, and broadcasts state."""
        import mosfet_client
        client = banc_client or mosfet_client.create_banc_client_from_config(self.config.get("config_path"))
        if not client:
            return {"ok": False, "error": "Banc client not configured"}
        try:
            data = client.download_layout_sync(layout_name)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not data:
            return {"ok": False, "error": f"Failed to download layout '{layout_name}'"}

        clean_name = os.path.basename(layout_name)
        if not clean_name.lower().endswith(".json"):
            clean_name += ".json"
        target_fname = f"banc_{clean_name}"
        self.load_layout_data(data, layout_filename=target_fname)
        state = self.get_render_state()
        self.broadcast_state(state)
        return {
            "ok": True,
            "layout_filename": self.layout_filename,
            "elements_count": len(self.elements),
            "current_state": state,
        }

    def get_layouts_dir(self) -> str:
        """Finds or creates the primary layouts directory."""
        candidates = []
        if getattr(self, "config_dir", None):
            candidates.append(os.path.join(self.config_dir, "layouts"))
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(script_dir, "layouts"))
        candidates.append(os.path.join(os.getcwd(), "layouts"))
        for cand in candidates:
            if os.path.isdir(cand):
                return cand
        target = os.path.join(script_dir, "layouts")
        os.makedirs(target, exist_ok=True)
        return target

    def get_local_layouts(self) -> dict:
        """Scans the layouts subfolder for layout files and returns their names."""
        layout_files = set()
        candidates = []
        if getattr(self, "config_dir", None):
            candidates.append(os.path.join(self.config_dir, "layouts"))
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(script_dir, "layouts"))
        candidates.append(os.path.join(os.getcwd(), "layouts"))

        seen_dirs = set()
        for d in candidates:
            norm_d = os.path.abspath(d)
            if norm_d in seen_dirs or not os.path.isdir(norm_d):
                continue
            seen_dirs.add(norm_d)
            try:
                for entry in os.listdir(norm_d):
                    if entry.startswith(".") or entry.startswith("__"):
                        continue
                    if entry.lower().endswith((".json", ".itcvl")):
                        layout_files.add(entry)
            except Exception:
                pass

        sorted_layouts = sorted(list(layout_files), key=lambda s: s.lower())
        return {"ok": True, "layouts": sorted_layouts}

    def load_local_layout(self, layout_name: str) -> dict:
        """Loads a layout file from the layouts subfolder into the engine and broadcasts state."""
        clean_name = os.path.basename(layout_name.strip())
        if not clean_name:
            return {"ok": False, "error": "No layout name specified"}
        name_with_ext = clean_name if clean_name.lower().endswith((".json", ".itcvl")) else f"{clean_name}.json"

        candidates = []
        if getattr(self, "config_dir", None):
            candidates.append(os.path.join(self.config_dir, "layouts", name_with_ext))
            candidates.append(os.path.join(self.config_dir, "layouts", clean_name))
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(script_dir, "layouts", name_with_ext))
        candidates.append(os.path.join(script_dir, "layouts", clean_name))
        candidates.append(os.path.join(os.getcwd(), "layouts", name_with_ext))
        candidates.append(os.path.join(os.getcwd(), "layouts", clean_name))

        candidates.append(self._resolve_layout_path(os.path.join("layouts", name_with_ext)))
        candidates.append(self._resolve_layout_path(name_with_ext))
        candidates.append(self._resolve_layout_path(clean_name))

        target_file = None
        for cand in candidates:
            if cand and os.path.isfile(cand):
                target_file = os.path.abspath(cand)
                break

        if not target_file:
            return {"ok": False, "error": f"Local layout '{layout_name}' not found in layouts directory"}

        try:
            self.load_layout(target_file)
            state = self.get_render_state()
            self.broadcast_state(state)
            return {
                "ok": True,
                "layout_filename": self.layout_filename,
                "layout_filepath": target_file,
                "elements_count": len(self.elements),
                "current_state": state,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to load layout '{target_file}': {exc}"}

    def get_banc_layouts(self, banc_client=None) -> dict:
        """Retrieves the list of remote Banc layouts."""
        import mosfet_client
        client = banc_client or mosfet_client.create_banc_client_from_config(self.config.get("config_path"))
        if not client:
            return {"ok": False, "error": "Banc client not configured", "layouts": []}
        try:
            layouts = client.list_layouts_sync()
            return {"ok": True, "layouts": layouts}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "layouts": []}

    # -----------------------------------------------------------------
    # Secure Short Message Chat Service (Viewer <-> Engine <-> Banc)
    # -----------------------------------------------------------------
    def _get_chat_identity_store_path(self) -> str:
        custom_path = self.config.get("chat_identities_path")
        if custom_path:
            return str(custom_path)
        target_dir = self.config_dir or os.getcwd()
        if os.path.basename(os.path.abspath(target_dir)).lower() == "layouts":
            target_dir = os.path.dirname(os.path.abspath(target_dir)) or os.getcwd()
        return os.path.join(target_dir, ".apexbos_chat_identities.json")

    def _load_local_chat_identities(self):
        path = self._get_chat_identity_store_path()
        if not os.path.exists(path):
            return
        try:
            import mosfet_client
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            s_id = mosfet_client.sanitize_chat_identity(data.get("self_identity") or "")
            s_name = str(data.get("self_name") or "").strip()[:48]
            if s_id and not self.chat_self_identity:
                self.chat_self_identity = s_id
            if s_name:
                self.chat_self_name = s_name
            raw_contacts = data.get("contacts")
            if isinstance(raw_contacts, dict):
                for k, v in raw_contacts.items():
                    ck = mosfet_client.sanitize_chat_identity(k)
                    cv = str(v or "").strip()[:48]
                    if ck and cv:
                        self.chat_contacts[ck] = cv
        except Exception:
            pass

    def _save_local_chat_identities(self):
        path = self._get_chat_identity_store_path()
        try:
            payload = {
                "self_identity": self.chat_self_identity,
                "self_name": self.chat_self_name,
                "contacts": dict(self.chat_contacts),
                "updated_at": time.time(),
            }
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            pass

    def get_active_chat_identity(self) -> str:
        import mosfet_client
        if getattr(self, "chat_self_identity", ""):
            clean = mosfet_client.sanitize_chat_identity(self.chat_self_identity)
            if clean:
                return clean
        conn_cfg = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        cfg_id = conn_cfg.get("identity") or self.config.get("identity")
        if cfg_id and str(cfg_id).strip().lower() not in ("", "auto", "none", "initializing...", "generating id..."):
            clean = mosfet_client.sanitize_chat_identity(cfg_id)
            if clean:
                return clean
        return "Dispatcher"

    def set_chat_identity_map(
        self,
        self_identity: Optional[str] = None,
        self_name: Optional[str] = None,
        contacts: Optional[Dict[str, str]] = None,
        persist: bool = True,
    ) -> dict:
        """Associates local user's identity with their name and incoming identities with names.
        Stored strictly locally; never broadcast or leaked to Banc or layout JSON.
        """
        import mosfet_client
        with self._chat_lock:
            if self_identity is not None:
                clean_id = mosfet_client.sanitize_chat_identity(self_identity)
                if clean_id:
                    self.chat_self_identity = clean_id
            if self_name is not None:
                clean_name = re.sub(r"[\x00-\x1f\x7f<>]", "", str(self_name)).strip()[:48]
                self.chat_self_name = clean_name
            if isinstance(contacts, dict):
                new_contacts: Dict[str, str] = {}
                for k, v in contacts.items():
                    ck = mosfet_client.sanitize_chat_identity(k)
                    cv = re.sub(r"[\x00-\x1f\x7f<>]", "", str(v or "")).strip()[:48]
                    if ck and cv:
                        new_contacts[ck] = cv
                self.chat_contacts.update(new_contacts)
                # Allow deleting empty mappings if explicitly passed as ""
                for k, v in contacts.items():
                    ck = mosfet_client.sanitize_chat_identity(k)
                    if ck and v == "" and ck in self.chat_contacts:
                        del self.chat_contacts[ck]
            if persist:
                self._save_local_chat_identities()
        return {
            "ok": True,
            "active_identity": self.get_active_chat_identity(),
            "contacts_count": len(self.chat_contacts),
        }

    def get_chat_fernet(self):
        """Returns the Fernet cipher using the exact same encryption_key used to decrypt packets from Banc."""
        from cryptography.fernet import Fernet
        conn_cfg = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        enc_key = (
            conn_cfg.get("encryption_key")
            or self.config.get("encryption_key")
            or "X0V6d2t5SG5zR1BBZ0xNblp6U3p3VmpvT2R6WnB6VGs="
        )
        return Fernet(enc_key.encode("utf-8") if isinstance(enc_key, str) else enc_key)

    def encrypt_chat_payload(self, payload: Any) -> bytes:
        """Encrypts a chat message dictionary or string using the same Fernet method as Banc packets."""
        fernet = self.get_chat_fernet()
        if isinstance(payload, (dict, list)):
            raw_bytes = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, bytes):
            raw_bytes = payload
        else:
            raw_bytes = str(payload).encode("utf-8")
        return fernet.encrypt(raw_bytes)

    def decrypt_chat_payload(self, encrypted_token: bytes) -> Any:
        """Decrypts an encrypted chat payload using the same Fernet method as Banc packets."""
        fernet = self.get_chat_fernet()
        raw_bytes = fernet.decrypt(encrypted_token)
        text = raw_bytes.decode("utf-8", errors="ignore")
        try:
            return json.loads(text)
        except Exception:
            return text

    def get_node_identity(self) -> str:
        import mosfet_client
        conn_cfg = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        cfg_id = conn_cfg.get("identity") or self.config.get("identity")
        if cfg_id and str(cfg_id).strip().lower() not in ("", "auto", "none", "initializing...", "generating id..."):
            clean = mosfet_client.sanitize_chat_identity(cfg_id)
            if clean:
                return clean
        return "ApexNode"

    def _create_chat_banc_client(self, override_client=None):
        if override_client is not None:
            return override_client
        import mosfet_client
        conn_cfg = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        host = conn_cfg.get("banc_host") or self.config.get("banc_host") or self.config.get("cloud_server")
        port = conn_cfg.get("banc_port") or self.config.get("banc_port") or self.config.get("cloud_port")
        auth = conn_cfg.get("auth_secret") or self.config.get("auth_secret")
        enc = conn_cfg.get("encryption_key") or self.config.get("encryption_key")
        node_id = self.get_node_identity()
        if host and str(host).lower() != "none" and port and auth and enc:
            try:
                return mosfet_client.BancClient(
                    host=str(host),
                    port=int(port),
                    secret_key=str(auth),
                    encryption_key=str(enc),
                    identity=node_id,
                )
            except Exception:
                return None
        cfg_path = self.config.get("config_path")
        if cfg_path and os.path.exists(str(cfg_path)):
            try:
                client = mosfet_client.create_banc_client_from_config(str(cfg_path))
                if client:
                    client.identity = node_id
                return client
            except Exception:
                pass
        return None

    def convert_chat_names_to_identities(
        self,
        text: str,
        extra_contacts: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, List[str]]:
        """Converts any @<name> or @name in outgoing text to @<identity> (@identity),
        and scrubs any accidental name references so names are never sent in the open.
        Returns (converted_text, target_identities).
        """
        import mosfet_client
        if not text:
            return "", []

        name_to_id: Dict[str, str] = {}
        with self._chat_lock:
            merged_contacts = dict(self.chat_contacts)
            if isinstance(extra_contacts, dict):
                for k, v in extra_contacts.items():
                    ck = mosfet_client.sanitize_chat_identity(k)
                    cv = str(v or "").strip()
                    if ck and cv:
                        merged_contacts[ck] = cv
            for ident, name in merged_contacts.items():
                clean_id = mosfet_client.sanitize_chat_identity(ident)
                clean_name = str(name or "").strip()
                if clean_id and clean_name:
                    name_to_id[clean_name.lower()] = clean_id
                    # Also index underscore/compact variant for @Name_With_Spaces
                    compact = re.sub(r"\s+", "_", clean_name.lower())
                    name_to_id[compact] = clean_id
                    no_space = re.sub(r"\s+", "", clean_name.lower())
                    if no_space:
                        name_to_id[no_space] = clean_id

            active_self_id = self.get_active_chat_identity()
            if self.chat_self_name and active_self_id:
                sn = self.chat_self_name.strip().lower()
                if sn:
                    name_to_id[sn] = active_self_id
                    name_to_id[re.sub(r"\s+", "_", sn)] = active_self_id

        converted = str(text)
        targets: List[str] = []

        # 1. Replace explicit bracketed @<name_or_id>
        def _replace_bracketed(match):
            token = match.group(1).strip()
            tl = token.lower()
            if tl in name_to_id:
                resolved_id = name_to_id[tl]
            else:
                resolved_id = mosfet_client.sanitize_chat_identity(token)
            if resolved_id:
                if resolved_id not in targets:
                    targets.append(resolved_id)
                return f"@{resolved_id}"
            return ""

        converted = re.sub(r"@<([^<>\r\n]{1,64})>", _replace_bracketed, converted)

        # 2. Replace @<name> without brackets (longest known names first so multi-word names match)
        if name_to_id:
            sorted_names = sorted(name_to_id.keys(), key=len, reverse=True)
            for n_lower in sorted_names:
                resolved_id = name_to_id[n_lower]
                pattern = re.compile(r"(?<!\w)@" + re.escape(n_lower) + r"(?=$|[\s,.:;!?\)\]\}])", re.IGNORECASE)
                if pattern.search(converted):
                    if resolved_id not in targets:
                        targets.append(resolved_id)
                    converted = pattern.sub(f"@{resolved_id}", converted)

        # 3. Extract any remaining direct @identity mentions
        for m in mosfet_client.extract_chat_mentions(converted):
            if m not in targets:
                targets.append(m)

        return converted, targets

    def resolve_sender_identity(self, raw_sender: Optional[str], extra_contacts: Optional[Dict[str, str]] = None) -> str:
        """Ensures sender is always converted from any display name to identity. Never sends names in the open."""
        import mosfet_client
        active_id = self.get_active_chat_identity()
        if not raw_sender:
            return active_id
        cand = str(raw_sender).strip()
        if not cand:
            return active_id
        # If raw_sender matches self_name, always convert to self_identity
        if self.chat_self_name and cand.lower() == self.chat_self_name.strip().lower():
            return active_id
        # If raw_sender matches any known contact name, convert to that contact's identity
        with self._chat_lock:
            for ident, name in self.chat_contacts.items():
                if name and cand.lower() == name.strip().lower():
                    return mosfet_client.sanitize_chat_identity(ident) or active_id
        if isinstance(extra_contacts, dict):
            for ident, name in extra_contacts.items():
                if name and cand.lower() == str(name).strip().lower():
                    return mosfet_client.sanitize_chat_identity(ident) or active_id
        return mosfet_client.sanitize_chat_identity(cand) or active_id

    def send_chat_message(
        self,
        text: str,
        sender: Optional[str] = None,
        banc_client=None,
        contacts: Optional[Dict[str, str]] = None,
    ) -> dict:
        """Accepts a message from Viewer, converts names to identities, sanitizes against threats,
        encrypts & relays via Banc (if configured), records in ChatPanel, and broadcasts state via multicast.
        """
        import mosfet_client
        if isinstance(contacts, dict) and contacts:
            self.set_chat_identity_map(contacts=contacts, persist=True)

        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "Empty chat message"}

        # Convert @<name> / @name -> @identity before sanitization so names are never sent in the open
        converted_text, targets = self.convert_chat_names_to_identities(text, extra_contacts=contacts)
        clean_text = mosfet_client.sanitize_chat_text(converted_text)
        if not clean_text:
            return {"ok": False, "error": "Message rejected by security filter"}

        # Ensure any mentions inside sanitized text are included in targets
        for m in mosfet_client.extract_chat_mentions(clean_text):
            if m not in targets:
                targets.append(m)

        sender_identity = self.resolve_sender_identity(sender, extra_contacts=contacts)
        client = self._create_chat_banc_client(override_client=banc_client)

        banc_msg_record = None
        banc_relayed = False
        if client is not None:
            try:
                client.identity = sender_identity
                resp = client.send_chat_message_sync(clean_text, target_identities=targets)
                if isinstance(resp, dict) and resp.get("ok"):
                    banc_relayed = True
                    banc_msg_record = resp.get("message")
            except Exception as exc:
                self._engine_log("WARN", f"Banc chat send warning: {exc}")

        if isinstance(banc_msg_record, dict):
            rec = self.chat_panel.add_message(banc_msg_record) or banc_msg_record
            try:
                mid = int(banc_msg_record.get("msg_id") or 0)
                if mid > self._last_banc_chat_id:
                    self._last_banc_chat_id = mid
            except (ValueError, TypeError):
                pass
        else:
            now_ts = time.time()
            local_msg = {
                "timestamp": now_ts,
                "sender_identity": sender_identity,
                "text": clean_text,
                "target_identities": targets,
            }
            rec = self.chat_panel.add_message(local_msg) or local_msg

        self._last_transmitted_signature = self._compute_logical_signature()
        self.transmitted_frames_count += 1
        state = self.get_render_state()
        self.broadcast_state(state)
        return {
            "ok": True,
            "status": "ok",
            "banc_relayed": banc_relayed,
            "message": rec,
            "current_state": state,
        }

    def ingest_chat_messages(self, messages: List[Any], broadcast: bool = True) -> int:
        """Ingests decrypted (or encrypted token) chat messages from Banc into the Engine and broadcasts via multicast."""
        if not messages:
            return 0
        added = 0
        for item in messages:
            msg_dict = None
            if isinstance(item, (bytes, bytearray)):
                try:
                    dec = self.decrypt_chat_payload(bytes(item))
                    if isinstance(dec, dict):
                        msg_dict = dec
                        if "messages" in dec and isinstance(dec["messages"], list):
                            added += self.ingest_chat_messages(dec["messages"], broadcast=False)
                            continue
                except Exception:
                    continue
            elif isinstance(item, dict):
                msg_dict = item
            if isinstance(msg_dict, dict):
                rec = self.chat_panel.add_message(msg_dict)
                if rec is not None:
                    added += 1
                try:
                    mid = int(msg_dict.get("msg_id") or 0)
                    if mid > self._last_banc_chat_id:
                        self._last_banc_chat_id = mid
                    mts = float(msg_dict.get("timestamp") or 0.0)
                    if mts > self._last_banc_chat_ts:
                        self._last_banc_chat_ts = mts
                except (ValueError, TypeError):
                    pass
        if added > 0 and broadcast:
            self._last_transmitted_signature = self._compute_logical_signature()
            self.transmitted_frames_count += 1
            state = self.get_render_state()
            self.broadcast_state(state)
        return added

    def poll_banc_chat(self, banc_client=None) -> int:
        """Polls Banc for new encrypted chat messages addressed to this Engine's identity or broadcast,
        decrypts them, and broadcasts state via multicast.
        """
        client = self._create_chat_banc_client(override_client=banc_client)
        if client is None:
            return 0
        try:
            client.identity = self.get_active_chat_identity()
            msgs = client.get_chat_messages_sync(
                since_id=self._last_banc_chat_id,
                since_ts=self._last_banc_chat_ts,
            )
            if msgs:
                return self.ingest_chat_messages(msgs, broadcast=True)
        except Exception:
            pass
        return 0

    def clear_chat_messages(self) -> dict:
        self.chat_panel.clear()
        self._last_transmitted_signature = self._compute_logical_signature()
        self.transmitted_frames_count += 1
        state = self.get_render_state()
        self.broadcast_state(state)
        return {"ok": True, "current_state": state}

    def lookup_signal_aspect(self, wiu_id: Optional[int], signal_val: int, signal_rules: Optional[str] = None) -> str:
        if "default" not in self.signal_tables_cache:
            def_tbl = dict(DEFAULT_SIGNAL_TABLE)
            for d in [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]:
                def_file = os.path.join(d, "signals_default.json")
                if os.path.exists(def_file):
                    try:
                        with open(def_file, "r", encoding="utf-8") as f:
                            raw_def = json.load(f)
                            def_tbl.update({int(k): str(v) for k, v in raw_def.items() if str(k).isdigit()})
                        break
                    except Exception:
                        pass
            self.signal_tables_cache["default"] = def_tbl

        default_tbl = self.signal_tables_cache["default"]

        rr_override, val_overrides = parse_signal_rules(signal_rules) if signal_rules else (None, {})

        # Determine base signal table according to precedence:
        # Precedence: RR override (or 'DEF') -> signals_NNN.json (wiu_id) -> default table
        base_tbl = default_tbl
        if rr_override:
            rr_clean = rr_override.strip().upper()
            if rr_clean in ("DEF", "DEFAULT"):
                base_tbl = default_tbl
            else:
                if rr_clean in self.signal_tables_cache:
                    base_tbl = self.signal_tables_cache[rr_clean]
                else:
                    tbl = dict(default_tbl)
                    cand_names = [f"signals_{rr_clean}.json", f"signals_{rr_override}.json"]
                    for k, v in (self.rrdata or {}).items():
                        if str(v).strip().upper() == rr_clean:
                            cand_names.append(f"signals_{k}.json")

                    found = False
                    for d in [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]:
                        for c_fname in cand_names:
                            cand_path = os.path.join(d, c_fname)
                            if os.path.exists(cand_path):
                                try:
                                    with open(cand_path, "r", encoding="utf-8") as f:
                                        raw_tbl = json.load(f)
                                        tbl.update({int(k): str(v) for k, v in raw_tbl.items() if str(k).isdigit()})
                                    found = True
                                    break
                                except Exception:
                                    pass
                        if found:
                            break
                    self.signal_tables_cache[rr_clean] = tbl
                    base_tbl = tbl
        elif wiu_id is not None:
            wiu_str = str(wiu_id)
            nnn = wiu_str[1:4] if len(wiu_str) >= 4 else "default"
            if nnn not in self.signal_tables_cache:
                tbl = dict(default_tbl)
                for d in [self.config_dir, os.getcwd(), os.path.join(os.getcwd(), "config")]:
                    cand_file = os.path.join(d, f"signals_{nnn}.json")
                    if os.path.exists(cand_file):
                        try:
                            with open(cand_file, "r", encoding="utf-8") as f:
                                raw_tbl = json.load(f)
                                tbl.update({int(k): str(v) for k, v in raw_tbl.items() if str(k).isdigit()})
                            break
                        except Exception:
                            pass
                self.signal_tables_cache[nnn] = tbl
            base_tbl = self.signal_tables_cache[nnn]

        base_aspect = base_tbl.get(signal_val, default_tbl.get(signal_val, "top:mg,mid:mg,bot:mg"))

        # Individual value overwrite takes highest precedence
        if val_overrides and signal_val in val_overrides:
            head_map: Dict[str, str] = {}
            for part in str(base_aspect).split(","):
                part = part.strip()
                if ":" in part:
                    k, v = part.split(":", 1)
                    k_norm = SIGNAL_POS_ALIASES.get(k.strip().lower(), k.strip().lower())
                    head_map[k_norm] = v.strip().lower()

            for pos_k, col_v in val_overrides[signal_val].items():
                head_map[pos_k] = col_v

            ordered_parts = []
            for h in ("top", "middle", "bottom"):
                if h in head_map:
                    ordered_parts.append(f"{h}:{head_map[h]}")
            for h, c in head_map.items():
                if h not in ("top", "middle", "bottom"):
                    ordered_parts.append(f"{h}:{c}")
            return ",".join(ordered_parts)

        return base_aspect

    def get_signal_gps(self, sig: Signal) -> Optional[Tuple[float, float]]:
        """Resolve GPS coordinates for a signal."""
        if sig.gps:
            try:
                return (float(sig.gps[0]), float(sig.gps[1]))
            except (ValueError, TypeError, IndexError):
                pass
        return None

    def _get_element_gps_any(self, el: BaseElement) -> Optional[Tuple[float, float]]:
        """Resolve GPS coordinates for any layout element (signal, track segment, turnout)."""
        if el.gps:
            try:
                return (float(el.gps[0]), float(el.gps[1]))
            except (ValueError, TypeError, IndexError):
                pass
        if isinstance(el, Signal):
            return self.get_signal_gps(el)
        for other in self.elements.values():
            if isinstance(other, Signal) and (other.protects_element == el.id or other.rear_element == el.id):
                sg = self.get_signal_gps(other)
                if sg:
                    return sg
        return None

    @staticmethod
    def _haversine_miles(gps1: Tuple[float, float], gps2: Tuple[float, float]) -> float:
        R_miles = 3958.8
        lat1, lon1 = math.radians(gps1[0]), math.radians(gps1[1])
        lat2, lon2 = math.radians(gps2[0]), math.radians(gps2[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * R_miles * math.asin(math.sqrt(a))

    def _infer_direction_between(self, prev_el: BaseElement, curr_el: BaseElement) -> Optional[str]:
        if isinstance(prev_el, TrackSegment):
            for d, nbrs in prev_el.neighbors.items():
                if curr_el.id in nbrs: return d
        if curr_el.gps and prev_el.gps:
            dlat = curr_el.gps[0] - prev_el.gps[0]
            dlon = curr_el.gps[1] - prev_el.gps[1]
            if abs(dlon) >= abs(dlat): return "E" if dlon > 0 else "W"
            return "N" if dlat > 0 else "S"
        return None

    def record_train_sighting(self, elem_id: str, timestamp: float, direction_hint: Optional[str] = None):
        curr_el = self.elements.get(elem_id)
        if not curr_el: return
        matched_prev_id, speed_mph, inferred_dir = None, None, direction_hint

        for prev_id, prev_ts in reversed(self.recent_sightings):
            if prev_id == elem_id: continue
            dt_real = timestamp - prev_ts
            if dt_real <= 0.05 or dt_real > 60.0: continue
            prev_el = self.elements.get(prev_id)
            if not prev_el: continue

            dist_miles = None
            curr_gps = self._get_element_gps_any(curr_el)
            prev_gps = self._get_element_gps_any(prev_el)
            if curr_gps and prev_gps:
                dist_miles = self._haversine_miles(prev_gps, curr_gps)
            elif curr_el.mile_marker is not None and prev_el.mile_marker is not None:
                dist_miles = abs(curr_el.mile_marker - prev_el.mile_marker)

            if dist_miles and 0.001 <= dist_miles <= 25.0:
                calc_spd = (dist_miles / dt_real) * 3600.0
                speed_mph = round(calc_spd, 1) if calc_spd <= 150.0 else None
                inferred_dir = direction_hint or self._infer_direction_between(prev_el, curr_el) or "E"
                matched_prev_id = prev_id
                break

        train_name = self.train_scroller.update_or_assign_train(elem_id, curr_el.label, matched_prev_id, speed_mph, inferred_dir, timestamp)
        if hasattr(curr_el, "_train_name"): curr_el._train_name = train_name
        if hasattr(curr_el, "_estimated_speed_mph"): curr_el._estimated_speed_mph = speed_mph or self.train_scroller.trains[train_name].get("speed_mph")
        if hasattr(curr_el, "_train_direction"): curr_el._train_direction = inferred_dir or self.train_scroller.trains[train_name].get("direction")
        self.recent_sightings.append((elem_id, timestamp))

    def notify_signal_dropped(self, sig: Signal, timestamp: float):
        if not getattr(self, "enable_train_inference", True):
            return
        TrainInferenceManager.notify_signal_dropped(
            sig=sig,
            elements=self.elements,
            train_tracker=self.train_scroller,
            now=timestamp,
            get_signal_gps_fn=self.get_signal_gps,
            resolve_jump_fn=self._resolve_through_jump,
        )
        self._evaluate_system_logic(timestamp, force=True)

    def _resolve_through_jump(self, next_id: Optional[str], next_dir: str, curr_id: Optional[str] = None) -> Tuple[Optional[str], str]:
        """If next_id is a TrackJump, or if next_id is None and curr_id connects to a TrackJump, traverse to the paired TrackJump's connected_element."""
        jump_el: Optional[TrackJump] = None
        if next_id:
            cand = self.elements.get(next_id)
            if isinstance(cand, TrackJump):
                jump_el = cand
        elif curr_id:
            # Fast cached lookup of TrackJumps by connected_element
            cache_key = len(self.elements)
            if getattr(self, "_jump_cache_len", -1) != cache_key:
                self._jumps_by_conn = {
                    el.connected_element: el
                    for el in self.elements.values()
                    if isinstance(el, TrackJump) and el.connected_element
                }
                self._jump_cache_len = cache_key
            cand_j = self._jumps_by_conn.get(curr_id)
            if cand_j is not None and cand_j.connected_element == curr_id and cand_j.id in self.elements:
                jump_el = cand_j
            else:
                for el in self.elements.values():
                    if isinstance(el, TrackJump) and el.connected_element == curr_id:
                        jump_el = el
                        self._jumps_by_conn[curr_id] = el
                        break
        if not jump_el:
            return next_id, next_dir

        partner: Optional[TrackJump] = None
        if jump_el.paired_jump_id:
            p = self.elements.get(jump_el.paired_jump_id)
            if isinstance(p, TrackJump) and p.id != jump_el.id:
                partner = p
        if not partner and jump_el.letter:
            target_letter = jump_el.letter.strip().upper()
            for el in self.elements.values():
                if isinstance(el, TrackJump) and el.id != jump_el.id and el.letter.strip().upper() == target_letter:
                    partner = el
                    break
        if partner and partner.connected_element:
            return partner.connected_element, (partner.direction or next_dir)
        return None, next_dir

    def _find_adjacent_block(self, start_el: Optional[BaseElement], direction: str) -> Optional[BaseElement]:
        if not start_el: return None
        nxt_id, nxt_dir = None, direction
        if isinstance(start_el, TrackSegment):
            nxt_id, nxt_dir = start_el.next_in_direction(direction)
        elif isinstance(start_el, Turnout):
            nxt_id = start_el.active_branch_neighbor()
        nxt_id, _ = self._resolve_through_jump(nxt_id, nxt_dir, curr_id=start_el.id)
        return self.elements.get(nxt_id) if nxt_id else None

    def _project_train_path(self, start_id: str, travel_dir: str, steps: int = 3) -> List[str]:
        projected = []
        curr_id, prev_id, curr_dir = start_id, None, travel_dir or "E"
        for _ in range(steps):
            curr_el = self.elements.get(curr_id)
            if not curr_el: break
            next_id, next_dir = None, curr_dir
            if isinstance(curr_el, TrackSegment):
                next_id, next_dir = curr_el.next_in_direction(curr_dir, prev_id=prev_id)
            elif isinstance(curr_el, Turnout):
                if prev_id == curr_el.common_neighbor or curr_dir in COMPATIBLE_DIRS.get(curr_el.orientation, ()):
                    next_id, next_dir = curr_el.active_branch_neighbor(), curr_el.active_branch_dir()
                else:
                    next_id, next_dir = curr_el.common_neighbor, OPPOSITE_DIR.get(curr_el.orientation, curr_dir)
            next_id, next_dir = self._resolve_through_jump(next_id, next_dir, curr_id=curr_id)
            if not next_id or next_id in projected: break
            projected.append(next_id)
            prev_id, curr_id, curr_dir = curr_id, next_id, next_dir
        return projected

    def _detect_train_collisions(self):
        self.active_collisions.clear()
        for el in self.elements.values():
            if isinstance(el, (TrackSegment, Turnout)):
                el._collision_warning, el._collision_details = False, ""

        # Only raise warnings if the two trains are on the same track going towards each other
        # and are not more than 2 track sections apart with no turnouts.
        occupied_tracks = [
            el for el in self.elements.values()
            if isinstance(el, TrackSegment) and el.is_occupied
        ]
        if len(occupied_tracks) < 2:
            return

        seen_pairs = set()
        for i, node_a in enumerate(occupied_tracks):
            for node_b in occupied_tracks[i + 1:]:
                pair_key = tuple(sorted((node_a.id, node_b.id)))
                if pair_key in seen_pairs:
                    continue

                tname_a = getattr(node_a, "_train_name", None)
                tname_b = getattr(node_b, "_train_name", None)
                if tname_a and tname_b and tname_a == tname_b:
                    continue

                dir_a = getattr(node_a, "_train_direction", None) or "E"
                dir_b = getattr(node_b, "_train_direction", None) or "E"

                # Must be going towards each other (opposing directions)
                if OPPOSITE_DIR.get(dir_a) != dir_b:
                    continue

                curr_id = node_a.id
                curr_dir = dir_a
                prev_id = None
                found_b = False
                sections_between = 0
                intermediate_elements = []

                for hop in range(1, 4):  # 1 hop = 0 sections apart, 2 hops = 1 section apart, 3 hops = 2 sections apart
                    curr_el = self.elements.get(curr_id)
                    if not isinstance(curr_el, TrackSegment):
                        break

                    next_id, next_dir = curr_el.next_in_direction(curr_dir, prev_id=prev_id)
                    next_id, next_dir = self._resolve_through_jump(next_id, next_dir, curr_id=curr_id)

                    if not next_id:
                        break

                    cand = self.elements.get(next_id)
                    if not cand or isinstance(cand, Turnout) or not isinstance(cand, TrackSegment):
                        # Any non-track or turnout halts trace (no turnouts allowed)
                        break

                    if cand.id == node_b.id:
                        found_b = True
                        sections_between = hop - 1
                        break
                    else:
                        intermediate_elements.append(cand.id)
                        prev_id, curr_id, curr_dir = curr_id, next_id, next_dir

                if found_b and sections_between <= 2:
                    seen_pairs.add(pair_key)
                    spd_a = getattr(node_a, "_estimated_speed_mph", None) or 25.0
                    spd_b = getattr(node_b, "_estimated_speed_mph", None) or 25.0
                    closing_mph = spd_a + spd_b

                    dist_miles = 0.5 * (sections_between + 1)
                    if node_a.gps and node_b.gps:
                        dist_miles = max(0.05, self._haversine_miles(node_a.gps, node_b.gps))
                    ttc_sec = round((dist_miles / max(1.0, closing_mph)) * 3600.0, 1)

                    desc_a = tname_a or node_a.id
                    desc_b = tname_b or node_b.id
                    alert_msg = (
                        f"HEAD_ON WARNING: {desc_a} ({dir_a}) & {desc_b} ({dir_b}) "
                        f"on same track ({sections_between} section(s) apart) ~{ttc_sec}s to impact"
                    )

                    self.active_collisions.append({
                        "type": "HEAD_ON",
                        "train_a": desc_a,
                        "train_b": desc_b,
                        "focal_element": intermediate_elements[0] if intermediate_elements else node_a.id,
                        "sections_apart": sections_between,
                        "distance_miles": round(dist_miles, 2),
                        "closing_speed_mph": round(closing_mph, 1),
                        "ttc_sec": ttc_sec,
                        "message": alert_msg,
                    })

                    node_a._collision_warning = True
                    node_a._collision_details = alert_msg
                    node_b._collision_warning = True
                    node_b._collision_details = alert_msg
                    for mid_id in intermediate_elements:
                        mid_el = self.elements.get(mid_id)
                        if mid_el:
                            mid_el._collision_warning = True
                            mid_el._collision_details = alert_msg

    def _refresh_green_paths_and_crossings(self, now: float):
        """Immediately updates green route paths on tracks/turnouts and crossing gate statuses."""
        for el in self.elements.values():
            if isinstance(el, (TrackSegment, Turnout)):
                el._green_path = False
                el._green_path_dir = None

        permissive_signals = [
            el for el in self.elements.values()
            if isinstance(el, Signal) and el.is_permissive and not el.is_dark
        ]
        for sig in permissive_signals:
            path = TrainInferenceManager.trace_signal_green_path(sig, self.elements, self._resolve_through_jump)
            if path:
                for elem_id, elem_dir in path:
                    obj = self.elements.get(elem_id)
                    if isinstance(obj, (TrackSegment, Turnout)) and not obj.is_occupied:
                        obj._green_path = True
                        obj._green_path_dir = elem_dir

        for el in self.elements.values():
            if isinstance(el, Crossing):
                occupied_nearby = any(getattr(self.elements.get(tid), "is_occupied", False) for tid in el.monitored_tracks)
                prev_gate = el._gate_status
                el._gate_status = "DOWN" if occupied_nearby else "OPEN"
                if el._gate_status != prev_gate or occupied_nearby:
                    el._last_packet_ts = now

    def _evaluate_system_logic(self, now: float, force: bool = True):
        if not force and self.train_logic_interval_sec > 0:
            if (now - self.last_train_eval_ts) < self.train_logic_interval_sec:
                self._refresh_green_paths_and_crossings(now)
                return
        self.last_train_eval_ts = now

        TrainInferenceManager.evaluate_system_trains(
            elements=self.elements,
            train_tracker=self.train_scroller,
            now=now,
            inferred_timeout_sec=self.inferred_timeout_sec,
            get_signal_gps_fn=self.get_signal_gps,
            resolve_jump_fn=self._resolve_through_jump,
        )

        for el in self.elements.values():
            if isinstance(el, Crossing):
                occupied_nearby = any(getattr(self.elements.get(tid), "is_occupied", False) for tid in el.monitored_tracks)
                prev_gate = el._gate_status
                el._gate_status = "DOWN" if occupied_nearby else "OPEN"
                if el._gate_status != prev_gate or occupied_nearby:
                    el._last_packet_ts = now
        self._detect_train_collisions()

    def _trace_signal_green_path(self, sig: Signal) -> List[Tuple[str, str]]:
        return TrainInferenceManager.trace_signal_green_path(sig, self.elements, self._resolve_through_jump)

    def _extract_downstream_signals(self, sig: Signal, path: List[Tuple[str, str]]) -> List[str]:
        return TrainInferenceManager.extract_downstream_signals(sig, path, self.elements)

    def _are_elements_continuous(self, el1_id: str, el2_id: str, travel_dir: str) -> bool:
        return TrainInferenceManager.are_elements_continuous(el1_id, el2_id, travel_dir, self.elements, self._resolve_through_jump)

    def _is_path_reachable(self, start_id: str, target_id: str, travel_dir: str, max_depth: int = 6) -> bool:
        return TrainInferenceManager.is_path_reachable(start_id, target_id, travel_dir, self.elements, self._resolve_through_jump, max_depth)

    def _are_trains_on_same_continuous_path(self, trn1: dict, trn2: dict) -> Tuple[bool, bool]:
        return TrainInferenceManager.are_trains_on_same_continuous_path(trn1, trn2, self.elements, self._resolve_through_jump)

    def _merge_two_potential_trains(self, upstream_key: str, downstream_key: str):
        TrainInferenceManager.merge_two_potential_trains(self.train_scroller, upstream_key, downstream_key)

    def _merge_potential_trains_on_continuous_paths(self):
        TrainInferenceManager.merge_potential_trains_on_continuous_paths(self.train_scroller, self.elements, self._resolve_through_jump)

    def delete_train(self, train_key: str) -> dict:
        deleted = self.train_scroller.delete_train(train_key)
        if deleted:
            curr_el = self.elements.get(deleted.get("current_element_id"))
            if curr_el:
                curr_el._inferred_occupied = False
                curr_el._train_name = None
                curr_el._estimated_speed_mph = None
                curr_el._train_direction = None
                curr_el._is_dimmed = False
            self._last_transmitted_signature = self._compute_logical_signature()
            self.broadcast_state(self.get_render_state())
            return {"ok": True, "deleted": deleted.get("train_key")}
        return {"ok": False, "error": f"Train '{train_key}' not found"}

    def assign_train_info(self, data_or_key: Any, assigned_id: Optional[Any] = None, assigned_name: Optional[str] = None) -> dict:
        if isinstance(data_or_key, dict):
            tkey = data_or_key.get("train_key") or data_or_key.get("train_name")
            assigned_id = data_or_key.get("assigned_id")
            assigned_name = data_or_key.get("assigned_name")
        else:
            tkey = str(data_or_key)
        res = self.train_scroller.assign_info(
            tkey,
            assigned_id=assigned_id,
            assigned_name=assigned_name,
            last_packets_by_wiu=self.last_packets_by_wiu
        )
        if isinstance(res, dict) and "error" not in res:
            curr_el = self.elements.get(res.get("current_element_id"))
            if curr_el:
                curr_el._train_name = res.get("assigned_name") or res.get("train_name")
            self._last_transmitted_signature = self._compute_logical_signature()
            self.broadcast_state(self.get_render_state())
        return res

    def _compute_logical_signature(self) -> str:
        parts = [f"v:{self.version}"]
        sorted_ids = getattr(self, "_sorted_element_ids", None)
        if sorted_ids is None or len(sorted_ids) != len(self.elements):
            self._sorted_element_ids = sorted(self.elements.keys())
            sorted_ids = self._sorted_element_ids
        for eid in sorted_ids:
            el = self.elements[eid]
            if isinstance(el, TrackSegment):
                parts.append(f"{eid}:{el._hw_occupied}:{el._inferred_occupied}:{el._green_path}:{el._green_path_dir}:{el._train_name}:{el._train_direction}:{el._estimated_speed_mph}:{el._collision_warning}:{el._packets_received}:{round(el._last_packet_ts, 3)}")
            elif isinstance(el, Turnout):
                parts.append(f"{eid}:{el._state}:{el._hw_occupied}:{el._inferred_occupied}:{el._green_path}:{el._green_path_dir}:{el._collision_warning}:{el._packets_received}:{round(el._last_packet_ts, 3)}")
            elif isinstance(el, Signal):
                parts.append(f"{eid}:{el._lamp_aspect}:{el._hw_occupied}:{el._packets_received}:{round(el._last_packet_ts, 3)}")
            elif isinstance(el, Crossing):
                parts.append(f"{eid}:{el._gate_status}:{el._packets_received}:{round(el._last_packet_ts, 3)}")
            elif isinstance(el, BaseRadio):
                parts.append(f"{eid}:{el.radio_id}:{el._packets_received}:{el._packets_relayed}")
            elif isinstance(el, PTCDateTime):
                parts.append(f"{eid}:{el.last_datetime_str}:{el._packets_received}")
        for col in self.active_collisions: parts.append(col["message"])
        if self.scroller_enabled:
            parts.append(f"pkts:{self.scroller.total_packets}")
        if getattr(self, "address_table_enabled", False):
            parts.append(f"at:{self.address_table.total_packets}")
        if getattr(self, "train_scroller_enabled", False):
            ts_summary = ";".join(f"{k}:{v.get('status')}:{v.get('is_dimmed')}:{v.get('speed_str')}:{v.get('is_receiving_packets')}:{v.get('assigned_name')}:{v.get('assigned_id')}" for k, v in sorted(self.train_scroller.trains.items()))
            parts.append(f"ts:{len(self.train_scroller.trains)}:{ts_summary}")
        if getattr(self, "chat_enabled", False) or (hasattr(self, "chat_panel") and len(self.chat_panel.messages) > 0):
            parts.append(f"chat:{self.chat_panel.total_messages}:{len(self.chat_panel.messages)}")
        parts.append(f"cache:{len(self.packet_cache)}")
        return "|".join(parts)

    def ingest_raw_packet(self, raw_packet: Any, meta: Optional[dict] = None) -> dict:
        # Check Banc time-based deduplication: discard packets already seen based on time
        if meta and meta.get("source") == "BANC":
            banc_time = float(meta.get("stamp_received") or meta.get("timestamp") or meta.get("unix_ts") or 0.0)
            if banc_time > 0.0 and self.last_seen_banc_time > 0.0 and banc_time < (self.last_seen_banc_time - 0.001):
                self.last_ingest_status = "ALREADY_SEEN_TIME"
                return self.get_render_state()
            if banc_time > self.last_seen_banc_time:
                self.last_seen_banc_time = banc_time

        # Determine full raw hex string of incoming packet before and after transport stripping
        if isinstance(raw_packet, (bytes, bytearray)):
            orig_raw_hex = bytes(raw_packet).hex().upper()
        elif isinstance(raw_packet, str):
            clean_s = raw_packet.strip().replace(" ", "")
            if len(clean_s) >= 2 and len(clean_s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in clean_s):
                orig_raw_hex = clean_s.upper()
                try:
                    raw_packet = bytes.fromhex(clean_s)
                except Exception:
                    pass
            else:
                orig_raw_hex = clean_s.encode("utf-8", errors="ignore").hex().upper()
        elif isinstance(raw_packet, dict):
            orig_raw_hex = (
                raw_packet.get("raw_hex")
                or raw_packet.get("_raw_hex_")
                or raw_packet.get("hex")
                or ""
            )
            if not orig_raw_hex and "bytes" in raw_packet and isinstance(raw_packet["bytes"], (bytes, bytearray)):
                orig_raw_hex = bytes(raw_packet["bytes"]).hex().upper()
            if not orig_raw_hex:
                orig_raw_hex = str(raw_packet).encode("utf-8", errors="ignore").hex().upper()
        else:
            orig_raw_hex = str(raw_packet).encode("utf-8", errors="ignore").hex().upper()

        # Handle optional CHxxx: or CHxxx:RSSI=...: prefix injected by ApexBOSFlux.py on UDP multicast
        if isinstance(raw_packet, (bytes, bytearray)):
            m_ch = RE_CH_RSSI_BYTES.match(raw_packet)
            if m_ch:
                mosfet_ch = int(m_ch.group(1))
                meta = dict(meta or {})
                meta["channel"] = mosfet_ch
                if m_ch.group(2):
                    try:
                        meta["rssi"] = float(m_ch.group(2))
                    except ValueError:
                        pass
                raw_packet = bytes(raw_packet[m_ch.end():])
                orig_raw_hex = bytes(raw_packet).hex().upper()
            elif len(raw_packet) > 6 and raw_packet[:2] == b"CH" and raw_packet[5:6] == b":" and raw_packet[2:5].isdigit():
                mosfet_ch = int(raw_packet[2:5])
                raw_packet = bytes(raw_packet[6:])
                orig_raw_hex = bytes(raw_packet).hex().upper()
                meta = dict(meta or {})
                meta["channel"] = mosfet_ch
        elif isinstance(raw_packet, str):
            m_ch = RE_CH_RSSI_STR.match(raw_packet)
            if m_ch:
                mosfet_ch = int(m_ch.group(1))
                meta = dict(meta or {})
                meta["channel"] = mosfet_ch
                if m_ch.group(2):
                    try:
                        meta["rssi"] = float(m_ch.group(2))
                    except ValueError:
                        pass
                raw_packet = raw_packet[m_ch.end():]
                clean_p = raw_packet.strip().replace(" ", "")
                if len(clean_p) >= 2 and len(clean_p) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in clean_p):
                    orig_raw_hex = clean_p.upper()
                else:
                    orig_raw_hex = clean_p.encode("utf-8", errors="ignore").hex().upper()
            elif len(raw_packet) > 6 and raw_packet.startswith("CH") and raw_packet[5] == ":" and raw_packet[2:5].isdigit():
                mosfet_ch = int(raw_packet[2:5])
                raw_packet = raw_packet[6:]
                clean_p = raw_packet.strip().replace(" ", "")
                if len(clean_p) >= 2 and len(clean_p) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in clean_p):
                    orig_raw_hex = clean_p.upper()
                else:
                    orig_raw_hex = clean_p.encode("utf-8", errors="ignore").hex().upper()
                meta = dict(meta or {})
                meta["channel"] = mosfet_ch

        # Also strip leading 'I' framing marker from MosFET ZMQ packets if present before raw bytes
        candidates = [raw_packet]
        if isinstance(raw_packet, (bytes, bytearray)) and raw_packet.startswith(b"I") and len(raw_packet) > 1:
            candidates.append(bytes(raw_packet[1:]))

        self.last_raw_hex = orig_raw_hex

        # Independent raw packet logging to temporary file when toggled ON
        if getattr(self, "raw_packet_logging", False):
            try:
                ch_log = meta.get("channel", 0) if meta else 0
                rssi_log = meta.get("rssi", -75.0) if meta else -75.0
                log_entry = {
                    "timestamp": time.time(),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "channel": ch_log,
                    "rssi": round(rssi_log, 1) if isinstance(rssi_log, (int, float)) else rssi_log,
                    "hex": orig_raw_hex,
                    "raw_hex": orig_raw_hex,
                }
                with open(self.raw_log_filepath, "a", encoding="utf-8") as f_log:
                    f_log.write(json.dumps(log_entry) + "\n")
                self.raw_log_count += 1
            except Exception as exc:
                self._engine_log("WARN", f"Failed writing raw packet log: {exc}")

        parsed_data = None
        if isinstance(raw_packet, dict) and (raw_packet.get("is_valid", True) or "_save_" in raw_packet or "_type_" in raw_packet or "_node_id_" in raw_packet):
            parsed_data = dict(raw_packet.get("data", raw_packet))
        if not parsed_data:
            for cand in candidates:
                for idx, parser_obj in enumerate(self.parsers):
                    try:
                        res = parser_obj.parse(cand)
                        if isinstance(res, dict) and res.get("is_valid"):
                            parsed_data = dict(res.get("data", res))
                            if idx > 0:
                                self.parsers.insert(0, self.parsers.pop(idx))
                            break
                    except Exception:
                        pass
                if parsed_data:
                    break
        if not parsed_data:
            self.last_ingest_status = "INVALID"
            self.last_parsed_packet = None
            raw_for_hex = candidates[-1] if candidates else raw_packet
            self.last_invalid_hex = orig_raw_hex
            self.scroller.add_invalid_packet(raw_for_hex, meta=meta)
            self.packet_cache.append({
                "is_valid": False,
                "raw_hex": orig_raw_hex,
                "timestamp": time.time(),
                "channel": meta.get("channel", 0) if meta else 0,
                "rssi": meta.get("rssi", -75.0) if meta else -75.0,
                "status": "INVALID",
            })
            if self.scroller_enabled:
                self._last_transmitted_signature = self._compute_logical_signature()
                self.transmitted_frames_count += 1
                state = self.get_render_state()
                self.broadcast_state(state)
                return state
            return None

        parsed_data["timestamp"] = time.time()
        parsed_data["raw_hex"] = orig_raw_hex
        parsed_data["_raw_hex_"] = orig_raw_hex
        if meta:
            parsed_data["rssi"] = meta.get("rssi", -75.0)
            meta_ch = meta.get("channel")
            if meta_ch is not None and int(meta_ch) > 0:
                parsed_data["channel_meta"] = int(meta_ch)
                parsed_data["Channel"] = int(meta_ch)
            else:
                parsed_data["channel_meta"] = parsed_data.get("Channel", 0)
            parsed_data["is_locomotive"] = meta.get("is_locomotive", False)
        else:
            parsed_data.setdefault("rssi", -75.0)
            parsed_data.setdefault("is_locomotive", False)
        self.last_parsed_packet = parsed_data
        self.packet_cache.append(dict(parsed_data))

        # Save decoded packets that have an element "_save_" in their dictionary
        if self._should_save_packet(parsed_data):
            self.save_saved_packet(parsed_data, raw_hex=orig_raw_hex)

        if parsed_data.get("is_locomotive", False):
            self.last_ingest_status = "LOCOMOTIVE_IGNORED"
            return None
        if not self.address_table.check_and_update_sequence(parsed_data, self.rrdata):
            self.suppressed_frames_count += 1
            self.last_ingest_status = "DUPLICATE_SEQ"
            return None

        wiu_node_id = parsed_data.get("_node_id_")
        if wiu_node_id is not None:
            try:
                self.last_packets_by_wiu[int(wiu_node_id)] = parsed_data
            except (ValueError, TypeError):
                pass
            pkt_name = (
                parsed_data.get("_wiu_name_")
                or parsed_data.get("wiu_name")
                or parsed_data.get("_node_name_")
                or parsed_data.get("node_name")
                or parsed_data.get("name")
                or self.get_layout_wiu_name(wiu_node_id)
            )
            pkt_lat = parsed_data.get("lat") if parsed_data.get("lat") is not None else parsed_data.get("latitude")
            pkt_lon = parsed_data.get("lon") if parsed_data.get("lon") is not None else parsed_data.get("longitude")
            pkt_gps = parsed_data.get("gps")
            pkt_ctc = (
                parsed_data.get("_ctc_id_")
                or parsed_data.get("ctc_id")
                or parsed_data.get("_ctc_addresses_")
                or parsed_data.get("ctc_addresses")
            )
            pkt_has_ctc = bool(parsed_data.get("has_ctc")) or bool(pkt_ctc)
            self._record_wiu_info(
                wiu_node_id,
                name=pkt_name,
                gps=pkt_gps,
                lat=pkt_lat,
                lon=pkt_lon,
                has_ctc=pkt_has_ctc,
                ctc_addresses=pkt_ctc,
                overwrite_gps=(pkt_gps is not None or (pkt_lat is not None and pkt_lon is not None)),
                refresh_on_seen=True,
            )
        self.scroller.add_packet(parsed_data)
        self.train_scroller.check_packet(parsed_data)
        self._dispatch_packet_to_elements(parsed_data)

        self._evaluate_system_logic(parsed_data["timestamp"], force=False)
        sig = self._compute_logical_signature()
        if sig != self._last_transmitted_signature:
            self._last_transmitted_signature = sig
            self.transmitted_frames_count += 1
            self.last_ingest_status = "BROADCAST"
            state = self.get_render_state()
            self.broadcast_state(state)
            return state
        else:
            self.suppressed_frames_count += 1
            self.last_ingest_status = "NO_STATE_CHANGE"
            return None

    def apply_temp_override(self, ovr: dict):
        eid = ovr.get("id")
        if not eid or eid not in self.elements:
            return
        el = self.elements[eid]
        if eid not in self._orig_element_bindings:
            self._orig_element_bindings[eid] = {
                "wiu_id": getattr(el, "wiu_id", None),
                "has_occ_bit": getattr(el, "has_occ_bit", False),
                "occ_bit_offset": getattr(el, "occ_bit_offset", None),
                "cmd_bit_offset": getattr(el, "cmd_bit_offset", None),
                "sig_bit_offset": getattr(el, "sig_bit_offset", None),
            }
        if "wiu_id" in ovr:
            el.wiu_id = int(ovr["wiu_id"]) if ovr["wiu_id"] not in (None, "") else None
        if "occ_bit_offset" in ovr and hasattr(el, "occ_bit_offset"):
            v = ovr["occ_bit_offset"]
            el.occ_bit_offset = int(v) if v not in (None, "") else None
            if hasattr(el, "has_occ_bit"):
                el.has_occ_bit = el.occ_bit_offset is not None
        if "cmd_bit_offset" in ovr and hasattr(el, "cmd_bit_offset"):
            v = ovr["cmd_bit_offset"]
            el.cmd_bit_offset = int(v) if v not in (None, "") else None
        if "sig_bit_offset" in ovr and hasattr(el, "sig_bit_offset"):
            v = ovr["sig_bit_offset"]
            el.sig_bit_offset = int(v) if v not in (None, "") else None
        if el.wiu_id is not None and int(el.wiu_id) in self.last_packets_by_wiu:
            el.process_packet(self.last_packets_by_wiu[int(el.wiu_id)], self)
        self._evaluate_system_logic(time.time())
        self.broadcast_state(self.get_render_state())

    def revert_temp_override(self, eid: Optional[str]):
        if not eid or eid not in self._orig_element_bindings or eid not in self.elements:
            return
        orig = self._orig_element_bindings.pop(eid)
        el = self.elements[eid]
        for k, v in orig.items():
            if hasattr(el, k):
                setattr(el, k, v)
        if el.wiu_id is not None and int(el.wiu_id) in self.last_packets_by_wiu:
            el.process_packet(self.last_packets_by_wiu[int(el.wiu_id)], self)
        self._evaluate_system_logic(time.time())
        self.broadcast_state(self.get_render_state())


    # -----------------------------------------------------------------
    # RAM Packet Cache & History Persistence
    # -----------------------------------------------------------------
    def get_layout_wiu_name(self, wiu_id: Any) -> str:
        """Looks up the WIU name from the loaded layout based on WIU ID."""
        if wiu_id is None:
            return ""
        try:
            wid_int = int(wiu_id)
        except (ValueError, TypeError):
            return ""

        raw_data = getattr(self, "raw_layout_data", None)
        if isinstance(raw_data, dict):
            raw_wius = raw_data.get("wius")
            if isinstance(raw_wius, dict):
                entry = raw_wius.get(str(wid_int)) or raw_wius.get(wid_int)
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
                elif isinstance(entry, dict):
                    name = entry.get("name") or entry.get("label") or ""
                    if str(name).strip():
                        return str(name).strip()
            elif isinstance(raw_wius, list):
                for item in raw_wius:
                    if isinstance(item, dict) and "wiu_id" in item:
                        try:
                            if int(item["wiu_id"]) == wid_int:
                                name = item.get("name") or item.get("label") or ""
                                if str(name).strip():
                                    return str(name).strip()
                        except (ValueError, TypeError):
                            pass

        for el in self.elements.values():
            el_wid = getattr(el, "wiu_id", None)
            if el_wid is None and getattr(el, "elem_type", "") == "base":
                el_wid = getattr(el, "radio_id", None)
            if el_wid is not None:
                try:
                    if int(el_wid) == wid_int:
                        lbl = getattr(el, "label", "") or getattr(el, "name", "") or ""
                        if str(lbl).strip():
                            return str(lbl).strip()
                except (ValueError, TypeError):
                    pass
        return ""

    def get_all_layout_wiu_names(self) -> Dict[int, str]:
        """Returns a dict mapping wiu_id (int) to layout name."""
        names: Dict[int, str] = {}
        raw_data = getattr(self, "raw_layout_data", None)
        if isinstance(raw_data, dict):
            raw_wius = raw_data.get("wius")
            if isinstance(raw_wius, dict):
                for k, v in raw_wius.items():
                    try:
                        wid = int(k)
                        name = v if isinstance(v, str) else (v.get("name") or v.get("label") or "")
                        if str(name).strip():
                            names[wid] = str(name).strip()
                    except (ValueError, TypeError):
                        pass
            elif isinstance(raw_wius, list):
                for item in raw_wius:
                    if isinstance(item, dict) and "wiu_id" in item:
                        try:
                            wid = int(item["wiu_id"])
                            name = item.get("name") or item.get("label") or ""
                            if str(name).strip():
                                names[wid] = str(name).strip()
                        except (ValueError, TypeError):
                            pass

        for el in self.elements.values():
            el_wid = getattr(el, "wiu_id", None)
            if el_wid is None and getattr(el, "elem_type", "") == "base":
                el_wid = getattr(el, "radio_id", None)
            if el_wid is not None:
                try:
                    wid = int(el_wid)
                    lbl = getattr(el, "label", "") or getattr(el, "name", "") or ""
                    if str(lbl).strip() and (wid not in names or not names[wid]):
                        names[wid] = str(lbl).strip()
                except (ValueError, TypeError):
                    pass
        return names

    # -----------------------------------------------------------------
    # WIU Metadata Tracking & Synchronization Helpers
    # -----------------------------------------------------------------
    @staticmethod
    def _normalize_wiu_id_str(val: Any) -> str:
        if val is None or isinstance(val, bool):
            return ""
        s = str(val).strip()
        if not s:
            return ""
        if re.fullmatch(r"-?\d+", s):
            try:
                return str(int(s))
            except Exception:
                pass
        return s

    @staticmethod
    def _format_valid_gps(gps_val: Any = None, lat_val: Any = None, lon_val: Any = None) -> str:
        lat = None
        lon = None
        if isinstance(gps_val, (list, tuple)) and len(gps_val) >= 2:
            lat, lon = gps_val[0], gps_val[1]
        elif isinstance(gps_val, dict):
            lat = gps_val.get("lat") if gps_val.get("lat") is not None else gps_val.get("latitude")
            lon = gps_val.get("lon") if gps_val.get("lon") is not None else gps_val.get("longitude")
        elif isinstance(gps_val, str) and gps_val.strip():
            parts = [p.strip() for p in re.split(r"[,;\s]+", gps_val.strip("()[] ")) if p.strip()]
            if len(parts) >= 2:
                lat, lon = parts[0], parts[1]
        if lat is None or lon is None:
            if lat_val is not None and lon_val is not None:
                lat, lon = lat_val, lon_val
        if lat is None or lon is None:
            return ""
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            return ""
        if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
            return ""
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
            return ""
        if lat_f == 0.0 and lon_f == 0.0:
            return ""
        return f"{lat_f:.6f}, {lon_f:.6f}"

    @staticmethod
    def _extract_ctc_list(raw_val: Any) -> List[str]:
        tokens: List[str] = []
        seen = set()

        def _add(item: str):
            tok = str(item).strip()
            if not tok:
                return
            low = tok.lower()
            if low not in seen:
                seen.add(low)
                tokens.append(tok)

        if raw_val is None or isinstance(raw_val, bool):
            return tokens
        if isinstance(raw_val, (list, tuple, set)):
            for elem in raw_val:
                if elem is not None and not isinstance(elem, bool):
                    for part in re.split(r"[,;]+", str(elem)):
                        _add(part)
        else:
            for part in re.split(r"[,;]+", str(raw_val)):
                _add(part)
        return tokens

    def _record_wiu_info(
        self,
        wiu_id: Any,
        name: Any = "",
        gps: Any = None,
        lat: Any = None,
        lon: Any = None,
        has_ctc: bool = False,
        ctc_addresses: Any = None,
        overwrite_name: bool = False,
        overwrite_gps: bool = False,
        refresh_on_seen: bool = False,
        mark_refresh_interval: float = 60.0,
    ):
        wid = self._normalize_wiu_id_str(wiu_id)
        if not wid:
            return
        norm_name = str(name).strip() if name is not None else ""
        norm_gps = self._format_valid_gps(gps, lat, lon)
        new_ctcs = self._extract_ctc_list(ctc_addresses)
        in_has_ctc = bool(has_ctc) or bool(new_ctcs)
        now = time.time()

        with self._wiu_sync_lock:
            rec = self._tracked_wius.get(wid)
            if rec is None:
                self._tracked_wius[wid] = {
                    "wiu_id": wid,
                    "name": norm_name,
                    "gps": norm_gps,
                    "has_ctc": bool(in_has_ctc),
                    "ctc_addresses": ",".join(new_ctcs),
                }
                self._dirty_wiu_ids.add(wid)
                self._wiu_last_report_ts[wid] = now
                return

            changed = False
            if norm_name and (not rec.get("name") or (overwrite_name and norm_name != rec.get("name", ""))):
                rec["name"] = norm_name
                changed = True

            if norm_gps and (not rec.get("gps") or (overwrite_gps and norm_gps != rec.get("gps", ""))):
                rec["gps"] = norm_gps
                changed = True

            if new_ctcs:
                ex_ctcs = self._extract_ctc_list(rec.get("ctc_addresses", ""))
                merged = self._extract_ctc_list(ex_ctcs + new_ctcs)
                merged_csv = ",".join(merged)
                if merged_csv != rec.get("ctc_addresses", ""):
                    rec["ctc_addresses"] = merged_csv
                    changed = True

            new_has = bool(rec.get("has_ctc")) or bool(in_has_ctc) or bool(rec.get("ctc_addresses"))
            if new_has != bool(rec.get("has_ctc")):
                rec["has_ctc"] = new_has
                changed = True

            if changed or (refresh_on_seen and (now - self._wiu_last_report_ts.get(wid, 0.0)) >= mark_refresh_interval):
                self._dirty_wiu_ids.add(wid)
                self._wiu_last_report_ts[wid] = now

    def _refresh_wius_from_layout(self):
        raw_data = getattr(self, "raw_layout_data", None)
        if isinstance(raw_data, dict):
            raw_wius = raw_data.get("wius")
            if isinstance(raw_wius, dict):
                for k, v in raw_wius.items():
                    if isinstance(v, str):
                        self._record_wiu_info(k, name=v, overwrite_name=True)
                    elif isinstance(v, dict):
                        self._record_wiu_info(
                            v.get("wiu_id", k),
                            name=v.get("name") or v.get("label") or "",
                            gps=v.get("gps"),
                            lat=v.get("lat") if v.get("lat") is not None else v.get("latitude"),
                            lon=v.get("lon") if v.get("lon") is not None else v.get("longitude"),
                            has_ctc=bool(v.get("has_ctc")),
                            ctc_addresses=v.get("ctc_addresses") or v.get("ctc_id") or v.get("ctc"),
                            overwrite_name=True,
                            overwrite_gps=True,
                        )
            elif isinstance(raw_wius, list):
                for item in raw_wius:
                    if isinstance(item, dict) and "wiu_id" in item:
                        self._record_wiu_info(
                            item.get("wiu_id"),
                            name=item.get("name") or item.get("label") or "",
                            gps=item.get("gps"),
                            lat=item.get("lat") if item.get("lat") is not None else item.get("latitude"),
                            lon=item.get("lon") if item.get("lon") is not None else item.get("longitude"),
                            has_ctc=bool(item.get("has_ctc")),
                            ctc_addresses=item.get("ctc_addresses") or item.get("ctc_id") or item.get("ctc"),
                            overwrite_name=True,
                            overwrite_gps=True,
                        )

        for el in self.elements.values():
            el_wid = getattr(el, "wiu_id", None)
            if el_wid is None and getattr(el, "elem_type", "") == "base":
                el_wid = getattr(el, "radio_id", None)
            if el_wid is None:
                continue
            el_name = getattr(el, "label", "") or getattr(el, "name", "") or ""
            el_gps = getattr(el, "gps", None)
            el_ctc = getattr(el, "ctc_id", "") or ""
            el_ctc_rules = getattr(el, "ctc_rules", "") or ""
            el_has_ctc = bool(str(el_ctc).strip()) or bool(str(el_ctc_rules).strip())
            self._record_wiu_info(
                el_wid,
                name=el_name,
                gps=el_gps,
                has_ctc=el_has_ctc,
                ctc_addresses=el_ctc,
            )

    def has_dirty_wius(self) -> bool:
        with self._wiu_sync_lock:
            return bool(self._dirty_wiu_ids)

    def get_tracked_wius(self) -> List[dict]:
        with self._wiu_sync_lock:
            return [dict(v) for v in self._tracked_wius.values()]

    def pop_dirty_wius(self) -> List[dict]:
        with self._wiu_sync_lock:
            if not self._dirty_wiu_ids:
                return []
            ids = list(self._dirty_wiu_ids)
            self._dirty_wiu_ids.clear()
            return [dict(self._tracked_wius[wid]) for wid in ids if wid in self._tracked_wius]

    def requeue_dirty_wius(self, wiu_ids: List[Any]):
        with self._wiu_sync_lock:
            for wid in wiu_ids:
                nwid = self._normalize_wiu_id_str(wid)
                if nwid and nwid in self._tracked_wius:
                    self._dirty_wiu_ids.add(nwid)

    def set_dedup_ttl(self, ttl: float):
        """Update duplicate packet deduplication expiration TTL in seconds."""
        self.dedup_ttl = max(0.0, float(ttl))
        if hasattr(self, "address_table") and self.address_table is not None:
            self.address_table.dedup_ttl = self.dedup_ttl

    def set_packet_cache_size(self, size: int):
        """Update the packet cache size and resize the in-memory cache deque."""
        size = max(1, int(size))
        if size != self.packet_cache_size:
            self.packet_cache_size = size
            old_packets = list(self.packet_cache)
            self.packet_cache = deque(old_packets[-size:], maxlen=size)
            self.packet_history = self.packet_cache

    def get_layout_temp_cache_path(self, layout_filepath: Optional[str] = None) -> str:
        """Return the cross-platform temporary file path associated with the layout.
        Uses the system's official temporary directory (tempfile.gettempdir())
        compatible with Linux, Windows, and MacOS.
        """
        target_path = layout_filepath or self.layout_filepath or "default_layout.json"
        base_name = os.path.basename(target_path)
        safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', base_name)
        try:
            abs_norm = os.path.normcase(os.path.abspath(target_path))
        except Exception:
            abs_norm = str(target_path).lower()
        hash_tag = hashlib.sha256(abs_norm.encode("utf-8")).hexdigest()[:12]
        temp_dir = tempfile.gettempdir()
        filename = f"apexbos_packets_{safe_name}_{hash_tag}.json"
        return os.path.join(temp_dir, filename)

    def save_packet_cache_to_temp(self) -> Optional[str]:
        """Save the in-memory packet cache to the system's official temporary directory
        associated with the current layout.
        """
        if not hasattr(self, "packet_cache") or not self.packet_cache:
            return None
        try:
            temp_path = self.get_layout_temp_cache_path()
            cache_list = [_make_json_serializable(p) for p in self.packet_cache]
            payload = {
                "version": self.version,
                "layout_filename": self.layout_filename,
                "layout_filepath": os.path.abspath(self.layout_filepath) if self.layout_filepath else "",
                "saved_at": time.time(),
                "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "packet_cache_size": self.packet_cache_size,
                "packet_count": len(cache_list),
                "packets": cache_list,
            }
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return temp_path
        except Exception as exc:
            self._engine_log("WARN", f"Failed saving temporary packet cache: {exc}")
            return None

    def load_packet_cache_from_temp(self, layout_filepath: Optional[str] = None, apply_to_state: bool = True) -> int:
        """Load cached packets associated with the specified layout from the
        system's official temporary directory, if available.
        """
        temp_path = self.get_layout_temp_cache_path(layout_filepath)
        if not os.path.exists(temp_path):
            return 0
        try:
            with open(temp_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_packets = data.get("packets") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if not raw_packets:
                return 0

            self.packet_cache.clear()
            slice_to_keep = raw_packets[-self.packet_cache_size:] if len(raw_packets) > self.packet_cache_size else raw_packets
            for p in slice_to_keep:
                if isinstance(p, dict):
                    self.packet_cache.append(p)

            if apply_to_state and self.elements:
                self.apply_cached_packets()

            self._engine_log("INFO", f"[{self.version}] Loaded {len(self.packet_cache)} cached packet(s) for layout '{self.layout_filename}' from {temp_path}.")
            return len(self.packet_cache)
        except Exception as exc:
            self._engine_log("WARN", f"Failed to load temporary packet cache from {temp_path}: {exc}")
            return 0

    def _dispatch_packet_to_elements(self, pkt: dict, catch_exceptions: bool = False):
        """Fast pre-filtered packet dispatch across layout elements."""
        node_id = pkt.get("_node_id_")
        has_pkt_ctc = bool(pkt.get("_ctc_id_") or pkt.get("ctc_id"))
        has_src_id = any(
            isinstance(k, str) and k.strip().lower() in ("_src_id_", "_src_id", "src_id", "_srcid_") and v not in (None, "")
            for k, v in pkt.items()
        )
        for el in self.elements.values():
            if not has_pkt_ctc and not getattr(el, "ctc_rules", ""):
                et = getattr(el, "elem_type", "")
                if et not in ("base", "ptc_datetime", "date_time", "datetime"):
                    wid = getattr(el, "wiu_id", None)
                    rid = getattr(el, "radio_id", None)
                    rid2 = getattr(el, "radioid", None)
                    if wid is None and rid is None and rid2 is None:
                        continue
                    if not has_src_id:
                        if not (
                            (wid is not None and BaseElement._wiu_matches(wid, node_id))
                            or (rid is not None and BaseElement._wiu_matches(rid, node_id))
                            or (rid2 is not None and BaseElement._wiu_matches(rid2, node_id))
                        ):
                            continue
            if catch_exceptions:
                try:
                    el.process_packet(pkt, self)
                except Exception:
                    pass
            else:
                el.process_packet(pkt, self)

    def apply_cached_packets(self):
        """Replay cached packets to populate last WIU packets, address table, scroller, elements, and system logic."""
        for pkt in self.packet_cache:
            if not isinstance(pkt, dict):
                continue
            if not pkt.get("is_valid", True):
                if "raw_hex" in pkt:
                    self.scroller.add_invalid_packet(
                        pkt.get("raw_hex"),
                        meta={"rssi": pkt.get("rssi", -75.0), "channel": pkt.get("channel", 0)}
                    )
                continue

            wiu_node_id = pkt.get("_node_id_")
            if wiu_node_id is not None:
                try:
                    self.last_packets_by_wiu[int(wiu_node_id)] = pkt
                except (ValueError, TypeError):
                    pass

            self.address_table.check_and_update_sequence(pkt, self.rrdata)
            self.scroller.add_packet(pkt)
            self.train_scroller.check_packet(pkt)
            self._dispatch_packet_to_elements(pkt, catch_exceptions=True)

        if self.packet_cache:
            last_pkt = self.packet_cache[-1]
            ts = last_pkt.get("timestamp", time.time())
            self._evaluate_system_logic(ts)
            self._last_transmitted_signature = self._compute_logical_signature()

    def save_packet_history(self, filepath: Optional[str] = None) -> str:
        """Command to save the current packet cache history to a file.
        If filepath is None or empty, generates a timestamped file associated with
        the current layout in self.config_dir.
        Returns the absolute filepath written.
        """
        if not filepath:
            safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', os.path.basename(self.layout_filename or "layout"))
            timestamp_str = time.strftime("%Y%m%d_%H%M%S")
            filename = f"packet_history_{safe_name}_{timestamp_str}.json"
            filepath = os.path.join(self.config_dir or os.getcwd(), filename)

        abs_path = os.path.abspath(filepath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        cache_list = [_make_json_serializable(p) for p in self.packet_cache]
        payload = {
            "version": self.version,
            "layout_filename": self.layout_filename,
            "layout_filepath": os.path.abspath(self.layout_filepath) if self.layout_filepath else "",
            "saved_at": time.time(),
            "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "packet_cache_size": self.packet_cache_size,
            "packet_count": len(cache_list),
            "packets": cache_list,
        }

        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        return abs_path

    def toggle_raw_packet_logging(self, enabled: Optional[bool] = None, filepath: Optional[str] = None) -> bool:
        """Command to toggle or set raw packet logging on/off (default off).
        When enabled, raw hex strings of incoming packets with channels and signal strengths
        are saved to a temporary log file independent of history or current layout.
        """
        if filepath:
            self.raw_log_filepath = filepath
        if enabled is not None:
            self.raw_packet_logging = bool(enabled)
        else:
            self.raw_packet_logging = not self.raw_packet_logging
        return self.raw_packet_logging

    def _should_save_packet(self, packet: Any) -> bool:
        """Check if packet contains an active '_save_' element in its dictionary."""
        if not isinstance(packet, dict) or "_save_" not in packet:
            return False
        val = packet.get("_save_")
        if val in (False, None, 0, "0", "false", "False", "no", "None"):
            return False
        return bool(val)

    def _extract_packet_datetime_digits(self, packet: dict) -> str:
        """Extract date/time state from packet fields or timestamp as a string of
        digits running together without any punctuation (e.g. YYYYMMDDHHMMSS).
        """
        # 1. Check if packet contains date/time components (e.g. from PTC Date/Time packet)
        year_val = packet.get("year2", packet.get("year", packet.get("yr")))
        mon_val = packet.get("mon", packet.get("month", packet.get("mo")))
        day_val = packet.get("day")
        hh_val = packet.get("hh", packet.get("hour", packet.get("hr")))
        mm_val = packet.get("mm", packet.get("minute", packet.get("min")))
        ss_val = packet.get("ss", packet.get("second", packet.get("sec")))

        if all(v is not None for v in (year_val, mon_val, day_val, hh_val, mm_val, ss_val)):
            try:
                y = int(year_val)
                if y < 100:
                    y += 2000
                m = int(mon_val)
                d = int(day_val)
                h = int(hh_val)
                mi = int(mm_val)
                s = int(ss_val)
                dt_obj = datetime.datetime(y, m, d, h, mi, s)
                return dt_obj.strftime("%Y%m%d%H%M%S")
            except Exception:
                pass

        # 2. Check if packet has explicit numeric timestamp (e.g. float or int epoch seconds)
        ts = packet.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt_obj = datetime.datetime.fromtimestamp(float(ts))
                return dt_obj.strftime("%Y%m%d%H%M%S")
            except Exception:
                pass

        # 3. Check for datetime string or time field containing digits
        for key in ("datetime", "date_time", "time", "date"):
            val = packet.get(key)
            if isinstance(val, str):
                digits_only = "".join(c for c in val if c.isdigit())
                if len(digits_only) >= 12:
                    return digits_only[:14]

        # 4. Fallback to current datetime
        return datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    def save_saved_packet(
        self,
        packet: dict,
        raw_hex: Optional[str] = None,
        extension: Optional[str] = None,
    ) -> Optional[str]:
        """Save a decoded packet containing an element '_save_' in its dictionary
        to temporary storage (self.saved_packets_dir).
        Filename format has date/time state with all digits running together without punctuation.
        Ensures raw_hex string is included in the saved file.
        Saves one packet per file.
        Returns the absolute filepath written, or None if packet is not saved.
        """
        if not self._should_save_packet(packet):
            return None

        # Ensure raw_hex is included in the saved packet dictionary
        pkt_to_save = dict(packet)
        if raw_hex:
            pkt_to_save["raw_hex"] = raw_hex
            pkt_to_save.setdefault("_raw_hex_", raw_hex)
        elif not pkt_to_save.get("raw_hex"):
            rh = (
                pkt_to_save.get("_raw_hex_")
                or pkt_to_save.get("hex")
                or getattr(self, "last_raw_hex", "")
            )
            pkt_to_save["raw_hex"] = rh
            pkt_to_save.setdefault("_raw_hex_", rh)

        digits = self._extract_packet_datetime_digits(pkt_to_save)
        target_dir = getattr(self, "saved_packets_dir", tempfile.gettempdir())
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            pass

        ext = extension if extension is not None else getattr(self, "saved_packet_extension", "")

        # Format filename: all digits running together without any punctuation
        filename = f"{digits}{ext}"
        filepath = os.path.join(target_dir, filename)

        # Ensure one packet per file by resolving any name collisions with incrementing digits
        counter = 1
        while os.path.exists(filepath):
            filename = f"{digits}{counter:02d}{ext}"
            filepath = os.path.join(target_dir, filename)
            counter += 1

        try:
            serializable = _make_json_serializable(pkt_to_save)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)

            self.last_saved_packet_file = filepath
            self.saved_packets_count = getattr(self, "saved_packets_count", 0) + 1
            if not hasattr(self, "saved_packets_files") or not isinstance(self.saved_packets_files, list):
                self.saved_packets_files = []
            self.saved_packets_files.append(filepath)
            return filepath
        except Exception as exc:
            self._engine_log("WARN", f"Failed saving packet with _save_ to {filepath}: {exc}")
            return None

    # Aliases for saving history command
    save_history = save_packet_history
    export_packet_history = save_packet_history

    def _on_process_exit(self):
        """Automatically save packet cache to temporary storage when process exits."""
        try:
            if getattr(self, "save_packet_cache_enabled", True):
                self.save_packet_cache_to_temp()
        except Exception:
            pass

    def stop_layout_server(self):
        self._server_running = False
        with self._tcp_stream_lock:
            for s in self._tcp_stream_clients:
                try:
                    s.close()
                except OSError:
                    pass
            self._tcp_stream_clients.clear()
        if hasattr(self, "_server_socket") and self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass

    def shutdown(self):
        """Gracefully stop engine services and persist packet cache to layout's temporary storage."""
        self._running = False
        if getattr(self, "save_packet_cache_enabled", True):
            self.save_packet_cache_to_temp()
        self.stop_layout_server()
        try:
            self.sock.close()
        except OSError:
            pass

    stop = shutdown
    close = shutdown

    def get_render_state(self) -> dict:
        self._sync_system_panels_from_elements()
        return {
            "version": self.version,
            "layout_filename": self.layout_filename,
            "thanks_text": self.thanks_text,
            "rrdata": self.rrdata,
            "timestamp": time.time(),
            "tx_frames": self.transmitted_frames_count,
            "suppressed_frames": self.suppressed_frames_count,
            "dedup_ttl": getattr(self, "dedup_ttl", 0.4),
            "packet_cache_count": len(self.packet_cache),
            "packet_cache_size": self.packet_cache_size,
            "last_raw_hex": getattr(self, "last_raw_hex", ""),
            "raw_packet_logging": getattr(self, "raw_packet_logging", False),
            "raw_log_filepath": getattr(self, "raw_log_filepath", ""),
            "raw_log_count": getattr(self, "raw_log_count", 0),
            "saved_packets_count": getattr(self, "saved_packets_count", 0),
            "last_saved_packet_file": getattr(self, "last_saved_packet_file", None),
            "collisions": list(self.active_collisions),
            "elements": {eid: el.to_state_dict() for eid, el in self.elements.items()},
            "packet_scroller": self.scroller.to_state_dict() if getattr(self, "scroller_enabled", False) else None,
            "address_table": self.address_table.to_state_dict(
                {int(getattr(el, "radio_id", None) or el.wiu_id) for el in self.elements.values() if (getattr(el, "wiu_id", None) is not None or getattr(el, "radio_id", None) is not None)},
                self.rrdata,
                layout_wiu_names=self.get_all_layout_wiu_names(),
                element_avg_rssi={
                    int(getattr(el, "radio_id", None) or el.wiu_id): getattr(el, "rssi_avg", None)
                    for el in self.elements.values()
                    if (getattr(el, "wiu_id", None) is not None or getattr(el, "radio_id", None) is not None) and getattr(el, "rssi_avg", None) is not None
                },
            ) if getattr(self, "address_table_enabled", False) else None,
            "train_scroller": self.train_scroller.to_state_dict() if getattr(self, "train_scroller_enabled", False) else None,
            "chat": self.chat_panel.to_state_dict(active_identity=self.get_active_chat_identity()) if (getattr(self, "chat_enabled", False) or len(self.chat_panel.messages) > 0) else None,
            "chat_identity": self.get_active_chat_identity(),
        }

    def broadcast_state(self, state: dict):
        raw_json = json.dumps(state).encode("utf-8")
        # Compress payload with fastest lossless compression (lz4 if installed, otherwise zlib level 1)
        if lz4_frame is not None:
            compressed = lz4_frame.compress(raw_json)
            magic = CHUNK_MAGIC_LZ4
        else:
            compressed = zlib.compress(raw_json, level=1)
            magic = CHUNK_MAGIC_ZLIB

        # 1. Stream over persistent TCP state stream to connected remote/local viewers (zero latency, zero packet loss, bypasses firewalls/Wi-Fi multicast drop)
        with self._tcp_stream_lock:
            if self._tcp_stream_clients:
                tcp_frame = struct.pack("!I4s", len(compressed), magic) + compressed
                dead = []
                for client_sock in self._tcp_stream_clients:
                    try:
                        client_sock.settimeout(0.05)
                        client_sock.sendall(tcp_frame)
                    except Exception:
                        dead.append(client_sock)
                for d in dead:
                    if d in self._tcp_stream_clients:
                        self._tcp_stream_clients.remove(d)
                    try:
                        d.close()
                    except Exception:
                        pass

        self._mcast_transfer_id = (self._mcast_transfer_id + 1) & 0xFFFFFFFF
        transfer_id = self._mcast_transfer_id
        total_len = len(compressed)

        # Break payload into chunks if exceeding MAX_UDP_CHUNK_SIZE
        if total_len <= MAX_UDP_CHUNK_SIZE:
            header = struct.pack(CHUNK_HEADER_FMT, magic, transfer_id, total_len, 0, 1)
            datagrams = [header + compressed]
        else:
            total_chunks = (total_len + MAX_UDP_CHUNK_SIZE - 1) // MAX_UDP_CHUNK_SIZE
            datagrams = []
            for idx in range(total_chunks):
                chunk_slice = compressed[idx * MAX_UDP_CHUNK_SIZE : (idx + 1) * MAX_UDP_CHUNK_SIZE]
                header = struct.pack(CHUNK_HEADER_FMT, magic, transfer_id, total_len, idx, total_chunks)
                datagrams.append(header + chunk_slice)

        now = time.time()
        with self._clients_lock:
            stale = [ip for ip, ts in self._client_subscribers.items() if now - ts > 900.0]
            for ip in stale:
                del self._client_subscribers[ip]
            active_clients = set(self._client_subscribers.keys())
        active_clients.update(self.unicast_clients)
        active_clients.discard("127.0.0.1")
        active_clients.discard("localhost")

        egress_ifs = [ip for ip in self.get_egress_interfaces() if not ip.startswith("169.254.")]

        sock_lock = getattr(self, "_sock_lock", None)
        if sock_lock is None:
            self._sock_lock = threading.Lock()
            sock_lock = self._sock_lock

        with sock_lock:
            if not getattr(self, "sock", None):
                return
            try:
                self.sock.setblocking(False)
            except OSError:
                pass

            # 1. Multicast out each physical / active network interface
            if egress_ifs:
                for if_ip in egress_ifs:
                    try:
                        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(if_ip))
                        for dgram in datagrams:
                            self.sock.sendto(dgram, (self.mcast_group, self.mcast_port))
                    except (OSError, BlockingIOError):
                        pass
            else:
                # Fall back to default route if no physical egress interfaces discovered
                for dgram in datagrams:
                    try:
                        self.sock.sendto(dgram, (self.mcast_group, self.mcast_port))
                    except (OSError, BlockingIOError):
                        pass

            # 2. Always deliver to localhost loopback if mcast group is not loopback
            if self.mcast_group not in ("127.0.0.1", "localhost"):
                for dgram in datagrams:
                    try:
                        self.sock.sendto(dgram, ("127.0.0.1", self.mcast_port))
                    except (OSError, BlockingIOError):
                        pass

            # 3. Direct unicast to registered / configured remote Viewers
            for client_ip in active_clients:
                for dgram in datagrams:
                    try:
                        self.sock.sendto(dgram, (client_ip, self.mcast_port))
                    except (OSError, BlockingIOError):
                        pass


def _console_command_loop(engine: "DispatcherEngine"):
    print("Console command loop active: Type 'save [filepath]' to save packet history, 'status' for cache info, 'quit' to exit.")
    while getattr(engine, "_running", False):
        try:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip()
            if not cmd:
                continue
            parts = cmd.split(maxsplit=1)
            action = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else None

            if action in ("delete", "del", "remove", "kill"):
                if arg:
                    res = engine.delete_train(arg)
                    print(f"[OK] Delete train: {res}")
                else:
                    print("[WARN] Usage: delete <train_key>")
            elif action in ("assign", "train", "assign_train"):
                if arg:
                    parts_sub = arg.split(maxsplit=2)
                    tkey = parts_sub[0]
                    aid = parts_sub[1] if len(parts_sub) > 1 else None
                    aname = parts_sub[2] if len(parts_sub) > 2 else None
                    res = engine.assign_train_info({"train_key": tkey, "assigned_id": aid, "assigned_name": aname})
                    print(f"[OK] Assigned train info: {res}")
            elif action in ("save", "save_history", "save_packet_history", "history"):
                out_path = engine.save_packet_history(arg)
                print(f"[OK] Saved {len(engine.packet_cache)} cached packets to '{out_path}'.")
            elif action in ("log", "raw_log", "toggle_log", "raw_logging"):
                toggled = engine.toggle_raw_packet_logging()
                st = "ENABLED" if toggled else "DISABLED"
                print(f"[OK] Raw packet logging {st} -> '{engine.raw_log_filepath}' (logged {engine.raw_log_count} pkts).")
            elif action in ("status", "cache", "info"):
                log_st = "ON" if engine.raw_packet_logging else "OFF"
                print(f"[INFO] Layout: '{engine.layout_filename}' | Cached: {len(engine.packet_cache)}/{engine.packet_cache_size} pkts | Dedup TTL: {engine.dedup_ttl:.2f}s | RawLog: {log_st} ({engine.raw_log_count} pkts) | Temp Cache: '{engine.get_layout_temp_cache_path()}'")
            elif action in ("dedup", "dup", "ttl", "set_dedup"):
                if arg:
                    try:
                        new_ttl = max(0.0, float(arg))
                        engine.set_dedup_ttl(new_ttl)
                        print(f"[OK] Dedup window TTL set to {new_ttl:.2f}s.")
                    except ValueError:
                        print(f"[ERROR] Invalid TTL '{arg}': must be a number in seconds.")
                else:
                    print(f"[INFO] Current Dedup Window TTL: {engine.dedup_ttl:.2f}s (use 'dedup <sec>' to change).")
            elif action in ("quit", "exit", "q"):
                engine.shutdown()
                break
            elif action in ("reload", "r"):
                msg = engine.reload_all_lookups()
                print(f"[OK] Reloaded: {msg}")
            elif action in ("help", "?"):
                print("Commands: dedup [sec] | save [filepath] | status | reload | quit")
            else:
                print(f"Unknown command: '{cmd}'. Type 'help' for available commands.")
        except Exception:
            break


def main():
    ap = argparse.ArgumentParser(
        description=f"Railroad Dispatcher Logic Engine ({VERSION})",
        epilog=FIREWALL_HELP_NOTICE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--layout", default="complex_layout.json", help="Layout JSON file to load and serve")
    ap.add_argument("--config", default=None, help="Optional host configuration JSON file")
    ap.add_argument("--mcast-group", default="224.1.1.1", help="Multicast IP group")
    ap.add_argument("--mcast-port", type=int, default=5007, help="Multicast UDP port")
    ap.add_argument("--layout-port", type=int, default=5008, help="TCP port to serve layout to remote renderers")
    ap.add_argument("--packet-cache-size", type=int, default=None, help="RAM packet cache size (default: 1000 or config)")
    ap.add_argument("--dedup-ttl", "--dedup-window", "--dedup-sec", dest="dedup_ttl", type=float, default=0.4, help="Duplicate packet deduplication TTL in seconds (default: 0.4s)")
    ap.add_argument("--client", "--unicast-client", dest="unicast_clients", action="append", default=[], help="Remote Viewer IP to stream state updates via unicast UDP (can specify multiple times)")
    args = ap.parse_args()

    cfg = {}
    if args.config and os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f: cfg = json.load(f)
        cfg["config_path"] = args.config

    engine = DispatcherEngine(
        config=cfg,
        layout_filepath=args.layout,
        mcast_group=args.mcast_group,
        mcast_port=args.mcast_port,
        layout_server_port=args.layout_port,
        packet_cache_size=args.packet_cache_size,
        dedup_ttl=args.dedup_ttl,
        unicast_clients=args.unicast_clients,
    )
    print(f"[{VERSION}] DispatcherEngine serving layout '{engine.layout_filename}' on TCP port {engine.layout_server_port} and Multicast {engine.mcast_group}:{engine.mcast_port}.")
    print(f"[{VERSION}] RAM packet cache: {len(engine.packet_cache)}/{engine.packet_cache_size} packets. Temp storage: '{engine.get_layout_temp_cache_path()}'.")

    if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        cmd_th = threading.Thread(target=_console_command_loop, args=(engine,), daemon=True)
        cmd_th.start()

    try:
        while engine._running:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nDispatcherEngine stopping...")
    finally:
        engine.shutdown()
        print("DispatcherEngine stopped.")

if __name__ == "__main__":
    main()