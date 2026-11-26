#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApexBOSFlux.py - ApexBOSFlux SDR Collector & Cloud Bursting Uplink

MIT License
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

import os
import sys
import json
import time
import struct
import socket
import ipaddress
import argparse
import asyncio
import collections
import hashlib
import re
import zlib
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

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

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message
from cryptography.fernet import Fernet

import mosfet_auth
import mosfet_client
from version import VERSION, __version__

# ---------------------------------------------------------------------------
# ANSI Colors and Backgrounds
# ---------------------------------------------------------------------------
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

BG_BANNER = "\033[44;1;37m"    # Blue background, bold white text
BG_AVAIL = "\033[42;1;30m"     # Green background, dark text
BG_UNAVAIL = "\033[41;1;37m"   # Red background, white text
BG_PAUSED = "\033[43;1;30m"    # Yellow background, dark text
BG_ACTIVE = "\033[42;1;30m"    # Green background, dark text

# ---------------------------------------------------------------------------
# Cloud & Identity Management (IPv4 & IPv6)
# ---------------------------------------------------------------------------

def normalize_host_arg(host: Optional[str]) -> str:
    """Strips whitespace and optional IPv6 square brackets '[...]' from a host string."""
    return str(host or "").strip().strip("[]")


def detect_ip_version(host: Optional[str]) -> Optional[int]:
    """Returns 4 for IPv4 literal, 6 for IPv6 literal, or None for hostname."""
    clean = normalize_host_arg(host)
    if not clean:
        return None
    if "%" in clean:
        clean = clean.split("%", 1)[0]
    try:
        return ipaddress.ip_address(clean).version
    except ValueError:
        return None


