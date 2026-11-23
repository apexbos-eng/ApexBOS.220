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
TRACK_VALID_DIRS = {
    1: {"E", "W"},
    2: {"E", "W"},
    3: {"E", "W"},
    7: {"E", "W"},
    21: {"E", "W"},
    15: {"N", "S"},
    4: {"NE", "SW"},
    5: {"NW", "SE"},
    6: {"W", "NE"},
    8: {"E", "SW"},
    9: {"W", "SE"},
    14: {"NW", "S"},
}


def _load_rrdata() -> Dict[str, str]:
    """Loads railroad codes from config/rrdata.json, with built-in fallbacks."""
    fallbacks = {
        "802": "UP",
        "022": "BNSF",
        "076": "BNSF",
        "891": "AMTK",
        "340": "SCAX",
        "125": "CSXT",
        "168": "CSSB",
        "190": "CSAO",
        "400": "KCS",
        "401": "KCT",
        "103": "CN",
        "024": "SEPTA",
        "083": "BRC",
        "105": "CPRS",
        "107": "SFRV",
        "263": "FEC",
        "550": "NS",
        "574": "NJTR",
        "605": "CFRC",
        "671": "MRL",
        "708": "JPBX",
        "751": "TRE",
        "001": "NMRX",
        "318": "NMRX",
        "510": "METX",
    }
    for cand in ("config/rrdata.json", os.path.join(os.path.dirname(__file__), "config", "rrdata.json")):
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        fallbacks.update({str(k): str(v) for k, v in data.items() if k != "DEF"})
                        break
            except Exception:
                pass
    return fallbacks


def compute_ctc_id_from_device_id(raw_id: Any, rrdata: Optional[Dict[str, str]] = None) -> str:
    """
    Computes a CTC ID string formatted as '<rr>.<5th-10th_digit>.nn' where <rr> is the
    lowercase Railroad identifier derived from the 2nd through 4th digits (1-indexed)
    via rrdata.json, and 'nn' is the literal placeholder string.
    Example: 78029430080202 -> 'up.943008.nn'.
    """
    if raw_id is None:
        return ""
    if isinstance(raw_id, dict):
        raw_id = raw_id.get("wiu_id") or raw_id.get("id") or raw_id.get("label") or ""
    elif hasattr(raw_id, "wiu_id") and getattr(raw_id, "wiu_id", None):
        raw_id = getattr(raw_id, "wiu_id")
    digits = re.sub(r"\D", "", str(raw_id))
    if len(digits) < 4:
        return ""
    if rrdata is None:
        rrdata = _load_rrdata()
    rr_code = digits[1:4]
    rr_name = rrdata.get(rr_code, rr_code).lower()
    sub_id = digits[4:10] if len(digits) >= 10 else digits[4:]
    return f"{rr_name}.{sub_id}.nn"


def _resolve_ctc_rule(name: str, cp: str, candidate_rules: List[str]) -> str:
    """Finds matching CTC rule starting with C: or K: for an element."""
    clean_name = str(name or "").strip()
    clean_cp = str(cp or "").strip()
    if not clean_cp and ":" in clean_name:
        pfx = clean_name.split(":", 1)[0]
        clean_cp = pfx[1:] if pfx.startswith("P") and len(pfx) > 1 else pfx

    # 1. Exact match to a candidate rule
    for rule in candidate_rules:
        if clean_name and (clean_name == rule or f"{clean_cp}:{clean_name}" == rule):
            return rule

    # 2. Extract tokens from clean_name
    name_body = clean_name.split(":")[-1] if ":" in clean_name else clean_name
    name_tokens = set(re.findall(r"[A-Za-z0-9]+", name_body))
    name_digits = set(re.findall(r"\d+", name_body))

    matched_rules: List[str] = []
    # 3. Filter candidate rules for this CP
    cp_rules = [r for r in candidate_rules if r.startswith(f"{clean_cp}:")] if clean_cp else candidate_rules

    for rule in cp_rules:
        rule_body = rule.split(":", 1)[1] if ":" in rule else rule
        rule_tokens = set(re.findall(r"[A-Za-z0-9]+", rule_body))
        rule_digits = set(re.findall(r"\d+", rule_body))

        # Check exact token match (e.g. GT or HT in C:T=T=GT or HT matching C:GT or C:HT)
        if name_tokens & rule_tokens:
            if rule not in matched_rules:
                matched_rules.append(rule)
        # Check track digit match (e.g. '1' in 1WG&1EG=1TE matching '1' in 1T or 1EA)
        elif name_digits and (name_digits & rule_digits):
            if rule not in matched_rules:
                matched_rules.append(rule)

    # 4. If no specific token/digit matched, check CP-wide general rules
    if not matched_rules and clean_cp:
        for rule in cp_rules:
            rule_body = rule.split(":", 1)[1] if ":" in rule else rule
            if not re.search(r"\d", rule_body):
                if rule not in matched_rules:
                    matched_rules.append(rule)

    # 5. If still no match and there are rules for this CP, use the first CP rule
    if not matched_rules and clean_cp and cp_rules:
        matched_rules.append(cp_rules[0])

    if matched_rules:
        matched_rules.sort(key=lambda r: (("&" in r or "or" in r), ("=" in r), len(r)), reverse=True)
        return matched_rules[0]

    return ""


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
        if ("=" in txt or "&" in txt) and not (txt.startswith("C:") or txt.startswith("K:")):
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
        if not head.get("ctc_rules"):
            for item in run[1:]:
                if item.get("ctc_rules"):
                    head["ctc_rules"] = item["ctc_rules"]
                    break
        if not head.get("ctc_id"):
            for item in run[1:]:
                if item.get("ctc_id"):
                    head["ctc_id"] = item["ctc_id"]
                    break

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


