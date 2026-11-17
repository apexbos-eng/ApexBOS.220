#!/usr/bin/env python3
# ==============================================================================
# ApexBOS.220.py
# Copyright (c) 2026 AB
# Licensed under the MIT License
# ==============================================================================
"""
ApexBOS.220 Dispatcher Host Node (v26.117.2)
- Receives live raw packets from ApexBOSFlux UDP Multicast and ApexBOSBanc Cloud (TCP/Fernet).
- Feeds raw packets + telemetry metadata into DispatcherEngine (from dispatcher_engine.py).
- DispatcherEngine evaluates track/turnout/signal/crossing/collision logic, serves the
  layout JSON to remote View_ApexBOS.220.py clients over TCP, and broadcasts render state
  on a separate UDP multicast address/port.
- Displays live text statistics and status/info/warning messages locally (no layout graphics).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from typing import Dict, Any, Optional, List
import re

try:
    import curses
except ImportError:
    curses = None

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

import mosfet_client
import mosfet_auth
from version import VERSION
from dispatcher_engine import DispatcherEngine

FIREWALL_HELP_NOTICE = """
Windows Firewall Requirement:
  To ingest UDP multicast packets from ApexBOSFlux or Grabber on Windows,
  Windows Defender Firewall must allow incoming UDP and TCP traffic, IGMP, and multicast routes.
  Run enable_firewall.bat as Administrator, or paste this command into an Administrator PowerShell prompt:
    New-NetFirewallRule -DisplayName "ApexBOS Inbound UDP Multicast" -Direction Inbound -LocalPort 23003,5007 -Protocol UDP -Action Allow -Profile Domain,Private,Public; New-NetFirewallRule -DisplayName "ApexBOS Inbound TCP Services" -Direction Inbound -LocalPort 1234,5008,18000-18175,20101-20175,23009,23010,56781 -Protocol TCP -Action Allow -Profile Domain,Private,Public; New-NetFirewallRule -DisplayName "ApexBOS Inbound IGMP" -Direction Inbound -Protocol 2 -Action Allow -Profile Domain,Private,Public; route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 metric 1
