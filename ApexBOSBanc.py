#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApexBOSBanc.py - ApexBOSBanc Cloud Receiver for ApexBOSFlux

MIT License
Copyright (c) 2026 AB

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import os
import re
import sys
import json
import time
import math
import uuid
import struct
import socket
import ipaddress
import sqlite3
import tempfile
import threading
import argparse
import asyncio
from typing import Optional, Tuple, List, Dict, Any

# Python 3.6 backward compatibility shims
if not hasattr(asyncio, "get_running_loop"):
    def _compat_get_running_loop():
        try:
            loop = asyncio.get_event_loop()
            if loop is not None and loop.is_running():
                return loop
        except Exception:
            pass
        raise RuntimeError("no running event loop")
    asyncio.get_running_loop = _compat_get_running_loop

if not hasattr(asyncio, "create_task"):
    asyncio.create_task = asyncio.ensure_future

if not hasattr(asyncio, "run") or getattr(asyncio.run, "_is_compat", False):
    def _compat_asyncio_run(coro):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed() or loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                return new_loop.run_until_complete(coro)
            finally:
                try:
                    new_loop.close()
                finally:
                    asyncio.set_event_loop(loop if (loop is not None and not loop.is_closed()) else None)
        return loop.run_until_complete(coro)
    _compat_asyncio_run._is_compat = True
    asyncio.run = _compat_asyncio_run
if hasattr(asyncio, "Server") and not hasattr(asyncio.Server, "serve_forever"):
    async def _compat_serve_forever(self):
        loop = getattr(self, "_loop", None) or asyncio.get_event_loop()
        fut = loop.create_future()
        try:
            await fut
        except asyncio.CancelledError:
            self.close()
            await self.wait_closed()
    asyncio.Server.serve_forever = _compat_serve_forever
from collections import deque
from datetime import datetime, timezone
from cryptography.fernet import Fernet
import mosfet_auth
from version import VERSION, __version__
from mosfet_client import (
    MAX_CHAT_MESSAGE_LEN,
    MAX_CHAT_PAYLOAD_BYTES,
    MAX_CHAT_IDENTITY_LEN,
    sanitize_chat_identity,
    sanitize_chat_text,
    extract_chat_mentions,
)

# ANSI Colors and Backgrounds
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

BG_BANNER = "\033[44;1;37m"    # Blue background, bold white text
BG_AVAIL = "\033[42;1;30m"     # Green background, dark text
BG_UNAVAIL = "\033[41;1;37m"   # Red background, white text
BG_PAUSED = "\033[43;1;30m"    # Yellow background, dark text
BG_ACTIVE = "\033[42;1;30m"    # Green background, dark text
BG_STATUS = "\033[46;1;30m"    # Cyan background, dark text


# ---------------------------------------------------------------------------
# Layout GPS Analysis (Center of Mass & Maximum Radius)
# ---------------------------------------------------------------------------

def calculate_layout_gps(layout_obj: Any) -> Tuple[Optional[float], Optional[float], float]:
    """
    Calculates the center of mass GPS location (lat, lon) and maximum radius (in km)
    based on any available GPS information in the layout.
    Returns: (center_lat, center_lon, radius_km)
    If no GPS points found, returns (None, None, 0.0).
    """
    if isinstance(layout_obj, (bytes, bytearray)):
        try:
            layout_obj = layout_obj.decode("utf-8")
        except Exception:
            return None, None, 0.0

    if isinstance(layout_obj, str):
        try:
            layout_obj = json.loads(layout_obj)
        except Exception:
            return None, None, 0.0

    if not isinstance(layout_obj, dict):
        return None, None, 0.0

    pts: List[Tuple[float, float]] = []

    # 1. Search elements
    elements = layout_obj.get("elements", [])
    if isinstance(elements, dict):
        elements = list(elements.values())

    if isinstance(elements, list):
        for el in elements:
            if not isinstance(el, dict):
                continue
            # Check 'gps' list/tuple [lat, lon]
            gps = el.get("gps")
            if isinstance(gps, (list, tuple)) and len(gps) >= 2:
                try:
                    lat, lon = float(gps[0]), float(gps[1])
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
                        pts.append((lat, lon))
                        continue
                except (ValueError, TypeError):
                    pass
            # Check 'lat' and 'lon' or 'latitude' and 'longitude'
            lat_val = el.get("lat") if el.get("lat") is not None else el.get("latitude")
            lon_val = el.get("lon") if el.get("lon") is not None else el.get("longitude")
            if lat_val is not None and lon_val is not None:
                try:
                    lat, lon = float(lat_val), float(lon_val)
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
                        pts.append((lat, lon))
                except (ValueError, TypeError):
                    pass

    # 2. Check top-level layout GPS info if no element points found
    if not pts:
        for k in ("gps", "center_gps"):
            gps = layout_obj.get(k)
            if isinstance(gps, (list, tuple)) and len(gps) >= 2:
                try:
                    lat, lon = float(gps[0]), float(gps[1])
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
                        pts.append((lat, lon))
                except (ValueError, TypeError):
                    pass

    if not pts:
        return None, None, 0.0

    center_lat = sum(p[0] for p in pts) / len(pts)
    center_lon = sum(p[1] for p in pts) / len(pts)

    # Calculate max distance using Haversine formula (km)
    max_dist_km = 0.0
    r_earth = 6371.0088  # km
    c_lat_rad = math.radians(center_lat)
    c_lon_rad = math.radians(center_lon)

    for lat, lon in pts:
        p_lat_rad = math.radians(lat)
        p_lon_rad = math.radians(lon)
        dlat = p_lat_rad - c_lat_rad
        dlon = p_lon_rad - c_lon_rad
        a = (math.sin(dlat / 2.0) ** 2) + math.cos(c_lat_rad) * math.cos(p_lat_rad) * (math.sin(dlon / 2.0) ** 2)
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
        dist_km = r_earth * c
        if dist_km > max_dist_km:
            max_dist_km = dist_km

    return round(center_lat, 6), round(center_lon, 6), round(max_dist_km, 3)


# ---------------------------------------------------------------------------
# WIU Metadata Normalization Helpers
# ---------------------------------------------------------------------------

def _normalize_wiu_id(val: Any) -> str:
    if val is None or isinstance(val, bool):
        return ""
    s = str(val).strip()
    if not s:
        return ""
    try:
        if re.fullmatch(r"-?\d+", s):
            return str(int(s))
    except Exception:
        pass
    return s


def _parse_and_format_gps(gps_val: Any = None, lat_val: Any = None, lon_val: Any = None) -> str:
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
            cleaned = s.strip("()[] ")
            parts = [p.strip() for p in re.split(r"[,;\s]+", cleaned) if p.strip()]
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


def _parse_ctc_tokens(raw_val: Any) -> List[str]:
    tokens: List[str] = []
    seen = set()

    def _add(item: str):
        tok = str(item).strip()
        if not tok:
            return
        k = tok.lower()
        if k not in seen:
            seen.add(k)
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


# ---------------------------------------------------------------------------
# Database Management (Executor wrapped for asyncio)
# ---------------------------------------------------------------------------
# Protocol identifiers stored in reserved packets.b2 (INTEGER) and packets.b3 (TEXT)
PKT_PROTO_PTC = 0
PKT_PROTO_ATCS = 1
PKT_PROTO_FRED = 2  # EOT / HOT / DPU / RLC