def resolve_target_ip_version(host: Optional[str]) -> Optional[int]:
    """Detects IP version from a literal address or via DNS getaddrinfo resolution."""
    ver = detect_ip_version(host)
    if ver is not None:
        return ver
    clean = normalize_host_arg(host)
    if not clean:
        return None
    try:
        infos = socket.getaddrinfo(clean, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not infos:
            return None
        families = {info[0] for info in infos}
        if socket.AF_INET6 in families and socket.AF_INET not in families:
            return 6
        if socket.AF_INET in families and socket.AF_INET6 not in families:
            return 4
        return 6 if infos[0][0] == socket.AF_INET6 else 4
    except OSError:
        return None


def format_host_port(host: str, port: int) -> str:
    """Formats host and port cleanly, wrapping IPv6 literals in square brackets."""
    clean = normalize_host_arg(host)
    if detect_ip_version(clean) == 6:
        return f"[{clean}]:{int(port)}"
    return f"{clean}:{int(port)}"


def normalize_sources_list(raw_sources: Any) -> List[str]:
    """Normalizes raw sources into a flat list of source strings.

    Accepts comma-separated strings, lists/tuples/sets of strings or ints,
    or None/empty, ensuring compatibility with all configuration formats.
    """
    if raw_sources is None:
        return []
    if isinstance(raw_sources, (list, tuple, set)):
        items: List[str] = []
        for x in raw_sources:
            if isinstance(x, (list, tuple, set)):
                items.extend(normalize_sources_list(x))
            elif isinstance(x, str):
                items.extend([s.strip() for s in x.split(",") if s.strip()])
            elif isinstance(x, int):
                items.append(f"127.0.0.1:{x}")
            elif x:
                items.append(str(x).strip())
        return [s for s in items if s]
    if isinstance(raw_sources, str):
        return [s.strip() for s in raw_sources.split(",") if s.strip()]
    if isinstance(raw_sources, int):
        return [f"127.0.0.1:{raw_sources}"]
    s = str(raw_sources).strip()
    return [s] if s else []


def extract_ips_from_traceroute(output_text: str) -> List[str]:
    """Legacy helper retained for compatibility: delegates to mosfet_client."""
    return mosfet_client.extract_ips_from_traceroute(output_text)


async def generate_identity(target_host: str = "") -> str:
    """Computes a stable 12-char SHA-256 network identity anchored to the LAN default gateway MAC.

    Delegates to mosfet_client.resolve_network_identity_details() so Flux, Dispatcher Engine,
    and Setup Wizard always compute the exact same network identity across all machines on a LAN.
    """
    clean_host = normalize_host_arg(target_host)
    print("[*] Generating stable network identity (LAN default gateway MAC)...")
    try:
        identity, source = await mosfet_client.resolve_network_identity_details(clean_host)
        print(f"[+] Network identity resolved via {source}: {identity}")
        return identity
    except Exception as e:
        print(f"[-] Network identity resolution failed: {e}. Using local fallback identity.")
        return mosfet_client.hash_network_identity("local")

# ---------------------------------------------------------------------------
# Statistics and Data Management
# ---------------------------------------------------------------------------

class WindowCounter:
    def __init__(self):
        self.buckets = [0] * 60
        self.last_second = int(time.time())

    def add(self, count=1):
        now = int(time.time())
        if now != self.last_second:
            diff = now - self.last_second
            if diff >= 60:
                self.buckets = [0] * 60
            else:
                for i in range(1, diff + 1):
                    self.buckets[(self.last_second + i) % 60] = 0
            self.last_second = now
        self.buckets[now % 60] += count

    def get_rate(self):
        self.add(0)
        return sum(self.buckets)

class StatsManager:
    def __init__(self, auth_secret="", cloud_host="", cloud_port=0):
        self.endpoints = {}
        self.statuses = {}
        self.total_packets = WindowCounter()
        self.identity = "GENERATING..."
        self.auth_secret = auth_secret
        
        # Cloud Uplink Stats
        self.cloud_host = cloud_host
        self.cloud_port = cloud_port
        self.cloud_bursts = 0
        self.cloud_last_status = "PENDING"
        self.cloud_total_kb = 0.0
        self.cloud_last_kb = 0.0
        self.cloud_next_burst = "N/A"
        self.packets_in_buffer = 0
        
        # Controls
        self.upload_paused = False

    def toggle_upload_pause(self):
        """Toggles upload paused state and updates burst buffer stats accordingly."""
        self.upload_paused = not self.upload_paused
        self.cloud_last_status = "PAUSED BY USER" if self.upload_paused else "RESUMED"
        if self.upload_paused:
            self.cloud_next_burst = "PAUSED"
            self.packets_in_buffer = 0
        return self.upload_paused

    def register_endpoint(self, endpoint):
        if endpoint not in self.endpoints:
            self.endpoints[endpoint] = WindowCounter()
            self.statuses[endpoint] = "INITIALIZING"

    def add_packet(self, endpoint):
        self.endpoints[endpoint].add(1)
        self.total_packets.add(1)

    def update_status(self, endpoint, status):
        self.statuses[endpoint] = status

    def generate_ascii_screen(self, current_buffer: str = ''):
        active_count = sum(1 for s in self.statuses.values() if s == "ACTIVE")
        restoring_count = sum(1 for s in self.statuses.values() if s in ("RESTORING", "INITIALIZING"))
        
        lines = [
            f"{BG_BANNER}  ApexBOSFlux ({VERSION}) Status & Telemetry  {C_RESET}",
            ""
        ]
        
        lines.append(f"  {C_BOLD}Identity ID:{C_RESET}     {C_YELLOW}{self.identity}{C_RESET}")
        
        if self.auth_secret:
            current_token = mosfet_auth.generate_token(self.auth_secret)
            # Calculate remaining time in the 10-minute (600s) window
            time_rem = 600 - (int(time.time()) % 600)
            rem_m, rem_s = divmod(time_rem, 60)
            token_display = f"{current_token} (changes in {rem_m:02d}:{rem_s:02d})"
        else:
            token_display = "N/A"
            
        lines.append(f"  {C_BOLD}Auth Token:{C_RESET}      {C_GREEN}{token_display}{C_RESET}")
        lines.append(f"  {C_BOLD}Total Pkts/Min:{C_RESET}  {C_CYAN}{self.total_packets.get_rate()}{C_RESET}")
        lines.append(f"  {C_BOLD}Connections:{C_RESET}     Active: {C_GREEN}{active_count:<6}{C_RESET}  Pending: {C_YELLOW}{restoring_count}{C_RESET}")

        # Endpoints Table
        lines.append("")
        lines.append(f"{BG_BANNER}  Monitored Endpoints  {C_RESET}")
        lines.append(f"  {C_BOLD}{C_CYAN}{'Endpoint':<38}  {'Status':<16}  {'Packets/Min':<16}{C_RESET}")

        sorted_endpoints = sorted(self.endpoints.keys(), key=lambda e: (self.statuses.get(e, ""), e))
        for ep in sorted_endpoints:
            status = self.statuses.get(ep, "UNKNOWN")
            rate = self.endpoints[ep].get_rate()
            
            if status == "ACTIVE":
                s_badge = f"{BG_ACTIVE} ACTIVE {C_RESET}"
            elif status == "DISCONNECTED":
                s_badge = f"{BG_UNAVAIL} DISCONNECTED {C_RESET}"
            else:
                s_badge = f"{BG_PAUSED} {status} {C_RESET}"
                
            lines.append(f"  {C_CYAN}{ep:<38}{C_RESET}  {s_badge:<26}  {C_CYAN}{rate:<16}{C_RESET}")

        # Cloud Uplink Status
        lines.append("")
        lines.append(f"{BG_BANNER}  Cloud Uplink Status  {C_RESET}")
        
        srv_val = format_host_port(self.cloud_host, self.cloud_port) if self.cloud_host else f":{self.cloud_port}"
        l_badge = f"{BG_PAUSED} PAUSED {C_RESET}" if self.upload_paused else (f"{BG_ACTIVE} SUCCESS {C_RESET}" if "SUCCESS" in self.cloud_last_status else f"{BG_UNAVAIL} {self.cloud_last_status} {C_RESET}")
        
        lines.append(f"  {C_BOLD}Server:{C_RESET}           {C_CYAN}{srv_val:<26}{C_RESET}  {C_BOLD}Last Status:{C_RESET} {l_badge}")
        
        b_val = str(self.cloud_bursts)
        n_val = f"{self.cloud_next_burst}"
        n_col = C_YELLOW if self.upload_paused else C_CYAN
        lines.append(f"  {C_BOLD}Bursts:{C_RESET}           {C_CYAN}{b_val:<26}{C_RESET}  {C_BOLD}Next Burst:{C_RESET}  {n_col}{n_val}{C_RESET}")
        
        t_val = f"{self.cloud_total_kb:.2f} KB"
        l_val = f"{self.cloud_last_kb:.2f} KB"
        lines.append(f"  {C_BOLD}Total Upload:{C_RESET}     {C_CYAN}{t_val:<26}{C_RESET}  {C_BOLD}Last Burst:{C_RESET}  {C_CYAN}{l_val}{C_RESET}")
        
        buf_val = f"{self.packets_in_buffer} pkts"
        lines.append(f"  {C_BOLD}Buffer Queue:{C_RESET}     {C_CYAN}{buf_val}{C_RESET}")
        
        lines.append("")
        lines.append(f"{BG_BANNER}  [Press 'P' to Pause/Resume Uploads]  {C_RESET}")
        lines.append(f"  {C_BOLD}{C_YELLOW}Command > {C_RESET}{current_buffer}")
        return "\r\n".join(lines) + "\r\n"

# ---------------------------------------------------------------------------
# ZeroMQ Connection Manager & Data Collection
# ---------------------------------------------------------------------------

# Protocol codes stored in Banc packets.b2 (INTEGER) and packets.b3 (TEXT)
PKT_PROTO_PTC = 0
PKT_PROTO_ATCS = 1
PKT_PROTO_FRED = 2  # EOT / HOT / DPU / RLC


def classify_packet_protocol(
    data_bytes: Optional[bytes],
    hex_data: str = "",
    channel: int = 0,
) -> Tuple[int, str, str, bytes]:
    """
    Classifies a packet arriving on Flux ZMQ into Banc storage metadata:
        (b2_proto_code, b3_proto_tag, banc_hex_data, hash_basis_bytes)
    - PTC (legacy/default): (0, '"NA"', hex_data, data_bytes) — 100% unchanged!
    - ATCS (b'A{...}'):     (1, 'ATCS', data_bytes.hex().upper(), canonical_bytes)
    - FRED (b'E{...}'):     (2, 'EOT'|'HOT'|'DPU'|'RLC'|'FRED', data_bytes.hex().upper(), canonical_bytes)
    """
    raw = bytes(data_bytes) if isinstance(data_bytes, (bytes, bytearray)) else b""
    if len(raw) > 2 and raw[:2] in (b"A{", b"E{"):
        is_atcs = raw[:1] == b"A"
        b2_code = PKT_PROTO_ATCS if is_atcs else PKT_PROTO_FRED
        b3_tag = "ATCS" if is_atcs else "FRED"
        inner_hex = str(hex_data or "").strip().upper()
        try:
            env = json.loads(raw[1:].decode("utf-8"))
            if isinstance(env, dict):
                proto = str(env.get("protocol") or "").strip().upper()
                if proto == "ATCS":
                    b2_code = PKT_PROTO_ATCS
                    b3_tag = "ATCS"
                elif proto in ("EOT", "HOT", "DPU", "RLC", "FRED"):
                    b2_code = PKT_PROTO_FRED
                    b3_tag = proto
                if env.get("hex"):
                    inner_hex = str(env["hex"]).strip().upper()
                if not channel and env.get("channel"):
                    try:
                        channel = int(env["channel"])
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        # Sanitize b3_tag so CSV splitting on ',' is always safe
        b3_tag = re.sub(r"[^A-Za-z0-9_-]", "", b3_tag) or ("ATCS" if b2_code == PKT_PROTO_ATCS else "FRED")
        banc_hex = raw.hex().upper()
        basis = f"{b2_code}:{b3_tag}:{int(channel or 0)}:{inner_hex or banc_hex}".encode("ascii", errors="ignore")
        return b2_code, b3_tag, banc_hex, basis

    return PKT_PROTO_PTC, '"NA"', str(hex_data or "").strip().upper(), raw


def parse_incoming_zmq_packet(frames: List[bytes]) -> Tuple[Optional[bytes], str, Optional[float], Optional[int]]:
    """
    Parses incoming ZeroMQ frames from Grabber or other external sources.
    Returns:
        (data_bytes, hex_data, rssi, channel_hint)
    Handles:
        - Grabber multipart: [mosfet_frame (b'I'...'), hex_str, metadata_json, rssi_frame]
        - Raw binary MosFET frames: [b'I'...']
        - Raw ASCII hex strings without RSSI: [b'4900...']
        - Hex strings with RSSI: [b'4900... RSSI:-68.5']
        - JSON metadata: [b'{"hex": "...", "rssi": -68.5}']
        - Comma-separated: [b'4900...,-68.5']
        - Backwards compatible with hex strings without RSSI from legacy sources.
    """
    if not frames:
        return None, "", None, None

    data_bytes = None
    hex_data = ""
    rssi = None
    channel = None

    # 1. Scan frames for metadata JSON, RSSI tags, channel hint, or explicit hex
    for frame in frames:
        if not frame:
            continue

        try:
            text = frame.decode("utf-8", errors="ignore").strip()
        except Exception:
            text = ""

        if not text:
            continue

        # Check for JSON object
        if text.startswith("{") and text.endswith("}"):
            try:
                meta = json.loads(text)
                if rssi is None:
                    if "rssi" in meta and meta["rssi"] is not None:
                        rssi = float(meta["rssi"])
                    elif "rssi_dbfs" in meta and meta["rssi_dbfs"] is not None:
                        rssi = float(meta["rssi_dbfs"])
                if channel is None and "channel" in meta and meta["channel"] is not None:
                    try:
                        channel = int(meta["channel"])
                    except (ValueError, TypeError):
                        pass
                if not hex_data and ("hex" in meta or "raw_hex" in meta):
                    h = str(meta.get("hex") or meta.get("raw_hex")).strip()
                    clean_h = re.sub(r"[^0-9a-fA-F]", "", h)
                    if clean_h and len(clean_h) % 2 == 0:
                        hex_data = clean_h.upper()
                        if data_bytes is None:
                            try:
                                data_bytes = bytes.fromhex(clean_h)
                            except Exception:
                                pass
                continue
            except Exception:
                pass

        # Check for RSSI patterns in text
        if rssi is None:
            m_rssi = re.search(r'RSSI[:=\s]*(-?\d+(?:\.\d+)?)', text, re.IGNORECASE)
            if m_rssi:
                try:
                    rssi = float(m_rssi.group(1))
                except ValueError:
                    pass
            elif re.match(r'^[0-9a-fA-F]+\s*,\s*(-?\d+(?:\.\d+)?)', text):
                m_comma = re.match(r'^[0-9a-fA-F]+\s*,\s*(-?\d+(?:\.\d+)?)', text)
                try:
                    rssi = float(m_comma.group(1))
                except ValueError:
                    pass

        # Check for Channel hint in text
        if channel is None:
            m_ch = re.search(r'(?:CH|Channel)[:=\s]*(\d+)', text, re.IGNORECASE)
            if m_ch:
                try:
                    channel = int(m_ch.group(1))
                except ValueError:
                    pass

    # 2. Extract binary payload and hex representation
    first_frame = frames[0]

    # Check if first frame is an ASCII hex string (e.g. from an external decoder)
    is_pure_ascii_hex = False
    try:
        first_text = first_frame.decode("ascii", errors="strict").strip()
        hex_match = re.search(r'^([0-9a-fA-F]{4,})', first_text)
        if hex_match:
            cand_hex = hex_match.group(1)
            rest = first_text[len(cand_hex):].strip()
            if not rest or rest.upper().startswith("RSSI") or rest.startswith(","):
                is_pure_ascii_hex = True
                if not hex_data:
                    hex_data = cand_hex.upper()
                if data_bytes is None and len(cand_hex) % 2 == 0:
                    try:
                        data_bytes = bytes.fromhex(cand_hex)
                    except Exception:
                        pass
    except (UnicodeDecodeError, ValueError):
        is_pure_ascii_hex = False

    if not is_pure_ascii_hex:
        # Binary data (e.g. MosFET frame starting with b'I' = 0x49)
        data_bytes = first_frame
        if not hex_data:
            hex_data = data_bytes.hex().upper()

    return data_bytes, hex_data, rssi, channel


class ZmqConnection:
    """
    Supervised ZeroMQ subscriber connection.
    Automatically restores and reconnects upon failure, publisher restart, or network interruption.
    Receives both single and multipart frames, extracting true RSSI while remaining backwards
    compatible with hex strings without RSSI from legacy sources.
    """
    def __init__(self, endpoint, ctx, mcast_sock, mcast_addr, stats, packet_queue):
        self.endpoint = endpoint
        self.ctx = ctx
        self.mcast_sock = mcast_sock
        self.mcast_addr = mcast_addr
        self.stats = stats
        self.packet_queue = packet_queue
        self.stats.register_endpoint(endpoint)
        
        self.is_running = True
        self.sock = None
        self.monitor = None
        self.monitor_task = None
        self.supervisor_task = asyncio.create_task(self.connection_supervisor())

    async def _setup_socket(self):
        """Creates socket, applies reconnect/keepalive options, subscribes to all, and connects."""
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.setsockopt(zmq.RECONNECT_IVL, 500)
        self.sock.setsockopt(zmq.RECONNECT_IVL_MAX, 3000)
        self.sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
        self.sock.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 30)
        self.sock.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 5)
        self.sock.setsockopt(zmq.LINGER, 0)
        
        # Subscribe to all topics to receive binary b'I' frames, hex text, and multipart
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        
        self.monitor = self.sock.get_monitor_socket()
        self.monitor.setsockopt(zmq.LINGER, 0)
        self.sock.connect(self.endpoint)
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    async def _cleanup_socket(self):
        """Safely cleans up monitor and subscriber sockets."""
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self.monitor_task = None

        if self.monitor:
            try:
                self.monitor.close(linger=0)
            except Exception:
                pass
            self.monitor = None

        if self.sock:
            try:
                self.sock.close(linger=0)
            except Exception:
                pass
            self.sock = None

    async def monitor_loop(self):
        """Monitors socket state transitions and updates connection status."""
        try:
            while self.is_running and self.monitor:
                msg = await self.monitor.recv_multipart()
                ev = parse_monitor_message(msg)
                event_id = ev['event']
                if event_id == zmq.EVENT_CONNECTED:
                    self.stats.update_status(self.endpoint, "ACTIVE")
                elif event_id in (zmq.EVENT_DISCONNECTED, zmq.EVENT_CONNECT_RETRIED, zmq.EVENT_CONNECT_DELAYED):
                    self.stats.update_status(self.endpoint, "RESTORING")
                elif event_id == zmq.EVENT_CLOSED:
                    self.stats.update_status(self.endpoint, "RESTORING")
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            if self.is_running:
                self.stats.update_status(self.endpoint, "RESTORING")

    async def connection_supervisor(self):
        """Supervises connection lifecycle and automatically restores connection after a failure."""
        backoff = 1.0
        while self.is_running:
            try:
                self.stats.update_status(self.endpoint, "INITIALIZING")
                await self._setup_socket()
                await self._read_loop()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            finally:
                if self.is_running:
                    self.stats.update_status(self.endpoint, "RESTORING")
                await self._cleanup_socket()

            if self.is_running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)
            else:
                break

    async def _read_loop(self):
        """Continuously receives messages, extracts true RSSI, and queues packets."""
        while self.is_running and self.sock:
            frames = await self.sock.recv_multipart()
            if not frames:
                continue

            data_bytes, hex_data, rssi, channel_hint = parse_incoming_zmq_packet(frames)
            if data_bytes:
                self.stats.add_packet(self.endpoint)
                
                # Send to packet buffer queue for cloud bursting only when uploads are active
                if not getattr(self.stats, "upload_paused", False):
                    await self.packet_queue.put((self.endpoint, data_bytes, hex_data, rssi, channel_hint))
                
                if self.mcast_sock and self.mcast_addr:
                    try:
                        port = int(self.endpoint.split(":")[-1])
                        ch = port - 20000 if 20000 <= port < 21000 else (port - 18000 if 18000 <= port < 19000 else 0)
                        if channel_hint and channel_hint > 0:
                            ch = channel_hint
                        ch_val = max(0, min(999, ch))
                        if rssi is not None:
                            mcast_payload = f"CH{ch_val:03d}:RSSI={rssi:.1f}:".encode("ascii") + data_bytes
                        else:
                            mcast_payload = f"CH{ch_val:03d}:".encode("ascii") + data_bytes
                        self.mcast_sock.sendto(mcast_payload, self.mcast_addr)
                    except Exception:
                        pass

    def close(self):
        """Terminates connection and cancels background supervisor task."""
        self.is_running = False
        if self.supervisor_task:
            self.supervisor_task.cancel()
        if self.monitor_task:
            self.monitor_task.cancel()
        if self.monitor:
            try:
                self.monitor.close(linger=0)
            except Exception:
                pass
            self.monitor = None
        if self.sock:
            try:
                self.sock.close(linger=0)
            except Exception:
                pass
            self.sock = None

