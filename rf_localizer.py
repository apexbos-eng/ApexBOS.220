# -*- coding: utf-8 -*-
"""
rf_localizer.py - Privacy-Preserving Multi-SDR RF Localization Engine for ApexBOSBanc

Estimates physical transmitter locations from multi-SDR packet observations collected
by ApexBOSBanc without exposing SDR locations or linking them to user identities:

  Phase 1: Deterministic cross-node burst fingerprinting & sliding coincidence window
           (collects observations from all hearing SDRs before Banc deduplication).
  Phase 2: Stationary transmitter localization (PTC 0x33/0x34 WIUs, 0x02/0x06 Bases,
           ATCS 896 MHz MCPs, DPU offboard repeaters) with automatic per-SDR gain-bias
           calibration against verified known-GPS WIUs + zero-input auto-calibration
           of PTC SDR anchor positions.
  Phase 3: Moving train localization & heading estimation (EOT, HOT, Direct DPU/DSU
           with strict RP==DI repeater filtering) using RSSI-difference likelihood
           and exponential kinematic smoothing.

Privacy / Anti-Doxxing Guarantees:
  - Sensor coordinates are stored strictly server-side under opaque random sensor_ids
    decoupled from user identities and NEVER returned by any GET API.
  - Strictly enforces k-anonymity: single-SDR observations (k=1) NEVER produce a
    published location estimate.
  - Published estimates omit sensor IDs, sensor coordinates, and per-SDR RSSI values,
    quantize output coordinates, and enforce minimum containment radii.
"""

from collections import deque
import hashlib
import math
import re
import secrets
import sqlite3
import threading
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Protocol numeric codes (aligned with ApexBOSBanc.py / ApexBOSFlux.py)
PKT_PROTO_PTC = 0
PKT_PROTO_ATCS = 1
PKT_PROTO_FRED = 2

EARTH_RADIUS_KM = 6371.0088
MILES_TO_KM = 1.609344
KM_TO_MILES = 1.0 / MILES_TO_KM

# Coincidence & solver tuning constants
COINCIDENCE_WINDOW_SEC = 3.0            # Max timestamp skew across SDRs for same RF burst
MAX_COINCIDENCE_BURSTS = 4000           # Bounded in-memory burst buffer
MIN_SENSOR_SEPARATION_KM = 1.0          # Distinct sensors closer than this count as co-located
MIN_K_STATIONARY_HIGH = 3               # >=3 independent SDRs -> medium/high confidence stationary fix
MIN_K_TRAIN_HIGH = 3                    # >=3 independent SDRs -> medium/high confidence moving fix
ALLOW_K2_COARSE = True                  # k=2 produces coarse low-confidence estimate with wide floor
K2_MIN_RADIUS_KM = 6.0                  # Privacy & geometry floor for k=2 coarse estimates
K3_STATIONARY_MIN_RADIUS_KM = 0.5       # Privacy & RSSI floor for k>=3 stationary estimates
K3_TRAIN_MIN_RADIUS_KM = 1.5            # Privacy & RSSI floor for k>=3 moving train estimates
STATIONARY_OUT_GRID_DEG = 0.0025        # ~275 m output quantization for stationary estimates
TRAIN_OUT_GRID_DEG = 0.005              # ~550 m output quantization for k>=3 moving trains
K2_OUT_GRID_DEG = 0.01                  # ~1.1 km output quantization for k=2 coarse estimates
PRIVATE_SENSOR_GRID_DEG = 0.0025        # ~275 m quantization for opt-in private sensor anchors

# RF log-distance propagation model defaults (220/450/900 MHz overland railroad environments)
DEFAULT_PATH_LOSS_EXP = {
    PKT_PROTO_PTC: 3.0,   # 220 MHz VHF
    PKT_PROTO_ATCS: 3.3,  # 896/935 MHz UHF
    PKT_PROTO_FRED: 3.2,  # 452/457 MHz UHF
}
DEFAULT_RSSI_SIGMA_DB = 4.5             # Expected single-sensor shadowing/multipath std dev (dB)
MAX_GAIN_BIAS_DB = 28.0                 # Clamp per-SDR calibrated gain offset
TRAIN_TRACK_TTL_SEC = 420.0             # Expire moving train estimates after 7 min inactivity
STATIONARY_OBS_TTL_SEC = 86400.0 * 7    # Keep stationary burst aggregates up to 7 days
ACTIVE_SENSOR_WINDOW_SEC = 300.0        # Sensor considered "online" for soft absence evidence


