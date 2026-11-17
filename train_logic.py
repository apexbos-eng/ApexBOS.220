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

from typing import Dict, List, Optional, Tuple, Any, Set
import math
import time
from version import VERSION

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
    def __init__(self, x: int = 86, y: int = 25, max_len: int = 10, width: int = 430, height: int = 150):
        self.id = "SYS_TRAIN_SCROLLER"
        self.elem_type = "train_scroller"
        self.x = x
        self.y = y
        self.max_len = max_len
        self.width = int(width) if width and int(width) > 0 else 430
        self.height = int(height) if height and int(height) > 0 else 150
        self.trains: Dict[str, dict] = {}
        self._next_train_num: int = 101

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
            "last_packet_ts": 0.0,
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

    def check_packet(self, parsed_pkt: dict):
        """
        Rule 10: If a packet arrives with _node_id_ matching the assigned train ID,
        display an indication that the train is receiving packets.
        If '_speed_' is in the packet, update its estimated speed.
        """
        node_id = parsed_pkt.get("_node_id_")
        if node_id is None:
            return
        node_str = str(node_id).strip()
        for trn in self.trains.values():
            if trn.get("assigned_id") is not None and str(trn["assigned_id"]).strip() == node_str:
                trn["is_receiving_packets"] = True
                trn["packets_received"] = trn.get("packets_received", 0) + 1
                trn["last_packet_ts"] = parsed_pkt.get("timestamp", time.time())
                if "_speed_" in parsed_pkt:
                    try:
                        s = float(parsed_pkt["_speed_"])
                        trn["speed_mph"] = round(s, 1)
                        trn["speed_str"] = f"{s:.1f} MPH"
                    except (ValueError, TypeError):
                        pass

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
        rec.update({
            "train_key": train_name,
            "train_name": train_name,
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
            "last_packet_ts": rec.get("last_packet_ts", 0.0),
        })
        self.trains[train_name] = rec
        return train_name

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
            return self.trains.pop(matched_key, None)
        return None

    def prune_inactive(self, active_elem_ids: set, now: float, max_age: float = 180.0):
        """Cleans up stale orphaned records whose elements have been inactive for max_age."""
        to_delete = [
            tname for tname, rec in self.trains.items()
            if rec.get("status") != "Potential"
            and rec.get("current_element_id") not in active_elem_ids
            and rec.get("last_element_id") not in active_elem_ids
            and (now - rec.get("updated_ts", 0.0)) > max_age
        ]
        for tname in to_delete:
            del self.trains[tname]

    def to_state_dict(self, layout_wiu_ids: Optional[set] = None, rrdata: Optional[Dict[str, str]] = None) -> dict:
        """Serializes current trains for network telemetry broadcast and renderer consumption."""
        sorted_trains = sorted(self.trains.values(), key=lambda r: r.get("updated_ts", 0.0), reverse=True)[: self.max_len]
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


# Maintain backward-compatible class alias
TrainScroller = TrainTracker