# ---------------------------------------------------------------------------
# Cloud Bursting Routine
# ---------------------------------------------------------------------------

async def cloud_burst_loop(config, stats, packet_queue):
    cloud_srv = config.get("cloud_server")
    if not cloud_srv or str(cloud_srv).strip().lower() in ("none", "null", ""):
        stats.cloud_next_burst = "DISABLED (No Cloud Server)"
        return

    cloud_ip = normalize_host_arg(cloud_srv)
    cloud_port = config.get("cloud_port", 9000)
    auth_secret = config.get("auth_secret", "DEFAULT_SECRET")
    enc_key = config.get("encryption_key")
    max_packets = config.get("burst_max_packets", 100)
    interval_sec = config.get("burst_interval_sec", 120)  # 2 mins
    
    if not enc_key:
        print("[!] No encryption_key found in config. Generating a new one for this session.")
        enc_key = Fernet.generate_key().decode()
        print(f"[!] YOUR KEY: {enc_key}")
        
    fernet = Fernet(enc_key.encode())
    buffer = []
    start_time = time.time()
    sdr_regions = config.get("_sdr_regions") if isinstance(config.get("_sdr_regions"), dict) else {}
    last_region_sync = 0.0
    region_sync_interval = 600.0  # Refresh private blurred SDR region registration every 10 min

    async def _sync_sdr_regions_once() -> bool:
        if not sdr_regions or cloud_ip == "None" or not cloud_port:
            return False
        try:
            tok = mosfet_auth.generate_token(auth_secret)
            hdr = f"PUT_SDR_REGION:{stats.identity}:{tok}"
            enc_hdr = fernet.encrypt(hdr.encode("utf-8"))
            enc_pay = fernet.encrypt(json.dumps(sdr_regions).encode("utf-8"))
            frame = struct.pack(">I", len(enc_hdr)) + enc_hdr + struct.pack(">I", len(enc_pay)) + enc_pay
            r_reg, w_reg = await asyncio.wait_for(asyncio.open_connection(cloud_ip, cloud_port), timeout=5.0)
            try:
                w_reg.write(frame)
                await w_reg.drain()
                # Read optional ack frame cleanly without failing against older Banc servers
                try:
                    len_b = await asyncio.wait_for(r_reg.readexactly(4), timeout=3.0)
                    r_len = struct.unpack(">I", len_b)[0]
                    if 0 < r_len < 65536:
                        await asyncio.wait_for(r_reg.readexactly(r_len), timeout=3.0)
                except Exception:
                    pass
            finally:
                w_reg.close()
                await w_reg.wait_closed()
            return True
        except Exception:
            return False

    while True:
        # Privately register configured blurred SDR regions with Banc on startup & periodically
        if sdr_regions and not getattr(stats, "upload_paused", False):
            now_reg = time.time()
            if last_region_sync <= 0.0 or (now_reg - last_region_sync) >= region_sync_interval:
                ok_reg = await _sync_sdr_regions_once()
                last_region_sync = now_reg if ok_reg else (now_reg - region_sync_interval + 60.0)

        # If uploads are paused, clear buffer, drain incoming queue, and do not accumulate packets for Banc
        if getattr(stats, "upload_paused", False):
            stats.cloud_next_burst = "PAUSED"
            if buffer:
                buffer.clear()
            while not packet_queue.empty():
                try:
                    packet_queue.get_nowait()
                except Exception:
                    break
            stats.packets_in_buffer = 0
            start_time = time.time()
            await asyncio.sleep(0.5)
            continue

        try:
            # Wait for data with timeout
            time_left = max(1, interval_sec - (time.time() - start_time))
            stats.cloud_next_burst = f"in ~{int(time_left)}s or {max_packets - len(buffer)} pkts"
            stats.packets_in_buffer = len(buffer)
            
            # Short wait timeout ensures rapid reaction if uploads are paused by the user
            wait_timeout = min(1.0, max(0.1, time_left))
            queue_item = await asyncio.wait_for(packet_queue.get(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            queue_item = None

        if getattr(stats, "upload_paused", False):
            if buffer:
                buffer.clear()
            while not packet_queue.empty():
                try:
                    packet_queue.get_nowait()
                except Exception:
                    break
            stats.packets_in_buffer = 0
            stats.cloud_next_burst = "PAUSED"
            start_time = time.time()
            continue

        if queue_item is not None:
            if len(queue_item) == 5:
                endpoint, data, hex_data, rssi, channel_hint = queue_item
            elif len(queue_item) == 4:
                endpoint, data, hex_data, rssi = queue_item
                channel_hint = None
            elif len(queue_item) == 3:
                endpoint, data, rssi = queue_item
                hex_data = data.hex().upper() if isinstance(data, (bytes, bytearray)) else str(data)
                channel_hint = None
            else:
                endpoint, data = queue_item
                hex_data = data.hex().upper() if isinstance(data, (bytes, bytearray)) else str(data)
                rssi = None
                channel_hint = None
            
            # Format CSV Row
            unix_ts = time.time()
            date_str = datetime.fromtimestamp(unix_ts).strftime('%Y-%m-%d %H:%M:%S')
            
            # Calculate channel based on channel hint or port
            if channel_hint and channel_hint > 0:
                channel = channel_hint
            else:
                try:
                    port = int(endpoint.split(":")[-1])
                    channel = port - 20000 if 20000 <= port < 21000 else port - 18000
                except Exception:
                    channel = 0
            
            b2_code, b3_tag, banc_hex, hash_basis = classify_packet_protocol(data, hex_data, channel=channel)
            if b2_code == PKT_PROTO_PTC:
                pkt_hash = hash(data) & ((1<<32)-1) # 32-bit positive int (legacy PTC path)
            else:
                # Deterministic cross-node 32-bit hash for ATCS / EOT-HOT-DPU-RLC deduplication in Banc
                pkt_hash = zlib.crc32(hash_basis) & 0xFFFFFFFF

            # Relay true RSSI or fallback to "NA" / 0.0 for backwards compatibility
            if rssi is not None:
                sig_str = f"{rssi:.1f}"
                b1_val = f"{float(rssi):.2f}"
            else:
                sig_str = "NA"
                b1_val = "0.0"

            row = f"{date_str},{unix_ts},{stats.identity},{banc_hex},{channel},{sig_str},{pkt_hash},{b1_val},{b2_code},{b3_tag}"
            buffer.append(row)
            stats.packets_in_buffer = len(buffer)

        # Check if we should burst
        if not getattr(stats, "upload_paused", False):
            if len(buffer) >= max_packets or (time.time() - start_time) >= interval_sec:
                if buffer:
                    # 1. Create and Encrypt Header (Identity & Token)
                    token = mosfet_auth.generate_token(auth_secret)
                    header_str = f"PUT:{stats.identity}:{token}"
                    enc_header = fernet.encrypt(header_str.encode('utf-8'))
                    
                    # 2. Create and Encrypt Payload
                    payload_str = "\n".join(buffer)
                    enc_payload = fernet.encrypt(payload_str.encode('utf-8'))
                    
                    # 3. Format final dual-frame transmission: 
                    # [4-byte Len][Header] + [4-byte Len][Payload]
                    final_tx = struct.pack(">I", len(enc_header)) + enc_header + struct.pack(">I", len(enc_payload)) + enc_payload
                    
                    kb_size = len(final_tx) / 1024.0
                    
                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(cloud_ip, cloud_port), timeout=5.0
                        )
                        writer.write(final_tx)
                        await writer.drain()
                        writer.close()
                        await writer.wait_closed()
                        
                        stats.cloud_last_status = "SUCCESS"
                        stats.cloud_bursts += 1
                        stats.cloud_last_kb = kb_size
                        stats.cloud_total_kb += kb_size
                    except Exception as e:
                        stats.cloud_last_status = f"FAIL: {str(e)[:15]}"
                        
                    # Reset buffer
                    buffer.clear()
                    stats.packets_in_buffer = 0
                start_time = time.time()

# ---------------------------------------------------------------------------
# UI Terminal Routines
# ---------------------------------------------------------------------------

async def local_display_loop(stats):
    """Continuously prints the UI to the local console standard output."""
    try:
        while True:
            # Clear screen and move cursor to home position
            sys.stdout.write("\033[2J\033[H")
            buf = getattr(stats, "local_buffer", "")
            sys.stdout.write(stats.generate_ascii_screen(buf))
            sys.stdout.flush()
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

async def local_input_loop(stats):
    stats.local_buffer = ""
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if sys.platform == "win32" and is_tty:
        import msvcrt
        while True:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        cmd = stats.local_buffer.strip().lower()
                        stats.local_buffer = ""
                        if 'p' in cmd:
                            if hasattr(stats, "toggle_upload_pause"):
                                stats.toggle_upload_pause()
                            else:
                                stats.upload_paused = not stats.upload_paused
                                stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                                if stats.upload_paused:
                                    stats.cloud_next_burst = "PAUSED"
                                    stats.packets_in_buffer = 0
                    elif ch in ("\x08", "\x7f"):
                        if stats.local_buffer:
                            stats.local_buffer = stats.local_buffer[:-1]
                    elif ch in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                    elif ord(ch) >= 32:
                        stats.local_buffer += ch
                        if ch in ('p', 'P'):
                            stats.local_buffer = ""
                            if hasattr(stats, "toggle_upload_pause"):
                                stats.toggle_upload_pause()
                            else:
                                stats.upload_paused = not stats.upload_paused
                                stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                                if stats.upload_paused:
                                    stats.cloud_next_burst = "PAUSED"
                                    stats.packets_in_buffer = 0
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.05)
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
                tty.setcbreak(fd)
                cbreak_settings = termios.tcgetattr(fd)
                cbreak_settings[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, cbreak_settings)
                while True:
                    try:
                        rlist, _, _ = select.select([sys.stdin], [], [], 0)
                        if rlist:
                            ch = sys.stdin.read(1)
                            if ch:
                                if ch in ("\r", "\n"):
                                    cmd = stats.local_buffer.strip().lower()
                                    stats.local_buffer = ""
                                    if 'p' in cmd:
                                        if hasattr(stats, "toggle_upload_pause"):
                                            stats.toggle_upload_pause()
                                        else:
                                            stats.upload_paused = not stats.upload_paused
                                            stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                                            if stats.upload_paused:
                                                stats.cloud_next_burst = "PAUSED"
                                                stats.packets_in_buffer = 0
                                elif ch in ("\x08", "\x7f"):
                                    if stats.local_buffer:
                                        stats.local_buffer = stats.local_buffer[:-1]
                                elif ch == "\x03":
                                    sys.exit(0)
                                elif ch == "\x1b":
                                    while True:
                                        r_esc, _, _ = select.select([sys.stdin], [], [], 0.01)
                                        if r_esc:
                                            sys.stdin.read(1)
                                        else:
                                            break
                                elif ord(ch) >= 32:
                                    stats.local_buffer += ch
                                    if ch in ('p', 'P'):
                                        stats.local_buffer = ""
                                        if hasattr(stats, "toggle_upload_pause"):
                                            stats.toggle_upload_pause()
                                        else:
                                            stats.upload_paused = not stats.upload_paused
                                            stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                                            if stats.upload_paused:
                                                stats.cloud_next_burst = "PAUSED"
                                                stats.packets_in_buffer = 0
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(0.05)
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass
            return
        except Exception:
            pass

    # Fallback line-reading loop for redirected streams, pipes, and non-interactive environments
    loop = asyncio.get_running_loop() if hasattr(asyncio, "get_running_loop") else asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.5)
                continue
            cmd = line.strip().lower()
            if 'p' in cmd:
                if hasattr(stats, "toggle_upload_pause"):
                    stats.toggle_upload_pause()
                else:
                    stats.upload_paused = not stats.upload_paused
                    stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                    if stats.upload_paused:
                        stats.cloud_next_burst = "PAUSED"
                        stats.packets_in_buffer = 0
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)

