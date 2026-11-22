"""
config_loader.py - ApexBOS Unified Configuration & Service Manager (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Provides unified configuration loading, channel allocation across multiple
processes, IP version auto-detection, legacy config file synchronization,
and OS service automation for Windows, Linux, and macOS.
"""

import os
import re
import sys
import copy
import json
import base64
import socket
import secrets
import ipaddress
import subprocess

# Python 3.6 subprocess.run compatibility (capture_output and text were added in 3.7)
if sys.version_info < (3, 7):
    _orig_subprocess_run = subprocess.run

    def _compat_subprocess_run(*args, **kwargs):
        if "capture_output" in kwargs:
            if kwargs.pop("capture_output"):
                kwargs.setdefault("stdout", subprocess.PIPE)
                kwargs.setdefault("stderr", subprocess.PIPE)
        if "text" in kwargs:
            text_val = kwargs.pop("text")
            if "universal_newlines" not in kwargs:
                kwargs["universal_newlines"] = text_val
        return _orig_subprocess_run(*args, **kwargs)

    subprocess.run = _compat_subprocess_run
import shutil
import importlib.util
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set

from version import VERSION

CONFIG_FILENAME = "config.json"
CONFIG_DEFAULTS_FILENAME = "config.defaults.json"
INSTALL_RECORD_FILENAME = "install_record.json"
# List-valued keys whose items are merged (union) during configuration migration.
MERGE_LIST_KEYS = {"parsers", "rules"}
# Windows / Unix scheduled service identifiers used by boot automation + uninstall.
WINDOWS_TASK_NAMES = {"grabber": "ApexBOS_Grabber", "flux": "ApexBOS_Flux"}
DEFAULT_CHANNELS = [
    77, 81, 85, 89, 91, 92, 93, 101, 102, 107, 108, 113, 114,
    124, 125, 126, 127, 128, 131, 132, 134, 141, 142, 147,
    153, 154, 165, 166, 167, 171, 172,
]
DEFAULT_CHANNELS_STR = ", ".join(str(c) for c in DEFAULT_CHANNELS)
DEFAULT_BASE_ZMQ_PORT = 20101
DEFAULT_LAYOUT = "layouts/arrow_layout.json"
DEFAULT_ENGINE_PARSERS = [
    "x33_rule.json",
    "x2_base_rules.json",
    "x01_rules.json",
    "x05_rules.json",
    "x70_rules.json",
    "x06_rules.json",
    "x04_rules.json",
]
DEFAULT_SPY_RULES = [
    "x33_rule.json",
    "x2_base_rules.json",
    "x01_rules.json",
    "x05_rules.json",
    "x70_rules.json",
    "x06_rules.json",
    "x04_rules.json",
]



def itc_channel_frequency(channel_num: int) -> int:
    """ITC frequency formula: 217 587 500 + (channel# * 25 000)."""
    return 217587500 + int(channel_num) * 25000


def distribute_channels_across_workers(channel_list: List[int], worker_count: int) -> List[List[int]]:
    """Distributes channels evenly across worker processes matching ApexBOSGrabber logic."""
    if worker_count <= 0:
        return []
    sorted_unique = sorted(set(int(ch) for ch in channel_list))
    buckets = [[] for _ in range(worker_count)]
    for idx, ch in enumerate(sorted_unique):
        buckets[idx % worker_count].append(ch)
    return buckets


def calculate_grabber_procs(channel_list: List[int], process_count: int) -> Tuple[List[int], List[List[int]]]:
    """
    Calculates worker ports and channel distributions for a single Grabber process:
    Port for each process = 18000 + channel_no of the first channel for that process.
    """
    procs = max(1, int(process_count))
    buckets = distribute_channels_across_workers(channel_list, procs)
    ports = []
    used_ports = set()
    for idx, b in enumerate(buckets):
        first_ch = b[0] if b else (101 + idx * 4)
        p = 18000 + first_ch
        while p in used_ports:
            p += 1
        used_ports.add(p)
        ports.append(p)
    return ports, buckets


def get_max_allowed_procs() -> int:
    """
    Returns allowed Grabber worker processes: number of CPU threads minus 1,
    no less than 1. Uses 12 if unable to determine CPU threads.
    """
    try:
        threads = os.cpu_count()
        if threads is not None and threads > 0:
            return max(1, threads - 1)
    except Exception:
        pass
    return 12


def parse_channels_with_locks(chans_input: Any) -> Tuple[str, List[int], Set[int]]:
    """
    Parses channel input (string, list, or range) supporting suffixes '.', 'l', 'p' for locking.
    Returns:
      (raw_chans_str, sorted_unique_channels_list, set_of_locked_channels)
    """
    if chans_input is None:
        chans_input = DEFAULT_CHANNELS

    if isinstance(chans_input, (list, tuple, set)):
        raw_items = [str(x).strip() for x in chans_input if str(x).strip()]
        raw_str = ", ".join(raw_items)
    else:
        raw_str = str(chans_input).strip()
        raw_items = [x.strip() for x in raw_str.split(",") if x.strip()]

    channel_set: Set[int] = set()
    locked_set: Set[int] = set()

    for item in raw_items:
        if not item:
            continue
        if "-" in item:
            parts = item.split("-", 1)
            s_str, e_str = parts[0].strip(), parts[1].strip()
            m_s = re.match(r"^(\d+)\s*([lpLP\.]+)?$", s_str)
            m_e = re.match(r"^(\d+)\s*([lpLP\.]+)?$", e_str)
            if m_s and m_e:
                s_ch, e_ch = int(m_s.group(1)), int(m_e.group(1))
                is_lock = bool(m_s.group(2) or m_e.group(2))
                rng = list(range(min(s_ch, e_ch), max(s_ch, e_ch) + 1))
                channel_set.update(rng)
                if is_lock:
                    locked_set.update(rng)
        else:
            m = re.match(r"^(\d+)\s*([lpLP\.]+)?$", item)
            if m:
                ch = int(m.group(1))
                is_lock = bool(m.group(2))
                channel_set.add(ch)
                if is_lock:
                    locked_set.add(ch)
            else:
                digits = re.sub(r"[^\d]", "", item)
                if digits:
                    ch = int(digits)
                    is_lock = bool(re.search(r"[lpLP\.]+$", item))
                    channel_set.add(ch)
                    if is_lock:
                        locked_set.add(ch)

    if not channel_set:
        channel_set = set(DEFAULT_CHANNELS)
        raw_str = DEFAULT_CHANNELS_STR

    sorted_channels = sorted(channel_set)
    return raw_str, sorted_channels, locked_set


def generate_default_ports(channel_list: Any, process_count: int) -> List[int]:
    """Generates default unique network ports for process_count worker processes."""
    procs = max(1, int(process_count))
    if isinstance(channel_list, str) or any(not isinstance(c, int) for c in (channel_list or [])):
        _, chans, _ = parse_channels_with_locks(channel_list)
    else:
        chans = list(channel_list) if channel_list else DEFAULT_CHANNELS

    buckets = distribute_channels_across_workers(chans, procs)
    ports: List[int] = []
    used_ports: Set[int] = set()

    for idx, b in enumerate(buckets):
        first_ch = b[0] if b else (101 + idx * 4)
        p = 18000 + first_ch
        while p in used_ports:
            p += 1
        used_ports.add(p)
        ports.append(p)

    while len(ports) < procs:
        next_p = max(ports) + 1 if ports else 18071
        while next_p in used_ports:
            next_p += 1
        used_ports.add(next_p)
        ports.append(next_p)

    return ports[:procs]


def partition_channels(channel_list: List[int], process_count: int, base_zmq_port: int = DEFAULT_BASE_ZMQ_PORT) -> List[Dict[str, Any]]:
    """
    Partitions a sorted channel list across N processes using frequency-contiguous
    clustering so each process monitors a minimal RF bandwidth span suitable for SDRs.
    """
    procs = max(1, int(process_count))
    sorted_unique = sorted(set(int(ch) for ch in channel_list))
    total_chans = len(sorted_unique)
    if procs > total_chans:
        procs = total_chans

    # Calculate partition slice sizes
    base_size = total_chans // procs
    rem = total_chans % procs

    result = []
    idx = 0
    for p_i in range(procs):
        chunk_len = base_size + (1 if p_i < rem else 0)
        chunk = sorted_unique[idx:idx + chunk_len]
        idx += chunk_len

        freqs = [itc_channel_frequency(c) for c in chunk]
        min_f = min(freqs) if freqs else 0
        max_f = max(freqs) if freqs else 0
        span_hz = max_f - min_f
        center_f = (min_f + max_f) // 2 if freqs else 0
        port = base_zmq_port + p_i

        result.append({
            "index": p_i,
            "device": p_i,
            "zmq_port": port,
            "channels": chunk,
            "center_freq_hz": center_f,
            "span_hz": span_hz,
            "span_mhz": round(span_hz / 1e6, 3),
        })
    return result


