#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApexBOSSpy.py - High-Performance Real-Time Packet Spy & Rule Inspector for ApexBOSGrabber

Connects directly to ApexBOSGrabber ZeroMQ PUB and/or UDP sockets, decodes raw packet data
through modular packet rule parsers, and presents a dual-panel, split-scrolling ANSI/ASCII
Terminal User Interface (TUI):
  - Upper Panel: Known Packets (passed one of the modular packet rules)
  - Lower Panel: Unknown Packets (unparsed / raw packets, shorter section)
  - Raw Hex Display Toggle: Shows raw hex string on next line slightly offset
  - Unknown ASCII Text: Extracts and displays printable ASCII on next line, replacing non-printables with '.'
  - Remote Host Procs: Supports connecting to Grabber procs on remote hosts via --host or host:ports
  - On-The-Fly Parser Reload: Reload rules via 'r' key or auto-detection when rule files are modified
  - Pinned Header: System stats, Pack/Min rates, and command reference (never scrolls off)
  - Independent RegEx display filter, Pause-on-match stop RegEx, and DVR ring buffer for each panel
  - Full cross-platform support for Windows 11, Linux, and macOS.

Released under the MIT License.
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

import argparse
import collections
from datetime import datetime
import glob
import json
import os
import re
import select
import shutil
import signal
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# Compatibility for Python < 3.7 where re.Pattern does not exist (zero overhead on 3.7+)
_RePattern = getattr(re, "Pattern", None)
if _RePattern is None:
    import typing as _typing
    _RePattern = getattr(_typing, "Pattern", Any)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from version import VERSION, __version__
except ImportError:
    VERSION = "v26.1113.01"
    __version__ = VERSION

try:
    from parse_packets import PacketParser
except ImportError:
    print("[!] Error: 'parse_packets.py' not found in workspace.", file=sys.stderr)
    sys.exit(1)

try:
    import zmq