async def ui_writer_task(writer, stats, session_state: dict):
    """Continuously pushes the UI display to the remote telnet client."""
    try:
        while True:
            writer.write(b"\033[2J\033[H")
            buf = session_state.get("buffer", "")
            screen_data = stats.generate_ascii_screen(buf)
            writer.write(screen_data.encode('utf-8'))
            await writer.drain()
            await asyncio.sleep(1)
    except Exception:
        pass

async def ui_reader_task(reader, stats, session_state: dict):
    """Listens for user input from the remote UI terminal to execute commands."""
    try:
        while True:
            data = await reader.read(10)
            if not data:
                break
            for b in data:
                if b in (ord('\r'), ord('\n')):
                    cmd = session_state.get("buffer", "").strip().lower()
                    session_state["buffer"] = ""
                    if 'p' in cmd:
                        if hasattr(stats, "toggle_upload_pause"):
                            stats.toggle_upload_pause()
                        else:
                            stats.upload_paused = not stats.upload_paused
                            stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                            if stats.upload_paused:
                                stats.cloud_next_burst = "PAUSED"
                                stats.packets_in_buffer = 0
                elif b in (8, 127):
                    buf = session_state.get("buffer", "")
                    if buf:
                        session_state["buffer"] = buf[:-1]
                elif 32 <= b <= 126:
                    session_state["buffer"] = session_state.get("buffer", "") + chr(b)
                    if chr(b) in ('p', 'P'):
                        session_state["buffer"] = ""
                        if hasattr(stats, "toggle_upload_pause"):
                            stats.toggle_upload_pause()
                        else:
                            stats.upload_paused = not stats.upload_paused
                            stats.cloud_last_status = "PAUSED BY USER" if stats.upload_paused else "RESUMED"
                            if stats.upload_paused:
                                stats.cloud_next_burst = "PAUSED"
                                stats.packets_in_buffer = 0
    except Exception:
        pass