# Regional privacy & geo-scoping constants for short-range RF packets (e.g. FRED)
MILES_TO_KM = 1.609344
DEFAULT_AUTO_PTC_RADIUS_KM = 24.0   # ~15 mi default enclosing radius for auto-inferred PTC WIU centroid
MIN_GEO_QUERY_RADIUS_KM = 10.0      # Anti-triangulation minimum viewer query radius floor
GEO_QUERY_GRID_DEG = 0.05           # ~5.5 km coarse snapping grid for viewer GET coordinates
AUTO_PTC_GRID_DEG = 0.10            # ~11 km coarse quantization grid for Tier-2 auto PTC centroid
GEO_QUERY_MAX_SWITCHES_30S = 5      # Anti-triangulation rate-limit on distinct coord sweeps per 30s


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two WGS84 points."""
    r_earth = 6371.0088
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dlat = p2 - p1
    dlon = math.radians(float(lon2) - float(lon1))
    a = (math.sin(dlat / 2.0) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(max(0.0, a)), math.sqrt(max(0.0, 1.0 - a)))
    return r_earth * c


def quantize_geo_point(lat: float, lon: float, step_deg: float = GEO_QUERY_GRID_DEG) -> Tuple[float, float]:
    """Snaps (lat, lon) to a coarse degree grid to prevent coordinate-oracle triangulation."""
    step = max(0.001, float(step_deg))
    q_lat = max(-85.0, min(85.0, round(round(float(lat) / step) * step, 4)))
    q_lon = max(-180.0, min(180.0, round(round(float(lon) / step) * step, 4)))
    return q_lat, q_lon


def _parse_gps_latlon(gps_str: Any) -> Optional[Tuple[float, float]]:
    """Parses a stored WIU GPS string ('lat, lon') or tuple into (lat, lon) floats."""
    if not gps_str:
        return None
    if isinstance(gps_str, (list, tuple)) and len(gps_str) >= 2:
        try:
            lat, lon = float(gps_str[0]), float(gps_str[1])
            if -85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
                return lat, lon
        except (ValueError, TypeError):
            return None
        return None
    s = str(gps_str).strip().strip("()[] ")
    parts = [p.strip() for p in re.split(r"[,;\s]+", s) if p.strip()]
    if len(parts) < 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
        if -85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
            return lat, lon
    except (ValueError, TypeError):
        return None
    return None


def extract_ptc_wiu_id_from_hex(hex_data: str) -> Optional[str]:
    """
    Extracts the 40-bit PTC WIU _node_id_ from an 0x33/0x34 packet hex payload
    (Prefix[1] + PacketSize[2] + Type[1 in {0x33,0x34}] + Channel[1] + _node_id_[5]).
    """
    s = str(hex_data or "").strip()
    if len(s) < 20:
        return None
    try:
        raw = bytes.fromhex(s[:128])
    except ValueError:
        return None
    if raw.startswith(b"CH"):
        m = re.match(rb"^CH\d{2,3}(?::RSSI=[+-]?\d+(?:\.\d+)?)?:", raw)
        if m:
            raw = raw[m.end():]
    if len(raw) >= 10 and raw[3] in (0x33, 0x34):
        node_id = int.from_bytes(raw[5:10], "big", signed=False)
        if node_id > 0:
            return str(node_id)
    return None


def _proto_key_to_code(proto_key: Any) -> Optional[int]:
    if isinstance(proto_key, int):
        return proto_key if proto_key in (PKT_PROTO_PTC, PKT_PROTO_ATCS, PKT_PROTO_FRED) else None
    s = str(proto_key or "").strip().lower()
    if s in ("0", "ptc", "grabber", "itc"):
        return PKT_PROTO_PTC
    if s in ("1", "atcs", "latch"):
        return PKT_PROTO_ATCS
    if s in ("2", "fred", "eot", "hot", "dpu", "rlc"):
        return PKT_PROTO_FRED
    return None


def normalize_packet_proto(b2_raw: Any, b3_raw: Any) -> Tuple[int, str]:
    """
    Normalizes reserved columns (b2, b3) for PTC (0), ATCS (1), and EOT/HOT/DPU/RLC (2).
    Preserves legacy PTC rows (b2=0, b3='"NA"') verbatim for full backwards compatibility.
    """
    try:
        b2 = int(b2_raw)
    except (ValueError, TypeError):
        b2 = PKT_PROTO_PTC

    raw_b3 = str(b3_raw) if b3_raw is not None else '"NA"'
    clean_b3 = raw_b3.strip().strip('"').strip("'").upper()

    if b2 == PKT_PROTO_PTC:
        if clean_b3 == "ATCS":
            b2 = PKT_PROTO_ATCS
        elif clean_b3 in ("EOT", "HOT", "DPU", "RLC", "FRED"):
            b2 = PKT_PROTO_FRED

    if b2 == PKT_PROTO_PTC:
        return PKT_PROTO_PTC, raw_b3
    if b2 == PKT_PROTO_ATCS:
        tag = re.sub(r"[^A-Za-z0-9_-]", "", clean_b3) if clean_b3 and clean_b3 != "NA" else "ATCS"
        return PKT_PROTO_ATCS, tag or "ATCS"
    if b2 == PKT_PROTO_FRED:
        tag = re.sub(r"[^A-Za-z0-9_-]", "", clean_b3) if clean_b3 and clean_b3 != "NA" else "FRED"
        return PKT_PROTO_FRED, tag or "FRED"
    safe_tag = re.sub(r"[^A-Za-z0-9_-]", "", clean_b3) or "NA"
    return b2, safe_tag


class DatabaseManager:
    def __init__(self, db_path, retention_sec=600):
        self.db_path = db_path
        self.retention_sec = retention_sec
        self._region_lock = threading.Lock()
        self._wiu_gps_cache: Dict[str, Tuple[float, float]] = {}
        self._wiu_gps_cache_ts: float = 0.0
        self._identity_wiu_obs: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS packets (
                    pkt_hash INTEGER PRIMARY KEY,
                    date_str TEXT,
                    unix_ts REAL,
                    identity TEXT,
                    hex_data TEXT,
                    channel INTEGER,
                    signal_strength TEXT,
                    b1 REAL,
                    b2 INTEGER,
                    b3 TEXT,
                    stamp_received REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_stamp ON packets(stamp_received)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ident ON packets(identity)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pkt_proto ON packets(b2)")

            # Layouts storage table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS layouts (
                    layout_name TEXT PRIMARY KEY,
                    layout_data TEXT,
                    center_lat REAL,
                    center_lon REAL,
                    radius_km REAL,
                    is_available INTEGER DEFAULT 1,
                    uploaded_by TEXT,
                    stamp_created REAL,
                    stamp_updated REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_layout_avail ON layouts(is_available)")

            # WIUs registry table (with integer, float, and string reserved fields)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wius (
                    wiu_id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    gps TEXT DEFAULT '',
                    submitter TEXT DEFAULT '',
                    first_submitted TEXT DEFAULT '',
                    last_updated TEXT DEFAULT '',
                    has_ctc INTEGER DEFAULT 0,
                    ctc_addresses TEXT DEFAULT '',
                    reserved_int INTEGER DEFAULT 0,
                    reserved_float REAL DEFAULT 0.0,
                    reserved_str TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wius_updated ON wius(last_updated)")

            # Private server-side feeder reception regions (NEVER returned to GET clients)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feeder_regions (
                    identity TEXT NOT NULL,
                    proto INTEGER NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    source TEXT DEFAULT 'user_blurred',
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY (identity, proto)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feeder_regions_ident ON feeder_regions(identity)")
            conn.commit()

    def upsert_feeder_region(
        self,
        identity: str,
        proto: int,
        center_lat: float,
        center_lon: float,
        radius_km: float,
        source: str = "user_blurred",
    ) -> bool:
        """
        Stores a privacy-blurred feeder reception region in the private server-side table.
        Auto-inferred ('ptc_wiu_auto') regions never overwrite explicit 'user_blurred' regions.
        """
        ident = str(identity or "").strip()
        if not ident:
            return False
        p_code = _proto_key_to_code(proto)
        if p_code is None:
            return False
        try:
            lat = float(center_lat)
            lon = float(center_lon)
            rad = float(radius_km)
        except (ValueError, TypeError):
            return False
        if not (-85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0):
            return False
        if lat == 0.0 and lon == 0.0:
            return False
        rad = max(8.0, min(130.0, rad))
        src = str(source or "user_blurred").strip()
        now_ts = time.time()
        with self._region_lock:
            conn = sqlite3.connect(self.db_path)
            try:
                if src == "ptc_wiu_auto":
                    cur = conn.execute(
                        "SELECT source FROM feeder_regions WHERE identity = ? AND proto = ?",
                        (ident, p_code),
                    )
                    row = cur.fetchone()
                    if row and row[0] == "user_blurred":
                        return False
                conn.execute(
                    """
                    INSERT INTO feeder_regions (identity, proto, center_lat, center_lon, radius_km, source, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity, proto) DO UPDATE SET
                        center_lat=excluded.center_lat,
                        center_lon=excluded.center_lon,
                        radius_km=excluded.radius_km,
                        source=excluded.source,
                        updated_ts=excluded.updated_ts
                    """,
                    (ident, p_code, round(lat, 4), round(lon, 4), round(rad, 2), src, now_ts),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    def delete_feeder_region(self, identity: str, proto: int) -> bool:
        ident = str(identity or "").strip()
        p_code = _proto_key_to_code(proto)
        if not ident or p_code is None:
            return False
        with self._region_lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM feeder_regions WHERE identity = ? AND proto = ? AND source = 'user_blurred'",
                    (ident, p_code),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def upsert_feeder_regions_payload(self, identity: str, payload: Any) -> int:
        """
        Processes a PUT_SDR_REGION JSON payload for a feeder identity.
        Supports:
          - {'ptc': {'center_lat': ..., 'center_lon': ..., 'radius_km': ...}, 'fred': {...}}
          - {'proto': 'fred', 'center_lat': ..., 'center_lon': ..., 'radius_km': ...}
          - {'fred': {'clear': True}} to remove an explicit user_blurred region.
        """
        if not isinstance(payload, dict):
            return 0
        updated = 0
        if "proto" in payload and ("center_lat" in payload or "region_lat" in payload or payload.get("clear")):
            entries = [(payload.get("proto"), payload)]
        else:
            entries = list(payload.items())

        for p_key, rec in entries:
            p_code = _proto_key_to_code(p_key)
            if p_code is None:
                continue
            if rec is None or (isinstance(rec, dict) and rec.get("clear")):
                if self.delete_feeder_region(identity, p_code):
                    updated += 1
                continue
            if not isinstance(rec, dict):
                continue
            lat = rec.get("center_lat") if rec.get("center_lat") is not None else rec.get("region_lat")
            lon = rec.get("center_lon") if rec.get("center_lon") is not None else rec.get("region_lon")
            rad_km = rec.get("radius_km")
            if rad_km is None and rec.get("region_radius_mi") is not None:
                try:
                    rad_km = float(rec["region_radius_mi"]) * MILES_TO_KM
                except (ValueError, TypeError):
                    rad_km = None
            if rad_km is None:
                rad_km = DEFAULT_AUTO_PTC_RADIUS_KM
            if lat is not None and lon is not None:
                if self.upsert_feeder_region(identity, p_code, lat, lon, rad_km, source="user_blurred"):
                    updated += 1
        return updated

    def get_all_feeder_regions_map(self) -> Dict[Tuple[str, int], Tuple[float, float, float, str]]:
        """Loads all private feeder regions into memory for server-side geo-filtering."""
        out: Dict[Tuple[str, int], Tuple[float, float, float, str]] = {}
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT identity, proto, center_lat, center_lon, radius_km, source FROM feeder_regions"
            )
            for ident, proto, lat, lon, rad_km, src in cur.fetchall():
                out[(str(ident), int(proto))] = (float(lat), float(lon), float(rad_km), str(src or ""))
        finally:
            conn.close()
        return out

    @staticmethod
    def resolve_feeder_region(
        regions_map: Dict[Tuple[str, int], Tuple[float, float, float, str]],
        identity: str,
        proto: int = PKT_PROTO_FRED,
    ) -> Optional[Tuple[float, float, float]]:
        """
        3-tier resolution for a feeder's regional footprint:
          1. Explicit/stored region for (identity, proto) (e.g. dedicated FRED SDR region)
          2. Fallback to (identity, PKT_PROTO_PTC) (explicit Grabber region or Tier-2 auto PTC WIU centroid)
          3. Fallback to (identity, PKT_PROTO_ATCS) if configured
          4. None -> unlocated (Tier-3: private to own identity only)
        """
        ident = str(identity or "")
        for p_try in (proto, PKT_PROTO_PTC, PKT_PROTO_ATCS):
            rec = regions_map.get((ident, p_try))
            if rec is not None:
                return rec[0], rec[1], rec[2]
        return None

    def _refresh_wiu_gps_cache(self, force: bool = False):
        now = time.time()
        if not force and (now - self._wiu_gps_cache_ts) < 30.0 and self._wiu_gps_cache:
            return
        cache: Dict[str, Tuple[float, float]] = {}
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute("SELECT wiu_id, gps FROM wius WHERE gps IS NOT NULL AND gps != ''")
                for wid, gps_s in cur.fetchall():
                    pt = _parse_gps_latlon(gps_s)
                    if pt is not None:
                        cache[_normalize_wiu_id(wid)] = pt
            finally:
                conn.close()
        except Exception:
            return
        self._wiu_gps_cache = cache
        self._wiu_gps_cache_ts = now

    def update_auto_regions_from_ptc_rows(self, rows: List[Tuple]):
        """
        Tier-2 automatic regional inference: extracts PTC WIU IDs from uploaded PTC rows,
        cross-references Banc's wius table, computes a coarse quantized median centroid per
        feeder identity, and stores it as a 'ptc_wiu_auto' region (never overriding explicit
        'user_blurred' regions).
        """
        ptc_rows = [r for r in rows if len(r) >= 9 and int(r[8]) == PKT_PROTO_PTC]
        if not ptc_rows:
            return
        self._refresh_wiu_gps_cache()
        if not self._wiu_gps_cache:
            return
        now = time.time()
        touched_idents = set()
        for r in ptc_rows:
            ident = str(r[2] or "").strip()
            hex_s = str(r[3] or "")
            if not ident or not hex_s:
                continue
            wid = extract_ptc_wiu_id_from_hex(hex_s)
            if not wid:
                continue
            pt = self._wiu_gps_cache.get(wid)
            if pt is None:
                continue
            obs = self._identity_wiu_obs.setdefault(ident, {})
            obs[wid] = (pt[0], pt[1], now)
            touched_idents.add(ident)

        for ident in touched_idents:
            obs = self._identity_wiu_obs.get(ident)
            if not obs:
                continue
            # Keep recent WIU observations (last 24h)
            fresh = {k: v for k, v in obs.items() if (now - v[2]) <= 86400.0}
            self._identity_wiu_obs[ident] = fresh
            if not fresh:
                continue
            lats = sorted(v[0] for v in fresh.values())
            lons = sorted(v[1] for v in fresh.values())
            mid = len(lats) // 2
            med_lat = lats[mid] if (len(lats) % 2 == 1) else 0.5 * (lats[mid - 1] + lats[mid])
            med_lon = lons[mid] if (len(lons) % 2 == 1) else 0.5 * (lons[mid - 1] + lons[mid])
            q_lat, q_lon = quantize_geo_point(med_lat, med_lon, step_deg=AUTO_PTC_GRID_DEG)
            self.upsert_feeder_region(
                ident,
                PKT_PROTO_PTC,
                q_lat,
                q_lon,
                DEFAULT_AUTO_PTC_RADIUS_KM,
                source="ptc_wiu_auto",
            )

    def insert_rows(self, rows):
        """Bulk insert rows, rapidly updating timestamps, signal strength, and protocol metadata if hash exists."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("""
                INSERT INTO packets (date_str, unix_ts, identity, hex_data, channel, signal_strength, pkt_hash, b1, b2, b3, stamp_received)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pkt_hash) DO UPDATE SET 
                    date_str=excluded.date_str, 
                    stamp_received=excluded.stamp_received,
                    signal_strength=excluded.signal_strength,
                    hex_data=excluded.hex_data,
                    b1=excluded.b1,
                    b2=excluded.b2,
                    b3=excluded.b3
            """, rows)
            conn.commit()
        try:
            self.update_auto_regions_from_ptc_rows(rows)
        except Exception:
            pass
        return True

    def get_recent_rows(
        self,
        limit=1000,
        since_ts=None,
        requester_identity: Optional[str] = None,
        geo_filter: Optional[Tuple[float, float, float]] = None,
    ):
        """
        Fetches rows for the GET service, strictly omitting the identity column.
        When geo_filter=(lat, lon, radius_km) is provided, filters PKT_PROTO_FRED (b2=2)
        rows server-side by Haversine overlap against each feeder's private region
        (explicit blurred region -> auto PTC WIU centroid -> own-identity-only).
        """
        cutoff = time.time() - self.retention_sec
        if since_ts is not None:
            try:
                s_val = float(since_ts)
                if s_val > cutoff:
                    cutoff = s_val
            except (ValueError, TypeError):
                pass

        if geo_filter is None:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT date_str, unix_ts, hex_data, channel, signal_strength, pkt_hash, b1, b2, b3, stamp_received 
                    FROM packets WHERE stamp_received >= ? ORDER BY stamp_received DESC LIMIT ?
                """, (cutoff, limit))
                return cur.fetchall()
            finally:
                conn.close()

        q_lat, q_lon = quantize_geo_point(geo_filter[0], geo_filter[1], step_deg=GEO_QUERY_GRID_DEG)
        q_rad_km = max(MIN_GEO_QUERY_RADIUS_KM, min(2000.0, float(geo_filter[2])))
        req_ident = str(requester_identity or "").strip()

        regions_map = self.get_all_feeder_regions_map()
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date_str, unix_ts, hex_data, channel, signal_strength, pkt_hash, b1, b2, b3, stamp_received, identity
                FROM packets WHERE stamp_received >= ? ORDER BY stamp_received DESC LIMIT ?
            """, (cutoff, limit))
            raw_rows = cur.fetchall()
        finally:
            conn.close()

        filtered_rows = []
        for row in raw_rows:
            b2_val = int(row[7]) if row[7] is not None else PKT_PROTO_PTC
            row_ident = str(row[10] or "").strip()
            if b2_val == PKT_PROTO_FRED:
                if req_ident and row_ident == req_ident:
                    filtered_rows.append(row[:10])
                    continue
                feeder_reg = self.resolve_feeder_region(regions_map, row_ident, PKT_PROTO_FRED)
                if feeder_reg is None:
                    # Tier 3: unlocated FRED packets remain private to own identity
                    continue
                f_lat, f_lon, f_rad_km = feeder_reg
                if haversine_km(q_lat, q_lon, f_lat, f_lon) <= (q_rad_km + f_rad_km):
                    filtered_rows.append(row[:10])
            else:
                filtered_rows.append(row[:10])
        return filtered_rows

    def get_identity_stats(self):
        """Returns total counts for all identities for periodic analysis."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT identity, COUNT(*) FROM packets GROUP BY identity")
            return cur.fetchall()

    def cleanup_old(self):
        """Deletes rows older than retention_sec."""
        cutoff = time.time() - self.retention_sec
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM packets WHERE stamp_received < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    def get_stats(self):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM packets")
            count = cur.fetchone()[0]
        size_kb = os.path.getsize(self.db_path) / 1024.0 if os.path.exists(self.db_path) else 0
        return count, size_kb

    def get_protocol_stats(self) -> Dict[int, int]:
        """Returns row counts keyed by reserved protocol code b2 (0=PTC, 1=ATCS, 2=FRED/EOT-HOT)."""
        counts = {PKT_PROTO_PTC: 0, PKT_PROTO_ATCS: 0, PKT_PROTO_FRED: 0}
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(b2, 0), COUNT(*) FROM packets GROUP BY COALESCE(b2, 0)")
            for proto_id, cnt in cur.fetchall():
                try:
                    k = int(proto_id)
                except (ValueError, TypeError):
                    k = PKT_PROTO_PTC
                counts[k] = counts.get(k, 0) + int(cnt)
        return counts

    def save_layout(self, layout_name: str, layout_data: str, center_lat: Optional[float], center_lon: Optional[float], radius_km: float, uploaded_by: str = ""):
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO layouts (layout_name, layout_data, center_lat, center_lon, radius_km, is_available, uploaded_by, stamp_created, stamp_updated)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(layout_name) DO UPDATE SET
                    layout_data=excluded.layout_data,
                    center_lat=excluded.center_lat,
                    center_lon=excluded.center_lon,
                    radius_km=excluded.radius_km,
                    uploaded_by=excluded.uploaded_by,
                    stamp_updated=excluded.stamp_updated
            """, (layout_name, layout_data, center_lat, center_lon, radius_km, uploaded_by, now, now))
            conn.commit()
        return True

    def get_layout(self, layout_name: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT layout_name, layout_data, center_lat, center_lon, radius_km, is_available, uploaded_by, stamp_updated
                FROM layouts WHERE layout_name = ?
            """, (layout_name,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "name": row[0],
                "layout_name": row[0],
                "data": row[1],
                "layout_data": row[1],
                "center_lat": row[2],
                "center_lon": row[3],
                "radius_km": row[4],
                "is_available": int(row[5]),
                "uploaded_by": row[6],
                "stamp_updated": row[7],
            }

    def list_layouts(self, include_unavailable: bool = True) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            if include_unavailable:
                cur.execute("""
                    SELECT layout_name, center_lat, center_lon, radius_km, is_available, uploaded_by, stamp_updated, length(layout_data)
                    FROM layouts ORDER BY layout_name ASC
                """)
            else:
                cur.execute("""
                    SELECT layout_name, center_lat, center_lon, radius_km, is_available, uploaded_by, stamp_updated, length(layout_data)
                    FROM layouts WHERE is_available = 1 ORDER BY layout_name ASC
                """)
            res = []
            for r in cur.fetchall():
                res.append({
                    "name": r[0],
                    "layout_name": r[0],
                    "center_lat": r[1],
                    "center_lon": r[2],
                    "radius_km": r[3],
                    "is_available": int(r[4]),
                    "uploaded_by": r[5],
                    "stamp_updated": r[6],
                    "size_bytes": r[7],
                })
            return res

    def delete_layout(self, layout_name: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM layouts WHERE layout_name = ?", (layout_name,))
            conn.commit()
            return cur.rowcount > 0

    def set_layout_availability(self, layout_name: str, is_available: bool) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            val = 1 if is_available else 0
            cur = conn.execute("UPDATE layouts SET is_available = ? WHERE layout_name = ?", (val, layout_name))
            conn.commit()
            return cur.rowcount > 0

    def upsert_wius(self, records: Any, submitter: str = "", now_str: Optional[str] = None) -> int:
        """Inserts or updates WIU records into the wius table, preserving first_submitted and merging CTC addresses."""
        if isinstance(records, dict):
            if "wius" in records and isinstance(records["wius"], list):
                rec_list = records["wius"]
            else:
                rec_list = [records]
        elif isinstance(records, list):
            rec_list = records
        else:
            return 0

        ts_str = now_str or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        upserted = 0

        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            for rec in rec_list:
                if not isinstance(rec, dict):
                    continue
                raw_wid = rec.get("wiu_id")
                if raw_wid is None:
                    raw_wid = rec.get("id")
                wiu_id = _normalize_wiu_id(raw_wid)
                if not wiu_id:
                    continue

                raw_name = rec.get("name")
                if raw_name is None:
                    raw_name = rec.get("wiu_name")
                if raw_name is None:
                    raw_name = rec.get("label")
                in_name = str(raw_name).strip() if raw_name is not None else ""

                lat_v = rec.get("lat") if rec.get("lat") is not None else rec.get("latitude")
                lon_v = rec.get("lon") if rec.get("lon") is not None else rec.get("longitude")
                in_gps = _parse_and_format_gps(rec.get("gps"), lat_v, lon_v)

                in_submitter = str(submitter or rec.get("submitter") or "").strip()

                in_ctc_tokens: List[str] = []
                for ckey in ("ctc_addresses", "ctc_id", "ctc"):
                    if rec.get(ckey) is not None:
                        in_ctc_tokens.extend(_parse_ctc_tokens(rec.get(ckey)))
                in_ctc_tokens = _parse_ctc_tokens(in_ctc_tokens)

                raw_has_ctc = rec.get("has_ctc")
                in_has_ctc = bool(in_ctc_tokens) or (
                    bool(int(raw_has_ctc)) if isinstance(raw_has_ctc, (int, float)) else (
                        str(raw_has_ctc).strip().lower() in ("1", "true", "yes", "on") if raw_has_ctc is not None else False
                    )
                )

                in_rint = rec.get("reserved_int")
                in_rfloat = rec.get("reserved_float")
                in_rstr = rec.get("reserved_str")

                cur.execute("""
                    SELECT name, gps, submitter, first_submitted, last_updated, has_ctc, ctc_addresses,
                           reserved_int, reserved_float, reserved_str
                    FROM wius WHERE wiu_id = ?
                """, (wiu_id,))
                existing = cur.fetchone()

                if existing:
                    ex_name, ex_gps, ex_sub, ex_first, _ex_last, ex_has_ctc, ex_ctc_csv, ex_rint, ex_rfloat, ex_rstr = existing
                    final_name = in_name if in_name else (ex_name or "")
                    final_gps = in_gps if in_gps else (ex_gps or "")
                    final_sub = in_submitter if in_submitter else (ex_sub or "")
                    final_first = ex_first if (ex_first and str(ex_first).strip()) else ts_str
                    final_last = ts_str

                    merged_ctc = _parse_ctc_tokens(_parse_ctc_tokens(ex_ctc_csv or "") + in_ctc_tokens)
                    final_ctc_csv = ",".join(merged_ctc)
                    final_has_ctc = 1 if (merged_ctc or in_has_ctc or bool(ex_has_ctc)) else 0

                    try:
                        final_rint = int(in_rint) if in_rint is not None else (int(ex_rint) if ex_rint is not None else 0)
                    except (ValueError, TypeError):
                        final_rint = int(ex_rint) if ex_rint is not None else 0
                    try:
                        final_rfloat = float(in_rfloat) if in_rfloat is not None else (float(ex_rfloat) if ex_rfloat is not None else 0.0)
                    except (ValueError, TypeError):
                        final_rfloat = float(ex_rfloat) if ex_rfloat is not None else 0.0
                    final_rstr = str(in_rstr) if in_rstr is not None else (ex_rstr if ex_rstr is not None else "")

                    cur.execute("""
                        UPDATE wius
                        SET name = ?, gps = ?, submitter = ?, first_submitted = ?, last_updated = ?,
                            has_ctc = ?, ctc_addresses = ?, reserved_int = ?, reserved_float = ?, reserved_str = ?
                        WHERE wiu_id = ?
                    """, (
                        final_name, final_gps, final_sub, final_first, final_last,
                        final_has_ctc, final_ctc_csv, final_rint, final_rfloat, final_rstr, wiu_id
                    ))
                else:
                    final_ctc_csv = ",".join(in_ctc_tokens)
                    final_has_ctc = 1 if (in_ctc_tokens or in_has_ctc) else 0
                    try:
                        final_rint = int(in_rint) if in_rint is not None else 0
                    except (ValueError, TypeError):
                        final_rint = 0
                    try:
                        final_rfloat = float(in_rfloat) if in_rfloat is not None else 0.0
                    except (ValueError, TypeError):
                        final_rfloat = 0.0
                    final_rstr = str(in_rstr) if in_rstr is not None else ""

                    cur.execute("""
                        INSERT INTO wius (
                            wiu_id, name, gps, submitter, first_submitted, last_updated,
                            has_ctc, ctc_addresses, reserved_int, reserved_float, reserved_str
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        wiu_id, in_name, in_gps, in_submitter, ts_str, ts_str,
                        final_has_ctc, final_ctc_csv, final_rint, final_rfloat, final_rstr
                    ))
                upserted += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if upserted > 0:
            self._wiu_gps_cache_ts = 0.0
        return upserted

    def list_wius(self) -> List[dict]:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT wiu_id, name, gps, submitter, first_submitted, last_updated,
                       has_ctc, ctc_addresses, reserved_int, reserved_float, reserved_str
                FROM wius
                ORDER BY last_updated DESC, wiu_id ASC
            """)
            out = []
            for r in cur.fetchall():
                out.append({
                    "wiu_id": r[0],
                    "name": r[1] or "",
                    "gps": r[2] or "",
                    "submitter": r[3] or "",
                    "first_submitted": r[4] or "",
                    "last_updated": r[5] or "",
                    "has_ctc": bool(r[6]),
                    "ctc_addresses": r[7] or "",
                    "reserved_int": r[8] if r[8] is not None else 0,
                    "reserved_float": r[9] if r[9] is not None else 0.0,
                    "reserved_str": r[10] if r[10] is not None else "",
                })
            return out
        finally:
            conn.close()

def normalize_ip(ip_str: Any) -> str:
    """
    Normalizes an IPv4 or IPv6 address string:
    - Strips surrounding brackets `[...]` and IPv6 scope/zone suffixes `%eth0` when parsing
    - Unwraps IPv4-mapped IPv6 addresses (`::ffff:1.2.3.4` -> `1.2.3.4`) so dual-stack
      sockets apply consistent IP identity and DoS cooldown tracking across both IP versions.
    """
    raw = str(ip_str or "").strip()
    if not raw:
        return "unknown"
    clean = raw.strip("[]")
    zone = ""
    if "%" in clean:
        clean, zone = clean.split("%", 1)
    try:
        addr = ipaddress.ip_address(clean)
        if getattr(addr, "ipv4_mapped", None) is not None:
            return str(addr.ipv4_mapped)
        canon = str(addr)
        return f"{canon}%{zone}" if (zone and addr.is_link_local) else canon
    except ValueError:
        return raw


def detect_ip_version(host: Optional[str]) -> Optional[int]:
    """Returns 4 for IPv4 literal, 6 for IPv6 literal, or None for hostname/wildcard."""
    if not host:
        return None
    clean = str(host).strip().strip("[]")
    if "%" in clean:
        clean = clean.split("%", 1)[0]
    try:
        return ipaddress.ip_address(clean).version
    except ValueError:
        return None


def format_host_port(host: str, port: int) -> str:
    """Formats host and port cleanly, wrapping IPv6 literals in square brackets."""
    clean = str(host or "").strip().strip("[]")
    if detect_ip_version(clean) == 6:
        return f"[{clean}]:{int(port)}"
    return f"{clean}:{int(port)}"


def _parse_mirrors_list(raw_mirrors: Any, default_port: int = 9000) -> List[str]:
    """
    Normalizes mirror specifications (list or comma-separated host[:port]) into unique host:port strings.
    Supports IPv4, hostnames, bracketed IPv6 (`[::1]:9000`, `[::1]`), and bare IPv6 literals (`2001:db8::5`).
    """
    tokens: List[str] = []
    if raw_mirrors is None:
        return tokens
    items = raw_mirrors if isinstance(raw_mirrors, (list, tuple, set)) else [raw_mirrors]
    for item in items:
        if item is None:
            continue
        for part in str(item).split(","):
            m = part.strip()
            if not m:
                continue
            # 1. Bare IPv6 literal without brackets or port (e.g. "2001:db8::5" or "::1")
            if not m.startswith("[") and detect_ip_version(m) == 6:
                m = f"[{m}]:{int(default_port)}"
            # 2. Bracketed IPv6 without port (e.g. "[::1]")
            elif m.startswith("[") and m.endswith("]"):
                m = f"{m}:{int(default_port)}"
            # 3. IPv4 or hostname without port
            elif ":" not in m:
                m = f"{m}:{int(default_port)}"
            if m not in tokens:
                tokens.append(m)
    return tokens


# ---------------------------------------------------------------------------
# Core Server State & Cooldown Tracking
# ---------------------------------------------------------------------------

class BlockedIPInfo(dict):
    """Metadata for an IP in cooldown, supporting numeric comparison for backwards compatibility."""
    def __init__(self, expiry: float, identity: str = "unknown", reason: str = "Authentication failed", blocked_at: Optional[float] = None):
        now = time.time() if blocked_at is None else float(blocked_at)
        super().__init__(
            expiry=float(expiry),
            identity=str(identity or "unknown"),
            reason=str(reason or "Authentication failed"),
            blocked_at=now,
        )

    @property
    def expiry(self) -> float:
        return float(self.get("expiry", 0.0))

    @property
    def identity(self) -> str:
        return str(self.get("identity", "unknown"))

    @property
    def reason(self) -> str:
        return str(self.get("reason", "Authentication failed"))

    @property
    def blocked_at(self) -> float:
        return float(self.get("blocked_at", 0.0))

    def __float__(self) -> float:
        return self.expiry

    def __lt__(self, other):
        val = float(other) if not isinstance(other, BlockedIPInfo) else other.expiry
        return self.expiry < val

    def __le__(self, other):
        val = float(other) if not isinstance(other, BlockedIPInfo) else other.expiry
        return self.expiry <= val

    def __gt__(self, other):
        val = float(other) if not isinstance(other, BlockedIPInfo) else other.expiry
        return self.expiry > val

    def __ge__(self, other):
        val = float(other) if not isinstance(other, BlockedIPInfo) else other.expiry
        return self.expiry >= val


class BancServer:
    def __init__(self, config):
        if isinstance(config, dict) and isinstance(config.get("banc"), dict) and "encryption_key" not in config:
            config = _unwrap_unified_banc_config(config)
        self.config = config
        enc_key = config.get("encryption_key", "")
        if not enc_key:
            raise KeyError("encryption_key (missing or empty in Banc config; run setup_wizard.py)")
        self.fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        self.auth_secret = config.get("auth_secret", "")
        
        # Load Whitelist
        self.whitelist_file = config.get("whitelist_file", "whitelist.txt")
        self.whitelist = set()
        if os.path.exists(self.whitelist_file):
            with open(self.whitelist_file, 'r') as f:
                self.whitelist = set(line.strip() for line in f if line.strip())
        
        self.db = DatabaseManager(config.get("db_path", "banc.db"), config.get("db_retention_sec", 86400))
        
        self.active_connections = 0
        self.blocked_ips = {}
        self.pending_identities = []
        self.pending_ips = {}
        
        # Mirror Configurations
        default_m_port = int(config.get("port", 9000) or 9000)
        self.mirrors = _parse_mirrors_list(config.get("mirrors", []), default_port=default_m_port)
        self.mirror_stats = {m: {"status": "PENDING", "errors": 0, "retry_at": 0} for m in self.mirrors}
        
        # Periodic Top 10 Stats 
        self._prev_ident_counts = {}
        self.top_identities = [] # Stores list of (identity, total_count, rate_per_min)
        
        # Controls
        self.put_paused = False
        self.get_paused = False

        # Stats
        self.packets_in = 0
        self.packets_out = 0
        self.start_time = time.time()

        # UI & Layout Management View State
        self.ui_view = "telemetry"  # "telemetry", "layouts", or "chat"
        self.status_message = ""
        self.status_message_ts = 0.0

        # Chat Relay, TUI Scrolling List & /tmp Audit Log State
        try:
            chat_hist_len = max(50, int(config.get("chat_history_limit", 1000) or 1000))
        except (ValueError, TypeError):
            chat_hist_len = 1000
        self.chat_messages: deque = deque(maxlen=chat_hist_len)
        self._chat_seq: int = 0
        self.chat_scroll_offset: int = 0
        self.chat_tui_rows: int = 6
        self.chat_subscribers: Dict[str, List[Any]] = {}
        self._chat_rate_windows: Dict[str, deque] = {}
        self._seen_chat_relay_uids: deque = deque(maxlen=2000)
        self._seen_chat_relay_uid_set: set = set()
        self._chat_log_lock = threading.Lock()
        # Anti-triangulation geo-query state per identity: {ident: (last_q_point, switch_ts_deque)}
        self._geo_query_guard: Dict[str, Tuple[Tuple[float, float], deque]] = {}

        default_log_dir = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        cfg_log_path = config.get("chat_log_path") or config.get("chat_log_file")
        if cfg_log_path:
            self.chat_log_filepath = str(cfg_log_path)
        else:
            cfg_log_dir = config.get("chat_log_dir") or default_log_dir
            self.chat_log_filepath = os.path.join(str(cfg_log_dir), "apexbosbanc_chat.log")
        try:
            log_parent = os.path.dirname(os.path.abspath(self.chat_log_filepath))
            if log_parent:
                os.makedirs(log_parent, exist_ok=True)
        except Exception:
            pass

    def block_ip(self, ip: str, identity: str = "unknown", reason: str = "Authentication failed", duration: float = 60.0):
        """Places an IP in cooldown with identity and reason tracking."""
        norm_ip = normalize_ip(ip)
        expiry = time.time() + max(1.0, float(duration))
        info = BlockedIPInfo(
            expiry=expiry,
            identity=identity,
            reason=reason,
            blocked_at=time.time(),
        )
        self.blocked_ips[norm_ip] = info
        if ip != norm_ip:
            self.blocked_ips[ip] = info

    def unblock_ip(self, ip: str) -> bool:
        """Cancels cooldown for a specific IP. Returns True if unblocked, False if not found."""
        target = normalize_ip(ip)
        removed = False
        for k in list(self.blocked_ips.keys()):
            if k == target or normalize_ip(k) == target or k == ip:
                del self.blocked_ips[k]
                removed = True
        return removed

    def unblock_all(self) -> int:
        """Cancels cooldown for all blocked IPs. Returns count of unblocked IPs."""
        unique_ips = set(normalize_ip(k) for k in self.blocked_ips.keys())
        count = len(unique_ips)
        self.blocked_ips.clear()
        return count

    def get_cooldown_stats(self) -> List[dict]:
        """Returns list of active cooldown records with identity, reason, and time remaining."""
        now = time.time()
        active = []
        seen = set()
        for ip, info in list(self.blocked_ips.items()):
            exp = float(info) if not isinstance(info, dict) else info.get("expiry", 0.0)
            if now >= exp:
                del self.blocked_ips[ip]
                continue
            norm = normalize_ip(ip)
            if norm in seen:
                continue
            seen.add(norm)
            rem = max(0.0, exp - now)
            ident = info.get("identity", "unknown") if isinstance(info, dict) else "unknown"
            rsn = info.get("reason", "Authentication failed") if isinstance(info, dict) else "Authentication failed"
            bl_at = info.get("blocked_at", exp - 60.0) if isinstance(info, dict) else (exp - 60.0)
            active.append({
                "ip": norm,
                "identity": ident,
                "reason": rsn,
                "remaining_sec": rem,
                "remaining": rem,
                "blocked_at": bl_at,
                "expiry": exp,
            })
        active.sort(key=lambda x: x["remaining_sec"])
        return active

    def execute_ui_command(self, raw_text: str):
        return execute_ui_command(self, raw_text)

    async def client_handler(self, reader, writer):
        return await client_handler(reader, writer, self)

    def _check_chat_rate_limit(self, identity: str, max_per_window: int = 60, window_sec: float = 10.0) -> bool:
        now = time.time()
        q = self._chat_rate_windows.setdefault(identity, deque(maxlen=max_per_window + 5))
        while q and (now - q[0]) > window_sec:
            q.popleft()
        if len(q) >= max_per_window:
            return False
        q.append(now)
        return True

    def _append_chat_to_log_file(self, msg_record: dict, relayed_from_mirror: bool = False):
        """Appends every chat message passing through Banc to the /tmp chat log file."""
        try:
            date_str = str(msg_record.get("date_str") or "").replace("\r", "").replace("\n", "")
            mid = msg_record.get("msg_id", 0)
            sender = str(msg_record.get("sender_identity") or "Unknown").replace("\r", "").replace("\n", "")
            targets = msg_record.get("target_identities") or []
            to_str = ",".join(f"@{t}" for t in targets) if targets else "ALL"
            text_val = str(msg_record.get("text") or "").replace("\r", " ").replace("\n", " ")
            src_tag = "MIRROR" if relayed_from_mirror else "CLIENT"
            log_line = f"[{date_str} UTC] [{src_tag}] #{mid} {sender} -> {to_str}: {text_val}\n"
            with self._chat_log_lock:
                with open(self.chat_log_filepath, "a", encoding="utf-8") as f:
                    f.write(log_line)
                    f.flush()
        except Exception:
            pass

    def _chat_tui_window_size(self) -> int:
        return 18 if self.ui_view == "chat" else max(1, int(getattr(self, "chat_tui_rows", 6) or 6))

    def scroll_chat_up(self, step: Optional[int] = None):
        win = self._chat_tui_window_size()
        delta = step if step is not None else max(1, win - 1)
        msgs = getattr(self, "chat_messages", ())
        cur_off = int(getattr(self, "chat_scroll_offset", 0) or 0)
        max_off = max(0, len(msgs) - win)
        self.chat_scroll_offset = min(max_off, max(0, cur_off + max(1, int(delta))))

    def scroll_chat_down(self, step: Optional[int] = None):
        win = self._chat_tui_window_size()
        delta = step if step is not None else max(1, win - 1)
        cur_off = int(getattr(self, "chat_scroll_offset", 0) or 0)
        self.chat_scroll_offset = max(0, cur_off - max(1, int(delta)))

    def scroll_chat_top(self):
        win = self._chat_tui_window_size()
        msgs = getattr(self, "chat_messages", ())
        self.chat_scroll_offset = max(0, len(msgs) - win)

    def scroll_chat_bottom(self):
        self.chat_scroll_offset = 0

    def record_chat_message(
        self,
        sender_identity: str,
        raw_payload: Any,
        relayed_from_mirror: bool = False,
    ) -> dict:
        """
        Validates, sanitizes, logs to /tmp, and records an incoming chat message from an authenticated identity.
        If @<identity> is present in the message, marks target_identities so Banc relays it ONLY
        to the Engine(s) with matching identity.
        """
        clean_sender = sanitize_chat_identity(sender_identity) or "Unknown"
        if not self._check_chat_rate_limit(clean_sender):
            raise ValueError("RATE_LIMIT:Chat rate limit exceeded")

        if isinstance(raw_payload, bytes):
            if len(raw_payload) > MAX_CHAT_PAYLOAD_BYTES:
                raise ValueError("OVERFLOW:Chat payload exceeds maximum byte limit")
            raw_payload = raw_payload.decode("utf-8", errors="ignore")
        elif isinstance(raw_payload, str):
            if len(raw_payload.encode("utf-8")) > MAX_CHAT_PAYLOAD_BYTES:
                raise ValueError("OVERFLOW:Chat payload exceeds maximum byte limit")

        explicit_targets: List[str] = []
        raw_text = ""
        incoming_uid = ""
        parsed_dict: Optional[dict] = None
        if isinstance(raw_payload, dict):
            parsed_dict = raw_payload
        elif isinstance(raw_payload, str):
            s_strip = raw_payload.strip()
            if s_strip.startswith("{"):
                try:
                    cand = json.loads(s_strip)
                    if isinstance(cand, dict):
                        parsed_dict = cand
                    else:
                        raw_text = s_strip
                except Exception:
                    raw_text = s_strip
            else:
                raw_text = s_strip
        else:
            raw_text = str(raw_payload or "")

        if parsed_dict is not None:
            raw_text = str(parsed_dict.get("text", ""))
            raw_t = parsed_dict.get("target_identities") or parsed_dict.get("targets") or []
            if isinstance(raw_t, str):
                raw_t = [raw_t]
            if isinstance(raw_t, list):
                explicit_targets = [sanitize_chat_identity(x) for x in raw_t if sanitize_chat_identity(x)]
            incoming_uid = sanitize_chat_identity(parsed_dict.get("_relay_uid") or "")

        if incoming_uid and incoming_uid in self._seen_chat_relay_uid_set:
            return {
                "msg_id": 0,
                "timestamp": time.time(),
                "date_str": datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "sender_identity": clean_sender,
                "target_identities": [],
                "text": "",
                "relay_uid": incoming_uid,
                "_duplicate": True,
            }

        if len(raw_text) > MAX_CHAT_PAYLOAD_BYTES:
            raise ValueError("OVERFLOW:Chat message text too large")

        clean_text = sanitize_chat_text(raw_text, max_len=MAX_CHAT_MESSAGE_LEN)
        if not clean_text:
            raise ValueError("EMPTY:Chat message is empty after sanitization")

        mentions = extract_chat_mentions(clean_text)
        targets: List[str] = []
        for t in explicit_targets + mentions:
            st = sanitize_chat_identity(t)
            if st and st not in targets:
                targets.append(st)

        now_ts = time.time()
        date_str = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._chat_seq += 1
        relay_uid = incoming_uid or uuid.uuid4().hex
        if len(self._seen_chat_relay_uids) == self._seen_chat_relay_uids.maxlen:
            oldest_uid = self._seen_chat_relay_uids[0]
            self._seen_chat_relay_uid_set.discard(oldest_uid)
        self._seen_chat_relay_uids.append(relay_uid)
        self._seen_chat_relay_uid_set.add(relay_uid)

        msg_record = {
            "msg_id": self._chat_seq,
            "timestamp": now_ts,
            "date_str": date_str,
            "sender_identity": clean_sender,
            "target_identities": targets,
            "text": clean_text,
            "relay_uid": relay_uid,
            "relayed_from_mirror": bool(relayed_from_mirror),
        }
        self.chat_messages.append(msg_record)
        self._append_chat_to_log_file(msg_record, relayed_from_mirror=relayed_from_mirror)
        return msg_record

    def get_chat_for_identity(
        self,
        requesting_identity: str,
        since_id: int = 0,
        since_ts: float = 0.0,
    ) -> List[dict]:
        """
        Returns chat messages visible to requesting_identity.
        If @<identity> is present in a message (target_identities is non-empty),
        relays that message ONLY to the Engine with matching identity.
        Broadcast messages (empty target_identities) are relayed to all connected clients.
        """
        req_id = sanitize_chat_identity(requesting_identity)
        req_lower = req_id.lower()
        results: List[dict] = []
        for msg in list(self.chat_messages):
            mid = int(msg.get("msg_id", 0) or 0)
            mts = float(msg.get("timestamp", 0.0) or 0.0)
            if since_id > 0:
                if mid <= since_id:
                    continue
            elif since_ts > 0.0:
                if mts <= since_ts:
                    continue

            targets = msg.get("target_identities") or []
            if targets:
                target_lowers = [str(t).strip().lower() for t in targets if str(t).strip()]
                if req_lower not in target_lowers:
                    continue
            results.append(dict(msg))
        return results

    async def broadcast_chat_to_subscribers(self, msg_record: dict):
        """Pushes an encrypted chat message frame to active STREAM_CHAT subscribers (addressed or all)."""
        if not self.chat_subscribers:
            return
        targets = msg_record.get("target_identities") or []
        target_lowers = {str(t).strip().lower() for t in targets if str(t).strip()}
        payload_str = json.dumps({"ok": True, "messages": [msg_record]})

        for sub_ident, writers in list(self.chat_subscribers.items()):
            if target_lowers and sub_ident.strip().lower() not in target_lowers:
                continue
            dead_writers = []
            for w in list(writers):
                try:
                    await write_frame(w, self.fernet, payload_str)
                except Exception:
                    dead_writers.append(w)
            for dw in dead_writers:
                if dw in writers:
                    writers.remove(dw)

    def set_status_message(self, msg: str, duration: float = 5.0):
        self.status_message = msg
        self.status_message_ts = time.time() + duration

    def add_whitelist(self, identity):
        """Adds an identity to the whitelist, saves it, and removes active IP blocks."""
        if identity not in self.whitelist:
            self.whitelist.add(identity)
            with open(self.whitelist_file, 'a') as f:
                f.write(f"{identity}\n")
                
        if identity in self.pending_identities:
            self.pending_identities.remove(identity)
            
        ip_to_unblock = self.pending_ips.pop(identity, None)
        if ip_to_unblock:
            self.unblock_ip(ip_to_unblock)

        # Also unblock any IP currently blocked for this identity
        for blk_ip, info in list(self.blocked_ips.items()):
            if isinstance(info, dict) and info.get("identity") == identity:
                self.unblock_ip(blk_ip)

    def get_packet_rate(self):
        uptime = (time.time() - self.start_time) / 60.0
        return self.packets_in / uptime if uptime > 0 else 0

    def col(self, label, val, color, width):
        """Helper to ensure exact printable width regardless of ANSI codes."""
        val_str = str(val)
        pad_len = max(0, width - len(label))
        return f"{label}{color}{val_str:<{pad_len}}{C_RESET}"

    # --- Relay Engine ---
    async def relay_payload(self, header_str, payload_str, expect_ack: bool = False):
        """Dispatches PUT / PUT_WIUS / PUT_CHAT payload to all active mirrors concurrently."""
        tasks = []
        for m in self.mirrors:
            st = self.mirror_stats.setdefault(m, {"status": "PENDING", "errors": 0, "retry_at": 0})
            if time.time() >= st["retry_at"]:
                tasks.append(self._relay_single(m, header_str, payload_str, expect_ack=expect_ack))
        if tasks:
            await asyncio.gather(*tasks)

    async def _relay_single(self, mirror, header_str, payload_str, expect_ack: bool = False):
        try:
            host, port_s = mirror.rsplit(":", 1)
            host = host.strip("[]")
            port = int(port_s)

            # Reconstruct the Dual-Frame packet
            enc_header = self.fernet.encrypt(header_str.encode('utf-8'))
            enc_payload = self.fernet.encrypt(payload_str.encode('utf-8'))
            final_tx = struct.pack(">I", len(enc_header)) + enc_header + struct.pack(">I", len(enc_payload)) + enc_payload

            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)
            try:
                writer.write(final_tx)
                await writer.drain()
                if expect_ack:
                    try:
                        await asyncio.wait_for(reader.read(4096), timeout=2.0)
                    except (asyncio.TimeoutError, EOFError, OSError):
                        pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

            self.mirror_stats[mirror]["status"] = "ACTIVE"
            self.mirror_stats[mirror]["retry_at"] = 0
        except Exception:
            self.mirror_stats[mirror]["status"] = "GRACE"
            self.mirror_stats[mirror]["errors"] += 1
            self.mirror_stats[mirror]["retry_at"] = time.time() + 60

    def _format_chat_tui_rows(self, window: int) -> Tuple[List[str], int, int, int]:
        """Returns formatted ANSI lines for the scrolling chat message window."""
        msgs = list(getattr(self, "chat_messages", ()))
        total = len(msgs)
        max_off = max(0, total - window)
        cur_off = int(getattr(self, "chat_scroll_offset", 0) or 0)
        self.chat_scroll_offset = max(0, min(max_off, cur_off))
        end_idx = total - self.chat_scroll_offset
        start_idx = max(0, end_idx - window)
        visible = msgs[start_idx:end_idx]

        rows_out: List[str] = []
        for m in visible:
            date_s = str(m.get("date_str") or "")
            t_hms = date_s.split(" ")[-1] if " " in date_s else date_s[-8:]
            sender = str(m.get("sender_identity") or "Unknown")[:14]
            targets = m.get("target_identities") or []
            if targets:
                to_s = "@" + ",".join(str(t) for t in targets)
                if len(to_s) > 14:
                    to_s = to_s[:13] + "+"
                to_col = C_YELLOW
            else:
                to_s = "ALL"
                to_col = C_CYAN
            txt = str(m.get("text") or "").replace("\r", " ").replace("\n", " ")
            if len(txt) > 54:
                txt = txt[:51] + "..."
            src_badge = f"{C_DIM}[M]{C_RESET}" if m.get("relayed_from_mirror") else "   "
            rows_out.append(
                f"  {C_DIM}{t_hms:<8}{C_RESET} {src_badge} {C_GREEN}{sender:<14}{C_RESET} -> {to_col}{to_s:<14}{C_RESET} {C_WHITE}{txt}{C_RESET}"
            )
        return rows_out, total, start_idx, end_idx

    # --- GUI Renderer ---
    def generate_ascii_screen(self, current_buffer: str = ''):
        log_path = getattr(self, "chat_log_filepath", "/tmp/apexbosbanc_chat.log")
        # Chat Messages Full Scrolling View
        if self.ui_view == "chat":
            win = 18
            chat_rows, total, start_idx, end_idx = self._format_chat_tui_rows(win)
            range_txt = f"{start_idx + 1}-{end_idx} of {total}" if total > 0 else "0 of 0"
            lines = [
                f"{BG_BANNER}  ApexBOSBanc Cloud Receiver ({VERSION}) - Chat Messages ({range_txt})  {C_RESET}",
                f"  {C_BOLD}Audit Log:{C_RESET} {C_CYAN}{log_path}{C_RESET}",
                f"  {C_BOLD}{C_CYAN}{'Time UTC':<8}     {'From':<14}    {'To':<14} {'Message'}{C_RESET}",
                f"  {C_DIM}{'-'*84}{C_RESET}",
            ]
            if not chat_rows:
                lines.append(f"  {C_YELLOW}No chat messages relayed through Banc yet.{C_RESET}")
            else:
                lines.extend(chat_rows)

            if self.status_message and time.time() < self.status_message_ts:
                lines.append("")
                lines.append(f"  {BG_PAUSED} {self.status_message[:75]} {C_RESET}")

            lines.append("")
            ctrl = "[[]/PgUp] Older   []/PgDn] Newer   [Top]/[End]   [M] Back"
            lines.append(f"{BG_BANNER}  {ctrl}  {C_RESET}")
            lines.append(f"  {C_BOLD}{C_YELLOW}Command > {C_RESET}{current_buffer}")
            return "\r\n".join(lines) + "\r\n"

        # Layout Management View
        if self.ui_view == "layouts":
            lines = [
                f"{BG_BANNER}  ApexBOSBanc Cloud Receiver ({VERSION}) - Remote Layout Management  {C_RESET}",
                ""
            ]
            layouts = self.db.list_layouts(include_unavailable=True)
            hdr = f"  {C_BOLD}{C_CYAN}{'#':<3}  {'Layout Name':<25}  {'Center GPS (Lat, Lon)':<24}  {'Radius':<11}  {'Status':<12}{C_RESET}"
            lines.append(hdr)
            lines.append(f"  {C_DIM}{'-'*79}{C_RESET}")
            if not layouts:
                lines.append(f"  {C_YELLOW}No stored layouts on cloud server.{C_RESET}")
            else:
                for idx, lay in enumerate(layouts[:14]):
                    name = lay['name'][:25]
                    if lay['center_lat'] is not None and lay['center_lon'] is not None:
                        gps_str = f"{lay['center_lat']:.4f}, {lay['center_lon']:.4f}"
                    else:
                        gps_str = "No GPS info"
                    rad_str = f"{lay['radius_km']:.2f} km" if lay['radius_km'] > 0 else "0.00 km"
                    if lay['is_available']:
                        st_badge = f"{BG_AVAIL} AVAILABLE {C_RESET}"
                    else:
                        st_badge = f"{BG_UNAVAIL} UNAVAIL  {C_RESET}"
                    lines.append(f"  {C_DIM}{idx:<3}{C_RESET}  {C_WHITE}{name:<25}{C_RESET}  {C_CYAN}{gps_str:<24}{C_RESET}  {C_YELLOW}{rad_str:<11}{C_RESET}  {st_badge}")

            if self.status_message and time.time() < self.status_message_ts:
                lines.append("")
                lines.append(f"  {BG_PAUSED} {self.status_message[:75]} {C_RESET}")

            lines.append("")
            ctrl = "[0-9] Toggle Avail   [D#] Delete   [A#] Avail   [U#] Unavail   [L] Back"
            lines.append(f"{BG_BANNER}  {ctrl}  {C_RESET}")
            lines.append(f"  {C_BOLD}{C_YELLOW}Command > {C_RESET}{current_buffer}")
            return "\r\n".join(lines) + "\r\n"

        # Standard Telemetry View
        lines = [
            f"{BG_BANNER}  ApexBOSBanc Cloud Receiver ({VERSION}) - Status & Telemetry  {C_RESET}",
            ""
        ]
        
        if self.auth_secret:
            current_token = mosfet_auth.generate_token(self.auth_secret)
            # Calculate remaining time in the 10-minute (600s) window
            time_rem = 600 - (int(time.time()) % 600)
            rem_m, rem_s = divmod(time_rem, 60)
            token_display = f"{current_token} (changes in {rem_m:02d}:{rem_s:02d})"
        else:
            token_display = "N/A"
            
        lines.append(f"  {C_BOLD}Auth Token:{C_RESET}     {C_GREEN}{token_display}{C_RESET}")

        rate_val = f"{self.get_packet_rate():.2f}"
        lines.append(f"  {C_BOLD}Active Conns:{C_RESET}   {C_GREEN}{self.active_connections:<12}{C_RESET}   {C_BOLD}Total Pkt/Min:{C_RESET} {C_CYAN}{rate_val}{C_RESET}")
        
        rows, size_kb = self.db.get_stats()
        proto_counts = self.db.get_protocol_stats()
        atcs_cnt = proto_counts.get(PKT_PROTO_ATCS, 0)
        fred_cnt = proto_counts.get(PKT_PROTO_FRED, 0)
        proto_suffix = (
            f"  {C_DIM}(PTC:{proto_counts.get(PKT_PROTO_PTC, 0)} ATCS:{atcs_cnt} FRED:{fred_cnt}){C_RESET}"
            if (atcs_cnt or fred_cnt)
            else ""
        )
        lines.append(f"  {C_BOLD}DB Rows:{C_RESET}        {C_GREEN}{rows:<12}{C_RESET}   {C_BOLD}DB Size:{C_RESET}       {C_YELLOW}{size_kb:.2f} KB{C_RESET}{proto_suffix}")
        
        p_badge = f"{BG_PAUSED} PAUSED {C_RESET}" if self.put_paused else f"{BG_ACTIVE} ACTIVE {C_RESET}"
        g_badge = f"{BG_PAUSED} PAUSED {C_RESET}" if self.get_paused else f"{BG_ACTIVE} ACTIVE {C_RESET}"
        lines.append(f"  {C_BOLD}PUT Service:{C_RESET}    {p_badge}        {C_BOLD}GET Service:{C_RESET}   {g_badge}")
        
        # Mirror Display
        if self.mirrors:
            lines.append("")
            lines.append(f"{BG_BANNER}  Active Mirror Relays  {C_RESET}")
            for m in self.mirrors:
                stat = self.mirror_stats[m]
                if stat['status'] == 'ACTIVE':
                    s_badge = f"{BG_ACTIVE} ACTIVE {C_RESET}"
                elif stat['status'] == 'PENDING':
                    s_badge = f"{BG_PAUSED} PENDING {C_RESET}"
                else:
                    s_badge = f"{BG_UNAVAIL} {stat['status']} {C_RESET}"
                
                err_txt = f"{C_RED}{stat['errors']}{C_RESET}" if stat['errors'] > 0 else f"{C_GREEN}0{C_RESET}"
                lines.append(f"  {C_CYAN}{m[:28]:<28}{C_RESET} {s_badge}  Errors: {err_txt}")

        # Top 10 Identities Table
        lines.append("")
        lines.append(f"{BG_BANNER}  Top 10 Active Identities  {C_RESET}")
        lines.append(f"  {C_BOLD}{C_CYAN}{'Identity':<32}  {'Total Packets':<20}  {'Packets/Min':<20}{C_RESET}")
        if not self.top_identities:
            lines.append(f"  {C_YELLOW}Calculating stats...{C_RESET}")
        else:
            for ident, total, rate in self.top_identities:
                lines.append(f"  {C_GREEN}{ident:<32}{C_RESET}  {C_CYAN}{total:<20}{C_RESET}  {C_CYAN}{rate:<20.2f}{C_RESET}")
        
        w_list = ", ".join(self.whitelist) if self.whitelist else "None"
        lines.append("")
        lines.append(f"  {C_BOLD}Whitelisted:{C_RESET}  {C_GREEN}{w_list[:64]}{C_RESET}")
        
        if self.pending_identities:
            lines.append("")
            lines.append(f"{BG_BANNER}  Pending Identities  {C_RESET}")
            for i, ident in enumerate(self.pending_identities[:5]):
                lines.append(f"  [{i}] {C_YELLOW}{ident}{C_RESET}")

        # Cooldown / Blocked IPs Display with Identity & Reason Stats
        cd_stats = self.get_cooldown_stats()
        if cd_stats:
            lines.append("")
            lines.append(f"{BG_PAUSED}  Active Cooldowns ({len(cd_stats)} Blocked IP{'s' if len(cd_stats) != 1 else ''}) - [U] Unblock All | [U <IP>] Unblock IP  {C_RESET}")
            lines.append(f"  {C_BOLD}{C_CYAN}{'IP Address':<20}  {'Identity':<18}  {'Reason':<32}  {'Remaining'}{C_RESET}")
            for item in cd_stats[:5]:
                ip_str = item["ip"][:20]
                ident_str = item["identity"][:18]
                rsn_str = item["reason"][:32]
                rem_str = f"{item['remaining_sec']:.0f}s"
                lines.append(f"  {C_RED}{ip_str:<20}{C_RESET}  {C_YELLOW}{ident_str:<18}{C_RESET}  {C_WHITE}{rsn_str:<32}{C_RESET}  {C_CYAN}{rem_str}{C_RESET}")
            b_list = f"{len(cd_stats)} IP(s) in cooldown ([U] to cancel)"
        else:
            b_list = "None"
        lines.append(f"  {C_BOLD}DoS Blocked:{C_RESET}  {C_RED if cd_stats else C_GREEN}{b_list}{C_RESET}")

        # Scrolling Chat Messages List (All messages passing through Banc)
        win = max(1, int(getattr(self, "chat_tui_rows", 6) or 6))
        chat_rows, chat_total, c_start, c_end = self._format_chat_tui_rows(win)
        c_range = f"{c_start + 1}-{c_end}/{chat_total}" if chat_total > 0 else "0/0"
        lines.append("")
        lines.append(f"{BG_BANNER}  Chat Messages ({c_range})  Log: {log_path}  {C_RESET}")
        if not chat_rows:
            lines.append(f"  {C_DIM}No chat messages relayed yet.{C_RESET}")
        else:
            lines.extend(chat_rows)
        
        if self.status_message and time.time() < self.status_message_ts:
            lines.append("")
            lines.append(f"  {BG_PAUSED} {self.status_message[:75]} {C_RESET}")
        lines.append("")
        ctrl = "[P] PUT  [G] GET  [L] Layouts  [M] Chat  [U] Unblock  [[]/[]] Scroll  [0-9] Auth  [C] Clear"
        lines.append(f"{BG_BANNER}  {ctrl}  {C_RESET}")
        lines.append(f"  {C_BOLD}{C_YELLOW}Command > {C_RESET}{current_buffer}")
        return "\r\n".join(lines) + "\r\n"

