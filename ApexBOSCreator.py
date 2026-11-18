#!/usr/bin/env python3
# Copyright by AB
# Licensed under the MIT License.
"""
ApexBOSCreator.py - Railroad Dispatcher Layout Editor & Verification Studio (v26.117.2)
Copyright by AB. Licensed under the MIT License.

Features:
  - Supports label_pos ('top', 'bottom', 'left', 'right') editing for all elements.
  - Adds global HUD toggles: Lbls(On/Off) and InfTrns(On/Off).
  - Train badges automatically avoid overlapping labels by anchoring to the opposite side.
  - Interactive track editing, 1-cell gapless welding, 45-degree routing, and Undo/Redo.
"""
import os
import sys

# --- LINUX NVIDIA / NOUVEAU & XORG GLAMOR CRASH FIX ---
# MUST BE SET BEFORE PYGAME IS IMPORTED!
# 1. Forces SDL2/Mesa in the Python processes to use CPU software rendering.
# 2. Disables X11 MIT-SHM shared-memory DMA transfers (SDL_VIDEO_X11_XSHM=0) so Xorg's
#    Glamor/Nouveau Copy Engine (CE2) doesn't hit a Page Table Entry (PTE) fault when
#    two processes push frames simultaneously.
# 3. Disables X11 compositor bypass so Cinnamon/Muffin doesn't thrash VRAM page tables.
if sys.platform.startswith('linux'):
    os.environ['SDL_RENDER_DRIVER'] = 'software'
    os.environ['SDL_FRAMEBUFFER_ACCELERATION'] = '0'
    os.environ['LIBGL_ALWAYS_SOFTWARE'] = '1'
    os.environ['GALLIUM_DRIVER'] = 'llvmpipe'
    os.environ['SDL_VIDEO_X11_XSHM'] = '0'
    os.environ['SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR'] = '0'

import argparse
import copy
try:
    import dataclasses
except ImportError:
    raise ImportError("Python 3.6 requires the 'dataclasses' package. Install it via: pip install dataclasses")
import json
import math
import re
import sqlite3
import subprocess
import tempfile
import time
import threading
import queue
import urllib.request
from collections import deque
from typing import Dict, List, Optional, Tuple, Any, Set, Callable

import pygame
import mosfet_client

from dispatcher_engine import (
    OPPOSITE_DIR,
    DispatcherEngine,
    format_wiu_display_name,
    BaseElement,
    TrackSegment,
    Turnout,
    Signal,
    Crossing,
    BaseRadio,
    TrackJump,
    DisplayElement,
    PTCDateTime,
)

from train_logic import (
    TrainTracker,
    TrainScroller,
    TrainInferenceManager,
    get_signal_gps,
    get_element_gps_any,
)

from version import VERSION, __version__
CREATOR_VERSION = VERSION

LOGICAL_GRID_SCALE = 24.0
WIU_CACHE_FILE = ".wiu_address_cache.json"
MAP_CACHE_DIR = ".map_tile_cache"
FRAGS_DIR = os.path.join(os.getcwd(), "frags")
MAX_UNDO_STEPS = 50

# =====================================================================
# COLOR PALETTE
# =====================================================================

COLOR_BG = (14, 18, 24)
COLOR_GRID_MINOR = (24, 30, 40)
COLOR_GRID_MAJOR = (36, 45, 58)
COLOR_TRACK_WHITE = (185, 195, 210)
COLOR_TRACK_GREEN = (40, 230, 95)
DIR_SCREEN_VECTORS: Dict[str, Tuple[float, float]] = {
    "E": (1.0, 0.0),
    "W": (-1.0, 0.0),
    "N": (0.0, -1.0),
    "S": (0.0, 1.0),
    "NE": (0.70710678, -0.70710678),
    "NW": (-0.70710678, -0.70710678),
    "SE": (0.70710678, 0.70710678),
    "SW": (-0.70710678, 0.70710678),
}
COLOR_TRACK_RED = (245, 50, 50)
COLOR_TRACK_INFERRED = (255, 140, 45)
COLOR_TRACK_FLASH_DARK_RED = (110, 15, 20)
COLOR_TURNOUT_TIE = (100, 112, 130)
COLOR_SIGNAL_MAST = (165, 175, 190)
COLOR_HANDLE = (0, 215, 255)
COLOR_HANDLE_ACTIVE = (255, 210, 50)

LAMP_COLORS: Dict[str, Tuple[int, int, int]] = {
    "GREEN": (35, 235, 90),
    "YELLOW": (255, 215, 35),
    "RED": (250, 50, 50),
    "MAGENTA": (235, 55, 235),
    "BLACK": (20, 22, 28),
    "GRAY": (95, 102, 115),
    "DARK": (55, 60, 70),
}

DARK_LAMP_COLORS: Dict[str, Tuple[int, int, int]] = {
    "GREEN": (14, 95, 38),
    "YELLOW": (118, 95, 16),
    "RED": (108, 22, 22),
    "MAGENTA": (98, 24, 98),
    "BLACK": (15, 16, 20),
    "GRAY": (52, 56, 64),
    "DARK": (35, 38, 44),
}

COLOR_CROSSING_OPEN = (70, 210, 255)
COLOR_CROSSING_DOWN = (255, 50, 80)
COLOR_BASE_RADIO = (155, 110, 255)
COLOR_LANDMARK = (225, 195, 90)
COLOR_TEXT = (225, 235, 248)
COLOR_TEXT_DIM = (130, 142, 160)
COLOR_HUD_BG = (20, 26, 36, 238)
COLOR_HUD_BORDER = (65, 85, 115)
COLOR_EDIT_ACCENT = (255, 185, 40)
COLOR_SELECT_RING = (0, 240, 255)
COLOR_ERROR_RED = (255, 75, 75)
COLOR_OK_GREEN = (50, 225, 110)
COLOR_TEXT_SEL_BG = (45, 105, 195)

ELEMENT_TYPES = [
    "track",
    "turnout",
    "signal",
    "crossing",
    "base",
    "station",
    "bridge",
    "diamond",
    "label",
    "track_jump",
    "packet_scroller",
    "address_table",
    "train_scroller",
    "chat",
    "ptc_datetime",
]

SHORT_ID_PREFIX = {
    "track": "T",
    "turnout": "W",
    "signal": "S",
    "crossing": "X",
    "base": "B",
    "station": "ST",
    "bridge": "BR",
    "diamond": "D",
    "label": "L",
    "track_jump": "J",
    "packet_scroller": "PL",
    "address_table": "WL",
    "train_scroller": "TL",
    "chat": "CH",
    "ptc_datetime": "DT",
}

SYSTEM_LIST_TYPES = ("packet_scroller", "address_table", "train_scroller", "chat")
COMPASS_DIRS = ["E", "W", "N", "S", "NE", "NW", "SE", "SW"]
COMPATIBLE_DIRS: Dict[str, Tuple[str, ...]] = {
    "E": ("NE", "SE"),
    "W": ("NW", "SW"),
    "N": ("NE", "NW"),
    "S": ("SE", "SW"),
    "NE": ("E", "N"),
    "SW": ("W", "S"),
    "NW": ("W", "N"),
    "SE": ("E", "S"),
}
TRACK_ORIENTATIONS = ["E-W", "N-S", "NE-SW", "NW-SE"]
SIGNAL_POSITIONS = ["top", "middle", "bottom"]
LABEL_POSITIONS = ["top", "bottom", "left", "right"]
SIM_ASPECT_CYCLE = [
    (24, "green"),
    (2, "yellow"),
    (12, "flashing_yellow"),
    (30, "red"),
    (0, "magenta"),
    (15, "black"),
]


# =====================================================================
# 1. SQLITE WIUS LOADER & WIU CACHE
# =====================================================================

def _parse_wiu_gps(gps_val: Any = None, lat_val: Any = None, lon_val: Any = None) -> Optional[Tuple[float, float]]:
    lat = None
    lon = None
    if isinstance(gps_val, (list, tuple)) and len(gps_val) >= 2:
        lat, lon = gps_val[0], gps_val[1]
    elif isinstance(gps_val, dict):
        lat = gps_val.get("lat") if gps_val.get("lat") is not None else gps_val.get("latitude")
        lon = gps_val.get("lon") if gps_val.get("lon") is not None else gps_val.get("longitude")
    elif isinstance(gps_val, str) and gps_val.strip():
        s = gps_val.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    lat, lon = parsed[0], parsed[1]
            except Exception:
                pass
        if lat is None or lon is None:
            parts = [p.strip() for p in re.split(r"[,;\s]+", s.strip("()[] ")) if p.strip()]
            if len(parts) >= 2:
                lat, lon = parts[0], parts[1]

    if lat is None or lon is None:
        if lat_val is not None and lon_val is not None:
            lat, lon = lat_val, lon_val

    if lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    if lat_f == 0.0 and lon_f == 0.0:
        return None
    return (lat_f, lon_f)


def _parse_ctc_csv(raw_val: Any) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()

    def _add(item: str):
        tok = str(item).strip()
        if not tok:
            return
        k = tok.lower()
        if k not in seen:
            seen.add(k)
            out.append(tok)

    if raw_val is None or isinstance(raw_val, bool):
        return out
    if isinstance(raw_val, (list, tuple, set)):
        for elem in raw_val:
            if elem is not None and not isinstance(elem, bool):
                for part in re.split(r"[,;]+", str(elem)):
                    _add(part)
    else:
        for part in re.split(r"[,;]+", str(raw_val)):
            _add(part)
    return out


def _normalize_wiu_key(wiu_id: Any) -> str:
    if wiu_id is None or isinstance(wiu_id, bool):
        return ""
    s = str(wiu_id).strip()
    if not s:
        return ""
    if re.fullmatch(r"-?\d+", s):
        try:
            return str(int(s))
        except Exception:
            pass
    return s


def _wiu_sort_scalar(wiu_str: str) -> Tuple[int, int, str]:
    s = str(wiu_str).strip()
    if re.fullmatch(r"-?\d+", s):
        return (0, int(s), s)
    return (1, 0, s)


def _haversine_miles_coords(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_mi = 3958.7613
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2.0) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dl / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r_mi * c


class WIUAddressCache:
    def __init__(self, cache_filepath: str = WIU_CACHE_FILE, load_existing_cache: bool = True):
        self.cache_filepath = cache_filepath
        self.wiu_records: Dict[str, dict] = {}
        self.last_banc_loaded_ok: bool = False
        self.last_banc_error: str = ""
        if load_existing_cache:
            self.load_cache()

    def load_cache(self):
        if os.path.exists(self.cache_filepath):
            try:
                with open(self.cache_filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.wiu_records = data.get("wius", {})
            except Exception as exc:
                print(f"[WARN] Could not load WIU cache '{self.cache_filepath}': {exc}")

    def save_cache(self):
        data = {
            "version": CREATOR_VERSION,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wius": self.wiu_records,
        }
        try:
            with open(self.cache_filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            print(f"[WARN] Could not save WIU cache '{self.cache_filepath}': {exc}")

    def register_wiu(
        self,
        wiu_id: Any,
        base_bits: int = 16,
        channel: int = 1,
        name: Optional[str] = None,
        gps: Any = None,
        has_ctc: Optional[bool] = None,
        ctc_addresses: Any = None,
    ):
        key = _normalize_wiu_key(wiu_id)
        if not key:
            return
        try:
            wid_stored: Any = int(key) if re.fullmatch(r"-?\d+", key) else key
        except Exception:
            wid_stored = key

        parsed_gps = _parse_wiu_gps(gps)
        parsed_ctcs = _parse_ctc_csv(ctc_addresses)
        norm_name = str(name).strip() if name is not None else ""

        if key not in self.wiu_records:
            self.wiu_records[key] = {
                "wiu_id": wid_stored,
                "max_base_bits": int(base_bits),
                "channels": [int(channel)],
                "seen_count": 1,
                "name": norm_name,
                "gps": [parsed_gps[0], parsed_gps[1]] if parsed_gps else None,
                "ctc_addresses": parsed_ctcs,
                "has_ctc": bool(parsed_ctcs) or bool(has_ctc),
            }
        else:
            rec = self.wiu_records[key]
            rec["max_base_bits"] = max(int(rec.get("max_base_bits", 0)), int(base_bits))
            if int(channel) not in rec.get("channels", []):
                rec.setdefault("channels", []).append(int(channel))
            rec["seen_count"] = int(rec.get("seen_count", 0)) + 1
            if norm_name and not str(rec.get("name", "")).strip():
                rec["name"] = norm_name
            if parsed_gps and _parse_wiu_gps(rec.get("gps")) is None:
                rec["gps"] = [parsed_gps[0], parsed_gps[1]]
            if parsed_ctcs:
                existing_ctcs = _parse_ctc_csv(rec.get("ctc_addresses"))
                rec["ctc_addresses"] = _parse_ctc_csv(existing_ctcs + parsed_ctcs)
            if parsed_ctcs or has_ctc:
                rec["has_ctc"] = bool(rec.get("has_ctc")) or bool(parsed_ctcs) or bool(has_ctc)

    def _ingest_wiu_row_dict(self, row_dict: dict) -> bool:
        """Ingests or merges a single WIU metadata dict (from SQLite or Banc GET_WIUS) into wiu_records."""
        if not isinstance(row_dict, dict):
            return False
        lower_map = {str(k).lower(): v for k, v in row_dict.items()}

        def _get(col_key: str, *alts: str) -> Any:
            for ck in (col_key,) + alts:
                kl = ck.lower()
                if kl in lower_map:
                    return lower_map[kl]
            return None

        raw_wid = _get("wiu_id", "id")
        key = _normalize_wiu_key(raw_wid)
        if not key:
            return False

        w_name = str(_get("name", "wiu_name", "label") or "").strip()
        gps_tup = _parse_wiu_gps(
            _get("gps"),
            _get("lat", "latitude"),
            _get("lon", "longitude"),
        )
        raw_ctcs = _get("ctc_addresses", "ctc_id", "ctc")
        ctc_list = _parse_ctc_csv(raw_ctcs)
        raw_has_ctc = _get("has_ctc")
        has_ctc = bool(ctc_list) or (
            bool(int(raw_has_ctc)) if isinstance(raw_has_ctc, (int, float)) else (
                str(raw_has_ctc).strip().lower() in ("1", "true", "yes", "on") if raw_has_ctc is not None else False
            )
        )

        try:
            wid_stored: Any = int(key) if re.fullmatch(r"-?\d+", key) else key
        except Exception:
            wid_stored = key

        submitter = str(_get("submitter") or "")
        first_submitted = str(_get("first_submitted") or "")
        last_updated = str(_get("last_updated") or "")

        existing = self.wiu_records.get(key)
        if existing is None:
            self.wiu_records[key] = {
                "wiu_id": wid_stored,
                "max_base_bits": 16,
                "channels": [1],
                "seen_count": 1,
                "name": w_name,
                "gps": [gps_tup[0], gps_tup[1]] if gps_tup else None,
                "ctc_addresses": ctc_list,
                "has_ctc": has_ctc,
                "submitter": submitter,
                "first_submitted": first_submitted,
                "last_updated": last_updated,
            }
        else:
            if w_name:
                existing["name"] = w_name
            if gps_tup:
                existing["gps"] = [gps_tup[0], gps_tup[1]]
            ex_ctcs = _parse_ctc_csv(existing.get("ctc_addresses"))
            merged_ctcs = _parse_ctc_csv(ex_ctcs + ctc_list)
            existing["ctc_addresses"] = merged_ctcs
            existing["has_ctc"] = bool(existing.get("has_ctc")) or has_ctc or bool(merged_ctcs)
            if submitter:
                existing["submitter"] = submitter
            if first_submitted and not existing.get("first_submitted"):
                existing["first_submitted"] = first_submitted
            if last_updated:
                existing["last_updated"] = last_updated
        return True

    def load_from_wius_table(
        self,
        db_filepath: str,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Reads suggested WIUs and metadata directly from the SQLite 'wius' table instead of scanning 'packets'."""
        if not os.path.exists(db_filepath):
            if progress_cb:
                progress_cb(f"SQLite file '{db_filepath}' not found.")
            return 0

        loaded_count = 0
        conn = None
        try:
            if progress_cb:
                progress_cb(f"Reading WIUs from 'wius' table in '{os.path.basename(db_filepath)}'. Please wait...")
            conn = sqlite3.connect(db_filepath)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND LOWER(name)='wius'")
            tbl_row = cur.fetchone()
            if not tbl_row:
                if progress_cb:
                    progress_cb(f"No 'wius' table found in '{os.path.basename(db_filepath)}'.")
                return 0

            tbl_name = tbl_row[0]
            cur.execute(f"PRAGMA table_info({tbl_name})")
            col_names = [c[1] for c in cur.fetchall()]

            if progress_cb:
                progress_cb(f"Loading entries from '{tbl_name}' table...")

            cur.execute(f"SELECT {', '.join(col_names)} FROM {tbl_name}")
            rows = cur.fetchall()
            conn.close()
            conn = None

            for row in rows:
                row_dict = {col_names[i]: row[i] for i in range(min(len(col_names), len(row)))}
                if self._ingest_wiu_row_dict(row_dict):
                    loaded_count += 1
        except Exception as exc:
            print(f"[WARN] Error reading 'wius' table from '{db_filepath}': {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if loaded_count > 0:
            self.save_cache()
        if progress_cb:
            progress_cb(f"Completed! Loaded {loaded_count} WIU record(s) from 'wius' table.")
        return loaded_count

    def load_from_banc(
        self,
        banc_client: Any,
        progress_cb: Optional[Callable[[str], None]] = None,
        timeout: float = 3.0,
        replace_existing: bool = True,
    ) -> int:
        """
        Loads suggested WIUs and metadata directly from a configured & valid Banc Cloud service.
        Returns loaded record count (>= 0) on success, or -1 if Banc is unreachable/invalid.
        """
        self.last_banc_loaded_ok = False
        self.last_banc_error = ""
        if not mosfet_client.is_valid_banc_client(banc_client):
            self.last_banc_error = "Banc client is not configured or invalid"
            return -1

        loaded_count = 0
        try:
            if progress_cb:
                progress_cb("Querying 'wius' table from Banc Cloud. Please wait...")
            rows = banc_client.get_wius_sync(timeout=timeout)
            if not isinstance(rows, list):
                raise ValueError("Invalid response from Banc GET_WIUS: expected list")
            if replace_existing:
                self.wiu_records.clear()
            for row in rows:
                if self._ingest_wiu_row_dict(row):
                    loaded_count += 1
            self.last_banc_loaded_ok = True
            if loaded_count > 0 or replace_existing:
                self.save_cache()
            if progress_cb:
                progress_cb(f"Completed! Loaded {loaded_count} WIU record(s) from Banc Cloud 'wius' table.")
            return loaded_count
        except Exception as exc:
            self.last_banc_loaded_ok = False
            self.last_banc_error = str(exc)
            print(f"[WARN] Could not load WIUs from Banc Cloud: {exc}")
            return -1

    def harvest_from_sqlite(
        self,
        db_filepath: str,
        engine: DispatcherEngine,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> int:
        if not os.path.exists(db_filepath):
            if progress_cb:
                progress_cb(f"SQLite file '{db_filepath}' not found.")
            return 0

        cutoff_epoch = time.time() - 86400.0
        cutoff_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cutoff_epoch))
        parsed_count = 0

        try:
            if progress_cb:
                progress_cb(f"Opening SQLite database '{os.path.basename(db_filepath)}'. Please wait...")
            conn = sqlite3.connect(db_filepath)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]

            all_table_rows: List[Tuple[str, List[Any]]] = []
            for tbl in tables:
                cur.execute(f"PRAGMA table_info({tbl})")
                cols = cur.fetchall()
                col_names = [c[1] for c in cols]

                payload_cols = [
                    c for c in col_names
                    if any(k in c.lower() for k in ("packet", "payload", "raw", "data", "bytes", "hex", "frame", "msg"))
                ]
                if not payload_cols:
                    payload_cols = col_names

                ts_cols = [
                    c for c in col_names
                    if any(k in c.lower() for k in ("time", "ts", "date", "created", "received", "epoch"))
                ]

                rows = []
                if ts_cols:
                    tcol = ts_cols[0]
                    try:
                        cur.execute(
                            f"SELECT {', '.join(payload_cols)} FROM {tbl} "
                            f"WHERE ({tcol} >= ? OR {tcol} >= ?)",
                            (cutoff_epoch, cutoff_iso),
                        )
                        rows = cur.fetchall()
                    except sqlite3.Error:
                        rows = []
                if not rows:
                    try:
                        cur.execute(f"SELECT {', '.join(payload_cols)} FROM {tbl} LIMIT 50000")
                        rows = cur.fetchall()
                    except sqlite3.Error:
                        continue
                if rows:
                    all_table_rows.append((tbl, rows))

            for tbl, rows in all_table_rows:
                if progress_cb:
                    progress_cb(f"Parsing {len(rows)} packets from table '{tbl}'. Please wait...")
                for row in rows:
                    for cell in row:
                        raw_input = self._coerce_packet_cell(cell)
                        if raw_input is None:
                            continue
                        for parser_obj in engine.parsers:
                            try:
                                res = parser_obj.parse(raw_input)
                            except Exception:
                                continue
                            if isinstance(res, dict) and res.get("is_valid"):
                                pdata = res.get("data", res)
                                wiu_id = pdata.get("_node_id_")
                                if wiu_id is not None:
                                    base_b = int(pdata.get("_base_", 16) or 16)
                                    ch = int(pdata.get("Channel", 1) or 1)
                                    self.register_wiu(int(wiu_id), base_b, ch)
                                    parsed_count += 1
                                break
            conn.close()
        except Exception as exc:
            print(f"[WARN] Error harvesting SQLite DB '{db_filepath}': {exc}")

        if parsed_count > 0:
            self.save_cache()
        if progress_cb:
            progress_cb(f"Completed! Parsed {parsed_count} valid packets ({len(self.wiu_records)} unique WIUs cached).")
        return parsed_count

    @staticmethod
    def _coerce_packet_cell(cell: Any) -> Optional[bytes]:
        if cell is None:
            return None
        if isinstance(cell, bytes):
            return cell
        if isinstance(cell, str):
            s = cell.strip()
            if not s:
                return None
            clean_hex = s[2:] if s.lower().startswith("0x") else s.replace(" ", "")
            if len(clean_hex) >= 8 and len(clean_hex) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in clean_hex):
                try:
                    return bytes.fromhex(clean_hex)
                except ValueError:
                    pass
        return None

    def get_ctc_addresses(
        self,
        wiu_id: Any,
        layout_elements: Optional[Dict[str, BaseElement]] = None,
    ) -> List[str]:
        key = _normalize_wiu_key(wiu_id)
        if not key:
            return []
        collected: List[str] = []
        rec = self.wiu_records.get(key)
        if isinstance(rec, dict):
            collected.extend(_parse_ctc_csv(rec.get("ctc_addresses")))
        if layout_elements:
            for el in layout_elements.values():
                el_wid = getattr(el, "wiu_id", None)
                if el_wid is None and getattr(el, "elem_type", "") == "base":
                    el_wid = getattr(el, "radio_id", None)
                if el_wid is not None and _normalize_wiu_key(el_wid) == key:
                    collected.extend(_parse_ctc_csv(getattr(el, "ctc_id", "")))
        return _parse_ctc_csv(collected)

    def get_wiu_gps(
        self,
        wiu_id: Any,
        layout_elements: Optional[Dict[str, BaseElement]] = None,
    ) -> Optional[Tuple[float, float]]:
        key = _normalize_wiu_key(wiu_id)
        if not key:
            return None
        rec = self.wiu_records.get(key)
        if isinstance(rec, dict):
            g = _parse_wiu_gps(rec.get("gps"))
            if g is not None:
                return g
        if layout_elements:
            for el in layout_elements.values():
                el_wid = getattr(el, "wiu_id", None)
                if el_wid is None and getattr(el, "elem_type", "") == "base":
                    el_wid = getattr(el, "radio_id", None)
                if el_wid is not None and _normalize_wiu_key(el_wid) == key:
                    g = _parse_wiu_gps(getattr(el, "gps", None))
                    if g is not None:
                        return g
        return None

    def get_suggestion_list(
        self,
        layout_elements: Dict[str, BaseElement],
        ref_gps: Optional[Tuple[float, float]] = None,
    ) -> List[str]:
        ids: Set[str] = set()
        for k, v in self.wiu_records.items():
            raw = v.get("wiu_id", k) if isinstance(v, dict) else k
            nk = _normalize_wiu_key(raw)
            if nk:
                ids.add(nk)
        for el in (layout_elements or {}).values():
            el_wid = getattr(el, "wiu_id", None)
            if el_wid is None and getattr(el, "elem_type", "") == "base":
                el_wid = getattr(el, "radio_id", None)
            if el_wid is not None:
                nk = _normalize_wiu_key(el_wid)
                if nk:
                    ids.add(nk)

        parsed_ref = _parse_wiu_gps(ref_gps)
        if parsed_ref is not None:
            ref_lat, ref_lon = parsed_ref

            def _prox_key(wid_str: str) -> Tuple[int, float, Tuple[int, int, str]]:
                g = self.get_wiu_gps(wid_str, layout_elements)
                if g is not None:
                    dist = _haversine_miles_coords(ref_lat, ref_lon, g[0], g[1])
                    return (0, round(dist, 6), _wiu_sort_scalar(wid_str))
                return (1, float("inf"), _wiu_sort_scalar(wid_str))

            return sorted(ids, key=_prox_key)

        return sorted(ids, key=_wiu_sort_scalar)


# =====================================================================
# 2. 45-DEGREE GEOMETRY & TOPOLOGY ASSISTANT
# =====================================================================

def format_hex_lines(hex_str: str, bytes_per_line: int = 16) -> List[str]:
    """Break a raw hex string into formatted lines of spaced bytes (e.g. '02 01 04 1B ...')."""
    clean = re.sub(r"[^0-9a-fA-F]", "", hex_str or "").upper()
    if not clean:
        return []
    tokens = [clean[i:i+2] for i in range(0, len(clean), 2)]
    lines = []
    for i in range(0, len(tokens), bytes_per_line):
        chunk = tokens[i:i + bytes_per_line]
        lines.append(" ".join(chunk))
    return lines


def copy_to_clipboard(text: str) -> bool:
    """Cross-platform clipboard copy helper."""
    if not text:
        return False
    copied = False
    try:
        import tkinter as tk
        r = tk.Tk(); r.withdraw()
        r.clipboard_clear(); r.clipboard_append(text); r.update(); r.destroy()
        copied = True
    except Exception:
        pass
    if not copied and os.name == "nt":
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-8"), check=False)
            copied = True
        except Exception:
            pass
    if not copied:
        try:
            pygame.scrap.init()
            pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
            copied = True
        except Exception:
            pass
    return copied


class LayoutTopologyAssistant:
    LOGICAL_UNITS_PER_MILE = 4.0

    @classmethod
    def get_track_endpoints_logical(cls, el_dict_or_obj: Any) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        if isinstance(el_dict_or_obj, dict):
            cx, cy = float(el_dict_or_obj.get("x", 0)), float(el_dict_or_obj.get("y", 0))
            length_mi = float(el_dict_or_obj.get("length_miles", 0.5) or 0.5)
            orient = el_dict_or_obj.get("orientation", "E-W")
        else:
            cx, cy = float(el_dict_or_obj.x), float(el_dict_or_obj.y)
            length_mi = float(getattr(el_dict_or_obj, "length_miles", 0.5) or 0.5)
            orient = getattr(el_dict_or_obj, "orientation", "E-W")

        half_span = max(0.4, (length_mi * cls.LOGICAL_UNITS_PER_MILE) / 2.0)
        if orient == "N-S":
            dx, dy = 0.0, half_span
        elif orient == "NE-SW":
            d = half_span * 0.70710678
            dx, dy = d, -d
        elif orient == "NW-SE":
            d = half_span * 0.70710678
            dx, dy = d, d
        else:
            dx, dy = half_span, 0.0
        return (cx - dx, cy - dy), (cx + dx, cy + dy)

    @staticmethod
    def compute_45deg_polyline(p1: Tuple[float, float], p2: Tuple[float, float]) -> List[Tuple[float, float]]:
        x1, y1 = p1
        x2, y2 = p2
        dx = x2 - x1
        dy = y2 - y1
        adx, ady = abs(dx), abs(dy)

        if adx < 1e-3 or ady < 1e-3 or abs(adx - ady) < 1e-3:
            return [p1, p2]

        diag = min(adx, ady)
        sx = 1.0 if dx > 0 else -1.0
        sy = 1.0 if dy > 0 else -1.0

        if adx > ady:
            knee = (x2 - sx * diag, y1)
        else:
            knee = (x1, y2 - sy * diag)
        return [p1, knee, p2]

    @staticmethod
    def vector_to_compass(dx: float, dy: float) -> str:
        angle_deg = math.degrees(math.atan2(-dy, dx))
        if -22.5 <= angle_deg < 22.5:
            return "E"
        if 22.5 <= angle_deg < 67.5:
            return "NE"
        if 67.5 <= angle_deg < 112.5:
            return "N"
        if 112.5 <= angle_deg < 157.5:
            return "NW"
        if angle_deg >= 157.5 or angle_deg < -157.5:
            return "W"
        if -157.5 <= angle_deg < -112.5:
            return "SW"
        if -112.5 <= angle_deg < -67.5:
            return "S"
        return "SE"

    @staticmethod
    def vector_to_track_orientation(dx: float, dy: float) -> str:
        c = LayoutTopologyAssistant.vector_to_compass(dx, dy)
        if c in ("E", "W"):
            return "E-W"
        if c in ("N", "S"):
            return "N-S"
        if c in ("NE", "SW"):
            return "NE-SW"
        return "NW-SE"

    @classmethod
    def determine_relative_direction(cls, source_el: BaseElement, target_el: BaseElement) -> str:
        """Determines the compass direction of target_el relative to source_el.
        If source_el has an orientation in TRACK_ORIENTATIONS, only directions parallel
        to source_el's orientation are returned.
        """
        dx = float(target_el.x - source_el.x)
        dy = float(target_el.y - source_el.y)

        orient = getattr(source_el, "orientation", None)
        if orient and orient in TRACK_ORIENTATIONS:
            if orient == "E-W":
                return "E" if dx >= 0 else "W"
            elif orient == "N-S":
                return "N" if dy <= 0 else "S"
            elif orient == "NE-SW":
                # Along NE (dx>0, dy<0), dot product with (1, -1) is dx - dy
                return "NE" if (dx - dy) >= 0 else "SW"
            elif orient == "NW-SE":
                # Along SE (dx>0, dy>0), dot product with (1, 1) is dx + dy
                return "SE" if (dx + dy) >= 0 else "NW"

        return cls.vector_to_compass(dx, dy)

    @staticmethod
    def endpoints_in_same_tile(p1: Tuple[float, float], p2: Tuple[float, float]) -> bool:
        """Returns True if two logical points lie within the same grid tile square."""
        return int(round(p1[0])) == int(round(p2[0])) and int(round(p1[1])) == int(round(p2[1]))

    @classmethod
    def connect_track_ends_in_same_tile(cls, target: TrackSegment, elements: Dict[str, BaseElement]) -> int:
        """
        Connects a TrackSegment to any other TrackSegment or Turnout whose endpoint
        is dragged into the exact same tile square.
        """
        if not isinstance(target, TrackSegment):
            return 0
        t_ends = cls.get_track_endpoints_logical(target)
        orient = target.orientation
        if orient == "E-W":
            end_dirs = [("W", t_ends[0]), ("E", t_ends[1])]
        elif orient == "N-S":
            end_dirs = [("N", t_ends[0]), ("S", t_ends[1])]
        elif orient == "NE-SW":
            end_dirs = [("SW", t_ends[0]), ("NE", t_ends[1])]
        else:
            end_dirs = [("NW", t_ends[0]), ("SE", t_ends[1])]

        connected_count = 0
        for eid, other in elements.items():
            if eid == target.id or not isinstance(other, (TrackSegment, Turnout)):
                continue
            if isinstance(other, TrackSegment):
                o_ends = cls.get_track_endpoints_logical(other)
                o_orient = other.orientation
                if o_orient == "E-W":
                    o_end_dirs = [("W", o_ends[0]), ("E", o_ends[1])]
                elif o_orient == "N-S":
                    o_end_dirs = [("N", o_ends[0]), ("S", o_ends[1])]
                elif o_orient == "NE-SW":
                    o_end_dirs = [("SW", o_ends[0]), ("NE", o_ends[1])]
                else:
                    o_end_dirs = [("NW", o_ends[0]), ("SE", o_ends[1])]

                for t_dir, t_pt in end_dirs:
                    for o_dir, o_pt in o_end_dirs:
                        if cls.endpoints_in_same_tile(t_pt, o_pt):
                            target.neighbors.setdefault(t_dir, [])
                            if other.id not in target.neighbors[t_dir]:
                                target.neighbors[t_dir].append(other.id)
                            other.neighbors.setdefault(o_dir, [])
                            if target.id not in other.neighbors[o_dir]:
                                other.neighbors[o_dir].append(target.id)
                            connected_count += 1
            elif isinstance(other, Turnout):
                o_pt = (float(other.x), float(other.y))
                for t_dir, t_pt in end_dirs:
                    if cls.endpoints_in_same_tile(t_pt, o_pt):
                        target.neighbors.setdefault(t_dir, [])
                        if other.id not in target.neighbors[t_dir]:
                            target.neighbors[t_dir].append(other.id)
                        opp_orient = OPPOSITE_DIR.get(other.orientation, "W")
                        cdir_from_to = OPPOSITE_DIR.get(t_dir, "E")
                        if not other.common_neighbor and cdir_from_to == opp_orient:
                            other.common_neighbor = target.id
                        elif not other.normal_neighbor and target.id != other.common_neighbor:
                            other.normal_neighbor = target.id
                        elif not other.reverse_neighbor and target.id not in (other.common_neighbor, other.normal_neighbor):
                            other.reverse_neighbor = target.id
                        connected_count += 1
        return connected_count

    @classmethod
    def auto_suggest_connections(cls, target: BaseElement, elements: Dict[str, BaseElement]):
        if isinstance(target, TrackSegment):
            # In Creator, tracks only auto-connect if their ends are dragged within the same tile square
            cls.connect_track_ends_in_same_tile(target, elements)
            return

        candidates: List[Tuple[float, str, BaseElement]] = []
        t_ends = cls.get_track_endpoints_logical(target) if isinstance(target, TrackSegment) else ((float(target.x), float(target.y)),)

        if isinstance(target, (TrackSegment, Turnout)):
            search_radius = 1.5
        elif isinstance(target, Crossing):
            search_radius = 4.0
        else:
            search_radius = 3.5

        for eid, other in elements.items():
            if eid == target.id or other.elem_type in SYSTEM_LIST_TYPES:
                continue
            dx = other.x - target.x
            dy = other.y - target.y
            center_dist = math.hypot(dx, dy)

            o_ends = cls.get_track_endpoints_logical(other) if isinstance(other, TrackSegment) else ((float(other.x), float(other.y)),)
            min_end_dist = min(
                math.hypot(tx - ox, ty - oy)
                for (tx, ty) in t_ends
                for (ox, oy) in o_ends
            )
            eff_dist = min(center_dist, min_end_dist)
            
            if eff_dist <= search_radius and (abs(dx) > 0.01 or abs(dy) > 0.01):
                cdir = cls.vector_to_compass(dx, dy)
                candidates.append((eff_dist, cdir, other))

        candidates.sort(key=lambda item: item[0])

        if isinstance(target, TrackSegment):
            track_cands = [(d, cdir, o) for d, cdir, o in candidates if isinstance(o, (TrackSegment, Turnout))]
            orient = target.orientation
            if orient == "E-W":
                fwd_dirs, back_dirs = ("E", "NE", "SE"), ("W", "NW", "SW")
            elif orient == "N-S":
                fwd_dirs, back_dirs = ("N", "NE", "NW"), ("S", "SE", "SW")
            elif orient == "NE-SW":
                fwd_dirs, back_dirs = ("NE", "N", "E"), ("SW", "S", "W")
            else:
                fwd_dirs, back_dirs = ("SE", "S", "E"), ("NW", "N", "W")

            if not any(target.neighbors.get(d) for d in fwd_dirs):
                for _, cdir, other in track_cands:
                    if cdir in fwd_dirs:
                        target.neighbors.setdefault(cdir, [])
                        if other.id not in target.neighbors[cdir]:
                            target.neighbors[cdir].append(other.id)
                        cls._add_reciprocal_link(other, target, OPPOSITE_DIR[cdir])
                        break

            if not any(target.neighbors.get(d) for d in back_dirs):
                for _, cdir, other in track_cands:
                    if cdir in back_dirs:
                        target.neighbors.setdefault(cdir, [])
                        if other.id not in target.neighbors[cdir]:
                            target.neighbors[cdir].append(other.id)
                        cls._add_reciprocal_link(other, target, OPPOSITE_DIR[cdir])
                        break

            if not target.parallel_track_id:
                for dist, cdir, other in track_cands:
                    if isinstance(other, TrackSegment) and other.orientation == target.orientation and dist <= 4.0:
                        if (target.orientation == "E-W" and cdir in ("N", "S")) or (
                            target.orientation == "N-S" and cdir in ("E", "W")
                        ):
                            target.parallel_track_id = other.id
                            break

        elif isinstance(target, Turnout):
            track_cands = [(d, cdir, o) for d, cdir, o in candidates if isinstance(o, (TrackSegment, Turnout))]
            opp_orient = OPPOSITE_DIR.get(target.orientation, "W")

            if not target.common_neighbor:
                for _, cdir, other in track_cands:
                    if cdir == opp_orient:
                        target.common_neighbor = other.id
                        cls._add_reciprocal_link(other, target, target.orientation)
                        break

            if not target.normal_neighbor:
                for _, cdir, other in track_cands:
                    if other.id != target.common_neighbor and cdir == target.orientation:
                        target.normal_neighbor = other.id
                        target.normal_dir = cdir
                        cls._add_reciprocal_link(other, target, OPPOSITE_DIR[cdir])
                        break

            if not target.reverse_neighbor:
                for _, cdir, other in track_cands:
                    if other.id not in (target.common_neighbor, target.normal_neighbor):
                        target.reverse_neighbor = other.id
                        target.reverse_dir = cdir
                        cls._add_reciprocal_link(other, target, OPPOSITE_DIR[cdir])
                        break

        elif isinstance(target, Signal):
            track_cands = [(d, cdir, o) for d, cdir, o in candidates if isinstance(o, (TrackSegment, Turnout))]
            if track_cands:
                if not target.protects_element:
                    target.protects_element = track_cands[0][2].id
                    prot = track_cands[0][2]
                    if isinstance(prot, Turnout) and target.id not in prot.guarded_by_signals:
                        prot.guarded_by_signals.append(target.id)

                if not target.rear_element and target.protects_element in elements:
                    prot = elements[target.protects_element]
                    opp = OPPOSITE_DIR.get(target.direction, "W")
                    if isinstance(prot, TrackSegment):
                        rear_list = prot.neighbors.get(opp, [])
                        if rear_list:
                            target.rear_element = rear_list[0]
                    elif isinstance(prot, Turnout):
                        target.rear_element = prot.common_neighbor

                if not target.rear_element and len(track_cands) >= 2:
                    for _, _, other in track_cands:
                        if other.id != target.protects_element:
                            target.rear_element = other.id
                            break

        elif isinstance(target, Crossing):
            if not target.monitored_tracks:
                for dist, _, other in candidates:
                    if isinstance(other, (TrackSegment, Turnout)) and dist <= 6.0:
                        target.monitored_tracks.append(other.id)
                        if len(target.monitored_tracks) >= 2:
                            break

        elif isinstance(target, TrackJump):
            track_cands = [(d, cdir, o) for d, cdir, o in candidates if isinstance(o, (TrackSegment, Turnout))]
            if track_cands and not target.connected_element:
                _, cdir, nearest = track_cands[0]
                target.connected_element = nearest.id
                target.direction = cdir
                cls._add_reciprocal_link(nearest, target, OPPOSITE_DIR.get(cdir, "W"))
            elif target.connected_element and target.connected_element in elements:
                conn_el = elements[target.connected_element]
                cdir = cls.vector_to_compass(conn_el.x - target.x, conn_el.y - target.y)
                cls._add_reciprocal_link(conn_el, target, OPPOSITE_DIR.get(cdir, "W"))

            # Auto-pair with another TrackJump sharing the same connector letter
            my_letter = (target.letter or "A").strip().upper()
            for other in elements.values():
                if isinstance(other, TrackJump) and other.id != target.id and (other.letter or "").strip().upper() == my_letter:
                    target.paired_jump_id = other.id
                    if not other.paired_jump_id or other.paired_jump_id not in elements:
                        other.paired_jump_id = target.id
                    break

    @staticmethod
    def _add_reciprocal_link(neighbor: BaseElement, source: BaseElement, compass_from_neighbor: str):
        if isinstance(neighbor, TrackSegment):
            lst = neighbor.neighbors.setdefault(compass_from_neighbor, [])
            if source.id not in lst:
                lst.append(source.id)

    @staticmethod
    def verify_layout(elements: Dict[str, BaseElement]) -> List[Tuple[str, str]]:
        issues: List[Tuple[str, str]] = []
        tracks_and_turnouts = {
            eid: el for eid, el in elements.items() if isinstance(el, (TrackSegment, Turnout))
        }
        signals = [el for el in elements.values() if isinstance(el, Signal)]

        if not tracks_and_turnouts:
            issues.append(("ERROR", "Layout contains no Track or Turnout elements."))
            return issues

        wiu_bit_owners: Dict[Tuple[int, int], List[str]] = {}

        for el in elements.values():
            if isinstance(el, Signal):
                if el.wiu_id is None:
                    issues.append(("ERROR", f"Signal '{el.id}' is missing mandatory wiu_id."))
                if el.sig_bit_offset is None or el.sig_bit_offset < 0:
                    issues.append(("ERROR", f"Signal '{el.id}' is missing valid sig_bit_offset."))
                if not el.protects_element or el.protects_element not in elements:
                    issues.append(("ERROR", f"Signal '{el.id}' has unset/invalid protects_element '{el.protects_element}'."))
                if not el.rear_element or el.rear_element not in elements:
                    issues.append(("ERROR", f"Signal '{el.id}' has unset/invalid rear_element '{el.rear_element}'."))

                if el.wiu_id is not None and el.sig_bit_offset is not None:
                    for b in range(el.sig_bit_offset, el.sig_bit_offset + 5):
                        key = (int(el.wiu_id), b)
                        owner_tag = f"SIG:{el.id}({el.position})"
                        wiu_bit_owners.setdefault(key, []).append(owner_tag)

            elif isinstance(el, Turnout):
                if el.wiu_id is None:
                    issues.append(("ERROR", f"Turnout '{el.id}' is missing mandatory wiu_id."))
                if el.cmd_bit_offset is None or el.cmd_bit_offset < 0:
                    issues.append(("ERROR", f"Turnout '{el.id}' is missing mandatory cmd_bit_offset (2 switch bits)."))
                for leg_attr in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                    leg_val = getattr(el, leg_attr, "")
                    if not leg_val or leg_val not in elements:
                        issues.append(("ERROR", f"Turnout '{el.id}' has unset/invalid {leg_attr}='{leg_val}'."))

                if el.wiu_id is not None and el.cmd_bit_offset is not None:
                    for b in range(el.cmd_bit_offset, el.cmd_bit_offset + 2):
                        wiu_bit_owners.setdefault((int(el.wiu_id), b), []).append(f"TO_CMD:{el.id}")

                if el.has_occ_bit:
                    if el.occ_bit_offset is None:
                        issues.append(("ERROR", f"Turnout '{el.id}' has_occ_bit=True but occ_bit_offset is None."))
                    elif el.wiu_id is not None:
                        wiu_bit_owners.setdefault((int(el.wiu_id), int(el.occ_bit_offset)), []).append(f"TO_OCC:{el.id}")

            elif isinstance(el, TrackSegment):
                if el.has_occ_bit:
                    if el.wiu_id is None or el.occ_bit_offset is None:
                        issues.append(("ERROR", f"Track '{el.id}' has_occ_bit=True but missing wiu_id or occ_bit_offset."))
                    else:
                        wiu_bit_owners.setdefault((int(el.wiu_id), int(el.occ_bit_offset)), []).append(f"TK_OCC:{el.id}")

        for (wiu_id, bit_idx), owners in sorted(wiu_bit_owners.items()):
            if len(owners) > 1:
                all_sigs = all(o.startswith("SIG:") for o in owners)
                positions = [o.split("(")[-1].rstrip(")") for o in owners] if all_sigs else []
                if not all_sigs or len(set(positions)) != len(positions):
                    issues.append((
                        "ERROR",
                        f"Duplicate WIU Bit Controller: WIU {wiu_id} bit {bit_idx} is claimed by multiple entities: {', '.join(owners)}",
                    ))

        transition_controllers: Dict[Tuple[str, str, str], List[str]] = {}
        directly_guarded: Set[str] = set()

        for sig in signals:
            if sig.protects_element:
                directly_guarded.add(sig.protects_element)
            if sig.rear_element:
                directly_guarded.add(sig.rear_element)
            t_key = (sig.rear_element, sig.protects_element, sig.position.lower())
            transition_controllers.setdefault(t_key, []).append(sig.id)

        for (rear_id, prot_id, pos), sig_ids in transition_controllers.items():
            if rear_id and prot_id and len(sig_ids) > 1:
                issues.append((
                    "ERROR",
                    f"Duplicate Controlling Signals: {sig_ids} both control transition {rear_id} -> {prot_id} (position='{pos}').",
                ))

        for eid, el in tracks_and_turnouts.items():
            if isinstance(el, Turnout):
                if not el.guarded_by_signals and eid not in directly_guarded:
                    issues.append(("ERROR", f"Turnout '{eid}' has no controlling/guarding signal."))
            elif isinstance(el, TrackSegment):
                if eid not in directly_guarded:
                    adj_ids = [n for nbrs in el.neighbors.values() for n in nbrs]
                    if not any(aid in directly_guarded for aid in adj_ids):
                        issues.append(("ERROR", f"Track '{eid}' has no controlling signal on itself or its immediate neighbors."))

        adj_graph: Dict[str, Set[str]] = {eid: set() for eid in tracks_and_turnouts}
        for eid, el in tracks_and_turnouts.items():
            if isinstance(el, TrackSegment):
                for nbr_list in el.neighbors.values():
                    for nid in nbr_list:
                        if nid in adj_graph:
                            adj_graph[eid].add(nid)
                            adj_graph[nid].add(eid)
            elif isinstance(el, Turnout):
                for nid in (el.common_neighbor, el.normal_neighbor, el.reverse_neighbor):
                    if nid in adj_graph:
                        adj_graph[eid].add(nid)
                        adj_graph[nid].add(eid)

        # Include TrackJump line-break connectors in reachability graph
        jumps = [el for el in elements.values() if isinstance(el, TrackJump)]
        for j_a in jumps:
            if not j_a.connected_element or j_a.connected_element not in adj_graph:
                continue
            partner = elements.get(j_a.paired_jump_id) if j_a.paired_jump_id else None
            if not isinstance(partner, TrackJump):
                for j_b in jumps:
                    if j_b.id != j_a.id and (j_b.letter or "").strip().upper() == (j_a.letter or "").strip().upper():
                        partner = j_b
                        break
            if isinstance(partner, TrackJump) and partner.connected_element in adj_graph:
                adj_graph[j_a.connected_element].add(partner.connected_element)
                adj_graph[partner.connected_element].add(j_a.connected_element)

        start_node = next(iter(tracks_and_turnouts.keys()))
        visited: Set[str] = set()
        queue = [start_node]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            for nbr in adj_graph.get(curr, set()):
                if nbr not in visited:
                    queue.append(nbr)

        unreachable = sorted(set(tracks_and_turnouts.keys()) - visited)
        if unreachable:
            issues.append((
                "ERROR",
                f"Unreachable Tracks/Turnouts (disconnected from main network): {', '.join(unreachable)}",
            ))

        for eid in tracks_and_turnouts:
            if not adj_graph[eid]:
                issues.append(("ERROR", f"Element '{eid}' has zero connected track/turnout neighbors."))

        return issues


# =====================================================================
# 3. IN-PLACE PROPERTY EDITOR PANEL & BUILT-IN INTERACTIVE GPS PICKER

class OSMTileMapManager:
    def __init__(self, cache_dir: str = MAP_CACHE_DIR):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.surface_cache: Dict[Tuple[int, int, int], pygame.Surface] = {}
        self.pending: set = set()
        self.lock = threading.Lock()

    @staticmethod
    def latlon_to_world_px(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
        lat_rad = math.radians(max(-85.0511, min(85.0511, lat)))
        n = 2.0 ** zoom
        x = (lon + 180.0) / 360.0 * n * 256.0
        y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * 256.0
        return x, y

    @staticmethod
    def world_px_to_latlon(px: float, py: float, zoom: int) -> Tuple[float, float]:
        n = 2.0 ** zoom
        lon = (px / (n * 256.0)) * 360.0 - 180.0
        val = max(-50.0, min(50.0, math.pi * (1.0 - 2.0 * py / (n * 256.0))))
        lat_rad = math.atan(math.sinh(val))
        lat = math.degrees(lat_rad)
        return max(-85.0511, min(85.0511, lat)), max(-180.0, min(180.0, lon))

    def _tile_filepath(self, z: int, x: int, y: int, layer: str = "sat") -> str:
        ext = "jpg" if layer == "sat" else "png"
        return os.path.join(self.cache_dir, f"{layer}_{z}_{x}_{y}.{ext}")

    def _fetch_worker(self, z: int, x: int, y: int, layer: str = "sat"):
        path = self._tile_filepath(z, x, y, layer)
        if not os.path.exists(path):
            if layer == "sat":
                url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            else:
                url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"ApexBOSCreator/{CREATOR_VERSION} (MIT License)"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    data = resp.read()
                with open(path, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        with self.lock:
            self.pending.discard((layer, z, x, y))

    def get_tile_surface(self, z: int, x: int, y: int, layer: str = "sat") -> Optional[pygame.Surface]:
        key = (layer, z, x, y)
        if key in self.surface_cache:
            return self.surface_cache[key]
        path = self._tile_filepath(z, x, y, layer)
        if os.path.exists(path):
            try:
                surf = pygame.image.load(path).convert()
                self.surface_cache[key] = surf
                return surf
            except Exception:
                pass
        with self.lock:
            if key not in self.pending:
                self.pending.add(key)
                threading.Thread(target=self._fetch_worker, args=(z, x, y, layer), daemon=True).start()
        return None


class InteractiveGPSMapModal:
    RADIUS_PRESETS: List[float] = [0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 50.0, 75.0, 100.0]

    def __init__(
        self,
        tile_mgr: OSMTileMapManager,
        start_lat: float,
        start_lon: float,
        all_elements: Dict[str, BaseElement],
        on_pick_cb: Optional[Callable[[float, float], None]] = None,
        on_create_element_cb: Optional[Callable[[str, float, float], None]] = None,
        estimate_layout_pos_cb: Optional[Callable[[float, float], Tuple[int, int]]] = None,
        mode: str = "pick",
        zoom: int = 16,
        view_radius_miles: float = 0.5,
        on_radius_change_cb: Optional[Callable[[float], None]] = None,
    ):
        self.tile_mgr = tile_mgr
        self.center_lat = float(start_lat)
        self.center_lon = float(start_lon)
        self.picked_lat = float(start_lat)
        self.picked_lon = float(start_lon)
        self.all_elements = all_elements
        self.on_pick_cb = on_pick_cb
        self.on_create_element_cb = on_create_element_cb
        self.estimate_layout_pos_cb = estimate_layout_pos_cb
        self.mode = mode
        self.zoom = max(3, min(19, int(zoom)))
        self.view_radius_miles = float(view_radius_miles)
        self.on_radius_change_cb = on_radius_change_cb
        self.layer = "sat"
        self.is_panning = False
        self.pan_has_moved = False
        self.pan_start_mouse = (0, 0)
        self.pan_start_world_px = (0.0, 0.0)
        self.modal_rect = pygame.Rect(0, 0, 0, 0)
        self.map_rect = pygame.Rect(0, 0, 0, 0)
        self.btn_rects: Dict[str, pygame.Rect] = {}
        self.show_element_selector: bool = False
        self.selector_btn_rects: Dict[str, pygame.Rect] = {}
        self.selector_box_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)

    def step_view_radius(self, step: int):
        cur = self.view_radius_miles
        idx = min(range(len(self.RADIUS_PRESETS)), key=lambda i: abs(self.RADIUS_PRESETS[i] - cur))
        new_idx = max(0, min(len(self.RADIUS_PRESETS) - 1, idx + step))
        self.view_radius_miles = self.RADIUS_PRESETS[new_idx]
        if self.on_radius_change_cb:
            self.on_radius_change_cb(self.view_radius_miles)

    def cycle_view_radius(self, step: int = 1):
        cur = self.view_radius_miles
        idx = min(range(len(self.RADIUS_PRESETS)), key=lambda i: abs(self.RADIUS_PRESETS[i] - cur))
        new_idx = (idx + step) % len(self.RADIUS_PRESETS)
        self.view_radius_miles = self.RADIUS_PRESETS[new_idx]
        if self.on_radius_change_cb:
            self.on_radius_change_cb(self.view_radius_miles)

    def zoom_at_point(self, mx: int, my: int, delta_z: int):
        new_z = max(3, min(19, self.zoom + delta_z))
        if new_z == self.zoom:
            return
        if self.map_rect.width > 0 and self.map_rect.collidepoint(mx, my):
            cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            dx = mx - self.map_rect.centerx
            dy = my - self.map_rect.centery
            hover_lat, hover_lon = self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)
            self.zoom = new_z
            new_hx, new_hy = self.tile_mgr.latlon_to_world_px(hover_lat, hover_lon, self.zoom)
            self.center_lat, self.center_lon = self.tile_mgr.world_px_to_latlon(new_hx - dx, new_hy - dy, self.zoom)
        else:
            self.zoom = new_z

    def screen_to_latlon(self, mx: int, my: int) -> Tuple[float, float]:
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
        dx = mx - self.map_rect.centerx
        dy = my - self.map_rect.centery
        return self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)

    def draw(self, screen: pygame.Surface, font_sm: pygame.font.Font, font_md: pygame.font.Font, font_sm_bold: Optional[pygame.font.Font] = None):
        if font_sm_bold is None:
            font_sm_bold = font_md

        sw, sh = screen.get_size()
        if self.mode == "create":
            mw = max(720, min(sw - 40, 1600))
            mh = max(520, min(sh - 50, 1000))
        else:
            mw = min(sw - 80, 860)
            mh = min(sh - 90, 620)
        mx = (sw - mw) // 2
        my = (sh - mh) // 2
        self.modal_rect = pygame.Rect(mx, my, mw, mh)
        self.map_rect = pygame.Rect(mx + 14, my + 42, mw - 28, mh - 94)

        panel = pygame.Surface((mw, mh), pygame.SRCALPHA)
        panel.fill((16, 22, 32, 248))
        pygame.draw.rect(panel, COLOR_SELECT_RING, (0, 0, mw, mh), 2, border_radius=8)
        screen.blit(panel, (mx, my))

        mode_lbl = "SATELLITE" if self.layer == "sat" else "STREET MAP"
        rad_val = getattr(self, "view_radius_miles", 0.5)
        rad_disp = f"{int(rad_val)}mi" if float(rad_val).is_integer() else f"{rad_val:g}mi"
        if self.mode == "create":
            title = f"BIG MAP — DEFINE ELEMENT FROM GPS [{mode_lbl} | Zoom:{self.zoom} | 1xRad:{rad_disp}] — Pick Spot | Drag: Pan | Wheel: Zoom | Rad-/+: Adjust 1x Radius"
        else:
            title = f"GPS SELECTOR [{mode_lbl} | Zoom:{self.zoom}] — Left-Drag: Pan | Wheel: Zoom | Right-Click: Set GPS"
        screen.blit(font_md.render(title, True, COLOR_EDIT_ACCENT), (mx + 16, my + 12))

        map_surf = pygame.Surface((self.map_rect.width, self.map_rect.height))
        map_surf.fill((26, 34, 46))
        z = self.zoom
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, z)
        mw_map, mh_map = self.map_rect.width, self.map_rect.height

        min_tx = int(math.floor((cx_px - mw_map / 2.0) / 256.0))
        max_tx = int(math.floor((cx_px + mw_map / 2.0) / 256.0))
        min_ty = int(math.floor((cy_px - mh_map / 2.0) / 256.0))
        max_ty = int(math.floor((cy_px + mh_map / 2.0) / 256.0))
        n_tiles = 2 ** z

        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles
                if ty < 0 or ty >= n_tiles:
                    continue
                tile_s = self.tile_mgr.get_tile_surface(z, wrapped_tx, ty, layer=self.layer)
                dx = int(mw_map / 2.0 + (tx * 256.0 - cx_px))
                dy = int(mh_map / 2.0 + (ty * 256.0 - cy_px))
                if tile_s:
                    map_surf.blit(tile_s, (dx, dy))
                else:
                    pygame.draw.rect(map_surf, (38, 48, 64), (dx, dy, 256, 256), 1)

        type_colors = {
            "track": (50, 230, 110),
            "turnout": (255, 175, 45),
            "signal": (255, 220, 50),
            "crossing": (255, 80, 80),
            "base": (195, 130, 255),
            "station": (60, 205, 255),
            "bridge": (140, 200, 255),
            "diamond": (255, 190, 140),
            "label": (230, 240, 255),
            "track_jump": (255, 130, 190),
            "ptc_datetime": (100, 245, 200),
        }

        for el in self.all_elements.values():
            if getattr(el, "gps", None) and len(el.gps) == 2:
                ex_px, ey_px = self.tile_mgr.latlon_to_world_px(el.gps[0], el.gps[1], z)
                px = int(mw_map / 2.0 + (ex_px - cx_px))
                py = int(mh_map / 2.0 + (ey_px - cy_px))
                if 0 <= px < mw_map and 0 <= py < mh_map:
                    dot_c = type_colors.get(getattr(el, "elem_type", "track"), (160, 180, 205))
                    pygame.draw.circle(map_surf, (20, 24, 30), (px, py), 6)
                    pygame.draw.circle(map_surf, dot_c, (px, py), 4)

                    tag_str = str(getattr(el, "label", None) or el.id)
                    lbl_s = font_sm.render(tag_str, True, (240, 248, 255))
                    lw, lh = lbl_s.get_width() + 6, lbl_s.get_height() + 2
                    tag_bg = pygame.Surface((lw, lh), pygame.SRCALPHA)
                    tag_bg.fill((15, 20, 28, 200))
                    pygame.draw.rect(tag_bg, (80, 100, 125, 210), (0, 0, lw, lh), 1, border_radius=2)
                    tag_bg.blit(lbl_s, (3, 1))
                    map_surf.blit(tag_bg, (px + 7, py - lh // 2))

        pk_x, pk_y = self.tile_mgr.latlon_to_world_px(self.picked_lat, self.picked_lon, z)
        ppx = int(mw_map / 2.0 + (pk_x - cx_px))
        ppy = int(mh_map / 2.0 + (pk_y - cy_px))
        if 0 <= ppx < mw_map and 0 <= ppy < mh_map:
            pygame.draw.circle(map_surf, (10, 10, 15, 160), (ppx + 2, ppy + 2), 5)
            pygame.draw.line(map_surf, (20, 20, 25), (ppx, ppy), (ppx, ppy - 16), 3)
            pygame.draw.circle(map_surf, (255, 255, 255), (ppx, ppy - 16), 10)
            pygame.draw.circle(map_surf, (0, 235, 255), (ppx, ppy - 16), 7)
            pygame.draw.circle(map_surf, (255, 45, 45), (ppx, ppy - 16), 4)
            pygame.draw.circle(map_surf, (255, 220, 0), (ppx, ppy), 3)

            pin_lbl = "NEW ELEMENT LOCATION" if self.mode == "create" else "SELECTED GPS"
            pin_s = font_sm.render(pin_lbl, True, (0, 240, 255))
            pw, ph = pin_s.get_width() + 8, pin_s.get_height() + 4
            pbox = pygame.Surface((pw, ph), pygame.SRCALPHA)
            pbox.fill((16, 24, 36, 225))
            pygame.draw.rect(pbox, (0, 220, 255), (0, 0, pw, ph), 1, border_radius=3)
            pbox.blit(pin_s, (4, 2))
            map_surf.blit(pbox, (ppx - pw // 2, ppy - 22 - ph))

        pygame.draw.rect(map_surf, COLOR_HUD_BORDER, (0, 0, mw_map, mh_map), 2)
        screen.blit(map_surf, self.map_rect.topleft)

        fy = self.map_rect.bottom + 10
        self.btn_rects.clear()
        if self.mode == "create":
            rad_val = getattr(self, "view_radius_miles", 0.5)
            rad_disp = f"{int(rad_val)}mi" if float(rad_val).is_integer() else f"{rad_val:g}mi"
            est_info = ""
            if self.estimate_layout_pos_cb:
                elx, ely = self.estimate_layout_pos_cb(self.picked_lat, self.picked_lon)
                est_info = f"   ➔   Estimated Grid: ({elx}, {ely}) [@1x: {rad_disp}]"
            coord_txt = f"Picked GPS: {self.picked_lat:.6f}, {self.picked_lon:.6f}{est_info}"
            btn_specs = [
                ("toggle_layer", "Map: Street" if self.layer == "sat" else "Map: Satellite", (85, 65, 135)),
                ("center_layout", "Center Layout", (65, 80, 110)),
                ("zoom_in", "Zoom +", (45, 85, 135)),
                ("zoom_out", "Zoom -", (45, 85, 135)),
                ("radius_dec", "Rad -", (55, 75, 110)),
                ("radius_val", f"1x Rad: {rad_disp}", (40, 60, 90)),
                ("radius_inc", "Rad +", (55, 75, 110)),
                ("define_element", "+ Define Element Here (Enter)", (30, 145, 75)),
                ("cancel", "Close (Esc)", (135, 45, 45)),
            ]
        else:
            coord_txt = f"Selected GPS: {self.picked_lat:.6f}, {self.picked_lon:.6f}   (Right-click on map to pick & set)"
            btn_specs = [
                ("toggle_layer", "Map: Street" if self.layer == "sat" else "Map: Satellite", (85, 65, 135)),
                ("zoom_in", "Zoom +", (45, 85, 135)),
                ("zoom_out", "Zoom -", (45, 85, 135)),
                ("apply", "Use GPS (Enter)", (35, 130, 65)),
                ("cancel", "Close (Esc)", (135, 45, 45)),
            ]

        # Calculate dynamic button widths with generous padding so text fits cleanly inside frames
        btn_layout = []
        for bkey, btxt, bcol in btn_specs:
            tw, th = font_sm.size(btxt)
            pad = 16 if bkey in ("radius_dec", "radius_inc") else 24
            min_w = 56 if bkey in ("radius_dec", "radius_inc") else 74
            bw = max(min_w, tw + pad)
            btn_layout.append((bkey, btxt, bw, bcol))

        btn_gap = 8
        cur_rx = mx + mw - 16
        btns = []
        for bkey, btxt, bw, bcol in reversed(btn_layout):
            bx = cur_rx - bw
            btns.append((bkey, btxt, bx, bw, bcol))
            cur_rx = bx - btn_gap
        btns.reverse()

        # Render left-side coordinates text without colliding into buttons
        left_avail_w = max(100, (btns[0][2] - (mx + 16) - 10)) if btns else (mw - 32)
        coord_surf = font_sm.render(coord_txt, True, COLOR_OK_GREEN)
        if coord_surf.get_width() > left_avail_w:
            coord_surf = font_sm.render(f"GPS: {self.picked_lat:.6f}, {self.picked_lon:.6f}", True, COLOR_OK_GREEN)
        screen.blit(coord_surf, (mx + 16, fy + 6))

        for bkey, btxt, bx, bw, bcol in btns:
            r = pygame.Rect(bx, fy, bw, 28)
            self.btn_rects[bkey] = r
            pygame.draw.rect(screen, bcol, r, border_radius=4)
            pygame.draw.rect(screen, COLOR_HUD_BORDER, r, 1, border_radius=4)
            ts = font_sm.render(btxt, True, (255, 255, 255))
            screen.blit(ts, (bx + (bw - ts.get_width()) // 2, fy + (28 - ts.get_height()) // 2))

        if self.show_element_selector and self.mode == "create":
            dim = pygame.Surface((mw, mh), pygame.SRCALPHA)
            dim.fill((5, 10, 18, 185))
            screen.blit(dim, (mx, my))

            box_w, box_h = 630, 456
            box_x = mx + (mw - box_w) // 2
            box_y = my + (mh - box_h) // 2
            self.selector_box_rect = pygame.Rect(box_x, box_y, box_w, box_h)

            sbox = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            sbox.fill((18, 26, 38, 250))
            pygame.draw.rect(sbox, COLOR_SELECT_RING, (0, 0, box_w, box_h), 2, border_radius=8)
            screen.blit(sbox, (box_x, box_y))

            t1 = font_md.render("SELECT ELEMENT TYPE TO DEFINE ON LAYOUT", True, COLOR_EDIT_ACCENT)
            screen.blit(t1, (box_x + (box_w - t1.get_width()) // 2, box_y + 16))

            elx, ely = (0, 0)
            if self.estimate_layout_pos_cb:
                elx, ely = self.estimate_layout_pos_cb(self.picked_lat, self.picked_lon)
            sub = f"Location GPS: ({self.picked_lat:.6f}, {self.picked_lon:.6f})   |   Estimated Grid: ({elx}, {ely})"
            t2 = font_sm.render(sub, True, COLOR_OK_GREEN)
            screen.blit(t2, (box_x + (box_w - t2.get_width()) // 2, box_y + 42))

            elem_choices = [
                ("track", "1. Track Segment", "T", (40, 100, 60)),
                ("turnout", "2. Turnout / Switch", "W", (110, 75, 30)),
                ("signal", "3. Signal Head", "S", (110, 95, 25)),
                ("crossing", "4. Grade Crossing", "X", (110, 45, 45)),
                ("base", "5. Base Station / WIU", "B", (75, 45, 110)),
                ("station", "6. Station / Platform", "ST", (35, 85, 110)),
                ("bridge", "7. Railroad Bridge", "BR", (45, 80, 110)),
                ("diamond", "8. Track Diamond", "D", (100, 70, 50)),
                ("label", "9. Landmark / Label", "L", (70, 80, 95)),
                ("track_jump", "0. Track Jump Break", "J", (95, 45, 80)),
                ("ptc_datetime", "D. PTC Clock Display", "DT", (35, 95, 85)),
                ("chat", "C. Chat Broadcast", "CH", (40, 95, 115)),
                ("cancel_selector", "Esc. Back to Map", "◄", (85, 35, 35)),
            ]

            self.selector_btn_rects.clear()
            btn_w = 188
            btn_h = 56
            start_x = box_x + 18
            start_y = box_y + 70
            col_gap = 14
            row_gap = 10

            m_pos = pygame.mouse.get_pos()
            for i, (ekey, elbl, eicon, ecol) in enumerate(elem_choices):
                col = i % 3
                row = i // 3
                bx = start_x + col * (btn_w + col_gap)
                by = start_y + row * (btn_h + row_gap)
                brect = pygame.Rect(bx, by, btn_w, btn_h)
                self.selector_btn_rects[ekey] = brect

                is_hov = brect.collidepoint(m_pos)
                draw_col = tuple(min(255, c + 35) for c in ecol) if is_hov else ecol
                border_c = COLOR_SELECT_RING if is_hov else COLOR_HUD_BORDER

                pygame.draw.rect(screen, draw_col, brect, border_radius=6)
                pygame.draw.rect(screen, border_c, brect, 2 if is_hov else 1, border_radius=6)

                ic_surf = font_md.render(eicon, True, (255, 255, 255))
                screen.blit(ic_surf, (bx + 10, by + 8))

                lbl_surf = font_sm_bold.render(elbl, True, (255, 255, 255))
                screen.blit(lbl_surf, (bx + 10, by + 30))
# =====================================================================

class InPlaceElementEditor:
    def __init__(
        self,
        element: BaseElement,
        screen_pos: Tuple[int, int],
        all_elements: Dict[str, BaseElement],
        wiu_cache: WIUAddressCache,
        get_last_gps_cb: Callable[[], Tuple[float, float]],
        set_last_gps_cb: Callable[[Tuple[float, float]], None],
        default_radius: float = 20.0,
        is_newly_created: bool = False,
        get_ref_gps_cb: Optional[Callable[[], Optional[Tuple[float, float]]]] = None,
    ):
        self.element = element
        self.orig_id = element.id
        self.screen_pos = screen_pos
        self.all_elements = all_elements
        self.wiu_cache = wiu_cache
        self.get_last_gps_cb = get_last_gps_cb
        self.set_last_gps_cb = set_last_gps_cb
        self.default_radius = default_radius
        self.is_newly_created = is_newly_created
        self.get_ref_gps_cb = get_ref_gps_cb

        self.fields: List[Dict[str, Any]] = []
        self.active_field_idx: int = 0
        self.select_all_active: bool = True
        self.validation_error: str = ""
        self.field_rects: List[pygame.Rect] = []
        self.btn_rects: Dict[str, pygame.Rect] = {}
        self.suggestion_rects: List[Tuple[pygame.Rect, str]] = []
        self._ctc_user_modified: bool = False
        self._ctc_auto_populated: bool = False

        self.is_dragging = False
        self.drag_offset = (0, 0)
        self.px: Optional[int] = None
        self.py: Optional[int] = None
        self.panel_w = 0
        self.panel_h = 0
        self.panel_rect = pygame.Rect(0, 0, 0, 0)

        self._build_fields_from_element()

    def _get_editor_ref_gps(self) -> Optional[Tuple[float, float]]:
        for fd in self.fields:
            if fd.get("key") == "gps" and fd.get("val", "").strip():
                g = _parse_wiu_gps(fd["val"])
                if g is not None:
                    return g
                break
        el_g = _parse_wiu_gps(getattr(self.element, "gps", None))
        if el_g is not None:
            return el_g
        if self.get_ref_gps_cb:
            try:
                cb_g = _parse_wiu_gps(self.get_ref_gps_cb())
                if cb_g is not None:
                    return cb_g
            except Exception:
                pass
        return None

    def _refresh_wiu_choices(self):
        ref_gps = self._get_editor_ref_gps()
        wiu_choices = self.wiu_cache.get_suggestion_list(self.all_elements, ref_gps=ref_gps)
        is_dt = isinstance(self.element, PTCDateTime) or self.element.elem_type in ("ptc_datetime", "date_time", "datetime")
        for fd in self.fields:
            if fd.get("key") == "wiu_id":
                fd["choices"] = ([""] + wiu_choices) if is_dt else list(wiu_choices)
            elif fd.get("key") == "radio_id":
                fd["choices"] = list(wiu_choices)

    def _sync_ctc_for_wiu(self, force_auto_fill: bool = False):
        wiu_fd = next((f for f in self.fields if f.get("key") == "wiu_id"), None)
        ctc_fd = next((f for f in self.fields if f.get("key") == "ctc_id"), None)
        if not wiu_fd or not ctc_fd:
            return
        wiu_val = wiu_fd.get("val", "").strip()
        ctc_list = self.wiu_cache.get_ctc_addresses(wiu_val, self.all_elements) if wiu_val else []
        ctc_fd["choices"] = list(ctc_list)
        if ctc_list:
            ctc_fd["tooltip"] = f"Suggested CTC address(es) for WIU {wiu_val}: {', '.join(ctc_list)}"
        else:
            ctc_fd["tooltip"] = "CTC ID string (e.g., C12 or K21). Case-insensitive."

        if self.is_newly_created and not self._ctc_user_modified:
            if ctc_list and (force_auto_fill or not ctc_fd.get("val", "").strip() or self._ctc_auto_populated):
                ctc_fd["val"] = ctc_list[0]
                self._ctc_auto_populated = True
            elif not ctc_list and self._ctc_auto_populated:
                ctc_fd["val"] = ""
                self._ctc_auto_populated = False

    def on_field_changed(self, field_key: str):
        if field_key == "wiu_id":
            self._sync_ctc_for_wiu()
        elif field_key == "ctc_id":
            self._ctc_user_modified = True
            self._ctc_auto_populated = False
        elif field_key == "gps":
            self._refresh_wiu_choices()

    def _track_turnout_ids(self) -> List[str]:
        return sorted(
            eid for eid, el in self.all_elements.items()
            if eid != self.element.id and isinstance(el, (TrackSegment, Turnout))
        )

    def _signal_ids(self) -> List[str]:
        return sorted(
            eid for eid, el in self.all_elements.items()
            if isinstance(el, Signal)
        )

    def _build_fields_from_element(self):
        el = self.element
        ref_gps = self.get_ref_gps_cb() if self.get_ref_gps_cb else None
        if getattr(el, "gps", None):
            ref_gps = _parse_wiu_gps(el.gps) or ref_gps
        wiu_choices = self.wiu_cache.get_suggestion_list(self.all_elements, ref_gps=ref_gps)
        trk_choices = self._track_turnout_ids()
        sig_choices = self._signal_ids()

        gps_str = f"{el.gps[0]:.6f}, {el.gps[1]:.6f}" if el.gps else ""
        mp_str = str(el.mile_marker) if el.mile_marker is not None else ""

        self.fields = [
            {"key": "id", "label": "Element ID *", "val": el.id, "req": True, "choices": [], "tooltip": "Unique identifier for this element (e.g., T1, W1)."},
            {"key": "label", "label": "Display Name", "val": el.label, "req": False, "choices": [], "tooltip": "Optional human-readable display name shown on the panel."},
            {"key": "label_pos", "label": "Label Pos (Wheel)", "val": el.label_pos, "req": True, "choices": LABEL_POSITIONS, "tooltip": "Render location for this element's text tag."},
            {"key": "x", "label": "Logical X *", "val": str(el.x), "req": True, "choices": [], "tooltip": "Logical X coordinate on the grid."},
            {"key": "y", "label": "Logical Y *", "val": str(el.y), "req": True, "choices": [], "tooltip": "Logical Y coordinate on the grid."},
        ]

        if isinstance(el, TrackSegment):
            nbr_parts = []
            for d, nlist in el.neighbors.items():
                for nid in nlist:
                    nbr_parts.append(f"{d}:{nid}")
            self.fields.extend([
                {"key": "orientation", "label": "Orientation (45° Wheel)", "val": el.orientation, "req": True, "choices": TRACK_ORIENTATIONS, "tooltip": "Visual alignment of the track (E-W, N-S, NE-SW, NW-SE)."},
                {"key": "length_miles", "label": "Track Length (Miles)*", "val": str(el.length_miles), "req": True, "choices": ["0.25", "0.5", "0.75", "1.0", "1.5", "2.0", "3.0"], "tooltip": "Length of this segment in miles (scales the drawing)."},
                {"key": "neighbors", "label": "Neighbors (Suggested)", "val": ", ".join(nbr_parts), "req": False, "choices": trk_choices, "tooltip": "Connected adjacent elements in format DIR:ID (e.g., E:T2, W:W1)."},
                {"key": "parallel_track_id", "label": "Parallel Track (Suggested)", "val": el.parallel_track_id or "", "req": False, "choices": [""] + trk_choices, "tooltip": "ID of the adjacent parallel track segment (if any)."},
                {"key": "has_occ_bit", "label": "Has HW Occ Bit (Wheel)", "val": str(el.has_occ_bit), "req": True, "choices": ["False", "True"], "tooltip": "True if this track has a dedicated hardware occupancy bit."},
                {"key": "wiu_id", "label": "WIU ID (if HW Occ)", "val": str(el.wiu_id or ""), "req": False, "choices": wiu_choices, "tooltip": "WIU address transmitting this track's hardware occupancy bit."},
                {"key": "occ_bit_offset", "label": "Occ Bit Offset", "val": str(el.occ_bit_offset if el.occ_bit_offset is not None else ""), "req": False, "choices": [str(i) for i in range(32)], "tooltip": "Bit index (0-31) in the WIU payload for occupancy."},
                {"key": "ctc_id", "label": "CTC ID (Optional)", "val": getattr(el, "ctc_id", "") or "", "req": False, "choices": [], "tooltip": "CTC ID string (e.g., C12 or K21). Case-insensitive."},
                {"key": "ctc_rules", "label": "CTC Rules (Optional)", "val": getattr(el, "ctc_rules", "") or "", "req": False, "choices": [], "tooltip": "CTC rules (e.g., K11 OR C11,K12 OR C12,K25). Case-insensitive."},
            ])

        elif isinstance(el, Turnout):
            self.fields.extend([
                {"key": "wiu_id", "label": "WIU ID *", "val": str(el.wiu_id or ""), "req": True, "choices": wiu_choices, "tooltip": "WIU address transmitting switch state and occupancy."},
                {"key": "cmd_bit_offset", "label": "Switch Bits Offset (2b)*", "val": str(el.cmd_bit_offset if el.cmd_bit_offset is not None else ""), "req": True, "choices": [str(i) for i in range(32)], "tooltip": "Bit index (0-31) in payload for the 2-bit switch alignment (ATCS: 01=Normal, 10=Reverse, 00=Out of corr., 11=Invalid)."},
                {"key": "orientation", "label": "Point Orientation (Wheel)", "val": el.orientation, "req": True, "choices": COMPASS_DIRS, "tooltip": "Direction the points face (where trains approach to diverge)."},
                {"key": "common_neighbor", "label": "Common Leg (Suggested)", "val": el.common_neighbor, "req": False, "choices": trk_choices, "tooltip": "Track ID connected to the base/points end of the switch."},
                {"key": "normal_neighbor", "label": "Normal Leg (Suggested)", "val": el.normal_neighbor, "req": False, "choices": trk_choices, "tooltip": "Track ID for the straight/default path (State 0)."},
                {"key": "normal_dir", "label": "Normal Out Dir (Wheel)", "val": el.normal_dir, "req": True, "choices": COMPASS_DIRS, "tooltip": "Outgoing compass direction when traveling on the Normal leg."},
                {"key": "reverse_neighbor", "label": "Reverse Leg (Suggested)", "val": el.reverse_neighbor, "req": False, "choices": trk_choices, "tooltip": "Track ID for the diverging path (State 1)."},
                {"key": "reverse_dir", "label": "Reverse Out Dir (Wheel)", "val": el.reverse_dir, "req": True, "choices": COMPASS_DIRS, "tooltip": "Outgoing compass direction when traveling on the Reverse leg."},
                {"key": "guarded_by_signals", "label": "Guarding Signals", "val": ", ".join(el.guarded_by_signals), "req": False, "choices": sig_choices, "tooltip": "IDs of signals directly protecting this switch."},
                {"key": "has_occ_bit", "label": "Has HW Occ Bit (Wheel)", "val": str(el.has_occ_bit), "req": True, "choices": ["False", "True"], "tooltip": "True if this switch has a dedicated hardware occupancy bit."},
                {"key": "occ_bit_offset", "label": "Occ Bit Offset (1b)", "val": str(el.occ_bit_offset if el.occ_bit_offset is not None else ""), "req": False, "choices": [str(i) for i in range(32)], "tooltip": "Bit index (0-31) in the payload for hardware occupancy."},
                {"key": "ctc_id", "label": "CTC ID (Optional)", "val": getattr(el, "ctc_id", "") or "", "req": False, "choices": [], "tooltip": "CTC ID string (e.g., C12 or K21). Case-insensitive."},
                {"key": "ctc_rules", "label": "CTC Rules (Optional)", "val": getattr(el, "ctc_rules", "") or "", "req": False, "choices": [], "tooltip": "CTC rules (e.g., K11 OR C11,K12 OR C12,K25). Case-insensitive."},
            ])

        elif isinstance(el, Signal):
            self.fields.extend([
                {"key": "wiu_id", "label": "WIU ID *", "val": str(el.wiu_id or ""), "req": True, "choices": wiu_choices, "tooltip": "WIU address transmitting this signal's 5-bit aspect."},
                {"key": "sig_bit_offset", "label": "Signal Bits Offset (5b)*", "val": str(el.sig_bit_offset if el.sig_bit_offset is not None else ""), "req": True, "choices": [str(i) for i in range(32)], "tooltip": "Starting bit index (0-31) for the 5-bit aspect payload."},
                {"key": "position", "label": "Head Position (Wheel)*", "val": el.position, "req": True, "choices": SIGNAL_POSITIONS, "tooltip": "Physical placement on the mast (top, middle, bottom) for multi-head routing."},
                {"key": "direction", "label": "Gov Direction (Wheel)*", "val": el.direction, "req": True, "choices": COMPASS_DIRS, "tooltip": "Direction of traffic governed by this signal head."},
                {"key": "protects_element", "label": "Protects (Suggested)", "val": el.protects_element, "req": False, "choices": trk_choices, "tooltip": "Track or turnout immediately ahead, protected by this signal."},
                {"key": "rear_element", "label": "Rear Track (Suggested)", "val": el.rear_element, "req": False, "choices": trk_choices, "tooltip": "Track or turnout immediately behind this signal."},
                {"key": "has_occ_bit", "label": "Has HW Occ Bit (Wheel)", "val": str(el.has_occ_bit), "req": True, "choices": ["False", "True"], "tooltip": "True if the signal mast tracks its own OS occupancy."},
                {"key": "occ_bit_offset", "label": "Occ Bit Offset (1b)", "val": str(el.occ_bit_offset if el.occ_bit_offset is not None else ""), "req": False, "choices": [str(i) for i in range(32)], "tooltip": "Bit index for OS hardware occupancy."},
                {"key": "signal_rules", "label": "Signal Rules / RR Override", "val": getattr(el, "signal_rules", "") or "", "req": False, "choices": ["<DEF>", "<BNSF>", "<UP>", "<NS>", "<CSX>"], "tooltip": "Signal table override e.g. <RRR> or 32:\"t:flashing_green,m:magenta,b:black\"."},
                {"key": "ctc_id", "label": "CTC ID (Optional)", "val": getattr(el, "ctc_id", "") or "", "req": False, "choices": [], "tooltip": "CTC ID string (e.g., C12 or K21). Case-insensitive."},
                {"key": "ctc_rules", "label": "CTC Rules (Optional)", "val": getattr(el, "ctc_rules", "") or "", "req": False, "choices": [], "tooltip": "CTC rules (e.g., K11 OR C11,K12 OR C12,K25). Case-insensitive."},
            ])

        elif isinstance(el, Crossing):
            self.fields.extend([
                {"key": "monitored_tracks", "label": "Monitored Tracks (Sugg.)", "val": ", ".join(el.monitored_tracks), "req": False, "choices": trk_choices, "tooltip": "Track IDs that trigger the crossing gates to drop when occupied."},
            ])

        elif isinstance(el, BaseRadio):
            rad_val = str(getattr(el, "radio_id", "") if getattr(el, "radio_id", None) is not None else "")
            self.fields.extend([
                {"key": "channel", "label": "Radio Channel (Wheel)*", "val": str(el.channel), "req": True, "choices": [str(i) for i in range(1, 10)], "tooltip": "RF Channel (1-9) this base station operates on."},
                {"key": "radio_id", "label": "Radio ID (Node ID)", "val": rad_val, "req": False, "choices": wiu_choices, "tooltip": "Radio / Node ID number for this base station."},
            ])

        elif isinstance(el, TrackJump):
            jump_choices = sorted(
                eid for eid, o in self.all_elements.items()
                if eid != el.id and isinstance(o, TrackJump)
            )
            letter_choices = [chr(ord("A") + i) for i in range(26)]
            self.fields.extend([
                {"key": "letter", "label": "Connector Letter (Wheel)*", "val": str(el.letter or "A").upper(), "req": True, "choices": letter_choices, "tooltip": "Connector letter (e.g. A, B, C) shared between previous line and next line."},
                {"key": "connected_element", "label": "Connected Track/Turnout *", "val": str(el.connected_element or ""), "req": True, "choices": trk_choices, "tooltip": "Track or Turnout ID attached to this line-break connector."},
                {"key": "direction", "label": "Continue Dir (Wheel)*", "val": str(el.direction or "E").upper(), "req": True, "choices": COMPASS_DIRS, "tooltip": "Compass direction traffic continues when emerging from this connector."},
                {"key": "paired_jump_id", "label": "Paired Jump ID (Auto)", "val": str(el.paired_jump_id or ""), "req": False, "choices": jump_choices, "tooltip": "Partner TrackJump element ID with the same connector letter on the next/previous line."},
            ])

        elif el.elem_type in SYSTEM_LIST_TYPES:
            max_l = getattr(el, "extra_info", "") or ("12" if el.elem_type == "chat" else "10")
            def_w = getattr(el, "width", 0) or (580 if el.elem_type == "packet_scroller" else (450 if el.elem_type == "address_table" else (460 if el.elem_type == "chat" else 430)))
            def_h = getattr(el, "height", 0) or (195 if el.elem_type == "packet_scroller" else (220 if el.elem_type == "chat" else 150))
            self.fields.extend([
                {"key": "extra_info", "label": "Max List Rows (Wheel)", "val": str(max_l), "req": True, "choices": ["5", "8", "10", "12", "15", "20"], "tooltip": "Maximum number of rows/entries to display in this list."},
                {"key": "width", "label": "Default Width px (Wheel)*", "val": str(def_w), "req": True, "choices": ["360", "430", "450", "520", "580", "650", "750", "850"], "tooltip": "Default width in pixels for this informational panel in the Viewer."},
                {"key": "height", "label": "Default Height px (Wheel)*", "val": str(def_h), "req": True, "choices": ["120", "150", "180", "195", "240", "300", "360", "420"], "tooltip": "Default height in pixels for this informational panel in the Viewer."},
            ])

        elif isinstance(el, PTCDateTime) or el.elem_type in ("ptc_datetime", "date_time", "datetime"):
            def_w = getattr(el, "width", 0) or 240
            def_h = getattr(el, "height", 0) or 50
            wiu_val = str(el.wiu_id or "")
            self.fields.extend([
                {"key": "wiu_id", "label": "Filter WIU ID (Optional)", "val": wiu_val, "req": False, "choices": [""] + wiu_choices, "tooltip": "Optional WIU/Node ID to filter Date/Time packets from (leave empty for any node)."},
                {"key": "width", "label": "Width px (Wheel)*", "val": str(def_w), "req": True, "choices": ["180", "220", "240", "280", "320"], "tooltip": "Width in pixels for this clock element."},
                {"key": "height", "label": "Height px (Wheel)*", "val": str(def_h), "req": True, "choices": ["36", "46", "50", "60", "72"], "tooltip": "Height in pixels for this clock element."},
            ])

        if el.elem_type not in SYSTEM_LIST_TYPES:
            self.fields.extend([
                {"key": "mile_marker", "label": "Mile Marker (MP)", "val": mp_str, "req": False, "choices": [], "tooltip": "Railroad milepost location (e.g., 12.5)."},
                {"key": "gps", "label": "GPS (lat, lon)", "val": gps_str, "req": False, "choices": [], "tooltip": "Physical geographic coordinates (Latitude, Longitude)."},
            ])

        self._sync_ctc_for_wiu()
        self._sync_ctc_for_wiu(force_auto_fill=bool(self.is_newly_created and not (getattr(el, "ctc_id", "") or "").strip()))

    def select_field(self, idx: int):
        if idx == self.active_field_idx:
            self.select_all_active = False
        else:
            self.active_field_idx = idx
            self.select_all_active = True

    def current_field(self) -> Dict[str, Any]:
        return self.fields[self.active_field_idx]

    def get_filtered_suggestions(self, fdict: Dict[str, Any]) -> List[str]:
        choices = fdict.get("choices", [])
        if not choices:
            return []
        raw_val = fdict["val"].strip()
        last_tok = raw_val.split(",")[-1].strip()
        if ":" in last_tok:
            last_tok = last_tok.split(":")[-1].strip()
        if not last_tok:
            return choices[:8]
        matches = [c for c in choices if c.lower().startswith(last_tok.lower()) and c != last_tok]
        if not matches:
            matches = [c for c in choices if last_tok.lower() in c.lower() and c != last_tok]
        return matches[:8]

    def cycle_field_by_wheel(self, field_idx: int, direction: int):
        fdict = self.fields[field_idx]
        choices = fdict.get("choices", [])
        if not choices or len(choices) > 24:
            return
        cur_val = fdict["val"].strip()
        try:
            idx = choices.index(cur_val)
            new_idx = (idx + direction) % len(choices)
        except ValueError:
            new_idx = 0 if direction > 0 else len(choices) - 1
        fdict["val"] = choices[new_idx]
        self.select_all_active = False
        self.on_field_changed(fdict["key"])

    def accept_suggestion(self, suggestion: str):
        fdict = self.current_field()
        raw_val = fdict["val"]
        if "," in raw_val:
            parts = [p.strip() for p in raw_val.split(",")[:-1]]
            last_part = raw_val.split(",")[-1].strip()
            if ":" in last_part:
                dir_prefix = last_part.split(":")[0].strip()
                parts.append(f"{dir_prefix}:{suggestion}")
            else:
                parts.append(suggestion)
            fdict["val"] = ", ".join(parts)
        elif ":" in raw_val and fdict["key"] == "neighbors":
            dir_prefix = raw_val.split(":")[0].strip()
            fdict["val"] = f"{dir_prefix}:{suggestion}"
        else:
            fdict["val"] = suggestion
        self.select_all_active = False
        self.on_field_changed(fdict["key"])

    def get_initial_gps(self) -> Tuple[float, float]:
        start_lat, start_lon = self.get_last_gps_cb()
        for fd in self.fields:
            if fd["key"] == "gps" and fd["val"].strip():
                parts = [p.strip() for p in fd["val"].strip("()[] ").split(",")]
                if len(parts) == 2:
                    try:
                        return float(parts[0]), float(parts[1])
                    except ValueError:
                        pass
                break
        if self.element.gps:
            return float(self.element.gps[0]), float(self.element.gps[1])
        return float(start_lat), float(start_lon)

    def apply_picked_gps(self, lat_v: float, lon_v: float):
        if -90.0 <= lat_v <= 90.0 and -180.0 <= lon_v <= 180.0:
            for fd in self.fields:
                if fd["key"] == "gps":
                    fd["val"] = f"{lat_v:.6f}, {lon_v:.6f}"
            self.element.gps = (lat_v, lon_v)
            self.set_last_gps_cb((lat_v, lon_v))
            self._refresh_wiu_choices()
            self.validation_error = f"GPS set & remembered: ({lat_v:.6f}, {lon_v:.6f})"

    def invoke_gps_selector(self):
        base_dirs = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]
        script_candidates = []
        for d in base_dirs:
            script_candidates.append(os.path.join(d, "gps_selector.py"))
            script_candidates.append(os.path.join(d, "gps_select.py"))
        script_path = next((p for p in script_candidates if os.path.exists(p)), None)
        if not script_path:
            self.validation_error = "Neither gps_selector.py nor gps_select.py found in working directory."
            return

        start_lat, start_lon = self.get_last_gps_cb()
        for fd in self.fields:
            if fd["key"] == "gps" and fd["val"].strip():
                parts = [p.strip() for p in fd["val"].strip("()[] ").split(",")]
                if len(parts) == 2:
                    try:
                        start_lat, start_lon = float(parts[0]), float(parts[1])
                    except ValueError:
                        pass
                break
        else:
            if self.element.gps:
                start_lat, start_lon = self.element.gps[0], self.element.gps[1]

        cmd = [
            sys.executable,
            "-u",
            script_path,
            "--lat", str(start_lat),
            "--lon", str(start_lon),
            "--radius", str(self.default_radius),
        ]

        child_env = os.environ.copy()
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env.pop("SDL_WINDOWID", None)
        child_env.pop("SDL_VIDEO_WINDOW_POS", None)

        pygame.event.pump()
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                env=child_env,
                cwd=os.path.dirname(script_path) or os.getcwd(),
                timeout=300,
            )
            pygame.event.clear()

            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            matches = re.findall(r"([-+]?\d{1,2}\.\d+)\s*[,;\s]\s*([-+]?\d{1,3}\.\d+)", out)
            if matches:
                lat_s, lon_s = matches[-1]
                lat_v, lon_v = float(lat_s), float(lon_s)
                if -90.0 <= lat_v <= 90.0 and -180.0 <= lon_v <= 180.0:
                    for fd in self.fields:
                        if fd["key"] == "gps":
                            fd["val"] = f"{lat_v:.6f}, {lon_v:.6f}"
                    self.element.gps = (lat_v, lon_v)
                    self.set_last_gps_cb((lat_v, lon_v))
                    self._refresh_wiu_choices()
                    self.validation_error = f"GPS set & remembered for next pick: ({lat_v:.5f}, {lon_v:.5f})"
                    return
            rc_info = f" (rc={proc.returncode})" if proc.returncode != 0 else ""
            self.validation_error = f"GPS selector exited without valid coordinates{rc_info}."
        except Exception as exc:
            pygame.event.clear()
            self.validation_error = f"GPS selector error: {exc}"

    def validate_and_apply(self, force_apply: bool = False) -> bool:
        vals = {fd["key"]: fd["val"].strip() for fd in self.fields}

        if not force_apply:
            for fd in self.fields:
                if fd["req"] and not fd["val"].strip():
                    self.validation_error = f"Mandatory field '{fd['label']}' cannot be empty!"
                    return False

        new_id = vals.get("id", "").strip()
        if not new_id:
            self.validation_error = "Element ID cannot be empty."
            return False
        if new_id != self.orig_id and new_id in self.all_elements:
            self.validation_error = f"Element ID '{new_id}' already exists in layout!"
            return False

        el = self.element
        try:
            el.x = int(round(float(vals.get("x", el.x))))
            el.y = int(round(float(vals.get("y", el.y))))
        except ValueError:
            if not force_apply:
                self.validation_error = "Logical X and Y must be numeric."
                return False

        el.label = vals.get("label", "")
        el.label_pos = vals.get("label_pos", "top").lower()

        mp_raw = vals.get("mile_marker", "")
        if mp_raw:
            try:
                el.mile_marker = round(float(mp_raw), 2)
            except ValueError:
                if not force_apply:
                    self.validation_error = "Mile Marker must be a valid decimal number."
                    return False
        else:
            el.mile_marker = None

        gps_raw = vals.get("gps", "").strip("()[] ")
        if gps_raw:
            parts = [p.strip() for p in gps_raw.split(",")]
            if len(parts) == 2:
                try:
                    lat_v, lon_v = float(parts[0]), float(parts[1])
                    el.gps = (lat_v, lon_v)
                    self.set_last_gps_cb((lat_v, lon_v))
                except ValueError:
                    if not force_apply:
                        self.validation_error = "GPS must be formatted as: lat, lon"
                        return False
            elif not force_apply:
                self.validation_error = "GPS must be two comma-separated decimals: lat, lon"
                return False
        else:
            el.gps = None

        if isinstance(el, TrackSegment):
            el.orientation = vals.get("orientation", "E-W")
            if el.orientation not in TRACK_ORIENTATIONS:
                el.orientation = "E-W"
            try:
                el.length_miles = max(0.05, round(float(vals.get("length_miles", "0.5")), 2))
            except ValueError:
                if not force_apply:
                    self.validation_error = "Track Length (Miles) must be a positive number."
                    return False
                el.length_miles = 0.5

            el.parallel_track_id = vals.get("parallel_track_id") or None
            el.has_occ_bit = vals.get("has_occ_bit", "False").lower() == "true"

            wiu_raw = vals.get("wiu_id", "")
            occ_raw = vals.get("occ_bit_offset", "")
            if el.has_occ_bit and not force_apply and (not wiu_raw or not occ_raw):
                self.validation_error = "Track with Has HW Occ Bit=True requires WIU ID and Occ Bit Offset!"
                return False

            el.wiu_id = int(wiu_raw) if wiu_raw.isdigit() else None
            el.occ_bit_offset = int(occ_raw) if occ_raw.isdigit() else None

            parsed_nbrs: Dict[str, List[str]] = {}
            for tok in [t.strip() for t in vals.get("neighbors", "").split(",") if t.strip()]:
                if ":" in tok:
                    d_part, id_part = [x.strip() for x in tok.split(":", 1)]
                    d_up = d_part.upper()
                    if d_up in COMPASS_DIRS and id_part:
                        parsed_nbrs.setdefault(d_up, []).append(next((k for k in self.all_elements if k.lower() == id_part.lower()), id_part))
                else:
                    other = self.all_elements.get(tok)
                    if other:
                        cdir = LayoutTopologyAssistant.vector_to_compass(other.x - el.x, other.y - el.y)
                    else:
                        cdir = "E"
                    parsed_nbrs.setdefault(cdir, []).append(next((k for k in self.all_elements if k.lower() == tok.lower()), tok))
            el.neighbors = parsed_nbrs
            el.ctc_id = vals.get("ctc_id", "").strip()
            el.ctc_rules = vals.get("ctc_rules", "").strip()

        elif isinstance(el, Turnout):
            wiu_raw = vals.get("wiu_id", "")
            cmd_raw = vals.get("cmd_bit_offset", "")
            if not force_apply and (not wiu_raw.isdigit() or not cmd_raw.isdigit()):
                self.validation_error = "Turnout requires numeric WIU ID and Switch Bits Offset (cmd_bit_offset)!"
                return False

            el.wiu_id = int(wiu_raw) if wiu_raw.isdigit() else None
            el.cmd_bit_offset = int(cmd_raw) if cmd_raw.isdigit() else 0
            el.orientation = vals.get("orientation", "E").upper()
            el.common_neighbor = vals.get("common_neighbor", "")
            el.normal_neighbor = vals.get("normal_neighbor", "")
            el.normal_dir = vals.get("normal_dir", "E").upper()
            el.reverse_neighbor = vals.get("reverse_neighbor", "")
            el.reverse_dir = vals.get("reverse_dir", "NE").upper()
            el.guarded_by_signals = [s.strip() for s in vals.get("guarded_by_signals", "").split(",") if s.strip()]
            el.has_occ_bit = vals.get("has_occ_bit", "False").lower() == "true"
            occ_raw = vals.get("occ_bit_offset", "")
            if el.has_occ_bit and not force_apply and not occ_raw.isdigit():
                self.validation_error = "Turnout with Has HW Occ Bit=True requires Occ Bit Offset!"
                return False
            el.occ_bit_offset = int(occ_raw) if occ_raw.isdigit() else None
            el.ctc_id = vals.get("ctc_id", "").strip()
            el.ctc_rules = vals.get("ctc_rules", "").strip()

        elif isinstance(el, Signal):
            wiu_raw = vals.get("wiu_id", "")
            sig_raw = vals.get("sig_bit_offset", "")
            if not force_apply and (not wiu_raw.isdigit() or not sig_raw.isdigit()):
                self.validation_error = "Signal requires numeric WIU ID and 5-bit Signal Offset (sig_bit_offset)!"
                return False

            el.wiu_id = int(wiu_raw) if wiu_raw.isdigit() else None
            el.sig_bit_offset = int(sig_raw) if sig_raw.isdigit() else 0
            el.position = vals.get("position", "top").lower()
            el.direction = vals.get("direction", "E").upper()
            el.protects_element = vals.get("protects_element", "")
            el.rear_element = vals.get("rear_element", "")
            el.has_occ_bit = vals.get("has_occ_bit", "False").lower() == "true"
            occ_raw = vals.get("occ_bit_offset", "")
            el.occ_bit_offset = int(occ_raw) if occ_raw.isdigit() else None
            el.signal_rules = vals.get("signal_rules", "").strip()
            el.ctc_id = vals.get("ctc_id", "").strip()
            el.ctc_rules = vals.get("ctc_rules", "").strip()

        elif isinstance(el, Crossing):
            tracks = [t.strip() for t in vals.get("monitored_tracks", "").split(",") if t.strip()]
            el.monitored_tracks = tracks

        elif isinstance(el, BaseRadio):
            ch_raw = vals.get("channel", "1")
            el.channel = int(ch_raw) if ch_raw.isdigit() else 1
            rad_raw = str(vals.get("radio_id", "")).strip()
            el.radio_id = int(rad_raw) if (rad_raw and rad_raw.isdigit()) else None

        elif isinstance(el, TrackJump):
            el.letter = (vals.get("letter", "A").strip().upper() or "A")
            el.connected_element = vals.get("connected_element", "").strip()
            el.direction = (vals.get("direction", "E").strip().upper() or "E")
            if el.direction not in COMPASS_DIRS:
                el.direction = "E"
            el.paired_jump_id = vals.get("paired_jump_id", "").strip()
            if not el.paired_jump_id:
                for other in self.all_elements.values():
                    if isinstance(other, TrackJump) and other.id != el.id and (other.letter or "").strip().upper() == el.letter:
                        el.paired_jump_id = other.id
                        other.paired_jump_id = el.id
                        break

        elif isinstance(el, DisplayElement) and el.elem_type in SYSTEM_LIST_TYPES:
            el.extra_info = vals.get("extra_info", "12" if el.elem_type == "chat" else "10")
            w_raw = vals.get("width", "")
            h_raw = vals.get("height", "")
            def_w = 580 if el.elem_type == "packet_scroller" else (450 if el.elem_type == "address_table" else (460 if el.elem_type == "chat" else 430))
            def_h = 195 if el.elem_type == "packet_scroller" else (220 if el.elem_type == "chat" else 150)
            el.width = max(240, int(w_raw)) if w_raw.isdigit() else def_w
            el.height = max(80, int(h_raw)) if h_raw.isdigit() else def_h

        elif isinstance(el, PTCDateTime) or el.elem_type in ("ptc_datetime", "date_time", "datetime"):
            w_raw = vals.get("width", "")
            h_raw = vals.get("height", "")
            el.width = max(120, int(w_raw)) if w_raw.isdigit() else 240
            el.height = max(30, int(h_raw)) if h_raw.isdigit() else 50
            wiu_raw = vals.get("wiu_id", "")
            el.wiu_id = int(wiu_raw) if (wiu_raw and wiu_raw.isdigit()) else None

        if new_id != self.orig_id:
            self._rename_element_references(self.orig_id, new_id)
            el.id = new_id
            self.orig_id = new_id

        if el.wiu_id is not None:
            self.wiu_cache.register_wiu(
                el.wiu_id,
                name=getattr(el, "label", ""),
                gps=getattr(el, "gps", None),
                ctc_addresses=getattr(el, "ctc_id", ""),
            )
            self.wiu_cache.save_cache()

        self.validation_error = ""
        return True

    def _rename_element_references(self, old_id: str, new_id: str):
        if old_id in self.all_elements:
            obj = self.all_elements.pop(old_id)
            self.all_elements[new_id] = obj
        for other in self.all_elements.values():
            if isinstance(other, TrackSegment):
                for d, nlist in other.neighbors.items():
                    other.neighbors[d] = [new_id if x == old_id else x for x in nlist]
                if other.parallel_track_id == old_id:
                    other.parallel_track_id = new_id
            elif isinstance(other, Turnout):
                if other.common_neighbor == old_id:
                    other.common_neighbor = new_id
                if other.normal_neighbor == old_id:
                    other.normal_neighbor = new_id
                if other.reverse_neighbor == old_id:
                    other.reverse_neighbor = new_id
                other.guarded_by_signals = [new_id if x == old_id else x for x in other.guarded_by_signals]
            elif isinstance(other, Signal):
                if other.protects_element == old_id:
                    other.protects_element = new_id
                if other.rear_element == old_id:
                    other.rear_element = new_id
            elif isinstance(other, Crossing):
                other.monitored_tracks = [new_id if x == old_id else x for x in other.monitored_tracks]
            elif isinstance(other, TrackJump):
                if other.connected_element == old_id:
                    other.connected_element = new_id
                if other.paired_jump_id == old_id:
                    other.paired_jump_id = new_id

    def draw(self, screen: pygame.Surface, font_sm: pygame.font.Font, font_md: pygame.font.Font):
        sw, sh = screen.get_size()
        row_h = 26
        title = f"EDIT {self.element.elem_type.upper()}: {self.element.id}  (Tab/Wheel=Choices, Enter=Apply)"

        max_lbl_w = max((font_sm.size(fd["label"])[0] for fd in self.fields), default=160)
        self.lbl_col_w = max(195, max_lbl_w + 20)

        max_val_w = max((font_sm.size(fd["val"] + "   ")[0] for fd in self.fields), default=240)
        self.box_w = max(250, max_val_w + 24)

        title_w = font_md.size(title)[0] + 28
        err_w = (font_sm.size(self.validation_error)[0] + 28) if self.validation_error else 0

        # Raw Packet Hex String formatting (small and broken up into 16-byte lines)
        raw_hex = getattr(self.element, "_last_raw_hex", "") or getattr(self.element, "last_raw_hex", "") or ""
        hex_lines = format_hex_lines(raw_hex, bytes_per_line=16) if raw_hex else []
        hex_display_lines = hex_lines if hex_lines else ["(No raw packet received for this element yet)"]
        hex_line_h = 16
        hex_inner_h = max(24, len(hex_display_lines) * hex_line_h + 6)
        hex_section_h = 24 + hex_inner_h + 8

        self.panel_w = min(sw - 24, max(510, self.lbl_col_w + self.box_w + 28, title_w, err_w))
        self.box_w = self.panel_w - self.lbl_col_w - 24
        self.panel_h = 84 + len(self.fields) * row_h + hex_section_h + (28 if self.validation_error else 0)

        if self.px is None or self.py is None:
            self.px = min(max(12, self.screen_pos[0] + 30), max(12, sw - self.panel_w - 12))
            self.py = min(max(56, self.screen_pos[1] + 15), max(56, sh - self.panel_h - 42))

        self.px = min(max(12, self.px), max(12, sw - self.panel_w - 12))
        self.py = min(max(56, self.py), max(56, sh - self.panel_h - 42))

        self.panel_rect = pygame.Rect(self.px, self.py, self.panel_w, self.panel_h)

        surf = pygame.Surface((self.panel_w, self.panel_h), pygame.SRCALPHA)
        surf.fill((18, 24, 34, 190))
        pygame.draw.rect(surf, COLOR_SELECT_RING, (0, 0, self.panel_w, self.panel_h), 2, border_radius=7)
        screen.blit(surf, (self.px, self.py))

        screen.blit(font_md.render(title, True, COLOR_EDIT_ACCENT), (self.px + 12, self.py + 8))

        self.field_rects.clear()
        for idx, fd in enumerate(self.fields):
            fy = self.py + 34 + idx * row_h
            lbl_col = COLOR_EDIT_ACCENT if fd["req"] else COLOR_TEXT_DIM
            screen.blit(font_sm.render(fd["label"], True, lbl_col), (self.px + 12, fy + 4))

            box_rect = pygame.Rect(self.px + self.lbl_col_w, fy, self.box_w, row_h - 3)
            self.field_rects.append(box_rect)
            is_act = (idx == self.active_field_idx)
            bg_c = (36, 48, 68) if is_act else (25, 32, 44)
            bd_c = COLOR_SELECT_RING if is_act else COLOR_HUD_BORDER
            pygame.draw.rect(screen, bg_c, box_rect, border_radius=4)
            pygame.draw.rect(screen, bd_c, box_rect, 1, border_radius=4)

            val_str = fd["val"]
            if is_act and self.select_all_active and val_str:
                txt_surf = font_sm.render(val_str, True, (255, 255, 255))
                sel_rect = pygame.Rect(
                    box_rect.x + 5,
                    box_rect.y + 3,
                    min(box_rect.width - 10, txt_surf.get_width() + 4),
                    row_h - 9,
                )
                pygame.draw.rect(screen, COLOR_TEXT_SEL_BG, sel_rect, border_radius=2)
                screen.blit(txt_surf, (box_rect.x + 6, box_rect.y + 4))
            else:
                val_txt = val_str + ("|" if is_act else "")
                screen.blit(font_sm.render(val_txt, True, (255, 255, 255)), (box_rect.x + 6, box_rect.y + 4))

        # Draw Last Raw Packet Hex section with [Copy Hex]
        hex_y = self.py + 36 + len(self.fields) * row_h
        screen.blit(font_sm.render("Last Raw Packet Hex:", True, COLOR_EDIT_ACCENT), (self.px + 12, hex_y + 2))
        copy_w = 80
        copy_rect = pygame.Rect(self.px + self.panel_w - copy_w - 12, hex_y - 2, copy_w, 22)
        self.btn_rects["copy_hex"] = copy_rect
        copy_bg = (35, 95, 140) if raw_hex else (42, 48, 60)
        pygame.draw.rect(screen, copy_bg, copy_rect, border_radius=4)
        pygame.draw.rect(screen, COLOR_HUD_BORDER, copy_rect, 1, border_radius=4)
        cp_txt = font_sm.render("Copy Hex", True, (255, 255, 255) if raw_hex else (130, 140, 155))
        screen.blit(cp_txt, (copy_rect.x + (copy_w - cp_txt.get_width()) // 2, copy_rect.y + 3))

        # Inset container for small broken-up hex lines
        box_y = hex_y + 24
        box_w_hex = self.panel_w - 24
        box_rect = pygame.Rect(self.px + 12, box_y, box_w_hex, hex_inner_h)
        pygame.draw.rect(screen, (12, 16, 24), box_rect, border_radius=4)
        pygame.draw.rect(screen, COLOR_HUD_BORDER, box_rect, 1, border_radius=4)

        for l_idx, h_line in enumerate(hex_display_lines):
            line_y = box_y + 3 + l_idx * hex_line_h
            line_col = (110, 245, 175) if raw_hex else COLOR_TEXT_DIM
            screen.blit(font_sm.render(h_line, True, line_col), (box_rect.x + 8, line_y))

        btn_y = box_y + hex_inner_h + 10
        self.btn_rects.clear()
        self.btn_rects["copy_hex"] = copy_rect
        if isinstance(self.element, TrackSegment):
            btns = [
                ("apply", "Apply(Enter)", self.px + 12, 98, (35, 130, 65)),
                ("auto_adj", "Suggest(A)", self.px + 116, 92, (45, 90, 150)),
                ("split", "Split(X)", self.px + 214, 78, (175, 95, 30)),
                ("gps", "GPS(G)", self.px + 298, 76, (120, 75, 165)),
                ("cancel", "Cancel(Esc)", self.px + 380, 96, (135, 45, 45)),
            ]
        else:
            btns = [
                ("apply", "Apply (Enter)", self.px + 12, 115, (35, 130, 65)),
                ("auto_adj", "Suggest (A)", self.px + 135, 110, (45, 90, 150)),
                ("gps", "Pick GPS (G)", self.px + 253, 105, (120, 75, 165)),
                ("cancel", "Cancel (Esc)", self.px + 366, 92, (135, 45, 45)),
            ]
        for bkey, btxt, bx, bw, bcol in btns:
            r = pygame.Rect(bx, btn_y, bw, 26)
            self.btn_rects[bkey] = r
            pygame.draw.rect(screen, bcol, r, border_radius=4)
            pygame.draw.rect(screen, COLOR_HUD_BORDER, r, 1, border_radius=4)
            ts = font_sm.render(btxt, True, (255, 255, 255))
            screen.blit(ts, (bx + (bw - ts.get_width()) // 2, btn_y + 6))

        if self.validation_error:
            err_surf = font_sm.render(self.validation_error, True, COLOR_ERROR_RED)
            screen.blit(err_surf, (self.px + 12, btn_y + 30))

        self.suggestion_rects.clear()
        act_fd = self.current_field()
        suggs = self.get_filtered_suggestions(act_fd)
        if suggs and self.active_field_idx < len(self.field_rects):
            anchor_r = self.field_rects[self.active_field_idx]
            max_sug_w = max((font_sm.size(f"[Tab] {s}")[0] + 16 for s in suggs), default=self.box_w)
            drop_w = max(self.box_w, max_sug_w)
            drop_h = len(suggs) * 20 + 4
            drop_x = min(sw - drop_w - 10, anchor_r.x)
            drop_y = min(sh - drop_h - 10, anchor_r.bottom + 2)
            pygame.draw.rect(screen, (28, 38, 54), (drop_x, drop_y, drop_w, drop_h), border_radius=4)
            pygame.draw.rect(screen, COLOR_EDIT_ACCENT, (drop_x, drop_y, drop_w, drop_h), 1, border_radius=4)
            for s_idx, s_val in enumerate(suggs):
                sr = pygame.Rect(drop_x + 2, drop_y + 2 + s_idx * 20, drop_w - 4, 19)
                self.suggestion_rects.append((sr, s_val))
                prefix = "[Tab] " if s_idx == 0 else "      "
                col = COLOR_EDIT_ACCENT if s_idx == 0 else COLOR_TEXT
                screen.blit(font_sm.render(f"{prefix}{s_val}", True, col), (sr.x + 4, sr.y + 2))
        elif (
            act_fd.get("key") == "ctc_id"
            and act_fd.get("choices")
            and self.active_field_idx < len(self.field_rects)
        ):
            alt_choices = act_fd["choices"][:8]
            anchor_r = self.field_rects[self.active_field_idx]
            max_sug_w = max((font_sm.size(f"[Opt] {s}")[0] + 16 for s in alt_choices), default=self.box_w)
            drop_w = max(self.box_w, max_sug_w)
            drop_h = len(alt_choices) * 20 + 4
            drop_x = min(sw - drop_w - 10, anchor_r.x)
            drop_y = min(sh - drop_h - 10, anchor_r.bottom + 2)
            pygame.draw.rect(screen, (28, 38, 54), (drop_x, drop_y, drop_w, drop_h), border_radius=4)
            pygame.draw.rect(screen, COLOR_EDIT_ACCENT, (drop_x, drop_y, drop_w, drop_h), 1, border_radius=4)
            cur_ctc = act_fd.get("val", "").strip().lower()
            for s_idx, s_val in enumerate(alt_choices):
                sr = pygame.Rect(drop_x + 2, drop_y + 2 + s_idx * 20, drop_w - 4, 19)
                self.suggestion_rects.append((sr, s_val))
                is_cur = (s_val.strip().lower() == cur_ctc)
                prefix = "[Sel] " if is_cur else "[Opt] "
                col = COLOR_EDIT_ACCENT if is_cur else COLOR_TEXT
                screen.blit(font_sm.render(f"{prefix}{s_val}", True, col), (sr.x + 4, sr.y + 2))

        # Tooltips overlay
        mx, my = pygame.mouse.get_pos()
        in_sugg = any(sr.collidepoint(mx, my) for sr, _ in self.suggestion_rects)
        if not in_sugg and getattr(self, 'panel_rect', None) and self.panel_rect.collidepoint(mx, my):
            for idx, fd in enumerate(self.fields):
                tt = fd.get("tooltip")
                if tt:
                    row_rect = pygame.Rect(self.px + 12, self.py + 34 + idx * row_h, self.panel_w - 24, row_h)
                    if row_rect.collidepoint(mx, my):
                        tt_surf = font_sm.render(tt, True, (255, 255, 255))
                        tt_bg = pygame.Surface((tt_surf.get_width() + 10, tt_surf.get_height() + 6))
                        tt_bg.fill((30, 40, 55))
                        pygame.draw.rect(tt_bg, COLOR_EDIT_ACCENT, tt_bg.get_rect(), 1)
                        tt_bg.blit(tt_surf, (5, 3))
                        screen.blit(tt_bg, (mx + 12, my + 15))
                        break


def render_fragment_thumbnail(elements: List[Any], size: Tuple[int, int] = (220, 140)) -> pygame.Surface:
    tw, th = size
    surf = pygame.Surface((tw, th))
    surf.fill((14, 18, 26))

    pts: List[Tuple[float, float]] = []
    track_endpoints: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {}
    elem_dict_map: Dict[str, dict] = {}

    for el in elements:
        d = el if isinstance(el, dict) else el.to_config_dict()
        eid = str(d.get("id", ""))
        elem_dict_map[eid] = d
        etype = d.get("elem_type", "track")
        lx = float(d.get("x", 0))
        ly = float(d.get("y", 0))
        pts.append((lx, ly))
        if etype == "track":
            ep0, ep1 = LayoutTopologyAssistant.get_track_endpoints_logical(d)
            track_endpoints[eid] = (ep0, ep1)
            pts.extend([ep0, ep1])

    if not pts:
        return surf

    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)

    if max_x == min_x: max_x += 2; min_x -= 2
    if max_y == min_y: max_y += 2; min_y -= 2

    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0

    scale_x = (tw - 36) / span_x
    scale_y = (th - 36) / span_y
    scale = min(scale_x, scale_y)

    def to_s(lx: float, ly: float) -> Tuple[int, int]:
        return int(tw / 2.0 + (lx - cx) * scale), int(th / 2.0 + (ly - cy) * scale)

    line_w = max(2, min(5, int(scale * 0.35)))

    # Subtle inner border
    pygame.draw.rect(surf, (35, 46, 64), (0, 0, tw, th), 1)

    # 1. Turnout ties/links to neighbors
    for eid, d in elem_dict_map.items():
        if d.get("elem_type") == "turnout":
            t_log = (float(d.get("x", 0)), float(d.get("y", 0)))
            ts = to_s(*t_log)
            for leg_key in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                nid = str(d.get(leg_key, ""))
                if nid and nid in elem_dict_map:
                    nbr = elem_dict_map[nid]
                    target_pt = (float(nbr.get("x", 0)), float(nbr.get("y", 0)))
                    if nid in track_endpoints:
                        p0, p1 = track_endpoints[nid]
                        target_pt = min((p0, p1), key=lambda p: (p[0] - t_log[0])**2 + (p[1] - t_log[1])**2)
                    ns = to_s(*target_pt)
                    pygame.draw.line(surf, COLOR_TURNOUT_TIE, ts, ns, max(1, line_w - 1))

    # 2. Track bars & inter-track links
    drawn_pairs: Set[Tuple[str, str]] = set()
    for eid, (ep0, ep1) in track_endpoints.items():
        s0 = to_s(*ep0)
        s1 = to_s(*ep1)
        pygame.draw.line(surf, COLOR_TRACK_WHITE, s0, s1, line_w)
        d = elem_dict_map[eid]
        nbrs = [str(nid) for nlist in d.get("neighbors", {}).values() for nid in (nlist if isinstance(nlist, list) else [nlist])]
        for nid in nbrs:
            pair = tuple(sorted((eid, nid)))
            if pair in drawn_pairs or nid not in track_endpoints:
                continue
            drawn_pairs.add(pair)
            np0, np1 = track_endpoints[nid]
            best_pair = min(
                ((a, b) for a in (ep0, ep1) for b in (np0, np1)),
                key=lambda ab: (ab[0][0] - ab[1][0])**2 + (ab[0][1] - ab[1][1])**2
            )
            pygame.draw.line(surf, COLOR_TRACK_WHITE, to_s(*best_pair[0]), to_s(*best_pair[1]), line_w)

    # 3. Non-track elements
    for eid, d in elem_dict_map.items():
        etype = d.get("elem_type", "track")
        lx, ly = float(d.get("x", 0)), float(d.get("y", 0))
        sx, sy = to_s(lx, ly)
        if etype == "turnout":
            sz = max(4, min(8, int(scale * 0.35)))
            pts_dia = [(sx, sy - sz), (sx + sz, sy), (sx, sy + sz), (sx - sz, sy)]
            pygame.draw.polygon(surf, (255, 185, 45), pts_dia)
        elif etype == "signal":
            rad = max(3, min(6, int(scale * 0.28)))
            pygame.draw.circle(surf, (40, 230, 95), (sx, sy), rad)
            pygame.draw.circle(surf, (180, 195, 215), (sx, sy), rad + 1, 1)
        elif etype == "crossing":
            csz = max(4, min(8, int(scale * 0.35)))
            pygame.draw.line(surf, COLOR_CROSSING_OPEN, (sx - csz, sy - csz), (sx + csz, sy + csz), 2)
            pygame.draw.line(surf, COLOR_CROSSING_OPEN, (sx - csz, sy + csz), (sx + csz, sy - csz), 2)
        elif etype == "base":
            bsz = max(4, min(8, int(scale * 0.35)))
            bpts = [(sx, sy - bsz), (sx - bsz, sy + bsz), (sx + bsz, sy + bsz)]
            pygame.draw.polygon(surf, (195, 155, 255), bpts, 2)
        elif etype == "track_jump":
            rad = max(4, min(8, int(scale * 0.32)))
            pygame.draw.circle(surf, (255, 215, 50), (sx, sy), rad, 1)

    return surf


def remap_fragment_elements(existing_ids: Set[str], frag_elements: List[dict]) -> Tuple[List[dict], Dict[str, str]]:
    used_ids = set(existing_ids)
    id_remap: Dict[str, str] = {}

    # 1. Allocate unique IDs for fragment elements
    for el in frag_elements:
        orig_id = str(el.get("id", ""))
        if orig_id in used_ids:
            etype = el.get("elem_type", "track")
            m = re.match(r"^([A-Za-z]+)(\d+)$", orig_id)
            if m:
                pref, num_str = m.group(1), m.group(2)
                num = int(num_str) + 1
                cand = f"{pref}{num}"
                while cand in used_ids:
                    num += 1
                    cand = f"{pref}{num}"
                new_id = cand
            else:
                idx = 1
                cand = f"{orig_id}_{idx}"
                while cand in used_ids:
                    idx += 1
                    cand = f"{orig_id}_{idx}"
                new_id = cand
        else:
            new_id = orig_id
        used_ids.add(new_id)
        id_remap[orig_id] = new_id

    # 2. Update relative internal connections within the fragment
    remapped_elements: List[dict] = []
    for el in frag_elements:
        it = copy.deepcopy(el)
        orig_id = str(it.get("id", ""))
        it["id"] = id_remap.get(orig_id, orig_id)
        if it.get("label") == orig_id:
            it["label"] = it["id"]

        # TrackSegment: neighbors & parallel_track_id
        if "neighbors" in it and isinstance(it["neighbors"], dict):
            new_nbrs = {}
            for d, nlist in it["neighbors"].items():
                if isinstance(nlist, list):
                    new_nbrs[d] = [id_remap.get(str(nid), str(nid)) for nid in nlist]
                elif isinstance(nlist, str):
                    new_nbrs[d] = [id_remap.get(str(nlist), str(nlist))]
                else:
                    new_nbrs[d] = nlist
            it["neighbors"] = new_nbrs
        if it.get("parallel_track_id"):
            it["parallel_track_id"] = id_remap.get(str(it["parallel_track_id"]), str(it["parallel_track_id"]))

        # Turnout: legs & guarded signals
        for leg in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
            if it.get(leg):
                it[leg] = id_remap.get(str(it[leg]), str(it[leg]))
        if "guarded_by_signals" in it and isinstance(it["guarded_by_signals"], list):
            it["guarded_by_signals"] = [id_remap.get(str(s), str(s)) for s in it["guarded_by_signals"]]

        # Signal: protected element & rear element
        for sref in ("protects_element", "rear_element"):
            if it.get(sref):
                it[sref] = id_remap.get(str(it[sref]), str(it[sref]))

        # Crossing: monitored tracks
        if "monitored_tracks" in it and isinstance(it["monitored_tracks"], list):
            it["monitored_tracks"] = [id_remap.get(str(t), str(t)) for t in it["monitored_tracks"]]

        # TrackJump: paired jump & connected element
        for jref in ("paired_jump_id", "connected_element"):
            if it.get(jref):
                it[jref] = id_remap.get(str(it[jref]), str(it[jref]))

        remapped_elements.append(it)

    return remapped_elements, id_remap


class FragmentPickerModal:
    """
    Grid modal overlay for visual inspection and loading of layout fragments from 'frags/'.
    Displays a scrollable grid of thumbnails ('sub-nails'), names, and element stats.
    """
    def __init__(
        self,
        frags_dir: str,
        on_load_fragment_cb: Callable[[str], None],
        on_close_cb: Optional[Callable[[], None]] = None,
    ):
        self.frags_dir = frags_dir
        self.on_load_cb = on_load_fragment_cb
        self.on_close_cb = on_close_cb
        self.items: List[Dict[str, Any]] = []
        self.selected_idx: Optional[int] = 0
        self.hover_idx: Optional[int] = None
        self.scroll_y: float = 0.0
        self.max_scroll: float = 0.0
        self.cols: int = 3
        self.modal_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.grid_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.btn_rects: Dict[str, pygame.Rect] = {}
        self.card_rects: List[pygame.Rect] = []
        self._last_click_ts: float = 0.0
        self.refresh()

    def refresh(self):
        os.makedirs(self.frags_dir, exist_ok=True)
        self.items = []

        frag_files: List[Tuple[str, str, str]] = []
        try:
            for root, dirs, files in os.walk(self.frags_dir):
                dirs[:] = sorted([d for d in dirs if not d.startswith(".")], key=lambda s: s.lower())
                rel_dir = os.path.relpath(root, self.frags_dir)
                for f in sorted(files, key=lambda s: s.lower()):
                    if f.lower().endswith(".json"):
                        stem = os.path.splitext(f)[0]
                        full_p = os.path.join(root, f)
                        ppath = os.path.join(root, f"{stem}.png")
                        if rel_dir in (".", ""):
                            disp_name = stem
                        else:
                            rel_posix = rel_dir.replace("\\", "/")
                            disp_name = f"{rel_posix}/{stem}"
                        frag_files.append((disp_name, full_p, ppath))
        except Exception:
            pass
        frag_files.sort(key=lambda item: item[0].lower())

        for name, jpath, ppath in frag_files:

            elements = []
            try:
                with open(jpath, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
                elements = data.get("elements", [])
                elem_count = len(elements)
                type_counts: Dict[str, int] = {}
                for it in elements:
                    t = it.get("elem_type", "track")
                    type_counts[t] = type_counts.get(t, 0) + 1
                summary_parts = [f"{c} {t}" for t, c in type_counts.items()]
                summary = ", ".join(summary_parts[:3])
                if len(summary_parts) > 3:
                    summary += "..."
            except Exception:
                elem_count = 0
                summary = "Error reading fragment"

            thumb_surf = None
            if os.path.exists(ppath):
                try:
                    thumb_surf = pygame.image.load(ppath)
                except Exception:
                    thumb_surf = None

            if thumb_surf is None and elements:
                try:
                    thumb_surf = render_fragment_thumbnail(elements, size=(220, 140))
                    pygame.image.save(thumb_surf, ppath)
                except Exception:
                    pass

            self.items.append({
                "name": name,
                "json_path": jpath,
                "png_path": ppath,
                "thumb_surface": thumb_surf,
                "element_count": elem_count,
                "summary": summary,
            })

        if self.items:
            if self.selected_idx is None or self.selected_idx >= len(self.items):
                self.selected_idx = 0
        else:
            self.selected_idx = None

    def draw(self, screen: pygame.Surface, font_sm, font_md, font_sm_bold):
        sw, sh = screen.get_size()
        mw = min(sw - 40, 880)
        mh = min(sh - 40, 620)
        mx = (sw - mw) // 2
        my = (sh - mh) // 2
        self.modal_rect = pygame.Rect(mx, my, mw, mh)

        overlay = pygame.Surface((sw, sh), pygame.SRCALPHA)
        overlay.fill((4, 8, 14, 195))
        screen.blit(overlay, (0, 0))

        pygame.draw.rect(screen, (16, 22, 32), self.modal_rect, border_radius=8)
        pygame.draw.rect(screen, (52, 75, 110), self.modal_rect, 2, border_radius=8)

        # Header
        hdr_rect = pygame.Rect(mx, my, mw, 48)
        pygame.draw.rect(screen, (24, 32, 46), hdr_rect, border_top_left_radius=8, border_top_right_radius=8)
        pygame.draw.line(screen, (45, 65, 95), (mx, my + 48), (mx + mw, my + 48), 1)

        title_surf = font_md.render("LOAD LAYOUT FRAGMENT", True, (255, 255, 255))
        screen.blit(title_surf, (mx + 16, my + 8))
        sub_surf = font_sm.render(f"Folder: '{self.frags_dir}'  |  Click thumbnail to select, double-click or Enter to load", True, (130, 160, 195))
        screen.blit(sub_surf, (mx + 16, my + 28))

        close_btn_rect = pygame.Rect(mx + mw - 36, my + 10, 26, 26)
        self.btn_rects["close"] = close_btn_rect
        pygame.draw.rect(screen, (70, 30, 35), close_btn_rect, border_radius=4)
        pygame.draw.rect(screen, (140, 60, 65), close_btn_rect, 1, border_radius=4)
        x_surf = font_sm_bold.render("X", True, (255, 210, 210))
        screen.blit(x_surf, (close_btn_rect.centerx - x_surf.get_width() // 2, close_btn_rect.centery - x_surf.get_height() // 2))

        # Footer
        fy = my + mh - 52
        foot_rect = pygame.Rect(mx, fy, mw, 52)
        pygame.draw.rect(screen, (22, 30, 42), foot_rect, border_bottom_left_radius=8, border_bottom_right_radius=8)
        pygame.draw.line(screen, (45, 65, 95), (mx, fy), (mx + mw, fy), 1)

        btn_w_load = 140
        btn_w_ref = 85
        btn_w_canc = 85
        btn_h = 32
        btn_gap = 10
        btn_y = fy + 10

        bx_load = mx + mw - 16 - btn_w_load
        bx_ref = bx_load - btn_gap - btn_w_ref
        bx_canc = bx_ref - btn_gap - btn_w_canc

        self.btn_rects["cancel"] = pygame.Rect(bx_canc, btn_y, btn_w_canc, btn_h)
        self.btn_rects["refresh"] = pygame.Rect(bx_ref, btn_y, btn_w_ref, btn_h)
        self.btn_rects["load"] = pygame.Rect(bx_load, btn_y, btn_w_load, btn_h)

        pygame.draw.rect(screen, (105, 38, 42), self.btn_rects["cancel"], border_radius=4)
        pygame.draw.rect(screen, COLOR_HUD_BORDER, self.btn_rects["cancel"], 1, border_radius=4)
        canc_ts = font_sm.render("Cancel", True, (255, 230, 230))
        screen.blit(canc_ts, (self.btn_rects["cancel"].centerx - canc_ts.get_width() // 2, self.btn_rects["cancel"].centery - canc_ts.get_height() // 2))

        pygame.draw.rect(screen, (50, 65, 88), self.btn_rects["refresh"], border_radius=4)
        pygame.draw.rect(screen, COLOR_HUD_BORDER, self.btn_rects["refresh"], 1, border_radius=4)
        ref_ts = font_sm.render("Refresh", True, (230, 240, 255))
        screen.blit(ref_ts, (self.btn_rects["refresh"].centerx - ref_ts.get_width() // 2, self.btn_rects["refresh"].centery - ref_ts.get_height() // 2))

        can_load = (self.selected_idx is not None and 0 <= self.selected_idx < len(self.items))
        load_bg = (35, 140, 70) if can_load else (45, 55, 68)
        pygame.draw.rect(screen, load_bg, self.btn_rects["load"], border_radius=4)
        pygame.draw.rect(screen, COLOR_HUD_BORDER, self.btn_rects["load"], 1, border_radius=4)
        load_ts = font_sm_bold.render("Load Fragment", True, (255, 255, 255) if can_load else (120, 130, 145))
        screen.blit(load_ts, (self.btn_rects["load"].centerx - load_ts.get_width() // 2, self.btn_rects["load"].centery - load_ts.get_height() // 2))

        if self.items:
            sel_name = self.items[self.selected_idx]["name"] if can_load else "None"
            f_info = f"{len(self.items)} fragment(s) available  |  Selected: {sel_name}"
        else:
            f_info = "0 fragments found in 'frags/' folder."
        f_surf = font_sm.render(f_info, True, (160, 185, 215))
        screen.blit(f_surf, (mx + 16, fy + 16))

        # Center Grid Area
        grid_top = my + 54
        grid_h = fy - grid_top - 6
        self.grid_rect = pygame.Rect(mx + 16, grid_top, mw - 32, grid_h)

        card_w = 230
        card_h = 168
        gap_x = 16
        gap_y = 16

        cols = max(1, (self.grid_rect.width + gap_x) // (card_w + gap_x))
        self.cols = cols
        total_grid_w = cols * card_w + (cols - 1) * gap_x
        start_x = self.grid_rect.x + (self.grid_rect.width - total_grid_w) // 2

        rows = (len(self.items) + cols - 1) // cols if self.items else 0
        total_content_h = rows * card_h + max(0, rows - 1) * gap_y
        self.max_scroll = max(0.0, float(total_content_h - self.grid_rect.height + 10))
        self.scroll_y = max(0.0, min(self.max_scroll, self.scroll_y))

        screen.set_clip(self.grid_rect)
        self.card_rects = []

        if not self.items:
            empty_title = font_md.render("No Layout Fragments Found in 'frags/'", True, (210, 220, 235))
            empty_sub = font_sm.render("Create small layout fragments by selecting elements and clicking 'SaveFrag' in the toolbar.", True, (130, 145, 165))
            screen.blit(empty_title, (self.grid_rect.centerx - empty_title.get_width() // 2, self.grid_rect.centery - 20))
            screen.blit(empty_sub, (self.grid_rect.centerx - empty_sub.get_width() // 2, self.grid_rect.centery + 8))
        else:
            for idx, it in enumerate(self.items):
                r = idx // cols
                c = idx % cols
                cx = start_x + c * (card_w + gap_x)
                cy = int(self.grid_rect.y + r * (card_h + gap_y) - self.scroll_y)
                crect = pygame.Rect(cx, cy, card_w, card_h)
                self.card_rects.append(crect)

                if crect.bottom >= self.grid_rect.top and crect.top <= self.grid_rect.bottom:
                    is_selected = (idx == self.selected_idx)
                    is_hovered = (idx == self.hover_idx)

                    bg_col = (36, 48, 68) if is_selected else ((30, 40, 56) if is_hovered else (24, 32, 45))
                    pygame.draw.rect(screen, bg_col, crect, border_radius=6)

                    bd_col = (50, 185, 255) if is_selected else ((95, 135, 185) if is_hovered else (45, 58, 80))
                    bd_w = 2 if is_selected else 1
                    pygame.draw.rect(screen, bd_col, crect, bd_w, border_radius=6)

                    pad = 6
                    tw = card_w - pad * 2
                    th = 108
                    t_area = pygame.Rect(cx + pad, cy + pad, tw, th)
                    pygame.draw.rect(screen, (12, 16, 24), t_area, border_radius=4)

                    tsurf = it.get("thumb_surface")
                    if tsurf:
                        if tsurf.get_size() != (tw, th):
                            scaled = pygame.transform.smoothscale(tsurf, (tw, th))
                            screen.blit(scaled, t_area)
                        else:
                            screen.blit(tsurf, t_area)
                    else:
                        ph_txt = font_sm.render("[No Preview]", True, (100, 115, 130))
                        screen.blit(ph_txt, (t_area.centerx - ph_txt.get_width() // 2, t_area.centery - ph_txt.get_height() // 2))
                    pygame.draw.rect(screen, (40, 52, 70), t_area, 1, border_radius=4)

                    name_txt = it["name"]
                    name_col = (255, 255, 255) if is_selected else (225, 235, 250)
                    n_surf = font_sm_bold.render(name_txt, True, name_col)
                    if n_surf.get_width() > card_w - 16:
                        if "/" in name_txt:
                            trimmed = name_txt
                            while len(trimmed) > 8 and font_sm_bold.size("..." + trimmed)[0] > card_w - 16:
                                trimmed = trimmed[1:]
                            name_txt = "..." + trimmed
                        else:
                            while len(name_txt) > 8 and font_sm_bold.size(name_txt + "...")[0] > card_w - 16:
                                name_txt = name_txt[:-1]
                            name_txt = name_txt + "..."
                        n_surf = font_sm_bold.render(name_txt, True, name_col)
                    screen.blit(n_surf, (cx + 8, cy + th + pad + 6))

                    cnt_txt = f"{it['element_count']} element{'s' if it['element_count'] != 1 else ''}"
                    if it.get("summary"):
                        cnt_txt += f" - {it['summary']}"
                    c_surf = font_sm.render(cnt_txt, True, (135, 185, 240) if is_selected else (135, 150, 170))
                    if c_surf.get_width() > card_w - 16:
                        short_cnt = f"{it['element_count']} elements"
                        c_surf = font_sm.render(short_cnt, True, (135, 185, 240) if is_selected else (135, 150, 170))
                    screen.blit(c_surf, (cx + 8, cy + th + pad + 24))

        if self.max_scroll > 0:
            sb_w = 6
            sb_x = self.grid_rect.right - sb_w - 2
            sb_y = self.grid_rect.y + 2
            sb_h = self.grid_rect.height - 4
            pygame.draw.rect(screen, (22, 30, 42), (sb_x, sb_y, sb_w, sb_h), border_radius=3)

            thumb_h = max(24, int(sb_h * (self.grid_rect.height / total_content_h)))
            thumb_y = sb_y + int((self.scroll_y / self.max_scroll) * (sb_h - thumb_h))
            pygame.draw.rect(screen, (85, 115, 160), (sb_x, thumb_y, sb_w, thumb_h), border_radius=3)

        screen.set_clip(None)

    def handle_scroll(self, dy: float):
        self.scroll_y = max(0.0, min(self.max_scroll, self.scroll_y - dy * 45.0))

    def handle_mouse_move(self, pos: Tuple[int, int]):
        mx, my = pos
        self.hover_idx = None
        if self.grid_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    self.hover_idx = idx
                    break

    def handle_mouse_down(self, pos: Tuple[int, int], button: int) -> Optional[str]:
        if button != 1:
            return None
        mx, my = pos

        if self.btn_rects.get("close", pygame.Rect(0, 0, 0, 0)).collidepoint(mx, my):
            if self.on_close_cb: self.on_close_cb()
            return None

        if self.btn_rects.get("cancel", pygame.Rect(0, 0, 0, 0)).collidepoint(mx, my):
            if self.on_close_cb: self.on_close_cb()
            return None
        if self.btn_rects.get("refresh", pygame.Rect(0, 0, 0, 0)).collidepoint(mx, my):
            self.refresh()
            return None
        if self.btn_rects.get("load", pygame.Rect(0, 0, 0, 0)).collidepoint(mx, my):
            if self.selected_idx is not None and 0 <= self.selected_idx < len(self.items):
                return self.items[self.selected_idx]["json_path"]
            return None

        if self.grid_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    now = time.time()
                    if self.selected_idx == idx and (now - self._last_click_ts) < 0.35:
                        return self.items[idx]["json_path"]
                    self._last_click_ts = now
                    self.selected_idx = idx
                    return None

        if not self.modal_rect.collidepoint(mx, my):
            if self.on_close_cb: self.on_close_cb()

        return None

    def handle_key_down(self, event: pygame.event.Event) -> Optional[str]:
        if event.key == pygame.K_ESCAPE:
            if self.on_close_cb: self.on_close_cb()
            return None
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.selected_idx is not None and 0 <= self.selected_idx < len(self.items):
                return self.items[self.selected_idx]["json_path"]
        elif event.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            if not self.items:
                return None
            cur = self.selected_idx if self.selected_idx is not None else 0
            if event.key == pygame.K_LEFT:
                cur = max(0, cur - 1)
            elif event.key == pygame.K_RIGHT:
                cur = min(len(self.items) - 1, cur + 1)
            elif event.key == pygame.K_UP:
                cur = max(0, cur - max(1, self.cols))
            elif event.key == pygame.K_DOWN:
                cur = min(len(self.items) - 1, cur + max(1, self.cols))
            self.selected_idx = cur
            if self.card_rects and self.selected_idx < len(self.card_rects):
                crect = self.card_rects[self.selected_idx]
                if crect.top < self.grid_rect.top:
                    self.scroll_y = max(0.0, self.scroll_y - (self.grid_rect.top - crect.top + 8))
                elif crect.bottom > self.grid_rect.bottom:
                    self.scroll_y = min(self.max_scroll, self.scroll_y + (crect.bottom - self.grid_rect.bottom + 8))
        return None


class BancCoverageMapModal:
    """
    Modal overlay for Banc Cloud remote layout management and coverage map display.
    - Displays a live coverage map of all Banc Cloud layouts (center GPS + radius circles).
    - Lists remote layouts with their availability status, center GPS, and radius.
    - Allows the user to select and download an available layout (loading it into the Creator).
    - Prohibits downloading layouts marked unavailable.
    - Allows uploading the current layout to Banc Cloud.
    """
    def __init__(
        self,
        tile_mgr: OSMTileMapManager,
        client: Any,
        on_download_and_open_cb: Callable[[dict, str], None],
        on_upload_current_cb: Callable[[], None],
        on_close_cb: Optional[Callable[[], None]] = None,
        zoom: int = 7,
    ):
        self.tile_mgr = tile_mgr
        self.client = client
        self.on_download_and_open_cb = on_download_and_open_cb
        self.on_upload_current_cb = on_upload_current_cb
        self.on_close_cb = on_close_cb
        self.zoom = zoom
        self.center_lat = 39.8283
        self.center_lon = -98.5795
        self.layer = "sat"
        self.layouts: List[Dict[str, Any]] = []
        self.selected_idx: Optional[int] = 0
        self.hover_idx: Optional[int] = None
        self.status_toast: str = ""
        self.status_toast_ts: float = 0.0
        self.scroll_y: float = 0.0
        self.max_scroll: float = 0.0
        self.modal_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.list_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.map_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.btn_rects: Dict[str, pygame.Rect] = {}
        self.card_rects: List[pygame.Rect] = []
        self.is_panning: bool = False
        self.pan_has_moved: bool = False
        self.pan_start_mouse = (0, 0)
        self.pan_start_world_px = (0.0, 0.0)
        self._last_click_ts: float = 0.0
        self.refresh()

    def set_toast(self, msg: str):
        self.status_toast = msg
        self.status_toast_ts = time.time()

    def refresh(self):
        if not self.client:
            self.layouts = []
            self.set_toast("Banc Cloud error: Banc client not configured.")
            return
        try:
            self.layouts = self.client.list_layouts_sync()
            self.set_toast(f"Retrieved {len(self.layouts)} layout(s) from Banc Cloud.")
        except Exception as exc:
            self.layouts = []
            self.set_toast(f"Banc Cloud error: {exc}")

        if self.layouts:
            if self.selected_idx is None or self.selected_idx >= len(self.layouts):
                self.selected_idx = 0
            gps_pts = [(it["center_lat"], it["center_lon"]) for it in self.layouts if it.get("center_lat") is not None and it.get("center_lon") is not None]
            if gps_pts:
                self.center_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                self.center_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
        else:
            self.selected_idx = None

    def center_on_layout(self, idx: int):
        if 0 <= idx < len(self.layouts):
            it = self.layouts[idx]
            if it.get("center_lat") is not None and it.get("center_lon") is not None:
                self.center_lat = float(it["center_lat"])
                self.center_lon = float(it["center_lon"])

    def zoom_at_point(self, mx: int, my: int, delta_z: int):
        new_z = max(3, min(19, self.zoom + delta_z))
        if new_z == self.zoom:
            return
        if self.map_rect.width > 0 and self.map_rect.collidepoint(mx, my):
            cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            dx = mx - self.map_rect.centerx
            dy = my - self.map_rect.centery
            hover_lat, hover_lon = self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)
            self.zoom = new_z
            new_hx, new_hy = self.tile_mgr.latlon_to_world_px(hover_lat, hover_lon, self.zoom)
            self.center_lat, self.center_lon = self.tile_mgr.world_px_to_latlon(new_hx - dx, new_hy - dy, self.zoom)
        else:
            self.zoom = new_z

    def screen_to_latlon(self, mx: int, my: int) -> Tuple[float, float]:
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
        dx = mx - self.map_rect.centerx
        dy = my - self.map_rect.centery
        return self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)

    def download_selected(self):
        if not self.client:
            self.set_toast("Banc Cloud error: Banc client not configured.")
            return
        if self.selected_idx is None or self.selected_idx < 0 or self.selected_idx >= len(self.layouts):
            self.set_toast("No layout selected.")
            return
        item = self.layouts[self.selected_idx]
        name = item.get("layout_name")
        if not item.get("is_available"):
            self.set_toast(f"Layout '{name}' is marked UNAVAILABLE and cannot be downloaded.")
            return

        self.set_toast(f"Downloading '{name}' from Banc Cloud...")
        try:
            data = self.client.download_layout_sync(name)
            if not data:
                self.set_toast(f"Failed to download '{name}' (empty response).")
                return
            if self.on_download_and_open_cb:
                self.on_download_and_open_cb(data, name)
        except Exception as exc:
            self.set_toast(f"Download failed: {exc}")

    def draw(self, screen: pygame.Surface, font_sm, font_md, font_sm_bold):
        sw, sh = screen.get_size()
        mw = min(sw - 40, 1180)
        mh = min(sh - 40, 720)
        mx = (sw - mw) // 2
        my = (sh - mh) // 2
        self.modal_rect = pygame.Rect(mx, my, mw, mh)

        overlay = pygame.Surface((sw, sh), pygame.SRCALPHA)
        overlay.fill((4, 8, 14, 210))
        screen.blit(overlay, (0, 0))

        pygame.draw.rect(screen, (16, 22, 32), self.modal_rect, border_radius=8)
        pygame.draw.rect(screen, (52, 75, 110), self.modal_rect, 2, border_radius=8)

        # Header
        hdr_rect = pygame.Rect(mx, my, mw, 48)
        pygame.draw.rect(screen, (24, 32, 46), hdr_rect, border_top_left_radius=8, border_top_right_radius=8)
        pygame.draw.line(screen, (45, 65, 95), (mx, my + 48), (mx + mw, my + 48), 1)

        title_surf = font_md.render("BANC CLOUD LAYOUT COVERAGE & REMOTE REPOSITORY", True, (255, 255, 255))
        screen.blit(title_surf, (mx + 16, my + 8))
        h_host = getattr(self.client, 'host', '127.0.0.1')
        h_port = getattr(self.client, 'port', 23009)
        sub_surf = font_sm.render(f"Cloud Server: {h_host}:{h_port}  |  Pick layout to download & open as local  |  Coverage map displays layout GPS radius", True, (130, 160, 195))
        screen.blit(sub_surf, (mx + 16, my + 28))

        close_btn_rect = pygame.Rect(mx + mw - 36, my + 10, 26, 26)
        self.btn_rects["close"] = close_btn_rect
        pygame.draw.rect(screen, (70, 30, 35), close_btn_rect, border_radius=4)
        pygame.draw.rect(screen, (140, 60, 65), close_btn_rect, 1, border_radius=4)
        x_surf = font_sm_bold.render("X", True, (255, 210, 210))
        screen.blit(x_surf, (close_btn_rect.centerx - x_surf.get_width() // 2, close_btn_rect.centery - x_surf.get_height() // 2))

        # Main Layout Split: Left List (370px) + Right Map
        list_w = 370
        map_w = mw - list_w - 28
        body_h = mh - 48 - 14
        self.list_rect = pygame.Rect(mx + 12, my + 54, list_w, body_h - 100)
        self.map_rect = pygame.Rect(mx + 12 + list_w + 10, my + 54, map_w, body_h - 40)

        # Draw Left Panel (Layout List & Actions)
        screen.blit(font_sm_bold.render(f"Remote Layouts ({len(self.layouts)}):", True, COLOR_EDIT_ACCENT), (self.list_rect.x, self.list_rect.y - 18))

        list_box = pygame.Rect(self.list_rect.x, self.list_rect.y, self.list_rect.width, self.list_rect.height)
        pygame.draw.rect(screen, (20, 26, 38), list_box, border_radius=4)
        pygame.draw.rect(screen, (45, 60, 85), list_box, 1, border_radius=4)

        card_h = 68
        card_gap = 6
        total_content_h = max(1, len(self.layouts) * (card_h + card_gap))
        self.max_scroll = max(0.0, float(total_content_h - self.list_rect.height + 10))

        screen.set_clip(self.list_rect)
        self.card_rects = []
        for idx, it in enumerate(self.layouts):
            cx = self.list_rect.x + 6
            cy = int(self.list_rect.y + 6 + idx * (card_h + card_gap) - self.scroll_y)
            card_w = self.list_rect.width - 16
            crect = pygame.Rect(cx, cy, card_w, card_h)
            self.card_rects.append(crect)

            if cy + card_h < self.list_rect.y or cy > self.list_rect.bottom:
                continue

            is_sel = (self.selected_idx == idx)
            is_hov = (self.hover_idx == idx)
            bg = (38, 52, 74) if is_sel else ((28, 38, 54) if is_hov else (22, 30, 42))
            border_c = COLOR_EDIT_ACCENT if is_sel else ((70, 95, 135) if is_hov else (38, 50, 70))
            pygame.draw.rect(screen, bg, crect, border_radius=4)
            pygame.draw.rect(screen, border_c, crect, 2 if is_sel else 1, border_radius=4)

            # Name
            name_txt = str(it.get("layout_name", "unknown"))
            n_surf = font_sm_bold.render(name_txt[:26], True, (255, 255, 255) if is_sel else (225, 235, 250))
            screen.blit(n_surf, (cx + 8, cy + 6))

            # Availability Badge
            is_avail = bool(it.get("is_available"))
            badge_txt = "AVAILABLE" if is_avail else "UNAVAILABLE"
            badge_color = (40, 210, 90) if is_avail else (235, 60, 60)
            badge_surf = font_sm_bold.render(badge_txt, True, badge_color)
            bw, bh = badge_surf.get_width() + 6, badge_surf.get_height() + 2
            bx = cx + card_w - bw - 6
            by = cy + 6
            pygame.draw.rect(screen, (15, 20, 28, 200), (bx, by, bw, bh), border_radius=3)
            pygame.draw.rect(screen, badge_color, (bx, by, bw, bh), 1, border_radius=3)
            screen.blit(badge_surf, (bx + 3, by + 1))

            # GPS & Radius
            lat = it.get("center_lat")
            lon = it.get("center_lon")
            rad = it.get("radius_km")
            if lat is not None and lon is not None:
                gps_str = f"GPS: {lat:.4f}, {lon:.4f}  |  Rad: {rad:.1f}km ({rad*0.621371:.1f}mi)" if rad is not None else f"GPS: {lat:.4f}, {lon:.4f}"
            else:
                gps_str = "No GPS Information"
            g_surf = font_sm.render(gps_str, True, (135, 175, 220) if is_sel else (125, 140, 165))
            screen.blit(g_surf, (cx + 8, cy + 28))

            # Creator/Updated info
            up_by = it.get("uploaded_by") or "user"
            raw_stamp = it.get("stamp_updated") or it.get("stamp_created")
            up_time_str = ""
            if isinstance(raw_stamp, (int, float)) and raw_stamp > 0:
                try:
                    up_time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(raw_stamp))
                except Exception:
                    up_time_str = str(raw_stamp)[:16]
            elif raw_stamp:
                up_time_str = str(raw_stamp)[:16]

            t_str = f"By: {up_by}" + (f"  |  {up_time_str}" if up_time_str else "")
            t_surf = font_sm.render(t_str, True, (100, 120, 145))
            screen.blit(t_surf, (cx + 8, cy + 46))

        screen.set_clip(None)

        # Left Action Buttons Under List
        act_y = self.list_rect.bottom + 12
        btn_w_half = (list_w - 8) // 2

        btn_dl = pygame.Rect(self.list_rect.x, act_y, btn_w_half, 32)
        self.btn_rects["download_open"] = btn_dl
        sel_is_avail = (self.selected_idx is not None and 0 <= self.selected_idx < len(self.layouts) and self.layouts[self.selected_idx].get("is_available"))
        dl_bg = (35, 120, 75) if sel_is_avail else (45, 55, 65)
        pygame.draw.rect(screen, dl_bg, btn_dl, border_radius=4)
        pygame.draw.rect(screen, (55, 165, 105) if sel_is_avail else (70, 80, 90), btn_dl, 1, border_radius=4)
        dl_txt = font_sm_bold.render("Download & Open", True, (255, 255, 255) if sel_is_avail else (140, 150, 160))
        screen.blit(dl_txt, (btn_dl.centerx - dl_txt.get_width() // 2, btn_dl.centery - dl_txt.get_height() // 2))

        btn_up = pygame.Rect(self.list_rect.x + btn_w_half + 8, act_y, btn_w_half, 32)
        self.btn_rects["upload_current"] = btn_up
        pygame.draw.rect(screen, (45, 85, 140), btn_up, border_radius=4)
        pygame.draw.rect(screen, (70, 125, 200), btn_up, 1, border_radius=4)
        up_txt = font_sm_bold.render("Upload Current", True, (255, 255, 255))
        screen.blit(up_txt, (btn_up.centerx - up_txt.get_width() // 2, btn_up.centery - up_txt.get_height() // 2))

        btn_ref = pygame.Rect(self.list_rect.x, act_y + 38, btn_w_half, 28)
        self.btn_rects["refresh"] = btn_ref
        pygame.draw.rect(screen, (42, 60, 85), btn_ref, border_radius=4)
        pygame.draw.rect(screen, (65, 90, 125), btn_ref, 1, border_radius=4)
        ref_txt = font_sm_bold.render("Refresh List", True, (210, 225, 245))
        screen.blit(ref_txt, (btn_ref.centerx - ref_txt.get_width() // 2, btn_ref.centery - ref_txt.get_height() // 2))

        btn_canc = pygame.Rect(self.list_rect.x + btn_w_half + 8, act_y + 38, btn_w_half, 28)
        self.btn_rects["cancel"] = btn_canc
        pygame.draw.rect(screen, (65, 35, 42), btn_canc, border_radius=4)
        pygame.draw.rect(screen, (115, 60, 70), btn_canc, 1, border_radius=4)
        canc_txt = font_sm_bold.render("Close", True, (245, 215, 220))
        screen.blit(canc_txt, (btn_canc.centerx - canc_txt.get_width() // 2, btn_canc.centery - canc_txt.get_height() // 2))

        # Status Toast Bar
        if self.status_toast:
            st_color = COLOR_ERROR_RED if "error" in self.status_toast.lower() or "unavail" in self.status_toast.lower() else COLOR_EDIT_ACCENT
            screen.blit(font_sm.render(self.status_toast[:55], True, st_color), (self.list_rect.x, act_y + 72))

        # Draw Right Panel (Coverage Map)
        mode_lbl = "SATELLITE" if self.layer == "sat" else "STREET MAP"
        map_title = f"CLOUD LAYOUT COVERAGE MAP [{mode_lbl} | Zoom:{self.zoom}] — Drag to Pan | Scroll to Zoom | Click Pin/Circle to Select"
        screen.blit(font_sm_bold.render(map_title, True, (0, 225, 245)), (self.map_rect.x, self.map_rect.y - 18))

        map_surf = pygame.Surface((self.map_rect.width, self.map_rect.height))
        map_surf.fill((22, 28, 38))

        z = self.zoom
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, z)
        mw_map, mh_map = self.map_rect.width, self.map_rect.height

        min_tx = int(math.floor((cx_px - mw_map / 2.0) / 256.0))
        max_tx = int(math.floor((cx_px + mw_map / 2.0) / 256.0))
        min_ty = int(math.floor((cy_px - mh_map / 2.0) / 256.0))
        max_ty = int(math.floor((cy_px + mh_map / 2.0) / 256.0))
        n_tiles = 2 ** z

        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles
                if ty < 0 or ty >= n_tiles:
                    continue
                tile_s = self.tile_mgr.get_tile_surface(z, wrapped_tx, ty, layer=self.layer)
                dx = int(mw_map / 2.0 + (tx * 256.0 - cx_px))
                dy = int(mh_map / 2.0 + (ty * 256.0 - cy_px))
                if tile_s:
                    map_surf.blit(tile_s, (dx, dy))
                else:
                    pygame.draw.rect(map_surf, (34, 44, 58), (dx, dy, 256, 256), 1)

        # Draw Coverage Circles and Center Pins for all layouts
        for idx, it in enumerate(self.layouts):
            lat = it.get("center_lat")
            lon = it.get("center_lon")
            if lat is None or lon is None:
                continue

            ex_px, ey_px = self.tile_mgr.latlon_to_world_px(lat, lon, z)
            px = int(mw_map / 2.0 + (ex_px - cx_px))
            py = int(mh_map / 2.0 + (ey_px - cy_px))

            rad_km = float(it.get("radius_km") or 1.0)
            lat_rad = math.radians(lat)
            meters_per_px = 156543.03392 * math.cos(lat_rad) / (2.0 ** z)
            rad_px = max(8, min(1500, int((rad_km * 1000.0) / max(0.001, meters_per_px))))

            is_sel = (self.selected_idx == idx)
            is_avail = bool(it.get("is_available"))

            # Coverage circle
            if is_sel:
                circ_color = (255, 215, 30)
                fill_color = (255, 215, 30, 40)
                circ_w = 3
            elif is_avail:
                circ_color = (0, 230, 180)
                fill_color = (0, 230, 180, 25)
                circ_w = 2
            else:
                circ_color = (235, 75, 75)
                fill_color = (235, 75, 75, 25)
                circ_w = 2

            if -rad_px < px < mw_map + rad_px and -rad_px < py < mh_map + rad_px:
                c_surf = pygame.Surface((rad_px * 2 + 4, rad_px * 2 + 4), pygame.SRCALPHA)
                pygame.draw.circle(c_surf, fill_color, (rad_px + 2, rad_px + 2), rad_px)
                pygame.draw.circle(c_surf, circ_color, (rad_px + 2, rad_px + 2), rad_px, circ_w)
                map_surf.blit(c_surf, (px - rad_px - 2, py - rad_px - 2))

            # Center pin & Label
            if 0 <= px < mw_map and 0 <= py < mh_map:
                pin_c = (255, 215, 30) if is_sel else (circ_color)
                pygame.draw.circle(map_surf, (15, 20, 28), (px, py), 9)
                pygame.draw.circle(map_surf, pin_c, (px, py), 6)
                pygame.draw.circle(map_surf, (255, 255, 255), (px, py), 2)

                lname = str(it.get("layout_name", "layout"))
                avail_tag = "AVAIL" if is_avail else "UNAVAIL"
                tag_txt = f"{lname} [{avail_tag} | {rad_km:.1f}km]"
                tag_surf = font_sm.render(tag_txt, True, (255, 255, 255) if is_sel else (220, 230, 245))
                tw, th = tag_surf.get_width() + 8, tag_surf.get_height() + 4
                tbox = pygame.Surface((tw, th), pygame.SRCALPHA)
                tbox.fill((16, 24, 34, 230))
                pygame.draw.rect(tbox, pin_c if is_sel else (60, 80, 110), (0, 0, tw, th), 1, border_radius=3)
                tbox.blit(tag_surf, (4, 2))
                map_surf.blit(tbox, (px - tw // 2, py - 14 - th))

        pygame.draw.rect(map_surf, (55, 75, 105), (0, 0, mw_map, mh_map), 2)
        screen.blit(map_surf, self.map_rect.topleft)

        # Map Bottom Control Bar
        mctrl_y = self.map_rect.bottom + 8
        btn_layer = pygame.Rect(self.map_rect.x, mctrl_y, 110, 26)
        self.btn_rects["toggle_layer"] = btn_layer
        pygame.draw.rect(screen, (55, 45, 95), btn_layer, border_radius=3)
        pygame.draw.rect(screen, (85, 70, 145), btn_layer, 1, border_radius=3)
        lyr_txt = font_sm_bold.render("Sat / Street", True, (230, 220, 255))
        screen.blit(lyr_txt, (btn_layer.centerx - lyr_txt.get_width() // 2, btn_layer.centery - lyr_txt.get_height() // 2))

        btn_zin = pygame.Rect(self.map_rect.x + 118, mctrl_y, 65, 26)
        self.btn_rects["zoom_in"] = btn_zin
        pygame.draw.rect(screen, (40, 75, 115), btn_zin, border_radius=3)
        pygame.draw.rect(screen, (60, 110, 165), btn_zin, 1, border_radius=3)
        zin_txt = font_sm_bold.render("Zoom +", True, (215, 235, 255))
        screen.blit(zin_txt, (btn_zin.centerx - zin_txt.get_width() // 2, btn_zin.centery - zin_txt.get_height() // 2))

        btn_zout = pygame.Rect(self.map_rect.x + 188, mctrl_y, 65, 26)
        self.btn_rects["zoom_out"] = btn_zout
        pygame.draw.rect(screen, (40, 75, 115), btn_zout, border_radius=3)
        pygame.draw.rect(screen, (60, 110, 165), btn_zout, 1, border_radius=3)
        zout_txt = font_sm_bold.render("Zoom -", True, (215, 235, 255))
        screen.blit(zout_txt, (btn_zout.centerx - zout_txt.get_width() // 2, btn_zout.centery - zout_txt.get_height() // 2))

        btn_call = pygame.Rect(self.map_rect.x + 258, mctrl_y, 90, 26)
        self.btn_rects["center_all"] = btn_call
        pygame.draw.rect(screen, (45, 80, 85), btn_call, border_radius=3)
        pygame.draw.rect(screen, (65, 115, 125), btn_call, 1, border_radius=3)
        call_txt = font_sm_bold.render("Center All", True, (215, 250, 245))
        screen.blit(call_txt, (btn_call.centerx - call_txt.get_width() // 2, btn_call.centery - call_txt.get_height() // 2))

        # Center coord readout
        ctr_str = f"Center: ({self.center_lat:.4f}, {self.center_lon:.4f})"
        screen.blit(font_sm.render(ctr_str, True, (135, 160, 190)), (self.map_rect.x + 360, mctrl_y + 5))

    def handle_scroll(self, dy: float, mx: int, my: int):
        if self.map_rect.collidepoint(mx, my):
            self.zoom_at_point(mx, my, 1 if dy > 0 else -1)
        elif self.list_rect.collidepoint(mx, my):
            self.scroll_y = max(0.0, min(self.max_scroll, self.scroll_y - dy * 40.0))

    def handle_mouse_move(self, pos: Tuple[int, int]):
        mx, my = pos
        self.hover_idx = None
        if self.list_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    self.hover_idx = idx
                    break

        if self.is_panning:
            dx = mx - self.pan_start_mouse[0]
            dy = my - self.pan_start_mouse[1]
            if abs(dx) > 3 or abs(dy) > 3:
                self.pan_has_moved = True
            sx, sy = self.pan_start_world_px
            self.center_lat, self.center_lon = self.tile_mgr.world_px_to_latlon(sx - dx, sy - dy, self.zoom)

    def handle_mouse_down(self, pos: Tuple[int, int], button: int):
        if button != 1:
            return
        mx, my = pos

        # Buttons
        for bkey, r in self.btn_rects.items():
            if r.collidepoint(mx, my):
                if bkey == "close" or bkey == "cancel":
                    if self.on_close_cb: self.on_close_cb()
                elif bkey == "refresh":
                    self.refresh()
                elif bkey == "download_open":
                    self.download_selected()
                elif bkey == "upload_current":
                    if self.on_upload_current_cb: self.on_upload_current_cb()
                    self.refresh()
                elif bkey == "toggle_layer":
                    self.layer = "osm" if self.layer == "sat" else "sat"
                elif bkey == "zoom_in":
                    self.zoom_at_point(self.map_rect.centerx, self.map_rect.centery, 1)
                elif bkey == "zoom_out":
                    self.zoom_at_point(self.map_rect.centerx, self.map_rect.centery, -1)
                elif bkey == "center_all":
                    gps_pts = [(it["center_lat"], it["center_lon"]) for it in self.layouts if it.get("center_lat") is not None and it.get("center_lon") is not None]
                    if gps_pts:
                        self.center_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                        self.center_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                return

        # List Cards
        if self.list_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    now = time.time()
                    if self.selected_idx == idx and (now - self._last_click_ts) < 0.35:
                        self.download_selected()
                    else:
                        self.selected_idx = idx
                        self.center_on_layout(idx)
                    self._last_click_ts = now
                    return

        # Map Clicks
        if self.map_rect.collidepoint(mx, my):
            # Check if clicked on a pin/circle
            z = self.zoom
            cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, z)
            mw_map, mh_map = self.map_rect.width, self.map_rect.height
            clicked_pin_idx = None
            for idx, it in enumerate(self.layouts):
                lat = it.get("center_lat")
                lon = it.get("center_lon")
                if lat is None or lon is None:
                    continue
                ex_px, ey_px = self.tile_mgr.latlon_to_world_px(lat, lon, z)
                px = int(self.map_rect.x + mw_map / 2.0 + (ex_px - cx_px))
                py = int(self.map_rect.y + mh_map / 2.0 + (ey_px - cy_px))
                dist = math.hypot(mx - px, my - py)
                if dist <= 14.0:
                    clicked_pin_idx = idx
                    break

            if clicked_pin_idx is not None:
                self.selected_idx = clicked_pin_idx
                self.center_on_layout(clicked_pin_idx)
                return

            self.is_panning = True
            self.pan_has_moved = False
            self.pan_start_mouse = (mx, my)
            self.pan_start_world_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            return

        if not self.modal_rect.collidepoint(mx, my):
            if self.on_close_cb: self.on_close_cb()

    def handle_mouse_up(self, pos: Tuple[int, int], button: int):
        if button == 1:
            self.is_panning = False

    def handle_key_down(self, event: pygame.event.Event):
        if event.key == pygame.K_ESCAPE:
            if self.on_close_cb: self.on_close_cb()
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.download_selected()
        elif event.key == pygame.K_UP:
            if self.layouts:
                cur = self.selected_idx if self.selected_idx is not None else 0
                self.selected_idx = max(0, cur - 1)
                self.center_on_layout(self.selected_idx)
        elif event.key == pygame.K_DOWN:
            if self.layouts:
                cur = self.selected_idx if self.selected_idx is not None else 0
                self.selected_idx = min(len(self.layouts) - 1, cur + 1)
                self.center_on_layout(self.selected_idx)
        elif event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            self.zoom = min(19, self.zoom + 1)
        elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.zoom = max(3, self.zoom - 1)
        elif event.key == pygame.K_r:
            self.refresh()
        elif event.key == pygame.K_u:
            if self.on_upload_current_cb: self.on_upload_current_cb()
            self.refresh()


# =====================================================================
# 4. MAIN APEXBOS CREATOR STUDIO APPLICATION (v26.117.2)
# =====================================================================

class ApexBOSCreatorApp:
    def __init__(
        self,
        layout_filepath: str = "complex_layout.json",
        db_filepath: Optional[str] = None,
        parser_files: Optional[List[str]] = None,
        default_lat: float = 33.0522,
        default_lon: float = -110.2400,
        default_radius: float = 20.0,
        map_view_radius_miles: float = 0.5,
        banc_config: Optional[str] = None,
        gps_specified: Optional[bool] = None,
    ):
        pygame.init()
        pygame.display.set_caption(f"ApexBOSCreator {CREATOR_VERSION} - Railroad Layout Studio (Copyright by AB - MIT License)")
        self.width = 1600
        self.height = 950
        self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()

        self.font_sm_thin = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 11)
        self.font_sm = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 12)
        self.font_sm_bold = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 12, bold=True)
        self.font_md = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 14, bold=True)
        self.font_lg = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 18, bold=True)
        self.font_xl = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 24, bold=True)
        self.font_splash_thanks = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 15, bold=False)
        self._elem_label_font_cache: Dict[Tuple[int, bool], pygame.font.Font] = {}

        self.last_selected_gps: Tuple[float, float] = (default_lat, default_lon)
        self.gps_specified: bool = (
            bool(gps_specified)
            if gps_specified is not None
            else (
                _parse_wiu_gps((default_lat, default_lon)) is not None
                and (float(default_lat), float(default_lon)) != (33.0522, -110.2400)
            )
        )
        self.specified_gps: Optional[Tuple[float, float]] = (
            _parse_wiu_gps((default_lat, default_lon)) if self.gps_specified else None
        )
        self.default_radius: float = default_radius
        self.map_view_radius_miles: float = float(map_view_radius_miles)
        self.last_map_picked_gps: Optional[Tuple[float, float]] = None
        self.last_map_zoom: int = 15

        # Raw Packet Log playback state
        self.playback_packets: List[dict] = []
        self.playback_idx: int = 0
        self.playback_active: bool = False
        self.playback_filepath: str = ""
        self.playback_last_tick: float = 0.0
        self.playback_delay_sec: float = 0.12
        self.playback_btn_rects: Dict[str, pygame.Rect] = {}

        self.show_labels: bool = True
        self.show_inferred_trains: bool = True
        self.enable_inference_logic: bool = True

        self._monotonic_counters: Dict[str, int] = {etype: 1 for etype in ELEMENT_TYPES}
        self.thanks_text: str = self._load_thanks_text(layout_filepath)
        self.tile_map_mgr = OSMTileMapManager(cache_dir=MAP_CACHE_DIR)
        self.gps_picker_modal: Optional[InteractiveGPSMapModal] = None
        self.frag_picker_modal: Optional[FragmentPickerModal] = None
        self.banc_coverage_modal: Optional[BancCoverageMapModal] = None
        if not banc_config:
            if os.path.exists("config.json"):
                banc_config = "config.json"
            elif os.path.exists(os.path.join("config", "config.json")):
                banc_config = os.path.join("config", "config.json")
        self.banc_config_path: Optional[str] = banc_config
        self.banc_client: Optional[mosfet_client.BancClient] = mosfet_client.create_banc_client_from_config(banc_config, prefer_section="creator")
        self.last_saved_frag_name: Optional[str] = None
        self._draw_startup_splash("Initializing DispatcherEngine & loading layout configuration. Please wait...", progress=0.05)

        cfg = {
            "parsers": parser_files or (["ptc_status_parser.json"] if os.path.exists("ptc_status_parser.json") else []),
            "start_layout_server": False,
        }
        self.engine = DispatcherEngine(config=cfg, layout_filepath=layout_filepath)
        self.layout_filepath = layout_filepath
        if os.path.exists(layout_filepath):
            self.engine.load_layout(layout_filepath)
            self._sync_monotonic_counters()
            for el in self.engine.elements.values():
                if el.gps:
                    self.last_selected_gps = (float(el.gps[0]), float(el.gps[1]))

        self.wiu_cache = WIUAddressCache(load_existing_cache=not bool(db_filepath))
        self.banc_wius_loaded: bool = False
        self.banc_wius_count: int = 0

        if db_filepath:
            self._draw_startup_splash(f"Reading WIUs from 'wius' table in '{db_filepath}'. Please wait...", progress=0.25)
            loaded_wius = self.wiu_cache.load_from_wius_table(
                db_filepath,
                progress_cb=lambda msg: self._draw_startup_splash(msg, progress=0.75),
            )
            time.sleep(0.15)
            self.status_toast = f"Loaded {loaded_wius} WIUs from 'wius' table in '{db_filepath}' ({len(self.wiu_cache.wiu_records)} WIUs cached)"
        else:
            t_prep_start = time.time()
            banc_attempted = False
            banc_loaded_from_cloud = -1

            if self._is_banc_connection_configured():
                banc_attempted = True
                self._draw_startup_splash(
                    "Loading WIUs from Banc Cloud 'wius' table. Please wait...",
                    progress=0.25,
                )
                banc_loaded_from_cloud = self.wiu_cache.load_from_banc(
                    self.banc_client,
                    progress_cb=lambda msg: self._draw_startup_splash(msg, progress=0.75),
                    timeout=3.0,
                    replace_existing=True,
                )

            if banc_loaded_from_cloud > 0:
                self.banc_wius_loaded = True
                self.banc_wius_count = banc_loaded_from_cloud
                time.sleep(0.15)
                self.status_toast = (
                    f"Loaded {banc_loaded_from_cloud} WIUs from Banc Cloud 'wius' table "
                    f"({len(self.wiu_cache.wiu_records)} WIUs cached)"
                )
            else:
                if banc_loaded_from_cloud == 0:
                    self.banc_wius_loaded = True
                for el in self.engine.elements.values():
                    if el.wiu_id is not None:
                        self.wiu_cache.register_wiu(
                            el.wiu_id,
                            name=getattr(el, "label", ""),
                            gps=getattr(el, "gps", None),
                            ctc_addresses=getattr(el, "ctc_id", ""),
                        )
                elapsed_prep = time.time() - t_prep_start
                remaining_splash = max(0.0, 5.0 - elapsed_prep)
                steps = 50
                for step_i in range(1, steps + 1):
                    frac = step_i / float(steps)
                    self._draw_startup_splash(
                        f"Loaded '{layout_filepath}' ({len(self.engine.elements)} elements). Preparing studio... ({int(frac * 100)}%)",
                        progress=frac,
                    )
                    if remaining_splash > 0.0:
                        time.sleep(remaining_splash / steps)
                time.sleep(0.25 if remaining_splash > 0.0 else 0.05)
                if banc_attempted and banc_loaded_from_cloud == 0:
                    self.status_toast = (
                        f"Loaded '{layout_filepath}' ({len(self.engine.elements)} elements, "
                        f"0 WIUs in Banc Cloud, {len(self.wiu_cache.wiu_records)} cached WIUs)"
                    )
                elif banc_attempted and banc_loaded_from_cloud < 0:
                    self.status_toast = (
                        f"Loaded '{layout_filepath}' ({len(self.engine.elements)} elements, "
                        f"{len(self.wiu_cache.wiu_records)} cached WIUs; Banc unavailable)"
                    )
                else:
                    self.status_toast = (
                        f"Loaded '{layout_filepath}' ({len(self.engine.elements)} elements, "
                        f"{len(self.wiu_cache.wiu_records)} cached WIUs)"
                    )

        self.zoom: float = 1.0
        self.min_zoom: float = 0.35 / 20.0  # 20x more zoom out capability (0.0175)
        self.last_placed_element_id: Optional[str] = None
        self.last_estimate_beyond_zoom: bool = False
        self.last_estimate_dist_miles: float = 0.0
        self.max_zoom: float = 4.0
        self.cam_x: float = 55.0
        self.cam_y: float = 80.0
        self.center_view_on_layout()
        self.is_panning: bool = False
        self.pan_start_mouse: Tuple[int, int] = (0, 0)
        self.pan_start_cam: Tuple[float, float] = (0.0, 0.0)

        self.undo_stack: deque = deque(maxlen=MAX_UNDO_STEPS)
        self.redo_stack: deque = deque(maxlen=MAX_UNDO_STEPS)
        self.dirty: bool = False
        self._broadcast_queue: queue.Queue = queue.Queue(maxsize=2)
        self._broadcast_worker_started: bool = False
        self.confirm_exit_modal: bool = False
        self.exit_modal_buttons: Dict[str, pygame.Rect] = {}
        self.confirm_new_modal: bool = False
        self.new_modal_buttons: Dict[str, pygame.Rect] = {}
        self.pending_post_save_action: Optional[Any] = None
        self.confirm_load_modal: bool = False
        self.load_modal_buttons: Dict[str, pygame.Rect] = {}
        self.pending_load_target: Optional[Any] = None

        self.active_tool: str = "select"
        self.selected_ids: Set[str] = set()
        self.clipboard: List[dict] = []

        self.dragging_selection: bool = False
        self.drag_moved: bool = False
        self.drag_anchor_logical: Tuple[float, float] = (0.0, 0.0)
        self.drag_initial_positions: Dict[str, Tuple[int, int]] = {}
        self._group_rot_session: Optional[Dict[str, Any]] = None
        self._shift_arrow_repeat_ts: float = 0.0
        self._shift_arrow_moved_during_hold: bool = False

        self.dragging_track_end: Optional[Tuple[str, int, Tuple[float, float]]] = None
        self.track_end_drag_curr_logical: Tuple[float, float] = (0.0, 0.0)

        self.box_selecting: bool = False
        self.box_start_screen: Tuple[int, int] = (0, 0)
        self.box_curr_screen: Tuple[int, int] = (0, 0)

        self.last_click_time: float = 0.0
        self.last_click_id: Optional[str] = None

        self.inplace_editor: Optional[InPlaceElementEditor] = None
        self.file_prompt_mode: Optional[str] = None
        self.file_prompt_buffer: str = ""
        self.file_prompt_suggestions: List[str] = []
        self.file_prompt_selected_idx: int = -1
        self.file_prompt_suggestion_rects: List[Tuple[pygame.Rect, str]] = []
        self._last_suggest_mode: Optional[Any] = None

        self.verification_issues: List[Tuple[str, str]] = []
        self.show_verification_panel: bool = False

        self.hud_buttons: Dict[str, pygame.Rect] = {}
        self.hud_bar_height: int = 50

        # Train Assignment Dialog & Scroller Interaction State
        self.train_assign_active: bool = False
        self.train_assign_trn_key: Optional[str] = None
        self.train_assign_active_field: str = "assigned_id"
        self.train_assign_buffers: Dict[str, str] = {"assigned_id": "", "assigned_name": ""}
        self._train_scroller_row_rects: List[Tuple[pygame.Rect, dict]] = []
        self._train_scroller_del_rects: List[Tuple[pygame.Rect, str]] = []
        self._last_engine_eval_ts: float = 0.0

        self.crank_engine()

    def _is_banc_connection_configured(self) -> bool:
        """Returns True if Banc client configuration and client parameters are configured and valid."""
        client = getattr(self, "banc_client", None)
        if not mosfet_client.is_valid_banc_client(client):
            return False
        factory_fn = getattr(mosfet_client, "create_banc_client_from_config", None)
        if getattr(factory_fn, "_is_default_impl", False):
            return mosfet_client.has_valid_banc_config(getattr(self, "banc_config_path", None), prefer_section="creator")
        return True

    @staticmethod
    def _load_thanks_text(layout_filepath: str) -> str:
        dirs = [
            os.path.dirname(os.path.abspath(layout_filepath)) if layout_filepath else os.getcwd(),
            os.getcwd(),
            os.path.join(os.getcwd(), "config"),
        ]
        for d in dirs:
            p = os.path.join(d, "thanks.txt")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read().strip()
                except Exception:
                    pass
        return ""

    @staticmethod
    def _wrap_splash_thanks_text(raw_text: str, font: pygame.font.Font, max_width_px: int) -> List[str]:
        if not raw_text:
            return []
        lines: List[str] = []
        for raw_para in str(raw_text).splitlines():
            para = raw_para.strip()
            if not para:
                continue
            words = para.split()
            if not words:
                continue
            cur = []
            for word in words:
                test_str = (" ".join(cur + [word])) if cur else word
                if font.size(test_str)[0] <= max_width_px:
                    cur.append(word)
                else:
                    if cur:
                        lines.append(" ".join(cur))
                        cur = [word]
                    else:
                        lines.append(word)
                        cur = []
            if cur:
                lines.append(" ".join(cur))
        return lines

    def _draw_startup_splash(self, message: str, progress: float = 0.5):
        pygame.event.pump()
        self.screen.fill(COLOR_BG)
        self.draw_grid_static()

        font_thanks = getattr(self, "font_splash_thanks", self.font_md)
        card_w = 820
        box_inner_max_w = card_w - 72 - 32
        raw_thanks = getattr(self, "thanks_text", "")
        thanks_lines = self._wrap_splash_thanks_text(raw_thanks, font_thanks, box_inner_max_w)
        line_height = 24
        extra_h = max(54, len(thanks_lines) * line_height + 22) if thanks_lines else 0
        card_h = 240 + extra_h
        cx = (self.width - card_w) // 2
        cy = (self.height - card_h) // 2

        card = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
        card.fill((20, 28, 40, 248))
        pygame.draw.rect(card, COLOR_SELECT_RING, (0, 0, card_w, card_h), 2, border_radius=10)
        self.screen.blit(card, (cx, cy))

        title = self.font_xl.render(f"Welcome to ApexBOSCreator {CREATOR_VERSION}", True, COLOR_EDIT_ACCENT)
        sub = self.font_md.render("Railroad Dispatcher Layout Authoring & Verification Studio", True, COLOR_TEXT)
        copy_txt = self.font_sm_bold.render("Copyright by AB  |  Licensed under the MIT License", True, COLOR_OK_GREEN)

        self.screen.blit(title, (cx + (card_w - title.get_width()) // 2, cy + 24))
        self.screen.blit(sub, (cx + (card_w - sub.get_width()) // 2, cy + 62))
        self.screen.blit(copy_txt, (cx + (card_w - copy_txt.get_width()) // 2, cy + 92))

        msg_surf = self.font_md.render(message, True, COLOR_TEXT)
        if thanks_lines:
            box_rect = pygame.Rect(cx + 36, cy + 120, card_w - 72, extra_h - 10)
            pygame.draw.rect(self.screen, (14, 20, 30), box_rect, border_radius=6)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, box_rect, 1, border_radius=6)
            for idx, tline in enumerate(thanks_lines):
                ts = font_thanks.render(tline, True, COLOR_LANDMARK)
                self.screen.blit(ts, (box_rect.x + (box_rect.width - ts.get_width()) // 2, box_rect.y + 10 + idx * line_height))
        msg_y = cy + 130 + extra_h
        self.screen.blit(msg_surf, (cx + (card_w - msg_surf.get_width()) // 2, msg_y))
        bar_w, bar_h = card_w - 100, 18
        bar_x, bar_y = cx + 50, msg_y + 32
        pygame.draw.rect(self.screen, (12, 16, 24), (bar_x, bar_y, bar_w, bar_h), border_radius=5)
        fill_w = max(4, int(bar_w * max(0.0, min(1.0, progress))))
        pygame.draw.rect(self.screen, COLOR_SELECT_RING, (bar_x, bar_y, fill_w, bar_h), border_radius=5)
        pygame.draw.rect(self.screen, COLOR_HUD_BORDER, (bar_x, bar_y, bar_w, bar_h), 1, border_radius=5)

        pygame.display.flip()

    def draw_grid_static(self):
        step = int(LOGICAL_GRID_SCALE)
        for x in range(0, self.width, step):
            pygame.draw.line(self.screen, COLOR_GRID_MINOR, (x, 0), (x, self.height), 1)
        for y in range(0, self.height, step):
            pygame.draw.line(self.screen, COLOR_GRID_MINOR, (0, y), (self.width, y), 1)

    def draw_grid(self):
        step_world = LOGICAL_GRID_SCALE * self.zoom
        if step_world < 8:
            return
        start_x = int(self.cam_x % step_world)
        start_y = int(self.cam_y % step_world)
        for x in range(start_x, self.width, max(1, int(step_world))):
            lx = round((x - self.cam_x) / step_world)
            col = COLOR_GRID_MAJOR if (lx % 5 == 0) else COLOR_GRID_MINOR
            pygame.draw.line(self.screen, col, (x, 0), (x, self.height), 1)
        for y in range(start_y, self.height, max(1, int(step_world))):
            ly = round((y - self.cam_y) / step_world)
            col = COLOR_GRID_MAJOR if (ly % 5 == 0) else COLOR_GRID_MINOR
            pygame.draw.line(self.screen, col, (0, y), (self.width, y), 1)

    def _capture_snapshot(self) -> dict:
        return {
            "layout_filepath": self.layout_filepath,
            "elements": [copy.deepcopy(el.to_config_dict()) for el in self.engine.elements.values()],
        }

    def _restore_snapshot(self, snapshot: Any):
        self.engine.elements.clear()
        if isinstance(snapshot, dict):
            elements_list = snapshot.get("elements", [])
            self.layout_filepath = snapshot.get("layout_filepath", self.layout_filepath)
            if hasattr(self.engine, "layout_filepath"):
                self.engine.layout_filepath = self.layout_filepath
            if hasattr(self.engine, "layout_filename"):
                self.engine.layout_filename = os.path.basename(self.layout_filepath) if self.layout_filepath else ""
        else:
            elements_list = snapshot

        type_map = {
            "track": TrackSegment, "turnout": Turnout, "signal": Signal, "crossing": Crossing,
            "base": BaseRadio, "track_jump": TrackJump, "ptc_datetime": PTCDateTime,
            "date_time": PTCDateTime, "datetime": PTCDateTime, "packet_scroller": DisplayElement,
            "address_table": DisplayElement, "train_scroller": DisplayElement, "chat": DisplayElement,
            "station": DisplayElement, "bridge": DisplayElement, "diamond": DisplayElement, "label": DisplayElement,
        }
        for item in copy.deepcopy(elements_list):
            cls = type_map.get(item["elem_type"], DisplayElement)
            if item.get("gps"): item["gps"] = tuple(item["gps"])
            valid_keys = {f.name for f in dataclasses.fields(cls) if f.init}
            filtered = {k: v for k, v in item.items() if k in valid_keys}
            self.engine.elements[item["id"]] = cls(**filtered)
        self._sync_monotonic_counters()
        self.selected_ids = {sid for sid in self.selected_ids if sid in self.engine.elements}
        self.crank_engine()

    def push_undo_state(self, action_desc: str = ""):
        self.undo_stack.append(self._capture_snapshot())
        self.redo_stack.clear()
        self.dirty = True
        if action_desc: self.status_toast = action_desc

    def undo(self):
        if not self.undo_stack: return
        self._group_rot_session = None
        self.redo_stack.append(self._capture_snapshot())
        prev_snap = self.undo_stack.pop()
        self._restore_snapshot(prev_snap)
        self.dirty = True
        self.status_toast = f"Undo performed ({len(self.undo_stack)} undo steps remaining)."

    def redo(self):
        if not self.redo_stack: return
        self._group_rot_session = None
        self.undo_stack.append(self._capture_snapshot())
        next_snap = self.redo_stack.pop()
        self._restore_snapshot(next_snap)
        self.dirty = True
        self.status_toast = f"Redo performed ({len(self.redo_stack)} redo steps remaining)."

    def _sync_monotonic_counters(self):
        for eid, el in self.engine.elements.items():
            pref = SHORT_ID_PREFIX.get(el.elem_type, "E")
            m = re.match(rf"^{re.escape(pref)}_?(\d+)$", eid, re.IGNORECASE)
            if m:
                num = int(m.group(1))
                if num >= self._monotonic_counters.get(el.elem_type, 1):
                    self._monotonic_counters[el.elem_type] = num + 1

    def _generate_unique_id(self, elem_type: str) -> str:
        pref = SHORT_ID_PREFIX.get(elem_type, "E")
        idx = self._monotonic_counters.get(elem_type, 1)
        while f"{pref}{idx}" in self.engine.elements:
            idx += 1
        self._monotonic_counters[elem_type] = idx + 1
        return f"{pref}{idx}"

    def logical_to_screen(self, lx: float, ly: float) -> Tuple[int, int]:
        wx = lx * LOGICAL_GRID_SCALE
        wy = ly * LOGICAL_GRID_SCALE
        return int(wx * self.zoom + self.cam_x), int(wy * self.zoom + self.cam_y)

    def screen_to_logical(self, sx: int, sy: int) -> Tuple[float, float]:
        wx = (sx - self.cam_x) / self.zoom
        wy = (sy - self.cam_y) / self.zoom
        return wx / LOGICAL_GRID_SCALE, wy / LOGICAL_GRID_SCALE

    def _get_element_label_font(self, bold: bool = False) -> pygame.font.Font:
        """Returns a reduced, zoom-scaled font (clamped to [7, 14]) for track, signal, and turnout names."""
        sz = max(7, min(14, int(round(9.0 * self.zoom))))
        key = (sz, bool(bold))
        if not hasattr(self, "_elem_label_font_cache"):
            self._elem_label_font_cache = {}
        if key not in self._elem_label_font_cache:
            self._elem_label_font_cache[key] = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", sz, bold=bold)
        return self._elem_label_font_cache[key]

    def _get_label_rect(self, sx: int, sy: int, w: int, h: int, pos: str, offset: int) -> Tuple[int, int]:
        p = str(pos).lower()
        if p == "bottom": return (sx - w // 2, sy + offset)
        if p == "left": return (sx - offset - w, sy - h // 2)
        if p == "right": return (sx + offset, sy - h // 2)
        return (sx - w // 2, sy - offset - h)

    def zoom_at_cursor(self, mouse_pos: Tuple[int, int], factor: float):
        mx, my = mouse_pos
        wx = (mx - self.cam_x) / self.zoom
        wy = (my - self.cam_y) / self.zoom
        new_zoom = max(self.min_zoom, min(self.max_zoom, self.zoom * factor))
        if abs(new_zoom - self.zoom) > 1e-4:
            self.zoom = new_zoom
            self.cam_x = mx - wx * self.zoom
            self.cam_y = my - wy * self.zoom

    def center_view_on_layout(self):
        """Centers the camera view to the center-of-mass (centroid) of all layout elements."""
        candidates = [
            el for el in self.engine.elements.values()
            if getattr(el, "elem_type", "") not in SYSTEM_LIST_TYPES
        ]
        if not candidates and self.engine.elements:
            candidates = list(self.engine.elements.values())
        if not candidates:
            self.cam_x = 55.0
            self.cam_y = 80.0
            return

        pts = []
        for el in candidates:
            if getattr(el, "elem_type", "") == "track":
                p0, p1 = LayoutTopologyAssistant.get_track_endpoints_logical(el)
                pts.append(p0)
                pts.append(p1)
            else:
                pts.append((float(el.x), float(el.y)))

        if not pts:
            pts = [(float(el.x), float(el.y)) for el in candidates]

        cx = sum(p[0] for p in pts) / float(len(pts))
        cy = sum(p[1] for p in pts) / float(len(pts))

        target_sx = self.width / 2.0
        target_sy = self.height / 2.0
        wx = cx * LOGICAL_GRID_SCALE
        wy = cy * LOGICAL_GRID_SCALE
        self.cam_x = target_sx - wx * self.zoom
        self.cam_y = target_sy - wy * self.zoom

    def reset_camera(self):
        self.zoom = 1.0
        self.center_view_on_layout()
        self.status_toast = "Camera reset to layout center-mass"

    def get_active_ref_gps(self, element: Optional[BaseElement] = None) -> Optional[Tuple[float, float]]:
        if element is not None:
            eg = _parse_wiu_gps(getattr(element, "gps", None))
            if eg is not None:
                return eg
        if getattr(self, "gps_specified", False):
            sg = _parse_wiu_gps(getattr(self, "specified_gps", None))
            if sg is not None:
                return sg
        mg = _parse_wiu_gps(getattr(self, "last_map_picked_gps", None))
        if mg is not None:
            return mg
        return None

    def create_element_at(self, elem_type: str, lx: int, ly: int, screen_pos: Tuple[int, int], initial_gps: Optional[Tuple[float, float]] = None, auto_connect: bool = True):
        self.push_undo_state()
        new_id = self._generate_unique_id(elem_type)
        ref_gps = _parse_wiu_gps(initial_gps) or self.get_active_ref_gps()
        sugg_list = self.wiu_cache.get_suggestion_list(self.engine.elements, ref_gps=ref_gps)
        suggested_wiu = 12201
        for s_w in sugg_list:
            if re.fullmatch(r"-?\d+", str(s_w).strip()):
                suggested_wiu = int(s_w)
                break

        if elem_type == "track":
            el = TrackSegment(id=new_id, elem_type="track", x=lx, y=ly, label="", orientation="E-W", length_miles=0.5)
        elif elem_type == "turnout":
            el = Turnout(id=new_id, elem_type="turnout", x=lx, y=ly, label="", wiu_id=suggested_wiu, cmd_bit_offset=0)
        elif elem_type == "signal":
            el = Signal(id=new_id, elem_type="signal", x=lx, y=ly, label="", wiu_id=suggested_wiu, sig_bit_offset=0, position="top", direction="E")
            el._signal_val = 24; el._raw_aspect_str = "green"; el._lamp_aspect = "green"
        elif elem_type == "crossing":
            el = Crossing(id=new_id, elem_type="crossing", x=lx, y=ly, label="")
        elif elem_type == "base":
            el = BaseRadio(id=new_id, elem_type="base", x=lx, y=ly, label="", channel=1)
        elif elem_type == "track_jump":
            letter_counts: Dict[str, List[TrackJump]] = {}
            for existing in self.engine.elements.values():
                if isinstance(existing, TrackJump):
                    ltr = (existing.letter or "A").strip().upper()
                    letter_counts.setdefault(ltr, []).append(existing)
            chosen_letter = None
            partner_jump: Optional[TrackJump] = None
            for ltr in sorted(letter_counts.keys()):
                if len(letter_counts[ltr]) == 1:
                    chosen_letter = ltr
                    partner_jump = letter_counts[ltr][0]
                    break
            if not chosen_letter:
                for i in range(26):
                    cand_ltr = chr(ord("A") + i)
                    if cand_ltr not in letter_counts:
                        chosen_letter = cand_ltr
                        break
                if not chosen_letter:
                    chosen_letter = "A"
            paired_id = partner_jump.id if partner_jump else ""
            el = TrackJump(id=new_id, elem_type="track_jump", x=lx, y=ly, label="", letter=chosen_letter, paired_jump_id=paired_id, direction="E")
            if partner_jump:
                partner_jump.paired_jump_id = new_id
        elif elem_type in SYSTEM_LIST_TYPES:
            def_w = 580 if elem_type == "packet_scroller" else (450 if elem_type == "address_table" else (460 if elem_type == "chat" else 430))
            def_h = 195 if elem_type == "packet_scroller" else (220 if elem_type == "chat" else 150)
            def_rows = "12" if elem_type == "chat" else "10"
            el = DisplayElement(id=new_id, elem_type=elem_type, x=lx, y=ly, label="", extra_info=def_rows, width=def_w, height=def_h)
        elif elem_type == "ptc_datetime":
            el = PTCDateTime(id=new_id, elem_type="ptc_datetime", x=lx, y=ly, label="PTC Clock", width=240, height=50)
        else:
            el = DisplayElement(id=new_id, elem_type=elem_type, x=lx, y=ly, label="")

        if initial_gps:
            el.gps = (float(initial_gps[0]), float(initial_gps[1]))
            self.last_selected_gps = el.gps

        self.engine.elements[new_id] = el
        if elem_type not in SYSTEM_LIST_TYPES and auto_connect:
            LayoutTopologyAssistant.auto_suggest_connections(el, self.engine.elements)
        self.crank_engine()
        self.last_placed_element_id = new_id

        self.selected_ids = {new_id}
        self.inplace_editor = InPlaceElementEditor(
            element=el, screen_pos=screen_pos, all_elements=self.engine.elements,
            wiu_cache=self.wiu_cache, get_last_gps_cb=lambda: self.last_selected_gps,
            set_last_gps_cb=self._update_last_selected_gps, default_radius=self.default_radius,
            is_newly_created=True,
            get_ref_gps_cb=lambda el_ref=el: self.get_active_ref_gps(el_ref),
        )
        self.status_toast = f"Placed {elem_type} '{new_id}' at ({lx}, {ly})."

    def _update_last_selected_gps(self, gps_pair: Tuple[float, float]):
        self.last_selected_gps = (float(gps_pair[0]), float(gps_pair[1]))
        parsed_g = _parse_wiu_gps(gps_pair)
        if parsed_g is not None:
            self.specified_gps = parsed_g
            self.gps_specified = True

    def _apply_track_endpoint_drag(self, trk: TrackSegment, fixed_pt: Tuple[float, float], drag_pt: Tuple[float, float], finalize_subdivision: bool = False):
        pts = LayoutTopologyAssistant.compute_45deg_polyline(fixed_pt, drag_pt)
        p_start, p_mid = pts[0], pts[1]
        dx1, dy1 = p_mid[0] - p_start[0], p_mid[1] - p_start[1]
        dist1 = max(0.8, math.hypot(dx1, dy1))

        trk.x = int(round((p_start[0] + p_mid[0]) / 2.0))
        trk.y = int(round((p_start[1] + p_mid[1]) / 2.0))
        trk.orientation = LayoutTopologyAssistant.vector_to_track_orientation(dx1, dy1)
        trk.length_miles = max(0.1, round(dist1 / LayoutTopologyAssistant.LOGICAL_UNITS_PER_MILE, 2))

        if finalize_subdivision and len(pts) == 3:
            p_end = pts[2]
            dx2, dy2 = p_end[0] - p_mid[0], p_end[1] - p_mid[1]
            dist2 = math.hypot(dx2, dy2)
            if dist2 >= 1.5:
                new_id = self._generate_unique_id("track")
                cx2, cy2 = int(round((p_mid[0] + p_end[0]) / 2.0)), int(round((p_mid[1] + p_end[1]) / 2.0))
                orient2 = LayoutTopologyAssistant.vector_to_track_orientation(dx2, dy2)
                len_mi2 = max(0.1, round(dist2 / LayoutTopologyAssistant.LOGICAL_UNITS_PER_MILE, 2))
                cdir = LayoutTopologyAssistant.vector_to_compass(dx2, dy2)
                opp_dir = OPPOSITE_DIR[cdir]

                sec2 = TrackSegment(id=new_id, elem_type="track", x=cx2, y=cy2, label="", orientation=orient2, length_miles=len_mi2, neighbors={opp_dir: [trk.id]})
                trk.neighbors.setdefault(cdir, [])
                if new_id not in trk.neighbors[cdir]: trk.neighbors[cdir].append(new_id)

                self.engine.elements[new_id] = sec2
                LayoutTopologyAssistant.auto_suggest_connections(sec2, self.engine.elements)
                self.status_toast = f"Adjusted '{trk.id}' ({trk.length_miles}mi {trk.orientation}) & added 45° section '{new_id}'."
                return

        if finalize_subdivision:
            LayoutTopologyAssistant.auto_suggest_connections(trk, self.engine.elements)
            self.status_toast = f"Updated '{trk.id}' length to {trk.length_miles} mi ({trk.orientation})."

    def copy_selected(self):
        if not self.selected_ids:
            self.status_toast = "Nothing selected to copy."
            return
        self.clipboard = [copy.deepcopy(self.engine.elements[eid].to_config_dict()) for eid in self.selected_ids if eid in self.engine.elements]
        self.status_toast = f"Copied {len(self.clipboard)} element(s)."

    def paste_clipboard(self):
        if not self.clipboard: return
        self.push_undo_state()
        id_remap: Dict[str, str] = {}
        new_configs: List[dict] = []
        for item in self.clipboard:
            old_id = item["id"]
            new_id = self._generate_unique_id(item["elem_type"])
            self.engine.elements[new_id] = DisplayElement(id=new_id, elem_type="label", x=0, y=0)
            id_remap[old_id] = new_id
            c_item = copy.deepcopy(item)
            c_item["id"] = new_id
            c_item["x"] = int(c_item.get("x", 0)) + 3
            c_item["y"] = int(c_item.get("y", 0)) + 2
            new_configs.append(c_item)

        type_map = {"track": TrackSegment, "turnout": Turnout, "signal": Signal, "crossing": Crossing, "base": BaseRadio, "track_jump": TrackJump, "ptc_datetime": PTCDateTime, "date_time": PTCDateTime, "datetime": PTCDateTime}
        pasted_ids: Set[str] = set()
        for cfg in new_configs:
            if cfg["elem_type"] == "track" and "neighbors" in cfg:
                cfg["neighbors"] = {d: [id_remap.get(nid, nid) for nid in nlist] for d, nlist in cfg["neighbors"].items()}
            elif cfg["elem_type"] == "turnout":
                for k in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                    if cfg.get(k) in id_remap: cfg[k] = id_remap[cfg[k]]
            elif cfg["elem_type"] == "signal":
                for k in ("protects_element", "rear_element"):
                    if cfg.get(k) in id_remap: cfg[k] = id_remap[cfg[k]]
            elif cfg["elem_type"] == "track_jump":
                for k in ("connected_element", "paired_jump_id"):
                    if cfg.get(k) in id_remap: cfg[k] = id_remap[cfg[k]]

            cls = type_map.get(cfg["elem_type"], DisplayElement)
            if cfg.get("gps"): cfg["gps"] = tuple(cfg["gps"])
            valid_keys = {f.name for f in dataclasses.fields(cls) if f.init}
            filtered = {k: v for k, v in cfg.items() if k in valid_keys}
            self.engine.elements[cfg["id"]] = cls(**filtered)
            pasted_ids.add(cfg["id"])

        self.selected_ids = pasted_ids
        self.crank_engine()
        self.status_toast = f"Pasted {len(pasted_ids)} element(s)."

    def delete_selected(self):
        if not self.selected_ids: return
        self.push_undo_state()
        removed = list(self.selected_ids)
        for eid in removed: self.engine.elements.pop(eid, None)
        for el in self.engine.elements.values():
            if isinstance(el, TrackSegment):
                for d in list(el.neighbors.keys()): el.neighbors[d] = [x for x in el.neighbors[d] if x not in self.selected_ids]
                if el.parallel_track_id in self.selected_ids: el.parallel_track_id = None
            elif isinstance(el, Turnout):
                if el.common_neighbor in self.selected_ids: el.common_neighbor = ""
                if el.normal_neighbor in self.selected_ids: el.normal_neighbor = ""
                if el.reverse_neighbor in self.selected_ids: el.reverse_neighbor = ""
                el.guarded_by_signals = [x for x in el.guarded_by_signals if x not in self.selected_ids]
            elif isinstance(el, Signal):
                if el.protects_element in self.selected_ids: el.protects_element = ""
                if el.rear_element in self.selected_ids: el.rear_element = ""
            elif isinstance(el, Crossing):
                el.monitored_tracks = [x for x in el.monitored_tracks if x not in self.selected_ids]
            elif isinstance(el, TrackJump):
                if el.connected_element in self.selected_ids: el.connected_element = ""
                if el.paired_jump_id in self.selected_ids: el.paired_jump_id = ""
        self.selected_ids.clear()
        self.crank_engine()
        self.status_toast = f"Deleted {len(removed)} element(s)."

    def merge_layout_from_file(self, incoming_filepath: str):
        if not os.path.exists(incoming_filepath): return
        try:
            self.push_undo_state()
            with open(incoming_filepath, "r", encoding="utf-8") as f: data = json.load(f)
            type_map = {"track": TrackSegment, "turnout": Turnout, "signal": Signal, "crossing": Crossing, "base": BaseRadio, "track_jump": TrackJump, "ptc_datetime": PTCDateTime, "date_time": PTCDateTime, "datetime": PTCDateTime}
            added, discarded = 0, 0
            for item in data.get("elements", []):
                eid = item["id"]
                if eid in self.engine.elements:
                    discarded += 1
                    continue
                item_copy = dict(item)
                cls = type_map.get(item_copy["elem_type"], DisplayElement)
                if item_copy.get("gps"): item_copy["gps"] = tuple(item_copy["gps"])
                valid_keys = {f.name for f in dataclasses.fields(cls) if f.init}
                filtered = {k: v for k, v in item_copy.items() if k in valid_keys}
                new_el = cls(**filtered)
                self.engine.elements[eid] = new_el
                if new_el.wiu_id is not None:
                    self.wiu_cache.register_wiu(
                        new_el.wiu_id,
                        name=getattr(new_el, "label", ""),
                        gps=getattr(new_el, "gps", None),
                        ctc_addresses=getattr(new_el, "ctc_id", ""),
                    )
                added += 1
            self._sync_monotonic_counters()
            self.crank_engine()
            self.center_view_on_layout()
            self.status_toast = f"Merged '{incoming_filepath}': Added {added} new, discarded {discarded} duplicates."
        except Exception as exc:
            self.status_toast = f"[ERROR] Merge failed: {exc}"

    def prompt_save_fragment(self):
        os.makedirs(FRAGS_DIR, exist_ok=True)
        if not self.engine.elements:
            self.status_toast = "[ERROR] Layout has no elements to save as fragment."
            return
        if self.last_saved_frag_name:
            default_name = self.last_saved_frag_name
        else:
            existing_frags = [f for f in os.listdir(FRAGS_DIR) if f.lower().endswith(".json")]
            default_name = f"frag_{len(existing_frags) + 1}"
        self.file_prompt_mode = "save_frag"
        self.file_prompt_buffer = default_name
        sel_count = len(self.selected_ids) if self.selected_ids else len(self.engine.elements)
        scope = f"{sel_count} selected element(s)" if self.selected_ids else f"all {sel_count} element(s)"
        self.status_toast = f"Enter fragment name to save {scope} to '{FRAGS_DIR}'."

    def save_fragment(self, frag_name: str) -> Optional[Tuple[str, str]]:
        os.makedirs(FRAGS_DIR, exist_ok=True)
        clean_name = os.path.basename(frag_name.strip())
        if clean_name.lower().endswith(".json"):
            clean_name = clean_name[:-5]
        if not clean_name:
            clean_name = "frag_1"

        if self.selected_ids:
            target_elements = [self.engine.elements[eid] for eid in self.selected_ids if eid in self.engine.elements]
        else:
            target_elements = list(self.engine.elements.values())

        if not target_elements:
            self.status_toast = "[ERROR] No elements to save as fragment."
            return None

        json_path = os.path.join(FRAGS_DIR, f"{clean_name}.json")
        png_path = os.path.join(FRAGS_DIR, f"{clean_name}.png")

        frag_data = {
            "version": getattr(self.engine, "version", CREATOR_VERSION),
            "layout_filename": f"{clean_name}.json",
            "is_fragment": True,
            "elements": [el.to_config_dict() for el in target_elements],
        }

        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(frag_data, f, indent=2)

            thumb_surf = render_fragment_thumbnail(target_elements, size=(220, 140))
            pygame.image.save(thumb_surf, png_path)

            self.last_saved_frag_name = clean_name
            self.status_toast = f"Saved fragment '{clean_name}' ({len(target_elements)} elements) & thumbnail to 'frags/'."
            return (json_path, png_path)
        except Exception as exc:
            self.status_toast = f"[ERROR] Failed to save fragment '{clean_name}': {exc}"
            return None

    def open_frag_picker_modal(self):
        os.makedirs(FRAGS_DIR, exist_ok=True)
        self.frag_picker_modal = FragmentPickerModal(
            frags_dir=FRAGS_DIR,
            on_load_fragment_cb=self.load_and_merge_fragment,
            on_close_cb=self.close_frag_picker_modal,
        )

    def close_frag_picker_modal(self):
        self.frag_picker_modal = None

    def open_banc_coverage_modal(self):
        if not getattr(self, "banc_client", None):
            self.banc_client = mosfet_client.create_banc_client_from_config(getattr(self, "banc_config_path", None), prefer_section="creator")
        self.banc_coverage_modal = BancCoverageMapModal(
            tile_mgr=self.tile_map_mgr,
            client=self.banc_client,
            on_download_and_open_cb=self.request_open_layout_from_banc,
            on_upload_current_cb=self.upload_current_to_banc,
            on_close_cb=self.close_banc_coverage_modal,
        )

    def close_banc_coverage_modal(self):
        self.banc_coverage_modal = None

    def request_open_layout_from_banc(self, layout_dict: dict, layout_name: str):
        if getattr(self, "dirty", False):
            self.confirm_load_modal = True
            self.pending_load_target = ("banc", layout_dict, layout_name)
            return
        self.open_layout_from_banc(layout_dict, layout_name)

    def open_layout_from_banc(self, layout_dict: dict, layout_name: str):
        clean_name = os.path.basename(layout_name)
        if not clean_name.lower().endswith(".json"):
            clean_name += ".json"
        local_filepath = clean_name
        try:
            with open(local_filepath, "w", encoding="utf-8") as f:
                json.dump(layout_dict, f, indent=2)
        except Exception:
            pass
        self.push_undo_state()
        self.layout_filepath = local_filepath
        self.engine.load_layout_data(layout_dict, layout_filename=os.path.basename(local_filepath), filepath=os.path.abspath(local_filepath))
        self._sync_monotonic_counters()
        self.selected_ids.clear()
        self.dirty = False
        self.crank_engine()
        self.center_view_on_layout()
        self.status_toast = f"Loaded Banc layout '{layout_name}' as local file '{local_filepath}'."
        self.banc_coverage_modal = None

    def upload_current_to_banc(self, layout_name: Optional[str] = None):
        if not getattr(self, "banc_client", None):
            self.banc_client = mosfet_client.create_banc_client_from_config(getattr(self, "banc_config_path", None), prefer_section="creator")
        if not self.banc_client:
            self.status_toast = "Banc upload error: Banc client not configured."
            return
        name = layout_name
        if not name:
            if self.layout_filepath:
                name = os.path.splitext(os.path.basename(self.layout_filepath))[0]
            elif getattr(self.engine, "layout_filename", None):
                name = os.path.splitext(os.path.basename(self.engine.layout_filename))[0]
            else:
                name = "layout"
        try:
            layout_data = self.engine.get_layout_dict()
            self.banc_client.upload_layout_sync(name, layout_data)
            self.status_toast = f"Uploaded layout '{name}' to Banc Cloud!"
            if self.banc_coverage_modal:
                self.banc_coverage_modal.refresh()
        except Exception as exc:
            self.status_toast = f"Banc upload error: {exc}"

    def load_and_merge_fragment(self, json_path: str):
        self.frag_picker_modal = None
        if not os.path.exists(json_path):
            self.status_toast = f"[ERROR] Fragment file '{json_path}' not found."
            return
        self.merge_fragment_from_file(json_path)

    def merge_fragment_from_file(self, incoming_filepath: str):
        if not os.path.exists(incoming_filepath):
            self.status_toast = f"[ERROR] Fragment file '{incoming_filepath}' does not exist."
            return
        try:
            self.push_undo_state()
            with open(incoming_filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            frag_elements = data.get("elements", [])
            if not frag_elements:
                self.status_toast = f"[WARNING] Fragment '{os.path.basename(incoming_filepath)}' contains no elements."
                return

            existing_ids = set(self.engine.elements.keys())
            remapped_elements, id_remap = remap_fragment_elements(existing_ids, frag_elements)

            type_map = {
                "track": TrackSegment, "turnout": Turnout, "signal": Signal, "crossing": Crossing,
                "base": BaseRadio, "track_jump": TrackJump, "ptc_datetime": PTCDateTime,
                "date_time": PTCDateTime, "datetime": PTCDateTime,
                "station": DisplayElement, "label": DisplayElement, "bridge": DisplayElement,
                "diamond": DisplayElement, "packet_scroller": DisplayElement,
                "address_table": DisplayElement, "train_scroller": DisplayElement, "chat": DisplayElement,
            }

            new_ids = []
            for item in remapped_elements:
                eid = item["id"]
                item_copy = dict(item)
                cls = type_map.get(item_copy.get("elem_type", ""), DisplayElement)
                if item_copy.get("gps"):
                    item_copy["gps"] = tuple(item_copy["gps"])
                valid_keys = {f.name for f in dataclasses.fields(cls) if f.init}
                filtered = {k: v for k, v in item_copy.items() if k in valid_keys}
                new_el = cls(**filtered)
                self.engine.elements[eid] = new_el
                if getattr(new_el, "wiu_id", None) is not None:
                    self.wiu_cache.register_wiu(
                        new_el.wiu_id,
                        name=getattr(new_el, "label", ""),
                        gps=getattr(new_el, "gps", None),
                        ctc_addresses=getattr(new_el, "ctc_id", ""),
                    )
                new_ids.append(eid)

            self._sync_monotonic_counters()
            self.dirty = True
            self.selected_ids = set(new_ids)
            self.crank_engine()

            if len(existing_ids) == 0:
                self.center_view_on_layout()

            if not self.last_saved_frag_name:
                bname = os.path.basename(incoming_filepath)
                if bname.lower().endswith(".json"):
                    bname = bname[:-5]
                self.last_saved_frag_name = bname

            remap_count = sum(1 for old_id, new_id in id_remap.items() if old_id != new_id)
            remap_msg = f" ({remap_count} IDs remapped)" if remap_count > 0 else ""
            self.status_toast = f"Merged fragment '{os.path.basename(incoming_filepath)}': {len(new_ids)} elements added{remap_msg}."
        except Exception as exc:
            self.status_toast = f"[ERROR] Failed to merge fragment: {exc}"

    def new_layout(self, force: bool = False):
        """Creates a new empty layout by clearing all elements and resetting the layout name.
        If changes are pending (self.dirty is True) and not force, asks the user to save changes.
        """
        if not force and getattr(self, "dirty", False):
            self.confirm_new_modal = True
            return

        self.confirm_new_modal = False
        self.pending_post_save_action = None
        self.push_undo_state()
        self.engine.elements.clear()
        self.layout_filepath = ""
        if hasattr(self.engine, "layout_filepath"):
            self.engine.layout_filepath = ""
        if hasattr(self.engine, "layout_filename"):
            self.engine.layout_filename = ""
        self.selected_ids.clear()
        self.dragging_track_end = None
        self.dragging_selection = False
        self.inplace_editor = None
        self.last_saved_frag_name = None
        self._monotonic_counters = {etype: 1 for etype in ELEMENT_TYPES}
        self.cam_x = self.width / 2.0
        self.cam_y = self.height / 2.0
        self.zoom = 1.0
        self.dirty = False
        self.crank_engine()
        self.status_toast = "Created new layout: all elements and layout name cleared."

    def _handle_save_and_new(self):
        """Save pending changes and then proceed with creating a new layout."""
        self.confirm_new_modal = False
        if not self.layout_filepath:
            self.pending_post_save_action = "new_layout"
            self.file_prompt_mode = "save_as"
            self.file_prompt_buffer = "new_layout.json"
            self.status_toast = "Save changes to file before creating new layout (Enter=Save, Esc=Cancel)."
        else:
            self.save_current_layout()
            self.new_layout(force=True)
            self.status_toast = "Saved changes. Created new layout."

    def request_load_layout(self, target_path: Optional[str] = None):
        """
        When a load layout is selected, check if changes are pending.
        If changes are pending (self.dirty is True), prompts the user to save, discard, or cancel.
        """
        if getattr(self, "dirty", False):
            self.confirm_load_modal = True
            self.pending_load_target = target_path
            return
        self._proceed_with_load_layout(target_path)

    def _proceed_with_load_layout(self, target_path: Optional[str] = None):
        self.confirm_load_modal = False
        target = target_path if target_path is not None else self.pending_load_target
        self.pending_load_target = None
        if target is None:
            self.file_prompt_mode = "load"
            self.file_prompt_buffer = self.layout_filepath or "complex_layout.json"
        elif isinstance(target, tuple) and len(target) == 3 and target[0] == "banc":
            _, layout_dict, layout_name = target
            self.open_layout_from_banc(layout_dict, layout_name)
        elif isinstance(target, tuple) and len(target) == 2 and target[0] == "import_itcvl":
            _, itcvl_path = target
            if itcvl_path:
                self.import_itcvl_layout(itcvl_path)
            else:
                default_itcvl = "UPMOFAT.itcvl" if os.path.exists("UPMOFAT.itcvl") else ("SoCalAll.itcvl" if os.path.exists("SoCalAll.itcvl") else "")
                self.open_file_prompt("import_itcvl", default_itcvl)
        elif isinstance(target, tuple) and len(target) == 2 and target[0] == "import_atcs":
            _, atcs_path = target
            if atcs_path:
                self.import_atcs_layout(atcs_path)
            else:
                default_atcs = "up_yuma_0201_20.lay" if os.path.exists("up_yuma_0201_20.lay") else ""
                self.open_file_prompt("import_atcs", default_atcs)
        elif isinstance(target, str):
            self._execute_load_layout(target)

    def _execute_load_layout(self, target_path: str):
        if os.path.exists(target_path):
            self.push_undo_state()
            self.layout_filepath = target_path
            self.engine.load_layout(target_path)
            self._sync_monotonic_counters()
            self.selected_ids.clear()
            self.dirty = False
            self.crank_engine()
            self.center_view_on_layout()
            self.status_toast = f"Loaded layout '{target_path}'."
        else:
            self.status_toast = f"[ERROR] File '{target_path}' does not exist."

    def _handle_save_and_load(self):
        """Save pending changes and then proceed with loading layout."""
        self.confirm_load_modal = False
        if not self.layout_filepath:
            self.pending_post_save_action = ("load", self.pending_load_target)
            self.file_prompt_mode = "save_as"
            self.file_prompt_buffer = "new_layout.json"
            self.status_toast = "Save changes to file before loading layout (Enter=Save, Esc=Cancel)."
        else:
            self.save_current_layout()
            self._proceed_with_load_layout()
            self.status_toast = "Saved changes. Loading layout..."

    def _handle_discard_and_load(self):
        """Discard pending changes and proceed with loading layout."""
        self.dirty = False
        self._proceed_with_load_layout()

    def request_import_itcvl(self, target_path: Optional[str] = None):
        """
        Prompts to import an .itcvl layout file. If unsaved changes are pending,
        asks to save, discard, or cancel first.
        """
        if getattr(self, "dirty", False):
            self.confirm_load_modal = True
            self.pending_load_target = ("import_itcvl", target_path)
            return
        if target_path:
            self.import_itcvl_layout(target_path)
        else:
            default_itcvl = "UPMOFAT.itcvl" if os.path.exists("UPMOFAT.itcvl") else ("SoCalAll.itcvl" if os.path.exists("SoCalAll.itcvl") else "")
            self.open_file_prompt("import_itcvl", default_itcvl)

    def import_itcvl_layout(self, filepath: str):
        """
        Imports an .itcvl layout file using itcvl_converter, saving templates as fragments
        in frags/itcvl, loading the converted layout into the Creator, and marking it ready
        to be saved as one's own layout.
        """
        if not os.path.exists(filepath):
            self.status_toast = f"[ERROR] ITCVL file '{filepath}' does not exist."
            return
        try:
            import itcvl_converter
            frags_base = getattr(self, "FRAGS_DIR", getattr(sys.modules.get("ApexBOSCreator", None), "FRAGS_DIR", FRAGS_DIR))
            itcvl_frags_base = os.path.join(frags_base, "itcvl")
            itcvl_frags_dir = itcvl_converter.resolve_itcvl_frags_subdir(filepath, itcvl_frags_base)
            stem = os.path.splitext(os.path.basename(filepath))[0]
            sub_folder_name = os.path.basename(itcvl_frags_dir)
            own_filename = f"{stem}.json"
            own_filepath = os.path.join(os.path.dirname(os.path.abspath(filepath)) or os.getcwd(), own_filename)

            layout_dict = itcvl_converter.convert_itcvl_file(
                filepath,
                output_layout_path=None,
                frags_dir=itcvl_frags_dir,
            )
            self.push_undo_state()
            self.layout_filepath = own_filepath
            self.engine.load_layout_data(
                layout_dict,
                layout_filename=own_filename,
                config_dir=os.path.dirname(own_filepath) or os.getcwd(),
                filepath=own_filepath,
            )
            self._sync_monotonic_counters()
            self.selected_ids.clear()
            self.dirty = True
            self.crank_engine()
            self.center_view_on_layout()
            self.status_toast = (
                f"Imported '{os.path.basename(filepath)}' ({len(self.engine.elements)} elements, "
                f"templates -> frags/itcvl/{sub_folder_name}). Save / SaveAs to save as '{own_filename}'."
            )
        except Exception as exc:
            self.status_toast = f"[ERROR] Failed to import ITCVL '{filepath}': {exc}"

    def request_import_atcs(self, target_path: Optional[str] = None):
        """
        Prompts to import an ATCSMonitor .lay layout file. If unsaved changes are pending,
        asks to save, discard, or cancel first.
        """
        if getattr(self, "dirty", False):
            self.confirm_load_modal = True
            self.pending_load_target = ("import_atcs", target_path)
            return
        if target_path:
            self.import_atcs_layout(target_path)
        else:
            default_atcs = "up_yuma_0201_20.lay" if os.path.exists("up_yuma_0201_20.lay") else ""
            self.open_file_prompt("import_atcs", default_atcs)

    def import_atcs_layout(self, filepath: str):
        """
        Imports a legacy ATCSMonitor (.lay) binary layout file using atcs_converter,
        saving per-Control-Point fragments in frags/atcs, loading the converted layout
        into the Creator, and marking it ready to update WIU IDs and save as an ApexBOS layout.
        """
        if not os.path.exists(filepath):
            self.status_toast = f"[ERROR] ATCS file '{filepath}' does not exist."
            return
        try:
            import atcs_converter
            frags_base = getattr(self, "FRAGS_DIR", getattr(sys.modules.get("ApexBOSCreator", None), "FRAGS_DIR", FRAGS_DIR))
            atcs_frags_base = os.path.join(frags_base, "atcs")
            atcs_frags_dir = atcs_converter.resolve_atcs_frags_subdir(filepath, atcs_frags_base)
            stem = os.path.splitext(os.path.basename(filepath))[0]
            sub_folder_name = os.path.basename(atcs_frags_dir)
            own_filename = f"{stem}.json"
            own_filepath = os.path.join(os.path.dirname(os.path.abspath(filepath)) or os.getcwd(), own_filename)

            layout_dict = atcs_converter.convert_atcs_file(
                filepath,
                output_layout_path=None,
                frags_dir=atcs_frags_dir,
            )
            self.push_undo_state()
            self.layout_filepath = own_filepath
            self.engine.load_layout_data(
                layout_dict,
                layout_filename=own_filename,
                config_dir=os.path.dirname(own_filepath) or os.getcwd(),
                filepath=own_filepath,
            )
            self._sync_monotonic_counters()
            self.selected_ids.clear()
            self.dirty = True
            self.crank_engine()
            self.center_view_on_layout()
            self.status_toast = (
                f"Imported ATCS '{os.path.basename(filepath)}' ({len(self.engine.elements)} elements, "
                f"CP fragments -> frags/atcs/{sub_folder_name}). Update WIU IDs and Save / SaveAs as '{own_filename}'."
            )
        except Exception as exc:
            self.status_toast = f"[ERROR] Failed to import ATCS '{filepath}': {exc}"

    def save_current_layout(self, filepath: Optional[str] = None):
        target = filepath or self.layout_filepath
        if not target:
            self.file_prompt_mode = "save_as"
            self.file_prompt_buffer = "new_layout.json"
            self.status_toast = "Enter filepath to save layout."
            return
        self.layout_filepath = target
        self.engine.save_layout(target)
        self.dirty = False
        self.status_toast = f"Saved layout to '{target}'."

    def toggle_physical_state_on_element(self, el: BaseElement):
        now = time.time()
        el._last_packet_ts = now
        if isinstance(el, TrackSegment):
            if not el._hw_occupied and not el._inferred_occupied:
                el._hw_occupied = True; el._train_direction = "E"
                self.engine.record_train_sighting(el.id, now, "E")
            elif el._hw_occupied and el._train_direction == "E":
                el._train_direction = "W"
                self.engine.record_train_sighting(el.id, now, "W")
            elif el._hw_occupied:
                if self.enable_inference_logic:
                    el._hw_occupied = False; el._inferred_occupied = True; el._inferred_timestamp = now + 3600.0; el._train_direction = "E"
                else:
                    el._hw_occupied = False; el._inferred_occupied = False; el._train_direction = None; el._estimated_speed_mph = None; el._train_name = None
            else:
                el._hw_occupied = False; el._inferred_occupied = False; el._train_direction = None; el._estimated_speed_mph = None; el._train_name = None
        elif isinstance(el, Turnout):
            if el._state == 0 and not el._hw_occupied: el._state = 1
            elif el._state == 1 and not el._hw_occupied:
                el._state = 0; el._hw_occupied = True
                self.engine.record_train_sighting(el.id, now, el.orientation)
            else:
                el._state = 0; el._hw_occupied = False; el._inferred_occupied = False
        elif isinstance(el, Signal):
            cur_aspect = el._lamp_aspect
            aspect_names = [a[1] for a in SIM_ASPECT_CYCLE]
            try: idx = (aspect_names.index(cur_aspect) + 1) % len(SIM_ASPECT_CYCLE)
            except ValueError: idx = 0
            was_perm = el.is_permissive
            code, new_aspect = SIM_ASPECT_CYCLE[idx]
            el._signal_val = code; el._raw_aspect_str = new_aspect; el._lamp_aspect = new_aspect
            c = el.base_color
            if c not in ("GRAY", "DARK", "BLACK"):
                el._last_reported_color = c
            if was_perm and el.is_restricted:
                el._dropped_to_red_ts = now
                if self.enable_inference_logic:
                    self.engine.notify_signal_dropped(el, now)
        self.crank_engine()
        self.status_toast = f"Cranked engine after toggling state on '{el.id}'"

    def turn_all_signals_to_stop(self):
        """Sets all signals on the layout to Stop (red aspect) in simulation mode."""
        now = time.time()
        count = 0
        for el in list(self.engine.elements.values()):
            if isinstance(el, Signal) or getattr(el, "elem_type", "") == "signal":
                was_perm = getattr(el, "is_permissive", False)
                el._signal_val = 30
                el._raw_aspect_str = "red"
                el._lamp_aspect = "red"
                c = getattr(el, "base_color", "RED")
                if c not in ("GRAY", "DARK", "BLACK"):
                    el._last_reported_color = c
                if was_perm and getattr(el, "is_restricted", True):
                    el._dropped_to_red_ts = now
                    if getattr(self, "enable_inference_logic", True):
                        self.engine.notify_signal_dropped(el, now)
                count += 1
        self.crank_engine()
        self.status_toast = f"Turned {count} signal(s) to STOP (Red)."

    def _dispatch_broadcast(self, state: dict):
        """Asynchronously dispatches state broadcast to prevent freezing the Pygame GUI event loop."""
        if not getattr(self, "_broadcast_worker_started", False):
            self._broadcast_worker_started = True
            def _worker():
                while True:
                    try:
                        st = self._broadcast_queue.get()
                        self.engine.broadcast_state(st)
                    except Exception:
                        pass
                    finally:
                        self._broadcast_queue.task_done()
            t = threading.Thread(target=_worker, daemon=True, name="CreatorBroadcastWorker")
            t.start()

        if not hasattr(self, "_broadcast_queue"):
            self._broadcast_queue = queue.Queue(maxsize=2)

        try:
            self._broadcast_queue.put_nowait(state)
        except queue.Full:
            try:
                self._broadcast_queue.get_nowait()
                self._broadcast_queue.task_done()
            except Exception:
                pass
            try:
                self._broadcast_queue.put_nowait(state)
            except Exception:
                pass

    def crank_engine(self):
        from train_logic import TrainInferenceManager
        TrainInferenceManager.invalidate_caches()
        self.engine._evaluate_system_logic(time.time(), force=True)
        state = self.engine.get_render_state()
        self._cached_render_state = state
        self._cached_render_state_ts = time.time()
        self._dispatch_broadcast(state)

    def _open_train_assignment(self, trn: dict):
        self.train_assign_active = True
        self.train_assign_trn_key = trn.get("train_key") or trn.get("train_name")
        self.train_assign_buffers["assigned_id"] = str(trn.get("assigned_id") or "")
        self.train_assign_buffers["assigned_name"] = str(trn.get("assigned_name") or "")
        self.train_assign_active_field = "assigned_id"
        self.status_toast = f"Editing info for {self.train_assign_trn_key}: Tab to switch field, Enter to Save"

    def delete_train(self, train_key: str):
        if not train_key:
            return
        self.engine.delete_train(train_key)
        self.crank_engine()
        self.status_toast = f"Deleted train '{train_key}' from list"

    def _submit_train_assignment(self):
        if not self.train_assign_trn_key:
            self.train_assign_active = False
            return
        aid = self.train_assign_buffers.get("assigned_id", "").strip()
        aname = self.train_assign_buffers.get("assigned_name", "").strip()
        self.engine.assign_train_info(
            self.train_assign_trn_key,
            assigned_id=aid if aid else None,
            assigned_name=aname if aname else None,
        )
        self.crank_engine()
        self.train_assign_active = False
        self.status_toast = f"Saved assignment for train '{self.train_assign_trn_key}'"

    def _handle_train_assign_keydown(self, event: pygame.event.Event):
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._submit_train_assignment()
        elif event.key == pygame.K_ESCAPE:
            self.train_assign_active = False
            self.status_toast = "Train assignment cancelled"
        elif event.key == pygame.K_TAB:
            self.train_assign_active_field = "assigned_name" if self.train_assign_active_field == "assigned_id" else "assigned_id"
        elif event.key == pygame.K_BACKSPACE:
            fld = self.train_assign_active_field
            if event.mod & pygame.KMOD_CTRL:
                self.train_assign_buffers[fld] = ""
            else:
                self.train_assign_buffers[fld] = self.train_assign_buffers.get(fld, "")[:-1]
        else:
            if event.unicode and event.unicode.isprintable():
                fld = self.train_assign_active_field
                self.train_assign_buffers[fld] = self.train_assign_buffers.get(fld, "") + event.unicode

    def _handle_train_assign_mouse_down(self, event: pygame.event.Event):
        mx, my = event.pos
        if hasattr(self, "_train_assign_del_rect") and self._train_assign_del_rect.collidepoint(mx, my):
            self.delete_train(self.train_assign_trn_key)
            self.train_assign_active = False
            return
        if hasattr(self, "_train_assign_save_rect") and self._train_assign_save_rect.collidepoint(mx, my):
            self._submit_train_assignment()
            return
        if hasattr(self, "_train_assign_cancel_rect") and self._train_assign_cancel_rect.collidepoint(mx, my):
            self.train_assign_active = False
            self.status_toast = "Train assignment cancelled"
            return
        if hasattr(self, "_train_assign_id_rect") and self._train_assign_id_rect.collidepoint(mx, my):
            self.train_assign_active_field = "assigned_id"
            return
        if hasattr(self, "_train_assign_name_rect") and self._train_assign_name_rect.collidepoint(mx, my):
            self.train_assign_active_field = "assigned_name"
            return
        if hasattr(self, "_train_assign_modal_rect") and not self._train_assign_modal_rect.collidepoint(mx, my):
            self.train_assign_active = False

    def draw_train_assign_dialog(self):
        overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 150))
        self.screen.blit(overlay, (0, 0))

        box_w, box_h = 460, 246
        box_x = (self.width - box_w) // 2
        box_y = (self.height - box_h) // 2
        self._train_assign_modal_rect = pygame.Rect(box_x, box_y, box_w, box_h)

        pygame.draw.rect(self.screen, (22, 28, 38), self._train_assign_modal_rect, border_radius=8)
        pygame.draw.rect(self.screen, (70, 160, 255), self._train_assign_modal_rect, 2, border_radius=8)

        title_txt = f"ASSIGN TRAIN INFO - {self.train_assign_trn_key}"
        self.screen.blit(self.font_md.render(title_txt, True, (255, 255, 255)), (box_x + 18, box_y + 14))

        lbl_id = "Train / WIU Node ID (e.g. 7842):"
        self.screen.blit(self.font_sm_bold.render(lbl_id, True, (170, 195, 225)), (box_x + 18, box_y + 46))
        self._train_assign_id_rect = pygame.Rect(box_x + 18, box_y + 66, box_w - 36, 26)
        id_border = (80, 220, 255) if self.train_assign_active_field == "assigned_id" else (65, 80, 105)
        pygame.draw.rect(self.screen, (12, 16, 24), self._train_assign_id_rect, border_radius=4)
        pygame.draw.rect(self.screen, id_border, self._train_assign_id_rect, 1 if self.train_assign_active_field != "assigned_id" else 2, border_radius=4)
        id_text = self.train_assign_buffers.get("assigned_id", "")
        cursor_id = "|" if (self.train_assign_active_field == "assigned_id" and (int(time.time() * 2) % 2 == 0)) else ""
        self.screen.blit(self.font_sm.render(id_text + cursor_id, True, (240, 245, 255)), (box_x + 24, box_y + 70))

        lbl_name = "Train Name / Symbol (e.g. BNSF 7842):"
        self.screen.blit(self.font_sm_bold.render(lbl_name, True, (170, 195, 225)), (box_x + 18, box_y + 100))
        self._train_assign_name_rect = pygame.Rect(box_x + 18, box_y + 120, box_w - 36, 26)
        name_border = (80, 220, 255) if self.train_assign_active_field == "assigned_name" else (65, 80, 105)
        pygame.draw.rect(self.screen, (12, 16, 24), self._train_assign_name_rect, border_radius=4)
        pygame.draw.rect(self.screen, name_border, self._train_assign_name_rect, 1 if self.train_assign_active_field != "assigned_name" else 2, border_radius=4)
        name_text = self.train_assign_buffers.get("assigned_name", "")
        cursor_name = "|" if (self.train_assign_active_field == "assigned_name" and (int(time.time() * 2) % 2 == 0)) else ""
        self.screen.blit(self.font_sm.render(name_text + cursor_name, True, (240, 245, 255)), (box_x + 24, box_y + 124))

        hint_txt = "Tab: Switch Field   |   Enter: Save   |   Esc: Cancel"
        font_thin = getattr(self, "font_sm_thin", self.font_sm)
        self.screen.blit(font_thin.render(hint_txt, True, (135, 155, 180)), (box_x + 18, box_y + 158))

        btn_y = box_y + 192
        btn_h = 28

        # Delete Train Button (left)
        self._train_assign_del_rect = pygame.Rect(box_x + 18, btn_y, 115, btn_h)
        pygame.draw.rect(self.screen, (90, 25, 25), self._train_assign_del_rect, border_radius=4)
        pygame.draw.rect(self.screen, (220, 60, 60), self._train_assign_del_rect, 1, border_radius=4)
        del_lbl = self.font_sm_bold.render("Delete Train", True, (255, 200, 200))
        self.screen.blit(del_lbl, (self._train_assign_del_rect.x + (115 - del_lbl.get_width()) // 2, btn_y + 6))

        # Save Button
        self._train_assign_save_rect = pygame.Rect(box_x + box_w - 195, btn_y, 90, btn_h)
        pygame.draw.rect(self.screen, (35, 115, 65), self._train_assign_save_rect, border_radius=4)
        pygame.draw.rect(self.screen, (75, 220, 130), self._train_assign_save_rect, 1, border_radius=4)
        save_lbl = self.font_sm_bold.render("Save (↵)", True, (255, 255, 255))
        self.screen.blit(save_lbl, (self._train_assign_save_rect.x + (90 - save_lbl.get_width()) // 2, btn_y + 6))

        # Cancel Button
        self._train_assign_cancel_rect = pygame.Rect(box_x + box_w - 95, btn_y, 75, btn_h)
        pygame.draw.rect(self.screen, (45, 52, 68), self._train_assign_cancel_rect, border_radius=4)
        pygame.draw.rect(self.screen, (95, 110, 135), self._train_assign_cancel_rect, 1, border_radius=4)
        cnl_lbl = self.font_sm_bold.render("Cancel", True, (200, 210, 225))
        self.screen.blit(cnl_lbl, (self._train_assign_cancel_rect.x + (75 - cnl_lbl.get_width()) // 2, btn_y + 6))

    def run_verification(self):
        self.verification_issues = LayoutTopologyAssistant.verify_layout(self.engine.elements)
        self.show_verification_panel = True
        err_cnt = sum(1 for s, _ in self.verification_issues if s == "ERROR")
        if err_cnt == 0: self.status_toast = "Verification PASSED: All tracks/turnouts signaled, no conflicts, 100% reachable!"
        else: self.status_toast = f"Verification found {err_cnt} error(s) — see report panel."

    def pick_track_endpoint_handle_at_screen(self, sx: int, sy: int) -> Optional[Tuple[str, int, Tuple[float, float]]]:
        hit_radius = max(9.0, 11.0 * self.zoom)
        mlx, mly = self.screen_to_logical(sx, sy)
        max_log_r = hit_radius / max(1e-3, LOGICAL_GRID_SCALE * self.zoom) + 0.5
        for eid, el in self.engine.elements.items():
            if not isinstance(el, TrackSegment): continue
            half_span = max(0.5, float(getattr(el, "length_miles", 0.5) or 0.5) * 2.0) + max_log_r
            if abs(el.x - mlx) > half_span or abs(el.y - mly) > half_span:
                continue
            p0, p1 = LayoutTopologyAssistant.get_track_endpoints_logical(el)
            s0 = self.logical_to_screen(p0[0], p0[1])
            s1 = self.logical_to_screen(p1[0], p1[1])
            if math.hypot(sx - s0[0], sy - s0[1]) <= hit_radius: return (eid, 0, p1)
            if math.hypot(sx - s1[0], sy - s1[1]) <= hit_radius: return (eid, 1, p0)
        return None

    @staticmethod
    def _dist_to_segment_px(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        if l2 < 1e-6:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy
        return math.hypot(px - proj_x, py - proj_y)

    def pick_element_at_screen(self, sx: int, sy: int) -> Optional[str]:
        best_id = None
        best_dist = max(14.0, 18.0 * self.zoom)
        mlx, mly = self.screen_to_logical(sx, sy)
        max_log_r = best_dist / max(1e-3, LOGICAL_GRID_SCALE * self.zoom) + 0.5

        for eid, el in self.engine.elements.items():
            et = el.elem_type
            if et in SYSTEM_LIST_TYPES:
                px, py = self.logical_to_screen(el.x, el.y)
                pw = getattr(el, "width", 0) or (580 if et == "packet_scroller" else (450 if et == "address_table" else (460 if et == "chat" else 430)))
                ph = getattr(el, "height", 0) or (195 if et == "packet_scroller" else (220 if et == "chat" else 150))
                rect = pygame.Rect(px, py, int(pw * self.zoom), int(ph * self.zoom))
                if rect.collidepoint(sx, sy): return eid
                continue
            if et in ("ptc_datetime", "date_time", "datetime"):
                px, py = self.logical_to_screen(el.x, el.y)
                pw = getattr(el, "width", 0) or 240
                ph = getattr(el, "height", 0) or 50
                rect = pygame.Rect(px, py, int(pw * self.zoom), int(ph * self.zoom))
                if rect.collidepoint(sx, sy): return eid
                continue
            if et == "track":
                half_span = max(0.5, float(getattr(el, "length_miles", 0.5) or 0.5) * 2.0) + max_log_r
                if abs(el.x - mlx) > half_span or abs(el.y - mly) > half_span:
                    continue
                p0, p1 = LayoutTopologyAssistant.get_track_endpoints_logical(el)
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                dist = self._dist_to_segment_px(sx, sy, s0[0], s0[1], s1[0], s1[1])
            else:
                if abs(el.x - mlx) > max_log_r or abs(el.y - mly) > max_log_r:
                    continue
                ex, ey = self.logical_to_screen(el.x, el.y)
                dist = math.hypot(sx - ex, sy - ey)
            if dist <= best_dist:
                best_dist, best_id = dist, eid
        return best_id

    def request_exit(self) -> bool:
        if not self.dirty: return False
        self.confirm_exit_modal = True
        return True

    MAP_VIEW_RADIUS_MILES: float = 0.1
    MAP_RADIUS_PRESETS: List[float] = [0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 50.0, 75.0, 100.0]

    def set_map_view_radius(self, radius_miles: float):
        radius_miles = max(0.01, min(1000.0, float(radius_miles)))
        self.map_view_radius_miles = radius_miles
        if self.gps_picker_modal:
            self.gps_picker_modal.view_radius_miles = radius_miles
        scale = self.get_map_scale_logical_per_mile()
        self.status_toast = f"1x View radius set to {radius_miles:g} mi ({scale:.2f} logical units/mi)."

    def step_map_view_radius(self, step: int = 1):
        cur = getattr(self, "map_view_radius_miles", self.MAP_VIEW_RADIUS_MILES)
        idx = min(range(len(self.MAP_RADIUS_PRESETS)), key=lambda i: abs(self.MAP_RADIUS_PRESETS[i] - cur))
        new_idx = max(0, min(len(self.MAP_RADIUS_PRESETS) - 1, idx + step))
        self.set_map_view_radius(self.MAP_RADIUS_PRESETS[new_idx])

    def cycle_map_view_radius(self, step: int = 1):
        cur = getattr(self, "map_view_radius_miles", self.MAP_VIEW_RADIUS_MILES)
        idx = min(range(len(self.MAP_RADIUS_PRESETS)), key=lambda i: abs(self.MAP_RADIUS_PRESETS[i] - cur))
        new_idx = (idx + step) % len(self.MAP_RADIUS_PRESETS)
        self.set_map_view_radius(self.MAP_RADIUS_PRESETS[new_idx])

    def get_map_scale_logical_per_mile(self) -> float:
        """Returns the layout scale (logical grid units per mile) such that at layout
        zoom level 1.0, the user can see a map_view_radius_miles radius on the screen."""
        view_radius_px = min(self.width, self.height) / 2.0
        radius_miles = max(0.1, getattr(self, "map_view_radius_miles", self.MAP_VIEW_RADIUS_MILES))
        pixels_per_mile = view_radius_px / radius_miles
        return pixels_per_mile / LOGICAL_GRID_SCALE

    def estimate_position_from_gps(self, target_lat: float, target_lon: float) -> Tuple[int, int, Tuple[int, int]]:
        """Estimates logical grid coordinates (lx, ly) and screen coordinates (sx, sy) for a target GPS coordinate
        based on the user-configurable view radius scale at layout zoom level 1.0. If the estimated position goes beyond zoom
        capability, places the new element in the vicinity of the last placed element unconnected."""
        ref_el = None
        if getattr(self, "last_placed_element_id", None) and self.last_placed_element_id in self.engine.elements:
            cand = self.engine.elements[self.last_placed_element_id]
            if getattr(cand, "gps", None) and len(cand.gps) == 2:
                ref_el = cand
        if ref_el is None and self.engine.elements:
            gps_elems = [el for el in self.engine.elements.values() if getattr(el, "gps", None) and len(el.gps) == 2]
            if gps_elems:
                ref_el = gps_elems[-1]

        if ref_el is None:
            center_sx = self.width // 2
            center_sy = self.height // 2
            clx, cly = self.screen_to_logical(center_sx, center_sy)
            est_lx = int(round(clx))
            est_ly = int(round(cly))
            self.last_estimate_beyond_zoom = False
            self.last_estimate_dist_miles = 0.0
            return est_lx, est_ly, (center_sx, center_sy)

        ref_lx = float(ref_el.x)
        ref_ly = float(ref_el.y)
        if getattr(ref_el, "gps", None) and len(ref_el.gps) == 2:
            ref_lat, ref_lon = float(ref_el.gps[0]), float(ref_el.gps[1])
        else:
            ref_lat, ref_lon = self.last_selected_gps

        d_lat = target_lat - ref_lat
        d_lon = target_lon - ref_lon
        lat_mid_rad = math.radians((target_lat + ref_lat) / 2.0)
        d_lat_miles = d_lat * 69.0
        d_lon_miles = d_lon * 69.0 * math.cos(lat_mid_rad)
        dist_miles = math.hypot(d_lon_miles, d_lat_miles)

        scale_lpm = self.get_map_scale_logical_per_mile()
        raw_lx = ref_lx + d_lon_miles * scale_lpm
        raw_ly = ref_ly - d_lat_miles * scale_lpm

        # Maximum viewable radius in miles at min_zoom
        min_z = max(1e-4, getattr(self, "min_zoom", 0.0175))
        radius_miles = max(0.1, getattr(self, "map_view_radius_miles", self.MAP_VIEW_RADIUS_MILES))
        max_visible_radius_miles = radius_miles / min_z
        view_radius_px = min(self.width, self.height) / 2.0
        max_logical_span = view_radius_px / (LOGICAL_GRID_SCALE * min_z)

        if dist_miles > max_visible_radius_miles or math.hypot(raw_lx - ref_lx, raw_ly - ref_ly) > max_logical_span:
            # Beyond zoom capability: place in vicinity of last placed element, unconnected
            self.last_estimate_beyond_zoom = True
            if dist_miles > 1e-6:
                ux = d_lon_miles / dist_miles
                uy = -d_lat_miles / dist_miles
            else:
                ux, uy = 1.0, 0.0
            vicinity_dist = 6.0
            est_lx = int(round(ref_lx + ux * vicinity_dist))
            est_ly = int(round(ref_ly + uy * vicinity_dist))
        else:
            self.last_estimate_beyond_zoom = False
            est_lx = int(round(raw_lx))
            est_ly = int(round(raw_ly))

        self.last_estimate_dist_miles = dist_miles
        sx, sy = self.logical_to_screen(est_lx, est_ly)
        return est_lx, est_ly, (sx, sy)

        first_lon, first_lat = points[0][0], points[0][1]
        if all(abs(p[0] - first_lon) < 1e-7 and abs(p[1] - first_lat) < 1e-7 for p in points):
            lon0, lat0, x0, y0 = points[0]
            d_lat = target_lat - lat0
            d_lon = target_lon - lon0
            lat_mid_rad = math.radians((target_lat + lat0) / 2.0)
            d_lat_miles = d_lat * 69.0
            d_lon_miles = d_lon * 69.0 * math.cos(lat_mid_rad)
            scale = getattr(LayoutTopologyAssistant, "LOGICAL_UNITS_PER_MILE", 4.0)
            est_lx = int(round(x0 + d_lon_miles * scale))
            est_ly = int(round(y0 - d_lat_miles * scale))
            sx, sy = self.logical_to_screen(est_lx, est_ly)
            return est_lx, est_ly, (sx, sy)

        mean_lon = sum(p[0] for p in points) / len(points)
        mean_lat = sum(p[1] for p in points) / len(points)
        mean_x = sum(p[2] for p in points) / len(points)
        mean_y = sum(p[3] for p in points) / len(points)

        u = [p[0] - mean_lon for p in points]
        v = [p[1] - mean_lat for p in points]
        dx = [p[2] - mean_x for p in points]
        dy = [p[3] - mean_y for p in points]

        s_uu = sum(ui * ui for ui in u)
        s_vv = sum(vi * vi for vi in v)
        s_uv = sum(ui * vi for ui, vi in zip(u, v))
        det = s_uu * s_vv - s_uv * s_uv

        if abs(det) > 1e-12:
            s_ux = sum(ui * dxi for ui, dxi in zip(u, dx))
            s_vx = sum(vi * dxi for vi, dxi in zip(v, dx))
            s_uy = sum(ui * dyi for ui, dyi in zip(u, dy))
            s_vy = sum(vi * dyi for vi, dyi in zip(v, dy))

            a = (s_vv * s_ux - s_uv * s_vx) / det
            b = (s_uu * s_vx - s_uv * s_ux) / det
            c = (s_vv * s_uy - s_uv * s_vy) / det
            d = (s_uu * s_vy - s_uv * s_uy) / det

            tu = target_lon - mean_lon
            tv = target_lat - mean_lat
            est_lx = int(round(mean_x + a * tu + b * tv))
            est_ly = int(round(mean_y + c * tu + d * tv))
        else:
            var_u = s_uu
            var_v = s_vv
            if var_u >= var_v and var_u > 1e-12:
                slope_x = sum(ui * dxi for ui, dxi in zip(u, dx)) / var_u
                slope_y = sum(ui * dyi for ui, dyi in zip(u, dy)) / var_u
                tu = target_lon - mean_lon
                est_lx = int(round(mean_x + slope_x * tu))
                est_ly = int(round(mean_y + slope_y * tu))
            elif var_v > 1e-12:
                slope_x = sum(vi * dxi for vi, dxi in zip(v, dx)) / var_v
                slope_y = sum(vi * dyi for vi, dyi in zip(v, dy)) / var_v
                tv = target_lat - mean_lat
                est_lx = int(round(mean_x + slope_x * tv))
                est_ly = int(round(mean_y + slope_y * tv))
            else:
                nearest = min(points, key=lambda p: math.hypot(p[0] - target_lon, p[1] - target_lat))
                d_lat_miles = (target_lat - nearest[1]) * 69.0
                d_lon_miles = (target_lon - nearest[0]) * 69.0 * math.cos(math.radians(nearest[1]))
                scale = getattr(LayoutTopologyAssistant, "LOGICAL_UNITS_PER_MILE", 4.0)
                est_lx = int(round(nearest[2] + d_lon_miles * scale))
                est_ly = int(round(nearest[3] - d_lat_miles * scale))

        sx, sy = self.logical_to_screen(est_lx, est_ly)
        return est_lx, est_ly, (sx, sy)

    def _estimate_layout_pos_for_map(self, lat: float, lon: float) -> Tuple[int, int]:
        lx, ly, _ = self.estimate_position_from_gps(lat, lon)
        return lx, ly

    def _close_gps_modal(self):
        if self.gps_picker_modal:
            self.last_map_picked_gps = (self.gps_picker_modal.picked_lat, self.gps_picker_modal.picked_lon)
            self.last_map_zoom = self.gps_picker_modal.zoom
            self.gps_picker_modal = None

    def open_big_map(self):
        """Opens the full-screen Big Map for picking geographic locations and defining layout elements."""
        if getattr(self, "last_map_picked_gps", None) is not None:
            start_lat, start_lon = self.last_map_picked_gps
            start_zoom = getattr(self, "last_map_zoom", 15)
        else:
            gps_pts = [el.gps for el in self.engine.elements.values() if getattr(el, "gps", None) and len(el.gps) == 2]
            if gps_pts:
                avg_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                avg_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                start_lat, start_lon = avg_lat, avg_lon
            else:
                start_lat, start_lon = self.last_selected_gps
            start_zoom = 15

        self.gps_picker_modal = InteractiveGPSMapModal(
            tile_mgr=self.tile_map_mgr,
            start_lat=start_lat,
            start_lon=start_lon,
            all_elements=self.engine.elements,
            on_create_element_cb=self._create_element_from_big_map,
            estimate_layout_pos_cb=self._estimate_layout_pos_for_map,
            mode="create",
            zoom=start_zoom,
            view_radius_miles=getattr(self, "map_view_radius_miles", 0.5),
            on_radius_change_cb=self.set_map_view_radius,
        )
        self.status_toast = "Big Map: Pick a location on the map, then choose an element type to define."

    def _create_element_from_big_map(self, elem_type: str, lat: float, lon: float):
        """Creates a new layout element from Big Map coordinates, estimates its grid position, and opens its editor with GPS filled."""
        if self.gps_picker_modal:
            self.last_map_zoom = self.gps_picker_modal.zoom
        self.last_map_picked_gps = (lat, lon)
        self.last_selected_gps = (lat, lon)
        parsed_g = _parse_wiu_gps((lat, lon))
        if parsed_g is not None:
            self.specified_gps = parsed_g
            self.gps_specified = True
        self.gps_picker_modal = None
        est_lx, est_ly, (sx, sy) = self.estimate_position_from_gps(lat, lon)
        is_beyond = getattr(self, "last_estimate_beyond_zoom", False)
        dist_mi = getattr(self, "last_estimate_dist_miles", 0.0)

        target_sx = self.width // 2
        target_sy = self.height // 2
        wx = est_lx * LOGICAL_GRID_SCALE
        wy = est_ly * LOGICAL_GRID_SCALE
        self.cam_x = target_sx - wx * self.zoom
        self.cam_y = target_sy - wy * self.zoom
        screen_pos = (target_sx, target_sy)

        self.create_element_at(elem_type, est_lx, est_ly, screen_pos, initial_gps=(lat, lon), auto_connect=(not is_beyond))
        new_id = next(iter(self.selected_ids)) if self.selected_ids else elem_type

        if is_beyond:
            self.status_toast = f"Target GPS is {dist_mi:.0f}mi away (beyond zoom limits). Placed {elem_type} '{new_id}' in vicinity at ({est_lx}, {est_ly}) unconnected."
        else:
            self.status_toast = f"Defined {elem_type} '{new_id}' from Big Map at GPS ({lat:.6f}, {lon:.6f}) -> Grid ({est_lx}, {est_ly}) [{dist_mi:.1f}mi]."

    def _open_gps_picker_for_editor(self):
        if not self.inplace_editor:
            return
        lat, lon = self.inplace_editor.get_initial_gps()
        self.gps_picker_modal = InteractiveGPSMapModal(
            tile_mgr=self.tile_map_mgr,
            start_lat=lat,
            start_lon=lon,
            all_elements=self.engine.elements,
            on_pick_cb=self.inplace_editor.apply_picked_gps,
            mode="pick",
            zoom=13,
        )

    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.KEYDOWN):
                self._cached_render_state = None
            if event.type == pygame.QUIT:
                if not self.request_exit(): return False
            elif event.type == pygame.VIDEORESIZE:
                self.width, self.height = event.w, event.h
                self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
            elif event.type == pygame.MOUSEWHEEL:
                mx, my = pygame.mouse.get_pos()
                if self.confirm_exit_modal or self.confirm_new_modal or self.confirm_load_modal: continue
                if self.banc_coverage_modal:
                    if hasattr(self.banc_coverage_modal, "handle_scroll"):
                        self.banc_coverage_modal.handle_scroll(event.y, mx, my)
                    elif hasattr(self.banc_coverage_modal, "handle_mouse_wheel"):
                        self.banc_coverage_modal.handle_mouse_wheel(event)
                    continue
                if self.frag_picker_modal:
                    self.frag_picker_modal.handle_scroll(event.y)
                    continue
                if self.gps_picker_modal:
                    self.gps_picker_modal.zoom_at_point(mx, my, 1 if event.y > 0 else -1)
                    continue

                mods = pygame.key.get_mods()
                if (mods & pygame.KMOD_SHIFT) or (mods & pygame.KMOD_LSHIFT) or (mods & pygame.KMOD_RSHIFT):
                    direction = 1 if event.y > 0 else -1
                    if not self.rotate_selected_track(direction, mx, my):
                        self.status_toast = "Select a track or multiple elements to rotate in 45° increments (Shift + Scroll)."
                    continue

                if self.inplace_editor:
                    for idx, r in enumerate(self.inplace_editor.field_rects):
                        if r.collidepoint(mx, my):
                            self.inplace_editor.cycle_field_by_wheel(idx, 1 if event.y > 0 else -1)
                            break
                else:
                    factor = 1.14 if event.y > 0 else (1.0 / 1.14)
                    self.zoom_at_cursor((mx, my), factor)
            elif event.type == pygame.KEYDOWN:
                if self.confirm_exit_modal:
                    if event.key == pygame.K_s:
                        self.save_current_layout()
                        return False
                    elif event.key in (pygame.K_d, pygame.K_y, pygame.K_RETURN): return False
                    elif event.key in (pygame.K_ESCAPE, pygame.K_n): self.confirm_exit_modal = False
                elif self.confirm_new_modal:
                    if event.key == pygame.K_s:
                        self._handle_save_and_new()
                    elif event.key in (pygame.K_d, pygame.K_y):
                        self.confirm_new_modal = False
                        self.new_layout(force=True)
                    elif event.key in (pygame.K_ESCAPE, pygame.K_n):
                        self.confirm_new_modal = False
                elif self.confirm_load_modal:
                    if event.key == pygame.K_s:
                        self._handle_save_and_load()
                    elif event.key in (pygame.K_d, pygame.K_y):
                        self._handle_discard_and_load()
                    elif event.key in (pygame.K_ESCAPE, pygame.K_n):
                        self.confirm_load_modal = False
                        self.pending_load_target = None
                elif self.banc_coverage_modal:
                    self.banc_coverage_modal.handle_key_down(event)
                elif self.frag_picker_modal:
                    frag_to_load = self.frag_picker_modal.handle_key_down(event)
                    if frag_to_load:
                        self.load_and_merge_fragment(frag_to_load)
                elif self.gps_picker_modal:
                    if event.key == pygame.K_ESCAPE:
                        if self.gps_picker_modal.show_element_selector:
                            self.gps_picker_modal.show_element_selector = False
                        else:
                            self._close_gps_modal()
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        if self.gps_picker_modal.mode == "create":
                            if not self.gps_picker_modal.show_element_selector:
                                self.gps_picker_modal.show_element_selector = True
                        else:
                            if self.gps_picker_modal.on_pick_cb:
                                self.gps_picker_modal.on_pick_cb(self.gps_picker_modal.picked_lat, self.gps_picker_modal.picked_lon)
                            self.gps_picker_modal = None
                    elif not self.gps_picker_modal.show_element_selector:
                        if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS, pygame.K_LEFTBRACKET):
                            self.gps_picker_modal.step_view_radius(-1)
                        elif event.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS, pygame.K_RIGHTBRACKET):
                            self.gps_picker_modal.step_view_radius(+1)
                        elif event.key == pygame.K_r:
                            self.gps_picker_modal.cycle_view_radius(+1)
                    elif self.gps_picker_modal.show_element_selector:
                        sel_keys = {
                            pygame.K_1: "track",
                            pygame.K_2: "turnout",
                            pygame.K_3: "signal",
                            pygame.K_4: "crossing",
                            pygame.K_5: "base",
                            pygame.K_6: "station",
                            pygame.K_7: "bridge",
                            pygame.K_8: "diamond",
                            pygame.K_9: "label",
                            pygame.K_0: "track_jump",
                            pygame.K_d: "ptc_datetime",
                            pygame.K_c: "chat",
                        }
                        if event.key in sel_keys:
                            chosen = sel_keys[event.key]
                            if self.gps_picker_modal.on_create_element_cb:
                                self.gps_picker_modal.on_create_element_cb(
                                    chosen,
                                    self.gps_picker_modal.picked_lat,
                                    self.gps_picker_modal.picked_lon
                                )
                elif self.train_assign_active:
                    self._handle_train_assign_keydown(event)
                elif self.file_prompt_mode:
                    self._handle_file_prompt_keydown(event)
                elif self.inplace_editor:
                    self._handle_editor_keydown(event)
                else:
                    if not self._handle_canvas_keydown(event): return False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if self.train_assign_active and event.button == 1:
                    self._handle_train_assign_mouse_down(event)
                    continue
                if self.confirm_exit_modal and event.button == 1:
                    mx, my = event.pos
                    for bkey, r in self.exit_modal_buttons.items():
                        if r.collidepoint(mx, my):
                            if bkey == "save_exit":
                                self.save_current_layout()
                                return False
                            elif bkey == "discard_exit": return False
                            elif bkey == "cancel_exit": self.confirm_exit_modal = False
                    continue
                if self.confirm_new_modal and event.button == 1:
                    mx, my = event.pos
                    for bkey, r in self.new_modal_buttons.items():
                        if r.collidepoint(mx, my):
                            if bkey == "save_new":
                                self._handle_save_and_new()
                            elif bkey == "discard_new":
                                self.confirm_new_modal = False
                                self.new_layout(force=True)
                            elif bkey == "cancel_new":
                                self.confirm_new_modal = False
                    continue
                if self.confirm_load_modal and event.button == 1:
                    mx, my = event.pos
                    for bkey, r in self.load_modal_buttons.items():
                        if r.collidepoint(mx, my):
                            if bkey == "save_load":
                                self._handle_save_and_load()
                            elif bkey == "discard_load":
                                self._handle_discard_and_load()
                            elif bkey == "cancel_load":
                                self.confirm_load_modal = False
                                self.pending_load_target = None
                    continue
                self._handle_mouse_down(event)
            elif event.type == pygame.KEYUP:
                if event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN, pygame.K_LSHIFT, pygame.K_RSHIFT):
                    self._shift_arrow_repeat_ts = 0.0
                    if self._shift_arrow_moved_during_hold:
                        self._shift_arrow_moved_during_hold = False
                        for sid in self.selected_ids:
                            el = self.engine.elements.get(sid)
                            if el and el.elem_type not in SYSTEM_LIST_TYPES:
                                LayoutTopologyAssistant.auto_suggest_connections(el, self.engine.elements)
                        self.crank_engine()
            elif event.type == pygame.MOUSEBUTTONUP:
                self._handle_mouse_up(event)
            elif event.type == pygame.MOUSEMOTION:
                self._handle_mouse_motion(event)

        if (not self.inplace_editor and not self.file_prompt_mode and not self.confirm_exit_modal
                and not self.confirm_new_modal and not self.confirm_load_modal
                and not self.frag_picker_modal and not self.banc_coverage_modal
                and not self.gps_picker_modal and not self.train_assign_active):
            keys = pygame.key.get_pressed()
            shift_held = bool(keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT])
            if shift_held:
                if self.selected_ids and self._shift_arrow_repeat_ts > 0.0 and time.time() >= self._shift_arrow_repeat_ts:
                    dx = (-1 if keys[pygame.K_LEFT] else 0) + (1 if keys[pygame.K_RIGHT] else 0)
                    dy = (-1 if keys[pygame.K_UP] else 0) + (1 if keys[pygame.K_DOWN] else 0)
                    if dx != 0 or dy != 0:
                        self.move_selected_elements(dx, dy, push_undo=False, auto_connect=False)
                        self._shift_arrow_repeat_ts = time.time() + 0.07
                        self._shift_arrow_moved_during_hold = True
            else:
                pan_speed = 10.0
                if keys[pygame.K_LEFT]: self.cam_x += pan_speed
                if keys[pygame.K_RIGHT]: self.cam_x -= pan_speed
                if keys[pygame.K_UP]: self.cam_y += pan_speed
                if keys[pygame.K_DOWN]: self.cam_y -= pan_speed
        return True

    def open_file_prompt(self, mode: str, default_buffer: str = ""):
        """Opens a file prompt with autocompletion suggestions initialized."""
        self.file_prompt_mode = mode
        self.file_prompt_buffer = default_buffer
        self.file_prompt_selected_idx = -1
        self._update_file_prompt_suggestions()

    def _update_file_prompt_suggestions(self):
        """Scans for matching files and directories to provide a suggested completion list."""
        if not self.file_prompt_mode or self.file_prompt_mode == "set_map_radius":
            self.file_prompt_suggestions = []
            self.file_prompt_selected_idx = -1
            self.file_prompt_suggestion_rects = []
            return

        mode = self.file_prompt_mode
        raw_buf = getattr(self, "file_prompt_buffer", "")
        clean_buf = raw_buf.strip()

        if mode == "load_log":
            valid_exts = {".log", ".jsonl", ".txt", ".json"}
        elif mode == "import_itcvl":
            valid_exts = {".itcvl"}
        elif mode == "import_atcs":
            valid_exts = {".lay"}
        elif mode == "save_frag":
            valid_exts = {".json"}
        else:
            valid_exts = {".json"}

        frags_dir = getattr(self, "FRAGS_DIR", getattr(sys.modules.get("ApexBOSCreator", None), "FRAGS_DIR", FRAGS_DIR))
        if mode == "save_frag" and not ("/" in clean_buf or "\\" in clean_buf):
            search_dir = frags_dir if os.path.exists(frags_dir) else "."
            prefix = clean_buf
            dir_prefix = ""
        else:
            if "/" in clean_buf or "\\" in clean_buf:
                last_sep = max(clean_buf.rfind("/"), clean_buf.rfind("\\"))
                dir_part = clean_buf[:last_sep + 1]
                prefix = clean_buf[last_sep + 1:]
                search_dir = dir_part if os.path.isdir(dir_part) else (os.path.dirname(dir_part) or ".")
                dir_prefix = dir_part
            else:
                search_dir = "."
                prefix = clean_buf
                dir_prefix = ""

        matches = []
        try:
            if os.path.isdir(search_dir):
                for entry in sorted(os.listdir(search_dir), key=lambda s: s.lower()):
                    if entry.startswith(".") or entry.startswith("__"):
                        continue
                    full_path = os.path.join(search_dir, entry)
                    is_dir = os.path.isdir(full_path)
                    ext = os.path.splitext(entry)[1].lower()

                    if is_dir:
                        cand = f"{dir_prefix}{entry}/"
                    elif not valid_exts or ext in valid_exts:
                        cand = f"{dir_prefix}{entry}"
                    else:
                        continue

                    p_lower = prefix.lower()
                    entry_lower = entry.lower()

                    if not p_lower:
                        matches.append((1 if is_dir else 2, entry_lower, cand))
                    elif entry_lower.startswith(p_lower):
                        matches.append((0 if is_dir else 1, entry_lower, cand))
                    elif p_lower in entry_lower:
                        matches.append((2 if is_dir else 3, entry_lower, cand))
        except Exception:
            pass

        if mode == "load_log" and not dir_prefix:
            try:
                temp_dir = tempfile.gettempdir()
                default_log = getattr(self.engine, "raw_log_filepath", os.path.join(temp_dir, "apexbos_raw_packets.log"))
                if os.path.exists(default_log):
                    b_name = os.path.basename(default_log)
                    if not prefix or prefix.lower() in b_name.lower():
                        if default_log not in [m[2] for m in matches]:
                            matches.append((1, b_name.lower(), default_log))
            except Exception:
                pass

        matches.sort(key=lambda t: (t[0], t[1]))
        self.file_prompt_suggestions = [cand for _, _, cand in matches[:8]]

        if self.file_prompt_selected_idx >= len(self.file_prompt_suggestions):
            self.file_prompt_selected_idx = -1

    def _handle_file_prompt_mouse_down(self, pos: Tuple[int, int]) -> bool:
        """Handles mouse clicks on the file prompt suggested completion list."""
        mx, my = pos
        for r, cand in getattr(self, "file_prompt_suggestion_rects", []):
            if r.collidepoint(mx, my):
                self.file_prompt_buffer = cand
                if cand.endswith("/") or cand.endswith("\\"):
                    self.file_prompt_selected_idx = -1
                else:
                    try:
                        self.file_prompt_selected_idx = self.file_prompt_suggestions.index(cand)
                    except ValueError:
                        self.file_prompt_selected_idx = -1
                self._update_file_prompt_suggestions()
                return True
        return False

    def _handle_file_prompt_keydown(self, event: pygame.event.Event):
        if event.key == pygame.K_TAB:
            if getattr(self, "file_prompt_suggestions", None):
                if 0 <= self.file_prompt_selected_idx < len(self.file_prompt_suggestions):
                    self.file_prompt_selected_idx = (self.file_prompt_selected_idx + 1) % len(self.file_prompt_suggestions)
                else:
                    self.file_prompt_selected_idx = 0
                self.file_prompt_buffer = self.file_prompt_suggestions[self.file_prompt_selected_idx]
            return
        elif event.key == pygame.K_UP:
            if getattr(self, "file_prompt_suggestions", None):
                if self.file_prompt_selected_idx <= 0:
                    self.file_prompt_selected_idx = len(self.file_prompt_suggestions) - 1
                else:
                    self.file_prompt_selected_idx -= 1
                self.file_prompt_buffer = self.file_prompt_suggestions[self.file_prompt_selected_idx]
            return
        elif event.key == pygame.K_DOWN:
            if getattr(self, "file_prompt_suggestions", None):
                if self.file_prompt_selected_idx >= len(self.file_prompt_suggestions) - 1:
                    self.file_prompt_selected_idx = 0
                else:
                    self.file_prompt_selected_idx += 1
                self.file_prompt_buffer = self.file_prompt_suggestions[self.file_prompt_selected_idx]
            return
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            target_path = self.file_prompt_buffer.strip()
            if target_path and (target_path.endswith("/") or target_path.endswith("\\")):
                self.file_prompt_selected_idx = -1
                self._update_file_prompt_suggestions()
                return
            mode = self.file_prompt_mode
            self.file_prompt_mode = None
            self.file_prompt_suggestions = []
            self.file_prompt_selected_idx = -1
            if target_path:
                if mode == "save_as":
                    self.save_current_layout(target_path)
                    post_act = getattr(self, "pending_post_save_action", None)
                    if post_act == "new_layout":
                        self.pending_post_save_action = None
                        self.new_layout(force=True)
                    elif isinstance(post_act, tuple) and post_act[0] == "load":
                        self.pending_post_save_action = None
                        self._proceed_with_load_layout(post_act[1])
                elif mode == "save_frag": self.save_fragment(target_path)
                elif mode == "load":
                    self.request_load_layout(target_path)
                elif mode == "import_itcvl":
                    self.import_itcvl_layout(target_path)
                elif mode == "import_atcs":
                    self.import_atcs_layout(target_path)
                elif mode == "merge": self.merge_layout_from_file(target_path)
                elif mode == "load_log": self.load_raw_packet_log(target_path)
                elif mode == "set_map_radius":
                    try:
                        val = float(target_path)
                        if val > 0:
                            self.set_map_view_radius(val)
                        else:
                            self.status_toast = "[ERROR] Map radius must be positive."
                    except ValueError:
                        self.status_toast = f"[ERROR] Invalid number for map radius: '{target_path}'"
        elif event.key == pygame.K_ESCAPE:
            self.file_prompt_mode = None
            self.file_prompt_suggestions = []
            self.file_prompt_selected_idx = -1
            self.pending_post_save_action = None
            self.status_toast = "File operation cancelled."
        elif event.key == pygame.K_BACKSPACE:
            if event.mod & pygame.KMOD_CTRL: self.file_prompt_buffer = ""
            else: self.file_prompt_buffer = self.file_prompt_buffer[:-1]
            self.file_prompt_selected_idx = -1
            self._update_file_prompt_suggestions()
        elif event.unicode and event.unicode.isprintable():
            self.file_prompt_buffer += event.unicode
            self.file_prompt_selected_idx = -1
            self._update_file_prompt_suggestions()

    # -----------------------------------------------------------------
    # Clipboard helpers (WIU address copy / paste)
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Raw Packet Log Playback & Testing
    # -----------------------------------------------------------------
    def load_raw_packet_log(self, filepath: Optional[str] = None):
        """Loads a raw packet log file (.log or .jsonl) for layout testing playback."""
        path = filepath
        if not path:
            default_path = getattr(self.engine, "raw_log_filepath", os.path.join(tempfile.gettempdir(), "apexbos_raw_packets.log"))
            if os.path.exists(default_path):
                path = default_path
            else:
                self.file_prompt_mode = "load_log"
                self.file_prompt_buffer = default_path
                return

        if not os.path.exists(path):
            self.status_toast = f"[ERROR] Log file not found: '{path}'"
            return

        loaded_packets = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    hex_str = ""
                    ch = 0
                    rssi = -75.0
                    ts = 0.0
                    if line.startswith("{"):
                        try:
                            d = json.loads(line)
                            hex_str = d.get("hex") or d.get("raw_hex") or ""
                            ch = int(d.get("channel", 0) or 0)
                            rssi = float(d.get("rssi", -75.0) or -75.0)
                            ts = float(d.get("timestamp", 0.0) or 0.0)
                        except Exception:
                            continue
                    else:
                        m_hex = re.search(r'(?:HEX:\s*|RAW:\s*|^)([0-9a-fA-F]{4,})', line)
                        if m_hex:
                            hex_str = m_hex.group(1)
                        m_ch = re.search(r'(?:CH|Ch|Channel)[:\s]*(\d+)', line)
                        if m_ch:
                            ch = int(m_ch.group(1))
                        m_rssi = re.search(r'(?:RSSI)[:\s]*(-?\d+(?:\.\d+)?)', line)
                        if m_rssi:
                            rssi = float(m_rssi.group(1))

                    clean_hex = re.sub(r"[^0-9a-fA-F]", "", hex_str)
                    if clean_hex and len(clean_hex) % 2 == 0:
                        try:
                            b_data = bytes.fromhex(clean_hex)
                            loaded_packets.append({
                                "hex": clean_hex.upper(),
                                "bytes": b_data,
                                "channel": ch,
                                "rssi": rssi,
                                "timestamp": ts,
                            })
                        except Exception:
                            pass

            if loaded_packets:
                self.playback_packets = loaded_packets
                self.playback_idx = 0
                self.playback_active = False
                self.playback_filepath = path
                self.status_toast = f"Loaded {len(loaded_packets)} packet(s) from '{os.path.basename(path)}'. Press Space=Play/Pause, ]=Step."
            else:
                self.status_toast = f"No valid raw hex packets found in '{path}'."
        except Exception as exc:
            self.status_toast = f"Error loading log: {exc}"

    def step_playback(self, forward: bool = True):
        """Step playback through loaded raw packets."""
        if not self.playback_packets:
            return
        if forward:
            if self.playback_idx < len(self.playback_packets):
                pkt = self.playback_packets[self.playback_idx]
                self.playback_idx += 1
                self.engine.ingest_raw_packet(pkt["bytes"], meta={"channel": pkt["channel"], "rssi": pkt["rssi"]})
                self.crank_engine()
                self.status_toast = f"Playback [{self.playback_idx}/{len(self.playback_packets)}] Ch:{pkt['channel']} Hex:{pkt['hex'][:16]}..."
            else:
                self.playback_active = False
                self.status_toast = f"Log playback completed ({len(self.playback_packets)} packets)."
        else:
            if self.playback_idx > 0:
                self.playback_idx = max(0, self.playback_idx - 1)
                self.status_toast = f"Rewound to packet {self.playback_idx}/{len(self.playback_packets)}"

    def draw_playback_bar(self):
        """Draws a floating playback controller bar at bottom of window."""
        if not self.playback_packets:
            return
        bar_w = 640
        bar_h = 32
        bx = (self.width - bar_w) // 2
        by = self.height - bar_h - 28
        p_surf = pygame.Surface((bar_w, bar_h), pygame.SRCALPHA)
        p_surf.fill((16, 22, 32, 235))
        pygame.draw.rect(p_surf, COLOR_SELECT_RING, (0, 0, bar_w, bar_h), 1, border_radius=5)
        self.screen.blit(p_surf, (bx, by))

        self.playback_btn_rects.clear()
        btns = [
            ("reset", "|<<", bx + 8, 38, (45, 60, 80)),
            ("prev", "<", bx + 50, 28, (45, 60, 80)),
            ("play_pause", "Pause" if self.playback_active else "Play", bx + 82, 54, (35, 125, 65) if self.playback_active else (45, 95, 140)),
            ("next", ">", bx + 140, 28, (45, 60, 80)),
            ("close", "X Close", bx + bar_w - 68, 60, (115, 35, 35)),
        ]
        for bkey, btxt, rx, rw, bg in btns:
            r = pygame.Rect(rx, by + 4, rw, bar_h - 8)
            self.playback_btn_rects[bkey] = r
            pygame.draw.rect(self.screen, bg, r, border_radius=3)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, r, 1, border_radius=3)
            ts = self.font_sm_bold.render(btxt, True, (255, 255, 255))
            self.screen.blit(ts, (rx + (rw - ts.get_width()) // 2, by + 8))

        fn = os.path.basename(self.playback_filepath) if self.playback_filepath else "log"
        prog_txt = f"Packet {self.playback_idx}/{len(self.playback_packets)}  |  {fn}"
        col = (110, 245, 175) if self.playback_active else (220, 230, 245)
        self.screen.blit(self.font_sm_bold.render(prog_txt, True, col), (bx + 176, by + 8))

    # -----------------------------------------------------------------
    # Track Splitting & Interactive Mouse Element Selection for Forms
    # -----------------------------------------------------------------
    def _generate_split_id(self, orig_id: str) -> str:
        """Generate a clean unique ID for the second half of a split track."""
        m = re.match(r"^(.*?)(\d+)$", orig_id)
        if m:
            prefix, num_str = m.group(1), m.group(2)
            num = int(num_str)
            candidate = f"{prefix}{num + 1}"
            if candidate not in self.engine.elements:
                return candidate
        for suffix in ("_2", "_B", "_b", "_part2"):
            candidate = f"{orig_id}{suffix}"
            if candidate not in self.engine.elements:
                return candidate
        i = 2
        while f"{orig_id}_{i}" in self.engine.elements:
            i += 1
        return f"{orig_id}_{i}"

    def split_selected_track(self, target_id: Optional[str] = None):
        """Splits a track segment into two separate disconnected segments with a gap
        between them so a turnout can be inserted.
        """
        trk_id = target_id
        if not trk_id and self.selected_ids:
            for sid in self.selected_ids:
                if isinstance(self.engine.elements.get(sid), TrackSegment):
                    trk_id = sid
                    break

        if not trk_id:
            self.status_toast = "Select a track segment to split."
            return

        el = self.engine.elements.get(trk_id)
        if not isinstance(el, TrackSegment):
            self.status_toast = f"Element '{trk_id}' is a {getattr(el, 'elem_type', 'unknown')}, not a track."
            return

        self.push_undo_state()

        new_id = self._generate_split_id(el.id)
        orient = el.orientation or "E-W"
        if orient == "N-S":
            neg_dir, pos_dir = "N", "S"
            ux, uy = 0, 1
        elif orient == "NE-SW":
            neg_dir, pos_dir = "SW", "NE"
            ux, uy = 1, -1
        elif orient == "NW-SE":
            neg_dir, pos_dir = "NW", "SE"
            ux, uy = 1, 1
        else:
            neg_dir, pos_dir = "W", "E"
            ux, uy = 1, 0

        orig_x, orig_y = el.x, el.y
        orig_len = float(el.length_miles or 0.5)
        half_len = max(0.1, round(orig_len / 2.0, 2))

        # Check collision with other elements to pick best offset (leaves gap at orig_x, orig_y)
        offset = 2
        for test_off in (2, 1, 3):
            cand_x1 = orig_x - test_off * ux
            cand_y1 = orig_y - test_off * uy
            cand_x2 = orig_x + test_off * ux
            cand_y2 = orig_y + test_off * uy
            pos1_occ = any(o.id != el.id and o.x == cand_x1 and o.y == cand_y1 for o in self.engine.elements.values())
            pos2_occ = any(o.id != el.id and o.x == cand_x2 and o.y == cand_y2 for o in self.engine.elements.values())
            if not pos1_occ and not pos2_occ:
                offset = test_off
                break

        el.x = orig_x - offset * ux
        el.y = orig_y - offset * uy
        el.length_miles = half_len

        # Partition neighbors: negative side stays with el, positive side moves to new_el
        old_neighbors = dict(el.neighbors or {})
        el_neighbors = {}
        new_neighbors = {}

        for d, nlist in old_neighbors.items():
            d_up = d.upper()
            if d_up == pos_dir or d_up in COMPATIBLE_DIRS.get(pos_dir, ()):
                new_neighbors[d] = list(nlist)
            else:
                el_neighbors[d] = list(nlist)

        # Do NOT connect them so a turnout could be inserted!
        el.neighbors = el_neighbors

        # Create new track element
        new_el = TrackSegment(
            id=new_id,
            elem_type="track",
            x=orig_x + offset * ux,
            y=orig_y + offset * uy,
            label=f"{el.label} B" if el.label else "",
            label_pos=el.label_pos,
            orientation=el.orientation,
            length_miles=half_len,
            neighbors=new_neighbors,
            parallel_track_id=el.parallel_track_id,
            has_occ_bit=False,
            wiu_id=el.wiu_id,
            occ_bit_offset=None,
        )

        self.engine.elements[new_id] = new_el

        # Update positive-side neighbors so they point to new_id instead of el.id
        for d, nlist in new_neighbors.items():
            for nid in nlist:
                nbr_obj = self.engine.elements.get(nid)
                if isinstance(nbr_obj, TrackSegment):
                    for nd, nd_list in nbr_obj.neighbors.items():
                        nbr_obj.neighbors[nd] = [new_id if x == el.id else x for x in nd_list]
                elif isinstance(nbr_obj, Turnout):
                    if nbr_obj.common_neighbor == el.id: nbr_obj.common_neighbor = new_id
                    if nbr_obj.normal_neighbor == el.id: nbr_obj.normal_neighbor = new_id
                    if nbr_obj.reverse_neighbor == el.id: nbr_obj.reverse_neighbor = new_id

        if self.inplace_editor:
            self.inplace_editor = None

        self.dirty = True
        self.selected_ids = {el.id, new_id}
        self.crank_engine()
        self.status_toast = f"Split '{el.id}' into '{el.id}' & '{new_id}' (gap at ({orig_x},{orig_y}) for turnout)."

    @staticmethod
    def _rotate_compass_dir(direction_str: str, step: int) -> str:
        """Rotates an 8-point compass direction clockwise by `step` * 45 degrees."""
        compass_cw = ("E", "SE", "S", "SW", "W", "NW", "N", "NE")
        d = str(direction_str or "").strip().upper()
        if d not in compass_cw:
            return direction_str
        return compass_cw[(compass_cw.index(d) + step) % 8]

    @staticmethod
    def _rotate_track_orientation(orient_str: str, step: int) -> str:
        """Rotates a TrackSegment orientation clockwise by `step` * 45 degrees."""
        rot_cycle = ("E-W", "NW-SE", "N-S", "NE-SW")
        cur = str(orient_str or "E-W").strip().upper()
        if cur not in rot_cycle:
            cur = "E-W"
        return rot_cycle[(rot_cycle.index(cur) + step) % 4]

    def move_selected_elements(self, dx: int, dy: int, push_undo: bool = True, auto_connect: bool = True) -> bool:
        """Moves all currently selected layout elements by (dx, dy) logical grid units."""
        targets = [
            self.engine.elements[sid]
            for sid in sorted(self.selected_ids)
            if sid in self.engine.elements
        ]
        if not targets or (dx == 0 and dy == 0):
            return False

        self._group_rot_session = None
        if push_undo:
            self.push_undo_state()

        for el in targets:
            el.x = int(el.x) + int(dx)
            el.y = int(el.y) + int(dy)

        if auto_connect:
            for el in targets:
                if el.elem_type not in SYSTEM_LIST_TYPES:
                    LayoutTopologyAssistant.auto_suggest_connections(el, self.engine.elements)

        self.dirty = True
        self._cached_render_state = None
        self.crank_engine()
        if len(targets) == 1:
            t0 = targets[0]
            self.status_toast = f"Moved '{t0.id}' to ({t0.x}, {t0.y})."
        else:
            self.status_toast = f"Moved {len(targets)} selected elements by ({dx:+d}, {dy:+d})."
        return True

    def rotate_selected_track(self, direction: int = 1, mx: Optional[int] = None, my: Optional[int] = None) -> bool:
        """Rotates either:
          - Multiple selected layout elements together around their common center of mass in 45° increments, OR
          - A single selected (or hovered) TrackSegment in place in 45° increments.
        direction: +1 for clockwise / wheel up, -1 for counter-clockwise / wheel down.
        """
        step_dir = 1 if direction >= 0 else -1
        selected_layout = [
            self.engine.elements[sid]
            for sid in sorted(self.selected_ids)
            if sid in self.engine.elements and self.engine.elements[sid].elem_type not in SYSTEM_LIST_TYPES
        ]

        # Multi-element rotation around common center of mass (when >1 layout elements are selected)
        if len(selected_layout) > 1:
            target_ids = frozenset(el.id for el in selected_layout)
            sess = self._group_rot_session
            reuse_session = (
                isinstance(sess, dict)
                and sess.get("ids") == target_ids
                and all(
                    (int(el.x), int(el.y)) == sess.get("last_pos", {}).get(el.id)
                    for el in selected_layout
                )
            )
            if not reuse_session:
                cx = sum(float(el.x) for el in selected_layout) / float(len(selected_layout))
                cy = sum(float(el.y) for el in selected_layout) / float(len(selected_layout))
                orig_map: Dict[str, Dict[str, Any]] = {}
                for el in selected_layout:
                    rec: Dict[str, Any] = {"x": float(el.x), "y": float(el.y)}
                    if isinstance(el, TrackSegment):
                        rec["orientation"] = el.orientation or "E-W"
                        rec["neighbors"] = copy.deepcopy(el.neighbors) if isinstance(el.neighbors, dict) else {}
                    elif isinstance(el, Turnout):
                        rec["orientation"] = el.orientation or "E"
                        rec["normal_dir"] = el.normal_dir or "E"
                        rec["reverse_dir"] = el.reverse_dir or "NE"
                    elif isinstance(el, (Signal, TrackJump)):
                        rec["direction"] = el.direction or "E"
                    orig_map[el.id] = rec
                sess = {
                    "ids": target_ids,
                    "cx": cx,
                    "cy": cy,
                    "step": 0,
                    "orig": orig_map,
                    "last_pos": {},
                }
                self._group_rot_session = sess

            self.push_undo_state()
            sess["step"] += step_dir
            step_total = sess["step"]
            cx = sess["cx"]
            cy = sess["cy"]

            # Exact 8-step clockwise trig table for screen coordinates (+x East, +y South)
            s45 = math.sqrt(0.5)
            trig_table = (
                (1.0, 0.0),    # 0°
                (s45, s45),    # 45° CW
                (0.0, 1.0),    # 90° CW
                (-s45, s45),   # 135° CW
                (-1.0, 0.0),   # 180°
                (-s45, -s45),  # 225° CW
                (0.0, -1.0),   # 270° CW
                (s45, -s45),   # 315° CW
            )
            cos_t, sin_t = trig_table[step_total % 8]
            last_pos: Dict[str, Tuple[int, int]] = {}

            for el in selected_layout:
                orig = sess["orig"][el.id]
                dx = orig["x"] - cx
                dy = orig["y"] - cy
                el.x = int(round(cx + dx * cos_t - dy * sin_t))
                el.y = int(round(cy + dx * sin_t + dy * cos_t))
                last_pos[el.id] = (el.x, el.y)

                if isinstance(el, TrackSegment):
                    el.orientation = self._rotate_track_orientation(orig["orientation"], step_total)
                    new_nbrs: Dict[str, List[str]] = {}
                    for d_key, id_list in orig["neighbors"].items():
                        rot_key = self._rotate_compass_dir(d_key, step_total)
                        bucket = new_nbrs.setdefault(rot_key, [])
                        for nid in id_list:
                            if nid not in bucket:
                                bucket.append(nid)
                    el.neighbors = new_nbrs
                elif isinstance(el, Turnout):
                    el.orientation = self._rotate_compass_dir(orig["orientation"], step_total)
                    el.normal_dir = self._rotate_compass_dir(orig["normal_dir"], step_total)
                    el.reverse_dir = self._rotate_compass_dir(orig["reverse_dir"], step_total)
                elif isinstance(el, (Signal, TrackJump)):
                    el.direction = self._rotate_compass_dir(orig["direction"], step_total)

                if self.inplace_editor and self.inplace_editor.element.id == el.id:
                    for f in self.inplace_editor.fields:
                        if f["key"] == "orientation" and hasattr(el, "orientation"):
                            f["val"] = el.orientation
                        elif f["key"] == "direction" and hasattr(el, "direction"):
                            f["val"] = el.direction
                        elif f["key"] == "normal_dir" and hasattr(el, "normal_dir"):
                            f["val"] = el.normal_dir
                        elif f["key"] == "reverse_dir" and hasattr(el, "reverse_dir"):
                            f["val"] = el.reverse_dir

            sess["last_pos"] = last_pos
            self.dirty = True
            self._cached_render_state = None
            self.crank_engine()
            delta_deg = step_dir * 45
            total_deg = (step_total * 45) % 360
            self.status_toast = (
                f"Rotated {len(selected_layout)} elements by {delta_deg:+d}° around center "
                f"({cx:.1f}, {cy:.1f}) [total {total_deg}°]."
            )
            return True

        # Single-track rotation in place
        self._group_rot_session = None
        target_tracks: List[TrackSegment] = []
        if self.selected_ids:
            for sid in self.selected_ids:
                el = self.engine.elements.get(sid)
                if isinstance(el, TrackSegment):
                    target_tracks.append(el)
        if not target_tracks and self.inplace_editor and isinstance(self.inplace_editor.element, TrackSegment):
            target_tracks.append(self.inplace_editor.element)
        if not target_tracks and mx is not None and my is not None:
            hit_id = self.pick_element_at_screen(mx, my)
            if hit_id:
                el = self.engine.elements.get(hit_id)
                if isinstance(el, TrackSegment):
                    target_tracks.append(el)
                    self.selected_ids = {hit_id}

        if not target_tracks:
            return False

        self.push_undo_state()
        for trk in target_tracks:
            new_orient = self._rotate_track_orientation(trk.orientation or "E-W", step_dir)
            trk.orientation = new_orient

            # Sync editor if open for this track
            if self.inplace_editor and self.inplace_editor.element.id == trk.id:
                for f in self.inplace_editor.fields:
                    if f["key"] == "orientation":
                        f["val"] = new_orient

        self.dirty = True
        self._cached_render_state = None
        self.crank_engine()
        names = ", ".join(t.id for t in target_tracks)
        last_orient = target_tracks[0].orientation
        self.status_toast = f"Rotated track '{names}' to {last_orient} (45° increment)."
        return True

    def _handle_element_picked_for_editor(self, hit_id: str):
        """Handles selecting a layout element with the mouse while filling out an element form."""
        if not self.inplace_editor:
            return
        ed = self.inplace_editor
        edited_el = ed.element
        if hit_id == edited_el.id:
            return
        pointed_el = self.engine.elements.get(hit_id)
        if not pointed_el:
            return

        rel_dir = LayoutTopologyAssistant.determine_relative_direction(edited_el, pointed_el)
        cur_field = ed.current_field()
        fld_key = cur_field.get("key")

        # 1. TrackSegment: Set neighbors field with DIR:ID parallel to orientation
        if fld_key == "neighbors" or (isinstance(edited_el, TrackSegment) and fld_key not in ("parallel_track_id",)):
            nbr_idx = next((i for i, f in enumerate(ed.fields) if f["key"] == "neighbors"), None)
            if nbr_idx is not None:
                ed.active_field_idx = nbr_idx
                cur_field = ed.fields[nbr_idx]

            existing_str = cur_field["val"]
            tokens = [t.strip() for t in existing_str.split(",") if t.strip()]
            new_token = f"{rel_dir}:{hit_id}"

            new_tokens = []
            replaced = False
            for tok in tokens:
                if ":" in tok:
                    d, nid = [x.strip() for x in tok.split(":", 1)]
                    if d.upper() == rel_dir.upper():
                        new_tokens.append(new_token)
                        replaced = True
                    else:
                        new_tokens.append(tok)
                else:
                    new_tokens.append(tok)
            if not replaced:
                new_tokens.append(new_token)

            cur_field["val"] = ", ".join(new_tokens)
            ed.select_all_active = False
            self.status_toast = f"Set neighbor '{new_token}' for '{edited_el.id}'."
            return

        # 2. Turnout leg fields
        if isinstance(edited_el, Turnout):
            if fld_key in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                cur_field["val"] = hit_id
                ed.select_all_active = False
                self.status_toast = f"Set {cur_field['label']} = '{hit_id}'."
                return
            elif fld_key == "guarded_by_signals":
                items = [x.strip() for x in cur_field["val"].split(",") if x.strip()]
                if hit_id not in items:
                    items.append(hit_id)
                cur_field["val"] = ", ".join(items)
                ed.select_all_active = False
                self.status_toast = f"Added '{hit_id}' to guarded signals."
                return
            else:
                pt_dir = edited_el.orientation or "E"
                opp_dir = OPPOSITE_DIR.get(pt_dir, "")
                if rel_dir == opp_dir:
                    target_key = "common_neighbor"
                elif rel_dir == edited_el.normal_dir:
                    target_key = "normal_neighbor"
                elif rel_dir == edited_el.reverse_dir:
                    target_key = "reverse_neighbor"
                else:
                    target_key = next((k for k in ("common_neighbor", "normal_neighbor", "reverse_neighbor")
                                       if not getattr(edited_el, k, "")), "normal_neighbor")

                for i, f in enumerate(ed.fields):
                    if f["key"] == target_key:
                        ed.active_field_idx = i
                        f["val"] = hit_id
                        ed.select_all_active = False
                        self.status_toast = f"Set {f['label']} = '{hit_id}' ({rel_dir})."
                        break
                return

        # 3. Signal fields
        if isinstance(edited_el, Signal):
            if fld_key in ("protects_element", "rear_element"):
                cur_field["val"] = hit_id
                ed.select_all_active = False
                self.status_toast = f"Set {cur_field['label']} = '{hit_id}'."
                return
            else:
                sig_dir = getattr(edited_el, "direction", "E")
                target_key = "protects_element" if rel_dir == sig_dir else "rear_element"
                for i, f in enumerate(ed.fields):
                    if f["key"] == target_key:
                        ed.active_field_idx = i
                        f["val"] = hit_id
                        ed.select_all_active = False
                        self.status_toast = f"Set {f['label']} = '{hit_id}' ({rel_dir})."
                        break
                return

        # 4. Other single element reference fields
        if fld_key in ("parallel_track_id", "connected_element", "paired_jump_id"):
            cur_field["val"] = hit_id
            ed.select_all_active = False
            self.status_toast = f"Set {cur_field['label']} = '{hit_id}'."
            return

        # 5. Multi-element list fields (e.g. Crossing monitored_tracks)
        if fld_key == "monitored_tracks":
            items = [x.strip() for x in cur_field["val"].split(",") if x.strip()]
            if hit_id not in items:
                items.append(hit_id)
            cur_field["val"] = ", ".join(items)
            ed.select_all_active = False
            self.status_toast = f"Added '{hit_id}' to monitored tracks."
            return

        # 6. Default fallback for choice fields containing element IDs
        if hit_id in cur_field.get("choices", []):
            cur_field["val"] = hit_id
            ed.select_all_active = False
            self.status_toast = f"Set {cur_field['label']} = '{hit_id}'."
    def _clip_set(self, text: str):
        self.wiu_clipboard = text
        try:
            import tkinter as tk
            r = tk.Tk(); r.withdraw()
            r.clipboard_clear(); r.clipboard_append(text); r.update(); r.destroy()
        except Exception:
            pass

    def _clip_get(self) -> str:
        try:
            import tkinter as tk
            r = tk.Tk(); r.withdraw()
            txt = r.clipboard_get()
            r.destroy()
            if txt:
                return str(txt).strip()
        except Exception:
            pass
        return getattr(self, "wiu_clipboard", "") or ""

    def copy_wiu_of_selected(self):
        for eid in sorted(self.selected_ids):
            el = self.engine.elements.get(eid)
            if el is not None and getattr(el, "wiu_id", None) is not None:
                self._clip_set(str(el.wiu_id))
                self.status_toast = f"Copied WIU {el.wiu_id} from '{eid}'."
                return
        self.status_toast = "No selected element has a WIU address to copy."

    def paste_wiu_to_selected(self):
        raw = self._clip_get().strip()
        if not raw.isdigit():
            self.status_toast = f"Clipboard does not contain a WIU address ('{raw[:20]}')."
            return
        targets = [self.engine.elements[e] for e in self.selected_ids if e in self.engine.elements and hasattr(self.engine.elements[e], "wiu_id") and self.engine.elements[e].elem_type not in SYSTEM_LIST_TYPES]
        if not targets:
            self.status_toast = "Select element(s) to paste the WIU address into."
            return
        self.push_undo_state()
        for el in targets:
            el.wiu_id = int(raw)
        try:
            self.wiu_cache.register_wiu(int(raw))
        except Exception:
            pass
        self.dirty = True
        self.crank_engine()
        self.status_toast = f"Pasted WIU {raw} into {len(targets)} element(s)."

    def cycle_selection(self, delta: int):
        ids = sorted(self.engine.elements.keys())
        if not ids:
            return
        cur = next(iter(self.selected_ids)) if len(self.selected_ids) == 1 else None
        if cur in ids:
            nxt = ids[(ids.index(cur) + delta) % len(ids)]
        else:
            nxt = ids[0] if delta > 0 else ids[-1]
        self.selected_ids = {nxt}
        el = self.engine.elements[nxt]
        sw, sh = self.screen.get_size()
        sx, sy = self.logical_to_screen(float(el.x), float(el.y))
        if not (60 <= sx <= sw - 60 and 80 <= sy <= sh - 60):
            self.cam_x += sw / 2 - sx
            self.cam_y += sh / 2 - sy
        self.status_toast = f"Selected '{nxt}' ({el.elem_type}) [{ids.index(nxt) + 1}/{len(ids)}]"

    def _handle_editor_keydown(self, event: pygame.event.Event):
        ed = self.inplace_editor
        if not ed: return

        if event.key == pygame.K_ESCAPE:
            if ed.is_newly_created:
                self.engine.elements.pop(ed.element.id, None)
                self.selected_ids.discard(ed.element.id)
                if self.undo_stack: self.undo_stack.pop()
                self.status_toast = f"Cancelled creation of '{ed.element.id}'."
            self.inplace_editor = None
            return

        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            force = bool(event.mod & pygame.KMOD_SHIFT)
            if ed.validate_and_apply(force_apply=force):
                if ed.element.elem_type not in SYSTEM_LIST_TYPES:
                    LayoutTopologyAssistant.auto_suggest_connections(ed.element, self.engine.elements)
                self.dirty = True
                self.crank_engine()
                self.inplace_editor = None
                self.status_toast = f"Updated '{ed.element.id}' and cranked engine."
            return

        if event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_END):
            ed.select_all_active = False
            return

        if event.key == pygame.K_TAB:
            suggs = ed.get_filtered_suggestions(ed.current_field())
            if suggs and not (event.mod & pygame.KMOD_SHIFT):
                ed.accept_suggestion(suggs[0])
            else:
                delta = -1 if (event.mod & pygame.KMOD_SHIFT) else 1
                ed.active_field_idx = (ed.active_field_idx + delta) % len(ed.fields)
                ed.select_all_active = True
            return
        if event.key == pygame.K_DOWN:
            ed.active_field_idx = (ed.active_field_idx + 1) % len(ed.fields)
            ed.select_all_active = True
            return
        if event.key == pygame.K_UP:
            ed.active_field_idx = (ed.active_field_idx - 1) % len(ed.fields)
            ed.select_all_active = True
            return

        if (event.mod & pygame.KMOD_CTRL) and event.key == pygame.K_g:
            self._open_gps_picker_for_editor()
            return

        if (event.mod & pygame.KMOD_CTRL) and event.key == pygame.K_c:
            fd = ed.current_field()
            val = fd["val"].strip()
            if not val:
                wfd = next((f for f in ed.fields if f["key"] == "wiu_id"), None)
                val = wfd["val"].strip() if wfd else ""
            if val:
                self._clip_set(val)
                self.status_toast = f"Copied '{val}' to clipboard."
            return

        if (event.mod & pygame.KMOD_CTRL) and event.key == pygame.K_v:
            txt = self._clip_get().replace("\r", "").split("\n")[0].strip()
            if txt:
                fd = ed.current_field()
                if ed.select_all_active or not fd["val"]:
                    fd["val"] = txt
                else:
                    fd["val"] += txt
                ed.select_all_active = False
                ed.on_field_changed(fd["key"])
                self.status_toast = f"Pasted '{txt}' into {fd['label']}."
            return

        if event.key == pygame.K_BACKSPACE:
            fd = ed.current_field()
            if (event.mod & pygame.KMOD_CTRL) or ed.select_all_active:
                fd["val"] = ""
                ed.select_all_active = False
            else:
                fd["val"] = fd["val"][:-1]
            ed.on_field_changed(fd["key"])
            return

        if event.unicode and event.unicode.isprintable():
            fd = ed.current_field()
            if ed.select_all_active:
                fd["val"] = event.unicode
                ed.select_all_active = False
            else:
                fd["val"] += event.unicode
            ed.on_field_changed(fd["key"])

    def _handle_canvas_keydown(self, event: pygame.event.Event) -> bool:
        ctrl = bool(event.mod & pygame.KMOD_CTRL)
        shift = bool(event.mod & pygame.KMOD_SHIFT)

        if event.key == pygame.K_q and ctrl: return self.request_exit()
        elif ctrl and event.key == pygame.K_z and not shift: self.undo()
        elif (ctrl and event.key == pygame.K_y) or (ctrl and shift and event.key == pygame.K_z): self.redo()
        elif event.key == pygame.K_ESCAPE:
            self.selected_ids.clear()
            self.active_tool = "select"
            self.show_verification_panel = False
            self.status_toast = "Deselected all elements."
        elif event.key in (pygame.K_DELETE, pygame.K_BACKSPACE): self.delete_selected()
        elif shift and not ctrl and event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
            dx = -1 if event.key == pygame.K_LEFT else (1 if event.key == pygame.K_RIGHT else 0)
            dy = -1 if event.key == pygame.K_UP else (1 if event.key == pygame.K_DOWN else 0)
            if self.move_selected_elements(dx, dy, push_undo=True, auto_connect=True):
                self._shift_arrow_repeat_ts = time.time() + 0.35
                self._shift_arrow_moved_during_hold = False
            else:
                self.status_toast = "Select element(s) to move with Shift + Arrow keys."
        elif event.key == pygame.K_r and not ctrl:
            direction = -1 if shift else 1
            if not self.rotate_selected_track(direction):
                self.status_toast = "Select a track or multiple elements to rotate in 45° increments (R or Shift-Scroll)."
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.selected_ids:
            sel_id = next(iter(sorted(self.selected_ids)))
            if sel_id in self.engine.elements:
                el = self.engine.elements[sel_id]
                self.push_undo_state()
                self.selected_ids = {sel_id}
                sx, sy = self.logical_to_screen(float(el.x), float(el.y))
                self.inplace_editor = InPlaceElementEditor(
                    element=el,
                    screen_pos=(int(sx), int(sy)),
                    all_elements=self.engine.elements,
                    wiu_cache=self.wiu_cache,
                    get_last_gps_cb=lambda: self.last_selected_gps,
                    set_last_gps_cb=self._update_last_selected_gps,
                    default_radius=self.default_radius,
                    is_newly_created=False,
                    get_ref_gps_cb=lambda el_ref=el: self.get_active_ref_gps(el_ref),
                )
                self.status_toast = f"Opened editor for '{sel_id}'."
        elif event.key == pygame.K_TAB:
            self.cycle_selection(-1 if (event.mod & (pygame.KMOD_ALT | pygame.KMOD_SHIFT)) else 1)
        elif ctrl and shift and event.key == pygame.K_c: self.copy_wiu_of_selected()
        elif ctrl and shift and event.key == pygame.K_v: self.paste_wiu_to_selected()
        elif ctrl and event.key == pygame.K_c: self.copy_selected()
        elif ctrl and event.key == pygame.K_v: self.paste_clipboard()
        elif ctrl and event.key == pygame.K_n: self.new_layout()
        elif ctrl and event.key == pygame.K_s and not shift: self.save_current_layout()
        elif ctrl and event.key == pygame.K_s and shift:
            self.file_prompt_mode = "save_as"; self.file_prompt_buffer = self.layout_filepath or "new_layout.json"
        elif ctrl and event.key == pygame.K_o:
            self.request_load_layout()
        elif ctrl and shift and event.key == pygame.K_f: self.prompt_save_fragment()
        elif ctrl and event.key == pygame.K_f: self.open_frag_picker_modal()
        elif ctrl and shift and event.key == pygame.K_b: self.upload_current_to_banc()
        elif ctrl and event.key == pygame.K_b: self.open_banc_coverage_modal()
        elif event.key == pygame.K_m and not ctrl:
            self.file_prompt_mode = "merge"; self.file_prompt_buffer = ""
        elif event.key == pygame.K_v and not ctrl: self.run_verification()
        elif event.key == pygame.K_k:
            self.crank_engine()
            self.status_toast = "Cranked DispatcherEngine across current layout states."
        elif event.key == pygame.K_s and not ctrl and self.active_tool == "sim_crank":
            self.turn_all_signals_to_stop()
        elif event.key == pygame.K_SPACE and self.playback_packets and not self.inplace_editor:
            self.playback_active = not self.playback_active
            st = "PLAYING" if self.playback_active else "PAUSED"
            self.status_toast = f"Playback: {st} ({self.playback_idx}/{len(self.playback_packets)})"
        elif event.key == pygame.K_RIGHTBRACKET and self.playback_packets and not self.inplace_editor:
            self.step_playback(forward=True)
        elif event.key == pygame.K_LEFTBRACKET and self.playback_packets and not self.inplace_editor:
            self.step_playback(forward=False)
        elif event.key == pygame.K_l and not ctrl and not self.inplace_editor:
            is_on = self.engine.toggle_raw_packet_logging()
            st = "ENABLED" if is_on else "DISABLED"
            self.status_toast = f"Engine Raw Packet Logging: {st} -> '{self.engine.raw_log_filepath}'"
        elif event.key == pygame.K_b and not ctrl and not self.inplace_editor:
            self.open_big_map()
        elif event.key == pygame.K_a and self.selected_ids:
            self.push_undo_state()
            for eid in self.selected_ids:
                el = self.engine.elements.get(eid)
                if el and el.elem_type not in SYSTEM_LIST_TYPES:
                    LayoutTopologyAssistant.auto_suggest_connections(el, self.engine.elements)
            self.crank_engine()
            self.status_toast = f"Updated connection suggestions for {len(self.selected_ids)} element(s)."
        elif pygame.K_1 <= event.key <= pygame.K_9:
            idx = event.key - pygame.K_1
            if idx < len(ELEMENT_TYPES):
                self.active_tool = ELEMENT_TYPES[idx]
                self.status_toast = f"Placement Tool: {self.active_tool.upper()} (Click anywhere on field to place)"
        elif event.key == pygame.K_0:
            self.active_tool = "track_jump"
            self.status_toast = "Placement Tool: TRACK_JUMP Line-Break Connector (Click anywhere on field to place)"
        elif event.key == pygame.K_x and not ctrl:
            self.split_selected_track()
        elif event.key == pygame.K_HOME:
            self.center_view_on_layout()
            self.status_toast = "Centered view on layout elements."
        return True

    def _handle_mouse_down(self, event: pygame.event.Event):
        mx, my = event.pos

        if self.file_prompt_mode:
            if event.button == 1 and self._handle_file_prompt_mouse_down(event.pos):
                return
            return

        if self.banc_coverage_modal:
            self.banc_coverage_modal.handle_mouse_down(event.pos, event.button)
            return

        if self.frag_picker_modal:
            frag_to_load = self.frag_picker_modal.handle_mouse_down(event.pos, event.button)
            if frag_to_load:
                self.load_and_merge_fragment(frag_to_load)
            return

        if self.gps_picker_modal:
            gm = self.gps_picker_modal
            if gm.show_element_selector:
                if event.button == 1:
                    for ekey, r in gm.selector_btn_rects.items():
                        if r.collidepoint(mx, my):
                            if ekey == "cancel_selector":
                                gm.show_element_selector = False
                            elif gm.on_create_element_cb:
                                gm.on_create_element_cb(ekey, gm.picked_lat, gm.picked_lon)
                            return
                    if not getattr(gm, "selector_box_rect", pygame.Rect(0, 0, 0, 0)).collidepoint(mx, my):
                        gm.show_element_selector = False
                return

            if event.button == 1:
                for bkey, r in gm.btn_rects.items():
                    if r.collidepoint(mx, my):
                        if bkey == "toggle_layer":
                            gm.layer = "osm" if gm.layer == "sat" else "sat"
                        elif bkey == "center_layout":
                            gps_pts = [el.gps for el in gm.all_elements.values() if getattr(el, "gps", None) and len(el.gps) == 2]
                            if gps_pts:
                                gm.center_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                                gm.center_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                                self.status_toast = "Big Map: Centered on layout elements."
                        elif bkey == "zoom_in":
                            gm.zoom_at_point(gm.map_rect.centerx, gm.map_rect.centery, 1)
                        elif bkey == "zoom_out":
                            gm.zoom_at_point(gm.map_rect.centerx, gm.map_rect.centery, -1)
                        elif bkey == "radius_dec":
                            gm.step_view_radius(-1)
                        elif bkey == "radius_inc":
                            gm.step_view_radius(+1)
                        elif bkey == "radius_val":
                            gm.cycle_view_radius(+1)
                        elif bkey == "define_element":
                            gm.show_element_selector = True
                        elif bkey == "apply":
                            if gm.on_pick_cb:
                                gm.on_pick_cb(gm.picked_lat, gm.picked_lon)
                            self.gps_picker_modal = None
                        elif bkey == "cancel":
                            self._close_gps_modal()
                        return

                if gm.map_rect.collidepoint(mx, my):
                    gm.is_panning = True
                    gm.pan_has_moved = False
                    gm.pan_start_mouse = (mx, my)
                    gm.pan_start_world_px = gm.tile_mgr.latlon_to_world_px(gm.center_lat, gm.center_lon, gm.zoom)
                    return

                if not gm.modal_rect.collidepoint(mx, my):
                    self._close_gps_modal()
                return

            elif event.button == 3 and gm.map_rect.collidepoint(mx, my):
                lat_v, lon_v = gm.screen_to_latlon(mx, my)
                gm.picked_lat, gm.picked_lon = lat_v, lon_v
                if gm.mode == "pick" and gm.on_pick_cb:
                    gm.on_pick_cb(lat_v, lon_v)
                self.status_toast = f"Picked GPS ({lat_v:.6f}, {lon_v:.6f})."
                return
            return

        if self.inplace_editor:
            for s_rect, s_val in self.inplace_editor.suggestion_rects:
                if s_rect.collidepoint(mx, my):
                    self.inplace_editor.accept_suggestion(s_val)
                    return
            for idx, r in enumerate(self.inplace_editor.field_rects):
                if r.collidepoint(mx, my):
                    self.inplace_editor.select_field(idx)
                    return
            for bkey, r in self.inplace_editor.btn_rects.items():
                if r.collidepoint(mx, my):
                    if bkey == "apply":
                        if self.inplace_editor.validate_and_apply():
                            if self.inplace_editor.element.elem_type not in SYSTEM_LIST_TYPES:
                                LayoutTopologyAssistant.auto_suggest_connections(self.inplace_editor.element, self.engine.elements)
                            self.dirty = True
                            self.crank_engine()
                            self.inplace_editor = None
                    elif bkey == "auto_adj":
                        if self.inplace_editor.element.elem_type not in SYSTEM_LIST_TYPES:
                            LayoutTopologyAssistant.auto_suggest_connections(self.inplace_editor.element, self.engine.elements)
                        self.inplace_editor._build_fields_from_element()
                    elif bkey == "gps":
                        self._open_gps_picker_for_editor()
                    elif bkey == "cancel":
                        if self.inplace_editor.is_newly_created:
                            self.engine.elements.pop(self.inplace_editor.element.id, None)
                            self.selected_ids.discard(self.inplace_editor.element.id)
                            if self.undo_stack: self.undo_stack.pop()
                        self.inplace_editor = None
                    elif bkey == "split":
                        split_id = self.inplace_editor.element.id
                        self.inplace_editor = None
                        self.split_selected_track(split_id)
                        return
                    elif bkey == "copy_hex":
                        raw_hex = getattr(self.inplace_editor.element, "_last_raw_hex", "") or getattr(self.inplace_editor.element, "last_raw_hex", "") or ""
                        if raw_hex:
                            copy_to_clipboard(raw_hex)
                            self.status_toast = f"Copied raw hex ({len(raw_hex)} chars) to clipboard!"
                        else:
                            self.status_toast = "No raw packet hex available to copy."
                    return
            
            if getattr(self.inplace_editor, 'panel_rect', None) and self.inplace_editor.panel_rect.collidepoint(mx, my):
                self.inplace_editor.is_dragging = True
                self.inplace_editor.drag_offset = (self.inplace_editor.px - mx, self.inplace_editor.py - my)
                return

            # Click was outside editor panel: check HUD buttons or select element for form
            if event.button == 1:
                if my < getattr(self, "hud_bar_height", 50):
                    for bkey, rect in self.hud_buttons.items():
                        if rect.collidepoint(mx, my):
                            self._activate_hud_button(bkey)
                            return
                hit_id = self.pick_element_at_screen(mx, my)
                if hit_id and hit_id != self.inplace_editor.element.id:
                    self._handle_element_picked_for_editor(hit_id)
                    return
            return

        if event.button == 1:
            if self.playback_packets:
                for pb_key, pb_rect in self.playback_btn_rects.items():
                    if pb_rect.collidepoint(mx, my):
                        if pb_key == "reset":
                            self.playback_idx = 0
                            self.playback_active = False
                            self.status_toast = "Playback reset to beginning."
                        elif pb_key == "prev":
                            self.step_playback(forward=False)
                        elif pb_key == "play_pause":
                            self.playback_active = not self.playback_active
                            st = "PLAYING" if self.playback_active else "PAUSED"
                            self.status_toast = f"Playback: {st}"
                        elif pb_key == "next":
                            self.step_playback(forward=True)
                        elif pb_key == "close":
                            self.playback_packets.clear()
                            self.playback_active = False
                            self.status_toast = "Closed raw log playback."
                        return

            for bkey, rect in self.hud_buttons.items():
                if rect.collidepoint(mx, my):
                    self._activate_hud_button(bkey)
                    return
            if my < getattr(self, "hud_bar_height", 50):
                return

        if event.button == 2:
            if my < getattr(self, "hud_bar_height", 50):
                return
            self.is_panning = True
            self.pan_start_mouse = (mx, my)
            self.pan_start_cam = (self.cam_x, self.cam_y)
            return

        if event.button == 3:
            if my < getattr(self, "hud_bar_height", 50):
                for bkey, rect in self.hud_buttons.items():
                    if rect.collidepoint(mx, my):
                        if bkey == "set_map_radius":
                            self.file_prompt_mode = "set_map_radius"
                            self.file_prompt_buffer = f"{self.map_view_radius_miles:g}"
                            return
                return
            hit_id = self.pick_element_at_screen(mx, my)
            if hit_id and hit_id in self.engine.elements:
                self.toggle_physical_state_on_element(self.engine.elements[hit_id])
            else:
                self.is_panning = True
                self.pan_start_mouse = (mx, my)
                self.pan_start_cam = (self.cam_x, self.cam_y)
            return

        if event.button == 1:
            # Check Train Scroller delete and assignment row interactions
            for del_rect, tkey in self._train_scroller_del_rects:
                if del_rect.collidepoint(mx, my):
                    self.delete_train(tkey)
                    return
            for row_rect, trn in self._train_scroller_row_rects:
                if row_rect.collidepoint(mx, my):
                    self._open_train_assignment(trn)
                    return

            if self.active_tool in ELEMENT_TYPES:
                lx, ly = self.screen_to_logical(mx, my)
                rx, ry = int(round(lx)), int(round(ly))
                self.create_element_at(self.active_tool, rx, ry, (mx, my))
                self.active_tool = "select"
                return

            handle_hit = self.pick_track_endpoint_handle_at_screen(mx, my)
            if handle_hit and self.active_tool == "select":
                trk_id, end_idx, fixed_pt = handle_hit
                self.push_undo_state()
                self.selected_ids = {trk_id}
                self.dragging_track_end = (trk_id, end_idx, fixed_pt)
                self.track_end_drag_curr_logical = self.screen_to_logical(mx, my)
                self.status_toast = f"Dragging endpoint of '{trk_id}' (45° snap + auto-sectioning enabled)..."
                return

            hit_id = self.pick_element_at_screen(mx, my)
            now = time.time()

            if hit_id and hit_id in self.engine.elements:
                el = self.engine.elements[hit_id]
                if self.active_tool == "sim_crank":
                    self.toggle_physical_state_on_element(el)
                    return

                if hit_id == self.last_click_id and (now - self.last_click_time) < 0.35:
                    self.push_undo_state()
                    self.selected_ids = {hit_id}
                    self.inplace_editor = InPlaceElementEditor(
                        element=el,
                        screen_pos=(mx, my),
                        all_elements=self.engine.elements,
                        wiu_cache=self.wiu_cache,
                        get_last_gps_cb=lambda: self.last_selected_gps,
                        set_last_gps_cb=self._update_last_selected_gps,
                        default_radius=self.default_radius,
                        is_newly_created=False,
                        get_ref_gps_cb=lambda el_ref=el: self.get_active_ref_gps(el_ref),
                    )
                    self.last_click_time = 0.0
                    return

                self.last_click_id = hit_id
                self.last_click_time = now

                shift = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)
                if shift:
                    if hit_id in self.selected_ids: self.selected_ids.remove(hit_id)
                    else: self.selected_ids.add(hit_id)
                else:
                    if hit_id not in self.selected_ids: self.selected_ids = {hit_id}

                self.dragging_selection = True
                self.drag_moved = False
                self.drag_anchor_logical = self.screen_to_logical(mx, my)
                self.drag_initial_positions = {
                    sid: (self.engine.elements[sid].x, self.engine.elements[sid].y)
                    for sid in self.selected_ids if sid in self.engine.elements
                }
            else:
                if not (pygame.key.get_mods() & pygame.KMOD_SHIFT):
                    self.selected_ids.clear()
                self.box_selecting = True
                self.box_start_screen = (mx, my)
                self.box_curr_screen = (mx, my)

    def _handle_mouse_up(self, event: pygame.event.Event):
        if self.banc_coverage_modal:
            self.banc_coverage_modal.handle_mouse_up(event.pos, event.button)
            return
        if self.frag_picker_modal:
            return
        if event.button == 1 and self.gps_picker_modal:
            gm = self.gps_picker_modal
            was_panning = gm.is_panning
            gm.is_panning = False
            if was_panning and not getattr(gm, "pan_has_moved", False) and not gm.show_element_selector:
                if gm.map_rect.collidepoint(event.pos):
                    lat_v, lon_v = gm.screen_to_latlon(event.pos[0], event.pos[1])
                    gm.picked_lat, gm.picked_lon = lat_v, lon_v
                    if gm.mode == "pick" and gm.on_pick_cb:
                        gm.on_pick_cb(lat_v, lon_v)
                    self.status_toast = f"Picked GPS ({lat_v:.6f}, {lon_v:.6f})."
            return
        if event.button == 1:
            if self.inplace_editor and getattr(self.inplace_editor, 'is_dragging', False):
                self.inplace_editor.is_dragging = False

            if self.dragging_track_end:
                trk_id, _, fixed_pt = self.dragging_track_end
                trk = self.engine.elements.get(trk_id)
                if isinstance(trk, TrackSegment):
                    self._apply_track_endpoint_drag(trk, fixed_pt, self.track_end_drag_curr_logical, finalize_subdivision=True)
                self.dragging_track_end = None
                self.crank_engine()

            if self.dragging_selection:
                if self.drag_moved:
                    for sid in self.selected_ids:
                        el = self.engine.elements.get(sid)
                        if el and el.elem_type not in SYSTEM_LIST_TYPES:
                            LayoutTopologyAssistant.auto_suggest_connections(el, self.engine.elements)
                    self.crank_engine()
                self.dragging_selection = False
                self.drag_moved = False

            if self.box_selecting:
                x1, y1 = self.box_start_screen
                x2, y2 = self.box_curr_screen
                rect = pygame.Rect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
                if rect.width > 6 or rect.height > 6:
                    for eid, el in self.engine.elements.items():
                        sx, sy = self.logical_to_screen(el.x, el.y)
                        if rect.collidepoint(sx, sy): self.selected_ids.add(eid)
                self.box_selecting = False

        if event.button in (2, 3):
            self.is_panning = False

    def _handle_mouse_motion(self, event: pygame.event.Event):
        if self.banc_coverage_modal:
            self.banc_coverage_modal.handle_mouse_move(event.pos)
            return
        if self.frag_picker_modal:
            self.frag_picker_modal.handle_mouse_move(event.pos)
            return
        if self.gps_picker_modal:
            gm = self.gps_picker_modal
            if gm.is_panning:
                mx, my = event.pos
                dx = mx - gm.pan_start_mouse[0]
                dy = my - gm.pan_start_mouse[1]
                if abs(dx) > 3 or abs(dy) > 3:
                    gm.pan_has_moved = True
                sx, sy = gm.pan_start_world_px
                gm.center_lat, gm.center_lon = gm.tile_mgr.world_px_to_latlon(sx - dx, sy - dy, gm.zoom)
            return
        mx, my = event.pos
        if self.inplace_editor and getattr(self.inplace_editor, 'is_dragging', False):
            self.inplace_editor.px = mx + self.inplace_editor.drag_offset[0]
            self.inplace_editor.py = my + self.inplace_editor.drag_offset[1]
            return

        if self.dragging_track_end:
            trk_id, _, fixed_pt = self.dragging_track_end
            mlx, mly = self.screen_to_logical(mx, my)
            self.track_end_drag_curr_logical = (round(mlx * 2.0) / 2.0, round(mly * 2.0) / 2.0)
            trk = self.engine.elements.get(trk_id)
            if isinstance(trk, TrackSegment):
                self._apply_track_endpoint_drag(trk, fixed_pt, self.track_end_drag_curr_logical, finalize_subdivision=False)
        elif self.dragging_selection and self.selected_ids:
            mlx, mly = self.screen_to_logical(mx, my)
            dx = int(round(mlx - self.drag_anchor_logical[0]))
            dy = int(round(mly - self.drag_anchor_logical[1]))
            if (dx != 0 or dy != 0) and not self.drag_moved:
                self.push_undo_state()
                self.drag_moved = True
            for sid, (ox, oy) in self.drag_initial_positions.items():
                if sid in self.engine.elements:
                    self.engine.elements[sid].x = ox + dx
                    self.engine.elements[sid].y = oy + dy
        elif self.box_selecting:
            self.box_curr_screen = (mx, my)
        elif self.is_panning:
            self.cam_x = self.pan_start_cam[0] + (mx - self.pan_start_mouse[0])
            self.cam_y = self.pan_start_cam[1] + (my - self.pan_start_mouse[1])

    def _activate_hud_button(self, bkey: str):
        if bkey == "toggle_sim_select":
            if self.active_tool == "select":
                self.active_tool = "sim_crank"
                self.status_toast = "Mode: SIM/CRANK (Click elements to toggle physical state)"
            elif self.active_tool == "sim_crank":
                self.active_tool = "select"
                self.status_toast = "Mode: SELECT (Click elements to select/drag)"
            else:
                self.active_tool = "select"
                self.status_toast = "Mode: SELECT (Click elements to select/drag)"
        elif bkey == "all_signals_stop":
            self.turn_all_signals_to_stop()
        elif bkey == "open_big_map":
            self.open_big_map()
        elif bkey == "set_map_radius":
            self.cycle_map_view_radius(+1)
        elif bkey.startswith("tool_"):
            self.active_tool = bkey.replace("tool_", "")
            self.status_toast = f"Active Tool: {self.active_tool.upper()} (Click on field to place)"
        elif bkey == "undo": self.undo()
        elif bkey == "redo": self.redo()
        elif bkey == "verify": self.run_verification()
        elif bkey == "crank":
            self.crank_engine()
            self.status_toast = "Cranked DispatcherEngine logic!"
        elif bkey == "copy": self.copy_selected()
        elif bkey == "paste": self.paste_clipboard()
        elif bkey == "split": self.split_selected_track()
        elif bkey == "rotate": self.rotate_selected_track(1)
        elif bkey == "delete": self.delete_selected()
        elif bkey == "new":
            self.new_layout()
        elif bkey == "load":
            self.request_load_layout()
        elif bkey == "import_itcvl":
            self.request_import_itcvl()
        elif bkey == "import_atcs":
            self.request_import_atcs()
        elif bkey == "save": self.save_current_layout()
        elif bkey == "save_as":
            self.file_prompt_mode = "save_as"; self.file_prompt_buffer = self.layout_filepath or "new_layout.json"
        elif bkey == "merge":
            self.file_prompt_mode = "merge"; self.file_prompt_buffer = ""
        elif bkey == "save_frag":
            self.prompt_save_fragment()
        elif bkey == "load_frag":
            self.open_frag_picker_modal()
        elif bkey == "banc_layouts":
            self.open_banc_coverage_modal()
        elif bkey == "banc_upload":
            self.upload_current_to_banc()
        elif bkey == "toggle_labels":
            self.show_labels = not self.show_labels
            self.status_toast = f"Labels {'Shown' if self.show_labels else 'Hidden'}"
        elif bkey == "toggle_inferred":
            self.show_inferred_trains = not self.show_inferred_trains
            self.status_toast = f"Inferred Train Badges {'Shown' if self.show_inferred_trains else 'Hidden'}"
        elif bkey == "toggle_infer_logic":
            self.enable_inference_logic = not self.enable_inference_logic
            self.engine.enable_train_inference = self.enable_inference_logic
            if not self.enable_inference_logic:
                for el in self.engine.elements.values():
                    if hasattr(el, "_inferred_occupied"):
                        el._inferred_occupied = False
                        el._train_direction = None
                        el._train_name = None
                        el._estimated_speed_mph = None
            self.crank_engine()
            self.status_toast = f"Train Inference Logic {'Enabled' if self.enable_inference_logic else 'Disabled (Signals Only)'}"
        elif bkey == "load_log":
            if not self.playback_packets:
                default_log = getattr(self.engine, "raw_log_filepath", os.path.join(tempfile.gettempdir(), "apexbos_raw_packets.log"))
                if os.path.exists(default_log):
                    self.load_raw_packet_log(default_log)
                else:
                    self.file_prompt_mode = "load_log"
                    self.file_prompt_buffer = default_log
            else:
                self.playback_active = not self.playback_active
                st = "PLAYING" if self.playback_active else "PAUSED"
                self.status_toast = f"Playback: {st} ({self.playback_idx}/{len(self.playback_packets)})"
        elif bkey == "toggle_raw_log":
            is_on = self.engine.toggle_raw_packet_logging()
            st = "ENABLED" if is_on else "DISABLED"
            self.status_toast = f"Engine Raw Packet Logging: {st} -> '{self.engine.raw_log_filepath}'"

    # -----------------------------------------------------------------
    # Rendering 
    # -----------------------------------------------------------------
    def _track_color(self, el_dict: dict, flash_on: bool) -> Tuple[int, int, int]:
        if el_dict.get("collision_warning"): return COLOR_TRACK_RED if flash_on else COLOR_TRACK_FLASH_DARK_RED
        if el_dict.get("hw_occupied"): return COLOR_TRACK_RED
        if el_dict.get("is_dimmed"): return (110, 115, 125)
        if el_dict.get("inferred_occupied"): return COLOR_TRACK_INFERRED
        if el_dict.get("green_path"): return COLOR_TRACK_GREEN
        return COLOR_TRACK_WHITE

    def _render_cached_text(self, font: pygame.font.Font, text: str, color: Tuple[int, int, int]) -> pygame.Surface:
        cache = getattr(self, "_text_surf_cache", None)
        if cache is None:
            cache = {}
            self._text_surf_cache = cache
        key = (id(font), text, color)
        surf = cache.get(key)
        if surf is None:
            if len(cache) >= 4096:
                cache.clear()
            surf = font.render(text, True, color)
            cache[key] = surf
        return surf

    def _is_point_visible(self, sx: int, sy: int, margin_x: int = 80, margin_y: int = 80) -> bool:
        return (-margin_x <= sx <= self.width + margin_x) and (-margin_y <= sy <= self.height + margin_y)

    def _is_seg_visible(self, s0: Tuple[int, int], s1: Tuple[int, int], margin: int = 80) -> bool:
        x0, y0 = s0
        x1, y1 = s1
        if (x0 < -margin and x1 < -margin) or (x0 > self.width + margin and x1 > self.width + margin):
            return False
        if (y0 < -margin and y1 < -margin) or (y0 > self.height + margin and y1 > self.height + margin):
            return False
        return True

    # -----------------------------------------------------------------
    # Directional Arrowheads for Lined Green Track Paths
    # -----------------------------------------------------------------
    def _draw_green_path_arrowhead(self, cx: float, cy: float, vx: float, vy: float, sz: float = 5.0):
        nx, ny = -vy, vx
        L_arr = sz * 1.5
        W_arr = sz * 0.95
        tip = (int(round(cx + vx * L_arr * 0.6)), int(round(cy + vy * L_arr * 0.6)))
        barb_l = (int(round(cx - vx * L_arr * 0.4 + nx * W_arr)), int(round(cy - vy * L_arr * 0.4 + ny * W_arr)))
        barb_r = (int(round(cx - vx * L_arr * 0.4 - nx * W_arr)), int(round(cy - vy * L_arr * 0.4 - ny * W_arr)))
        notch = (int(round(cx - vx * L_arr * 0.15)), int(round(cy - vy * L_arr * 0.15)))
        pts = [tip, barb_r, notch, barb_l]
        pygame.draw.polygon(self.screen, (235, 255, 240), pts)
        if self.zoom >= 0.7:
            pygame.draw.polygon(self.screen, (10, 40, 20), pts, 1)

    def _draw_track_bar_arrows(self, p1: Tuple[int, int], p2: Tuple[int, int], direction: str):
        if self.zoom < 0.45 or not direction:
            return
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length < 22.0 * self.zoom:
            return
        ux, uy = dx / length, dy / length
        dir_vec = DIR_SCREEN_VECTORS.get(direction.upper(), (1.0, 0.0))
        dot = ux * dir_vec[0] + uy * dir_vec[1]
        if dot > 0.05:
            vx, vy = ux, uy
        elif dot < -0.05:
            vx, vy = -ux, -uy
        else:
            vx, vy = dir_vec[0], dir_vec[1]

        sz = max(3.6, 4.8 * self.zoom)
        if length >= 150.0 * self.zoom:
            t_vals = (0.30, 0.70)
        else:
            t_vals = (0.50,)

        for t in t_vals:
            cx = p1[0] + ux * length * t
            cy = p1[1] + uy * length * t
            if self._is_point_visible(int(cx), int(cy), 20, 20):
                self._draw_green_path_arrowhead(cx, cy, vx, vy, sz)

    def _draw_link_green_arrows(self, p1: Tuple[int, int], p2: Tuple[int, int], direction: str):
        if self.zoom < 0.55 or not direction:
            return
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length < 38.0 * self.zoom:
            return
        ux, uy = dx / length, dy / length
        dir_vec = DIR_SCREEN_VECTORS.get(direction.upper(), (1.0, 0.0))
        dot = ux * dir_vec[0] + uy * dir_vec[1]
        if dot > 0.05:
            vx, vy = ux, uy
        elif dot < -0.05:
            vx, vy = -ux, -uy
        else:
            vx, vy = dir_vec[0], dir_vec[1]

        sz = max(3.6, 4.8 * self.zoom)
        cx = p1[0] + ux * length * 0.5
        cy = p1[1] + uy * length * 0.5
        if self._is_point_visible(int(cx), int(cy), 20, 20):
            self._draw_green_path_arrowhead(cx, cy, vx, vy, sz)

    def _draw_turnout_diamond_arrow(self, sx: int, sy: int, sz: float, direction: str):
        if not direction or self.zoom < 0.5:
            return
        dir_vec = DIR_SCREEN_VECTORS.get(direction.upper(), (1.0, 0.0))
        self._draw_green_path_arrowhead(sx, sy, dir_vec[0], dir_vec[1], sz=sz * 0.65)

    def _draw_octilinear_link(self, p1_log: Tuple[float, float], p2_log: Tuple[float, float], col1: Tuple[int, int, int], col2: Tuple[int, int, int], line_w: int, green_dir: Optional[str] = None):
        s_start = self.logical_to_screen(p1_log[0], p1_log[1])
        s_end = self.logical_to_screen(p2_log[0], p2_log[1])
        if not self._is_seg_visible(s_start, s_end, margin=100):
            return
        poly_log = LayoutTopologyAssistant.compute_45deg_polyline(p1_log, p2_log)
        if len(poly_log) == 2:
            scr_pts = [s_start, s_end]
            if col1 == col2:
                pygame.draw.line(self.screen, col1, s_start, s_end, line_w)
            else:
                mx, my = (s_start[0] + s_end[0]) // 2, (s_start[1] + s_end[1]) // 2
                pygame.draw.line(self.screen, col1, s_start, (mx, my), line_w)
                pygame.draw.line(self.screen, col2, (mx, my), s_end, line_w)
                if self.zoom >= 0.6:
                    pygame.draw.circle(self.screen, col1, (mx, my), max(2, line_w // 2))
        else:
            scr_pts = [s_start, self.logical_to_screen(poly_log[1][0], poly_log[1][1]), s_end]
            pygame.draw.line(self.screen, col1, scr_pts[0], scr_pts[1], line_w)
            if self.zoom >= 0.6:
                pygame.draw.circle(self.screen, col1, scr_pts[1], max(2, line_w // 2))
            pygame.draw.line(self.screen, col2, scr_pts[1], scr_pts[2], line_w)

        if col1 == COLOR_TRACK_GREEN and col2 == COLOR_TRACK_GREEN and green_dir:
            for i in range(len(scr_pts) - 1):
                self._draw_link_green_arrows(scr_pts[i], scr_pts[i + 1], green_dir)

    def _draw_section_separators(self, visible_track_screen_pts: List[Tuple[Tuple[int, int], Tuple[int, int]]]):
        """Draws dim perpendicular markers at visible track ends to show track section boundaries."""
        if self.zoom < 0.55:
            return
        col = (92, 104, 124)
        half = max(5, int(9 * self.zoom))
        use_dots = (self.zoom >= 1.15)
        dot_r = max(1, int(round(1.3 * self.zoom)))
        n_dots = 5
        for s0, s1 in visible_track_screen_pts:
            dx, dy = s1[0] - s0[0], s1[1] - s0[1]
            ln = math.hypot(dx, dy)
            if ln < 2.0:
                continue
            nx, ny = -dy / ln, dx / ln
            for sx, sy in (s0, s1):
                if not self._is_point_visible(sx, sy, 25, 25):
                    continue
                if use_dots:
                    for i in range(n_dots):
                        t = -half + (2.0 * half) * i / (n_dots - 1)
                        pygame.draw.circle(self.screen, col, (int(sx + nx * t), int(sy + ny * t)), dot_r)
                else:
                    pygame.draw.line(
                        self.screen,
                        col,
                        (int(sx - nx * half), int(sy - ny * half)),
                        (int(sx + nx * half), int(sy + ny * half)),
                        1,
                    )

    def draw_layout_state(self, state: dict, flash_on: bool):
        self._train_scroller_row_rects.clear()
        self._train_scroller_del_rects.clear()
        elements = state.get("elements", {})
        drawn_pairs = set()
        line_w = max(2 if self.zoom < 0.45 else 3, int(5 * self.zoom))

        track_endpoints: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {}
        track_screen_pts: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]] = {}
        visible_track_screen_pts: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []
        track_colors: Dict[str, Tuple[int, int, int]] = {}
        tile_buckets: Dict[Tuple[int, int], List[Tuple[str, Tuple[float, float]]]] = {}

        for eid, el in elements.items():
            et = el["elem_type"]
            if et == "track":
                p0, p1 = LayoutTopologyAssistant.get_track_endpoints_logical(el)
                track_endpoints[eid] = (p0, p1)
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                track_screen_pts[eid] = (s0, s1)
                track_colors[eid] = self._track_color(el, flash_on)
                t0 = (int(round(p0[0])), int(round(p0[1])))
                t1 = (int(round(p1[0])), int(round(p1[1])))
                if self._is_point_visible(s0[0], s0[1], 80, 80):
                    tile_buckets.setdefault(t0, []).append((eid, p0))
                if t1 != t0 and self._is_point_visible(s1[0], s1[1], 80, 80):
                    tile_buckets.setdefault(t1, []).append((eid, p1))
            elif et == "turnout":
                track_colors[eid] = self._track_color(el, flash_on)

        # 1. Draw Track Bars & Topological Links
        for eid, el in elements.items():
            etype = el["elem_type"]
            if etype not in ("track", "turnout"): continue
            col1 = track_colors[eid]

            if etype == "track":
                p0, p1 = track_endpoints[eid]
                s0, s1 = track_screen_pts[eid]
                is_vis = self._is_seg_visible(s0, s1, margin=120)
                if is_vis and self._is_seg_visible(s0, s1, margin=60):
                    visible_track_screen_pts.append((s0, s1))
                    pygame.draw.line(self.screen, col1, s0, s1, line_w)
                    if col1 == COLOR_TRACK_GREEN and el.get("green_path_dir"):
                        self._draw_track_bar_arrows(s0, s1, el["green_path_dir"])

                if not is_vis:
                    continue

                nbr_ids = [nid for d_list in el.get("neighbors", {}).values() for nid in d_list]
                for nid in nbr_ids:
                    pair_key = (eid, nid) if eid < nid else (nid, eid)
                    if pair_key in drawn_pairs or nid not in elements: continue
                    nbr = elements[nid]
                    if nbr["elem_type"] == "turnout": continue
                    drawn_pairs.add(pair_key)
                    col2 = track_colors.get(nid) or self._track_color(nbr, flash_on)
                    if nid in track_endpoints:
                        np0, np1 = track_endpoints[nid]
                        best_pair = min(
                            ((a, b) for a in (p0, p1) for b in (np0, np1)),
                            key=lambda ab: math.hypot(ab[0][0] - ab[1][0], ab[0][1] - ab[1][1]),
                        )
                        g_dir = (el.get("green_path_dir") or nbr.get("green_path_dir")) if (col1 == COLOR_TRACK_GREEN and col2 == COLOR_TRACK_GREEN) else None
                        self._draw_octilinear_link(best_pair[0], best_pair[1], col1, col2, line_w, green_dir=g_dir)

            elif etype == "turnout":
                t_log = (float(el["x"]), float(el["y"]))
                tsx, tsy = self.logical_to_screen(t_log[0], t_log[1])
                if not self._is_point_visible(tsx, tsy, margin_x=180, margin_y=180):
                    continue
                common_id = el.get("common_neighbor", "")
                norm_id = el.get("normal_neighbor", "")
                rev_id = el.get("reverse_neighbor", "")
                active_id = norm_id if el.get("state", 0) == 0 else rev_id
                inactive_id = rev_id if el.get("state", 0) == 0 else norm_id

                if inactive_id and inactive_id in elements:
                    inbr = elements[inactive_id]
                    target_pt = (float(inbr["x"]), float(inbr["y"]))
                    if inactive_id in track_endpoints:
                        ip0, ip1 = track_endpoints[inactive_id]
                        target_pt = min((ip0, ip1), key=lambda p: math.hypot(p[0] - t_log[0], p[1] - t_log[1]))
                    self._draw_octilinear_link(t_log, target_pt, COLOR_TURNOUT_TIE, COLOR_TURNOUT_TIE, max(1, line_w - 2))

                for leg_id in (common_id, active_id):
                    if leg_id and leg_id in elements:
                        lnbr = elements[leg_id]
                        leg_col = track_colors.get(leg_id) or self._track_color(lnbr, flash_on)
                        target_pt = (float(lnbr["x"]), float(lnbr["y"]))
                        if leg_id in track_endpoints:
                            lp0, lp1 = track_endpoints[leg_id]
                            target_pt = min((lp0, lp1), key=lambda p: math.hypot(p[0] - t_log[0], p[1] - t_log[1]))
                        g_dir = (el.get("green_path_dir") or lnbr.get("green_path_dir")) if (col1 == COLOR_TRACK_GREEN and leg_col == COLOR_TRACK_GREEN) else None
                        self._draw_octilinear_link(t_log, target_pt, col1, leg_col, line_w, green_dir=g_dir)

        # 1a. Dim track section separators (visible tracks only)
        self._draw_section_separators(visible_track_screen_pts)

        # 1b. Draw TrackJump Line-Break Links to Connected Track/Turnout
        for eid, el in elements.items():
            if el["elem_type"] == "track_jump":
                conn_id = el.get("connected_element", "")
                if conn_id and conn_id in elements:
                    j_log = (float(el["x"]), float(el["y"]))
                    cnbr = elements[conn_id]
                    c_col = track_colors.get(conn_id) or self._track_color(cnbr, flash_on)
                    target_pt = (float(cnbr["x"]), float(cnbr["y"]))
                    if conn_id in track_endpoints:
                        cp0, cp1 = track_endpoints[conn_id]
                        target_pt = min((cp0, cp1), key=lambda p: math.hypot(p[0] - j_log[0], p[1] - j_log[1]))
                    g_dir = cnbr.get("green_path_dir") if c_col == COLOR_TRACK_GREEN else None
                    self._draw_octilinear_link(j_log, target_pt, c_col, c_col, line_w, green_dir=g_dir)

        # 2. Gapless 1-Cell Welding (O(N) via spatial tile buckets)
        for bucket in tile_buckets.values():
            n_b = len(bucket)
            if n_b < 2:
                continue
            for i in range(n_b):
                tid_a, pa = bucket[i]
                col_a = track_colors[tid_a]
                for j in range(i + 1, n_b):
                    tid_b, pb = bucket[j]
                    if tid_a == tid_b:
                        continue
                    pair_key = (tid_a, tid_b) if tid_a < tid_b else (tid_b, tid_a)
                    if pair_key in drawn_pairs:
                        continue
                    drawn_pairs.add(pair_key)
                    col_b = track_colors[tid_b]
                    g_dir = (elements[tid_a].get("green_path_dir") or elements[tid_b].get("green_path_dir")) if (col_a == COLOR_TRACK_GREEN and col_b == COLOR_TRACK_GREEN) else None
                    self._draw_octilinear_link(pa, pb, col_a, col_b, line_w, green_dir=g_dir)

        if self.dragging_track_end:
            _, _, fixed_pt = self.dragging_track_end
            preview_pts = LayoutTopologyAssistant.compute_45deg_polyline(fixed_pt, self.track_end_drag_curr_logical)
            scr_prev = [self.logical_to_screen(px, py) for px, py in preview_pts]
            if len(scr_prev) >= 2:
                pygame.draw.lines(self.screen, COLOR_HANDLE_ACTIVE, False, scr_prev, max(2, line_w - 1))

        # 3. Draw Track Endpoint Handles, Signals & Placed System Lists
        r_sig = max(4 if self.zoom < 0.55 else 6, int(7.5 * self.zoom))
        handle_r = max(3, int(5.5 * self.zoom))
        show_elem_labels = self.show_labels and (self.zoom >= 0.42)
        draw_handles_all = (self.zoom >= 0.55)
        lbl_font_reg = self._get_element_label_font(bold=False) if self.show_labels else self.font_sm
        lbl_font_bold = self._get_element_label_font(bold=True) if self.show_labels else self.font_sm_bold

        for eid, el in elements.items():
            etype = el["elem_type"]
            sx, sy = self.logical_to_screen(el["x"], el["y"])
            is_sel = (eid in self.selected_ids)

            if etype not in SYSTEM_LIST_TYPES and etype not in ("ptc_datetime", "date_time", "datetime"):
                if etype == "track":
                    s0, s1 = track_screen_pts[eid]
                    if not is_sel and not self._is_seg_visible(s0, s1, margin=80):
                        continue
                elif etype == "label":
                    if not is_sel and not self._is_point_visible(sx, sy, margin_x=240, margin_y=60):
                        continue
                else:
                    if not is_sel and not self._is_point_visible(sx, sy, margin_x=80, margin_y=80):
                        continue

            label = el.get("label", "")
            disp_tag = f"{eid} ({label})" if label else eid
            lpos = el.get("label_pos", "top")

            if is_sel and etype not in SYSTEM_LIST_TYPES:
                pygame.draw.circle(self.screen, COLOR_SELECT_RING, (sx, sy), int(15 * self.zoom) + 5, 2)

            if el.get("collision_warning") and flash_on:
                pygame.draw.circle(self.screen, COLOR_TRACK_RED, (sx, sy), int(18 * self.zoom) + 4, 3)
                pygame.draw.circle(self.screen, (255, 190, 40), (sx, sy), int(24 * self.zoom) + 6, 2)

            if etype == "signal":
                base_col_name = el.get("base_color", "GRAY")
                lamp_rgb = LAMP_COLORS.get(base_col_name, LAMP_COLORS["GRAY"])
                if el.get("flashing", False) and not flash_on:
                    lamp_rgb = LAMP_COLORS["BLACK"]
                if self.zoom < 0.55:
                    pygame.draw.circle(self.screen, lamp_rgb, (sx, sy), r_sig)
                else:
                    pygame.draw.circle(self.screen, (18, 22, 28), (sx, sy), r_sig + 2)
                    pygame.draw.circle(self.screen, lamp_rgb, (sx, sy), r_sig)
                    pygame.draw.circle(self.screen, COLOR_SIGNAL_MAST, (sx, sy), r_sig + 2, 1)
                    self._draw_signal_chevron(sx, sy, max(3, r_sig - 2), el.get("direction", "E"))

                if show_elem_labels or el.get("ctc_active") or (is_sel and self.show_labels):
                    pos_tag = el.get("position", "top")[0].upper()
                    disp_str = f"{eid}[{pos_tag}:{el.get('direction','E')}]" if self.show_labels else eid
                    s_surf = self._render_cached_text(lbl_font_reg, disp_str, COLOR_TEXT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, s_surf.get_width(), s_surf.get_height(), lpos, r_sig + 5)
                    self.screen.blit(s_surf, (lbl_x, lbl_y))
                    if el.get("ctc_active"):
                        pygame.draw.rect(self.screen, (0, 240, 255), (lbl_x - 3, lbl_y - 2, s_surf.get_width() + 6, s_surf.get_height() + 4), 1, border_radius=2)

            elif etype == "track":
                if draw_handles_all or is_sel or (self.dragging_track_end is not None and self.dragging_track_end[0] == eid):
                    s0, s1 = track_screen_pts[eid]
                    for end_idx, (esx, esy) in enumerate((s0, s1)):
                        if not is_sel and not self._is_point_visible(esx, esy, 20, 20):
                            continue
                        is_dragging_this = (
                            self.dragging_track_end is not None
                            and self.dragging_track_end[0] == eid
                            and self.dragging_track_end[1] == end_idx
                        )
                        h_col = COLOR_HANDLE_ACTIVE if (is_dragging_this or is_sel) else COLOR_HANDLE
                        pygame.draw.rect(
                            self.screen, h_col,
                            (esx - handle_r, esy - handle_r, handle_r * 2, handle_r * 2),
                            0 if is_dragging_this else 2, border_radius=2,
                        )

                if (show_elem_labels or el.get("ctc_active") or (is_sel and self.show_labels)) and self._is_point_visible(sx, sy, 140, 50):
                    len_mi = el.get("length_miles", 0.5)
                    tk_title = f"{disp_tag} [{len_mi}mi]" if self.show_labels else disp_tag
                    id_surf = self._render_cached_text(lbl_font_bold, tk_title, COLOR_TEXT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, id_surf.get_width(), id_surf.get_height(), lpos, int(15 * self.zoom))
                    self.screen.blit(id_surf, (lbl_x, lbl_y))
                    if el.get("ctc_active"):
                        pygame.draw.rect(self.screen, (0, 240, 255), (lbl_x - 3, lbl_y - 2, id_surf.get_width() + 6, id_surf.get_height() + 4), 1, border_radius=2)

                if el.get("occupied"):
                    is_inferred = el.get("inferred_occupied") and not el.get("hw_occupied")
                    if not (is_inferred and not self.show_inferred_trains):
                        tname = el.get("train_name") or "TRN"
                        spd = el.get("estimated_speed_mph")
                        tdir = el.get("train_direction") or ""
                        is_dimmed = el.get("is_dimmed", False)
                        dim_tag = " (DIMMED)" if is_dimmed else ""
                        spd_txt = f"{spd:.1f}MPH" if spd is not None else ("Unk." if is_inferred else "")
                        badge_txt = f"{tname} {tdir} {spd_txt}{dim_tag}".strip()
                        b_col = (175, 185, 195) if is_dimmed else (240, 248, 255)
                        b_surf = self._render_cached_text(self.font_sm_bold, badge_txt, b_col)
                        pad_w, pad_h = b_surf.get_width() + 12, b_surf.get_height() + 6
                        badge_box = pygame.Surface((pad_w, pad_h), pygame.SRCALPHA)
                        bg_rgba = (35, 45, 60, 130) if is_dimmed else (25, 95, 215, 165)
                        border_rgba = (95, 105, 120, 160) if is_dimmed else (90, 175, 255, 210)
                        pygame.draw.rect(badge_box, bg_rgba, (0, 0, pad_w, pad_h), border_radius=5)
                        pygame.draw.rect(badge_box, border_rgba, (0, 0, pad_w, pad_h), 1, border_radius=5)
                        badge_box.blit(b_surf, (6, 3))

                        badge_pos = "bottom" if lpos == "top" else ("top" if lpos == "bottom" else ("right" if lpos == "left" else "left"))
                        bx, by = self._get_label_rect(sx, sy, pad_w, pad_h, badge_pos, int(12 * self.zoom))
                        self.screen.blit(badge_box, (bx, by))

            elif etype == "turnout":
                sz = max(4 if self.zoom < 0.55 else 6, int(7 * self.zoom))
                pts = [(sx, sy - sz), (sx + sz, sy), (sx, sy + sz), (sx - sz, sy)]
                t_col = track_colors[eid]
                pygame.draw.polygon(self.screen, t_col, pts)
                if t_col == COLOR_TRACK_GREEN and el.get("green_path_dir"):
                    self._draw_turnout_diamond_arrow(sx, sy, sz, el["green_path_dir"])

                if show_elem_labels or el.get("ctc_active") or (is_sel and self.show_labels):
                    align_char = "N" if el.get("state", 0) == 0 else "R"
                    t_title = f"{disp_tag}[{align_char}]" if self.show_labels else disp_tag
                    t_surf = self._render_cached_text(lbl_font_bold, t_title, COLOR_EDIT_ACCENT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, t_surf.get_width(), t_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(t_surf, (lbl_x, lbl_y))
                    if el.get("ctc_active"):
                        pygame.draw.rect(self.screen, (0, 240, 255), (lbl_x - 3, lbl_y - 2, t_surf.get_width() + 6, t_surf.get_height() + 4), 1, border_radius=2)

            elif etype == "crossing":
                gate = el.get("gate_status", "OPEN")
                col = COLOR_CROSSING_DOWN if gate == "DOWN" else COLOR_CROSSING_OPEN
                sz = max(7, int(9 * self.zoom))
                pygame.draw.line(self.screen, col, (sx - sz, sy - sz), (sx + sz, sy + sz), 3)
                pygame.draw.line(self.screen, col, (sx - sz, sy + sz), (sx + sz, sy - sz), 3)

                if show_elem_labels or (is_sel and self.show_labels):
                    c_surf = self._render_cached_text(self.font_sm, f"{disp_tag}[{gate}]", col)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, c_surf.get_width(), c_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(c_surf, (lbl_x, lbl_y))

            elif etype == "base":
                sz = max(7, int(9 * self.zoom))
                pts = [(sx, sy - sz), (sx - sz, sy + sz), (sx + sz, sy + sz)]
                rad_id = el.get("radio_id")
                rad_tag = f" [ID:{rad_id}]" if rad_id is not None else ""

                last_ts = float(el.get("last_packet_ts", 0.0) or 0.0)
                active_col = (195, 155, 255)
                dark_col = (65, 72, 85)
                if last_ts <= 0.0:
                    col = dark_col
                    is_active = False
                    age = 999.0
                else:
                    age = max(0.0, time.time() - last_ts)
                    if age < 3.5:
                        col = active_col
                        is_active = True
                    elif age >= 15.0:
                        col = dark_col
                        is_active = False
                    else:
                        fade = min(1.0, max(0.0, (age - 3.5) / (15.0 - 3.5)))
                        col = (
                            int(round(active_col[0] * (1.0 - fade) + dark_col[0] * fade)),
                            int(round(active_col[1] * (1.0 - fade) + dark_col[1] * fade)),
                            int(round(active_col[2] * (1.0 - fade) + dark_col[2] * fade)),
                        )
                        is_active = (fade < 0.45)

                line_th = 3 if (is_active and age < 2.0) else 2
                pygame.draw.polygon(self.screen, col, pts, line_th)
                pygame.draw.circle(self.screen, col, (sx, sy - sz - 3), 3, 0 if is_active else 1)
                if is_active and age < 2.0:
                    pygame.draw.circle(self.screen, col, (sx, sy - sz - 3), max(5, int(6.5 * self.zoom)), 1)

                if show_elem_labels or (is_sel and self.show_labels):
                    b_surf = self._render_cached_text(self.font_sm, f"{disp_tag}{rad_tag}(Ch{el.get('channel')})", col)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, b_surf.get_width(), b_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(b_surf, (lbl_x, lbl_y))

            elif etype == "station":
                w, h = max(14, int(18 * self.zoom)), max(10, int(12 * self.zoom))
                pygame.draw.rect(self.screen, COLOR_LANDMARK, (sx - w // 2, sy - h // 2, w, h), border_radius=3)

                if show_elem_labels or (is_sel and self.show_labels):
                    s_surf = self._render_cached_text(self.font_sm, disp_tag, COLOR_LANDMARK)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, s_surf.get_width(), s_surf.get_height(), lpos, h // 2 + 5)
                    self.screen.blit(s_surf, (lbl_x, lbl_y))

            elif etype == "bridge":
                w, h = max(18, int(24 * self.zoom)), max(10, int(14 * self.zoom))
                pygame.draw.line(self.screen, COLOR_LANDMARK, (sx - w // 2, sy - h // 2), (sx + w // 2, sy - h // 2), 2)
                pygame.draw.line(self.screen, COLOR_LANDMARK, (sx - w // 2, sy + h // 2), (sx + w // 2, sy + h // 2), 2)

                if show_elem_labels or (is_sel and self.show_labels):
                    b_surf = self._render_cached_text(self.font_sm, disp_tag, COLOR_TEXT_DIM)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, b_surf.get_width(), b_surf.get_height(), lpos, h // 2 + 5)
                    self.screen.blit(b_surf, (lbl_x, lbl_y))

            elif etype == "diamond":
                sz = max(8, int(11 * self.zoom))
                pts = [(sx, sy - sz), (sx + sz, sy), (sx, sy + sz), (sx - sz, sy)]
                pygame.draw.polygon(self.screen, COLOR_LANDMARK, pts, 2)

                if show_elem_labels or (is_sel and self.show_labels):
                    d_surf = self._render_cached_text(self.font_sm, disp_tag, COLOR_TEXT_DIM)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, d_surf.get_width(), d_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(d_surf, (lbl_x, lbl_y))

            elif etype == "label":
                if self.show_labels:
                    l_font = self.font_sm if self.zoom < 0.65 else self.font_lg
                    l_surf = self._render_cached_text(l_font, label or eid, COLOR_LANDMARK)
                    self.screen.blit(l_surf, (sx - l_surf.get_width() // 2, sy))

            elif etype == "track_jump":
                letter = (el.get("letter") or "A").upper()
                paired_id = el.get("paired_jump_id") or ""
                rad = max(11, int(13 * self.zoom))
                pygame.draw.circle(self.screen, (26, 38, 58), (sx, sy), rad)
                pygame.draw.circle(self.screen, COLOR_EDIT_ACCENT, (sx, sy), rad, 2)
                ltr_surf = self._render_cached_text(self.font_md, letter, (255, 230, 90))
                self.screen.blit(ltr_surf, (sx - ltr_surf.get_width() // 2, sy - ltr_surf.get_height() // 2))
                if show_elem_labels or (is_sel and self.show_labels):
                    pair_tag = f"->{paired_id}" if paired_id else ""
                    j_surf = self._render_cached_text(self.font_sm, f"{eid}[{letter}{pair_tag}]", COLOR_EDIT_ACCENT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, j_surf.get_width(), j_surf.get_height(), lpos, rad + 4)
                    self.screen.blit(j_surf, (lbl_x, lbl_y))

            elif etype in ("ptc_datetime", "date_time", "datetime"):
                def_w = el.get("width") or 240
                def_h = el.get("height") or 50
                w, h = max(120, int(def_w * self.zoom)), max(36, int(def_h * self.zoom))
                clk_surf = pygame.Surface((w, h), pygame.SRCALPHA)
                pygame.draw.rect(clk_surf, (22, 28, 38, 220), (0, 0, w, h), border_radius=6)
                pygame.draw.rect(clk_surf, (45, 145, 235), (0, 0, w, h), 2, border_radius=6)
                title_txt = f"PTC CLOCK - {disp_tag}" if label else f"PTC DATE/TIME [{eid}]"
                hdr_surf = self.font_sm.render(title_txt, True, (190, 215, 245))
                clk_surf.blit(hdr_surf, (8, 4))
                dt_str = el.get("datetime_str") or el.get("last_datetime_str") or "--- --:--:-- UTC"
                val_surf = self.font_md.render(dt_str, True, (100, 240, 255))
                clk_surf.blit(val_surf, (8, max(4, h - val_surf.get_height() - 5)))
                self.screen.blit(clk_surf, (sx, sy))
                if eid in self.selected_ids:
                    pygame.draw.rect(self.screen, COLOR_SELECT_RING, (sx - 2, sy - 2, w + 4, h + 4), 2, border_radius=8)

            elif etype in SYSTEM_LIST_TYPES:
                if etype == "train_scroller":
                    def_w = el.get("width") or 430
                    def_h = el.get("height") or 150
                    w, h = max(380, int(def_w * self.zoom)), max(130, int(def_h * self.zoom))
                    panel = pygame.Surface((w, h), pygame.SRCALPHA)
                    panel.fill(COLOR_HUD_BG)
                    bd_col = COLOR_SELECT_RING if eid in self.selected_ids else COLOR_HUD_BORDER
                    pygame.draw.rect(panel, bd_col, (0, 0, w, h), 2, border_radius=6)
                    
                    trains = list(self.engine.train_scroller.trains.values())
                    hdr = f"[TRAINS] {disp_tag} ({len(trains)} active)"
                    panel.blit(self.font_md.render(hdr, True, COLOR_CROSSING_OPEN), (10, 8))
                    
                    col_hdr = f"{'STATUS':<10} {'TRAIN / NAME':<17} {'SPEED':<10} {'DIR':<5} {'RX':<6} {'LOCATION'}"
                    panel.blit(self.font_sm_bold.render(col_hdr, True, (140, 180, 220)), (10, 28))
                    pygame.draw.line(panel, (55, 75, 105), (10, 43), (w - 10, 43), 1)

                    if not trains:
                        panel.blit(self.font_sm.render("No trains currently tracked. Simulate by lining paths or toggling signals.", True, COLOR_TEXT_DIM), (10, 52))
                    else:
                        max_r = int(el.get("extra_info") or 10)
                        for drawn_idx, trn in enumerate(trains[:max_r]):
                            row_y = 46 + drawn_idx * 19
                            if row_y + 18 > h - 4:
                                break
                            st = trn.get("status", "Confirmed")
                            is_dimmed = trn.get("is_dimmed", False)
                            disp_name = trn.get("assigned_name") or trn.get("train_name", "TRN")
                            if trn.get("assigned_id"):
                                disp_name = f"{disp_name} (#{trn['assigned_id']})"
                            spd_txt = trn.get("speed_str") or (f"{trn.get('speed_mph', 0.0):.1f} MPH" if trn.get("speed_mph") is not None else "Unk.")
                            dir_txt = str(trn.get("direction", "E"))
                            is_rx = trn.get("is_receiving_packets", False)
                            rx_txt = "● RX" if is_rx else "---"
                            loc_txt = str(trn.get("last_location", "") or trn.get("current_element_id", ""))

                            # Row interaction
                            row_screen_rect = pygame.Rect(sx + 10, sy + row_y, w - 38, 18)
                            self._train_scroller_row_rects.append((row_screen_rect, trn))

                            # Delete train [x] button
                            del_rect = pygame.Rect(sx + w - 24, sy + row_y + 1, 16, 16)
                            tkey = trn.get("train_key") or trn.get("train_name")
                            self._train_scroller_del_rects.append((del_rect, tkey))
                            m_pos = pygame.mouse.get_pos()
                            is_del_h = del_rect.collidepoint(m_pos)
                            del_c = (255, 75, 75) if is_del_h else (160, 80, 80)
                            del_surf = self.font_sm_bold.render("x", True, del_c)
                            panel.blit(del_surf, (w - 22, row_y + 1))

                            # Highlight if row hovered
                            if row_screen_rect.collidepoint(m_pos) and not is_del_h:
                                row_hl = pygame.Surface((w - 38, 18), pygame.SRCALPHA)
                                row_hl.fill((35, 75, 125, 160))
                                panel.blit(row_hl, (10, row_y))
                                pygame.draw.rect(panel, (80, 200, 255, 230), (10, row_y, w - 38, 18), 1, border_radius=3)

                            if is_dimmed:
                                col = (130, 135, 145)
                                st_col = (120, 125, 135)
                            elif st == "Potential":
                                col = (255, 215, 130)
                                st_col = (255, 185, 40)
                            elif st == "Inferred":
                                col = (160, 220, 255)
                                st_col = (75, 205, 255)
                            else:
                                col = (180, 255, 190)
                                st_col = (90, 255, 130)

                            st_badge = f"{st[:9]:<10}"
                            panel.blit(self.font_sm_bold.render(st_badge, True, st_col), (10, row_y))
                            rem_row = f"{disp_name[:16]:<17} {spd_txt:<10} {dir_txt:<5} "
                            panel.blit(self.font_sm.render(rem_row, True, col), (85, row_y))
                            rx_col = (0, 255, 130) if is_rx else (110, 115, 125)
                            panel.blit(self.font_sm_bold.render(f"{rx_txt:<6}", True, rx_col), (290, row_y))
                            panel.blit(self.font_sm.render(loc_txt[:18], True, col), (340, row_y))

                    self.screen.blit(panel, (sx, sy))
                elif etype == "chat":
                    def_w = el.get("width") or 460
                    def_h = el.get("height") or 220
                    w, h = max(320, int(def_w * self.zoom)), max(130, int(def_h * self.zoom))
                    panel = pygame.Surface((w, h), pygame.SRCALPHA)
                    panel.fill(COLOR_HUD_BG)
                    bd_col = COLOR_SELECT_RING if eid in self.selected_ids else COLOR_HUD_BORDER
                    pygame.draw.rect(panel, bd_col, (0, 0, w, h), 2, border_radius=6)
                    hdr = f"[CHAT] {disp_tag} (Rows: {el.get('extra_info') or 12} | {def_w}x{def_h}px)"
                    panel.blit(self.font_md.render(hdr, True, (90, 225, 255)), (10, 6))
                    ctrl_preview = "[Mute] [Filter:RegEx] [Excl:RegEx] [ID/Names] [PgUp] [PgDn] [Clear]"
                    panel.blit(self.font_sm.render(ctrl_preview, True, (150, 185, 225)), (10, 26))
                    pygame.draw.line(panel, (55, 75, 105), (10, 42), (w - 10, 42), 1)
                    panel.blit(self.font_sm.render("[12:00:00] Dispatcher: Encrypted Banc cloud chat ready (@<identity> supported)", True, COLOR_TEXT_DIM), (10, 48))
                    inp_h = 24
                    inp_y = max(68, h - inp_h - 6)
                    pygame.draw.rect(panel, (12, 16, 24), (10, inp_y, max(80, w - 72), inp_h), border_radius=4)
                    pygame.draw.rect(panel, (60, 90, 130), (10, inp_y, max(80, w - 72), inp_h), 1, border_radius=4)
                    panel.blit(self.font_sm.render("Type message or @<identity>... (B/I/U/Color)", True, (110, 130, 155)), (16, inp_y + 4))
                    pygame.draw.rect(panel, (35, 110, 85), (w - 56, inp_y, 46, inp_h), border_radius=4)
                    panel.blit(self.font_sm_bold.render("Send", True, (255, 255, 255)), (w - 48, inp_y + 4))
                    self.screen.blit(panel, (sx, sy))
                else:
                    def_w = el.get("width") or (580 if etype == "packet_scroller" else 450)
                    def_h = el.get("height") or (195 if etype == "packet_scroller" else 150)
                    w, h = int(def_w * self.zoom), int(def_h * self.zoom)
                    panel = pygame.Surface((max(180, w), max(60, h)), pygame.SRCALPHA)
                    panel.fill(COLOR_HUD_BG)
                    bd_col = COLOR_SELECT_RING if eid in self.selected_ids else COLOR_HUD_BORDER
                    pygame.draw.rect(panel, bd_col, (0, 0, panel.get_width(), panel.get_height()), 2, border_radius=6)
                    hdr = f"[{etype.upper()}] {disp_tag} (Rows: {el.get('extra_info') or 10} | Default: {def_w}x{def_h}px)"
                    panel.blit(self.font_md.render(hdr, True, COLOR_CROSSING_OPEN), (10, 8))
                    if etype == "packet_scroller":
                        panel.blit(self.font_sm.render("Stats: Elapsed | Total Pkts & Pkts/min | Per-Channel Pkts & Pkts/min", True, COLOR_LANDMARK), (10, 32))
                        panel.blit(self.font_sm.render("Optional Telemetry List Panel (Double-click to configure size & rows)", True, COLOR_TEXT_DIM), (10, 52))
                    else:
                        panel.blit(self.font_sm.render("Optional Telemetry List Panel (Double-click to configure size & rows)", True, COLOR_TEXT_DIM), (10, 34))
                    self.screen.blit(panel, (sx, sy))

        if self.box_selecting:
            x1, y1 = self.box_start_screen
            x2, y2 = self.box_curr_screen
            rect = pygame.Rect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            pygame.draw.rect(self.screen, COLOR_SELECT_RING, rect, 1)

    def _draw_signal_chevron(self, cx: int, cy: int, sz: int, direction: str):
        d = direction.upper()
        if d in ("E", "NE", "SE"):
            pts = [(cx - sz // 2, cy - sz), (cx + sz, cy), (cx - sz // 2, cy + sz)]
        elif d in ("W", "NW", "SW"):
            pts = [(cx + sz // 2, cy - sz), (cx - sz, cy), (cx + sz // 2, cy + sz)]
        elif d == "N":
            pts = [(cx - sz, cy + sz // 2), (cx, cy - sz), (cx + sz, cy + sz // 2)]
        else:
            pts = [(cx - sz, cy - sz // 2), (cx, cy + sz), (cx + sz, cy - sz // 2)]
        pygame.draw.lines(self.screen, (15, 18, 22), False, pts, 2)

    def draw_hud(self):
        self.hud_buttons.clear()

        group1 = [
            ("toggle_sim_select", "Sim/Crank" if self.active_tool == "sim_crank" else "Select"),
            ("crank", "Crank(K)"),
            ("verify", "Verify(V)"),
        ]
        if self.active_tool == "sim_crank":
            group1.append(("all_signals_stop", "All Stop(S)"))

        button_groups = [
            # Group 1: Mode & Verification
            group1,
            # Group 2: Layout Infrastructure
            [
                ("open_big_map", "+Map(B)"),
                ("set_map_radius", f"MapRad:{int(self.map_view_radius_miles) if self.map_view_radius_miles.is_integer() else self.map_view_radius_miles:g}mi"),
                ("tool_track", "+Track(1)"),
                ("tool_turnout", "+Turnout(2)"),
                ("tool_signal", "+Signal(3)"),
                ("tool_crossing", "+Xing(4)"),
                ("tool_base", "+Base(5)"),
                ("tool_station", "+Stn(6)"),
                ("tool_bridge", "+Brg(7)"),
                ("tool_diamond", "+Dmd(8)"),
                ("tool_label", "+Lbl(9)"),
                ("tool_track_jump", "+Break(0)"),
            ],
            # Group 3: Telemetry Panels
            [
                ("tool_packet_scroller", "+PktList"),
                ("tool_address_table", "+WIUList"),
                ("tool_train_scroller", "+TrnList"),
                ("tool_chat", "+Chat"),
                ("tool_ptc_datetime", "+PTC-Clock"),
            ],
            # Group 4: Edit & Clipboard
            [
                ("undo", "Undo"),
                ("redo", "Redo"),
                ("copy", "Copy"),
                ("paste", "Paste"),
                ("split", "Split(X)"),
                ("rotate", "Rot(R)"),
                ("delete", "Del"),
            ],
            # Group 5: File Operations & Playback
            [
                ("new", "New"),
                ("load", "Load"),
                ("import_itcvl", "Import ITCVL"),
                ("import_atcs", "Import from ATCS"),
                ("merge", "Merge(M)"),
                ("save", "Save*" if self.dirty else "Save"),
                ("save_as", "SaveAs"),
                ("save_frag", "SaveFrag"),
                ("load_frag", "LoadFrag"),
                ("banc_layouts", "CloudLayouts"),
                ("banc_upload", "CloudUpload"),
                ("load_log", f"PlayLog({self.playback_idx}/{len(self.playback_packets)})" if self.playback_packets else "LoadLog"),
            ],
            # Group 6: View & Logic Toggles
            [
                ("toggle_labels", "Lbls(On)" if self.show_labels else "Lbls(Off)"),
                ("toggle_inferred", "InfTrns(On)" if self.show_inferred_trains else "InfTrns(Off)"),
                ("toggle_infer_logic", "InferLogic(On)" if self.enable_inference_logic else "InferLogic(Off)"),
                ("toggle_raw_log", "● REC LOG" if (getattr(self.engine, "raw_packet_logging", False) and (int(time.time() * 2) % 2 == 0)) else ("REC LOG" if getattr(self.engine, "raw_packet_logging", False) else "LogOff")),
            ],
        ]

        margin_x = 6
        pad_top = 5
        pad_bottom = 5
        btn_h = 24
        btn_gap = 3
        group_gap = 12
        row_gap = 4
        max_w = self.width - margin_x

        positioned_buttons = []
        separators = []
        row_idx = 0
        x_cursor = margin_x

        for group in button_groups:
            g_items = []
            for bkey, label in group:
                tw, th = self.font_sm_bold.size(label)
                bw = tw + 4

                if bkey == "toggle_sim_select":
                    if self.active_tool == "sim_crank":
                        bg = (215, 125, 25)
                    elif self.active_tool == "select":
                        bg = (45, 95, 140)
                    else:
                        bg = (36, 46, 62)
                elif bkey == f"tool_{self.active_tool}":
                    bg = (215, 125, 25)
                elif bkey == "verify":
                    bg = (215, 125, 25) if self.show_verification_panel else (35, 110, 85)
                elif bkey == "crank":
                    bg = (35, 110, 85)
                elif bkey == "all_signals_stop":
                    bg = (195, 45, 45)
                elif bkey == "save" and self.dirty:
                    bg = (195, 85, 35)
                elif bkey == "split":
                    bg = (175, 95, 30)
                elif bkey == "open_big_map":
                    bg = (38, 90, 68)
                elif bkey == "set_map_radius":
                    bg = (42, 70, 105)
                elif bkey in ("save", "save_as", "load", "merge"):
                    bg = (48, 72, 120)
                elif bkey in ("import_itcvl", "import_atcs"):
                    bg = (52, 95, 130)
                elif bkey == "new":
                    bg = (38, 95, 110)
                elif bkey == "save_frag":
                    bg = (45, 95, 125)
                elif bkey == "load_frag":
                    bg = (35, 110, 115)
                elif bkey == "banc_layouts":
                    bg = (32, 115, 125)
                elif bkey == "banc_upload":
                    bg = (48, 85, 145)
                elif bkey == "load_log":
                    bg = (35, 125, 75) if self.playback_active else ((45, 95, 140) if self.playback_packets else (55, 65, 85))
                elif bkey == "toggle_raw_log":
                    is_log_on = getattr(self.engine, "raw_packet_logging", False)
                    flash_log = (int(time.time() * 2) % 2) == 0
                    bg = (195, 35, 35) if (is_log_on and flash_log) else ((120, 25, 25) if is_log_on else (45, 52, 68))
                elif bkey in ("tool_packet_scroller", "tool_address_table", "tool_train_scroller", "tool_chat"):
                    bg = (70, 52, 115)
                elif bkey in ("toggle_labels", "toggle_inferred", "toggle_infer_logic"):
                    bg = (45, 105, 155) if (
                        (self.show_labels and bkey == "toggle_labels") or
                        (self.show_inferred_trains and bkey == "toggle_inferred") or
                        (self.enable_inference_logic and bkey == "toggle_infer_logic")
                    ) else (45, 60, 85)
                else:
                    bg = (36, 46, 62)

                g_items.append((bkey, label, bw, tw, th, bg))

            group_w = sum(it[2] for it in g_items) + (len(g_items) - 1) * btn_gap

            if x_cursor > margin_x:
                if x_cursor + group_gap + group_w <= max_w:
                    y_pos = pad_top + row_idx * (btn_h + row_gap)
                    separators.append((x_cursor + group_gap // 2, y_pos))
                    x_cursor += group_gap
                else:
                    row_idx += 1
                    x_cursor = margin_x

            for bkey, label, bw, tw, th, bg in g_items:
                if x_cursor > margin_x and x_cursor + bw > max_w:
                    row_idx += 1
                    x_cursor = margin_x

                y_pos = pad_top + row_idx * (btn_h + row_gap)
                rect = pygame.Rect(x_cursor, y_pos, bw, btn_h)
                positioned_buttons.append((bkey, label, rect, tw, th, bg))
                x_cursor += bw + btn_gap

        num_rows = row_idx + 1
        bar_h = pad_top + num_rows * btn_h + (num_rows - 1) * row_gap + pad_bottom
        self.hud_bar_height = bar_h

        hud = pygame.Surface((self.width, bar_h), pygame.SRCALPHA)
        hud.fill((18, 22, 32, 244))
        pygame.draw.line(hud, COLOR_HUD_BORDER, (0, bar_h - 1), (self.width, bar_h - 1), 2)
        self.screen.blit(hud, (0, 0))

        for sep_x, sep_y in separators:
            pygame.draw.line(self.screen, (55, 68, 90), (sep_x, sep_y + 3), (sep_x, sep_y + btn_h - 3), 1)

        for bkey, label, rect, tw, th, bg in positioned_buttons:
            self.hud_buttons[bkey] = rect
            pygame.draw.rect(self.screen, bg, rect, border_radius=2)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, rect, 1, border_radius=2)
            ts = self.font_sm_bold.render(label, True, (255, 255, 255))
            self.screen.blit(ts, (rect.x + 2, rect.y + (btn_h - th) // 2))

        if self.show_verification_panel:
            hdr_txt = (
                f"AUTOMATIC LAYOUT VERIFICATION REPORT ({CREATOR_VERSION}) — "
                f"{'0 ERRORS (ALL REACHABLE & SIGNALED)' if not self.verification_issues else f'{len(self.verification_issues)} ISSUE(S) FOUND'} "
                f"[Press Esc to Close, Ctrl+S to Save Anyway]"
            )
            ok_msg = "Every Track & Turnout has a controlling Signal, no duplicate controllers exist, and 100% of tracks are reachable."
            max_msg_w = self.font_md.size(hdr_txt)[0] + 32
            if not self.verification_issues:
                max_msg_w = max(max_msg_w, self.font_sm.size(ok_msg)[0] + 32)
            else:
                for sev, msg in self.verification_issues[:18]:
                    max_msg_w = max(max_msg_w, self.font_sm.size(f"[{sev}] {msg}")[0] + 32)

            vw = min(self.width - 24, max(620, max_msg_w))
            vy = self.hud_bar_height + 10
            vh = min(self.height - vy - 20, 75 + max(1, min(18, len(self.verification_issues))) * 22)
            vx = max(12, (self.width - vw) // 2)

            vsurf = pygame.Surface((vw, vh), pygame.SRCALPHA)
            vsurf.fill((16, 22, 32, 248))
            border_c = COLOR_OK_GREEN if not self.verification_issues else COLOR_ERROR_RED
            pygame.draw.rect(vsurf, border_c, (0, 0, vw, vh), 2, border_radius=6)
            self.screen.blit(vsurf, (vx, vy))

            self.screen.blit(self.font_md.render(hdr_txt, True, border_c), (vx + 14, vy + 10))
            if not self.verification_issues:
                self.screen.blit(self.font_sm.render(ok_msg, True, COLOR_OK_GREEN), (vx + 14, vy + 40))
            else:
                for idx, (sev, msg) in enumerate(self.verification_issues[:18]):
                    c = COLOR_ERROR_RED if sev == "ERROR" else COLOR_EDIT_ACCENT
                    self.screen.blit(self.font_sm.render(f"[{sev}] {msg}", True, c), (vx + 14, vy + 38 + idx * 22))

        if self.confirm_exit_modal:
            mw, mh = 540, 150
            mx_pos = (self.width - mw) // 2
            my_pos = (self.height - mh) // 2
            msurf = pygame.Surface((mw, mh), pygame.SRCALPHA)
            msurf.fill((22, 28, 42, 250))
            pygame.draw.rect(msurf, COLOR_EDIT_ACCENT, (0, 0, mw, mh), 2, border_radius=8)
            self.screen.blit(msurf, (mx_pos, my_pos))

            t1 = self.font_lg.render("UNSAVED CHANGES DETECTED", True, COLOR_EDIT_ACCENT)
            disp_layout = os.path.basename(self.layout_filepath) if self.layout_filepath else "[Untitled]"
            t2 = self.font_sm.render(f"Layout '{disp_layout}' has unsaved changes.", True, COLOR_TEXT)
            self.screen.blit(t1, (mx_pos + (mw - t1.get_width()) // 2, my_pos + 18))
            self.screen.blit(t2, (mx_pos + (mw - t2.get_width()) // 2, my_pos + 52))

            self.exit_modal_buttons.clear()
            ebtns = [
                ("save_exit", "Save & Exit (S)", mx_pos + 30, 150, (35, 130, 65)),
                ("discard_exit", "Discard & Exit (D)", mx_pos + 195, 165, (165, 50, 50)),
                ("cancel_exit", "Cancel (Esc)", mx_pos + 375, 135, (60, 75, 95)),
            ]
            for bkey, btxt, bx, bw, bcol in ebtns:
                r = pygame.Rect(bx, my_pos + 94, bw, 34)
                self.exit_modal_buttons[bkey] = r
                pygame.draw.rect(self.screen, bcol, r, border_radius=5)
                pygame.draw.rect(self.screen, COLOR_HUD_BORDER, r, 1, border_radius=5)
                ts = self.font_sm_bold.render(btxt, True, (255, 255, 255))
                self.screen.blit(ts, (bx + (bw - ts.get_width()) // 2, my_pos + 104))

        if self.confirm_new_modal:
            mw, mh = 560, 150
            mx_pos = (self.width - mw) // 2
            my_pos = (self.height - mh) // 2
            msurf = pygame.Surface((mw, mh), pygame.SRCALPHA)
            msurf.fill((22, 28, 42, 250))
            pygame.draw.rect(msurf, COLOR_EDIT_ACCENT, (0, 0, mw, mh), 2, border_radius=8)
            self.screen.blit(msurf, (mx_pos, my_pos))

            t1 = self.font_lg.render("UNSAVED CHANGES DETECTED", True, COLOR_EDIT_ACCENT)
            disp_layout = os.path.basename(self.layout_filepath) if self.layout_filepath else "[Untitled]"
            t2 = self.font_sm.render(f"Save pending changes in '{disp_layout}' before creating a new layout?", True, COLOR_TEXT)
            self.screen.blit(t1, (mx_pos + (mw - t1.get_width()) // 2, my_pos + 18))
            self.screen.blit(t2, (mx_pos + (mw - t2.get_width()) // 2, my_pos + 52))

            self.new_modal_buttons.clear()
            nbtns = [
                ("save_new", "Save Changes (S)", mx_pos + 30, 160, (35, 130, 65)),
                ("discard_new", "Discard & New (D)", mx_pos + 205, 175, (165, 50, 50)),
                ("cancel_new", "Cancel (Esc)", mx_pos + 395, 135, (60, 75, 95)),
            ]
            for bkey, btxt, bx, bw, bcol in nbtns:
                r = pygame.Rect(bx, my_pos + 94, bw, 34)
                self.new_modal_buttons[bkey] = r
                pygame.draw.rect(self.screen, bcol, r, border_radius=5)
                pygame.draw.rect(self.screen, COLOR_HUD_BORDER, r, 1, border_radius=5)
                ts = self.font_sm_bold.render(btxt, True, (255, 255, 255))
                self.screen.blit(ts, (bx + (bw - ts.get_width()) // 2, my_pos + 104))

        if self.confirm_load_modal:
            mw, mh = 560, 150
            mx_pos = (self.width - mw) // 2
            my_pos = (self.height - mh) // 2
            msurf = pygame.Surface((mw, mh), pygame.SRCALPHA)
            msurf.fill((22, 28, 42, 250))
            pygame.draw.rect(msurf, COLOR_EDIT_ACCENT, (0, 0, mw, mh), 2, border_radius=8)
            self.screen.blit(msurf, (mx_pos, my_pos))

            t1 = self.font_lg.render("UNSAVED CHANGES DETECTED", True, COLOR_EDIT_ACCENT)
            disp_layout = os.path.basename(self.layout_filepath) if self.layout_filepath else "[Untitled]"
            t2 = self.font_sm.render(f"Save pending changes in '{disp_layout}' before loading layout?", True, COLOR_TEXT)
            self.screen.blit(t1, (mx_pos + (mw - t1.get_width()) // 2, my_pos + 18))
            self.screen.blit(t2, (mx_pos + (mw - t2.get_width()) // 2, my_pos + 52))

            self.load_modal_buttons.clear()
            lbtns = [
                ("save_load", "Save Changes (S)", mx_pos + 30, 160, (35, 130, 65)),
                ("discard_load", "Discard (D)", mx_pos + 205, 150, (165, 50, 50)),
                ("cancel_load", "Cancel (Esc)", mx_pos + 370, 135, (60, 75, 95)),
            ]
            for bkey, btxt, bx, bw, bcol in lbtns:
                r = pygame.Rect(bx, my_pos + 94, bw, 34)
                self.load_modal_buttons[bkey] = r
                pygame.draw.rect(self.screen, bcol, r, border_radius=5)
                pygame.draw.rect(self.screen, COLOR_HUD_BORDER, r, 1, border_radius=5)
                ts = self.font_sm_bold.render(btxt, True, (255, 255, 255))
                self.screen.blit(ts, (bx + (bw - ts.get_width()) // 2, my_pos + 104))

        footer_h = 34

        # Sync autocompletion suggestions if mode changed
        if self.file_prompt_mode and getattr(self, "_last_suggest_mode", None) != self.file_prompt_mode:
            self._last_suggest_mode = self.file_prompt_mode
            self._update_file_prompt_suggestions()
        elif not self.file_prompt_mode:
            self._last_suggest_mode = None

        # Draw suggested completion list popup if file prompt has suggestions
        if self.file_prompt_mode and getattr(self, "file_prompt_suggestions", None):
            suggestions = self.file_prompt_suggestions
            row_h = 24
            hdr_h = 22
            pop_h = hdr_h + len(suggestions) * row_h + 8

            max_txt_w = max(self.font_sm.size(c)[0] for c in suggestions) if suggestions else 200
            pop_w = max(440, min(self.width - 28, max_txt_w + 90))
            pop_x = 14
            pop_y = (self.height - footer_h) - pop_h - 4

            pop_surf = pygame.Surface((pop_w, pop_h), pygame.SRCALPHA)
            pop_surf.fill((18, 24, 38, 248))
            pygame.draw.rect(pop_surf, COLOR_EDIT_ACCENT, (0, 0, pop_w, pop_h), 1, border_radius=6)
            self.screen.blit(pop_surf, (pop_x, pop_y))

            hdr_lbl = self.font_sm.render("SUGGESTIONS  (Tab = Complete,  \u2191\u2193 = Select,  Click to choose)", True, (130, 175, 220))
            self.screen.blit(hdr_lbl, (pop_x + 10, pop_y + 4))
            pygame.draw.line(self.screen, (40, 65, 95), (pop_x + 6, pop_y + hdr_h), (pop_x + pop_w - 6, pop_y + hdr_h), 1)

            mx_pos, my_pos = pygame.mouse.get_pos()
            self.file_prompt_suggestion_rects.clear()

            for idx, cand in enumerate(suggestions):
                ry = pop_y + hdr_h + 4 + idx * row_h
                r = pygame.Rect(pop_x + 4, ry, pop_w - 8, row_h - 2)
                self.file_prompt_suggestion_rects.append((r, cand))

                is_sel = (idx == self.file_prompt_selected_idx)
                is_hov = r.collidepoint(mx_pos, my_pos)

                if is_sel:
                    pygame.draw.rect(self.screen, (35, 80, 135), r, border_radius=4)
                    pygame.draw.rect(self.screen, COLOR_EDIT_ACCENT, r, 1, border_radius=4)
                elif is_hov:
                    pygame.draw.rect(self.screen, (28, 48, 75), r, border_radius=4)

                is_dir = cand.endswith("/") or cand.endswith("\\")
                prefix_tag = "DIR " if is_dir else "FILE"
                tag_color = (230, 180, 60) if is_dir else (100, 180, 240)
                tag_surf = self.font_sm_bold.render(f"[{prefix_tag}]", True, tag_color)
                self.screen.blit(tag_surf, (r.x + 8, r.y + 3))

                txt_color = (255, 255, 255) if is_sel else ((220, 235, 255) if is_hov else COLOR_TEXT)
                item_surf = self.font_sm.render(cand, True, txt_color)
                self.screen.blit(item_surf, (r.x + 60, r.y + 3))

        foot = pygame.Surface((self.width, footer_h), pygame.SRCALPHA)
        foot.fill((18, 22, 32, 240))
        pygame.draw.line(foot, COLOR_HUD_BORDER, (0, 0), (self.width, 0), 1)
        self.screen.blit(foot, (0, self.height - footer_h))

        if self.file_prompt_mode:
            if self.file_prompt_mode == "set_map_radius":
                prompt = f"ENTER 1X LAYOUT VIEW RADIUS (MILES) > {self.file_prompt_buffer}_   (Enter=Confirm, Esc=Cancel)"
            elif self.file_prompt_mode == "save_frag":
                prompt = f"SAVE FRAGMENT NAME > {self.file_prompt_buffer}_   (Saved to frags/, Enter=Confirm, Esc=Cancel, Ctrl+BS=Clear)"
            elif self.file_prompt_mode == "import_atcs":
                prompt = f"IMPORT ATCS (.lay) FILEPATH > {self.file_prompt_buffer}_   (Enter=Import, Esc=Cancel, Ctrl+BS=Clear)"
            else:
                prompt = f"{self.file_prompt_mode.upper()} FILEPATH > {self.file_prompt_buffer}_   (Enter=Confirm, Esc=Cancel, Ctrl+BS=Clear)"
            self.screen.blit(self.font_md.render(prompt, True, COLOR_EDIT_ACCENT), (14, self.height - 25))
        else:
            mx, my = pygame.mouse.get_pos()
            mlx, mly = self.screen_to_logical(mx, my)
            dirty_tag = " [UNSAVED]" if self.dirty else ""
            disp_layout = os.path.basename(self.layout_filepath) if self.layout_filepath else "[Untitled]"
            info = (
                f"[{CREATOR_VERSION}] {disp_layout}{dirty_tag} | Pos:({int(round(mlx))},{int(round(mly))}) | "
                f"Undo:{len(self.undo_stack)} Redo:{len(self.redo_stack)} | 1xRad:{self.map_view_radius_miles:g}mi | "
                f"LastGPS:({self.last_selected_gps[0]:.4f},{self.last_selected_gps[1]:.4f} R={self.default_radius:.0f}mi) | {self.status_toast}"
            )
            self.screen.blit(self.font_sm.render(info, True, COLOR_TEXT), (14, self.height - 23))

    def _format_element_tooltip(self, el) -> str:
        if el is None:
            return ""
        eid = getattr(el, "id", "")
        etype = getattr(el, "elem_type", "")
        lbl = getattr(el, "label", "")
        wiu_id = getattr(el, "wiu_id", None)
        radio_id = getattr(el, "radio_id", None)
        node_id = getattr(el, "node_id", None)
        channel = getattr(el, "channel", None)

        parts = []
        if eid:
            parts.append(f"ID: {eid}")
        elif etype:
            parts.append(f"Type: {etype}")

        if lbl and str(lbl).strip() and str(lbl).strip() != str(eid).strip():
            parts.append(f"'{lbl.strip()}'")

        if wiu_id is not None and str(wiu_id).strip() != "" and str(wiu_id) != "None":
            parts.append(f"WIU: {wiu_id}")

        if radio_id is not None and str(radio_id).strip() != "" and str(radio_id) != "None":
            parts.append(f"Radio: {radio_id}")
        elif etype == "base" and channel is not None:
            parts.append(f"Radio Ch: {channel}")

        if node_id is not None and str(node_id).strip() != "" and str(node_id) != "None":
            parts.append(f"NodeID: {node_id}")

        avg_rssi = getattr(el, "avg_rssi", getattr(el, "rssi_avg", None))
        if avg_rssi is None and isinstance(el, dict):
            avg_rssi = el.get("avg_rssi", el.get("rssi_avg"))
        last_rssi = getattr(el, "_last_rssi", getattr(el, "last_rssi", None))
        if last_rssi is None and isinstance(el, dict):
            last_rssi = el.get("last_rssi")
        if avg_rssi is not None:
            try:
                parts.append(f"RSSI Avg: {float(avg_rssi):.1f} dBm")
            except (ValueError, TypeError):
                parts.append(f"RSSI Avg: {avg_rssi} dBm")
        elif last_rssi is not None:
            try:
                parts.append(f"RSSI: {float(last_rssi):.1f} dBm")
            except (ValueError, TypeError):
                parts.append(f"RSSI: {last_rssi} dBm")

        return " | ".join(parts)

    def _draw_related_elements_hover(self):
        if not (pygame.key.get_mods() & pygame.KMOD_SHIFT):
            return
        if (self.frag_picker_modal or self.gps_picker_modal or self.inplace_editor or 
            self.confirm_exit_modal or self.confirm_new_modal or self.confirm_load_modal or 
            self.file_prompt_mode or getattr(self, "banc_coverage_modal", None)):
            return
        if (getattr(self, 'box_selecting', False) or 
            getattr(self, 'dragging_selection', False) or 
            (getattr(self, 'dragging_track_end', None) is not None) or
            getattr(self, 'is_panning', False)):
            return

        mx, my = pygame.mouse.get_pos()
        hud_h = getattr(self, "hud_bar_height", 50)
        footer_h = 34
        if my < hud_h or my > self.height - footer_h:
            return

        hov_id = self.pick_element_at_screen(mx, my)
        if not hov_id or hov_id not in self.engine.elements:
            return

        el = self.engine.elements[hov_id]
        etype = getattr(el, "elem_type", "")
        if etype not in ("track", "signal", "turnout"):
            return

        # Collect related elements: (target_id, relation_label, rgb_color)
        raw_related: List[Tuple[str, str, Tuple[int, int, int]]] = []

        if etype == "track":
            # 1. Neighbors (Green)
            nbrs = getattr(el, "neighbors", {}) or {}
            if isinstance(nbrs, dict):
                for d, nlist in nbrs.items():
                    lbl = f"Neighbor ({d})" if d else "Neighbor"
                    if isinstance(nlist, list):
                        for nid in nlist:
                            if nid and nid in self.engine.elements and nid != hov_id:
                                raw_related.append((nid, lbl, (50, 220, 120)))
                    elif isinstance(nlist, str) and nlist:
                        if nlist in self.engine.elements and nlist != hov_id:
                            raw_related.append((nlist, lbl, (50, 220, 120)))
            elif isinstance(nbrs, (list, tuple, set)):
                for nid in nbrs:
                    if nid and nid in self.engine.elements and nid != hov_id:
                        raw_related.append((nid, "Neighbor", (50, 220, 120)))

            # 2. Parallel Track (Gray)
            par_id = getattr(el, "parallel_track_id", None)
            if par_id and par_id in self.engine.elements and par_id != hov_id:
                raw_related.append((par_id, "Parallel Track", (170, 180, 195)))

        elif etype == "signal":
            # 1. Protected element (Green)
            prot_id = getattr(el, "protects_element", "")
            if prot_id and prot_id in self.engine.elements and prot_id != hov_id:
                raw_related.append((prot_id, "Protects", (50, 220, 120)))

            # 2. Rear element (Yellow)
            rear_id = getattr(el, "rear_element", "")
            if rear_id and rear_id in self.engine.elements and rear_id != hov_id:
                raw_related.append((rear_id, "Rear Track", (255, 215, 50)))

        elif etype == "turnout":
            # 1. Common leg (Cyan)
            com_id = getattr(el, "common_neighbor", "")
            if com_id and com_id in self.engine.elements and com_id != hov_id:
                raw_related.append((com_id, "Common Leg", (0, 225, 255)))

            # 2. Normal leg (Green)
            norm_id = getattr(el, "normal_neighbor", "")
            if norm_id and norm_id in self.engine.elements and norm_id != hov_id:
                raw_related.append((norm_id, "Normal Leg", (50, 220, 120)))

            # 3. Reverse leg (Orange)
            rev_id = getattr(el, "reverse_neighbor", "")
            if rev_id and rev_id in self.engine.elements and rev_id != hov_id:
                raw_related.append((rev_id, "Reverse Leg", (255, 140, 40)))

            # 4. Guarded by signals (Magenta)
            sigs = getattr(el, "guarded_by_signals", []) or []
            if isinstance(sigs, str):
                sigs = [s.strip() for s in sigs.split(",") if s.strip()]
            if isinstance(sigs, (list, tuple, set)):
                for sid in sigs:
                    if sid and sid in self.engine.elements and sid != hov_id:
                        raw_related.append((sid, "Guarding Signal", (220, 100, 255)))

        if not raw_related:
            return

        # Deduplicate and group by target_id
        targets_dict: Dict[str, Dict[str, Any]] = {}
        for tid, lbl, col in raw_related:
            if tid not in targets_dict:
                targets_dict[tid] = {"labels": [lbl], "color": col}
            else:
                if lbl not in targets_dict[tid]["labels"]:
                    targets_dict[tid]["labels"].append(lbl)

        # Source screen position
        src_sx, src_sy = self.logical_to_screen(float(el.x), float(el.y))

        overlay_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        now = time.time()
        pulse = (now * 2.5) % 1.0

        # Draw subtle anchor highlight on source hovered element
        if etype == "track":
            try:
                sp0, sp1 = LayoutTopologyAssistant.get_track_endpoints_logical(el)
                ss0 = self.logical_to_screen(sp0[0], sp0[1])
                ss1 = self.logical_to_screen(sp1[0], sp1[1])
                bw_src = max(3, int(4 * self.zoom))
                pygame.draw.line(overlay_surf, (120, 190, 255, 60), ss0, ss1, bw_src + 6)
                pygame.draw.line(overlay_surf, (255, 255, 255, 180), ss0, ss1, bw_src)
            except Exception:
                pass
        pygame.draw.circle(overlay_surf, (255, 255, 255, 220), (src_sx, src_sy), 4)
        pygame.draw.circle(overlay_surf, (100, 180, 255, 150), (src_sx, src_sy), 8, 2)

        occupied_badge_rects: List[pygame.Rect] = []

        for tid, tdata in targets_dict.items():
            rel_el = self.engine.elements.get(tid)
            if not rel_el:
                continue

            theme_rgb = tdata["color"]
            combined_lbl = " / ".join(tdata["labels"])
            badge_text = f"{combined_lbl}: {tid}"

            tgt_sx, tgt_sy = self.logical_to_screen(float(rel_el.x), float(rel_el.y))
            rel_etype = getattr(rel_el, "elem_type", "")

            # Check if target is off-screen
            is_offscreen = (tgt_sx < 25 or tgt_sx > self.width - 25 or 
                            tgt_sy < hud_h + 20 or tgt_sy > self.height - footer_h - 20)

            if is_offscreen:
                csx = max(35, min(self.width - 35, tgt_sx))
                csy = max(hud_h + 25, min(self.height - footer_h - 25, tgt_sy))

                # Guide line to edge beacon
                pygame.draw.line(overlay_surf, (*theme_rgb, 60), (src_sx, src_sy), (csx, csy), 3)
                pygame.draw.line(overlay_surf, (255, 255, 255, 175), (src_sx, src_sy), (csx, csy), 1)

                # Edge pointing beacon
                angle = math.atan2(tgt_sy - csy, tgt_sx - csx)
                arr_len = 14
                tip_x = csx + math.cos(angle) * arr_len
                tip_y = csy + math.sin(angle) * arr_len
                p1_x = csx + math.cos(angle + 2.5) * (arr_len * 0.7)
                p1_y = csy + math.sin(angle + 2.5) * (arr_len * 0.7)
                p2_x = csx + math.cos(angle - 2.5) * (arr_len * 0.7)
                p2_y = csy + math.sin(angle - 2.5) * (arr_len * 0.7)
                pygame.draw.polygon(overlay_surf, (*theme_rgb, 240), [(tip_x, tip_y), (p1_x, p1_y), (p2_x, p2_y)])

                # Edge badge
                tag_surf = self.font_sm_bold.render(f"{badge_text} (OFF-SCREEN)", True, (240, 245, 255))
                tw, th = tag_surf.get_width() + 12, tag_surf.get_height() + 6
                tx = max(10, min(self.width - tw - 10, csx - tw // 2))
                ty = max(hud_h + 4, min(self.height - footer_h - th - 4, csy - th - 12))
                b_rect = pygame.Rect(tx, ty, tw, th)
                pygame.draw.rect(overlay_surf, (18, 24, 34, 230), b_rect, border_radius=4)
                pygame.draw.rect(overlay_surf, (*theme_rgb, 220), b_rect, 1, border_radius=4)
                overlay_surf.blit(tag_surf, (tx + 6, ty + 3))
                continue

            # Target is on-screen:
            # 1. Guide line from hovered element to target element
            pygame.draw.line(overlay_surf, (*theme_rgb, 60), (src_sx, src_sy), (tgt_sx, tgt_sy), 3)
            pygame.draw.line(overlay_surf, (255, 255, 255, 175), (src_sx, src_sy), (tgt_sx, tgt_sy), 1)

            # 2. If target is a track segment, highlight track bar
            if rel_etype == "track":
                try:
                    tp0, tp1 = LayoutTopologyAssistant.get_track_endpoints_logical(rel_el)
                    ts0 = self.logical_to_screen(tp0[0], tp0[1])
                    ts1 = self.logical_to_screen(tp1[0], tp1[1])
                    bar_w = max(4, int(5 * self.zoom))
                    pygame.draw.line(overlay_surf, (*theme_rgb, 70), ts0, ts1, bar_w + 10)
                    pygame.draw.line(overlay_surf, (*theme_rgb, 140), ts0, ts1, bar_w + 4)
                    pygame.draw.line(overlay_surf, (255, 255, 255, 210), ts0, ts1, bar_w)
                except Exception:
                    pass

            # 3. Pulsating radar reticle at (tgt_sx, tgt_sy)
            base_r = max(9, int(13 * self.zoom))
            ping_r = base_r + int(20 * pulse * self.zoom)
            ping_alpha = max(0, int(220 * (1.0 - pulse)))

            # Expanding ping ring
            pygame.draw.circle(overlay_surf, (*theme_rgb, ping_alpha), (tgt_sx, tgt_sy), ping_r, 2)
            # Inner glow
            pygame.draw.circle(overlay_surf, (*theme_rgb, 75), (tgt_sx, tgt_sy), base_r + 4)
            # Solid border ring
            pygame.draw.circle(overlay_surf, (*theme_rgb, 240), (tgt_sx, tgt_sy), base_r + 1, 2)
            # Center bright dot
            pygame.draw.circle(overlay_surf, (255, 255, 255, 230), (tgt_sx, tgt_sy), max(2, int(3 * self.zoom)))

            # Reticle crosshair ticks
            tick_len = max(4, int(6 * self.zoom))
            gap = base_r + 3
            pygame.draw.line(overlay_surf, (*theme_rgb, 220), (tgt_sx, tgt_sy - gap - tick_len), (tgt_sx, tgt_sy - gap), 2)
            pygame.draw.line(overlay_surf, (*theme_rgb, 220), (tgt_sx, tgt_sy + gap), (tgt_sx, tgt_sy + gap + tick_len), 2)
            pygame.draw.line(overlay_surf, (*theme_rgb, 220), (tgt_sx - gap - tick_len, tgt_sy), (tgt_sx - gap, tgt_sy), 2)
            pygame.draw.line(overlay_surf, (*theme_rgb, 220), (tgt_sx + gap, tgt_sy), (tgt_sx + gap + tick_len, tgt_sy), 2)

            # 4. Relationship badge near target element
            b_surf = self.font_sm_bold.render(badge_text, True, (245, 250, 255))
            bw, bh = b_surf.get_width() + 12, b_surf.get_height() + 6
            bx = tgt_sx + base_r + 8
            by = tgt_sy - bh // 2
            if bx + bw > self.width - 10:
                bx = tgt_sx - base_r - bw - 8
            if by < hud_h + 4:
                by = hud_h + 4
            if by + bh > self.height - footer_h - 4:
                by = self.height - footer_h - bh - 4

            badge_rect = pygame.Rect(bx, by, bw, bh)
            for prev_rect in occupied_badge_rects:
                if badge_rect.colliderect(prev_rect):
                    badge_rect.y = prev_rect.bottom + 3
                    if badge_rect.bottom > self.height - footer_h - 4:
                        badge_rect.y = prev_rect.top - bh - 3
            occupied_badge_rects.append(badge_rect)

            pygame.draw.rect(overlay_surf, (18, 24, 34, 230), badge_rect, border_radius=4)
            pygame.draw.rect(overlay_surf, (*theme_rgb, 220), badge_rect, 1, border_radius=4)
            overlay_surf.blit(b_surf, (badge_rect.x + 6, badge_rect.y + 3))

        self.screen.blit(overlay_surf, (0, 0))

    def _draw_element_hover_tooltip(self):
        if self.frag_picker_modal or self.gps_picker_modal or self.inplace_editor or self.confirm_exit_modal or self.confirm_new_modal or self.confirm_load_modal or self.file_prompt_mode:
            return
        if getattr(self, 'box_selecting', False) or getattr(self, 'dragging_selection', False) or (getattr(self, 'dragging_track_end', None) is not None):
            return

        mx, my = pygame.mouse.get_pos()
        hud_h = getattr(self, "hud_bar_height", 50)
        footer_h = 34
        if my < hud_h or my > self.height - footer_h:
            return

        hov_id = self.pick_element_at_screen(mx, my)
        if not hov_id or hov_id not in self.engine.elements:
            return

        el = self.engine.elements[hov_id]
        if getattr(el, "elem_type", "") in SYSTEM_LIST_TYPES:
            return

        tip_txt = self._format_element_tooltip(el)
        if not tip_txt:
            return

        txt_surf = self.font_sm.render(tip_txt, True, (240, 245, 255))
        tw, th = txt_surf.get_size()
        pad_x, pad_y = 6, 3
        bw, bh = tw + pad_x * 2, th + pad_y * 2

        bx = mx + 14
        by = my + 14
        if bx + bw > self.width - 8:
            bx = mx - bw - 8
        if by + bh > self.height - footer_h - 2:
            by = my - bh - 8
        bx = max(8, bx)
        by = max(hud_h + 2, by)

        tip_surf = pygame.Surface((bw, bh), pygame.SRCALPHA)
        tip_surf.fill((16, 22, 32, 225))
        pygame.draw.rect(tip_surf, (70, 115, 165, 220), (0, 0, bw, bh), 1, border_radius=4)
        tip_surf.blit(txt_surf, (pad_x, pad_y))
        self.screen.blit(tip_surf, (bx, by))

    def run(self):
        running = True
        try:
            while running:
                running = self.handle_events()
                flash_on = (int(time.time() * 4.0) % 2) == 0

                # Periodic evaluation of train inference logic & timeouts
                now_tick = time.time()
                if now_tick - getattr(self, "_last_engine_eval_ts", 0) >= 0.25:
                    self._last_engine_eval_ts = now_tick
                    if getattr(self, "enable_inference_logic", True):
                        self.engine._evaluate_system_logic(now_tick, force=False)
                    self._cached_render_state = None

                # Advance raw packet log playback if active
                if self.playback_active and self.playback_packets:
                    if now_tick - self.playback_last_tick >= self.playback_delay_sec:
                        self.playback_last_tick = now_tick
                        self.step_playback(forward=True)
                        self._cached_render_state = None

                self.screen.fill(COLOR_BG)
                self.draw_grid()

                state = getattr(self, "_cached_render_state", None)
                if (
                    state is None
                    or getattr(self, "dragging_selection", False)
                    or getattr(self, "dragging_track_end", None) is not None
                    or len(state.get("elements", {})) != len(self.engine.elements)
                ):
                    state = self.engine.get_render_state()
                    self._cached_render_state = state
                    self._cached_render_state_ts = now_tick
                self.draw_layout_state(state, flash_on)

                if self.inplace_editor:
                    self.inplace_editor.draw(self.screen, self.font_sm, self.font_md)
                    # Visual pointer & badge when hovering over layout elements while editing
                    mx_h, my_h = pygame.mouse.get_pos()
                    p_rect = getattr(self.inplace_editor, 'panel_rect', None)
                    if p_rect and not p_rect.collidepoint(mx_h, my_h) and my_h >= getattr(self, "hud_bar_height", 50):
                        hov_id = self.pick_element_at_screen(mx_h, my_h)
                        if hov_id and hov_id != self.inplace_editor.element.id:
                            hov_el = self.engine.elements.get(hov_id)
                            if hov_el:
                                esx, esy = self.logical_to_screen(float(self.inplace_editor.element.x), float(self.inplace_editor.element.y))
                                hsx, hsy = self.logical_to_screen(float(hov_el.x), float(hov_el.y))
                                r_dir = LayoutTopologyAssistant.determine_relative_direction(self.inplace_editor.element, hov_el)
                                pygame.draw.line(self.screen, COLOR_SELECT_RING, (esx, esy), (hsx, hsy), 2)
                                pygame.draw.circle(self.screen, COLOR_EDIT_ACCENT, (hsx, hsy), 7, 2)
                                is_trk = isinstance(self.inplace_editor.element, TrackSegment)
                                tip_txt = f"Click to pick: {r_dir}:{hov_id}" if is_trk else f"Click to pick: {hov_id} ({r_dir})"
                                t_surf = self.font_sm_bold.render(tip_txt, True, (255, 255, 255))
                                t_bg = pygame.Rect(mx_h + 14, my_h + 14, t_surf.get_width() + 8, t_surf.get_height() + 4)
                                pygame.draw.rect(self.screen, (24, 34, 48), t_bg, border_radius=3)
                                pygame.draw.rect(self.screen, COLOR_EDIT_ACCENT, t_bg, 1, border_radius=3)
                                self.screen.blit(t_surf, (t_bg.x + 4, t_bg.y + 2))
                if self.playback_packets:
                    self.draw_playback_bar()

                self._draw_related_elements_hover()
                self.draw_hud()
                self._draw_element_hover_tooltip()
                if self.train_assign_active:
                    self.draw_train_assign_dialog()
                if self.gps_picker_modal:
                    self.gps_picker_modal.draw(self.screen, self.font_sm, self.font_md, self.font_sm_bold)
                if self.frag_picker_modal:
                    self.frag_picker_modal.draw(self.screen, self.font_sm, self.font_md, self.font_sm_bold)
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.draw(self.screen, self.font_sm, self.font_md, self.font_sm_bold)
                pygame.display.flip()
                self.clock.tick(60)
        finally:
            self.wiu_cache.save_cache()
            pygame.quit()


def main():
    ap = argparse.ArgumentParser(description=f"ApexBOSCreator ({CREATOR_VERSION}) - Railroad Layout Studio")
    ap.add_argument("--layout", default="complex_layout.json", help="Layout JSON file to edit or create")
    ap.add_argument("--db", default=None, help="Optional SQLite3 DB file whose 'wius' table provides suggested WIUs")
    ap.add_argument("--parsers", nargs="*", default=None, help="Optional list of PacketParser JSON rule filenames")
    ap.add_argument("--lat", type=float, default=None, help="Initial latitude for GPS proximity sorting and gps_selector.py")
    ap.add_argument("--lon", type=float, default=None, help="Initial longitude for GPS proximity sorting and gps_selector.py")
    ap.add_argument("--radius", type=float, default=20.0, help="Default radius (miles) for gps_selector.py (default: 20)")
    ap.add_argument("--map-radius", type=float, default=0.1, help="Layout view radius (miles) visible at zoom 1.0 (default: 0.1)")
    ap.add_argument("--banc-config", default=None, help="Optional path to Banc configuration file (JSON)")
    args = ap.parse_args()

    gps_specified = (args.lat is not None and args.lon is not None)
    default_lat = 33.0522 if args.lat is None else args.lat
    default_lon = -110.2400 if args.lon is None else args.lon
    banc_config_path = args.banc_config
    if not banc_config_path:
        if os.path.exists("config.json"):
            banc_config_path = "config.json"
        elif os.path.exists(os.path.join("config", "config.json")):
            banc_config_path = os.path.join("config", "config.json")

    app = ApexBOSCreatorApp(
        layout_filepath=args.layout,
        db_filepath=args.db,
        parser_files=args.parsers,
        default_lat=default_lat,
        default_lon=default_lon,
        default_radius=args.radius,
        map_view_radius_miles=args.map_radius,
        banc_config=banc_config_path,
        gps_specified=gps_specified,
    )
    app.run()


if __name__ == "__main__":
    main()