"""
ATCSMonitor (.lay) Binary Layout Converter for ApexBOS (v26.117.2)
Copyright by AB. Licensed under the MIT License.

Converts legacy Track Builder 3 / ATCSMonitor binary layout files (.lay) from the
amateur hobbyist ATCSMonitor system into native ApexBOS layout dictionaries / JSON files:
  - Extracts header metadata (title, author, version, dimensions)
  - Decodes the 2-layer column-major 2D schematic tile matrix (Layer 1: tile shapes,
    Layer 2: element record bindings) into horizontal East-West ApexBOS coordinates
  - Extracts 547-byte element records (element names, lengths, CP prefixes)
  - Extracts 158-byte Wayside Address Table records (CP key -> 14-digit MCP/WIU ID)
  - Extracts boolean indication logic equations and assigns non-conflicting bit offsets
  - Extracts station, yard, milepost, and territory text labels with scaled coordinates
  - Wires full topological neighbors for tracks, turnouts (3 legs), and signals
  - Merges redundant collinear track segments and de-overlaps standalone labels
  - Preserves 'source' metadata with 'converted_by': f"ApexBOS.220 {VERSION}"

Note: ATCSMonitor and ApexBOS are amateur enthusiast / hobbyist simulation and
visualization reference tools and are NOT real Positive Train Control (PTC) systems.
"""

import argparse
import copy
try:
    import dataclasses  # noqa: F401
except ImportError:
    raise ImportError("Python 3.6 requires the 'dataclasses' package. Install it via: pip install dataclasses")
import json
import math
import os
import re
import struct
from typing import Dict, List, Optional, Tuple, Any, Set

from version import VERSION

DEFAULT_ATCS_FRAGS_DIR = os.path.join("frags", "atcs")
LOGICAL_UNITS_PER_MILE = 4.0
GRID_SCALE = 2  # Each ATCS tile maps to 2 logical grid units (0.5 mi tangent)


def sanitize_fragment_filename(name: str) -> str:
    """Sanitize a layout or strip name into a safe filename stem."""
    s = str(name).strip()
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    return s or "atcs_layout"


def resolve_atcs_frags_subdir(lay_name_or_path: str, base_frags_dir: str = DEFAULT_ATCS_FRAGS_DIR) -> str:
    """
    Returns the per-layout subdirectory under base_frags_dir (e.g. 'frags/atcs/up_yuma_0201_20')
    so importing different .lay files never overwrites another layout's fragments.
    """
    if not base_frags_dir:
        return ""
    stem = sanitize_fragment_filename(os.path.splitext(os.path.basename(lay_name_or_path))[0])
    if os.path.basename(os.path.normpath(base_frags_dir)) == stem:
        return base_frags_dir
    return os.path.join(base_frags_dir, stem)


def _extract_header_metadata(data: bytes, fallback_name: str = "ATCS Layout") -> Dict[str, Any]:
    """Extracts title, author, email, version, and grid hints from the .lay binary header."""
    header = data[:0x06F8]
    strings = [m.group().decode("latin1", errors="ignore").strip() for m in re.finditer(rb"[\x20-\x7e]{3,}", header)]

    title = fallback_name
    author = ""
    email = ""
    lay_version = ""

    # Title normally appears around 0x7e
    if len(header) > 0x7E:
        m_title = re.match(rb"([\x20-\x7e]{3,64})\x00", header[0x7E:])
        if m_title:
            cand = m_title.group(1).decode("latin1", errors="ignore").strip()
            if cand:
                title = cand

    for s in strings:
        if "@" in s and "." in s and not email:
            email = s
        elif re.fullmatch(r"\d+\.\d+(\.\d+)?", s) and not lay_version:
            lay_version = s

    # Author typically precedes email in header
    if email and email in strings:
        idx = strings.index(email)
        if idx > 0 and strings[idx - 1] != title and len(strings[idx - 1]) >= 3:
            author = strings[idx - 1]
    elif len(header) > 0x2A6:
        m_auth = re.match(rb"([\x20-\x7e]{3,48})\x00", header[0x2A6:])
        if m_auth:
            cand = m_auth.group(1).decode("latin1", errors="ignore").strip()
            if cand and cand != title:
                author = cand

    # Header dimension hints
    header_w = 0
    header_h = 0
    cols_hint = 0
    rows_hint = 0
    if len(header) >= 0x3A:
        header_w, header_h = struct.unpack("<II", header[0x32:0x3A])
    if len(header) >= 0x20:
        max_col_idx = struct.unpack("<H", header[0x1E:0x20])[0]
        if 10 <= max_col_idx <= 512:
            cols_hint = max_col_idx + 1
    if len(header) >= 0x4D4:
        r_val = struct.unpack("<H", header[0x4D2:0x4D4])[0]
        if 20 <= r_val <= 2000 and r_val % 2 == 0:
            rows_hint = r_val // 2

    return {
        "title": title,
        "author": author,
        "email": email,
        "version": lay_version or "1.0",
        "header_width": header_w,
        "header_height": header_h,
        "cols_hint": cols_hint,
        "rows_hint": rows_hint,
    }