# ---------------------------------------------------------------------------
# Background Stats Loop
# ---------------------------------------------------------------------------
async def identity_stats_loop(server: BancServer):
    """Calculates individual packets and packets/min over configured periods."""
    interval = server.config.get("stats_interval_sec", 300)
    loop = asyncio.get_running_loop()
    
    # Run an initial non-blocking tally on startup
    stats = await loop.run_in_executor(None, server.db.get_identity_stats)
    new_top = []
    for ident, count in stats:
        server._prev_ident_counts[ident] = count
        new_top.append((ident, count, 0.0))
    new_top.sort(key=lambda x: x[1], reverse=True)
    server.top_identities = new_top[:10]

    while True:
        await asyncio.sleep(interval)
        
        stats = await loop.run_in_executor(None, server.db.get_identity_stats)
        new_top = []
        for ident, count in stats:
            prev_count = server._prev_ident_counts.get(ident, 0)
            
            # Compute packets per minute rate for this specific identity
            rate = max(0, count - prev_count) / (interval / 60.0)
            
            server._prev_ident_counts[ident] = count
            new_top.append((ident, count, rate))
        
        new_top.sort(key=lambda x: x[1], reverse=True)
        server.top_identities = new_top[:10]

# ---------------------------------------------------------------------------
# Network Protocol Handlers
# ---------------------------------------------------------------------------
async def read_frame(reader, fernet):
    length_bytes = await reader.readexactly(4)
    frame_len = struct.unpack(">I", length_bytes)[0]
    if frame_len > 10 * 1024 * 1024:
        raise ValueError("Frame too large")
    enc_data = await reader.readexactly(frame_len)
    return fernet.decrypt(enc_data).decode('utf-8')

