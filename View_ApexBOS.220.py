#!/usr/bin/env python3
# Copyright by AB
# Licensed under the MIT License.
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

"""
Pygame Railroad Dispatcher Display Renderer (v26.117.2)
Features:
  - Honors label_pos ('top', 'bottom', 'left', 'right') for all element labels.
  - HUD Toggles: "Lbls(On/Off)" (Hides/Shows all captions) and "InfTrns(On/Off)" (Hides/Shows inferred blue train badges).
  - Train badge automatically anchors to the opposite vertical side of the track's label_pos.
  - Multi-Head Signal Stacking, Inactivity Aging, Cache Map Insets, DVR Replay.
"""

import argparse
import copy
import json
import math
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
import urllib.request
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
from collections import deque
from typing import Dict, Optional, Tuple, Any, List

import pygame
import mosfet_client
from banc_coverage_modal import BancCoverageMapModal
from version import VERSION, __version__
DEFAULT_MCAST_GROUP = "224.1.1.1"
DEFAULT_MCAST_PORT = 5007
DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 5008
HISTORY_MAX_LEN = 35

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


def _format_channel_val(v: Any) -> str:
    if v is None:
        return "---"
    if isinstance(v, dict):
        parts = [f"{k}: {val}" for k, val in v.items() if not str(k).startswith("_")]
        return ", ".join(parts) if parts else "{}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)
LOGICAL_GRID_SCALE = 24.0
MAP_CACHE_DIR = ".map_tile_cache"

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
COLOR_TRACK_FLASH_DARK_RED = (110, 15, 20)
COLOR_TRACK_STALE_GRAY = (82, 88, 98)
COLOR_TURNOUT_TIE = (100, 112, 130)
COLOR_SIGNAL_MAST = (165, 175, 190)

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
COLOR_HUD_BG = (20, 26, 36, 228)
COLOR_HUD_BORDER = (65, 85, 115)
COLOR_EDIT_ACCENT = (255, 185, 40)
COLOR_SELECT_RING = (0, 240, 255)


# =====================================================================
# USER PREFERENCES (SCOPED TO SERVED LAYOUT FILENAME)
# =====================================================================

class UserPreferences:
    def __init__(self, layout_filename: str = "default_layout.json"):
        self.view_state: Dict[str, float] = {}
        self.set_layout_filename(layout_filename)

    def set_layout_filename(self, layout_filename: str):
        safe_base = os.path.basename(layout_filename or "default_layout.json")
        self.layout_filename = safe_base
        self.filepath = f"prefs_{safe_base}"
        self.overrides: Dict[str, Dict[str, Any]] = {}
        self.view_state: Dict[str, float] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.overrides = data.get("element_overrides", {})
                    self.view_state = data.get("view_state", {})
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[WARN] Could not load preferences '{self.filepath}': {exc}")

    def save(self, view_state: Optional[Dict[str, float]] = None):
        if view_state is not None:
            self.set_view(
                view_state.get("cam_x", 0.0),
                view_state.get("cam_y", 0.0),
                view_state.get("zoom", 1.0),
            )
        data = {
            "version": VERSION,
            "layout_filename": self.layout_filename,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "element_overrides": self.overrides,
            "view_state": self.view_state,
        }
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            print(f"[ERROR] Failed saving preferences '{self.filepath}': {exc}")

    def set_view(self, cam_x: float, cam_y: float, zoom: float):
        self.view_state = {
            "cam_x": round(float(cam_x), 2),
            "cam_y": round(float(cam_y), 2),
            "zoom": round(float(zoom), 4),
        }

    def get_view(self) -> Optional[Dict[str, float]]:
        if (
            isinstance(self.view_state, dict)
            and "cam_x" in self.view_state
            and "cam_y" in self.view_state
            and "zoom" in self.view_state
        ):
            return self.view_state
        return None

    def set_position(self, elem_id: str, x: float, y: float):
        rec = self.overrides.setdefault(elem_id, {})
        rec["x"] = round(x, 2)
        rec["y"] = round(y, 2)

    def set_label(self, elem_id: str, label: str):
        rec = self.overrides.setdefault(elem_id, {})
        rec["label"] = label

    def set_size(self, elem_id: str, width: int, height: int):
        rec = self.overrides.setdefault(elem_id, {})
        rec["width"] = max(240, int(width))
        rec["height"] = max(80, int(height))

    def clear_all(self):
        self.overrides.clear()
        self.view_state.clear()
        if os.path.exists(self.filepath):
            try:
                os.remove(self.filepath)
            except OSError:
                pass

    def get_pos(self, elem_id: str, default_x: float, default_y: float) -> Tuple[float, float]:
        ovr = self.overrides.get(elem_id)
        if ovr and "x" in ovr and "y" in ovr:
            return float(ovr["x"]), float(ovr["y"])
        return float(default_x), float(default_y)

    def get_label(self, elem_id: str, default_label: str) -> str:
        ovr = self.overrides.get(elem_id)
        if ovr and "label" in ovr:
            return str(ovr["label"])
        return default_label

    def get_size(self, elem_id: str, default_w: int, default_h: int) -> Tuple[int, int]:
        ovr = self.overrides.get(elem_id)
        w = int(ovr["width"]) if (ovr and "width" in ovr and ovr["width"]) else int(default_w)
        h = int(ovr["height"]) if (ovr and "height" in ovr and ovr["height"]) else int(default_h)
        return max(240, w), max(80, h)


# =====================================================================
# OPENSTREETMAP TILE CACHE & GPS MAP INSET RENDERER
# =====================================================================