def detect_ip_version(host_str: str) -> str:
    """
    Analyzes host/IP string and detects whether it is IPv4 or IPv6.
    Resolves hostnames to detect remote address family.
    """
    clean = str(host_str or "").strip()
    if clean.startswith("[") and "]" in clean:
        clean = clean[1:clean.index("]")]
    elif ":" in clean and clean.count(":") == 1:
        clean = clean.split(":")[0]

    try:
        ip = ipaddress.ip_address(clean)
        return "ipv6" if ip.version == 6 else "ipv4"
    except ValueError:
        pass

    # Hostname: resolve via getaddrinfo
    try:
        infos = socket.getaddrinfo(clean, None)
        for family, _, _, _, _ in infos:
            if family == socket.AF_INET6:
                return "ipv6"
        return "ipv4"
    except Exception:
        return "ipv4"


def format_endpoint(host: str, port: int, ip_version: Optional[str] = None) -> str:
    """Formats an endpoint host:port, enclosing IPv6 addresses in brackets."""
    h = str(host).strip()
    ver = ip_version or detect_ip_version(h)
    clean_h = h.strip("[]")
    if ver == "ipv6" and not h.startswith("["):
        return f"[{clean_h}]:{port}"
    return f"{clean_h}:{port}"


# ---------------------------------------------------------------------------
# Security: Authentication Secret & Encryption Key (generated at setup, never packaged)
# ---------------------------------------------------------------------------

def generate_encryption_key() -> str:
    """Generates a fresh Fernet-compatible encryption key (44-char urlsafe base64 of 32 random bytes)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def is_valid_encryption_key(key: Any) -> bool:
    """True if `key` is a valid Fernet key (urlsafe base64 encoding of exactly 32 bytes)."""
    try:
        k = str(key).strip()
        return len(k) == 44 and len(base64.urlsafe_b64decode(k.encode("ascii"))) == 32
    except Exception:
        return False


def generate_auth_secret(nbytes: int = 24) -> str:
    """Generates a random URL-safe authentication secret."""
    return secrets.token_urlsafe(nbytes)


def generate_unique_identity(prefix: str = "ApexNode") -> str:
    """
    Generates a cryptographically random, collision-resistant node identity
    (e.g. ApexNode_3f8a1c92) so that fresh installations do not repeat ApexNode_01.
    """
    pfx = (prefix or "ApexNode").rstrip("_")
    return f"{pfx}_{secrets.token_hex(4)}"


MIN_AUTH_SECRET_LEN = 8


def apply_secrets(cfg: Dict[str, Any], auth_secret: str, encryption_key: str) -> Dict[str, Any]:
    """Writes the shared auth_secret / encryption_key into every section of a unified config that uses them."""
    cfg.setdefault("common", {}).setdefault("banc", {})
    targets = [
        cfg["common"]["banc"],
        cfg.get("flux"),
        cfg.get("banc"),
        (cfg.get("engine") or {}).get("connections"),
        cfg.get("viewer"),
        cfg.get("creator"),
    ]
    for sect in targets:
        if isinstance(sect, dict):
            sect["auth_secret"] = str(auth_secret)
            sect["encryption_key"] = str(encryption_key)
    return cfg


def _canonical_secrets(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Finds the first non-empty auth_secret / encryption_key in a unified config."""
    sections = [
        (cfg.get("common") or {}).get("banc"),
        cfg.get("flux"),
        cfg.get("banc"),
        (cfg.get("engine") or {}).get("connections"),
        cfg.get("viewer"),
        cfg.get("creator"),
    ]
    auth, key = "", ""
    for s in sections:
        if isinstance(s, dict):
            auth = auth or str(s.get("auth_secret") or "")
            key = key or str(s.get("encryption_key") or "")
    return auth, key


# ---------------------------------------------------------------------------
# SDR Interface Detection & User-Supplied Paths (rtl_tcp / rtl_sdr / rtlsdr DLLs)
# ---------------------------------------------------------------------------

SDR_LIBRARY_NAMES = ("rtlsdr.dll", "librtlsdr.so", "librtlsdr.so.0", "librtlsdr.dylib", "librtlsdr.0.dylib")