async def write_frame(writer, fernet, data_str):
    enc_data = fernet.encrypt(data_str.encode('utf-8'))
    writer.write(struct.pack(">I", len(enc_data)) + enc_data)
    await writer.drain()

async def client_handler(reader, writer, server: BancServer):
    peer = writer.get_extra_info('peername')
    ip = normalize_ip(peer[0]) if peer else "unknown"
    server.active_connections += 1
    
    try:
        # 1. Check DoS / Cooldown Timer
        if ip in server.blocked_ips:
            info = server.blocked_ips[ip]
            exp = float(info) if not isinstance(info, dict) else info.get("expiry", 0.0)
            if time.time() < exp:
                return # Silently drop
            else:
                server.unblock_ip(ip)

        # 2. Read Header Frame (Fast decryption)
        try:
            header = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=5.0)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            return
        except Exception:
            server.block_ip(ip, identity="unknown", reason="Header decryption failed (invalid Fernet key or framing error)")
            return

        parts = header.split(":")
        if len(parts) < 3:
            server.block_ip(ip, identity="unknown", reason="Malformed frame header (< 3 fields)")
            return
            
        action, identity, token = parts[0], parts[1], parts[2]
        extra = parts[3:]

        # Check Service Status
        if action == "PUT" and server.put_paused:
            return
        if action == "GET" and server.get_paused:
            return

        # 3. Authenticate Identity & Token
        if identity not in server.whitelist:
            if identity not in server.pending_identities:
                server.pending_identities.append(identity)
            server.pending_ips[identity] = ip 
            server.block_ip(ip, identity=identity, reason=f"Identity '{identity}' not in whitelist.txt")
            return

        valid_tokens = mosfet_auth.get_valid_tokens(server.auth_secret)
        if token not in valid_tokens:
            server.block_ip(ip, identity=identity, reason="Invalid auth token (auth_secret mismatch)")
            return

        # 4. Handle Service Request
        if action == "PUT":
            payload = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=30.0)
            rows = []
            for line in payload.split("\n"):
                if not line.strip(): continue
                cols = line.split(",")
                if len(cols) == 10:
                    try:
                        b2_norm, b3_norm = normalize_packet_proto(cols[8], cols[9])
                        tup = (cols[0], float(cols[1]), cols[2], cols[3], int(cols[4]), cols[5], int(cols[6]), float(cols[7]), b2_norm, b3_norm, time.time())
                        rows.append(tup)
                    except ValueError:
                        pass
            
            if rows:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, server.db.insert_rows, rows)
                server.packets_in += len(rows)
                
                # Relay to Mirrors
                if server.mirrors:
                    asyncio.create_task(server.relay_payload(header, payload))

        elif action == "GET":
            since_ts = None
            geo_filter = None
            if extra:
                try:
                    raw_since = float(extra[0])
                    since_ts = raw_since if raw_since > 0.0 else None
                except (ValueError, TypeError):
                    since_ts = None

                # Optional regional geo-filter for short-range RF telemetry (FRED):
                #   Format A: GET:<id>:<tok>:<since_ts>:<lat>:<lon>:<radius_km>
                #   Format B: GET:<id>:<tok>:<since_ts>:LAYOUT:<layout_name>
                if len(extra) >= 3 and str(extra[1]).strip().upper() == "LAYOUT":
                    l_name = ":".join(extra[2:]).strip()
                    if l_name:
                        loop = asyncio.get_running_loop()
                        l_row = await loop.run_in_executor(None, server.db.get_layout, l_name)
                        if l_row and l_row.get("center_lat") is not None and l_row.get("center_lon") is not None:
                            geo_filter = (
                                float(l_row["center_lat"]),
                                float(l_row["center_lon"]),
                                max(MIN_GEO_QUERY_RADIUS_KM, float(l_row.get("radius_km") or 0.0)),
                            )
                elif len(extra) >= 4:
                    try:
                        q_lat = float(extra[1])
                        q_lon = float(extra[2])
                        q_rad = float(extra[3])
                        if -85.0 <= q_lat <= 85.0 and -180.0 <= q_lon <= 180.0 and not (q_lat == 0.0 and q_lon == 0.0):
                            geo_filter = (q_lat, q_lon, max(MIN_GEO_QUERY_RADIUS_KM, min(2000.0, q_rad)))
                    except (ValueError, TypeError):
                        geo_filter = None

            if geo_filter is not None:
                q_lat, q_lon = quantize_geo_point(geo_filter[0], geo_filter[1], step_deg=GEO_QUERY_GRID_DEG)
                q_rad = max(MIN_GEO_QUERY_RADIUS_KM, min(2000.0, float(geo_filter[2])))
                now_g = time.time()
                prev_state = server._geo_query_guard.get(identity)
                if prev_state is not None:
                    prev_pt, sw_times = prev_state
                    while sw_times and (now_g - sw_times[0]) > 30.0:
                        sw_times.popleft()
                    if (q_lat, q_lon) != prev_pt:
                        if len(sw_times) >= GEO_QUERY_MAX_SWITCHES_30S:
                            # Anti-triangulation throttle: hold last quantized point
                            q_lat, q_lon = prev_pt
                        else:
                            sw_times.append(now_g)
                            server._geo_query_guard[identity] = ((q_lat, q_lon), sw_times)
                else:
                    server._geo_query_guard[identity] = ((q_lat, q_lon), deque())
                geo_filter = (q_lat, q_lon, q_rad)

            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(
                None,
                server.db.get_recent_rows,
                5000,
                since_ts,
                identity,
                geo_filter,
            )
            
            out_csv = []
            for r in rows:
                out_csv.append(",".join(str(x) for x in r))
                
            await write_frame(writer, server.fernet, "\n".join(out_csv))
            server.packets_out += len(rows)

        elif action in ("PUT_SDR_REGION", "SDR_REGION_PUT"):
            if server.put_paused:
                await write_frame(writer, server.fernet, "ERROR:PAUSED:PUT service is paused")
                return
            payload = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=15.0)
            try:
                region_payload = json.loads(payload)
            except Exception:
                await write_frame(writer, server.fernet, "ERROR:INVALID_JSON:Region payload is not valid JSON")
                return

            is_mirrored_hop = "MIRROR" in [str(e).strip().upper() for e in extra]
            loop = asyncio.get_running_loop()
            updated = await loop.run_in_executor(
                None,
                server.db.upsert_feeder_regions_payload,
                identity,
                region_payload,
            )
            if updated > 0:
                server.set_status_message(f"Updated {updated} private SDR region(s) for '{identity}'")
                if server.mirrors and not is_mirrored_hop:
                    relay_tok = mosfet_auth.generate_token(server.auth_secret)
                    relay_header = f"PUT_SDR_REGION:{identity}:{relay_tok}:MIRROR"
                    asyncio.create_task(server.relay_payload(relay_header, payload))
            await write_frame(writer, server.fernet, json.dumps({"ok": True, "updated": updated}))

        elif action in ("PUT_LAYOUT", "LAYOUT_PUT"):
            layout_name = ":".join(extra) if extra else "layout.json"
            payload = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=30.0)
            try:
                layout_dict = json.loads(payload)
            except Exception:
                await write_frame(writer, server.fernet, "ERROR:INVALID_JSON:Payload is not valid JSON")
                return

            center_lat, center_lon, radius_km = calculate_layout_gps(layout_dict)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                server.db.save_layout,
                layout_name,
                payload,
                center_lat,
                center_lon,
                radius_km,
                identity,
            )
            resp = {
                "ok": True,
                "status": "OK",
                "name": layout_name,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "radius_km": radius_km,
            }
            await write_frame(writer, server.fernet, json.dumps(resp))
            server.set_status_message(f"Saved layout '{layout_name}' ({radius_km:.1f}km radius)")

        elif action in ("GET_LAYOUTS", "LAYOUT_LIST"):
            loop = asyncio.get_running_loop()
            layouts = await loop.run_in_executor(None, server.db.list_layouts, True)
            await write_frame(writer, server.fernet, json.dumps(layouts))

        elif action in ("GET_LAYOUT", "LAYOUT_GET"):
            layout_name = ":".join(extra)
            loop = asyncio.get_running_loop()
            layout_row = await loop.run_in_executor(None, server.db.get_layout, layout_name)
            if not layout_row:
                await write_frame(writer, server.fernet, f"ERROR:NOT_FOUND:Layout '{layout_name}' not found")
            elif not layout_row.get("is_available", True):
                await write_frame(writer, server.fernet, f"ERROR:UNAVAILABLE:Layout '{layout_name}' is marked unavailable")
            else:
                await write_frame(writer, server.fernet, layout_row["data"])

        elif action in ("PUT_WIUS", "PUT_WIU", "WIUS_PUT"):
            payload = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=30.0)
            try:
                wiu_payload = json.loads(payload)
            except Exception:
                await write_frame(writer, server.fernet, "ERROR:INVALID_JSON:WIU payload is not valid JSON")
                return

            loop = asyncio.get_running_loop()
            upserted = await loop.run_in_executor(None, server.db.upsert_wius, wiu_payload, identity)
            if upserted > 0:
                server.set_status_message(f"Updated {upserted} WIU record(s) from '{identity}'")
                if server.mirrors:
                    asyncio.create_task(server.relay_payload(header, payload))
            await write_frame(writer, server.fernet, json.dumps({"ok": True, "upserted": upserted}))

        elif action in ("GET_WIUS", "GET_WIU", "WIUS_GET"):
            loop = asyncio.get_running_loop()
            wius = await loop.run_in_executor(None, server.db.list_wius)
            if extra:
                target_id = _normalize_wiu_id(":".join(extra))
                matched = [w for w in wius if str(w.get("wiu_id", "")) == target_id]
                await write_frame(writer, server.fernet, json.dumps(matched))
            else:
                await write_frame(writer, server.fernet, json.dumps(wius))

        elif action in ("PUT_CHAT", "SEND_CHAT", "CHAT_PUT"):
            if server.put_paused:
                await write_frame(writer, server.fernet, "ERROR:PAUSED:PUT service is paused")
                return
            payload = await asyncio.wait_for(read_frame(reader, server.fernet), timeout=10.0)
            if len(payload.encode("utf-8")) > MAX_CHAT_PAYLOAD_BYTES:
                await write_frame(writer, server.fernet, "ERROR:OVERFLOW:Chat message exceeds size limit")
                return

            is_mirrored_hop = "MIRROR" in [str(e).strip().upper() for e in extra]
            if not is_mirrored_hop and payload.strip().startswith("{"):
                try:
                    p_chk = json.loads(payload)
                    if isinstance(p_chk, dict) and p_chk.get("_relayed"):
                        is_mirrored_hop = True
                except Exception:
                    pass

            try:
                msg_record = server.record_chat_message(identity, payload, relayed_from_mirror=is_mirrored_hop)
            except ValueError as ve:
                await write_frame(writer, server.fernet, f"ERROR:{ve}")
                return
            except Exception:
                await write_frame(writer, server.fernet, "ERROR:INVALID_CHAT:Invalid chat payload")
                return

            if msg_record.get("_duplicate"):
                await write_frame(writer, server.fernet, json.dumps({"ok": True, "duplicate": True, "message": msg_record}))
                return

            await server.broadcast_chat_to_subscribers(msg_record)
            targets_info = f" -> @{','.join(msg_record['target_identities'])}" if msg_record.get("target_identities") else " (broadcast)"
            src_prefix = "Mirror Chat" if is_mirrored_hop else "Chat"
            server.set_status_message(f"{src_prefix} from '{identity}'{targets_info}")
            if server.mirrors and not is_mirrored_hop:
                relay_tok = mosfet_auth.generate_token(server.auth_secret)
                relay_header = f"PUT_CHAT:{identity}:{relay_tok}:MIRROR"
                relay_payload = json.dumps({
                    "text": msg_record["text"],
                    "target_identities": msg_record["target_identities"],
                    "timestamp": msg_record["timestamp"],
                    "_relayed": True,
                    "_relay_uid": msg_record.get("relay_uid", ""),
                })
                asyncio.create_task(server.relay_payload(relay_header, relay_payload, expect_ack=True))
            await write_frame(writer, server.fernet, json.dumps({"ok": True, "message": msg_record}))

        elif action in ("GET_CHAT", "CHAT_GET"):
            if server.get_paused:
                await write_frame(writer, server.fernet, json.dumps({"ok": True, "messages": []}))
                return
            since_id = 0
            since_ts = 0.0
            if extra:
                raw_extra = ":".join(extra).strip()
                if "|" in raw_extra:
                    p_id, p_ts = raw_extra.split("|", 1)
                    try:
                        since_id = int(p_id.strip() or 0)
                    except ValueError:
                        since_id = 0
                    try:
                        since_ts = float(p_ts.strip() or 0.0)
                    except ValueError:
                        since_ts = 0.0
                else:
                    try:
                        since_id = int(raw_extra)
                    except ValueError:
                        try:
                            since_ts = float(raw_extra)
                        except ValueError:
                            pass
            msgs = server.get_chat_for_identity(identity, since_id=since_id, since_ts=since_ts)
            await write_frame(writer, server.fernet, json.dumps({"ok": True, "messages": msgs}))

        elif action in ("STREAM_CHAT", "CHAT_STREAM"):
            sub_list = server.chat_subscribers.setdefault(identity, [])
            sub_list.append(writer)
            try:
                # Send initial recent messages for this identity
                init_msgs = server.get_chat_for_identity(identity)
                await write_frame(writer, server.fernet, json.dumps({"ok": True, "messages": init_msgs}))
                while True:
                    data = await reader.read(64)
                    if not data:
                        break
            finally:
                if writer in sub_list:
                    sub_list.remove(writer)

    except Exception:
        pass 
    finally:
        server.active_connections -= 1
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def db_cleanup_loop(server: BancServer):
    while True:
        await asyncio.sleep(300)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, server.db.cleanup_old)

