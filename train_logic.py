# -*- coding: utf-8 -*-
"""
train_logic.py - Unified Train Tracking & Inference Engine (v26.117.2)

Shared train tracking and inference logic used by DispatcherEngine and ApexBOSCreator:
1. Lined up path detection (>1 segments indicating go & green) -> Potential train.
2. Review potential trains: discard new potential train if another train already exists
   on any part of this continuous path going in the same direction.
3. First signal drops to restricted -> Status transitions to "Inferred", expects next signal.
4. Next signal drops to restricted -> Status transitions to "Confirmed". If GPS available
   for both initial and current signals, estimate speed in MPH; otherwise "Unk.".
5. Dropping any signal other than the expected next signal on the same path going the
   same direction does not create a new train; keeps existing train and watches expected signal.
6. Opposite direction signal drops are ignored.
7. Downstream signals monitored and evaluated sequentially one at a time.
8. Multiple independent paths and multiple trains tracked concurrently.
9. If next signal lacks GPS, maintain the last estimated speed.
10. User-assigned Train ID and Name; matching _node_id_ in packet sets RX indicator;
    '_speed_' in packet updates train speed.
11. Timeout starts when next signal in lined path stops receiving updates or train reaches
    edge of layout -> dims train on layout and scroller. If next signal drops during countdown,
    train resumes motion and reactivates. Otherwise deletes train when timeout expires.
"""

import re
from typing import Dict, List, Optional, Tuple, Any, Set
import math
import time
from datetime import datetime
from version import VERSION

# Train source labels displayed in Engine, Viewer, Creator, and Train Scroller
TRAIN_SOURCE_PTC = "PTC Inferred"
TRAIN_SOURCE_EMP = "EMP"
TRAIN_SOURCE_HOT_EOT = "HOT/EOT"
TRAIN_SOURCE_DPU = "DPU"
TRAIN_SOURCE_HOT_EOT_DPU = "HOT/EOT/DPU"

# RF train lifecycle & correlation parameters
RF_TRAIN_TIMEOUT_SEC = 420.0        # 7 minutes of inactivity -> delete EOT/HOT or DPU train
RF_BOARD_MIN_PACKETS = 2            # Minimum confirmed valid packets before promoting HOT/EOT/DPU onto Train Board
RF_CORRELATION_WINDOW_SEC = 12.0    # Max lag for DPU brake/pressure change -> EOT response
RF_MOTION_CORR_WINDOW_SEC = 15.0    # Max lag for DPU traction/stop change -> EOT motion change
RF_RSSI_WINDOW_SIZE = 10            # Sliding average RSSI sample window
RF_RSSI_MAX_AGE_SEC = 180.0         # Drop RSSI samples older than 3 min from trend window
RF_RSSI_TREND_THRESH_DB = 1.5       # +/- dB shift between older & newer half -> Towards / Away


def compute_train_fade_factor(age_sec: float, max_age_sec: float = RF_TRAIN_TIMEOUT_SEC) -> float:
    """
    Returns a normalized fade factor in [0.0, 1.0]:
      0.0 = brand-new packet (full brightness)
      1.0 = reached inactivity expiration (fully faded)
    """
    if max_age_sec <= 0.0:
        return 0.0
    return max(0.0, min(1.0, float(age_sec) / float(max_age_sec)))


def fade_rgb_by_age(
    base_rgb: Tuple[int, int, int],
    age_sec: float,
    max_age_sec: float = RF_TRAIN_TIMEOUT_SEC,
    target_rgb: Tuple[int, int, int] = (78, 84, 96),
) -> Tuple[int, int, int]:
    """Smoothly fades a base RGB color toward muted slate based on time since last packet."""
    f = compute_train_fade_factor(age_sec, max_age_sec)
    # Mild curve keeps freshly-active trains bright and smoothly dims them across minutes 1..7
    w = f ** 0.85
    return (
        int(round(base_rgb[0] + (target_rgb[0] - base_rgb[0]) * w)),
        int(round(base_rgb[1] + (target_rgb[1] - base_rgb[1]) * w)),
        int(round(base_rgb[2] + (target_rgb[2] - base_rgb[2]) * w)),
    )


def _coerce_packet_ts(raw_ts: Any) -> float:
    if isinstance(raw_ts, (int, float)) and float(raw_ts) > 0.0:
        return float(raw_ts)
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            return float(raw_ts)
        except ValueError:
            try:
                return datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
    return time.time()


def _format_dpu_short_power(power: Optional[str]) -> str:
    p = str(power or "").strip().upper()
    if not p:
        return "---"
    if p == "IDLE":
        return "Idle"
    if p == "IDLMD":
        return "IdlMd"
    if p == "SETOT":
        return "SetOut"
    if p == "BSTUP":
        return "DBSet"
    if p.startswith("B") and len(p) >= 2:
        try:
            return f"DB{float(p[1:]):.1f}"
        except ValueError:
            return p[:6]
    return p[:6]


TRAIN_BOARD_SORTABLE_COLS = ("source", "name", "spd_pwr", "dir", "sig", "trd", "recent")
TRAIN_BOARD_DEFAULT_DESC: Dict[str, bool] = {
    "source": False,
    "name": False,
    "spd_pwr": True,
    "dir": False,
    "sig": True,
    "trd": True,
    "recent": True,
}


def cycle_train_board_sort(
    cur_col: Optional[str],
    cur_desc: bool,
    clicked_col: str,
) -> Tuple[Optional[str], bool]:
    """
    Cycles Train Board sort state on click:
      - Inactive column -> activate in natural direction (desc for SIG/TRD/SPD/Recent, asc for Source/Name/DIR)
      - Same column (1st direction) -> reverse direction
      - Same column (2nd direction) -> reset to None (pinned stable board_order)
    """
    col = str(clicked_col or "").strip().lower()
    if col not in TRAIN_BOARD_SORTABLE_COLS:
        return cur_col, cur_desc
    default_desc = TRAIN_BOARD_DEFAULT_DESC.get(col, False)
    if cur_col != col:
        return col, default_desc
    if bool(cur_desc) == default_desc:
        return col, (not default_desc)
    return None, False


def _natural_sort_key(text: str) -> Tuple[Tuple[int, Any], ...]:
    parts = re.split(r"(\d+)", str(text or ""))
    out: List[Tuple[int, Any]] = []
    for p in parts:
        if not p:
            continue
        if p.isdigit():
            out.append((0, int(p)))
        else:
            out.append((1, p.lower()))
    return tuple(out)


def _train_source_sort_badge(trn: dict) -> str:
    src = str(trn.get("train_source") or TRAIN_SOURCE_PTC)
    if src in (TRAIN_SOURCE_HOT_EOT_DPU, TRAIN_SOURCE_HOT_EOT, TRAIN_SOURCE_DPU, TRAIN_SOURCE_EMP):
        return src
    st = str(trn.get("status") or "Confirmed")
    return "PTC Cnf" if st == "Confirmed" else "PTC Inf"


def _train_spd_pwr_sort_value(trn: dict) -> Optional[Tuple[int, float]]:
    spd_mph = trn.get("speed_mph")
    if isinstance(spd_mph, (int, float)):
        return (1, float(spd_mph))
    raw = str(trn.get("speed_str") or trn.get("dpu_power") or "").strip().upper()
    if not raw or raw in ("---", "UNK.", "UNK", "UNKNOWN", "N/A"):
        return None
    m_mph = re.match(r"^(-?\d+(?:\.\d+)?)\s*MPH$", raw)
    if m_mph:
        return (1, float(m_mph.group(1)))
    if raw in ("IDLE", "IDLMD", "SETOT", "SETOUT"):
        return (0, 0.0)
    if raw in ("BSTUP", "DBSET"):
        return (0, -0.001)
    if raw.startswith("N") and raw[1:].isdigit():
        return (0, float(int(raw[1:])))
    if raw.startswith("DB") and len(raw) > 2:
        try:
            return (0, -max(0.01, float(raw[2:])))
        except ValueError:
            return None
    if raw.startswith("B") and len(raw) > 1:
        try:
            return (0, -max(0.01, float(raw[1:])))
        except ValueError:
            return None
    try:
        return (1, float(raw))
    except ValueError:
        return None


def sort_train_board_rows(
    trains: List[dict],
    sort_col: Optional[str] = None,
    sort_desc: bool = False,
) -> List[dict]:
    """
    Sorts Train Board rows by the selected column (`source`, `name`, `spd_pwr`, `dir`, `sig`, `trd`, `recent`)
    or returns rows in pinned stable `board_order` when `sort_col` is None.
    Rows with missing/unknown values for numeric/directional columns always stay at the bottom.
    """
    base = sorted(
        list(trains or []),
        key=lambda r: (int(r.get("board_order", 0)), float(r.get("created_ts", 0.0))),
    )
    if not sort_col or sort_col not in TRAIN_BOARD_SORTABLE_COLS:
        return base

    if sort_col == "recent":
        return sorted(
            base,
            key=lambda r: float(r.get("last_packet_ts") or r.get("updated_ts") or r.get("created_ts") or 0.0),
            reverse=bool(sort_desc),
        )

    if sort_col == "source":
        return sorted(
            base,
            key=lambda r: _train_source_sort_badge(r).lower(),
            reverse=bool(sort_desc),
        )

    if sort_col == "name":
        def _disp_name(r: dict) -> str:
            dn = str(r.get("assigned_name") or r.get("train_name") or "TRN")
            if r.get("assigned_id"):
                dn = f"{dn} (#{r['assigned_id']})"
            return dn
        return sorted(
            base,
            key=lambda r: _natural_sort_key(_disp_name(r)),
            reverse=bool(sort_desc),
        )

    present: List[Tuple[Any, dict]] = []
    missing: List[dict] = []

    if sort_col == "spd_pwr":
        for r in base:
            v = _train_spd_pwr_sort_value(r)
            if v is None:
                missing.append(r)
            else:
                present.append((v, r))
    elif sort_col == "dir":
        for r in base:
            d = str(r.get("direction") or "").strip().upper()
            if not d or d in ("---", "NONE", "UNK", "UNKNOWN"):
                missing.append(r)
            else:
                present.append((d, r))
    elif sort_col == "sig":
        for r in base:
            sig = r.get("rssi_avg")
            if sig is None:
                sig = r.get("rssi_dbfs")
            if sig is None:
                missing.append(r)
            else:
                try:
                    present.append((float(sig), r))
                except (ValueError, TypeError):
                    missing.append(r)
    elif sort_col == "trd":
        trend_rank = {"TOWARDS": 3, "STEADY": 2, "AWAY": 1}
        for r in base:
            tc = str(r.get("rssi_trend") or "NONE").strip().upper()
            rk = trend_rank.get(tc)
            if rk is None:
                missing.append(r)
            else:
                delta = float(r.get("rssi_delta_db") or 0.0)
                present.append(((rk, delta), r))
    else:
        return base

    present.sort(key=lambda item: item[0], reverse=bool(sort_desc))
    return [r for _, r in present] + missing

OPPOSITE_DIR: Dict[str, str] = {
    "E": "W", "W": "E",
    "N": "S", "S": "N",
    "NE": "SW", "SW": "NE",
    "NW": "SE", "SE": "NW",
}

COMPATIBLE_DIRS: Dict[str, Tuple[str, ...]] = {
    "E": ("E", "NE", "SE"),
    "W": ("W", "NW", "SW"),
    "N": ("N", "NE", "NW"),
    "S": ("S", "SE", "SW"),
    "NE": ("NE", "E", "N"),
    "NW": ("NW", "W", "N"),
    "SE": ("SE", "E", "S"),
    "SW": ("SW", "W", "S"),
}


def haversine_miles(pos1: Tuple[float, float], pos2: Tuple[float, float]) -> float:
    """Calculate great-circle distance between two (lat, lon) points in miles."""
    try:
        lat1, lon1 = math.radians(float(pos1[0])), math.radians(float(pos1[1]))
        lat2, lon2 = math.radians(float(pos2[0])), math.radians(float(pos2[1]))
    except (ValueError, TypeError, IndexError):
        return 0.0
    r_miles = 3958.8
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r_miles * c


def get_signal_gps(sig: Any, elements: Optional[Dict[str, Any]] = None) -> Optional[Tuple[float, float]]:
    """Resolve GPS coordinates for a signal element."""
    if not sig:
        return None
    raw_gps = getattr(sig, "gps", None)
    if raw_gps:
        try:
            return (float(raw_gps[0]), float(raw_gps[1]))
        except (ValueError, TypeError, IndexError):
            pass
    # Fallback: check protects_element or rear_element track GPS if available
    if elements:
        for tid in (getattr(sig, "protects_element", None), getattr(sig, "rear_element", None)):
            if tid and tid in elements:
                t_el = elements[tid]
                t_gps = getattr(t_el, "gps", None)
                if t_gps:
                    try:
                        return (float(t_gps[0]), float(t_gps[1]))
                    except (ValueError, TypeError, IndexError):
                        pass
    return None


def get_element_gps_any(el: Any, elements: Optional[Dict[str, Any]] = None) -> Optional[Tuple[float, float]]:
    """Resolve GPS coordinates for any layout element (signal, track segment, turnout)."""
    if not el:
        return None
    raw_gps = getattr(el, "gps", None)
    if raw_gps:
        try:
            return (float(raw_gps[0]), float(raw_gps[1]))
        except (ValueError, TypeError, IndexError):
            pass
    if getattr(el, "elem_type", "") == "signal" or hasattr(el, "is_permissive"):
        return get_signal_gps(el, elements)
    if elements:
        eid = getattr(el, "id", None)
        if eid:
            for other in elements.values():
                if getattr(other, "elem_type", "") == "signal" or hasattr(other, "is_permissive"):
                    if getattr(other, "protects_element", None) == eid or getattr(other, "rear_element", None) == eid:
                        sg = get_signal_gps(other, elements)
                        if sg:
                            return sg
    return None