def _align_track_transitions(all_elements: List[dict]):
    """
    Aligns track endpoints at transitions between horizontal (E-W) tracks and diagonal
    (NE-SW, NW-SE) tracks so track ends meet seamlessly with zero gap.
    Moves and resizes both the diagonal and horizontal segments to share the exact
    45-degree ray intersection point at the horizontal track's Y coordinate.
    """
    by_id = {e["id"]: e for e in all_elements}

    def _track_endpoints(e: dict) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        cx, cy = float(e["x"]), float(e["y"])
        l = float(e.get("length_miles", 0.5) or 0.5)
        hs = max(0.4, l * 2.0)
        o = e.get("orientation", "E-W")
        if o == "N-S":
            return (cx, cy - hs), (cx, cy + hs)
        elif o == "NE-SW":
            d = hs * 0.70710678
            return (cx - d, cy + d), (cx + d, cy - d)
        elif o == "NW-SE":
            d = hs * 0.70710678
            return (cx - d, cy - d), (cx + d, cy + d)
        else:
            return (cx - hs, cy), (cx + hs, cy)

    track_ends: Dict[str, List[Tuple[float, float]]] = {
        e["id"]: list(_track_endpoints(e))
        for e in all_elements
        if e.get("elem_type") == "track"
    }

    pairs: List[Tuple[dict, dict]] = []
    seen = set()
    for e in all_elements:
        if e.get("elem_type") != "track":
            continue
        for d, nlist in e.get("neighbors", {}).items():
            for nid in nlist:
                nb = by_id.get(nid)
                if not nb or nb.get("elem_type") != "track":
                    continue
                pair = tuple(sorted([e["id"], nb["id"]]))
                if pair in seen:
                    continue
                o1, o2 = e.get("orientation"), nb.get("orientation")
                if (o1 == "E-W" and o2 in ("NE-SW", "NW-SE")) or (o2 == "E-W" and o1 in ("NE-SW", "NW-SE")):
                    seen.add(pair)
                    h_el = e if o1 == "E-W" else nb
                    d_el = nb if o1 == "E-W" else e
                    pairs.append((h_el, d_el))

    for h, d in pairs:
        h_ends = track_ends[h["id"]]
        d_ends = track_ends[d["id"]]
        y_h = float(h["y"])

        best_d_idx, best_h_idx = 0, 0
        min_dist = float("inf")
        for di, pd in enumerate(d_ends):
            for hi, ph in enumerate(h_ends):
                dist = math.hypot(pd[0] - ph[0], pd[1] - ph[1])
                if dist < min_dist:
                    min_dist = dist
                    best_d_idx = di
                    best_h_idx = hi

        d_other_idx = 1 - best_d_idx
        x_d0, y_d0 = d_ends[d_other_idx]

        d_orient = d.get("orientation")
        dy = y_h - y_d0
        if d_orient == "NE-SW":
            x_trans = x_d0 - dy
        else:  # NW-SE
            x_trans = x_d0 + dy

        d_ends[best_d_idx] = (x_trans, y_h)
        h_ends[best_h_idx] = (x_trans, y_h)

    for eid, ends in track_ends.items():
        el = by_id[eid]
        p0, p1 = ends
        cx = (p0[0] + p1[0]) / 2.0
        cy = (p0[1] + p1[1]) / 2.0
        seg_len = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        l_mi = round(seg_len / 4.0, 3)
        if l_mi < 0.1:
            l_mi = 0.25
        el["x"] = round(cx, 2)
        el["y"] = round(cy, 2)
        el["length_miles"] = l_mi