# ---------------------------------------------------------------------------
# UI Routines
# ---------------------------------------------------------------------------
def execute_ui_command(server: BancServer, raw_text: str):
    raw_text = raw_text.strip()
    if not raw_text:
        return
    char = raw_text.lower()

    if char == 'l':
        server.ui_view = "telemetry" if server.ui_view == "layouts" else "layouts"
        server.set_status_message(f"Switched view to {server.ui_view.upper()}")
        return

    if char == 'm':
        server.ui_view = "telemetry" if server.ui_view == "chat" else "chat"
        server.set_status_message(f"Switched view to {server.ui_view.upper()}")
        return

    if char in ('[', 'pgup', 'pageup', 'scrollup'):
        server.scroll_chat_up()
        return
    if char in (']', 'pgdn', 'pagedown', 'scrolldown'):
        server.scroll_chat_down()
        return
    if char == 'up':
        server.scroll_chat_up(1)
        return
    if char == 'down':
        server.scroll_chat_down(1)
        return
    if char in ('top', 'home'):
        server.scroll_chat_top()
        return
    if char in ('bottom', 'end'):
        server.scroll_chat_bottom()
        return

    # Check for unblock / cooldown cancellation commands
    m_unb = re.match(r'^(?:unblock|u)\s*(.+)$', raw_text, re.IGNORECASE)
    if m_unb:
        target = m_unb.group(1).strip()
        if target.lower() in ("all", "*"):
            cnt = server.unblock_all()
            server.set_status_message(f"Unblocked all {cnt} IP(s) from cooldown")
            return
        else:
            if server.unblock_ip(target):
                server.set_status_message(f"Unblocked IP '{target}' from cooldown")
            else:
                server.set_status_message(f"IP '{target}' not found in cooldown")
            return

    if char in ('unblock', 'unblock all', 'clear cooldown'):
        cnt = server.unblock_all()
        server.set_status_message(f"Unblocked all {cnt} IP(s) from cooldown")
        return

    if char in ('cooldown', 'blocked', 'cooldowns'):
        stats = server.get_cooldown_stats()
        if not stats:
            server.set_status_message("No IPs currently in cooldown")
        else:
            server.set_status_message(f"{len(stats)} IP(s) in cooldown. Type 'u all' or 'u <ip>' to cancel")
        return

    if server.ui_view == "layouts":
        layouts = server.db.list_layouts(include_unavailable=True)

        # Check for delete: "d0", "d 0", "del 0", "delete <name>"
        m_del = re.match(r'^(?:d|del|delete)\s*(\S+)$', char)
        if m_del:
            target = m_del.group(1)
            if target.isdigit() and int(target) < len(layouts):
                name = layouts[int(target)]["name"]
            else:
                name = target
            if server.db.delete_layout(name):
                server.set_status_message(f"Deleted layout '{name}'")
            else:
                server.set_status_message(f"Layout '{name}' not found")
            return

        # Check for mark available: "a0", "a 0", "avail <target>"
        m_avail = re.match(r'^(?:a|avail|available)\s*(\S+)$', char)
        if m_avail:
            target = m_avail.group(1)
            name = layouts[int(target)]["name"] if target.isdigit() and int(target) < len(layouts) else target
            if server.db.set_layout_availability(name, True):
                server.set_status_message(f"Layout '{name}' marked [AVAILABLE]")
            else:
                server.set_status_message(f"Layout '{name}' not found")
            return

        # Check for mark unavailable: "u0", "u 0", "unavail <target>"
        m_unavail = re.match(r'^(?:u|unavail|unavailable)\s*(\S+)$', char)
        if m_unavail:
            target = m_unavail.group(1)
            name = layouts[int(target)]["name"] if target.isdigit() and int(target) < len(layouts) else target
            if server.db.set_layout_availability(name, False):
                server.set_status_message(f"Layout '{name}' marked [UNAVAILABLE]")
            else:
                server.set_status_message(f"Layout '{name}' not found")
            return

        # Digit alone in layouts mode: toggle availability
        for idx in range(10):
            if str(idx) in char and idx < len(layouts):
                l_item = layouts[idx]
                new_st = not l_item["is_available"]
                server.db.set_layout_availability(l_item["name"], new_st)
                st_label = "[AVAILABLE]" if new_st else "[UNAVAILABLE]"
                server.set_status_message(f"Layout '{l_item['name']}' toggled to {st_label}")
                break
    else:
        if 'p' in char:
            server.put_paused = not server.put_paused
        elif 'g' in char:
            server.get_paused = not server.get_paused
        elif 'c' in char:
            server.pending_identities.clear()
            server.pending_ips.clear()
        elif char == 'u':
            stats = server.get_cooldown_stats()
            if not stats:
                server.set_status_message("No IPs currently in cooldown")
            elif len(stats) == 1:
                target_ip = stats[0]["ip"]
                server.unblock_ip(target_ip)
                server.set_status_message(f"Unblocked IP '{target_ip}' from cooldown")
            else:
                cnt = server.unblock_all()
                server.set_status_message(f"Unblocked all {cnt} IP(s) from cooldown")
        else:
            for i in range(10):
                if str(i) in char:
                    if i < len(server.pending_identities):
                        server.add_whitelist(server.pending_identities[i])
                    break