# ---------------------------------------------------------------------------
# Geodesic & Math Helpers (Pure Python, Zero External Dependencies)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp * 0.5) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dl * 0.5) ** 2)
    c = 2.0 * math.atan2(math.sqrt(max(0.0, a)), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_KM * c


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def heading_to_cardinal(deg: Optional[float]) -> str:
    if deg is None or not math.isfinite(deg):
        return ""
    dirs = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    idx = int(round((float(deg) % 360.0) / 45.0)) % 8
    return dirs[idx]


def quantize_latlon(lat: float, lon: float, step_deg: float) -> Tuple[float, float]:
    step = max(0.0005, float(step_deg))
    q_lat = round(round(float(lat) / step) * step, 5)
    q_lon = round(round(float(lon) / step) * step, 5)
    q_lat = max(-85.0, min(85.0, q_lat))
    q_lon = ((q_lon + 180.0) % 360.0) - 180.0
    return q_lat, q_lon


def _robust_median(vals: Iterable[float]) -> float:
    arr = sorted(float(v) for v in vals if math.isfinite(float(v)))
    if not arr:
        return 0.0
    n = len(arr)
    mid = n // 2
    if n % 2 == 1:
        return arr[mid]
    return 0.5 * (arr[mid - 1] + arr[mid])


def _huber_loss(r: float, delta: float = 2.0) -> float:
    ar = abs(r)
    if ar <= delta:
        return 0.5 * ar * ar
    return delta * (ar - 0.5 * delta)


def _reverse_bits(val: int, width: int) -> int:
    v = int(val)
    out = 0
    for _ in range(int(width)):
        out = (out << 1) | (v & 1)
        v >>= 1
    return out


# ---------------------------------------------------------------------------
# Protocol-Specific Transmitter Identification & Relay Filtering
# ---------------------------------------------------------------------------

def _strip_channel_prefix(raw: bytes) -> Tuple[bytes, Optional[int]]:
    """Strips optional 'CHxxx[:RSSI=...]:' ASCII framing prefix if present."""
    if raw.startswith(b"CH"):
        m = re.match(rb"^CH(\d{2,3})(?::RSSI=[+-]?\d+(?:\.\d+)?)?:", raw)
        if m:
            try:
                ch_hint = int(m.group(1))
            except ValueError:
                ch_hint = None
            return raw[m.end():], ch_hint
    return raw, None


def _decode_banc_hex(hex_data: str) -> bytes:
    s = str(hex_data or "").strip()
    if not s:
        return b""
    try:
        return bytes.fromhex(s)
    except ValueError:
        clean = re.sub(r"[^0-9a-fA-F]", "", s)
        if len(clean) >= 2 and len(clean) % 2 == 0:
            try:
                return bytes.fromhex(clean)
            except ValueError:
                return b""
        return b""


def canonical_packet_fingerprint(proto_code: int, hex_data: str, channel: int = 0) -> str:
    """
    Produces a deterministic cross-node burst fingerprint for exact-packet correlation
    across independent SDRs/Flux nodes. Includes protocol code, RF channel, and full
    canonical RF payload bytes (never inner _hmac_ alone, so relayed copies on different
    channels or with altered L2 headers never collide with original bursts).
    """
    raw = _decode_banc_hex(hex_data)
    body, ch_in_prefix = _strip_channel_prefix(raw)
    eff_ch = int(channel or ch_in_prefix or 0)
    h = hashlib.sha256()
    h.update(f"{int(proto_code)}:{eff_ch}:".encode("ascii"))
    h.update(body if body else str(hex_data or "").strip().upper().encode("ascii"))
    return h.hexdigest()[:24]


def deterministic_pkt_hash_u32(data_bytes: bytes, channel: int = 0, proto_code: int = PKT_PROTO_PTC) -> int:
    """Deterministic 32-bit unsigned packet hash safe for cross-node Banc deduplication."""
    basis = f"{int(proto_code)}:{int(channel)}:".encode("ascii") + bytes(data_bytes or b"")
    return zlib.crc32(basis) & 0xFFFFFFFF


def extract_tx_observation(
    proto_code: int,
    hex_data: str,
    channel: int = 0,
    b3_tag: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Extracts transmitter identity, stationarity, direct-RF eligibility (filtering out
    forwarded/relayed/repeated packets), and deterministic burst fingerprint.

    Returns dict with keys:
      tx_key, proto, category, unit_id, stationary, direct, fingerprint, channel, detail
    or None if the packet cannot be cleanly attributed to a direct RF transmitter.
    """
    raw = _decode_banc_hex(hex_data)
    if not raw:
        return None
    body, ch_in_prefix = _strip_channel_prefix(raw)
    eff_ch = int(channel or ch_in_prefix or 0)
    tag_u = str(b3_tag or "").strip().strip('"').strip("'").upper()
    fp = canonical_packet_fingerprint(proto_code, hex_data, eff_ch)

    # ------------------------------------------------------------------
    # 1. PTC (220 MHz) - MosFET frame: [0x49 'I'][len_hi][len_lo][type][flags][payload...]
    # ------------------------------------------------------------------
    if proto_code == PKT_PROTO_PTC:
        if len(body) < 6:
            return None
        pkt_type = body[3]
        payload = body[5:]

        # 0x33 / 0x34: Direct WIU Status Broadcast (NEVER relayed -> #1 stationary target)
        if pkt_type in (0x33, 0x34) and len(payload) >= 5:
            wiu_id = int.from_bytes(payload[0:5], "big", signed=False)
            if wiu_id <= 0:
                return None
            return {
                "tx_key": f"wiu:{wiu_id}",
                "proto": "ptc",
                "proto_code": PKT_PROTO_PTC,
                "category": "wiu",
                "unit_id": str(wiu_id),
                "stationary": True,
                "direct": True,
                "fingerprint": fp,
                "channel": eff_ch,
                "detail": {"pkt_type": pkt_type},
            }

        # 0x02 / 0x06: Direct PTC Base Station Beacon / Time Sync (stationary tower)
        if pkt_type in (0x02, 0x06) and len(payload) >= 3:
            base_id = int.from_bytes(payload[0:3], "big", signed=False)
            if base_id <= 0:
                return None
            return {
                "tx_key": f"base:{base_id:06X}",
                "proto": "ptc",
                "proto_code": PKT_PROTO_PTC,
                "category": "base",
                "unit_id": f"{base_id:06X}",
                "stationary": True,
                "direct": True,
                "fingerprint": fp,
                "channel": eff_ch,
                "detail": {"pkt_type": pkt_type},
            }

        # 0x50..0x5F and 0x70..0x7F: EMP Class D / HRX packets
        # Guard against relayed/forwarded packets:
        #   - If payload matches HRX header layout with Hops Cnt > 0, mark direct=False.
        #   - Require exact full-frame fingerprint + channel match (never inner _hmac_ alone).
        if ((0x50 <= pkt_type <= 0x5F) or (0x70 <= pkt_type <= 0x7F)) and len(payload) >= 12:
            # HRX check: TID(3) + RID(3) + Type(1) + Ser#(1) + Pri/ID(1) + HopsCnt(1) + Len(2) + 'HRX?'
            hops_cnt = 0
            is_hrx = len(payload) >= 16 and payload[12:15] == b"HRX"
            if is_hrx:
                hops_cnt = int(payload[9])
                if hops_cnt > 0:
                    return None  # Relayed multi-hop packet -> do NOT localize as originating transmitter
            src_id = int.from_bytes(payload[0:3], "big", signed=False)
            if src_id <= 0:
                return None
            return {
                "tx_key": f"emp:{src_id:06X}",
                "proto": "ptc",
                "proto_code": PKT_PROTO_PTC,
                "category": "emp",
                "unit_id": f"{src_id:06X}",
                "stationary": False,
                "direct": True,
                "fingerprint": fp,
                "channel": eff_ch,
                "detail": {"pkt_type": pkt_type, "hops": hops_cnt},
            }

        return None

    # ------------------------------------------------------------------
    # 2. ATCS (896 MHz MCP field vs. 935 MHz BCP base)
    # ------------------------------------------------------------------
    if proto_code == PKT_PROTO_ATCS:
        # ATCS frames typically begin with 'A' (0x41) or raw BCH payload; extract address bytes
        payload = body[5:] if (len(body) >= 6 and body[0] in (0x41, 0x49)) else body
        if len(payload) < 4:
            return None
        addr_hex = payload[:4].hex().upper()
        # Channels 11..16 are 896 MHz field MCPs (direct from bungalow); 1..6 are 935 MHz BCP towers
        is_mcp = (eff_ch >= 11) or ("MCP" in tag_u)
        cat = "mcp" if is_mcp else "bcp"
        return {
            "tx_key": f"{cat}:{addr_hex}",
            "proto": "atcs",
            "proto_code": PKT_PROTO_ATCS,
            "category": cat,
            "unit_id": addr_hex,
            "stationary": True,
            "direct": True,
            "fingerprint": fp,
            "channel": eff_ch,
            "detail": {},
        }

    # ------------------------------------------------------------------
    # 3. FRED (452/457 MHz: EOT, HOT, DPU/DSU, RLC)
    # ------------------------------------------------------------------
    if proto_code == PKT_PROTO_FRED:
        # Determine payload bytes (strip optional 5-byte MosFET/FRED envelope if present)
        # ApexBOSFlux wraps FRED frames as b'F' / b'I' or raw payload depending on source
        payload = body
        if len(body) >= 6 and body[0] in (0x46, 0x49):
            declared_len = (body[1] << 8) | body[2]
            if declared_len > 0 and len(body) >= 5 + declared_len:
                payload = body[5:5 + declared_len]

        # 3a. DPU / DSU (ch 3..6 or b3_tag == 'DPU')
        # Header layout per config/dpu_rules.json:
        #   ADDR(16b) | TRID(8b) | TP(2b) | OR(1b: 0=LD,1=RM) | RP(2b: 0/3=DI,1=RP,2=RO) | NRM(3b) | SEQ(5b)
        if tag_u == "DPU" or eff_ch in (3, 4, 5, 6):
            if len(payload) < 5:
                return None
            addr = (payload[0] << 8) | payload[1]
            trid = payload[2]
            b3 = payload[3]
            b4 = payload[4]
            tp = (b3 >> 6) & 0x3
            or_bit = (b3 >> 5) & 0x1       # 0 = LEAD, 1 = REMOTE
            rp_bits = (b3 >> 3) & 0x3      # 0/3 = Direct (DI), 1 = Onboard Repeat (RP), 2 = Offboard Repeater (RO)
            seq = (b4 >> 3) & 0x1F         # 5-bit rolling sequence counter (0..31)

            if addr == 0:
                return None
            if rp_bits == 1:
                # Onboard remote repeater: RF originated from another unit in consist -> skip for direct train fix
                return None
            if rp_bits == 2:
                # Offboard wayside repeater (tunnel/canyon fixed site) -> stationary repeater target
                return {
                    "tx_key": f"dpu_rep:{addr}:{eff_ch}",
                    "proto": "dpu",
                    "proto_code": PKT_PROTO_FRED,
                    "category": "repeater",
                    "unit_id": f"{addr}",
                    "stationary": True,
                    "direct": True,
                    "fingerprint": fp,
                    "channel": eff_ch,
                    "detail": {"addr": addr, "trid": trid, "seq": seq, "rp": "RO"},
                }
            # Direct lead or remote locomotive transmission (RP == DI)
            role = "rem" if or_bit else "lead"
            return {
                "tx_key": f"dpu:{addr}:{trid}:{role}",
                "proto": "dpu",
                "proto_code": PKT_PROTO_FRED,
                "category": "train",
                "unit_id": f"{addr}",
                "stationary": False,
                "direct": True,
                "fingerprint": fp,
                "channel": eff_ch,
                "detail": {"addr": addr, "trid": trid, "role": role, "tp": tp, "seq": seq, "rp": "DI"},
            }

        # 3b. HOT (452.9375 MHz, ch 2 or b3_tag == 'HOT')
        if tag_u == "HOT" or eff_ch == 2:
            if len(payload) == 4:
                # 30-bit BCH(63,30) AAR S-5701 HOT word (packed as 8 hex chars)
                d30 = int.from_bytes(payload, "big", signed=False) & ((1 << 30) - 1)
                unit_id = _reverse_bits((d30 >> 8) & 0x1FFFF, 17)
            elif len(payload) == 6:
                # 45-bit legacy/fallback HOT word (12 hex chars)
                d45 = int.from_bytes(payload, "big", signed=False) & ((1 << 45) - 1)
                unit_id = _reverse_bits((d45 >> 21) & 0x1FFFF, 17)
            else:
                return None
            if not (1 <= unit_id <= 99999):
                return None
            return {
                "tx_key": f"hot:{unit_id:05d}",
                "proto": "hot",
                "proto_code": PKT_PROTO_FRED,
                "category": "train",
                "unit_id": f"{unit_id:05d}",
                "stationary": False,
                "direct": True,
                "fingerprint": fp,
                "channel": eff_ch,
                "detail": {"kind": "hot"},
            }

        # 3c. EOT (457.9375 MHz, ch 1 or b3_tag == 'EOT')
        if tag_u in ("EOT", "FRED", "") or eff_ch == 1:
            if len(payload) == 6:
                d45 = int.from_bytes(payload, "big", signed=False) & ((1 << 45) - 1)
                unit_id = _reverse_bits((d45 >> 21) & 0x1FFFF, 17)
                if not (1 <= unit_id <= 99999):
                    return None
                return {
                    "tx_key": f"eot:{unit_id:05d}",
                    "proto": "eot",
                    "proto_code": PKT_PROTO_FRED,
                    "category": "train",
                    "unit_id": f"{unit_id:05d}",
                    "stationary": False,
                    "direct": True,
                    "fingerprint": fp,
                    "channel": eff_ch,
                    "detail": {"kind": "eot"},
                }

    return None


# ---------------------------------------------------------------------------
# Core RSSI-Difference Spatial Likelihood Solver
# ---------------------------------------------------------------------------

def _distinct_sensor_cluster_count(coords: List[Tuple[float, float]], min_sep_km: float = MIN_SENSOR_SEPARATION_KM) -> int:
    """Counts geographically distinct SDR sites separated by at least min_sep_km."""
    reps: List[Tuple[float, float]] = []
    for lat, lon in coords:
        if all(haversine_km(lat, lon, rlat, rlon) >= min_sep_km for rlat, rlon in reps):
            reps.append((lat, lon))
    return len(reps)


def solve_rssi_difference_location(
    sensor_obs: List[Dict[str, Any]],
    path_loss_exp: float = 3.0,
    silent_sensors: Optional[List[Dict[str, Any]]] = None,
    stationary: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Estimates transmitter location from calibrated multi-SDR RSSI observations using
    pairwise RSSI-difference Huber likelihood + soft silent-sensor absence evidence.

    Each item in `sensor_obs` must contain:
      - lat (float), lon (float)
      - rssi_cal (float): bias-corrected RSSI (or raw RSSI if uncalibrated)
      - pos_sigma_km (float): sensor anchor position uncertainty (0.25 km for opt-in,
                              2-3 km for WIU-auto, ~5-10 km for blurred fallback)
      - rssi_sigma_db (float, optional): per-sensor measurement std dev (default 4.5 dB)

    Privacy & Geometry Guarantees:
      - Returns None if fewer than 2 geographically distinct sensors heard the transmitter.
      - For k == 2, returns a coarse low-confidence estimate with minimum radius >= 6.0 km
        and ~1.1 km coordinate quantization (or None if ALLOW_K2_COARSE is False).
      - For k >= 3, returns medium/high confidence estimate with privacy-floored radius
        and quantized coordinates.
    """
    if not sensor_obs:
        return None

    distinct_k = _distinct_sensor_cluster_count([(o["lat"], o["lon"]) for o in sensor_obs])
    if distinct_k < 2:
        return None
    if distinct_k == 2 and not ALLOW_K2_COARSE:
        return None

    n_exp = max(1.8, min(5.0, float(path_loss_exp)))
    silent = list(silent_sensors or [])

    lats = [float(o["lat"]) for o in sensor_obs]
    lons = [float(o["lon"]) for o in sensor_obs]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    mid_lat = 0.5 * (min_lat + max_lat)
    cos_lat = max(0.15, math.cos(math.radians(mid_lat)))

    span_km = max(
        haversine_km(min_lat, min_lon, max_lat, max_lon),
        6.0,
    )
    pad_km = max(10.0, min(45.0, span_km * 0.65))
    pad_lat = pad_km / 111.32
    pad_lon = pad_km / (111.32 * cos_lat)

    lat_lo = max(-84.5, min_lat - pad_lat)
    lat_hi = min(84.5, max_lat + pad_lat)
    lon_lo = min_lon - pad_lon
    lon_hi = max_lon + pad_lon

    # Prebuild all unordered sensor pairs (i, j)
    m = len(sensor_obs)
    pairs: List[Tuple[int, int, float, float]] = []
    for i in range(m):
        for j in range(i + 1, m):
            d_ij_obs = float(sensor_obs[i]["rssi_cal"]) - float(sensor_obs[j]["rssi_cal"])
            si = float(sensor_obs[i].get("rssi_sigma_db", DEFAULT_RSSI_SIGMA_DB))
            sj = float(sensor_obs[j].get("rssi_sigma_db", DEFAULT_RSSI_SIGMA_DB))
            pi_km = float(sensor_obs[i].get("pos_sigma_km", 0.5))
            pj_km = float(sensor_obs[j].get("pos_sigma_km", 0.5))
            # Convert position uncertainty into equivalent dB uncertainty (~2.5 dB per 2 km at typical range)
            pos_db = 1.2 * math.hypot(pi_km, pj_km)
            sig_ij = max(2.5, math.sqrt(si * si + sj * sj + pos_db * pos_db))
            pairs.append((i, j, d_ij_obs, sig_ij))

    # Reference range scale for gentle physical-horizon prior (prevents solutions flying to infinity)
    max_plausible_km = 55.0 if stationary else 35.0
    max_rssi = max(float(o["rssi_cal"]) for o in sensor_obs)

    def _eval_point(plat: float, plon: float) -> float:
        dists = [max(0.2, haversine_km(plat, plon, float(o["lat"]), float(o["lon"]))) for o in sensor_obs]
        cost = 0.0
        for i, j, d_obs, sig_ij in pairs:
            # Predicted RSSI_i - RSSI_j = -10 * n * log10(d_i / d_j)
            d_pred = -10.0 * n_exp * math.log10(dists[i] / dists[j])
            cost += _huber_loss((d_obs - d_pred) / sig_ij, delta=2.2)

        # Gentle prior: transmitter is physically within RF horizon of the sensors that heard it
        # and closer to the strongest-hearing sensor
        mean_d = sum(dists) / float(m)
        if mean_d > max_plausible_km:
            excess = (mean_d - max_plausible_km) / 10.0
            cost += 0.35 * excess * excess
        # Mild pull toward strongest sensor when k == 2 (breaks circular Apollonius ambiguity cleanly)
        if distinct_k == 2:
            for idx, o in enumerate(sensor_obs):
                w = max(0.05, 1.0 - max(0.0, (max_rssi - float(o["rssi_cal"])) / 25.0))
                cost += 0.025 * w * ((dists[idx] / max(5.0, span_km)) ** 2)

        # Soft negative evidence from active nearby sensors that did NOT hear the burst
        if silent:
            min_heard_d = min(dists)
            for s in silent:
                ds = max(0.2, haversine_km(plat, plon, float(s["lat"]), float(s["lon"])))
                if ds < min(8.0, 0.55 * min_heard_d):
                    ratio = (min(8.0, 0.55 * min_heard_d) - ds) / 4.0
                    cost += 0.45 * ratio * ratio
        return cost

    def _grid_search(lo_lat: float, hi_lat: float, lo_lon: float, hi_lon: float, steps: int):
        best_c = float("inf")
        best_pt = (mid_lat, 0.5 * (min_lon + max_lon))
        grid_pts: List[Tuple[float, float, float]] = []
        d_lat = (hi_lat - lo_lat) / float(max(1, steps - 1))
        d_lon = (hi_lon - lo_lon) / float(max(1, steps - 1))
        for iy in range(steps):
            glat = lo_lat + iy * d_lat
            for ix in range(steps):
                glon = lo_lon + ix * d_lon
                c = _eval_point(glat, glon)
                grid_pts.append((glat, glon, c))
                if c < best_c:
                    best_c = c
                    best_pt = (glat, glon)
        return best_pt, best_c, grid_pts

    # Stage 1: Coarse 21x21 grid across full padded sensor envelope
    (c_lat, c_lon), _, _ = _grid_search(lat_lo, lat_hi, lon_lo, lon_hi, steps=21)

    # Stage 2: Fine 21x21 grid around coarse optimum (+/- 18% of coarse box, min +/- 4 km)
    fine_lat_r = max(4.0 / 111.32, (lat_hi - lat_lo) * 0.18)
    fine_lon_r = max(4.0 / (111.32 * cos_lat), (lon_hi - lon_lo) * 0.18)
    (f_lat, f_lon), best_cost, fine_pts = _grid_search(
        max(-84.5, c_lat - fine_lat_r),
        min(84.5, c_lat + fine_lat_r),
        c_lon - fine_lon_r,
        c_lon + fine_lon_r,
        steps=21,
    )

    # Posterior likelihood-weighted centroid & containment radius estimate
    w_sum = 0.0
    w_lat = 0.0
    w_lon = 0.0
    for glat, glon, c in fine_pts:
        dc = c - best_cost
        if dc > 14.0:
            continue
        w = math.exp(-0.5 * dc)
        w_sum += w
        w_lat += w * glat
        w_lon += w * glon

    if w_sum > 1e-12:
        est_lat = w_lat / w_sum
        est_lon = w_lon / w_sum
    else:
        est_lat, est_lon = f_lat, f_lon

    # Posterior RMS spread in km
    var_km2 = 0.0
    if w_sum > 1e-12:
        for glat, glon, c in fine_pts:
            dc = c - best_cost
            if dc > 14.0:
                continue
            w = math.exp(-0.5 * dc)
            dkm = haversine_km(est_lat, est_lon, glat, glon)
            var_km2 += w * (dkm * dkm)
        rms_km = math.sqrt(var_km2 / w_sum)
    else:
        rms_km = 5.0

    # Factor in sensor anchor uncertainty & residual fit quality
    avg_pos_sigma = sum(float(o.get("pos_sigma_km", 0.5)) for o in sensor_obs) / float(m)
    rms_residual = math.sqrt(max(0.0, best_cost) / float(max(1, len(pairs))))

    raw_radius_km = max(1.35 * rms_km, avg_pos_sigma * 1.2) * (1.0 + 0.25 * min(3.0, rms_residual))

    # Enforce strict privacy & geometry floors + output coordinate quantization
    if distinct_k == 2:
        out_radius_km = max(K2_MIN_RADIUS_KM, min(40.0, raw_radius_km))
        q_lat, q_lon = quantize_latlon(est_lat, est_lon, K2_OUT_GRID_DEG)
        confidence = "low"
    else:
        min_r = K3_STATIONARY_MIN_RADIUS_KM if stationary else K3_TRAIN_MIN_RADIUS_KM
        out_radius_km = max(min_r, min(35.0, raw_radius_km))
        grid_deg = STATIONARY_OUT_GRID_DEG if stationary else TRAIN_OUT_GRID_DEG
        q_lat, q_lon = quantize_latlon(est_lat, est_lon, grid_deg)
        if distinct_k >= 4 and out_radius_km <= 3.0 and rms_residual <= 1.2:
            confidence = "high"
        elif out_radius_km <= 6.5 and rms_residual <= 2.0:
            confidence = "medium"
        else:
            confidence = "low"

    return {
        "est_lat": round(q_lat, 5),
        "est_lon": round(q_lon, 5),
        "est_radius_km": round(out_radius_km, 2),
        "confidence": confidence,
        "fit_residual": round(rms_residual, 3),
        "_raw_lat": est_lat,
        "_raw_lon": est_lon,
        "_distinct_k": distinct_k,
    }


def solve_sensor_position_from_known_wius(
    wiu_obs: List[Tuple[float, float, float]],
    path_loss_exp: float = 3.0,
) -> Optional[Tuple[float, float, float]]:
    """
    Zero-input Phase-2 calibration helper: given >= 3 verified known-GPS WIUs heard by a
    PTC SDR [(wiu_lat, wiu_lon, median_rssi_db), ...], estimates the SDR's approximate
    private anchor position (lat, lon, pos_sigma_km) purely from the known WIUs!
    """
    if len(wiu_obs) < 3:
        return None
    pseudo = [
        {"lat": lat, "lon": lon, "rssi_cal": rssi, "pos_sigma_km": 0.2, "rssi_sigma_db": 5.0}
        for lat, lon, rssi in wiu_obs
    ]
    res = solve_rssi_difference_location(pseudo, path_loss_exp=path_loss_exp, stationary=True)
    if not res:
        return None
    return float(res["_raw_lat"]), float(res["_raw_lon"]), max(1.5, min(8.0, float(res["est_radius_km"])))


# ---------------------------------------------------------------------------
# Stateful Multi-SDR Coincidence Aggregator, Calibrator & Persistent Store
# ---------------------------------------------------------------------------

class RfLocalizer:
    """
    Server-side privacy-preserving multi-SDR RF localization engine embedded in ApexBOSBanc.
    Thread-safe; persists decoupled sensor anchors and anonymized transmitter estimates in SQLite.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()

        # Private identity->opaque sensor_id map (NEVER returned over GET)
        self._ident_to_sensor_id: Dict[Tuple[str, int], str] = {}
        # Sensor anchors: {sensor_id: {proto_code, lat, lon, pos_sigma_km, source, gain_bias_db, last_seen_ts}}
        self._sensors: Dict[str, Dict[str, Any]] = {}
        # Known verified WIU GPS cache (from Banc's `wius` table ONLY - never estimated WIUs!)
        self._known_wiu_gps: Dict[str, Tuple[float, float]] = {}

        # Phase 1: In-memory sliding coincidence window keyed by (proto_code, fingerprint)
        # Value: {"tx": tx_dict, "first_ts": float, "obs": {sensor_id: (rssi, rx_ts)}}
        self._coincidences: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._coincidence_order: deque = deque()

        # Phase 2: Stationary transmitter multi-SDR aggregates
        # {tx_key: {"tx": tx_dict, "per_sensor": {sensor_id: deque(maxlen=64)}, "coincidence_bursts": int, "last_ts": float}}
        self._stationary_agg: Dict[str, Dict[str, Any]] = {}
        # Known-WIU per-sensor RSSI samples for gain bias calibration:
        # {sensor_id: {wiu_id: deque(maxlen=40)}}
        self._cal_wiu_samples: Dict[str, Dict[str, deque]] = {}

        # Phase 3: Moving train smoothed tracks & history
        # {tx_key: {"tx": tx_dict, "raw_lat": float, "raw_lon": float, "prev_points": deque([(ts, lat, lon)]),
        #           "sensor_trends": {sensor_id: deque([(ts, rssi)])}, "burst_count": int, "last_ts": float}}
        self._moving_tracks: Dict[str, Dict[str, Any]] = {}

        # Public anonymized transmitter estimates (safe to return via GET_ESTIMATES)
        self._estimates: Dict[str, Dict[str, Any]] = {}

        self._init_tables()
        self._load_state_from_db()

    def _init_tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # Private server-only decoupled sensor anchors (NEVER exposed to clients)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sensor_anchors (
                    sensor_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    proto INTEGER NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    pos_sigma_km REAL DEFAULT 0.5,
                    gain_bias_db REAL DEFAULT 0.0,
                    source TEXT DEFAULT 'user_private_optin',
                    updated_ts REAL NOT NULL,
                    UNIQUE(identity, proto)
                )
            """)
            # Public anonymized transmitter location estimates (strictly zero SDR info)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tx_estimates (
                    tx_key TEXT PRIMARY KEY,
                    proto TEXT NOT NULL,
                    category TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    stationary INTEGER DEFAULT 1,
                    est_lat REAL NOT NULL,
                    est_lon REAL NOT NULL,
                    est_radius_km REAL NOT NULL,
                    confidence TEXT DEFAULT 'low',
                    heading_deg REAL,
                    heading_cardinal TEXT DEFAULT '',
                    speed_mph REAL,
                    sample_count INTEGER DEFAULT 0,
                    updated_ts REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_estimates_cat ON tx_estimates(category, updated_ts)")
            conn.commit()
        finally:
            conn.close()

    def _load_state_from_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT sensor_id, identity, proto, lat, lon, pos_sigma_km, gain_bias_db, source, updated_ts FROM sensor_anchors"
            )
            for sid, ident, proto, lat, lon, ps, gb, src, uts in cur.fetchall():
                self._ident_to_sensor_id[(str(ident), int(proto))] = str(sid)
                self._sensors[str(sid)] = {
                    "proto_code": int(proto),
                    "lat": float(lat),
                    "lon": float(lon),
                    "pos_sigma_km": float(ps or 0.5),
                    "gain_bias_db": float(gb or 0.0),
                    "source": str(src or "user_private_optin"),
                    "last_seen_ts": float(uts or 0.0),
                }

            cur2 = conn.execute(
                "SELECT tx_key, proto, category, unit_id, stationary, est_lat, est_lon, est_radius_km, "
                "confidence, heading_deg, heading_cardinal, speed_mph, sample_count, updated_ts FROM tx_estimates"
            )
            now = time.time()
            for row in cur2.fetchall():
                tx_key = str(row[0])
                is_stat = bool(row[4])
                uts = float(row[13] or 0.0)
                if not is_stat and (now - uts) > TRAIN_TRACK_TTL_SEC:
                    continue
                self._estimates[tx_key] = {
                    "tx_key": tx_key,
                    "proto": str(row[1]),
                    "category": str(row[2]),
                    "unit_id": str(row[3]),
                    "stationary": is_stat,
                    "est_lat": float(row[5]),
                    "est_lon": float(row[6]),
                    "est_radius_km": float(row[7]),
                    "confidence": str(row[8] or "low"),
                    "heading_deg": float(row[9]) if row[9] is not None else None,
                    "heading_cardinal": str(row[10] or ""),
                    "speed_mph": float(row[11]) if row[11] is not None else None,
                    "sample_count": int(row[12] or 0),
                    "updated_ts": uts,
                }
        finally:
            conn.close()

    def _get_or_create_sensor_id_unlocked(self, identity: str, proto_code: int) -> str:
        key = (str(identity).strip(), int(proto_code))
        sid = self._ident_to_sensor_id.get(key)
        if not sid:
            sid = "s_" + secrets.token_hex(12)
            self._ident_to_sensor_id[key] = sid
        return sid

    # ------------------------------------------------------------------
    # Sensor Anchor Registration (Opt-In Private / Blurred Fallback / Auto)
    # ------------------------------------------------------------------

    def upsert_private_sensor_anchor(
        self,
        identity: str,
        proto_code: int,
        lat: float,
        lon: float,
        pos_sigma_km: float = 0.35,
        source: str = "user_private_optin",
    ) -> bool:
        """
        Stores a decoupled private sensor anchor for localization.
        Coordinates are quantized to ~275 m (never sub-house precision) and linked
        internally to an opaque random `sensor_id`. Never overwrites an explicit
        `user_private_optin` anchor with an auto-inferred or blurred fallback.
        """
        ident = str(identity or "").strip()
        if not ident or proto_code not in (PKT_PROTO_PTC, PKT_PROTO_ATCS, PKT_PROTO_FRED):
            return False
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            sig_km = max(0.25, min(40.0, float(pos_sigma_km)))
        except (ValueError, TypeError):
            return False
        if not (-85.0 <= lat_f <= 85.0 and -180.0 <= lon_f <= 180.0):
            return False
        if lat_f == 0.0 and lon_f == 0.0:
            return False

        q_lat, q_lon = quantize_latlon(lat_f, lon_f, PRIVATE_SENSOR_GRID_DEG)
        now = time.time()
        src = str(source or "user_private_optin").strip()

        with self._lock:
            sid = self._get_or_create_sensor_id_unlocked(ident, proto_code)
            existing = self._sensors.get(sid)
            # Priority hierarchy: user_private_optin > wiu_calibrated_auto > user_blurred
            prio = {"user_private_optin": 3, "wiu_calibrated_auto": 2, "user_blurred": 1}
            if existing is not None and prio.get(existing.get("source", ""), 0) > prio.get(src, 0):
                return False
            gain_bias = float(existing.get("gain_bias_db", 0.0)) if existing else 0.0
            last_seen = max(now, float(existing.get("last_seen_ts", 0.0))) if existing else now
            self._sensors[sid] = {
                "proto_code": int(proto_code),
                "lat": q_lat,
                "lon": q_lon,
                "pos_sigma_km": sig_km,
                "gain_bias_db": gain_bias,
                "source": src,
                "last_seen_ts": last_seen,
            }
            self._persist_sensor_anchor_unlocked(sid, ident, proto_code, q_lat, q_lon, sig_km, gain_bias, src, now)
        return True

    def delete_private_sensor_anchor(self, identity: str, proto_code: int) -> bool:
        ident = str(identity or "").strip()
        if not ident:
            return False
        with self._lock:
            sid = self._ident_to_sensor_id.get((ident, int(proto_code)))
            if not sid:
                return False
            existing = self._sensors.get(sid)
            if existing and existing.get("source") == "user_private_optin":
                self._sensors.pop(sid, None)
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute(
                    "DELETE FROM sensor_anchors WHERE identity = ? AND proto = ? AND source = 'user_private_optin'",
                    (ident, int(proto_code)),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def upsert_private_sensors_payload(self, identity: str, payload: Any) -> int:
        """
        Processes a PUT_SDR_SENSOR JSON payload:
          {'ptc': {'lat': ..., 'lon': ...}, 'fred': {'lat': ..., 'lon': ...}}
          or {'fred': {'clear': True}} to opt out cleanly.
        """
        if not isinstance(payload, dict):
            return 0
        key_map = {
            "ptc": PKT_PROTO_PTC, "grabber": PKT_PROTO_PTC, "0": PKT_PROTO_PTC,
            "atcs": PKT_PROTO_ATCS, "latch": PKT_PROTO_ATCS, "1": PKT_PROTO_ATCS,
            "fred": PKT_PROTO_FRED, "eot": PKT_PROTO_FRED, "hot": PKT_PROTO_FRED, "2": PKT_PROTO_FRED,
        }
        updated = 0
        for raw_k, rec in payload.items():
            p_code = key_map.get(str(raw_k).strip().lower())
            if p_code is None:
                continue
            if rec is None or (isinstance(rec, dict) and rec.get("clear")):
                if self.delete_private_sensor_anchor(identity, p_code):
                    updated += 1
                continue
            if not isinstance(rec, dict):
                continue
            lat = rec.get("lat") if rec.get("lat") is not None else rec.get("loc_lat")
            lon = rec.get("lon") if rec.get("lon") is not None else rec.get("loc_lon")
            if lat is not None and lon is not None:
                if self.upsert_private_sensor_anchor(identity, p_code, lat, lon, pos_sigma_km=0.35, source="user_private_optin"):
                    updated += 1
        return updated

    def sync_blurred_feeder_regions(self, regions_map: Dict[Tuple[str, int], Tuple[float, float, float]]):
        """Registers coarse blurred regions as low-priority fallback anchors (never overrides opt-in/auto)."""
        for (ident, proto_code), (lat, lon, rad_km) in (regions_map or {}).items():
            self.upsert_private_sensor_anchor(
                ident,
                int(proto_code),
                lat,
                lon,
                pos_sigma_km=max(3.0, float(rad_km) * 0.45),
                source="user_blurred",
            )

    def sync_known_wiu_gps(self, wiu_gps_map: Dict[str, Tuple[float, float]]):
        """Updates verified ground-truth WIU coordinates (strictly from `wius` table, never estimates)."""
        clean: Dict[str, Tuple[float, float]] = {}
        for wid, pt in (wiu_gps_map or {}).items():
            if not wid or not isinstance(pt, (tuple, list)) or len(pt) < 2:
                continue
            try:
                lat, lon = float(pt[0]), float(pt[1])
                if -85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0):
                    clean[str(wid).strip()] = (lat, lon)
            except (ValueError, TypeError):
                pass
        with self._lock:
            self._known_wiu_gps = clean

    def _persist_sensor_anchor_unlocked(
        self, sid: str, ident: str, proto_code: int, lat: float, lon: float,
        pos_sigma_km: float, gain_bias_db: float, source: str, updated_ts: float,
    ):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO sensor_anchors (sensor_id, identity, proto, lat, lon, pos_sigma_km, gain_bias_db, source, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity, proto) DO UPDATE SET
                    lat=excluded.lat,
                    lon=excluded.lon,
                    pos_sigma_km=excluded.pos_sigma_km,
                    gain_bias_db=excluded.gain_bias_db,
                    source=excluded.source,
                    updated_ts=excluded.updated_ts
                """,
                (sid, ident, int(proto_code), lat, lon, pos_sigma_km, gain_bias_db, source, updated_ts),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Phase 1: Packet Ingestion & Multi-SDR Coincidence Aggregation
    # ------------------------------------------------------------------

    def ingest_put_rows(self, rows: List[tuple]) -> int:
        """
        Ingests raw Banc PUT tuples BEFORE SQLite PK deduplication overwrites signal_strength:
          (date_str, unix_ts, identity, hex_data, channel, sig_str, pkt_hash, b1_rssi, b2_proto, b3_tag, stamp_rx)
        Returns number of valid transmitter observations recorded.
        """
        if not rows:
            return 0
        now = time.time()
        recorded = 0
        moving_to_update: List[Tuple[str, Dict[str, Any], Dict[str, Tuple[float, float]]]] = []

        with self._lock:
            for row in rows:
                try:
                    unix_ts = float(row[1]) if row[1] else now
                    ident = str(row[2] or "").strip()
                    hex_data = str(row[3] or "").strip()
                    channel = int(row[4] or 0)
                    sig_str = str(row[5] or "").strip()
                    b1_val = float(row[7]) if row[7] is not None else 0.0
                    b2_code = int(row[8]) if row[8] is not None else PKT_PROTO_PTC
                    b3_tag = str(row[9] or "")
                    stamp_rx = float(row[10]) if len(row) > 10 and row[10] else now
                except (ValueError, TypeError, IndexError):
                    continue

                if not ident or not hex_data:
                    continue

                # Require genuine numeric RSSI (ignore legacy "NA" / 0.0 placeholder)
                rssi: Optional[float] = None
                if sig_str and sig_str.upper() != "NA":
                    try:
                        rssi = float(sig_str)
                    except ValueError:
                        rssi = None
                if rssi is None and b1_val != 0.0 and -130.0 <= b1_val <= 10.0:
                    rssi = b1_val
                if rssi is None or not (-130.0 <= rssi <= 10.0):
                    continue

                # Use packet timestamp if plausible (within 30s of server rx), else server rx time
                obs_ts = unix_ts if abs(stamp_rx - unix_ts) <= 30.0 else stamp_rx

                tx = extract_tx_observation(b2_code, hex_data, channel=channel, b3_tag=b3_tag)
                if tx is None or not tx.get("direct", False):
                    continue

                sid = self._get_or_create_sensor_id_unlocked(ident, b2_code)
                if sid in self._sensors:
                    self._sensors[sid]["last_seen_ts"] = max(self._sensors[sid]["last_seen_ts"], obs_ts)

                # Feed known-WIU calibration buffer whenever a PTC sensor hears a verified WIU
                if tx["category"] == "wiu" and tx["unit_id"] in self._known_wiu_gps:
                    s_cal = self._cal_wiu_samples.setdefault(sid, {})
                    dq = s_cal.get(tx["unit_id"])
                    if dq is None:
                        dq = deque(maxlen=40)
                        s_cal[tx["unit_id"]] = dq
                    dq.append(rssi)

                # Track individual sensor RSSI trend for moving trains (used for 2-SDR handoff heading)
                if not tx["stationary"]:
                    tr = self._moving_tracks.get(tx["tx_key"])
                    if tr is None:
                        tr = {
                            "tx": tx,
                            "raw_lat": None,
                            "raw_lon": None,
                            "prev_points": deque(maxlen=24),
                            "sensor_trends": {},
                            "burst_count": 0,
                            "last_ts": obs_ts,
                        }
                        self._moving_tracks[tx["tx_key"]] = tr
                    tr["last_ts"] = max(tr["last_ts"], obs_ts)
                    st_dq = tr["sensor_trends"].get(sid)
                    if st_dq is None:
                        st_dq = deque(maxlen=20)
                        tr["sensor_trends"][sid] = st_dq
                    st_dq.append((obs_ts, rssi))

                # Phase 1: Insert into multi-SDR coincidence window keyed by (proto_code, fingerprint)
                ckey = (b2_code, tx["fingerprint"])
                entry = self._coincidences.get(ckey)
                if entry is not None and abs(obs_ts - entry["first_ts"]) > COINCIDENCE_WINDOW_SEC:
                    # Previous identical payload (e.g. steady EOT 65s later) -> flush & start fresh burst!
                    self._flush_coincidence_unlocked(entry, moving_to_update)
                    entry = None

                if entry is None:
                    entry = {
                        "tx": tx,
                        "first_ts": obs_ts,
                        "last_ts": obs_ts,
                        "obs": {sid: (rssi, obs_ts)},
                        "flushed_count": 0,
                    }
                    self._coincidences[ckey] = entry
                    self._coincidence_order.append((obs_ts, ckey))
                else:
                    entry["obs"][sid] = (rssi, obs_ts)
                    entry["last_ts"] = max(entry["last_ts"], obs_ts)
                    # As soon as >=2 distinct sensors observe this exact burst, fold into aggregates
                    if len(entry["obs"]) >= 2 and len(entry["obs"]) > entry["flushed_count"]:
                        self._flush_coincidence_unlocked(entry, moving_to_update)
                        entry["flushed_count"] = len(entry["obs"])

                recorded += 1

            # Evict expired coincidence entries
            self._prune_coincidences_unlocked(now, moving_to_update)

        # Solve any newly updated moving train bursts outside lock contention if needed
        if moving_to_update:
            with self._lock:
                for tx_key, tx_info, obs_map in moving_to_update:
                    self._update_moving_train_estimate_unlocked(tx_key, tx_info, obs_map)

        return recorded

    def _flush_coincidence_unlocked(
        self,
        entry: Dict[str, Any],
        moving_out: List[Tuple[str, Dict[str, Any], Dict[str, Tuple[float, float]]]],
    ):
        obs_map = entry["obs"]
        if len(obs_map) < 2:
            return
        tx = entry["tx"]
        tx_key = tx["tx_key"]
        if tx["stationary"]:
            agg = self._stationary_agg.get(tx_key)
            if agg is None:
                agg = {
                    "tx": tx,
                    "per_sensor": {},
                    "coincidence_bursts": 0,
                    "last_ts": entry["last_ts"],
                }
                self._stationary_agg[tx_key] = agg
            agg["coincidence_bursts"] += 1
            agg["last_ts"] = max(agg["last_ts"], entry["last_ts"])
            for sid, (rssi, _ts) in obs_map.items():
                dq = agg["per_sensor"].get(sid)
                if dq is None:
                    dq = deque(maxlen=64)
                    agg["per_sensor"][sid] = dq
                dq.append(rssi)
        else:
            moving_out.append((tx_key, tx, dict(obs_map)))

    def _prune_coincidences_unlocked(
        self,
        now: float,
        moving_out: List[Tuple[str, Dict[str, Any], Dict[str, Tuple[float, float]]]],
    ):
        while self._coincidence_order and (
            len(self._coincidences) > MAX_COINCIDENCE_BURSTS
            or (now - self._coincidence_order[0][0]) > (COINCIDENCE_WINDOW_SEC * 2.5)
        ):
            _, ckey = self._coincidence_order.popleft()
            entry = self._coincidences.pop(ckey, None)
            if entry is not None and len(entry["obs"]) >= 2 and entry["flushed_count"] < len(entry["obs"]):
                self._flush_coincidence_unlocked(entry, moving_out)

    # ------------------------------------------------------------------
    # Phase 2: Auto-Calibration & Stationary Transmitter Solver
    # ------------------------------------------------------------------

    def calibrate_sensors_from_known_wius(self) -> int:
        """
        1. For PTC sensors without an explicit opt-in coordinate, estimates an approximate
           private sensor anchor from >= 3 verified known-GPS WIUs it hears (zero-input!).
        2. For every anchored PTC sensor hearing >= 2 verified known-GPS WIUs, solves its
           relative gain bias G_i (dB) so SDRs with different antennas/LNAs/gain settings
           align onto a common propagation scale.
        Returns number of sensors calibrated.
        """
        calibrated = 0
        with self._lock:
            if not self._known_wiu_gps or not self._cal_wiu_samples:
                return 0

            # Step 2a: Zero-input PTC sensor anchor inference for sensors lacking opt-in GPS
            # Reverse lookup identity for persistence
            sid_to_ident: Dict[str, Tuple[str, int]] = {
                sid: k for k, sid in self._ident_to_sensor_id.items()
            }
            for sid, wiu_map in self._cal_wiu_samples.items():
                existing = self._sensors.get(sid)
                if existing is not None and existing.get("source") == "user_private_optin":
                    continue
                pts = []
                for wid, dq in wiu_map.items():
                    if len(dq) >= 1 and wid in self._known_wiu_gps:
                        wlat, wlon = self._known_wiu_gps[wid]
                        pts.append((wlat, wlon, _robust_median(dq)))
                if len(pts) >= 3:
                    inferred = solve_sensor_position_from_known_wius(pts, path_loss_exp=DEFAULT_PATH_LOSS_EXP[PKT_PROTO_PTC])
                    if inferred is not None:
                        ilat, ilon, isig = inferred
                        q_lat, q_lon = quantize_latlon(ilat, ilon, PRIVATE_SENSOR_GRID_DEG)
                        now = time.time()
                        gb = float(existing.get("gain_bias_db", 0.0)) if existing else 0.0
                        self._sensors[sid] = {
                            "proto_code": PKT_PROTO_PTC,
                            "lat": q_lat,
                            "lon": q_lon,
                            "pos_sigma_km": isig,
                            "gain_bias_db": gb,
                            "source": "wiu_calibrated_auto",
                            "last_seen_ts": max(now, float(existing.get("last_seen_ts", 0.0))) if existing else now,
                        }
                        if sid in sid_to_ident:
                            ident, pcode = sid_to_ident[sid]
                            self._persist_sensor_anchor_unlocked(
                                sid, ident, pcode, q_lat, q_lon, isig, gb, "wiu_calibrated_auto", now
                            )

            # Step 2b: Cross-sensor relative gain-bias calibration using verified WIUs
            # For each sensor i and verified WIU w: residual_raw = R_{i,w} + 10*n*log10(d_{i,w})
            # Sensor bias relative to network reference C_0 is median_w(residual_raw) - C_0!
            n_exp = DEFAULT_PATH_LOSS_EXP[PKT_PROTO_PTC]
            per_sensor_raw_offsets: Dict[str, List[float]] = {}
            all_offsets: List[float] = []
            for sid, wiu_map in self._cal_wiu_samples.items():
                srec = self._sensors.get(sid)
                if srec is None:
                    continue
                slat, slon = float(srec["lat"]), float(srec["lon"])
                offsets = []
                for wid, dq in wiu_map.items():
                    if not dq or wid not in self._known_wiu_gps:
                        continue
                    wlat, wlon = self._known_wiu_gps[wid]
                    d_km = max(0.25, haversine_km(slat, slon, wlat, wlon))
                    if d_km > 85.0:
                        continue
                    r_med = _robust_median(dq)
                    # Estimated zero-distance constant + sensor gain bias: R + 10*n*log10(d)
                    c_iw = r_med + 10.0 * n_exp * math.log10(d_km)
                    offsets.append(c_iw)
                if len(offsets) >= 2:
                    per_sensor_raw_offsets[sid] = offsets
                    all_offsets.append(_robust_median(offsets))

            if len(all_offsets) >= 2:
                network_ref = _robust_median(all_offsets)
                now = time.time()
                for sid, offsets in per_sensor_raw_offsets.items():
                    raw_bias = _robust_median(offsets) - network_ref
                    # Regularize mildly when few WIUs observed so noise doesn't overfit
                    shrink = min(1.0, len(offsets) / 4.0)
                    bias = max(-MAX_GAIN_BIAS_DB, min(MAX_GAIN_BIAS_DB, round(raw_bias * shrink, 2)))
                    srec = self._sensors[sid]
                    srec["gain_bias_db"] = bias
                    calibrated += 1
                    if sid in sid_to_ident:
                        ident, pcode = sid_to_ident[sid]
                        self._persist_sensor_anchor_unlocked(
                            sid, ident, pcode, srec["lat"], srec["lon"],
                            srec["pos_sigma_km"], bias, srec["source"], now
                        )
        return calibrated

    def _build_silent_sensors_unlocked(self, proto_code: int, heard_sids: set, now: float) -> List[Dict[str, Any]]:
        silent = []
        for sid, srec in self._sensors.items():
            if sid in heard_sids:
                continue
            if int(srec["proto_code"]) != int(proto_code):
                continue
            if (now - float(srec.get("last_seen_ts", 0.0))) > ACTIVE_SENSOR_WINDOW_SEC:
                continue
            if srec.get("source") == "user_blurred" and float(srec.get("pos_sigma_km", 10.0)) > 12.0:
                continue
            silent.append({"lat": srec["lat"], "lon": srec["lon"]})
        return silent

    def run_solver_cycle(self) -> int:
        """
        Runs calibration + solves/updates all eligible stationary transmitter estimates
        and prunes expired moving train estimates. Returns number of updated estimates.
        """
        self.calibrate_sensors_from_known_wius()
        now = time.time()
        updated = 0
        to_persist: List[Dict[str, Any]] = []

        with self._lock:
            # 1. Solve stationary transmitters (WIUs, Bases, ATCS MCPs/BCPs, DPU repeaters)
            stale_stat = []
            for tx_key, agg in self._stationary_agg.items():
                if (now - agg["last_ts"]) > STATIONARY_OBS_TTL_SEC:
                    stale_stat.append(tx_key)
                    continue
                tx = agg["tx"]
                # Skip WIUs that already have verified ground-truth GPS in Banc
                if tx["category"] == "wiu" and tx["unit_id"] in self._known_wiu_gps:
                    continue

                pcode = int(tx["proto_code"])
                sensor_obs = []
                heard_sids = set()
                for sid, dq in agg["per_sensor"].items():
                    if not dq or sid not in self._sensors:
                        continue
                    srec = self._sensors[sid]
                    r_med = _robust_median(dq)
                    r_cal = r_med - float(srec.get("gain_bias_db", 0.0))
                    # More bursts reduce multipath std dev smoothly (down to 2.2 dB floor)
                    eff_sigma = max(2.2, DEFAULT_RSSI_SIGMA_DB / math.sqrt(min(16, len(dq))))
                    sensor_obs.append({
                        "lat": srec["lat"],
                        "lon": srec["lon"],
                        "rssi_cal": r_cal,
                        "pos_sigma_km": srec["pos_sigma_km"],
                        "rssi_sigma_db": eff_sigma,
                    })
                    heard_sids.add(sid)

                if len(sensor_obs) < 2:
                    continue
                silent = self._build_silent_sensors_unlocked(pcode, heard_sids, now)
                res = solve_rssi_difference_location(
                    sensor_obs,
                    path_loss_exp=DEFAULT_PATH_LOSS_EXP.get(pcode, 3.0),
                    silent_sensors=silent,
                    stationary=True,
                )
                if res is None:
                    continue

                est_rec = {
                    "tx_key": tx_key,
                    "proto": tx["proto"],
                    "category": tx["category"],
                    "unit_id": tx["unit_id"],
                    "stationary": True,
                    "est_lat": res["est_lat"],
                    "est_lon": res["est_lon"],
                    "est_radius_km": res["est_radius_km"],
                    "confidence": res["confidence"],
                    "heading_deg": None,
                    "heading_cardinal": "",
                    "speed_mph": None,
                    "sample_count": int(agg["coincidence_bursts"]),
                    "updated_ts": now,
                }
                self._estimates[tx_key] = est_rec
                to_persist.append(est_rec)
                updated += 1

            for k in stale_stat:
                self._stationary_agg.pop(k, None)

            # 2. Prune expired moving train tracks & estimates
            expired_mov = [k for k, tr in self._moving_tracks.items() if (now - tr["last_ts"]) > TRAIN_TRACK_TTL_SEC]
            for k in expired_mov:
                self._moving_tracks.pop(k, None)
                self._estimates.pop(k, None)

            expired_est = [
                k for k, e in self._estimates.items()
                if (not e.get("stationary", True)) and (now - float(e.get("updated_ts", 0.0))) > TRAIN_TRACK_TTL_SEC
            ]
            for k in expired_est:
                self._estimates.pop(k, None)

            if to_persist or expired_est:
                self._persist_estimates_unlocked(to_persist, delete_keys=expired_est)

        return updated

    # ------------------------------------------------------------------
    # Phase 3: Moving Train Estimator & Heading Tracker
    # ------------------------------------------------------------------

    def _update_moving_train_estimate_unlocked(
        self,
        tx_key: str,
        tx: Dict[str, Any],
        obs_map: Dict[str, Tuple[float, float]],
    ):
        now = max((ts for _, ts in obs_map.values()), default=time.time())
        pcode = int(tx["proto_code"])

        sensor_obs = []
        heard_sids = set()
        for sid, (rssi, _ts) in obs_map.items():
            srec = self._sensors.get(sid)
            if srec is None:
                continue
            r_cal = float(rssi) - float(srec.get("gain_bias_db", 0.0))
            sensor_obs.append({
                "lat": srec["lat"],
                "lon": srec["lon"],
                "rssi_cal": r_cal,
                "pos_sigma_km": srec["pos_sigma_km"],
                "rssi_sigma_db": DEFAULT_RSSI_SIGMA_DB,
            })
            heard_sids.add(sid)

        if len(sensor_obs) < 2:
            return

        silent = self._build_silent_sensors_unlocked(pcode, heard_sids, now)
        res = solve_rssi_difference_location(
            sensor_obs,
            path_loss_exp=DEFAULT_PATH_LOSS_EXP.get(pcode, 3.2),
            silent_sensors=silent,
            stationary=False,
        )
        if res is None:
            return

        tr = self._moving_tracks.get(tx_key)
        if tr is None:
            tr = {
                "tx": tx,
                "raw_lat": None,
                "raw_lon": None,
                "prev_points": deque(maxlen=24),
                "sensor_trends": {},
                "burst_count": 0,
                "last_ts": now,
            }
            self._moving_tracks[tx_key] = tr

        raw_lat = float(res["_raw_lat"])
        raw_lon = float(res["_raw_lon"])
        # Exponential kinematic smoothing (absorbs single-burst multipath jumps)
        if tr["raw_lat"] is not None and tr["raw_lon"] is not None:
            dt = max(1.0, now - tr["last_ts"])
            alpha = max(0.25, min(0.75, 1.0 - math.exp(-dt / 35.0)))
            sm_lat = alpha * raw_lat + (1.0 - alpha) * float(tr["raw_lat"])
            sm_lon = alpha * raw_lon + (1.0 - alpha) * float(tr["raw_lon"])
        else:
            sm_lat, sm_lon = raw_lat, raw_lon

        tr["raw_lat"] = sm_lat
        tr["raw_lon"] = sm_lon
        tr["burst_count"] += 1
        tr["last_ts"] = now
        tr["prev_points"].append((now, sm_lat, sm_lon))

        # Estimate heading & rough speed from smoothed track displacement (or 2-SDR RSSI handoff trend)
        hdg_deg: Optional[float] = None
        spd_mph: Optional[float] = None
        pts = list(tr["prev_points"])
        if len(pts) >= 2:
            # Find oldest point within last 180s with >= 0.8 km displacement
            for p_ts, p_lat, p_lon in pts[:-1]:
                dt_s = now - p_ts
                if 10.0 <= dt_s <= 240.0:
                    dist_km = haversine_km(p_lat, p_lon, sm_lat, sm_lon)
                    if dist_km >= 0.8:
                        hdg_deg = round(bearing_deg(p_lat, p_lon, sm_lat, sm_lon), 1)
                        raw_mph = (dist_km * KM_TO_MILES) / (dt_s / 3600.0)
                        if 2.0 <= raw_mph <= 95.0:
                            spd_mph = round(raw_mph, 1)
                        break

        # Fallback 2-SDR RSSI handoff heading: if sensor A's RSSI is falling and B's is rising, heading ~ A -> B
        if hdg_deg is None and len(tr["sensor_trends"]) >= 2:
            falling = []
            rising = []
            for sid, dq in tr["sensor_trends"].items():
                if sid not in self._sensors or len(dq) < 4:
                    continue
                vals = [v for t, v in dq if (now - t) <= 180.0]
                if len(vals) < 4:
                    continue
                half = len(vals) // 2
                slope_db = _robust_median(vals[half:]) - _robust_median(vals[:half])
                srec = self._sensors[sid]
                if slope_db <= -2.0:
                    falling.append((slope_db, srec["lat"], srec["lon"]))
                elif slope_db >= 2.0:
                    rising.append((slope_db, srec["lat"], srec["lon"]))
            if falling and rising:
                falling.sort(key=lambda x: x[0])        # most negative (receding from)
                rising.sort(key=lambda x: -x[0])        # most positive (approaching)
                f_lat, f_lon = falling[0][1], falling[0][2]
                r_lat, r_lon = rising[0][1], rising[0][2]
                if haversine_km(f_lat, f_lon, r_lat, r_lon) >= MIN_SENSOR_SEPARATION_KM:
                    hdg_deg = round(bearing_deg(f_lat, f_lon, r_lat, r_lon), 1)

        distinct_k = int(res.get("_distinct_k", 2))
        q_step = K2_OUT_GRID_DEG if distinct_k == 2 else TRAIN_OUT_GRID_DEG
        q_lat, q_lon = quantize_latlon(sm_lat, sm_lon, q_step)

        est_rec = {
            "tx_key": tx_key,
            "proto": tx["proto"],
            "category": tx["category"],
            "unit_id": tx["unit_id"],
            "stationary": False,
            "est_lat": round(q_lat, 5),
            "est_lon": round(q_lon, 5),
            "est_radius_km": res["est_radius_km"],
            "confidence": res["confidence"],
            "heading_deg": hdg_deg,
            "heading_cardinal": heading_to_cardinal(hdg_deg),
            "speed_mph": spd_mph,
            "sample_count": int(tr["burst_count"]),
            "updated_ts": now,
        }
        self._estimates[tx_key] = est_rec
        self._persist_estimates_unlocked([est_rec])

    def _persist_estimates_unlocked(
        self,
        records: List[Dict[str, Any]],
        delete_keys: Optional[List[str]] = None,
    ):
        conn = sqlite3.connect(self.db_path)
        try:
            if records:
                conn.executemany(
                    """
                    INSERT INTO tx_estimates (
                        tx_key, proto, category, unit_id, stationary,
                        est_lat, est_lon, est_radius_km, confidence,
                        heading_deg, heading_cardinal, speed_mph, sample_count, updated_ts
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tx_key) DO UPDATE SET
                        proto=excluded.proto,
                        category=excluded.category,
                        unit_id=excluded.unit_id,
                        stationary=excluded.stationary,
                        est_lat=excluded.est_lat,
                        est_lon=excluded.est_lon,
                        est_radius_km=excluded.est_radius_km,
                        confidence=excluded.confidence,
                        heading_deg=excluded.heading_deg,
                        heading_cardinal=excluded.heading_cardinal,
                        speed_mph=excluded.speed_mph,
                        sample_count=excluded.sample_count,
                        updated_ts=excluded.updated_ts
                    """,
                    [
                        (
                            r["tx_key"], r["proto"], r["category"], r["unit_id"],
                            1 if r.get("stationary") else 0,
                            r["est_lat"], r["est_lon"], r["est_radius_km"], r["confidence"],
                            r.get("heading_deg"), r.get("heading_cardinal", ""),
                            r.get("speed_mph"), int(r.get("sample_count", 0)), float(r["updated_ts"]),
                        )
                        for r in records
                    ],
                )
            if delete_keys:
                conn.executemany("DELETE FROM tx_estimates WHERE tx_key = ?", [(k,) for k in delete_keys])
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public Privacy-Safe Query API (Served by Banc GET_ESTIMATES)
    # ------------------------------------------------------------------

    def get_public_estimates(
        self,
        category: Optional[str] = None,
        since_ts: Optional[float] = None,
        geo_filter: Optional[Tuple[float, float, float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Returns strictly anonymized transmitter location estimates.
        NEVER includes sensor_id, identity, sensor coordinates, or per-SDR RSSI.
        """
        cat_f = str(category or "").strip().lower()
        if cat_f in ("", "all", "*"):
            cat_f = ""
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for est in self._estimates.values():
                if not est.get("stationary", True) and (now - float(est.get("updated_ts", 0.0))) > TRAIN_TRACK_TTL_SEC:
                    continue
                if cat_f and str(est.get("category", "")).lower() != cat_f:
                    continue
                if since_ts is not None and float(est.get("updated_ts", 0.0)) < float(since_ts):
                    continue
                if geo_filter is not None:
                    glat, glon, grad_km = geo_filter
                    dist = haversine_km(glat, glon, float(est["est_lat"]), float(est["est_lon"]))
                    if dist > (float(grad_km) + float(est["est_radius_km"])):
                        continue
                # Explicit allow-list of public keys (defense-in-depth against leaking sensor metadata)
                out.append({
                    "tx_key": est["tx_key"],
                    "proto": est["proto"],
                    "category": est["category"],
                    "unit_id": est["unit_id"],
                    "stationary": bool(est["stationary"]),
                    "est_lat": float(est["est_lat"]),
                    "est_lon": float(est["est_lon"]),
                    "est_radius_km": float(est["est_radius_km"]),
                    "confidence": str(est["confidence"]),
                    "heading_deg": est.get("heading_deg"),
                    "heading_cardinal": str(est.get("heading_cardinal") or ""),
                    "speed_mph": est.get("speed_mph"),
                    "sample_count": int(est.get("sample_count", 0)),
                    "updated_ts": float(est["updated_ts"]),
                })
        out.sort(key=lambda r: (-float(r["updated_ts"]), r["tx_key"]))
        return out

    def get_internal_summary(self) -> Dict[str, int]:
        """Aggregate counts only (for Banc TUI telemetry); zero coordinates or identities."""
        with self._lock:
            wiu_n = sum(1 for e in self._estimates.values() if e.get("category") == "wiu")
            train_n = sum(1 for e in self._estimates.values() if e.get("category") == "train")
            other_n = len(self._estimates) - wiu_n - train_n
            return {
                "sensors": len(self._sensors),
                "estimates_total": len(self._estimates),
                "estimates_wiu": wiu_n,
                "estimates_train": train_n,
                "estimates_other": other_n,
            }