def _link_track_neighbors_by_radius(all_elements: List[dict], radius: float = 1.75):
    """
    Connects track endpoints that are missing neighbors by searching in a small radius
    (+-1.75 grid squares) around track ends for nearby compatible track ends or turnouts.
    """
    by_id = {e["id"]: e for e in all_elements}
    tracks = [e for e in all_elements if e.get("elem_type") == "track"]
    turnouts = [e for e in all_elements if e.get("elem_type") == "turnout"]

    dir_vec = {
        "E": (1.0, 0.0), "W": (-1.0, 0.0), "N": (0.0, -1.0), "S": (0.0, 1.0),
        "NE": (0.70710678, -0.70710678), "SW": (-0.70710678, 0.70710678),
        "NW": (-0.70710678, -0.70710678), "SE": (0.70710678, 0.70710678),
    }

    def _dirs_compatible(d1: str, d2: str) -> bool:
        v1 = dir_vec.get(d1, (0.0, 0.0))
        v2 = dir_vec.get(d2, (0.0, 0.0))
        return (v1[0] * v2[0] + v1[1] * v2[1]) <= 0.1

    def _track_endpoints(e: dict) -> List[Tuple[str, Tuple[float, float]]]:
        cx, cy = float(e["x"]), float(e["y"])
        l = float(e.get("length_miles", 0.5) or 0.5)
        hs = max(0.4, l * 2.0)
        o = e.get("orientation", "E-W")
        if o == "N-S":
            return [("N", (cx, cy - hs)), ("S", (cx, cy + hs))]
        elif o == "NE-SW":
            d = hs * 0.70710678
            return [("SW", (cx - d, cy + d)), ("NE", (cx + d, cy - d))]
        elif o == "NW-SE":
            d = hs * 0.70710678
            return [("NW", (cx - d, cy - d)), ("SE", (cx + d, cy + d))]
        else:
            return [("W", (cx - hs, cy)), ("E", (cx + hs, cy))]

    OPP = {"E": "W", "W": "E", "N": "S", "S": "N", "NE": "SW", "SW": "NE", "NW": "SE", "SE": "NW"}

    all_ends: List[Tuple[str, str, Tuple[float, float]]] = []
    for t in tracks:
        for d, pt in _track_endpoints(t):
            all_ends.append((t["id"], d, pt))

    for t in tracks:
        nbrs = t.setdefault("neighbors", {})
        existing_nbr_ids: Set[str] = set()
        for nlist in nbrs.values():
            existing_nbr_ids.update(nlist)

        for out_dir, pt in _track_endpoints(t):
            has_nbr = any(
                d in nbrs and nbrs[d]
                for d in [out_dir, out_dir[0] if len(out_dir) == 2 else out_dir]
            )
            if has_nbr:
                continue

            best_cand, best_dist = None, radius
            v_out = dir_vec.get(out_dir, (0.0, 0.0))

            for o_id, in_dir, o_pt in all_ends:
                if o_id == t["id"] or o_id in existing_nbr_ids:
                    continue
                if not _dirs_compatible(out_dir, in_dir):
                    continue
                o_el = by_id.get(o_id)
                if o_el:
                    o_nbrs = o_el.get("neighbors", {})
                    if any(d in o_nbrs and o_nbrs[d] for d in [in_dir, in_dir[0] if len(in_dir) == 2 else in_dir]):
                        continue

                dist = math.hypot(pt[0] - o_pt[0], pt[1] - o_pt[1])
                if dist <= best_dist:
                    best_dist = dist
                    best_cand = (o_id, out_dir, in_dir)

            for to in turnouts:
                if to["id"] in existing_nbr_ids:
                    continue
                has_free_leg = any(not to.get(leg) for leg in ("common_neighbor", "normal_neighbor", "reverse_neighbor"))
                if not has_free_leg:
                    continue
                dx_to = to["x"] - pt[0]
                dy_to = to["y"] - pt[1]
                if (dx_to * v_out[0] + dy_to * v_out[1]) < -0.1:
                    continue
                dist = math.hypot(dx_to, dy_to)
                if dist <= best_dist:
                    best_dist = dist
                    best_cand = (to["id"], out_dir, None)

            if best_cand:
                nb_id, d_out, d_in = best_cand
                if nb_id not in nbrs.setdefault(d_out, []):
                    nbrs[d_out].append(nb_id)
                    existing_nbr_ids.add(nb_id)
                    if nb_id in by_id and by_id[nb_id].get("elem_type") == "track":
                        recip_dir = d_in if d_in else OPP.get(d_out, "W")
                        by_id[nb_id].setdefault("neighbors", {}).setdefault(recip_dir, [])
                        if t["id"] not in by_id[nb_id]["neighbors"][recip_dir]:
                            by_id[nb_id]["neighbors"][recip_dir].append(t["id"])
                    elif nb_id in by_id and by_id[nb_id].get("elem_type") == "turnout":
                        to_el = by_id[nb_id]
                        for leg in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                            if not to_el.get(leg):
                                to_el[leg] = t["id"]
                                break