async def local_display_loop(server: BancServer):
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            buf = getattr(server, "local_buffer", "")
            sys.stdout.write(server.generate_ascii_screen(buf))
            sys.stdout.flush()
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

async def local_input_loop(server: BancServer):
    server.local_buffer = ""
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if sys.platform == "win32" and is_tty:
        import msvcrt
        while True:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        cmd = server.local_buffer
                        server.local_buffer = ""
                        if cmd:
                            execute_ui_command(server, cmd)
                    elif ch in ("\x08", "\x7f"):
                        if server.local_buffer:
                            server.local_buffer = server.local_buffer[:-1]
                    elif ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            ext_ch = msvcrt.getwch()
                            if ext_ch == "H":
                                execute_ui_command(server, "up")
                            elif ext_ch == "P":
                                execute_ui_command(server, "down")
                            elif ext_ch == "I":
                                execute_ui_command(server, "pgup")
                            elif ext_ch == "Q":
                                execute_ui_command(server, "pgdn")
                            elif ext_ch == "G":
                                execute_ui_command(server, "top")
                            elif ext_ch == "O":
                                execute_ui_command(server, "bottom")
                    elif ord(ch) >= 32:
                        server.local_buffer += ch
                        if server.local_buffer in ('p', 'g', 'c') and server.ui_view == "telemetry":
                            cmd = server.local_buffer
                            server.local_buffer = ""
                            execute_ui_command(server, cmd)
                        elif server.local_buffer in ('l', 'm', '[', ']'):
                            cmd = server.local_buffer
                            server.local_buffer = ""
                            execute_ui_command(server, cmd)
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.05)
        return

    # Native Linux / macOS console keyboard listener using termios and tty
    if sys.platform != "win32" and is_tty:
        try:
            import termios
            import tty
            import select
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                cbreak_settings = termios.tcgetattr(fd)
                cbreak_settings[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, cbreak_settings)
                while True:
                    try:
                        rlist, _, _ = select.select([sys.stdin], [], [], 0)
                        if rlist:
                            ch = sys.stdin.read(1)
                            if ch:
                                if ch in ("\r", "\n"):
                                    cmd = server.local_buffer
                                    server.local_buffer = ""
                                    if cmd:
                                        execute_ui_command(server, cmd)
                                elif ch in ("\x08", "\x7f"):
                                    if server.local_buffer:
                                        server.local_buffer = server.local_buffer[:-1]
                                elif ch == "\x03":
                                    sys.exit(0)
                                elif ch == "\x1b":
                                    esc_seq = ""
                                    while True:
                                        r_esc, _, _ = select.select([sys.stdin], [], [], 0.01)
                                        if r_esc:
                                            esc_seq += sys.stdin.read(1)
                                            if len(esc_seq) > 8:
                                                break
                                        else:
                                            break
                                    if esc_seq == "[A":
                                        execute_ui_command(server, "up")
                                    elif esc_seq == "[B":
                                        execute_ui_command(server, "down")
                                    elif esc_seq in ("[5~", "O5~"):
                                        execute_ui_command(server, "pgup")
                                    elif esc_seq in ("[6~", "O6~"):
                                        execute_ui_command(server, "pgdn")
                                    elif esc_seq in ("[H", "[1~"):
                                        execute_ui_command(server, "top")
                                    elif esc_seq in ("[F", "[4~"):
                                        execute_ui_command(server, "bottom")
                                elif ord(ch) >= 32:
                                    server.local_buffer += ch
                                    if server.local_buffer in ('p', 'g', 'c') and server.ui_view == "telemetry":
                                        cmd = server.local_buffer
                                        server.local_buffer = ""
                                        execute_ui_command(server, cmd)
                                    elif server.local_buffer in ('l', 'm', '[', ']'):
                                        cmd = server.local_buffer
                                        server.local_buffer = ""
                                        execute_ui_command(server, cmd)
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(0.05)
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass
            return
        except Exception:
            pass

    # Fallback line-reading loop for redirected streams, pipes, and non-interactive environments
    loop = asyncio.get_running_loop() if hasattr(asyncio, "get_running_loop") else asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.5)
                continue
            cmd = line.strip()
            if cmd:
                execute_ui_command(server, cmd)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)