def _exe_name(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def inspect_sdr_path(path: Any) -> Dict[str, Any]:
    """
    Interprets a user-supplied path: an rtl_tcp / rtl_sdr executable, an rtlsdr library file,
    or a folder containing any of them (also scans its 'x64' and 'bin' sub-folders).
    Returns {"rtl_tcp_bin", "rtl_sdr_bin", "rtlsdr_dll_dir", "error"}.
    """
    res: Dict[str, Any] = {"rtl_tcp_bin": None, "rtl_sdr_bin": None, "rtlsdr_dll_dir": None, "error": None}
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        res["error"] = "No path entered."
        return res
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if not os.path.exists(p):
        res["error"] = f"Path not found: {p}"
        return res

    if os.path.isfile(p):
        name = os.path.basename(p).lower()
        base_dir = os.path.dirname(p)
        if name.startswith("rtl_tcp"):
            res["rtl_tcp_bin"] = p
        elif name.startswith("rtl_sdr"):
            res["rtl_sdr_bin"] = p
        elif name.startswith(("rtlsdr", "librtlsdr")):
            res["rtlsdr_dll_dir"] = base_dir
        else:
            res["error"] = f"'{os.path.basename(p)}' is not rtl_tcp, rtl_sdr or an rtlsdr library."
            return res
        scan_dirs = [base_dir]
    else:
        scan_dirs = [p, os.path.join(p, "x64"), os.path.join(p, "bin")]

    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for key, base in (("rtl_tcp_bin", "rtl_tcp"), ("rtl_sdr_bin", "rtl_sdr")):
            if not res[key]:
                cand = os.path.join(d, _exe_name(base))
                if os.path.isfile(cand):
                    res[key] = cand
        if not res["rtlsdr_dll_dir"]:
            if any(os.path.isfile(os.path.join(d, lib)) for lib in SDR_LIBRARY_NAMES):
                res["rtlsdr_dll_dir"] = d

    if not (res["rtl_tcp_bin"] or res["rtl_sdr_bin"] or res["rtlsdr_dll_dir"]) and not res["error"]:
        res["error"] = f"No rtl_tcp, rtl_sdr or rtlsdr library found in: {p}"
    return res


def apply_rtlsdr_dll_dir(path: Optional[str]):
    """Makes an rtlsdr / libusb library folder visible to this process (PATH + Windows DLL search)."""
    if not path or not os.path.isdir(path):
        return
    path = os.path.abspath(path)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if path not in parts:
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            _DLL_HANDLES.append(os.add_dll_directory(path))
        except OSError:
            pass


_DLL_HANDLES: List[Any] = []


def detect_sdr_environment(dll_dir: Optional[str] = None) -> Dict[str, Any]:
    """Detects which SDR interfaces are available on this machine (PATH lookup + pyrtlsdr)."""
    apply_rtlsdr_dll_dir(dll_dir)
    env: Dict[str, Any] = {
        "rtl_sdr_on_path": shutil.which(_exe_name("rtl_sdr")),
        "rtl_tcp_on_path": shutil.which(_exe_name("rtl_tcp")),
        "pyrtlsdr_ok": False,
        "pyrtlsdr_error": None,
    }
    if importlib.util.find_spec("rtlsdr") is None:
        env["pyrtlsdr_error"] = "Python package 'pyrtlsdr' is not installed"
    else:
        try:
            from rtlsdr import RtlSdr  # noqa: F401
            env["pyrtlsdr_ok"] = True
        except Exception as exc:
            env["pyrtlsdr_error"] = f"rtlsdr library failed to load: {exc}"
    return env


def build_sdr_settings(user_path: Optional[str] = None, env: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Builds the Grabber SDR-interface settings from what the user specified (path to rtl_tcp/rtl_sdr
    or a rtlsdr DLL folder) and what is auto-detected on PATH.
    Returns {"mode", "rtl_sdr_bin", "rtl_tcp_bin", "rtlsdr_dll_dir", "error"}; only user-supplied
    paths are stored (PATH-resolved tools are looked up at run time).
    """
    sdr: Dict[str, Any] = {"mode": "auto", "rtl_sdr_bin": None, "rtl_tcp_bin": None, "rtlsdr_dll_dir": None, "error": None}
    user: Dict[str, Any] = {}
    if user_path:
        user = inspect_sdr_path(user_path)
        sdr["error"] = user.get("error")
        for k in ("rtl_sdr_bin", "rtl_tcp_bin", "rtlsdr_dll_dir"):
            sdr[k] = user.get(k)
    env = env if env is not None else detect_sdr_environment(sdr["rtlsdr_dll_dir"])

    if user.get("rtl_tcp_bin"):
        sdr["mode"] = "tcp"
    elif user.get("rtlsdr_dll_dir"):
        sdr["mode"] = "rtlsdr"
    elif user.get("rtl_sdr_bin"):
        sdr["mode"] = "pipe"
    elif env.get("rtl_sdr_on_path"):
        sdr["mode"] = "pipe"
    elif env.get("rtl_tcp_on_path"):
        sdr["mode"] = "tcp"
    elif env.get("pyrtlsdr_ok"):
        sdr["mode"] = "rtlsdr"
    return sdr


def _sdr_from_cfg(grabber_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mode": grabber_cfg.get("sdr_mode", "auto") or "auto",
        "rtl_sdr_bin": grabber_cfg.get("rtl_sdr_bin"),
        "rtl_tcp_bin": grabber_cfg.get("rtl_tcp_bin"),
        "rtlsdr_dll_dir": grabber_cfg.get("rtlsdr_dll_dir"),
        "rtl_tcp_host": grabber_cfg.get("rtl_tcp_host", "127.0.0.1"),
        "rtl_tcp_port": int(grabber_cfg.get("rtl_tcp_port", 1234)),
    }


def generate_unified_config(
    deployment_mode: str = "cloud",  # "local", "cloud", or "engines_viewers"
    banc_location: str = "remote",   # "local" or "remote"
    banc_host: str = "127.0.0.1",
    banc_port: int = 23009,
    banc_ui_port: int = 23010,
    auth_secret: str = "",
    encryption_key: str = "",
    identity: Optional[str] = None,
    process_count: int = 2,
    channels: Optional[Any] = None,
    ports: Optional[List[int]] = None,
    sample_rate: int = 2048000,
    device_index: int = 0,
    ppm: int = 0,
    gain: str = "auto",
    sdr: Optional[Dict[str, Any]] = None,
    packet_source: str = "flux",     # "flux" or "banc"
    flux_mcast_group: Optional[str] = "239.1.1.1",
    flux_mcast_port: Optional[int] = 23003,
    burst_max_packets: int = 100,
    burst_interval_sec: int = 120,
    banc_initial_history_sec: int = 60,
    sdr_mode: Optional[str] = None,
    rtl_tcp_host: Optional[str] = None,
    rtl_tcp_port: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generates the unified configuration dictionary.
    auth_secret / encryption_key are intentionally empty by default: they are chosen/generated
    by the setup wizard and are never shipped in the distribution.
    identity is uniquely generated per installation so that nodes do not collide on Banc.
    """
    raw_chans_str, clean_channels, locked_channels = parse_channels_with_locks(channels)
    ip_ver = detect_ip_version(banc_host)
    sdr = sdr or {"mode": "auto", "rtl_sdr_bin": None, "rtl_tcp_bin": None, "rtlsdr_dll_dir": None}
    chosen_sdr_mode = str(sdr_mode if sdr_mode is not None else (sdr.get("mode") if sdr else "auto") or "auto").strip().lower()
    chosen_tcp_host = str(rtl_tcp_host if rtl_tcp_host is not None else ((sdr.get("rtl_tcp_host") if sdr else None) or "127.0.0.1")).strip()
    try:
        chosen_tcp_port = int(rtl_tcp_port if rtl_tcp_port is not None else ((sdr.get("rtl_tcp_port") if sdr else None) or 1234))
    except (ValueError, TypeError):
        chosen_tcp_port = 1234

    if sdr is not None and isinstance(sdr, dict):
        sdr["mode"] = chosen_sdr_mode
        sdr["rtl_tcp_host"] = chosen_tcp_host
        sdr["rtl_tcp_port"] = chosen_tcp_port
    device_index = max(0, int(device_index))
    if not identity or identity == "ApexNode_01":
        node_identity = generate_unique_identity()
    else:
        node_identity = str(identity).strip()

    procs = max(1, int(process_count)) if process_count else 2
    if ports:
        valid_ports = []
        for p in ports:
            try:
                ip = int(p)
                if 1024 <= ip <= 65535:
                    valid_ports.append(ip)
            except (ValueError, TypeError):
                pass
        if len(valid_ports) < procs:
            fallback = generate_default_ports(clean_channels, procs)
            for fb in fallback:
                if fb not in valid_ports:
                    valid_ports.append(fb)
                if len(valid_ports) == procs:
                    break
        worker_ports = valid_ports[:procs]
    else:
        worker_ports = generate_default_ports(clean_channels, procs)

    procs_arg = ",".join(str(p) for p in worker_ports)
    flux_sources_str = ", ".join(f"127.0.0.1:{p}" for p in worker_ports)

    # Note: Grabber will automatically re-arrange channels for each proc at runtime,
    # so there is no need to create a config of channels per proc.
    proc_allocations = []
    for idx, port in enumerate(worker_ports):
        proc_allocations.append({
            "index": idx,
            "device": device_index,
            "ppm": int(ppm),
            "gain": str(gain) if gain is not None else "auto",
            "zmq_port": port,
        })

    dep_mode_low = str(deployment_mode or "").lower().strip()
    is_engines_viewers = dep_mode_low in ("engines_viewers", "client", "display", "viewer_only", "engine_viewer")

    if is_engines_viewers:
        source_low = str(packet_source or "flux").lower().strip()
        is_banc_source = (source_low == "banc")
        is_cloud_enabled = is_banc_source
        banc_target_host = banc_host if is_banc_source else "none"
        banc_target_port = int(banc_port) if is_banc_source else 23009
        flux_cloud_server = banc_target_host if is_banc_source else "None"
        flux_cloud_port = banc_target_port if is_banc_source else 0
        engine_banc_host = banc_target_host if is_banc_source else "none"
        engine_banc_port = banc_target_port if is_banc_source else "none"
        if is_banc_source:
            engine_mcast_grp = None
            engine_mcast_prt = None
            flux_grp_val = None
            flux_prt_val = None
        else:
            engine_mcast_grp = str(flux_mcast_group) if flux_mcast_group is not None else "239.1.1.1"
            engine_mcast_prt = int(flux_mcast_port) if flux_mcast_port is not None else 23003
            flux_grp_val = engine_mcast_grp
            flux_prt_val = engine_mcast_prt
        saved_dep_mode = "engines_viewers"
        saved_packet_source = "banc" if is_banc_source else "flux"
        banc_loc_val = banc_location if is_banc_source else "none"
    else:
        is_cloud_enabled = (dep_mode_low == "cloud")
        banc_target_host = banc_host if is_cloud_enabled else "127.0.0.1"
        banc_target_port = int(banc_port)
        flux_cloud_server = banc_target_host if is_cloud_enabled else "None"
        flux_cloud_port = banc_target_port if is_cloud_enabled else 0
        engine_banc_host = banc_target_host if is_cloud_enabled else "none"
        engine_banc_port = banc_target_port if is_cloud_enabled else "none"
        engine_mcast_grp = str(flux_mcast_group) if flux_mcast_group is not None else "239.1.1.1"
        engine_mcast_prt = int(flux_mcast_port) if flux_mcast_port is not None else 23003
        flux_grp_val = engine_mcast_grp
        flux_prt_val = engine_mcast_prt
        saved_dep_mode = "cloud" if is_cloud_enabled else "local"
        saved_packet_source = "banc" if is_cloud_enabled else "flux"
        banc_loc_val = banc_location if is_cloud_enabled else "none"

    cfg = {
        "version": VERSION,
        "common": {
            "deployment_mode": saved_dep_mode,
            "packet_source": saved_packet_source,
            "banc_location": banc_loc_val,
            "ip_version": ip_ver,
            "banc": {
                "enabled": is_cloud_enabled,
                "host": banc_target_host,
                "port": int(banc_port),
                "ui_port": int(banc_ui_port),
                "auth_secret": str(auth_secret),
                "encryption_key": str(encryption_key),
                "identity": node_identity,
                "banc_initial_history_sec": max(1, int(banc_initial_history_sec)) if str(banc_initial_history_sec).isdigit() or isinstance(banc_initial_history_sec, (int, float)) else 60,
            },
            "multicast": {
                "flux_group": flux_grp_val,
                "flux_port": flux_prt_val,
                "engine_group": "224.1.1.1",
                "engine_port": 5007,
                "interface": "0.0.0.0",
            },
        },
        "grabber": {
            "channels": clean_channels,
            "chans": raw_chans_str,
            "locked_channels": sorted(list(locked_channels)),
            "process_count": len(worker_ports),
            "procs": procs_arg,
            "ports": worker_ports,
            "base_zmq_port": worker_ports[0] if worker_ports else 18077,
            "sample_rate": int(sample_rate),
            "gain": str(gain) if gain is not None else "auto",
            "ppm": int(ppm),
            "frequency_correction": int(ppm),
            "auto_tune": False,
            "bias_t": True,
            "bias_tee": 1,
            "device": device_index,
            "sdr_mode": chosen_sdr_mode,
            "rtl_sdr_bin": sdr.get("rtl_sdr_bin"),
            "rtl_tcp_bin": sdr.get("rtl_tcp_bin"),
            "rtlsdr_dll_dir": sdr.get("rtlsdr_dll_dir"),
            "rtl_tcp_host": chosen_tcp_host,
            "rtl_tcp_port": chosen_tcp_port,
            "dedup_ttl": 0.4,
            "processes": proc_allocations,
        },
        "flux": {
            "sources": flux_sources_str,
            "command_port": 56781,
            "burst_max_packets": int(burst_max_packets),
            "burst_interval_sec": int(burst_interval_sec),
            "multicast_group": flux_grp_val,
            "multicast_port": flux_prt_val,
            "cloud_server": flux_cloud_server,
            "cloud_port": flux_cloud_port,
            "auth_secret": str(auth_secret),
            "encryption_key": str(encryption_key),
        },
        "engine": {
            "default_layout": DEFAULT_LAYOUT,
            "layout_port": 5008,
            "mcast_group": "224.1.1.1",
            "mcast_port": 5007,
            "dedup_ttl": 0.4,
            "time_scale": 360.0,
            "inferred_timeout_sec": 120,
            "train_logic_interval_sec": 7.0,
            "parsers": list(DEFAULT_ENGINE_PARSERS),
            "connections": {
                "mcast_group": engine_mcast_grp,
                "mcast_port": engine_mcast_prt,
                "banc_host": engine_banc_host,
                "banc_port": engine_banc_port,
                "auth_secret": str(auth_secret),
                "encryption_key": str(encryption_key),
                "identity": node_identity,
                "banc_initial_history_sec": max(1, int(banc_initial_history_sec)) if str(banc_initial_history_sec).isdigit() or isinstance(banc_initial_history_sec, (int, float)) else 60,
            },
        },
        "viewer": {
            "default_layout": DEFAULT_LAYOUT,
            "engine_host": "127.0.0.1",
            "engine_port": 5008,
            "mcast_group": "224.1.1.1",
            "mcast_port": 5007,
            "stale_timeout": 120.0,
            "banc_host": banc_target_host if is_cloud_enabled else "none",
            "banc_port": int(banc_port) if is_cloud_enabled else 23009,
            "auth_secret": str(auth_secret),
            "encryption_key": str(encryption_key),
            "identity": node_identity,
        },
        "creator": {
            "db_path": "wius.db",
            "default_layout": DEFAULT_LAYOUT,
            "banc_host": banc_target_host if is_cloud_enabled else "none",
            "banc_port": int(banc_port) if is_cloud_enabled else 23009,
            "auth_secret": str(auth_secret),
            "encryption_key": str(encryption_key),
            "identity": node_identity,
        },
        "banc": {
            "port": int(banc_port),
            "ui_port": int(banc_ui_port),
            "auth_secret": str(auth_secret),
            "encryption_key": str(encryption_key),
            "whitelist_file": "whitelist.txt",
            "db_path": "banc.db",
            "db_retention_sec": 86400,
            "stats_interval_sec": 300,
            "bind_host": "auto" if ip_ver == "ipv6" else "0.0.0.0",
            "mirrors": [],
        },
        "spy": {
            "connect_endpoints": flux_sources_str,
            "dvr_size": 2000,
            "filter_channel": "",
            "rules": list(DEFAULT_SPY_RULES),
        },
        "packfig": {
            "default_rules": "signals_default.json",
        },
    }
    return cfg


def load_unified_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads unified configuration from disk or returns default configuration."""
    target_path = config_path or CONFIG_FILENAME
    if os.path.exists(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "common" in data:
                    return data
        except Exception as e:
            print(f"[WARN] Failed to load config from {target_path}: {e}", file=sys.stderr)
    return generate_unified_config()


def save_unified_config(cfg: Dict[str, Any], config_path: Optional[str] = None):
    """Saves unified configuration to disk."""
    target_path = config_path or CONFIG_FILENAME
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Configuration Template, Version Handling & Update Migration
# ---------------------------------------------------------------------------

def generate_template_config() -> Dict[str, Any]:
    """
    Secret-free default configuration shipped as config.defaults.json. It is only ever used to add
    keys that a newer version introduced; it never overrides a user's existing values.
    """
    return generate_unified_config(deployment_mode="local", auth_secret="", encryption_key="")


def parse_version(ver: Any) -> Tuple[int, ...]:
    """'v26.1113.01' -> (26, 1113, 1). Unparseable -> ()."""
    return tuple(int(x) for x in re.findall(r"\d+", str(ver or "")))


def _merge_into(target: Dict[str, Any], defaults: Dict[str, Any], path: str, added: List[str]):
    for key, dval in defaults.items():
        here = f"{path}.{key}" if path else key
        if key not in target:
            target[key] = copy.deepcopy(dval)
            added.append(here)
        elif isinstance(target[key], dict) and isinstance(dval, dict):
            _merge_into(target[key], dval, here, added)
        elif key in MERGE_LIST_KEYS and isinstance(target[key], list) and isinstance(dval, list):
            for item in dval:
                if item not in target[key]:
                    target[key].append(copy.deepcopy(item))
                    added.append(f"{here}[+{item}]")


def migrate_unified_config(current: Dict[str, Any], defaults: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Deep-merges a user's config with the new version's defaults:
      * user values always win (scalars, lists, sections);
      * keys that are new in `defaults` are appended with their default value;
      * list keys in MERGE_LIST_KEYS (parsers, rules) get new standard entries appended;
      * "version" is advanced; blank secrets are filled from the shared ones or newly generated.
    Returns (merged_config, report{"added": [...], "notes": [...], "old_version", "new_version"}).
    """
    merged = copy.deepcopy(current)
    added: List[str] = []
    notes: List[str] = []
    _merge_into(merged, defaults, "", added)

    old_version = str(current.get("version", ""))
    merged["version"] = VERSION

    # Secrets: keep the user's; propagate into newly added sections; generate only if truly missing.
    auth, key = _canonical_secrets(merged)
    if not key or not is_valid_encryption_key(key):
        key = generate_encryption_key()
        notes.append("No valid encryption_key found: generated a new one.")
    if not auth:
        auth = generate_auth_secret()
        notes.append("No auth_secret found: generated a new random one.")
    apply_secrets(merged, auth, key)

    # Unique node identity: ensure identity is unique and does not repeat legacy default "ApexNode_01"
    common_banc = merged.setdefault("common", {}).setdefault("banc", {})
    cur_ident = common_banc.get("identity")
    if not cur_ident or cur_ident == "ApexNode_01":
        new_ident = generate_unique_identity()
        common_banc["identity"] = new_ident
        notes.append(f"Replaced default identity with unique node identity: {new_ident}")
        merged.setdefault("engine", {}).setdefault("connections", {})["identity"] = new_ident
        merged.setdefault("viewer", {})["identity"] = new_ident
        merged.setdefault("creator", {})["identity"] = new_ident

    # Banc connection propagation for remote/cloud deployments
    if common_banc.get("enabled"):
        b_host = common_banc.get("host")
        b_port = common_banc.get("port", 23009)
        b_ident = common_banc.get("identity")
        for sect_name in ("viewer", "creator"):
            sect = merged.setdefault(sect_name, {})
            if isinstance(sect, dict):
                if b_host and str(b_host).strip().lower() not in ("none", "null", ""):
                    cur_h = str(sect.get("banc_host") or "").strip().lower()
                    if not cur_h or cur_h in ("none", "null", "127.0.0.1"):
                        sect["banc_host"] = b_host
                sect["banc_port"] = b_port
                if b_ident and not sect.get("identity"):
                    sect["identity"] = b_ident
        flux_sec = merged.setdefault("flux", {})
        if isinstance(flux_sec, dict):
            cur_srv = str(flux_sec.get("cloud_server") or "").strip().lower()
            if b_host and (not cur_srv or cur_srv in ("none", "null")):
                flux_sec["cloud_server"] = b_host
            flux_sec["cloud_port"] = b_port

    return merged, {"added": added, "notes": notes, "old_version": old_version, "new_version": VERSION}


def needs_migration(config_path: Optional[str] = None, defaults_path: Optional[str] = None) -> bool:
    """True if config.json is older than this software version or lacks keys the new defaults define."""
    path = config_path or CONFIG_FILENAME
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            current = json.load(f)
        if not isinstance(current, dict):
            return False
        if parse_version(current.get("version")) != parse_version(VERSION):
            return True
        _, report = migrate_unified_config(current, _load_defaults(path, defaults_path))
        return bool(report["added"])
    except Exception:
        return False


def _load_defaults(config_path: str, defaults_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads config.defaults.json (next to the config, then next to this module); falls back to the built-in template."""
    candidates = []
    if defaults_path:
        candidates.append(defaults_path)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(config_path)), CONFIG_DEFAULTS_FILENAME))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_DEFAULTS_FILENAME))
    for cp in candidates:
        if os.path.isfile(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return generate_template_config()


def migrate_config_file(
    config_path: Optional[str] = None,
    defaults_path: Optional[str] = None,
    backup: bool = True,
    sync_legacy: bool = True,
) -> Dict[str, Any]:
    """
    Migrates config.json in place after an update: timestamped backup, deep merge with the new
    defaults, version bump, and re-sync of the legacy config files.
    Returns a report dict with status "no_config" | "current" | "migrated" | "error".
    """
    path = config_path or CONFIG_FILENAME
    if not os.path.isfile(path):
        return {"status": "no_config", "path": path}
    try:
        with open(path, "r", encoding="utf-8") as f:
            current = json.load(f)
        if not isinstance(current, dict):
            return {"status": "error", "path": path, "error": "config.json is not a JSON object"}
    except Exception as exc:
        return {"status": "error", "path": path, "error": str(exc)}

    merged, report = migrate_unified_config(current, _load_defaults(path, defaults_path))
    if merged == current:
        return {"status": "current", "path": path, **report}

    backup_path = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        old_v = re.sub(r"[^0-9A-Za-z._-]", "_", report["old_version"] or "unknown")
        backup_path = f"{path}.bak.{old_v}_{stamp}"
        shutil.copy2(path, backup_path)

    save_unified_config(merged, path)
    if sync_legacy:
        try:
            sync_legacy_configs(merged, target_dir=os.path.dirname(os.path.abspath(path)))
        except Exception as exc:
            report["notes"].append(f"Legacy config sync failed: {exc}")
    return {"status": "migrated", "path": path, "backup": backup_path, **report}


def sync_legacy_configs(cfg: Dict[str, Any], target_dir: Optional[str] = None):
    """
    Exports synchronized legacy configuration files (grabber_config.json,
    MosFET.json, banc.json) so legacy command-line tools without --config flags
    continue working seamlessly.
    """
    base_dir = target_dir or os.getcwd()

    # 1. grabber_config.json
    grabber_cfg = cfg.get("grabber", {})
    procs = grabber_cfg.get("processes", [])
    ports_str = grabber_cfg.get("procs") or (",".join(str(p) for p in grabber_cfg.get("ports", [18077, 18081])))
    chans_str = str(grabber_cfg.get("chans") or "")
    if not chans_str:
        all_chans = grabber_cfg.get("channels", DEFAULT_CHANNELS)
        chans_str = ",".join(str(c) for c in all_chans)
    first_zmq = grabber_cfg.get("ports", [18077])[0] if grabber_cfg.get("ports") else (procs[0].get("zmq_port", 18077) if procs else 18077)
    num_workers = grabber_cfg.get("process_count", len(grabber_cfg.get("ports", procs)) if (procs or grabber_cfg.get("ports")) else 2)

    grabber_dst = os.path.join(base_dir, "grabber_config.json")
    existing_grabber: Dict[str, Any] = {}
    if os.path.exists(grabber_dst):
        try:
            with open(grabber_dst, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    existing_grabber = d
        except Exception:
            pass

    sdr_mode = str(grabber_cfg.get("sdr_mode") or "auto").lower()
    legacy_grabber = {
        "pipe": (sdr_mode == "pipe") if "sdr_mode" in grabber_cfg else existing_grabber.get("pipe", True),
        "stdin": existing_grabber.get("stdin", False),
        "rtl_sdr_bin": grabber_cfg.get("rtl_sdr_bin", existing_grabber.get("rtl_sdr_bin", None)),
        "rtl_tcp": (sdr_mode == "tcp") if "sdr_mode" in grabber_cfg else existing_grabber.get("rtl_tcp", False),
        "rtl_tcp_bin": grabber_cfg.get("rtl_tcp_bin", existing_grabber.get("rtl_tcp_bin", None)),
        "rtlsdr": (sdr_mode == "rtlsdr") if "sdr_mode" in grabber_cfg else existing_grabber.get("rtlsdr", False),
        "rtlsdr_dll_dir": grabber_cfg.get("rtlsdr_dll_dir", existing_grabber.get("rtlsdr_dll_dir", None)),
        "sdr_mode": sdr_mode,
        "host": grabber_cfg.get("rtl_tcp_host", existing_grabber.get("host", "127.0.0.1")),
        "port": grabber_cfg.get("rtl_tcp_port", existing_grabber.get("port", 1234)),
        "device": grabber_cfg.get("device", existing_grabber.get("device", 0)),
        "rate": existing_grabber.get("rate", 192000),
        "gain": str(grabber_cfg.get("gain") or existing_grabber.get("gain") or "auto"),
        "bias_tee": grabber_cfg.get("bias_tee", 1),
        "ppm": int(grabber_cfg.get("ppm", grabber_cfg.get("frequency_correction", existing_grabber.get("ppm", 0)))),
        "bank_size": existing_grabber.get("bank_size", 148000),
        "max_errors": existing_grabber.get("max_errors", 8),
        "auto_tune": grabber_cfg.get("auto_tune", False),
        "tune_interval": existing_grabber.get("tune_interval", 5.0),
        "channel_afc": existing_grabber.get("channel_afc", False),
        "afc_limit": existing_grabber.get("afc_limit", 500.0),
        "auto_ppm": existing_grabber.get("auto_ppm", True),
        "dedup_ttl": existing_grabber.get("dedup_ttl", 1.0),
        "chans": chans_str,
        "chans_file": None,
        "channel_timeout": 300.0,
        "procs": ports_str,
        "workers": num_workers,
        "zmq_port": first_zmq,
        "udp_port": None,
        "no_color": False,
        "full": False,
        "all": False,
        "no_rssi": False,
    }
    with open(grabber_dst, "w", encoding="utf-8") as f:
        json.dump(legacy_grabber, f, indent=2)

    # 2. MosFET.json (Flux)
    flux_cfg = cfg.get("flux", {})
    common_banc = cfg.get("common", {}).get("banc", {})
    banc_sec = cfg.get("banc", {})
    creator_sec = cfg.get("creator", {})
    viewer_sec = cfg.get("viewer", {})

    canonical_auth = (
        common_banc.get("auth_secret")
        or banc_sec.get("auth_secret")
        or creator_sec.get("auth_secret")
        or viewer_sec.get("auth_secret")
        or cfg.get("flux", {}).get("auth_secret")
        or cfg.get("engine", {}).get("connections", {}).get("auth_secret")
        or ""
    )
    canonical_key = (
        common_banc.get("encryption_key")
        or banc_sec.get("encryption_key")
        or creator_sec.get("encryption_key")
        or viewer_sec.get("encryption_key")
        or cfg.get("flux", {}).get("encryption_key")
        or cfg.get("engine", {}).get("connections", {}).get("encryption_key")
        or ""
    )

    banc_host = common_banc.get("host", "127.0.0.1") if common_banc.get("enabled") else "None"
    cloud_srv = flux_cfg.get("cloud_server")
    if not cloud_srv or str(cloud_srv).strip().lower() in ("none", "null", ""):
        cloud_srv = banc_host
    elif common_banc.get("enabled") and str(banc_host).strip().lower() not in ("none", "null", ""):
        cloud_srv = banc_host

    raw_srcs = flux_cfg.get("sources", "127.0.0.1:18077, 127.0.0.1:18081")
    if isinstance(raw_srcs, str):
        raw_srcs = re.sub(r"\blocalhost\b", "127.0.0.1", raw_srcs)

    legacy_flux = {
        "multicast_group": flux_cfg.get("multicast_group", "239.1.1.1"),
        "multicast_port": flux_cfg.get("multicast_port", 23003),
        "command_port": flux_cfg.get("command_port", 56781),
        "sources": raw_srcs,
        "cloud_server": cloud_srv,
        "cloud_port": flux_cfg.get("cloud_port") or (common_banc.get("port", 23009) if common_banc.get("enabled") else 0),
        "auth_secret": canonical_auth,
        "encryption_key": canonical_key,
        "burst_max_packets": int(flux_cfg.get("burst_max_packets", 100)),
        "burst_interval_sec": int(flux_cfg.get("burst_interval_sec", 120)),
    }
    with open(os.path.join(base_dir, "MosFET.json"), "w", encoding="utf-8") as f:
        json.dump(legacy_flux, f, indent=2)

    # 3. banc.json
    legacy_banc = {
        "port": banc_sec.get("port", 23009),
        "ui_port": banc_sec.get("ui_port", 23010),
        "auth_secret": canonical_auth,
        "encryption_key": canonical_key,
        "whitelist_file": banc_sec.get("whitelist_file", "whitelist.txt"),
        "db_path": banc_sec.get("db_path", "banc.db"),
        "db_retention_sec": banc_sec.get("db_retention_sec", 86400),
        "stats_interval_sec": banc_sec.get("stats_interval_sec", 300),
        "bind_host": banc_sec.get("bind_host", "0.0.0.0"),
        "mirrors": banc_sec.get("mirrors", []),
    }
    with open(os.path.join(base_dir, "banc.json"), "w", encoding="utf-8") as f:
        json.dump(legacy_banc, f, indent=2)

    # 4. mosfet_client.json
    mcast_grp = viewer_sec.get("mcast_group", "224.1.1.1")
    mcast_prt = viewer_sec.get("mcast_port", 5007)

    c_banc_host = common_banc.get("host", "none") if common_banc.get("enabled") else "none"
    active_banc_host = (
        viewer_sec.get("banc_host")
        or creator_sec.get("banc_host")
        or (cfg.get("engine", {}).get("connections", {}).get("banc_host"))
        or c_banc_host
    )
    if not active_banc_host:
        active_banc_host = "none"

    active_banc_port = (
        viewer_sec.get("banc_port")
        or creator_sec.get("banc_port")
        or (cfg.get("engine", {}).get("connections", {}).get("banc_port"))
        or common_banc.get("port", 23009)
    )
    active_ident = (
        viewer_sec.get("identity")
        or creator_sec.get("identity")
        or (cfg.get("engine", {}).get("connections", {}).get("identity"))
        or common_banc.get("identity")
        or generate_unique_identity()
    )
    if not active_ident or active_ident == "ApexNode_01":
        active_ident = generate_unique_identity()

    legacy_client = {
        "mcast_group": mcast_grp,
        "mcast_port": mcast_prt,
        "banc_host": active_banc_host,
        "banc_port": int(active_banc_port) if active_banc_port else 23009,
        "auth_secret": canonical_auth,
        "encryption_key": canonical_key,
        "identity": str(active_ident),
    }
    client_dst = os.path.join(base_dir, "mosfet_client.json")
    with open(client_dst, "w", encoding="utf-8") as f:
        json.dump(legacy_client, f, indent=2)
    cfg_sub_dir = os.path.join(base_dir, "config")
    if os.path.isdir(cfg_sub_dir):
        with open(os.path.join(cfg_sub_dir, "mosfet_client.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_client, f, indent=2)

    # 5. Ensure whitelist.txt contains canonical identities
    wl_file = banc_sec.get("whitelist_file", "whitelist.txt")
    wl_path = os.path.join(base_dir, wl_file)
    existing_wl = set()
    if os.path.exists(wl_path):
        try:
            with open(wl_path, "r", encoding="utf-8") as f:
                existing_wl = set(line.strip() for line in f if line.strip())
        except Exception:
            existing_wl = set()
    target_idents = set()
    if active_ident:
        target_idents.add(active_ident)
    c_ident = common_banc.get("identity")
    if c_ident:
        target_idents.add(c_ident)
    cr_ident = creator_sec.get("identity")
    if cr_ident:
        target_idents.add(cr_ident)
    new_idents = target_idents - existing_wl
    if new_idents:
        try:
            with open(wl_path, "a", encoding="utf-8") as f:
                for nid in sorted(new_idents):
                    f.write(f"{nid}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Boot Service Configuration & Removal (Windows / Linux / macOS)
# ---------------------------------------------------------------------------

def _service_commands(wdir: str, cfg_p: str) -> Dict[str, Tuple[str, str]]:
    """Returns {key: (script_path, description)} for the services that can start at boot."""
    return {
        "grabber": (os.path.join(wdir, "ApexBOSGrabber.py"), "ApexBOS SDR Grabber"),
        "flux": (os.path.join(wdir, "ApexBOSFlux.py"), "ApexBOS Flux Relay"),
    }


def _run_quiet(cmd: List[str]) -> bool:
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def configure_boot_services(
    enable_grabber: bool = False,
    enable_flux: bool = False,
    working_dir: Optional[str] = None,
    config_path: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Configures boot services on the host OS for Grabber and Flux.
      * Windows: per-user Task Scheduler tasks (run at logon, no admin required)
      * Linux:   systemd user units (enabled)
      * macOS:   LaunchAgents plists (RunAtLoad)
    With dry_run=True nothing is written or registered; commands/paths are only reported.
    """
    wdir = os.path.abspath(working_dir or os.getcwd())
    cfg_p = os.path.abspath(config_path or os.path.join(wdir, CONFIG_FILENAME))
    py_exe = sys.executable
    wanted = {"grabber": enable_grabber, "flux": enable_flux}
    services = _service_commands(wdir, cfg_p)
    results: Dict[str, Any] = {"platform": sys.platform, "grabber": False, "flux": False, "details": [], "created": []}

    if sys.platform == "win32":
        results["details"].append("Configured for Windows Task Scheduler (per-user, at logon)")
        for key, (script, _desc) in services.items():
            if not wanted[key]:
                continue
            task_cmd = f'"{py_exe}" "{script}" --config "{cfg_p}"'
            cmd = ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAMES[key], "/TR", task_cmd,
                   "/SC", "ONLOGON", "/RL", "LIMITED", "/F"]
            results[f"{key}_cmd"] = subprocess.list2cmdline(cmd)
            if dry_run or _run_quiet(cmd):
                results[key] = True
                results["created"].append({"type": "schtask", "name": WINDOWS_TASK_NAMES[key]})
            else:
                results["details"].append(f"schtasks failed to create {WINDOWS_TASK_NAMES[key]}")

    elif sys.platform == "darwin":
        agents_dir = os.path.expanduser("~/Library/LaunchAgents")
        results["details"].append(f"Configured macOS LaunchAgents in {agents_dir}")
        for key, (script, _desc) in services.items():
            if not wanted[key]:
                continue
            plist_p = os.path.join(agents_dir, f"com.apexbos.{key}.plist")
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.apexbos.{key}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py_exe}</string>
        <string>{script}</string>
        <string>--config</string>
        <string>{cfg_p}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{wdir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
            results[f"{key}_file"] = plist_p
            if dry_run:
                results[key] = True
                continue
            try:
                os.makedirs(agents_dir, exist_ok=True)
                with open(plist_p, "w", encoding="utf-8") as f:
                    f.write(plist_content)
                results[key] = True
                results["created"].append({"type": "plist", "path": plist_p})
            except Exception as e:
                results["details"].append(f"Error writing {plist_p}: {e}")

    else:
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        results["details"].append(f"Configured Linux systemd user units in {unit_dir}")
        for key, (script, desc) in services.items():
            if not wanted[key]:
                continue
            unit_name = f"apexbos-{key}.service"
            service_p = os.path.join(unit_dir, unit_name)
            content = f"""[Unit]
Description={desc}
After=network.target

[Service]
Type=simple
WorkingDirectory={wdir}
ExecStart={py_exe} {script} --config {cfg_p}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
            results[f"{key}_file"] = service_p
            if dry_run:
                results[key] = True
                continue
            try:
                os.makedirs(unit_dir, exist_ok=True)
                with open(service_p, "w", encoding="utf-8") as f:
                    f.write(content)
                results[key] = True
                results["created"].append({"type": "systemd", "path": service_p, "unit": unit_name})
            except Exception as e:
                results["details"].append(f"Error writing {service_p}: {e}")
        if not dry_run and results["created"] and shutil.which("systemctl"):
            _run_quiet(["systemctl", "--user", "daemon-reload"])
            for item in results["created"]:
                _run_quiet(["systemctl", "--user", "enable", item["unit"]])
            results["details"].append("Enabled units (for start before login run: loginctl enable-linger $USER)")

    return results


def remove_boot_services(dry_run: bool = False) -> Dict[str, Any]:
    """Removes the Grabber/Flux boot services created by configure_boot_services (idempotent)."""
    removed: List[str] = []
    errors: List[str] = []

    if sys.platform == "win32":
        for key, task in WINDOWS_TASK_NAMES.items():
            exists = _run_quiet(["schtasks", "/Query", "/TN", task])
            if not exists:
                continue
            if dry_run or _run_quiet(["schtasks", "/Delete", "/TN", task, "/F"]):
                removed.append(f"schtask:{task}")
            else:
                errors.append(f"Could not delete scheduled task {task}")

    elif sys.platform == "darwin":
        agents_dir = os.path.expanduser("~/Library/LaunchAgents")
        for key in ("grabber", "flux"):
            plist_p = os.path.join(agents_dir, f"com.apexbos.{key}.plist")
            if not os.path.isfile(plist_p):
                continue
            if dry_run:
                removed.append(plist_p)
                continue
            _run_quiet(["launchctl", "unload", plist_p])
            try:
                os.remove(plist_p)
                removed.append(plist_p)
            except OSError as e:
                errors.append(f"Could not remove {plist_p}: {e}")

    else:
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        for key in ("grabber", "flux"):
            unit_name = f"apexbos-{key}.service"
            service_p = os.path.join(unit_dir, unit_name)
            if not os.path.isfile(service_p):
                continue
            if dry_run:
                removed.append(service_p)
                continue
            if shutil.which("systemctl"):
                _run_quiet(["systemctl", "--user", "disable", "--now", unit_name])
            try:
                os.remove(service_p)
                removed.append(service_p)
            except OSError as e:
                errors.append(f"Could not remove {service_p}: {e}")
        if removed and not dry_run and shutil.which("systemctl"):
            _run_quiet(["systemctl", "--user", "daemon-reload"])

    return {"platform": sys.platform, "removed": removed, "errors": errors}


# ---------------------------------------------------------------------------
# Windows 11 Firewall & Multicast Routing Script Generator
# ---------------------------------------------------------------------------

def generate_windows_firewall_commands(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Generates dynamic Windows Defender Firewall and Multicast routing commands
    and script content based on the configured ports and multicast groups in `cfg`.
    """
    cfg = cfg or {}
    flux_cfg = cfg.get("flux", {})
    engine_cfg = cfg.get("engine", {})
    grabber_cfg = cfg.get("grabber", {})
    banc_cfg = cfg.get("banc", {})
    common_banc = cfg.get("common", {}).get("banc", {})

    # UDP ports
    udp_ports = set()
    f_mcast_port = flux_cfg.get("multicast_port")
    if f_mcast_port is None and "multicast_port" not in flux_cfg:
        f_mcast_port = cfg.get("common", {}).get("multicast", {}).get("flux_port")
        if f_mcast_port is None and "flux_port" not in cfg.get("common", {}).get("multicast", {}):
            f_mcast_port = 23003
    if f_mcast_port is not None:
        try:
            udp_ports.add(int(f_mcast_port))
        except (ValueError, TypeError):
            pass
    e_mcast_port = engine_cfg.get("mcast_port") or 5007
    try:
        e_mcast_int = int(e_mcast_port)
        for offset in (0, 10, 20, 30):
            udp_ports.add(e_mcast_int + offset)
    except (ValueError, TypeError):
        udp_ports.add(5007)
        udp_ports.add(5017)
        udp_ports.add(5027)
        udp_ports.add(5037)

    conn_mcast_port = engine_cfg.get("connections", {}).get("mcast_port")
    if conn_mcast_port is not None:
        try:
            udp_ports.add(int(conn_mcast_port))
        except (ValueError, TypeError):
            pass

    # TCP ports
    tcp_ports = set()
    layout_port = engine_cfg.get("layout_port") or 5008
    try:
        layout_int = int(layout_port)
        for offset in (0, 10, 20, 30):
            tcp_ports.add(layout_int + offset)
    except (ValueError, TypeError):
        tcp_ports.add(5008)
        tcp_ports.add(5018)
        tcp_ports.add(5028)
        tcp_ports.add(5038)

    b_port = banc_cfg.get("port") or common_banc.get("port") or 23009
    try:
        tcp_ports.add(int(b_port))
    except (ValueError, TypeError):
        pass

    b_ui_port = banc_cfg.get("ui_port") or common_banc.get("ui_port") or 23010
    try:
        tcp_ports.add(int(b_ui_port))
    except (ValueError, TypeError):
        pass

    cmd_port = flux_cfg.get("command_port") or 56781
    try:
        tcp_ports.add(int(cmd_port))
    except (ValueError, TypeError):
        pass

    rtl_tcp_port = grabber_cfg.get("rtl_tcp_port") or 1234
    try:
        tcp_ports.add(int(rtl_tcp_port))
    except (ValueError, TypeError):
        pass

    grabber_ports = grabber_cfg.get("ports", [])
    for p in grabber_ports:
        try:
            tcp_ports.add(int(p))
        except (ValueError, TypeError):
            pass
    for proc in grabber_cfg.get("processes", []):
        if "zmq_port" in proc:
            try:
                tcp_ports.add(int(proc["zmq_port"]))
            except (ValueError, TypeError):
                pass

    # Multicast groups
    mcast_groups = set()
    f_group = flux_cfg.get("multicast_group")
    if f_group is None and "multicast_group" not in flux_cfg:
        f_group = cfg.get("common", {}).get("multicast", {}).get("flux_group")
        if f_group is None and "flux_group" not in cfg.get("common", {}).get("multicast", {}):
            f_group = "239.1.1.1"
    if f_group and str(f_group).lower() != "none" and str(f_group).strip() != "":
        mcast_groups.add(str(f_group))
    e_group = engine_cfg.get("mcast_group") or "224.1.1.1"
    if e_group and str(e_group).lower() != "none" and str(e_group).strip() != "":
        mcast_groups.add(str(e_group))
    conn_mcast_grp = engine_cfg.get("connections", {}).get("mcast_group")
    if conn_mcast_grp and str(conn_mcast_grp).lower() != "none" and str(conn_mcast_grp).strip() != "":
        mcast_groups.add(str(conn_mcast_grp))

    sorted_udp = [str(p) for p in sorted(udp_ports)]
    udp_ports_str = ",".join(sorted_udp)

    # For TCP, include dynamic Grabber worker ranges 18000-18175, 20101-20175
    tcp_items = [str(p) for p in sorted(tcp_ports)]
    if "18000-18175" not in tcp_items:
        tcp_items.append("18000-18175")
    if "20101-20175" not in tcp_items:
        tcp_items.append("20101-20175")
    tcp_ports_str = ",".join(tcp_items)

    mcast_groups_list = sorted(mcast_groups)
    mcast_groups_str = ", ".join(mcast_groups_list)

    ps1_script = f"""# ==============================================================================
# ApexBOS Windows Defender Firewall & Multicast Group Configuration
# Automatically generated by ApexBOS Setup
# Run this script in an elevated PowerShell prompt (Run as Administrator)
# ==============================================================================
$ErrorActionPreference = "Stop"

# Elevate if not running as administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {{
    Write-Warning "Administrator rights required. Requesting elevation..."
    Start-Process powershell.exe -ArgumentList ("-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"") -Verb RunAs
    exit
}}

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  ApexBOS Windows 11 Firewall & Multicast Group Setup" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "Configured Multicast Groups: {mcast_groups_str}" -ForegroundColor Cyan
Write-Host ""

# 1. Clean up old ApexBOS rules to prevent duplicates
Write-Host "[1/5] Removing existing ApexBOS firewall rules (if any)..." -ForegroundColor Gray
Get-NetFirewallRule -DisplayName "ApexBOS *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue

# 2. Inbound UDP for Flux & Engine Multicast
Write-Host "[2/5] Creating Inbound UDP Multicast rule for ports: {udp_ports_str}..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "ApexBOS Inbound UDP Multicast" `
    -Description "Allows incoming UDP multicast traffic for ApexBOS Flux and Engine." `
    -Direction Inbound `
    -LocalPort {udp_ports_str} `
    -Protocol UDP `
    -Action Allow `
    -Profile Domain,Private,Public | Out-Null
Write-Host "      -> Allowed UDP ports: {udp_ports_str}" -ForegroundColor Green

# 3. Inbound TCP for Grabber, Engine layout server, Banc, and Flux
Write-Host "[3/5] Creating Inbound TCP rule for ports: {tcp_ports_str}..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "ApexBOS Inbound TCP Services" `
    -Description "Allows inbound TCP traffic for Grabber workers, Engine layout, and Banc." `
    -Direction Inbound `
    -LocalPort {tcp_ports_str} `
    -Protocol TCP `
    -Action Allow `
    -Profile Domain,Private,Public | Out-Null
Write-Host "      -> Allowed TCP ports: {tcp_ports_str}" -ForegroundColor Green

# 4. Inbound IGMP (Protocol 2)
Write-Host "[4/5] Creating Inbound IGMP rule (Protocol 2)..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "ApexBOS Inbound IGMP" `
    -Description "Allows IGMP protocol for IPv4 multicast group join and membership reports." `
    -Direction Inbound `
    -Protocol 2 `
    -Action Allow `
    -Profile Domain,Private,Public | Out-Null
Write-Host "      -> Allowed IGMP (Protocol 2)" -ForegroundColor Green

# 5. Whitelist Python runtimes
Write-Host "[5/5] Whitelisting Python runtime executables..." -ForegroundColor Yellow
$pyExes = Get-Command py, python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -Unique
foreach ($exe in $pyExes) {{
    if (Test-Path $exe) {{
        $exeName = Split-Path $exe -Leaf
        New-NetFirewallRule -DisplayName "ApexBOS Python Runtime ($exeName)" `
            -Description "Allows Python runtime to bind and accept network connections for ApexBOS." `
            -Direction Inbound `
            -Program $exe `
            -Action Allow `
            -Profile Domain,Private,Public | Out-Null
        Write-Host "      -> Whitelisted Python binary: $exe" -ForegroundColor Green
    }}
}}

# 6. Ensure Multicast Route (224.0.0.0/4)
Write-Host ""
Write-Host "[+] Configuring Multicast Route 224.0.0.0/4 for active network adapters..." -ForegroundColor Yellow
try {{
    route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 metric 1 2>$null | Out-Null
}} catch {{}}

$activeAdapters = Get-NetIPInterface -AddressFamily IPv4 -ConnectionState Connected -ErrorAction SilentlyContinue | Where-Object {{ $_.InterfaceAlias -notlike "*Loopback*" }}
foreach ($adapter in $activeAdapters) {{
    try {{
        route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 if $adapter.InterfaceIndex metric 1 2>$null | Out-Null
        Write-Host "      -> Multicast route active on interface: $($adapter.InterfaceAlias) (Index $($adapter.InterfaceIndex))" -ForegroundColor Green
    }} catch {{}}
}}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  Windows Firewall and Multicast setup complete!" -ForegroundColor Green
Write-Host "  Flux and Engine can now receive all local and network packets." -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
"""

    bat_script = f"""@echo off
setlocal
cd /d "%~dp0"

:: Check for administrative rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges to configure Windows Firewall...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ==============================================================================
echo   ApexBOS Windows Firewall ^& Multicast Configuration
echo ==============================================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable_firewall.ps1"
echo.
pause
"""

    ps_oneliner = (
        f'New-NetFirewallRule -DisplayName "ApexBOS Inbound UDP Multicast" -Direction Inbound -LocalPort {udp_ports_str} -Protocol UDP -Action Allow -Profile Domain,Private,Public; '
        f'New-NetFirewallRule -DisplayName "ApexBOS Inbound TCP Services" -Direction Inbound -LocalPort {tcp_ports_str} -Protocol TCP -Action Allow -Profile Domain,Private,Public; '
        f'New-NetFirewallRule -DisplayName "ApexBOS Inbound IGMP" -Direction Inbound -Protocol 2 -Action Allow -Profile Domain,Private,Public; '
        f'route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 metric 1; '
        f'Get-NetIPInterface -AddressFamily IPv4 -ConnectionState Connected | Where-Object {{ $_.InterfaceAlias -notlike "*Loopback*" }} | ForEach-Object {{ route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 if $_.InterfaceIndex metric 1 }}; '
        f'Write-Host "ApexBOS Windows Firewall & Multicast rules enabled successfully!" -ForegroundColor Green'
    )

    cmd_oneliner = (
        f'powershell -ExecutionPolicy Bypass -NoProfile -Command "'
        f'New-NetFirewallRule -DisplayName \'ApexBOS Inbound UDP Multicast\' -Direction Inbound -LocalPort {udp_ports_str} -Protocol UDP -Action Allow -Profile Domain,Private,Public; '
        f'New-NetFirewallRule -DisplayName \'ApexBOS Inbound TCP Services\' -Direction Inbound -LocalPort {tcp_ports_str} -Protocol TCP -Action Allow -Profile Domain,Private,Public; '
        f'New-NetFirewallRule -DisplayName \'ApexBOS Inbound IGMP\' -Direction Inbound -Protocol 2 -Action Allow -Profile Domain,Private,Public; '
        f'route add 224.0.0.0 mask 240.0.0.0 0.0.0.0 metric 1; '
        f'Write-Host \'ApexBOS Windows Firewall & Multicast rules enabled successfully!\' -ForegroundColor Green"'
    )

    return {
        "ps1_script": ps1_script,
        "bat_script": bat_script,
        "ps_oneliner": ps_oneliner,
        "cmd_oneliner": cmd_oneliner,
        "udp_ports": sorted(udp_ports),
        "tcp_ports": sorted(tcp_ports),
        "mcast_groups": mcast_groups_list,
        "udp_ports_str": udp_ports_str,
        "tcp_ports_str": tcp_ports_str,
    }


def save_windows_firewall_files(cfg: Optional[Dict[str, Any]] = None, target_dir: Optional[str] = None) -> Tuple[str, str]:
    """
    Saves enable_firewall.ps1 and enable_firewall.bat into target_dir.
    Returns (ps1_path, bat_path).
    """
    wdir = os.path.abspath(target_dir or os.getcwd())
    fw_info = generate_windows_firewall_commands(cfg)
    ps1_path = os.path.join(wdir, "enable_firewall.ps1")
    bat_path = os.path.join(wdir, "enable_firewall.bat")

    with open(ps1_path, "w", encoding="utf-8") as f:
        f.write(fw_info["ps1_script"])

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(fw_info["bat_script"])

    return ps1_path, bat_path


def launch_firewall_script(target_dir: Optional[str] = None) -> bool:
    """
    Launches enable_firewall.bat with UAC elevation on Windows.
    Returns True if launched, False otherwise.
    """
    wdir = os.path.abspath(target_dir or os.getcwd())
    bat_path = os.path.join(wdir, "enable_firewall.bat")
    ps1_path = os.path.join(wdir, "enable_firewall.ps1")
    if not os.path.isfile(bat_path) or not os.path.isfile(ps1_path):
        save_windows_firewall_files(None, wdir)

    if sys.platform == "win32":
        try:
            if hasattr(os, "startfile"):
                os.startfile(bat_path)
                return True
        except Exception:
            pass
        try:
            subprocess.Popen([
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", f"Start-Process -FilePath '{bat_path}' -Verb RunAs"
            ])
            return True
        except Exception:
            pass
    return False