class TrainInferenceManager:
    """
    Encapsulates railroad layout path tracing, signal event correlation,
    potential train de-duplication, speed estimation, dimming, and timeout logic.
    """

    @staticmethod
    def resolve_through_jump(
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
            for el in elements.values():
                if getattr(el, "elem_type", "") == "track_jump" and getattr(el, "connected_element", None) == curr_id:
                    jump_el = el
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
            facing_sigs = [
                s for s in elements.values()
                if (getattr(s, "elem_type", "") == "signal" or hasattr(s, "is_permissive"))
                and getattr(s, "direction", "") == curr_dir
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
        sig_id = getattr(sig, "id", "")
        downstream: List[str] = []
        for i in range(len(path)):
            curr_id, curr_dir = path[i]
            next_id = path[i + 1][0] if i + 1 < len(path) else None
            step_sigs = []
            for s in elements.values():
                if not (getattr(s, "elem_type", "") == "signal" or hasattr(s, "is_permissive")):
                    continue
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

        # Signal boundary check
        for s in elements.values():
            if (getattr(s, "elem_type", "") == "signal" or hasattr(s, "is_permissive")) and getattr(s, "direction", "") == travel_dir:
                if (getattr(s, "rear_element", None) == el1_id and getattr(s, "protects_element", None) == el2_id) or \
                   (getattr(s, "protects_element", None) == el1_id and getattr(s, "rear_element", None) == el2_id):
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
        curr_id = start_id
        curr_dir = travel_dir
        prev_id = None
        visited: Set[str] = set()
        jump_fn = resolve_jump_fn or (lambda nid, ndir, cid: cls.resolve_through_jump(elements, nid, ndir, cid))

        for _ in range(max_depth):
            if curr_id == target_id:
                return True
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
            if curr_id == target_id:
                return True
        return False

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
        if not d1 or not d2 or d1 != d2:
            return False, True

        p1 = trn1.get("path_element_ids") or []
        p2 = trn2.get("path_element_ids") or []
        sig1 = trn1.get("initial_signal_id")
        sig2 = trn2.get("initial_signal_id")
        ds1 = trn1.get("downstream_signals") or []
        ds2 = trn2.get("downstream_signals") or []

        # 1. Downstream signal explicitly in path
        if sig2 and sig2 in ds1:
            return True, True
        if sig1 and sig1 in ds2:
            return True, False

        # 2. Overlapping track elements
        overlap = set(p1) & set(p2)
        if overlap:
            first_overlap = None
            for eid in p1:
                if eid in overlap:
                    first_overlap = eid
                    break
            idx1 = p1.index(first_overlap) if first_overlap else 0
            idx2 = p2.index(first_overlap) if first_overlap in p2 else 0
            return True, (idx1 >= idx2)

        # 3. End-to-start element continuity
        if p1 and p2:
            if cls.are_elements_continuous(p1[-1], p2[0], d1, elements, resolve_jump_fn) or \
               cls.is_path_reachable(p1[-1], p2[0], d1, elements, resolve_jump_fn):
                return True, True
            if cls.are_elements_continuous(p2[-1], p1[0], d1, elements, resolve_jump_fn) or \
               cls.is_path_reachable(p2[-1], p1[0], d1, elements, resolve_jump_fn):
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
                    is_cont, trn1_is_up = cls.are_trains_on_same_continuous_path(trn1, trn2, elements, resolve_jump_fn)
                    if is_cont:
                        if trn1_is_up:
                            cls.merge_two_potential_trains(train_tracker, key1, key2)
                        else:
                            cls.merge_two_potential_trains(train_tracker, key2, key1)
                        merged = True
                        break
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
                        is_cont, act_is_up = cls.are_trains_on_same_continuous_path(trn_act, trn_pot, elements, resolve_jump_fn)
                        if is_cont and act_is_up:
                            cls.merge_two_potential_trains(train_tracker, key_act, key_pot)
                            merged = True
                            break
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

        # 1. Rule 3: Check if this signal drop matches a Potential train (first signal in lined path)
        matched_potential = None
        for tkey, trn in train_tracker.trains.items():
            if trn.get("status") == "Potential" and trn.get("initial_signal_id") == sig_id:
                matched_potential = trn
                break

        # 2. Rule 4: Check if this signal drop matches an Inferred or Confirmed train expecting this signal
        matched_expected = None
        if not matched_potential:
            for tkey, trn in train_tracker.trains.items():
                if trn.get("status") in ("Inferred", "Confirmed"):
                    # Rule 6: Ignore opposite direction signal drops
                    if trn.get("direction") != sig_dir:
                        continue
                    exp_sig = trn.get("expected_signal_id")
                    if exp_sig == sig_id or (exp_sig is None and (sig_id in trn.get("downstream_signals", []) or trn.get("timeout_start_ts") is not None or trn.get("is_dimmed"))):
                        matched_expected = trn
                        break

        # 3. Rule 5: Non-next signal on the same path going the same direction drops
        if not matched_potential and not matched_expected:
            for tkey, trn in train_tracker.trains.items():
                if trn.get("status") in ("Inferred", "Confirmed"):
                    if trn.get("direction") != sig_dir:
                        continue
                    # Check if sig_id is a downstream signal or along this train's path, but NOT the expected next one
                    ds_list = trn.get("downstream_signals", [])
                    if sig_id in ds_list and sig_id != trn.get("expected_signal_id"):
                        # Rule 5: Keep existing train, do not create new train, continue watching expected signal
                        return trn

        # 4. Fallback: If no potential train was lined up, check if a potential train can be formed or inferred
        if not matched_potential and not matched_expected:
            # Check if any train in the same direction on this continuous path already exists
            path = cls.trace_signal_green_path(sig, elements, resolve_jump_fn)
            if not path:
                path = [(getattr(sig, "protects_element", ""), sig_dir)]
            downstream_sigs = cls.extract_downstream_signals(sig, path, elements)

            # Check if this signal is on any existing train's path going the same direction
            has_existing = False
            for trn in train_tracker.trains.values():
                if trn.get("direction") == sig_dir:
                    overlap = set(p[0] for p in path) & set(trn.get("path_element_ids", []))
                    if overlap or sig_id in trn.get("downstream_signals", []):
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
                trn["ahead_elements"] = fwd_eids
                trn["downstream_signals"] = fwd_sigs
                trn["expected_signal_id"] = fwd_sigs[0] if fwd_sigs else None
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
                trn["ahead_elements"] = fwd_eids
                trn["downstream_signals"] = fwd_sigs
                trn["expected_signal_id"] = fwd_sigs[0] if fwd_sigs else None
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

                # Associate with existing train going in same direction if downstream/ahead
                is_duplicate = False
                matched_existing = None
                for tkey, trn in train_tracker.trains.items():
                    if trn.get("direction") != sig_dir:
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
                    if set(path_eids) & set(trn.get("ahead_elements", [])):
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 5. Continuous connection from train's current element to prot_el
                    curr_el = trn.get("current_element_id")
                    if curr_el and (cls.are_elements_continuous(curr_el, prot_el, sig_dir, elements, resolve_jump_fn) or
                                   cls.is_path_reachable(curr_el, prot_el, sig_dir, elements, resolve_jump_fn, max_depth=6)):
                        is_duplicate = True
                        matched_existing = trn
                        break
                    # 6. Fallback overlap with path_element_ids
                    overlap = set(path_eids) & set(trn.get("path_element_ids", []))
                    if overlap:
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