def _locate_grid_bounds(data: bytes, meta: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Locates (grid_start, cols, rows_per_layer) for the 2-layer uint16 schematic grid.
    In TrackBuilder 3 .lay files, the grid begins at 0x06F8 and ends immediately before
    the rolling stock definitions ('Passenger' / '.bmp').
    """
    grid_start = 0x06F8
    cols = meta.get("cols_hint") or 117
    stride = cols * 2

    # Find rolling stock boundary
    rolling_pos = data.find(b"Passenger", grid_start)
    if rolling_pos == -1:
        rolling_pos = data.find(b".bmp", grid_start)
    if rolling_pos != -1 and rolling_pos > grid_start:
        total_rows = (rolling_pos - grid_start) // stride
    else:
        total_rows = (meta.get("rows_hint") or 188) * 2

    # Two equal layers: Layer 1 (tile shapes) + Layer 2 (element bindings)
    rows_per_layer = max(1, total_rows // 2)
    return grid_start, cols, rows_per_layer


def _parse_wayside_address_table(data: bytes) -> Tuple[Dict[str, int], Dict[str, Tuple[int, int]], Dict[str, str], int]:
    """
    Parses the 158-byte Wayside Address Table records.
    Returns:
      - cp_to_wiu: dict mapping CP single-char key -> integer MCP/WIU address
      - cp_to_px: dict mapping CP key -> (pixel_x, pixel_y)
      - cp_to_station: dict mapping CP key -> station name (if any)
      - table_start: byte offset where the address table begins
    """
    cp_to_wiu: Dict[str, int] = {}
    cp_to_px: Dict[str, Tuple[int, int]] = {}
    cp_to_station: Dict[str, str] = {}

    # Locate first 14-digit MCP address record
    m_first = re.search(rb"\d{12,14}\x00", data[0x16000:])
    if not m_first:
        return cp_to_wiu, cp_to_px, cp_to_station, len(data)

    table_start = 0x16000 + m_first.start()
    rec_size = 158

    for i in range(500):
        off = table_start + i * rec_size
        if off + rec_size > len(data):
            break
        rec = data[off : off + rec_size]
        # Verify record marker ('Arial', 'Courier', '(AS', 'ntLogo')
        if not (b"Arial" in rec or b"(AS" in rec or b"Courier" in rec or b"ntLogo" in rec):
            break

        raw_str = rec[:14].split(b"\x00")[0].decode("latin1", errors="ignore").strip()
        char_b = rec[21]
        char_key = chr(char_b) if 32 <= char_b < 127 else ""
        px, py = struct.unpack("<HH", rec[24:28])

        if char_key:
            if raw_str.isdigit() and len(raw_str) >= 10:
                cp_to_wiu[char_key] = int(raw_str)
                if px > 0 or py > 0:
                    cp_to_px[char_key] = (px, py)
            elif raw_str and not any(c in raw_str for c in ("=", "&")):
                if char_key not in cp_to_station:
                    cp_to_station[char_key] = raw_str

    return cp_to_wiu, cp_to_px, cp_to_station, table_start


def _parse_element_records(data: bytes, grid_end_approx: int, addr_table_start: int) -> Dict[int, Dict[str, Any]]:
    """
    Parses the 547-byte (0x223) TrackBuilder 3 element records between the rolling stock
    section and the wayside address table.
    Returns dict: record_index -> {'name': str, 'cp_key': str, 'length_miles': float}
    """
    rec_len = 547
    elements_meta: Dict[int, Dict[str, Any]] = {}

    # Find start of element records after rolling stock bitmaps
    search_start = grid_end_approx
    last_bmp = data.rfind(b".bmp", grid_end_approx, min(len(data), grid_end_approx + 4096))
    if last_bmp != -1:
        search_start = last_bmp + 4

    # Locate the first named record matching X:YYY\x00
    m_first = re.search(rb"([A-Za-z0-9]+:[A-Za-z0-9_]+)\x00", data[search_start:addr_table_start])
    if not m_first:
        return elements_meta

    # In TrackBuilder 3, the first named record ('A:4EA') appears at +0x29 inside record 0
    # and subsequent signal/track records ('3:2WA', '3:3WA') align at stride 547 bytes.
    first_name_abs = search_start + m_first.start()
    # Align start_rec so index 0 is 0x1636d (41 bytes before 'A:4EA' at 0x16396)
    rec0_start = max(search_start, first_name_abs - 0x29)
    # Verify stride alignment with next named match
    next_m = re.search(rb"([A-Za-z0-9]+:[A-Za-z0-9_]+)\x00", data[first_name_abs + 16 : first_name_abs + 1200])
    if next_m:
        next_abs = first_name_abs + 16 + next_m.start()
        if (next_abs - rec0_start) == rec_len:
            pass
        elif abs(next_abs - first_name_abs) == rec_len:
            rec0_start = first_name_abs

    max_recs = max(0, (addr_table_start - rec0_start) // rec_len)
    for i in range(max_recs):
        off = rec0_start + i * rec_len
        rec = data[off : off + rec_len]
        m = re.search(rb"([A-Za-z0-9]+:[A-Za-z0-9_]+)\x00", rec)
        if not m:
            continue
        name = m.group(1).decode("ascii", errors="ignore")
        cp_prefix = name.split(":")[0]
        if cp_prefix.startswith("P") and len(cp_prefix) > 1:
            cp_key = cp_prefix[1:]
        else:
            cp_key = cp_prefix

        # Optional length marker (e.g. 'L25' -> 0.25 miles)
        length_mi = 0.5
        m_len = re.search(rb"L(\d{2,3})", rec)
        if m_len:
            try:
                val_mi = int(m_len.group(1)) / 100.0
                if 0.05 <= val_mi <= 10.0:
                    length_mi = val_mi
            except ValueError:
                pass

        elements_meta[i] = {
            "name": name,
            "cp_key": cp_key,
            "length_miles": length_mi,
        }

    return elements_meta


def _parse_indication_equations(data: bytes, start_offset: int) -> List[Dict[str, str]]:
    """Extracts ATCS boolean indication equations (e.g. '3:2EG&2WG=2TE', '5:1WTE=1WTE')."""
    equations: List[Dict[str, str]] = []
    seen: Set[str] = set()
    if start_offset >= len(data):
        return equations

    for m in re.finditer(rb"([A-Za-z0-9_]+:[A-Za-z0-9_&|/!]+=[A-Za-z0-9_]+)", data[start_offset:]):
        eq_str = m.group(1).decode("latin1", errors="ignore").strip()
        if eq_str in seen or len(eq_str) < 5:
            continue
        if any(k in eq_str for k in ("Arial", "Courier", "Verdana", "If", "true", "false")):
            continue
        seen.add(eq_str)
        cp_part, rest = eq_str.split(":", 1)
        lhs, rhs = rest.split("=", 1)
        equations.append({"cp_key": cp_part, "lhs": lhs, "rhs": rhs, "raw": eq_str})

    return equations


def _parse_text_labels(data: bytes, start_offset: int) -> List[Dict[str, Any]]:
    """
    Extracts station, yard, milepost, and territory text labels from 248-byte 'hAS\\x00' records.
    Text starts at +28 bytes; bounding box (X, Y, W, H) is at +128 bytes (<HHHH).
    """
    labels: List[Dict[str, Any]] = []
    if start_offset >= len(data):
        return labels

    seen_pos: Set[Tuple[str, int, int]] = set()
    for m in re.finditer(rb"hAS\x00", data[start_offset:]):
        off = start_offset + m.start()
        if off + 136 > len(data):
            break
        txt_raw = data[off + 28 : off + 128]
        txt = txt_raw.split(b"\x00")[0].decode("latin1", errors="ignore").strip()
        px, py, pw, ph = struct.unpack("<HHHH", data[off + 128 : off + 136])
        if not txt or len(txt) < 2:
            continue
        if "=" in txt or "&" in txt:
            continue
        if any(f in txt for f in ("Courier", "Arial", "Verdana", "Times", "Sans MS", "ntLogo")):
            continue
        # Normalize whitespace / control characters
        txt = re.sub(r"[\r\n\t]+", " ", txt)
        txt = re.sub(r"\s{2,}", " ", txt).strip()
        if not txt:
            continue
        key = (txt, px, py)
        if key in seen_pos:
            continue
        seen_pos.add(key)
        labels.append({"text": txt, "px": px, "py": py, "pw": pw, "ph": ph})

    return labels


def _allocate_wiu_bit(
    used_bits: Dict[int, Set[int]],
    wiu_id: Optional[int],
    width: int = 1,
    preferred: Optional[int] = None,
) -> Optional[int]:
    """
    Allocates a non-overlapping bit offset on `wiu_id` of `width` bits so that
    ApexBOSCreator's Duplicate WIU Bit Controller validator always passes cleanly.
    """
    if wiu_id is None:
        return None
    taken = used_bits.setdefault(int(wiu_id), set())
    if preferred is not None and preferred >= 0:
        if all((preferred + b) not in taken for b in range(width)):
            for b in range(width):
                taken.add(preferred + b)
            return preferred

    # Find first free aligned slot
    candidate = 0
    while True:
        if all((candidate + b) not in taken for b in range(width)):
            for b in range(width):
                taken.add(candidate + b)
            return candidate
        candidate += width


def _parse_preferred_index(meta_name: str) -> Optional[int]:
    """Extracts a small track/switch/signal number from an ATCS name like '3:2T' or 'u:1EG'."""
    if ":" not in meta_name:
        return None
    suffix = meta_name.split(":", 1)[1]
    m = re.match(r"(\d+)", suffix)
    if m:
        try:
            idx = int(m.group(1))
            if 1 <= idx <= 32:
                return idx - 1
        except ValueError:
            pass
    return None


def _merge_collinear_tracks(elem_dicts: List[dict]) -> List[dict]:
    """
    Fast O(N) merger for consecutive collinear E-W or N-S track segments that share
    the same WIU ID and label and are not separated by turnouts, diagonals, or signals.
    Rewrites all neighbor / turnout / signal references in a single pass.
    """
    by_id: Dict[str, dict] = {el["id"]: el for el in elem_dicts}

    # Tracks directly separating a signal's rear and protected elements must not merge across the signal
    signal_blocked_pairs: Set[Tuple[str, str]] = set()
    for el in elem_dicts:
        if el.get("elem_type") == "signal":
            p_id = el.get("protects_element")
            r_id = el.get("rear_element")
            if p_id and r_id:
                signal_blocked_pairs.add((p_id, r_id))
                signal_blocked_pairs.add((r_id, p_id))

    axis_info = {
        "E-W": ("W", "E"),
        "N-S": ("N", "S"),
    }

    def _is_simple_axis_track(el: dict, orient: str) -> bool:
        if el.get("elem_type") != "track" or el.get("orientation") != orient:
            return False
        if el.get("_atcs_shape") in (4, 5, 6, 8, 9, 14):
            return False
        d_prev, d_next = axis_info[orient]
        nbrs = el.get("neighbors", {})
        for k, v in nbrs.items():
            if v and k not in (d_prev, d_next):
                return False
            if len(v) > 1:
                return False
        return True

    def _can_merge_pair(a: dict, b: dict, orient: str) -> bool:
        if (a["id"], b["id"]) in signal_blocked_pairs:
            return False
        if a.get("wiu_id") != b.get("wiu_id"):
            return False
        if (a.get("label") or "") != (b.get("label") or ""):
            return False
        return _is_simple_axis_track(a, orient) and _is_simple_axis_track(b, orient)

    visited: Set[str] = set()
    removed: Set[str] = set()
    id_remap: Dict[str, str] = {}

    for el in elem_dicts:
        eid = el["id"]
        if eid in visited:
            continue
        orient = el.get("orientation")
        if orient not in axis_info or not _is_simple_axis_track(el, orient):
            continue

        d_prev, d_next = axis_info[orient]

        # Walk backward to find the start of this mergeable run
        curr = el
        while True:
            prev_list = curr.get("neighbors", {}).get(d_prev, [])
            if len(prev_list) != 1:
                break
            prev_el = by_id.get(prev_list[0])
            if not prev_el or prev_el["id"] in visited or not _can_merge_pair(prev_el, curr, orient):
                break
            curr = prev_el

        # Walk forward collecting the full run
        run: List[dict] = [curr]
        visited.add(curr["id"])
        while True:
            next_list = curr.get("neighbors", {}).get(d_next, [])
            if len(next_list) != 1:
                break
            next_el = by_id.get(next_list[0])
            if not next_el or next_el["id"] in visited or not _can_merge_pair(curr, next_el, orient):
                break
            run.append(next_el)
            visited.add(next_el["id"])
            curr = next_el

        if len(run) <= 1:
            continue

        head = run[0]
        tail = run[-1]
        head["x"] = int(round((float(head["x"]) + float(tail["x"])) / 2.0))
        head["y"] = int(round((float(head["y"]) + float(tail["y"])) / 2.0))
        head["length_miles"] = round(sum(float(item.get("length_miles", 0.5)) for item in run), 2)

        # Preserve outer boundary neighbors
        outer_next = [nid for nid in tail.get("neighbors", {}).get(d_next, []) if nid != head["id"]]
        if outer_next:
            head["neighbors"][d_next] = outer_next
        else:
            head["neighbors"].pop(d_next, None)

        for item in run[1:]:
            removed.add(item["id"])
            id_remap[item["id"]] = head["id"]

    if not removed:
        return elem_dicts

    # Single O(N) pass to drop merged segments and remap all ID references
    kept: List[dict] = []
    for el in elem_dicts:
        if el["id"] in removed:
            continue
        etype = el.get("elem_type")
        if etype == "track":
            nbrs = el.get("neighbors", {})
            for d_key, n_list in list(nbrs.items()):
                updated = []
                for nid in n_list:
                    mapped = id_remap.get(nid, nid)
                    if mapped != el["id"] and mapped not in updated:
                        updated.append(mapped)
                if updated:
                    nbrs[d_key] = updated
                else:
                    nbrs.pop(d_key, None)
        elif etype == "turnout":
            for attr in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                val = el.get(attr)
                if val in id_remap:
                    el[attr] = id_remap[val]
        elif etype == "signal":
            for attr in ("protects_element", "rear_element"):
                val = el.get(attr)
                if val in id_remap:
                    el[attr] = id_remap[val]
        kept.append(el)

    return kept


def _deoverlap_labels(elem_dicts: List[dict]):
    """Nudges standalone 'label' elements so no two labels occupy overlapping bounding boxes."""
    occupied_boxes: List[Tuple[float, float, float, float]] = []

    def _bbox(x: float, y: float, text: str) -> Tuple[float, float, float, float]:
        half_w = max(1.5, len(text) * 0.28)
        return (x - half_w, y - 0.55, x + half_w, y + 0.55)

    def _overlaps(b1: Tuple[float, float, float, float], b2: Tuple[float, float, float, float]) -> bool:
        return not (b1[2] <= b2[0] or b1[0] >= b2[2] or b1[3] <= b2[1] or b1[1] >= b2[3])

    for el in elem_dicts:
        if el.get("elem_type") != "label":
            continue
        txt = str(el.get("label", "") or "")
        orig_x = int(round(float(el.get("x", 0))))
        orig_y = int(round(float(el.get("y", 0))))
        placed_x, placed_y = orig_x, orig_y

        cand_box = _bbox(placed_x, placed_y, txt)
        if any(_overlaps(cand_box, ob) for ob in occupied_boxes):
            found = False
            for step in range(1, 20):
                for dy in (-step, step):
                    test_box = _bbox(orig_x, orig_y + dy, txt)
                    if not any(_overlaps(test_box, ob) for ob in occupied_boxes):
                        placed_y = orig_y + dy
                        cand_box = test_box
                        found = True
                        break
                if found:
                    break
        el["x"] = placed_x
        el["y"] = placed_y
        occupied_boxes.append(cand_box)


def convert_atcs_data(
    data: bytes,
    layout_filename: str = "atcs_layout.json",
    frags_dir: str = "",
    save_fragments: bool = False,
) -> dict:
    """
    Converts raw ATCSMonitor .lay binary bytes into a native ApexBOS layout dictionary.
    """
    fallback_title = os.path.splitext(os.path.basename(layout_filename))[0]
    meta = _extract_header_metadata(data, fallback_name=fallback_title)
    grid_start, cols, rows = _locate_grid_bounds(data, meta)
    stride = cols * 2
    grid_end = grid_start + (rows * 2) * stride

    cp_to_wiu, cp_to_px, cp_to_station, addr_table_start = _parse_wayside_address_table(data)
    elements_meta = _parse_element_records(data, grid_end, addr_table_start)
    equations = _parse_indication_equations(data, addr_table_start)
    raw_labels = _parse_text_labels(data, addr_table_start)

    # Read 2-layer grid:
    # Column-major storage in TrackBuilder 3:
    #   gx_tile = r (0..rows-1, West -> East horizontally)
    #   gy_tile = c (0..cols-1, Top -> Bottom across strips)
    shape_grid: Dict[Tuple[int, int], int] = {}
    bind_grid: Dict[Tuple[int, int], int] = {}

    for r in range(rows):
        off1 = grid_start + r * stride
        off2 = grid_start + (r + rows) * stride
        if off2 + stride > len(data):
            break
        row1 = struct.unpack(f"<{cols}H", data[off1 : off1 + stride])
        row2 = struct.unpack(f"<{cols}H", data[off2 : off2 + stride])
        for c in range(cols):
            sh = row1[c]
            bd = row2[c]
            if sh not in (0, 0xFFFF):
                shape_grid[(r, c)] = sh
            if bd not in (0, 0xFFFF):
                bind_grid[(r, c)] = bd

    # Turnout shape geometry in transposed (x=r, y=c) coordinates:
    #   shape -> (orientation, normal_dir, reverse_dir, common_delta, normal_delta, reverse_delta)
    TURNOUT_SHAPES = {
        10: ("E", "E", "NE", (-1, 0), (1, 0), (1, -1)),
        11: ("W", "W", "NW", (1, 0), (-1, 0), (-1, -1)),
        12: ("W", "W", "SW", (1, 0), (-1, 0), (-1, 1)),
        13: ("E", "E", "SE", (-1, 0), (1, 0), (1, 1)),
    }
    SIGNAL_SHAPES = {19, 20}
    DIAGONAL_NESW_SHAPES = {4, 6, 8}
    DIAGONAL_NWSE_SHAPES = {5, 9, 14}
    VERTICAL_TRACK_SHAPES = {15}
    HORIZONTAL_TRACK_SHAPES = {1, 2, 3, 7, 21}

    used_wiu_bits: Dict[int, Set[int]] = {}
    tile_elements: Dict[Tuple[int, int], dict] = {}
    signals_list: List[dict] = []

    def _resolve_binding(gx_t: int, gy_t: int) -> Tuple[str, str, Optional[int], float]:
        bd = bind_grid.get((gx_t, gy_t))
        if bd is None:
            return "", "", None, 0.5
        rec_idx = bd % 1000
        info = elements_meta.get(rec_idx)
        if not info:
            return "", "", None, 0.5
        cp_key = info["cp_key"]
        return info["name"], cp_key, cp_to_wiu.get(cp_key), info["length_miles"]

    def _find_nearest_wiu(gx_t: int, gy_t: int, max_dx: int = 12, max_dy: int = 3) -> Tuple[str, str, Optional[int]]:
        """Searches nearby tiles along the same strip for the closest CP/WIU binding."""
        for dist in range(1, max_dx + 1):
            for dy in (0, -1, 1, -2, 2, -3, 3):
                if abs(dy) > max_dy:
                    continue
                for dx in (-dist, dist):
                    n_name, n_cp, n_wiu, _ = _resolve_binding(gx_t + dx, gy_t + dy)
                    if n_wiu is not None:
                        return n_name, n_cp, n_wiu
        return "", "", None

    # Pass 1: Instantiate Turnouts and Tracks on the scaled integer grid (x = r*2, y = c*2)
    for (gx_t, gy_t), shape in sorted(shape_grid.items()):
        if shape in SIGNAL_SHAPES:
            continue

        meta_name, cp_key, wiu_id, _rec_len_mi = _resolve_binding(gx_t, gy_t)
        ax = gx_t * GRID_SCALE
        ay = gy_t * GRID_SCALE

        if shape in TURNOUT_SHAPES:
            orient, norm_dir, rev_dir, _, _, _ = TURNOUT_SHAPES[shape]
            # Inherit WIU from nearest neighbor on the same strip if binding wasn't on the frog cell
            if wiu_id is None:
                n_name, n_cp, n_wiu = _find_nearest_wiu(gx_t, gy_t)
                if n_wiu is not None:
                    wiu_id = n_wiu
                    cp_key = n_cp
                    if not meta_name:
                        meta_name = n_name

            pref = _parse_preferred_index(meta_name)
            cmd_bit = _allocate_wiu_bit(used_wiu_bits, wiu_id, width=2, preferred=(pref * 2 if pref is not None else None))
            el = {
                "id": f"TO_{gx_t}_{gy_t}",
                "elem_type": "turnout",
                "x": ax,
                "y": ay,
                "label": meta_name,
                "label_pos": "top",
                "wiu_id": wiu_id,
                "gps": None,
                "mile_marker": None,
                "common_neighbor": None,
                "normal_neighbor": None,
                "reverse_neighbor": None,
                "orientation": orient,
                "normal_dir": norm_dir,
                "reverse_dir": rev_dir,
                "cmd_bit_offset": cmd_bit if cmd_bit is not None else 0,
                "has_occ_bit": False,
                "occ_bit_offset": None,
                "guarded_by_signals": [],
                "_atcs_shape": shape,
                "_atcs_gx": gx_t,
                "_atcs_gy": gy_t,
                "_atcs_cp": cp_key,
            }
            tile_elements[(gx_t, gy_t)] = el
        elif shape in (
            HORIZONTAL_TRACK_SHAPES
            | VERTICAL_TRACK_SHAPES
            | DIAGONAL_NESW_SHAPES
            | DIAGONAL_NWSE_SHAPES
        ):
            if shape in DIAGONAL_NESW_SHAPES:
                orient = "NE-SW"
                length_mi = 1.41
            elif shape in DIAGONAL_NWSE_SHAPES:
                orient = "NW-SE"
                length_mi = 1.41
            elif shape in VERTICAL_TRACK_SHAPES:
                orient = "N-S"
                length_mi = 0.5
            else:
                orient = "E-W"
                length_mi = 0.5

            el = {
                "id": f"TK_{gx_t}_{gy_t}",
                "elem_type": "track",
                "x": ax,
                "y": ay,
                "label": meta_name,
                "label_pos": "top",
                "wiu_id": wiu_id,
                "gps": None,
                "mile_marker": None,
                "orientation": orient,
                "length_miles": length_mi,
                "neighbors": {},
                "parallel_track_id": None,
                "has_occ_bit": False,
                "occ_bit_offset": None,
                "_atcs_shape": shape,
                "_atcs_gx": gx_t,
                "_atcs_gy": gy_t,
                "_atcs_cp": cp_key,
                "_atcs_bind": bind_grid.get((gx_t, gy_t)),
            }
            tile_elements[(gx_t, gy_t)] = el

    # Pass 2: Instantiate Signals (shapes 19=Westbound above track, 20=Eastbound below track)
    for (gx_t, gy_t), shape in sorted(shape_grid.items()):
        if shape not in SIGNAL_SHAPES:
            continue

        meta_name, cp_key, wiu_id, _ = _resolve_binding(gx_t, gy_t)
        if shape == 19:
            # Westbound signal sits above track at (gx_t, gy_t + 1)
            sig_dir = "W"
            sig_pos = "top"
            host_coord = (gx_t, gy_t + 1)
            if host_coord not in tile_elements:
                host_coord = (gx_t, gy_t - 1)
            ax = gx_t * GRID_SCALE
            ay = (host_coord[1] * GRID_SCALE) - 1 if host_coord in tile_elements else (gy_t * GRID_SCALE)
        else:
            # Eastbound signal (20) sits below track at (gx_t, gy_t - 1)
            sig_dir = "E"
            sig_pos = "bottom"
            host_coord = (gx_t, gy_t - 1)
            if host_coord not in tile_elements:
                host_coord = (gx_t, gy_t + 1)
            ax = gx_t * GRID_SCALE
            ay = (host_coord[1] * GRID_SCALE) + 1 if host_coord in tile_elements else (gy_t * GRID_SCALE)

        # Inherit WIU from host track cell or nearest track along the strip
        if wiu_id is None and host_coord in tile_elements:
            host_el = tile_elements[host_coord]
            wiu_id = host_el.get("wiu_id")
            cp_key = host_el.get("_atcs_cp", "")
            if not meta_name:
                meta_name = host_el.get("label", "")
        if wiu_id is None:
            n_name, n_cp, n_wiu = _find_nearest_wiu(gx_t, gy_t)
            if n_wiu is not None:
                wiu_id = n_wiu
                cp_key = n_cp
                if not meta_name:
                    meta_name = n_name

        # Determine protected and rear elements along track direction
        protects_id = None
        rear_id = None
        if host_coord in tile_elements:
            hx, hy = host_coord
            fwd_dx = 1 if sig_dir == "E" else -1
            fwd_el = tile_elements.get((hx + fwd_dx, hy))
            rear_el = tile_elements.get((hx - fwd_dx, hy))
            host_el = tile_elements[host_coord]
            protects_id = fwd_el["id"] if fwd_el else host_el["id"]
            rear_id = host_el["id"] if fwd_el else (rear_el["id"] if rear_el else None)

        if protects_id is None or rear_id is None:
            continue

        # Avoid duplicate (rear, protects, position) heads by shifting secondary heads to 'middle' / 'bottom'
        existing_positions = {
            s["position"]
            for s in signals_list
            if s.get("rear_element") == rear_id and s.get("protects_element") == protects_id
        }
        if sig_pos in existing_positions:
            for alt_pos in ("middle", "bottom", "top"):
                if alt_pos not in existing_positions:
                    sig_pos = alt_pos
                    break
            else:
                continue

        pref = _parse_preferred_index(meta_name)
        sig_bit = _allocate_wiu_bit(used_wiu_bits, wiu_id, width=5, preferred=(pref * 5 if pref is not None else None))

        sig_el = {
            "id": f"SIG_{gx_t}_{gy_t}_{sig_dir}",
            "elem_type": "signal",
            "x": ax,
            "y": ay,
            "label": meta_name,
            "label_pos": "top" if sig_pos == "top" else "bottom",
            "wiu_id": wiu_id,
            "gps": None,
            "mile_marker": None,
            "position": sig_pos,
            "direction": sig_dir,
            "protects_element": protects_id,
            "rear_element": rear_id,
            "governed_route": None,
            "sig_bit_offset": sig_bit if sig_bit is not None else 0,
            "has_occ_bit": False,
            "occ_bit_offset": None,
            "_atcs_host": host_coord,
        }
        signals_list.append(sig_el)

    # Pass 3: Wire Turnout 3-leg topology and Track neighbors on the grid
    def _add_nb(el_dict: dict, d_key: str, nb_id: str):
        if el_dict.get("elem_type") != "track" or nb_id == el_dict["id"]:
            return
        lst = el_dict["neighbors"].setdefault(d_key, [])
        if nb_id not in lst:
            lst.append(nb_id)

    DELTA_TO_DIR = {
        (-1, 0): "W",
        (1, 0): "E",
        (0, -1): "N",
        (0, 1): "S",
        (1, -1): "NE",
        (-1, 1): "SW",
        (-1, -1): "NW",
        (1, 1): "SE",
    }
    OPPOSITE_DIR = {
        "E": "W",
        "W": "E",
        "N": "S",
        "S": "N",
        "NE": "SW",
        "SW": "NE",
        "NW": "SE",
        "SE": "NW",
    }

    spur_stubs: List[dict] = []
    for (gx_t, gy_t), el in list(tile_elements.items()):
        shape = el.get("_atcs_shape")
        if el["elem_type"] == "turnout" and shape in TURNOUT_SHAPES:
            _, _, _, c_delta, n_delta, r_delta = TURNOUT_SHAPES[shape]
            for attr, delta in (
                ("common_neighbor", c_delta),
                ("normal_neighbor", n_delta),
                ("reverse_neighbor", r_delta),
            ):
                nb = tile_elements.get((gx_t + delta[0], gy_t + delta[1]))
                if nb:
                    el[attr] = nb["id"]
                    if nb["elem_type"] == "track":
                        back_dir = OPPOSITE_DIR[DELTA_TO_DIR[delta]]
                        _add_nb(nb, back_dir, el["id"])
                elif attr == "reverse_neighbor":
                    # Synthesize a short diagonal spur stub for off-edge / industry diverging legs
                    d_dir = DELTA_TO_DIR[delta]
                    back_dir = OPPOSITE_DIR[d_dir]
                    spur_orient = "NE-SW" if d_dir in ("NE", "SW") else "NW-SE"
                    spur_id = f"TK_SPUR_{gx_t}_{gy_t}"
                    spur_el = {
                        "id": spur_id,
                        "elem_type": "track",
                        "x": int(el["x"]) + delta[0] * GRID_SCALE,
                        "y": int(el["y"]) + delta[1] * GRID_SCALE,
                        "label": "",
                        "label_pos": "top",
                        "wiu_id": None,
                        "gps": None,
                        "mile_marker": None,
                        "orientation": spur_orient,
                        "length_miles": 0.71,
                        "neighbors": {back_dir: [el["id"]]},
                        "parallel_track_id": None,
                        "has_occ_bit": False,
                        "occ_bit_offset": None,
                        "_atcs_shape": 4 if spur_orient == "NE-SW" else 5,
                    }
                    el[attr] = spur_id
                    spur_stubs.append(spur_el)
        elif el["elem_type"] == "track":
            orient = el["orientation"]
            if orient == "E-W":
                deltas = [(-1, 0), (1, 0)]
            elif orient == "N-S":
                deltas = [(0, -1), (0, 1)]
            elif orient == "NE-SW":
                deltas = [(1, -1), (-1, 1)]
            else:  # NW-SE
                deltas = [(-1, -1), (1, 1)]

            # Also handle bend shapes (6: W+NE, 8: E+SW, 9: W+SE, 14: NW+S)
            if shape == 6:
                deltas = [(-1, 0), (1, -1)]
            elif shape == 8:
                deltas = [(1, 0), (-1, 1)]
            elif shape == 9:
                deltas = [(-1, 0), (1, 1)]
            elif shape == 14:
                deltas = [(-1, -1), (0, 1)]

            for dx, dy in deltas:
                nb = tile_elements.get((gx_t + dx, gy_t + dy))
                if nb:
                    d_key = DELTA_TO_DIR[(dx, dy)]
                    _add_nb(el, d_key, nb["id"])
                    if nb["elem_type"] == "track":
                        _add_nb(nb, OPPOSITE_DIR[d_key], el["id"])

    # Pass 4: Merge consecutive collinear horizontal/vertical tracks that share the same
    # WIU/binding and are not split by a signal or turnout, keeping the layout crisp.
    all_elements: List[dict] = list(tile_elements.values()) + spur_stubs + signals_list
    all_elements = _merge_collinear_tracks(all_elements)

    # Pass 5: Assign non-conflicting occupancy bits to merged track blocks that have a WIU ID
    for el in all_elements:
        if el.get("elem_type") == "track" and el.get("wiu_id") is not None:
            pref = _parse_preferred_index(el.get("label", ""))
            occ_bit = _allocate_wiu_bit(used_wiu_bits, el["wiu_id"], width=1, preferred=pref)
            if occ_bit is not None:
                el["has_occ_bit"] = True
                el["occ_bit_offset"] = occ_bit

    # Pass 6: Map ATCS text labels into DisplayElement ('label') nodes aligned to the scaled grid
    max_px = max((lbl["px"] for lbl in raw_labels), default=2800) or 2800
    max_py = max((lbl["py"] for lbl in raw_labels), default=2400) or 2400
    x_scale = float(rows * GRID_SCALE) / float(max(max_px, 1))
    y_scale = float(cols * GRID_SCALE) / float(max(max_py, 1))

    for idx, lbl in enumerate(raw_labels):
        lx = int(round(lbl["px"] * x_scale))
        ly = int(round(lbl["py"] * y_scale))
        all_elements.append(
            {
                "id": f"LBL_ATCS_{idx + 1}",
                "elem_type": "label",
                "x": lx,
                "y": ly,
                "label": lbl["text"],
                "label_pos": "top",
                "wiu_id": None,
                "gps": None,
                "mile_marker": None,
                "extra_info": "",
            }
        )

    # De-overlap standalone labels so text never stacks on top of another label
    _deoverlap_labels(all_elements)

    # Strip internal temporary conversion keys
    clean_elements: List[dict] = []
    for el in all_elements:
        clean_el = {k: v for k, v in el.items() if not k.startswith("_atcs_") and not k.startswith("_itcvl_")}
        clean_elements.append(clean_el)

    # Build source metadata block
    unique_wius = sorted(set(cp_to_wiu.values()))
    source_block: Dict[str, Any] = {
        "provider": "ATCSMonitor TrackBuilder 3",
        "format": "ATCS Binary (.lay)",
        "converted_by": f"ApexBOS.220 {VERSION}",
        "title": meta["title"],
        "author": meta["author"],
        "email": meta["email"],
        "lay_version": meta["version"],
        "grid_cols": cols,
        "grid_rows": rows,
        "total_control_points": len(unique_wius),
        "indication_equations_count": len(equations),
        "hobbyist_notice": (
            "Amateur enthusiast reference layout converted from legacy ATCSMonitor (.lay) format. "
            "Not for real PTC or railroad operations."
        ),
    }

    layout_dict = {
        "version": VERSION,
        "layout_filename": layout_filename,
        "source": source_block,
        "elements": clean_elements,
    }

    # Optionally save reusable per-CP fragments in frags/atcs/<stem>/
    if save_fragments and frags_dir:
        os.makedirs(frags_dir, exist_ok=True)
        by_wiu: Dict[int, List[dict]] = {}
        for el in clean_elements:
            wid = el.get("wiu_id")
            if wid is not None:
                by_wiu.setdefault(int(wid), []).append(el)
        wiu_to_cp = {v: k for k, v in cp_to_wiu.items()}
        for wid, grp in by_wiu.items():
            if len(grp) < 2:
                continue
            cp_k = wiu_to_cp.get(wid, str(wid))
            st_name = cp_to_station.get(cp_k, "")
            frag_stem = sanitize_fragment_filename(f"CP_{cp_k}_{st_name}" if st_name else f"CP_{cp_k}_{wid}")
            min_x = min(float(e["x"]) for e in grp)
            min_y = min(float(e["y"]) for e in grp)
            frag_elems = []
            for e in grp:
                ec = copy.deepcopy(e)
                ec["x"] = int(round(float(ec["x"]) - min_x))
                ec["y"] = int(round(float(ec["y"]) - min_y))
                frag_elems.append(ec)
            frag_doc = {
                "version": VERSION,
                "fragment_name": frag_stem,
                "source": {"provider": "ATCSMonitor TrackBuilder 3", "wiu_id": wid, "cp_key": cp_k},
                "elements": frag_elems,
            }
            frag_path = os.path.join(frags_dir, f"{frag_stem}.json")
            with open(frag_path, "w", encoding="utf-8") as fp:
                json.dump(frag_doc, fp, indent=2)

    return layout_dict


def convert_atcs_file(
    lay_path: str,
    output_layout_path: Optional[str] = None,
    frags_dir: str = "",
) -> dict:
    """
    Reads an ATCSMonitor .lay binary file from disk, converts it to a native ApexBOS
    layout dictionary, optionally saves per-CP fragments, and optionally writes the
    resulting layout JSON to output_layout_path.
    """
    with open(lay_path, "rb") as f:
        raw_bytes = f.read()

    stem = os.path.splitext(os.path.basename(lay_path))[0]
    layout_filename = os.path.basename(output_layout_path) if output_layout_path else f"{stem}.json"
    target_frags_dir = resolve_atcs_frags_subdir(lay_path, frags_dir) if frags_dir else ""

    layout_dict = convert_atcs_data(
        raw_bytes,
        layout_filename=layout_filename,
        frags_dir=target_frags_dir,
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
    parser = argparse.ArgumentParser(
        description="Convert legacy ATCSMonitor (.lay) binary layout files to ApexBOS .json format."
    )
    parser.add_argument("input", help="Path to input ATCSMonitor .lay file")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to output ApexBOS .json layout file (default: <input_stem>.json)",
    )
    parser.add_argument(
        "--frags-dir",
        default="",
        help="Optional directory to save per-Control-Point fragments (e.g. frags/atcs)",
    )
    args = parser.parse_args()

    out_path = args.output
    if not out_path:
        stem = os.path.splitext(args.input)[0]
        out_path = f"{stem}.json"

    res = convert_atcs_file(args.input, output_layout_path=out_path, frags_dir=args.frags_dir)
    elems = res.get("elements", [])
    by_type: Dict[str, int] = {}
    for e in elems:
        et = e.get("elem_type", "unknown")
        by_type[et] = by_type.get(et, 0) + 1
    src = res.get("source", {})
    print(f"Converted '{args.input}' -> '{out_path}' ({len(elems)} total elements).")
    print(f"  Title: {src.get('title')} | Author: {src.get('author')} | CPs: {src.get('total_control_points')}")
    print(f"  Breakdown: {by_type}")


if __name__ == "__main__":
    main()