async def telnet_handler(reader, writer, stats):
    """Handles incoming Telnet/NC connections with dual I/O tasks."""
    session_state = {"buffer": ""}
    w_task = asyncio.create_task(ui_writer_task(writer, stats, session_state))
    r_task = asyncio.create_task(ui_reader_task(reader, stats, session_state))
    
    # Wait until either the reader (client disconnects) or writer fails
    done, pending = await asyncio.wait(
        [w_task, r_task], return_when=asyncio.FIRST_COMPLETED
    )
    
    for task in pending:
        task.cancel()
        
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core Scanner Routines
# ---------------------------------------------------------------------------

def check_port_sync(host, port, timeout=1.5):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except Exception:
        return False

async def is_port_open(host, port):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, check_port_sync, host, port)

async def scan_endpoint(host, port, active_conns, ctx, mcast_sock, mcast_addr, stats, packet_queue):
    endpoint = f"tcp://{host}:{port}"
    if await is_port_open(host, port):
        active_conns[endpoint] = ZmqConnection(endpoint, ctx, mcast_sock, mcast_addr, stats, packet_queue)

async def autodiscover_loop(scan_pool, active_conns, ctx, mcast_sock, mcast_addr, stats, packet_queue):
    batch_size = 50
    while True:
        # Clean up any dead/stopped connections so they can be re-scanned and restored
        dead_conns = [ep for ep, conn in list(active_conns.items()) if not getattr(conn, "is_running", True)]
        for ep in dead_conns:
            active_conns.pop(ep, None)

        unconnected_pool = [(h, p) for (h, p) in scan_pool if f"tcp://{h}:{p}" not in active_conns]
        total = len(unconnected_pool)
        
        if total > 0:
            for i in range(0, total, batch_size):
                batch = unconnected_pool[i:i+batch_size]
                tasks = [
                    scan_endpoint(host, port, active_conns, ctx, mcast_sock, mcast_addr, stats, packet_queue)
                    for host, port in batch
                ]
                await asyncio.gather(*tasks)
        await asyncio.sleep(60)