def _optimize_imported_tracks(all_elements: List[dict]) -> List[dict]:
    """
    Post-import layout optimization:
    1. Removes non-connected tracks that are not protected by nor rear to a signal.
    2. Merges track segments that are neighbors on the same path but are not protected
       by a signal nor are rear to any signal into the nearest track that is protected by a signal.
    """
    by_id = {e["id"]: e for e in all_elements}
    signals = [e for e in all_elements if e.get("elem_type") == "signal"]
    protected_ids = {s.get("protects_element") for s in signals if s.get("protects_element")}
    rear_ids = {s.get("rear_element") for s in signals if s.get("rear_element")}
    sig_critical_ids = protected_ids | rear_ids

    # Step 1: Remove non-connected tracks not in sig_critical_ids
    referenced_ids: Set[str] = set()
    for el in all_elements:
        if el.get("elem_type") == "track":
            for nlist in el.get("neighbors", {}).values():
                referenced_ids.update(nlist)
        elif el.get("elem_type") == "turnout":
            for leg in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                val = el.get(leg)
                if val:
                    referenced_ids.add(val)

    removed_orphans: Set[str] = set()
    for el in all_elements:
        if el.get("elem_type") == "track":
            eid = el["id"]
            if eid in sig_critical_ids:
                continue
            nbrs = el.get("neighbors", {})
            has_no_neighbors = not any(nbrs.values())
            is_not_referenced = (eid not in referenced_ids)
            if has_no_neighbors and is_not_referenced:
                removed_orphans.add(eid)

    if removed_orphans:
        all_elements = [e for e in all_elements if e["id"] not in removed_orphans]
        by_id = {e["id"]: e for e in all_elements}

    # Step 2: Merge un-signaled tracks on same path into nearest protected track
    axis_pairs = {
        "E-W": ("W", "E"),
        "N-S": ("N", "S"),
        "NE-SW": ("SW", "NE"),
        "NW-SE": ("NW", "SE"),
    }

    def _are_collinear(t1: dict, t2: dict, orient: str) -> bool:
        if orient == "E-W":
            return abs(float(t1["y"]) - float(t2["y"])) < 0.5
        elif orient == "N-S":
            return abs(float(t1["x"]) - float(t2["x"])) < 0.5
        elif orient == "NE-SW":
            return abs((float(t2["x"]) - float(t1["x"])) + (float(t2["y"]) - float(t1["y"]))) < 0.5
        elif orient == "NW-SE":
            return abs((float(t2["x"]) - float(t1["x"])) - (float(t2["y"]) - float(t1["y"]))) < 0.5
        return False

    visited_chains: Set[Tuple[str, ...]] = set()
    removed_tracks: Set[str] = set()
    id_remap: Dict[str, str] = {}

    for orient, (d_neg, d_pos) in axis_pairs.items():
        tracks_orient = [e for e in all_elements if e.get("elem_type") == "track" and e.get("orientation") == orient]
        for t in tracks_orient:
            if t["id"] in removed_tracks:
                continue
            pos_list = t.get("neighbors", {}).get(d_pos, [])
            if len(pos_list) != 1:
                continue
            nbr_id = pos_list[0]
            if nbr_id in removed_tracks:
                continue
            nbr = by_id.get(nbr_id)
            if not nbr or nbr.get("elem_type") != "track" or nbr.get("orientation") != orient:
                continue
            if not _are_collinear(t, nbr, orient):
                continue
            rev_list = nbr.get("neighbors", {}).get(d_neg, [])
            if len(rev_list) != 1 or rev_list[0] != t["id"]:
                continue

            # Find chain start
            chain_start = t
            while True:
                neg_list = chain_start.get("neighbors", {}).get(d_neg, [])
                if len(neg_list) != 1:
                    break
                prev_id = neg_list[0]
                if prev_id in removed_tracks:
                    break
                prev_el = by_id.get(prev_id)
                if not prev_el or prev_el.get("elem_type") != "track" or prev_el.get("orientation") != orient:
                    break
                if not _are_collinear(prev_el, chain_start, orient):
                    break
                rev = prev_el.get("neighbors", {}).get(d_pos, [])
                if len(rev) != 1 or rev[0] != chain_start["id"]:
                    break
                chain_start = prev_el

            # Collect full chain
            chain = [chain_start]
            curr = chain_start
            while True:
                pos_l = curr.get("neighbors", {}).get(d_pos, [])
                if len(pos_l) != 1:
                    break
                next_i = pos_l[0]
                if next_i in removed_tracks:
                    break
                next_e = by_id.get(next_i)
                if not next_e or next_e.get("elem_type") != "track" or next_e.get("orientation") != orient:
                    break
                if not _are_collinear(curr, next_e, orient):
                    break
                rev = next_e.get("neighbors", {}).get(d_neg, [])
                if len(rev) != 1 or rev[0] != curr["id"]:
                    break
                if next_e["id"] in [c["id"] for c in chain]:
                    break
                chain.append(next_e)
                curr = next_e

            if len(chain) <= 1:
                continue
            chain_ids = tuple(c["id"] for c in chain)
            if chain_ids in visited_chains:
                continue
            visited_chains.add(chain_ids)

            prot_indices = [idx for idx, c in enumerate(chain) if c["id"] in protected_ids]
            if not prot_indices:
                continue

            absorptions: Dict[int, List[int]] = {p_idx: [] for p_idx in prot_indices}
            for idx, c in enumerate(chain):
                cid = c["id"]
                if cid in sig_critical_ids:
                    continue
                best_p_idx = None
                best_dist = float("inf")
                for p_idx in prot_indices:
                    dist = math.hypot(float(c["x"]) - float(chain[p_idx]["x"]), float(c["y"]) - float(chain[p_idx]["y"]))
                    if dist < best_dist:
                        best_dist = dist
                        best_p_idx = p_idx
                if best_p_idx is not None:
                    step = 1 if idx > best_p_idx else -1
                    blocked = False
                    for step_idx in range(best_p_idx + step, idx, step):
                        if chain[step_idx]["id"] in rear_ids:
                            blocked = True
                            break
                    if not blocked:
                        absorptions[best_p_idx].append(idx)

            for p_idx, abs_indices in absorptions.items():
                if not abs_indices:
                    continue
                p_el = chain[p_idx]
                absorbed_elements = [chain[i] for i in abs_indices]
                run = [p_el] + absorbed_elements

                if orient == "E-W":
                    min_x = min(float(e["x"]) - float(e.get("length_miles", 0.5) or 0.5) * 2.0 for e in run)
                    max_x = max(float(e["x"]) + float(e.get("length_miles", 0.5) or 0.5) * 2.0 for e in run)
                    p_el["x"] = round((min_x + max_x) / 2.0, 2)
                    l_new = (max_x - min_x) / 4.0
                    p_el["length_miles"] = max(0.25, round(l_new * 4.0) / 4.0)
                elif orient == "N-S":
                    min_y = min(float(e["y"]) - float(e.get("length_miles", 0.5) or 0.5) * 2.0 for e in run)
                    max_y = max(float(e["y"]) + float(e.get("length_miles", 0.5) or 0.5) * 2.0 for e in run)
                    p_el["y"] = round((min_y + max_y) / 2.0, 2)
                    l_new = (max_y - min_y) / 4.0
                    p_el["length_miles"] = max(0.25, round(l_new * 4.0) / 4.0)
                else:  # diagonal
                    pts = []
                    for e in run:
                        cx, cy = float(e["x"]), float(e["y"])
                        hs = float(e.get("length_miles", 0.5) or 0.5) * 2.0 * 0.70710678
                        pts.extend([(cx - hs, cy - hs), (cx + hs, cy + hs)] if orient == "NW-SE" else [(cx - hs, cy + hs), (cx + hs, cy - hs)])
                    min_x = min(pt[0] for pt in pts)
                    max_x = max(pt[0] for pt in pts)
                    min_y = min(pt[1] for pt in pts)
                    max_y = max(pt[1] for pt in pts)
                    p_el["x"] = round((min_x + max_x) / 2.0, 2)
                    p_el["y"] = round((min_y + max_y) / 2.0, 2)
                    l_new = math.hypot(max_x - min_x, max_y - min_y) / 4.0
                    p_el["length_miles"] = max(0.25, round(l_new, 3))

                # Update p_el's outer neighbors
                pos_abs = [i for i in abs_indices if i > p_idx]
                if pos_abs:
                    pos_tail = chain[max(pos_abs)]
                    p_el.setdefault("neighbors", {})[d_pos] = pos_tail.get("neighbors", {}).get(d_pos, [])
                neg_abs = [i for i in abs_indices if i < p_idx]
                if neg_abs:
                    neg_tail = chain[min(neg_abs)]
                    p_el.setdefault("neighbors", {})[d_neg] = neg_tail.get("neighbors", {}).get(d_neg, [])

                for abs_el in absorbed_elements:
                    removed_tracks.add(abs_el["id"])
                    id_remap[abs_el["id"]] = p_el["id"]
                    if not p_el.get("ctc_rules") and abs_el.get("ctc_rules"):
                        p_el["ctc_rules"] = abs_el["ctc_rules"]
                    if not p_el.get("ctc_id") and abs_el.get("ctc_id"):
                        p_el["ctc_id"] = abs_el["ctc_id"]

    # Remap neighbor references across all kept elements
    kept_elements: List[dict] = []
    for el in all_elements:
        if el["id"] in removed_tracks:
            continue
        etype = el.get("elem_type")
        if etype == "track":
            nbrs = el.get("neighbors", {})
            for d_key, nlist in list(nbrs.items()):
                new_nlist = []
                for nid in nlist:
                    mapped = id_remap.get(nid, nid)
                    if mapped != el["id"] and mapped not in new_nlist and mapped not in removed_tracks:
                        new_nlist.append(mapped)
                if new_nlist:
                    nbrs[d_key] = new_nlist
                else:
                    nbrs.pop(d_key, None)
        elif etype == "turnout":
            for leg in ("common_neighbor", "normal_neighbor", "reverse_neighbor"):
                val = el.get(leg)
                if val in id_remap:
                    el[leg] = id_remap[val]
        kept_elements.append(el)

    return kept_elements