except ImportError:
    print("\n" + "=" * 76)
    print(" [!] ApexBOSSpy: Missing Required Package 'pyzmq'")
    print("=" * 76)
    print("Please install pyzmq via:")
    print("    pip install pyzmq\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Cross-Platform Terminal Support (Windows 11, Linux, macOS)
# ---------------------------------------------------------------------------
def init_cross_platform_terminal(no_color_flag: bool = False) -> bool:
    """
    Initializes terminal capabilities for Windows 10/11, Linux, and macOS.
    Forces UTF-8 stdout encoding and enables ANSI Virtual Terminal Processing on Windows.
    Returns True if ANSI escape sequences (colors, cursor positioning) are supported.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if no_color_flag or os.environ.get("NO_COLOR"):
        return False

    if os.name == "nt":
        vt_ok = False
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            # 1. STD_OUTPUT_HANDLE (-11)
            h_out = kernel32.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                # DISABLE_NEWLINE_AUTO_RETURN = 0x0008
                if kernel32.SetConsoleMode(h_out, mode.value | 0x0004 | 0x0008):
                    vt_ok = True

            # 2. Also try CONOUT$ handle directly
            h_con = kernel32.CreateFileW("CONOUT$", 0x40000000 | 0x80000000, 2, None, 3, 0, None)
            if h_con and h_con != -1:
                mode_c = wintypes.DWORD()
                if kernel32.GetConsoleMode(h_con, ctypes.byref(mode_c)):
                    if kernel32.SetConsoleMode(h_con, mode_c.value | 0x0004):
                        vt_ok = True
                kernel32.CloseHandle(h_con)
        except Exception:
            pass

        try:
            os.system("")
        except Exception:
            pass

        if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
            return True
        return vt_ok
    return True


# ---------------------------------------------------------------------------
# ANSI Color & Styling Codes
# ---------------------------------------------------------------------------
class Colors:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._update()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self._update()

    def _update(self):
        if self.enabled:
            self.RESET = "\033[0m"
            self.BOLD = "\033[1m"
            self.DIM = "\033[90m"
            self.RED = "\033[91m"
            self.GREEN = "\033[92m"
            self.YELLOW = "\033[93m"
            self.BLUE = "\033[94m"
            self.MAGENTA = "\033[95m"
            self.CYAN = "\033[96m"
            self.WHITE = "\033[97m"
            self.BG_CYAN = "\033[46m\033[30m"
            self.BG_BLUE = "\033[44m\033[97m"
            self.BG_YELLOW = "\033[43m\033[30m"
            self.BG_RED = "\033[41m\033[97m"
            self.CLEAR_EOL = "\033[K"
        else:
            self.RESET = ""
            self.BOLD = ""
            self.DIM = ""
            self.RED = ""
            self.GREEN = ""
            self.YELLOW = ""
            self.BLUE = ""
            self.MAGENTA = ""
            self.CYAN = ""
            self.WHITE = ""
            self.BG_CYAN = ""
            self.BG_BLUE = ""
            self.BG_YELLOW = ""
            self.BG_RED = ""
            self.CLEAR_EOL = ""


# Global color palette
C = Colors(enabled=True)


# ---------------------------------------------------------------------------
# Grabber Endpoint Discovery & Remote Host Resolution
# ---------------------------------------------------------------------------
def discover_grabber_ports(config_path: Optional[str] = None) -> List[int]:
    """Finds configured procs and zmq_port numbers from grabber_config.json files."""
    ports = []
    if config_path:
        cfg_candidates = [config_path]
    else:
        cfg_candidates = [
            os.path.join(PROJECT_ROOT, "grabber_config.json"),
            os.path.join(PROJECT_ROOT, "deploy", "ApexBOSGrabber", "grabber_config.json"),
            os.path.join(PROJECT_ROOT, "deploy", "grabber_config.json"),
            os.path.join(PROJECT_ROOT, "config", "grabber_config.json"),
        ]

    for cfg_path in cfg_candidates:
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if isinstance(cfg, dict):
                        procs_val = cfg.get("procs")
                        if procs_val:
                            for p in str(procs_val).split(","):
                                p = p.strip()
                                if p.isdigit() and int(p) not in ports:
                                    ports.append(int(p))
                        zmq_port_val = cfg.get("zmq_port")
                        if zmq_port_val:
                            p = str(zmq_port_val).strip()
                            if p.isdigit() and int(p) not in ports:
                                ports.append(int(p))
            except Exception:
                pass

    if not ports:
        ports = [5555, 18002, 20101, 20102, 20113, 20114, 20127, 20131]
    return ports


def discover_grabber_endpoints(host: Optional[str] = None) -> List[str]:
    """Returns default endpoints on the specified host (or 127.0.0.1)."""
    target_host = host or "127.0.0.1"
    if target_host.lower() == "localhost":
        target_host = "127.0.0.1"
    ports = discover_grabber_ports()
    return [f"tcp://{target_host}:{p}" for p in ports]


def parse_connection_endpoints(connect_arg: Optional[str], host_arg: Optional[str] = None) -> List[str]:
    """
    Parses ZeroMQ connection endpoints from --connect and --host arguments.
    Supports:
      - Bare host with port list: '192.168.1.50:20101,20113,20131' -> tcp://192.168.1.50:20101, etc.
      - Scheme with host and port list: 'tcp://192.168.1.50:20101,20113,20131'
      - Host flag + port list: --host 192.168.1.50 --connect 20101,20113,20131
      - Just host: --connect 192.168.1.50 or --host 192.168.1.50 -> connects to procs on that host
      - Standard URI list: 'tcp://host1:5555, tcp://host2:5556'
      - Default / auto-discovery when empty.
    """
    discovered_ports = discover_grabber_ports()
    default_host = (host_arg or "").strip()
    if default_host.lower() == "localhost":
        default_host = "127.0.0.1"

    # Nothing supplied: use auto-discovered endpoints on 127.0.0.1 or default_host
    if not connect_arg and not default_host:
        return [f"tcp://127.0.0.1:{p}" for p in discovered_ports]

    if not connect_arg and default_host:
        return [f"tcp://{default_host}:{p}" for p in discovered_ports]

    current_host = default_host or "127.0.0.1"
    if current_host.lower() == "localhost":
        current_host = "127.0.0.1"
    raw_connect = (connect_arg or "").strip()
    if not raw_connect:
        return [f"tcp://{current_host}:{p}" for p in discovered_ports]

    items = [x.strip() for x in raw_connect.split(",") if x.strip()]
    if not items:
        return [f"tcp://{current_host}:{p}" for p in discovered_ports]

    endpoints: List[str] = []
    for item in items:
        clean = item
        proto = "tcp://"
        if clean.startswith(("tcp://", "ipc://", "udp://")):
            proto = clean.split("://")[0] + "://"
            clean = clean.split("://", 1)[1]

        if ":" in clean:
            h, p = clean.split(":", 1)
            h = h.strip()
            if h.lower() == "localhost":
                h = "127.0.0.1"
            if h:
                current_host = h
            endpoints.append(f"{proto}{current_host}:{p.strip()}")
        elif clean.isdigit():
            endpoints.append(f"{proto}{current_host}:{clean}")
        else:
            # It's a hostname or IP string without port (e.g. "192.168.1.50")
            current_host = clean
            if current_host.lower() == "localhost":
                current_host = "127.0.0.1"
            for p in discovered_ports:
                endpoints.append(f"{proto}{current_host}:{p}")

    seen = set()
    result: List[str] = []
    for ep in endpoints:
        if ep not in seen:
            seen.add(ep)
            result.append(ep)
    return result


# ---------------------------------------------------------------------------
# Message Record Data Structure
# ---------------------------------------------------------------------------
class SpyMessage:
    """Represents a single parsed or raw packet event with formatted presentation."""
    def __init__(
        self,
        raw_bytes: bytes,
        channel: Any,
        rssi: Optional[float],
        timestamp: float,
        parsed_data: Optional[Dict[str, Any]],
        rule_name: str,
        is_known: bool = False,
        raw_hex: str = "",
    ):
        self.raw_bytes = raw_bytes
        self.channel = channel if channel is not None else 0
        self.rssi = rssi
        self.timestamp = timestamp
        self.parsed_data = parsed_data or {}
        self.rule_name = rule_name
        self.is_known = is_known
        self.raw_hex = raw_hex or raw_bytes.hex().upper()

        dt = datetime.fromtimestamp(timestamp)
        self.time_str = dt.strftime("%H:%M:%S") + f".{int(dt.microsecond / 1000):03d}"

        # Extract printable ASCII representation, substituting non-printables with '.'
        self.raw_ascii = "".join(chr(b) if 32 <= b <= 126 else "." for b in self.raw_bytes)

        # Extract _scroll_ptc_ from parsed data or synthesize meaningful description
        scroll = self.parsed_data.get("_scroll_ptc_")
        if not scroll:
            ptype = self.parsed_data.get("Type") or self.parsed_data.get("_type_")
            node = self.parsed_data.get("_node_id_") or self.parsed_data.get("ADDR")
            if ptype is not None and node is not None:
                scroll = f"Packet Type {ptype}: Addr {node}"
            elif ptype is not None:
                scroll = f"Packet Type {ptype}"
            elif self.parsed_data:
                scroll = "Parsed: " + " ".join(f"{k}={v}" for k, v in list(self.parsed_data.items())[:4])
            else:
                # Raw / Unknown packet
                if len(self.raw_bytes) >= 4 and self.raw_bytes[0] == 0x49:
                    raw_type = self.raw_bytes[3]
                    scroll = f"[RAW Type {raw_type:#04x}] {self.raw_hex[:36]}"
                else:
                    scroll = f"[RAW] {self.raw_hex[:40]}"
        self.scroll_ptc = str(scroll).strip()

        # Build clean plain-text line representation
        ch_text = f"Ch:{self.channel:<3}" if self.channel else "Ch:---"
        rssi_text = f"RSSI:{self.rssi:+5.1f} dBFS" if self.rssi is not None else "RSSI:  --- dBFS"
        self.plain_line = f"[{self.time_str}]  {ch_text}  {rssi_text}  |  {self.scroll_ptc}"

    @property
    def hex_line(self) -> str:
        """Offset raw hex string line."""
        return f"    HEX: {self.raw_hex}"

    @property
    def ascii_line(self) -> str:
        """Offset ASCII text string line."""
        return f"    ASCII: {self.raw_ascii}"

    def format_terminal(self, colors: Colors) -> str:
        """Returns the ANSI colored line representation for the TUI."""
        ch_text = f"Ch:{self.channel:<3}" if self.channel else "Ch:---"

        # Colorize RSSI based on dBFS signal thresholds
        if self.rssi is not None:
            if self.rssi >= 0.0:
                rssi_str = f"{colors.RED}{colors.BOLD}RSSI:{self.rssi:+5.1f} dBFS{colors.RESET}"
            elif self.rssi >= -65.0:
                rssi_str = f"{colors.GREEN}{colors.BOLD}RSSI:{self.rssi:+5.1f} dBFS{colors.RESET}"
            elif self.rssi >= -80.0:
                rssi_str = f"{colors.YELLOW}RSSI:{self.rssi:+5.1f} dBFS{colors.RESET}"
            else:
                rssi_str = f"{colors.MAGENTA}RSSI:{self.rssi:+5.1f} dBFS{colors.RESET}"
        else:
            rssi_str = f"{colors.DIM}RSSI:  --- dBFS{colors.RESET}"

        time_part = f"{colors.DIM}[{colors.WHITE}{self.time_str}{colors.DIM}]{colors.RESET}"
        ch_part = f"{colors.CYAN}{colors.BOLD}{ch_text}{colors.RESET}"
        divider = f"{colors.DIM}|{colors.RESET}"
        if self.is_known:
            text_part = f"{colors.WHITE}{colors.BOLD}{self.scroll_ptc}{colors.RESET}"
        else:
            text_part = f"{colors.YELLOW}{self.scroll_ptc}{colors.RESET}"

        return f"{time_part}  {ch_part}  {rssi_str}  {divider}  {text_part}"


# ---------------------------------------------------------------------------
# Rule Parser Loader, Matcher & On-The-Fly Reloader
# ---------------------------------------------------------------------------
DEFAULT_SPY_PARSER_RULES = [
    "x33_rule.json",
    "x2_base_rules.json",
    "x01_rules.json",
    "x05_rules.json",
    "x70_rules.json",
    "x06_rules.json",
    "x04_rules.json",
]


class SpyStatusMessage:
    """Special status / informational message displayed in the TUI scroll view."""
    def __init__(self, text: str, color_code: str = ""):
        self.text = text
        self.color_code = color_code
        self.is_status = True
        self.raw_ascii = ""
        self.raw_hex = ""

    def format_terminal(self, colors: Colors) -> str:
        c = self.color_code if self.color_code else colors.CYAN
        return f"{c}{self.text}{colors.RESET}"


class RulesRegistry:
    """Manages, executes, and reloads modular JSON packet rule parsers on the fly."""
    def __init__(self, rule_paths: Optional[List[str]] = None):
        self.parsers: List[Tuple[str, PacketParser]] = []
        self.custom_paths: Optional[List[str]] = list(rule_paths) if rule_paths else None
        self._file_mtimes: Dict[str, float] = {}
        self._lock = threading.RLock()
        self.load_rules(self.custom_paths)

    def load_rules(self, rule_paths: Optional[List[str]] = None) -> int:
        """Discovers and instantiates PacketParser instances from JSON files."""
        with self._lock:
            if rule_paths:
                self.custom_paths = list(rule_paths)
            self.parsers.clear()
            self._file_mtimes.clear()
            candidate_files: List[str] = []

            paths_to_search = self.custom_paths or []
            if paths_to_search:
                for p in paths_to_search:
                    if os.path.isdir(p):
                        candidate_files.extend(glob.glob(os.path.join(p, "*rule*.json")))
                        candidate_files.extend(glob.glob(os.path.join(p, "x*.json")))
                    elif any(c in p for c in ("*", "?")):
                        candidate_files.extend(glob.glob(p))
                    elif os.path.exists(p):
                        candidate_files.append(p)
            else:
                config_dir = os.path.join(PROJECT_ROOT, "config")
                found_defaults = []
                for fname in DEFAULT_SPY_PARSER_RULES:
                    for d in (config_dir, PROJECT_ROOT):
                        p = os.path.join(d, fname)
                        if os.path.exists(p):
                            found_defaults.append(p)
                            break
                if found_defaults:
                    candidate_files.extend(found_defaults)
                else:
                    if os.path.exists(config_dir):
                        candidate_files.extend(glob.glob(os.path.join(config_dir, "*rule*.json")))
                        candidate_files.extend(glob.glob(os.path.join(config_dir, "x*.json")))
                    candidate_files.extend(glob.glob(os.path.join(PROJECT_ROOT, "*rule*.json")))

            unique_files = sorted(set(candidate_files))
            for fpath in unique_files:
                name = os.path.basename(fpath)
                try:
                    mtime = os.path.getmtime(fpath)
                    with open(fpath, "r", encoding="utf-8") as f:
                        rule_content = json.load(f)
                    if isinstance(rule_content, list):
                        parser = PacketParser(rule_content)
                        self.parsers.append((name, parser))
                        self._file_mtimes[fpath] = mtime
                except Exception:
                    pass

            return len(self.parsers)

    def check_and_reload_modified(self) -> Optional[int]:
        """Checks if any rule files were modified, added, or removed; reloads if changed."""
        with self._lock:
            # 1. Check existing files for modifications or deletions
            for fpath, old_mtime in list(self._file_mtimes.items()):
                if not os.path.exists(fpath):
                    return self.load_rules(self.custom_paths)
                try:
                    if os.path.getmtime(fpath) != old_mtime:
                        return self.load_rules(self.custom_paths)
                except OSError:
                    pass

            # 2. Check if new files appeared in candidate directories
            candidate_files = []
            paths_to_search = self.custom_paths or []
            if paths_to_search:
                for p in paths_to_search:
                    if os.path.isdir(p):
                        candidate_files.extend(glob.glob(os.path.join(p, "*rule*.json")))
                        candidate_files.extend(glob.glob(os.path.join(p, "x*.json")))
                    elif any(c in p for c in ("*", "?")):
                        candidate_files.extend(glob.glob(p))
                    elif os.path.exists(p):
                        candidate_files.append(p)
            else:
                config_dir = os.path.join(PROJECT_ROOT, "config")
                if os.path.exists(config_dir):
                    candidate_files.extend(glob.glob(os.path.join(config_dir, "*rule*.json")))
                    candidate_files.extend(glob.glob(os.path.join(config_dir, "x*.json")))
                candidate_files.extend(glob.glob(os.path.join(PROJECT_ROOT, "*rule*.json")))

            unique_files = sorted(set(candidate_files))
            if len(unique_files) != len(self._file_mtimes):
                return self.load_rules(self.custom_paths)

        return None

    def parse(self, raw_bytes: bytes) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Tries all registered parsers against raw bytes, and falls back to stripped Flux headers."""
        candidates = [raw_bytes]
        if raw_bytes.startswith(b"I") and len(raw_bytes) > 1:
            candidates.append(bytes(raw_bytes[1:]))
        if len(raw_bytes) >= 5 and raw_bytes[0] == 0x49:
            candidates.append(bytes(raw_bytes[5:]))

        with self._lock:
            # Prioritize candidates in order across all registered parsers
            for cand in candidates:
                for idx, (name, parser) in enumerate(self.parsers):
                    try:
                        res = parser.parse(cand)
                        if isinstance(res, dict) and res.get("is_valid"):
                            parsed_data = res.get("data", {})
                            # Real packet rules provide Type, _type_, _scroll_ptc_, or Prefix == 73
                            if parsed_data and (
                                "_scroll_ptc_" in parsed_data
                                or "Type" in parsed_data
                                or "_type_" in parsed_data
                                or parsed_data.get("Prefix") == 73
                            ):
                                if idx > 0:
                                    self.parsers.insert(0, self.parsers.pop(idx))
                                return True, parsed_data, name
                    except Exception:
                        pass

        return False, None, ""


# ---------------------------------------------------------------------------
# Cross-Platform Interactive Terminal Command Listener
# ---------------------------------------------------------------------------
class TerminalCommandListener(threading.Thread):
    """
    Cross-platform interactive command listener for Windows 11, Linux, and macOS.
    Reads input asynchronously without blocking packet ingestion or TUI rendering.
    Handles Windows extended keys (\x00, \xe0) and POSIX ANSI sequences so cursor navigation works,
    and automatically pre-fills active filter strings when entering filter commands (fk, fu, f, fc).
    """
    _KNOWN_FILTER_CMDS = ("fk", "filter-known", "filter_known")
    _UNKNOWN_FILTER_CMDS = ("fu", "filter-unknown", "filter_unknown")
    _BOTH_FILTER_CMDS = ("f", "filter")
    _CHANNEL_FILTER_CMDS = ("fc", "filter-channel", "filter_channel", "channel-filter", "channel_filter", "channels")
    _ALL_FILTER_CMDS = _KNOWN_FILTER_CMDS + _UNKNOWN_FILTER_CMDS + _BOTH_FILTER_CMDS + _CHANNEL_FILTER_CMDS

    def __init__(self, dispatch_fn, shutdown_flag, app=None):
        super().__init__(name="TerminalCommandListener", daemon=True)
        self.dispatch_fn = dispatch_fn
        self.shutdown_flag = shutdown_flag
        self.app = app
        self._buffer = ""
        self._cursor_pos: Optional[int] = None
        self._prefill_done = False
        self._lock = threading.Lock()

    def _effective_cursor_pos(self) -> int:
        # Caller holds self._lock
        if self._cursor_pos is None:
            return len(self._buffer)
        return max(0, min(self._cursor_pos, len(self._buffer)))

    def get_current_buffer(self) -> str:
        with self._lock:
            return self._buffer

    def get_cursor_pos(self) -> int:
        with self._lock:
            return self._effective_cursor_pos()

    def clear_buffer(self):
        with self._lock:
            self._buffer = ""
            self._cursor_pos = None
            self._prefill_done = False

    def _get_active_filter_for_cmd(self, cmd_token: str) -> str:
        if self.app is None:
            return ""
        tok = cmd_token.strip().lower()
        if tok in self._KNOWN_FILTER_CMDS:
            return getattr(self.app, "filter_known_str", "") or ""
        if tok in self._UNKNOWN_FILTER_CMDS:
            return getattr(self.app, "filter_unknown_str", "") or ""
        if tok in self._BOTH_FILTER_CMDS:
            fk = getattr(self.app, "filter_known_str", "") or ""
            fu = getattr(self.app, "filter_unknown_str", "") or ""
            return fk or fu
        if tok in self._CHANNEL_FILTER_CMDS:
            return getattr(self.app, "filter_channel_str", "") or ""
        return ""

    def _move_cursor_left(self):
        with self._lock:
            pos = self._effective_cursor_pos()
            if pos > 0:
                self._cursor_pos = pos - 1

    def _move_cursor_right(self):
        with self._lock:
            pos = self._effective_cursor_pos()
            if pos < len(self._buffer):
                new_pos = pos + 1
                self._cursor_pos = None if new_pos >= len(self._buffer) else new_pos

    def _move_cursor_home(self):
        with self._lock:
            pos = self._effective_cursor_pos()
            if " " in self._buffer:
                arg_start = self._buffer.find(" ") + 1
                if pos > arg_start:
                    self._cursor_pos = arg_start
                    return
            self._cursor_pos = 0 if self._buffer else None

    def _move_cursor_end(self):
        with self._lock:
            self._cursor_pos = None

    def _handle_delete(self):
        with self._lock:
            pos = self._effective_cursor_pos()
            if pos < len(self._buffer):
                self._buffer = self._buffer[:pos] + self._buffer[pos + 1:]
                self._cursor_pos = None if pos >= len(self._buffer) else pos
                if not self._buffer:
                    self._prefill_done = False

    def _process_char(self, ch: str) -> bool:
        """Processes a single non-escape character; returns True if listener should terminate."""
        if ch in ("\r", "\n"):
            with self._lock:
                cmd = self._buffer.strip()
                if cmd and not self._prefill_done and cmd.lower() in self._ALL_FILTER_CMDS:
                    existing = self._get_active_filter_for_cmd(cmd)
                    if existing:
                        self._buffer = f"{cmd} {existing}"
                        self._cursor_pos = None
                        self._prefill_done = True
                        return False
                self._buffer = ""
                self._cursor_pos = None
                self._prefill_done = False
            if cmd:
                self.dispatch_fn(cmd)
            return False

        if ch in ("\x08", "\x7f"):  # Backspace
            with self._lock:
                pos = self._effective_cursor_pos()
                if pos > 0 and self._buffer:
                    self._buffer = self._buffer[:pos - 1] + self._buffer[pos:]
                    new_pos = pos - 1
                    self._cursor_pos = None if new_pos >= len(self._buffer) else new_pos
                    if not self._buffer:
                        self._prefill_done = False
            return False

        if ch == "\x03":  # Ctrl+C
            self.shutdown_flag.set()
            return True

        if ch == "\x15":  # Ctrl+U (clear line)
            self.clear_buffer()
            return False

        if ch == "\x01":  # Ctrl+A (Home)
            self._move_cursor_home()
            return False

        if ch == "\x05":  # Ctrl+E (End)
            self._move_cursor_end()
            return False

        # Quick single-key commands (only when input buffer is empty)
        if not self._buffer:
            if ch in (" ", "p", "P"):
                self.dispatch_fn("p")
                return False
            if ch in ("x", "X"):
                self.dispatch_fn("x")
                return False
            if ch in ("r", "R"):
                self.dispatch_fn("r")
                return False
            if ch in ("a", "A"):
                self.dispatch_fn("a")
                return False
            if ch in ("c", "C"):
                self.dispatch_fn("c")
                return False
            if ch in ("d", "D"):
                self.dispatch_fn("d")
                return False
            if ch in ("l", "L"):
                self.dispatch_fn("l")
                return False
            if ch in ("q", "Q"):
                self.shutdown_flag.set()
                return True
            if ch in ("?", "h", "H"):
                self.dispatch_fn("h")
                return False

        if ord(ch) >= 32:
            with self._lock:
                pos = self._effective_cursor_pos()
                if (
                    ch == " "
                    and pos == len(self._buffer)
                    and not self._prefill_done
                    and self._buffer.lower() in self._ALL_FILTER_CMDS
                ):
                    existing = self._get_active_filter_for_cmd(self._buffer)
                    self._prefill_done = True
                    if existing:
                        self._buffer = f"{self._buffer} {existing}"
                        self._cursor_pos = None
                        return False

                if pos >= len(self._buffer):
                    self._buffer += ch
                    self._cursor_pos = None
                else:
                    self._buffer = self._buffer[:pos] + ch + self._buffer[pos:]
                    self._cursor_pos = pos + 1
        return False

    def run(self):
        is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

        # Native Windows console keyboard listener using msvcrt
        if sys.platform == "win32" and is_tty:
            import msvcrt
            while not self.shutdown_flag.is_set():
                try:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ("\x00", "\xe0"):  # Extended key prefix (arrows, home, end, delete)
                            scan = msvcrt.getwch() if msvcrt.kbhit() else ""
                            if scan == "K":
                                self._move_cursor_left()
                            elif scan == "M":
                                self._move_cursor_right()
                            elif scan == "G":
                                self._move_cursor_home()
                            elif scan == "O":
                                self._move_cursor_end()
                            elif scan == "S":
                                self._handle_delete()
                        elif ch == "\x1b":  # Esc key cancels current edit
                            self.clear_buffer()
                        elif self._process_char(ch):
                            break
                    else:
                        time.sleep(0.02)
                except Exception:
                    time.sleep(0.05)
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
                    # Put terminal in cbreak mode and disable local echo
                    tty.setcbreak(fd)
                    cbreak_settings = termios.tcgetattr(fd)
                    cbreak_settings[3] &= ~termios.ECHO
                    termios.tcsetattr(fd, termios.TCSADRAIN, cbreak_settings)

                    while not self.shutdown_flag.is_set():
                        try:
                            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if not rlist:
                                continue
                            ch = sys.stdin.read(1)
                            if not ch:
                                continue

                            if ch == "\x1b":  # Escape / ANSI sequence (arrow keys, home, end, delete)
                                r_esc, _, _ = select.select([sys.stdin], [], [], 0.02)
                                if not r_esc:
                                    self.clear_buffer()
                                else:
                                    seq = sys.stdin.read(1)
                                    if seq in ("[", "O"):
                                        seq_chars = []
                                        while True:
                                            r_more, _, _ = select.select([sys.stdin], [], [], 0.02)
                                            if not r_more:
                                                break
                                            c2 = sys.stdin.read(1)
                                            if not c2:
                                                break
                                            seq_chars.append(c2)
                                            if "@" <= c2 <= "~":
                                                break
                                        tail = "".join(seq_chars)
                                        if tail == "D":
                                            self._move_cursor_left()
                                        elif tail == "C":
                                            self._move_cursor_right()
                                        elif tail in ("H", "1~", "7~"):
                                            self._move_cursor_home()
                                        elif tail in ("F", "4~", "8~"):
                                            self._move_cursor_end()
                                        elif tail == "3~":
                                            self._handle_delete()
                                    else:
                                        while True:
                                            r_more, _, _ = select.select([sys.stdin], [], [], 0.02)
                                            if r_more:
                                                sys.stdin.read(1)
                                            else:
                                                break
                            elif self._process_char(ch):
                                break
                        except Exception:
                            time.sleep(0.02)
                finally:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                return
            except Exception:
                # Fall back to line reader below if termios setup fails
                pass

        # Fallback line reader for redirected streams and non-interactive environments
        while not self.shutdown_flag.is_set():
            try:
                line = sys.stdin.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                cmd = line.strip()
                if cmd:
                    self.dispatch_fn(cmd)
            except Exception:
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Core ApexBOSSpy Engine
# ---------------------------------------------------------------------------
class ApexBOSSpy:
    """Core Dual-Panel Packet Ingestion, Filtering, DVR, and Display Engine."""
    def __init__(
        self,
        endpoints: Optional[List[str]] = None,
        host: Optional[str] = None,
        udp_port: Optional[int] = None,
        rule_paths: Optional[List[str]] = None,
        regex_filter: str = "",
        filter_known: str = "",
        filter_unknown: str = "",
        filter_channel: str = "",
        case_sensitive: bool = False,
        pause_on_regex: Optional[str] = None,
        pause_on_known: Optional[str] = None,
        pause_on_unknown: Optional[str] = None,
        dvr_size: int = 2000,
        log_file: Optional[str] = None,
        show_hex: bool = False,
        show_ascii: Optional[bool] = None,
        show_ascii_known: bool = False,
        show_ascii_unknown: bool = True,
        stream_mode: bool = False,
        no_color: bool = False,
    ):
        # Resolve endpoints with host support
        if not endpoints:
            self.endpoints = discover_grabber_endpoints(host=host)
        else:
            self.endpoints = endpoints

        self.remote_host = host
        self.udp_port = udp_port
        self.stream_mode = stream_mode
        self.show_hex = show_hex
        if show_ascii is not None:
            self.show_ascii_known = bool(show_ascii)
            self.show_ascii_unknown = bool(show_ascii)
        else:
            self.show_ascii_known = bool(show_ascii_known)
            self.show_ascii_unknown = bool(show_ascii_unknown)

        self.vt_supported = init_cross_platform_terminal(no_color_flag=no_color)
        self.colors = Colors(enabled=not no_color)

        self.rules_registry = RulesRegistry(rule_paths)
        self.rules_status_msg: str = ""
        self.last_rules_reload_time: float = time.time()
        self.start_time = time.time()

        # DVR Ring Buffers (Separate for Known and Unknown)
        self.dvr_size = max(1, dvr_size)
        self.dvr_known: collections.deque = collections.deque(maxlen=self.dvr_size)
        self.dvr_unknown: collections.deque = collections.deque(maxlen=self.dvr_size)
        self._dvr_lock = threading.RLock()

        # Display Rolling Windows
        self.display_known: collections.deque = collections.deque(maxlen=200)
        self.display_unknown: collections.deque = collections.deque(maxlen=100)
        self._display_lock = threading.RLock()

        # Filtering State (Separate content filters for Known and Unknown, shared channel filter for both)
        self.case_sensitive = case_sensitive
        self.filter_known_str = filter_known or regex_filter or ""
        self.filter_unknown_str = filter_unknown or ""
        self.filter_channel_str = (filter_channel or "").strip()
        self._filter_known_re: Optional[_RePattern] = None
        self._filter_unknown_re: Optional[_RePattern] = None
        self._filter_channel_re: Optional[_RePattern] = None
        self._update_filters()

        # Pause-On-Match (Stop RegEx Breakpoint) State
        self.pause_on_known_str = pause_on_known or pause_on_regex or ""
        self.pause_on_unknown_str = pause_on_unknown or ""
        self._pause_on_known_re: Optional[_RePattern] = None
        self._pause_on_unknown_re: Optional[_RePattern] = None
        self._update_pause_on_triggers()

        # Scrolling & Execution Flags
        self.is_paused = False
        self.is_paused_known = False
        self.is_paused_unknown = False
        self.pause_reason_known = ""
        self.pause_reason_unknown = ""
        self.trigger_alert = ""
        self._status_timer: Optional[threading.Thread] = None
        self._status_unpause_cancel: threading.Event = threading.Event()
        self.shutdown_flag = threading.Event()

        # Logging State
        self.log_filepath = log_file
        self.log_file_handle = None
        self.logged_count = 0
        self._log_lock = threading.RLock()
        if self.log_filepath:
            self._open_log_file(self.log_filepath)

        # Sliding Window Statistics (Pack/Min live rates)
        self.total_packets_received = 0
        self.parsed_packets_count = 0
        self.total_unknown_count = 0
        self.matched_known_count = 0
        self.matched_unknown_count = 0
        self.rate_window_total = collections.deque()
        self.rate_window_known = collections.deque()
        self.rate_window_unknown = collections.deque()
        self._stats_lock = threading.RLock()

        # Background Threads
        self.zmq_thread: Optional[threading.Thread] = None
        self.udp_thread: Optional[threading.Thread] = None
        self.cmd_listener: Optional[TerminalCommandListener] = None
        self.tui_thread: Optional[threading.Thread] = None

    # Backward compatibility properties
    @property
    def dvr_buffer(self) -> collections.deque:
        """Alias for known DVR ring buffer for backwards compatibility with tests."""
        return self.dvr_known

    @property
    def filter_str(self) -> str:
        return self.filter_known_str

    @property
    def pause_on_str(self) -> str:
        return self.pause_on_known_str

    @property
    def pause_reason(self) -> str:
        return self.pause_reason_known or self.pause_reason_unknown

    @property
    def matched_packets_count(self) -> int:
        return self.matched_known_count + self.matched_unknown_count

    # -----------------------------------------------------------------------
    # Filter & Trigger Compilation
    # -----------------------------------------------------------------------
    def _compile_re(self, pattern: str) -> Optional[_RePattern]:
        if not pattern:
            return None
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            print(f"\n[!] Invalid Regular Expression '{pattern}': {e}", file=sys.stderr)
            return None

    def _update_filters(self):
        self._filter_known_re = self._compile_re(self.filter_known_str)
        self._filter_unknown_re = self._compile_re(self.filter_unknown_str)
        self._filter_channel_re = self._compile_re(self.filter_channel_str)

    def _update_filter_re(self):
        """Backward compatibility alias for tests."""
        self._update_filters()

    def _update_pause_on_triggers(self):
        self._pause_on_known_re = self._compile_re(self.pause_on_known_str)
        self._pause_on_unknown_re = self._compile_re(self.pause_on_unknown_str)

    def _update_pause_on_re(self):
        """Backward compatibility alias for tests."""
        self._update_pause_on_triggers()

    @property
    def _filter_re(self) -> Optional[_RePattern]:
        return self._filter_known_re

    @property
    def _pause_on_re(self) -> Optional[_RePattern]:
        return self._pause_on_known_re

    def set_channel_filter(self, regex_str: str):
        """Sets the shared channel-number RegEx filter for both known and unknown packets."""
        self.filter_channel_str = (regex_str or "").strip()
        self._update_filters()

    def set_filter(self, regex_str: str, target: str = "both"):
        """Sets RegEx filter for known, unknown, both panels, or shared channel filter."""
        regex_str = (regex_str or "").strip()
        target = target.lower()
        if target in ("channel", "channels", "ch"):
            self.set_channel_filter(regex_str)
            return
        if target in ("known", "k"):
            self.filter_known_str = regex_str
        elif target in ("unknown", "u", "unk"):
            self.filter_unknown_str = regex_str
        else:
            self.filter_known_str = regex_str
            self.filter_unknown_str = regex_str
        self._update_filters()

    def set_pause_on(self, regex_str: str, target: str = "both"):
        """Sets stop RegEx trigger for known, unknown, or both panels."""
        regex_str = (regex_str or "").strip()
        target = target.lower()
        if target in ("known", "k"):
            self.pause_on_known_str = regex_str
            self.pause_reason_known = ""
        elif target in ("unknown", "u", "unk"):
            self.pause_on_unknown_str = regex_str
            self.pause_reason_unknown = ""
        else:
            self.pause_on_known_str = regex_str
            self.pause_on_unknown_str = regex_str
            self.pause_reason_known = ""
            self.pause_reason_unknown = ""
        self._update_pause_on_triggers()

    def toggle_case_sensitivity(self):
        """Toggles case sensitivity for all active filters and triggers."""
        self.case_sensitive = not self.case_sensitive
        self._update_filters()
        self._update_pause_on_triggers()

    def toggle_pause(self, target: str = "all"):
        """Toggles live scrolling pause state."""
        if hasattr(self, "_status_unpause_cancel"):
            self._status_unpause_cancel.set()
        target = target.lower()
        if target in ("known", "k"):
            self.is_paused_known = not self.is_paused_known
            if not self.is_paused_known:
                self.pause_reason_known = ""
        elif target in ("unknown", "u", "unk"):
            self.is_paused_unknown = not self.is_paused_unknown
            if not self.is_paused_unknown:
                self.pause_reason_unknown = ""
        else:
            self.is_paused = not self.is_paused
            self.is_paused_known = self.is_paused
            self.is_paused_unknown = self.is_paused
            if not self.is_paused:
                self.pause_reason_known = ""
                self.pause_reason_unknown = ""
                self.trigger_alert = ""

    @property
    def show_ascii(self) -> bool:
        """True if either known or unknown ASCII display is enabled."""
        return self.show_ascii_known or self.show_ascii_unknown

    @show_ascii.setter
    def show_ascii(self, val: bool):
        self.show_ascii_known = bool(val)
        self.show_ascii_unknown = bool(val)

    def toggle_show_hex(self):
        """Toggles displaying raw hex string line."""
        self.show_hex = not self.show_hex

    def toggle_show_ascii(self, target: str = "both"):
        """Toggles displaying extracted ASCII text line for known and/or unknown packets."""
        if target == "known":
            self.show_ascii_known = not self.show_ascii_known
        elif target == "unknown":
            self.show_ascii_unknown = not self.show_ascii_unknown
        else:
            if self.show_ascii_known or self.show_ascii_unknown:
                self.show_ascii_known = False
                self.show_ascii_unknown = False
            else:
                self.show_ascii_known = True
                self.show_ascii_unknown = True

    def reload_rules(self, path: Optional[str] = None) -> int:
        """Reloads packet rule parsers on the fly."""
        count = self.rules_registry.load_rules([path] if path else None)
        t_str = datetime.now().strftime("%H:%M:%S")
        self.rules_status_msg = f"[RULES] Reloaded {count} rule parser(s) at {t_str}"
        self.last_rules_reload_time = time.time()
        if self.stream_mode or not sys.stdout.isatty():
            print(f"\n{self.colors.GREEN}[RULES] Reloaded {count} rule parser(s){self.colors.RESET}")
        return count

    # -----------------------------------------------------------------------
    # Logging & DVR Export
    # -----------------------------------------------------------------------
    def _open_log_file(self, filepath: str) -> bool:
        with self._log_lock:
            try:
                dirname = os.path.dirname(os.path.abspath(filepath))
                if dirname and not os.path.exists(dirname):
                    os.makedirs(dirname, exist_ok=True)
                self.log_file_handle = open(filepath, "a", encoding="utf-8")
                self.log_filepath = filepath
                return True
            except Exception as e:
                print(f"[!] Error opening log file '{filepath}': {e}", file=sys.stderr)
                self.log_file_handle = None
                return False

    def toggle_logging(self, filepath: Optional[str] = None):
        """Starts or stops continuous logging."""
        with self._log_lock:
            if self.log_file_handle:
                try:
                    self.log_file_handle.flush()
                    self.log_file_handle.close()
                except Exception:
                    pass
                self.log_file_handle = None
            else:
                target = filepath or f"apexbos_spy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                if self._open_log_file(target):
                    self.logged_count = 0

    def save_dvr_known(self, filepath: Optional[str] = None) -> str:
        """Exports the Known Packets DVR ring buffer to file."""
        target = filepath or f"apexbos_spy_dvr_known_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with self._dvr_lock:
            snapshot = list(self.dvr_known)
        return self._write_dvr_file(target, "Known Packets", snapshot, self.filter_known_str)

    def save_dvr_unknown(self, filepath: Optional[str] = None) -> str:
        """Exports the Unknown Packets DVR ring buffer to file."""
        target = filepath or f"apexbos_spy_dvr_unknown_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with self._dvr_lock:
            snapshot = list(self.dvr_unknown)
        return self._write_dvr_file(target, "Unknown Packets", snapshot, self.filter_unknown_str)

    def save_dvr(self, filepath: Optional[str] = None) -> str:
        """Exports both DVR buffers (or known if single file) for unified preservation."""
        target = filepath or f"apexbos_spy_dvr_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with self._dvr_lock:
            known_snap = list(self.dvr_known)
            unk_snap = list(self.dvr_unknown)

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write("# ==============================================================================\n")
                f.write(f"# ApexBOSSpy DVR Export - {datetime.now().isoformat()}\n")
                f.write(f"# Total Messages Captured: {len(known_snap) + len(unk_snap)} / {self.dvr_size}\n")
                f.write(f"# Known Filter: '{self.filter_known_str}' | Unknown Filter: '{self.filter_unknown_str}' | Channel Filter: '{self.filter_channel_str}'\n")
                f.write("# ==============================================================================\n\n")

                if known_snap:
                    f.write("# --- KNOWN PACKETS ---\n")
                    for m in known_snap:
                        f.write(m.plain_line + "\n")
                        if self.show_ascii_known:
                            f.write(m.ascii_line + "\n")
                        if self.show_hex:
                            f.write(m.hex_line + "\n")
                    f.write("\n")

                if unk_snap:
                    f.write("# --- UNKNOWN PACKETS ---\n")
                    for m in unk_snap:
                        f.write(m.plain_line + "\n")
                        if self.show_ascii_unknown:
                            f.write(m.ascii_line + "\n")
                        if self.show_hex:
                            f.write(m.hex_line + "\n")
            return target
        except Exception as e:
            print(f"[!] Failed saving DVR export: {e}", file=sys.stderr)
            return ""

    def _write_dvr_file(self, target: str, label: str, snapshot: List[SpyMessage], flt: str) -> str:
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(f"# ApexBOSSpy {label} DVR Export - {datetime.now().isoformat()}\n")
                f.write(f"# Total Messages Captured: {len(snapshot)} / {self.dvr_size}\n")
                f.write(f"# Active Filter: '{flt}' | Channel Filter: '{self.filter_channel_str}' | Case-Sensitive: {self.case_sensitive}\n")
                f.write("# ==============================================================================\n\n")
                for msg in snapshot:
                    f.write(msg.plain_line + "\n")
                    if (label == "Known Packets" and self.show_ascii_known) or (label == "Unknown Packets" and self.show_ascii_unknown):
                        f.write(msg.ascii_line + "\n")
                    if self.show_hex:
                        f.write(msg.hex_line + "\n")
            return target
        except Exception as e:
            print(f"[!] Failed saving {label} DVR: {e}", file=sys.stderr)
            return ""

    # -----------------------------------------------------------------------
    # Rate Calculation (Pack/Min)
    # -----------------------------------------------------------------------
    def calculate_pack_min(self, window: collections.deque) -> float:
        """Calculates Packets per Minute using sliding 60-second window."""
        now = time.time()
        cutoff = now - 60.0
        with self._stats_lock:
            while window and window[0] < cutoff:
                window.popleft()
            count = len(window)

        elapsed = max(0.2, now - self.start_time)
        factor = 60.0 / min(elapsed, 60.0)
        return count * factor

    # -----------------------------------------------------------------------
    # Packet Processing Pipeline
    # -----------------------------------------------------------------------
    def process_incoming_packet(
        self,
        raw_bytes: bytes,
        channel: Any = None,
        rssi: Optional[float] = None,
        timestamp: Optional[float] = None,
        raw_hex: str = "",
    ):
        """Processes, decodes, filters, records into DVR, and routes to appropriate panel."""
        now_t = timestamp or time.time()

        with self._stats_lock:
            self.total_packets_received += 1
            self.rate_window_total.append(now_t)

        # Parse through modular JSON packet rules
        is_valid, parsed_data, rule_name = self.rules_registry.parse(raw_bytes)
        if is_valid and channel is None and parsed_data:
            channel = parsed_data.get("Channel") or parsed_data.get("channel") or parsed_data.get("channel_meta")

        msg = SpyMessage(
            raw_bytes=raw_bytes,
            channel=channel,
            rssi=rssi,
            timestamp=now_t,
            parsed_data=parsed_data,
            rule_name=rule_name,
            is_known=is_valid,
            raw_hex=raw_hex,
        )

        if is_valid:
            self._process_known_packet(msg, now_t)
        else:
            self._process_unknown_packet(msg, now_t)

    def _matches_channel_filter(self, msg: SpyMessage) -> bool:
        """Returns True if no channel filter is active or the message channel matches it."""
        if not self._filter_channel_re:
            return True
        ch_str = str(msg.channel) if msg.channel is not None else ""
        return bool(self._filter_channel_re.search(ch_str))

    def _process_known_packet(self, msg: SpyMessage, now_t: float):
        with self._stats_lock:
            self.parsed_packets_count += 1
            self.rate_window_known.append(now_t)

        # 1. Record into Known DVR buffer (always records for playback/export)
        with self._dvr_lock:
            self.dvr_known.append(msg)

        # 2. Evaluate shared Channel RegEx filter & Known RegEx filter
        if not self._matches_channel_filter(msg):
            return

        if self._filter_known_re:
            scroll_val = str(msg.parsed_data.get("_scroll_ptc_", "") or msg.scroll_ptc)
            matched = bool(
                self._filter_known_re.search(scroll_val)
                or self._filter_known_re.search(msg.scroll_ptc)
                or self._filter_known_re.search(msg.plain_line)
            )
            # If ASCII strings are toggled on, also use filters on the ASCII text
            if not matched and self.show_ascii_known:
                raw_ascii = msg.raw_bytes.decode("latin-1", errors="ignore")
                matched = bool(
                    self._filter_known_re.search(msg.raw_ascii)
                    or self._filter_known_re.search(raw_ascii)
                )
            if not matched and self.show_hex:
                matched = bool(self._filter_known_re.search(msg.raw_hex))

            if not matched:
                return

        with self._stats_lock:
            self.matched_known_count += 1

        # 3. Check Known Stop RegEx (Pause-On-Match) Trigger
        if self._pause_on_known_re:
            scroll_val = str(msg.parsed_data.get("_scroll_ptc_", "") or msg.scroll_ptc)
            matched_stop = bool(
                self._pause_on_known_re.search(scroll_val)
                or self._pause_on_known_re.search(msg.scroll_ptc)
                or self._pause_on_known_re.search(msg.plain_line)
            )
            if not matched_stop and self.show_ascii_known:
                raw_ascii = msg.raw_bytes.decode("latin-1", errors="ignore")
                matched_stop = bool(
                    self._pause_on_known_re.search(msg.raw_ascii)
                    or self._pause_on_known_re.search(raw_ascii)
                )
            if not matched_stop and self.show_hex:
                matched_stop = bool(self._pause_on_known_re.search(msg.raw_hex))

            if matched_stop:
                self.is_paused = True
                self.is_paused_known = True
                self.pause_reason_known = f"Matched Stop RegEx '{self.pause_on_known_str}'"
                self.trigger_alert = f"[!] KNOWN STOP REGEX TRIGGERED: {self.pause_reason_known}"

        # 4. Continuous Logging
        if self.log_file_handle:
            with self._log_lock:
                try:
                    self.log_file_handle.write(f"[KNOWN] {msg.plain_line}\n")
                    if self.show_ascii_known:
                        self.log_file_handle.write(f"    ASCII: {msg.raw_ascii}\n")
                    if self.show_hex:
                        self.log_file_handle.write(f"    HEX: {msg.raw_hex}\n")
                    self.log_file_handle.flush()
                    self.logged_count += 1
                except Exception:
                    pass

        # 5. Append to Known display buffer if not paused
        if not self.is_paused and not self.is_paused_known:
            with self._display_lock:
                self.display_known.append(msg)

        # Stream mode fallback
        if self.stream_mode or not sys.stdout.isatty():
            if not self.is_paused and not self.is_paused_known:
                sys.stdout.write(f"[KNOWN] {msg.format_terminal(self.colors)}\n")
                if self.show_ascii_known:
                    sys.stdout.write(f"    ASCII: {msg.raw_ascii}\n")
                if self.show_hex:
                    sys.stdout.write(f"    HEX: {msg.raw_hex}\n")
                sys.stdout.flush()

    def _process_unknown_packet(self, msg: SpyMessage, now_t: float):
        with self._stats_lock:
            self.total_unknown_count += 1
            self.rate_window_unknown.append(now_t)

        # 1. Record into Unknown DVR buffer
        with self._dvr_lock:
            self.dvr_unknown.append(msg)

        # 2. Evaluate shared Channel RegEx filter & Unknown RegEx filter
        if not self._matches_channel_filter(msg):
            return

        raw_ascii = msg.raw_bytes.decode("latin-1", errors="ignore")
        if self._filter_unknown_re:
            matched = bool(
                self._filter_unknown_re.search(msg.scroll_ptc)
                or self._filter_unknown_re.search(msg.plain_line)
                or self._filter_unknown_re.search(msg.raw_hex)
                or self._filter_unknown_re.search(raw_ascii)
            )
            if not matched:
                return

        with self._stats_lock:
            self.matched_unknown_count += 1

        # 3. Check Unknown Stop RegEx Trigger
        if self._pause_on_unknown_re:
            if (
                self._pause_on_unknown_re.search(msg.scroll_ptc)
                or self._pause_on_unknown_re.search(msg.plain_line)
                or self._pause_on_unknown_re.search(msg.raw_hex)
                or self._pause_on_unknown_re.search(raw_ascii)
            ):
                self.is_paused = True
                self.is_paused_unknown = True
                self.pause_reason_unknown = f"Matched Stop RegEx '{self.pause_on_unknown_str}'"
                self.trigger_alert = f"[!] UNKNOWN STOP REGEX TRIGGERED: {self.pause_reason_unknown}"

        # 4. Continuous Logging
        if self.log_file_handle:
            with self._log_lock:
                try:
                    self.log_file_handle.write(f"[UNKNOWN] {msg.plain_line}\n")
                    if self.show_ascii_unknown:
                        self.log_file_handle.write(f"    ASCII: {msg.raw_ascii}\n")
                    if self.show_hex:
                        self.log_file_handle.write(f"    HEX: {msg.raw_hex}\n")
                    self.log_file_handle.flush()
                    self.logged_count += 1
                except Exception:
                    pass

        # 5. Append to Unknown display buffer if not paused
        if not self.is_paused and not self.is_paused_unknown:
            with self._display_lock:
                self.display_unknown.append(msg)

        # Stream mode fallback
        if self.stream_mode or not sys.stdout.isatty():
            if not self.is_paused and not self.is_paused_unknown:
                sys.stdout.write(f"[UNKNOWN] {msg.format_terminal(self.colors)}\n")
                if self.show_ascii_unknown:
                    sys.stdout.write(f"    ASCII: {msg.raw_ascii}\n")
                if self.show_hex:
                    sys.stdout.write(f"    HEX: {msg.raw_hex}\n")
                sys.stdout.flush()

    # -----------------------------------------------------------------------
    # Dual-Panel Split TUI Screen Renderer
    # -----------------------------------------------------------------------
    @staticmethod
    def compute_panel_heights(height: int) -> Tuple[int, int]:
        """
        Computes (known_lines_count, unk_lines_count) for the dual-panel TUI layout.
        Allocates ~40% of available packet rows to Unknown (up to 16) while keeping Known taller.
        """
        # Header (7 lines) + Panel Headers (2 lines) + Footer (2 lines) = 11 fixed chrome lines
        fixed_overhead = 11
        available_lines = max(8, height - fixed_overhead)
        unk_lines_count = max(4, min(16, int(round(available_lines * 0.40))))
        if unk_lines_count >= available_lines - unk_lines_count:
            unk_lines_count = max(3, (available_lines - 1) // 2)
        known_lines_count = max(4, available_lines - unk_lines_count)
        return known_lines_count, unk_lines_count

    def render_ascii_screen(self, width: Optional[int] = None, height: Optional[int] = None) -> str:
        """Constructs the full dual-panel ASCII TUI screen string."""
        cols = self.colors
        if width is None or height is None:
            term_cols, term_lines = shutil.get_terminal_size((100, 28))
            width = max(80, term_cols) if width is None else max(80, width)
            height = max(20, term_lines) if height is None else max(20, height)
        else:
            width = max(80, width)
            height = max(20, height)

        # Live Packets per Minute calculation
        ppm_tot = self.calculate_pack_min(self.rate_window_total)
        ppm_k = self.calculate_pack_min(self.rate_window_known)
        ppm_u = self.calculate_pack_min(self.rate_window_unknown)

        # Compute panel layout heights
        known_lines_count, unk_lines_count = self.compute_panel_heights(height)

        lines: List[str] = []

        # --- PINNED COMMAND INFO & HEADER SECTION ---
        sep = cols.CYAN + "=" * width + cols.RESET
        div = cols.DIM + "─" * width + cols.RESET

        lines.append(sep)
        title = f" {cols.BOLD}{cols.WHITE}ApexBOSSpy ({VERSION}){cols.RESET} | Direct Grabber Packet Spy & Rule Inspector"
        lines.append(title + cols.CLEAR_EOL)

        eps_str = ", ".join(self.endpoints[:3])
        if len(self.endpoints) > 3:
            eps_str += f" (+{len(self.endpoints) - 3} more)"
        lines.append(f" * Endpoints: {cols.WHITE}{eps_str}{cols.RESET} | Rules: {cols.WHITE}{len(self.rules_registry.parsers)} loaded{cols.RESET}" + cols.CLEAR_EOL)

        # Pack/Min Stat Line
        pack_stat = (
            f" * {cols.BOLD}Pack/Min:{cols.RESET} Total: {cols.GREEN}{cols.BOLD}{ppm_tot:5.1f}{cols.RESET} Pkts/m | "
            f"Known: {cols.CYAN}{ppm_k:5.1f}{cols.RESET} Pkts/m | "
            f"Unknown: {cols.YELLOW}{ppm_u:5.1f}{cols.RESET} Pkts/m  "
            f"{cols.DIM}(Totals: {self.total_packets_received:,} | K: {self.parsed_packets_count:,}, U: {self.total_unknown_count:,}){cols.RESET}"
        )
        lines.append(pack_stat + cols.CLEAR_EOL)

        # Scrolling & Filters Status Line
        status_k = f"{cols.RED}[PAUSED]{cols.RESET}" if (self.is_paused or self.is_paused_known) else f"{cols.GREEN}[SCROLLING]{cols.RESET}"
        status_u = f"{cols.RED}[PAUSED]{cols.RESET}" if (self.is_paused or self.is_paused_unknown) else f"{cols.GREEN}[SCROLLING]{cols.RESET}"
        flt_k_desc = f"'{self.filter_known_str}'" if self.filter_known_str else "ALL"
        flt_u_desc = f"'{self.filter_unknown_str}'" if self.filter_unknown_str else "ALL"
        flt_ch_desc = f"'{self.filter_channel_str}'" if self.filter_channel_str else "ALL"
        hex_desc = f"{cols.GREEN}ON{cols.RESET}" if self.show_hex else f"{cols.DIM}OFF{cols.RESET}"
        ascii_k_desc = f"{cols.GREEN}ON{cols.RESET}" if self.show_ascii_known else f"{cols.DIM}OFF{cols.RESET}"
        log_desc = f"{cols.GREEN}ON{cols.RESET}" if self.log_file_handle else f"{cols.DIM}OFF{cols.RESET}"
        case_desc = f"Case:{cols.WHITE}{'ON' if self.case_sensitive else 'OFF'}{cols.RESET}"

        status_line = (
            f" * Status: Known {status_k} [Flt: {cols.WHITE}{flt_k_desc}{cols.RESET}] | "
            f"Unknown {status_u} [Flt: {cols.WHITE}{flt_u_desc}{cols.RESET}] | "
            f"ChFlt: {cols.WHITE}{flt_ch_desc}{cols.RESET} | "
            f"Hex: {hex_desc} | AsciiK: {ascii_k_desc} | Log: {log_desc} | {case_desc}"
        )
        lines.append(status_line + cols.CLEAR_EOL)

        # Quick Commands Reference
        cmd_ref = (
            f" {cols.DIM}Commands: [p]ause  [x] hex  [ak/a] known ascii  [r]eload rules  [fk] known flt  "
            f"[fu] unk flt  [fc] ch flt  [tk] stop regex  [c]ase  [d]vr  [l]og  [q]uit{cols.RESET}"
        )
        lines.append(cmd_ref + cols.CLEAR_EOL)

        # Alert banner if stop regex triggered or rules reloaded
        banner_msg = self.trigger_alert or self.rules_status_msg
        if banner_msg:
            color_bg = cols.BG_RED if self.trigger_alert else cols.BG_BLUE
            lines.append(f"{color_bg}{cols.WHITE} {banner_msg} {cols.RESET}" + cols.CLEAR_EOL)
        else:
            lines.append(div)

        # --- SECTION 1: KNOWN PACKETS PANEL (Upper, Taller) ---
        with self._dvr_lock:
            k_dvr_len = len(self.dvr_known)
        k_panel_hdr = (
            f"{cols.CYAN}--- KNOWN PACKETS (Rule Parsed) "
            f"[Filter: {flt_k_desc} | Stop: '{self.pause_on_known_str or 'None'}' | DVR: {k_dvr_len}/{self.dvr_size}] "
            f"{'-' * 40}{cols.RESET}"
        )
        lines.append(k_panel_hdr[:width] + cols.CLEAR_EOL)

        # Build display lines for Known Panel (accounting for offset ASCII and raw hex lines)
        with self._display_lock:
            recent_known = list(self.display_known)

        known_panel_lines: List[str] = []
        for m in recent_known:
            if isinstance(m, str):
                known_panel_lines.append(m)
                continue
            known_panel_lines.append(m.format_terminal(cols))
            if getattr(m, "is_status", False):
                continue
            if self.show_ascii_known and hasattr(m, "raw_ascii"):
                known_panel_lines.append(f"    {cols.DIM}ASCII:{cols.RESET} {cols.CYAN}{m.raw_ascii}{cols.RESET}")
            if self.show_hex and hasattr(m, "raw_hex"):
                known_panel_lines.append(f"    {cols.DIM}HEX:{cols.RESET} {cols.WHITE}{m.raw_hex}{cols.RESET}")

        visible_known = known_panel_lines[-known_lines_count:]
        for line in visible_known:
            lines.append(line + cols.CLEAR_EOL)
        for _ in range(known_lines_count - len(visible_known)):
            lines.append(cols.DIM + "  (waiting for known packets...)" + cols.RESET + cols.CLEAR_EOL)

        # --- SECTION 2: UNKNOWN PACKETS PANEL (Lower, Shorter) ---
        with self._dvr_lock:
            u_dvr_len = len(self.dvr_unknown)
        u_panel_hdr = (
            f"{cols.YELLOW}--- UNKNOWN PACKETS (Unparsed / Raw) "
            f"[Filter: {flt_u_desc} | Stop: '{self.pause_on_unknown_str or 'None'}' | DVR: {u_dvr_len}/{self.dvr_size}] "
            f"{'-' * 40}{cols.RESET}"
        )
        lines.append(u_panel_hdr[:width] + cols.CLEAR_EOL)

        # Build display lines for Unknown Panel (accounting for offset ASCII and hex lines)
        with self._display_lock:
            recent_unknown = list(self.display_unknown)

        unk_panel_lines: List[str] = []
        for m in recent_unknown:
            unk_panel_lines.append(m.format_terminal(cols))
            if self.show_ascii_unknown:
                unk_panel_lines.append(f"    {cols.DIM}ASCII:{cols.RESET} {cols.CYAN}{m.raw_ascii}{cols.RESET}")
            if self.show_hex:
                unk_panel_lines.append(f"    {cols.DIM}HEX:{cols.RESET} {cols.WHITE}{m.raw_hex}{cols.RESET}")

        visible_unk = unk_panel_lines[-unk_lines_count:]
        for line in visible_unk:
            lines.append(line + cols.CLEAR_EOL)
        for _ in range(unk_lines_count - len(visible_unk)):
            lines.append(cols.DIM + "  (waiting for unknown packets...)" + cols.RESET + cols.CLEAR_EOL)

        # --- FOOTER / COMMAND PROMPT ---
        lines.append(div)
        cur_cmd = self.cmd_listener.get_current_buffer() if self.cmd_listener else ""
        if self.cmd_listener and hasattr(self.cmd_listener, "get_cursor_pos"):
            cpos = self.cmd_listener.get_cursor_pos()
        else:
            cpos = len(cur_cmd)
        if cols.enabled and 0 <= cpos < len(cur_cmd):
            rendered_cmd = (
                cur_cmd[:cpos]
                + f"\033[7m{cur_cmd[cpos]}\033[27m"
                + cur_cmd[cpos + 1:]
            )
        else:
            rendered_cmd = cur_cmd
        lines.append(f"{cols.BOLD}Command >{cols.RESET} {rendered_cmd}" + cols.CLEAR_EOL)

        return "\n".join(lines)

    def render_tui(self):
        """Renders the fixed pinned header and dual scrolling panels in-place."""
        term_cols, term_lines = shutil.get_terminal_size((100, 28))
        screen = self.render_ascii_screen(width=term_cols, height=term_lines)
        if self.vt_supported:
            sys.stdout.write("\033[H" + screen)
        else:
            sys.stdout.write(screen + "\n")
        sys.stdout.flush()

    def run_tui_refresh_loop(self):
        """Refreshes the dual-panel TUI display and auto-checks modified rules on the fly."""
        last_mtime_check = time.time()
        while not self.shutdown_flag.is_set():
            try:
                now = time.time()
                # Check for modified or new packet rules every 1 second
                if now - last_mtime_check >= 1.0:
                    last_mtime_check = now
                    reloaded_count = self.rules_registry.check_and_reload_modified()
                    if reloaded_count is not None:
                        t_str = datetime.now().strftime("%H:%M:%S")
                        self.rules_status_msg = f"[RULES] Auto-reloaded {reloaded_count} rule parser(s) at {t_str}"

                self.render_tui()
                time.sleep(0.1)
            except Exception:
                time.sleep(0.1)

    # -----------------------------------------------------------------------
    # Ingestion Receivers (ZeroMQ and UDP)
    # -----------------------------------------------------------------------
    def run_zmq_receiver(self):
        """Background thread receiving ZeroMQ multipart messages from Grabber."""
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.setsockopt(zmq.RCVTIMEO, 500)
        sock.setsockopt(zmq.TCP_KEEPALIVE, 1)

        for ep in self.endpoints:
            target_ep = ep.strip()
            if target_ep.isdigit():
                target_ep = f"tcp://127.0.0.1:{target_ep}"
            elif not target_ep.startswith("tcp://") and not target_ep.startswith("ipc://"):
                target_ep = f"tcp://{target_ep}"
            if "localhost" in target_ep:
                target_ep = target_ep.replace("localhost", "127.0.0.1")
            try:
                sock.connect(target_ep)
            except Exception as e:
                print(f"[!] Warning: Failed connecting to {target_ep}: {e}", file=sys.stderr)

        while not self.shutdown_flag.is_set():
            try:
                frames = sock.recv_multipart()
                if not frames:
                    continue

                raw_bytes = None
                hex_data = ""
                rssi = None
                channel = None
                timestamp = None

                for frame in frames:
                    try:
                        text = frame.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        text = ""

                    if not text:
                        continue

                    # Check for JSON metadata
                    if text.startswith("{") and text.endswith("}"):
                        try:
                            meta = json.loads(text)
                            if rssi is None:
                                if "rssi" in meta and meta["rssi"] is not None:
                                    rssi = float(meta["rssi"])
                                elif "rssi_dbfs" in meta and meta["rssi_dbfs"] is not None:
                                    rssi = float(meta["rssi_dbfs"])
                            if channel is None and "channel" in meta and meta["channel"] is not None:
                                channel = int(meta["channel"])
                            if "hex" in meta or "raw_hex" in meta:
                                hex_data = str(meta.get("hex") or meta.get("raw_hex")).strip().upper()
                            continue
                        except Exception:
                            pass

                    # Check for RSSI tag
                    if rssi is None:
                        m_rssi = re.search(r"RSSI[:=\s]*(-?\d+(?:\.\d+)?)", text, re.I)
                        if m_rssi:
                            try:
                                rssi = float(m_rssi.group(1))
                            except ValueError:
                                pass

                    # Check for Channel tag
                    if channel is None:
                        m_ch = re.search(r"(?:CH|Channel)[:=\s]*(\d+)", text, re.I)
                        if m_ch:
                            try:
                                channel = int(m_ch.group(1))
                            except ValueError:
                                pass

                first_frame = frames[0]
                try:
                    txt = first_frame.decode("ascii", errors="strict").strip()
                    m_hex = re.match(r"^([0-9a-fA-F]{4,})", txt)
                    if m_hex and (len(txt) == len(m_hex.group(1)) or "RSSI" in txt):
                        clean_h = m_hex.group(1).upper()
                        if len(clean_h) % 2 == 0:
                            raw_bytes = bytes.fromhex(clean_h)
                            if not hex_data:
                                hex_data = clean_h
                except Exception:
                    pass

                if raw_bytes is None:
                    raw_bytes = first_frame
                    if not hex_data:
                        hex_data = raw_bytes.hex().upper()

                self.process_incoming_packet(
                    raw_bytes=raw_bytes,
                    channel=channel,
                    rssi=rssi,
                    timestamp=timestamp,
                    raw_hex=hex_data,
                )

            except zmq.Again:
                continue
            except Exception:
                if not self.shutdown_flag.is_set():
                    time.sleep(0.05)

        try:
            sock.close(linger=0)
        except Exception:
            pass

    def run_udp_receiver(self):
        """Background thread receiving UDP broadcast/datagram packets from Grabber."""
        if not self.udp_port:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        sock.bind(("0.0.0.0", self.udp_port))
        sock.settimeout(0.5)

        re_rssi = re.compile(rb"RSSI[:=\s]*(-?\d+(?:\.\d+)?)", re.I)
        re_ch = re.compile(rb"^CH(\d{3}):(?:RSSI=([+-]?\d+(?:\.\d+)?):)?")

        while not self.shutdown_flag.is_set():
            try:
                data, _ = sock.recvfrom(65535)
                if not data:
                    continue

                rssi = None
                channel = None
                raw_bytes = data

                m_ch = re_ch.match(data)
                if m_ch:
                    channel = int(m_ch.group(1))
                    if m_ch.group(2):
                        try:
                            rssi = float(m_ch.group(2))
                        except ValueError:
                            pass
                    raw_bytes = data[m_ch.end():]

                if b" " in raw_bytes or b"\n" in raw_bytes:
                    text = raw_bytes.decode("ascii", errors="ignore").strip()
                    m_r = re.search(r"RSSI[:=\s]*(-?\d+(?:\.\d+)?)", text, re.I)
                    if m_r and rssi is None:
                        try:
                            rssi = float(m_r.group(1))
                        except ValueError:
                            pass
                    cand_hex = re.sub(r"[^0-9a-fA-F]", "", text.split()[0] if text else "")
                    if cand_hex and len(cand_hex) % 2 == 0:
                        try:
                            raw_bytes = bytes.fromhex(cand_hex)
                        except Exception:
                            pass

                self.process_incoming_packet(
                    raw_bytes=raw_bytes,
                    channel=channel,
                    rssi=rssi,
                )
            except socket.timeout:
                continue
            except Exception:
                if not self.shutdown_flag.is_set():
                    time.sleep(0.05)

        try:
            sock.close()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Interactive CLI Command Dispatcher
    # -----------------------------------------------------------------------
    def dispatch_command(self, cmd: str):
        """Processes interactive terminal commands."""
        cmd = cmd.strip()
        if not cmd:
            return

        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Pause / Resume
        if action in ("p", "pause", "resume", "scroll"):
            self.toggle_pause("all")
        elif action in ("pk", "pause-known", "pause_known"):
            self.toggle_pause("known")
        elif action in ("pu", "pause-unknown", "pause_unknown"):
            self.toggle_pause("unknown")

        # Raw Hex Toggle
        elif action in ("x", "hex", "raw", "raw-hex", "raw_hex"):
            self.toggle_show_hex()
            state_str = "ON" if self.show_hex else "OFF"
            print(f"\n{self.colors.GREEN}[HEX] Raw Hex display is now {state_str}{self.colors.RESET}")

        # ASCII Display Toggle
        elif action in ("ak", "ascii-known", "ascii_known"):
            self.show_ascii_known = not self.show_ascii_known
            state_str = "ON" if self.show_ascii_known else "OFF"
            print(f"\n{self.colors.GREEN}[ASCII] Known ASCII display is now {state_str}{self.colors.RESET}")

        elif action in ("au", "ascii-unknown", "ascii_unknown"):
            self.show_ascii_unknown = not self.show_ascii_unknown
            state_str = "ON" if self.show_ascii_unknown else "OFF"
            print(f"\n{self.colors.GREEN}[ASCII] Unknown ASCII display is now {state_str}{self.colors.RESET}")

        elif action in ("a", "ascii"):
            if arg.lower() in ("u", "unk", "unknown"):
                self.show_ascii_unknown = not self.show_ascii_unknown
                state_str = "ON" if self.show_ascii_unknown else "OFF"
                print(f"\n{self.colors.GREEN}[ASCII] Unknown ASCII display is now {state_str}{self.colors.RESET}")
            else:
                self.show_ascii_known = not self.show_ascii_known
                state_str = "ON" if self.show_ascii_known else "OFF"
                print(f"\n{self.colors.GREEN}[ASCII] Known ASCII display is now {state_str}{self.colors.RESET}")

        # Rules Reload on the Fly
        elif action in ("r", "reload", "rules"):
            self.reload_rules(arg or None)

        # Filters
        elif action in ("fk", "filter-known", "filter_known"):
            self.set_filter(arg, target="known")
        elif action in ("fu", "filter-unknown", "filter_unknown"):
            self.set_filter(arg, target="unknown")
        elif action in ("fc", "filter-channel", "filter_channel", "channel-filter", "channel_filter", "channels"):
            self.set_channel_filter(arg)
        elif action in ("f", "filter"):
            self.set_filter(arg, target="both")

        # Stop RegEx (Pause-On-Match Breakpoint)
        elif action in ("tk", "trigger-known", "stop-known", "stop_known"):
            self.set_pause_on(arg, target="known")
        elif action in ("tu", "trigger-unknown", "stop-unknown", "stop_unknown"):
            self.set_pause_on(arg, target="unknown")
        elif action in ("t", "trigger", "stop", "pause-on", "pause_on"):
            self.set_pause_on(arg, target="both")

        # Case sensitivity
        elif action in ("c", "case"):
            self.toggle_case_sensitivity()

        # DVR Export
        elif action in ("dk", "dvr-known", "dvr_known"):
            self.save_dvr_known(arg or None)
        elif action in ("du", "dvr-unknown", "dvr_unknown"):
            self.save_dvr_unknown(arg or None)
        elif action in ("d", "dvr", "save"):
            self.save_dvr(arg or None)

        # Logging
        elif action in ("l", "log"):
            self.toggle_logging(arg or None)

        # Status / Stats
        elif action in ("s", "status", "info"):
            self.print_status()

        # Help
        elif action in ("h", "help", "?"):
            self.print_help()

        # Quit
        elif action in ("q", "quit", "exit"):
            self.shutdown_flag.set()

        else:
            print(f"\n{self.colors.RED}[!] Unknown command '{cmd}'. Type 'help' for available commands.{self.colors.RESET}")

    def print_status(self):
        """Incorporate status messages into the known messages scroll view and pause for review."""
        cols = self.colors
        ppm_tot = self.calculate_pack_min(self.rate_window_total)
        ppm_k = self.calculate_pack_min(self.rate_window_known)
        ppm_u = self.calculate_pack_min(self.rate_window_unknown)
        log_st = f"ACTIVE ({self.log_filepath}, {self.logged_count} written)" if self.log_file_handle else "OFF"

        stat_lines = [
            f"=== [STATUS REPORT] ApexBOSSpy ({VERSION}) ===",
            f" * Endpoints      : {', '.join(self.endpoints)}",
            f" * Packets/Min    : Total: {ppm_tot:.1f} Pkts/m | Known: {ppm_k:.1f} | Unknown: {ppm_u:.1f}",
            f" * Lifetime Total : {self.total_packets_received:,} packets (Known: {self.parsed_packets_count:,}, Unk: {self.total_unknown_count:,})",
            f" * Filters        : Known: '{self.filter_known_str or 'ALL'}' | Unk: '{self.filter_unknown_str or 'ALL'}' | Ch: '{self.filter_channel_str or 'ALL'}'",
            f" * Parsers / DVR  : Parsers: {len(self.rules_registry.parsers)} | DVR: K:{len(self.dvr_known)}/{self.dvr_size} U:{len(self.dvr_unknown)}/{self.dvr_size}",
            f" * Flags / Log    : Hex: {'ON' if self.show_hex else 'OFF'} | AsciiK: {'ON' if self.show_ascii_known else 'OFF'} | Log: {log_st}",
            f" * Stop Triggers  : Known: '{self.pause_on_known_str or 'None'}' | Unk: '{self.pause_on_unknown_str or 'None'}'",
            f"==================================================",
        ]

        with self._display_lock:
            for sl in stat_lines:
                self.display_known.append(SpyStatusMessage(sl, cols.CYAN))

        # Stream mode or non-interactive fallback
        if self.stream_mode or not sys.stdout.isatty():
            for sl in stat_lines:
                sys.stdout.write(f"{cols.CYAN}{sl}{cols.RESET}\n")
            sys.stdout.flush()

        # Pause scrolling for a few seconds to allow user review
        if hasattr(self, "_status_unpause_cancel") and self._status_unpause_cancel:
            self._status_unpause_cancel.set()
        self._status_unpause_cancel = threading.Event()

        self.is_paused = True
        self.is_paused_known = True
        self.is_paused_unknown = True
        self.pause_reason_known = "Status review"
        self.pause_reason_unknown = "Status review"
        self.trigger_alert = "[STATUS] Paused for 5s for review (press 'p' or space to toggle)"

        def _unpause_worker():
            if not self._status_unpause_cancel.wait(5.0):
                if self.pause_reason_known == "Status review":
                    self.is_paused = False
                    self.is_paused_known = False
                    self.is_paused_unknown = False
                    self.pause_reason_known = ""
                    self.pause_reason_unknown = ""
                    if "[STATUS]" in self.trigger_alert:
                        self.trigger_alert = ""

        self._status_timer = threading.Thread(target=_unpause_worker, daemon=True, name="StatusUnpauseTimer")
        self._status_timer.start()

    def print_help(self):
        """Displays interactive command reference."""
        cols = self.colors
        print(f"\n{cols.YELLOW}--- Interactive Commands ---{cols.RESET}")
        print("  p, space        : Toggle Pause / Resume scrolling for ALL panels")
        print("  pk, pu          : Toggle Pause / Resume for Known or Unknown panel")
        print("  x               : Toggle showing raw hex string line slightly offset")
        print("  a               : Toggle showing extracted ASCII text line for all packets")
        print("  ak, au          : Toggle showing extracted ASCII text line for Known or Unknown panel")
        print("  r [path]        : Reload packet rules on the fly (or auto-reloads when files change)")
        print("  fk <regex>      : Edit/set RegEx filter for Known packets (pre-fills existing; empty clears)")
        print("  fu <regex>      : Edit/set RegEx filter for Unknown packets (pre-fills existing; empty clears)")
        print("  fc <regex>      : Edit/set shared Channel number RegEx filter for BOTH panels (empty clears)")
        print("  f <regex>       : Edit/set RegEx filter for both panels")
        print("  tk <regex>      : Set Stop RegEx trigger for Known packets")
        print("  tu <regex>      : Set Stop RegEx trigger for Unknown packets")
        print("  c               : Toggle case-sensitivity for all filters and triggers")
        print("  dk [filename]   : Save Known DVR buffer to file")
        print("  du [filename]   : Save Unknown DVR buffer to file")
        print("  d [filename]    : Save Both DVR buffers to file")
        print("  l [filename]    : Toggle continuous file logging ON / OFF")
        print("  s, status       : Show detailed statistics report")
        print("  q, quit         : Exit ApexBOSSpy cleanly")
        print(f"{cols.YELLOW}----------------------------{cols.RESET}\n")

    # -----------------------------------------------------------------------
    # Lifecycle Management
    # -----------------------------------------------------------------------
    def start(self):
        """Starts background ingestion threads and the TUI display loop."""
        # Start ZMQ receiver thread
        self.zmq_thread = threading.Thread(target=self.run_zmq_receiver, daemon=True, name="ZmqReceiver")
        self.zmq_thread.start()

        # Start UDP receiver thread if configured
        if self.udp_port:
            self.udp_thread = threading.Thread(target=self.run_udp_receiver, daemon=True, name="UdpReceiver")
            self.udp_thread.start()

        # Start keyboard listener
        self.cmd_listener = TerminalCommandListener(self.dispatch_command, self.shutdown_flag, app=self)
        self.cmd_listener.start()

        # Start TUI renderer thread if in interactive TTY mode
        if sys.stdout.isatty() and not self.stream_mode:
            self.tui_thread = threading.Thread(target=self.run_tui_refresh_loop, daemon=True, name="TuiRenderer")
            self.tui_thread.start()
        else:
            print(f"[*] ApexBOSSpy connected to {', '.join(self.endpoints)}. Streaming incoming packets...")

        try:
            while not self.shutdown_flag.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.shutdown_flag.set()

        self.cleanup()

    def cleanup(self):
        """Shuts down all threads, sockets, and open log files cleanly."""
        self.shutdown_flag.set()
        with self._log_lock:
            if self.log_file_handle:
                try:
                    self.log_file_handle.flush()
                    self.log_file_handle.close()
                except Exception:
                    pass
                self.log_file_handle = None
        print(f"\n{self.colors.CYAN}[*] ApexBOSSpy terminated cleanly.{self.colors.RESET}")


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------
def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ApexBOSSpy",
        description="ApexBOSSpy - Dual-Panel Real-Time Packet Spy & Rule Inspector for ApexBOSGrabber",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ApexBOSSpy.py
  python ApexBOSSpy.py --host 192.168.1.50
  python ApexBOSSpy.py --connect 192.168.1.50:20101,20113,20131
  python ApexBOSSpy.py --host 192.168.1.50 --connect 20101,20113
  python ApexBOSSpy.py --filter-known "UTC" --filter-unknown "RAW"
  python ApexBOSSpy.py --filter-channel "^10[12]$"
  python ApexBOSSpy.py --pause-on-known "18433" --show-hex
  python ApexBOSSpy.py --dvr-size 5000 --log captured_bursts.log
  python ApexBOSSpy.py --stream  # Line-by-line stream mode
        """,
    )

    parser.add_argument(
        "--connect", "-s",
        default="tcp://127.0.0.1:5555",
        help="ZeroMQ publisher endpoint(s), host:ports, or port list (e.g. '192.168.1.50:20101,20113,20131', '20101,20113', or 'tcp://host:5555')",
    )
    parser.add_argument(
        "--host", "-H",
        default=None,
        help="Remote host name or IP address running Grabber (default: 127.0.0.1). Used when specifying lists of ports in --connect or connecting to remote procs.",
    )
    parser.add_argument(
        "--udp", "-u",
        type=int,
        default=None,
        help="Optional local UDP port to listen for Grabber UDP datagram broadcasts",
    )
    parser.add_argument(
        "--rules", "-r",
        nargs="+",
        default=None,
        help="Path(s) or glob patterns to packet rules JSON files (default: auto-detect config/*rule*.json)",
    )
    parser.add_argument(
        "--filter", "-f",
        default="",
        help="RegEx filter for Known packets (or both if -fk/-fu not specified)",
    )
    parser.add_argument(
        "--filter-known", "-fk",
        default="",
        help="RegEx filter specifically for Known packets panel",
    )
    parser.add_argument(
        "--filter-unknown", "-fu",
        default="",
        help="RegEx filter specifically for Unknown packets panel",
    )
    parser.add_argument(
        "--filter-channel", "--channel-filter", "--channels", "-fc",
        default="",
        dest="filter_channel",
        help="Shared RegEx filter for channel numbers across BOTH Known and Unknown panels (default: empty = all channels)",
    )
    parser.add_argument(
        "--case-sensitive", "-c",
        action="store_true",
        default=False,
        help="Enable case-sensitive evaluation for RegEx filters (default: case-insensitive)",
    )
    parser.add_argument(
        "--pause-on", "--trigger", "-t",
        default=None,
        dest="pause_on",
        help="Stop RegEx trigger for Known packets: automatically pauses scrolling when matching",
    )
    parser.add_argument(
        "--pause-on-known", "-tk",
        default=None,
        dest="pause_on_known",
        help="Stop RegEx trigger specifically for Known packets panel",
    )
    parser.add_argument(
        "--pause-on-unknown", "-tu",
        default=None,
        dest="pause_on_unknown",
        help="Stop RegEx trigger specifically for Unknown packets panel",
    )
    parser.add_argument(
        "--show-hex", "-x",
        action="store_true",
        default=False,
        help="Show raw hex string on the next line slightly offset",
    )
    parser.add_argument(
        "--show-ascii-known", "--ascii-known", "-ak",
        action="store_true",
        default=False,
        dest="show_ascii_known",
        help="Show extracted ASCII text line for known packets (default: off)",
    )
    parser.add_argument(
        "--no-ascii", "--no-ascii-unknown",
        action="store_true",
        default=False,
        dest="no_ascii",
        help="Disable displaying extracted ASCII text line for unknown packets",
    )
    parser.add_argument(
        "--dvr-size", "-n",
        type=int,
        default=2000,
        help="Capacity of the DVR ring buffer per panel in messages (default: 2000)",
    )
    parser.add_argument(
        "--log", "-l",
        default=None,
        help="File path to start logging filtered messages immediately upon startup",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="Stream packets line-by-line instead of rendering full-screen dual-panel TUI",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI terminal colors and text formatting",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"ApexBOSSpy {VERSION}",
    )
    return parser


def main():
    parser = build_cli_parser()
    args = parser.parse_args()

    # Parse endpoints with remote host support
    endpoints = parse_connection_endpoints(args.connect, host_arg=args.host)

    spy = ApexBOSSpy(
        endpoints=endpoints,
        host=args.host,
        udp_port=args.udp,
        rule_paths=args.rules,
        regex_filter=args.filter,
        filter_known=args.filter_known,
        filter_unknown=args.filter_unknown,
        filter_channel=args.filter_channel,
        case_sensitive=args.case_sensitive,
        pause_on_regex=args.pause_on,
        pause_on_known=args.pause_on_known,
        pause_on_unknown=args.pause_on_unknown,
        dvr_size=args.dvr_size,
        log_file=args.log,
        show_hex=args.show_hex,
        show_ascii_known=args.show_ascii_known,
        show_ascii_unknown=not args.no_ascii,
        stream_mode=args.stream,
        no_color=args.no_color,
    )

    def sig_handler(sig, frame):
        spy.shutdown_flag.set()

    signal.signal(signal.SIGINT, sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, sig_handler)

    spy.start()


if __name__ == "__main__":
    main()