async def ui_writer_task(writer, server: BancServer, session_state: dict):
    try:
        while True:
            writer.write(b"\033[2J\033[H")
            buf = session_state.get("buffer", "")
            writer.write(server.generate_ascii_screen(buf).encode('utf-8'))
            await writer.drain()
            await asyncio.sleep(1)
    except Exception:
        pass

async def ui_reader_task(reader, writer, server: BancServer, session_state: dict):
    try:
        while True:
            data = await reader.read(100)
            if not data:
                break
            for b in data:
                if b in (ord('\r'), ord('\n')):
                    cmd = session_state.get("buffer", "").strip()
                    session_state["buffer"] = ""
                    if cmd:
                        execute_ui_command(server, cmd)
                elif b in (8, 127):  # Backspace
                    buf = session_state.get("buffer", "")
                    if buf:
                        session_state["buffer"] = buf[:-1]
                elif 32 <= b <= 126:
                    session_state["buffer"] = session_state.get("buffer", "") + chr(b)
                    if session_state["buffer"] in ('p', 'g', 'c') and server.ui_view == "telemetry":
                        cmd = session_state["buffer"]
                        session_state["buffer"] = ""
                        execute_ui_command(server, cmd)
                    elif session_state["buffer"] in ('l', 'm', '[', ']'):
                        cmd = session_state["buffer"]
                        session_state["buffer"] = ""
                        execute_ui_command(server, cmd)
    except Exception:
        pass