async def aux_autodiscover_loop(aux_pool, active_conns, ctx, mcast_sock, mcast_addr, stats, packet_queue, interval_sec: float = 3.0):
    """
    Lightweight fast-polling auto-discovery loop for auxiliary SDR demodulators
    (ApexBOSLatch ATCS 900 MHz and ApexBOSFred 450 MHz EOT/HOT/DPU/RLC).
    Only connects once the auxiliary port is actually listening so stopped aux
    demodulators never clutter Flux's endpoint table or break PTC-only operation.
    """
    while True:
        try:
            for host, port in aux_pool:
                ep = f"tcp://{host}:{port}"
                if ep in active_conns and getattr(active_conns[ep], "is_running", True):
                    continue
                if await is_port_open(host, port):
                    active_conns[ep] = ZmqConnection(ep, ctx, mcast_sock, mcast_addr, stats, packet_queue)
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(interval_sec)


async def main():
    parser = argparse.ArgumentParser(description=f"MosFET ({VERSION}) ZMQ Multicast Relay")
    parser.add_argument("--config", type=str, default="MosFET.json", help="Path to JSON configuration file")
    parser.add_argument("--gen-config", action="store_true", help="Generate an example configuration file with all features")
    parser.add_argument("--display", action="store_true", help="Display the UI locally in the console")
    parser.add_argument("--itcmon", type=str, help="Path to an itcmon.json file to import enabled sources from")
    args = parser.parse_args()

    # Generate configuration and exit
    if args.gen_config:
        example_config = {
            "multicast_group": "239.1.1.1",
            "multicast_port": 5000,
            "command_port": 8080,
            "sources": "192.168.1.100, 192.168.1.101:20101",
            "cloud_server": "127.0.0.1",
            "cloud_port": 9000,
            "auth_secret": "EXAMPLE_SECRET_KEY_123",
            "encryption_key": Fernet.generate_key().decode(),
            "burst_max_packets": 100,
            "burst_interval_sec": 120
        }
        with open(args.config, 'w') as f:
            json.dump(example_config, f, indent=4)
        print(f"[*] Successfully generated example configuration: {args.config}")
        sys.exit(0)

    # Normal startup
    if not os.path.exists(args.config):
        print(f"Error: Configuration file '{args.config}' not found.")
        print(f"Run '{sys.argv[0]} --gen-config' to create a template.")
        sys.exit(1)

    with open(args.config, 'r') as f:
        config = json.load(f)

    # Support unified config.json root structures (capture aux Latch/Fred ports before unwrapping)
    aux_scan_pool: List[Tuple[str, int]] = []
    if isinstance(config, dict):
        latch_sec = config.get("latch") if isinstance(config.get("latch"), dict) else None
        fred_sec = config.get("fred") if isinstance(config.get("fred"), dict) else None
        if latch_sec is None or latch_sec.get("enabled", True):
            lp = int((latch_sec or {}).get("zmq_port", 18201) or 18201)
            if lp > 0:
                aux_scan_pool.append(("127.0.0.1", lp))
        if fred_sec is None or fred_sec.get("enabled", True):
            fp = int((fred_sec or {}).get("zmq_port", 18301) or 18301)
            if fp > 0:
                aux_scan_pool.append(("127.0.0.1", fp))

    # Extract optional privacy-blurred SDR reception regions from unified config before unwrapping
    extracted_sdr_regions: Dict[str, Any] = {}
    try:
        import config_loader as _cfg_loader
        extracted_sdr_regions = _cfg_loader.extract_sdr_regions_from_config(config)
    except Exception:
        extracted_sdr_regions = {}

    if "flux" in config and isinstance(config["flux"], dict):
        flux_sub = dict(config["flux"])
        if extracted_sdr_regions:
            flux_sub["_sdr_regions"] = extracted_sdr_regions
        if "common" in config and isinstance(config["common"], dict):
            common = config["common"]
            if "banc" in common:
                banc_common = common["banc"]
                if banc_common.get("enabled"):
                    b_host = banc_common.get("host")
                    if b_host and str(b_host).strip().lower() not in ("none", "null", ""):
                        cur_srv = str(flux_sub.get("cloud_server") or "").strip().lower()
                        if not cur_srv or cur_srv in ("none", "null"):
                            flux_sub["cloud_server"] = b_host
                        else:
                            flux_sub.setdefault("cloud_server", b_host)
                    flux_sub.setdefault("cloud_port", banc_common.get("port", 23009))
                    if not flux_sub.get("cloud_port") or flux_sub.get("cloud_port") == 0:
                        flux_sub["cloud_port"] = banc_common.get("port", 23009)
                    if not flux_sub.get("auth_secret"):
                        flux_sub["auth_secret"] = banc_common.get("auth_secret", "DEFAULT_SECRET")
                    if not flux_sub.get("encryption_key"):
                        flux_sub["encryption_key"] = banc_common.get("encryption_key")
                else:
                    flux_sub.setdefault("cloud_server", "None")
        config = flux_sub

    # Process itcmon integration
    if args.itcmon:
        if os.path.exists(args.itcmon):
            try:
                with open(args.itcmon, 'r') as f:
                    itcmon_data = json.load(f)
                    
                new_sources = set()
                for srv in itcmon_data.get("servers", []):
                    if srv.get("enabled"):
                        ip = srv.get("ip")
                        port = srv.get("port")
                        if ip and port:
                            new_sources.add(f"{ip}:{port}")
                            
                if new_sources:
                    raw_cfg_sources = config.get("sources", "")
                    is_list_format = isinstance(raw_cfg_sources, (list, tuple))
                    existing_sources = set()
                    for s in normalize_sources_list(raw_cfg_sources):
                        if "://" in s:
                            s = s.partition("://")[2]
                        existing_sources.add(s)
                    original_len = len(existing_sources)
                    existing_sources.update(new_sources)
                    
                    if len(existing_sources) > original_len:
                        if is_list_format:
                            config["sources"] = [f"tcp://{s}" for s in sorted(existing_sources)]
                        else:
                            config["sources"] = ", ".join(sorted(existing_sources))
                        with open(args.config, 'w') as f:
                            json.dump(config, f, indent=4)
                        if not args.display:
                            print(f"[*] Updated {args.config} with enabled sources from {args.itcmon}")
            except Exception as e:
                print(f"[!] Error parsing {args.itcmon}: {e}")
        else:
            print(f"[!] Warning: {args.itcmon} file not found.")

    # Initialize Stats and Identity
    auth_secret = config.get("auth_secret", "DEFAULT_SECRET")
    raw_cloud_srv = config.get("cloud_server", "8.8.8.8")
    if raw_cloud_srv and str(raw_cloud_srv).strip().lower() not in ("none", "null", ""):
        cloud_ip = normalize_host_arg(raw_cloud_srv)
        cloud_port = config.get("cloud_port", 9000)
    else:
        cloud_ip = "None"
        cloud_port = 0
    
    stats = StatsManager(
        auth_secret=auth_secret,
        cloud_host=cloud_ip,
        cloud_port=cloud_port
    )
    
    cfg_identity = config.get("identity")
    if cfg_identity and str(cfg_identity).strip().lower() != "auto":
        stats.identity = str(cfg_identity).strip()
    elif cloud_ip != "None":
        stats.identity = await generate_identity(cloud_ip)
    else:
        stats.identity = "LOCAL"
    
    if not args.display:
        print(f"[+] Local Node Identity established: {stats.identity}")

    # Multicast Setup
    mcast_sock, mcast_addr = None, None
    if "multicast_group" in config and "multicast_port" in config:
        mcast_addr = (config["multicast_group"], config["multicast_port"])
        mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except Exception:
            pass

    raw_sources = normalize_sources_list(config.get("sources", ""))
    direct_endpoints, scan_pool = [], []
    
    for raw_src in raw_sources:
        src = raw_src.strip()
        if not src:
            continue
        proto = "tcp://"
        if "://" in src:
            proto_part, _, rem = src.partition("://")
            proto = f"{proto_part}://"
            src = rem.strip()

        if ":" in src:
            host_part, port_part = src.rsplit(":", 1)
            if host_part.lower() in ("localhost", "127.0.0.1", ""):
                host_part = "127.0.0.1"
            endpoint = f"{proto}{host_part}:{port_part}"
            if endpoint not in direct_endpoints:
                direct_endpoints.append(endpoint)
        else:
            host_part = "127.0.0.1" if src.lower() in ("localhost", "") else src
            for p in range(18000, 18175):
                item = (host_part, p)
                if item not in scan_pool:
                    scan_pool.append(item)
            for p in range(20101, 20175):
                item = (host_part, p)
                if item not in scan_pool:
                    scan_pool.append(item)

    ctx = zmq.asyncio.Context()
    active_connections = {}
    packet_queue = asyncio.Queue()

    # Connections
    for ep in direct_endpoints:
        active_connections[ep] = ZmqConnection(ep, ctx, mcast_sock, mcast_addr, stats, packet_queue)

    if scan_pool:
        asyncio.create_task(autodiscover_loop(scan_pool, active_connections, ctx, mcast_sock, mcast_addr, stats, packet_queue))

    # Auto-attach to running ApexBOSLatch (ATCS) & ApexBOSFred (EOT/HOT/DPU/RLC) ZMQ PUB ports
    filtered_aux = [(h, p) for (h, p) in aux_scan_pool if f"tcp://{h}:{p}" not in direct_endpoints]
    if filtered_aux:
        asyncio.create_task(aux_autodiscover_loop(filtered_aux, active_connections, ctx, mcast_sock, mcast_addr, stats, packet_queue))

    # Cloud Bursting Task
    asyncio.create_task(cloud_burst_loop(config, stats, packet_queue))

    # Start Local Display Loop if requested
    if args.display:
        stats.local_buffer = ""
        asyncio.create_task(local_display_loop(stats))
        asyncio.create_task(local_input_loop(stats))

    # UI Server
    cmd_port = config.get("command_port", 8080)
    server = await asyncio.start_server(lambda r, w: telnet_handler(r, w, stats), '0.0.0.0', cmd_port)
    
    if not args.display:
        print(f"Command UI server started on 0.0.0.0:{cmd_port}. Use 'nc <IP_ADDRESS> {cmd_port}' to view stats.")

    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        # Cross-platform support for asyncio + Windows ANSI rendering
        if sys.platform == 'win32':
            os.system('color') # Enable ANSI escape codes in Windows cmd/powershell
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        # Clear screen on exit cleanly if running display mode
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        print("\nApexBOSFlux shut down successfully.")

# ---------------------------------------------------------------------------
# Rebranded Backward Compatibility Aliases
# ---------------------------------------------------------------------------
ApexBOSFlux = StatsManager
ApexBOSFluxStats = StatsManager

# Register module alias for backward compatibility
import sys as _sys
_sys.modules.setdefault("MosFET", _sys.modules[__name__])
_sys.modules.setdefault("mosfet", _sys.modules[__name__])