class TrainTracker:
    """
    Maintains the list of active trains (Potential, Inferred, Confirmed)
    and user metadata assignments. Backward compatible with TrainScroller.
    """
    def __init__(self, x: int = 86, y: int = 25, max_len: int = 10, width: int = 680, height: int = 165, rf_min_packets: int = RF_BOARD_MIN_PACKETS):
        self.id = "SYS_TRAIN_SCROLLER"
        self.elem_type = "train_scroller"
        self.x = x
        self.y = y
        self.max_len = max_len
        self.width = int(width) if width and int(width) > 0 else 680
        self.height = int(height) if height and int(height) > 0 else 165
        self.rf_min_packets: int = max(1, int(rf_min_packets))
        self.trains: Dict[str, dict] = {}
        self._rf_pending: Dict[str, dict] = {}
        self._next_train_num: int = 101
        self._next_board_order: int = 1
        self._rssi_history: Dict[str, List[Tuple[float, float]]] = {}
        self._rf_events: Dict[str, List[Tuple[float, str, Any]]] = {}
        self._pair_corr_scores: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._consumed_corr_events: Set[Tuple[str, float, str, str, float, str]] = set()

    def _alloc_board_order(self) -> int:
        order = self._next_board_order
        self._next_board_order += 1
        return order

    @property
    def total_packets(self) -> int:
        return len(self.trains)

    def get_or_create_potential_train(
        self,
        sig: Any,
        path: List[Tuple[str, str]],
        downstream_sigs: List[str],
        timestamp: float
    ) -> str:
        """Finds or creates a Potential train record for a lined up path."""
        sig_id = getattr(sig, "id", "")
        sig_dir = getattr(sig, "direction", "E")
        prot_el = getattr(sig, "protects_element", "")
        all_eids = [p[0] for p in path]
        ahead_eids = [eid for eid in all_eids if eid != prot_el]

        for tkey, trn in self.trains.items():
            if trn.get("initial_signal_id") == sig_id and trn.get("status") in ("Potential", "Inferred", "Confirmed"):
                trn["downstream_signals"] = list(downstream_sigs)
                trn["ahead_elements"] = list(ahead_eids)
                trn["full_path"] = list(trn.get("passed_elements", [])) + ([trn.get("current_element_id")] if trn.get("current_element_id") else []) + list(ahead_eids)
                trn["path_element_ids"] = list(trn["full_path"])
                if not trn.get("expected_signal_id") and downstream_sigs:
                    trn["expected_signal_id"] = downstream_sigs[0]
                return tkey

        train_key = f"TRN-{self._next_train_num}"
        self._next_train_num += 1

        exp_sig = downstream_sigs[0] if downstream_sigs else None

        rec = {
            "train_key": train_key,
            "train_name": train_key,
            "board_order": self._alloc_board_order(),
            "train_source": TRAIN_SOURCE_PTC,
            "is_rf_train": False,
            "assigned_id": None,
            "assigned_name": None,
            "status": "Potential",
            "direction": sig_dir,
            "speed_mph": None,
            "speed_str": "Unk.",
            "initial_signal_id": sig_id,
            "current_signal_id": sig_id,
            "expected_signal_id": exp_sig,
            "passed_signals": [],
            "downstream_signals": list(downstream_sigs),
            "passed_elements": [],
            "current_element_id": prot_el,
            "ahead_elements": list(ahead_eids),
            "full_path": list(all_eids),
            "path_element_ids": list(all_eids),
            "last_location": f"{prot_el}",
            "is_dimmed": False,
            "timeout_start_ts": None,
            "is_receiving_packets": False,
            "packets_received": 0,
            "last_packet_ts": timestamp,
            "rssi_dbfs": None,
            "rssi_avg": None,
            "rssi_delta_db": 0.0,
            "rssi_trend": "NONE",
            "rssi_trend_label": "---",
            "psi": None,
            "motion": None,
            "marker_light": None,
            "light_str": "---",
            "rf_protocols": [],
            "created_ts": timestamp,
            "updated_ts": timestamp,
            "time_str": time.strftime("%H:%M:%S", time.localtime(timestamp)),
        }
        self.trains[train_key] = rec
        return train_key

    def assign_info(
        self,
        train_key: str,
        assigned_id: Optional[Any] = None,
        assigned_name: Optional[str] = None,
        last_packets_by_wiu: Optional[dict] = None
    ) -> dict:
        """Assigns an ID and/or Name to a train and correlates with packets."""
        matched = None
        if train_key in self.trains:
            matched = self.trains[train_key]
        else:
            for trn in self.trains.values():
                if trn.get("train_name") == train_key or str(trn.get("assigned_id")) == str(train_key):
                    matched = trn
                    break
        if not matched:
            return {"error": f"Train '{train_key}' not found"}

        if assigned_id not in (None, ""):
            matched["assigned_id"] = str(assigned_id).strip()
        elif assigned_id == "":
            matched["assigned_id"] = None

        if assigned_name not in (None, ""):
            matched["assigned_name"] = str(assigned_name).strip()
        elif assigned_name == "":
            matched["assigned_name"] = None

        if matched.get("assigned_id") and last_packets_by_wiu:
            try:
                nid = int(matched["assigned_id"])
                if nid in last_packets_by_wiu:
                    pkt = last_packets_by_wiu[nid]
                    matched["is_receiving_packets"] = True
                    matched["packets_received"] = matched.get("packets_received", 0) + 1
                    matched["last_packet_ts"] = pkt.get("timestamp", time.time())
                    if "_speed_" in pkt:
                        try:
                            s = float(pkt["_speed_"])
                            matched["speed_mph"] = round(s, 1)
                            matched["speed_str"] = f"{s:.1f} MPH"
                        except (ValueError, TypeError):
                            pass
            except (ValueError, TypeError):
                pass
        return matched

    def _update_rssi_stats(self, train_key: str, rec: dict, raw_rssi: Any, ts: float):
        """Updates sliding-average RSSI and Towards/Away/Steady movement indicator."""
        if raw_rssi is None:
            return
        try:
            rssi_val = float(raw_rssi)
        except (ValueError, TypeError):
            return
        if not math.isfinite(rssi_val) or rssi_val <= -140.0 or rssi_val >= 20.0:
            return

        hist = self._rssi_history.setdefault(train_key, [])
        hist.append((ts, rssi_val))
        cutoff = ts - RF_RSSI_MAX_AGE_SEC
        hist = [(t, r) for (t, r) in hist if t >= cutoff][-RF_RSSI_WINDOW_SIZE:]
        self._rssi_history[train_key] = hist

        vals = [r for _, r in hist]
        avg_window = vals[-6:]
        avg_rssi = sum(avg_window) / float(len(avg_window))
        delta = 0.0
        trend = "NONE"
        trend_label = "---"

        if len(hist) >= 4:
            # Compare the most recent sliding sub-window (up to 3 samples) against the
            # immediately preceding sub-window so Towards -> Away flips cleanly right after peak passage.
            half = min(3, len(hist) // 2)
            tail_vals = vals[-(2 * half):]
            old_avg = sum(tail_vals[:half]) / float(half)
            new_avg = sum(tail_vals[half:]) / float(half)
            delta = new_avg - old_avg
            if delta >= RF_RSSI_TREND_THRESH_DB:
                trend = "TOWARDS"
                trend_label = "▲TWD"
            elif delta <= -RF_RSSI_TREND_THRESH_DB:
                trend = "AWAY"
                trend_label = "▼AWY"
            else:
                trend = "STEADY"
                trend_label = "─STD"
        elif len(hist) == 3:
            old_avg = vals[0]
            new_avg = 0.5 * (vals[1] + vals[2])
            delta = new_avg - old_avg
            if delta >= RF_RSSI_TREND_THRESH_DB:
                trend = "TOWARDS"
                trend_label = "▲TWD"
            elif delta <= -RF_RSSI_TREND_THRESH_DB:
                trend = "AWAY"
                trend_label = "▼AWY"
            else:
                trend = "STEADY"
                trend_label = "─STD"
        elif len(hist) == 2 and abs(hist[1][0] - hist[0][0]) >= 1.0:
            delta = hist[1][1] - hist[0][1]
            if delta >= RF_RSSI_TREND_THRESH_DB:
                trend = "TOWARDS"
                trend_label = "▲TWD"
            elif delta <= -RF_RSSI_TREND_THRESH_DB:
                trend = "AWAY"
                trend_label = "▼AWY"
            else:
                trend = "STEADY"
                trend_label = "─STD"

        rec["rssi_dbfs"] = round(rssi_val, 1)
        rec["rssi_avg"] = round(avg_rssi, 1)
        rec["rssi_delta_db"] = round(delta, 1)
        rec["rssi_trend"] = trend
        rec["rssi_trend_label"] = trend_label

    def _record_rf_event(self, train_key: str, ts: float, event_kind: str, val: Any = None):
        ev_list = self._rf_events.setdefault(train_key, [])
        ev_list.append((ts, event_kind, val))
        # Retain recent 45s window of state-change events for correlation
        cutoff = ts - 45.0
        self._rf_events[train_key] = [e for e in ev_list if e[0] >= cutoff][-24:]

    def _apply_rf_telemetry_to_record(self, train_key: str, rec: dict, parsed_pkt: dict, proto: str, ts: float):
        """Enriches an existing train record (RF or PTC-linked) with HOT/EOT/DPU telemetry."""
        prots = list(rec.get("rf_protocols") or [])
        if proto and proto not in prots:
            prots.append(proto)
        rec["rf_protocols"] = prots

        rssi_raw = parsed_pkt.get("rssi") if parsed_pkt.get("rssi") is not None else parsed_pkt.get("rssi_dbfs")
        self._update_rssi_stats(train_key, rec, rssi_raw, ts)

        rec["is_receiving_packets"] = True
        rec["packets_received"] = int(rec.get("packets_received", 0)) + 1
        rec["last_packet_ts"] = ts
        rec["updated_ts"] = ts
        rec["time_str"] = time.strftime("%H:%M:%S", time.localtime(ts))
        rec["is_dimmed"] = False

        if proto in ("eot", "hot"):
            uid = parsed_pkt.get("unit_id")
            if uid is None:
                uid = parsed_pkt.get("_node_id_")
            if uid is not None:
                try:
                    rec["eot_unit_id"] = int(uid)
                except (ValueError, TypeError):
                    pass
            if proto == "hot":
                rec["hot_seen"] = True
                if parsed_pkt.get("emergency"):
                    rec["hot_cmd"] = "EMERG"
                elif parsed_pkt.get("armed"):
                    rec["hot_cmd"] = "ARM"
                else:
                    rec["hot_cmd"] = "STAT"
                if parsed_pkt.get("emergency"):
                    rec["emergency"] = True
            else:
                rec["eot_seen"] = True
                if parsed_pkt.get("pressure_psi") is not None:
                    try:
                        rec["psi"] = int(parsed_pkt["pressure_psi"])
                        rec["_eot_psi_ts"] = ts
                    except (ValueError, TypeError):
                        pass
                if parsed_pkt.get("motion") is not None:
                    mov = bool(parsed_pkt["motion"])
                    rec["motion"] = "MOV" if mov else "STOP"
                    rec["_eot_motion_ts"] = ts
                if parsed_pkt.get("marker_light") is not None:
                    ml = bool(parsed_pkt["marker_light"])
                    rec["marker_light"] = ml
                    rec["light_str"] = "ON" if ml else "OFF"
                if parsed_pkt.get("battery_pct") is not None:
                    rec["battery_pct"] = int(parsed_pkt["battery_pct"])
                if parsed_pkt.get("armed") is not None:
                    rec["armed"] = bool(parsed_pkt["armed"])
                if parsed_pkt.get("emergency") is not None:
                    rec["emergency"] = bool(parsed_pkt["emergency"])

        elif proto == "dpu":
            addr = parsed_pkt.get("ADDR") if parsed_pkt.get("ADDR") is not None else parsed_pkt.get("loco_id")
            trid = parsed_pkt.get("TRID")
            tp = str(parsed_pkt.get("TP") or "").strip().upper()
            power = parsed_pkt.get("Power") or parsed_pkt.get("throttle")
            rev = parsed_pkt.get("REV")
            nrm = parsed_pkt.get("NRM")
            mtr = parsed_pkt.get("MTR")
            prk = parsed_pkt.get("PRK")
            alarms = parsed_pkt.get("AL")

            if trid is not None:
                try:
                    t_i = int(trid)
                    if t_i > 0 or rec.get("dpu_trid") is None:
                        rec["dpu_trid"] = t_i
                except (ValueError, TypeError):
                    pass
            if addr is not None:
                try:
                    a_i = int(addr)
                    addrs = list(rec.get("dpu_addrs") or [])
                    if a_i not in addrs:
                        addrs.append(a_i)
                    rec["dpu_addrs"] = addrs
                except (ValueError, TypeError):
                    pass
            if nrm is not None:
                try:
                    rec["dpu_nrm"] = int(nrm)
                except (ValueError, TypeError):
                    pass
            if power:
                rec["dpu_power"] = str(power).strip().upper()
                if rec.get("is_rf_train"):
                    rec["speed_str"] = _format_dpu_short_power(rec["dpu_power"])
            if rev:
                rec["dpu_rev"] = str(rev).strip().upper()
                if rec.get("is_rf_train"):
                    rec["direction"] = rec["dpu_rev"]
            if isinstance(alarms, list):
                rec["dpu_alarms"] = [str(a) for a in alarms]
            if tp == "ST" and parsed_pkt.get("BP") is not None:
                try:
                    # Prefer fresher EOT PSI if received within the last 15s, else update from DPU ST BP
                    if (ts - float(rec.get("_eot_psi_ts") or 0.0)) > 15.0 or rec.get("psi") is None:
                        rec["psi"] = int(parsed_pkt["BP"])
                except (ValueError, TypeError):
                    pass
            # Only infer motion from DPU if EOT has not reported motion recently
            if (ts - float(rec.get("_eot_motion_ts") or 0.0)) > 30.0 or rec.get("motion") is None:
                pwr_u = str(power or "").upper()
                if mtr == 1 or (pwr_u.startswith("N") and pwr_u[1:].isdigit()):
                    rec["motion"] = "MOV"
                elif prk == 1 or pwr_u == "SETOT":
                    rec["motion"] = "STOP"

        # Refresh human-readable RF summary info column
        if rec.get("is_rf_train"):
            info_parts: List[str] = []
            if rec.get("eot_unit_id") is not None:
                if rec.get("hot_seen") and rec.get("hot_cmd"):
                    info_parts.append(f"HOT:{rec['hot_cmd']}")
                elif rec.get("armed"):
                    info_parts.append("Arm")
                if rec.get("emergency"):
                    info_parts.append("EMERG")
            if rec.get("dpu_trid") or rec.get("dpu_addrs"):
                if rec.get("dpu_nrm"):
                    info_parts.append(f"{rec['dpu_nrm']}rem")
                if rec.get("dpu_alarms"):
                    info_parts.append(f"AL:{','.join(rec['dpu_alarms'])}")
            if rec.get("merged_confidence"):
                info_parts.append("Linked")
            rec["last_location"] = " ".join(info_parts) if info_parts else (rec.get("train_source") or "RF")

    def _merge_eot_and_dpu_trains(self, eot_key: str, dpu_key: str, score: float, now: float) -> Optional[str]:
        """Merges a correlated standalone HOT/EOT train with a standalone DPU consist into one HOT/EOT/DPU entry."""
        eot_rec = self.trains.get(eot_key)
        dpu_rec = self.trains.get(dpu_key)
        if not eot_rec or not dpu_rec:
            return None

        # Keep EOT key, EOT ID, and exact board_order row position; mark source as HOT/EOT/DPU
        merged = dict(eot_rec)
        merged["train_source"] = TRAIN_SOURCE_HOT_EOT_DPU
        merged["is_rf_train"] = True
        merged["merged_confidence"] = round(float(score), 1)
        merged["merged_ts"] = now

        if not merged.get("assigned_name") and dpu_rec.get("assigned_name"):
            merged["assigned_name"] = dpu_rec["assigned_name"]
        if not merged.get("assigned_id") and dpu_rec.get("assigned_id"):
            merged["assigned_id"] = dpu_rec["assigned_id"]

        eot_uid = merged.get("eot_unit_id")
        dpu_trid = dpu_rec.get("dpu_trid")
        dpu_addrs = list(dpu_rec.get("dpu_addrs") or [])
        # Always use the reliable 5-digit EOT ID as the train name (TRID can be 0 / unreliable)
        if eot_uid is not None:
            merged["train_name"] = f"EOT #{int(eot_uid):05d}"

        merged["dpu_trid"] = dpu_trid if (dpu_trid and int(dpu_trid) > 0) else None
        merged["dpu_addrs"] = dpu_addrs
        merged["dpu_nrm"] = dpu_rec.get("dpu_nrm", 0)
        merged["dpu_power"] = dpu_rec.get("dpu_power")
        merged["dpu_rev"] = dpu_rec.get("dpu_rev")
        merged["dpu_alarms"] = list(dpu_rec.get("dpu_alarms") or [])
        if merged.get("dpu_power"):
            merged["speed_str"] = _format_dpu_short_power(merged["dpu_power"])
        if merged.get("dpu_rev"):
            merged["direction"] = merged["dpu_rev"]
        if merged.get("psi") is None and dpu_rec.get("psi") is not None:
            merged["psi"] = dpu_rec["psi"]
        if merged.get("motion") is None and dpu_rec.get("motion") is not None:
            merged["motion"] = dpu_rec["motion"]

        prots = list(merged.get("rf_protocols") or [])
        for p in (dpu_rec.get("rf_protocols") or []):
            if p not in prots:
                prots.append(p)
        merged["rf_protocols"] = prots
        merged["packets_received"] = int(eot_rec.get("packets_received", 0)) + int(dpu_rec.get("packets_received", 0))
        merged["last_packet_ts"] = max(float(eot_rec.get("last_packet_ts", 0.0)), float(dpu_rec.get("last_packet_ts", 0.0)))
        merged["updated_ts"] = max(float(eot_rec.get("updated_ts", 0.0)), float(dpu_rec.get("updated_ts", 0.0)))

        # Combine RSSI sliding histories sorted by timestamp
        comb_rssi = sorted(
            list(self._rssi_history.get(eot_key, [])) + list(self._rssi_history.get(dpu_key, [])),
            key=lambda x: x[0],
        )[-RF_RSSI_WINDOW_SIZE:]
        if comb_rssi:
            self._rssi_history[eot_key] = comb_rssi[:-1]
            self._update_rssi_stats(eot_key, merged, comb_rssi[-1][1], comb_rssi[-1][0])

        # Refresh summary location/info string
        info_parts: List[str] = []
        if merged.get("hot_seen") and merged.get("hot_cmd"):
            info_parts.append(f"HOT:{merged['hot_cmd']}")
        elif merged.get("armed"):
            info_parts.append("Arm")
        if merged.get("dpu_nrm"):
            info_parts.append(f"{merged['dpu_nrm']}rem")
        if merged.get("dpu_alarms"):
            info_parts.append(f"AL:{','.join(merged['dpu_alarms'])}")
        info_parts.append("Linked")
        merged["last_location"] = " ".join(info_parts)

        self.trains[eot_key] = merged
        self.trains.pop(dpu_key, None)
        self._rssi_history.pop(dpu_key, None)
        self._rf_events.pop(eot_key, None)
        self._rf_events.pop(dpu_key, None)
        self._pair_corr_scores = {
            pk: sc for pk, sc in self._pair_corr_scores.items()
            if eot_key not in pk and dpu_key not in pk
        }
        return eot_key

    def _attempt_eot_dpu_correlation(self, trigger_key: str, now: float):
        """
        Infers whether an unmerged HOT/EOT train belongs to an unmerged DPU consist by
        scoring sequential correlated state changes:
          - DPU brake reduction / BP drop -> EOT pressure drop within 0..12s (+3, +1 if PSI matches within 6)
          - DPU brake release / BP rise   -> EOT pressure rise within 0..12s (+2)
          - DPU traction start / stop     -> EOT motion start / stop within 0..15s (+2)
          - Simultaneous Emergency        -> +4
        Merges only when confidence >= 4.0 and unambiguously ahead of any rival candidate (+2.0 margin).
        """
        trig = self.trains.get(trigger_key)
        if not trig or not trig.get("is_rf_train"):
            return
        src = trig.get("train_source")
        if src not in (TRAIN_SOURCE_HOT_EOT, TRAIN_SOURCE_DPU):
            return

        # Decay stale pair scores (> 180s without reinforcement)
        self._pair_corr_scores = {
            pk: (sc, pts) for pk, (sc, pts) in self._pair_corr_scores.items()
            if (now - pts) <= 180.0 and pk[0] in self.trains and pk[1] in self.trains
        }

        if src == TRAIN_SOURCE_HOT_EOT:
            eot_keys = [trigger_key]
            dpu_keys = [k for k, t in self.trains.items() if t.get("train_source") == TRAIN_SOURCE_DPU]
        else:
            eot_keys = [k for k, t in self.trains.items() if t.get("train_source") == TRAIN_SOURCE_HOT_EOT]
            dpu_keys = [trigger_key]

        if not eot_keys or not dpu_keys:
            return

        for ek in eot_keys:
            e_evs = self._rf_events.get(ek, [])
            if not e_evs:
                continue
            for dk in dpu_keys:
                d_evs = self._rf_events.get(dk, [])
                if not d_evs:
                    continue
                pair_k = (ek, dk)
                cur_score, cur_ts = self._pair_corr_scores.get(pair_k, (0.0, now))
                added = 0.0
                for d_ts, d_kind, d_val in d_evs:
                    for e_ts, e_kind, e_val in e_evs:
                        dt = e_ts - d_ts  # Positive when DPU command precedes EOT response
                        sig_tok = (dk, round(d_ts, 3), d_kind, ek, round(e_ts, 3), e_kind)
                        if sig_tok in self._consumed_corr_events:
                            continue
                        pts = 0.0
                        if d_kind == "bp_drop" and e_kind == "bp_drop" and -2.0 <= dt <= RF_CORRELATION_WINDOW_SEC:
                            pts = 3.0
                            if isinstance(d_val, (int, float)) and isinstance(e_val, (int, float)):
                                if abs(float(d_val) - float(e_val)) <= 6.0:
                                    pts += 1.0
                        elif d_kind == "bp_rise" and e_kind == "bp_rise" and -2.0 <= dt <= RF_CORRELATION_WINDOW_SEC:
                            pts = 2.0
                        elif d_kind == "motion_start" and e_kind == "motion_start" and -2.0 <= dt <= RF_MOTION_CORR_WINDOW_SEC:
                            pts = 2.0
                        elif d_kind == "motion_stop" and e_kind == "motion_stop" and -2.0 <= dt <= RF_MOTION_CORR_WINDOW_SEC:
                            pts = 2.0
                        elif d_kind == "emergency" and e_kind == "emergency" and abs(dt) <= 8.0:
                            pts = 4.0
                        if pts > 0.0:
                            self._consumed_corr_events.add(sig_tok)
                            if len(self._consumed_corr_events) > 512:
                                self._consumed_corr_events.clear()
                            added += pts
                if added > 0.0:
                    self._pair_corr_scores[pair_k] = (cur_score + added, now)

        # Evaluate whether any EOT candidate has an unambiguous winning DPU match (score >= 4.0, lead >= 2.0)
        for ek in list(eot_keys):
            if ek not in self.trains or self.trains[ek].get("train_source") != TRAIN_SOURCE_HOT_EOT:
                continue
            cand_scores = sorted(
                [(sc, dk) for (pek, dk), (sc, _) in self._pair_corr_scores.items()
                 if pek == ek and dk in self.trains and self.trains[dk].get("train_source") == TRAIN_SOURCE_DPU],
                reverse=True,
            )
            if not cand_scores:
                continue
            best_sc, best_dk = cand_scores[0]
            second_sc = cand_scores[1][0] if len(cand_scores) > 1 else 0.0
            # Also ensure best_dk does not have a competing higher-scoring EOT match
            rival_eot = [
                sc for (pek, pdk), (sc, _) in self._pair_corr_scores.items()
                if pdk == best_dk and pek != ek and pek in self.trains
            ]
            rival_max = max(rival_eot) if rival_eot else 0.0
            if best_sc >= 4.0 and (best_sc - second_sc) >= 2.0 and (best_sc - rival_max) >= 2.0:
                self._merge_eot_and_dpu_trains(ek, best_dk, best_sc, now)

    def check_packet(self, parsed_pkt: dict):
        """
        Rule 10 + Unified RF Train Tracking (EMP / PTC Inferred / HOT / EOT / DPU):
          - If a packet matches a user-assigned train ID (_node_id_ / unit_id / ADDR / TRID),
            updates RX indicator, _speed_, and enriches that train with any RF telemetry.
          - Otherwise, tracks standalone HOT/EOT (unified by 5-digit EOT unit_id) and DPU
            consists (unified by TRID) as RF trains with sliding-avg RSSI & Towards/Away trend,
            and runs sequential state-change correlation to merge EOT/HOT + DPU when proven.
          - Supports future EMP protocol packets with source 'EMP'.
        """
        if not isinstance(parsed_pkt, dict):
            return
        ts = _coerce_packet_ts(parsed_pkt.get("timestamp"))
        proto = str(parsed_pkt.get("protocol") or "").strip().lower()

        # 0. Future EMP source hook
        if proto == "emp" or parsed_pkt.get("_emp_train_"):
            emp_id = parsed_pkt.get("train_id") or parsed_pkt.get("_node_id_") or parsed_pkt.get("unit_id") or "EMP"
            tkey = f"EMP-{emp_id}"
            rec = self.trains.get(tkey)
            if rec is None:
                rec = {
                    "train_key": tkey,
                    "train_name": f"EMP #{emp_id}",
                    "board_order": self._alloc_board_order(),
                    "train_source": TRAIN_SOURCE_EMP,
                    "is_rf_train": True,
                    "assigned_id": str(emp_id),
                    "assigned_name": parsed_pkt.get("train_name"),
                    "status": "Active",
                    "direction": str(parsed_pkt.get("direction") or "---"),
                    "speed_mph": parsed_pkt.get("_speed_"),
                    "speed_str": f"{float(parsed_pkt['_speed_']):.1f} MPH" if parsed_pkt.get("_speed_") is not None else "Unk.",
                    "last_location": str(parsed_pkt.get("location") or "EMP"),
                    "is_dimmed": False,
                    "is_receiving_packets": True,
                    "packets_received": 0,
                    "last_packet_ts": ts,
                    "rssi_dbfs": None,
                    "rssi_avg": None,
                    "rssi_delta_db": 0.0,
                    "rssi_trend": "NONE",
                    "rssi_trend_label": "---",
                    "psi": parsed_pkt.get("pressure_psi"),
                    "motion": None,
                    "marker_light": None,
                    "light_str": "---",
                    "rf_protocols": ["emp"],
                    "created_ts": ts,
                    "updated_ts": ts,
                    "time_str": time.strftime("%H:%M:%S", time.localtime(ts)),
                }
                self.trains[tkey] = rec
            self._apply_rf_telemetry_to_record(tkey, rec, parsed_pkt, "emp", ts)
            if parsed_pkt.get("_speed_") is not None:
                try:
                    s = float(parsed_pkt["_speed_"])
                    rec["speed_mph"] = round(s, 1)
                    rec["speed_str"] = f"{s:.1f} MPH"
                except (ValueError, TypeError):
                    pass
            return

        # 1. Rule 10: Check if packet matches any existing train's user-assigned ID
        candidate_ids: Set[str] = set()
        for fld in ("_node_id_", "unit_id", "ADDR", "loco_id", "TRID"):
            v = parsed_pkt.get(fld)
            if v is not None and str(v).strip() != "":
                # TRID=0 is common/unlinked on DPU frames -> never treat "0" as a matchable train ID
                if fld == "TRID":
                    try:
                        if int(v) == 0:
                            continue
                    except (ValueError, TypeError):
                        pass
                candidate_ids.add(str(v).strip())
                if fld == "unit_id":
                    try:
                        candidate_ids.add(f"{int(v):05d}")
                    except (ValueError, TypeError):
                        pass

        matched_assigned_non_rf = False
        if candidate_ids:
            for tkey, trn in self.trains.items():
                aid = trn.get("assigned_id")
                if aid is not None and str(aid).strip() in candidate_ids:
                    if proto in ("eot", "hot", "dpu"):
                        self._apply_rf_telemetry_to_record(tkey, trn, parsed_pkt, proto, ts)
                        if not trn.get("is_rf_train"):
                            matched_assigned_non_rf = True
                    else:
                        trn["is_receiving_packets"] = True
                        trn["packets_received"] = int(trn.get("packets_received", 0)) + 1
                        trn["last_packet_ts"] = ts
                        rssi_raw = parsed_pkt.get("rssi") if parsed_pkt.get("rssi") is not None else parsed_pkt.get("rssi_dbfs")
                        self._update_rssi_stats(tkey, trn, rssi_raw, ts)
                    if "_speed_" in parsed_pkt and parsed_pkt["_speed_"] is not None:
                        try:
                            s = float(parsed_pkt["_speed_"])
                            trn["speed_mph"] = round(s, 1)
                            trn["speed_str"] = f"{s:.1f} MPH"
                        except (ValueError, TypeError):
                            pass

        # If a HOT/EOT/DPU packet already attached to a user-assigned PTC track train, do not create a duplicate RF row
        if matched_assigned_non_rf:
            return

        # 2. Standalone HOT / EOT tracking (unified by shared 5-digit AAR S-9152 unit_id)
        if proto in ("eot", "hot"):
            uid_raw = parsed_pkt.get("unit_id")
            if uid_raw is None:
                uid_raw = parsed_pkt.get("_node_id_")
            if uid_raw is None:
                return
            try:
                uid = int(uid_raw)
            except (ValueError, TypeError):
                return
            # AAR S-5701 / S-9152 EOT unit IDs are strictly 5 decimal digits (00001..99999)
            if not (1 <= uid <= 99999):
                return

            # Find existing EOT/HOT or already-merged HOT/EOT/DPU train on the board OR in pending staging
            target_key = None
            in_board = False
            for k, trn in self.trains.items():
                if trn.get("is_rf_train") and trn.get("eot_unit_id") == uid:
                    target_key = k
                    in_board = True
                    break
            if target_key is None:
                for k, trn in self._rf_pending.items():
                    if trn.get("eot_unit_id") == uid:
                        target_key = k
                        break
            if target_key is None:
                target_key = f"RF-EOT-{uid:05d}"
                new_rec = {
                    "train_key": target_key,
                    "train_name": f"EOT #{uid:05d}",
                    "train_source": TRAIN_SOURCE_HOT_EOT,
                    "is_rf_train": True,
                    "assigned_id": None,
                    "assigned_name": None,
                    "status": "Active",
                    "direction": "---",
                    "speed_mph": None,
                    "speed_str": "---",
                    "last_location": "HOT/EOT",
                    "is_dimmed": False,
                    "is_receiving_packets": True,
                    "packets_received": 0,
                    "last_packet_ts": ts,
                    "eot_unit_id": uid,
                    "hot_seen": False,
                    "eot_seen": False,
                    "hot_cmd": None,
                    "rssi_dbfs": None,
                    "rssi_avg": None,
                    "rssi_delta_db": 0.0,
                    "rssi_trend": "NONE",
                    "rssi_trend_label": "---",
                    "psi": None,
                    "motion": None,
                    "marker_light": None,
                    "light_str": "---",
                    "rf_protocols": [],
                    "created_ts": ts,
                    "updated_ts": ts,
                    "time_str": time.strftime("%H:%M:%S", time.localtime(ts)),
                }
                if self.rf_min_packets <= 1:
                    new_rec["board_order"] = self._alloc_board_order()
                    self.trains[target_key] = new_rec
                    in_board = True
                else:
                    self._rf_pending[target_key] = new_rec

            rec = self.trains[target_key] if in_board else self._rf_pending[target_key]
            prev_psi = rec.get("psi")
            prev_mov = rec.get("motion")
            prev_emg = bool(rec.get("emergency", False))

            self._apply_rf_telemetry_to_record(target_key, rec, parsed_pkt, proto, ts)

            # Promote staged RF candidate onto Train Board once it reaches rf_min_packets (default: 2)
            if not in_board and int(rec.get("packets_received", 0)) >= self.rf_min_packets:
                rec["board_order"] = self._alloc_board_order()
                self.trains[target_key] = rec
                self._rf_pending.pop(target_key, None)
                in_board = True

            # Log state-change events for sequential EOT <-> DPU correlation
            if rec.get("train_source") == TRAIN_SOURCE_HOT_EOT:
                new_psi = rec.get("psi")
                if prev_psi is not None and new_psi is not None:
                    if (new_psi - prev_psi) <= -2:
                        self._record_rf_event(target_key, ts, "bp_drop", new_psi)
                    elif (new_psi - prev_psi) >= 2:
                        self._record_rf_event(target_key, ts, "bp_rise", new_psi)
                new_mov = rec.get("motion")
                if prev_mov is not None and new_mov is not None and new_mov != prev_mov:
                    self._record_rf_event(target_key, ts, "motion_start" if new_mov == "MOV" else "motion_stop", new_mov)
                if bool(rec.get("emergency", False)) and not prev_emg:
                    self._record_rf_event(target_key, ts, "emergency", True)
                if in_board:
                    self._attempt_eot_dpu_correlation(target_key, ts)
            return

        # 3. Standalone DPU tracking: look up primarily by permanent 16-bit hardware ADDR
        # (TRID is often 0 / unreliable across trains; only group by TRID when TRID > 0).
        # Never add unverified CRC-less DPU_BURST hits to the Train Board!
        if proto == "dpu":
            if parsed_pkt.get("crc_ok") is False or str(parsed_pkt.get("type_name") or "").upper() == "DPU_BURST":
                return
            trid_raw = parsed_pkt.get("TRID")
            addr_raw = parsed_pkt.get("ADDR") if parsed_pkt.get("ADDR") is not None else parsed_pkt.get("loco_id")
            if addr_raw is None:
                addr_raw = parsed_pkt.get("_node_id_")
            try:
                trid = int(trid_raw or 0)
            except (ValueError, TypeError):
                trid = 0
            try:
                addr = int(addr_raw or 0)
            except (ValueError, TypeError):
                addr = 0
            if trid == 0 and addr == 0:
                return

            target_key = None
            in_board = False
            # Priority 1: Exact hardware ADDR lookup on board (standalone DPU or merged HOT/EOT/DPU)
            if addr > 0:
                for k, trn in self.trains.items():
                    if trn.get("is_rf_train") and addr in (trn.get("dpu_addrs") or []):
                        target_key = k
                        in_board = True
                        break
            # Priority 2: Non-zero TRID consist lookup on board
            if target_key is None and trid > 0:
                for k, trn in self.trains.items():
                    if trn.get("is_rf_train") and trn.get("dpu_trid") == trid:
                        target_key = k
                        in_board = True
                        break
            # Priority 3: Check pending staging candidates
            if target_key is None and addr > 0:
                for k, trn in self._rf_pending.items():
                    if addr in (trn.get("dpu_addrs") or []):
                        target_key = k
                        break
            if target_key is None and trid > 0:
                for k, trn in self._rf_pending.items():
                    if trn.get("dpu_trid") == trid:
                        target_key = k
                        break
            if target_key is None:
                target_key = f"RF-DPU-{addr}" if addr > 0 else f"RF-DPU-T{trid}"
                dname = f"DPU #{addr}" if addr > 0 else f"DPU Tr:{trid}"
                new_rec = {
                    "train_key": target_key,
                    "train_name": dname,
                    "train_source": TRAIN_SOURCE_DPU,
                    "is_rf_train": True,
                    "assigned_id": None,
                    "assigned_name": None,
                    "status": "Active",
                    "direction": "---",
                    "speed_mph": None,
                    "speed_str": "---",
                    "last_location": "DPU",
                    "is_dimmed": False,
                    "is_receiving_packets": True,
                    "packets_received": 0,
                    "last_packet_ts": ts,
                    "dpu_trid": trid if trid > 0 else None,
                    "dpu_addrs": [addr] if addr > 0 else [],
                    "dpu_nrm": int(parsed_pkt.get("NRM") or 0),
                    "dpu_power": None,
                    "dpu_rev": None,
                    "dpu_alarms": [],
                    "rssi_dbfs": None,
                    "rssi_avg": None,
                    "rssi_delta_db": 0.0,
                    "rssi_trend": "NONE",
                    "rssi_trend_label": "---",
                    "psi": None,
                    "motion": None,
                    "marker_light": None,
                    "light_str": "---",
                    "rf_protocols": [],
                    "created_ts": ts,
                    "updated_ts": ts,
                    "time_str": time.strftime("%H:%M:%S", time.localtime(ts)),
                }
                if self.rf_min_packets <= 1:
                    new_rec["board_order"] = self._alloc_board_order()
                    self.trains[target_key] = new_rec
                    in_board = True
                else:
                    self._rf_pending[target_key] = new_rec

            rec = self.trains[target_key] if in_board else self._rf_pending[target_key]
            prev_psi = rec.get("psi")
            prev_pwr = str(rec.get("dpu_power") or "").upper()
            prev_mtr = 1 if (prev_pwr.startswith("N") and prev_pwr[1:].isdigit()) else 0
            prev_bpred = rec.get("_last_dpu_bpred")

            self._apply_rf_telemetry_to_record(target_key, rec, parsed_pkt, proto, ts)

            # Promote staged DPU candidate onto Train Board once it reaches rf_min_packets (default: 2)
            if not in_board and int(rec.get("packets_received", 0)) >= self.rf_min_packets:
                rec["board_order"] = self._alloc_board_order()
                self.trains[target_key] = rec
                self._rf_pending.pop(target_key, None)
                in_board = True

            # Log state-change events for sequential DPU <-> EOT correlation
            if rec.get("train_source") == TRAIN_SOURCE_DPU:
                tp = str(parsed_pkt.get("TP") or "").upper()
                bpred = parsed_pkt.get("BPRED")
                new_psi = rec.get("psi")
                new_pwr = str(rec.get("dpu_power") or "").upper()
                new_mtr = 1 if (parsed_pkt.get("MTR") == 1 or (new_pwr.startswith("N") and new_pwr[1:].isdigit())) else 0
                alarms = [str(a).upper() for a in (parsed_pkt.get("AL") or [])]

                if tp == "CM":
                    if bpred == "EMER":
                        self._record_rf_event(target_key, ts, "emergency", True)
                        self._record_rf_event(target_key, ts, "bp_drop", None)
                    elif isinstance(bpred, (int, float)) and bpred > 0 and bpred != prev_bpred:
                        est_bp = (prev_psi - int(bpred)) if prev_psi is not None else None
                        self._record_rf_event(target_key, ts, "bp_drop", est_bp)
                    elif bpred == "REL" and isinstance(prev_bpred, (int, float)) and prev_bpred > 0:
                        self._record_rf_event(target_key, ts, "bp_rise", None)
                    rec["_last_dpu_bpred"] = bpred
                elif tp == "ST":
                    if prev_psi is not None and new_psi is not None:
                        if (new_psi - prev_psi) <= -2:
                            self._record_rf_event(target_key, ts, "bp_drop", new_psi)
                        elif (new_psi - prev_psi) >= 2:
                            self._record_rf_event(target_key, ts, "bp_rise", new_psi)
                    if "EMR" in alarms or "BPDR" in alarms:
                        self._record_rf_event(target_key, ts, "emergency", True)

                if prev_pwr and new_pwr != prev_pwr:
                    if prev_mtr == 0 and new_mtr == 1:
                        self._record_rf_event(target_key, ts, "motion_start", new_pwr)
                    elif prev_mtr == 1 and (new_mtr == 0 or new_pwr == "SETOT" or parsed_pkt.get("PRK") == 1):
                        self._record_rf_event(target_key, ts, "motion_stop", new_pwr)
                if in_board:
                    self._attempt_eot_dpu_correlation(target_key, ts)

    def update_or_assign_train(
        self,
        elem_id: str,
        elem_label: str,
        prev_elem_id: Optional[str],
        speed_mph: Optional[float],
        direction: Optional[str],
        timestamp: float,
        known_name: Optional[str] = None
    ) -> str:
        """Updates or advances an active train to a newly occupied element."""
        train_name = known_name
        if not train_name and prev_elem_id:
            for tname, info in self.trains.items():
                if info.get("last_element_id") == prev_elem_id and (timestamp - info.get("updated_ts", 0)) < 45.0:
                    train_name = tname
                    break
        if not train_name:
            for tname, info in self.trains.items():
                if info.get("last_element_id") == elem_id or info.get("current_element_id") == elem_id:
                    train_name = tname
                    break
        if not train_name:
            train_name = f"TRN-{self._next_train_num}"
            self._next_train_num += 1

        prev_rec = self.trains.get(train_name, {})
        resolved_speed = speed_mph if speed_mph is not None else prev_rec.get("speed_mph")
        resolved_dir = direction or prev_rec.get("direction", "E")

        rec = dict(prev_rec)
        if "board_order" not in rec:
            rec["board_order"] = self._alloc_board_order()
        rec.update({
            "train_key": train_name,
            "train_name": train_name,
            "train_source": rec.get("train_source", TRAIN_SOURCE_PTC),
            "is_rf_train": rec.get("is_rf_train", False),
            "status": rec.get("status", "Inferred"),
            "speed_mph": resolved_speed,
            "speed_str": f"{resolved_speed:.1f} MPH" if resolved_speed is not None else rec.get("speed_str", "Unk."),
            "direction": resolved_dir,
            "last_element_id": elem_id,
            "current_element_id": elem_id,
            "last_location": f"{elem_id} ({elem_label})" if elem_label else elem_id,
            "updated_ts": timestamp,
            "time_str": time.strftime("%H:%M:%S", time.localtime(timestamp)),
            "is_dimmed": rec.get("is_dimmed", False),
            "is_receiving_packets": rec.get("is_receiving_packets", False),
            "packets_received": rec.get("packets_received", 0),
            "last_packet_ts": rec.get("last_packet_ts") or timestamp,
        })
        rec.setdefault("rssi_dbfs", None)
        rec.setdefault("rssi_avg", None)
        rec.setdefault("rssi_delta_db", 0.0)
        rec.setdefault("rssi_trend", "NONE")
        rec.setdefault("rssi_trend_label", "---")
        rec.setdefault("psi", None)
        rec.setdefault("motion", None)
        rec.setdefault("marker_light", None)
        rec.setdefault("light_str", "---")
        rec.setdefault("rf_protocols", [])
        self.trains[train_name] = rec
        return train_name

    def apply_rf_location_estimates(self, estimates: List[dict]) -> int:
        """
        Enriches active RF trains (HOT/EOT/DPU) with anonymized multi-SDR RF location
        and heading estimates fetched from Banc Cloud (GET_ESTIMATES category='train').
        Returns the number of train rows updated.
        """
        if not estimates or not self.trains:
            return 0
        by_eot: Dict[str, dict] = {}
        by_dpu: Dict[str, dict] = {}
        for est in estimates:
            if not isinstance(est, dict):
                continue
            tx_key = str(est.get("tx_key") or "").strip().lower()
            uid = str(est.get("unit_id") or "").strip()
            proto = str(est.get("proto") or "").strip().lower()
            if not uid or est.get("est_lat") is None or est.get("est_lon") is None:
                continue
            if proto in ("eot", "hot") or tx_key.startswith(("eot:", "hot:")):
                norm_u = uid.zfill(5) if uid.isdigit() else uid
                prev = by_eot.get(norm_u)
                # Prefer higher-confidence or newer estimate when both HOT and EOT exist
                if prev is None or float(est.get("updated_ts", 0.0)) >= float(prev.get("updated_ts", 0.0)):
                    by_eot[norm_u] = est
            elif proto == "dpu" or tx_key.startswith("dpu:"):
                prev = by_dpu.get(uid)
                if prev is None or float(est.get("updated_ts", 0.0)) >= float(prev.get("updated_ts", 0.0)):
                    by_dpu[uid] = est

        updated = 0
        for rec in self.trains.values():
            if not rec.get("is_rf_train"):
                continue
            matched = None
            eot_id = rec.get("eot_id")
            if eot_id is not None:
                norm_e = str(eot_id).strip().zfill(5) if str(eot_id).strip().isdigit() else str(eot_id).strip()
                matched = by_eot.get(norm_e)
            if matched is None:
                dpu_addr = rec.get("dpu_addr") or rec.get("dpu_id")
                if dpu_addr is not None:
                    matched = by_dpu.get(str(dpu_addr).strip())
            if matched is None:
                continue

            est_lat = float(matched["est_lat"])
            est_lon = float(matched["est_lon"])
            est_rad_km = float(matched.get("est_radius_km", 6.0))
            est_rad_mi = est_rad_km * 0.621371192
            conf = str(matched.get("confidence") or "low")
            hdg_deg = matched.get("heading_deg")
            hdg_card = str(matched.get("heading_cardinal") or "").strip()
            spd_mph = matched.get("speed_mph")

            rec["est_gps"] = [est_lat, est_lon]
            rec["est_radius_km"] = round(est_rad_km, 2)
            rec["est_confidence"] = conf
            rec["est_heading_deg"] = hdg_deg
            rec["est_heading_cardinal"] = hdg_card
            rec["est_speed_mph"] = spd_mph
            rec["est_sample_count"] = int(matched.get("sample_count", 0))
            rec["est_updated_ts"] = float(matched.get("updated_ts", 0.0))

            hdg_part = f" {hdg_card}" if hdg_card else ""
            rec["last_location"] = f"RF~{est_lat:.3f},{est_lon:.3f} (±{est_rad_mi:.1f}mi{hdg_part})"
            if hdg_card and str(rec.get("direction") or "").strip() in ("", "---", "?", "Unk."):
                rec["direction"] = f"~{hdg_card}"
            updated += 1
        return updated

    def delete_train(self, train_key: str) -> Optional[dict]:
        """Deletes a train by its key or assigned identifier."""
        matched_key = None
        if train_key in self.trains:
            matched_key = train_key
        else:
            for k, trn in self.trains.items():
                if trn.get("train_name") == train_key or str(trn.get("assigned_id")) == str(train_key):
                    matched_key = k
                    break
        if matched_key:
            self._rssi_history.pop(matched_key, None)
            self._rf_events.pop(matched_key, None)
            self._pair_corr_scores = {
                pk: sc for pk, sc in self._pair_corr_scores.items()
                if matched_key not in pk
            }
            return self.trains.pop(matched_key, None)
        return None

    def prune_rf_trains(self, now: Optional[float] = None, rf_max_age: float = RF_TRAIN_TIMEOUT_SEC) -> List[str]:
        """Deletes EOT/HOT, DPU, and EMP RF trains (and stale 1-packet candidates) after 7 minutes (420s) of inactivity."""
        cur_now = float(now) if now is not None else time.time()
        expired_pending = [
            tkey for tkey, rec in self._rf_pending.items()
            if (cur_now - float(rec.get("last_packet_ts") or rec.get("updated_ts") or 0.0)) >= float(rf_max_age)
        ]
        for tkey in expired_pending:
            self._rf_pending.pop(tkey, None)
            self._rssi_history.pop(tkey, None)
            self._rf_events.pop(tkey, None)

        expired = [
            tkey for tkey, rec in self.trains.items()
            if rec.get("is_rf_train")
            and (cur_now - float(rec.get("last_packet_ts") or rec.get("updated_ts") or 0.0)) >= float(rf_max_age)
        ]
        for tkey in expired:
            self.delete_train(tkey)
        return expired

    def prune_inactive(self, active_elem_ids: set, now: float, max_age: float = 180.0):
        """Cleans up stale orphaned PTC records (max_age) and expired RF trains (7 minutes / 420s)."""
        self.prune_rf_trains(now=now, rf_max_age=RF_TRAIN_TIMEOUT_SEC)
        to_delete = [
            tname for tname, rec in self.trains.items()
            if not rec.get("is_rf_train")
            and rec.get("status") != "Potential"
            and rec.get("current_element_id") not in active_elem_ids
            and rec.get("last_element_id") not in active_elem_ids
            and (now - rec.get("updated_ts", 0.0)) > max_age
        ]
        for tname in to_delete:
            del self.trains[tname]

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None, now: Optional[float] = None) -> dict:
        """Serializes all trains on the Train Board in stable row order (no row jumping on updates)."""
        cur_now = float(now) if now is not None else time.time()
        self.prune_rf_trains(now=cur_now, rf_max_age=RF_TRAIN_TIMEOUT_SEC)
        # Keep stable board_order ascending so rows stay pinned in place until deleted
        for r in self.trains.values():
            if "board_order" not in r:
                r["board_order"] = self._alloc_board_order()
        sorted_raw = sorted(
            self.trains.values(),
            key=lambda r: (int(r.get("board_order", 0)), float(r.get("created_ts", 0.0))),
        )
        sorted_trains: List[dict] = []
        for rec in sorted_raw:
            out_r = {k: v for k, v in rec.items() if not k.startswith("_")}
            out_r.setdefault("train_source", TRAIN_SOURCE_PTC)
            out_r.setdefault("is_rf_train", False)
            out_r.setdefault("rssi_avg", None)
            out_r.setdefault("rssi_trend", "NONE")
            out_r.setdefault("rssi_trend_label", "---")
            out_r.setdefault("psi", None)
            out_r.setdefault("motion", None)
            out_r.setdefault("marker_light", None)
            out_r.setdefault("light_str", "---")
            ref_ts = float(out_r.get("last_packet_ts") or out_r.get("updated_ts") or cur_now)
            age_s = max(0.0, cur_now - ref_ts)
            max_age_s = RF_TRAIN_TIMEOUT_SEC if out_r.get("is_rf_train") else 180.0
            out_r["age_sec"] = round(age_s, 1)
            out_r["rf_timeout_sec"] = RF_TRAIN_TIMEOUT_SEC if out_r.get("is_rf_train") else None
            out_r["fade_factor"] = round(compute_train_fade_factor(age_s, max_age_s), 3)
            sorted_trains.append(out_r)
        last_ts = max((r.get("updated_ts", 0.0) for r in self.trains.values()), default=0.0)
        return {
            "id": self.id,
            "elem_type": self.elem_type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "max_len": self.max_len,
            "packets_received": len(self.trains),
            "last_packet_ts": last_ts,
            "last_status": f"{len(self.trains)} train(s) tracked",
            "trains": sorted_trains,
        }