def _snap_elements_to_grid(all_elements: List[dict], snap_step: float = 0.5):
    """
    Snaps all layout elements to the grid (multiples of snap_step, e.g. 0.5 logical units),
    discarding original floating-point drift and re-aligning elements nicely on the new grid.
    """
    for el in all_elements:
        if "x" in el and el["x"] is not None:
            nx = round(float(el["x"]) / snap_step) * snap_step
            el["x"] = int(nx) if nx.is_integer() else nx
        if "y" in el and el["y"] is not None:
            ny = round(float(el["y"]) / snap_step) * snap_step
            el["y"] = int(ny) if ny.is_integer() else ny
        if el.get("elem_type") == "track" and "length_miles" in el:
            orient = el.get("orientation", "E-W")
            if orient in ("E-W", "N-S"):
                lm = float(el.get("length_miles") or 0.5)
                el["length_miles"] = max(0.25, round(lm * 4.0) / 4.0)


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
    rrdata = _load_rrdata()

    candidate_rules: List[str] = []
    for eq in equations:
        r = eq.get("raw", "")
        if r.startswith(("C:", "K:")) and r not in candidate_rules:
            candidate_rules.append(r)
    for lbl in raw_labels:
        t = lbl.get("text", "")
        if t.startswith(("C:", "K:")) and t not in candidate_rules:
            candidate_rules.append(t)
    for rec in elements_meta.values():
        n = rec.get("name", "")
        if n.startswith(("C:", "K:")) and n not in candidate_rules:
            if any(x in n for x in ["=", "&", "WA", "EA", "RA", "LA", "NW", "RW"]):
                candidate_rules.append(n)

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
                "ctc_id": compute_ctc_id_from_device_id(wiu_id, rrdata),
                "ctc_rules": _resolve_ctc_rule(meta_name, cp_key, candidate_rules),
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
                length_mi = 0.7071
            elif shape in DIAGONAL_NWSE_SHAPES:
                orient = "NW-SE"
                length_mi = 0.7071
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
                "ctc_id": compute_ctc_id_from_device_id(wiu_id, rrdata),
                "ctc_rules": _resolve_ctc_rule(meta_name, cp_key, candidate_rules),
                "_atcs_shape": shape,
                "_atcs_gx": gx_t,
                "_atcs_gy": gy_t,
                "_atcs_cp": cp_key,
                "_atcs_bind": bind_grid.get((gx_t, gy_t)),
            }
            tile_elements[(gx_t, gy_t)] = el

    # Pass 2: Instantiate Signals (both shape 19/20 tiles and Layer 2 bound signals)
    covered_hosts: Set[Tuple[Tuple[int, int], str]] = set()
    covered_names: Set[str] = set()

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
            "ctc_id": compute_ctc_id_from_device_id(wiu_id, rrdata),
            "ctc_rules": _resolve_ctc_rule(meta_name, cp_key, candidate_rules),
            "_atcs_host": host_coord,
        }
        signals_list.append(sig_el)
        if host_coord in tile_elements:
            covered_hosts.add((host_coord, sig_dir))
        if meta_name:
            covered_names.add(meta_name)

    # 2B: Bound signals from bind_grid (TrackBuilder Layer 2 bindings matching signal naming conventions)
    SIG_RE = re.compile(
        r"(?:^|:)(?:\d+)?(?:[EWNSLR][AG]|SIG|DEAD|[EWNSLR]DEAD|HA|[EWNS][Hh][Dd]|[1-9][EWNSLR][AG])$",
        re.IGNORECASE,
    )
    bound_sig_cells: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
    for (gx_t, gy_t), bd in bind_grid.items():
        if (gx_t, gy_t) in shape_grid and shape_grid[(gx_t, gy_t)] in SIGNAL_SHAPES:
            continue
        rec = elements_meta.get(bd % 1000)
        if not rec:
            continue
        name = rec.get("name", "")
        if not SIG_RE.search(name):
            continue
        cp_key = rec.get("cp_key", "")
        bound_sig_cells.setdefault((name, cp_key), []).append((gx_t, gy_t))

    for (meta_name, cp_key), cells in sorted(bound_sig_cells.items()):
        clusters: List[List[Tuple[int, int]]] = []
        for cell in sorted(cells):
            placed = False
            for cl in clusters:
                if any(abs(cell[0] - c[0]) <= 5 and abs(cell[1] - c[1]) <= 3 for c in cl):
                    cl.append(cell)
                    placed = True
                    break
            if not placed:
                clusters.append([cell])

        clean_n = meta_name.split(":")[-1] if ":" in meta_name else meta_name
        is_west = any(w in clean_n for w in ("W", "L"))
        is_east = any(e in clean_n for e in ("E", "R"))

        for cl in clusters:
            host_coord = None
            chosen_cell = cl[0]
            for c in cl:
                if c in tile_elements:
                    host_coord = c
                    chosen_cell = c
                    break
            if not host_coord:
                for c in cl:
                    for dy in (1, -1, 2, -2):
                        adj = (c[0], c[1] + dy)
                        if adj in tile_elements:
                            host_coord = adj
                            chosen_cell = c
                            break
                    if host_coord:
                        break
                    for dx in (1, -1, 2, -2):
                        adj = (c[0] + dx, c[1])
                        if adj in tile_elements:
                            host_coord = adj
                            chosen_cell = c
                            break
                    if host_coord:
                        break

            if not host_coord or host_coord not in tile_elements:
                continue

            host_orient = tile_elements[host_coord].get("orientation", "E-W")
            if is_west:
                sig_dir = "W"
            elif is_east:
                sig_dir = "E"
            elif "N" in clean_n:
                sig_dir = "N" if host_orient == "N-S" else ("E" if chosen_cell[0] <= host_coord[0] else "W")
            elif "S" in clean_n:
                sig_dir = "S" if host_orient == "N-S" else ("W" if chosen_cell[0] >= host_coord[0] else "E")
            else:
                sig_dir = "W"

            if (host_coord, sig_dir) in covered_hosts and meta_name in covered_names:
                continue

            sig_pos = "top" if chosen_cell[1] <= host_coord[1] else "bottom"
            ax = chosen_cell[0] * GRID_SCALE
            ay = (host_coord[1] * GRID_SCALE) - 1 if sig_pos == "top" else (host_coord[1] * GRID_SCALE) + 1

            wiu_id = cp_to_wiu.get(cp_key)
            if wiu_id is None and host_coord in tile_elements:
                wiu_id = tile_elements[host_coord].get("wiu_id")
            if wiu_id is None:
                _, _, n_wiu = _find_nearest_wiu(chosen_cell[0], chosen_cell[1])
                wiu_id = n_wiu

            hx, hy = host_coord
            fwd_dx = 1 if sig_dir in ("E", "S") else -1
            fwd_el = tile_elements.get((hx + fwd_dx, hy))
            if not fwd_el:
                fwd_el = tile_elements.get((hx + fwd_dx * 2, hy))
            rear_el = tile_elements.get((hx - fwd_dx, hy))
            if not rear_el:
                rear_el = tile_elements.get((hx - fwd_dx * 2, hy))
            host_el = tile_elements[host_coord]

            protects_id = fwd_el["id"] if fwd_el else host_el["id"]
            rear_id = host_el["id"] if fwd_el else (rear_el["id"] if rear_el else None)

            if rear_id is None or protects_id == rear_id:
                for dist in (1, 2, 3):
                    for dy in range(-dist, dist + 1):
                        for dx in range(-dist, dist + 1):
                            adj_el = tile_elements.get((hx + dx, hy + dy))
                            if adj_el and adj_el["id"] != protects_id:
                                rear_id = adj_el["id"]
                                break
                        if rear_id and rear_id != protects_id:
                            break
                    if rear_id and rear_id != protects_id:
                        break

            if protects_id is None or rear_id is None or protects_id == rear_id:
                continue

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

            existing_sig_ids = {s["id"] for s in signals_list}
            sig_id = f"SIG_{chosen_cell[0]}_{chosen_cell[1]}_{sig_dir}"
            if sig_id in existing_sig_ids:
                idx_s = 2
                while f"{sig_id}_{idx_s}" in existing_sig_ids:
                    idx_s += 1
                sig_id = f"{sig_id}_{idx_s}"

            sig_el = {
                "id": sig_id,
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
                "ctc_id": compute_ctc_id_from_device_id(wiu_id, rrdata),
                "ctc_rules": _resolve_ctc_rule(meta_name, cp_key, candidate_rules),
                "_atcs_host": host_coord,
            }
            signals_list.append(sig_el)
            covered_hosts.add((host_coord, sig_dir))
            covered_names.add(meta_name)

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
                        "ctc_id": "",
                        "ctc_rules": "",
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
                    back_dir = OPPOSITE_DIR[d_key]
                    if nb["elem_type"] == "track":
                        nb_shape = nb.get("_atcs_shape")
                        if nb_shape in TRACK_VALID_DIRS and back_dir not in TRACK_VALID_DIRS[nb_shape]:
                            continue
                        _add_nb(el, d_key, nb["id"])
                        _add_nb(nb, back_dir, el["id"])
                    elif nb["elem_type"] == "turnout":
                        _add_nb(el, d_key, nb["id"])

    # Pass 4: Merge consecutive collinear horizontal/vertical tracks that share the same
    # WIU/binding and are not split by a signal or turnout, keeping the layout crisp.
    all_elements: List[dict] = list(tile_elements.values()) + spur_stubs + signals_list
    all_elements = _merge_collinear_tracks(all_elements)

    # Pass 4b: Wire track neighbors within a small radius (+-1.75 grid squares) around track ends
    _link_track_neighbors_by_radius(all_elements, radius=1.75)

    # Pass 4c: Align track transitions between horizontal and diagonal segments
    _align_track_transitions(all_elements)

    # Pass 4d: Post-import layout optimization:
    # 1. Merge un-signaled tracks on same path into nearest protected track
    # 2. Remove non-connected tracks not protected by nor rear to a signal
    all_elements = _optimize_imported_tracks(all_elements)

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
        txt = lbl["text"]
        if txt.startswith(("C:", "K:")) or txt in candidate_rules:
            continue
        lx = int(round(lbl["px"] * x_scale))
        ly = int(round(lbl["py"] * y_scale))
        all_elements.append(
            {
                "id": f"LBL_ATCS_{idx + 1}",
                "elem_type": "label",
                "x": lx,
                "y": ly,
                "label": txt,
                "label_pos": "top",
                "wiu_id": None,
                "gps": None,
                "mile_marker": None,
                "extra_info": "",
                "ctc_id": "",
                "ctc_rules": "",
            }
        )

    # De-overlap standalone labels so text never stacks on top of another label
    _deoverlap_labels(all_elements)

    # Snap all layout elements cleanly to 0.5 grid multiples
    _snap_elements_to_grid(all_elements, snap_step=0.5)

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