"""

DEFAULT_CONFIG = {
    "engine": {
        "mcast_group": "224.1.1.1",
        "mcast_port": 5007,
        "layout_port": 5008,
        "inferred_timeout_sec": 120.0,
        "train_logic_interval_sec": 7.0,
        "time_scale": 360.0,
        "dedup_ttl": 0.4,
        "parsers": [
            "x33_rule.json",
            "x2_base_rules.json",
            "x01_rules.json",
            "x05_rules.json",
            "x70_rules.json",
            "x06_rules.json",
            "x04_rules.json",
        ],
        "connections": {
            "mcast_group": "239.1.1.1",
            "mcast_port": 23003,
            "banc_host": "none",
            "banc_port": 23009,
            "auth_secret": "_SECRET_KEY_123",
            "encryption_key": "",
            "banc_pacing": True,
            "banc_startup_burst": True,
            "banc_initial_history_sec": 60,
        },
    },
    "connections": {
        "mcast_group": "239.1.1.1",
        "mcast_port": 23003,
        "banc_host": "none",
        "banc_port": 23009,
        "auth_secret": "_SECRET_KEY_123",
        "encryption_key": "",
        "banc_pacing": True,
        "banc_startup_burst": True,
        "banc_initial_history_sec": 60,
    },
    "parsers": [
        "x33_rule.json",
        "x2_base_rules.json",
        "x01_rules.json",
        "x05_rules.json",
        "x70_rules.json",
        "x06_rules.json",
        "x04_rules.json",
    ],
}


# ---------------------------------------------------------------------------
# Packet Reception Statistics
# ---------------------------------------------------------------------------

class PacketStats:
    def __init__(self):
        self.start_time = time.time()
        self.banc_total = 0
        self.mcast_total = 0
        self.decoded_total = 0
        self.unknown_total = 0
        self.banc_times: deque = deque()
        self.mcast_times: deque = deque()

    @property
    def valid_total(self) -> int:
        return self.decoded_total

    @valid_total.setter
    def valid_total(self, val: int):
        self.decoded_total = val

    @property
    def invalid_total(self) -> int:
        return self.unknown_total

    @invalid_total.setter
    def invalid_total(self, val: int):
        self.unknown_total = val

    def add_banc(self, count: int = 1):
        now = time.time()
        self.banc_total += count
        for _ in range(count):
            self.banc_times.append(now)

    def add_mcast(self, count: int = 1):
        now = time.time()
        self.mcast_total += count
        for _ in range(count):
            self.mcast_times.append(now)

    def add_decoded(self, count: int = 1):
        self.decoded_total += count

    def add_unknown(self, count: int = 1):
        self.unknown_total += count

    def add_valid(self, count: int = 1):
        self.add_decoded(count)

    def add_invalid(self, count: int = 1):
        self.add_unknown(count)

    @staticmethod
    def _clean(q: deque, now: float):
        while q and now - q[0] > 60.0:
            q.popleft()

    def get_stats(self) -> Dict[str, Any]:
        now = time.time()
        self._clean(self.banc_times, now)
        self._clean(self.mcast_times, now)

        elapsed_sec = max(0.0, now - self.start_time)
        el_int = int(elapsed_sec)
        elapsed_str = f"{el_int // 3600:02d}:{(el_int % 3600) // 60:02d}:{el_int % 60:02d}"

        ratio = 0.0
        if self.decoded_total > 0:
            ratio = 100.0 - self.unknown_total / self.decoded_total * 100.0
        elif self.decoded_total > 0:
            ratio = float("inf")

        return {
            "elapsed_sec": elapsed_sec,
            "elapsed_str": elapsed_str,
            "total": self.banc_total + self.mcast_total,
            "banc_total": self.banc_total,
            "mcast_total": self.mcast_total,
            "total_ppm": len(self.banc_times) + len(self.mcast_times),
            "banc_ppm": len(self.banc_times),
            "mcast_ppm": len(self.mcast_times),
            "decoded_total": self.decoded_total,
            "unknown_total": self.unknown_total,
            "valid_total": self.decoded_total,
            "invalid_total": self.unknown_total,
            "ratio": ratio,
        }


# ---------------------------------------------------------------------------
# Dispatcher Host Controller (Engine Bridge + Status/Stats Monitor)
# ---------------------------------------------------------------------------

class DispatcherHostMonitor:
    def __init__(
        self,
        config: dict,
        layout_file: str,
        engine_mcast_group: str = "224.1.1.1",
        engine_mcast_port: int = 5007,
        layout_port: int = 5008,
        stdscr=None,
        banc_layout: Optional[str] = None,
        dedup_ttl: Optional[float] = None,
        unicast_clients: Optional[List[str]] = None,
        scroll_decoded: bool = False,
        force_banc_source: bool = False,
        initial_source: Optional[str] = None,
    ):
        self.stdscr = stdscr
        self.config = config
        self.scroll_decoded: bool = bool(scroll_decoded)
        self.stats = PacketStats()
        self.identity = "INITIALIZING..."

        engine_section = config.get("engine", {}) if isinstance(config.get("engine"), dict) else {}
        connections = self._get_connections()
        self.packet_source = self._init_packet_source(force_banc_source=force_banc_source, initial_source=initial_source)
        self.force_banc_source = bool(self.packet_source == "banc")
        self._banc_task = None
        self._mcast_task = None
        self._banc_reset_needed = False

        self.auth_secret = connections.get("auth_secret")
        self.rx_mcast_group = connections.get("mcast_group")
        if self.rx_mcast_group is None and "mcast_group" not in connections:
            self.rx_mcast_group = "239.1.1.1"
        raw_rx_prt = connections.get("mcast_port")
        if raw_rx_prt is None and "mcast_port" not in connections:
            raw_rx_prt = 23003
        self.rx_mcast_port = int(raw_rx_prt) if (raw_rx_prt is not None and str(raw_rx_prt).isdigit()) else None
        self.banc_host = connections.get("banc_host", "none")
        self.banc_port = connections.get("banc_port", 23009)
        self.banc_layout_name: Optional[str] = banc_layout

        self.active_file = self._resolve_layout_path(layout_file, config_path=config.get("config_path"))
        self.messages: deque = deque(maxlen=200)
        self._last_collision_msgs: set = set()
        self.last_seen_banc_time: float = 0.0
        self.banc_pacing_queue: Optional[asyncio.Queue] = None
        self.banc_pacer_task = None
        self._last_banc_layouts: List[dict] = []
        self.input_mode: Optional[str] = None
        self.input_buffer: str = ""
        self.banc_layout_page_offset: int = 0
        self._tab_completion_idx: int = -1
        self._initial_cleared: bool = False
        self._last_wiu_sync_err_ts: float = 0.0

        engine_cfg = dict(engine_section)
        parsers_list = engine_section.get("parsers") or config.get("parsers") or DEFAULT_CONFIG["parsers"]
        engine_cfg["parsers"] = list(parsers_list)
        engine_cfg["connections"] = connections
        engine_cfg["engine_mcast_group"] = engine_mcast_group
        engine_cfg["engine_mcast_port"] = engine_mcast_port
        engine_cfg["layout_server_port"] = layout_port
        engine_cfg["config_path"] = config.get("config_path")
        engine_cfg["quiet"] = (self.stdscr is not None)
        engine_cfg["log_fn"] = self.log_message if self.stdscr is not None else None
        if dedup_ttl is not None:
            engine_cfg["dedup_ttl"] = dedup_ttl
        if unicast_clients:
            engine_cfg["unicast_clients"] = unicast_clients

        self.engine = DispatcherEngine(
            config=engine_cfg,
            layout_filepath=self.active_file if not self.banc_layout_name else None,
            mcast_group=engine_mcast_group,
            mcast_port=engine_mcast_port,
            layout_server_port=layout_port,
            dedup_ttl=dedup_ttl,
            unicast_clients=unicast_clients,
        )

        if self.stdscr is not None and curses is not None:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            curses.init_pair(6, curses.COLOR_CYAN, -1)

        if self.banc_layout_name:
            self.load_layout_from_banc(self.banc_layout_name)

        self.log_message(
            "INFO",
            f"DispatcherEngine {VERSION} initialized | Layout: '{self.engine.layout_filename}' ({len(self.engine.elements)} elements)",
        )
        self.log_message(
            "INFO",
            f"Serving remote layout over TCP 0.0.0.0:{self.engine.layout_server_port} | State Multicast TX -> {self.engine.mcast_group}:{self.engine.mcast_port}",
        )
        self.log_message(
            "INFO",
            f"Loaded {len(self.engine.parsers)} packet parser(s) from configuration",
        )
        if not self.banc_layout_name and not os.path.exists(self.active_file):
            self.log_message("WARN", f"Layout file '{self.active_file}' not found on disk (serving empty layout)")
        if not self.engine.parsers:
            self.log_message("WARN", "No valid packet parser rule files found! Check 'parsers' in config.")

    def _get_connections(self) -> dict:
        eng = self.config.get("engine", {}) if isinstance(self.config.get("engine"), dict) else {}
        conns = dict(eng.get("connections") or self.config.get("connections", {}))
        cb = (self.config.get("common", {}) if isinstance(self.config.get("common"), dict) else {}).get("banc", {})
        if isinstance(cb, dict):
            if not conns.get("auth_secret") and cb.get("auth_secret"):
                conns["auth_secret"] = cb["auth_secret"]
            if not conns.get("encryption_key") and cb.get("encryption_key"):
                conns["encryption_key"] = cb["encryption_key"]
            if not conns.get("identity") and cb.get("identity"):
                conns["identity"] = cb["identity"]
            if (not conns.get("banc_host") or str(conns.get("banc_host")).lower() == "none") and cb.get("host") and str(cb.get("host")).lower() != "none":
                conns["banc_host"] = cb["host"]
            if not conns.get("banc_port") and cb.get("port"):
                conns["banc_port"] = cb["port"]
        top_banc = self.config.get("banc", {})
        if isinstance(top_banc, dict):
            if not conns.get("auth_secret") and top_banc.get("auth_secret"):
                conns["auth_secret"] = top_banc["auth_secret"]
            if not conns.get("encryption_key") and top_banc.get("encryption_key"):
                conns["encryption_key"] = top_banc["encryption_key"]
        if not conns.get("banc_initial_history_sec"):
            if isinstance(cb, dict) and cb.get("initial_history_sec"):
                conns["banc_initial_history_sec"] = cb["initial_history_sec"]
            elif isinstance(cb, dict) and cb.get("banc_initial_history_sec"):
                conns["banc_initial_history_sec"] = cb["banc_initial_history_sec"]
            elif isinstance(top_banc, dict) and top_banc.get("initial_history_sec"):
                conns["banc_initial_history_sec"] = top_banc["initial_history_sec"]
            elif isinstance(top_banc, dict) and top_banc.get("banc_initial_history_sec"):
                conns["banc_initial_history_sec"] = top_banc["banc_initial_history_sec"]
            elif self.config.get("banc_initial_history_sec"):
                conns["banc_initial_history_sec"] = self.config["banc_initial_history_sec"]
            else:
                conns["banc_initial_history_sec"] = 60
        return conns

    def _init_packet_source(self, force_banc_source: bool = False, initial_source: Optional[str] = None) -> str:
        connections = self._get_connections()
        forced_source = str(connections.get("packet_source") or self.config.get("packet_source") or "").lower().strip()
        common_src = str((self.config.get("common") or {}).get("packet_source") or "").lower().strip()
        eng_src = str((self.config.get("engine") or {}).get("packet_source") or "").lower().strip()

        cli_src = str(initial_source or "").lower().strip()
        if cli_src in ("mcast", "multicast", "flux", "udp"):
            cli_choice = "mcast"
        elif cli_src in ("banc", "cloud", "tcp"):
            cli_choice = "banc"
        elif cli_src in ("both", "all", "dual"):
            cli_choice = "both"
        else:
            cli_choice = None

        if cli_choice:
            explicit_source = cli_choice
        elif force_banc_source:
            explicit_source = "banc"
        else:
            explicit_source = ""
            for s in (forced_source, common_src, eng_src):
                if s in ("both", "all", "dual"):
                    explicit_source = "both"
                    break
                elif s in ("banc", "cloud", "tcp"):
                    explicit_source = "banc"
                    break
                elif s in ("mcast", "multicast", "flux", "udp"):
                    explicit_source = "mcast"
                    break

        m_valid = self.is_multicast_valid()
        b_valid = self.is_banc_valid()

        if m_valid and b_valid:
            if explicit_source in ("both", "banc", "mcast"):
                return explicit_source
            return "mcast"  # Default when both are defined: automatically select multicast
        elif m_valid:
            return "mcast"
        elif b_valid:
            return "banc"
        else:
            return "none"

    def set_packet_source(self, new_source: str) -> bool:
        src = str(new_source or "").lower().strip()
        if src in ("mcast", "multicast", "flux", "udp"):
            target = "mcast"
        elif src in ("banc", "cloud", "tcp"):
            target = "banc"
        elif src in ("both", "all", "dual"):
            target = "both"
        else:
            self.log_message("WARN", f"Unknown packet source '{new_source}'. Valid options: mcast, banc, both")
            return False

        m_valid = self.is_multicast_valid()
        b_valid = self.is_banc_valid()

        if target == "both" and not (m_valid and b_valid):
            self.log_message("WARN", "Cannot select 'both': both Multicast and Banc must be configured and valid.")
            return False
        if target == "mcast" and not m_valid:
            self.log_message("WARN", "Cannot select 'mcast': Multicast is not configured or disabled.")
            return False
        if target == "banc" and not b_valid:
            self.log_message("WARN", "Cannot select 'banc': Banc Cloud is not configured or disabled.")
            return False

        if target == self.packet_source:
            self.log_message("INFO", f"Packet source is already '{target.upper()}'.")
            return True

        old_source = self.packet_source
        self.packet_source = target
        self.force_banc_source = bool(target == "banc")

        if target in ("banc", "both") and old_source == "mcast":
            self._banc_reset_needed = True
            self.ensure_banc_listener_running()

        self.log_message("ALERT", f"Packet source switched: {old_source.upper()} -> {target.upper()}")
        return True

    def cycle_packet_source(self) -> str:
        m_valid = self.is_multicast_valid()
        b_valid = self.is_banc_valid()
        if not (m_valid and b_valid):
            cur = self.packet_source.upper()
            self.log_message("WARN", f"Cannot cycle packet source: only {cur} is valid in configuration.")
            return self.packet_source

        order = ["mcast", "banc", "both"]
        cur = self.packet_source if self.packet_source in order else "mcast"
        next_idx = (order.index(cur) + 1) % len(order)
        nxt = order[next_idx]
        self.set_packet_source(nxt)
        return self.packet_source

    def ensure_banc_listener_running(self):
        if not self.is_banc_valid():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            if self._banc_task is None or self._banc_task.done():
                self._banc_task = loop.create_task(banc_listener(self, self.config))

    def prompt_set_source(self):
        m_valid = self.is_multicast_valid()
        b_valid = self.is_banc_valid()
        if not (m_valid and b_valid):
            cur = self.packet_source.upper()
            self.log_message("WARN", f"Cannot switch source: only {cur} is configured and valid.")
            return
        self.input_mode = "set_source"
        self.input_buffer = ""
        cur = self.packet_source.upper()
        self.log_message("INFO", f"Select Packet Source [1=MCAST, 2=BANC, 3=BOTH, or press Enter to cycle] (current: {cur}):")

    def is_multicast_valid(self) -> bool:
        connections = self._get_connections()
        group = connections.get("mcast_group")
        port = connections.get("mcast_port")
        return bool(group and str(group).lower() != "none" and str(group).strip() != "" and port is not None)

    def is_banc_valid(self) -> bool:
        connections = self._get_connections()
        host = connections.get("banc_host")
        port = connections.get("banc_port")
        auth = connections.get("auth_secret")
        enc = connections.get("encryption_key")
        return bool(
            host
            and str(host).lower() != "none"
            and str(host).strip() != ""
            and port is not None
            and auth is not None
            and str(auth).strip() != ""
            and enc is not None
            and str(enc).strip() != ""
        )

    def _get_banc_client(self) -> Optional[mosfet_client.BancClient]:
        if not self.is_banc_valid():
            return None
        connections = self._get_connections()
        cfg_ident = connections.get("identity") or self.config.get("identity")
        if cfg_ident and str(cfg_ident).lower() != "auto":
            resolved_ident = str(cfg_ident)
        elif self.identity and self.identity not in ("INITIALIZING...", "GENERATING ID...", "OFFLINE", "KEY_ERROR"):
            resolved_ident = str(self.identity)
        else:
            resolved_ident = "ApexBOS_Host"
        return mosfet_client.BancClient(
            host=self.banc_host,
            port=int(self.banc_port),
            secret_key=self.auth_secret,
            identity=resolved_ident,
            encryption_key=connections.get("encryption_key"),
        )

    async def sync_wius_to_banc(self, flush_all: bool = False) -> int:
        """Uploads newly discovered or updated WIU metadata to Banc Cloud when configured."""
        if not self.is_banc_valid():
            return 0
        client = self._get_banc_client()
        if not client:
            return 0
        if flush_all:
            self.engine.pop_dirty_wius()
            batch = self.engine.get_tracked_wius()
        else:
            batch = self.engine.pop_dirty_wius()
        if not batch:
            return 0
        try:
            resp = await client.put_wius(batch, timeout=10.0)
            return int((resp or {}).get("upserted", len(batch)))
        except Exception as exc:
            self.engine.requeue_dirty_wius([item.get("wiu_id") for item in batch if isinstance(item, dict)])
            now = time.time()
            if now - getattr(self, "_last_wiu_sync_err_ts", 0.0) >= 30.0:
                self._last_wiu_sync_err_ts = now
                self.log_message("WARN", f"Banc WIU sync warning ({self.banc_host}:{self.banc_port}): {exc}")
            return -1

    def list_banc_layouts(self) -> List[dict]:
        """Queries ApexBOSBanc Cloud for list of remote layouts, logs summary, and returns list."""
        client = self._get_banc_client()
        if not client:
            self.log_message("WARN", "Cannot list Banc layouts: Banc host or auth_secret not configured.")
            return []
        try:
            layouts = client.list_layouts_sync()
            self._last_banc_layouts = layouts
            self.log_message("INFO", f"Fetched {len(layouts)} layout(s) from ApexBOSBanc Cloud ({self.banc_host}:{self.banc_port}):")
            for idx, item in enumerate(layouts):
                name = item.get("layout_name") or item.get("name") or "unknown"
                status = "AVAILABLE" if item.get("is_available") else "UNAVAILABLE"
                lat = item.get("center_lat")
                lon = item.get("center_lon")
                rad = item.get("radius_km")
                gps_str = f"GPS: ({lat:.4f}, {lon:.4f})" if lat is not None and lon is not None else "No GPS"
                rad_str = f"Rad: {rad:.2f}km" if rad is not None else ""
                self.log_message("INFO", f"  #{idx+1:<2} {name:<20} [{status:<11}] {gps_str} {rad_str}")
            return layouts
        except Exception as exc:
            self.log_message("ERROR", f"Failed to list Banc layouts: {exc}")
            return []

    def prompt_load_banc_layout(self):
        """Activates interactive prompt to load a layout from ApexBOSBanc Cloud."""
        if not self._last_banc_layouts:
            self.list_banc_layouts()
        self.input_mode = "load_banc"
        self.input_buffer = ""
        self.banc_layout_page_offset = 0
        self._tab_completion_idx = -1
        self.log_message("INFO", "Enter Banc layout name or # to load, or press Esc/Enter to cancel:")

    def load_layout_from_banc(self, layout_name: str) -> bool:
        """Downloads a layout from ApexBOSBanc Cloud and loads it into DispatcherEngine."""
        client = self._get_banc_client()
        if not client:
            self.log_message("ERROR", f"Cannot load Banc layout '{layout_name}': Banc client not configured.")
            return False
        try:
            layout_data = client.download_layout_sync(layout_name)
            if not layout_data:
                self.log_message("ERROR", f"Banc layout '{layout_name}' download returned empty data.")
                return False
            self.banc_layout_name = layout_name
            self.active_file = f"banc_{layout_name}.json"
            self.engine.load_layout_data(layout_data, layout_filename=f"banc_{layout_name}.json")
            state = self.engine.get_render_state()
            self.engine.broadcast_state(state)
            self.log_message(
                "INFO",
                f"Loaded layout '{layout_name}' from ApexBOSBanc Cloud ({len(self.engine.elements)} elements) and broadcast snapshot",
            )
            return True
        except Exception as exc:
            self.log_message("ERROR", f"Failed loading layout '{layout_name}' from ApexBOSBanc Cloud: {exc}")
            return False

    @staticmethod
    def _resolve_layout_path(layout_file: Optional[str], base_dir: Optional[str] = None, config_path: Optional[str] = None) -> str:
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
        if base_dir:
            candidates.extend([
                os.path.join(base_dir, layout_file),
                os.path.join(base_dir, "layouts", fname),
                os.path.join(base_dir, "config", fname),
                os.path.join(base_dir, fname),
            ])
        if config_path:
            cfg_dir = os.path.dirname(os.path.abspath(config_path))
            candidates.extend([
                os.path.join(cfg_dir, layout_file),
                os.path.join(cfg_dir, "layouts", fname),
                os.path.join(cfg_dir, "config", fname),
                os.path.join(cfg_dir, fname),
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

    def log_message(self, level: str, text: str):
        ts = time.strftime("%H:%M:%S")
        entry = (ts, level.upper(), text)
        self.messages.appendleft(entry)
        if self.stdscr is None:
            print(f"[{ts}] [{level.upper():<5}] {text}", flush=True)

    def reload_layout(self):
        if getattr(self, "banc_layout_name", None):
            self.log_message("INFO", f"Reloading layout '{self.banc_layout_name}' from ApexBOSBanc Cloud...")
            self.load_layout_from_banc(self.banc_layout_name)
            return
        resolved = self._resolve_layout_path(self.active_file, config_path=self.config.get("config_path"))
        if os.path.exists(resolved):
            try:
                self.engine.load_layout(resolved)
                state = self.engine.get_render_state()
                self.engine.broadcast_state(state)
                self.log_message(
                    "INFO",
                    f"Reloaded layout '{self.engine.layout_filename}' ({len(self.engine.elements)} elements) and broadcast snapshot",
                )
            except Exception as exc:
                self.log_message("ERROR", f"Failed to reload layout '{resolved}': {exc}")
        else:
            self.log_message("WARN", f"Cannot reload layout: file '{resolved}' does not exist")

    def reload_parsers(self):
        try:
            summary = self.engine.reload_all_lookups()
            self.log_message("INFO", f"Reloaded parsers & layout lookups: {summary}")
        except Exception as exc:
            self.log_message("ERROR", f"Failed reloading parsers: {exc}")

    def force_broadcast(self):
        try:
            state = self.engine.get_render_state()
            self.engine.transmitted_frames_count += 1
            self.engine.broadcast_state(state)
            self.log_message(
                "INFO",
                f"Forced state broadcast #{self.engine.transmitted_frames_count} to {self.engine.mcast_group}:{self.engine.mcast_port}",
            )
        except Exception as exc:
            self.log_message("ERROR", f"Broadcast failed: {exc}")

    def set_dedup_ttl(self, ttl: float):
        """Set duplicate packet sequence expiration TTL in seconds."""
        try:
            val = max(0.0, float(ttl))
            self.engine.set_dedup_ttl(val)
            if val == 0.0:
                self.log_message("INFO", "Deduplication DISABLED (TTL = 0.0s)")
            else:
                self.log_message("INFO", f"Deduplication TTL set to {val:.2f}s")
        except (ValueError, TypeError) as exc:
            self.log_message("ERROR", f"Invalid dedup TTL '{ttl}': {exc}")

    def prompt_set_dedup(self):
        """Activates interactive prompt to set packet deduplication TTL."""
        self.input_mode = "set_dedup"
        self.input_buffer = ""
        self.log_message("INFO", f"Enter new Dedup TTL in seconds (current: {self.engine.dedup_ttl:.2f}s, 0=disable):")

    def execute_command(self, cmd_line: str):
        """Execute a text command from TUI input."""
        cmd = cmd_line.strip()
        if not cmd:
            return
        tokens = cmd.split()
        verb = tokens[0].lower()
        if verb in ("dedup", "dup", "ttl"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("status", "info"):
                    cur_ttl = getattr(self.engine, "dedup_ttl", 0.4)
                    st = "DISABLED" if cur_ttl == 0.0 else f"{cur_ttl:.2f}s"
                    self.log_message("INFO", f"Dedup Window: {st}")
                else:
                    val_str = tokens[2] if sub in ("window", "ttl", "sec", "win", "limit", "size") and len(tokens) > 2 else tokens[1]
                    try:
                        new_ttl = max(0.0, float(val_str))
                        self.set_dedup_ttl(new_ttl)
                    except ValueError:
                        self.log_message("ERROR", f"Invalid dedup TTL '{val_str}' (e.g. 'dedup 0.4' or '0')")
            else:
                cur_ttl = getattr(self.engine, "dedup_ttl", 0.4)
                st = "DISABLED" if cur_ttl == 0.0 else f"{cur_ttl:.2f}s"
                self.log_message("INFO", f"Dedup Window: {st} (use 'dedup <sec>' to change)")
        elif verb in ("clear", "cls"):
            self.messages.clear()
            self.log_message("INFO", "Cleared status message log")
        elif verb in ("save", "export"):
            try:
                saved = self.engine.save_packet_history()
                self.log_message("INFO", f"Saved {len(self.engine.packet_cache)} cached packets to '{saved}'")
            except Exception as exc:
                self.log_message("ERROR", f"Failed saving packets: {exc}")
        elif verb in ("scroll", "decoded", "pktscroll"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("on", "1", "true", "enable", "yes"):
                    self.scroll_decoded = True
                elif sub in ("off", "0", "false", "disable", "no"):
                    self.scroll_decoded = False
                else:
                    self.scroll_decoded = not self.scroll_decoded
            else:
                self.scroll_decoded = not self.scroll_decoded
            st = "ENABLED" if self.scroll_decoded else "DISABLED"
            self.log_message("INFO", f"Decoded packet scroll {st}")
        elif verb in ("source", "src", "packet_source", "pktsrc"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("status", "info", "cur", "show"):
                    self.log_message("INFO", f"Active Packet Source: {self.packet_source.upper()} (Mcast valid: {self.is_multicast_valid()}, Banc valid: {self.is_banc_valid()})")
                else:
                    self.set_packet_source(sub)
            else:
                if self.is_multicast_valid() and self.is_banc_valid():
                    self.cycle_packet_source()
                else:
                    self.log_message("INFO", f"Active Packet Source: {self.packet_source.upper()} (only one valid source configured)")
        elif verb in ("help", "?", "h"):
            self.log_message("INFO", "Commands: source [mcast|banc|both] | dedup <sec> | scroll [on|off] | clear | save | help")
        else:
            self.log_message("WARN", f"Unknown command: '{cmd}'. Type 'help' or 'dedup <sec>'.")

    def ingest_packet(self, raw_data: bytes, source: str, meta: Optional[dict] = None):
        if source == "MCAST":
            self.stats.add_mcast(1)
        elif source == "BANC":
            self.stats.add_banc(1)

        try:
            self.engine.ingest_raw_packet(raw_data, meta=meta)
        except Exception as exc:
            self.stats.add_unknown(1)
            self.log_message("ERROR", f"[{source}] Engine packet ingestion error: {exc}, raw: {raw_data}")
            return

        status = getattr(self.engine, "last_ingest_status", "UNKNOWN")
        pkt = getattr(self.engine, "last_parsed_packet", None) or {}

        if status in ("INVALID", "UNKNOWN"):
            self.stats.add_unknown(1)
            hex_str = getattr(self.engine, "last_invalid_hex", "") or (
                raw_data.hex().upper() if isinstance(raw_data, (bytes, bytearray)) else str(raw_data)
            )
            ch_in = (meta or {}).get("channel", 0)
            self.log_message("UNKNOWN", f"[{source}] Ch:{ch_in} UNKNOWN HEX: {hex_str}")
            return

        self.stats.add_decoded(1)
        wiu_id = pkt.get("_node_id_", pkt.get("ADDR", "-"))
        seq = pkt.get("_sequence_", "-")
        ch = pkt.get("Channel", pkt.get("channel_meta", 0))
        rssi = pkt.get("rssi", -75.0)
        scroll_txt = pkt.get("_scroll_ptc_", "")

        if status == "LOCOMOTIVE_IGNORED":
            if self.scroll_decoded:
                self.log_message("INFO", f"[{source}] Ignored locomotive frame from Node:{wiu_id} Ch:{ch}")
        elif status == "DUPLICATE_SEQ":
            pass
        elif status in ("BROADCAST", "NO_STATE_CHANGE"):
            if self.scroll_decoded:
                tx_tag = "TX->VIEW" if status == "BROADCAST" else "SUPP"
                detail = f" | {scroll_txt}" if scroll_txt else ""
                self.log_message(
                    "INFO",
                    f"[{source}][{tx_tag}] WIU:{wiu_id} Ch:{ch} Seq:{seq} RSSI:{rssi:.1f}dBm{detail}",
                )

        # Check for new collision alerts generated by the engine
        cur_collisions = {c.get("message", "") for c in self.engine.active_collisions if c.get("message")}
        for new_alert in cur_collisions - self._last_collision_msgs:
            self.log_message("ALERT", f"COLLISION WARNING: {new_alert}")
        for cleared_alert in self._last_collision_msgs - cur_collisions:
            self.log_message("INFO", f"Collision cleared: {cleared_alert}")
        self._last_collision_msgs = cur_collisions

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0):
        if self.stdscr is None:
            return
        max_y, max_x = self.stdscr.getmaxyx()
        if 0 <= y < max_y and 0 <= x < max_x and text:
            try:
                max_len = max(0, max_x - x - 2) if y >= max_y - 1 else max(0, max_x - x - 1)
                self.stdscr.addstr(y, x, text[:max_len], attr)
            except Exception:
                pass

    def draw(self):
        if self.stdscr is None:
            return
        if not self._initial_cleared:
            self.stdscr.clear()
            self._initial_cleared = True
        else:
            self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()

        c_white = curses.color_pair(1)
        c_yellow = curses.color_pair(2) | curses.A_BOLD
        c_green = curses.color_pair(3) | curses.A_BOLD
        c_red = curses.color_pair(4) | curses.A_BOLD
        c_magenta = curses.color_pair(5) | curses.A_BOLD
        c_cyan = curses.color_pair(6) | curses.A_BOLD

        # Row 0: Title Bar (responsive to terminal width so commands are never cut off)
        is_logging = getattr(self.engine, "raw_packet_logging", False)
        flash_rec = (int(time.time() * 2) % 2) == 0
        rec_str = " [● REC]" if (is_logging and flash_rec) else (" [○ REC]" if is_logging else "")
        ttl_str = f" ({self.engine.dedup_ttl:.2f}s)" if max_x >= 120 else ""
        banc_cmds = "| 'o' Load Banc " if (self.is_banc_valid() and max_x >= 140) else ""
        scroll_st = "ON" if self.scroll_decoded else "OFF"
        scroll_btn = f"| 'p' Scroll [{scroll_st}] "

        both_valid = self.is_multicast_valid() and self.is_banc_valid()
        src_btn = f"| 'i' Src [{self.packet_source.upper()}] " if both_valid else f"| Src: [{self.packet_source.upper()}] "

        if max_x >= 145:
            header = (
                f" ApexBOS.220 Dispatcher Engine Host ({VERSION}){rec_str} | Layout: {self.engine.layout_filename} "
                f"{banc_cmds}{src_btn}| 'd' Dedup{ttl_str} {scroll_btn}| 'r' Reload | 'R' Parsers | 's' Save | 'b' Broadcast | 'c' Clear | 'q' Quit "
            )
        elif max_x >= 110:
            header = (
                f" ApexBOS.220 ({VERSION}){rec_str} | {self.engine.layout_filename} "
                f"{src_btn}| 'd' Dedup{ttl_str} {scroll_btn}| 'r' Rel | 's' Save | 'b' Bcast | 'c' Clr | 'q' Quit "
            )
        else:
            l_name = self.engine.layout_filename
            if len(l_name) > 12:
                l_name = l_name[:9] + "..."
            header = (
                f" ApexBOS.220{rec_str} | {l_name} "
                f"| [{self.packet_source.upper()}] | 'd' Dedup | 'p' Scr [{scroll_st}] | 'r' Rel | 'c' Clr | 'q' Quit "
            )
        self._safe_addstr(0, 0, header.ljust(max(0, max_x - 1)), curses.A_REVERSE)

        # Row 1: Network & Engine Endpoints
        q_info = ""
        if self.banc_pacing_queue is not None:
            q_sz = self.banc_pacing_queue.qsize()
            if q_sz > 0:
                q_info = f" | Banc Q: {q_sz}"
        mcast_disp = f"{self.rx_mcast_group}:{self.rx_mcast_port}" if self.is_multicast_valid() else "none"
        net_str = (
            f" IN [Source: {self.packet_source.upper()} | Mcast: {mcast_disp} | Banc: {self.banc_host}:{self.banc_port}{q_info}]   "
            f"OUT -> VIEW [State Mcast: {self.engine.mcast_group}:{self.engine.mcast_port} | Layout TCP: 0.0.0.0:{self.engine.layout_server_port}]"
        )
        self._safe_addstr(1, 0, net_str, c_cyan)

        # Row 2: Auth & Cloud Identity Status
        if self.auth_secret:
            current_time = time.time()
            window_sec = 10 * 60
            next_window = ((current_time // window_sec) + 1) * window_sec
            mins, secs = divmod(int(next_window - current_time), 60)
            try:
                current_token = mosfet_auth.generate_token(self.auth_secret)
            except Exception:
                current_token = "ERROR"
            auth_str = f" ApexBOSBanc Cloud ID: {self.identity} | Auth Token: {current_token} | Renew In: {mins:02d}:{secs:02d}"
            self._safe_addstr(2, 0, auth_str, curses.A_DIM)

        # Row 3: Separator
        self._safe_addstr(3, 0, "-" * max(0, max_x - 1), curses.A_DIM)

        # Row 4-7: Packet & Engine Telemetry Statistics
        s = self.stats.get_stats()
        sc_state = self.engine.scroller.to_state_dict()
        ratio_str = f"{s['ratio']:.2f}%" if s["ratio"] != float("inf") else "INF"

        row4 = (
            f" RX STATS   | Elapsed: {s['elapsed_str']}  |  Rate P/Min [Total: {s['total_ppm']} | Mcast: {s['mcast_ppm']} | Banc: {s['banc_ppm']}]  "
            f"|  Packets [Total: {s['total']} | Mcast: {s['mcast_total']} | Banc: {s['banc_total']} | Decoded: {s['decoded_total']} | Unknown: {s['unknown_total']} | {ratio_str}]"
        )
        self._safe_addstr(4, 0, row4, c_green if s["decoded_total"] > 0 else c_white)

        active_wius = len(self.engine.address_table.wius)
        active_trains = len(self.engine.train_scroller.trains)
        active_cols = len(self.engine.active_collisions)
        row5 = (
            f" ENGINE TX  | State Frames Broadcast: {self.engine.transmitted_frames_count}  "
            f"|  Suppressed: {self.engine.suppressed_frames_count}  "
            f"|  Dedup TTL: {self.engine.dedup_ttl:.2f}s  "
            f"|  Elements: {len(self.engine.elements)}  |  Active WIUs: {active_wius}  "
            f"|  Tracked Trains: {active_trains}  |  Active Collisions: {active_cols}"
        )
        self._safe_addstr(5, 0, row5, c_red if active_cols > 0 else c_yellow)

        # Row 6: Abbreviated Channels, sorted by packet rate descending (top 3-5 based on terminal width)
        ch_stats = sc_state.get("channel_stats") or {}
        sorted_channels = sorted(
            ch_stats.items(),
            key=lambda kv: kv[1].get("ppm", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )
        max_ch = 5 if max_x >= 120 else (4 if max_x >= 90 else 3)
        ch_parts = [
            f"Ch{ch}:{cinfo.get('ppm', 0)}/m({cinfo.get('total', 0)})"
            for ch, cinfo in sorted_channels[:max_ch]
        ]
        ch_summary = "  ".join(ch_parts) if ch_parts else "None"
        tot_pkts = sc_state.get("total_packets", 0)
        tot_ppm = sc_state.get("total_ppm", 0)
        row6 = f" CHANNELS   | Tot: {tot_pkts} ({tot_ppm}/m)  |  Top: {ch_summary}"
        self._safe_addstr(6, 0, row6.ljust(max(0, max_x - 1))[: max_x - 1], c_cyan)

        # Row 7: Active Collision Banner (if any) or Divider (WIU table removed)
        log_start_y = 8
        if self.engine.active_collisions:
            col_msg = " !! ACTIVE COLLISION ALERTS: " + "  ||  ".join(
                c.get("message", "") for c in self.engine.active_collisions
            )
            self._safe_addstr(7, 0, col_msg.ljust(max(0, max_x - 1))[: max_x - 1], c_red | curses.A_REVERSE)
            log_start_y = 9
        else:
            self._safe_addstr(7, 0, "-" * max(0, max_x - 1), curses.A_DIM)

        # Main Workspace Display Area
        if self.input_mode == "load_banc":
            # Dedicated Non-Scrolling ApexBOSBanc Cloud Layouts Panel
            layouts = self._last_banc_layouts or []
            total_layouts = len(layouts)
            prompt_y = max_y - 1
            sugg_y = max(log_start_y + 3, max_y - 2)
            list_start_y = log_start_y
            avail_layout_rows = max(2, sugg_y - list_start_y - 2)

            cur_page = (self.banc_layout_page_offset // avail_layout_rows) + 1 if avail_layout_rows > 0 else 1
            total_pages = max(1, (total_layouts + avail_layout_rows - 1) // avail_layout_rows) if avail_layout_rows > 0 else 1

            panel_title = (
                f" APEXBOSBANC CLOUD LAYOUTS ({total_layouts} total | Page {cur_page}/{total_pages} - PgUp/PgDn to scroll, Tab to complete):"
            )
            self._safe_addstr(list_start_y, 0, panel_title.ljust(max(0, max_x - 1))[: max_x - 1], c_cyan | curses.A_BOLD)

            col_hdr = f"  {'#':<3} {'Layout Name':<24} {'Status':<13} {'Center GPS':<24} {'Radius':<10}"
            self._safe_addstr(list_start_y + 1, 0, col_hdr.ljust(max(0, max_x - 1))[: max_x - 1], c_yellow | curses.A_UNDERLINE)

            page_slice = layouts[self.banc_layout_page_offset : self.banc_layout_page_offset + avail_layout_rows]
            current_typed = self.input_buffer.strip().lower()

            for row_idx, item in enumerate(page_slice):
                y = list_start_y + 2 + row_idx
                global_idx = self.banc_layout_page_offset + row_idx + 1
                name = item.get("layout_name") or item.get("name") or "unknown"
                is_avail = bool(item.get("is_available"))
                st_str = "[AVAILABLE]  " if is_avail else "[UNAVAILABLE]"
                lat = item.get("center_lat")
                lon = item.get("center_lon")
                rad = item.get("radius_km")
                gps_str = f"({lat:.4f}, {lon:.4f})" if lat is not None and lon is not None else "No GPS"
                rad_str = f"{rad:.2f}km" if rad is not None else "---"

                line_str = f"  #{global_idx:<3} {name:<24} {st_str:<13} {gps_str:<24} {rad_str:<10}"

                matches_typed = bool(current_typed) and (
                    name.lower().startswith(current_typed)
                    or current_typed == str(global_idx)
                    or current_typed in name.lower()
                )

                if matches_typed:
                    attr = c_yellow | curses.A_BOLD | curses.A_REVERSE
                elif is_avail:
                    attr = c_white
                else:
                    attr = c_red | curses.A_DIM
                self._safe_addstr(y, 0, line_str.ljust(max(0, max_x - 1))[: max_x - 1], attr)

            for clr_y in range(list_start_y + 2 + len(page_slice), sugg_y):
                self._safe_addstr(clr_y, 0, " " * max(0, max_x - 1))

            # Suggested layout names
            matches = []
            if current_typed:
                for item in layouts:
                    n = item.get("layout_name") or item.get("name")
                    if n and (n.lower().startswith(current_typed) or current_typed in n.lower()):
                        matches.append(n)
            else:
                for item in layouts[:6]:
                    n = item.get("layout_name") or item.get("name")
                    if n:
                        matches.append(n)

            sugg_str = " Suggestions: " + (", ".join(matches[:5]) + ("..." if len(matches) > 5 else "") if matches else "(none)") + "   [Press TAB to complete]"
            self._safe_addstr(sugg_y, 0, sugg_str.ljust(max(0, max_x - 1))[: max_x - 1], c_cyan)

            # Interactive Prompt Bar
            prompt = f" LOAD APEXBOSBANC LAYOUT > {self.input_buffer}_  [Type name or #, Tab=Complete, PgUp/PgDn=Scroll, Esc=Cancel] "
            self._safe_addstr(prompt_y, 0, prompt.ljust(max(0, max_x - 1))[: max_x - 1], c_yellow | curses.A_REVERSE)

        else:
            # Status / Info / Warning Log Section
            self._safe_addstr(log_start_y, 0, " STATUS / INFO / WARNING MESSAGES (Newest First):", c_yellow)
            avail_rows = max(0, (max_y - 1) - (log_start_y + 1))
            for idx, (ts, lvl, msg) in enumerate(list(self.messages)[:avail_rows]):
                y = log_start_y + 1 + idx
                if lvl in ("INVALID", "UNKNOWN") or "INVALID HEX:" in msg or "UNKNOWN HEX:" in msg:
                    attr = c_magenta
                elif lvl in ("ERROR", "ALERT"):
                    attr = c_red
                elif lvl == "WARN":
                    attr = c_yellow
                elif "TX->VIEW" in msg:
                    attr = c_green
                else:
                    attr = c_white
                self._safe_addstr(y, 1, f"[{ts}] [{lvl:<5}] {msg}", attr)

            # Interactive prompt or bottom footer
            if self.input_mode == "set_source":
                prompt = f" SELECT PACKET SOURCE > {self.input_buffer}_  [1=MCAST, 2=BANC, 3=BOTH | Enter=Cycle/Apply, Esc=Cancel] (Current: {self.packet_source.upper()}) "
                self._safe_addstr(max_y - 1, 0, prompt.ljust(max(0, max_x - 1))[: max_x - 1], c_yellow | curses.A_REVERSE)
            elif self.input_mode == "set_dedup":
                prompt = f" SET PACKET DEDUP TTL (sec) > {self.input_buffer}_  [Current: {self.engine.dedup_ttl:.2f}s | 0 to disable | Enter=Apply, Esc=Cancel] "
                self._safe_addstr(max_y - 1, 0, prompt.ljust(max(0, max_x - 1))[: max_x - 1], c_yellow | curses.A_REVERSE)
            elif self.input_mode == "command":
                prompt = f" COMMAND > {self.input_buffer}_  [e.g. 'source banc', 'dedup 0.4', 'clear', 'help' | Enter=Execute, Esc=Cancel] "
                self._safe_addstr(max_y - 1, 0, prompt.ljust(max(0, max_x - 1))[: max_x - 1], c_yellow | curses.A_REVERSE)
            else:
                both_valid = self.is_multicast_valid() and self.is_banc_valid()
                src_help = f" 'i' Source ({self.packet_source.upper()}) |" if both_valid else ""
                footer = (
                    f"{src_help} 'd' Dedup TTL ({self.engine.dedup_ttl:.2f}s) | ':' Cmd | "
                    f"View_ApexBOS.220.py Connect Args: --engine-host <THIS_IP> --engine-port {self.engine.layout_server_port} "
                    f"--mcast-group {self.engine.mcast_group}:{self.engine.mcast_port} "
                )
                self._safe_addstr(max_y - 1, 0, footer.ljust(max(0, max_x - 1))[: max_x - 1], curses.A_REVERSE)

        try:
            self.stdscr.move(0, 0)
        except Exception:
            pass
        self.stdscr.refresh()

    async def handle_input(self, key: Optional[int] = None) -> bool:
        if key is None:
            if self.stdscr is None:
                return True
            try:
                key = self.stdscr.getch()
            except Exception:
                return True

        if key == -1:
            return True

        if self.input_mode == "load_banc":
            is_pgup = (curses and key == getattr(curses, "KEY_PPAGE", -999)) or key == 339
            is_pgdn = (curses and key == getattr(curses, "KEY_NPAGE", -999)) or key == 338
            is_up = (curses and key == getattr(curses, "KEY_UP", -999))
            is_down = (curses and key == getattr(curses, "KEY_DOWN", -999))

            if key == 27:  # Esc
                self.input_mode = None
                self.input_buffer = ""
                self._tab_completion_idx = -1
                self.log_message("INFO", "Banc layout selection cancelled.")
            elif key in (9, ord("\t")):  # Tab autocomplete
                layouts = self._last_banc_layouts or []
                all_names = [l.get("layout_name") or l.get("name") for l in layouts if l.get("layout_name") or l.get("name")]
                typed = self.input_buffer.strip().lower()
                matches = [n for n in all_names if n.lower().startswith(typed)]
                if not matches:
                    matches = [n for n in all_names if typed in n.lower()]
                if matches:
                    if len(matches) == 1:
                        self.input_buffer = matches[0]
                        self._tab_completion_idx = 0
                    else:
                        common = os.path.commonprefix([m.lower() for m in matches])
                        if len(common) > len(typed):
                            self.input_buffer = matches[0][:len(common)]
                        else:
                            self._tab_completion_idx = (self._tab_completion_idx + 1) % len(matches)
                            self.input_buffer = matches[self._tab_completion_idx]
            elif is_pgup or is_up:
                step = 5 if is_pgup else 1
                self.banc_layout_page_offset = max(0, self.banc_layout_page_offset - step)
            elif is_pgdn or is_down:
                step = 5 if is_pgdn else 1
                max_offset = max(0, len(self._last_banc_layouts) - 1)
                self.banc_layout_page_offset = min(max_offset, self.banc_layout_page_offset + step)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.input_buffer = self.input_buffer[:-1]
                self._tab_completion_idx = -1
            elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
                target = self.input_buffer.strip()
                self.input_mode = None
                self.input_buffer = ""
                self._tab_completion_idx = -1
                if not target:
                    self.log_message("INFO", "No layout entered.")
                elif target.isdigit():
                    idx = int(target) - 1
                    if 0 <= idx < len(self._last_banc_layouts):
                        sel = self._last_banc_layouts[idx]
                        lname = sel.get("layout_name") or sel.get("name")
                        self.load_layout_from_banc(lname)
                    else:
                        self.log_message("ERROR", f"Invalid layout number #{target} (available: 1..{len(self._last_banc_layouts)})")
                else:
                    self.load_layout_from_banc(target)
            elif 32 <= key <= 126:
                self.input_buffer += chr(key)
                self._tab_completion_idx = -1
            return True

        if self.input_mode == "set_source":
            if key == 27:  # Esc
                self.input_mode = None
                self.input_buffer = ""
                self.log_message("INFO", "Source selection cancelled.")
            elif key in (getattr(curses, "KEY_BACKSPACE", -999), 127, 8):
                self.input_buffer = self.input_buffer[:-1]
            elif key in (ord("\n"), ord("\r"), getattr(curses, "KEY_ENTER", 10)):
                target = self.input_buffer.strip().lower()
                self.input_mode = None
                self.input_buffer = ""
                if not target:
                    self.cycle_packet_source()
                elif target in ("1", "m", "mcast", "multicast", "flux", "udp"):
                    self.set_packet_source("mcast")
                elif target in ("2", "b", "banc", "cloud", "tcp"):
                    self.set_packet_source("banc")
                elif target in ("3", "both", "all", "dual"):
                    self.set_packet_source("both")
                else:
                    self.log_message("ERROR", f"Invalid packet source '{target}'. Enter 1 (mcast), 2 (banc), or 3 (both).")
            elif 32 <= key <= 126:
                self.input_buffer += chr(key)
            return True

        if self.input_mode == "set_dedup":
            if key == 27:  # Esc
                self.input_mode = None
                self.input_buffer = ""
                self.log_message("INFO", "Dedup TTL change cancelled.")
            elif key in (getattr(curses, "KEY_BACKSPACE", -999), 127, 8):
                self.input_buffer = self.input_buffer[:-1]
            elif key in (ord("\n"), ord("\r"), getattr(curses, "KEY_ENTER", 10)):
                target = self.input_buffer.strip()
                self.input_mode = None
                self.input_buffer = ""
                if not target:
                    self.log_message("INFO", f"Dedup TTL unchanged ({self.engine.dedup_ttl:.2f}s)")
                else:
                    try:
                        val = float(target)
                        self.set_dedup_ttl(val)
                    except ValueError:
                        self.log_message("ERROR", f"Invalid dedup TTL '{target}': must be a number in seconds (e.g. 0.4)")
            elif 32 <= key <= 126:
                ch = chr(key)
                if ch.isdigit() or ch in (".", "-"):
                    self.input_buffer += ch
            return True

        if self.input_mode == "command":
            if key == 27:  # Esc
                self.input_mode = None
                self.input_buffer = ""
                self.log_message("INFO", "Command cancelled.")
            elif key in (getattr(curses, "KEY_BACKSPACE", -999), 127, 8):
                self.input_buffer = self.input_buffer[:-1]
            elif key in (ord("\n"), ord("\r"), getattr(curses, "KEY_ENTER", 10)):
                cmd = self.input_buffer.strip()
                self.input_mode = None
                self.input_buffer = ""
                if cmd:
                    self.execute_command(cmd)
            elif 32 <= key <= 126:
                self.input_buffer += chr(key)
            return True

        if key in (ord("q"), ord("Q")):
            return False
        elif key == curses.KEY_RESIZE:
            self.stdscr.clear()
        elif key in (ord("d"), ord("D")):
            self.prompt_set_dedup()
        elif key in (ord("i"), ord("I")):
            self.prompt_set_source()
        elif key == ord(":"):
            self.input_mode = "command"
            self.input_buffer = ""
        elif key == ord("r"):
            self.reload_layout()
        elif key == ord("R"):
            self.reload_parsers()
        elif key in (ord("o"), ord("O")):
            if self.is_banc_valid():
                self.prompt_load_banc_layout()
            else:
                self.log_message("WARN", "ApexBOSBanc Cloud is not configured or disabled.")
        elif key == ord("b"):
            self.force_broadcast()
        elif key == ord("B"):
            if self.is_banc_valid():
                self.list_banc_layouts()
                self.prompt_load_banc_layout()
            else:
                self.log_message("WARN", "ApexBOSBanc Cloud is not configured or disabled.")
        elif key in (ord("c"), ord("C")):
            self.messages.clear()
            self.log_message("INFO", "Cleared status message log")
        elif key in (ord("p"), ord("P")):
            self.scroll_decoded = not self.scroll_decoded
            st = "ENABLED" if self.scroll_decoded else "DISABLED"
            self.log_message("INFO", f"Decoded packet scroll {st}")
        elif key in (ord("l"), ord("L")):
            try:
                toggled = self.engine.toggle_raw_packet_logging()
                st = "ENABLED" if toggled else "DISABLED"
                self.log_message("INFO", f"Raw packet logging {st} -> '{self.engine.raw_log_filepath}' (logged {self.engine.raw_log_count} pkts)")
            except Exception as exc:
                self.log_message("ERROR", f"Failed toggling raw packet logging: {exc}")
        elif key in (ord("s"), ord("S")):
            try:
                saved_path = self.engine.save_packet_history()
                self.log_message("INFO", f"Saved {len(self.engine.packet_cache)} cached packets to '{saved_path}'")
            except Exception as exc:
                self.log_message("ERROR", f"Failed saving packet history: {exc}")
        return True


# ---------------------------------------------------------------------------
# Async Packet Listeners (MosFET Multicast & ApexBOSBanc Cloud)
# ---------------------------------------------------------------------------

async def mcast_listener(monitor: DispatcherHostMonitor, config: dict):
    if not monitor.is_multicast_valid():
        monitor.log_message("WARN", "Multicast input listener disabled in configuration")
        return

    connections = monitor._get_connections()
    group = connections.get("mcast_group", "239.1.1.1")
    port = connections.get("mcast_port", 23003)

    try:
        group = mosfet_client.enforce_multicast_group(str(group).strip())
    except ValueError as exc:
        monitor.log_message("ERROR", f"Multicast group enforcement error on Windows: {exc}")
        return

    if getattr(monitor, "packet_source", "mcast") == "banc":
        monitor.log_message("INFO", f"Multicast standby on UDP {group}:{port} (Banc configured as active packet source)")
    else:
        monitor.log_message("INFO", f"Listening for raw ApexBOSFlux packets on UDP Multicast {group}:{port}")

    client = mosfet_client.MulticastClient(group=group, port=int(port))
    try:
        async for raw_data, addr in client.listen():
            if getattr(monitor, "packet_source", "mcast") not in ("mcast", "both"):
                continue
            meta = None
            if isinstance(raw_data, (bytes, bytearray)):
                m_ch = re.match(rb"^CH(\d{3})(?::RSSI=([+-]?\d+(?:\.\d+)?))?:", raw_data)
                if m_ch:
                    meta = {"channel": int(m_ch.group(1))}
                    if m_ch.group(2):
                        try:
                            meta["rssi"] = float(m_ch.group(2))
                        except ValueError:
                            pass
                    raw_data = bytes(raw_data[m_ch.end():])
                elif len(raw_data) > 6 and raw_data[:2] == b"CH" and raw_data[5:6] == b":" and raw_data[2:5].isdigit():
                    meta = {"channel": int(raw_data[2:5])}
                    raw_data = bytes(raw_data[6:])
            monitor.ingest_packet(raw_data, source="MCAST", meta=meta)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        monitor.log_message("ERROR", f"Multicast listener stopped with error: {exc}")


# ---------------------------------------------------------------------------
# ApexBOSBanc Cloud Real-Time Packet Pacer
# ---------------------------------------------------------------------------

class BancPacketPacer:
    """
    Spaces out packets received in batches from ApexBOSBanc Cloud so they appear
    as if they are being received from the air in real-time.
    Uses packet timestamps (stamp_received or unix_ts) when available.
    Bursts everything received on startup immediately, then begins real-time air emulation.
    """

    def __init__(
        self,
        enabled: bool = True,
        max_delay: float = 2.5,
        nominal_delay: float = 0.05,
        burst_spacing: float = 0.015,
        backlog_threshold: int = 15,
        startup_burst: bool = True,
    ):
        self.enabled = enabled
        self.max_delay = max_delay
        self.nominal_delay = nominal_delay
        self.burst_spacing = burst_spacing
        self.backlog_threshold = backlog_threshold
        self.startup_burst = startup_burst
        self.startup_burst_active = False
        self.startup_burst_done = False
        self.catchup_mode = False

    def calculate_delay(
        self,
        curr_timestamp: float,
        prev_timestamp: Optional[float],
        elapsed_real: float,
        queue_backlog: int = 0,
        is_startup: bool = False,
    ) -> float:
        """
        Calculates the sleep delay in seconds before ingesting curr_timestamp.
        - curr_timestamp: Packet timestamp (seconds) or 0.0 if unavailable
        - prev_timestamp: Previous packet timestamp (seconds) or None if first in burst
        - elapsed_real: Wall-clock seconds elapsed since previous packet was ingested
        - queue_backlog: Number of queued packets waiting behind this one
        - is_startup: True if this packet is part of the initial startup burst batch
        """
        if not self.enabled:
            return 0.0

        # Burst everything received on startup immediately without delay
        if is_startup and self.startup_burst:
            self.startup_burst_active = True
            return 0.0

        # First packet after idle/drain: ingest immediately
        if prev_timestamp is None:
            return 0.0

        # Activate catchup mode if backlog exceeds threshold; clear once fully drained
        if queue_backlog >= self.backlog_threshold:
            self.catchup_mode = True
        elif queue_backlog == 0 and self.catchup_mode:
            # Last packet of the catchup batch
            was_catchup = True
            self.catchup_mode = False
            return max(0.0, 0.01 - elapsed_real)

        # Timestamps missing or invalid: use nominal spacing
        if curr_timestamp <= 0.0 or prev_timestamp <= 0.0:
            if self.catchup_mode:
                target_dt = 0.01
            elif queue_backlog > 5:
                target_dt = 0.025
            else:
                target_dt = self.nominal_delay
            return max(0.0, target_dt - elapsed_real)

        # Timestamps available: calculate air delta
        delta = curr_timestamp - prev_timestamp

        # Packets in same burst / simultaneous reception / backwards timestamp
        if delta <= 0.0:
            if self.catchup_mode:
                target_dt = 0.005
            else:
                target_dt = self.burst_spacing
            return max(0.0, target_dt - elapsed_real)

        # Catchup mode: accelerate playback so historical batch catches up smoothly
        if self.catchup_mode:
            target_dt = min(max(delta * 0.02, 0.005), 0.025)
        elif queue_backlog > 5:
            # Moderate backlog: scale down by 10x
            target_dt = min(max(delta * 0.1, 0.01), 0.08)
        else:
            # Live real-time mode: air spacing clamped to max_delay
            target_dt = min(max(delta, self.burst_spacing), self.max_delay)

        delay = target_dt - elapsed_real
        return max(0.0, delay)


ApexBOSBancPacketPacer = BancPacketPacer


async def banc_pacer_worker(
    pacing_queue: asyncio.Queue,
    monitor: DispatcherHostMonitor,
    pacer: Optional[BancPacketPacer] = None,
):
    """
    Paces packets from ApexBOSBanc Cloud so they are ingested in real-time air spacing.
    Bursts everything received on startup immediately, then starts emulating real time.
    """
    if pacer is None:
        pacer = BancPacketPacer()

    prev_timestamp: Optional[float] = None
    last_ingest_time: Optional[float] = None

    try:
        while True:
            item = await pacing_queue.get()
            if item is None:
                pacing_queue.task_done()
                break

            if len(item) == 4:
                raw_data, meta, row_t, is_startup = item
            else:
                raw_data, meta, row_t = item
                is_startup = bool((meta or {}).get("startup_burst", False))

            now = time.monotonic()

            if is_startup and pacer.startup_burst:
                delay = 0.0
                pacer.startup_burst_active = True
                # Yield control briefly during large startup bursts so event loop stays responsive
                if pacing_queue.qsize() % 25 == 0:
                    await asyncio.sleep(0)
            else:
                if pacer.startup_burst_active:
                    pacer.startup_burst_active = False
                    pacer.startup_burst_done = True
                    elapsed_real = 0.0
                    prev_timestamp = None
                elif last_ingest_time is None or (now - last_ingest_time) > 4.0:
                    # First packet ever, or queue was idle (new batch after quiet period)
                    elapsed_real = 0.0
                    prev_timestamp = None
                else:
                    elapsed_real = now - last_ingest_time

                backlog = pacing_queue.qsize()
                delay = pacer.calculate_delay(
                    curr_timestamp=row_t,
                    prev_timestamp=prev_timestamp,
                    elapsed_real=elapsed_real,
                    queue_backlog=backlog,
                    is_startup=False,
                )

            if delay > 0.002:
                await asyncio.sleep(delay)

            try:
                monitor.ingest_packet(raw_data, source="BANC", meta=meta)
            except Exception as exc:
                monitor.log_message("ERROR", f"Error ingesting paced Banc packet: {exc}")

            if row_t > 0.0:
                prev_timestamp = row_t
            last_ingest_time = time.monotonic()
            pacing_queue.task_done()
    except asyncio.CancelledError:
        pass


async def banc_listener(monitor: DispatcherHostMonitor, config: dict):
    if not monitor.is_banc_valid():
        monitor.identity = "OFFLINE"
        monitor.log_message("WARN", "Banc cloud listener disabled or missing credentials in configuration")
        return

    connections = monitor._get_connections()
    host = connections.get("banc_host")
    port = connections.get("banc_port")
    auth = connections.get("auth_secret")
    enc = connections.get("encryption_key")

    cfg_identity = connections.get("identity") or config.get("identity")
    if cfg_identity and str(cfg_identity).lower() != "auto":
        ident = str(cfg_identity)
        monitor.identity = ident
        monitor.log_message("INFO", f"Using configured Banc identity '{ident}' for {host}:{port}")
    else:
        monitor.identity = "GENERATING ID..."
        monitor.log_message("INFO", "Generating Banc network identity (LAN default gateway MAC)...")
        try:
            ident, id_src = await mosfet_client.resolve_network_identity_details(host)
            monitor.identity = ident
            monitor.log_message("INFO", f"Generated Banc identity '{ident}' (via {id_src})")
        except Exception as exc:
            ident = "FALLBACK"
            monitor.identity = ident
            monitor.log_message("WARN", f"Network identity generation failed ({exc}); using FALLBACK")

    # If both multicast and banc are configured, default to receiving packets from multicast only,
    # unless Banc (or Both) is selected as packet source.
    if getattr(monitor, "packet_source", "mcast") not in ("banc", "both"):
        monitor.log_message(
            "INFO",
            f"Both Multicast and Banc configured: defaulting to Multicast for packets. Banc ({host}:{port}) active for layouts.",
        )
        return

    try:
        client = mosfet_client.BancClient(host, int(port), auth, enc, monitor.identity or ident)
    except Exception as exc:
        monitor.identity = "KEY_ERROR"
        monitor.log_message("ERROR", f"Failed initializing BancClient (check encryption_key): {exc}")
        return

    pacing_queue: asyncio.Queue = asyncio.Queue()
    monitor.banc_pacing_queue = pacing_queue
    pacer = BancPacketPacer(
        enabled=connections.get("banc_pacing", True),
        max_delay=float(connections.get("banc_pacing_max_delay", 2.5)),
        nominal_delay=float(connections.get("banc_pacing_nominal_delay", 0.05)),
        burst_spacing=float(connections.get("banc_pacing_burst_spacing", 0.015)),
        backlog_threshold=int(connections.get("banc_pacing_catchup_threshold", 20)),
        startup_burst=connections.get("banc_startup_burst", True),
    )
    pacer_task = asyncio.create_task(banc_pacer_worker(pacing_queue, monitor, pacer))
    monitor.banc_pacer_task = pacer_task

    init_hist_sec = float(
        connections.get("banc_initial_history_sec")
        or connections.get("banc_initial_download_sec")
        or config.get("banc_initial_history_sec")
        or 60.0
    )

    last_seen_time = getattr(monitor, "last_seen_banc_time", 0.0)
    now = time.time()
    startup_cutoff = (now - init_hist_sec) if init_hist_sec > 0.0 else 0.0
    if last_seen_time <= 0.0 or last_seen_time < startup_cutoff:
        last_seen_time = startup_cutoff
        monitor.last_seen_banc_time = last_seen_time

    seen_window_keys: set = set()
    window_records: deque = deque()
    last_err_log = 0.0
    is_startup = True

    try:
        while getattr(monitor, "packet_source", "banc") in ("banc", "both"):
            try:
                if getattr(monitor, "_banc_reset_needed", False):
                    monitor._banc_reset_needed = False
                    is_startup = True
                    now = time.time()
                    startup_cutoff = (now - init_hist_sec) if init_hist_sec > 0.0 else 0.0
                    last_seen_time = max(last_seen_time, startup_cutoff)
                    monitor.last_seen_banc_time = last_seen_time

                query_since = last_seen_time if last_seen_time > 0.0 else None
                rows = await client.get_packets(timeout=5.0, since_ts=query_since)
                if rows:
                    def get_row_time(r):
                        try:
                            val = r.get("stamp_received") or r.get("unix_ts") or r.get("timestamp")
                            return float(val) if val is not None else 0.0
                        except (ValueError, TypeError):
                            return 0.0

                    # Process rows chronologically (oldest to newest)
                    sorted_rows = sorted(rows, key=get_row_time)
                    new_rows = []

                    for row in sorted_rows:
                        row_t = get_row_time(row)
                        pkt_key = (
                            row.get("pkt_hash"),
                            str(row.get("hex_data", ""))[:32],
                            row.get("channel", 0),
                        )

                        # 1. Discard packets strictly older than the last seen timestamp
                        if last_seen_time > 0.0 and row_t < (last_seen_time - 0.001):
                            continue

                        # 2. In boundary window, check if already seen
                        if (row_t, pkt_key) in seen_window_keys:
                            continue

                        seen_window_keys.add((row_t, pkt_key))
                        window_records.append((row_t, pkt_key))
                        if row_t > last_seen_time:
                            last_seen_time = row_t
                            monitor.last_seen_banc_time = last_seen_time

                        new_rows.append(row)

                    # Prune seen keys older than last_seen_time - 15.0s
                    cutoff_t = last_seen_time - 15.0
                    while window_records and window_records[0][0] < cutoff_t:
                        old_t, old_k = window_records.popleft()
                        seen_window_keys.discard((old_t, old_k))

                    for row in new_rows:
                        try:
                            raw_data = bytes.fromhex(row["hex_data"])
                        except Exception:
                            monitor.stats.add_banc(1)
                            monitor.stats.add_unknown(1)
                            monitor.log_message("WARN", "Malformed hex_data row from Banc cloud")
                            continue

                        try:
                            rssi_val = float(row.get("signal_strength", -75.0))
                        except (ValueError, TypeError):
                            rssi_val = -75.0
                        try:
                            ch_val = int(row.get("channel", 0))
                        except (ValueError, TypeError):
                            ch_val = 0

                        row_t = get_row_time(row)
                        meta = {
                            "rssi": rssi_val,
                            "channel": ch_val,
                            "timestamp": row_t,
                            "unix_ts": float(row.get("unix_ts") or row_t),
                            "stamp_received": float(row.get("stamp_received") or row_t),
                            "source": "BANC",
                            "startup_burst": is_startup,
                        }
                        await pacing_queue.put((raw_data, meta, row_t, is_startup))

                    if is_startup:
                        if new_rows:
                            monitor.log_message(
                                "INFO",
                                f"Bursted {len(new_rows)} historical Banc packet(s) (window: last {int(init_hist_sec)}s) on startup; emulating real-time for live packets",
                            )
                        is_startup = False
                else:
                    if is_startup:
                        is_startup = False

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = time.time()
                if now - last_err_log >= 15.0:
                    monitor.log_message("WARN", f"Banc poll warning ({host}:{port}): {exc}")
                    last_err_log = now
            await asyncio.sleep(3.0)
    except asyncio.CancelledError:
        pass
    finally:
        pacer_task.cancel()
        try:
            await pacer_task
        except (asyncio.CancelledError, Exception):
            pass
        monitor.banc_pacer_task = None
        monitor.banc_pacing_queue = None


async def banc_wiu_sync_loop(monitor: DispatcherHostMonitor, config: dict):
    """Periodically supplies discovered and layout WIU metadata to ApexBOSBanc when configured."""
    if not monitor.is_banc_valid():
        return

    connections = monitor._get_connections()
    cfg_ident = connections.get("identity") or config.get("identity")
    if not cfg_ident or str(cfg_ident).lower() == "auto":
        # Wait briefly for banc_listener to resolve dynamic network identity
        for _ in range(25):
            if monitor.identity not in ("INITIALIZING...", "GENERATING ID..."):
                break
            await asyncio.sleep(0.2)
        if monitor.identity in ("INITIALIZING...", "GENERATING ID..."):
            try:
                host = connections.get("banc_host")
                if host:
                    monitor.identity = await mosfet_client.generate_identity(host)
            except Exception:
                monitor.identity = "FALLBACK"

    first_flush = True
    try:
        while True:
            if first_flush or monitor.engine.has_dirty_wius():
                res = await monitor.sync_wius_to_banc(flush_all=first_flush)
                if res >= 0:
                    first_flush = False
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(10.0)
            else:
                await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass


async def banc_chat_loop(monitor: DispatcherHostMonitor, config: dict):
    """Periodically polls ApexBOSBanc for encrypted chat messages and broadcasts updates via multicast."""
    if not monitor.is_banc_valid():
        return

    connections = monitor._get_connections()
    cfg_ident = connections.get("identity") or config.get("identity")
    if not cfg_ident or str(cfg_ident).lower() == "auto":
        for _ in range(25):
            if monitor.identity not in ("INITIALIZING...", "GENERATING ID..."):
                break
            await asyncio.sleep(0.2)

    if monitor.identity and monitor.identity not in ("INITIALIZING...", "GENERATING ID...", "OFFLINE", "KEY_ERROR"):
        if not monitor.engine.chat_self_identity:
            monitor.engine.chat_self_identity = monitor.identity

    since_id = 0
    since_ts = 0.0
    try:
        while True:
            client = monitor._get_banc_client()
            if client is not None:
                try:
                    msgs = await client.get_chat_messages(since_id=since_id, since_ts=since_ts, timeout=5.0)
                    if msgs:
                        for m in msgs:
                            if isinstance(m, dict):
                                mid = int(m.get("msg_id") or 0)
                                mts = float(m.get("timestamp") or 0.0)
                                if mid > since_id:
                                    since_id = mid
                                if mts > since_ts:
                                    since_ts = mts
                        monitor.engine.ingest_chat_messages(msgs, broadcast=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Curses stdout / stderr redirector (prevents rogue prints from scrolling TUI)
# ---------------------------------------------------------------------------

class CursesStdoutRedirector:
    """Redirects stdout/stderr during curses mode to prevent rogue prints from scrolling the screen."""
    def __init__(self, log_fn):
        self.log_fn = log_fn
        self._buf = ""

    def write(self, s):
        if not s:
            return
        self._buf += str(s)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line and self.log_fn:
                try:
                    self.log_fn("INFO", line)
                except Exception:
                    pass

    def flush(self):
        if self._buf.strip() and self.log_fn:
            try:
                self.log_fn("INFO", self._buf.strip())
            except Exception:
                pass
            self._buf = ""


# ---------------------------------------------------------------------------
# Main Loops (Curses TUI & Headless Console Fallback)
# ---------------------------------------------------------------------------

async def main_loop(
    stdscr,
    config: dict,
    layout_file: str,
    engine_mcast_group: str,
    engine_mcast_port: int,
    layout_port: int,
    banc_layout: Optional[str] = None,
    dedup_ttl: Optional[float] = None,
    unicast_clients: Optional[List[str]] = None,
    scroll_decoded: bool = False,
    force_banc_source: bool = False,
    initial_source: Optional[str] = None,
):
    old_stdout = sys.stdout
    old_stderr = sys.stderr

    if stdscr is not None and curses is not None:
        stdscr.nodelay(True)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)
        try:
            stdscr.scrollok(False)
            stdscr.idlok(False)
            stdscr.leaveok(False)
        except Exception:
            pass

    monitor = DispatcherHostMonitor(
        config=config,
        layout_file=layout_file,
        engine_mcast_group=engine_mcast_group,
        engine_mcast_port=engine_mcast_port,
        layout_port=layout_port,
        stdscr=stdscr,
        banc_layout=banc_layout,
        dedup_ttl=dedup_ttl,
        unicast_clients=unicast_clients,
        scroll_decoded=scroll_decoded,
        force_banc_source=force_banc_source,
        initial_source=initial_source,
    )

    if stdscr is not None:
        redirector = CursesStdoutRedirector(monitor.log_message)
        sys.stdout = redirector
        sys.stderr = redirector

    mcast_task = asyncio.create_task(mcast_listener(monitor, config))
    banc_task = asyncio.create_task(banc_listener(monitor, config))
    monitor._mcast_task = mcast_task
    monitor._banc_task = banc_task
    wiu_sync_task = asyncio.create_task(banc_wiu_sync_loop(monitor, config))
    chat_task = asyncio.create_task(banc_chat_loop(monitor, config))

    last_console_stats = time.time()
    running = True
    try:
        while running:
            running = await monitor.handle_input()
            if stdscr is not None:
                monitor.draw()
            else:
                now = time.time()
                if now - last_console_stats >= 10.0:
                    s = monitor.stats.get_stats()
                    sc = monitor.engine.scroller.to_state_dict()
                    print(
                        f"[STATS] Elapsed: {s['elapsed_str']} | RX Tot: {s['total']} ({s['total_ppm']}/m) "
                        f"[Mcast: {s['mcast_total']} | Banc: {s['banc_total']}] | Decoded: {s['decoded_total']} | "
                        f"Unknown: {s['unknown_total']} | Engine TX: {monitor.engine.transmitted_frames_count} | "
                        f"Dedup TTL: {monitor.engine.dedup_ttl:.2f}s | WIUs: {len(monitor.engine.address_table.wius)} | "
                        f"Pkts/m: {sc.get('total_ppm', 0)}",
                        flush=True,
                    )
                    last_console_stats = now
            await asyncio.sleep(0.05)
    finally:
        if stdscr is not None:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        tasks_to_cancel = [mcast_task, banc_task, wiu_sync_task, chat_task]
        if monitor._banc_task and monitor._banc_task not in tasks_to_cancel:
            tasks_to_cancel.append(monitor._banc_task)
        if monitor._mcast_task and monitor._mcast_task not in tasks_to_cancel:
            tasks_to_cancel.append(monitor._mcast_task)
        for t in tasks_to_cancel:
            if t and not t.done():
                t.cancel()
        await asyncio.gather(*[t for t in tasks_to_cancel if t], return_exceptions=True)


def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        alt = os.path.join("config", config_path)
        if os.path.exists(alt):
            config_path = alt
        else:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
            return dict(DEFAULT_CONFIG)

    with open(config_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    # Normalize engine section, parsers, and connections
    eng_sub = loaded.get("engine", {}) if isinstance(loaded.get("engine"), dict) else {}

    # Load 'parsers' from 'engine' section, or top-level, or fallback
    if "parsers" in eng_sub:
        loaded["parsers"] = eng_sub["parsers"]
    elif "parsers" in loaded:
        eng_sub["parsers"] = loaded["parsers"]
    else:
        loaded["parsers"] = list(DEFAULT_CONFIG["parsers"])
        eng_sub["parsers"] = list(DEFAULT_CONFIG["parsers"])

    # Load 'connections' from 'engine' section, or top-level, or flat config, or fallback
    if "connections" in eng_sub and isinstance(eng_sub["connections"], dict):
        conns = dict(eng_sub["connections"])
    elif "connections" in loaded and isinstance(loaded["connections"], dict):
        conns = dict(loaded["connections"])
    else:
        conns = {
            "mcast_group": loaded.get("mcast_group", loaded.get("multicast_group", DEFAULT_CONFIG["connections"]["mcast_group"])),
            "mcast_port": loaded.get("mcast_port", loaded.get("multicast_port", DEFAULT_CONFIG["connections"]["mcast_port"])),
            "banc_host": loaded.get("banc_host", loaded.get("cloud_server", DEFAULT_CONFIG["connections"]["banc_host"])),
            "banc_port": loaded.get("banc_port", loaded.get("cloud_port", DEFAULT_CONFIG["connections"]["banc_port"])),
            "auth_secret": loaded.get("auth_secret", DEFAULT_CONFIG["connections"]["auth_secret"]),
            "encryption_key": loaded.get("encryption_key", DEFAULT_CONFIG["connections"]["encryption_key"]),
            "identity": loaded.get("identity"),
        }

    cb = (loaded.get("common", {}) if isinstance(loaded.get("common"), dict) else {}).get("banc", {})
    if isinstance(cb, dict):
        if not conns.get("auth_secret") and cb.get("auth_secret"):
            conns["auth_secret"] = cb["auth_secret"]
        if not conns.get("encryption_key") and cb.get("encryption_key"):
            conns["encryption_key"] = cb["encryption_key"]
        if not conns.get("identity") and cb.get("identity"):
            conns["identity"] = cb["identity"]
        if (not conns.get("banc_host") or str(conns.get("banc_host")).lower() == "none") and cb.get("host") and str(cb.get("host")).lower() != "none":
            conns["banc_host"] = cb["host"]
        if not conns.get("banc_port") and cb.get("port"):
            conns["banc_port"] = cb["port"]
    top_banc = loaded.get("banc", {})
    if isinstance(top_banc, dict):
        if not conns.get("auth_secret") and top_banc.get("auth_secret"):
            conns["auth_secret"] = top_banc["auth_secret"]
        if not conns.get("encryption_key") and top_banc.get("encryption_key"):
            conns["encryption_key"] = top_banc["encryption_key"]

    if "banc_initial_history_sec" not in conns:
        if isinstance(cb, dict) and cb.get("initial_history_sec"):
            conns["banc_initial_history_sec"] = cb["initial_history_sec"]
        elif isinstance(cb, dict) and cb.get("banc_initial_history_sec"):
            conns["banc_initial_history_sec"] = cb["banc_initial_history_sec"]
        elif isinstance(top_banc, dict) and top_banc.get("initial_history_sec"):
            conns["banc_initial_history_sec"] = top_banc["initial_history_sec"]
        elif isinstance(top_banc, dict) and top_banc.get("banc_initial_history_sec"):
            conns["banc_initial_history_sec"] = top_banc["banc_initial_history_sec"]
        elif "banc_initial_history_sec" in loaded:
            conns["banc_initial_history_sec"] = loaded["banc_initial_history_sec"]
        else:
            conns["banc_initial_history_sec"] = 60

    loaded["connections"] = conns
    eng_sub["connections"] = conns

    if "engine" not in loaded or not isinstance(loaded["engine"], dict):
        loaded["engine"] = eng_sub

    return loaded


def main():
    parser = argparse.ArgumentParser(
        description=f"ApexBOS.220 ({VERSION}) - PTC Dispatcher Engine & Telemetry Ingestion Host",
        epilog=FIREWALL_HELP_NOTICE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.json", help="Path to the configuration file (JSON).")
    parser.add_argument("--layout", default=None, help="Path to the layout file (JSON) for the engine to load and serve.")
    parser.add_argument("--banc-layout", default=None, help="Name of layout to download and load directly from ApexBOSBanc Cloud.")
    parser.add_argument("--list-banc-layouts", action="store_true", help="List available layouts on ApexBOSBanc Cloud and exit.")
    parser.add_argument("--engine-mcast-group", default=None, help="Outbound multicast group for View_ApexBOS.220.py (default: 224.1.1.1)")
    parser.add_argument("--engine-mcast-port", type=int, default=None, help="Outbound multicast UDP port for View_ApexBOS.220.py (default: 5007)")
    parser.add_argument("--layout-port", type=int, default=None, help="TCP port to serve layout JSON to remote View_ApexBOS.220.py clients (default: 5008)")
    parser.add_argument("--no-curses", action="store_true", help="Run in plain console output mode instead of curses TUI")
    parser.add_argument("--no-pacing", action="store_true", help="Disable real-time pacing of Banc cloud packets")
    parser.add_argument("--no-startup-burst", action="store_true", help="Disable bursting startup packets; pace everything from the start")
    parser.add_argument(
        "--scroll-decoded",
        "--show-decoded",
        dest="scroll_decoded",
        action="store_true",
        default=False,
        help="Enable real-time scrolling log of decoded packets in the Engine (default: False; errors and unknown packets only)",
    )
    parser.add_argument(
        "--dedup-ttl",
        "--dedup-window",
        "--dedup-sec",
        dest="dedup_ttl",
        type=float,
        default=None,
        help="Duplicate packet deduplication TTL in seconds (default: 0.4s). Set to 0 to disable.",
    )
    parser.add_argument(
        "--client",
        "--unicast-client",
        dest="unicast_clients",
        action="append",
        default=[],
        help="Explicit remote Viewer client IP for unicast state updates (can be used multiple times)",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "mcast", "banc", "both"],
        default="auto",
        help="Explicit packet ingestion source: 'auto' (multicast if configured, else banc), 'mcast' (force multicast), 'banc' (force Banc Cloud), or 'both' (both multicast and Banc Cloud)",
    )
    parser.add_argument(
        "--banc-source",
        action="store_true",
        default=False,
        help="Force packet ingestion from ApexBOSBanc Cloud even if multicast is configured",
    )
    parser.add_argument(
        "--mcast-source",
        action="store_true",
        default=False,
        help="Force packet ingestion from Multicast UDP",
    )
    parser.add_argument(
        "--both-source",
        action="store_true",
        default=False,
        help="Force packet ingestion from both Multicast UDP and ApexBOSBanc Cloud",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    config["config_path"] = args.config
    cli_source = None
    if args.both_source or args.source == "both":
        cli_source = "both"
    elif args.banc_source or args.source == "banc":
        cli_source = "banc"
    elif args.mcast_source or args.source == "mcast":
        cli_source = "mcast"
    force_banc = bool(cli_source == "banc")

    if args.list_banc_layouts:
        client = mosfet_client.create_banc_client_from_config(args.config)
        try:
            layouts = client.list_layouts_sync()
            print(f"\nAvailable layouts on ApexBOSBanc Cloud ({client.host}:{client.port}):")
            print(f"{'Layout Name':<25} {'Status':<12} {'Center GPS':<24} {'Radius (km)':<12}")
            print("-" * 75)
            for item in layouts:
                name = item.get("layout_name", "unknown")
                status = "AVAILABLE" if item.get("is_available") else "UNAVAILABLE"
                lat = item.get("center_lat")
                lon = item.get("center_lon")
                rad = item.get("radius_km")
                gps_str = f"{lat:.4f}, {lon:.4f}" if lat is not None and lon is not None else "-"
                rad_str = f"{rad:.2f}" if rad is not None else "-"
                print(f"{name:<25} {status:<12} {gps_str:<24} {rad_str:<12}")
            print()
            return
        except Exception as exc:
            print(f"[ERROR] Failed to list Banc layouts: {exc}")
            return

    if args.no_pacing:
        config.setdefault("connections", {})["banc_pacing"] = False
    if args.no_startup_burst:
        config.setdefault("connections", {})["banc_startup_burst"] = False
    eng_cfg = config.get("engine", {})
    layout_file = args.layout
    if not layout_file:
        layout_file = eng_cfg.get("default_layout") or eng_cfg.get("layout") or "layouts/arrow_layout.json"
    resolved_layout = DispatcherHostMonitor._resolve_layout_path(layout_file, config_path=args.config)

    cli_dedup = args.dedup_ttl if args.dedup_ttl is not None else eng_cfg.get("dedup_ttl", 0.4)
    try:
        dedup_ttl = max(0.0, float(cli_dedup))
    except (ValueError, TypeError):
        dedup_ttl = 0.4
    config.setdefault("engine", {})["dedup_ttl"] = dedup_ttl

    engine_mcast_group = args.engine_mcast_group or eng_cfg.get("mcast_group", "224.1.1.1")
    engine_mcast_port = int(args.engine_mcast_port or eng_cfg.get("mcast_port", 5007))
    layout_port = int(args.layout_port or eng_cfg.get("layout_port", 5008))

    if args.no_curses or curses is None or not sys.stdout.isatty():
        try:
            asyncio.run(
                main_loop(
                    None,
                    config,
                    resolved_layout,
                    engine_mcast_group,
                    engine_mcast_port,
                    layout_port,
                    banc_layout=args.banc_layout,
                    dedup_ttl=dedup_ttl,
                    unicast_clients=args.unicast_clients,
                    scroll_decoded=args.scroll_decoded,
                    force_banc_source=force_banc,
                    initial_source=cli_source,
                )
            )
        except KeyboardInterrupt:
            print("\nApexBOS.220 stopped by user.")
    else:
        try:
            curses.wrapper(
                lambda stdscr: asyncio.run(
                    main_loop(
                        stdscr,
                        config,
                        resolved_layout,
                        engine_mcast_group,
                        engine_mcast_port,
                        layout_port,
                        banc_layout=args.banc_layout,
                        dedup_ttl=dedup_ttl,
                        unicast_clients=args.unicast_clients,
                        scroll_decoded=args.scroll_decoded,
                        force_banc_source=force_banc,
                        initial_source=cli_source,
                    )
                )
            )
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