class OSMTileMapManager:
    def __init__(self, cache_dir: str = MAP_CACHE_DIR, zoom: int = 14):
        self.cache_dir = cache_dir
        self.zoom = zoom
        os.makedirs(self.cache_dir, exist_ok=True)
        self.surface_cache: Dict[Tuple[str, int, int, int], pygame.Surface] = {}
        self.pending: set = set()
        self.lock = threading.Lock()

    @staticmethod
    def latlon_to_tile_xy(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
        lat_rad = math.radians(max(-85.0511, min(85.0511, lat)))
        n = 2.0 ** zoom
        xtile = (lon + 180.0) / 360.0 * n
        ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return xtile, ytile

    @staticmethod
    def tile_xy_to_latlon(x: float, y: float, zoom: int) -> Tuple[float, float]:
        n = 2.0 ** zoom
        lon_deg = (x / n) * 360.0 - 180.0
        val = max(-50.0, min(50.0, math.pi * (1.0 - 2.0 * y / n)))
        lat_rad = math.atan(math.sinh(val))
        lat_deg = math.degrees(lat_rad)
        return max(-85.0511, min(85.0511, lat_deg)), max(-180.0, min(180.0, lon_deg))

    @staticmethod
    def latlon_to_world_px(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
        tx, ty = OSMTileMapManager.latlon_to_tile_xy(lat, lon, zoom)
        return tx * 256.0, ty * 256.0

    @staticmethod
    def world_px_to_latlon(wx: float, wy: float, zoom: int) -> Tuple[float, float]:
        return OSMTileMapManager.tile_xy_to_latlon(wx / 256.0, wy / 256.0, zoom)

    def _tile_filepath(self, z: int, x: int, y: int, layer: str = "osm") -> str:
        ext = "jpg" if layer == "sat" else "png"
        return os.path.join(self.cache_dir, f"{layer}_{z}_{x}_{y}.{ext}")

    def _fetch_worker(self, z: int, x: int, y: int, layer: str = "osm"):
        path = self._tile_filepath(z, x, y, layer=layer)
        if not os.path.exists(path):
            if layer == "sat":
                url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            else:
                url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"RailroadDispatcherRenderer/{VERSION} (MIT License)"},
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    data = resp.read()
                with open(path, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        with self.lock:
            self.pending.discard((layer, z, x, y))

    def get_tile_surface(self, z: int, x: int, y: int, layer: str = "osm") -> Optional[pygame.Surface]:
        key = (layer, z, x, y)
        if key in self.surface_cache:
            return self.surface_cache[key]

        path = self._tile_filepath(z, x, y, layer=layer)
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

    def render_gps_inset(
        self,
        target_gps: Tuple[float, float],
        all_gps_points: List[Tuple[float, float, bool]],
        width: int = 280,
        height: int = 210,
    ) -> pygame.Surface:
        inset = pygame.Surface((width, height))
        inset.fill((26, 34, 46))

        z = self.zoom
        cx_tile, cy_tile = self.latlon_to_tile_xy(target_gps[0], target_gps[1], z)
        center_px_x = cx_tile * 256.0
        center_px_y = cy_tile * 256.0

        min_tx = int(math.floor((center_px_x - width / 2) / 256.0))
        max_tx = int(math.floor((center_px_x + width / 2) / 256.0))
        min_ty = int(math.floor((center_px_y - height / 2) / 256.0))
        max_ty = int(math.floor((center_px_y + height / 2) / 256.0))

        n_tiles = 2 ** z
        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles
                if ty < 0 or ty >= n_tiles:
                    continue
                tile_surf = self.get_tile_surface(z, wrapped_tx, ty)
                dest_x = int(width / 2 + (tx * 256.0 - center_px_x))
                dest_y = int(height / 2 + (ty * 256.0 - center_px_y))
                if tile_surf:
                    inset.blit(tile_surf, (dest_x, dest_y))
                else:
                    pygame.draw.rect(inset, (32, 42, 56), (dest_x, dest_y, 256, 256), 1)

        for lat, lon, is_sel in all_gps_points:
            if is_sel:
                continue
            ptx, pty = self.latlon_to_tile_xy(lat, lon, z)
            px = int(width / 2 + (ptx * 256.0 - center_px_x))
            py = int(height / 2 + (pty * 256.0 - center_px_y))
            if 0 <= px < width and 0 <= py < height:
                pygame.draw.circle(inset, (40, 45, 52), (px, py), 6)
                pygame.draw.circle(inset, (145, 152, 162), (px, py), 4)

        for lat, lon, is_sel in all_gps_points:
            if not is_sel:
                continue
            ptx, pty = self.latlon_to_tile_xy(lat, lon, z)
            px = int(width / 2 + (ptx * 256.0 - center_px_x))
            py = int(height / 2 + (pty * 256.0 - center_px_y))
            pygame.draw.line(inset, (20, 20, 25), (px, py), (px, py - 10), 3)
            pygame.draw.circle(inset, (255, 255, 255), (px, py - 10), 8)
            pygame.draw.circle(inset, (0, 235, 255), (px, py - 10), 6)
            pygame.draw.circle(inset, (255, 230, 40), (px, py - 10), 3)

        pygame.draw.rect(inset, COLOR_SELECT_RING, (0, 0, width, height), 2)
        return inset


# =====================================================================
# COVERAGE MAP GEOMETRY HELPERS & FULLSCREEN INTERACTIVE VIEW
# =====================================================================

def convex_hull_2d(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 1:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def buffered_hull_2d(points: List[Tuple[float, float]], buffer_px: float = 30.0, num_angles: int = 16) -> List[Tuple[float, float]]:
    if not points:
        return []
    base_hull = convex_hull_2d(points)
    offset_pts = []
    angles = [2.0 * math.pi * i / num_angles for i in range(num_angles)]
    for vx, vy in base_hull:
        for a in angles:
            offset_pts.append((vx + buffer_px * math.cos(a), vy + buffer_px * math.sin(a)))
    return convex_hull_2d(offset_pts)


def draw_transparent_polygon(surface: pygame.Surface, color_rgba: Tuple[int, int, int, int], points: List[Tuple[float, float]], border_color: Optional[Tuple[int, int, int, int]] = None, border_width: int = 2):
    if len(points) < 3:
        return
    min_x = int(math.floor(min(p[0] for p in points)))
    max_x = int(math.ceil(max(p[0] for p in points)))
    min_y = int(math.floor(min(p[1] for p in points)))
    max_y = int(math.ceil(max(p[1] for p in points)))
    sw, sh = surface.get_size()
    if min_x >= sw or max_x < 0 or min_y >= sh or max_y < 0:
        return
    w = max(1, max_x - min_x + 1)
    h = max(1, max_y - min_y + 1)
    poly_surf = pygame.Surface((w, h), pygame.SRCALPHA)
    rel_pts = [(p[0] - min_x, p[1] - min_y) for p in points]
    pygame.draw.polygon(poly_surf, color_rgba, rel_pts)
    if border_color:
        pygame.draw.polygon(poly_surf, border_color, rel_pts, border_width)
    surface.blit(poly_surf, (min_x, min_y))


def haversine_distance_miles(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    lat1, lon1 = p1
    lat2, lon2 = p2
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c

def calculate_bearing_degrees(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return bearing

def bearing_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]

def rssi_to_coverage_color(rssi: Optional[float], min_rssi: Optional[float] = None, max_rssi: Optional[float] = None, is_active: bool = True) -> Tuple[int, int, int]:
    """Maps average RSSI in dBm to a color scale:
    - Lowest average RSSI received so far -> Light Blue (100, 205, 255)
    - Highest average RSSI received so far -> Hot Magenta (255, 20, 180)
    - Smoothly transitions through cyan-blue, periwinkle, lavender, violet, fuchsia, hot magenta.
    - Dim slate gray (125, 140, 155) for standby/inactive elements.
    """
    if not is_active:
        return (125, 140, 155)
    if rssi is None:
        return (100, 205, 255)  # Default light blue for active with unknown RSSI
    try:
        val = float(rssi)
    except (ValueError, TypeError):
        return (100, 205, 255)

    low_bound = min_rssi if min_rssi is not None else -105.0
    high_bound = max_rssi if max_rssi is not None else -50.0

    if high_bound <= low_bound:
        t = 0.0  # Base light blue if bounds are identical
    else:
        t = (val - low_bound) / (high_bound - low_bound)
        t = max(0.0, min(1.0, t))

    # Low strength: Light Blue (100, 205, 255) -> High strength: Hot Magenta (255, 20, 180)
    r = int(100 + t * (255 - 100))
    g = int(205 + t * (20 - 205))
    b = int(255 + t * (180 - 255))
    return (r, g, b)

def packets_to_pulse_frequency(packets: int) -> float:
    """Calculates subtle, scaled-down pulsing frequency (rad/sec) based on the number of packets received.
    Low packet count -> slow, calm pulsation (~0.7 rad/s, ~0.11 Hz, ~9 sec period).
    High packet count -> gentle, moderate pulse (~2.0 rad/s, ~0.32 Hz, ~3 sec period).
    """
    pkts = max(0, int(packets))
    if pkts <= 0:
        return 0.0
    factor = min(1.0, (pkts / 250.0) ** 0.5)
    return 0.7 + 1.3 * factor

class CoverageMapView:
    """Large zoomable and pan-able Coverage Map for the Railroad Dispatcher Viewer.
    Plots all WIUs with GPS, a green transparent overlay encompassing all GPS objects
    that have received packets, and a light gray overlay encompassing all layout GPS elements.
    """
    def __init__(self, map_manager: OSMTileMapManager):
        self.map_manager = map_manager
        self.zoom: int = 12
        self.center_lat: float = 34.07
        self.center_lon: float = -117.20
        self.layer: str = "osm"  # "osm" or "sat"
        self.is_panning: bool = False
        self.pan_start_mouse: Tuple[int, int] = (0, 0)
        self.pan_start_world: Tuple[float, float] = (0.0, 0.0)
        self.hovered_wiu: Optional[dict] = None
        self.buttons: Dict[str, pygame.Rect] = {}
        self.last_all_points: List[Tuple[float, float]] = []
        self.last_vp_rect: pygame.Rect = pygame.Rect(0, 46, 1200, 700)
        self.fitted_layout_hash: Optional[int] = None
        self.request_close: bool = False
        self.measure_mode: bool = False
        self.measure_points: List[dict] = []
        self.measure_hover: Optional[dict] = None
        self.measure_mouse_pos: Tuple[int, int] = (0, 0)
        self.min_avg_rssi_seen: Optional[float] = None
        self.max_avg_rssi_seen: Optional[float] = None

    def auto_fit(self, points: List[Tuple[float, float]], vp_rect: pygame.Rect):
        if not points or vp_rect.width <= 0 or vp_rect.height <= 0:
            return
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        self.center_lat = (min_lat + max_lat) / 2.0
        self.center_lon = (min_lon + max_lon) / 2.0

        if len(points) == 1:
            self.zoom = 14
            return

        pad_w = max(100, vp_rect.width - 160)
        pad_h = max(100, vp_rect.height - 160)

        best_z = 8
        for z in range(18, 4, -1):
            w_pts = [OSMTileMapManager.latlon_to_world_px(lat, lon, z) for lat, lon in points]
            min_wx = min(p[0] for p in w_pts)
            max_wx = max(p[0] for p in w_pts)
            min_wy = min(p[1] for p in w_pts)
            max_wy = max(p[1] for p in w_pts)
            if (max_wx - min_wx) <= pad_w and (max_wy - min_wy) <= pad_h:
                best_z = z
                break
        self.zoom = max(6, min(17, best_z))

    def zoom_at_mouse(self, mouse_pos: Tuple[int, int], delta_zoom: int, vp_rect: pygame.Rect):
        new_z = max(5, min(18, self.zoom + delta_zoom))
        if new_z == self.zoom:
            return
        mx, my = mouse_pos
        cur_cwx, cur_cwy = OSMTileMapManager.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
        cur_wx = cur_cwx + (mx - vp_rect.centerx)
        cur_wy = cur_cwy + (my - vp_rect.centery)
        cursor_lat, cursor_lon = OSMTileMapManager.world_px_to_latlon(cur_wx, cur_wy, self.zoom)

        self.zoom = new_z

        new_twx, new_twy = OSMTileMapManager.latlon_to_world_px(cursor_lat, cursor_lon, self.zoom)
        new_cwx = new_twx - (mx - vp_rect.centerx)
        new_cwy = new_twy - (my - vp_rect.centery)
        nlat, nlon = OSMTileMapManager.world_px_to_latlon(new_cwx, new_cwy, self.zoom)
        self.center_lat = max(-85.0, min(85.0, nlat))
        self.center_lon = ((nlon + 180.0) % 360.0) - 180.0

    @staticmethod
    def _get_scale_bar_info(m_per_px: float, min_px: int = 70, max_px: int = 180) -> Tuple[int, str]:
        candidates = [
            (100 * 0.3048, "100 ft (30 m)"),
            (250 * 0.3048, "250 ft (76 m)"),
            (500 * 0.3048, "500 ft (152 m)"),
            (1000 * 0.3048, "1,000 ft (305 m)"),
            (0.5 * 1609.344, "0.5 mi (805 m)"),
            (1.0 * 1609.344, "1 mi (1.61 km)"),
            (2.0 * 1609.344, "2 mi (3.22 km)"),
            (5.0 * 1609.344, "5 mi (8.05 km)"),
            (10.0 * 1609.344, "10 mi (16.1 km)"),
            (20.0 * 1609.344, "20 mi (32.2 km)"),
            (50.0 * 1609.344, "50 mi (80.5 km)"),
            (100.0 * 1609.344, "100 mi (161 km)"),
        ]
        for dist_m, label in candidates:
            px = dist_m / m_per_px
            if min_px <= px <= max_px:
                return round(px), label
        px = max(50, round(candidates[0][0] / m_per_px))
        return px, candidates[0][1]

    def trigger_action(self, action_name: str):
        if action_name == "zoom_in":
            self.zoom = min(18, self.zoom + 1)
        elif action_name == "zoom_out":
            self.zoom = max(5, self.zoom - 1)
        elif action_name == "toggle_layer":
            self.layer = "sat" if self.layer == "osm" else "osm"
        elif action_name == "toggle_measure":
            self.measure_mode = not self.measure_mode
            if not self.measure_mode:
                self.measure_hover = None
        elif action_name == "clear_measure":
            self.measure_points.clear()
            self.measure_hover = None
        elif action_name == "fit_all":
            self.auto_fit(self.last_all_points, self.last_vp_rect)
        elif action_name == "close":
            self.request_close = True

    def handle_mouse_down(self, event: pygame.event.Event, top_offset: int) -> bool:
        mx, my = event.pos
        if my < top_offset:
            return False
        if event.button == 1:
            for b_name, rect in self.buttons.items():
                if rect.collidepoint(mx, my):
                    self.trigger_action(b_name)
                    return True
            if self.measure_mode and self.measure_hover:
                self.measure_points.append(dict(self.measure_hover))
                return True
            self.is_panning = True
            self.pan_start_mouse = (mx, my)
            c_wx, c_wy = OSMTileMapManager.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            self.pan_start_world = (c_wx, c_wy)
            return True
        elif event.button in (2, 3):
            if self.measure_mode and event.button == 3 and self.measure_points:
                self.measure_points.pop()
                return True
            self.is_panning = True
            self.pan_start_mouse = (mx, my)
            c_wx, c_wy = OSMTileMapManager.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            self.pan_start_world = (c_wx, c_wy)
            return True
        return False

    def handle_mouse_up(self, event: pygame.event.Event):
        if event.button in (1, 2, 3):
            self.is_panning = False

    def handle_mouse_motion(self, event: pygame.event.Event, vp_rect: pygame.Rect):
        mx, my = event.pos
        self.measure_mouse_pos = (mx, my)
        if self.is_panning:
            dx = mx - self.pan_start_mouse[0]
            dy = my - self.pan_start_mouse[1]
            new_wx = self.pan_start_world[0] - dx
            new_wy = self.pan_start_world[1] - dy
            nlat, nlon = OSMTileMapManager.world_px_to_latlon(new_wx, new_wy, self.zoom)
            self.center_lat = max(-85.0, min(85.0, nlat))
            self.center_lon = ((nlon + 180.0) % 360.0) - 180.0

    def handle_mouse_wheel(self, event: pygame.event.Event, vp_rect: pygame.Rect):
        mx, my = pygame.mouse.get_pos()
        if not vp_rect.collidepoint(mx, my):
            return
        delta = 1 if event.y > 0 else -1
        self.zoom_at_mouse((mx, my), delta, vp_rect)

    def draw(self, screen: pygame.Surface, state: Optional[dict], width: int, height: int,
             font_sm_bold: pygame.font.Font, font_sm: pygame.font.Font, font_md: pygame.font.Font, font_lg: pygame.font.Font):
        vp_y = 46
        vp_h = max(200, height - vp_y)
        vp_w = width
        vp_rect = pygame.Rect(0, vp_y, vp_w, vp_h)
        self.last_vp_rect = vp_rect

        elements = state.get("elements", {}) if state else {}
        all_layout_gps: List[Tuple[float, float]] = []
        active_gps_points: List[Tuple[float, float]] = []
        wiu_markers: Dict[str, dict] = {}

        now = time.time()
        at_wius = state.get("address_table", {}).get("wius", {}) if (state and isinstance(state.get("address_table"), dict)) else {}

        for eid, el in elements.items():
            gps = el.get("gps") or el.get("transmitted_gps")
            if gps and len(gps) == 2:
                try:
                    lat_v, lon_v = float(gps[0]), float(gps[1])
                    all_layout_gps.append((lat_v, lon_v))
                    pkts = int(el.get("packets_received", 0))
                    relayed = int(el.get("packets_relayed", 0))
                    last_ts = float(el.get("last_packet_ts", 0.0))
                    if pkts > 0 or relayed > 0 or last_ts > 0:
                        active_gps_points.append((lat_v, lon_v))

                    wid = el.get("wiu_id") or (el.get("radio_id") if el.get("elem_type") == "base" else None)
                    avg_rssi = el.get("avg_rssi", el.get("rssi_avg"))
                    last_rssi = el.get("last_rssi")

                    # If element doesn't have its own RSSI or packet count, check address table
                    if wid is not None and isinstance(at_wius, dict):
                        wrec = at_wius.get(wid) or at_wius.get(str(wid))
                        if not wrec:
                            try:
                                wrec = at_wius.get(int(wid))
                            except (ValueError, TypeError):
                                pass
                        if isinstance(wrec, dict):
                            if avg_rssi is None:
                                avg_rssi = wrec.get("avg_rssi", wrec.get("rssi_avg"))
                            if last_rssi is None:
                                last_rssi = wrec.get("last_rssi")
                            at_pkts = int(wrec.get("packets", wrec.get("packets_received", 0)))
                            if at_pkts > pkts:
                                pkts = at_pkts
                            at_ts = float(wrec.get("last_time", 0.0))
                            if at_ts > last_ts:
                                last_ts = at_ts

                    if wid is not None or el.get("elem_type") == "base":
                        key = eid
                        disp_name = format_wiu_display_name(el.get("label") or eid, wid or eid)
                        wiu_markers[key] = {
                            "id": eid,
                            "wiu_id": wid,
                            "elem_type": el.get("elem_type", "element"),
                            "name": disp_name,
                            "raw_label": el.get("label", ""),
                            "gps": (lat_v, lon_v),
                            "packets": pkts,
                            "last_packet_ts": last_ts,
                            "last_raw_hex": el.get("last_raw_hex", ""),
                            "is_base": (el.get("elem_type") == "base"),
                            "channel": el.get("channel", 1),
                            "avg_rssi": avg_rssi,
                            "last_rssi": last_rssi,
                        }
                except (ValueError, TypeError):
                    pass

        # Check state address table for active WIUs
        if isinstance(at_wius, dict):
            for wid_k, wdata in at_wius.items():
                if isinstance(wdata, dict):
                    dyn_gps = wdata.get("transmitted_gps") or wdata.get("gps")
                    if dyn_gps and len(dyn_gps) == 2:
                        try:
                            lat_v, lon_v = float(dyn_gps[0]), float(dyn_gps[1])
                            if (lat_v, lon_v) not in all_layout_gps:
                                all_layout_gps.append((lat_v, lon_v))
                            pkts = int(wdata.get("packets", wdata.get("packets_received", 0)))
                            if pkts > 0:
                                if (lat_v, lon_v) not in active_gps_points:
                                    active_gps_points.append((lat_v, lon_v))

                            matched = False
                            for m in wiu_markers.values():
                                try:
                                    if m["wiu_id"] is not None and (int(m["wiu_id"]) == int(wid_k)):
                                        matched = True
                                        m["packets"] = max(m["packets"], pkts)
                                        if m.get("avg_rssi") is None:
                                            m["avg_rssi"] = wdata.get("avg_rssi", wdata.get("rssi_avg"))
                                        if m.get("last_rssi") is None:
                                            m["last_rssi"] = wdata.get("last_rssi")
                                        break
                                except (ValueError, TypeError):
                                    if str(m["wiu_id"]) == str(wid_k):
                                        matched = True
                                        m["packets"] = max(m["packets"], pkts)
                                        if m.get("avg_rssi") is None:
                                            m["avg_rssi"] = wdata.get("avg_rssi", wdata.get("rssi_avg"))
                                        if m.get("last_rssi") is None:
                                            m["last_rssi"] = wdata.get("last_rssi")
                                        break

                            if not matched:
                                raw_nm = wdata.get("name", "")
                                wiu_markers[f"wiu_{wid_k}"] = {
                                    "id": f"WIU_{wid_k}",
                                    "wiu_id": wid_k,
                                    "elem_type": "wiu",
                                    "name": format_wiu_display_name(raw_nm, wid_k),
                                    "raw_label": raw_nm,
                                    "gps": (lat_v, lon_v),
                                    "packets": pkts,
                                    "last_packet_ts": float(wdata.get("last_time", 0.0)),
                                    "last_raw_hex": wdata.get("raw_hex", ""),
                                    "is_base": False,
                                    "channel": 1,
                                    "avg_rssi": wdata.get("avg_rssi", wdata.get("rssi_avg")),
                                    "last_rssi": wdata.get("last_rssi"),
                                }
                        except (ValueError, TypeError):
                            pass

        # Dynamically track lowest and highest average RSSI received so far
        for winfo in wiu_markers.values():
            r = winfo.get("avg_rssi")
            if r is None:
                r = winfo.get("last_rssi")
            if r is not None and winfo.get("packets", 0) > 0:
                try:
                    rf = float(r)
                    if self.min_avg_rssi_seen is None or rf < self.min_avg_rssi_seen:
                        self.min_avg_rssi_seen = rf
                    if self.max_avg_rssi_seen is None or rf > self.max_avg_rssi_seen:
                        self.max_avg_rssi_seen = rf
                except (ValueError, TypeError):
                    pass

        if isinstance(at_wius, dict):
            for wdata in at_wius.values():
                if isinstance(wdata, dict):
                    r = wdata.get("avg_rssi", wdata.get("rssi_avg", wdata.get("last_rssi")))
                    pkts = int(wdata.get("packets", wdata.get("packets_received", 0)))
                    if r is not None and pkts > 0:
                        try:
                            rf = float(r)
                            if self.min_avg_rssi_seen is None or rf < self.min_avg_rssi_seen:
                                self.min_avg_rssi_seen = rf
                            if self.max_avg_rssi_seen is None or rf > self.max_avg_rssi_seen:
                                self.max_avg_rssi_seen = rf
                        except (ValueError, TypeError):
                            pass

        self.last_all_points = all_layout_gps
        layout_hash = len(all_layout_gps) + len(wiu_markers)
        if self.fitted_layout_hash != layout_hash and all_layout_gps:
            self.auto_fit(all_layout_gps, vp_rect)
            self.fitted_layout_hash = layout_hash

        # Screen transform helper
        c_wx, c_wy = OSMTileMapManager.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
        def latlon_to_screen(lat: float, lon: float) -> Tuple[float, float]:
            wx, wy = OSMTileMapManager.latlon_to_world_px(lat, lon, self.zoom)
            sx = (vp_w / 2.0) + (wx - c_wx)
            sy = vp_y + (vp_h / 2.0) + (wy - c_wy)
            return sx, sy

        # 1. Render Map Tiles clipped to viewport
        screen.set_clip(vp_rect)
        screen.fill((20, 26, 36))

        min_tx = int(math.floor((c_wx - vp_w / 2.0) / 256.0))
        max_tx = int(math.floor((c_wx + vp_w / 2.0) / 256.0))
        min_ty = int(math.floor((c_wy - vp_h / 2.0) / 256.0))
        max_ty = int(math.floor((c_wy + vp_h / 2.0) / 256.0))
        n_tiles = 2 ** self.zoom

        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles
                if ty < 0 or ty >= n_tiles:
                    continue
                dest_x = int((vp_w / 2.0) + (tx * 256.0 - c_wx))
                dest_y = int(vp_y + (vp_h / 2.0) + (ty * 256.0 - c_wy))
                tile_surf = self.map_manager.get_tile_surface(self.zoom, wrapped_tx, ty, layer=self.layer)
                if tile_surf:
                    screen.blit(tile_surf, (dest_x, dest_y))
                else:
                    pygame.draw.rect(screen, (28, 36, 48), (dest_x, dest_y, 256, 256), 1)

        # 2. Draw Light Gray Overlay area encompassing all layout elements with GPS
        if all_layout_gps:
            layout_screen_pts = [latlon_to_screen(lat, lon) for lat, lon in all_layout_gps]
            hull_pts = buffered_hull_2d(layout_screen_pts, buffer_px=38.0)
            draw_transparent_polygon(
                screen,
                color_rgba=(200, 212, 228, 48),
                points=hull_pts,
                border_color=(185, 200, 220, 140),
                border_width=2,
            )

        # 3. Draw Green Transparent Overlay area encompassing all objects with GPS that have ever received packets
        if active_gps_points:
            active_screen_pts = [latlon_to_screen(lat, lon) for lat, lon in active_gps_points]
            active_hull_pts = buffered_hull_2d(active_screen_pts, buffer_px=32.0)
            draw_transparent_polygon(
                screen,
                color_rgba=(45, 215, 95, 68),
                points=active_hull_pts,
                border_color=(35, 235, 95, 215),
                border_width=2,
            )

        # 4. Draw Layout Landmarks / Element Dots
        for lat, lon in all_layout_gps:
            sx, sy = latlon_to_screen(lat, lon)
            if -20 <= sx <= width + 20 and vp_y - 20 <= sy <= height + 20:
                pygame.draw.circle(screen, (26, 32, 42), (int(sx), int(sy)), 5)
                pygame.draw.circle(screen, (170, 190, 215), (int(sx), int(sy)), 3)

        # 5. Draw Plotted WIUs (Beacons, Pins, and Name Badges)
        mx, my = pygame.mouse.get_pos()
        hovered_item = None

        # Sort WIUs so active ones draw on top
        sorted_wius = sorted(wiu_markers.values(), key=lambda w: (w["packets"] > 0, w["packets"]))
        for winfo in sorted_wius:
            lat, lon = winfo["gps"]
            sx, sy = latlon_to_screen(lat, lon)
            is_active = (winfo["packets"] > 0)
            age = max(0.0, now - winfo["last_packet_ts"]) if winfo["last_packet_ts"] > 0 else 9999.0
            is_recent = is_active and (age < 20.0)

            # RSSI color scale: lowest avg RSSI received so far is light blue, highest is magenta
            rssi_val = winfo.get("avg_rssi")
            if rssi_val is None:
                rssi_val = winfo.get("last_rssi")
            rssi_col = rssi_to_coverage_color(rssi_val, self.min_avg_rssi_seen, self.max_avg_rssi_seen, is_active=is_active)

            # Hit test for hover tooltip
            if math.hypot(mx - sx, my - sy) <= 16:
                hovered_item = winfo

            if -60 <= sx <= width + 60 and vp_y - 60 <= sy <= height + 60:
                isx, isy = int(sx), int(sy)

                # Concentric circles effect with scaled-down pulsing frequency & compact size
                freq = packets_to_pulse_frequency(winfo["packets"])
                phase = (now * freq) % (2.0 * math.pi) if freq > 0 else 0.0

                if is_recent:
                    # Active & recent: compact concentric rings with calm, slow pulse
                    r1 = 7.0 + 1.0 * math.sin(phase)
                    r2 = 10.5 + 1.5 * math.sin(phase - 1.1)
                    prog = ((now * freq / (2.0 * math.pi)) + 0.35) % 1.0
                    r3 = 10.0 + prog * 4.5
                    fade = max(0.0, 1.0 - prog)
                    fade_col = (int(rssi_col[0] * fade), int(rssi_col[1] * fade), int(rssi_col[2] * fade))

                    # Concentric Ring 3 (outer delicate ripple)
                    if fade > 0.15:
                        pygame.draw.circle(screen, fade_col, (isx, isy), max(1, int(r3)), 1)
                    # Concentric Ring 2 (outer subtle pulse)
                    pygame.draw.circle(screen, rssi_col, (isx, isy), max(1, int(r2)), 1)
                    # Concentric Ring 1 (inner subtle pulse)
                    pygame.draw.circle(screen, rssi_col, (isx, isy), max(1, int(r1)), 1)
                    pin_col = (255, 255, 255)
                    border_col = rssi_col

                elif is_active:
                    # Active but idle (>20s since last packet): compact, calm concentric rings
                    r1 = 7.0 + 0.6 * math.sin(phase)
                    r2 = 10.5
                    dim_ring_col = (int(rssi_col[0] * 0.75), int(rssi_col[1] * 0.75), int(rssi_col[2] * 0.75))
                    # Concentric Ring 2
                    pygame.draw.circle(screen, dim_ring_col, (isx, isy), int(r2), 1)
                    # Concentric Ring 1
                    pygame.draw.circle(screen, rssi_col, (isx, isy), max(1, int(r1)), 1)
                    pin_col = (235, 245, 255)
                    border_col = rssi_col

                else:
                    # Standby / Inactive: single static compact ring
                    pygame.draw.circle(screen, (120, 135, 150), (isx, isy), 8, 1)
                    pin_col = (210, 220, 230)
                    border_col = (110, 125, 140)

                # Base / WIU Pin Circle (compact, unobtrusive)
                pygame.draw.circle(screen, (22, 28, 40), (isx, isy), 6)
                pygame.draw.circle(screen, rssi_col, (isx, isy), 4)
                pygame.draw.circle(screen, pin_col, (isx, isy), 1)

                # Station Caption: Display ONLY WIU ID
                wid = winfo.get("wiu_id")
                if wid is not None and str(wid).strip() and str(wid).strip() != "None":
                    caption_txt = str(wid).strip()
                elif winfo.get("is_base") and winfo.get("radio_id"):
                    caption_txt = str(winfo["radio_id"])
                else:
                    caption_txt = str(winfo["id"])

                t_surf = font_sm.render(caption_txt, True, (245, 250, 255))
                bw = t_surf.get_width() + 6
                bh = t_surf.get_height() + 2
                bx = isx - bw // 2
                by = isy + 8

                b_surf = pygame.Surface((bw, bh), pygame.SRCALPHA)
                b_surf.fill((16, 22, 32, 215))
                pygame.draw.rect(b_surf, border_col, (0, 0, bw, bh), 1, border_radius=3)
                b_surf.blit(t_surf, (3, 1))
                screen.blit(b_surf, (bx, by))

        self.hovered_wiu = hovered_item

        # 6. Map UI Chrome & Control Buttons
        self.buttons.clear()

        # Top-left Info Banner
        title_surf = font_md.render("WIU RF COVERAGE MAP", True, (0, 235, 255))
        stats_txt = f"Layout GPS: {len(all_layout_gps)}  |  Active RF Footprint: {len(active_gps_points)}  |  WIUs: {len(wiu_markers)}  |  Zoom: {self.zoom}x"
        stats_surf = font_sm.render(stats_txt, True, (215, 228, 245))
        info_w = max(title_surf.get_width(), stats_surf.get_width()) + 20
        info_h = 44
        info_surf = pygame.Surface((info_w, info_h), pygame.SRCALPHA)
        info_surf.fill((18, 24, 34, 230))
        pygame.draw.rect(info_surf, (45, 140, 235), (0, 0, info_w, info_h), 1, border_radius=6)
        info_surf.blit(title_surf, (10, 3))
        info_surf.blit(stats_surf, (10, 23))
        screen.blit(info_surf, (16, vp_y + 12))

        # Top-right Action Buttons
        meas_label = "Measure (D) [ON]" if self.measure_mode else "Measure (D)"
        ctrl_specs = [
            ("zoom_out", " - ", 36),
            ("zoom_in", " + ", 36),
            ("fit_all", "Fit All (F)", 84),
            ("toggle_layer", "Satellite (S)" if self.layer == "osm" else "Streets (S)", 98),
            ("toggle_measure", meas_label, 115 if self.measure_mode else 95),
        ]
        if self.measure_points:
            ctrl_specs.append(("clear_measure", "Clear (C)", 75))
        ctrl_specs.append(("close", "✕ Close (Esc)", 100))
        cur_rx = width - 16
        for b_name, b_text, bw in reversed(ctrl_specs):
            cur_rx -= bw
            rect = pygame.Rect(cur_rx, vp_y + 12, bw, 32)
            self.buttons[b_name] = rect
            is_hover = rect.collidepoint(mx, my)
            if b_name == "close":
                btn_bg = (180, 40, 40) if is_hover else (125, 30, 30)
            elif is_hover:
                btn_bg = (55, 90, 140)
            else:
                btn_bg = (28, 38, 54)
            pygame.draw.rect(screen, btn_bg, rect, border_radius=5)
            pygame.draw.rect(screen, (85, 125, 175), rect, 1, border_radius=5)
            b_lbl = font_sm_bold.render(b_text, True, (255, 255, 255))
            screen.blit(b_lbl, (rect.x + (bw - b_lbl.get_width()) // 2, rect.y + 7))
            cur_rx -= 8

        # Bottom-left Legend
        leg_w, leg_h = 360, 116
        leg_x, leg_y = 16, height - leg_h - 14
        leg_surf = pygame.Surface((leg_w, leg_h), pygame.SRCALPHA)
        leg_surf.fill((18, 24, 34, 235))
        pygame.draw.rect(leg_surf, (60, 80, 110), (0, 0, leg_w, leg_h), 1, border_radius=6)

        # Legend item 1: Light Gray
        pygame.draw.rect(leg_surf, (200, 212, 228), (10, 8, 16, 10), border_radius=2)
        pygame.draw.rect(leg_surf, (185, 200, 220), (10, 8, 16, 10), 1, border_radius=2)
        leg_surf.blit(font_sm.render("Layout Footprint (All GPS Elements)", True, (220, 230, 242)), (32, 6))

        # Legend item 2: Green
        pygame.draw.rect(leg_surf, (45, 215, 95), (10, 24, 16, 10), border_radius=2)
        pygame.draw.rect(leg_surf, (35, 235, 95), (10, 24, 16, 10), 1, border_radius=2)
        leg_surf.blit(font_sm.render("Active RF Coverage (Packets Received)", True, (135, 255, 170)), (32, 22))

        # Legend item 3: Concentric Circles / Station
        pygame.draw.circle(leg_surf, (100, 205, 255), (18, 44), 6, 1)
        pygame.draw.circle(leg_surf, (255, 20, 180), (18, 44), 4)
        pygame.draw.circle(leg_surf, (255, 255, 255), (18, 44), 1)
        leg_surf.blit(font_sm.render("WIU Station Pins (Captions: WIU IDs)", True, (215, 235, 255)), (32, 38))

        # Legend item 4: RSSI Color Scale gradient bar
        leg_surf.blit(font_sm_bold.render("Average RSSI Signal Strength Scale:", True, (235, 245, 255)), (10, 58))
        # Draw gradient bar (width 220 px, height 8 px)
        grad_x, grad_y, grad_w, grad_h = 10, 76, 220, 8
        for gx in range(grad_w):
            gt = gx / float(grad_w - 1)
            gr = int(100 + gt * (255 - 100))
            gg = int(205 + gt * (20 - 205))
            gb = int(255 + gt * (180 - 255))
            pygame.draw.line(leg_surf, (gr, gg, gb), (grad_x + gx, grad_y), (grad_x + gx, grad_y + grad_h))
        pygame.draw.rect(leg_surf, (100, 130, 160), (grad_x, grad_y, grad_w, grad_h), 1)

        # Labels for gradient bar showing lowest to highest received so far
        low_txt = f"Lowest: {self.min_avg_rssi_seen:.1f} dBm" if self.min_avg_rssi_seen is not None else "Lowest (Light Blue)"
        high_txt = f"Highest: {self.max_avg_rssi_seen:.1f} dBm" if self.max_avg_rssi_seen is not None else "Highest (Magenta)"
        lbl_low = font_sm.render(low_txt, True, (100, 205, 255))
        lbl_hi = font_sm.render(high_txt, True, (255, 50, 190))
        leg_surf.blit(lbl_low, (10, 88))
        leg_surf.blit(lbl_hi, (grad_x + grad_w - lbl_hi.get_width(), 88))
        screen.blit(leg_surf, (leg_x, leg_y))

        # 7. Hover Tooltip Details Card
        if self.hovered_wiu:
            w = self.hovered_wiu
            age_s = f"{max(0.0, now - w['last_packet_ts']):.1f}s ago" if w['last_packet_ts'] > 0 else "Never"
            rows = [
                f"[STATION] {w['name']}",
                f"WIU ID: {w['wiu_id'] or 'None'}  |  Type: {w['elem_type'].upper()}",
                f"Element ID: {w['id']}  |  Channel: {w['channel']}",
                f"GPS: {w['gps'][0]:.6f}, {w['gps'][1]:.6f}",
                f"Packets Rx: {w['packets']}  |  Last Seen: {age_s}",
            ]
            avg_r = w.get("avg_rssi")
            last_r = w.get("last_rssi")
            if avg_r is not None:
                try:
                    rssi_line = f"Average RSSI: {float(avg_r):.1f} dBm"
                except (ValueError, TypeError):
                    rssi_line = f"Average RSSI: {avg_r} dBm"
                if last_r is not None:
                    try:
                        rssi_line += f"  (Last: {float(last_r):.1f} dBm)"
                    except (ValueError, TypeError):
                        pass
                rows.append(rssi_line)
            elif last_r is not None:
                try:
                    rows.append(f"RSSI: {float(last_r):.1f} dBm")
                except (ValueError, TypeError):
                    rows.append(f"RSSI: {last_r} dBm")
            else:
                rows.append("RSSI: No RF data")

            if w.get("last_raw_hex"):
                rows.append(f"Last Hex: {w['last_raw_hex'][:32]}...")

            tw = max(font_sm.size(r)[0] for r in rows) + 24
            th = len(rows) * 18 + 12
            tx = min(width - tw - 16, mx + 16)
            ty = max(vp_y + 12, min(height - th - 16, my - th // 2))

            hov_rssi_col = rssi_to_coverage_color(w.get("avg_rssi", w.get("last_rssi")), self.min_avg_rssi_seen, self.max_avg_rssi_seen, is_active=(w["packets"] > 0))
            t_box = pygame.Surface((tw, th), pygame.SRCALPHA)
            t_box.fill((16, 22, 32, 245))
            pygame.draw.rect(t_box, hov_rssi_col, (0, 0, tw, th), 1, border_radius=6)
            for i, r in enumerate(rows):
                if i == 0:
                    col = (0, 235, 255)
                elif "Average RSSI:" in r or "RSSI:" in r:
                    col = hov_rssi_col
                elif "Packets Rx" in r:
                    col = (120, 255, 160)
                else:
                    col = (225, 235, 248)
                t_box.blit(font_sm_bold.render(r, True, col) if (i == 0 or "Average RSSI:" in r) else font_sm.render(r, True, col), (12, 6 + i * 18))
            screen.blit(t_box, (tx, ty))

        # Compute snapping for distance measurement tool
        cur_wx = c_wx + (mx - vp_w / 2.0)
        cur_wy = c_wy + (my - (vp_y + vp_h / 2.0))
        cur_lat, cur_lon = OSMTileMapManager.world_px_to_latlon(cur_wx, cur_wy, self.zoom)

        snap_pt, snap_name = None, None
        for winfo in wiu_markers.values():
            wlat, wlon = winfo["gps"]
            wsx, wsy = latlon_to_screen(wlat, wlon)
            if math.hypot(mx - wsx, my - wsy) <= 20:
                snap_pt = (wlat, wlon)
                snap_name = winfo["name"]
                break
        if not snap_pt:
            for elat, elon in all_layout_gps:
                esx, esy = latlon_to_screen(elat, elon)
                if math.hypot(mx - esx, my - esy) <= 16:
                    snap_pt = (elat, elon)
                    snap_name = "Track Landmark"
                    break

        if snap_pt:
            self.measure_hover = {"gps": snap_pt, "name": snap_name, "is_snapped": True}
        else:
            self.measure_hover = {"gps": (cur_lat, cur_lon), "name": f"{cur_lat:.5f}, {cur_lon:.5f}", "is_snapped": False}

        # 8. Draw Distance Measurement Polylines, Pins, and Live Readout
        total_measured_miles = 0.0
        if len(self.measure_points) >= 1:
            m_screen_pts = [latlon_to_screen(p["gps"][0], p["gps"][1]) for p in self.measure_points]
            for i in range(len(m_screen_pts) - 1):
                p1_s = m_screen_pts[i]
                p2_s = m_screen_pts[i + 1]
                seg_mi = haversine_distance_miles(self.measure_points[i]["gps"], self.measure_points[i + 1]["gps"])
                total_measured_miles += seg_mi
                seg_ft = seg_mi * 5280.0

                pygame.draw.line(screen, (10, 15, 25), p1_s, p2_s, 6)
                pygame.draw.line(screen, (255, 215, 30), p1_s, p2_s, 3)

                seg_lbl = f"{seg_mi:.2f} mi ({int(seg_ft):,} ft)" if seg_mi >= 0.2 else f"{int(seg_ft):,} ft ({seg_mi:.2f} mi)"
                lbl_surf = font_sm_bold.render(seg_lbl, True, (255, 245, 175))
                mid_x = (p1_s[0] + p2_s[0]) / 2.0
                mid_y = (p1_s[1] + p2_s[1]) / 2.0
                tag_w, tag_h = lbl_surf.get_width() + 10, lbl_surf.get_height() + 4
                tag_surf = pygame.Surface((tag_w, tag_h), pygame.SRCALPHA)
                tag_surf.fill((16, 22, 32, 230))
                pygame.draw.rect(tag_surf, (255, 210, 40), (0, 0, tag_w, tag_h), 1, border_radius=4)
                tag_surf.blit(lbl_surf, (5, 2))
                screen.blit(tag_surf, (int(mid_x - tag_w / 2), int(mid_y - tag_h / 2)))

            for i, p in enumerate(self.measure_points):
                sx, sy = m_screen_pts[i]
                isx, isy = int(sx), int(sy)
                pygame.draw.circle(screen, (20, 26, 36), (isx, isy), 11)
                pygame.draw.circle(screen, (255, 215, 30), (isx, isy), 9)
                pygame.draw.circle(screen, (255, 255, 255), (isx, isy), 4)
                num_s = font_sm_bold.render(str(i + 1), True, (255, 245, 180))
                screen.blit(num_s, (isx + 12, isy - 9))

            if self.measure_mode and self.measure_hover and vp_rect.collidepoint(mx, my):
                last_pt = self.measure_points[-1]["gps"]
                hov_pt = self.measure_hover["gps"]
                p_last_s = m_screen_pts[-1]
                p_hov_s = latlon_to_screen(hov_pt[0], hov_pt[1])

                hov_mi = haversine_distance_miles(last_pt, hov_pt)
                hov_ft = hov_mi * 5280.0
                cumul_mi = total_measured_miles + hov_mi

                pygame.draw.line(screen, (255, 175, 40), p_last_s, p_hov_s, 2)
                pygame.draw.circle(screen, (255, 220, 60), (int(p_hov_s[0]), int(p_hov_s[1])), 7, 2)

                deg = calculate_bearing_degrees(last_pt, hov_pt)
                comp = bearing_to_compass(deg)
                preview_txt = f"{hov_mi:.2f} mi ({int(hov_ft):,} ft) | Total: {cumul_mi:.2f} mi | {comp} ({deg:.0f}°)"
                ht_s = font_sm_bold.render(preview_txt, True, (255, 250, 220))
                hw, hh = ht_s.get_width() + 14, ht_s.get_height() + 6
                h_box = pygame.Surface((hw, hh), pygame.SRCALPHA)
                h_box.fill((20, 26, 38, 235))
                pygame.draw.rect(h_box, (255, 205, 50), (0, 0, hw, hh), 1, border_radius=4)
                h_box.blit(ht_s, (7, 3))
                hbx = min(width - hw - 10, int(p_hov_s[0] + 14))
                hby = max(vp_y + 10, min(height - hh - 10, int(p_hov_s[1] - hh // 2)))
                screen.blit(h_box, (hbx, hby))

            # Floating Measurement Summary Card
            tot_ft = total_measured_miles * 5280.0
            tot_km = total_measured_miles * 1.609344
            p_start_name = self.measure_points[0].get("name", "Point 1")
            p_end_name = self.measure_points[-1].get("name", f"Point {len(self.measure_points)}")

            card_lines = [
                f"MEASURED DISTANCE: {total_measured_miles:.2f} miles",
                f"  Feet: {int(tot_ft):,} ft  |  Metric: {tot_km:.2f} km",
                f"  From: {p_start_name}",
                f"  To:   {p_end_name}",
            ]
            if len(self.measure_points) >= 2:
                init_deg = calculate_bearing_degrees(self.measure_points[0]["gps"], self.measure_points[-1]["gps"])
                card_lines.append(f"  Heading: {bearing_to_compass(init_deg)} ({init_deg:.0f}°)  |  Segments: {len(self.measure_points) - 1}")
            card_lines.append("  [Click map/WIU to add points | 'C' Clear | 'D' Toggle]")

            card_w = max(font_sm.size(cl)[0] for cl in card_lines) + 24
            card_h = len(card_lines) * 17 + 10
            card_x = 16
            card_y = vp_y + 64

            card_box = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
            card_box.fill((18, 24, 36, 235))
            pygame.draw.rect(card_box, (255, 215, 40), (0, 0, card_w, card_h), 1, border_radius=6)
            for li, ltext in enumerate(card_lines):
                lcol = (255, 215, 40) if li == 0 else ((255, 180, 50) if li == len(card_lines) - 1 else (230, 240, 255))
                font_to_use = font_sm_bold if (li == 0 or li == len(card_lines) - 1) else font_sm
                card_box.blit(font_to_use.render(ltext, True, lcol), (12, 5 + li * 17))
            screen.blit(card_box, (card_x, card_y))

        elif self.measure_mode and self.measure_hover and vp_rect.collidepoint(mx, my):
            hov_pt = self.measure_hover["gps"]
            p_hov_s = latlon_to_screen(hov_pt[0], hov_pt[1])
            pygame.draw.circle(screen, (255, 215, 30), (int(p_hov_s[0]), int(p_hov_s[1])), 8, 2)
            first_txt = f"Click to set measure start point ({self.measure_hover['name']})"
            ft_s = font_sm_bold.render(first_txt, True, (255, 245, 180))
            fw, fh = ft_s.get_width() + 12, ft_s.get_height() + 6
            f_box = pygame.Surface((fw, fh), pygame.SRCALPHA)
            f_box.fill((18, 24, 34, 230))
            pygame.draw.rect(f_box, (255, 215, 30), (0, 0, fw, fh), 1, border_radius=4)
            f_box.blit(ft_s, (6, 3))
            screen.blit(f_box, (min(width - fw - 10, int(p_hov_s[0] + 12)), max(vp_y + 10, int(p_hov_s[1] - fh // 2))))

        # 9. Visual Map Scale Bar (Bottom-Right)
        m_per_px = 156543.03392 * math.cos(math.radians(self.center_lat)) / (2 ** self.zoom)
        bar_px, bar_label = self._get_scale_bar_info(m_per_px)
        scale_w = bar_px + 32
        scale_h = 32
        scale_x = width - scale_w - 16
        scale_y = height - scale_h - 14

        scale_surf = pygame.Surface((scale_w, scale_h), pygame.SRCALPHA)
        scale_surf.fill((18, 24, 34, 225))
        pygame.draw.rect(scale_surf, (60, 80, 110), (0, 0, scale_w, scale_h), 1, border_radius=5)

        lx0 = 16
        lx1 = 16 + bar_px
        ly = 20
        pygame.draw.line(scale_surf, (220, 235, 255), (lx0, ly), (lx1, ly), 2)
        pygame.draw.line(scale_surf, (220, 235, 255), (lx0, ly - 5), (lx0, ly + 5), 2)
        pygame.draw.line(scale_surf, (220, 235, 255), (lx1, ly - 5), (lx1, ly + 5), 2)

        lbl_s = font_sm_bold.render(bar_label, True, (220, 235, 255))
        scale_surf.blit(lbl_s, (lx0 + (bar_px - lbl_s.get_width()) // 2, 4))
        screen.blit(scale_surf, (scale_x, scale_y))

        screen.set_clip(None)



# =====================================================================
# NON-BLOCKING MULTICAST RECEIVER
# =====================================================================

class ChunkAssembler:
    """Reassembles binary compressed datagram chunks into complete frames."""
    def __init__(self, ttl_sec: float = 5.0, dedupe_len: int = 256):
        self.ttl_sec = ttl_sec
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._recent_completed = deque(maxlen=dedupe_len)
        self._recent_completed_set: set = set()

    def process_datagram(self, datagram: bytes) -> Optional[Tuple[bytes, bytes]]:
        now = time.time()
        # Clean up stale incomplete frames older than ttl_sec
        stale = [tid for tid, entry in self._pending.items() if now - entry["created_at"] > self.ttl_sec]
        for tid in stale:
            del self._pending[tid]

        if len(datagram) < CHUNK_HEADER_SIZE:
            return None

        magic = datagram[:4]
        if magic not in (CHUNK_MAGIC_ZLIB, CHUNK_MAGIC_LZ4):
            return None

        magic, transfer_id, total_size, chunk_idx, total_chunks = struct.unpack(
            CHUNK_HEADER_FMT, datagram[:CHUNK_HEADER_SIZE]
        )
        payload_chunk = datagram[CHUNK_HEADER_SIZE:]

        # Deduplicate already completed transfers
        if transfer_id in self._recent_completed_set:
            return None

        if total_chunks <= 1:
            self._mark_completed(transfer_id)
            return magic, payload_chunk

        if transfer_id not in self._pending:
            self._pending[transfer_id] = {
                "magic": magic,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "chunks": {},
                "created_at": now,
            }

        entry = self._pending[transfer_id]
        entry["chunks"][chunk_idx] = payload_chunk

        if len(entry["chunks"]) == entry["total_chunks"]:
            full_payload = b"".join(entry["chunks"][i] for i in range(entry["total_chunks"]))
            del self._pending[transfer_id]
            self._mark_completed(transfer_id)
            return entry["magic"], full_payload

        return None

    def _mark_completed(self, transfer_id: int):
        if len(self._recent_completed) == self._recent_completed.maxlen:
            old = self._recent_completed.popleft()
            self._recent_completed_set.discard(old)
        self._recent_completed.append(transfer_id)
        self._recent_completed_set.add(transfer_id)


class MulticastReceiver:
    def __init__(self, mcast_group: str, mcast_port: int, bind_ip: Optional[str] = None):
        self.mcast_group = mcast_group
        self.mcast_port = mcast_port
        self.bind_ip = bind_ip
        self.assembler = ChunkAssembler()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        except (AttributeError, OSError):
            pass
        self.sock.bind(("", self.mcast_port))

        # Join INADDR_ANY (0.0.0.0) safely with standard 8-byte struct ip_mreq (two 4-byte IPv4 in_addr)
        try:
            mreq_any = socket.inet_aton(self.mcast_group) + socket.inet_aton("0.0.0.0")
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_any)
        except OSError:
            pass

        # Join loopback
        try:
            mreq_loop = socket.inet_aton(self.mcast_group) + socket.inet_aton("127.0.0.1")
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_loop)
        except OSError:
            pass

        # Join discovered active interfaces
        for ip in self._discover_interfaces():
            self.join_interface(ip)

        if self.bind_ip:
            self.join_interface(self.bind_ip)

        self.sock.setblocking(False)

    def _discover_interfaces(self) -> List[str]:
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
        for probe_dest in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect(probe_dest)
                    pip = probe.getsockname()[0]
                    if pip and not pip.startswith("127.") and pip not in ips:
                        ips.append(pip)
            except Exception:
                pass
        return ips

    def join_interface(self, ip_str: str):
        if not ip_str:
            return
        try:
            mreq = socket.inet_aton(self.mcast_group) + socket.inet_aton(ip_str)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass

    def poll_packets(self) -> List[dict]:
        states: List[dict] = []
        while True:
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                break
            try:
                data, _ = self.sock.recvfrom(65535)
            except OSError:
                break

            result = self.assembler.process_datagram(data)
            if result is not None:
                magic, full_payload = result
                try:
                    if magic == CHUNK_MAGIC_LZ4 and lz4_frame is not None:
                        decomp = lz4_frame.decompress(full_payload)
                    else:
                        decomp = zlib.decompress(full_payload)
                    state = json.loads(decomp.decode("utf-8"))
                    states.append(state)
                except Exception:
                    pass
            else:
                # Fallback for uncompressed direct JSON datagrams
                try:
                    state = json.loads(data.decode("utf-8"))
                    states.append(state)
                except Exception:
                    pass
        return states

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# =====================================================================
# SECURE CHAT FORMATTING, SAFE REGEX & PERSISTENT IDENTITY STORE
# =====================================================================

CHAT_COLOR_MAP: Dict[str, Tuple[int, int, int]] = {
    "red": (255, 95, 95),
    "green": (90, 245, 135),
    "blue": (100, 185, 255),
    "yellow": (255, 230, 95),
    "cyan": (80, 240, 255),
    "magenta": (255, 120, 240),
    "orange": (255, 165, 70),
    "white": (245, 248, 255),
    "pink": (255, 155, 205),
    "gray": (165, 175, 190),
    "grey": (165, 175, 190),
}

CHAT_COLOR_CYCLE: List[str] = ["cyan", "yellow", "green", "orange", "red", "magenta", "blue", "white"]
MAX_CHAT_REGEX_LEN = 128
CHAT_PERMANENT_NOTICE = "Foamer Chat: Messages are not private, are logged by the Banc and moderated by Banc's owner"


def compile_safe_chat_regex(pattern: str):
    """Compiles a user regex safely with length cap to prevent ReDoS or crashes."""
    if not pattern or not isinstance(pattern, str):
        return None
    pat = pattern.strip()
    if not pat or len(pat) > MAX_CHAT_REGEX_LEN:
        return None
    try:
        return re.compile(pat, re.IGNORECASE)
    except re.error:
        return None


def strip_chat_formatting(text: str) -> str:
    """Strips basic bold, italic, underline, and color markup tags from chat text."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\[/?(?:b|i|u|color(?:=[a-zA-Z]+)?|red|green|blue|yellow|cyan|magenta|orange|white|pink|gray|grey)\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^\*]+?)\*(?!\*)", r"\1", s)
    return s


def parse_chat_formatted_spans(
    text: str,
    default_color: Tuple[int, int, int] = (225, 235, 248),
) -> List[Dict[str, Any]]:
    """
    Parses basic chat formatting into styled spans:
      - Bold: **text** or [b]text[/b]
      - Italic: *text* or [i]text[/i]
      - Underscore/Underline: __text__ or [u]text[/u]
      - Simple colors: [red]text[/red], [color=green]text[/color], or [cyan]text
    Returns list of dicts: {"text": str, "bold": bool, "italic": bool, "underline": bool, "color": (R, G, B)}
    """
    if not text:
        return []
    src = str(text)
    token_re = re.compile(
        r"(\[/?(?:b|i|u|color(?:=[a-zA-Z]+)?|red|green|blue|yellow|cyan|magenta|orange|white|pink|gray|grey)\]"
        r"|\*\*[^\*]+?\*\*"
        r"|__[^_]+?__"
        r"|(?<!\*)\*[^\*]+?\*(?!\*))",
        re.IGNORECASE,
    )
    spans: List[Dict[str, Any]] = []
    bold = False
    italic = False
    underline = False
    color_stack: List[Tuple[int, int, int]] = [default_color]

    def _emit(chunk: str, b: bool, it: bool, un: bool, col: Tuple[int, int, int]):
        if not chunk:
            return
        if spans and spans[-1]["bold"] == b and spans[-1]["italic"] == it and spans[-1]["underline"] == un and spans[-1]["color"] == col:
            spans[-1]["text"] += chunk
        else:
            spans.append({
                "text": chunk,
                "bold": b,
                "italic": it,
                "underline": un,
                "color": col,
            })

    pos = 0
    for m in token_re.finditer(src):
        if m.start() > pos:
            _emit(src[pos:m.start()], bold, italic, underline, color_stack[-1])
        tok = m.group(0)
        tl = tok.lower()
        if tl == "[b]":
            bold = True
        elif tl == "[/b]":
            bold = False
        elif tl == "[i]":
            italic = True
        elif tl == "[/i]":
            italic = False
        elif tl == "[u]":
            underline = True
        elif tl == "[/u]":
            underline = False
        elif tl.startswith("[color=") and tl.endswith("]"):
            cname = tl[7:-1].strip()
            if cname in CHAT_COLOR_MAP:
                color_stack.append(CHAT_COLOR_MAP[cname])
        elif tl in ("[/color]",) or (tl.startswith("[/") and tl[2:-1] in CHAT_COLOR_MAP):
            if len(color_stack) > 1:
                color_stack.pop()
            else:
                color_stack[-1] = default_color
        elif tl.startswith("[") and tl.endswith("]") and tl[1:-1] in CHAT_COLOR_MAP:
            color_stack.append(CHAT_COLOR_MAP[tl[1:-1]])
        elif tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
            _emit(tok[2:-2], True, italic, underline, color_stack[-1])
        elif tok.startswith("__") and tok.endswith("__") and len(tok) > 4:
            _emit(tok[2:-2], bold, italic, True, color_stack[-1])
        elif tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
            _emit(tok[1:-1], bold, True, underline, color_stack[-1])
        else:
            _emit(tok, bold, italic, underline, color_stack[-1])
        pos = m.end()

    if pos < len(src):
        _emit(src[pos:], bold, italic, underline, color_stack[-1])
    return spans


class ChatIdentityStore:
    """
    Persistent local store for associating:
      1. The user's own identity with their friendly name (self_identity <-> self_name)
      2. Incoming identities with user-assigned friendly names (contacts: identity -> name)
    Survives restarts of both the Engine and Viewer. Never leaks into layout files or network packets.
    """
    def __init__(self, filepath: str = ".apexbos_chat_identities.json"):
        self.filepath = filepath
        self.self_identity: str = ""
        self.self_name: str = ""
        self.contacts: Dict[str, str] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw_id = data.get("self_identity") or ""
                self.self_identity = mosfet_client.sanitize_chat_identity(raw_id) if raw_id else ""
                self.self_name = mosfet_client.sanitize_chat_text(data.get("self_name") or "", max_len=64)
                raw_contacts = data.get("contacts")
                if isinstance(raw_contacts, dict):
                    clean_c: Dict[str, str] = {}
                    for k, v in raw_contacts.items():
                        ck = mosfet_client.sanitize_chat_identity(k)
                        cv = mosfet_client.sanitize_chat_text(v, max_len=64)
                        if ck and cv:
                            clean_c[ck] = cv
                    self.contacts = clean_c
        except Exception:
            pass

    def save(self):
        try:
            payload = {
                "self_identity": self.self_identity,
                "self_name": self.self_name,
                "contacts": dict(self.contacts),
            }
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception:
            pass

    def set_self_identity_name(self, identity: str, name: str, persist: bool = True):
        self.self_identity = mosfet_client.sanitize_chat_identity(identity) if identity and str(identity).strip() else ""
        self.self_name = mosfet_client.sanitize_chat_text(name, max_len=64) if name and str(name).strip() else ""
        if self.self_identity and self.self_name:
            self.contacts[self.self_identity] = self.self_name
        if persist:
            self.save()

    def assign_contact_name(self, identity: str, name: str, persist: bool = True):
        cid = mosfet_client.sanitize_chat_identity(identity)
        if not cid:
            return
        cname = mosfet_client.sanitize_chat_text(name, max_len=64) if name else ""
        if cname:
            self.contacts[cid] = cname
        else:
            self.contacts.pop(cid, None)
        if persist:
            self.save()

    def resolve_display_name(self, identity: str) -> str:
        """Returns the local friendly name for an identity if assigned, otherwise the identity itself."""
        if not identity:
            return "Unknown"
        cid = str(identity).strip()
        for k, v in self.contacts.items():
            if k.lower() == cid.lower() and v:
                return v
        if self.self_identity and cid.lower() == self.self_identity.lower() and self.self_name:
            return self.self_name
        return cid

    def replace_identities_with_names_in_text(self, text: str) -> str:
        """Replaces @<identity> and @identity in incoming message text with @<assigned_name> for Viewer display."""
        if not text:
            return ""
        out = str(text)

        def _repl_angle(m):
            token = m.group(1).strip()
            disp = self.resolve_display_name(token)
            return f"@{disp}"

        out = re.sub(r"@<([^>]{1,64})>", _repl_angle, out)

        def _repl_word(m):
            prefix = m.group(1)
            token = m.group(2).strip()
            disp = self.resolve_display_name(token)
            return f"{prefix}@{disp}"

        out = re.sub(r"(^|\s)@([A-Za-z0-9_\.\-]{1,64})", _repl_word, out)
        return out

    def convert_names_to_identities_in_text(self, text: str) -> Tuple[str, List[str]]:
        """Converts any @<name> or @name in outgoing text into @identity so names are never sent in the open."""
        clean = mosfet_client.sanitize_chat_text(text)
        if not clean:
            return "", []
        name_to_id: Dict[str, str] = {}
        for ident, nm in self.contacts.items():
            if ident and nm:
                name_to_id[nm.lower()] = ident
                name_to_id[ident.lower()] = ident
        if self.self_name and self.self_identity:
            name_to_id[self.self_name.lower()] = self.self_identity
            name_to_id[self.self_identity.lower()] = self.self_identity

        targets: List[str] = []

        def _add_t(ident_val: str):
            iv = mosfet_client.sanitize_chat_identity(ident_val)
            if iv and iv not in targets:
                targets.append(iv)

        def _repl_angle(m):
            tok = m.group(1).strip()
            resolved = name_to_id.get(tok.lower(), mosfet_client.sanitize_chat_identity(tok))
            if resolved:
                _add_t(resolved)
                return f"@{resolved}"
            return m.group(0)

        converted = re.sub(r"@<([^>]{1,64})>", _repl_angle, clean)

        for nm_lower in sorted(name_to_id.keys(), key=len, reverse=True):
            if " " in nm_lower:
                ident_val = name_to_id[nm_lower]
                pat = re.compile(r"(^|\s)@" + re.escape(nm_lower) + r"(?=\s|$|[.,!?;:])", re.IGNORECASE)
                def _make_sub(iv):
                    def _sub(m):
                        _add_t(iv)
                        return f"{m.group(1)}@{iv}"
                    return _sub
                converted = pat.sub(_make_sub(ident_val), converted)

        def _repl_word(m):
            prefix = m.group(1)
            tok = m.group(2).strip()
            resolved = name_to_id.get(tok.lower(), mosfet_client.sanitize_chat_identity(tok))
            if resolved:
                _add_t(resolved)
                return f"{prefix}@{resolved}"
            return m.group(0)

        converted = re.sub(r"(^|\s)@([A-Za-z0-9_\.\-]{1,64})", _repl_word, converted)
        for m_id in mosfet_client.extract_chat_mentions(converted):
            _add_t(m_id)
        return converted, targets


# =====================================================================
# PYGAME DISPATCHER RENDER ENGINE
# =====================================================================

class DispatcherPygameRenderer:
    def __init__(
        self,
        mcast_group: str = DEFAULT_MCAST_GROUP,
        mcast_port: int = DEFAULT_MCAST_PORT,
        engine_host: str = DEFAULT_ENGINE_HOST,
        engine_port: int = DEFAULT_ENGINE_PORT,
        stale_timeout_sec: float = 15.0,
        bind_ip: Optional[str] = None,
        banc_config: Optional[str] = None,
    ):
        pygame.init()
        pygame.display.set_caption(f"Railroad Dispatcher Display Engine {VERSION} (Copyright by AB - MIT License)")
        self.width = 1520
        self.height = 940
        self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()

        self.font_sm_thin = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 12, bold=False)
        self.font_sm = self.font_sm_thin
        self.font_sm_bold = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 12, bold=True)
        self.font_md = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 13, bold=True)
        self.font_lg = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 17, bold=True)
        self.font_xl = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 24, bold=True)
        self.font_splash_thanks = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 15, bold=False)
        self._elem_label_font_cache: Dict[Tuple[int, bool], pygame.font.Font] = {}

        self.zoom: float = 1.0
        self.min_zoom: float = 0.35 / 20.0  # 20x more zoom out capability (0.0175)
        self.max_zoom: float = 4.0
        self.cam_x: float = 55.0
        self.cam_y: float = 75.0
        self.has_centered_on_layout: bool = False
        self.last_centered_layout_fname: Optional[str] = None
        self.is_panning: bool = False
        self.pan_start_mouse: Tuple[int, int] = (0, 0)
        self.pan_start_cam: Tuple[float, float] = (0.0, 0.0)

        # Global Display Toggles
        self.show_labels: bool = True
        self.show_inferred_trains: bool = True

        # Train Assignment Dialog state
        self.train_assign_active: bool = False
        self.train_assign_trn_key: Optional[str] = None
        self.train_assign_active_field: str = "assigned_id"
        self.train_assign_buffers: Dict[str, str] = {"assigned_id": "", "assigned_name": ""}
        self._train_scroller_row_rects: List[Tuple[pygame.Rect, dict]] = []
        self._train_scroller_del_rects: List[Tuple[pygame.Rect, str]] = []
        self.hovered_train: Optional[dict] = None
        self.hovered_train_rect: Optional[pygame.Rect] = None
        self.hovered_wiu_row: Optional[dict] = None
        self.hovered_wiu_row_rect: Optional[pygame.Rect] = None
        self.wiu_scroll_offset: int = 0
        self.addr_table_panel_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.addr_table_btn_rects: Dict[str, pygame.Rect] = {}
        self.addr_table_avail_rows: int = 10
        self.addr_table_total_items: int = 0
        self.last_scrolled_panel: str = "packet_scroller" 
        self.wiu_sort_by: str = "wiu"  # "wiu", "rssi", "pkts" (default to WIU ids)
        self.wiu_sort_desc: bool = False

        # Chat Element & Persistent Identity/Name State
        self.chat_permanent_notice: str = CHAT_PERMANENT_NOTICE
        self._chat_font_cache: Dict[Tuple[int, bool, bool, bool], pygame.font.Font] = {}
        self.chat_identity_store = ChatIdentityStore()
        self.chat_history: deque = deque(maxlen=500)
        self.chat_seen_keys: set = set()
        self.chat_cleared_before_ts: float = 0.0
        self.chat_cleared_before_id: int = 0
        self.chat_scroll_offset: int = 0
        self.chat_avail_rows: int = 8
        self.chat_mute_all: bool = False
        self.chat_include_regex: str = ""
        self.chat_exclude_regex: str = ""
        self.chat_input_buffer: str = ""
        self.chat_active_field: Optional[str] = None  # "message", "include_regex", "exclude_regex", or None
        self.chat_color_idx: int = 0
        self.chat_panel_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.chat_btn_rects: Dict[str, pygame.Rect] = {}
        self.chat_sender_rects: List[Tuple[pygame.Rect, str]] = []
        self.chat_id_modal_active: bool = False
        self.chat_id_modal_active_field: str = "self_identity"
        self.chat_id_modal_buffers: Dict[str, str] = {
            "self_identity": self.chat_identity_store.self_identity,
            "self_name": self.chat_identity_store.self_name,
            "contact_identity": "",
            "contact_name": "",
        }
        self._chat_id_modal_contact_rects: List[Tuple[pygame.Rect, str, str]] = []

        self.engine_host = engine_host
        self.engine_port = engine_port
        self.stale_timeout_sec = stale_timeout_sec
        self.bind_ip = bind_ip

        self.prefs = UserPreferences("default_layout.json")
        self._apply_saved_view()
        self.receiver = MulticastReceiver(mcast_group, mcast_port, bind_ip=bind_ip)
        self.last_packet_time: float = time.time()
        self._last_heartbeat_time: float = 0.0
        self._pending_heartbeat_states: deque = deque(maxlen=20)
        self._heartbeat_running: bool = False
        self._heartbeat_thread: Optional[threading.Thread] = None

        self._pending_stream_states: deque = deque(maxlen=200)
        self._stream_running: bool = False
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_sock: Optional[socket.socket] = None
        self._last_state_engine_time: float = 0.0
        self._last_state_local_time: float = 0.0
        self.map_manager = OSMTileMapManager()
        self.show_coverage_map: bool = False
        self.coverage_map_view = CoverageMapView(self.map_manager)

        self.live_history: deque = deque(maxlen=HISTORY_MAX_LEN)
        self.paused_history: Optional[List[dict]] = None
        self.paused_idx: int = 0
        self.paused: bool = False
        self.last_signature: str = ""
        self.total_packets_received: int = 0

        self.edit_mode: bool = False
        self.selected_id: Optional[str] = None
        self.map_selected_id: Optional[str] = None
        self.map_inset_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.dragging_id: Optional[str] = None
        self.drag_offset_logical: Tuple[float, float] = (0.0, 0.0)
        self.resizing_panel_id: Optional[str] = None
        self.resize_start_mouse: Tuple[int, int] = (0, 0)
        self.resize_start_size: Tuple[int, int] = (0, 0)
        self.panel_resize_rects: Dict[str, pygame.Rect] = {}

        self.readonly_inspector_id: Optional[str] = None
        self.inspector_pos: Optional[Tuple[int, int]] = None
        self.inspector_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_close_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_apply_ovr_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_revert_ovr_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_copy_hex_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_last_raw_hex: str = ""
        self.inspector_edit_rects: Dict[str, pygame.Rect] = {}
        self.inspector_active_field: Optional[str] = None
        self.inspector_edit_buffers: Dict[str, str] = {}
        self.temp_wiu_overrides: Dict[str, Dict[str, Any]] = {}
        self.orig_element_bindings: Dict[str, Dict[str, Any]] = {}
        self.scroller_history: deque = deque(maxlen=1500)
        self.scroller_scroll_offset: int = 0
        self.scroller_last_top_entry: Optional[str] = None
        self.scroller_panel_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.scroller_line_rects: List[Tuple[pygame.Rect, int, str]] = []
        self.scroller_btn_rects: Dict[str, pygame.Rect] = {}
        self.scroller_selecting: bool = False
        self.scroller_sel_start: Optional[int] = None
        self.scroller_sel_end: Optional[int] = None
        self.thanks_text: str = self._load_local_thanks_text()
        self.rrdata: Dict[str, str] = self._load_local_rrdata()
        self.inspector_dragging: bool = False
        self.inspector_drag_offset: Tuple[int, int] = (0, 0)

        self.last_click_time: float = 0.0
        self.last_click_id: Optional[str] = None
        self.renaming_active: bool = False
        self.rename_buffer: str = ""
        self.status_toast: str = f"Connecting to engine layout server {engine_host}:{engine_port}..."

        self.last_seen_signal_colors: Dict[str, str] = {}
        self.hud_buttons: Dict[str, pygame.Rect] = {}
        self.banc_coverage_modal: Optional[BancCoverageMapModal] = None
        self.banc_config_path: Optional[str] = banc_config
        self.banc_client = mosfet_client.create_banc_client_from_config(banc_config)

        # Local layout loader prompt & completion
        self.local_layout_prompt_active: bool = False
        self.local_layout_buffer: str = ""
        self.local_layout_all_files: List[str] = []
        self.local_layout_suggestions: List[str] = []
        self.local_layout_selected_idx: int = -1
        self.local_layout_suggestion_rects: List[Tuple[pygame.Rect, str]] = []

        self._fetch_remote_layout_from_engine()
        self.sync_chat_identities_to_engine()
        self._run_startup_splash_5s()
        self._start_stream_worker()
        self._start_heartbeat_worker()

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

    @staticmethod
    def _load_local_thanks_text() -> str:
        for d in [os.getcwd(), os.path.join(os.getcwd(), "config")]:
            p = os.path.join(d, "thanks.txt")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read().strip()
                except Exception:
                    pass
        return ""

    @staticmethod
    def _load_local_rrdata() -> Dict[str, str]:
        for d in [os.getcwd(), os.path.join(os.getcwd(), "config")]:
            p = os.path.join(d, "rrdata.json")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                        if isinstance(raw, dict):
                            return {str(k): str(v) for k, v in raw.items()}
                except Exception:
                    pass
        return {}

    def _draw_startup_splash(self, message: str, progress: float = 0.5):
        pygame.event.pump()
        self.screen.fill(COLOR_BG)
        step = int(LOGICAL_GRID_SCALE)
        for x in range(0, self.width, step):
            pygame.draw.line(self.screen, COLOR_GRID_MINOR, (x, 0), (x, self.height), 1)
        for y in range(0, self.height, step):
            pygame.draw.line(self.screen, COLOR_GRID_MINOR, (0, y), (self.width, y), 1)

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

        title = self.font_xl.render(f"Welcome to ApexBOS Viewer {VERSION}", True, COLOR_EDIT_ACCENT)
        sub = self.font_md.render("Railroad Dispatcher Remote Display & Telemetry Renderer", True, COLOR_TEXT)
        copy_txt = self.font_sm_bold.render("Copyright by AB  |  Licensed under the MIT License", True, COLOR_TRACK_GREEN)

        self.screen.blit(title, (cx + (card_w - title.get_width()) // 2, cy + 20))
        self.screen.blit(sub, (cx + (card_w - sub.get_width()) // 2, cy + 56))
        self.screen.blit(copy_txt, (cx + (card_w - copy_txt.get_width()) // 2, cy + 84))

        if thanks_lines:
            box_rect = pygame.Rect(cx + 36, cy + 114, card_w - 72, extra_h - 10)
            pygame.draw.rect(self.screen, (14, 20, 30), box_rect, border_radius=6)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, box_rect, 1, border_radius=6)
            for idx, tline in enumerate(thanks_lines):
                ts = font_thanks.render(tline, True, COLOR_LANDMARK)
                self.screen.blit(ts, (box_rect.x + (box_rect.width - ts.get_width()) // 2, box_rect.y + 10 + idx * line_height))

        msg_y = cy + 124 + extra_h
        msg_surf = self.font_md.render(message, True, COLOR_TEXT)
        self.screen.blit(msg_surf, (cx + (card_w - msg_surf.get_width()) // 2, msg_y))

        bar_w, bar_h = card_w - 100, 18
        bar_x, bar_y = cx + 50, msg_y + 32
        pygame.draw.rect(self.screen, (12, 16, 24), (bar_x, bar_y, bar_w, bar_h), border_radius=5)
        fill_w = max(4, int(bar_w * max(0.0, min(1.0, progress))))
        pygame.draw.rect(self.screen, COLOR_SELECT_RING, (bar_x, bar_y, fill_w, bar_h), border_radius=5)
        pygame.draw.rect(self.screen, COLOR_HUD_BORDER, (bar_x, bar_y, bar_w, bar_h), 1, border_radius=5)

        pygame.display.flip()

    def _run_startup_splash_5s(self):
        steps = 50
        for step_i in range(1, steps + 1):
            frac = step_i / float(steps)
            self._draw_startup_splash(
                f"Initializing Viewer ({self.prefs.layout_filename})... ({int(frac * 100)}%)",
                progress=frac,
            )
            time.sleep(5.0 / steps)

    _run_startup_splash_3s = _run_startup_splash_5s

    def _fetch_remote_layout_from_engine(self):
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=3.5) as sock:
                local_ip = sock.getsockname()[0]
                if local_ip:
                    self.receiver.join_interface(local_ip)
                sock.sendall(b"GET_LAYOUT\n")
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
            layout_fname = payload.get("layout_filename", "remote_layout.json")
            if layout_fname != self.prefs.layout_filename and self.prefs.layout_filename != "default_layout.json":
                self.save_preferences()
            self.prefs.set_layout_filename(layout_fname)
            self._apply_saved_view()
            if payload.get("thanks_text"):
                self.thanks_text = str(payload["thanks_text"]).strip()
            if isinstance(payload.get("rrdata"), dict) and payload["rrdata"]:
                self.rrdata.update({str(k): str(v) for k, v in payload["rrdata"].items()})

            if payload.get("layout"):
                self.raw_layout = payload.get("layout", {})
            cur_state = payload.get("current_state")
            if cur_state:
                self._record_live_state(cur_state)
            else:
                self._seed_from_layout_dict(payload.get("layout", {}), layout_fname)

            self.status_toast = f"Synced layout '{layout_fname}' from {self.engine_host}:{self.engine_port} (Prefs: {self.prefs.filepath})"
        except Exception as exc:
            self.status_toast = f"Awaiting multicast (TCP layout server {self.engine_host}:{self.engine_port} unreachable: {exc})"

    def _start_stream_worker(self):
        if self._stream_running:
            return
        self._stream_running = True
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()

    def _stream_loop(self):
        while self._stream_running:
            sock = None
            try:
                sock = socket.create_connection((self.engine_host, self.engine_port), timeout=3.5)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except (AttributeError, OSError):
                    pass
                local_ip = sock.getsockname()[0]
                if local_ip:
                    self.receiver.join_interface(local_ip)
                self._stream_sock = sock
                sock.sendall(b"STREAM_STATE\n")
                self.status_toast = f"Connected to real-time engine stream {self.engine_host}:{self.engine_port}"
                while self._stream_running:
                    hdr_raw = self._recv_exact(sock, 8)
                    if not hdr_raw:
                        break
                    payload_len, magic = struct.unpack("!I4s", hdr_raw)
                    if payload_len <= 0 or payload_len > 10 * 1024 * 1024:
                        break
                    payload = self._recv_exact(sock, payload_len)
                    if not payload:
                        break
                    try:
                        if magic == CHUNK_MAGIC_LZ4 and lz4_frame is not None:
                            decomp = lz4_frame.decompress(payload)
                        else:
                            decomp = zlib.decompress(payload)
                        st = json.loads(decomp.decode("utf-8"))
                        if isinstance(st, dict):
                            self._pending_stream_states.append(st)
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self._stream_sock = None
            if self._stream_running:
                time.sleep(1.0)

    def _recv_exact(self, sock: socket.socket, num_bytes: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < num_bytes and self._stream_running:
            try:
                r, _, _ = select.select([sock], [], [], 1.0)
                if not r:
                    continue
                chunk = sock.recv(min(num_bytes - len(buf), 65536))
                if not chunk:
                    return None
                buf.extend(chunk)
            except OSError:
                return None
        return bytes(buf) if len(buf) == num_bytes else None

    def _start_heartbeat_worker(self):
        if self._heartbeat_running:
            return
        self._heartbeat_running = True
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while self._heartbeat_running:
            time.sleep(1.5)
            now = time.time()
            if (now - self.last_packet_time > 2.0) or (now - self._last_heartbeat_time >= 10.0):
                self._send_engine_heartbeat()

    def _send_engine_heartbeat(self):
        self._last_heartbeat_time = time.time()
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=2.5) as sock:
                local_ip = sock.getsockname()[0]
                if local_ip:
                    self.receiver.join_interface(local_ip)
                sock.sendall(b"HEARTBEAT\n")
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                resp = json.loads(b"".join(chunks).decode("utf-8"))
                if (time.time() - self.last_packet_time) > 1.8:
                    st = resp.get("current_state")
                    if st:
                        self._pending_heartbeat_states.append(st)
        except Exception:
            pass

    def fetch_banc_layouts_fallback(self) -> List[dict]:
        """Queries Engine layout server via TCP for Banc layouts if direct Banc connection is unavailable."""
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=3.5) as sock:
                sock.sendall(b"GET_BANC_LAYOUTS\n")
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            resp = json.loads(b"".join(chunks).decode("utf-8"))
            if resp.get("ok"):
                return resp.get("layouts", [])
        except Exception:
            pass
        return []

    def load_layout_from_banc(self, layout_name: str) -> bool:
        """Sends command to DispatcherEngine to download and switch to a Banc Cloud layout."""
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=6.0) as sock:
                sock.sendall(f"LOAD_BANC_LAYOUT {layout_name}\n".encode("utf-8"))
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            resp = json.loads(b"".join(chunks).decode("utf-8"))
            if resp.get("ok"):
                self._fetch_remote_layout_from_engine()
                self.status_toast = f"Loaded Banc layout '{layout_name}' into Engine!"
                self.banc_coverage_modal = None
                return True
            else:
                err = resp.get("error", "Unknown error")
                self.status_toast = f"Engine error: {err}"
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.set_toast(f"Engine error: {err}")
                return False
        except Exception as exc:
            # Fallback: if Engine unreachable, try loading directly into viewer
            if self.banc_client:
                try:
                    data = self.banc_client.download_layout_sync(layout_name)
                    if data:
                        self._seed_from_layout_dict(data, f"banc_{layout_name}.json")
                        self.status_toast = f"Loaded Banc layout '{layout_name}' locally (Engine not connected)"
                        self.banc_coverage_modal = None
                        return True
                except Exception as b_exc:
                    exc = b_exc
            self.status_toast = f"Failed to load Banc layout '{layout_name}': {exc}"
            if self.banc_coverage_modal:
                self.banc_coverage_modal.set_toast(f"Error: {exc}")
            return False

    def open_banc_coverage_modal(self):
        if not getattr(self, "banc_client", None):
            self.banc_client = mosfet_client.create_banc_client_from_config(getattr(self, "banc_config_path", None))
        self.banc_coverage_modal = BancCoverageMapModal(
            tile_mgr=self.map_manager,
            client=self.banc_client,
            on_download_and_open_cb=self.load_layout_from_banc,
            on_upload_current_cb=None,
            on_close_cb=self.close_banc_coverage_modal,
            is_viewer_mode=True,
            fetch_layouts_fallback_cb=self.fetch_banc_layouts_fallback,
        )

    def close_banc_coverage_modal(self):
        self.banc_coverage_modal = None

    def fetch_local_layouts_from_engine(self) -> List[str]:
        """Queries Engine layout server via TCP for the list of files in the layouts subfolder."""
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=3.5) as sock:
                sock.sendall(b"GET_LOCAL_LAYOUTS\n")
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            resp = json.loads(b"".join(chunks).decode("utf-8"))
            if resp.get("ok"):
                return resp.get("layouts", [])
        except Exception:
            pass

        # Fallback: list files in local layouts/ subfolder if engine is unreachable
        candidates = [
            os.path.join(os.getcwd(), "layouts"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts"),
        ]
        local_files = set()
        for cdir in candidates:
            if os.path.isdir(cdir):
                try:
                    for f in os.listdir(cdir):
                        if not f.startswith(".") and f.lower().endswith((".json", ".itcvl")):
                            local_files.add(f)
                except Exception:
                    pass
        return sorted(list(local_files), key=lambda s: s.lower())

    def load_local_layout_via_engine(self, layout_name: str) -> bool:
        """Sends command to DispatcherEngine to switch to a local layout from the layouts subfolder."""
        clean_name = os.path.basename(layout_name.strip())
        if not clean_name:
            self.status_toast = "No layout name specified"
            return False

        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=6.0) as sock:
                sock.sendall(f"LOAD_LOCAL_LAYOUT {clean_name}\n".encode("utf-8"))
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            resp = json.loads(b"".join(chunks).decode("utf-8"))
            if resp.get("ok"):
                self._fetch_remote_layout_from_engine()
                self.status_toast = f"Loaded local layout '{clean_name}' into Engine!"
                self.close_local_layout_prompt()
                return True
            else:
                err = resp.get("error", "Unknown error")
                self.status_toast = f"Engine error: {err}"
                return False
        except Exception as exc:
            # Fallback: if Engine unreachable, try loading directly into viewer
            name_with_ext = clean_name if clean_name.lower().endswith((".json", ".itcvl")) else f"{clean_name}.json"
            cand_files = [
                os.path.join(os.getcwd(), "layouts", name_with_ext),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts", name_with_ext),
                os.path.join(os.getcwd(), "layouts", clean_name),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts", clean_name),
            ]
            for cand in cand_files:
                if os.path.isfile(cand):
                    try:
                        with open(cand, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        self._seed_from_layout_dict(data, os.path.basename(cand))
                        self.status_toast = f"Loaded layout '{clean_name}' locally (Engine not connected)"
                        self.close_local_layout_prompt()
                        return True
                    except Exception as fexc:
                        self.status_toast = f"Failed to load layout '{clean_name}': {fexc}"
                        return False
            self.status_toast = f"Failed to load layout '{clean_name}': {exc}"
            return False

    def open_local_layout_prompt(self, default_buffer: str = ""):
        """Opens the local layout selection prompt with tab-completion."""
        self.close_banc_coverage_modal()
        self.show_coverage_map = False
        self.renaming_active = False
        self.local_layout_prompt_active = True
        self.local_layout_buffer = default_buffer
        self.local_layout_selected_idx = -1
        self.local_layout_suggestion_rects = []
        self.local_layout_all_files = self.fetch_local_layouts_from_engine()
        self._update_local_layout_suggestions()
        self.status_toast = "Local Layout: Type name, Tab to complete, Up/Down to select, Enter to load, Esc to cancel"

    def close_local_layout_prompt(self):
        """Closes the local layout selection prompt."""
        self.local_layout_prompt_active = False
        self.local_layout_suggestions = []
        self.local_layout_selected_idx = -1
        self.local_layout_suggestion_rects = []

    def toggle_local_layout_prompt(self):
        """Toggles the local layout selection prompt."""
        if self.local_layout_prompt_active:
            self.close_local_layout_prompt()
        else:
            self.open_local_layout_prompt()

    def _update_local_layout_suggestions(self):
        """Filters available layouts based on current user buffer input."""
        clean_buf = (self.local_layout_buffer or "").strip().lower()
        files = getattr(self, "local_layout_all_files", []) or []

        matches = []
        for fname in files:
            f_lower = fname.lower()
            stem_lower = f_lower[:-5] if f_lower.endswith(".json") else f_lower
            if not clean_buf:
                matches.append((1, f_lower, fname))
            elif f_lower.startswith(clean_buf) or stem_lower.startswith(clean_buf):
                matches.append((0, f_lower, fname))
            elif clean_buf in f_lower:
                matches.append((2, f_lower, fname))

        matches.sort(key=lambda t: (t[0], t[1]))
        self.local_layout_suggestions = [cand for _, _, cand in matches[:10]]
        if self.local_layout_selected_idx >= len(self.local_layout_suggestions):
            self.local_layout_selected_idx = -1

    def _handle_local_layout_prompt_keydown(self, event: pygame.event.Event):
        """Handles keyboard navigation and autocompletion for the local layout prompt."""
        if event.key == pygame.K_TAB:
            if getattr(self, "local_layout_suggestions", None):
                if 0 <= self.local_layout_selected_idx < len(self.local_layout_suggestions):
                    self.local_layout_selected_idx = (self.local_layout_selected_idx + 1) % len(self.local_layout_suggestions)
                else:
                    self.local_layout_selected_idx = 0
                self.local_layout_buffer = self.local_layout_suggestions[self.local_layout_selected_idx]
            return
        elif event.key == pygame.K_UP:
            if getattr(self, "local_layout_suggestions", None):
                if self.local_layout_selected_idx <= 0:
                    self.local_layout_selected_idx = len(self.local_layout_suggestions) - 1
                else:
                    self.local_layout_selected_idx -= 1
                self.local_layout_buffer = self.local_layout_suggestions[self.local_layout_selected_idx]
            return
        elif event.key == pygame.K_DOWN:
            if getattr(self, "local_layout_suggestions", None):
                if self.local_layout_selected_idx >= len(self.local_layout_suggestions) - 1:
                    self.local_layout_selected_idx = 0
                else:
                    self.local_layout_selected_idx += 1
                self.local_layout_buffer = self.local_layout_suggestions[self.local_layout_selected_idx]
            return
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            target_name = (self.local_layout_buffer or "").strip()
            if target_name:
                self.load_local_layout_via_engine(target_name)
            return
        elif event.key == pygame.K_ESCAPE:
            self.close_local_layout_prompt()
            self.status_toast = "Local layout selection cancelled."
            return
        elif event.key == pygame.K_BACKSPACE:
            if event.mod & pygame.KMOD_CTRL:
                self.local_layout_buffer = ""
            else:
                self.local_layout_buffer = self.local_layout_buffer[:-1]
            self.local_layout_selected_idx = -1
            self._update_local_layout_suggestions()
            return
        elif event.unicode and event.unicode.isprintable():
            self.local_layout_buffer += event.unicode
            self.local_layout_selected_idx = -1
            self._update_local_layout_suggestions()
            return

    def _handle_local_layout_prompt_mouse_down(self, pos: Tuple[int, int]) -> bool:
        """Handles mouse selection on local layout suggestions popup."""
        mx, my = pos
        for r, cand in getattr(self, "local_layout_suggestion_rects", []):
            if r.collidepoint(mx, my):
                self.local_layout_buffer = cand
                try:
                    self.local_layout_selected_idx = self.local_layout_suggestions.index(cand)
                except ValueError:
                    self.local_layout_selected_idx = -1
                self._update_local_layout_suggestions()
                return True
        return False

    def lookup_wiu_name(self, wid: Any) -> str:
        """Looks up the WIU name from the loaded layout or live state based on WIU ID."""
        if wid is None:
            return ""
        try:
            wid_int = int(wid)
        except (ValueError, TypeError):
            return ""

        raw_layout = getattr(self, "raw_layout", None)
        if isinstance(raw_layout, dict):
            raw_wius = raw_layout.get("wius")
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

            for el in raw_layout.get("elements", []):
                el_wid = el.get("wiu_id") or (el.get("radio_id") if el.get("elem_type") == "base" else None)
                if el_wid is not None:
                    try:
                        if int(el_wid) == wid_int:
                            lbl = el.get("label") or el.get("name") or ""
                            if str(lbl).strip():
                                return str(lbl).strip()
                    except (ValueError, TypeError):
                        pass

        state = self.current_state()
        if state and isinstance(state.get("elements"), dict):
            for el in state["elements"].values():
                el_wid = el.get("wiu_id") or (el.get("radio_id") if el.get("elem_type") == "base" else None)
                if el_wid is not None:
                    try:
                        if int(el_wid) == wid_int:
                            lbl = el.get("label") or el.get("name") or ""
                            if str(lbl).strip():
                                return str(lbl).strip()
                    except (ValueError, TypeError):
                        pass
        return ""

    def _seed_from_layout_dict(self, raw_layout: dict, layout_fname: str):
        self.raw_layout = raw_layout
        elements_dict = {}
        for el in raw_layout.get("elements", []):
            eid = el["id"]
            el_copy = copy.deepcopy(el)
            el_copy.setdefault("label_pos", "top")
            el_copy.setdefault("hw_occupied", False)
            el_copy.setdefault("inferred_occupied", False)
            el_copy.setdefault("occupied", False)
            el_copy.setdefault("green_path", False)
            el_copy.setdefault("green_path_dir", None)
            el_copy.setdefault("radio_id", None)
            el_copy.setdefault("display_color", "WHITE")
            el_copy.setdefault("alignment", "STRAIGHT")
            el_copy.setdefault("state", 0)
            el_copy.setdefault("signal_val", 32)
            el_copy.setdefault("lamp_aspect", "gray")
            el_copy.setdefault("base_color", "GRAY")
            el_copy.setdefault("flashing", False)
            el_copy.setdefault("status", "DARK")
            el_copy.setdefault("gate_status", "OPEN")
            el_copy.setdefault("packets_relayed", 0)
            el_copy.setdefault("packets_received", 0)
            el_copy.setdefault("last_status", "IDLE")
            el_copy.setdefault("collision_warning", False)
            el_copy.setdefault("last_packet_ts", 0.0)
            elements_dict[eid] = el_copy

        sc = raw_layout.get("scroller")
        at = raw_layout.get("address_table")
        ts = raw_layout.get("train_scroller")
        ch = raw_layout.get("chat")
        if not ch:
            for el in raw_layout.get("elements", []):
                if isinstance(el, dict) and el.get("elem_type") == "chat":
                    ch = el
                    break
        seeded_state = {
            "version": raw_layout.get("version", VERSION),
            "layout_filename": layout_fname,
            "timestamp": time.time(),
            "tx_frames": 0,
            "suppressed_frames": 0,
            "collisions": [],
            "elements": elements_dict,
            "packet_scroller": None,
            "address_table": None,
            "train_scroller": None,
            "chat": None,
        }
        if sc:
            seeded_state["packet_scroller"] = {
                "id": "SYS_PACKET_SCROLLER", "elem_type": "packet_scroller",
                "x": sc.get("x", 0), "y": sc.get("y", 22), "max_len": sc.get("max_len", 10),
                "width": sc.get("width", 580), "height": sc.get("height", 195),
                "start_time": time.time(), "elapsed_sec": 0.0, "elapsed_str": "00:00:00",
                "total_packets": 0, "packets_received": 0, "last_packet_ts": 0.0, "last_status": "ACTIVE (0 pkts)",
                "total_ppm": 0, "channel_stats": {},
                "entries": [f"[SYNCED] Loaded remote layout '{layout_fname}' from engine..."],
            }
        if at:
            seeded_state["address_table"] = {
                "id": "SYS_ADDRESS_TABLE", "elem_type": "address_table",
                "x": at.get("x", 26), "y": at.get("y", 22),
                "width": at.get("width", 450), "height": at.get("height", 150),
                "packets_received": 0, "last_packet_ts": 0.0, "last_status": "0 WIUs active",
                "wius": {},
            }
        if ts:
            seeded_state["train_scroller"] = {
                "id": "SYS_TRAIN_SCROLLER", "elem_type": "train_scroller",
                "x": ts.get("x", 50), "y": ts.get("y", 22), "max_len": ts.get("max_len", 10),
                "width": ts.get("width", 430), "height": ts.get("height", 150),
                "packets_received": 0, "last_packet_ts": 0.0, "last_status": "0 trains tracked",
                "trains": [],
            }
        if ch:
            seeded_state["chat"] = {
                "id": ch.get("id", "SYS_CHAT"), "elem_type": "chat",
                "label": ch.get("label", "CHAT"),
                "x": ch.get("x", 74), "y": ch.get("y", 22),
                "max_len": ch.get("max_len", ch.get("extra_info", 50)),
                "width": ch.get("width", 460), "height": ch.get("height", 220),
                "packets_received": 0, "last_packet_ts": 0.0, "last_status": "0 messages",
                "total_messages": 0,
                "messages": [],
            }
        self._record_live_state(seeded_state)
        if not self._apply_saved_view():
            self.center_view_on_layout(seeded_state)
            self.has_centered_on_layout = True
        self.last_centered_layout_fname = layout_fname

    @staticmethod
    def _compute_state_signature(state: dict) -> str:
        elems = state.get("elements", {})
        parts = []
        for eid in sorted(elems.keys()):
            el = elems[eid]
            parts.append(
                f"{eid}:{el.get('display_color')}:{el.get('state')}:"
                f"{el.get('lamp_aspect')}:{el.get('gate_status')}:"
                f"{el.get('collision_warning')}:{el.get('estimated_speed_mph')}:"
                f"{el.get('packets_received')}:{round(float(el.get('last_packet_ts', 0.0) or 0.0), 2)}"
            )
        chat_st = state.get("chat")
        if isinstance(chat_st, dict):
            msgs = chat_st.get("messages") or []
            last_mid = msgs[-1].get("id", len(msgs)) if msgs and isinstance(msgs[-1], dict) else len(msgs)
            parts.append(f"CHAT:{chat_st.get('total_messages', len(msgs))}:{last_mid}")
        return "|".join(parts)

    def _ingest_scroller_entries(self, entries: List[str]):
        if not entries:
            return
        if not self.scroller_history or self.scroller_last_top_entry is None:
            for e in reversed(entries):
                self.scroller_history.appendleft(e)
            self.scroller_last_top_entry = entries[0]
            return
        if entries[0] == self.scroller_last_top_entry:
            return
        try:
            idx = entries.index(self.scroller_last_top_entry)
            new_items = entries[:idx]
        except ValueError:
            new_items = [e for e in entries if e not in self.scroller_history]
        if new_items:
            n_new = len(new_items)
            for e in reversed(new_items):
                self.scroller_history.appendleft(e)
            self.scroller_last_top_entry = entries[0]
            if self.scroller_scroll_offset > 0:
                self.scroller_scroll_offset = min(
                    max(0, len(self.scroller_history) - 1),
                    self.scroller_scroll_offset + n_new,
                )
            if self.scroller_sel_start is not None and self.scroller_sel_end is not None:
                self.scroller_sel_start += n_new
                self.scroller_sel_end += n_new

    def _apply_local_temp_overrides_to_state(self, state: dict):
        if not hasattr(self, "orig_element_bindings"):
            self.orig_element_bindings = {}
        if not hasattr(self, "temp_wiu_overrides"):
            self.temp_wiu_overrides = {}
        elems = state.get("elements", {})
        for eid, el in elems.items():
            if eid not in self.orig_element_bindings:
                self.orig_element_bindings[eid] = {
                    "wiu_id": el.get("wiu_id"),
                    "has_occ_bit": el.get("has_occ_bit", False),
                    "occ_bit_offset": el.get("occ_bit_offset"),
                    "cmd_bit_offset": el.get("cmd_bit_offset"),
                    "sig_bit_offset": el.get("sig_bit_offset"),
                }
            if eid in self.temp_wiu_overrides:
                ovr = self.temp_wiu_overrides[eid]
                for k, v in ovr.items():
                    el[k] = v

    def _send_engine_command(self, cmd: str) -> Optional[dict]:
        try:
            with socket.create_connection((self.engine_host, self.engine_port), timeout=3.5) as sock:
                sock.sendall(cmd.strip().encode("utf-8") + b"\n")
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                resp_raw = b"".join(chunks).decode("utf-8", errors="ignore").strip()
                if resp_raw:
                    resp = json.loads(resp_raw)
                    if isinstance(resp, dict) and resp.get("current_state"):
                        self._record_live_state(resp["current_state"])
                    return resp
        except Exception:
            pass
        return None

    def _send_temp_override_to_engine(self, cmd_prefix: str, payload_obj: dict):
        self._send_engine_command(f"{cmd_prefix} {json.dumps(payload_obj)}")

    def _copy_text_to_clipboard(self, text: str):
        if not text:
            return
        copied = False
        try:
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
            r.destroy()
            copied = True
        except Exception:
            pass
        if not copied and os.name == "nt":
            try:
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

    def _get_chat_font(self, bold: bool = False, italic: bool = False, underline: bool = False) -> pygame.font.Font:
        sz = 12
        key = (sz, bool(bold), bool(italic), bool(underline))
        if not hasattr(self, "_chat_font_cache"):
            self._chat_font_cache = {}
        if key not in self._chat_font_cache:
            fnt = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", sz, bold=bool(bold), italic=bool(italic))
            fnt.set_underline(bool(underline))
            self._chat_font_cache[key] = fnt
        return self._chat_font_cache[key]

    def get_viewer_chat_identity(self, state: Optional[dict] = None) -> str:
        """Returns the active chat identity for this Viewer (local override or Engine identity)."""
        if self.chat_identity_store.self_identity:
            return self.chat_identity_store.self_identity
        if state is None:
            state = self.current_state()
        if isinstance(state, dict):
            cid = state.get("chat_identity")
            if not cid and isinstance(state.get("chat"), dict):
                cid = state["chat"].get("active_identity")
            if cid:
                return mosfet_client.sanitize_chat_identity(cid)
        return "Dispatcher"

    def sync_chat_identities_to_engine(self) -> Optional[dict]:
        """Sends the user's identity/name mapping to the local Engine so Engine converts names to identities before sending."""
        payload = {
            "self_identity": self.chat_identity_store.self_identity,
            "self_name": self.chat_identity_store.self_name,
            "contacts": dict(self.chat_identity_store.contacts),
        }
        return self._send_engine_command(f"SET_CHAT_IDENTITY_MAP {json.dumps(payload)}")

    def associate_own_identity_and_name(self, identity: str, name: str):
        """Associates the user's own identity with their friendly name and syncs to Engine."""
        self.chat_identity_store.set_self_identity_name(identity, name, persist=True)
        self.chat_id_modal_buffers["self_identity"] = self.chat_identity_store.self_identity
        self.chat_id_modal_buffers["self_name"] = self.chat_identity_store.self_name
        self.sync_chat_identities_to_engine()
        self.status_toast = f"Chat: Associated your identity '{self.chat_identity_store.self_identity}' with name '{self.chat_identity_store.self_name}'"

    def associate_incoming_identity_with_name(self, identity: str, name: str):
        """Associates an incoming sender identity with a persistent friendly name in the Viewer."""
        self.chat_identity_store.assign_contact_name(identity, name, persist=True)
        self.sync_chat_identities_to_engine()
        cid = mosfet_client.sanitize_chat_identity(identity)
        cname = self.chat_identity_store.contacts.get(cid, "")
        if cname:
            self.status_toast = f"Chat: Associated identity '{cid}' with name '{cname}' (saved)"
        else:
            self.status_toast = f"Chat: Removed name association for '{cid}'"

    def _ingest_chat_messages_from_state(self, chat_state: dict):
        if not isinstance(chat_state, dict):
            return
        msgs = chat_state.get("messages")
        if not isinstance(msgs, list):
            return
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mid = int(m.get("id") or 0)
            ts = float(m.get("timestamp") or 0.0)
            if mid > 0 and mid <= self.chat_cleared_before_id:
                continue
            if self.chat_cleared_before_ts > 0.0 and ts > 0.0 and ts <= self.chat_cleared_before_ts and mid <= self.chat_cleared_before_id:
                continue
            sender = mosfet_client.sanitize_chat_identity(
                m.get("sender_identity") or m.get("sender") or "Unknown"
            )
            text = mosfet_client.sanitize_chat_text(m.get("text") or "")
            if not text:
                continue
            dedup_key = (mid, round(ts, 2), sender, text)
            if dedup_key in self.chat_seen_keys:
                continue
            self.chat_seen_keys.add(dedup_key)
            targets = m.get("target_identities")
            if not isinstance(targets, list):
                targets = mosfet_client.extract_chat_mentions(text)
            else:
                targets = [mosfet_client.sanitize_chat_identity(t) for t in targets if mosfet_client.sanitize_chat_identity(t)]
            entry = {
                "id": mid,
                "timestamp": ts if ts > 0.0 else time.time(),
                "time_str": m.get("time_str") or time.strftime("%H:%M:%S", time.localtime(ts if ts > 0.0 else time.time())),
                "sender_identity": sender,
                "text": text,
                "target_identities": targets,
            }
            self.chat_history.append(entry)

    def should_display_chat_message(self, msg: dict, state: Optional[dict] = None) -> bool:
        """
        Applies Viewer-side filtering:
          1. Mute All toggle
          2. Identity filtering (@<identity> directed messages are only shown if addressed to or sent by this Viewer's identity)
          3. RegEx Include filter
          4. RegEx Exclude filter
        """
        if self.chat_mute_all:
            return False
        if not isinstance(msg, dict):
            return False
        raw_text = str(msg.get("text") or "")
        sender_id = str(msg.get("sender_identity") or msg.get("sender") or "Unknown")
        targets = msg.get("target_identities")
        if not isinstance(targets, list):
            targets = mosfet_client.extract_chat_mentions(raw_text)

        # Viewer-side filtering by identity
        if targets:
            my_id = self.get_viewer_chat_identity(state).lower()
            self_id_override = self.chat_identity_store.self_identity.lower()
            target_lowers = {str(t).strip().lower() for t in targets if str(t).strip()}
            is_recipient = (my_id in target_lowers) or (bool(self_id_override) and self_id_override in target_lowers)
            is_sender = (sender_id.lower() == my_id) or (bool(self_id_override) and sender_id.lower() == self_id_override)
            if not (is_recipient or is_sender):
                return False

        disp_sender = self.chat_identity_store.resolve_display_name(sender_id)
        disp_text = self.chat_identity_store.replace_identities_with_names_in_text(raw_text)
        plain_text = strip_chat_formatting(disp_text)
        plain_raw = strip_chat_formatting(raw_text)
        searchable = f"{disp_sender} {sender_id} {plain_text} {plain_raw} {raw_text}"

        if self.chat_include_regex.strip():
            inc_re = compile_safe_chat_regex(self.chat_include_regex)
            if inc_re is None:
                return False
            if not inc_re.search(searchable):
                return False

        if self.chat_exclude_regex.strip():
            exc_re = compile_safe_chat_regex(self.chat_exclude_regex)
            if exc_re is not None and exc_re.search(searchable):
                return False

        return True

    def get_filtered_chat_messages(self, state: Optional[dict] = None) -> List[dict]:
        if state is None:
            state = self.current_state()
        if state and isinstance(state.get("chat"), dict):
            self._ingest_chat_messages_from_state(state["chat"])
        return [m for m in self.chat_history if self.should_display_chat_message(m, state)]

    def send_viewer_chat_message(self, text: Optional[str] = None) -> Optional[dict]:
        """Accepts typed message from user in Viewer and sends it to the Engine."""
        raw_msg = text if text is not None else self.chat_input_buffer
        clean_msg = mosfet_client.sanitize_chat_text(raw_msg)
        if not clean_msg:
            self.status_toast = "Chat: Cannot send empty message"
            return None
        sender_val = self.chat_identity_store.self_name or self.chat_identity_store.self_identity or self.get_viewer_chat_identity()
        payload = {
            "text": clean_msg,
            "sender": sender_val,
            "self_identity": self.chat_identity_store.self_identity,
            "self_name": self.chat_identity_store.self_name,
            "contacts": dict(self.chat_identity_store.contacts),
        }
        resp = self._send_engine_command(f"SEND_CHAT {json.dumps(payload)}")
        self.chat_input_buffer = ""
        self.chat_scroll_offset = 0
        if resp and isinstance(resp, dict) and (resp.get("ok") or resp.get("status") == "ok"):
            sent_msg = resp.get("message")
            if isinstance(sent_msg, dict):
                self._ingest_chat_messages_from_state({"messages": [sent_msg]})
            self.status_toast = "Chat: Message sent via Engine"
            return sent_msg

        # Offline / standalone fallback: convert names to identities locally so names are never exposed
        converted_text, targets = self.chat_identity_store.convert_names_to_identities_in_text(clean_msg)
        sender_id = self.chat_identity_store.self_identity or self.get_viewer_chat_identity()
        now_ts = time.time()
        next_id = (max((int(m.get("id") or 0) for m in self.chat_history), default=0) + 1)
        fallback_msg = {
            "id": next_id,
            "timestamp": now_ts,
            "time_str": time.strftime("%H:%M:%S", time.localtime(now_ts)),
            "sender_identity": mosfet_client.sanitize_chat_identity(sender_id) or "Dispatcher",
            "text": converted_text,
            "target_identities": targets,
        }
        self._ingest_chat_messages_from_state({"messages": [fallback_msg]})
        self.status_toast = "Chat: Message queued locally (Engine offline)"
        return fallback_msg

    def clear_viewer_chat_messages(self):
        """Clears all messages in the Chat element and notifies Engine."""
        self.chat_cleared_before_ts = time.time()
        max_id = max((int(m.get("id") or 0) for m in self.chat_history), default=0)
        state = self.current_state()
        if state and isinstance(state.get("chat"), dict):
            for m in state["chat"].get("messages", []):
                if isinstance(m, dict):
                    max_id = max(max_id, int(m.get("id") or 0))
            state["chat"]["messages"] = []
        self.chat_cleared_before_id = max(self.chat_cleared_before_id, max_id)
        self.chat_history.clear()
        self.chat_seen_keys.clear()
        self.chat_scroll_offset = 0
        self._send_engine_command("CLEAR_CHAT")
        self.status_toast = "Chat: Cleared all messages"

    def page_chat_up(self, step: Optional[int] = None):
        filtered = self.get_filtered_chat_messages()
        avail = max(1, getattr(self, "chat_avail_rows", 8))
        s = step if step is not None else max(1, avail - 1)
        max_off = max(0, len(filtered) - avail)
        self.chat_scroll_offset = min(max_off, self.chat_scroll_offset + s)
        self.last_scrolled_panel = "chat"
        self.status_toast = f"Chat: Paged up (+{self.chat_scroll_offset})"

    def page_chat_down(self, step: Optional[int] = None):
        avail = max(1, getattr(self, "chat_avail_rows", 8))
        s = step if step is not None else max(1, avail - 1)
        self.chat_scroll_offset = max(0, self.chat_scroll_offset - s)
        self.last_scrolled_panel = "chat"
        self.status_toast = f"Chat: Paged down (+{self.chat_scroll_offset})"

    def insert_chat_markup(self, tag_type: str):
        if tag_type == "b":
            self.chat_input_buffer += "[b][/b]"
        elif tag_type == "i":
            self.chat_input_buffer += "[i][/i]"
        elif tag_type == "u":
            self.chat_input_buffer += "[u][/u]"
        elif tag_type == "color":
            cname = CHAT_COLOR_CYCLE[self.chat_color_idx % len(CHAT_COLOR_CYCLE)]
            self.chat_color_idx += 1
            self.chat_input_buffer += f"[{cname}][/{cname}]"
        self.chat_active_field = "message"

    def open_chat_identity_modal(self, prefill_contact_id: str = ""):
        self.chat_id_modal_active = True
        self.chat_id_modal_buffers["self_identity"] = self.chat_identity_store.self_identity or self.get_viewer_chat_identity()
        self.chat_id_modal_buffers["self_name"] = self.chat_identity_store.self_name
        if prefill_contact_id:
            cid = mosfet_client.sanitize_chat_identity(prefill_contact_id)
            self.chat_id_modal_buffers["contact_identity"] = cid
            self.chat_id_modal_buffers["contact_name"] = self.chat_identity_store.contacts.get(cid, "")
            self.chat_id_modal_active_field = "contact_name"
        else:
            self.chat_id_modal_active_field = "self_name" if self.chat_id_modal_buffers["self_identity"] else "self_identity"
        self.status_toast = "Chat Identity & Name Manager: Tab to switch fields, Enter to Save, Esc to Close"

    def copy_selected_scroller_packets(self):
        hist_list = list(self.scroller_history)
        if not hist_list:
            self.status_toast = "No packets in scroller to copy."
            return
        if self.scroller_sel_start is not None and self.scroller_sel_end is not None:
            lo = max(0, min(self.scroller_sel_start, self.scroller_sel_end))
            hi = min(len(hist_list) - 1, max(self.scroller_sel_start, self.scroller_sel_end))
            selected_lines = hist_list[lo : hi + 1]
        else:
            selected_lines = [hist_list[idx] for idx, _ in self.scroller_line_rects if 0 <= idx < len(hist_list)]
        if selected_lines:
            self._copy_text_to_clipboard("\n".join(selected_lines))
            self.status_toast = f"Copied {len(selected_lines)} packet line(s) to clipboard!"

    def _record_live_state(self, state: dict):
        incoming_fname = state.get("layout_filename")
        if state.get("thanks_text") and not self.thanks_text:
            self.thanks_text = str(state["thanks_text"]).strip()
        if isinstance(state.get("rrdata"), dict) and state["rrdata"]:
            self.rrdata.update({str(k): str(v) for k, v in state["rrdata"].items()})
        sc = state.get("packet_scroller")
        if isinstance(sc, dict) and sc.get("entries"):
            self._ingest_scroller_entries(list(sc["entries"]))
        chat_st = state.get("chat")
        if isinstance(chat_st, dict):
            self._ingest_chat_messages_from_state(chat_st)
        self._apply_local_temp_overrides_to_state(state)
        if incoming_fname and incoming_fname != self.prefs.layout_filename:
            if self.prefs.layout_filename != "default_layout.json":
                self.save_preferences()
            self.prefs.set_layout_filename(incoming_fname)
            self._apply_saved_view()

        if incoming_fname and incoming_fname != getattr(self, "last_centered_layout_fname", None):
            self.last_centered_layout_fname = incoming_fname
            if not self._apply_saved_view():
                if state.get("elements"):
                    self.center_view_on_layout(state)
                    self.has_centered_on_layout = True
        elif not getattr(self, "has_centered_on_layout", False) and state.get("elements"):
            if not self._apply_saved_view():
                self.center_view_on_layout(state)
                self.has_centered_on_layout = True

        sig = self._compute_state_signature(state)
        st_ts = float(state.get("timestamp") or 0.0)
        if st_ts > 0.0:
            self._last_state_engine_time = st_ts
            self._last_state_local_time = time.time()
        if sig == self.last_signature and self.live_history:
            self.live_history[-1] = state
            return

        self.last_signature = sig
        self.live_history.append(state)

    def current_state(self) -> Optional[dict]:
        if self.paused and self.paused_history:
            idx = max(0, min(self.paused_idx, len(self.paused_history) - 1))
            return self.paused_history[idx]
        if self.live_history:
            return self.live_history[-1]
        return None

    def get_reference_now(self, state: Optional[dict] = None) -> float:
        if self.paused:
            if state:
                return float(state.get("timestamp") or time.time())
            return time.time()
        if getattr(self, "_last_state_engine_time", 0.0) > 0.0:
            elapsed = time.time() - getattr(self, "_last_state_local_time", time.time())
            return self._last_state_engine_time + elapsed
        if state and state.get("timestamp"):
            return float(state["timestamp"])
        return time.time()

    def _toggle_pause(self):
        if not self.paused:
            if not self.live_history:
                return
            self.paused_history = copy.deepcopy(list(self.live_history))
            self.paused_idx = len(self.paused_history) - 1
            self.paused = True
            self.status_toast = f"PAUSED (Frozen copy: {self.paused_idx + 1}/{len(self.paused_history)})"
        else:
            self._resume_live()

    def _resume_live(self):
        self.paused = False
        self.paused_history = None
        self.paused_idx = 0
        self.status_toast = "Resumed LIVE frame (discarded paused history copy)"

    def _step_history(self, delta: int):
        if not self.paused:
            if not self.live_history:
                return
            self.paused_history = copy.deepcopy(list(self.live_history))
            self.paused_idx = len(self.paused_history) - 1
            self.paused = True

        if self.paused_history:
            self.paused_idx = max(0, min(len(self.paused_history) - 1, self.paused_idx + delta))
            self.status_toast = f"PAUSED History Frame: {self.paused_idx + 1} / {len(self.paused_history)}"

    # -----------------------------------------------------------------
    # Coordinate Transforms & Inactivity Fade Helper
    # -----------------------------------------------------------------
    def logical_to_world(self, lx: float, ly: float) -> Tuple[float, float]:
        return lx * LOGICAL_GRID_SCALE, ly * LOGICAL_GRID_SCALE

    def world_to_screen(self, wx: float, wy: float) -> Tuple[int, int]:
        return int(wx * self.zoom + self.cam_x), int(wy * self.zoom + self.cam_y)

    def logical_to_screen(self, lx: float, ly: float) -> Tuple[int, int]:
        wx, wy = self.logical_to_world(lx, ly)
        return self.world_to_screen(wx, wy)

    def screen_to_logical(self, sx: int, sy: int) -> Tuple[float, float]:
        wx = (sx - self.cam_x) / self.zoom
        wy = (sy - self.cam_y) / self.zoom
        return wx / LOGICAL_GRID_SCALE, wy / LOGICAL_GRID_SCALE

    def get_elem_logical_pos(self, el: dict) -> Tuple[float, float]:
        return self.prefs.get_pos(el["id"], el.get("x", 0), el.get("y", 0))

    def get_elem_label(self, el: dict) -> str:
        return self.prefs.get_label(el["id"], el.get("label", el["id"]))

    def get_element_inactivity(self, el: dict, ref_now: float) -> Tuple[bool, float]:
        last_ts = float(el.get("last_packet_ts", 0.0) or 0.0)
        if last_ts <= 0.0:
            return True, 999.0
        age = max(0.0, ref_now - last_ts)
        return (age >= self.stale_timeout_sec), age

    def _get_element_label_font(self, bold: bool = False) -> pygame.font.Font:
        """Returns a reduced, zoom-scaled font (clamped to [7, 14]) for track, signal, and turnout names."""
        sz = max(7, min(14, int(round(9.0 * self.zoom))))
        key = (sz, bool(bold))
        if not hasattr(self, "_elem_label_font_cache"):
            self._elem_label_font_cache = {}
        if key not in self._elem_label_font_cache:
            self._elem_label_font_cache[key] = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", sz, bold=bold)
        return self._elem_label_font_cache[key]

    def render_aged_label(self, text: str, el: dict, ref_now: float, active_rgb: Tuple[int, int, int] = (245, 250, 255)) -> pygame.Surface:
        is_stale, age = self.get_element_inactivity(el, ref_now)
        use_zoom_font = el.get("elem_type") in ("track", "signal", "turnout")
        if age < 3.5:
            font = self._get_element_label_font(bold=True) if use_zoom_font else self.font_sm_bold
            return font.render(text, True, active_rgb)

        ratio = min(1.0, max(0.0, (age - 3.5) / max(1.0, self.stale_timeout_sec)))
        barely_visible_gray = (52, 58, 68)
        r = int(active_rgb[0] * (1.0 - ratio) + barely_visible_gray[0] * ratio)
        g = int(active_rgb[1] * (1.0 - ratio) + barely_visible_gray[1] * ratio)
        b = int(active_rgb[2] * (1.0 - ratio) + barely_visible_gray[2] * ratio)
        is_bold = ratio < 0.35
        font = self._get_element_label_font(bold=is_bold) if use_zoom_font else (self.font_sm_bold if is_bold else self.font_sm_thin)
        return font.render(text, True, (r, g, b))

    def _get_label_rect(self, sx: int, sy: int, w: int, h: int, pos: str, offset: int) -> Tuple[int, int]:
        """Returns (x, y) top-left coordinates for blitting a label relative to element center (sx, sy)."""
        p = str(pos).lower()
        if p == "bottom":
            return (sx - w // 2, sy + offset)
        if p == "left":
            return (sx - offset - w, sy - h // 2)
        if p == "right":
            return (sx + offset, sy - h // 2)
        # default top
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

    def center_view_on_layout(self, state: Optional[dict] = None):
        """Centers the camera view to the center-of-mass (centroid) of all layout elements."""
        if state is None:
            state = self.current_state()
        elements = state.get("elements", {}) if isinstance(state, dict) else {}
        if not elements and hasattr(self, "raw_layout") and isinstance(self.raw_layout, dict):
            raw_elems = self.raw_layout.get("elements", [])
            elements = {el["id"]: el for el in raw_elems if isinstance(el, dict) and "id" in el}

        if not elements:
            self.cam_x = 55.0
            self.cam_y = 75.0
            return

        candidates = [
            el for el in elements.values()
            if el.get("elem_type") not in ("packet_scroller", "address_table", "train_scroller", "chat")
        ]
        if not candidates:
            candidates = list(elements.values())

        pts = []
        for el in candidates:
            if el.get("elem_type") == "track":
                p0, p1 = self._get_track_endpoints_logical(el)
                pts.append(p0)
                pts.append(p1)
            else:
                pts.append(self.get_elem_logical_pos(el))

        if not pts:
            pts = [self.get_elem_logical_pos(el) for el in candidates]

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

    def _apply_saved_view(self) -> bool:
        """Applies saved pan and zoom from user preferences if available."""
        v = self.prefs.get_view()
        if v:
            try:
                self.cam_x = float(v["cam_x"])
                self.cam_y = float(v["cam_y"])
                self.zoom = max(self.min_zoom, min(self.max_zoom, float(v["zoom"])))
                self.has_centered_on_layout = True
                return True
            except (ValueError, TypeError):
                pass
        return False

    def save_preferences(self):
        """Persists current user preferences including canvas pan and zoom."""
        self.prefs.set_view(self.cam_x, self.cam_y, self.zoom)
        self.prefs.save()

    def get_panel_base_size(self, sys_key: str, panel: dict) -> Tuple[int, int]:
        def_w = int(panel.get("width") or (580 if sys_key == "packet_scroller" else (460 if sys_key == "chat" else (450 if sys_key == "address_table" else 430))))
        def_h = int(panel.get("height") or (195 if sys_key == "packet_scroller" else (220 if sys_key == "chat" else 150)))
        return self.prefs.get_size(panel["id"], def_w, def_h)

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
        state = self.current_state()
        if not state: return None

        for sys_key in ("packet_scroller", "address_table", "train_scroller", "chat"):
            panel = state.get(sys_key)
            if panel:
                lx, ly = self.get_elem_logical_pos(panel)
                px, py = self.logical_to_screen(lx, ly)
                pw, ph = self.get_panel_base_size(sys_key, panel)
                rect = pygame.Rect(px, py, int(pw * self.zoom), int(ph * self.zoom))
                if rect.collidepoint(sx, sy):
                    return panel["id"]

        elements = state.get("elements", {})
        best_id = None
        best_dist = max(15.0, 18.0 * self.zoom)
        for eid, el in elements.items():
            etype = el.get("elem_type")
            lx, ly = self.get_elem_logical_pos(el)
            ex, ey = self.logical_to_screen(lx, ly)
            if etype in ("ptc_datetime", "date_time", "datetime"):
                ew = int((el.get("width") or 220) * self.zoom)
                eh = int((el.get("height") or 46) * self.zoom)
                if pygame.Rect(ex, ey, ew, eh).collidepoint(sx, sy):
                    return eid
            if etype == "track":
                p0, p1 = self._get_track_endpoints_logical(el)
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                dist = self._dist_to_segment_px(sx, sy, s0[0], s0[1], s1[0], s1[1])
            else:
                dist = math.hypot(sx - ex, sy - ey)
            if dist <= best_dist:
                best_dist = dist
                best_id = eid
        return best_id

    def pick_layout_element_at_screen(self, sx: int, sy: int) -> Optional[str]:
        state = self.current_state()
        if not state: return None
        elements = state.get("elements", {})
        best_id = None
        best_dist = max(15.0, 18.0 * self.zoom)
        for eid, el in elements.items():
            etype = el.get("elem_type")
            lx, ly = self.get_elem_logical_pos(el)
            ex, ey = self.logical_to_screen(lx, ly)
            if etype in ("ptc_datetime", "date_time", "datetime"):
                ew = int((el.get("width") or 220) * self.zoom)
                eh = int((el.get("height") or 46) * self.zoom)
                if pygame.Rect(ex, ey, ew, eh).collidepoint(sx, sy):
                    return eid
            if etype == "track":
                p0, p1 = self._get_track_endpoints_logical(el)
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                dist = self._dist_to_segment_px(sx, sy, s0[0], s0[1], s1[0], s1[1])
            else:
                dist = math.hypot(sx - ex, sy - ey)
            if dist <= best_dist:
                best_dist = dist
                best_id = eid
        return best_id

    def _format_element_tooltip(self, el: dict) -> str:
        if not el or not isinstance(el, dict):
            return ""
        eid = el.get("id", "")
        etype = el.get("elem_type", "")
        lbl = el.get("label", "")
        wiu_id = el.get("wiu_id", None)
        radio_id = el.get("radio_id", None)
        node_id = el.get("node_id", None)
        if node_id is None:
            node_id = el.get("_node_id_", None)
        channel = el.get("channel", None)

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

        avg_rssi = el.get("avg_rssi", el.get("rssi_avg"))
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

    def _draw_element_hover_tooltip(self, state: Optional[dict]):
        if not state or self.show_coverage_map or self.train_assign_active:
            return
        if getattr(self, 'is_panning', False) or getattr(self, 'dragging_id', None) or getattr(self, 'resizing_panel_id', None):
            return

        mx, my = pygame.mouse.get_pos()
        hud_h = 32
        footer_h = 24
        if my < hud_h or my > self.height - footer_h:
            return

        # Don't show if mouse is inside telemetry panels
        for sys_key in ("packet_scroller", "address_table", "train_scroller", "chat"):
            panel = state.get(sys_key)
            if panel:
                lx, ly = self.get_elem_logical_pos(panel)
                px, py = self.logical_to_screen(lx, ly)
                pw, ph = self.get_panel_base_size(sys_key, panel)
                if pygame.Rect(px, py, int(pw * self.zoom), int(ph * self.zoom)).collidepoint(mx, my):
                    return

        # Don't show if mouse is over readonly inspector
        if getattr(self, "inspector_rect", None) and self.readonly_inspector_id and self.inspector_rect.collidepoint(mx, my):
            return

        hov_id = self.pick_layout_element_at_screen(mx, my)
        elements = state.get("elements", {})
        if not hov_id or hov_id not in elements:
            return

        el = elements[hov_id]
        tip_txt = self._format_element_tooltip(el)
        if not tip_txt:
            return

        font = getattr(self, "font_sm_thin", self.font_sm)
        txt_surf = font.render(tip_txt, True, (240, 245, 255))
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

    # -----------------------------------------------------------------
    # Event Handling
    # -----------------------------------------------------------------
    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            elif event.type == pygame.VIDEORESIZE:
                self.width, self.height = event.w, event.h
                self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
            elif event.type == pygame.KEYDOWN:
                if self.local_layout_prompt_active:
                    self._handle_local_layout_prompt_keydown(event)
                    continue
                elif self.banc_coverage_modal:
                    if event.key in (pygame.K_ESCAPE, pygame.K_b):
                        self.close_banc_coverage_modal()
                    else:
                        if hasattr(self.banc_coverage_modal, "handle_key_down"):
                            self.banc_coverage_modal.handle_key_down(event)
                        else:
                            self.banc_coverage_modal.handle_key(event.key, event.unicode)
                    continue
                elif self.show_coverage_map:
                    if event.key in (pygame.K_ESCAPE, pygame.K_m):
                        self.show_coverage_map = False
                        self.status_toast = "Coverage Map closed"
                    elif event.key == pygame.K_f:
                        self.coverage_map_view.trigger_action("fit_all")
                    elif event.key == pygame.K_s:
                        self.coverage_map_view.trigger_action("toggle_layer")
                    elif event.key == pygame.K_d:
                        self.coverage_map_view.trigger_action("toggle_measure")
                    elif event.key == pygame.K_c:
                        self.coverage_map_view.trigger_action("clear_measure")
                    elif event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                        self.coverage_map_view.trigger_action("zoom_in")
                    elif event.key in (pygame.K_MINUS, pygame.K_UNDERSCORE, pygame.K_KP_MINUS):
                        self.coverage_map_view.trigger_action("zoom_out")
                elif self.chat_id_modal_active:
                    self._handle_chat_id_modal_keydown(event)
                elif self.train_assign_active:
                    self._handle_train_assign_keydown(event)
                elif self.renaming_active:
                    self._handle_rename_keydown(event)
                elif self.readonly_inspector_id and self.inspector_active_field:
                    self._handle_inspector_edit_keydown(event)
                elif self.chat_active_field:
                    self._handle_chat_field_keydown(event)
                else:
                    if not self._handle_global_keydown(event):
                        return False
            elif event.type == pygame.MOUSEWHEEL:
                if self.banc_coverage_modal:
                    mx, my = pygame.mouse.get_pos()
                    if hasattr(self.banc_coverage_modal, "handle_scroll"):
                        self.banc_coverage_modal.handle_scroll(event.y, mx, my)
                    elif hasattr(self.banc_coverage_modal, "handle_mouse_wheel"):
                        self.banc_coverage_modal.handle_mouse_wheel(event)
                    continue
                if self.show_coverage_map:
                    vp = pygame.Rect(0, 46, self.width, self.height - 46)
                    self.coverage_map_view.handle_mouse_wheel(event, vp)
                    continue
                mx, my = pygame.mouse.get_pos()
                factor = 1.14 if event.y > 0 else (1.0 / 1.14)
                if self.scroller_panel_rect.width > 0 and self.scroller_panel_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "packet_scroller"
                    step = 3 if not (pygame.key.get_mods() & pygame.KMOD_SHIFT) else 10
                    delta = -step if event.y > 0 else step
                    self.scroller_scroll_offset = max(0, min(max(0, len(self.scroller_history) - 1), self.scroller_scroll_offset + delta))
                elif self.addr_table_panel_rect.width > 0 and self.addr_table_panel_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "address_table"
                    page_step = max(1, getattr(self, "addr_table_avail_rows", 10))
                    step = 3 if not (pygame.key.get_mods() & pygame.KMOD_SHIFT) else page_step
                    delta = -step if event.y > 0 else step
                    max_scroll = max(0, getattr(self, "addr_table_total_items", 0) - page_step)
                    self.wiu_scroll_offset = max(0, min(max_scroll, self.wiu_scroll_offset + delta))
                elif self.chat_panel_rect.width > 0 and self.chat_panel_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "chat"
                    step = 2 if not (pygame.key.get_mods() & pygame.KMOD_SHIFT) else max(1, self.chat_avail_rows - 1)
                    if event.y > 0:
                        self.page_chat_up(step)
                    else:
                        self.page_chat_down(step)
                else:
                    self.zoom_at_cursor((mx, my), factor)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if self.local_layout_prompt_active:
                    if self._handle_local_layout_prompt_mouse_down(event.pos):
                        continue
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.handle_mouse_down(event.pos, event.button)
                    continue
                if self.chat_id_modal_active:
                    self._handle_chat_id_modal_mouse_down(event)
                    continue
                if self.train_assign_active:
                    self._handle_train_assign_mouse_down(event)
                    continue
                self._handle_mouse_down(event)
            elif event.type == pygame.MOUSEBUTTONUP:
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.handle_mouse_up(event.pos, event.button)
                    continue
                self._handle_mouse_up(event)
            elif event.type == pygame.MOUSEMOTION:
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.handle_mouse_move(event.pos)
                    continue
                if self.show_coverage_map:
                    vp = pygame.Rect(0, 46, self.width, self.height - 46)
                    self.coverage_map_view.handle_mouse_motion(event, vp)
                self._handle_mouse_motion(event)

        if not self.renaming_active and not self.train_assign_active and not self.chat_id_modal_active and not self.chat_active_field and not self.banc_coverage_modal and not self.local_layout_prompt_active:
            keys = pygame.key.get_pressed()
            pan_speed = 10.0
            if keys[pygame.K_LEFT]: self.cam_x += pan_speed
            if keys[pygame.K_RIGHT]: self.cam_x -= pan_speed
            if keys[pygame.K_UP]: self.cam_y += pan_speed
            if keys[pygame.K_DOWN]: self.cam_y -= pan_speed

        return True

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
        self._send_engine_command(f"DELETE_TRAIN {train_key}")
        self.status_toast = f"Deleted train '{train_key}' from list"

    def _submit_train_assignment(self):
        if not self.train_assign_trn_key:
            self.train_assign_active = False
            return
        aid = self.train_assign_buffers.get("assigned_id", "").strip()
        aname = self.train_assign_buffers.get("assigned_name", "").strip()
        payload = {
            "train_key": self.train_assign_trn_key,
            "assigned_id": aid if aid else None,
            "assigned_name": aname if aname else None,
        }
        self._send_engine_command(f"ASSIGN_TRAIN_INFO {json.dumps(payload)}")
        self.train_assign_active = False
        self.status_toast = f"Assigned {self.train_assign_trn_key}: ID='{aid}', Name='{aname}'"

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

        # Help string cleanly positioned on its own line above buttons
        hint_txt = "Tab: Switch Field   |   Enter: Save   |   Esc: Cancel"
        self.screen.blit(self.font_sm_thin.render(hint_txt, True, (135, 155, 180)), (box_x + 18, box_y + 158))

        # Bottom Action Buttons row
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
        self._train_assign_cancel_rect = pygame.Rect(box_x + box_w - 95, btn_y, 77, btn_h)
        pygame.draw.rect(self.screen, (65, 45, 45), self._train_assign_cancel_rect, border_radius=4)
        pygame.draw.rect(self.screen, (180, 100, 100), self._train_assign_cancel_rect, 1, border_radius=4)
        cancel_lbl = self.font_sm_bold.render("Cancel", True, (220, 220, 220))
        self.screen.blit(cancel_lbl, (self._train_assign_cancel_rect.x + (77 - cancel_lbl.get_width()) // 2, btn_y + 6))

    def _handle_chat_field_keydown(self, event: pygame.event.Event):
        fld = self.chat_active_field
        if not fld:
            return
        if event.key == pygame.K_ESCAPE:
            self.chat_active_field = None
            self.status_toast = "Chat input unfocused"
            return
        if event.key == pygame.K_PAGEUP:
            self.page_chat_up()
            return
        if event.key == pygame.K_PAGEDOWN:
            self.page_chat_down()
            return
        if event.key == pygame.K_TAB:
            order = ["message", "include_regex", "exclude_regex"]
            idx = (order.index(fld) + 1) % len(order) if fld in order else 0
            self.chat_active_field = order[idx]
            return
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if fld == "message":
                if self.chat_input_buffer.strip():
                    self.send_viewer_chat_message()
            else:
                self.chat_active_field = None
                self.status_toast = f"Chat regex filter updated"
            return
        if event.key == pygame.K_BACKSPACE:
            ctrl = bool(event.mod & pygame.KMOD_CTRL)
            if fld == "message":
                self.chat_input_buffer = "" if ctrl else self.chat_input_buffer[:-1]
            elif fld == "include_regex":
                self.chat_include_regex = "" if ctrl else self.chat_include_regex[:-1]
            elif fld == "exclude_regex":
                self.chat_exclude_regex = "" if ctrl else self.chat_exclude_regex[:-1]
            return
        if (event.mod & pygame.KMOD_CTRL) and event.key in (pygame.K_b, pygame.K_i, pygame.K_u):
            if fld == "message":
                if event.key == pygame.K_b:
                    self.insert_chat_markup("b")
                elif event.key == pygame.K_i:
                    self.insert_chat_markup("i")
                elif event.key == pygame.K_u:
                    self.insert_chat_markup("u")
            return
        if event.unicode and event.unicode.isprintable() and event.unicode not in ("\r", "\n", "\t"):
            if fld == "message":
                if len(self.chat_input_buffer) < mosfet_client.MAX_CHAT_MESSAGE_LEN:
                    self.chat_input_buffer += event.unicode
            elif fld == "include_regex":
                if len(self.chat_include_regex) < MAX_CHAT_REGEX_LEN:
                    self.chat_include_regex += event.unicode
                    self.chat_scroll_offset = 0
            elif fld == "exclude_regex":
                if len(self.chat_exclude_regex) < MAX_CHAT_REGEX_LEN:
                    self.chat_exclude_regex += event.unicode
                    self.chat_scroll_offset = 0

    def _submit_chat_id_modal(self):
        self_id = self.chat_id_modal_buffers.get("self_identity", "").strip()
        self_nm = self.chat_id_modal_buffers.get("self_name", "").strip()
        if self_id or self_nm:
            self.associate_own_identity_and_name(self_id or self.get_viewer_chat_identity(), self_nm)
        cid = self.chat_id_modal_buffers.get("contact_identity", "").strip()
        cnm = self.chat_id_modal_buffers.get("contact_name", "").strip()
        if cid:
            self.associate_incoming_identity_with_name(cid, cnm)
        self.chat_id_modal_active = False

    def _handle_chat_id_modal_keydown(self, event: pygame.event.Event):
        fields = ["self_identity", "self_name", "contact_identity", "contact_name"]
        fld = self.chat_id_modal_active_field
        if event.key == pygame.K_ESCAPE:
            self.chat_id_modal_active = False
            self.status_toast = "Closed Chat Identity & Name Manager"
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._submit_chat_id_modal()
        elif event.key == pygame.K_TAB:
            idx = (fields.index(fld) + 1) % len(fields) if fld in fields else 0
            self.chat_id_modal_active_field = fields[idx]
        elif event.key == pygame.K_BACKSPACE:
            if event.mod & pygame.KMOD_CTRL:
                self.chat_id_modal_buffers[fld] = ""
            else:
                self.chat_id_modal_buffers[fld] = self.chat_id_modal_buffers.get(fld, "")[:-1]
        elif event.unicode and event.unicode.isprintable() and event.unicode not in ("\r", "\n", "\t"):
            if len(self.chat_id_modal_buffers.get(fld, "")) < 64:
                self.chat_id_modal_buffers[fld] = self.chat_id_modal_buffers.get(fld, "") + event.unicode

    def _handle_chat_id_modal_mouse_down(self, event: pygame.event.Event):
        mx, my = event.pos
        for fld, f_rect in getattr(self, "_chat_id_modal_field_rects", {}).items():
            if f_rect.collidepoint(mx, my):
                self.chat_id_modal_active_field = fld
                return
        for c_rect, cid, cname in getattr(self, "_chat_id_modal_contact_rects", []):
            if c_rect.collidepoint(mx, my):
                self.chat_id_modal_buffers["contact_identity"] = cid
                self.chat_id_modal_buffers["contact_name"] = cname
                self.chat_id_modal_active_field = "contact_name"
                return
        if hasattr(self, "_chat_id_modal_save_self_rect") and self._chat_id_modal_save_self_rect.collidepoint(mx, my):
            self.associate_own_identity_and_name(
                self.chat_id_modal_buffers.get("self_identity", ""),
                self.chat_id_modal_buffers.get("self_name", ""),
            )
            return
        if hasattr(self, "_chat_id_modal_save_contact_rect") and self._chat_id_modal_save_contact_rect.collidepoint(mx, my):
            cid = self.chat_id_modal_buffers.get("contact_identity", "").strip()
            cnm = self.chat_id_modal_buffers.get("contact_name", "").strip()
            if cid:
                self.associate_incoming_identity_with_name(cid, cnm)
                self.chat_id_modal_buffers["contact_identity"] = ""
                self.chat_id_modal_buffers["contact_name"] = ""
            return
        if hasattr(self, "_chat_id_modal_save_all_rect") and self._chat_id_modal_save_all_rect.collidepoint(mx, my):
            self._submit_chat_id_modal()
            return
        if hasattr(self, "_chat_id_modal_close_rect") and self._chat_id_modal_close_rect.collidepoint(mx, my):
            self.chat_id_modal_active = False
            return
        if hasattr(self, "_chat_id_modal_rect") and not self._chat_id_modal_rect.collidepoint(mx, my):
            self.chat_id_modal_active = False

    def draw_chat_identity_dialog(self):
        overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 165))
        self.screen.blit(overlay, (0, 0))

        box_w, box_h = 540, 410
        box_x = (self.width - box_w) // 2
        box_y = (self.height - box_h) // 2
        self._chat_id_modal_rect = pygame.Rect(box_x, box_y, box_w, box_h)
        self._chat_id_modal_field_rects = {}
        self._chat_id_modal_contact_rects = []

        pygame.draw.rect(self.screen, (20, 26, 38), self._chat_id_modal_rect, border_radius=8)
        pygame.draw.rect(self.screen, (75, 195, 255), self._chat_id_modal_rect, 2, border_radius=8)

        title = "CHAT IDENTITY & NAME ASSOCIATIONS (LOCAL & PRIVATE)"
        self.screen.blit(self.font_md.render(title, True, (110, 235, 255)), (box_x + 16, box_y + 12))
        sub = "Names are converted to identities before sending and never transmitted in the open."
        self.screen.blit(self.font_sm_thin.render(sub, True, (155, 175, 200)), (box_x + 16, box_y + 32))

        cursor_on = (int(time.time() * 2) % 2) == 0

        # Section 1: Your Identity & Name
        self.screen.blit(self.font_sm_bold.render("1. Your Identity (Banc ID):", True, (200, 220, 245)), (box_x + 16, box_y + 58))
        self.screen.blit(self.font_sm_bold.render("Your Display Name (Local Alias):", True, (200, 220, 245)), (box_x + 230, box_y + 58))

        r_self_id = pygame.Rect(box_x + 16, box_y + 76, 200, 26)
        r_self_nm = pygame.Rect(box_x + 230, box_y + 76, 200, 26)
        self._chat_id_modal_field_rects["self_identity"] = r_self_id
        self._chat_id_modal_field_rects["self_name"] = r_self_nm

        for fk, rct in (("self_identity", r_self_id), ("self_name", r_self_nm)):
            act = (self.chat_id_modal_active_field == fk)
            pygame.draw.rect(self.screen, (12, 16, 24), rct, border_radius=4)
            pygame.draw.rect(self.screen, (80, 230, 255) if act else (65, 85, 110), rct, 2 if act else 1, border_radius=4)
            val = self.chat_id_modal_buffers.get(fk, "") + ("|" if (act and cursor_on) else "")
            self.screen.blit(self.font_sm.render(val[-26:], True, (245, 250, 255)), (rct.x + 6, rct.y + 6))

        self._chat_id_modal_save_self_rect = pygame.Rect(box_x + 440, box_y + 76, 84, 26)
        pygame.draw.rect(self.screen, (30, 105, 135), self._chat_id_modal_save_self_rect, border_radius=4)
        pygame.draw.rect(self.screen, (90, 210, 255), self._chat_id_modal_save_self_rect, 1, border_radius=4)
        self.screen.blit(self.font_sm_bold.render("Set Self", True, (255, 255, 255)), (box_x + 455, box_y + 82))

        # Section 2: Incoming Identity -> Assigned Name
        self.screen.blit(self.font_sm_bold.render("2. Incoming Identity:", True, (200, 220, 245)), (box_x + 16, box_y + 116))
        self.screen.blit(self.font_sm_bold.render("Assigned Friendly Name:", True, (200, 220, 245)), (box_x + 230, box_y + 116))

        r_cid = pygame.Rect(box_x + 16, box_y + 134, 200, 26)
        r_cnm = pygame.Rect(box_x + 230, box_y + 134, 200, 26)
        self._chat_id_modal_field_rects["contact_identity"] = r_cid
        self._chat_id_modal_field_rects["contact_name"] = r_cnm

        for fk, rct in (("contact_identity", r_cid), ("contact_name", r_cnm)):
            act = (self.chat_id_modal_active_field == fk)
            pygame.draw.rect(self.screen, (12, 16, 24), rct, border_radius=4)
            pygame.draw.rect(self.screen, (80, 230, 255) if act else (65, 85, 110), rct, 2 if act else 1, border_radius=4)
            val = self.chat_id_modal_buffers.get(fk, "") + ("|" if (act and cursor_on) else "")
            self.screen.blit(self.font_sm.render(val[-26:], True, (245, 250, 255)), (rct.x + 6, rct.y + 6))

        self._chat_id_modal_save_contact_rect = pygame.Rect(box_x + 440, box_y + 134, 84, 26)
        pygame.draw.rect(self.screen, (35, 120, 70), self._chat_id_modal_save_contact_rect, border_radius=4)
        pygame.draw.rect(self.screen, (85, 230, 140), self._chat_id_modal_save_contact_rect, 1, border_radius=4)
        self.screen.blit(self.font_sm_bold.render("Assign", True, (255, 255, 255)), (box_x + 460, box_y + 140))

        # Section 3: Saved & Recent Identities List (click to edit)
        self.screen.blit(self.font_sm_bold.render("Saved Associations & Recent Senders (Click row to edit):", True, (255, 215, 110)), (box_x + 16, box_y + 174))
        list_rect = pygame.Rect(box_x + 16, box_y + 194, box_w - 32, 156)
        pygame.draw.rect(self.screen, (12, 16, 24), list_rect, border_radius=4)
        pygame.draw.rect(self.screen, (55, 72, 96), list_rect, 1, border_radius=4)

        combined: List[Tuple[str, str]] = []
        seen_ids = set()
        for k, v in sorted(self.chat_identity_store.contacts.items()):
            combined.append((k, v))
            seen_ids.add(k.lower())
        for m in reversed(self.chat_history):
            sid = str(m.get("sender_identity") or "")
            if sid and sid.lower() not in seen_ids:
                seen_ids.add(sid.lower())
                combined.append((sid, self.chat_identity_store.contacts.get(sid, "")))

        for idx, (cid, cname) in enumerate(combined[:7]):
            ry = list_rect.y + 6 + idx * 21
            r_row = pygame.Rect(list_rect.x + 6, ry, list_rect.width - 12, 19)
            self._chat_id_modal_contact_rects.append((r_row, cid, cname))
            pygame.draw.rect(self.screen, (24, 34, 50), r_row, border_radius=3)
            lbl = f"Identity: {cid:<20}  ->  Name: {cname if cname else '(unassigned - click to name)'}"
            col = (115, 245, 175) if cname else (175, 190, 210)
            self.screen.blit(self.font_sm.render(lbl[:68], True, col), (r_row.x + 8, r_row.y + 2))

        btn_y = box_y + box_h - 42
        self._chat_id_modal_save_all_rect = pygame.Rect(box_x + box_w - 210, btn_y, 105, 28)
        pygame.draw.rect(self.screen, (35, 120, 70), self._chat_id_modal_save_all_rect, border_radius=4)
        pygame.draw.rect(self.screen, (90, 230, 140), self._chat_id_modal_save_all_rect, 1, border_radius=4)
        self.screen.blit(self.font_sm_bold.render("Save & Close", True, (255, 255, 255)), (box_x + box_w - 198, btn_y + 7))

        self._chat_id_modal_close_rect = pygame.Rect(box_x + box_w - 96, btn_y, 80, 28)
        pygame.draw.rect(self.screen, (90, 40, 40), self._chat_id_modal_close_rect, border_radius=4)
        pygame.draw.rect(self.screen, (200, 95, 95), self._chat_id_modal_close_rect, 1, border_radius=4)
        self.screen.blit(self.font_sm_bold.render("Close", True, (240, 240, 240)), (box_x + box_w - 74, btn_y + 7))

    def set_wiu_sort(self, sort_by: str, toggle_if_same: bool = True):
        sort_by = sort_by.lower()
        if sort_by not in ("wiu", "rssi", "pkts"):
            sort_by = "wiu"
        if sort_by == self.wiu_sort_by:
            if toggle_if_same:
                self.wiu_sort_desc = not self.wiu_sort_desc
        else:
            self.wiu_sort_by = sort_by
            # For RSSI and Packet count, default to descending (strongest/highest first); for WIU IDs, default to ascending
            self.wiu_sort_desc = (sort_by in ("rssi", "pkts"))
        self.wiu_scroll_offset = 0
        lbl = {"wiu": "WIU ID", "rssi": "Average RSSI", "pkts": "Packet Count"}.get(self.wiu_sort_by, self.wiu_sort_by.upper())
        dir_lbl = "descending" if self.wiu_sort_desc else "ascending"
        self.status_toast = f"WIU Table: Sorted by {lbl} ({dir_lbl})"

    def cycle_wiu_sort(self):
        order = ["wiu", "rssi", "pkts"]
        cur_idx = order.index(self.wiu_sort_by) if self.wiu_sort_by in order else 0
        next_mode = order[(cur_idx + 1) % len(order)]
        self.set_wiu_sort(next_mode, toggle_if_same=False)

    def _handle_rename_keydown(self, event: pygame.event.Event):
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.selected_id:
                self.prefs.set_label(self.selected_id, self.rename_buffer.strip())
                self.save_preferences()
                self.status_toast = f"Saved custom name for {self.selected_id}: '{self.rename_buffer.strip()}'"
            self.renaming_active = False
        elif event.key == pygame.K_ESCAPE:
            self.renaming_active = False
            self.status_toast = "Rename cancelled"
        elif event.key == pygame.K_BACKSPACE:
            if event.mod & pygame.KMOD_CTRL:
                self.rename_buffer = ""
            else:
                self.rename_buffer = self.rename_buffer[:-1]
        else:
            if event.unicode and event.unicode.isprintable():
                self.rename_buffer += event.unicode

    def _open_inspector_for(self, elem_id: str, pos: Tuple[int, int]):
        self.readonly_inspector_id = elem_id
        self.inspector_pos = pos
        self.inspector_active_field = None
        self.inspector_edit_buffers.clear()
        state = self.current_state()
        target = self._lookup_any_element(state, elem_id) if state else None
        if target:
            for k in ("wiu_id", "occ_bit_offset", "cmd_bit_offset", "sig_bit_offset"):
                if k in target:
                    v = target.get(k)
                    self.inspector_edit_buffers[k] = str(v) if v is not None else ""

    def _apply_inspector_temp_overrides(self):
        eid = self.readonly_inspector_id
        if not eid:
            return
        ovr: Dict[str, Any] = {}
        for k, raw in self.inspector_edit_buffers.items():
            s = raw.strip()
            if s == "" or s.lower() == "none":
                ovr[k] = None
            elif s.lstrip("-").isdigit():
                ovr[k] = int(s)
        if "occ_bit_offset" in ovr:
            ovr["has_occ_bit"] = ovr["occ_bit_offset"] is not None
        self.temp_wiu_overrides[eid] = ovr
        state = self.current_state()
        if state:
            self._apply_local_temp_overrides_to_state(state)
        self._send_temp_override_to_engine("SET_TEMP_OVERRIDE", {"id": eid, **ovr})
        self.inspector_active_field = None
        self.status_toast = f"Applied temporary WIU/Bit overrides for '{eid}' (session-only, lost on exit)."

    def _revert_inspector_temp_overrides(self):
        eid = self.readonly_inspector_id
        if not eid:
            return
        self.temp_wiu_overrides.pop(eid, None)
        orig = self.orig_element_bindings.get(eid)
        state = self.current_state()
        if state and orig and eid in state.get("elements", {}):
            for k, v in orig.items():
                state["elements"][eid][k] = v
                if k in self.inspector_edit_buffers:
                    self.inspector_edit_buffers[k] = str(v) if v is not None else ""
        self._send_temp_override_to_engine("REVERT_TEMP_OVERRIDE", {"id": eid})
        self.inspector_active_field = None
        self.status_toast = f"Reverted '{eid}' WIU ID & bit offsets to original values."

    def _handle_inspector_edit_keydown(self, event: pygame.event.Event):
        fld = self.inspector_active_field
        if not fld:
            return
        if event.key == pygame.K_ESCAPE:
            self.inspector_active_field = None
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._apply_inspector_temp_overrides()
        elif event.key == pygame.K_TAB:
            keys = list(self.inspector_edit_rects.keys())
            if keys and fld in keys:
                idx = (keys.index(fld) + 1) % len(keys)
                self.inspector_active_field = keys[idx]
        elif event.key == pygame.K_BACKSPACE:
            if event.mod & pygame.KMOD_CTRL:
                self.inspector_edit_buffers[fld] = ""
            else:
                self.inspector_edit_buffers[fld] = self.inspector_edit_buffers.get(fld, "")[:-1]
        elif event.unicode and (event.unicode.isdigit() or event.unicode in "-"):
            self.inspector_edit_buffers[fld] = self.inspector_edit_buffers.get(fld, "") + event.unicode

    def _handle_global_keydown(self, event: pygame.event.Event) -> bool:
        if event.key == pygame.K_b:
            if self.banc_coverage_modal:
                self.close_banc_coverage_modal()
            else:
                self.open_banc_coverage_modal()
            return True
        if event.key == pygame.K_o:
            self.toggle_local_layout_prompt()
            return True
        if event.key == pygame.K_m:
            self.show_coverage_map = not self.show_coverage_map
            if self.show_coverage_map:
                self.status_toast = "Coverage Map opened: Pan/Drag, Scroll to Zoom, 'F' Fit, 'S' Satellite/Streets, Esc to close"
            else:
                self.status_toast = "Coverage Map closed"
            return True
        if event.key == pygame.K_ESCAPE:
            if self.show_coverage_map:
                self.show_coverage_map = False
                self.status_toast = "Coverage Map closed"
                return True
            closed_any = False
            if self.readonly_inspector_id:
                self.readonly_inspector_id = None
                self.inspector_active_field = None
                self.inspector_dragging = False
                closed_any = True
            if self.map_selected_id:
                self.map_selected_id = None
                closed_any = True
            if self.scroller_sel_start is not None or self.scroller_sel_end is not None:
                self.scroller_sel_start = None
                self.scroller_sel_end = None
                closed_any = True
            if closed_any:
                self.status_toast = "Closed details / map / selection"
                return True
        if (event.mod & pygame.KMOD_CTRL) and event.key == pygame.K_c:
            self.copy_selected_scroller_packets()
            return True
        if event.key == pygame.K_HOME:
            mx, my = pygame.mouse.get_pos()
            is_wiu = (self.addr_table_panel_rect.width > 0 and (
                self.addr_table_panel_rect.collidepoint(mx, my) or
                self.selected_id == "SYS_ADDRESS_TABLE" or
                (self.last_scrolled_panel == "address_table" and not self.scroller_panel_rect.collidepoint(mx, my))
            ))
            if is_wiu:
                self.wiu_scroll_offset = 0
                self.status_toast = "WIU Table: Jumped to top"
            else:
                self.scroller_scroll_offset = 0
                self.status_toast = "Packet Scroller: Jumped to top (live)"
            return True
        if event.key == pygame.K_PAGEUP:
            mx, my = pygame.mouse.get_pos()
            is_chat = (self.chat_panel_rect.width > 0 and (
                self.chat_panel_rect.collidepoint(mx, my) or
                self.selected_id == "SYS_CHAT" or
                self.last_scrolled_panel == "chat"
            ))
            is_wiu = (not is_chat and self.addr_table_panel_rect.width > 0 and (
                self.addr_table_panel_rect.collidepoint(mx, my) or
                self.selected_id == "SYS_ADDRESS_TABLE" or
                (self.last_scrolled_panel == "address_table" and not self.scroller_panel_rect.collidepoint(mx, my))
            ))
            if is_chat:
                self.page_chat_up()
            elif is_wiu:
                page_step = max(1, getattr(self, "addr_table_avail_rows", 10))
                self.wiu_scroll_offset = max(0, self.wiu_scroll_offset - page_step)
                self.status_toast = f"WIU Table: Paged up (offset {self.wiu_scroll_offset})"
            else:
                self.scroller_scroll_offset = min(max(0, len(self.scroller_history) - 1), self.scroller_scroll_offset + 10)
                self.status_toast = f"Packet Scroller: Scrolled back (+{self.scroller_scroll_offset})"
            return True
        if event.key == pygame.K_PAGEDOWN:
            mx, my = pygame.mouse.get_pos()
            is_chat = (self.chat_panel_rect.width > 0 and (
                self.chat_panel_rect.collidepoint(mx, my) or
                self.selected_id == "SYS_CHAT" or
                self.last_scrolled_panel == "chat"
            ))
            is_wiu = (not is_chat and self.addr_table_panel_rect.width > 0 and (
                self.addr_table_panel_rect.collidepoint(mx, my) or
                self.selected_id == "SYS_ADDRESS_TABLE" or
                (self.last_scrolled_panel == "address_table" and not self.scroller_panel_rect.collidepoint(mx, my))
            ))
            if is_chat:
                self.page_chat_down()
            elif is_wiu:
                page_step = max(1, getattr(self, "addr_table_avail_rows", 10))
                max_scroll = max(0, getattr(self, "addr_table_total_items", 0) - page_step)
                self.wiu_scroll_offset = min(max_scroll, self.wiu_scroll_offset + page_step)
                self.status_toast = f"WIU Table: Paged down (offset {self.wiu_scroll_offset})"
            else:
                self.scroller_scroll_offset = max(0, self.scroller_scroll_offset - 10)
                self.status_toast = f"Packet Scroller: Scrolled forward (+{self.scroller_scroll_offset})"
            return True
        if event.key == pygame.K_ESCAPE and self.readonly_inspector_id:
            self.readonly_inspector_id = None
            self.inspector_dragging = False
            self.status_toast = "Closed read-only element inspector"
            return True
        if event.key == pygame.K_q: return False
        elif event.key == pygame.K_e:
            self.edit_mode = not self.edit_mode
            if self.edit_mode:
                self.readonly_inspector_id = None
            self.status_toast = f"Edit Mode {'ENABLED' if self.edit_mode else 'DISABLED'}"
        elif event.key == pygame.K_SPACE:
            self._toggle_pause()
        elif event.key in (pygame.K_LEFTBRACKET, pygame.K_COMMA):
            self._step_history(-1)
        elif event.key in (pygame.K_RIGHTBRACKET, pygame.K_PERIOD):
            self._step_history(1)
        elif event.key == pygame.K_l:
            if event.mod & pygame.KMOD_SHIFT:
                resp = self._send_engine_command("TOGGLE_RAW_LOGGING")
                if resp:
                    is_on = resp.get("raw_packet_logging", False)
                    st = "ENABLED" if is_on else "DISABLED"
                    self.status_toast = f"Engine Raw Packet Logging: {st}"
                else:
                    self.status_toast = "Could not communicate with Engine layout server"
            else:
                self._resume_live()
        elif event.key == pygame.K_r:
            self.reset_camera()
        elif event.key == pygame.K_s:
            self.save_preferences()
            self.status_toast = f"Saved user preferences to '{self.prefs.filepath}'"
        elif event.key == pygame.K_x:
            self.prefs.clear_all()
            self.status_toast = "Cleared local position/label overrides"
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.selected_id:
            state = self.current_state()
            target = self._lookup_any_element(state, self.selected_id) if state else None
            if target:
                lx, ly = self.get_elem_logical_pos(target)
                sx, sy = self.logical_to_screen(lx, ly)
                self._open_inspector_for(self.selected_id, (int(sx) + 24, int(sy) + 12))
                self.status_toast = f"Opened Details Inspector for '{self.selected_id}' (Esc or click outside to close)"
        elif event.key == pygame.K_w and not self.train_assign_active and not self.renaming_active:
            self.cycle_wiu_sort()
            return True
        elif event.key == pygame.K_n and self.edit_mode and self.selected_id:
            self._begin_rename(self.selected_id)
        return True

    def _begin_rename(self, elem_id: str):
        state = self.current_state()
        if not state: return
        el = state.get("elements", {}).get(elem_id)
        if not el: return
        self.renaming_active = True
        self.rename_buffer = self.get_elem_label(el)
        self.status_toast = f"Renaming {elem_id}: Type new name, Enter to save, Ctrl+BS to clear"

    def _handle_mouse_down(self, event: pygame.event.Event):
        mx, my = event.pos
        if self.show_coverage_map and my >= 46:
            consumed = self.coverage_map_view.handle_mouse_down(event, 46)
            if getattr(self.coverage_map_view, "request_close", False):
                self.show_coverage_map = False
                self.coverage_map_view.request_close = False
                self.status_toast = "Coverage Map closed"
            if consumed:
                return
        if event.button == 1:
            if self.readonly_inspector_id and self.inspector_rect.width > 0:
                if self.inspector_close_rect.collidepoint(mx, my):
                    self.readonly_inspector_id = None
                    self.inspector_active_field = None
                    self.inspector_dragging = False
                    return
                if self.inspector_apply_ovr_rect.width > 0 and self.inspector_apply_ovr_rect.collidepoint(mx, my):
                    self._apply_inspector_temp_overrides()
                    return
                if self.inspector_revert_ovr_rect.width > 0 and self.inspector_revert_ovr_rect.collidepoint(mx, my):
                    self._revert_inspector_temp_overrides()
                    return
                if self.inspector_copy_hex_rect.width > 0 and self.inspector_copy_hex_rect.collidepoint(mx, my):
                    raw_hex = getattr(self, "inspector_last_raw_hex", "")
                    if raw_hex:
                        self._copy_text_to_clipboard(raw_hex)
                        self.status_toast = f"Copied raw hex ({len(raw_hex)} chars) to clipboard!"
                    else:
                        self.status_toast = "No raw packet hex available to copy."
                    return
                for fld, f_rect in self.inspector_edit_rects.items():
                    if f_rect.collidepoint(mx, my):
                        self.inspector_active_field = fld
                        return
                if self.inspector_rect.collidepoint(mx, my):
                    self.inspector_active_field = None
                    self.inspector_dragging = True
                    self.inspector_drag_offset = (self.inspector_rect.x - mx, self.inspector_rect.y - my)
                    return
                else:
                    self.readonly_inspector_id = None
                    self.inspector_active_field = None
                    self.inspector_dragging = False

            if self.map_selected_id and self.map_inset_rect.width > 0 and not self.map_inset_rect.collidepoint(mx, my):
                self.map_selected_id = None

            for b_key, b_rect in self.addr_table_btn_rects.items():
                if b_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "address_table"
                    page_step = max(1, getattr(self, "addr_table_avail_rows", 10))
                    max_scroll = max(0, getattr(self, "addr_table_total_items", 0) - page_step)
                    if b_key == "top":
                        self.wiu_scroll_offset = 0
                        self.status_toast = "WIU Table: Jumped to top"
                    elif b_key == "pgup":
                        self.wiu_scroll_offset = max(0, self.wiu_scroll_offset - page_step)
                        self.status_toast = f"WIU Table: Paged up (offset {self.wiu_scroll_offset})"
                    elif b_key == "pgdn":
                        self.wiu_scroll_offset = min(max_scroll, self.wiu_scroll_offset + page_step)
                        self.status_toast = f"WIU Table: Paged down (offset {self.wiu_scroll_offset})"
                    elif b_key.startswith("sort_"):
                        target_mode = b_key[5:]
                        if target_mode == "cycle":
                            self.cycle_wiu_sort()
                        else:
                            self.set_wiu_sort(target_mode, toggle_if_same=True)
                    return

            if self.addr_table_panel_rect.width > 0 and self.addr_table_panel_rect.collidepoint(mx, my):
                self.last_scrolled_panel = "address_table"
            elif self.scroller_panel_rect.width > 0 and self.scroller_panel_rect.collidepoint(mx, my):
                self.last_scrolled_panel = "packet_scroller"

            for b_key, b_rect in self.scroller_btn_rects.items():
                if b_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "packet_scroller"
                    if b_key == "top":
                        self.scroller_scroll_offset = 0
                        self.status_toast = "Packet Scroller: Jumped to top (live)"
                    elif b_key == "pgup":
                        self.scroller_scroll_offset = min(max(0, len(self.scroller_history) - 1), self.scroller_scroll_offset + 10)
                        self.status_toast = f"Packet Scroller: Scrolled back (+{self.scroller_scroll_offset})"
                    elif b_key == "pgdn":
                        self.scroller_scroll_offset = max(0, self.scroller_scroll_offset - 10)
                        self.status_toast = f"Packet Scroller: Scrolled forward (+{self.scroller_scroll_offset})"
                    elif b_key == "copy":
                        self.copy_selected_scroller_packets()
                    return

            # Chat panel buttons & interactive fields
            if not self.edit_mode and self.chat_panel_rect.width > 0:
                for b_key, b_rect in self.chat_btn_rects.items():
                    if b_rect.collidepoint(mx, my):
                        self.last_scrolled_panel = "chat"
                        if b_key == "mute":
                            self.chat_mute_all = not self.chat_mute_all
                            self.status_toast = f"Chat: Mute All {'ENABLED' if self.chat_mute_all else 'DISABLED'}"
                        elif b_key == "pgup":
                            self.page_chat_up()
                        elif b_key == "pgdn":
                            self.page_chat_down()
                        elif b_key == "bottom":
                            self.chat_scroll_offset = 0
                            self.status_toast = "Chat: Jumped to latest messages"
                        elif b_key == "clear":
                            self.clear_viewer_chat_messages()
                        elif b_key == "id_names":
                            self.open_chat_identity_modal()
                        elif b_key == "send":
                            self.send_viewer_chat_message()
                        elif b_key == "input_message":
                            self.chat_active_field = "message"
                        elif b_key == "input_include_regex":
                            self.chat_active_field = "include_regex"
                            self.status_toast = "Chat: Editing Include RegEx filter (Enter/Esc when done)"
                        elif b_key == "input_exclude_regex":
                            self.chat_active_field = "exclude_regex"
                            self.status_toast = "Chat: Editing Exclude RegEx filter (Enter/Esc when done)"
                        elif b_key == "clear_include_regex":
                            self.chat_include_regex = ""
                        elif b_key == "clear_exclude_regex":
                            self.chat_exclude_regex = ""
                        elif b_key in ("fmt_b", "fmt_i", "fmt_u", "fmt_color"):
                            self.insert_chat_markup(b_key[4:])
                        return
                for s_rect, sender_ident in self.chat_sender_rects:
                    if s_rect.collidepoint(mx, my):
                        self.open_chat_identity_modal(prefill_contact_id=sender_ident)
                        return
                if self.chat_panel_rect.collidepoint(mx, my):
                    self.last_scrolled_panel = "chat"
                else:
                    if self.chat_active_field is not None:
                        self.chat_active_field = None
            elif self.chat_active_field is not None:
                self.chat_active_field = None

            for del_rect, tkey in getattr(self, "_train_scroller_del_rects", []):
                if del_rect.collidepoint(mx, my):
                    self.delete_train(tkey)
                    return

            for row_rect, trn in getattr(self, "_train_scroller_row_rects", []):
                if row_rect.collidepoint(mx, my):
                    self._open_train_assignment(trn)
                    return

            if not self.edit_mode:
                for abs_idx, l_rect in self.scroller_line_rects:
                    if l_rect.collidepoint(mx, my):
                        if (pygame.key.get_mods() & pygame.KMOD_SHIFT) and self.scroller_sel_start is not None:
                            self.scroller_sel_end = abs_idx
                        else:
                            self.scroller_sel_start = abs_idx
                            self.scroller_sel_end = abs_idx
                        self.scroller_selecting = True
                        self.status_toast = "Selected packet line(s) — drag to select range, Ctrl+C or [Copy] to copy"
                        return
        mx, my = event.pos
        if event.button == 1:
            if self.readonly_inspector_id and self.inspector_rect.width > 0:
                if self.inspector_close_rect.collidepoint(mx, my):
                    self.readonly_inspector_id = None
                    self.inspector_dragging = False
                    return
                if self.inspector_rect.collidepoint(mx, my):
                    self.inspector_dragging = True
                    self.inspector_drag_offset = (self.inspector_rect.x - mx, self.inspector_rect.y - my)
                    return

            for btn_name, rect in self.hud_buttons.items():
                if rect.collidepoint(mx, my):
                    if btn_name == "prev": self._step_history(-1)
                    elif btn_name == "pause": self._toggle_pause()
                    elif btn_name == "next": self._step_history(1)
                    elif btn_name == "live": self._resume_live()
                    elif btn_name == "edit":
                        self.edit_mode = not self.edit_mode
                        if self.edit_mode:
                            self.readonly_inspector_id = None
                        self.status_toast = f"Edit Mode {'ON' if self.edit_mode else 'OFF'}"
                    elif btn_name == "save":
                        self.save_preferences()
                        self.status_toast = f"Saved overrides to '{self.prefs.filepath}'"
                    elif btn_name == "toggle_labels":
                        self.show_labels = not self.show_labels
                        self.status_toast = f"Labels {'Shown' if self.show_labels else 'Hidden'}"
                    elif btn_name == "toggle_inferred":
                        self.show_inferred_trains = not self.show_inferred_trains
                        self.status_toast = f"Inferred Train Badges {'Shown' if self.show_inferred_trains else 'Hidden'}"
                    elif btn_name == "toggle_raw_log":
                        resp = self._send_engine_command("TOGGLE_RAW_LOGGING")
                        if resp:
                            is_on = resp.get("raw_packet_logging", False)
                            st = "ENABLED" if is_on else "DISABLED"
                            self.status_toast = f"Engine Raw Packet Logging: {st}"
                        else:
                            self.status_toast = "Could not communicate with Engine layout server"
                    elif btn_name == "coverage_map":
                        self.show_coverage_map = not self.show_coverage_map
                        if self.show_coverage_map:
                            self.status_toast = "Coverage Map opened: Pan/Drag, Scroll to Zoom, 'F' Fit, 'S' Satellite/Streets, Esc to close"
                        else:
                            self.status_toast = "Coverage Map closed"
                    elif btn_name == "banc_layouts":
                        if self.banc_coverage_modal:
                            self.close_banc_coverage_modal()
                        else:
                            self.open_banc_coverage_modal()
                    elif btn_name == "local_layout":
                        self.toggle_local_layout_prompt()
                    return

            # Check informational panel bottom-right resize handles
            state = self.current_state()
            if state:
                for pid, r_rect in self.panel_resize_rects.items():
                    if r_rect.collidepoint(mx, my):
                        target_panel = self._lookup_any_element(state, pid)
                        if target_panel:
                            sys_key = target_panel.get("elem_type", "packet_scroller")
                            bw, bh = self.get_panel_base_size(sys_key, target_panel)
                            self.resizing_panel_id = pid
                            self.resize_start_mouse = (mx, my)
                            self.resize_start_size = (bw, bh)
                            self.selected_id = pid
                            self.status_toast = f"Resizing panel {pid} (drag bottom-right corner)..."
                            return

        if event.button == 3:
            hit_id = self.pick_element_at_screen(mx, my)
            if hit_id:
                self.selected_id = hit_id
                self.map_selected_id = hit_id
                state = self.current_state()
                sel_el = self._lookup_any_element(state, hit_id) if state else None
                if sel_el and sel_el.get("gps"):
                    self.status_toast = f"Showing GPS Map for '{hit_id}' (Esc or click elsewhere to close)"
                else:
                    self.status_toast = f"Element '{hit_id}' has no GPS coordinates yet"
                return
            self.map_selected_id = None
            self.readonly_inspector_id = None
            self.inspector_active_field = None
            self.is_panning = True
            self.pan_start_mouse = (mx, my)
            self.pan_start_cam = (self.cam_x, self.cam_y)
            return
        if event.button == 2:
            self.is_panning = True
            self.pan_start_mouse = (mx, my)
            self.pan_start_cam = (self.cam_x, self.cam_y)
            return

        if event.button == 1:
            hit_id = self.pick_element_at_screen(mx, my)
            now = time.time()
            if hit_id:
                if hit_id == self.last_click_id and (now - self.last_click_time) < 0.35:
                    self.selected_id = hit_id
                    self.last_click_time = 0.0
                    if self.edit_mode:
                        self._begin_rename(hit_id)
                    else:
                        self._open_inspector_for(hit_id, (mx + 24, my + 12))
                        state = self.current_state()
                        el = self._lookup_any_element(state, hit_id) if state else None
                        if el and el.get("elem_type") == "base":
                            self.status_toast = f"Base Station '{hit_id}': Displaying 6 Transmitted Channels (Esc to close)"
                        else:
                            self.status_toast = f"Opened Details Inspector for '{hit_id}' (Esc or click outside to close)"
                        return
                        self.inspector_pos = (mx + 24, my + 12)
                        self.status_toast = f"Opened Read-Only Inspector for '{hit_id}' (Esc to close)"
                    return
                self.selected_id = hit_id
                self.last_click_id = hit_id
                self.last_click_time = now
                if self.edit_mode:
                    state = self.current_state()
                    target = self._lookup_any_element(state, hit_id)
                    if target:
                        lx, ly = self.get_elem_logical_pos(target)
                        mlx, mly = self.screen_to_logical(mx, my)
                        self.dragging_id = hit_id
                        self.drag_offset_logical = (lx - mlx, ly - mly)
            else:
                self.selected_id = None
                self.map_selected_id = None
                self.readonly_inspector_id = None
                self.inspector_active_field = None
                self.renaming_active = False
                self.is_panning = True
                self.pan_start_mouse = (mx, my)
                self.pan_start_cam = (self.cam_x, self.cam_y)

    def _handle_mouse_up(self, event: pygame.event.Event):
        if self.show_coverage_map:
            self.coverage_map_view.handle_mouse_up(event)
        if event.button in (1, 2, 3):
            if self.scroller_selecting:
                self.scroller_selecting = False
            if self.inspector_dragging:
                self.inspector_dragging = False
            if self.resizing_panel_id:
                self.save_preferences()
                self.status_toast = f"Saved panel size for {self.resizing_panel_id} in '{self.prefs.filepath}'"
                self.resizing_panel_id = None
            if self.dragging_id:
                self.save_preferences()
                self.status_toast = f"Updated position for {self.dragging_id} in '{self.prefs.filepath}'"
            self.dragging_id = None
            self.is_panning = False

    def _handle_mouse_motion(self, event: pygame.event.Event):
        mx, my = event.pos
        if self.scroller_selecting:
            for abs_idx, l_rect in self.scroller_line_rects:
                if l_rect.collidepoint(mx, my):
                    self.scroller_sel_end = abs_idx
                    break
            return
        if self.inspector_dragging and self.readonly_inspector_id:
            self.inspector_pos = (mx + self.inspector_drag_offset[0], my + self.inspector_drag_offset[1])
            return
        if self.resizing_panel_id:
            dx = (mx - self.resize_start_mouse[0]) / max(0.25, self.zoom)
            dy = (my - self.resize_start_mouse[1]) / max(0.25, self.zoom)
            new_w = max(260, int(round(self.resize_start_size[0] + dx)))
            new_h = max(90, int(round(self.resize_start_size[1] + dy)))
            self.prefs.set_size(self.resizing_panel_id, new_w, new_h)
            return
        if self.dragging_id and self.edit_mode:
            mlx, mly = self.screen_to_logical(mx, my)
            new_x = round((mlx + self.drag_offset_logical[0]) * 2.0) / 2.0
            new_y = round((mly + self.drag_offset_logical[1]) * 2.0) / 2.0
            self.prefs.set_position(self.dragging_id, new_x, new_y)
        elif self.is_panning:
            dx = mx - self.pan_start_mouse[0]
            dy = my - self.pan_start_mouse[1]
            self.cam_x = self.pan_start_cam[0] + dx
            self.cam_y = self.pan_start_cam[1] + dy

    @staticmethod
    def _lookup_any_element(state: Optional[dict], elem_id: str) -> Optional[dict]:
        if not state: return None
        if elem_id in state.get("elements", {}): return state["elements"][elem_id]
        for sys_key in ("packet_scroller", "address_table", "train_scroller", "chat"):
            panel = state.get(sys_key)
            if panel and panel.get("id") == elem_id:
                return panel
        return None

    def _track_color(self, el: dict, flash_on: bool, ref_now: float) -> Tuple[int, int, int]:
        if el.get("collision_warning"):
            return COLOR_TRACK_RED if flash_on else COLOR_TRACK_FLASH_DARK_RED

        is_stale, age = self.get_element_inactivity(el, ref_now)
        last_ts = float(el.get("last_packet_ts", 0.0) or 0.0)
        fade_ratio = 0.0
        if last_ts > 0.0 and age >= 3.5:
            fade_ratio = min(1.0, max(0.0, (age - 3.5) / max(1.0, self.stale_timeout_sec - 3.5)))

        if el.get("occupied") or el.get("hw_occupied") or el.get("inferred_occupied"):
            active_col = COLOR_TRACK_RED
            dark_col = (120, 28, 28)
            if el.get("is_dimmed"):
                return dark_col
            if last_ts <= 0.0 or age < 3.5:
                return active_col
            if is_stale or age >= self.stale_timeout_sec:
                return dark_col
            return (
                int(round(active_col[0] * (1.0 - fade_ratio) + dark_col[0] * fade_ratio)),
                int(round(active_col[1] * (1.0 - fade_ratio) + dark_col[1] * fade_ratio)),
                int(round(active_col[2] * (1.0 - fade_ratio) + dark_col[2] * fade_ratio)),
            )

        if el.get("green_path"):
            active_col = COLOR_TRACK_GREEN
            dark_col = (18, 110, 48)
            if last_ts <= 0.0 or age < 3.5:
                return active_col
            if is_stale or age >= self.stale_timeout_sec:
                return dark_col
            return (
                int(round(active_col[0] * (1.0 - fade_ratio) + dark_col[0] * fade_ratio)),
                int(round(active_col[1] * (1.0 - fade_ratio) + dark_col[1] * fade_ratio)),
                int(round(active_col[2] * (1.0 - fade_ratio) + dark_col[2] * fade_ratio)),
            )

        if last_ts <= 0.0 or is_stale or age >= self.stale_timeout_sec:
            return COLOR_TRACK_STALE_GRAY
        if age < 3.5:
            return COLOR_TRACK_WHITE
        active_col = COLOR_TRACK_WHITE
        dark_col = COLOR_TRACK_STALE_GRAY
        return (
            int(round(active_col[0] * (1.0 - fade_ratio) + dark_col[0] * fade_ratio)),
            int(round(active_col[1] * (1.0 - fade_ratio) + dark_col[1] * fade_ratio)),
            int(round(active_col[2] * (1.0 - fade_ratio) + dark_col[2] * fade_ratio)),
        )

    # -----------------------------------------------------------------
    # Multi-Head Signal Grouping, Orientation & Directional Chevrons
    # -----------------------------------------------------------------
    def _get_track_endpoints_logical(self, el: dict) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        cx, cy = self.get_elem_logical_pos(el)
        length_mi = float(el.get("length_miles", 0.5) or 0.5)
        orient = el.get("orientation", "E-W")
        half_span = max(0.4, (length_mi * 4.0) / 2.0)
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
    def _compute_45deg_polyline(p1: Tuple[float, float], p2: Tuple[float, float]) -> List[Tuple[float, float]]:
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
        pygame.draw.polygon(self.screen, (10, 40, 20), pts, 1)

    def _draw_track_bar_arrows(self, p1: Tuple[int, int], p2: Tuple[int, int], direction: str):
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length < 18.0 * self.zoom or not direction:
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

        sz = max(3.8, 5.0 * self.zoom)
        if length >= 90.0 * self.zoom:
            t_vals = (0.18, 0.34, 0.66, 0.82)
        elif length >= 38.0 * self.zoom:
            t_vals = (0.28, 0.72)
        else:
            t_vals = (0.28,)

        for t in t_vals:
            cx = p1[0] + ux * length * t
            cy = p1[1] + uy * length * t
            self._draw_green_path_arrowhead(cx, cy, vx, vy, sz)

    def _draw_link_green_arrows(self, p1: Tuple[int, int], p2: Tuple[int, int], direction: str):
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length < 18.0 * self.zoom or not direction:
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

        sz = max(3.8, 5.0 * self.zoom)
        if length >= 50.0 * self.zoom:
            n = max(2, int(round(length / (42.0 * self.zoom))))
            t_vals = [i / (n + 1) for i in range(1, n + 1)]
        else:
            t_vals = [0.5]

        for t in t_vals:
            cx = p1[0] + ux * length * t
            cy = p1[1] + uy * length * t
            self._draw_green_path_arrowhead(cx, cy, vx, vy, sz)

    def _draw_turnout_diamond_arrow(self, sx: int, sy: int, sz: float, direction: str):
        if not direction:
            return
        dir_vec = DIR_SCREEN_VECTORS.get(direction.upper(), (1.0, 0.0))
        self._draw_green_path_arrowhead(sx, sy, dir_vec[0], dir_vec[1], sz=sz * 0.65)

    def _draw_octilinear_link(
        self,
        p1_log: Tuple[float, float],
        p2_log: Tuple[float, float],
        col1: Tuple[int, int, int],
        col2: Tuple[int, int, int],
        line_w: int,
        green_dir: Optional[str] = None,
    ):
        poly_log = self._compute_45deg_polyline(p1_log, p2_log)
        scr_pts = [self.logical_to_screen(px, py) for px, py in poly_log]
        if len(scr_pts) == 2:
            mx, my = (scr_pts[0][0] + scr_pts[1][0]) // 2, (scr_pts[0][1] + scr_pts[1][1]) // 2
            pygame.draw.line(self.screen, col1, scr_pts[0], (mx, my), line_w)
            pygame.draw.line(self.screen, col2, (mx, my), scr_pts[1], line_w)
            pygame.draw.circle(self.screen, col1, (mx, my), max(2, line_w // 2))
        elif len(scr_pts) >= 3:
            pygame.draw.line(self.screen, col1, scr_pts[0], scr_pts[1], line_w)
            pygame.draw.circle(self.screen, col1, scr_pts[1], max(2, line_w // 2))
            pygame.draw.line(self.screen, col2, scr_pts[1], scr_pts[2], line_w)

        if green_dir:
            for i in range(len(scr_pts) - 1):
                self._draw_link_green_arrows(scr_pts[i], scr_pts[i + 1], green_dir)

    def _draw_section_separators(self, track_endpoints: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]]):
        """Draws dim dotted lines perpendicular to each track at its ends to show track section boundaries."""
        col = (92, 104, 124)
        half = max(7, int(10 * self.zoom))
        dot_r = max(1, int(round(1.3 * self.zoom)))
        n_dots = 6
        for p0, p1 in track_endpoints.values():
            s0 = self.logical_to_screen(p0[0], p0[1])
            s1 = self.logical_to_screen(p1[0], p1[1])
            dx, dy = s1[0] - s0[0], s1[1] - s0[1]
            ln = math.hypot(dx, dy)
            if ln < 1.0:
                continue
            nx, ny = -dy / ln, dx / ln
            for sx, sy in (s0, s1):
                for i in range(n_dots):
                    t = -half + (2.0 * half) * i / (n_dots - 1)
                    pygame.draw.circle(self.screen, col, (int(sx + nx * t), int(sy + ny * t)), dot_r)

    def draw_layout_state(self, state: dict, flash_on: bool, ref_now: Optional[float] = None):
        if ref_now is None:
            ref_now = self.get_reference_now(state)
        elements = state.get("elements", {})
        drawn_pairs = set()
        line_w = max(3, int(5 * self.zoom))

        track_endpoints: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {}
        for eid, el in elements.items():
            if el.get("elem_type") == "track":
                track_endpoints[eid] = self._get_track_endpoints_logical(el)

        # 1. Draw Track Bars & Topological Links
        for eid, el in elements.items():
            etype = el.get("elem_type")
            if etype not in ("track", "turnout"):
                continue
            col1 = self._track_color(el, flash_on, ref_now)

            if etype == "track":
                p0, p1 = track_endpoints[eid]
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                pygame.draw.line(self.screen, col1, s0, s1, line_w)
                if el.get("green_path") and el.get("green_path_dir"):
                    self._draw_track_bar_arrows(s0, s1, el["green_path_dir"])

                nbr_ids = []
                if isinstance(el.get("neighbors"), dict):
                    nbr_ids = [nid for d_list in el.get("neighbors", {}).values() for nid in d_list]
                for nid in nbr_ids:
                    pair_key = tuple(sorted((eid, nid)))
                    if pair_key in drawn_pairs or nid not in elements:
                        continue
                    nbr = elements[nid]
                    if nbr.get("elem_type") == "turnout":
                        continue
                    drawn_pairs.add(pair_key)
                    col2 = self._track_color(nbr, flash_on, ref_now)
                    if nid in track_endpoints:
                        np0, np1 = track_endpoints[nid]
                        best_pair = min(
                            ((a, b) for a in (p0, p1) for b in (np0, np1)),
                            key=lambda ab: math.hypot(ab[0][0] - ab[1][0], ab[0][1] - ab[1][1]),
                        )
                        g_dir = (el.get("green_path_dir") or nbr.get("green_path_dir")) if (el.get("green_path") and nbr.get("green_path")) else None
                        self._draw_octilinear_link(best_pair[0], best_pair[1], col1, col2, line_w, green_dir=g_dir)

            elif etype == "turnout":
                t_log = self.get_elem_logical_pos(el)
                common_id = el.get("common_neighbor", "")
                norm_id = el.get("normal_neighbor", "")
                rev_id = el.get("reverse_neighbor", "")
                active_id = norm_id if el.get("state", 0) == 0 else rev_id
                inactive_id = rev_id if el.get("state", 0) == 0 else norm_id

                if inactive_id and inactive_id in elements:
                    inbr = elements[inactive_id]
                    target_pt = self.get_elem_logical_pos(inbr)
                    if inactive_id in track_endpoints:
                        ip0, ip1 = track_endpoints[inactive_id]
                        target_pt = min((ip0, ip1), key=lambda p: math.hypot(p[0] - t_log[0], p[1] - t_log[1]))
                    self._draw_octilinear_link(t_log, target_pt, COLOR_TURNOUT_TIE, COLOR_TURNOUT_TIE, max(2, line_w - 2))

                for leg_id in (common_id, active_id):
                    if leg_id and leg_id in elements:
                        lnbr = elements[leg_id]
                        leg_col = self._track_color(lnbr, flash_on, ref_now)
                        target_pt = self.get_elem_logical_pos(lnbr)
                        if leg_id in track_endpoints:
                            lp0, lp1 = track_endpoints[leg_id]
                            target_pt = min((lp0, lp1), key=lambda p: math.hypot(p[0] - t_log[0], p[1] - t_log[1]))
                        g_dir = (el.get("green_path_dir") or lnbr.get("green_path_dir")) if (el.get("green_path") and lnbr.get("green_path")) else None
                        self._draw_octilinear_link(t_log, target_pt, col1, leg_col, line_w, green_dir=g_dir)

        # 1a. Dim dotted track section separators
        self._draw_section_separators(track_endpoints)

        # 1b. Draw TrackJump Line-Break Links to Connected Track/Turnout
        for eid, el in elements.items():
            if el.get("elem_type") == "track_jump":
                conn_id = el.get("connected_element", "")
                if conn_id and conn_id in elements:
                    j_log = self.get_elem_logical_pos(el)
                    cnbr = elements[conn_id]
                    c_col = self._track_color(cnbr, flash_on, ref_now)
                    target_pt = self.get_elem_logical_pos(cnbr)
                    if conn_id in track_endpoints:
                        cp0, cp1 = track_endpoints[conn_id]
                        target_pt = min((cp0, cp1), key=lambda p: math.hypot(p[0] - j_log[0], p[1] - j_log[1]))
                    g_dir = cnbr.get("green_path_dir") if cnbr.get("green_path") else None
                    self._draw_octilinear_link(j_log, target_pt, c_col, c_col, line_w, green_dir=g_dir)

        # 2. Gapless 1-Cell Welding
        track_id_list = list(track_endpoints.keys())
        for i, tid_a in enumerate(track_id_list):
            col_a = self._track_color(elements[tid_a], flash_on, ref_now)
            for tid_b in track_id_list[i + 1 :]:
                if tuple(sorted((tid_a, tid_b))) in drawn_pairs:
                    continue
                col_b = self._track_color(elements[tid_b], flash_on, ref_now)
                for pa in track_endpoints[tid_a]:
                    for pb in track_endpoints[tid_b]:
                        if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= 1.5:
                            g_dir = (elements[tid_a].get("green_path_dir") or elements[tid_b].get("green_path_dir")) if (elements[tid_a].get("green_path") and elements[tid_b].get("green_path")) else None
                            self._draw_octilinear_link(pa, pb, col_a, col_b, line_w, green_dir=g_dir)

    def _draw_all_signals(self, elements: Dict[str, dict], flash_on: bool, ref_now: float):
        groups: Dict[Tuple[Any, Any, str, str], List[dict]] = {}
        for el in elements.values():
            if el.get("elem_type") != "signal":
                continue
            key = (el.get("wiu_id"), el.get("sig_bit_offset"), el.get("direction", "E"), el.get("protects_element", el["id"]))
            groups.setdefault(key, []).append(el)

        pos_rank = {"top": 0, "middle": 1, "bottom": 2}
        r = max(6, int(7.5 * self.zoom))
        spacing = r * 2 + 4

        for (_, _, direction, protects_id), heads in groups.items():
            heads.sort(key=lambda h: pos_rank.get(str(h.get("position", "top")).lower(), 0))
            anchor_head = heads[0]
            alx, aly = self.get_elem_logical_pos(anchor_head)
            asx, asy = self.logical_to_screen(alx, aly)

            prot_el = elements.get(protects_id)
            if prot_el:
                plx, ply = self.get_elem_logical_pos(prot_el)
                psx, psy = self.logical_to_screen(plx, ply)
            else:
                psx, psy = asx, asy + 20

            is_ns = direction in ("N", "S")
            if is_ns:
                step_dx = -spacing if psx >= asx else spacing
                step_dy = 0
            else:
                step_dx = 0
                step_dy = -spacing if psy >= asy else spacing

            is_stale, _ = self.get_element_inactivity(anchor_head, ref_now)
            mast_col = COLOR_TRACK_STALE_GRAY if is_stale else COLOR_SIGNAL_MAST
            if len(heads) > 1:
                end_x = asx + step_dx * (len(heads) - 1)
                end_y = asy + step_dy * (len(heads) - 1)
                pygame.draw.line(self.screen, mast_col, (asx, asy), (end_x, end_y), 3)

            head_screen_pts: List[Tuple[int, int]] = []
            for idx, h_el in enumerate(heads):
                if h_el["id"] in self.prefs.overrides and "x" in self.prefs.overrides[h_el["id"]]:
                    hlx, hly = self.get_elem_logical_pos(h_el)
                    hx, hy = self.logical_to_screen(hlx, hly)
                else:
                    hx = asx + idx * step_dx
                    hy = asy + idx * step_dy
                head_screen_pts.append((hx, hy))

                if h_el["id"] == self.selected_id:
                    pygame.draw.circle(self.screen, COLOR_SELECT_RING, (hx, hy), r + 6, 2)

                h_stale, age = self.get_element_inactivity(h_el, ref_now)

                curr_color = h_el.get("base_color")
                if curr_color and curr_color not in ("GRAY", "DARK", "BLACK"):
                    self.last_seen_signal_colors[h_el["id"]] = curr_color

                last_col_name = (
                    h_el.get("last_reported_color")
                    or self.last_seen_signal_colors.get(h_el["id"])
                    or curr_color
                    or "GRAY"
                )
                if last_col_name in ("GRAY", "DARK", "BLACK") and h_el["id"] in self.last_seen_signal_colors:
                    last_col_name = self.last_seen_signal_colors[h_el["id"]]

                active_rgb = LAMP_COLORS.get(last_col_name, LAMP_COLORS["GRAY"])
                dark_rgb = DARK_LAMP_COLORS.get(
                    last_col_name,
                    (max(10, int(active_rgb[0] * 0.42)), max(10, int(active_rgb[1] * 0.42)), max(10, int(active_rgb[2] * 0.42)))
                )

                if float(h_el.get("last_packet_ts", 0.0) or 0.0) <= 0.0:
                    lamp_rgb = DARK_LAMP_COLORS.get(last_col_name, DARK_LAMP_COLORS["GRAY"])
                elif age < 3.5:
                    lamp_rgb = active_rgb
                    if h_el.get("flashing", False) and not flash_on:
                        lamp_rgb = LAMP_COLORS["BLACK"]
                elif h_stale or age >= self.stale_timeout_sec:
                    # When fading signals, fade them to a darker version of their last reported color instead of dark gray
                    lamp_rgb = dark_rgb
                else:
                    # Smoothly fade from active color to darker version of last reported color
                    fade_ratio = min(1.0, max(0.0, (age - 3.5) / max(1.0, self.stale_timeout_sec - 3.5)))
                    r_c = int(round(active_rgb[0] * (1.0 - fade_ratio) + dark_rgb[0] * fade_ratio))
                    g_c = int(round(active_rgb[1] * (1.0 - fade_ratio) + dark_rgb[1] * fade_ratio))
                    b_c = int(round(active_rgb[2] * (1.0 - fade_ratio) + dark_rgb[2] * fade_ratio))
                    lamp_rgb = (r_c, g_c, b_c)
                    if h_el.get("flashing", False) and not flash_on and fade_ratio < 0.65:
                        lamp_rgb = LAMP_COLORS["BLACK"]

                pygame.draw.circle(self.screen, (18, 22, 28), (hx, hy), r + 2)
                pygame.draw.circle(self.screen, lamp_rgb, (hx, hy), r)
                pygame.draw.circle(self.screen, mast_col, (hx, hy), r + 2, 1)
                self._draw_signal_direction_arrow(hx, hy, max(3, r - 2), direction)

            if (self.show_labels or anchor_head.get("ctc_active")) and head_screen_pts:
                lbl = self.get_elem_label(anchor_head)
                disp_tag = f"{anchor_head['id']} ({lbl})" if (lbl and self.show_labels) else anchor_head['id']
                lbl_surf = self.render_aged_label(disp_tag, anchor_head, ref_now, active_rgb=(215, 230, 250))
                lpos = str(anchor_head.get("label_pos", "top")).lower()

                min_hx = min(pt[0] for pt in head_screen_pts)
                max_hx = max(pt[0] for pt in head_screen_pts)
                min_hy = min(pt[1] for pt in head_screen_pts)
                max_hy = max(pt[1] for pt in head_screen_pts)
                mid_hx = (min_hx + max_hx) // 2
                mid_hy = (min_hy + max_hy) // 2
                lw, lh = lbl_surf.get_width(), lbl_surf.get_height()
                pad = r + 8

                if lpos == "bottom":
                    lbl_x, lbl_y = mid_hx - lw // 2, max_hy + pad
                elif lpos == "left":
                    lbl_x, lbl_y = min_hx - pad - lw, mid_hy - lh // 2
                elif lpos == "right":
                    lbl_x, lbl_y = max_hx + pad, mid_hy - lh // 2
                else:
                    lbl_x, lbl_y = mid_hx - lw // 2, min_hy - pad - lh
                self.screen.blit(lbl_surf, (lbl_x, lbl_y))
                if anchor_head.get("ctc_active"):
                    pygame.draw.rect(self.screen, (0, 240, 255), (lbl_x - 3, lbl_y - 2, lw + 6, lh + 4), 1, border_radius=2)

    def _draw_signal_direction_arrow(self, cx: int, cy: int, sz: int, direction: str):
        arrow_col = (15, 18, 22)
        d = direction.upper()
        if d in ("E", "NE", "SE"):
            pts = [(cx - sz // 2, cy - sz), (cx + sz, cy), (cx - sz // 2, cy + sz)]
        elif d in ("W", "NW", "SW"):
            pts = [(cx + sz // 2, cy - sz), (cx - sz, cy), (cx + sz // 2, cy + sz)]
        elif d == "N":
            pts = [(cx - sz, cy + sz // 2), (cx, cy - sz), (cx + sz, cy + sz // 2)]
        else:
            pts = [(cx - sz, cy - sz // 2), (cx, cy + sz), (cx + sz, cy - sz // 2)]
        pygame.draw.lines(self.screen, arrow_col, False, pts, 2)

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

    def draw_nodes_and_overlays(self, elements: Dict[str, dict], flash_on: bool, ref_now: float):
        curr_state = self.current_state()
        ts_data = curr_state.get("train_scroller", {}) if isinstance(curr_state, dict) else {}
        self._draw_all_signals(elements, flash_on, ref_now)

        for eid, el in elements.items():
            etype = el["elem_type"]
            if etype == "signal":
                continue

            lx, ly = self.get_elem_logical_pos(el)
            sx, sy = self.logical_to_screen(lx, ly)
            label = self.get_elem_label(el)
            disp_tag = f"{eid} ({label})" if label else eid
            is_stale, age = self.get_element_inactivity(el, ref_now)
            lpos = el.get("label_pos", "top")

            if eid == self.selected_id:
                pygame.draw.circle(self.screen, COLOR_SELECT_RING, (sx, sy), int(15 * self.zoom) + 4, 2)
            elif self.edit_mode:
                pygame.draw.circle(self.screen, COLOR_GRID_MAJOR, (sx, sy), int(10 * self.zoom) + 2, 1)

            if el.get("collision_warning") and flash_on:
                rad1 = int(16 * self.zoom) + 4
                rad2 = int(23 * self.zoom) + 6
                pygame.draw.circle(self.screen, COLOR_TRACK_RED, (sx, sy), rad1, 3)
                pygame.draw.circle(self.screen, (255, 190, 40), (sx, sy), rad2, 2)

            if etype == "track":
                col = self._track_color(el, flash_on, ref_now)
                r = max(4, int(5 * self.zoom))
                pygame.draw.circle(self.screen, col, (sx, sy), r)
                pygame.draw.circle(self.screen, COLOR_BG, (sx, sy), max(2, r - 2))

                if self.show_labels or el.get("ctc_active"):
                    len_mi = el.get("length_miles", 0.5)
                    tk_title = f"{disp_tag} [{len_mi}mi]" if self.show_labels else disp_tag
                    id_surf = self.render_aged_label(tk_title, el, ref_now, active_rgb=(235, 245, 255))
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

                        is_rx = False
                        trn_st = "INFERRED" if is_inferred else "CONFIRMED"
                        if ts_data and "trains" in ts_data:
                            for trn in ts_data["trains"]:
                                if trn.get("current_element_id") == eid or trn.get("last_element_id") == eid or trn.get("train_name") == tname or trn.get("assigned_name") == tname:
                                    is_dimmed = is_dimmed or trn.get("is_dimmed", False)
                                    is_rx = trn.get("is_receiving_packets", False)
                                    trn_st = trn.get("status", trn_st).upper()
                                    if trn.get("assigned_name"):
                                        tname = trn["assigned_name"]
                                    if spd is None and trn.get("speed_mph") is not None:
                                        spd = trn.get("speed_mph")
                                    break

                        rx_dot = "● " if is_rx else ""
                        dim_tag = " (DIMMED)" if is_dimmed else ""
                        spd_txt = f"{spd:.1f}MPH" if spd is not None else ("Unk." if is_inferred else "")
                        badge_txt = f"{rx_dot}{tname} [{trn_st[:4]}] {tdir} {spd_txt}{dim_tag}".strip()
                        
                        b_col = (175, 185, 195) if is_dimmed else (240, 248, 255)
                        b_surf = self.font_sm_bold.render(badge_txt, True, b_col)
                        pad_w, pad_h = b_surf.get_width() + 12, b_surf.get_height() + 6
                        badge_box = pygame.Surface((pad_w, pad_h), pygame.SRCALPHA)
                        
                        bg_rgba = (35, 45, 60, 130) if is_dimmed else (25, 95, 215, 175)
                        border_rgba = (95, 105, 120, 160) if is_dimmed else (90, 175, 255, 215)
                        pygame.draw.rect(badge_box, bg_rgba, (0, 0, pad_w, pad_h), border_radius=5)
                        pygame.draw.rect(badge_box, border_rgba, (0, 0, pad_w, pad_h), 1, border_radius=5)
                        badge_box.blit(b_surf, (6, 3))

                        badge_pos = "bottom" if lpos == "top" else ("top" if lpos == "bottom" else ("right" if lpos == "left" else "left"))
                        bx, by = self._get_label_rect(sx, sy, pad_w, pad_h, badge_pos, int(12 * self.zoom))
                        self.screen.blit(badge_box, (bx, by))

            elif etype == "turnout":
                col = self._track_color(el, flash_on, ref_now)
                sz = max(6, int(7 * self.zoom))
                pts = [(sx, sy - sz), (sx + sz, sy), (sx, sy + sz), (sx - sz, sy)]
                pygame.draw.polygon(self.screen, col, pts)
                pygame.draw.polygon(self.screen, COLOR_BG, pts, 1)
                if el.get("green_path") and el.get("green_path_dir"):
                    self._draw_turnout_diamond_arrow(sx, sy, sz, el["green_path_dir"])
                
                if self.show_labels or el.get("ctc_active"):
                    align_char = "N" if el.get("state", 0) == 0 else "R"
                    t_title = f"{disp_tag}[{align_char}]" if self.show_labels else disp_tag
                    t_surf = self.render_aged_label(t_title, el, ref_now, active_rgb=COLOR_EDIT_ACCENT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, t_surf.get_width(), t_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(t_surf, (lbl_x, lbl_y))
                    if el.get("ctc_active"):
                        pygame.draw.rect(self.screen, (0, 240, 255), (lbl_x - 3, lbl_y - 2, t_surf.get_width() + 6, t_surf.get_height() + 4), 1, border_radius=2)

            elif etype == "crossing":
                gate = el.get("gate_status", "OPEN")
                col = COLOR_TRACK_STALE_GRAY if is_stale else (COLOR_CROSSING_DOWN if gate == "DOWN" else COLOR_CROSSING_OPEN)
                sz = max(7, int(9 * self.zoom))
                pygame.draw.line(self.screen, col, (sx - sz, sy - sz), (sx + sz, sy + sz), 3)
                pygame.draw.line(self.screen, col, (sx - sz, sy + sz), (sx + sz, sy - sz), 3)
                
                if self.show_labels:
                    c_surf = self.render_aged_label(f"{disp_tag} [{gate}]", el, ref_now, active_rgb=col)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, c_surf.get_width(), c_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(c_surf, (lbl_x, lbl_y))

            elif etype == "base":
                active_col = (195, 155, 255)
                dark_col = (65, 72, 85)
                last_ts = float(el.get("last_packet_ts", 0.0) or 0.0)

                if last_ts <= 0.0 or is_stale or age >= self.stale_timeout_sec:
                    col = dark_col
                    is_active = False
                elif age < 3.5:
                    col = active_col
                    is_active = True
                else:
                    fade_ratio = min(1.0, max(0.0, (age - 3.5) / max(1.0, self.stale_timeout_sec - 3.5)))
                    r_c = int(round(active_col[0] * (1.0 - fade_ratio) + dark_col[0] * fade_ratio))
                    g_c = int(round(active_col[1] * (1.0 - fade_ratio) + dark_col[1] * fade_ratio))
                    b_c = int(round(active_col[2] * (1.0 - fade_ratio) + dark_col[2] * fade_ratio))
                    col = (r_c, g_c, b_c)
                    is_active = (fade_ratio < 0.45)

                sz = max(7, int(9 * self.zoom))
                pts = [(sx, sy - sz), (sx - sz, sy + sz), (sx + sz, sy + sz)]
                line_th = 3 if (is_active and age < 2.0) else 2
                pygame.draw.polygon(self.screen, col, pts, line_th)
                pygame.draw.circle(self.screen, col, (sx, sy - sz - 3), 3, 0 if is_active else 1)
                if is_active and age < 2.0:
                    pygame.draw.circle(self.screen, col, (sx, sy - sz - 3), max(5, int(6.5 * self.zoom)), 1)
                    pygame.draw.circle(self.screen, (245, 235, 255), (sx, sy - sz - 3), 2)
                
                if self.show_labels:
                    rad_id = el.get("radio_id")
                    rad_str = f" [ID:{rad_id}]" if rad_id is not None else ""
                    b_surf = self.render_aged_label(f"{disp_tag}{rad_str} (Ch{el.get('channel')}: {el.get('packets_relayed', 0)})", el, ref_now, active_rgb=active_col)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, b_surf.get_width(), b_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(b_surf, (lbl_x, lbl_y))

            elif etype == "station":
                w, h = max(14, int(18 * self.zoom)), max(10, int(12 * self.zoom))
                rect = pygame.Rect(sx - w // 2, sy - h // 2, w, h)
                pygame.draw.rect(self.screen, COLOR_LANDMARK, rect, border_radius=3)
                
                if self.show_labels:
                    s_surf = self.font_sm_thin.render(disp_tag, True, COLOR_LANDMARK)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, s_surf.get_width(), s_surf.get_height(), lpos, h // 2 + 5)
                    self.screen.blit(s_surf, (lbl_x, lbl_y))

            elif etype == "bridge":
                w, h = max(18, int(24 * self.zoom)), max(10, int(14 * self.zoom))
                pygame.draw.line(self.screen, COLOR_LANDMARK, (sx - w // 2, sy - h // 2), (sx + w // 2, sy - h // 2), 2)
                pygame.draw.line(self.screen, COLOR_LANDMARK, (sx - w // 2, sy + h // 2), (sx + w // 2, sy + h // 2), 2)
                
                if self.show_labels:
                    b_surf = self.font_sm_thin.render(disp_tag, True, COLOR_TEXT_DIM)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, b_surf.get_width(), b_surf.get_height(), lpos, h // 2 + 5)
                    self.screen.blit(b_surf, (lbl_x, lbl_y))

            elif etype == "diamond":
                sz = max(8, int(11 * self.zoom))
                pts = [(sx, sy - sz), (sx + sz, sy), (sx, sy + sz), (sx - sz, sy)]
                pygame.draw.polygon(self.screen, COLOR_LANDMARK, pts, 2)
                
                if self.show_labels:
                    d_surf = self.font_sm_thin.render(disp_tag, True, COLOR_TEXT_DIM)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, d_surf.get_width(), d_surf.get_height(), lpos, sz + 5)
                    self.screen.blit(d_surf, (lbl_x, lbl_y))

            elif etype == "label":
                if self.show_labels:
                    l_surf = self.font_lg.render(label or eid, True, COLOR_LANDMARK)
                    self.screen.blit(l_surf, (sx - l_surf.get_width() // 2, sy))

            elif etype == "track_jump":
                letter = str(el.get("letter") or "A").upper()
                paired_id = el.get("paired_jump_id") or ""
                rad = max(11, int(13 * self.zoom))
                pygame.draw.circle(self.screen, (26, 38, 58), (sx, sy), rad)
                pygame.draw.circle(self.screen, COLOR_EDIT_ACCENT, (sx, sy), rad, 2)
                ltr_surf = self.font_md.render(letter, True, (255, 230, 90))
                self.screen.blit(ltr_surf, (sx - ltr_surf.get_width() // 2, sy - ltr_surf.get_height() // 2))
                if self.show_labels:
                    pair_tag = f"->{paired_id}" if paired_id else ""
                    j_surf = self.font_sm_bold.render(f"{eid}[{letter}{pair_tag}]", True, COLOR_EDIT_ACCENT)
                    lbl_x, lbl_y = self._get_label_rect(sx, sy, j_surf.get_width(), j_surf.get_height(), lpos, rad + 4)
                    self.screen.blit(j_surf, (lbl_x, lbl_y))

            elif etype in ("ptc_datetime", "date_time", "datetime"):
                w = int((el.get("width") or 220) * self.zoom)
                h = int((el.get("height") or 46) * self.zoom)
                dt_str = el.get("datetime_str") or el.get("last_datetime_str") or "--- --:--:-- UTC"
                is_stale, age = self.get_element_inactivity(el, ref_now)
                bg_col = (18, 24, 34, 210) if not is_stale else (24, 26, 30, 180)
                border_col = (45, 145, 235) if not is_stale else (80, 95, 110)
                txt_col = (100, 240, 255) if not is_stale else (140, 155, 170)
                if el.get("packets_received", 0) > 0 and not is_stale:
                    border_col = (45, 200, 120)
                    txt_col = (120, 255, 170)

                clk_surf = pygame.Surface((w, h), pygame.SRCALPHA)
                pygame.draw.rect(clk_surf, bg_col, (0, 0, w, h), border_radius=6)
                pygame.draw.rect(clk_surf, border_col, (0, 0, w, h), 2, border_radius=6)

                title_txt = f"PTC CLOCK - {disp_tag}" if label else f"PTC DATE/TIME [{eid}]"
                hdr_surf = self.font_sm_bold.render(title_txt, True, (190, 215, 245))
                clk_surf.blit(hdr_surf, (8, 4))

                val_surf = self.font_md.render(dt_str, True, txt_col)
                clk_surf.blit(val_surf, (8, max(4, h - val_surf.get_height() - 5)))

                self.screen.blit(clk_surf, (sx, sy))
                if eid == self.selected_id:
                    pygame.draw.rect(self.screen, COLOR_SELECT_RING, (sx - 2, sy - 2, w + 4, h + 4), 2, border_radius=8)

    # -----------------------------------------------------------------
    # Telemetry Panels, Resize Grips, & Read-Only Inspector
    # -----------------------------------------------------------------
    def _draw_panel_resize_grip(self, panel_surf: pygame.Surface, pid: str, px: int, py: int):
        pw, ph = panel_surf.get_size()
        grip_sz = max(14, int(16 * self.zoom))
        col = COLOR_EDIT_ACCENT if self.resizing_panel_id == pid else COLOR_SELECT_RING
        for step in (4, 8, 12):
            pygame.draw.line(panel_surf, col, (pw - step, ph - 2), (pw - 2, ph - step), 2)
        self.panel_resize_rects[pid] = pygame.Rect(px + pw - grip_sz, py + ph - grip_sz, grip_sz, grip_sz)

    def draw_telemetry_panels(self, state: dict):
        self.panel_resize_rects.clear()
        self.scroller_btn_rects.clear()
        self.scroller_line_rects.clear()
        self.scroller_panel_rect = pygame.Rect(0, 0, 0, 0)
        scroller = state.get("packet_scroller")
        if scroller:
            lx, ly = self.get_elem_logical_pos(scroller)
            px, py = self.logical_to_screen(lx, ly)
            base_w, base_h = self.get_panel_base_size("packet_scroller", scroller)
            w, h = max(220, int(base_w * self.zoom)), max(85, int(base_h * self.zoom))
            max_chars = max(28, int((base_w - 24) / 7.2))

            ch_stats = scroller.get("channel_stats") or {}
            ch_parts = [
                f"Ch{ch}: {cinfo.get('total', 0)} ({cinfo.get('ppm', 0)}/m)"
                for ch, cinfo in sorted(ch_stats.items(), key=lambda kv: int(kv[0]) if str(kv[0]).lstrip('-').isdigit() else 0)
            ]
            if not ch_parts:
                ch_lines = ["Channels: None"]
            else:
                ch_lines = []
                cur_line = "Channels: "
                for idx_c, part in enumerate(ch_parts):
                    sep = "  |  " if idx_c > 0 and cur_line != "Channels: " else ""
                    if len(cur_line) + len(sep) + len(part) > max_chars and cur_line != "Channels: ":
                        ch_lines.append(cur_line)
                        cur_line = "          " + part
                    else:
                        cur_line += sep + part
                if cur_line:
                    ch_lines.append(cur_line)

            panel_surf = pygame.Surface((w, h), pygame.SRCALPHA)
            panel_surf.fill(COLOR_HUD_BG)
            border_col = COLOR_SELECT_RING if self.selected_id == scroller["id"] else COLOR_HUD_BORDER
            pygame.draw.rect(panel_surf, border_col, (0, 0, w, h), 2, border_radius=6)

            title = self.get_elem_label(scroller) if scroller.get("label") else "PACKET SCROLLER"
            self.scroller_panel_rect = pygame.Rect(px, py, w, h)
            if self.scroller_scroll_offset > 0:
                title = f"{title} [SCROLLED +{self.scroller_scroll_offset} | Home=Top]"
            btn_defs = [("copy", "Copy"), ("pgdn", "PgDn>"), ("pgup", "<PgUp"), ("top", "Top(Home)")]
            bx_cur = w - 8
            for b_key, b_lbl in btn_defs:
                ts = self.font_sm_bold.render(b_lbl, True, (235, 245, 255))
                bw_btn = ts.get_width() + 10
                bx_cur -= bw_btn
                if bx_cur < 170:
                    break
                local_r = pygame.Rect(bx_cur, 5, bw_btn, 19)
                bg_b = (210, 120, 25) if (b_key == "top" and self.scroller_scroll_offset > 0) else (38, 52, 74)
                pygame.draw.rect(panel_surf, bg_b, local_r, border_radius=4)
                pygame.draw.rect(panel_surf, COLOR_HUD_BORDER, local_r, 1, border_radius=4)
                panel_surf.blit(ts, (bx_cur + 5, 7))
                self.scroller_btn_rects[b_key] = pygame.Rect(px + bx_cur, py + 5, bw_btn, 19)
                bx_cur -= 4
            panel_surf.blit(self.font_md.render(title, True, COLOR_CROSSING_OPEN), (10, 7))

            start_ts = scroller.get("start_time")
            if not self.paused and start_ts:
                el_int = max(0, int(time.time() - float(start_ts)))
                elapsed_str = f"{el_int // 3600:02d}:{(el_int % 3600) // 60:02d}:{el_int % 60:02d}"
            else:
                elapsed_str = scroller.get("elapsed_str", "00:00:00")
            total_pkts = scroller.get("total_packets", 0)
            total_ppm = scroller.get("total_ppm", 0)
            stats_hdr = f"Elapsed: {elapsed_str}   |   Total: {total_pkts} pkts ({total_ppm}/min)"
            panel_surf.blit(self.font_sm_bold.render(stats_hdr[:max_chars], True, COLOR_LANDMARK), (10, 28))

            y_cursor = 46
            for ch_ln in ch_lines:
                if y_cursor + 18 > h - 24:
                    break
                panel_surf.blit(self.font_sm_bold.render(ch_ln[:max_chars], True, (135, 195, 255)), (10, y_cursor))
                y_cursor += 18

            pygame.draw.line(panel_surf, COLOR_HUD_BORDER, (10, y_cursor + 1), (w - 10, y_cursor + 1), 1)
            y_cursor += 5

            avail_rows = max(1, (h - y_cursor - 8) // 20)
            max_r = min(avail_rows, int(scroller.get("extra_info") or scroller.get("max_len") or 20))
            hist_list = list(self.scroller_history) if self.scroller_history else list(scroller.get("entries", []))
            self.scroller_scroll_offset = max(0, min(max(0, len(hist_list) - 1), self.scroller_scroll_offset))
            entries = hist_list[self.scroller_scroll_offset : self.scroller_scroll_offset + max_r]
            sel_lo = min(self.scroller_sel_start, self.scroller_sel_end) if (self.scroller_sel_start is not None and self.scroller_sel_end is not None) else -1
            sel_hi = max(self.scroller_sel_start, self.scroller_sel_end) if (self.scroller_sel_start is not None and self.scroller_sel_end is not None) else -1
            for i, line in enumerate(entries):
                row_y = y_cursor + i * 20
                if row_y + 20 > h - 4:
                    break
                abs_idx = self.scroller_scroll_offset + i
                is_sel = (sel_lo <= abs_idx <= sel_hi and sel_lo >= 0)
                is_inv = ("INVALID HEX:" in line or "INVALID" in line)
                if is_sel:
                    pygame.draw.rect(panel_surf, (42, 88, 155, 195), (6, row_y - 1, w - 12, 19), border_radius=3)
                txt_col = (255, 145, 205) if is_inv else ((255, 255, 255) if is_sel else COLOR_TEXT)
                row_font = self.font_sm_bold if is_inv else self.font_sm_thin
                panel_surf.blit(row_font.render(line[:max_chars], True, txt_col), (10, row_y))
                self.scroller_line_rects.append((abs_idx, pygame.Rect(px + 6, py + row_y - 1, w - 12, 19)))
            for i, line in enumerate([]):
                pass
            for i, line in enumerate([]):
                if y_cursor + (i + 1) * 20 > h - 4:
                    break
                panel_surf.blit(self.font_sm_thin.render(line[:max_chars], True, COLOR_TEXT), (10, y_cursor + i * 20))
            self._draw_panel_resize_grip(panel_surf, scroller["id"], px, py)
            self.screen.blit(panel_surf, (px, py))

        addr_table = state.get("address_table")
        if addr_table:
            lx, ly = self.get_elem_logical_pos(addr_table)
            px, py = self.logical_to_screen(lx, ly)
            base_w, base_h = self.get_panel_base_size("address_table", addr_table)
            w, h = max(220, int(base_w * self.zoom)), max(85, int(base_h * self.zoom))
            panel_surf = pygame.Surface((w, h), pygame.SRCALPHA)
            panel_surf.fill(COLOR_HUD_BG)
            border_col = COLOR_SELECT_RING if self.selected_id == addr_table["id"] else COLOR_HUD_BORDER
            pygame.draw.rect(panel_surf, border_col, (0, 0, w, h), 2, border_radius=6)

            self.addr_table_panel_rect = pygame.Rect(px, py, w, h)
            self.addr_table_btn_rects.clear()

            wius_dict = addr_table.get("wius", {})
            wius_list = list(wius_dict.items())

            # Sort wius_list according to self.wiu_sort_by ("wiu", "rssi", "pkts")
            def _get_wiu_numeric_key(item):
                wid, winfo = item
                wid_str = str(wid).strip()
                m = re.search(r'\d+', wid_str)
                return (int(m.group(0)) if m else 0, wid_str)

            cur_sort = getattr(self, "wiu_sort_by", "wiu")
            cur_desc = getattr(self, "wiu_sort_desc", False)
            if cur_sort == "rssi":
                def _sort_key(item):
                    wid, winfo = item
                    r = winfo.get("avg_rssi", winfo.get("rssi_avg"))
                    if r is None:
                        r = winfo.get("last_rssi")
                    try:
                        val = float(r)
                    except (ValueError, TypeError):
                        val = -999.0 if cur_desc else 999.0
                    return (val, _get_wiu_numeric_key(item))
                wius_list.sort(key=_sort_key, reverse=cur_desc)
            elif cur_sort == "pkts":
                def _sort_key(item):
                    wid, winfo = item
                    try:
                        pkts = int(winfo.get("packet_count", 0) or 0)
                    except (ValueError, TypeError):
                        pkts = 0
                    return (pkts, _get_wiu_numeric_key(item))
                wius_list.sort(key=_sort_key, reverse=cur_desc)
            else:
                # Default to WIU ids
                wius_list.sort(key=_get_wiu_numeric_key, reverse=cur_desc)

            total_unique = addr_table.get("total_unique_wius", len(wius_dict))
            total_items = len(wius_list)
            self.addr_table_total_items = total_items

            avail_rows = max(1, (h - 52) // 19)
            self.addr_table_avail_rows = avail_rows
            max_scroll = max(0, total_items - avail_rows)
            self.wiu_scroll_offset = max(0, min(max_scroll, self.wiu_scroll_offset))

            title_base = self.get_elem_label(addr_table) if addr_table.get("label") else "WIU ADDRESS TABLE"
            curr_page = (self.wiu_scroll_offset // avail_rows) + 1 if avail_rows > 0 else 1
            total_pages = max(1, (total_items + avail_rows - 1) // avail_rows) if avail_rows > 0 else 1
            if total_items > avail_rows:
                title = f"{title_base} ({total_unique}) [Pg {curr_page}/{total_pages}]"
            else:
                title = f"{title_base}  (Total: {total_unique})"

            # Clickable paging and sort header buttons: [PgDn>] [<PgUp] [Top] [Sort: WIU]
            sort_lbl = f"Sort:{cur_sort.upper()}"
            btn_defs = [("pgdn", "PgDn>"), ("pgup", "<PgUp"), ("top", "Top"), ("sort_cycle", sort_lbl)]
            bx_cur = w - 8
            for b_key, b_lbl in btn_defs:
                ts = self.font_sm_bold.render(b_lbl, True, (235, 245, 255))
                bw_btn = ts.get_width() + 10
                bx_cur -= bw_btn
                if bx_cur < 170:
                    break
                local_r = pygame.Rect(bx_cur, 5, bw_btn, 19)
                is_active = (b_key == "top" and self.wiu_scroll_offset > 0) or \
                            (b_key == "pgup" and self.wiu_scroll_offset > 0) or \
                            (b_key == "pgdn" and self.wiu_scroll_offset < max_scroll) or \
                            (b_key == "sort_cycle")
                bg_b = (20, 85, 125) if is_active else (32, 42, 58)
                border_b = (0, 210, 255) if is_active else COLOR_HUD_BORDER
                pygame.draw.rect(panel_surf, bg_b, local_r, border_radius=4)
                pygame.draw.rect(panel_surf, border_b, local_r, 1, border_radius=4)
                panel_surf.blit(ts, (bx_cur + 5, 7))
                self.addr_table_btn_rects[b_key] = pygame.Rect(px + bx_cur, py + 5, bw_btn, 19)
                bx_cur -= 4

            panel_surf.blit(self.font_md.render(title, True, COLOR_CROSSING_OPEN), (10, 7))

            scale_x = max(1.0, w / 520.0)
            col_x = [int(round(ox * scale_x)) for ox in (10, 84, 182, 230, 280, 356, 432, 476)]
            hdr_cols = [
                ("AVG RSSI", "rssi"),
                ("WIU", "wiu"),
                ("RR", None),
                ("CH", None),
                ("LAST RSSI", None),
                ("MAX RSSI", None),
                ("SEQ", None),
                ("PKTS", "pkts"),
            ]
            for ci, (cname, col_key) in enumerate(hdr_cols):
                if col_x[ci] < w - 18:
                    if col_key and cur_sort == col_key:
                        arrow = " \u25bc" if cur_desc else " \u25b2"
                        disp_hdr = cname + arrow
                        hdr_col = (0, 240, 255)
                    else:
                        disp_hdr = cname
                        hdr_col = COLOR_LANDMARK
                    txt_surf = self.font_sm_bold.render(disp_hdr, True, hdr_col)
                    panel_surf.blit(txt_surf, (col_x[ci], 28))
                    if col_key:
                        next_x = col_x[ci + 1] if ci + 1 < len(col_x) else w - 10
                        hdr_click_w = max(txt_surf.get_width() + 4, min(next_x - col_x[ci] - 2, 70))
                        self.addr_table_btn_rects[f"sort_{col_key}"] = pygame.Rect(px + col_x[ci], py + 26, hdr_click_w, 18)

            layout_wiu_ids = {
                int(el["wiu_id"])
                for el in state.get("elements", {}).values()
                if el.get("wiu_id") is not None and str(el.get("wiu_id")).lstrip("-").isdigit()
            }
            ref_now_wiu = self.get_reference_now(state)

            m_pos = pygame.mouse.get_pos()
            can_hover = (not self.train_assign_active and not self.show_coverage_map)
            if can_hover and self.readonly_inspector_id and self.inspector_rect.width > 0:
                if self.inspector_rect.collidepoint(m_pos):
                    can_hover = False

            self.hovered_wiu_row = None
            self.hovered_wiu_row_rect = None

            visible_wius = wius_list[self.wiu_scroll_offset : self.wiu_scroll_offset + avail_rows]
            for idx, (wid, winfo) in enumerate(visible_wius):
                row_y = 46 + idx * 19
                if row_y + 19 > h - 4:
                    break
                chs = ",".join(map(str, winfo.get("channels", [])))
                rr_code = str(winfo.get("rr_code", "DEF"))
                wid_str = str(wid)
                wid_nnn = wid_str[1:4] if len(wid_str) >= 4 else ""
                rr_name = str(winfo.get("rr_name") or self.rrdata.get(rr_code) or self.rrdata.get(wid_nnn) or rr_code)
                on_layout = bool(winfo.get("on_layout")) or (str(wid).lstrip("-").isdigit() and int(wid) in layout_wiu_ids)
                last_seen = float(winfo.get("last_seen", 0.0) or 0.0)
                age_s = max(0.0, ref_now_wiu - last_seen) if last_seen > 0 else self.stale_timeout_sec
                fade = min(1.0, max(0.0, age_s / max(1.0, self.stale_timeout_sec)))
                if on_layout:
                    # Bright cyan fading to dim cyan/teal
                    r_c = int(round(45 + (35 - 45) * fade))
                    g_c = int(round(255 + (115 - 255) * fade))
                    b_c = int(round(255 + (130 - 255) * fade))
                else:
                    # Bright white fading to dim gray
                    r_c = int(round(255 + (110 - 255) * fade))
                    g_c = int(round(255 + (116 - 255) * fade))
                    b_c = int(round(255 + (128 - 255) * fade))
                row_col = (r_c, g_c, b_c)
                wiu_name = winfo.get("wiu_name") or self.lookup_wiu_name(wid)
                disp_wiu = winfo.get("wiu_display_name") or format_wiu_display_name(wiu_name, wid)

                # Row hit-testing for hover pointer
                row_screen_rect = pygame.Rect(px + 6, py + row_y, w - 12, 18)
                is_row_hovered = can_hover and row_screen_rect.collidepoint(m_pos)
                if is_row_hovered:
                    self.hovered_wiu_row = {
                        "wid": wid,
                        "winfo": winfo,
                        "disp_wiu": disp_wiu,
                        "rect": row_screen_rect,
                    }
                    self.hovered_wiu_row_rect = row_screen_rect
                    row_hl = pygame.Surface((w - 12, 18), pygame.SRCALPHA)
                    row_hl.fill((20, 80, 120, 150))
                    panel_surf.blit(row_hl, (6, row_y))
                    pygame.draw.rect(panel_surf, (0, 230, 255, 220), (6, row_y, w - 12, 18), 1, border_radius=3)

                last_r = winfo.get("last_rssi")
                last_r_str = f"{float(last_r):.1f}dBm" if last_r is not None else "---"
                avg_r = winfo.get("avg_rssi", winfo.get("rssi_avg"))
                avg_r_str = f"{float(avg_r):.1f}dBm" if avg_r is not None else last_r_str
                max_r = winfo.get("high_watermark_rssi")
                max_r_str = f"{float(max_r):.1f}dBm" if max_r is not None else "---"

                vals = (
                    avg_r_str,
                    disp_wiu,
                    rr_name[:8],
                    chs[:5],
                    last_r_str,
                    max_r_str,
                    str(winfo.get("last_sequence", 0)),
                    str(winfo.get("packet_count", 0)),
                )
                font_row = self.font_sm_bold if fade < 0.25 else self.font_sm_thin
                for ci, cval in enumerate(vals):
                    if col_x[ci] < w - 14:
                        panel_surf.blit(font_row.render(cval, True, row_col), (col_x[ci], row_y))

            self._draw_panel_resize_grip(panel_surf, addr_table["id"], px, py)
            self.screen.blit(panel_surf, (px, py))

        train_scroller = state.get("train_scroller")
        if train_scroller:
            lx, ly = self.get_elem_logical_pos(train_scroller)
            px, py = self.logical_to_screen(lx, ly)
            base_w, base_h = self.get_panel_base_size("train_scroller", train_scroller)
            w, h = max(240, int(base_w * self.zoom)), max(85, int(base_h * self.zoom))
            max_chars = max(32, int((base_w - 24) / 7.0))
            panel_surf = pygame.Surface((w, h), pygame.SRCALPHA)
            panel_surf.fill(COLOR_HUD_BG)
            border_col = COLOR_SELECT_RING if self.selected_id == train_scroller["id"] else COLOR_HUD_BORDER
            pygame.draw.rect(panel_surf, border_col, (0, 0, w, h), 2, border_radius=6)

            title = self.get_elem_label(train_scroller) if train_scroller.get("label") else "TRAIN SCROLLER"
            panel_surf.blit(self.font_md.render(title, True, COLOR_CROSSING_OPEN), (10, 7))
            hdr = f"{'STATUS':<11} {'TRAIN / NAME':<17} {'SPEED':<10} {'DIR':<5} {'RX':<6} {'LOCATION'}"
            panel_surf.blit(self.font_sm_bold.render(hdr[:max_chars], True, COLOR_LANDMARK), (10, 28))

            avail_rows = max(1, (h - 52) // 19)
            max_r = min(avail_rows, int(train_scroller.get("extra_info") or 200))
            drawn_idx = 0
            self._train_scroller_row_rects = []
            self._train_scroller_del_rects = []
            self.hovered_train = None
            self.hovered_train_rect = None

            m_pos = pygame.mouse.get_pos()
            can_hover = (not self.train_assign_active and not self.show_coverage_map)
            if can_hover and self.readonly_inspector_id and self.inspector_rect.width > 0:
                if self.inspector_rect.collidepoint(m_pos):
                    can_hover = False
            for trn in train_scroller.get("trains", []):
                if not self.show_inferred_trains:
                    le = trn.get("last_element_id") or trn.get("current_element_id")
                    if le and le in state.get("elements", {}):
                        el_st = state["elements"][le]
                        if el_st.get("inferred_occupied") and not el_st.get("hw_occupied"):
                            continue
                if drawn_idx >= max_r or (46 + (drawn_idx + 1) * 19 > h - 4):
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
                loc_txt = trn.get("last_location", "") or trn.get("current_element_id", "")

                row_y = 46 + drawn_idx * 19
                self._train_scroller_row_rects.append((pygame.Rect(px + 10, py + row_y, w - 38, 18), trn))
                
                # Delete train row button [x]
                del_btn_rect = pygame.Rect(px + w - 24, py + row_y + 1, 16, 16)
                t_del_key = trn.get("train_key") or trn.get("train_name")
                self._train_scroller_del_rects.append((del_btn_rect, t_del_key))
                is_del_h = del_btn_rect.collidepoint(pygame.mouse.get_pos())
                del_c = (255, 75, 75) if is_del_h else (160, 80, 80)
                del_surf = self.font_sm_bold.render("x", True, del_c)
                del_x = (w - 24) + (16 - del_surf.get_width()) // 2
                del_y = (row_y + 1) + (16 - del_surf.get_height()) // 2
                panel_surf.blit(del_surf, (del_x, del_y))

                # Highlight row if hovered
                row_screen_rect = pygame.Rect(px + 10, py + row_y, w - 38, 18)
                is_row_hovered = can_hover and row_screen_rect.collidepoint(m_pos) and not is_del_h
                if is_row_hovered:
                    self.hovered_train = trn
                    self.hovered_train_rect = row_screen_rect
                    row_hl = pygame.Surface((w - 38, 18), pygame.SRCALPHA)
                    row_hl.fill((35, 75, 125, 160))
                    panel_surf.blit(row_hl, (10, row_y))
                    pygame.draw.rect(panel_surf, (80, 200, 255, 230), (10, row_y, w - 38, 18), 1, border_radius=3)

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
                panel_surf.blit(self.font_sm_bold.render(st_badge, True, st_col), (10, row_y))

                rem_row = f"{disp_name[:16]:<17} {spd_txt:<10} {dir_txt:<5} "
                panel_surf.blit(self.font_sm_thin.render(rem_row, True, col), (85, row_y))

                rx_col = (0, 255, 130) if is_rx else (110, 115, 125)
                panel_surf.blit(self.font_sm_bold.render(f"{rx_txt:<6}", True, rx_col), (305, row_y))

                panel_surf.blit(self.font_sm_thin.render(loc_txt[:max(12, max_chars - 43)], True, col), (355, row_y))
                drawn_idx += 1
            self._draw_panel_resize_grip(panel_surf, train_scroller["id"], px, py)
            self.screen.blit(panel_surf, (px, py))

        # 4. CHAT PANEL
        self.chat_panel_rect = pygame.Rect(0, 0, 0, 0)
        self.chat_btn_rects.clear()
        self.chat_sender_rects.clear()
        chat_panel = state.get("chat")
        if not chat_panel:
            for el in state.get("elements", {}).values():
                if isinstance(el, dict) and el.get("elem_type") == "chat":
                    chat_panel = el
                    break
        if chat_panel:
            self._ingest_chat_messages_from_state(chat_panel)
            lx, ly = self.get_elem_logical_pos(chat_panel)
            px, py = self.logical_to_screen(lx, ly)
            base_w, base_h = self.get_panel_base_size("chat", chat_panel)
            w, h = max(320, int(base_w * self.zoom)), max(150, int(base_h * self.zoom))
            self.chat_panel_rect = pygame.Rect(px, py, w, h)

            panel_surf = pygame.Surface((w, h), pygame.SRCALPHA)
            panel_surf.fill(COLOR_HUD_BG)
            border_col = COLOR_SELECT_RING if self.selected_id == chat_panel.get("id", "SYS_CHAT") else COLOR_HUD_BORDER
            pygame.draw.rect(panel_surf, border_col, (0, 0, w, h), 2, border_radius=6)

            my_id = self.get_viewer_chat_identity(state)
            my_disp = self.chat_identity_store.resolve_display_name(my_id)
            id_badge = f"{my_disp}" if my_disp == my_id else f"{my_disp} ({my_id})"
            title_base = self.get_elem_label(chat_panel) if chat_panel.get("label") else "CHAT"
            title = f"{title_base} [{id_badge[:18]}]"
            panel_surf.blit(self.font_md.render(title, True, COLOR_CROSSING_OPEN), (10, 6))

            # Top row control buttons: [Clear] [PgDn>] [<PgUp] [ID/Names] [Mute]
            top_btns = [
                ("clear", "Clear", (95, 38, 38)),
                ("pgdn", "PgDn>", (36, 52, 74)),
                ("pgup", "<PgUp", (36, 52, 74)),
                ("id_names", "ID/Names", (32, 85, 115)),
                ("mute", "Mute:ON" if self.chat_mute_all else "Mute:OFF", (185, 42, 42) if self.chat_mute_all else (36, 52, 74)),
            ]
            bx_cur = w - 8
            for b_key, b_lbl, bg_b in top_btns:
                ts = self.font_sm_bold.render(b_lbl, True, (240, 248, 255))
                bw_btn = ts.get_width() + 10
                bx_cur -= bw_btn
                if bx_cur < 120:
                    break
                local_r = pygame.Rect(bx_cur, 5, bw_btn, 19)
                pygame.draw.rect(panel_surf, bg_b, local_r, border_radius=4)
                pygame.draw.rect(panel_surf, COLOR_HUD_BORDER, local_r, 1, border_radius=4)
                panel_surf.blit(ts, (bx_cur + 5, 7))
                self.chat_btn_rects[b_key] = pygame.Rect(px + bx_cur, py + 5, bw_btn, 19)
                bx_cur -= 4

            # Second row: Include RegEx & Exclude RegEx filters
            cursor_on = (int(time.time() * 2) % 2) == 0
            filter_y = 27
            half_w = max(90, (w - 24) // 2)
            inc_rect_local = pygame.Rect(8, filter_y, half_w - 2, 19)
            exc_rect_local = pygame.Rect(8 + half_w + 4, filter_y, w - 16 - half_w - 4, 19)

            inc_act = (self.chat_active_field == "include_regex")
            exc_act = (self.chat_active_field == "exclude_regex")
            inc_valid = (not self.chat_include_regex.strip()) or (compile_safe_chat_regex(self.chat_include_regex) is not None)
            exc_valid = (not self.chat_exclude_regex.strip()) or (compile_safe_chat_regex(self.chat_exclude_regex) is not None)

            pygame.draw.rect(panel_surf, (14, 20, 30), inc_rect_local, border_radius=3)
            inc_bd = (255, 85, 85) if not inc_valid else ((80, 225, 255) if inc_act else (55, 75, 100))
            pygame.draw.rect(panel_surf, inc_bd, inc_rect_local, 1, border_radius=3)
            inc_txt = f"Incl RegEx: {self.chat_include_regex}" + ("|" if (inc_act and cursor_on) else "")
            panel_surf.blit(self.font_sm_thin.render(inc_txt[:max(10, (inc_rect_local.width - 10) // 7)], True, (190, 235, 255) if inc_act or self.chat_include_regex else (120, 140, 165)), (inc_rect_local.x + 5, inc_rect_local.y + 2))
            self.chat_btn_rects["input_include_regex"] = pygame.Rect(px + inc_rect_local.x, py + inc_rect_local.y, inc_rect_local.width, inc_rect_local.height)

            pygame.draw.rect(panel_surf, (14, 20, 30), exc_rect_local, border_radius=3)
            exc_bd = (255, 85, 85) if not exc_valid else ((255, 175, 80) if exc_act else (55, 75, 100))
            pygame.draw.rect(panel_surf, exc_bd, exc_rect_local, 1, border_radius=3)
            exc_txt = f"Excl RegEx: {self.chat_exclude_regex}" + ("|" if (exc_act and cursor_on) else "")
            panel_surf.blit(self.font_sm_thin.render(exc_txt[:max(10, (exc_rect_local.width - 10) // 7)], True, (255, 210, 175) if exc_act or self.chat_exclude_regex else (120, 140, 165)), (exc_rect_local.x + 5, exc_rect_local.y + 2))
            self.chat_btn_rects["input_exclude_regex"] = pygame.Rect(px + exc_rect_local.x, py + exc_rect_local.y, exc_rect_local.width, exc_rect_local.height)

            # Permanent Chat notice banner (always visible regardless of Clear, Mute, Scroll, or RegEx filters)
            notice_text = getattr(self, "chat_permanent_notice", CHAT_PERMANENT_NOTICE)
            notice_y = 49
            max_notice_w = max(80, w - 26)
            notice_lines: List[str] = []
            cur_line = ""
            for word in notice_text.split(" "):
                cand = f"{cur_line} {word}".strip() if cur_line else word
                if self.font_sm_thin.size(cand)[0] <= max_notice_w or not cur_line:
                    cur_line = cand
                else:
                    notice_lines.append(cur_line)
                    cur_line = word
            if cur_line:
                notice_lines.append(cur_line)
            if not notice_lines:
                notice_lines = [notice_text]
            notice_line_h = 14
            notice_h = len(notice_lines) * notice_line_h + 6
            pygame.draw.rect(panel_surf, (44, 34, 14, 225), (8, notice_y, w - 16, notice_h), border_radius=4)
            pygame.draw.rect(panel_surf, (175, 130, 48), (8, notice_y, w - 16, notice_h), 1, border_radius=4)
            for n_i, n_line in enumerate(notice_lines):
                n_surf = self.font_sm_thin.render(n_line, True, (255, 222, 128))
                panel_surf.blit(n_surf, (13, notice_y + 3 + n_i * notice_line_h))

            # Message list area
            msg_top_y = notice_y + notice_h + 3
            input_bar_h = 26
            msg_bot_y = h - input_bar_h - 6
            msg_area_h = max(24, msg_bot_y - msg_top_y)
            row_h = 18
            avail_rows = max(1, msg_area_h // row_h)
            self.chat_avail_rows = avail_rows

            pygame.draw.rect(panel_surf, (10, 14, 22, 215), (8, msg_top_y, w - 16, msg_area_h), border_radius=4)
            pygame.draw.rect(panel_surf, (42, 58, 80), (8, msg_top_y, w - 16, msg_area_h), 1, border_radius=4)

            if self.chat_mute_all:
                mute_msg = "CHAT MUTED (Click [Mute:ON] to unmute)"
                msurf = self.font_sm_bold.render(mute_msg, True, (255, 120, 120))
                panel_surf.blit(msurf, (max(14, (w - msurf.get_width()) // 2), msg_top_y + msg_area_h // 2 - 8))
            else:
                filtered_msgs = self.get_filtered_chat_messages(state)
                max_off = max(0, len(filtered_msgs) - avail_rows)
                self.chat_scroll_offset = max(0, min(max_off, self.chat_scroll_offset))
                end_idx = len(filtered_msgs) - self.chat_scroll_offset
                start_idx = max(0, end_idx - avail_rows)
                visible_msgs = filtered_msgs[start_idx:end_idx]

                for r_i, m in enumerate(visible_msgs):
                    ry = msg_top_y + 3 + r_i * row_h
                    if ry + row_h > msg_bot_y:
                        break
                    t_str = str(m.get("time_str") or "00:00:00")
                    sender_id = str(m.get("sender_identity") or m.get("sender") or "Unknown")
                    sender_disp = self.chat_identity_store.resolve_display_name(sender_id)
                    targets = m.get("target_identities") or []
                    is_dm = len(targets) > 0

                    x_cur = 12
                    # Timestamp
                    ts_surf = self.font_sm_thin.render(f"[{t_str}] ", True, (130, 150, 175))
                    panel_surf.blit(ts_surf, (x_cur, ry))
                    x_cur += ts_surf.get_width()

                    # Clickable Sender Identity/Name badge
                    s_col = (255, 210, 95) if is_dm else (95, 225, 255)
                    s_surf = self.font_sm_bold.render(f"<{sender_disp}>: ", True, s_col)
                    panel_surf.blit(s_surf, (x_cur, ry))
                    self.chat_sender_rects.append((pygame.Rect(px + x_cur, py + ry, s_surf.get_width(), row_h), sender_id))
                    x_cur += s_surf.get_width()

                    # Rich text message body with @<identity> -> @<name> resolution
                    disp_body = self.chat_identity_store.replace_identities_with_names_in_text(str(m.get("text") or ""))
                    spans = parse_chat_formatted_spans(disp_body)
                    max_x = w - 14
                    for sp in spans:
                        if x_cur >= max_x:
                            break
                        fnt = self._get_chat_font(bold=sp["bold"], italic=sp["italic"], underline=sp["underline"])
                        avail_px = max_x - x_cur
                        txt_chunk = sp["text"]
                        if fnt.size(txt_chunk)[0] > avail_px:
                            est_chars = max(1, int(avail_px / 7.2))
                            txt_chunk = txt_chunk[:est_chars]
                        sp_surf = fnt.render(txt_chunk, True, sp["color"])
                        panel_surf.blit(sp_surf, (x_cur, ry))
                        x_cur += sp_surf.get_width()

            # Bottom bar: formatting buttons [B] [I] [U] [Col] + text input field + [Send]
            bot_y = h - input_bar_h - 4
            bx_left = 8
            for fk, flbl in (("fmt_b", "B"), ("fmt_i", "I"), ("fmt_u", "U"), ("fmt_color", "Col")):
                fs = self.font_sm_bold.render(flbl, True, (225, 235, 250))
                fbw = fs.get_width() + 8
                r_loc = pygame.Rect(bx_left, bot_y + 2, fbw, 20)
                pygame.draw.rect(panel_surf, (34, 48, 68), r_loc, border_radius=3)
                pygame.draw.rect(panel_surf, COLOR_HUD_BORDER, r_loc, 1, border_radius=3)
                panel_surf.blit(fs, (bx_left + 4, bot_y + 4))
                self.chat_btn_rects[fk] = pygame.Rect(px + bx_left, py + bot_y + 2, fbw, 20)
                bx_left += fbw + 3

            send_w = 46
            send_x = w - send_w - 18
            send_loc = pygame.Rect(send_x, bot_y + 2, send_w, 20)
            pygame.draw.rect(panel_surf, (32, 118, 72), send_loc, border_radius=4)
            pygame.draw.rect(panel_surf, (85, 225, 140), send_loc, 1, border_radius=4)
            s_lbl = self.font_sm_bold.render("Send", True, (255, 255, 255))
            panel_surf.blit(s_lbl, (send_x + (send_w - s_lbl.get_width()) // 2, bot_y + 4))
            self.chat_btn_rects["send"] = pygame.Rect(px + send_x, py + bot_y + 2, send_w, 20)

            inp_w = max(60, send_x - bx_left - 4)
            inp_loc = pygame.Rect(bx_left, bot_y + 2, inp_w, 20)
            msg_act = (self.chat_active_field == "message")
            pygame.draw.rect(panel_surf, (12, 16, 24), inp_loc, border_radius=4)
            pygame.draw.rect(panel_surf, (80, 230, 255) if msg_act else (65, 85, 110), inp_loc, 2 if msg_act else 1, border_radius=4)
            if self.chat_input_buffer or msg_act:
                max_inp_chars = max(8, (inp_w - 12) // 7)
                disp_inp = self.chat_input_buffer[-max_inp_chars:] + ("|" if (msg_act and cursor_on) else "")
                panel_surf.blit(self.font_sm.render(disp_inp, True, (245, 250, 255)), (inp_loc.x + 6, inp_loc.y + 3))
            else:
                ph = "Type message (@name, **bold**, [red]..."
                panel_surf.blit(self.font_sm_thin.render(ph[:max(8, (inp_w - 12) // 7)], True, (110, 125, 145)), (inp_loc.x + 6, inp_loc.y + 3))
            self.chat_btn_rects["input_message"] = pygame.Rect(px + inp_loc.x, py + inp_loc.y, inp_loc.width, inp_loc.height)

            self._draw_panel_resize_grip(panel_surf, chat_panel.get("id", "SYS_CHAT"), px, py)
            self.screen.blit(panel_surf, (px, py))

    def draw_hovered_train_locator(self, state: dict):
        """Draws visual indicators on the layout canvas to pinpoint where the train is when hovering over its row in Train Scroller."""
        if not self.hovered_train or not isinstance(state, dict):
            return
        if self.train_assign_active or self.show_coverage_map:
            return

        trn = self.hovered_train
        elements = state.get("elements", {})
        if not elements:
            return

        # 1. Resolve target element id
        target_id = trn.get("current_element_id")
        if not target_id or target_id not in elements:
            target_id = trn.get("last_element_id")
        if not target_id or target_id not in elements:
            loc_str = str(trn.get("last_location", "")).strip()
            cand = loc_str.split()[0] if loc_str else ""
            if cand in elements:
                target_id = cand
        if not target_id or target_id not in elements:
            target_id = trn.get("current_signal_id")
        if not target_id or target_id not in elements:
            target_id = trn.get("initial_signal_id")
        if not target_id or target_id not in elements:
            for pid in trn.get("path_element_ids", []):
                if pid in elements:
                    target_id = pid
                    break

        if not target_id or target_id not in elements:
            return

        target_el = elements[target_id]
        lx, ly = self.get_elem_logical_pos(target_el)
        sx, sy = self.logical_to_screen(lx, ly)

        st = trn.get("status", "Confirmed")
        is_dimmed = trn.get("is_dimmed", False)
        now = time.time()
        pulse = (now * 2.5) % 1.0

        if is_dimmed:
            theme_rgb = (165, 175, 190)
            badge_bg = (30, 35, 45, 225)
        elif st == "Potential":
            theme_rgb = (255, 210, 60)
            badge_bg = (50, 42, 12, 230)
        elif st == "Inferred":
            theme_rgb = (70, 210, 255)
            badge_bg = (14, 38, 58, 230)
        else:
            theme_rgb = (50, 255, 160)
            badge_bg = (12, 48, 32, 230)

        overlay_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)

        # Check if train is off-screen
        is_offscreen = (sx < 25 or sx > self.width - 25 or sy < 35 or sy > self.height - 35)

        if is_offscreen:
            csx = max(40, min(self.width - 40, sx))
            csy = max(60, min(self.height - 60, sy))

            # Leader line towards edge
            if self.hovered_train_rect:
                rr = self.hovered_train_rect
                ax = rr.left if csx < rr.left else (rr.right if csx > rr.right else rr.centerx)
                ay = rr.centery
                pygame.draw.line(overlay_surf, (*theme_rgb, 60), (ax, ay), (csx, csy), 3)
                pygame.draw.line(overlay_surf, (255, 255, 255, 170), (ax, ay), (csx, csy), 1)
                pygame.draw.circle(overlay_surf, (*theme_rgb, 230), (ax, ay), 4)

            # Draw edge beacon & pointing triangle towards (sx, sy)
            angle = math.atan2(sy - csy, sx - csx)
            arr_len = 16
            tip_x = csx + math.cos(angle) * arr_len
            tip_y = csy + math.sin(angle) * arr_len
            p1_x = csx + math.cos(angle + 2.5) * (arr_len * 0.7)
            p1_y = csy + math.sin(angle + 2.5) * (arr_len * 0.7)
            p2_x = csx + math.cos(angle - 2.5) * (arr_len * 0.7)
            p2_y = csy + math.sin(angle - 2.5) * (arr_len * 0.7)
            pygame.draw.polygon(overlay_surf, (*theme_rgb, 240), [(tip_x, tip_y), (p1_x, p1_y), (p2_x, p2_y)])

            disp_tname = trn.get("assigned_name") or trn.get("train_name", "TRN")
            tag_txt = f"{disp_tname} (OFF-SCREEN)"
            tag_surf = self.font_sm_bold.render(tag_txt, True, theme_rgb)
            tw, th = tag_surf.get_width() + 14, tag_surf.get_height() + 8
            tx = max(10, min(self.width - tw - 10, csx - tw // 2))
            ty = max(35, min(self.height - th - 35, csy - th - 10))
            pygame.draw.rect(overlay_surf, badge_bg, (tx, ty, tw, th), border_radius=4)
            pygame.draw.rect(overlay_surf, (*theme_rgb, 220), (tx, ty, tw, th), 1, border_radius=4)
            overlay_surf.blit(tag_surf, (tx + 7, ty + 4))

            self.screen.blit(overlay_surf, (0, 0))
            return

        # On-screen highlighting:
        # A. Highlight path elements if available
        path_ids = trn.get("path_element_ids", [])
        if path_ids:
            for pid in path_ids:
                if pid != target_id and pid in elements:
                    pel = elements[pid]
                    plx, ply = self.get_elem_logical_pos(pel)
                    psx, psy = self.logical_to_screen(plx, ply)
                    p_rad = max(4, int(6 * self.zoom))
                    pygame.draw.circle(overlay_surf, (*theme_rgb, 60), (psx, psy), p_rad + 3)
                    pygame.draw.circle(overlay_surf, (*theme_rgb, 120), (psx, psy), p_rad, 2)

        # B. If target element is track, highlight entire track bar
        if target_el.get("elem_type") == "track":
            p0, p1 = self._get_track_endpoints_logical(target_el)
            s0 = self.logical_to_screen(p0[0], p0[1])
            s1 = self.logical_to_screen(p1[0], p1[1])
            bar_w = max(4, int(6 * self.zoom))
            pygame.draw.line(overlay_surf, (*theme_rgb, 70), s0, s1, bar_w + 12)
            pygame.draw.line(overlay_surf, (*theme_rgb, 140), s0, s1, bar_w + 6)
            pygame.draw.line(overlay_surf, (255, 255, 255, 230), s0, s1, bar_w + 2)

        # C. Pulsating radar reticle at (sx, sy)
        base_r = max(10, int(15 * self.zoom))
        ping_r = base_r + int(24 * pulse * self.zoom)
        ping_alpha = max(0, int(220 * (1.0 - pulse)))
        pygame.draw.circle(overlay_surf, (*theme_rgb, ping_alpha), (sx, sy), ping_r, 2)

        pygame.draw.circle(overlay_surf, (*theme_rgb, 80), (sx, sy), base_r + 4)
        pygame.draw.circle(overlay_surf, (*theme_rgb, 240), (sx, sy), base_r + 1, 2)
        pygame.draw.circle(overlay_surf, (255, 255, 255, 230), (sx, sy), max(2, int(3 * self.zoom)))

        # Reticle crosshair ticks
        tick_len = max(5, int(8 * self.zoom))
        gap = base_r + 3
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx, sy - gap - tick_len), (sx, sy - gap), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx, sy + gap), (sx, sy + gap + tick_len), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx - gap - tick_len, sy), (sx - gap, sy), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx + gap, sy), (sx + gap + tick_len, sy), 2)

        # D. Leader guide line from train scroller row to target element
        if self.hovered_train_rect:
            rr = self.hovered_train_rect
            if sx < rr.left:
                ax, ay = rr.left, rr.centery
            elif sx > rr.right:
                ax, ay = rr.right, rr.centery
            else:
                ax, ay = rr.centerx, rr.top if sy < rr.top else rr.bottom

            pygame.draw.line(overlay_surf, (*theme_rgb, 60), (ax, ay), (sx, sy), 3)
            pygame.draw.line(overlay_surf, (255, 255, 255, 170), (ax, ay), (sx, sy), 1)
            pygame.draw.circle(overlay_surf, (*theme_rgb, 230), (ax, ay), 4)

        # E. Callout Box / Banner near (sx, sy)
        disp_name = trn.get("assigned_name") or trn.get("train_name", "TRN")
        if trn.get("assigned_id"):
            disp_name = f"{disp_name} (#{trn['assigned_id']})"
        spd_val = trn.get("speed_mph")
        spd_str = f"{spd_val:.1f} MPH" if spd_val is not None else trn.get("speed_str", "Unk.")
        dir_str = str(trn.get("direction") or "")
        loc_str = str(trn.get("last_location") or target_id)
        rx_tag = " [● RX ACTIVE]" if trn.get("is_receiving_packets") else ""
        dim_str = " (DIMMED/TIMEOUT)" if is_dimmed else ""

        line1 = f"TRAIN: {disp_name}{rx_tag}{dim_str}"
        line2 = f"STATUS: {st.upper()} | SPEED: {spd_str} {dir_str} | AT: {loc_str}"

        surf1 = self.font_sm_bold.render(line1, True, (255, 255, 255))
        surf2 = self.font_sm_thin.render(line2, True, theme_rgb)

        box_w = max(surf1.get_width(), surf2.get_width()) + 20
        box_h = surf1.get_height() + surf2.get_height() + 14

        box_x = sx - box_w // 2
        box_y = sy - base_r - box_h - 12
        if box_y < 40:
            box_y = sy + base_r + 14
        box_x = max(10, min(self.width - box_w - 10, box_x))
        box_y = max(35, min(self.height - box_h - 35, box_y))

        pygame.draw.rect(overlay_surf, badge_bg, (box_x, box_y, box_w, box_h), border_radius=6)
        pygame.draw.rect(overlay_surf, (*theme_rgb, 240), (box_x, box_y, box_w, box_h), 2, border_radius=6)
        pygame.draw.rect(overlay_surf, (255, 255, 255, 120), (box_x + 1, box_y + 1, box_w - 2, box_h - 2), 1, border_radius=5)

        overlay_surf.blit(surf1, (box_x + 10, box_y + 6))
        overlay_surf.blit(surf2, (box_x + 10, box_y + 8 + surf1.get_height()))

        if box_y < sy:
            ptr_pts = [(sx, sy - base_r - 2), (sx - 6, box_y + box_h), (sx + 6, box_y + box_h)]
        else:
            ptr_pts = [(sx, sy + base_r + 2), (sx - 6, box_y), (sx + 6, box_y)]
        pygame.draw.polygon(overlay_surf, (*theme_rgb, 240), ptr_pts)

        self.screen.blit(overlay_surf, (0, 0))

    def _find_layout_elements_for_wiu(self, wid: Any, elements: dict) -> List[Tuple[str, dict]]:
        if wid is None or not elements:
            return []
        matches = []
        wid_str = str(wid).strip().lower()
        wid_int = None
        try:
            wid_int = int(wid)
        except (ValueError, TypeError):
            pass

        for eid, el in elements.items():
            if not isinstance(el, dict):
                continue
            matched = False
            # Check wiu_id
            w = el.get("wiu_id")
            if w is not None:
                if wid_int is not None:
                    try:
                        if int(w) == wid_int:
                            matched = True
                    except (ValueError, TypeError):
                        pass
                if not matched and str(w).strip().lower() == wid_str:
                    matched = True

            # Check radio_id / radioid
            if not matched:
                r = el.get("radio_id", el.get("radioid"))
                if r is not None:
                    if wid_int is not None:
                        try:
                            if int(r) == wid_int:
                                matched = True
                        except (ValueError, TypeError):
                            pass
                    if not matched and str(r).strip().lower() == wid_str:
                        matched = True

            # Check direct element id
            if not matched and str(eid).strip().lower() == wid_str:
                matched = True

            if matched:
                matches.append((eid, el))

        # Sort matches so primary elements come first: base > signal > turnout > track > display
        prio_map = {"base": 0, "signal": 1, "turnout": 2, "track": 3, "crossing": 4}
        matches.sort(key=lambda item: prio_map.get(item[1].get("elem_type", ""), 99))
        return matches

    def draw_hovered_wiu_locator(self, state: dict):
        """Draws visual indicators on the layout canvas to pinpoint layout elements corresponding to the hovered WIU in the address table."""
        if not self.hovered_wiu_row or not isinstance(state, dict):
            return
        if self.train_assign_active or self.show_coverage_map:
            return

        elements = state.get("elements", {})
        if not elements:
            return

        wid = self.hovered_wiu_row.get("wid")
        winfo = self.hovered_wiu_row.get("winfo", {})
        disp_wiu = self.hovered_wiu_row.get("disp_wiu") or str(wid)

        matches = self._find_layout_elements_for_wiu(wid, elements)
        if not matches:
            return

        primary_id, primary_el = matches[0]
        lx, ly = self.get_elem_logical_pos(primary_el)
        sx, sy = self.logical_to_screen(lx, ly)

        theme_rgb = (0, 230, 255)  # Cyan
        badge_bg = (14, 38, 58, 230)
        now = time.time()
        pulse = (now * 2.5) % 1.0

        overlay_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)

        # Check if primary element is off-screen
        is_offscreen = (sx < 25 or sx > self.width - 25 or sy < 35 or sy > self.height - 35)

        if is_offscreen:
            csx = max(40, min(self.width - 40, sx))
            csy = max(60, min(self.height - 60, sy))

            # Leader line towards edge
            if self.hovered_wiu_row_rect:
                rr = self.hovered_wiu_row_rect
                ax = rr.left if csx < rr.left else (rr.right if csx > rr.right else rr.centerx)
                ay = rr.centery
                pygame.draw.line(overlay_surf, (*theme_rgb, 60), (ax, ay), (csx, csy), 3)
                pygame.draw.line(overlay_surf, (255, 255, 255, 170), (ax, ay), (csx, csy), 1)
                pygame.draw.circle(overlay_surf, (*theme_rgb, 230), (ax, ay), 4)

            angle = math.atan2(sy - csy, sx - csx)
            arr_len = 16
            tip_x = csx + math.cos(angle) * arr_len
            tip_y = csy + math.sin(angle) * arr_len
            p1_x = csx + math.cos(angle + 2.5) * (arr_len * 0.7)
            p1_y = csy + math.sin(angle + 2.5) * (arr_len * 0.7)
            p2_x = csx + math.cos(angle - 2.5) * (arr_len * 0.7)
            p2_y = csy + math.sin(angle - 2.5) * (arr_len * 0.7)
            pygame.draw.polygon(overlay_surf, (*theme_rgb, 240), [(tip_x, tip_y), (p1_x, p1_y), (p2_x, p2_y)])

            tag_txt = f"WIU {disp_wiu} ({primary_id}) (OFF-SCREEN)"
            tag_surf = self.font_sm_bold.render(tag_txt, True, theme_rgb)
            tw, th = tag_surf.get_width() + 14, tag_surf.get_height() + 8
            tx = max(10, min(self.width - tw - 10, csx - tw // 2))
            ty = max(35, min(self.height - th - 35, csy - th - 10))
            pygame.draw.rect(overlay_surf, badge_bg, (tx, ty, tw, th), border_radius=4)
            pygame.draw.rect(overlay_surf, (*theme_rgb, 220), (tx, ty, tw, th), 1, border_radius=4)
            overlay_surf.blit(tag_surf, (tx + 7, ty + 4))

            self.screen.blit(overlay_surf, (0, 0))
            return

        # On-screen highlighting:
        # A. Highlight all matching elements
        for mid, mel in matches:
            if mid != primary_id:
                mlx, mly = self.get_elem_logical_pos(mel)
                msx, msy = self.logical_to_screen(mlx, mly)
                m_rad = max(4, int(6 * self.zoom))
                pygame.draw.circle(overlay_surf, (*theme_rgb, 60), (msx, msy), m_rad + 3)
                pygame.draw.circle(overlay_surf, (*theme_rgb, 140), (msx, msy), m_rad, 2)
            if mel.get("elem_type") == "track":
                p0, p1 = self._get_track_endpoints_logical(mel)
                s0 = self.logical_to_screen(p0[0], p0[1])
                s1 = self.logical_to_screen(p1[0], p1[1])
                bar_w = max(4, int(6 * self.zoom))
                pygame.draw.line(overlay_surf, (*theme_rgb, 70), s0, s1, bar_w + 10)
                pygame.draw.line(overlay_surf, (255, 255, 255, 200), s0, s1, bar_w + 2)

        # B. Pulsating radar reticle at primary element (sx, sy)
        base_r = max(10, int(15 * self.zoom))
        ping_r = base_r + int(24 * pulse * self.zoom)
        ping_alpha = max(0, int(220 * (1.0 - pulse)))
        pygame.draw.circle(overlay_surf, (*theme_rgb, ping_alpha), (sx, sy), ping_r, 2)
        pygame.draw.circle(overlay_surf, (*theme_rgb, 80), (sx, sy), base_r + 4)
        pygame.draw.circle(overlay_surf, (*theme_rgb, 240), (sx, sy), base_r + 1, 2)
        pygame.draw.circle(overlay_surf, (255, 255, 255, 230), (sx, sy), max(2, int(3 * self.zoom)))

        # Crosshair ticks
        tick_len = max(5, int(8 * self.zoom))
        gap = base_r + 3
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx, sy - gap - tick_len), (sx, sy - gap), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx, sy + gap), (sx, sy + gap + tick_len), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx - gap - tick_len, sy), (sx - gap, sy), 2)
        pygame.draw.line(overlay_surf, (*theme_rgb, 220), (sx + gap, sy), (sx + gap + tick_len, sy), 2)

        # C. Leader guide line from WIU table row to primary element
        if self.hovered_wiu_row_rect:
            rr = self.hovered_wiu_row_rect
            if sx < rr.left:
                ax, ay = rr.left, rr.centery
            elif sx > rr.right:
                ax, ay = rr.right, rr.centery
            else:
                ax, ay = rr.centerx, rr.top if sy < rr.top else rr.bottom

            pygame.draw.line(overlay_surf, (*theme_rgb, 60), (ax, ay), (sx, sy), 3)
            pygame.draw.line(overlay_surf, (255, 255, 255, 170), (ax, ay), (sx, sy), 1)
            pygame.draw.circle(overlay_surf, (*theme_rgb, 230), (ax, ay), 4)

        # D. Callout Banner near (sx, sy)
        target_etype = primary_el.get("elem_type", "element").upper()
        avg_r = winfo.get("avg_rssi", primary_el.get("avg_rssi", primary_el.get("rssi_avg")))
        avg_str = f"{float(avg_r):.1f} dBm" if avg_r is not None else "---"
        last_r = winfo.get("last_rssi", primary_el.get("last_rssi"))
        last_str = f"{float(last_r):.1f} dBm" if last_r is not None else "---"
        pkt_cnt = winfo.get("packet_count", primary_el.get("packets_received", 0))
        chs = ",".join(map(str, winfo.get("channels", []))) or str(primary_el.get("channel", "---"))

        line1 = f"WIU: {disp_wiu} (ID: {wid})"
        line2 = f"ELEMENT: {primary_id} ({target_etype}) | RSSI AVG: {avg_str} | LAST: {last_str}"
        line3 = f"PACKETS: {pkt_cnt} | CHANNELS: {chs}" + (f" | +{len(matches) - 1} linked" if len(matches) > 1 else "")

        surf1 = self.font_sm_bold.render(line1, True, (255, 255, 255))
        surf2 = self.font_sm_thin.render(line2, True, theme_rgb)
        surf3 = self.font_sm_thin.render(line3, True, (180, 230, 255))

        box_w = max(surf1.get_width(), surf2.get_width(), surf3.get_width()) + 20
        box_h = surf1.get_height() + surf2.get_height() + surf3.get_height() + 16

        box_x = sx - box_w // 2
        box_y = sy - base_r - box_h - 12
        if box_y < 40:
            box_y = sy + base_r + 14
        box_x = max(10, min(self.width - box_w - 10, box_x))
        box_y = max(35, min(self.height - box_h - 35, box_y))

        pygame.draw.rect(overlay_surf, badge_bg, (box_x, box_y, box_w, box_h), border_radius=6)
        pygame.draw.rect(overlay_surf, (*theme_rgb, 220), (box_x, box_y, box_w, box_h), 1, border_radius=6)

        overlay_surf.blit(surf1, (box_x + 10, box_y + 4))
        overlay_surf.blit(surf2, (box_x + 10, box_y + 4 + surf1.get_height() + 2))
        overlay_surf.blit(surf3, (box_x + 10, box_y + 6 + surf1.get_height() + surf2.get_height()))

        self.screen.blit(overlay_surf, (0, 0))

    @staticmethod
    def _fmt_switch_val(el: dict) -> str:
        raw = el.get("raw_switch_val")
        if raw is None:
            return f"No data (assumed {'NORMAL' if el.get('state', 0) == 0 else 'REVERSE'})"
        desc = el.get("switch_desc") or {0: "OUT OF CORRESPONDENCE", 1: "NORMAL", 2: "REVERSE", 3: "INVALID/FAULT"}.get(int(raw), "?")
        return f"{int(raw)} (0b{int(raw):02b}) - {desc}"

    @staticmethod
    def _fmt_occ_val(el: dict) -> str:
        raw = el.get("raw_occ_val")
        if raw is None:
            return "No data"
        return f"{int(raw)} - {'OCCUPIED' if int(raw) == 1 else 'CLEAR'}"

    def _build_readonly_inspector_fields(self, el: dict, ref_now: float) -> List[Tuple[str, str, bool]]:
        """Returns list of (label, value, is_telemetry_highlight) for the Read-Only Element Inspector."""
        etype = el.get("elem_type", "unknown")
        lx, ly = self.get_elem_logical_pos(el)
        lbl = self.get_elem_label(el)
        fields: List[Tuple[str, str, bool]] = [
            ("Element ID", str(el.get("id", "")), False),
            ("Element Type", str(etype), False),
            ("Display Name", str(lbl or ""), False),
            ("Label Position", str(el.get("label_pos", "top")), False),
            ("Logical X, Y", f"{lx:.1f}, {ly:.1f}", False),
        ]
        if etype == "track":
            nbr_parts = []
            if isinstance(el.get("neighbors"), dict):
                for d, nlist in el["neighbors"].items():
                    for nid in nlist:
                        nbr_parts.append(f"{d}:{nid}")
            fields.extend([
                ("Orientation", str(el.get("orientation", "E-W")), False),
                ("Track Length (Miles)", str(el.get("length_miles", 0.5)), False),
                ("Neighbors", ", ".join(nbr_parts) or "None", False),
                ("Parallel Track", str(el.get("parallel_track_id") or "None"), False),
                ("Has HW Occ Bit", str(bool(el.get("has_occ_bit", False))), False),
                ("WIU ID", str(el.get("wiu_id") or "None"), False),
                ("Occ Bit Offset", str(el.get("occ_bit_offset") if el.get("occ_bit_offset") is not None else "None"), False),
                ("CTC ID", str(el.get("ctc_id") or "None"), False),
                ("CTC Rules", str(el.get("ctc_rules") or "None"), False),
            ])
        elif etype == "turnout":
            fields.extend([
                ("WIU ID", str(el.get("wiu_id") or "None"), False),
                ("Switch Bits Offset (2b)", str(el.get("cmd_bit_offset") if el.get("cmd_bit_offset") is not None else "None"), False),
                ("Switch Position", self._fmt_switch_val(el), True),
                ("Point Orientation", str(el.get("orientation", "E")), False),
                ("Common Leg", str(el.get("common_neighbor") or "None"), False),
                ("Normal Leg (Dir)", f"{el.get('normal_neighbor') or 'None'} ({el.get('normal_dir', 'E')})", False),
                ("Reverse Leg (Dir)", f"{el.get('reverse_neighbor') or 'None'} ({el.get('reverse_dir', 'NE')})", False),
                ("Guarding Signals", ", ".join(el.get("guarded_by_signals", [])) or "None", False),
                ("Has HW Occ Bit", str(bool(el.get("has_occ_bit", False))), False),
                ("Occ Bit Offset", str(el.get("occ_bit_offset") if el.get("occ_bit_offset") is not None else "None"), False),
                ("CTC ID", str(el.get("ctc_id") or "None"), False),
                ("CTC Rules", str(el.get("ctc_rules") or "None"), False),
            ])
        elif etype == "signal":
            fields.extend([
                ("WIU ID", str(el.get("wiu_id") or "None"), False),
                ("Signal Bits Offset (5b)", str(el.get("sig_bit_offset") if el.get("sig_bit_offset") is not None else "None"), False),
                ("Head Position", str(el.get("position", "top")), False),
                ("Gov Direction", str(el.get("direction", "E")), False),
                ("Protects Element", str(el.get("protects_element") or "None"), False),
                ("Rear Element", str(el.get("rear_element") or "None"), False),
                ("Has HW Occ Bit", str(bool(el.get("has_occ_bit", False))), False),
                ("Occ Bit Offset", str(el.get("occ_bit_offset") if el.get("occ_bit_offset") is not None else "None"), False),
                ("Signal Override / RR", str(el.get("signal_rules") or el.get("signal_override") or "None"), False),
                ("CTC ID", str(el.get("ctc_id") or "None"), False),
                ("CTC Rules", str(el.get("ctc_rules") or "None"), False),
            ])
        elif etype == "crossing":
            fields.append(("Monitored Tracks", ", ".join(el.get("monitored_tracks", [])) or "None", False))
        elif etype == "base":
            fields.append(("Radio Channel (Config)", str(el.get("channel", 1)), False))
            rad_id = el.get("radio_id")
            fields.append(("Radio ID", str(rad_id) if rad_id is not None else "None", False))
            channels = el.get("channels") or []
            chan_details = el.get("channel_details") or {}
            detail_keys = [k for k in chan_details.keys() if re.match(r"^chan_?(\d+)$", k, re.IGNORECASE)]
            if detail_keys:
                def _c_sort(k):
                    m = re.match(r"^chan_?(\d+)$", k, re.IGNORECASE)
                    return int(m.group(1)) if m else 999
                detail_keys.sort(key=_c_sort)
                for idx, k in enumerate(detail_keys[:6]):
                    v = chan_details[k]
                    fields.append((f"Channel {idx + 1} [{k}]", _format_channel_val(v), True))
                for idx in range(len(detail_keys), 6):
                    fields.append((f"Channel {idx + 1} [chan_{idx}]", "---", True))
            elif channels:
                for idx in range(min(6, len(channels))):
                    fields.append((f"Channel {idx + 1} [chan_{idx}]", _format_channel_val(channels[idx]), True))
                for idx in range(len(channels), 6):
                    fields.append((f"Channel {idx + 1} [chan_{idx}]", "---", True))
            else:
                for idx in range(6):
                    fields.append((f"Channel {idx + 1} [chan_{idx}]", "No data (offline)", True))
        elif etype in ("ptc_datetime", "date_time", "datetime"):
            fields.extend([
                ("Last Decoded Date/Time", str(el.get("datetime_str") or el.get("last_datetime_str") or "None"), True),
                ("Year", str(el.get("year") or "None"), False),
                ("Month", str(el.get("month") or "None"), False),
                ("Day", str(el.get("day") or "None"), False),
                ("Hour", str(el.get("hour") or "None"), False),
                ("Minute", str(el.get("minute") or "None"), False),
                ("Second", str(el.get("second") or "None"), False),
                ("Source WIU / Node", str(el.get("last_source_wiu") or "None"), False),
                ("Filter WIU ID", str(el.get("wiu_id") or "Any (All Nodes)"), False),
            ])
        elif etype == "track_jump":
            fields.extend([
                ("Connector Letter", str(el.get("letter", "A")), False),
                ("Connected Element", str(el.get("connected_element") or "None"), False),
                ("Continue Direction", str(el.get("direction", "E")), False),
                ("Paired Jump ID", str(el.get("paired_jump_id") or "None"), False),
            ])
        elif etype == "address_table":
            bw, bh = self.get_panel_base_size(etype, el)
            fields.extend([
                ("Max List Rows", str(el.get("extra_info") or el.get("max_len") or 10), False),
                ("Panel Size (W x H)", f"{bw} x {bh} px", False),
            ])
            wius_dict = el.get("wius", {})
            for wid, winfo in wius_dict.items():
                raw_name = winfo.get("wiu_name") or self.lookup_wiu_name(wid)
                disp_name = winfo.get("wiu_display_name") or format_wiu_display_name(raw_name, wid)
                label_txt = f"WIU: {disp_name}" if disp_name != str(wid) else f"WIU {wid}"
                val_txt = f"ID: {wid}  (RR: {winfo.get('rr_name', 'DEF')}, Pkts: {winfo.get('packet_count', 0)})"
                fields.append((label_txt, val_txt, True))
        elif etype in ("packet_scroller", "train_scroller", "chat"):
            bw, bh = self.get_panel_base_size(etype, el)
            fields.extend([
                ("Max List Rows", str(el.get("extra_info") or el.get("max_len") or (50 if etype == "chat" else 10)), False),
                ("Panel Size (W x H)", f"{bw} x {bh} px", False),
            ])

        if etype not in ("packet_scroller", "address_table", "train_scroller", "chat"):
            mp = el.get("mile_marker")
            gps = el.get("gps")
            tx_gps = el.get("transmitted_gps")
            if tx_gps and len(tx_gps) >= 2:
                gps_str = f"{tx_gps[0]:.6f}, {tx_gps[1]:.6f} (Transmitted)"
            elif gps and len(gps) >= 2:
                gps_str = f"{gps[0]:.6f}, {gps[1]:.6f}"
            else:
                gps_str = "None"
            fields.extend([
                ("Mile Marker (MP)", str(mp) if mp is not None else "None", False),
                ("GPS (lat, lon)", gps_str, bool(tx_gps)),
            ])

        if etype in ("track", "turnout", "signal") and (el.get("has_occ_bit") or el.get("raw_occ_val") is not None):
            fields.append(("Occupancy Bit", self._fmt_occ_val(el), True))

        # Live Telemetry Fields (Total Packets Received, Time Since Last Packet, Last Status)
        pkts_rx = el.get("packets_received")
        if pkts_rx is None:
            pkts_rx = el.get("packets_relayed", el.get("total_packets", 0))
        last_ts = float(el.get("last_packet_ts", 0.0) or 0.0)
        if last_ts > 0.0:
            age_sec = max(0.0, ref_now - last_ts)
            if age_sec < 60.0:
                since_str = f"{age_sec:.1f}s ago"
            else:
                mins, secs = divmod(int(age_sec), 60)
                hrs, mins = divmod(mins, 60)
                since_str = f"{hrs:02d}:{mins:02d}:{secs:02d} ago ({age_sec:.0f}s)"
        else:
            since_str = "Never (No packets yet)"

        last_status = (
            el.get("last_status")
            or el.get("status")
            or el.get("alignment")
            or el.get("gate_status")
            or "IDLE"
        )
        fields.extend([
            ("Total Packets Received", str(pkts_rx), True),
            ("Time Since Last Packet", since_str, True),
            ("Last Status", str(last_status), True),
        ])

        avg_rssi = el.get("avg_rssi", el.get("rssi_avg"))
        last_rssi = el.get("last_rssi")
        if avg_rssi is not None:
            try:
                fields.append(("RSSI (Sliding Avg)", f"{float(avg_rssi):.1f} dBm", True))
            except (ValueError, TypeError):
                fields.append(("RSSI (Sliding Avg)", f"{avg_rssi} dBm", True))
        if last_rssi is not None:
            try:
                fields.append(("Last RSSI", f"{float(last_rssi):.1f} dBm", True))
            except (ValueError, TypeError):
                fields.append(("Last RSSI", f"{last_rssi} dBm", True))

        return fields

    def draw_readonly_inspector(self, state: Optional[dict], ref_now: float):
        if not state or not self.readonly_inspector_id:
            return
        target = self._lookup_any_element(state, self.readonly_inspector_id)
        if not target:
            self.readonly_inspector_id = None
            return

        rows = self._build_readonly_inspector_fields(target, ref_now)
        row_h = 25
        is_base = (target.get("elem_type") == "base")
        title = f"[BASE STATION] {target.get('id')} — 6 TRANSMITTED CHANNELS" if is_base else f"[READ-ONLY] ELEMENT INSPECTOR: {target.get('id')} ({target.get('elem_type', '').upper()})"

        rack_h = 44 if is_base else 0
        card_gap = 6

        lbl_w = max((self.font_sm_bold.size(r[0])[0] for r in rows), default=170) + 22
        val_w = max((self.font_sm_bold.size(r[1])[0] for r in rows), default=220) + 26
        title_w = self.font_md.size(title)[0] + 36

        # Raw Packet Hex String formatting (small and broken up into 16-byte lines)
        raw_hex = target.get("last_raw_hex") or target.get("raw_hex") or ""
        self.inspector_last_raw_hex = raw_hex
        hex_lines = format_hex_lines(raw_hex, bytes_per_line=16) if raw_hex else []
        hex_display_lines = hex_lines if hex_lines else ["(No raw packet received for this element yet)"]
        hex_line_h = 16
        hex_inner_h = max(26, len(hex_display_lines) * hex_line_h + 8)
        hex_section_h = 24 + hex_inner_h + 8

        min_w = 540 if is_base else 490
        panel_w = min(self.width - 24, max(min_w, lbl_w + val_w + 28, title_w))
        box_w = panel_w - lbl_w - 24
        panel_h = 78 + (rack_h + 10 if is_base else 0) + len(rows) * row_h + hex_section_h

        if self.inspector_pos is None:
            px, py = (self.width - panel_w) // 2, (self.height - panel_h) // 2
        else:
            px, py = self.inspector_pos
        px = min(max(12, px), max(12, self.width - panel_w - 12))
        py = min(max(54, py), max(54, self.height - panel_h - 42))
        self.inspector_pos = (px, py)
        self.inspector_rect = pygame.Rect(px, py, panel_w, panel_h)

        surf = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        surf.fill((18, 24, 34, 242))
        pygame.draw.rect(surf, COLOR_SELECT_RING, (0, 0, panel_w, panel_h), 2, border_radius=7)
        self.screen.blit(surf, (px, py))

        self.screen.blit(self.font_md.render(title, True, COLOR_EDIT_ACCENT), (px + 12, py + 8))
        self.inspector_edit_rects.clear()
        self.inspector_apply_ovr_rect = pygame.Rect(0, 0, 0, 0)
        self.inspector_revert_ovr_rect = pygame.Rect(0, 0, 0, 0)
        editable_map = {
            "WIU ID": "wiu_id",
            "Occ Bit Offset": "occ_bit_offset",
            "Switch Bits Offset (2b)": "cmd_bit_offset",
            "Signal Bits Offset (5b)": "sig_bit_offset",
        }
        has_editable = False

        if is_base:
            channels = target.get("channels") or []
            chan_details = target.get("channel_details") or {}
            detail_keys = [k for k in chan_details.keys() if re.match(r"^chan_?(\d+)$", k, re.IGNORECASE)]
            if detail_keys:
                def _c_sort(k):
                    m = re.match(r"^chan_?(\d+)$", k, re.IGNORECASE)
                    return int(m.group(1)) if m else 999
                detail_keys.sort(key=_c_sort)

            card_w = max(40, (panel_w - 24 - 5 * card_gap) // 6)
            for c_idx in range(6):
                cx = px + 12 + c_idx * (card_w + card_gap)
                cy = py + 34
                c_rect = pygame.Rect(cx, cy, card_w, rack_h)

                if detail_keys and c_idx < len(detail_keys):
                    k_name = detail_keys[c_idx]
                    raw_val = chan_details[k_name]
                elif c_idx < len(channels):
                    k_name = f"chan_{c_idx}"
                    raw_val = channels[c_idx]
                else:
                    k_name = f"chan_{c_idx}"
                    raw_val = None

                val_str = _format_channel_val(raw_val)
                has_val = (raw_val is not None and val_str not in ("---", "No data (offline)"))

                bg_col = (20, 52, 62) if has_val else (24, 28, 38)
                bd_col = (65, 215, 245) if has_val else (50, 60, 75)
                tx_col = (110, 245, 175) if has_val else (130, 140, 155)

                pygame.draw.rect(self.screen, bg_col, c_rect, border_radius=4)
                pygame.draw.rect(self.screen, bd_col, c_rect, 1, border_radius=4)

                hdr_surf = self.font_sm_bold.render(f"CH {c_idx + 1}", True, (255, 230, 100) if has_val else (140, 150, 165))
                self.screen.blit(hdr_surf, (cx + (card_w - hdr_surf.get_width()) // 2, cy + 4))

                disp_c = val_str[:9]
                val_surf = self.font_sm_bold.render(disp_c, True, tx_col)
                self.screen.blit(val_surf, (cx + (card_w - val_surf.get_width()) // 2, cy + 22))

        for idx, (flbl, fval, is_telem) in enumerate(rows):
            fy = py + 34 + (rack_h + 10 if is_base else 0) + idx * row_h
            edit_key = editable_map.get(flbl)
            if edit_key is not None:
                has_editable = True
            is_active_fld = (edit_key is not None and self.inspector_active_field == edit_key)
            l_col = COLOR_EDIT_ACCENT if edit_key else (COLOR_LANDMARK if is_telem else COLOR_TEXT_DIM)
            lbl_txt = f"{flbl} [ Temp Edit ]" if edit_key else flbl
            self.screen.blit(self.font_sm_bold.render(lbl_txt, True, l_col), (px + 12, fy + 4))

            box_rect = pygame.Rect(px + lbl_w, fy, box_w, row_h - 3)
            if edit_key:
                self.inspector_edit_rects[edit_key] = box_rect
                if edit_key not in self.inspector_edit_buffers:
                    raw_v = target.get(edit_key)
                    self.inspector_edit_buffers[edit_key] = str(raw_v) if raw_v is not None else ""
                disp_v = self.inspector_edit_buffers.get(edit_key, "")
                if is_active_fld:
                    disp_v = f"{disp_v}_"
                bg_c = (44, 58, 86) if is_active_fld else (30, 42, 62)
                bd_c = COLOR_EDIT_ACCENT if is_active_fld else COLOR_SELECT_RING
                v_col = (255, 235, 120) if is_active_fld else (150, 230, 255)
            else:
                disp_v = fval
                bg_c = (28, 42, 58) if is_telem else (24, 30, 42)
                bd_c = COLOR_SELECT_RING if is_telem else COLOR_HUD_BORDER
                v_col = (110, 245, 150) if is_telem else COLOR_TEXT
            pygame.draw.rect(self.screen, bg_c, box_rect, border_radius=4)
            pygame.draw.rect(self.screen, bd_c, box_rect, 1, border_radius=4)
            self.screen.blit(self.font_sm_bold.render(disp_v, True, v_col), (box_rect.x + 8, box_rect.y + 4))

        # Render Raw Packet Hex string section
        hex_y = py + 36 + (rack_h + 10 if is_base else 0) + len(rows) * row_h
        self.screen.blit(self.font_sm_bold.render("Last Raw Packet Hex:", True, COLOR_EDIT_ACCENT), (px + 12, hex_y + 2))
        copy_w = 84
        copy_rect = pygame.Rect(px + panel_w - copy_w - 12, hex_y - 2, copy_w, 22)
        self.inspector_copy_hex_rect = copy_rect
        copy_bg = (35, 95, 140) if raw_hex else (42, 48, 60)
        pygame.draw.rect(self.screen, copy_bg, copy_rect, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_HUD_BORDER, copy_rect, 1, border_radius=4)
        cp_txt = self.font_sm_bold.render("Copy Hex", True, (255, 255, 255) if raw_hex else (130, 140, 155))
        self.screen.blit(cp_txt, (copy_rect.x + (copy_w - cp_txt.get_width()) // 2, copy_rect.y + 4))

        # Inset container for small broken-up hex lines
        box_y = hex_y + 24
        box_w_hex = panel_w - 24
        box_rect = pygame.Rect(px + 12, box_y, box_w_hex, hex_inner_h)
        pygame.draw.rect(self.screen, (12, 16, 24), box_rect, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_HUD_BORDER, box_rect, 1, border_radius=4)

        for l_idx, h_line in enumerate(hex_display_lines):
            line_y = box_y + 4 + l_idx * hex_line_h
            line_col = (110, 245, 175) if raw_hex else COLOR_TEXT_DIM
            self.screen.blit(self.font_sm_thin.render(h_line, True, line_col), (box_rect.x + 8, line_y))

        btn_y = box_y + hex_inner_h + 10
        if has_editable:
            apply_w = 155
            rev_w = 145
            self.inspector_apply_ovr_rect = pygame.Rect(px + 12, btn_y, apply_w, 28)
            self.inspector_revert_ovr_rect = pygame.Rect(px + 12 + apply_w + 8, btn_y, rev_w, 28)
            pygame.draw.rect(self.screen, (32, 118, 72), self.inspector_apply_ovr_rect, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, self.inspector_apply_ovr_rect, 1, border_radius=4)
            aps = self.font_sm_bold.render("Apply Temp (Enter)", True, (255, 255, 255))
            self.screen.blit(aps, (self.inspector_apply_ovr_rect.x + (apply_w - aps.get_width()) // 2, btn_y + 7))

            has_ovr = self.readonly_inspector_id in self.temp_wiu_overrides
            rev_bg = (165, 95, 28) if has_ovr else (55, 64, 78)
            pygame.draw.rect(self.screen, rev_bg, self.inspector_revert_ovr_rect, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, self.inspector_revert_ovr_rect, 1, border_radius=4)
            rvs = self.font_sm_bold.render("Revert to Original", True, (255, 255, 255))
            self.screen.blit(rvs, (self.inspector_revert_ovr_rect.x + (rev_w - rvs.get_width()) // 2, btn_y + 7))
        else:
            hint = self.font_sm_thin.render("Details Inspector (Drag panel to move)", True, COLOR_TEXT_DIM)
            self.screen.blit(hint, (px + 12, btn_y + 7))

        close_w = 130
        close_x = px + panel_w - close_w - 12
        self.inspector_close_rect = pygame.Rect(close_x, btn_y, close_w, 28)
        pygame.draw.rect(self.screen, (135, 45, 45), self.inspector_close_rect, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_HUD_BORDER, self.inspector_close_rect, 1, border_radius=4)
        cs = self.font_sm_bold.render("Close (Esc)", True, (255, 255, 255))
        self.screen.blit(cs, (close_x + (close_w - cs.get_width()) // 2, btn_y + 7))
        hint = self.font_sm_thin.render("" if has_editable else "Details Inspector (Drag panel to move)", True, COLOR_TEXT_DIM)
        self.screen.blit(hint, (px + 12, btn_y + 7))

    def draw_gps_map_inset_if_selected(self, state: Optional[dict]):
        self.map_inset_rect = pygame.Rect(0, 0, 0, 0)
        if not state or not self.map_selected_id: return
        elements = state.get("elements", {})
        sel_el = elements.get(self.map_selected_id)
        if not sel_el or not sel_el.get("gps"): return

        target_gps = (float(sel_el["gps"][0]), float(sel_el["gps"][1]))
        all_gps_pts = [(float(el["gps"][0]), float(el["gps"][1]), eid == self.map_selected_id) for eid, el in elements.items() if el.get("gps") and len(el["gps"]) >= 2]

        map_w, map_h = 280, 210
        inset_surf = self.map_manager.render_gps_inset(target_gps, all_gps_pts, width=map_w, height=map_h)
        dest_x = self.width - map_w - 16
        dest_y = self.height - map_h - 46
        self.map_inset_rect = pygame.Rect(dest_x, dest_y, map_w, map_h)
        self.screen.blit(inset_surf, (dest_x, dest_y))
        return
        if not state or not self.selected_id: return
        elements = state.get("elements", {})
        sel_el = elements.get(self.selected_id)
        if not sel_el or not sel_el.get("gps"): return

        target_gps = (float(sel_el["gps"][0]), float(sel_el["gps"][1]))
        all_gps_pts = [(float(el["gps"][0]), float(el["gps"][1]), eid == self.selected_id) for eid, el in elements.items() if el.get("gps") and len(el["gps"]) >= 2]

        map_w, map_h = 280, 210
        inset_surf = self.map_manager.render_gps_inset(target_gps, all_gps_pts, width=map_w, height=map_h)
        dest_x = self.width - map_w - 16
        dest_y = self.height - map_h - 46
        self.screen.blit(inset_surf, (dest_x, dest_y))

    def draw_hud(self, state: Optional[dict]):
        bar_h = 46
        hud_surf = pygame.Surface((self.width, bar_h), pygame.SRCALPHA)
        hud_surf.fill((18, 22, 32, 240))
        pygame.draw.line(hud_surf, COLOR_HUD_BORDER, (0, bar_h - 1), (self.width, bar_h - 1), 2)
        self.screen.blit(hud_surf, (0, 0))

        self.hud_buttons.clear()
        is_logging = bool(state.get("raw_packet_logging")) if state else False
        flash_rec = (int(time.time() * 2.0) % 2) == 0
        rec_label = ("* REC LOG" if flash_rec else "* REC LOG") if is_logging else "RawLog:Off"

        btn_specs = [
            ("prev", "<< Prev [", 12, 78),
            ("pause", "Resume (Space)" if self.paused else "Pause (Space)", 96, 125),
            ("next", "Next ] >>", 227, 78),
            ("live", "LIVE (L)", 311, 74),
            ("edit", f"Edit: {'ON' if self.edit_mode else 'OFF'} (E)", 395, 110),
            ("save", "Save Prefs (S)", 511, 114),
            ("toggle_labels", "Lbls(On)" if self.show_labels else "Lbls(Off)", 633, 76),
            ("toggle_inferred", "InfTrns(On)" if self.show_inferred_trains else "InfTrns(Off)", 715, 96),
            ("toggle_raw_log", rec_label, 818, 98),
            ("coverage_map", "CovMap (M)", 924, 98),
            ("banc_layouts", "CloudLayouts (B)", 1028, 120),
            ("local_layout", "Layout (O)", 1152, 96),
        ]

        for key, text, bx, bw in btn_specs:
            rect = pygame.Rect(bx, 8, bw, 30)
            self.hud_buttons[key] = rect
            active = (key == "pause" and self.paused) or (key == "edit" and self.edit_mode)
            if key == "toggle_raw_log" and is_logging:
                bg = (195, 35, 35) if flash_rec else (115, 25, 25)
            elif key == "coverage_map" and self.show_coverage_map:
                bg = (35, 140, 75)
            elif key == "banc_layouts" and self.banc_coverage_modal:
                bg = (32, 115, 125)
            elif key == "local_layout" and self.local_layout_prompt_active:
                bg = (35, 80, 135)
            elif active:
                bg = (210, 120, 25)
            else:
                bg = (38, 48, 66)
            pygame.draw.rect(self.screen, bg, rect, border_radius=5)
            pygame.draw.rect(self.screen, COLOR_HUD_BORDER, rect, 1, border_radius=5)
            t_surf = self.font_sm_bold.render(text, True, (255, 255, 255))
            self.screen.blit(t_surf, (bx + (bw - t_surf.get_width()) // 2, 15))

        hist_len = len(self.live_history)
        cur_idx = self.paused_idx + 1 if self.paused and self.paused_history else hist_len
        mode_badge = f"PAUSED COPY ({cur_idx}/{len(self.paused_history)})" if self.paused and self.paused_history else f"LIVE ({cur_idx}/{HISTORY_MAX_LEN})"
        badge_col = COLOR_EDIT_ACCENT if self.paused else COLOR_TRACK_GREEN

        ver_str = state.get("version", VERSION) if state else VERSION
        layout_name = self.prefs.layout_filename
        status_str = f"Layout: {layout_name}  |  Zoom: {self.zoom:.2f}x  |  Live Rx: {hist_len}"
        self.screen.blit(self.font_md.render(mode_badge, True, badge_col), (1256, 14))
        self.screen.blit(self.font_sm_thin.render(status_str, True, COLOR_TEXT), (1376, 16))

        footer_h = 34

        # Draw suggested local layout completion popup
        if self.local_layout_prompt_active and getattr(self, "local_layout_suggestions", None):
            suggestions = self.local_layout_suggestions
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

            hdr_lbl = self.font_sm.render("LOCAL LAYOUTS  (Tab = Complete,  \u2191\u2193 = Select,  Enter = Load,  Click to choose)", True, (130, 175, 220))
            self.screen.blit(hdr_lbl, (pop_x + 10, pop_y + 4))
            pygame.draw.line(self.screen, (40, 65, 95), (pop_x + 6, pop_y + hdr_h), (pop_x + pop_w - 6, pop_y + hdr_h), 1)

            mx_pos, my_pos = pygame.mouse.get_pos()
            self.local_layout_suggestion_rects.clear()

            for idx, cand in enumerate(suggestions):
                ry = pop_y + hdr_h + 4 + idx * row_h
                r = pygame.Rect(pop_x + 4, ry, pop_w - 8, row_h - 2)
                self.local_layout_suggestion_rects.append((r, cand))

                is_sel = (idx == self.local_layout_selected_idx)
                is_hov = r.collidepoint(mx_pos, my_pos)

                if is_sel:
                    pygame.draw.rect(self.screen, (35, 80, 135), r, border_radius=4)
                    pygame.draw.rect(self.screen, COLOR_EDIT_ACCENT, r, 1, border_radius=4)
                elif is_hov:
                    pygame.draw.rect(self.screen, (28, 48, 75), r, border_radius=4)

                tag_surf = self.font_sm_bold.render("[FILE]", True, (100, 180, 240))
                self.screen.blit(tag_surf, (r.x + 8, r.y + 3))

                txt_color = (255, 255, 255) if is_sel else ((220, 235, 255) if is_hov else COLOR_TEXT)
                item_surf = self.font_sm.render(cand, True, txt_color)
                self.screen.blit(item_surf, (r.x + 60, r.y + 3))

        foot_surf = pygame.Surface((self.width, footer_h), pygame.SRCALPHA)
        foot_surf.fill((18, 22, 32, 235))
        pygame.draw.line(foot_surf, COLOR_HUD_BORDER, (0, 0), (self.width, 0), 1)
        self.screen.blit(foot_surf, (0, self.height - footer_h))

        if self.local_layout_prompt_active:
            prompt = f"LOAD LOCAL LAYOUT > {self.local_layout_buffer}_   (Enter=Load in Engine, Esc=Cancel, Tab=Complete, Ctrl+BS=Clear)"
            self.screen.blit(self.font_md.render(prompt, True, COLOR_EDIT_ACCENT), (14, self.height - 25))
        elif self.renaming_active and self.selected_id:
            prompt = f"RENAME [{self.selected_id}] > {self.rename_buffer}_   (Enter=Save, Esc=Cancel, Ctrl+BS=Clear)"
            self.screen.blit(self.font_md.render(prompt, True, COLOR_EDIT_ACCENT), (14, self.height - 25))
        elif self.selected_id and state:
            target = self._lookup_any_element(state, self.selected_id)
            if target:
                lx, ly = self.get_elem_logical_pos(target)
                lbl = self.get_elem_label(target)
                gps_info = f" | GPS: {target['gps']}" if target.get("gps") else ""
                info = (
                    f"[{VERSION}] Selected: {self.selected_id} ({target.get('elem_type')}) | "
                    f"Label: '{lbl}' | Pos: ({lx:.1f},{ly:.1f}) | WIU: {target.get('wiu_id', 'None')}{gps_info}"
                )
                self.screen.blit(self.font_sm_bold.render(info, True, COLOR_SELECT_RING), (14, self.height - 23))
        else:
            help_txt = f"[{VERSION}] {self.status_toast}  |  Right-Click=GPS Map | Dbl-Click=Details | Home=Scroller Top"
            self.screen.blit(self.font_sm_thin.render(help_txt, True, COLOR_TEXT_DIM), (14, self.height - 23))

    def close(self):
        self._heartbeat_running = False
        self._stream_running = False
        if hasattr(self, "_stream_sock") and self._stream_sock:
            try:
                self._stream_sock.close()
            except OSError:
                pass
        try:
            self.receiver.close()
        except Exception:
            pass

    def run(self):
        running = True
        try:
            while running:
                incoming_states = self.receiver.poll_packets()
                while self._pending_stream_states:
                    try:
                        incoming_states.append(self._pending_stream_states.popleft())
                    except IndexError:
                        break
                while self._pending_heartbeat_states:
                    try:
                        incoming_states.append(self._pending_heartbeat_states.popleft())
                    except IndexError:
                        break
                if incoming_states:
                    self.last_packet_time = time.time()
                for st in incoming_states:
                    self.total_packets_received += 1
                    self._record_live_state(st)

                running = self.handle_events()
                flash_on = (int(time.time() * 4.0) % 2) == 0

                self.screen.fill(COLOR_BG)
                self.draw_grid()

                state = self.current_state()
                if state:
                    ref_now = self.get_reference_now(state)
                    elements = state.get("elements", {})
                    self.draw_layout_state(state, flash_on, ref_now)
                    self.draw_nodes_and_overlays(elements, flash_on, ref_now)
                    self.draw_telemetry_panels(state)
                    self.draw_hovered_train_locator(state)
                    self.draw_hovered_wiu_locator(state)
                    self.draw_gps_map_inset_if_selected(state)
                    self.draw_readonly_inspector(state, ref_now)

                if self.show_coverage_map:
                    self.coverage_map_view.draw(
                        self.screen, state, self.width, self.height,
                        self.font_sm_bold, self.font_sm_thin, self.font_md, self.font_lg
                    )

                self.draw_hud(state)
                if self.chat_id_modal_active:
                    self.draw_chat_identity_dialog()
                elif self.train_assign_active:
                    self.draw_train_assign_dialog()
                else:
                    self._draw_element_hover_tooltip(state)
                if self.banc_coverage_modal:
                    self.banc_coverage_modal.draw(self.screen, self.font_sm, self.font_md, self.font_sm_bold)
                pygame.display.flip()
                self.clock.tick(60)
        finally:
            self.save_preferences()
            self.close()
            pygame.quit()


def main():
    parser = argparse.ArgumentParser(description=f"Pygame Railroad Dispatcher Display Engine ({VERSION})")
    parser.add_argument("--config", default="config.json", help="Path to config.json (loads viewer configuration)")
    parser.add_argument("--mcast-group", default=None, help=f"UDP Multicast group (default: {DEFAULT_MCAST_GROUP})")
    parser.add_argument("--mcast-port", type=int, default=None, help=f"UDP Multicast port (default: {DEFAULT_MCAST_PORT})")
    parser.add_argument("--engine-host", default=None, help=f"Remote DispatcherEngine TCP host (default: {DEFAULT_ENGINE_HOST})")
    parser.add_argument("--engine-port", type=int, default=None, help=f"Remote DispatcherEngine TCP port (default: {DEFAULT_ENGINE_PORT})")
    parser.add_argument("--stale-timeout", type=float, default=None, help="Seconds of inactivity before element turns gray")
    parser.add_argument("--bind-ip", "--interface", default=None, help="Local network interface IP to bind/listen for multicast")
    parser.add_argument("--banc-config", default=None, help="Path to configuration file with Banc parameters")
    args = parser.parse_args()

    cfg_viewer = {}
    cfg_file = args.config
    if not os.path.exists(cfg_file):
        alt = os.path.join("config", cfg_file)
        if os.path.exists(alt):
            cfg_file = alt

    if cfg_file and os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                if isinstance(cdata, dict):
                    cfg_viewer = cdata.get("viewer", {})
        except Exception:
            pass

    mcast_group = args.mcast_group or cfg_viewer.get("mcast_group") or DEFAULT_MCAST_GROUP
    mcast_port = int(args.mcast_port or cfg_viewer.get("mcast_port") or DEFAULT_MCAST_PORT)
    engine_host = args.engine_host or cfg_viewer.get("engine_host") or DEFAULT_ENGINE_HOST
    engine_port = int(args.engine_port or cfg_viewer.get("engine_port") or DEFAULT_ENGINE_PORT)
    stale_timeout = float(args.stale_timeout or cfg_viewer.get("stale_timeout") or 15.0)
    banc_config_path = args.banc_config or (cfg_file if (cfg_file and os.path.exists(cfg_file)) else None)

    app = DispatcherPygameRenderer(
        mcast_group=mcast_group,
        mcast_port=mcast_port,
        engine_host=engine_host,
        engine_port=engine_port,
        stale_timeout_sec=stale_timeout,
        bind_ip=args.bind_ip,
        banc_config=banc_config_path,
    )
    app.run()


if __name__ == "__main__":
    main()