async def telnet_ui_handler(reader, writer, server: BancServer):
    session_state = {"buffer": ""}
    w_task = asyncio.create_task(ui_writer_task(writer, server, session_state))
    r_task = asyncio.create_task(ui_reader_task(reader, writer, server, session_state))
    
    done, pending = await asyncio.wait([w_task, r_task], return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def create_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"ApexBOSBanc ({VERSION}) - ApexBOSFlux Cloud Receiver")
    parser.add_argument("--config", type=str, default="banc.json", help="Path to configuration file")
    parser.add_argument("--gen-config", action="store_true", help="Generate an example configuration file")
    parser.add_argument("--display", action="store_true", help="Display the UI locally in the console")
    parser.add_argument("--inspect-db", type=int, metavar="LIMIT", help="Print the latest N rows from the database and exit")
    parser.add_argument(
        "--mirror",
        action="append",
        default=None,
        help="Mirror server host:port to relay packets, WIUs, and chat messages (repeatable or comma-separated; supports IPv4/IPv6)",
    )
    parser.add_argument(
        "--bind-host",
        default=None,
        help="Bind host for Banc data server ('auto' for dual-stack IPv4+IPv6, '::', '0.0.0.0', or specific IP)",
    )
    parser.add_argument(
        "--ui-bind-host",
        default=None,
        help="Bind host for Banc UI telnet server ('localhost' for 127.0.0.1 + ::1, 'auto', '::1', '127.0.0.1', etc.)",
    )
    parser.add_argument(
        "--ipv4-only",
        action="store_true",
        help="Force Banc data server to listen on IPv4 only",
    )
    parser.add_argument(
        "--ipv6-only",
        action="store_true",
        help="Force Banc data server to listen on IPv6 only",
    )
    return parser


def apply_cli_args_to_config(config: dict, args) -> dict:
    """Applies CLI overrides (mirror, bind hosts, IP version mode) onto the loaded Banc configuration dict."""
    if getattr(args, "mirror", None) is not None:
        default_port = int(config.get("port", 9000) or 9000)
        config["mirrors"] = _parse_mirrors_list(args.mirror, default_port=default_port)
    if getattr(args, "bind_host", None) is not None:
        config["bind_host"] = args.bind_host
    if getattr(args, "ui_bind_host", None) is not None:
        config["ui_bind_host"] = args.ui_bind_host
    if getattr(args, "ipv4_only", False):
        config["ip_mode"] = "ipv4"
    elif getattr(args, "ipv6_only", False):
        config["ip_mode"] = "ipv6"
    return config


async def start_dual_stack_server(
    client_connected_cb,
    host: Optional[str],
    port: int,
    ip_mode: str = "auto",
) -> Tuple[List[asyncio.AbstractServer], str]:
    """
    Starts an asyncio TCP server with automatic IPv4/IPv6 detection and dual-stack support:
    - 'auto'/None/'*'/'::' (unless ip_mode=='ipv6'): binds an AF_INET6 dual-stack socket (IPV6_V6ONLY=0)
      so a single port accepts BOTH IPv4 and IPv6 connections cleanly across Windows and Linux,
      with automatic fallback to IPv4-only or IPv6-only if the OS disables one stack.
    - 'localhost'/'loopback': binds BOTH '::1' and '127.0.0.1' on the same port (or whichever is available).
    - Explicit IPv4 or IPv6 address: auto-detects the IP version via detect_ip_version() and binds accordingly.
    Returns (list_of_servers, human_readable_bind_summary).
    """
    mode = str(ip_mode or "auto").strip().lower()
    raw_host = "" if host is None else str(host).strip()
    clean_host = raw_host.strip("[]")
    lower_host = clean_host.lower()

    def _make_listening_socket(family: int, bind_addr: str, bind_port: int, v6only: Optional[int] = None) -> socket.socket:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if v6only is not None and family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
            sock.bind((bind_addr, int(bind_port)))
            sock.listen(128)
            return sock
        except Exception:
            sock.close()
            raise

    # Explicit IPv4-only request on wildcard
    if mode in ("ipv4", "v4", "4") and lower_host in ("", "auto", "*", "::"):
        srv = await asyncio.start_server(client_connected_cb, "0.0.0.0", int(port), family=socket.AF_INET)
        actual_port = srv.sockets[0].getsockname()[1] if srv.sockets else int(port)
        return [srv], f"0.0.0.0:{actual_port} (IPv4-only)"

    # Explicit IPv6-only request on wildcard
    if mode in ("ipv6", "v6", "6") and lower_host in ("", "auto", "*", "::"):
        sock6 = _make_listening_socket(socket.AF_INET6, "::", int(port), v6only=1)
        actual_port = sock6.getsockname()[1]
        srv = await asyncio.start_server(client_connected_cb, sock=sock6)
        return [srv], f"[::]:{actual_port} (IPv6-only)"

    # Wildcard dual-stack (auto-detects/serves both IPv4 & IPv6 simultaneously)
    if lower_host in ("", "auto", "*", "::"):
        # 1. Preferred: single dual-stack AF_INET6 socket (IPV6_V6ONLY=0)
        try:
            sock_ds = _make_listening_socket(socket.AF_INET6, "::", int(port), v6only=0)
            actual_port = sock_ds.getsockname()[1]
            srv = await asyncio.start_server(client_connected_cb, sock=sock_ds)
            return [srv], f"0.0.0.0 & [::]:{actual_port} (dual-stack IPv4/IPv6)"
        except OSError:
            pass

        # 2. Fallback: bind AF_INET6 (if available) + AF_INET (if available)
        servers: List[asyncio.AbstractServer] = []
        bound_labels: List[str] = []
        target_port = int(port)
        try:
            sock6 = _make_listening_socket(socket.AF_INET6, "::", target_port, v6only=1)
            target_port = sock6.getsockname()[1]
            servers.append(await asyncio.start_server(client_connected_cb, sock=sock6))
            bound_labels.append(f"[::]:{target_port}")
        except OSError:
            pass

        try:
            sock4 = _make_listening_socket(socket.AF_INET, "0.0.0.0", target_port)
            target_port = sock4.getsockname()[1]
            servers.append(await asyncio.start_server(client_connected_cb, sock=sock4))
            bound_labels.append(f"0.0.0.0:{target_port}")
        except OSError:
            pass

        if servers:
            return servers, " & ".join(bound_labels)
        raise OSError(f"Failed to bind Banc server on port {port} for IPv4 or IPv6")

    # Loopback dual-stack ('localhost' or 'loopback')
    if lower_host in ("localhost", "loopback"):
        servers = []
        bound_labels = []
        target_port = int(port)
        if mode not in ("ipv4", "v4", "4"):
            try:
                sock6 = _make_listening_socket(socket.AF_INET6, "::1", target_port)
                target_port = sock6.getsockname()[1]
                servers.append(await asyncio.start_server(client_connected_cb, sock=sock6))
                bound_labels.append(f"[::1]:{target_port}")
            except OSError:
                pass
        if mode not in ("ipv6", "v6", "6"):
            try:
                sock4 = _make_listening_socket(socket.AF_INET, "127.0.0.1", target_port)
                target_port = sock4.getsockname()[1]
                servers.append(await asyncio.start_server(client_connected_cb, sock=sock4))
                bound_labels.append(f"127.0.0.1:{target_port}")
            except OSError:
                pass
        if servers:
            return servers, " & ".join(bound_labels)
        raise OSError(f"Failed to bind loopback server on port {port}")

    # Specific IP literal (auto-detect IP version) or hostname
    ip_ver = detect_ip_version(clean_host)
    if ip_ver == 4:
        srv = await asyncio.start_server(client_connected_cb, clean_host, int(port), family=socket.AF_INET)
        actual_port = srv.sockets[0].getsockname()[1] if srv.sockets else int(port)
        return [srv], f"{clean_host}:{actual_port} (IPv4)"
    if ip_ver == 6:
        srv = await asyncio.start_server(client_connected_cb, clean_host, int(port), family=socket.AF_INET6)
        actual_port = srv.sockets[0].getsockname()[1] if srv.sockets else int(port)
        return [srv], f"[{clean_host}]:{actual_port} (IPv6)"

    # Hostname (resolves via OS DNS/getaddrinfo)
    srv = await asyncio.start_server(client_connected_cb, clean_host, int(port))
    actual_port = srv.sockets[0].getsockname()[1] if srv.sockets else int(port)
    return [srv], f"{clean_host}:{actual_port}"


async def start_banc_servers(
    server: BancServer,
    config: dict,
) -> Tuple[List[asyncio.AbstractServer], List[asyncio.AbstractServer], str, str]:
    """Starts Banc data and UI servers with automatic IPv4/IPv6 dual-stack support."""
    bind_host = config.get("bind_host", config.get("host", "auto"))
    ui_bind_host = config.get("ui_bind_host", "localhost")
    ip_mode = config.get("ip_mode", "auto")
    data_port = int(config.get("port", 9000))
    ui_port = int(config.get("ui_port", 9001))

    data_srvs, data_desc = await start_dual_stack_server(
        lambda r, w: client_handler(r, w, server),
        host=bind_host,
        port=data_port,
        ip_mode=ip_mode,
    )
    ui_srvs, ui_desc = await start_dual_stack_server(
        lambda r, w: telnet_ui_handler(r, w, server),
        host=ui_bind_host,
        port=ui_port,
        ip_mode=ip_mode,
    )
    return data_srvs, ui_srvs, data_desc, ui_desc


def _unwrap_unified_banc_config(raw_cfg: dict) -> dict:
    """Extracts the 'banc' section when ApexBOSBanc is given unified config.json."""
    if not isinstance(raw_cfg, dict) or not isinstance(raw_cfg.get("banc"), dict):
        return raw_cfg
    banc_cfg = dict(raw_cfg["banc"])
    common = raw_cfg.get("common", {}) if isinstance(raw_cfg.get("common"), dict) else {}
    common_banc = common.get("banc", {}) if isinstance(common.get("banc"), dict) else {}
    for key in ("auth_secret", "encryption_key", "port", "ui_port", "whitelist_file", "identity"):
        if (key not in banc_cfg or not banc_cfg[key]) and key in common_banc and common_banc[key]:
            banc_cfg[key] = common_banc[key]
    if "ip_mode" not in banc_cfg and common.get("ip_version") in ("ipv4", "ipv6", "dual"):
        banc_cfg["ip_mode"] = common["ip_version"]
    return banc_cfg


async def main():
    parser = create_arg_parser()
    args = parser.parse_args()

    if args.gen_config:
        example_config = {
            "port": 9000,
            "ui_port": 9001,
            "auth_secret": "EXAMPLE_SECRET_KEY_123",
            "encryption_key": Fernet.generate_key().decode(),
            "whitelist_file": "whitelist.txt",
            "db_path": "banc.db",
            "db_retention_sec": 86400,
            "stats_interval_sec": 300,
            "mirrors": ["192.168.1.200:9000"]
        }
        with open(args.config, 'w') as f:
            json.dump(example_config, f, indent=4)
        print(f"[*] Generated config: {args.config}")
        if not os.path.exists("whitelist.txt"):
            open("whitelist.txt", "w").close()
        sys.exit(0)

    if not os.path.exists(args.config):
        print(f"Error: {args.config} not found.")
        sys.exit(1)

    with open(args.config, 'r') as f:
        config = _unwrap_unified_banc_config(json.load(f))
    apply_cli_args_to_config(config, args)

    if args.inspect_db:
        db_path = config.get("db_path", "banc.db")
        if not os.path.exists(db_path):
            print(f"Database {db_path} does not exist yet.")
            sys.exit(1)
            
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT date_str, identity, channel, hex_data, stamp_received FROM packets ORDER BY stamp_received DESC LIMIT ?", (args.inspect_db,))
            rows = cur.fetchall()
            
            print(f"\n--- Latest {len(rows)} Records in {db_path} ---")
            print(f"{'Date':<20} | {'Identity':<15} | {'Ch':<5} | {'Age (s)':<8} | {'Data (Hex)'}")
            print("-" * 80)
            now = time.time()
            for r in rows:
                age = round(now - r[4], 1)
                ident = r[1][:12]
                print(f"{r[0]:<20} | {ident:<15} | {r[2]:<5} | {age:<8} | {r[3][:25]}...")
            
            print("\n--- Channel Stats ---")
            cur.execute("SELECT channel, COUNT(*) FROM packets GROUP BY channel")
            stats = cur.fetchall()
            for s in stats:
                print(f"Channel {s[0]}: {s[1]} packets")
        sys.exit(0)

    server = BancServer(config)
    asyncio.create_task(db_cleanup_loop(server))
    asyncio.create_task(identity_stats_loop(server))

    if args.display:
        server.local_buffer = ""
        asyncio.create_task(local_display_loop(server))
        asyncio.create_task(local_input_loop(server))

    data_srvs, ui_srvs, data_desc, ui_desc = await start_banc_servers(server, config)
    all_srvs = data_srvs + ui_srvs

    if not args.display:
        print(f"[*] ApexBOSBanc Receiver ({VERSION}) started on {data_desc}")
        print(f"[*] UI Server started on {ui_desc}")

    try:
        await asyncio.gather(*(srv.serve_forever() for srv in all_srvs))
    finally:
        for srv in all_srvs:
            srv.close()
        for srv in all_srvs:
            try:
                await srv.wait_closed()
            except Exception:
                pass

if __name__ == "__main__":
    if sys.platform == 'win32':
        os.system('color')
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        print("\nApexBOSBanc shut down successfully.")

# ---------------------------------------------------------------------------
# Rebranded Backward Compatibility Aliases
# ---------------------------------------------------------------------------
ApexBOSBanc = BancServer
ApexBOSBancServer = BancServer
ApexBOXBanc = BancServer
ApexBOXBancServer = BancServer

# Register module alias for backward compatibility
import sys as _sys
_sys.modules.setdefault("banc", _sys.modules[__name__])
