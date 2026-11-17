"""
ITCVL Layout & Template Converter for ApexBOS (v26.117.2)
Copyright by AB. Licensed under the MIT License.

Converts .itcvl layout files (and their embedded templates) into:
  1. Reusable ApexBOS layout fragments saved in 'frags/itcvl/<template_name>.json'
  2. Native ApexBOS layout dictionaries / JSON files with:
     - Vertical Y-axis coordinate inversion (ITCVL Y increases upwards -> ApexBOS Y increases downwards)
     - Full topological connections, including collinear track auto-connection on import
       (<= 4 positions apart for horizontal/vertical, slightly more for 45-degree diagonal)
     - Redundant track section merging to minimize total number of tracks
     - Standalone label de-overlapping when labels share a tile or text could overlap
     - WIU bit offset bindings, labelTexts rule mappings, and preserved 'source' metadata
       with 'converted_by': f"ApexBOS.220 {VERSION}".
"""

import argparse
import copy
try:
    import dataclasses
except ImportError:
    raise ImportError("Python 3.6 requires the 'dataclasses' package. Install it via: pip install dataclasses")
import json
import math
import os
import re
from typing import Dict, List, Optional, Tuple, Any, Set

from version import VERSION, __version__
from dispatcher_engine import (
    OPPOSITE_DIR,
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

DEFAULT_ITCVL_FRAGS_DIR = os.path.join("frags", "itcvl")
LOGICAL_UNITS_PER_MILE = 4.0


def sanitize_fragment_filename(template_name: str) -> str:
    """Sanitize a template name into a safe filename stem."""
    s = str(template_name).strip()
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    return s or "template"


def resolve_itcvl_frags_subdir(itcvl_name_or_path: str, base_frags_dir: str = DEFAULT_ITCVL_FRAGS_DIR) -> str:
    """
    Returns the per-layout subdirectory under base_frags_dir (e.g. 'frags/itcvl/Wilmington Belt Line')
    so importing different .itcvl files never overwrites another layout's template fragments.
    """
    if not base_frags_dir:
        return ""
    stem = os.path.splitext(os.path.basename(str(itcvl_name_or_path or "").strip()))[0]
    safe_stem = sanitize_fragment_filename(stem or "imported_layout")
    norm_base = os.path.normpath(base_frags_dir)
    if os.path.basename(norm_base).lower() == safe_stem.lower():
        return norm_base
    return os.path.join(norm_base, safe_stem)


def _parse_wiu_id(wiu_raw: Any) -> Optional[int]:
    if wiu_raw is None:
        return None
    s = str(wiu_raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_milepost(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _atom_to_element_dict(
    atom: dict,
    elem_id: str,
    max_y: float = 0.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    wiu_id: Optional[int] = None,
) -> Tuple[Optional[dict], str]:
    """
    Converts a single ITCVL atom or standalone item into an ApexBOS element dict,
    converting the vertically flipped ITCVL Y coordinate (y_apex = max_y - y_itcvl).
    Returns (element_dict, role_name).
    """
    kind = str(atom.get("kind", "")).strip().lower()
    role = str(atom.get("role") or atom.get("id") or elem_id).strip()
    itcvl_x = float(atom.get("x", 0)) + offset_x
    itcvl_y = float(atom.get("y", 0)) + offset_y
    raw_x = itcvl_x
    raw_y = max_y - itcvl_y

    length_cells = max(1, int(round(float(atom.get("length", 1) or 1))))
    height_cells = max(1, int(round(float(atom.get("height", 1) or 1))))

    item_wiu = _parse_wiu_id(atom.get("wiu")) if "wiu" in atom else wiu_id

    if kind == "track":
        rot = float(atom.get("rotation", 0) or 0) % 360.0
        if rot in (90.0, 270.0) and length_cells > height_cells:
            length_cells, height_cells = height_cells, length_cells
        if length_cells > 1 and height_cells == 1:
            orient = "E-W"
            x0, x1 = raw_x, raw_x + float(length_cells - 1)
            y0, y1 = raw_y, raw_y
            cx = int(round((x0 + x1) / 2.0))
            cy = int(round(raw_y))
            span_cells = float(length_cells - 1)
        elif height_cells > 1 and length_cells == 1:
            orient = "N-S"
            x0, x1 = raw_x, raw_x
            # In ITCVL, height extends upwards (+y_itcvl), which is upwards (-y_apex) in ApexBOS
            y0, y1 = raw_y - float(height_cells - 1), raw_y
            cx = int(round(raw_x))
            cy = int(round((y0 + y1) / 2.0))
            span_cells = float(height_cells - 1)
        else:
            orient = "E-W"
            x0, x1 = raw_x, raw_x
            y0, y1 = raw_y, raw_y
            cx = int(round(raw_x))
            cy = int(round(raw_y))
            span_cells = 0.8

        length_miles = round(max(0.2, span_cells / LOGICAL_UNITS_PER_MILE), 2)
        return {
            "id": elem_id,
            "elem_type": "track",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "orientation": orient,
            "length_miles": length_miles,
            "neighbors": {},
            "parallel_track_id": None,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "_itcvl_role": role,
            "_itcvl_x0": x0,
            "_itcvl_x1": x1,
            "_itcvl_y0": y0,
            "_itcvl_y1": y1,
        }, role

    elif kind == "diagonal":
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        flip = bool(atom.get("flip", False))
        # With Y flipped (y_apex = max_y - y_itcvl):
        # flip=False in ITCVL goes up-right (+x, +y_itcvl) -> (+x, -y_apex) = NE-SW in ApexBOS.
        # flip=True in ITCVL goes down-right (+x, -y_itcvl) -> (+x, +y_apex) = NW-SE in ApexBOS.
        orient = "NW-SE" if flip else "NE-SW"
        return {
            "id": elem_id,
            "elem_type": "track",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "orientation": orient,
            "length_miles": 0.25,
            "neighbors": {},
            "parallel_track_id": None,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "_itcvl_role": role,
            "_itcvl_x0": raw_x,
            "_itcvl_x1": raw_x,
            "_itcvl_y0": raw_y,
            "_itcvl_y1": raw_y,
        }, role

    elif kind == "angled":
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        rot = float(atom.get("rotation", 0) or 0) % 360.0
        # Under Y flip:
        # rot in (0, 180, 135, 315) bends along NE-SW; rot in (45, 46, 225) bends along NW-SE
        if rot in (45.0, 46.0, 225.0):
            orient = "NW-SE"
        elif rot in (135.0, 315.0, 0.0, 180.0):
            orient = "NE-SW"
        elif rot in (90.0, 270.0):
            orient = "N-S"
        else:
            orient = "E-W"
        return {
            "id": elem_id,
            "elem_type": "track",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "orientation": orient,
            "length_miles": 0.2,
            "neighbors": {},
            "parallel_track_id": None,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "_itcvl_role": role,
            "_itcvl_x0": raw_x,
            "_itcvl_x1": raw_x,
            "_itcvl_y0": raw_y,
            "_itcvl_y1": raw_y,
        }, role

    elif kind in ("switch-point", "switch", "turnout"):
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        direction = str(atom.get("direction", "right")).strip().lower()
        orient_ud = str(atom.get("orientation", "up")).strip().lower()
        # In ITCVL (with Y flipped so 'up' in ITCVL is North / -Y in ApexBOS, 'down' is South / +Y):
        # - left + up: common W, normal E, reverse NE
        # - right + up: common E, normal W, reverse NW
        # - right + down: common W, normal E, reverse SE
        # - left + down: common E, normal W, reverse SW
        if orient_ud == "up":
            if direction == "left":
                to_orient, norm_dir, rev_dir = "E", "E", "NE"
            else:
                to_orient, norm_dir, rev_dir = "W", "W", "NW"
        else:
            if direction == "right":
                to_orient, norm_dir, rev_dir = "E", "E", "SE"
            else:
                to_orient, norm_dir, rev_dir = "W", "W", "SW"

        cmd_bit = atom.get("cmd_bit_offset")
        return {
            "id": elem_id,
            "elem_type": "turnout",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "common_neighbor": "",
            "normal_neighbor": "",
            "reverse_neighbor": "",
            "orientation": to_orient,
            "normal_dir": norm_dir,
            "reverse_dir": rev_dir,
            "cmd_bit_offset": int(cmd_bit) if cmd_bit is not None else 0,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "guarded_by_signals": [],
            "_itcvl_role": role,
            "_itcvl_dir": direction,
            "_itcvl_orient": orient_ud,
        }, role

    elif kind == "signal":
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        facing = str(atom.get("facing", "right")).strip().lower()
        sig_dir = "W" if facing == "left" else ("N" if facing == "up" else ("S" if facing == "down" else "E"))
        sig_bit = atom.get("sig_bit_offset", 0)
        return {
            "id": elem_id,
            "elem_type": "signal",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top" if sig_dir in ("E", "N") else "bottom",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "position": "top",
            "direction": sig_dir,
            "protects_element": "",
            "rear_element": "",
            "governed_route": None,
            "sig_bit_offset": int(sig_bit) if sig_bit is not None else 0,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "_itcvl_role": role,
            "_itcvl_track_role": str(atom.get("trackRole", "") or ""),
        }, role

    elif kind in ("crossing-45", "diamond"):
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        return {
            "id": elem_id,
            "elem_type": "diamond",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "extra_info": "",
            "width": 0,
            "height": 0,
            "_itcvl_role": role,
        }, role

    elif kind == "crossing":
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        return {
            "id": elem_id,
            "elem_type": "crossing",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "monitored_tracks": [],
            "_itcvl_role": role,
        }, role

    elif kind == "portal":
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        pair_id = str(atom.get("pairId", "") or "A").strip().upper() or "A"
        return {
            "id": elem_id,
            "elem_type": "track_jump",
            "x": cx,
            "y": cy,
            "label": str(atom.get("label", "") or ""),
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "letter": pair_id[0] if pair_id else "A",
            "paired_jump_id": "",
            "direction": "E",
            "connected_element": "",
            "_itcvl_role": role,
        }, role

    elif kind == "label":
        off_x = float(atom.get("offsetX", 0.0) or 0.0)
        off_y = float(atom.get("offsetY", 0.0) or 0.0)
        cx = int(round(raw_x + off_x))
        cy = int(round(raw_y - off_y))
        txt = str(atom.get("text", "") or atom.get("label", "") or "").strip()
        return {
            "id": elem_id,
            "elem_type": "label",
            "x": cx,
            "y": cy,
            "label": txt,
            "label_pos": "top",
            "wiu_id": None,
            "gps": None,
            "mile_marker": None,
            "extra_info": str(atom.get("color", "") or ""),
            "width": 0,
            "height": 0,
            "_itcvl_role": role,
            "_itcvl_purpose": str(atom.get("labelPurpose", "") or ""),
        }, role

    else:
        cx = int(round(raw_x))
        cy = int(round(raw_y))
        mapped_type = kind if kind in ("base", "station", "bridge", "diamond", "ptc_datetime") else "label"
        txt = str(atom.get("text", "") or atom.get("label", "") or role)
        return {
            "id": elem_id,
            "elem_type": mapped_type,
            "x": cx,
            "y": cy,
            "label": txt,
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "extra_info": "",
            "width": 0,
            "height": 0,
            "_itcvl_role": role,
        }, role


def _get_track_endpoints_for_import(d: dict) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Returns the two logical endpoints of a track dict during import."""
    from ApexBOSCreator import LayoutTopologyAssistant

    if d.get("elem_type") != "track":
        return (float(d["x"]), float(d["y"])), (float(d["x"]), float(d["y"]))

    orient = d.get("orientation", "E-W")
    if orient in ("E-W", "N-S") and "_itcvl_x0" in d and "_itcvl_x1" in d:
        x0, x1 = float(d["_itcvl_x0"]), float(d["_itcvl_x1"])
        y0, y1 = float(d["_itcvl_y0"]), float(d["_itcvl_y1"])
        if abs(x1 - x0) > 0.01 or abs(y1 - y0) > 0.01:
            return (x0, y0), (x1, y1)

    return LayoutTopologyAssistant.get_track_endpoints_logical(d)


def _wire_elements_topology(elem_dicts: List[dict]):
    """
    Connects tracks, turnouts, signals, and track_jumps during ITCVL import:
    1. Wires turnouts to their 3 legs (common, normal, reverse) based on geometry.
    2. Connects collinear track sections (horizontal E-W, vertical N-S, or 45-degree diagonal NE-SW/NW-SE)
       whose ends are <= 4.0 positions apart (or <= 5.8 units for 45-degree diagonal orientation)
       when no other track/turnout lies between them.
    3. Connects adjacent diagonal/angled tracks to horizontal/vertical tracks within 1.8 units.
    4. Wires signals and track_jump portals.
    """
    from ApexBOSCreator import LayoutTopologyAssistant

    by_id: Dict[str, dict] = {d["id"]: d for d in elem_dicts}
    by_role: Dict[str, dict] = {d.get("_itcvl_role", d["id"]): d for d in elem_dicts}

    tracks_and_turnouts = [d for d in elem_dicts if d["elem_type"] in ("track", "turnout")]
    tracks_only = [d for d in elem_dicts if d["elem_type"] == "track"]

    def _endpoints(d: dict) -> List[Tuple[float, float]]:
        if d["elem_type"] == "track":
            p0, p1 = _get_track_endpoints_for_import(d)
            return [p0, p1]
        return [(float(d["x"]), float(d["y"]))]

    def _min_dist(d1: dict, d2: dict) -> float:
        pts1 = _endpoints(d1)
        pts2 = _endpoints(d2)
        return min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in pts1 for b in pts2)

    def _add_track_neighbor(trk: dict, cdir: str, nbr_id: str):
        if trk["elem_type"] != "track":
            return
        lst = trk.setdefault("neighbors", {}).setdefault(cdir, [])
        if nbr_id not in lst:
            lst.append(nbr_id)

    # 1. Wire Turnouts to their 3 legs (common, normal, reverse)
    for d in elem_dicts:
        if d["elem_type"] != "turnout":
            continue
        tx, ty = float(d["x"]), float(d["y"])

        horiz_west: List[Tuple[float, dict]] = []
        horiz_east: List[Tuple[float, dict]] = []
        horiz_angled_west: List[Tuple[float, dict]] = []
        horiz_angled_east: List[Tuple[float, dict]] = []
        diag_cands: List[Tuple[float, float, float, dict]] = []

        for other in tracks_and_turnouts:
            if other["id"] == d["id"]:
                continue
            dist = _min_dist(d, other)
            if dist > 2.5:
                continue
            o_pts = _endpoints(other)
            near_pt = min(o_pts, key=lambda p: math.hypot(p[0] - tx, p[1] - ty))
            far_pt = max(o_pts, key=lambda p: math.hypot(p[0] - tx, p[1] - ty))
            dx = near_pt[0] - tx
            dy = near_pt[1] - ty
            if abs(dx) < 0.05 and abs(dy) < 0.05:
                dx = far_pt[0] - tx
                dy = far_pt[1] - ty
            if abs(dx) < 0.05 and abs(dy) < 0.05:
                dx = float(other["x"]) - tx
                dy = float(other["y"]) - ty

            if abs(dy) <= 0.45 and (
                other.get("elem_type") == "turnout" or other.get("orientation", "E-W") == "E-W"
            ):
                if dx < -0.05:
                    horiz_west.append((dist, other))
                elif dx > 0.05:
                    horiz_east.append((dist, other))
            elif abs(dy) < 0.35:
                # Angled/diagonal elbow aligned horizontally with the turnout's point/heel
                if dx < -0.05:
                    horiz_angled_west.append((dist, other))
                elif dx > 0.05:
                    horiz_angled_east.append((dist, other))
            elif abs(dy) >= 0.35:
                diag_cands.append((dist, dx, dy, other))

        horiz_west.sort(key=lambda x: x[0])
        horiz_east.sort(key=lambda x: x[0])
        horiz_angled_west.sort(key=lambda x: x[0])
        horiz_angled_east.sort(key=lambda x: x[0])
        if not horiz_west and horiz_angled_west:
            horiz_west = horiz_angled_west
        if not horiz_east and horiz_angled_east:
            horiz_east = horiz_angled_east
        diag_cands.sort(key=lambda x: x[0])

        # Determine actual diverging direction from diagonal candidate if present, else from _itcvl_dir/_itcvl_orient
        if diag_cands:
            exp_east = (d.get("orientation", "E") == "E")
            exp_north = ("N" in str(d.get("reverse_dir", "")))
            preferred_diag = [
                cand for cand in diag_cands
                if (cand[1] >= -0.15 if exp_east else cand[1] <= 0.15)
                and (cand[2] <= 0.15 if exp_north else cand[2] >= -0.15)
            ]
            chosen_diag = preferred_diag[0] if preferred_diag else diag_cands[0]
            _, d_dx, d_dy, rev_other = chosen_diag
            if abs(d_dx) < 0.15:
                # Diagonal is at same X; check which side has two branches or use d["orientation"]
                go_east = (d.get("orientation", "E") == "E")
            else:
                go_east = (d_dx > 0)
            go_north = (d_dy < 0)
            if go_east:
                d["orientation"] = "E"
                d["normal_dir"] = "E"
                d["reverse_dir"] = "NE" if go_north else "SE"
                common_list, normal_list = horiz_west, horiz_east
            else:
                d["orientation"] = "W"
                d["normal_dir"] = "W"
                d["reverse_dir"] = "NW" if go_north else "SW"
                common_list, normal_list = horiz_east, horiz_west
        else:
            if d.get("orientation", "E") == "E":
                common_list, normal_list = horiz_west, horiz_east
            else:
                common_list, normal_list = horiz_east, horiz_west
            rev_other = None

        if common_list and not d.get("common_neighbor"):
            cn = common_list[0][1]
            d["common_neighbor"] = cn["id"]
            c_dir = d["orientation"]  # From common_neighbor toward turnout
            _add_track_neighbor(cn, c_dir, d["id"])

        if normal_list and not d.get("normal_neighbor"):
            nn = normal_list[0][1]
            d["normal_neighbor"] = nn["id"]
            n_dir = OPPOSITE_DIR.get(d["normal_dir"], "W")
            _add_track_neighbor(nn, n_dir, d["id"])

        if rev_other is not None and not d.get("reverse_neighbor"):
            d["reverse_neighbor"] = rev_other["id"]
            r_dir = OPPOSITE_DIR.get(d["reverse_dir"], "SW")
            _add_track_neighbor(rev_other, r_dir, d["id"])

    # 2. Collinear Track Auto-Connection (During Import Only):
    # Connect track sections if they are on the same line (horizontal, vertical, or 45-degree diagonal)
    # and their ends are <= 4 positions apart (or <= 5.8 Euclidean units for 45-degree diagonal).
    for i, t1 in enumerate(tracks_only):
        orient = t1.get("orientation", "E-W")
        p1_a, p1_b = _get_track_endpoints_for_import(t1)

        if orient == "E-W":
            # Order endpoints West to East
            w1, e1 = (p1_a, p1_b) if p1_a[0] <= p1_b[0] else (p1_b, p1_a)
            y1 = float(t1["y"])
            # Find closest collinear E-W track to the East (gap <= 4.0)
            best_east: Optional[Tuple[float, dict]] = None
            for j, t2 in enumerate(tracks_only):
                if i == j or t2.get("orientation") != "E-W":
                    continue
                if abs(float(t2["y"]) - y1) > 0.45:
                    continue
                p2_a, p2_b = _get_track_endpoints_for_import(t2)
                w2, e2 = (p2_a, p2_b) if p2_a[0] <= p2_b[0] else (p2_b, p2_a)
                gap = w2[0] - e1[0]
                if -0.25 <= gap <= 4.05:
                    # Check no turnout or other E-W track sits on this line between e1[0] and w2[0]
                    blocked = False
                    for obs in tracks_and_turnouts:
                        if obs["id"] in (t1["id"], t2["id"]):
                            continue
                        if abs(float(obs["y"]) - y1) <= 0.45:
                            ox = float(obs["x"])
                            if e1[0] - 0.1 <= ox <= w2[0] + 0.1:
                                blocked = True
                                break
                    if not blocked:
                        if best_east is None or gap < best_east[0]:
                            best_east = (gap, t2)
            if best_east is not None:
                t2 = best_east[1]
                # Only connect if neither end is already wired to a turnout in that direction
                t1_e = t1.get("neighbors", {}).get("E", [])
                t2_w = t2.get("neighbors", {}).get("W", [])
                if not any(by_id.get(nid, {}).get("elem_type") == "turnout" for nid in t1_e) and not any(
                    by_id.get(nid, {}).get("elem_type") == "turnout" for nid in t2_w
                ):
                    _add_track_neighbor(t1, "E", t2["id"])
                    _add_track_neighbor(t2, "W", t1["id"])

        elif orient == "N-S":
            # Order endpoints North (smaller y) to South (larger y)
            n1, s1 = (p1_a, p1_b) if p1_a[1] <= p1_b[1] else (p1_b, p1_a)
            x1 = float(t1["x"])
            best_south: Optional[Tuple[float, dict]] = None
            for j, t2 in enumerate(tracks_only):
                if i == j or t2.get("orientation") != "N-S":
                    continue
                if abs(float(t2["x"]) - x1) > 0.45:
                    continue
                p2_a, p2_b = _get_track_endpoints_for_import(t2)
                n2, s2 = (p2_a, p2_b) if p2_a[1] <= p2_b[1] else (p2_b, p2_a)
                gap = n2[1] - s1[1]
                if -0.25 <= gap <= 4.05:
                    blocked = False
                    for obs in tracks_and_turnouts:
                        if obs["id"] in (t1["id"], t2["id"]):
                            continue
                        if abs(float(obs["x"]) - x1) <= 0.45:
                            oy = float(obs["y"])
                            if s1[1] - 0.1 <= oy <= n2[1] + 0.1:
                                blocked = True
                                break
                    if not blocked:
                        if best_south is None or gap < best_south[0]:
                            best_south = (gap, t2)
            if best_south is not None:
                t2 = best_south[1]
                _add_track_neighbor(t1, "S", t2["id"])
                _add_track_neighbor(t2, "N", t1["id"])

        elif orient == "NE-SW":
            # Line equation in screen coords: x + y = const; NE is (+dx, -dy) so larger x, smaller y
            sw1, ne1 = (p1_a, p1_b) if p1_a[0] <= p1_b[0] else (p1_b, p1_a)
            c1 = float(t1["x"]) + float(t1["y"])
            best_ne: Optional[Tuple[float, dict]] = None
            for j, t2 in enumerate(tracks_only):
                if i == j or t2.get("orientation") != "NE-SW":
                    continue
                c2 = float(t2["x"]) + float(t2["y"])
                if abs(c2 - c1) > 1.1:
                    continue
                p2_a, p2_b = _get_track_endpoints_for_import(t2)
                sw2, ne2 = (p2_a, p2_b) if p2_a[0] <= p2_b[0] else (p2_b, p2_a)
                dx = sw2[0] - ne1[0]
                dy = sw2[1] - ne1[1]
                dist = math.hypot(dx, dy)
                # Must be in NE direction (dx > 0, dy < 0) and within 5.8 units (~4.1 diagonal positions)
                if dx >= -0.2 and dy <= 0.2 and dist <= 5.8:
                    blocked = False
                    for obs in tracks_and_turnouts:
                        if obs["id"] in (t1["id"], t2["id"]):
                            continue
                        if abs((float(obs["x"]) + float(obs["y"])) - c1) <= 1.0:
                            ox = float(obs["x"])
                            if ne1[0] + 0.1 < ox < sw2[0] - 0.1:
                                blocked = True
                                break
                    if not blocked:
                        if best_ne is None or dist < best_ne[0]:
                            best_ne = (dist, t2)
            if best_ne is not None:
                t2 = best_ne[1]
                _add_track_neighbor(t1, "NE", t2["id"])
                _add_track_neighbor(t2, "SW", t1["id"])

        elif orient == "NW-SE":
            # Line equation in screen coords: x - y = const; SE is (+dx, +dy) so larger x, larger y
            nw1, se1 = (p1_a, p1_b) if p1_a[0] <= p1_b[0] else (p1_b, p1_a)
            c1 = float(t1["x"]) - float(t1["y"])
            best_se: Optional[Tuple[float, dict]] = None
            for j, t2 in enumerate(tracks_only):
                if i == j or t2.get("orientation") != "NW-SE":
                    continue
                c2 = float(t2["x"]) - float(t2["y"])
                if abs(c2 - c1) > 1.1:
                    continue
                p2_a, p2_b = _get_track_endpoints_for_import(t2)
                nw2, se2 = (p2_a, p2_b) if p2_a[0] <= p2_b[0] else (p2_b, p2_a)
                dx = nw2[0] - se1[0]
                dy = nw2[1] - se1[1]
                dist = math.hypot(dx, dy)
                if dx >= -0.2 and dy >= -0.2 and dist <= 5.8:
                    blocked = False
                    for obs in tracks_and_turnouts:
                        if obs["id"] in (t1["id"], t2["id"]):
                            continue
                        if abs((float(obs["x"]) - float(obs["y"])) - c1) <= 1.0:
                            ox = float(obs["x"])
                            if se1[0] + 0.1 < ox < nw2[0] - 0.1:
                                blocked = True
                                break
                    if not blocked:
                        if best_se is None or dist < best_se[0]:
                            best_se = (dist, t2)
            if best_se is not None:
                t2 = best_se[1]
                _add_track_neighbor(t1, "SE", t2["id"])
                _add_track_neighbor(t2, "NW", t1["id"])

    # 2b. Connect adjacent diagonal/angled tracks to horizontal/vertical tracks at knees (within 1.8 units)
    for d1 in tracks_only:
        o1 = d1.get("orientation", "E-W")
        if o1 not in ("NE-SW", "NW-SE"):
            continue
        p1_a, p1_b = _get_track_endpoints_for_import(d1)
        if o1 == "NE-SW":
            end_dirs = [("SW", p1_a if p1_a[0] <= p1_b[0] else p1_b), ("NE", p1_b if p1_a[0] <= p1_b[0] else p1_a)]
        else:
            end_dirs = [("NW", p1_a if p1_a[0] <= p1_b[0] else p1_b), ("SE", p1_b if p1_a[0] <= p1_b[0] else p1_a)]

        for cdir, ep1 in end_dirs:
            if d1.get("neighbors", {}).get(cdir):
                continue
            best_knee: Optional[Tuple[float, dict, str]] = None
            for d2 in tracks_only:
                if d2["id"] == d1["id"] or d2.get("orientation") == o1:
                    continue
                p2_a, p2_b = _get_track_endpoints_for_import(d2)
                for ep2 in (p2_a, p2_b):
                    dist = math.hypot(ep1[0] - ep2[0], ep1[1] - ep2[1])
                    if dist <= 1.65:
                        # Direction from d2 toward d1
                        dx = float(d1["x"]) - ep2[0]
                        dy = float(d1["y"]) - ep2[1]
                        if d2.get("orientation") == "E-W":
                            opp_cdir = "E" if dx >= 0 else "W"
                        elif d2.get("orientation") == "N-S":
                            opp_cdir = "S" if dy >= 0 else "N"
                        else:
                            opp_cdir = OPPOSITE_DIR.get(cdir, "W")
                        if best_knee is None or dist < best_knee[0]:
                            best_knee = (dist, d2, opp_cdir)
            if best_knee is not None:
                _, d2, opp_cdir = best_knee
                _add_track_neighbor(d1, cdir, d2["id"])
                _add_track_neighbor(d2, opp_cdir, d1["id"])

    # 3. Wire Signals using protects_element / trackRole, plus directional rear neighbor
    resolved_signal_ids: Set[str] = set()
    def _dist_sig_to_elem(sig_d: dict, trk_d: dict) -> float:
        sx, sy = float(sig_d.get("x", 0)), float(sig_d.get("y", 0))
        if trk_d.get("elem_type") == "track":
            (x0, y0), (x1, y1) = _get_track_endpoints_for_import(trk_d)
            dx, dy = x1 - x0, y1 - y0
            seg_len2 = dx * dx + dy * dy
            if seg_len2 > 1e-6:
                t = max(0.0, min(1.0, ((sx - x0) * dx + (sy - y0) * dy) / seg_len2))
                return math.hypot(sx - (x0 + t * dx), sy - (y0 + t * dy))
        return math.hypot(sx - float(trk_d.get("x", 0)), sy - float(trk_d.get("y", 0)))

    for d in elem_dicts:
        if d["elem_type"] != "signal":
            continue
        t_role = d.get("_itcvl_track_role", "")
        prot_id = d.get("protects_element", "")
        target_trk = by_id.get(prot_id) if prot_id else None
        if target_trk is None and t_role:
            cand_trk = by_role.get(t_role)
            if cand_trk and _dist_sig_to_elem(d, cand_trk) <= 2.5:
                target_trk = cand_trk
        if target_trk and target_trk["elem_type"] in ("track", "turnout"):
            d["protects_element"] = target_trk["id"]
            resolved_signal_ids.add(d["id"])
            sig_dir = d.get("direction", "E")
            opp_dir = OPPOSITE_DIR.get(sig_dir, "W")
            if target_trk["elem_type"] == "track":
                fwd_nbrs = target_trk.get("neighbors", {}).get(sig_dir, [])
                if fwd_nbrs:
                    nbr_el = by_id.get(fwd_nbrs[0])
                    if nbr_el and nbr_el["elem_type"] == "turnout":
                        if d["id"] not in nbr_el["guarded_by_signals"]:
                            nbr_el["guarded_by_signals"].append(d["id"])
                rear_nbrs = target_trk.get("neighbors", {}).get(opp_dir, [])
                if rear_nbrs and not d.get("rear_element"):
                    d["rear_element"] = rear_nbrs[0]
            elif target_trk["elem_type"] == "turnout":
                if d["id"] not in target_trk.get("guarded_by_signals", []):
                    target_trk.setdefault("guarded_by_signals", []).append(d["id"])
                if not d.get("rear_element"):
                    if sig_dir == target_trk.get("orientation") and target_trk.get("common_neighbor"):
                        d["rear_element"] = target_trk["common_neighbor"]
                    elif sig_dir == OPPOSITE_DIR.get(target_trk.get("orientation", "E")) and target_trk.get("normal_neighbor"):
                        d["rear_element"] = target_trk["normal_neighbor"]

    # 4. Run LayoutTopologyAssistant for remaining signals, crossings, and track_jump portals
    type_map = {
        "track": TrackSegment,
        "turnout": Turnout,
        "signal": Signal,
        "crossing": Crossing,
        "base": BaseRadio,
        "track_jump": TrackJump,
        "ptc_datetime": PTCDateTime,
        "station": DisplayElement,
        "label": DisplayElement,
        "bridge": DisplayElement,
        "diamond": DisplayElement,
    }
    temp_objs: Dict[str, BaseElement] = {}
    for d in elem_dicts:
        cls = type_map.get(d["elem_type"], DisplayElement)
        valid_keys = {f.name for f in dataclasses.fields(cls) if f.init}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        temp_objs[d["id"]] = cls(**filtered)

    for eid, obj in temp_objs.items():
        if isinstance(obj, Signal) and eid in resolved_signal_ids:
            continue
        if not isinstance(obj, TrackSegment):
            LayoutTopologyAssistant.auto_suggest_connections(obj, temp_objs)

    # Write back clean config dicts while preserving _itcvl_* metadata
    for idx, d in enumerate(elem_dicts):
        obj = temp_objs[d["id"]]
        cfg = obj.to_config_dict()
        for k, v in list(d.items()):
            if k.startswith("_itcvl_"):
                cfg[k] = v
        elem_dicts[idx] = cfg


def _merge_redundant_tracks(elem_dicts: List[dict]):
    """
    Iterates over all track, signal, and turnout sections and minimizes the total number
    of tracks where possible by merging collinear connected track sections that are redundant.
    Updates all references across tracks, turnouts, signals, crossings, and track_jumps.
    """
    primary_axes = {
        "E-W": ("W", "E"),
        "N-S": ("N", "S"),
        "NE-SW": ("SW", "NE"),
        "NW-SE": ("NW", "SE"),
    }

    def _replace_element_id_refs(old_id: str, new_id: str):
        for el in elem_dicts:
            etype = el.get("elem_type")
            if etype == "track":
                nbrs = el.get("neighbors", {})
                for d_key, n_list in list(nbrs.items()):
                    updated = []
                    for nid in n_list:
                        target_id = new_id if nid == old_id else nid
                        if target_id != el["id"] and target_id not in updated:
                            updated.append(target_id)
                    nbrs[d_key] = updated
                if el.get("parallel_track_id") == old_id:
                    el["parallel_track_id"] = new_id if new_id != el["id"] else None
            elif etype == "turnout":
                for attr in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                    if el.get(attr) == old_id:
                        el[attr] = new_id
            elif etype == "signal":
                if el.get("protects_element") == old_id:
                    el["protects_element"] = new_id
                if el.get("rear_element") == old_id:
                    el["rear_element"] = new_id
            elif etype == "crossing":
                mt = el.get("monitored_tracks", [])
                if old_id in mt:
                    el["monitored_tracks"] = [new_id if x == old_id else x for x in mt]
            elif etype == "track_jump":
                if el.get("connected_element") == old_id:
                    el["connected_element"] = new_id

    merged_any = True
    while merged_any:
        merged_any = False
        by_id: Dict[str, dict] = {d["id"]: d for d in elem_dicts}

        # Identify pairs of elements separated by a signal protecting their boundary
        signal_boundaries: Set[Tuple[str, str]] = set()
        for el in elem_dicts:
            if el.get("elem_type") == "signal":
                p_id = el.get("protects_element")
                r_id = el.get("rear_element")
                if p_id and r_id and p_id != r_id:
                    signal_boundaries.add((p_id, r_id))
                    signal_boundaries.add((r_id, p_id))

        for t1 in list(elem_dicts):
            if t1.get("elem_type") != "track":
                continue
            orient = t1.get("orientation", "E-W")
            if orient not in primary_axes:
                continue
            neg_dir, pos_dir = primary_axes[orient]

            # Look for a neighbor in pos_dir (or neg_dir)
            for fwd_dir, opp_dir in ((pos_dir, neg_dir), (neg_dir, pos_dir)):
                fwd_nbrs = t1.get("neighbors", {}).get(fwd_dir, [])
                if len(fwd_nbrs) != 1:
                    continue
                t2_id = fwd_nbrs[0]
                t2 = by_id.get(t2_id)
                if not t2 or t2.get("elem_type") != "track":
                    continue
                if t2.get("orientation") != orient:
                    continue

                # t2 must only have t1 on its opp_dir side
                t2_back = t2.get("neighbors", {}).get(opp_dir, [])
                if t2_back != [t1["id"]]:
                    continue

                # Neither t1 nor t2 may have extra off-axis neighbors
                t1_other_dirs = [k for k, v in t1.get("neighbors", {}).items() if k not in (pos_dir, neg_dir) and v]
                t2_other_dirs = [k for k, v in t2.get("neighbors", {}).items() if k not in (pos_dir, neg_dir) and v]
                if t1_other_dirs or t2_other_dirs:
                    continue

                # Ensure t1 and t2 are actually collinear
                if orient == "E-W" and abs(float(t1["y"]) - float(t2["y"])) > 0.45:
                    continue
                if orient == "N-S" and abs(float(t1["x"]) - float(t2["x"])) > 0.45:
                    continue
                if orient == "NE-SW" and abs((float(t1["x"]) + float(t1["y"])) - (float(t2["x"]) + float(t2["y"]))) > 1.1:
                    continue
                if orient == "NW-SE" and abs((float(t1["x"]) - float(t1["y"])) - (float(t2["x"]) - float(t2["y"]))) > 1.1:
                    continue

                # Do not merge across a signal block boundary
                if (t1["id"], t2["id"]) in signal_boundaries:
                    continue

                # Check WIU / occupancy compatibility
                if t1.get("has_occ_bit") and t2.get("has_occ_bit"):
                    if t1.get("wiu_id") != t2.get("wiu_id") or t1.get("occ_bit_offset") != t2.get("occ_bit_offset"):
                        continue

                w1, w2 = t1.get("wiu_id"), t2.get("wiu_id")
                if w1 is not None and w2 is not None and w1 != w2:
                    continue

                if t1.get("label") and t2.get("label") and t1.get("label") != t2.get("label"):
                    continue

                # Merge t2 into t1!
                pts = list(_get_track_endpoints_for_import(t1)) + list(_get_track_endpoints_for_import(t2))
                if orient == "E-W":
                    min_x = min(p[0] for p in pts)
                    max_x = max(p[0] for p in pts)
                    y_val = float(t1["y"])
                    t1["_itcvl_x0"] = min_x
                    t1["_itcvl_x1"] = max_x
                    t1["_itcvl_y0"] = y_val
                    t1["_itcvl_y1"] = y_val
                    t1["x"] = int(round((min_x + max_x) / 2.0))
                    t1["y"] = int(round(y_val))
                    span = max(0.8, max_x - min_x)
                    t1["length_miles"] = round(max(0.2, span / LOGICAL_UNITS_PER_MILE), 2)
                elif orient == "N-S":
                    min_y = min(p[1] for p in pts)
                    max_y = max(p[1] for p in pts)
                    x_val = float(t1["x"])
                    t1["_itcvl_x0"] = x_val
                    t1["_itcvl_x1"] = x_val
                    t1["_itcvl_y0"] = min_y
                    t1["_itcvl_y1"] = max_y
                    t1["x"] = int(round(x_val))
                    t1["y"] = int(round((min_y + max_y) / 2.0))
                    span = max(0.8, max_y - min_y)
                    t1["length_miles"] = round(max(0.2, span / LOGICAL_UNITS_PER_MILE), 2)
                else:
                    min_pt = min(pts, key=lambda p: p[0])
                    max_pt = max(pts, key=lambda p: p[0])
                    t1["_itcvl_x0"], t1["_itcvl_y0"] = min_pt
                    t1["_itcvl_x1"], t1["_itcvl_y1"] = max_pt
                    t1["x"] = int(round((min_pt[0] + max_pt[0]) / 2.0))
                    t1["y"] = int(round((min_pt[1] + max_pt[1]) / 2.0))
                    span = max(0.8, math.hypot(max_pt[0] - min_pt[0], max_pt[1] - min_pt[1]))
                    t1["length_miles"] = round(max(0.2, span / LOGICAL_UNITS_PER_MILE), 2)

                if t1.get("wiu_id") is None and t2.get("wiu_id") is not None:
                    t1["wiu_id"] = t2["wiu_id"]
                if not t1.get("has_occ_bit") and t2.get("has_occ_bit"):
                    t1["has_occ_bit"] = True
                    t1["occ_bit_offset"] = t2.get("occ_bit_offset")
                if not t1.get("label") and t2.get("label"):
                    t1["label"] = t2["label"]

                # Adopt t2's outer neighbors in fwd_dir
                t1["neighbors"][fwd_dir] = [
                    nid for nid in t2.get("neighbors", {}).get(fwd_dir, []) if nid != t1["id"]
                ]

                _replace_element_id_refs(t2["id"], t1["id"])
                elem_dicts.remove(t2)
                merged_any = True
                break

            if merged_any:
                break


def _deoverlap_standalone_labels(elem_dicts: List[dict]):
    """
    If standalone 'label' elements appear in the same tile or their text bounding boxes
    could overlap, moves the overlapping label to a nearby non-overlapping tile.
    """
    standalone_labels = [d for d in elem_dicts if d.get("elem_type") == "label"]
    if len(standalone_labels) <= 1:
        return

    # Track occupied tiles by non-label elements so we don't move a label onto a track/turnout/signal
    non_label_tiles: Set[Tuple[int, int]] = set()
    for d in elem_dicts:
        if d.get("elem_type") != "label":
            non_label_tiles.add((int(d.get("x", 0)), int(d.get("y", 0))))

    placed_boxes: List[Tuple[int, int, float, float, float, float]] = []

    def _label_bbox(x: int, y: int, text: str) -> Tuple[float, float, float, float]:
        # In Creator/Viewer, standalone labels are centered horizontally at sx and drawn at sy..sy+h
        # Each character is ~0.42 logical grid units wide, height is ~0.95 logical grid units.
        half_w = max(0.8, (len(str(text or "")) * 0.42) / 2.0)
        return (float(x) - half_w, float(y) - 0.45, float(x) + half_w, float(y) + 0.45)

    def _conflicts(x: int, y: int, text: str) -> bool:
        x0, y0, x1, y1 = _label_bbox(x, y, text)
        for px, py, bx0, by0, bx1, by1 in placed_boxes:
            if px == x and py == y:
                return True
            if not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1):
                return True
        return False

    # Candidate relative offsets ordered by proximity (prefer vertical shifts first to keep X alignment)
    candidate_offsets: List[Tuple[int, int]] = [(0, 0)]
    for dist in range(1, 7):
        for dy in (-dist, dist):
            candidate_offsets.append((0, dy))
        for dx in range(-dist * 2, dist * 2 + 1):
            if dx == 0:
                continue
            for dy in (-dist, 0, dist):
                candidate_offsets.append((dx, dy))

    for lbl in standalone_labels:
        orig_x = int(lbl.get("x", 0))
        orig_y = int(lbl.get("y", 0))
        txt = str(lbl.get("label", "") or lbl.get("id", ""))

        chosen_x, chosen_y = orig_x, orig_y
        if (orig_x, orig_y) in non_label_tiles or _conflicts(orig_x, orig_y, txt):
            for dx, dy in candidate_offsets[1:]:
                cx = orig_x + dx
                cy = max(1, orig_y + dy)
                if (cx, cy) in non_label_tiles:
                    continue
                if not _conflicts(cx, cy, txt):
                    chosen_x, chosen_y = cx, cy
                    break

        lbl["x"] = chosen_x
        lbl["y"] = chosen_y
        bx0, by0, bx1, by1 = _label_bbox(chosen_x, chosen_y, txt)
        placed_boxes.append((chosen_x, chosen_y, bx0, by0, bx1, by1))


def convert_template_to_fragment(
    template_name: str,
    template_def: dict,
    frags_dir: str = DEFAULT_ITCVL_FRAGS_DIR,
    save_to_disk: bool = True,
) -> dict:
    """
    Converts a single ITCVL template definition into an ApexBOS fragment dictionary
    (with flipped Y coordinates) and saves it to 'frags/itcvl/<template_name>.json'.
    """
    atoms = template_def.get("atoms", [])
    elem_dicts: List[dict] = []
    role_to_id: Dict[str, str] = {}

    tpl_max_y = 0.0
    for atom in atoms:
        h_cells = int(round(float(atom.get("height", 1) or 1)))
        l_cells = int(round(float(atom.get("length", 1) or 1)))
        if str(atom.get("kind", "")).strip().lower() == "track":
            rot = float(atom.get("rotation", 0) or 0) % 360.0
            if rot in (90.0, 270.0) and l_cells > h_cells:
                h_cells = l_cells
        ay = float(atom.get("y", 0) or 0) + max(0, h_cells - 1)
        if ay > tpl_max_y:
            tpl_max_y = ay

    counters = {"T": 1, "W": 1, "S": 1, "L": 1, "D": 1, "X": 1, "J": 1, "E": 1}
    prefix_for_kind = {
        "track": "T",
        "diagonal": "T",
        "angled": "T",
        "switch-point": "W",
        "switch": "W",
        "turnout": "W",
        "signal": "S",
        "label": "L",
        "crossing-45": "D",
        "diamond": "D",
        "crossing": "X",
        "portal": "J",
    }

    for atom in atoms:
        kind = str(atom.get("kind", "")).strip().lower()
        pref = prefix_for_kind.get(kind, "E")
        eid = f"{pref}{counters[pref]}"
        counters[pref] += 1

        el_dict, role = _atom_to_element_dict(
            atom, elem_id=eid, max_y=tpl_max_y, offset_x=0.0, offset_y=0.0, wiu_id=None
        )
        if el_dict is not None:
            elem_dicts.append(el_dict)
            if role:
                role_to_id[role] = eid

    _wire_elements_topology(elem_dicts)

    clean_elements = []
    for d in elem_dicts:
        clean_d = {k: v for k, v in d.items() if not k.startswith("_itcvl_")}
        clean_elements.append(clean_d)

    safe_stem = sanitize_fragment_filename(template_name)
    frag_filename = f"{safe_stem}.json"
    frag_data = {
        "version": VERSION,
        "layout_filename": frag_filename,
        "is_fragment": True,
        "template_name": template_name,
        "template_guid": template_def.get("templateGuid", ""),
        "tpl_max_y": tpl_max_y,
        "role_map": role_to_id,
        "elements": clean_elements,
    }

    if save_to_disk and frags_dir:
        os.makedirs(frags_dir, exist_ok=True)
        out_path = os.path.join(frags_dir, frag_filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(frag_data, f, indent=2)

    return frag_data


def _extract_templates_from_itcvl(itcvl_data: dict) -> Dict[str, dict]:
    """Extracts all template definitions from an ITCVL dictionary."""
    templates: Dict[str, dict] = {}
    layout_block = itcvl_data.get("layout", {})
    if isinstance(layout_block, dict):
        top_tpls = layout_block.get("templates", {})
        if isinstance(top_tpls, dict):
            templates.update(top_tpls)
        elif isinstance(top_tpls, list):
            for idx, t in enumerate(top_tpls):
                if isinstance(t, dict):
                    t_name = str(t.get("name", t.get("id", f"template_{idx}")))
                    templates[t_name] = t

        data_block = layout_block.get("data", {})
        if isinstance(data_block, dict):
            data_tpls = data_block.get("templates", {})
            if isinstance(data_tpls, dict):
                templates.update(data_tpls)
            elif isinstance(data_tpls, list):
                for idx, t in enumerate(data_tpls):
                    if isinstance(t, dict):
                        t_name = str(t.get("name", t.get("id", f"template_{idx}")))
                        templates[t_name] = t

    if "templates" in itcvl_data:
        root_tpls = itcvl_data["templates"]
        if isinstance(root_tpls, dict):
            templates.update(root_tpls)
        elif isinstance(root_tpls, list):
            for idx, t in enumerate(root_tpls):
                if isinstance(t, dict):
                    t_name = str(t.get("name", t.get("id", f"template_{idx}")))
                    templates[t_name] = t

    return templates


def _load_or_build_fragment(
    template_name: str,
    templates_map: Dict[str, dict],
    frags_dir: str,
    built_fragments: Dict[str, dict],
) -> Optional[dict]:
    """
    Loads the converted fragment from frags/itcvl/<template_name>.json (or from built_fragments cache)
    so that template instances are converted using the saved fragments in frags/itcvl.
    """
    if template_name in built_fragments:
        return built_fragments[template_name]

    safe_stem = sanitize_fragment_filename(template_name)
    frag_path = os.path.join(frags_dir, f"{safe_stem}.json") if frags_dir else ""
    if frag_path and os.path.isfile(frag_path):
        try:
            with open(frag_path, "r", encoding="utf-8") as f:
                frag_data = json.load(f)
                if "tpl_max_y" in frag_data:
                    built_fragments[template_name] = frag_data
                    return frag_data
        except Exception:
            pass

    if template_name in templates_map:
        frag_data = convert_template_to_fragment(
            template_name,
            templates_map[template_name],
            frags_dir=frags_dir,
            save_to_disk=bool(frags_dir),
        )
        built_fragments[template_name] = frag_data
        return frag_data

    return None


def _extract_ordered_label_texts(item: dict) -> List[str]:
    """
    Extracts non-empty label strings from item['labelTexts'] or v3 'name'/'milepost' fields,
    respecting 'hideName' and 'hideMilepost'.
    """
    hide_name = bool(item.get("hideName", False))
    hide_mp = bool(item.get("hideMilepost", False))
    raw_labels = item.get("labelTexts")
    result: List[str] = []
    if isinstance(raw_labels, dict):
        for k, v in raw_labels.items():
            if v is None:
                continue
            kl = str(k).lower()
            if hide_name and "name" in kl:
                continue
            if hide_mp and ("mp" in kl or "milepost" in kl):
                continue
            s = str(v).strip()
            if s:
                result.append(s)
    elif isinstance(raw_labels, list):
        for v in raw_labels:
            if v is not None:
                s = str(v).strip()
                if s:
                    result.append(s)

    if not result:
        if not hide_name:
            nm = item.get("name")
            if nm is not None:
                s = str(nm).strip()
                if s:
                    result.append(s)
        if not hide_mp:
            mp = item.get("milepost")
            if mp is not None:
                s = str(mp).strip()
                if s:
                    result.append(s)
    return result


def _apply_labels_to_elements(
    instance_elems: List[dict],
    label_texts: List[str],
    next_id_fn,
    item_x: float,
    item_y: float,
    item_wiu: Optional[int] = None,
    label_offset_x: float = 0.0,
    label_offset_y: float = 0.0,
) -> List[dict]:
    """
    Applies the label rule:
    - First label is set as the primary element's label.
    - Any additional labels become separate 'label' elements positioned near the element
      (honoring optional ITCVL labelOffsetX / labelOffsetY).
    """
    if not label_texts or not instance_elems:
        return []

    first_label = label_texts[0]
    extra_labels = label_texts[1:]

    primary_el = None
    for preferred_type in ("turnout", "track", "signal"):
        for el in instance_elems:
            if el.get("elem_type") == preferred_type:
                primary_el = el
                break
        if primary_el is not None:
            break
    if primary_el is None:
        primary_el = instance_elems[0]

    primary_el["label"] = first_label

    min_x = min(int(el.get("x", item_x)) for el in instance_elems)
    max_x = max(int(el.get("x", item_x)) for el in instance_elems)
    min_y = min(int(el.get("y", item_y)) for el in instance_elems)

    off_dx = int(round(float(label_offset_x or 0.0)))
    # In ITCVL, positive labelOffsetY moves upward (smaller ApexBOS Y)
    off_dy = -int(round(float(label_offset_y or 0.0)))

    center_x = max(0, int(round((min_x + max_x) / 2.0)) + off_dx)
    top_y = max(1, min_y - 1 + off_dy)
    left_x = max(0, min_x - 1 + off_dx)
    right_x = max(max_x + 1, min_x + 1) + off_dx

    created_label_elems: List[dict] = []
    for idx, txt in enumerate(extra_labels):
        lid = next_id_fn("L")
        if idx == 0:
            lx = center_x
            ly = top_y
        elif idx % 2 == 1:
            lx = right_x
            ly = max(1, top_y - (idx // 2))
        else:
            lx = left_x
            ly = max(1, top_y - (idx // 2))

        created_label_elems.append({
            "id": lid,
            "elem_type": "label",
            "x": int(lx),
            "y": int(ly),
            "label": txt,
            "label_pos": "top",
            "wiu_id": item_wiu,
            "gps": None,
            "mile_marker": None,
            "extra_info": "",
            "width": 0,
            "height": 0,
        })

    return created_label_elems


def convert_itcvl_data(
    itcvl_data: dict,
    frags_dir: str = DEFAULT_ITCVL_FRAGS_DIR,
    layout_filename: str = "imported_layout.json",
    save_fragments: bool = True,
) -> dict:
    """
    Converts a parsed ITCVL layout dictionary into an ApexBOS layout dictionary:
    1. Converts all 'templates' into 'fragments' and saves them to 'frags/itcvl/'.
    2. Flips the Y coordinate direction (ITCVL Y increases upwards -> ApexBOS Y increases downwards).
    3. Converts all 'items' using the saved fragments and coordinates, translating WIU directly.
    4. Applies the labelTexts rule (first label -> element's label; others -> separate label elements).
    5. Connects collinear tracks whose ends are <= 4 positions apart (slightly more for 45-deg diagonals).
    6. Merges redundant collinear track sections to minimize total track count.
    7. De-overlaps standalone labels that share a tile or whose text could overlap.
    8. Leaves the 'source' block as-is and adds 'converted_by': 'ApexBOS.220 <VERSION>'.
    """
    templates_map = _extract_templates_from_itcvl(itcvl_data)
    built_fragments: Dict[str, dict] = {}

    # 1. Convert all templates to fragments in frags/itcvl
    for tpl_name, tpl_def in templates_map.items():
        if isinstance(tpl_def, dict):
            frag = convert_template_to_fragment(
                tpl_name,
                tpl_def,
                frags_dir=frags_dir,
                save_to_disk=save_fragments,
            )
            built_fragments[tpl_name] = frag

    # 2. Extract items list
    items: List[dict] = []
    layout_block = itcvl_data.get("layout", {})
    if isinstance(layout_block, dict):
        data_block = layout_block.get("data", {})
        if isinstance(data_block, dict) and isinstance(data_block.get("items"), list):
            items = data_block["items"]
        elif isinstance(layout_block.get("items"), list):
            items = layout_block["items"]
    elif isinstance(itcvl_data.get("items"), list):
        items = itcvl_data["items"]

    # Compute layout_max_y across all items so flipped Y coordinates are positive (with top margin >= 2)
    max_itcvl_y = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        iy = float(item.get("y", 0) or 0)
        kind = str(item.get("kind", "")).strip().lower()
        if kind in ("template-instance", "template"):
            tpl_name = str(item.get("template", item.get("templateName", ""))).strip()
            frag = _load_or_build_fragment(tpl_name, templates_map, frags_dir if save_fragments else "", built_fragments)
            tpl_max_y = float(frag.get("tpl_max_y", 6.0)) if frag else 6.0
            max_itcvl_y = max(max_itcvl_y, iy + tpl_max_y)
        else:
            h_cells = max(1, int(round(float(item.get("height", 1) or 1))))
            l_cells = max(1, int(round(float(item.get("length", 1) or 1))))
            if kind == "track":
                rot = float(item.get("rotation", 0) or 0) % 360.0
                if rot in (90.0, 270.0) and l_cells > h_cells:
                    h_cells = l_cells
            max_itcvl_y = max(max_itcvl_y, iy + h_cells - 1)
    layout_max_y = max_itcvl_y + 2.0

    counters = {"T": 1, "W": 1, "S": 1, "L": 1, "D": 1, "X": 1, "J": 1, "E": 1}
    used_ids = set()

    def next_id(prefix: str) -> str:
        p = prefix if prefix in counters else "E"
        while True:
            cand = f"{p}{counters[p]}"
            counters[p] += 1
            if cand not in used_ids:
                used_ids.add(cand)
                return cand

    prefix_for_elem_type = {
        "track": "T",
        "turnout": "W",
        "signal": "S",
        "label": "L",
        "diamond": "D",
        "crossing": "X",
        "track_jump": "J",
    }
    prefix_for_kind = {
        "track": "T",
        "diagonal": "T",
        "angled": "T",
        "switch-point": "W",
        "switch": "W",
        "turnout": "W",
        "signal": "S",
        "label": "L",
        "crossing-45": "D",
        "diamond": "D",
        "crossing": "X",
        "portal": "J",
    }

    all_elements: List[dict] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        item_x = float(item.get("x", 0))
        item_y = float(item.get("y", 0))
        raw_wiu = str(item.get("wiu", "") or "").strip()
        item_wiu = _parse_wiu_id(raw_wiu) if raw_wiu else None
        label_texts = _extract_ordered_label_texts(item)

        if kind in ("template-instance", "template"):
            tpl_name = str(item.get("template", item.get("templateName", ""))).strip()
            frag = _load_or_build_fragment(tpl_name, templates_map, frags_dir if save_fragments else "", built_fragments)
            if not frag:
                continue

            frag_elements = copy.deepcopy(frag.get("elements", []))
            tpl_max_y = float(frag.get("tpl_max_y", 0.0))
            role_map: Dict[str, str] = frag.get("role_map", {})
            id_to_role: Dict[str, str] = {v: k for k, v in role_map.items()}

            placeholder_texts = {"name", "milepost", "label", "mp"}
            filtered_frag_elems = []
            for fel in frag_elements:
                if fel.get("elem_type") == "label":
                    if label_texts:
                        continue
                    lbl_txt = str(fel.get("label", "")).strip()
                    if not lbl_txt or lbl_txt.lower() in placeholder_texts:
                        continue
                filtered_frag_elems.append(fel)

            old_to_new_id: Dict[str, str] = {}
            for fel in filtered_frag_elems:
                etype = fel.get("elem_type", "label")
                pref = prefix_for_elem_type.get(etype, "E")
                old_to_new_id[fel["id"]] = next_id(pref)

            sig_indexes: Dict[str, int] = item.get("signalIndexes") or {}
            sw_indexes: Dict[str, int] = item.get("switchIndexes") or {}
            num_frag_signals = sum(1 for fel in filtered_frag_elems if fel.get("elem_type") == "signal")
            max_sig_idx = max([v for v in sig_indexes.values() if isinstance(v, int) and v > 0], default=0)
            sig_count = max(int(item.get("signalCount", 0) or 0), max_sig_idx, num_frag_signals)

            # In fragment, fel["y"] = tpl_max_y - atom_y.
            # Global flipped Y = layout_max_y - (item_y + atom_y) = (layout_max_y - item_y - tpl_max_y) + fel["y"]
            base_apex_y = layout_max_y - item_y - tpl_max_y

            instance_elems: List[dict] = []
            for fel in filtered_frag_elems:
                old_id = fel["id"]
                role = id_to_role.get(old_id, "")
                new_el = copy.deepcopy(fel)
                new_el["id"] = old_to_new_id[old_id]
                new_el["x"] = int(round(float(fel.get("x", 0)) + item_x))
                new_el["y"] = int(round(float(fel.get("y", 0)) + base_apex_y))
                if item_wiu and new_el.get("elem_type") in ("track", "turnout", "signal"):
                    new_el["wiu_id"] = item_wiu

                if new_el.get("elem_type") == "track":
                    old_nbrs = new_el.get("neighbors", {})
                    new_nbrs = {}
                    for d_key, n_list in old_nbrs.items():
                        new_nbrs[d_key] = [old_to_new_id[nid] for nid in n_list if nid in old_to_new_id]
                    new_el["neighbors"] = new_nbrs

                elif new_el.get("elem_type") == "turnout":
                    for attr in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                        old_ref = new_el.get(attr)
                        new_el[attr] = old_to_new_id.get(old_ref, "") if old_ref else ""
                    new_el["guarded_by_signals"] = [
                        old_to_new_id[sid] for sid in new_el.get("guarded_by_signals", []) if sid in old_to_new_id
                    ]
                    if role in sw_indexes:
                        sw_idx = int(sw_indexes[role])
                        if sw_idx >= 1:
                            new_el["cmd_bit_offset"] = sig_count * 5 + (sw_idx - 1) * 2
                        else:
                            new_el["cmd_bit_offset"] = 0
                            new_el["wiu_id"] = None

                elif new_el.get("elem_type") == "signal":
                    old_prot = new_el.get("protects_element")
                    new_el["protects_element"] = old_to_new_id.get(old_prot, "") if old_prot else ""
                    # Recompute rear_element cleanly in global topology pass so fragment edge guesses are replaced
                    new_el["rear_element"] = ""
                    if role in sig_indexes:
                        s_idx = int(sig_indexes[role])
                        if s_idx >= 1:
                            new_el["sig_bit_offset"] = (s_idx - 1) * 5
                        else:
                            new_el["sig_bit_offset"] = 0
                            new_el["wiu_id"] = None

                instance_elems.append(new_el)

            if label_texts:
                extra_lbl_elems = _apply_labels_to_elements(
                    instance_elems,
                    label_texts,
                    next_id,
                    item_x,
                    layout_max_y - item_y,
                    item_wiu,
                    label_offset_x=float(item.get("labelOffsetX", 0) or 0),
                    label_offset_y=float(item.get("labelOffsetY", 0) or 0),
                )
                instance_elems.extend(extra_lbl_elems)

            all_elements.extend(instance_elems)

        else:
            pref = prefix_for_kind.get(kind, "E")
            eid = next_id(pref)
            el_dict, _role = _atom_to_element_dict(
                item, elem_id=eid, max_y=layout_max_y, offset_x=0.0, offset_y=0.0, wiu_id=item_wiu
            )
            if el_dict is not None:
                if el_dict.get("elem_type") == "signal":
                    s_idx = int(item.get("signalIndex", item.get("sig_index", 1)) or 1)
                    if s_idx >= 1:
                        el_dict["sig_bit_offset"] = (s_idx - 1) * 5
                    else:
                        el_dict["sig_bit_offset"] = 0
                        el_dict["wiu_id"] = None
                elif el_dict.get("elem_type") == "turnout":
                    sw_idx = int(item.get("switchIndex", item.get("sw_index", 1)) or 1)
                    if sw_idx >= 1:
                        el_dict["cmd_bit_offset"] = (sw_idx - 1) * 2
                    else:
                        el_dict["cmd_bit_offset"] = 0
                        el_dict["wiu_id"] = None

                standalone_list = [el_dict]
                if label_texts:
                    extra_lbl_elems = _apply_labels_to_elements(
                        standalone_list,
                        label_texts,
                        next_id,
                        item_x,
                        layout_max_y - item_y,
                        item_wiu,
                        label_offset_x=float(item.get("labelOffsetX", 0) or 0),
                        label_offset_y=float(item.get("labelOffsetY", 0) or 0),
                    )
                    standalone_list.extend(extra_lbl_elems)

                all_elements.extend(standalone_list)

    # 3. Wire global topology (including collinear track auto-connection <= 4 units / 45-deg diagonals)
    _wire_elements_topology(all_elements)

    # 4. Minimize total number of tracks by merging redundant collinear track sections
    _merge_redundant_tracks(all_elements)

    # 5. De-overlap standalone labels that share a tile or whose text bounding boxes overlap
    _deoverlap_standalone_labels(all_elements)

    # Clean internal temporary keys
    clean_elements: List[dict] = []
    for d in all_elements:
        clean_d = {k: v for k, v in d.items() if not k.startswith("_itcvl_")}
        clean_elements.append(clean_d)

    # 6. Preserve "source" block as-is and add "converted_by": "ApexBOS.220 <VERSION>"
    raw_source = itcvl_data.get("source")
    if isinstance(raw_source, dict):
        source_block = copy.deepcopy(raw_source)
    else:
        source_block = {}
    source_block["converted_by"] = f"ApexBOS.220 {VERSION}"

    return {
        "version": VERSION,
        "layout_filename": layout_filename,
        "source": source_block,
        "elements": clean_elements,
    }


def convert_itcvl_file(
    itcvl_path: str,
    output_layout_path: Optional[str] = None,
    frags_dir: str = DEFAULT_ITCVL_FRAGS_DIR,
) -> dict:
    """
    Reads an .itcvl file from disk, converts its templates to a dedicated per-layout
    subdirectory under frags_dir (e.g. 'frags/itcvl/<itcvl_stem>/'), converts its items
    to an ApexBOS layout dictionary, and optionally writes the resulting layout JSON.
    """
    with open(itcvl_path, "r", encoding="utf-8") as f:
        itcvl_data = json.load(f)

    stem = os.path.splitext(os.path.basename(itcvl_path))[0]
    layout_filename = os.path.basename(output_layout_path) if output_layout_path else f"{stem}.json"
    target_frags_dir = resolve_itcvl_frags_subdir(itcvl_path, frags_dir) if frags_dir else ""

    layout_dict = convert_itcvl_data(
        itcvl_data,
        frags_dir=target_frags_dir,
        layout_filename=layout_filename,
        save_fragments=bool(target_frags_dir),
    )

    if output_layout_path:
        out_dir = os.path.dirname(os.path.abspath(output_layout_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_layout_path, "w", encoding="utf-8") as f:
            json.dump(layout_dict, f, indent=2)

    return layout_dict


def main():
    parser = argparse.ArgumentParser(description="Convert .itcvl layout files and templates to ApexBOS format.")
    parser.add_argument("input", help="Path to input .itcvl file")
    parser.add_argument("-o", "--output", help="Path to output ApexBOS .json layout file (default: <input_stem>.json)")
    parser.add_argument(
        "--frags-dir",
        default=DEFAULT_ITCVL_FRAGS_DIR,
        help="Base directory to save converted template fragments in a per-layout subdirectory (default: frags/itcvl)",
    )
    args = parser.parse_args()

    out_path = args.output
    if not out_path:
        stem = os.path.splitext(args.input)[0]
        out_path = f"{stem}.json"

    resolved_dir = resolve_itcvl_frags_subdir(args.input, args.frags_dir)
    res = convert_itcvl_file(args.input, output_layout_path=out_path, frags_dir=args.frags_dir)
    print(f"Converted '{args.input}' -> '{out_path}' ({len(res.get('elements', []))} elements).")
    print(f"Template fragments saved to '{resolved_dir}'.")


if __name__ == "__main__":
    main()