# Maintain backward-compatible class aliases
TrainScroller = TrainTracker
TrainBoard = TrainTracker


class TrainInferenceManager:
    """
    Encapsulates railroad layout path tracing, signal event correlation,
    potential train de-duplication, speed estimation, dimming, and timeout logic.
    """

    _cached_elem_id: Optional[int] = None
    _cached_elem_len: int = -1
    _cached_jumps_by_conn: Dict[str, Any] = {}
    _cached_sig_boundaries: Set[Tuple[str, str, str]] = set()
    _cached_sigs_by_elem: Dict[str, List[Any]] = {}
    _cached_reachable: Dict[Tuple[str, str, int], Set[str]] = {}

    @classmethod
    def invalidate_caches(cls):
        cls._cached_elem_id = None
        cls._cached_elem_len = -1
        cls._cached_reachable.clear()

    @classmethod
    def _ensure_topology_caches(cls, elements: Dict[str, Any]):
        if cls._cached_elem_id != id(elements) or cls._cached_elem_len != len(elements):
            j_map: Dict[str, Any] = {}
            s_set: Set[Tuple[str, str, str]] = set()
            s_by_el: Dict[str, List[Any]] = {}
            for el in elements.values():
                et = getattr(el, "elem_type", "")
                if et == "track_jump":
                    conn = getattr(el, "connected_element", None)
                    if conn:
                        j_map[conn] = el
                elif et == "signal" or hasattr(el, "is_permissive"):
                    s_dir = getattr(el, "direction", "")
                    s_rear = getattr(el, "rear_element", None)
                    s_prot = getattr(el, "protects_element", None)
                    if s_prot:
                        s_by_el.setdefault(s_prot, []).append(el)
                    if s_rear and s_rear != s_prot:
                        s_by_el.setdefault(s_rear, []).append(el)
                    if s_rear and s_prot and s_dir:
                        s_set.add((s_rear, s_prot, s_dir))
                        s_set.add((s_prot, s_rear, s_dir))
            cls._cached_elem_id = id(elements)
            cls._cached_elem_len = len(elements)
            cls._cached_jumps_by_conn = j_map
            cls._cached_sig_boundaries = s_set
            cls._cached_sigs_by_elem = s_by_el
            cls._cached_reachable.clear()

    @classmethod
    def resolve_through_jump(
        cls,
        elements: Dict[str, Any],
        next_id: Optional[str],
        next_dir: str,
        curr_id: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        """Traverse through TrackJump pairings if encountered."""
        jump_el = None
        if next_id and next_id in elements:
            cand = elements[next_id]
            if getattr(cand, "elem_type", "") == "track_jump":
                jump_el = cand
        elif curr_id and curr_id in elements:
            cls._ensure_topology_caches(elements)
            cand_j = cls._cached_jumps_by_conn.get(curr_id)
            if cand_j is not None and getattr(cand_j, "connected_element", None) == curr_id and getattr(cand_j, "id", "") in elements:
                jump_el = cand_j
            elif cls._cached_jumps_by_conn:
                for el in elements.values():
                    if getattr(el, "elem_type", "") == "track_jump" and getattr(el, "connected_element", None) == curr_id:
                        jump_el = el
                        cls._cached_jumps_by_conn[curr_id] = el
                        break
        if not jump_el:
            return next_id, next_dir

        partner = None
        paired_id = getattr(jump_el, "paired_jump_id", None)
        if paired_id and paired_id in elements:
            p = elements[paired_id]
            if getattr(p, "elem_type", "") == "track_jump" and getattr(p, "id", "") != getattr(jump_el, "id", ""):
                partner = p
        if not partner:
            target_letter = (getattr(jump_el, "letter", "") or "").strip().upper()
            if target_letter:
                for el in elements.values():
                    if (
                        getattr(el, "elem_type", "") == "track_jump"
                        and getattr(el, "id", "") != getattr(jump_el, "id", "")
                        and (getattr(el, "letter", "") or "").strip().upper() == target_letter
                    ):
                        partner = el
                        break
        if partner and getattr(partner, "connected_element", None):
            return partner.connected_element, next_dir
        return next_id, next_dir

    @classmethod
    def trace_signal_green_path(
        cls,
        sig: Any,
        elements: Dict[str, Any],
        resolve_jump_fn: Optional[Any] = None
    ) -> List[Tuple[str, str]]:
        """
        Traces the downstream lined path from a permissive signal across connected
        permissive tracks, turnouts (respecting switch alignment), and track jumps.
        """
        path: List[Tuple[str, str]] = []
        curr_id = getattr(sig, "protects_element", None)
        prev_id = getattr(sig, "rear_element", None)
        curr_dir = getattr(sig, "direction", "E")
        visited: Set[str] = set()

        first_el = elements.get(curr_id) if curr_id else None
        if first_el and getattr(first_el, "elem_type", "") == "track":
            neighbors = getattr(first_el, "neighbors", {})
            if prev_id in neighbors.get(curr_dir, []):
                prev_id = None
        if first_el and getattr(first_el, "elem_type", "") == "turnout":
            if prev_id == getattr(first_el, "common_neighbor", None):
                st = getattr(first_el, "_state", 0)
                pos = getattr(sig, "position", "")
                if st == 0 and pos != "top":
                    return []
                if st == 1 and pos not in ("middle", "bottom"):
                    return []

        jump_fn = resolve_jump_fn or (lambda nid, ndir, cid: cls.resolve_through_jump(elements, nid, ndir, cid))

        cls._ensure_topology_caches(elements)
        sigs_by_elem = cls._cached_sigs_by_elem

        while curr_id and curr_id not in visited:
            visited.add(curr_id)
            curr_el = elements.get(curr_id)
            if not curr_el:
                break
            etype = getattr(curr_el, "elem_type", "")
            if etype not in ("track", "turnout"):
                break
            if getattr(curr_el, "is_occupied", False):
                break

            next_id, next_dir = None, curr_dir
            if etype == "track":
                if hasattr(curr_el, "next_in_direction"):
                    next_id, next_dir = curr_el.next_in_direction(curr_dir, prev_id=prev_id)
                elem_dir = curr_dir
            elif etype == "turnout":
                common_nbr = getattr(curr_el, "common_neighbor", None)
                orient = getattr(curr_el, "orientation", "")
                if prev_id == common_nbr or curr_dir in COMPATIBLE_DIRS.get(orient, ()):
                    next_id = curr_el.active_branch_neighbor() if hasattr(curr_el, "active_branch_neighbor") else None
                    next_dir = curr_el.active_branch_dir() if hasattr(curr_el, "active_branch_dir") else curr_dir
                    elem_dir = next_dir
                else:
                    next_id = common_nbr
                    next_dir = OPPOSITE_DIR.get(orient, curr_dir)
                    elem_dir = next_dir
            else:
                elem_dir = curr_dir

            path.append((curr_id, elem_dir))
            next_id, next_dir = jump_fn(next_id, next_dir, curr_id)

            if not next_id:
                break
            next_el = elements.get(next_id)
            if next_el and getattr(next_el, "elem_type", "") == "turnout":
                norm_nbr = getattr(next_el, "normal_neighbor", None)
                rev_nbr = getattr(next_el, "reverse_neighbor", None)
                if curr_id in (norm_nbr, rev_nbr):
                    active_nbr = next_el.active_branch_neighbor() if hasattr(next_el, "active_branch_neighbor") else None
                    if curr_id != active_nbr:
                        break

            # If an intervening signal facing in this direction is strictly restricted (red), stop trace
            cand_sigs = sigs_by_elem.get(next_id, ())
            facing_sigs = [
                s for s in cand_sigs
                if getattr(s, "direction", "") == curr_dir
                and (
                    (getattr(s, "rear_element", None) == curr_id and getattr(s, "protects_element", None) == next_id)
                    or (getattr(s, "protects_element", None) == curr_id and getattr(s, "rear_element", None) == next_id)
                    or (getattr(s, "protects_element", None) == next_id)
                )
            ]
            active_sigs = [s for s in facing_sigs if not getattr(s, "is_dark", False)]
            if active_sigs and all(getattr(s, "is_restricted", False) for s in active_sigs):
                break

            prev_id, curr_id, curr_dir = curr_id, next_id, next_dir

        return path

    @classmethod
    def extract_downstream_signals(
        cls,
        sig: Any,
        path: List[Tuple[str, str]],
        elements: Dict[str, Any]
    ) -> List[str]:
        """Extracts all downstream signals along the path in order."""
        if not path:
            return []
        sig_id = getattr(sig, "id", "")
        downstream: List[str] = []
        cls._ensure_topology_caches(elements)
        sigs_by_elem = cls._cached_sigs_by_elem

        for i in range(len(path)):
            curr_id, curr_dir = path[i]
            next_id = path[i + 1][0] if i + 1 < len(path) else None
            step_sigs = []
            seen_cand: Set[int] = set()
            cands = list(sigs_by_elem.get(curr_id, ()))
            if next_id:
                cands.extend(sigs_by_elem.get(next_id, ()))
            for s in cands:
                if id(s) in seen_cand:
                    continue
                seen_cand.add(id(s))
                s_id = getattr(s, "id", "")
                if s_id == sig_id or getattr(s, "direction", "") != curr_dir:
                    continue
                s_rear = getattr(s, "rear_element", None)
                s_prot = getattr(s, "protects_element", None)
                is_match = False
                if next_id and (
                    (s_rear == curr_id and s_prot == next_id)
                    or (s_prot == curr_id and s_rear == next_id)
                    or (s_prot == next_id and s_id not in downstream)
                ):
                    is_match = True
                elif (s_prot == curr_id or s_rear == curr_id) and i > 0:
                    is_match = True
                if is_match and s_id not in downstream:
                    step_sigs.append(s_id)
            for sid in step_sigs:
                if sid not in downstream:
                    downstream.append(sid)
        return downstream

    @classmethod
    def are_elements_continuous(
        cls,
        el1_id: str,
        el2_id: str,
        travel_dir: str,
        elements: Dict[str, Any],
        resolve_jump_fn: Optional[Any] = None
    ) -> bool:
        """Determines if two track/turnout elements are directly connected in travel_dir."""
        if not el1_id or not el2_id:
            return False
        if el1_id == el2_id:
            return True
        el1 = elements.get(el1_id)
        el2 = elements.get(el2_id)
        if not el1 or not el2:
            return False

        jump_fn = resolve_jump_fn or (lambda nid, ndir, cid: cls.resolve_through_jump(elements, nid, ndir, cid))

        if getattr(el1, "elem_type", "") == "track":
            nxt, _ = el1.next_in_direction(travel_dir) if hasattr(el1, "next_in_direction") else (None, travel_dir)
            res, _ = jump_fn(nxt, travel_dir, el1_id)
            if nxt == el2_id or res == el2_id:
                return True
            neighbors = getattr(el1, "neighbors", {})
            if el2_id in neighbors.get(travel_dir, []):
                return True
            for compat in COMPATIBLE_DIRS.get(travel_dir, (travel_dir,)):
                if el2_id in neighbors.get(compat, []):
                    return True
            opp = OPPOSITE_DIR.get(travel_dir)
            for ndir, nval in neighbors.items():
                if ndir != opp:
                    cands = [nval] if isinstance(nval, str) else list(nval)
                    if el2_id in cands:
                        return True

        if getattr(el1, "elem_type", "") == "turnout":
            act = el1.active_branch_neighbor() if hasattr(el1, "active_branch_neighbor") else None
            com = getattr(el1, "common_neighbor", None)
            if act == el2_id or com == el2_id:
                return True
            if el2_id in (getattr(el1, "normal_neighbor", None), getattr(el1, "reverse_neighbor", None), com):
                return True

        opp_dir = OPPOSITE_DIR.get(travel_dir, "")
        if opp_dir and getattr(el2, "elem_type", "") == "track":
            neighbors = getattr(el2, "neighbors", {})
            if el1_id in neighbors.get(opp_dir, []):
                return True
            for compat in COMPATIBLE_DIRS.get(opp_dir, (opp_dir,)):
                if el1_id in neighbors.get(compat, []):
                    return True

        if getattr(el2, "elem_type", "") == "turnout":
            com2 = getattr(el2, "common_neighbor", None)
            if el1_id in (com2, getattr(el2, "normal_neighbor", None), getattr(el2, "reverse_neighbor", None)):
                return True

        # Signal boundary check (O(1) via cached boundary set)
        cls._ensure_topology_caches(elements)
        if (el1_id, el2_id, travel_dir) in cls._cached_sig_boundaries:
            return True

        return False

    @classmethod
    def is_path_reachable(
        cls,
        start_id: str,
        target_id: str,
        travel_dir: str,
        elements: Dict[str, Any],
        resolve_jump_fn: Optional[Any] = None,
        max_depth: int = 6
    ) -> bool:
        """Determines if target_id can be reached from start_id within max_depth steps."""
        if not start_id or not target_id:
            return False
        if start_id == target_id:
            return True
        cls._ensure_topology_caches(elements)
        cache_key = (start_id, travel_dir, max_depth)
        reached = cls._cached_reachable.get(cache_key)
        if reached is not None:
            return target_id in reached

        curr_id = start_id
        curr_dir = travel_dir
        prev_id = None
        visited: Set[str] = set()
        jump_fn = resolve_jump_fn or (lambda nid, ndir, cid: cls.resolve_through_jump(elements, nid, ndir, cid))

        for _ in range(max_depth):
            visited.add(curr_id)
            el = elements.get(curr_id)
            if not el:
                break
            nxt_id = None
            etype = getattr(el, "elem_type", "")
            if etype == "track":
                nxt_id, curr_dir = el.next_in_direction(curr_dir, prev_id=prev_id) if hasattr(el, "next_in_direction") else (None, curr_dir)
            elif etype == "turnout":
                nxt_id = el.active_branch_neighbor() if hasattr(el, "active_branch_neighbor") else None
                curr_dir = el.active_branch_dir() if hasattr(el, "active_branch_dir") else curr_dir
            nxt_id, curr_dir = jump_fn(nxt_id, curr_dir, curr_id)
            if not nxt_id or nxt_id in visited:
                break
            prev_id, curr_id = curr_id, nxt_id
            visited.add(curr_id)
        cls._cached_reachable[cache_key] = visited
        return target_id in visited

    @classmethod
    def are_trains_on_same_continuous_path(
        cls,
        trn1: dict,
        trn2: dict,
        elements: Dict[str, Any],
        resolve_jump_fn: Optional[Any] = None
    ) -> Tuple[bool, bool]:
        """
        Determines whether two trains are on the same continuous path going in the same direction.
        Returns: (is_same_continuous_path, trn1_is_upstream)
        """
        d1 = trn1.get("direction")
        d2 = trn2.get("direction")
        if not d1 or not d2:
            return False, True

        p1 = trn1.get("path_element_ids") or []
        p2 = trn2.get("path_element_ids") or []
        sig1 = trn1.get("initial_signal_id")
        sig2 = trn2.get("initial_signal_id")
        ds1 = trn1.get("downstream_signals") or []
        ds2 = trn2.get("downstream_signals") or []
        exp1 = trn1.get("expected_signal_id")
        exp2 = trn2.get("expected_signal_id")

        # 1. Downstream signal explicitly in path
        if sig2 and (sig2 in ds1 or sig2 == exp1):
            return True, True
        if sig1 and (sig1 in ds2 or sig1 == exp2):
            return True, False

        # 2. Overlapping track elements
        overlap = set(p1) & set(p2)
        if overlap:
            overlap_p1 = [eid for eid in p1 if eid in overlap]
            overlap_p2 = [eid for eid in p2 if eid in overlap]
            if overlap_p1 == overlap_p2:
                idx1 = p1.index(overlap_p1[0])
                idx2 = p2.index(overlap_p1[0])
                return True, (idx1 >= idx2)
            else:
                # Opposite traversal order along shared elements -> opposing trains
                return False, True

        # Strictly opposite directions with no downstream signal or forward overlap -> opposing trains
        if OPPOSITE_DIR.get(d1) == d2:
            return False, True

        # 3. End-to-start element continuity
        if p1 and p2:
            if (cls.are_elements_continuous(p1[-1], p2[0], d1, elements, resolve_jump_fn) or
                cls.are_elements_continuous(p1[-1], p2[0], d2, elements, resolve_jump_fn) or
                cls.is_path_reachable(p1[-1], p2[0], d1, elements, resolve_jump_fn) or
                cls.is_path_reachable(p1[-1], p2[0], d2, elements, resolve_jump_fn)):
                return True, True
            if (cls.are_elements_continuous(p2[-1], p1[0], d1, elements, resolve_jump_fn) or
                cls.are_elements_continuous(p2[-1], p1[0], d2, elements, resolve_jump_fn) or
                cls.is_path_reachable(p2[-1], p1[0], d1, elements, resolve_jump_fn) or
                cls.is_path_reachable(p2[-1], p1[0], d2, elements, resolve_jump_fn)):
                return True, False

        # 4. Entry signal connection
        if sig2 and p1:
            s2 = elements.get(sig2)
            if s2 and (getattr(s2, "rear_element", None) in p1 or getattr(s2, "protects_element", None) == p1[-1]):
                return True, True
        if sig1 and p2:
            s1 = elements.get(sig1)
            if s1 and (getattr(s1, "rear_element", None) in p2 or getattr(s1, "protects_element", None) == p2[-1]):
                return True, False

        return False, True

    @classmethod
    def merge_two_potential_trains(cls, train_tracker: TrainTracker, upstream_key: str, downstream_key: str):
        """Merges duplicate downstream potential train into upstream potential train."""
        up_trn = train_tracker.trains.get(upstream_key)
        down_trn = train_tracker.trains.get(downstream_key)
        if not up_trn or not down_trn:
            return

        up_passed = list(up_trn.get("passed_elements", []))
        up_curr = up_trn.get("current_element_id")
        up_ahead = list(up_trn.get("ahead_elements", []))

        down_elems = list(down_trn.get("path_element_ids", []))
        for eid in down_elems:
            if eid not in up_ahead and eid not in up_passed and eid != up_curr:
                up_ahead.append(eid)
        up_trn["ahead_elements"] = up_ahead

        up_sigs = list(up_trn.get("downstream_signals", []))
        down_init = down_trn.get("initial_signal_id")
        if down_init and down_init not in up_sigs and down_init != up_trn.get("initial_signal_id") and down_init not in up_trn.get("passed_signals", []):
            up_sigs.append(down_init)
        for sid in down_trn.get("downstream_signals", []):
            if sid not in up_sigs and sid != up_trn.get("initial_signal_id") and sid not in up_trn.get("passed_signals", []):
                up_sigs.append(sid)
        up_trn["downstream_signals"] = up_sigs

        if not up_trn.get("expected_signal_id") and up_sigs:
            up_trn["expected_signal_id"] = up_sigs[0]

        up_trn["full_path"] = list(up_passed) + ([up_curr] if up_curr else []) + list(up_ahead)
        up_trn["path_element_ids"] = list(up_trn["full_path"])

        if not up_trn.get("assigned_name") and down_trn.get("assigned_name"):
            up_trn["assigned_name"] = down_trn["assigned_name"]
        if not up_trn.get("assigned_id") and down_trn.get("assigned_id"):
            up_trn["assigned_id"] = down_trn["assigned_id"]

        train_tracker.trains.pop(downstream_key, None)

    @classmethod
    def merge_potential_trains_on_continuous_paths(
        cls,
        train_tracker: TrainTracker,
        elements: Dict[str, Any],
        resolve_jump_fn: Optional[Any] = None
    ):
        """
        Rule 2: Review all potential trains and if there are any already one on any part
        of the same continuous path going the same direction, merge/discard duplicates.
        """
        non_cont_pairs: Set[Tuple[str, str]] = set()
        while True:
            merged = False
            train_items = list(train_tracker.trains.items())
            for i in range(len(train_items)):
                key1, trn1 = train_items[i]
                if trn1.get("status") != "Potential":
                    continue
                for j in range(i + 1, len(train_items)):
                    key2, trn2 = train_items[j]
                    if trn2.get("status") != "Potential":
                        continue
                    pair_k = (key1, key2) if key1 < key2 else (key2, key1)
                    if pair_k in non_cont_pairs:
                        continue
                    is_cont, trn1_is_up = cls.are_trains_on_same_continuous_path(trn1, trn2, elements, resolve_jump_fn)
                    if is_cont:
                        up_k = key1 if trn1_is_up else key2
                        down_k = key2 if trn1_is_up else key1
                        cls.merge_two_potential_trains(train_tracker, up_k, down_k)
                        non_cont_pairs = {pk for pk in non_cont_pairs if up_k not in pk and down_k not in pk}
                        merged = True
                        break
                    else:
                        non_cont_pairs.add(pair_k)
                if merged:
                    break

            if not merged:
                # Also check potential train against active Inferred or Confirmed trains on the same path
                for key_pot, trn_pot in list(train_tracker.trains.items()):
                    if trn_pot.get("status") != "Potential":
                        continue
                    for key_act, trn_act in list(train_tracker.trains.items()):
                        if trn_act.get("status") not in ("Inferred", "Confirmed") or key_pot == key_act:
                            continue
                        pair_k = (key_act, key_pot)
                        if pair_k in non_cont_pairs:
                            continue
                        is_cont, act_is_up = cls.are_trains_on_same_continuous_path(trn_act, trn_pot, elements, resolve_jump_fn)
                        if is_cont and act_is_up:
                            cls.merge_two_potential_trains(train_tracker, key_act, key_pot)
                            non_cont_pairs = {pk for pk in non_cont_pairs if key_act not in pk and key_pot not in pk}
                            merged = True
                            break
                        else:
                            non_cont_pairs.add(pair_k)
                    if merged:
                        break

            if not merged:
                break

    @classmethod
    def notify_signal_dropped(
        cls,
        sig: Any,
        elements: Dict[str, Any],
        train_tracker: TrainTracker,
        now: float,
        get_signal_gps_fn: Optional[Any] = None,
        resolve_jump_fn: Optional[Any] = None
    ) -> Optional[dict]:
        """
        Handles signal transition to restricted (dropping to red).
        Applies Rules 3, 4, 5, 6, 7, 9, 11:
        - Rule 3: First signal drops -> Transition Potential to Inferred.
        - Rule 4: Expected next signal drops -> Transition Inferred to Confirmed & estimate speed.
        - Rule 5: Non-next signal on same path drops -> Keep existing train, ignore new train.
        - Rule 6: Opposite direction signals drops -> Ignored.
        - Rule 9: Next signal lacks GPS -> Maintain last estimated speed.
        - Rule 11: If train was dimmed / counting down timeout, reactivate it.
        """
        sig_id = getattr(sig, "id", "")
        sig_dir = getattr(sig, "direction", "E")
        ahead_el = elements.get(getattr(sig, "protects_element", None))
        gps_resolver = get_signal_gps_fn or (lambda s: get_signal_gps(s, elements))

        # 1. Rule 4: Check if this signal drop matches an Inferred or Confirmed train expecting this signal
        matched_expected = None
        for tkey, trn in train_tracker.trains.items():
            if trn.get("is_rf_train"):
                continue
            if trn.get("status") in ("Inferred", "Confirmed"):
                exp_sig = trn.get("expected_signal_id")
                is_expected = (exp_sig == sig_id)
                is_downstream = (sig_id in trn.get("downstream_signals", []))
                # Rule 6: Ignore opposite direction signal drops if not on this train's forward path
                if not is_expected and not is_downstream:
                    if OPPOSITE_DIR.get(trn.get("direction")) == sig_dir:
                        continue
                if is_expected or (exp_sig is None and (is_downstream or trn.get("timeout_start_ts") is not None or trn.get("is_dimmed"))):
                    matched_expected = trn
                    break

        # 2. Rule 3: Check if this signal drop matches a Potential train (first signal in lined path)
        matched_potential = None
        if not matched_expected:
            for tkey, trn in train_tracker.trains.items():
                if trn.get("status") == "Potential" and trn.get("initial_signal_id") == sig_id:
                    matched_potential = trn
                    break

        # If matched_expected was found, clean up any duplicate Potential train keyed to this signal
        if matched_expected:
            pot_keys = [
                k for k, t in train_tracker.trains.items()
                if t.get("status") == "Potential" and (t.get("initial_signal_id") == sig_id or sig_id in t.get("path_element_ids", []))
            ]
            for k in pot_keys:
                train_tracker.trains.pop(k, None)

        # 3. Rule 5: Non-next signal on the same path drops
        if not matched_potential and not matched_expected:
            for tkey, trn in train_tracker.trains.items():
                if trn.get("status") in ("Inferred", "Confirmed"):
                    ds_list = trn.get("downstream_signals", [])
                    if sig_id in ds_list and sig_id != trn.get("expected_signal_id"):
                        # Rule 5: Keep existing train, do not create new train, continue watching expected signal
                        return trn

        # 4. Fallback: If no potential train was lined up, check if a potential train can be formed or inferred
        if not matched_potential and not matched_expected:
            path = cls.trace_signal_green_path(sig, elements, resolve_jump_fn)
            if not path:
                path = [(getattr(sig, "protects_element", ""), sig_dir)]
            downstream_sigs = cls.extract_downstream_signals(sig, path, elements)

            # Check if this signal is on any existing train's path
            has_existing = False
            path_set = set(p[0] for p in path)
            for trn in train_tracker.trains.values():
                if sig_id in trn.get("downstream_signals", []) or sig_id == trn.get("expected_signal_id"):
                    has_existing = True
                    break
                trn_ahead = trn.get("ahead_elements", [])
                if set(trn_ahead) & path_set:
                    has_existing = True
                    break
                trn_path = trn.get("path_element_ids", [])
                overlap = set(trn_path) & path_set
                if overlap:
                    overlap_path = [e for e in [p[0] for p in path] if e in overlap]
                    overlap_trn = [e for e in trn_path if e in overlap]
                    if overlap_path == overlap_trn:
                        has_existing = True
                        break
            if not has_existing:
                tkey = train_tracker.get_or_create_potential_train(sig, path, downstream_sigs, now)
                matched_potential = train_tracker.trains.get(tkey)

        # -------------------------------------------------------------
        # Transition 1: Potential -> Inferred (Rule 3)
        # -------------------------------------------------------------
        if matched_potential:
            trn = matched_potential
            trn["status"] = "Inferred"
            trn["direction"] = sig_dir
            trn["current_signal_id"] = sig_id
            if sig_id not in trn.get("passed_signals", []):
                trn.setdefault("passed_signals", []).append(sig_id)
            trn["first_drop_ts"] = now
            trn["initial_drop_ts"] = now
            trn["updated_ts"] = now
            trn["time_str"] = time.strftime("%H:%M:%S", time.localtime(now))
            prot_el = getattr(sig, "protects_element", None)
            trn["current_element_id"] = prot_el
            trn["last_location"] = f"{prot_el}"

            # Trace forward from this signal along the train's direction to refresh ahead_elements and downstream_signals
            fwd_path = cls.trace_signal_green_path(sig, elements, resolve_jump_fn)
            if fwd_path:
                fwd_eids = [p[0] for p in fwd_path if p[0] != prot_el]
                fwd_sigs = cls.extract_downstream_signals(sig, fwd_path, elements)
                combined_sigs = list(fwd_sigs)
                for sid in trn.get("downstream_signals", []):
                    if sid not in combined_sigs and sid != sig_id and sid not in trn.get("passed_signals", []):
                        combined_sigs.append(sid)
                trn["downstream_signals"] = combined_sigs
                trn["expected_signal_id"] = combined_sigs[0] if combined_sigs else None
                combined_ahead = list(fwd_eids)
                for eid in trn.get("ahead_elements", []):
                    if eid not in combined_ahead and eid != prot_el:
                        combined_ahead.append(eid)
                trn["ahead_elements"] = combined_ahead
            else:
                ds_list = [s for s in trn.get("downstream_signals", []) if s != sig_id]
                trn["downstream_signals"] = ds_list
                trn["expected_signal_id"] = ds_list[0] if ds_list else None
                trn["ahead_elements"] = [e for e in trn.get("ahead_elements", []) if e != prot_el]

            trn["passed_elements"] = list(trn.get("passed_elements", []))
            trn["full_path"] = list(trn["passed_elements"]) + ([prot_el] if prot_el else []) + list(trn["ahead_elements"])
            trn["path_element_ids"] = list(trn["full_path"])

            trn["is_dimmed"] = False
            trn["timeout_start_ts"] = None

            sig_gps = gps_resolver(sig)
            if sig_gps:
                trn["initial_gps"] = sig_gps
                trn["last_gps"] = sig_gps
                trn["last_gps_ts"] = now
                trn["last_gps_sig_id"] = sig_id

            tname = trn.get("assigned_name") or trn.get("train_name")
            if ahead_el and not getattr(ahead_el, "has_occ_bit", False):
                ahead_el._inferred_occupied = True
                ahead_el._inferred_timestamp = now
                ahead_el._last_packet_ts = now
                ahead_el._train_direction = sig_dir
                ahead_el._train_name = tname
                ahead_el._estimated_speed_mph = None
                ahead_el._is_dimmed = False
            return trn

        # -------------------------------------------------------------
        # Transition 2: Inferred / Confirmed -> Confirmed & Advance (Rule 4, 9, 11)
        # -------------------------------------------------------------
        if matched_expected:
            trn = matched_expected
            trn["status"] = "Confirmed"
            trn["direction"] = sig_dir
            # Rule 11: Reactivate train if it resumed motion during timeout countdown
            trn["is_dimmed"] = False
            trn["timeout_start_ts"] = None

            sig_gps = gps_resolver(sig)
            ref_gps = trn.get("last_gps") or trn.get("initial_gps")
            ref_ts = trn.get("last_gps_ts") or trn.get("initial_drop_ts")

            # Fallback to initial signal GPS if last_gps not explicitly recorded
            if not ref_gps:
                init_sig = elements.get(trn.get("initial_signal_id"))
                if init_sig:
                    ref_gps = gps_resolver(init_sig)
                    ref_ts = trn.get("initial_drop_ts") or trn.get("created_ts")

            # Speed calculation: Rule 4 & Rule 9
            if sig_gps:
                if ref_gps and ref_ts:
                    dist_miles = haversine_miles(ref_gps, sig_gps)
                    dt = now - ref_ts
                    # Require dt >= 1.0s and physical distance > 0.001 miles to avoid burst/instantaneous spikes
                    if dt >= 1.0 and dist_miles > 0.001:
                        spd = (dist_miles / dt) * 3600.0
                        # Guard against physically impossible speeds (e.g. 1000s of MPH)
                        if spd <= 150.0:
                            trn["speed_mph"] = round(spd, 1)
                            trn["speed_str"] = f"{round(spd, 1)} MPH"
                            trn["last_gps"] = sig_gps
                            trn["last_gps_ts"] = now
                            trn["last_gps_sig_id"] = sig_id
                        else:
                            # Speed is unrealistic (> 150 MPH); maintain last speed or mark Unk.
                            if trn.get("speed_mph") is not None:
                                trn["speed_str"] = f"{trn['speed_mph']:.1f} MPH"
                            else:
                                trn["speed_str"] = "Unk."
                    elif trn.get("speed_mph") is not None:
                        trn["speed_str"] = f"{trn['speed_mph']:.1f} MPH"
                    else:
                        trn["speed_str"] = "Unk."
                else:
                    trn["last_gps"] = sig_gps
                    trn["last_gps_ts"] = now
                    trn["last_gps_sig_id"] = sig_id
                    if trn.get("speed_mph") is not None:
                        trn["speed_str"] = f"{trn['speed_mph']:.1f} MPH"
                    else:
                        trn["speed_str"] = "Unk."
            else:
                # Rule 9: If next signal does not have GPS, maintain last estimated speed!
                if trn.get("speed_mph") is not None:
                    trn["speed_str"] = f"{trn['speed_mph']:.1f} MPH"
                else:
                    trn["speed_str"] = "Unk."

            # Advance train position: update passed_elements, current_element_id, ahead_elements
            prev_el_id = trn.get("current_element_id")
            new_el_id = getattr(sig, "protects_element", None)

            # Clear occupancy of previous block if different from new block
            if prev_el_id and prev_el_id != new_el_id:
                prev_el = elements.get(prev_el_id)
                if prev_el and not getattr(prev_el, "has_occ_bit", False):
                    prev_el._inferred_occupied = False
                    prev_el._train_name = None
                    prev_el._estimated_speed_mph = None
                    prev_el._train_direction = None
                    prev_el._is_dimmed = False

            passed_elems = trn.setdefault("passed_elements", [])
            ahead_elems = trn.get("ahead_elements", [])

            if prev_el_id and prev_el_id not in passed_elems and prev_el_id != new_el_id:
                passed_elems.append(prev_el_id)

            if new_el_id in ahead_elems:
                idx = ahead_elems.index(new_el_id)
                for tel in ahead_elems[:idx]:
                    if tel not in passed_elems:
                        passed_elems.append(tel)
                remaining_ahead = ahead_elems[idx + 1:]
            else:
                remaining_ahead = [e for e in ahead_elems if e != new_el_id]

            trn["current_signal_id"] = sig_id
            if sig_id not in trn.setdefault("passed_signals", []):
                trn["passed_signals"].append(sig_id)

            trn["current_element_id"] = new_el_id
            trn["last_location"] = f"{new_el_id}"
            trn["updated_ts"] = now
            trn["time_str"] = time.strftime("%H:%M:%S", time.localtime(now))

            # Trace forward from the newly dropped signal to update path ahead
            fwd_path = cls.trace_signal_green_path(sig, elements, resolve_jump_fn)
            if fwd_path:
                fwd_eids = [p[0] for p in fwd_path if p[0] != new_el_id and p[0] not in passed_elems]
                fwd_sigs = cls.extract_downstream_signals(sig, fwd_path, elements)
                combined_sigs = list(fwd_sigs)
                for sid in trn.get("downstream_signals", []):
                    if sid not in combined_sigs and sid != sig_id and sid not in trn.get("passed_signals", []):
                        combined_sigs.append(sid)
                trn["downstream_signals"] = combined_sigs
                trn["expected_signal_id"] = combined_sigs[0] if combined_sigs else None
                combined_ahead = list(fwd_eids)
                for eid in remaining_ahead:
                    if eid not in combined_ahead and eid != new_el_id and eid not in passed_elems:
                        combined_ahead.append(eid)
                trn["ahead_elements"] = combined_ahead
            else:
                ds_list = trn.get("downstream_signals", [])
                if sig_id in ds_list:
                    s_idx = ds_list.index(sig_id)
                    ds_list = ds_list[s_idx + 1:]
                else:
                    ds_list = [s for s in ds_list if s != sig_id and s not in trn.get("passed_signals", [])]
                trn["downstream_signals"] = ds_list
                trn["expected_signal_id"] = ds_list[0] if ds_list else None
                trn["ahead_elements"] = remaining_ahead

            trn["full_path"] = list(trn["passed_elements"]) + ([new_el_id] if new_el_id else []) + list(trn["ahead_elements"])
            trn["path_element_ids"] = list(trn["full_path"])

            tname = trn.get("assigned_name") or trn.get("train_name")
            if ahead_el and not getattr(ahead_el, "has_occ_bit", False):
                ahead_el._inferred_occupied = True
                ahead_el._inferred_timestamp = now
                ahead_el._last_packet_ts = now
                ahead_el._train_direction = sig_dir
                ahead_el._train_name = tname
                ahead_el._estimated_speed_mph = trn.get("speed_mph")
                ahead_el._is_dimmed = False
            return trn

        return None

    @classmethod
    def evaluate_system_trains(
        cls,
        elements: Dict[str, Any],
        train_tracker: TrainTracker,
        now: float,
        inferred_timeout_sec: float = 15.0,
        get_signal_gps_fn: Optional[Any] = None,
        resolve_jump_fn: Optional[Any] = None
    ) -> Set[str]:
        """
        Runs periodic system evaluation across all signals, paths, and active trains.
        - Checks for signals dropping from permissive to restricted.
        - Detects lined up paths (>1 segment green) -> Potential trains.
        - Merges / de-duplicates potential trains on the same continuous path.
        - Manages timeout countdowns, dimming, and deletion (Rule 11).
        """
        active_occ_ids: Set[str] = set()
        gps_resolver = get_signal_gps_fn or (lambda s: get_signal_gps(s, elements))
        cls._ensure_topology_caches(elements)
        cls._cached_reachable.clear()

        # Clear green path flags and expired inferred occupancies on tracks
        for el in elements.values():
            etype = getattr(el, "elem_type", "")
            if etype in ("track", "turnout"):
                el._green_path = False
                el._green_path_dir = None
                if getattr(el, "_inferred_occupied", False) and (now - getattr(el, "_inferred_timestamp", now)) >= inferred_timeout_sec:
                    el._inferred_occupied = False
                    el._estimated_speed_mph = None
                    el._train_direction = None
                    el._train_name = None
                    el._is_dimmed = False
                if getattr(el, "is_occupied", False):
                    active_occ_ids.add(getattr(el, "id", ""))

        # 0. Check for signals dropping from permissive to restricted
        for sig in elements.values():
            if getattr(sig, "elem_type", "") == "signal" or hasattr(sig, "is_permissive"):
                was_perm = getattr(sig, "_prev_eval_permissive", None)
                if was_perm is True and getattr(sig, "is_restricted", False):
                    cls.notify_signal_dropped(sig, elements, train_tracker, now, gps_resolver, resolve_jump_fn)
                sig._prev_eval_permissive = getattr(sig, "is_permissive", False)

        # 1. Rule 1: Find all lined up paths (>1 segments green from permissive signals)
        permissive_signals = [
            el for el in elements.values()
            if (getattr(el, "elem_type", "") == "signal" or hasattr(el, "is_permissive"))
            and getattr(el, "is_permissive", False)
            and not getattr(el, "is_dark", False)
        ]
        all_paths: List[Tuple[Any, List[Tuple[str, str]]]] = []
        for sig in permissive_signals:
            path = cls.trace_signal_green_path(sig, elements, resolve_jump_fn)
            if path:
                all_paths.append((sig, path))
                for elem_id, elem_dir in path:
                    obj = elements.get(elem_id)
                    if obj and getattr(obj, "elem_type", "") in ("track", "turnout") and not getattr(obj, "is_occupied", False):
                        obj._green_path = True
                        obj._green_path_dir = elem_dir

        # Sort candidate paths by length descending (longest / most upstream entry path first)
        all_paths.sort(key=lambda item: len(item[1]), reverse=True)
        active_lined_entry_signals: Set[str] = set()

        for sig, path in all_paths:
            if len(path) > 1:
                sig_id = getattr(sig, "id", "")
                sig_dir = getattr(sig, "direction", "E")
                prot_el = getattr(sig, "protects_element", None)
                path_eids = [p[0] for p in path]

                # Associate with existing train if downstream/ahead along continuous path
                is_duplicate = False
                matched_existing = None
                for tkey, trn in train_tracker.trains.items():
                    if trn.get("is_rf_train"):
                        continue
                    # 1. Same entry signal
                    if trn.get("initial_signal_id") == sig_id:
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 2. sig_id is the expected signal or in downstream signals
                    if sig_id == trn.get("expected_signal_id") or sig_id in trn.get("downstream_signals", []):
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 3. Entry element is in this train's ahead elements
                    if prot_el in trn.get("ahead_elements", []):
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 4. Overlap with ahead elements
                    ahead_elems = trn.get("ahead_elements", [])
                    overlap_ahead = set(path_eids) & set(ahead_elems)
                    if overlap_ahead:
                        op = [e for e in path_eids if e in overlap_ahead]
                        oa = [e for e in ahead_elems if e in overlap_ahead]
                        if op == oa:
                            is_duplicate = True
                            matched_existing = trn
                            break
                    # 5. Continuous connection from train's current element to prot_el
                    curr_el = trn.get("current_element_id")
                    trn_dir = trn.get("direction", sig_dir)
                    if curr_el and (cls.are_elements_continuous(curr_el, prot_el, sig_dir, elements, resolve_jump_fn) or
                                   cls.are_elements_continuous(curr_el, prot_el, trn_dir, elements, resolve_jump_fn) or
                                   cls.is_path_reachable(curr_el, prot_el, sig_dir, elements, resolve_jump_fn, max_depth=6) or
                                   cls.is_path_reachable(curr_el, prot_el, trn_dir, elements, resolve_jump_fn, max_depth=6)):
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 6. Fallback overlap with path_element_ids
                    overlap = set(path_eids) & set(trn.get("path_element_ids", []))
                    if overlap:
                        op = [e for e in path_eids if e in overlap]
                        ot = [e for e in trn.get("path_element_ids", []) if e in overlap]
                        if op == ot:
                            is_duplicate = True
                            matched_existing = trn
                            break

                if is_duplicate and matched_existing:
                    active_lined_entry_signals.add(sig_id)
                    init_sig = matched_existing.get("initial_signal_id")
                    if init_sig:
                        active_lined_entry_signals.add(init_sig)
                    # Update and extend ahead_elements and downstream_signals of existing train
                    ahead = matched_existing.setdefault("ahead_elements", [])
                    passed = matched_existing.get("passed_elements", [])
                    curr = matched_existing.get("current_element_id")
                    for eid in path_eids:
                        if eid not in ahead and eid not in passed and eid != curr:
                            ahead.append(eid)

                    new_ds = cls.extract_downstream_signals(sig, path, elements)
                    ds = matched_existing.setdefault("downstream_signals", [])
                    pass_sigs = matched_existing.get("passed_signals", [])
                    curr_sig = matched_existing.get("current_signal_id")
                    if sig_id and sig_id not in ds and sig_id != init_sig and sig_id != curr_sig and sig_id not in pass_sigs:
                        ds.append(sig_id)
                    for sid in new_ds:
                        if sid not in ds and sid not in pass_sigs and sid != curr_sig:
                            ds.append(sid)

                    if not matched_existing.get("expected_signal_id") and ds:
                        matched_existing["expected_signal_id"] = ds[0]

                    matched_existing["full_path"] = list(passed) + ([curr] if curr else []) + list(ahead)
                    matched_existing["path_element_ids"] = list(matched_existing["full_path"])
                elif not is_duplicate:
                    active_lined_entry_signals.add(sig_id)
                    downstream_sigs = cls.extract_downstream_signals(sig, path, elements)
                    train_tracker.get_or_create_potential_train(sig, path, downstream_sigs, now)

        # Merge continuous potential trains (Rule 2)
        cls.merge_potential_trains_on_continuous_paths(train_tracker, elements, resolve_jump_fn)

        # 2. Clean up Potential trains whose lined path is no longer permissive / lined up
        potential_to_delete = [
            tkey for tkey, trn in train_tracker.trains.items()
            if trn.get("status") == "Potential" and trn.get("initial_signal_id") not in active_lined_entry_signals
        ]
        for tkey in potential_to_delete:
            train_tracker.trains.pop(tkey, None)

        cls.merge_potential_trains_on_continuous_paths(train_tracker, elements, resolve_jump_fn)

        # 3. Rule 11: Timeout, Dimming, Reactivation, and Deletion
        trains_to_delete = []
        for tkey, trn in list(train_tracker.trains.items()):
            if trn.get("is_rf_train"):
                continue
            if trn.get("status") in ("Inferred", "Confirmed"):
                at_edge = (trn.get("expected_signal_id") is None)
                stale_signal = False
                if not at_edge:
                    exp_sig = elements.get(trn.get("expected_signal_id"))
                    if exp_sig is None:
                        stale_signal = True
                    else:
                        sig_last_rx = getattr(exp_sig, "_last_packet_ts", 0.0) or 0.0
                        if sig_last_rx > 0:
                            if (now - sig_last_rx) >= inferred_timeout_sec:
                                stale_signal = True
                        else:
                            if (now - trn.get("updated_ts", now)) >= inferred_timeout_sec:
                                stale_signal = True

                if at_edge or stale_signal:
                    if trn.get("timeout_start_ts") is None:
                        trn["timeout_start_ts"] = now
                        trn["is_dimmed"] = True
                        curr_el = elements.get(trn.get("current_element_id"))
                        if curr_el:
                            curr_el._is_dimmed = True
                    else:
                        if (now - trn["timeout_start_ts"]) >= inferred_timeout_sec:
                            trains_to_delete.append(tkey)
                else:
                    if trn.get("is_dimmed") and not at_edge:
                        trn["is_dimmed"] = False
                        trn["timeout_start_ts"] = None
                        curr_el = elements.get(trn.get("current_element_id"))
                        if curr_el:
                            curr_el._is_dimmed = False

        for tkey in trains_to_delete:
            deleted_trn = train_tracker.trains.pop(tkey, None)
            if deleted_trn:
                curr_el = elements.get(deleted_trn.get("current_element_id"))
                if curr_el:
                    curr_el._inferred_occupied = False
                    curr_el._train_name = None
                    curr_el._estimated_speed_mph = None
                    curr_el._train_direction = None
                    curr_el._is_dimmed = False

        train_tracker.prune_inactive(active_occ_ids, now)
        return active_occ_ids
