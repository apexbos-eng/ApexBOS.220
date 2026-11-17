#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mosfet_client.py - API Client for MosFET Multicast and Banc Cloud

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
import socket
import struct
import subprocess
import ipaddress
import asyncio
import argparse
import time
import re
import hashlib
from typing import Optional, Any, Dict, List, Tuple, Union
from cryptography.fernet import Fernet
import mosfet_auth

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

# ---------------------------------------------------------------------------
# Identity Generation & IPv4/IPv6 Host Helpers
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
        # Dual-stack hostname: return the OS-preferred first entry
        return 6 if infos[0][0] == socket.AF_INET6 else 4
    except OSError:
        return None


def format_host_port(host: str, port: int) -> str:
    """Formats host and port cleanly, wrapping IPv6 literals in square brackets."""
    clean = normalize_host_arg(host)
    if detect_ip_version(clean) == 6:
        return f"[{clean}]:{int(port)}"
    return f"{clean}:{int(port)}"


IDENTITY_HASH_PREFIX = "apexbos-lan-v1:"
_VIRTUAL_IFACE_PREFIXES = (
    "tun", "tap", "wg", "ppp", "utun", "ipsec",
    "tailscale", "zt", "docker", "veth", "virbr", "br-", "lo",
)
_SHARED_VIRTUAL_ROUTER_MAC_PREFIXES = (
    "00:00:5e:00:01:",  # VRRP IPv4 / CARP
    "00:00:5e:00:02:",  # VRRP IPv6
    "00:00:0c:07:ac:",  # Cisco HSRP v1
    "00:00:0c:9f:f",    # Cisco HSRP v2
    "00:07:b4:00:",     # Cisco GLBP
)
_MAC_TOKEN_RE = re.compile(r"^(?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}$")


def normalize_mac_address(mac_str: Optional[str]) -> Optional[str]:
    """Normalizes a MAC address to lowercase colon-separated zero-padded format (aa:bb:cc:dd:ee:ff).

    Rejects blank, malformed, all-zero, broadcast, and multicast MAC addresses.
    """
    if not mac_str:
        return None
    raw = str(mac_str).strip().lower()
    if not _MAC_TOKEN_RE.match(raw):
        return None
    sep = ":" if ":" in raw else "-"
    parts = raw.split(sep)
    if len(parts) != 6:
        return None
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError:
        return None
    if any(b < 0 or b > 255 for b in octets):
        return None
    # Reject all-zero, broadcast, and multicast (LSB of first octet == 1)
    if all(b == 0 for b in octets) or all(b == 255 for b in octets):
        return None
    if (octets[0] & 1) != 0:
        return None
    return ":".join(f"{b:02x}" for b in octets)


def is_shared_virtual_router_mac(norm_mac: str) -> bool:
    """Returns True if a normalized MAC belongs to a shared virtual router protocol (VRRP/HSRP/GLBP)."""
    if not norm_mac:
        return False
    low = norm_mac.lower()
    return any(low.startswith(pref) for pref in _SHARED_VIRTUAL_ROUTER_MAC_PREFIXES)


def hash_network_identity(material: str) -> str:
    """Hashes canonical network identity material into a 12-character hex Banc identity."""
    payload = f"{IDENTITY_HASH_PREFIX}{material}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _is_virtual_iface_name(iface: str) -> bool:
    low = str(iface or "").strip().lower()
    return any(low.startswith(pref) for pref in _VIRTUAL_IFACE_PREFIXES)


def _parse_windows_ipv4_default_gateways(route_output: str) -> List[Tuple[int, str, str]]:
    """Parses `route print 0.0.0.0` Active Routes into sorted (metric, gateway_ip, iface_ip) tuples."""
    results: List[Tuple[int, str, str]] = []
    in_active = False
    for line in (route_output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Active Routes:"):
            in_active = True
            continue
        if in_active and (stripped.startswith("Persistent Routes:") or stripped.startswith("IPv6 Route Table")):
            break
        if not in_active:
            continue
        parts = stripped.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw_ip, iface_ip, metric_s = parts[2], parts[3], parts[4]
            try:
                addr = ipaddress.ip_address(gw_ip)
                if addr.version != 4 or addr.is_unspecified or addr.is_loopback:
                    continue
                metric = int(metric_s)
            except ValueError:
                continue
            results.append((metric, str(addr), iface_ip))
    results.sort(key=lambda item: (item[0], item[1]))
    return results


def _parse_windows_arp_mac(arp_output: str, target_ip: str) -> Optional[str]:
    """Extracts the normalized MAC address for target_ip from Windows `arp -a` output."""
    for line in (arp_output or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == target_ip:
            mac = normalize_mac_address(parts[1])
            if mac:
                return mac
    return None


def _parse_windows_ipv6_default_gateways(route_output: str) -> List[Tuple[int, str, str]]:
    """Parses `netsh interface ipv6 show route` for ::/0 default routes into (metric, gw_ip, iface_idx)."""
    results: List[Tuple[int, str, str]] = []
    for line in (route_output or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 6 and parts[3] == "::/0":
            try:
                metric = int(parts[2])
            except ValueError:
                metric = 9999
            iface_idx = parts[4]
            gw_cand = parts[5].split("%", 1)[0]
            try:
                addr = ipaddress.ip_address(gw_cand)
                if addr.version == 6 and not addr.is_unspecified and not addr.is_loopback:
                    results.append((metric, str(addr), iface_idx))
            except ValueError:
                continue
    results.sort(key=lambda item: (item[0], item[1]))
    return results


def _parse_windows_ipv6_neighbor_mac(
    neigh_output: str, target_ip: str, iface_idx: Optional[str] = None
) -> Optional[str]:
    """Extracts the normalized MAC for an IPv6 neighbor from `netsh interface ipv6 show neighbors`."""
    try:
        canonical_target = str(ipaddress.ip_address(target_ip.split("%", 1)[0]))
    except ValueError:
        return None
    cur_idx: Optional[str] = None
    fallback_mac: Optional[str] = None
    for line in (neigh_output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m_if = re.match(r"^Interface\s+(\d+)\s*:", stripped, re.IGNORECASE)
        if m_if:
            cur_idx = m_if.group(1)
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            ip_tok = parts[0].split("%", 1)[0]
            try:
                cand_ip = str(ipaddress.ip_address(ip_tok))
            except ValueError:
                continue
            if cand_ip == canonical_target:
                mac = normalize_mac_address(parts[1])
                if mac:
                    if iface_idx is None or cur_idx == str(iface_idx):
                        return mac
                    if fallback_mac is None:
                        fallback_mac = mac
    return fallback_mac


def _parse_linux_proc_net_route(route_text: str) -> List[Tuple[int, int, str, str]]:
    """Parses Linux `/proc/net/route` into sorted (is_virtual, metric, gateway_ip, iface) tuples."""
    results: List[Tuple[int, int, str, str]] = []
    for idx, line in enumerate((route_text or "").splitlines()):
        if idx == 0:
            continue
        parts = line.strip().split()
        if len(parts) < 8:
            continue
        iface, dest_hex, gw_hex, flags_hex = parts[0], parts[1], parts[2], parts[3]
        metric_s, mask_hex = parts[6], parts[7]
        if dest_hex != "00000000" or mask_hex != "00000000" or gw_hex == "00000000":
            continue
        try:
            flags = int(flags_hex, 16)
            if (flags & 0x3) != 0x3:  # RTF_UP | RTF_GATEWAY
                continue
            metric = int(metric_s)
            gw_ip = socket.inet_ntoa(struct.pack("=I", int(gw_hex, 16)))
        except (ValueError, struct.error, OSError):
            continue
        is_virt = 1 if _is_virtual_iface_name(iface) else 0
        results.append((is_virt, metric, gw_ip, iface))
    results.sort(key=lambda item: (item[0], item[1], item[3]))
    return results


def _parse_linux_proc_net_arp(
    arp_text: str, target_ip: str, iface: Optional[str] = None
) -> Optional[str]:
    """Extracts the normalized MAC for target_ip from Linux `/proc/net/arp`."""
    fallback_mac: Optional[str] = None
    for idx, line in enumerate((arp_text or "").splitlines()):
        if idx == 0:
            continue
        parts = line.strip().split()
        if len(parts) < 6:
            continue
        ip_col, _hw_type, flags_col, mac_col, _mask, dev_col = parts[:6]
        if ip_col != target_ip or flags_col == "0x0":
            continue
        mac = normalize_mac_address(mac_col)
        if mac:
            if not iface or dev_col == iface:
                return mac
            if fallback_mac is None:
                fallback_mac = mac
    return fallback_mac


def _parse_linux_ip_route_default(route_output: str, ipv6: bool = False) -> List[Tuple[int, int, str, str]]:
    """Parses `ip [-6] route show default` into sorted (is_virtual, metric, gateway_ip, iface) tuples."""
    results: List[Tuple[int, int, str, str]] = []
    for line in (route_output or "").splitlines():
        parts = line.strip().split()
        if not parts or parts[0] != "default":
            continue
        gw_ip = ""
        iface = ""
        metric = 1024
        for i, tok in enumerate(parts):
            if tok == "via" and i + 1 < len(parts):
                gw_ip = parts[i + 1].split("%", 1)[0]
            elif tok == "dev" and i + 1 < len(parts):
                iface = parts[i + 1]
            elif tok == "metric" and i + 1 < len(parts):
                try:
                    metric = int(parts[i + 1])
                except ValueError:
                    pass
        if not gw_ip:
            continue
        try:
            addr = ipaddress.ip_address(gw_ip)
            expected_ver = 6 if ipv6 else 4
            if addr.version != expected_ver or addr.is_unspecified or addr.is_loopback:
                continue
            gw_ip = str(addr)
        except ValueError:
            continue
        is_virt = 1 if _is_virtual_iface_name(iface) else 0
        results.append((is_virt, metric, gw_ip, iface))
    results.sort(key=lambda item: (item[0], item[1], item[3]))
    return results


def _parse_linux_ip_neigh_mac(neigh_output: str, target_ip: str) -> Optional[str]:
    """Extracts normalized MAC from `ip [-6] neigh show` output."""
    try:
        canonical_target = str(ipaddress.ip_address(target_ip.split("%", 1)[0]))
    except ValueError:
        return None
    for line in (neigh_output or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if "FAILED" in parts or "INCOMPLETE" in parts:
            continue
        try:
            cand_ip = str(ipaddress.ip_address(parts[0].split("%", 1)[0]))
        except ValueError:
            continue
        if cand_ip != canonical_target:
            continue
        for i, tok in enumerate(parts):
            if tok == "lladdr" and i + 1 < len(parts):
                mac = normalize_mac_address(parts[i + 1])
                if mac:
                    return mac
    return None


def _parse_macos_default_gateways(
    route_output: str, netstat_output: str = "", ipv6: bool = False
) -> List[Tuple[int, str, str]]:
    """Parses macOS `route -n get default` and `netstat -rn` into (is_virtual, gateway_ip, iface) tuples."""
    candidates: List[Tuple[int, str, str]] = []
    seen = set()
    expected_ver = 6 if ipv6 else 4

    def _add(gw_raw: str, iface: str):
        gw_clean = (gw_raw or "").split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(gw_clean)
            if addr.version != expected_ver or addr.is_unspecified or addr.is_loopback:
                return
            gw_s = str(addr)
        except ValueError:
            return
        key = (gw_s, iface)
        if key not in seen:
            seen.add(key)
            candidates.append((1 if _is_virtual_iface_name(iface) else 0, gw_s, iface))

    # Parse `route -n get [-inet6] default`
    gw_val = ""
    if_val = ""
    for line in (route_output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("gateway:"):
            gw_val = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("interface:"):
            if_val = stripped.split(":", 1)[1].strip()
    if gw_val:
        _add(gw_val, if_val)

    # Parse `netstat -rn -f inet[6]` to catch physical gateways behind a VPN utun default route
    for line in (netstat_output or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 4 and parts[0] == "default":
            _add(parts[1], parts[3])

    candidates.sort(key=lambda item: (item[0], item[2]))
    return candidates


def _parse_macos_arp_mac(arp_output: str, target_ip: str) -> Optional[str]:
    """Extracts normalized MAC from macOS `arp -n <ip>` output (handles unpadded hex octets)."""
    for line in (arp_output or "").splitlines():
        if f"({target_ip})" not in line:
            continue
        m = re.search(r"\bat\s+([0-9a-fA-F:.-]+)\b", line)
        if m:
            mac = normalize_mac_address(m.group(1))
            if mac:
                return mac
    return None


def _parse_macos_ndp_mac(ndp_output: str, target_ip: str) -> Optional[str]:
    """Extracts normalized MAC from macOS `ndp -an` output."""
    try:
        canonical_target = str(ipaddress.ip_address(target_ip.split("%", 1)[0]))
    except ValueError:
        return None
    for line in (ndp_output or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        try:
            cand_ip = str(ipaddress.ip_address(parts[0].split("%", 1)[0]))
        except ValueError:
            continue
        if cand_ip == canonical_target:
            mac = normalize_mac_address(parts[1])
            if mac:
                return mac
    return None


async def _run_cmd_text(cmd: List[str], timeout: float = 3.0) -> str:
    """Runs an OS networking query command safely with a short timeout and returns decoded stdout."""
    kwargs: Dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return ""
        return stdout.decode(errors="ignore")
    except Exception:
        return ""


def _nudge_arp_cache(gateway_ip: str) -> None:
    """Sends a harmless 1-byte UDP datagram to the gateway discard port (9) to populate ARP/NDP cache."""
    sock = None
    try:
        addr = ipaddress.ip_address(gateway_ip.split("%", 1)[0])
        fam = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.sendto(b"\x00", (str(addr), 9))
    except Exception:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _get_outbound_local_network_material(target_host: str = "") -> Optional[str]:
    """Determines a privacy-safe local network identifier without contacting any third-party server.

    Uses only UDP socket connect() (OS routing table query; transmits no packets on the wire) to
    inspect the local outbound address:
      - Global IPv6 address -> shared LAN /64 prefix (consistent across all hosts on the same LAN)
      - Public IPv4 address -> direct WAN IPv4 (for hosts directly attached to PPPoE/VPS without NAT)
    """
    clean_host = normalize_host_arg(target_host)
    probes: List[Tuple[int, str]] = []
    if clean_host:
        ver = detect_ip_version(clean_host)
        if ver == 6:
            probes.append((socket.AF_INET6, clean_host))
        elif ver == 4:
            probes.append((socket.AF_INET, clean_host))
        else:
            try:
                for info in socket.getaddrinfo(clean_host, 80, socket.AF_UNSPEC, socket.SOCK_DGRAM):
                    probes.append((info[0], info[4][0].split("%", 1)[0]))
            except OSError:
                pass
    probes.extend([(socket.AF_INET6, "2606:4700:4700::1111"), (socket.AF_INET, "1.1.1.1")])

    for fam, probe_ip in probes:
        sock = None
        try:
            sock = socket.socket(fam, socket.SOCK_DGRAM)
            sock.connect((probe_ip, 80))
            local_ip = sock.getsockname()[0].split("%", 1)[0]
            addr = ipaddress.ip_address(local_ip)
            if addr.version == 6 and addr.is_global:
                net64 = ipaddress.IPv6Network(f"{addr}/64", strict=False)
                return f"ipv6-prefix:{net64}"
            if addr.version == 4 and addr.is_global:
                return f"pub4:{addr}"
        except Exception:
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return None


async def _resolve_default_gateway_mac_windows() -> Tuple[Optional[str], Optional[str], str]:
    """Resolves (normalized_mac, gateway_ip, proto) on Windows. Prefers physical IPv4 gateway first."""
    route4 = await _run_cmd_text(["route", "print", "0.0.0.0"])
    gw4_list = _parse_windows_ipv4_default_gateways(route4)
    for _metric, gw_ip, _iface_ip in gw4_list:
        arp_out = await _run_cmd_text(["arp", "-a", gw_ip])
        mac = _parse_windows_arp_mac(arp_out, gw_ip)
        if not mac:
            _nudge_arp_cache(gw_ip)
            await asyncio.sleep(0.06)
            arp_out = await _run_cmd_text(["arp", "-a", gw_ip])
            mac = _parse_windows_arp_mac(arp_out, gw_ip)
        if mac:
            return mac, gw_ip, "ipv4"

    # IPv6-only fallback
    route6 = await _run_cmd_text(["netsh", "interface", "ipv6", "show", "route"])
    gw6_list = _parse_windows_ipv6_default_gateways(route6)
    if gw6_list:
        neigh6 = await _run_cmd_text(["netsh", "interface", "ipv6", "show", "neighbors"])
        for _metric, gw6_ip, iface_idx in gw6_list:
            mac = _parse_windows_ipv6_neighbor_mac(neigh6, gw6_ip, iface_idx)
            if mac:
                return mac, gw6_ip, "ipv6"

    return None, (gw4_list[0][1] if gw4_list else None), "none"


async def _resolve_default_gateway_mac_linux() -> Tuple[Optional[str], Optional[str], str]:
    """Resolves (normalized_mac, gateway_ip, proto) on Linux/Raspberry Pi. Prefers physical IPv4 gateway."""
    gw4_list: List[Tuple[int, int, str, str]] = []
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as fh:
            gw4_list = _parse_linux_proc_net_route(fh.read())
    except OSError:
        pass
    if not gw4_list:
        ip_route4 = await _run_cmd_text(["ip", "-4", "route", "show", "default"])
        gw4_list = _parse_linux_ip_route_default(ip_route4, ipv6=False)

    for _is_virt, _metric, gw_ip, iface in gw4_list:
        mac: Optional[str] = None
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as fh:
                mac = _parse_linux_proc_net_arp(fh.read(), gw_ip, iface)
        except OSError:
            pass
        if not mac:
            neigh_out = await _run_cmd_text(["ip", "-4", "neigh", "show", gw_ip])
            mac = _parse_linux_ip_neigh_mac(neigh_out, gw_ip)
        if not mac:
            _nudge_arp_cache(gw_ip)
            await asyncio.sleep(0.06)
            try:
                with open("/proc/net/arp", "r", encoding="utf-8") as fh:
                    mac = _parse_linux_proc_net_arp(fh.read(), gw_ip, iface)
            except OSError:
                pass
            if not mac:
                neigh_out = await _run_cmd_text(["ip", "-4", "neigh", "show", gw_ip])
                mac = _parse_linux_ip_neigh_mac(neigh_out, gw_ip)
        if mac:
            return mac, gw_ip, "ipv4"

    # IPv6-only fallback
    ip_route6 = await _run_cmd_text(["ip", "-6", "route", "show", "default"])
    gw6_list = _parse_linux_ip_route_default(ip_route6, ipv6=True)
    for _is_virt, _metric, gw6_ip, iface in gw6_list:
        cmd = ["ip", "-6", "neigh", "show", gw6_ip]
        if iface:
            cmd.extend(["dev", iface])
        neigh6 = await _run_cmd_text(cmd)
        mac = _parse_linux_ip_neigh_mac(neigh6, gw6_ip)
        if mac:
            return mac, gw6_ip, "ipv6"

    return None, (gw4_list[0][2] if gw4_list else None), "none"


async def _resolve_default_gateway_mac_macos() -> Tuple[Optional[str], Optional[str], str]:
    """Resolves (normalized_mac, gateway_ip, proto) on macOS. Prefers physical IPv4 gateway first."""
    route4 = await _run_cmd_text(["route", "-n", "get", "default"])
    netstat4 = await _run_cmd_text(["netstat", "-rn", "-f", "inet"])
    gw4_list = _parse_macos_default_gateways(route4, netstat4, ipv6=False)
    for _is_virt, gw_ip, _iface in gw4_list:
        arp_out = await _run_cmd_text(["arp", "-n", gw_ip])
        mac = _parse_macos_arp_mac(arp_out, gw_ip)
        if not mac:
            _nudge_arp_cache(gw_ip)
            await asyncio.sleep(0.06)
            arp_out = await _run_cmd_text(["arp", "-n", gw_ip])
            mac = _parse_macos_arp_mac(arp_out, gw_ip)
        if mac:
            return mac, gw_ip, "ipv4"

    # IPv6-only fallback
    route6 = await _run_cmd_text(["route", "-n", "get", "-inet6", "default"])
    netstat6 = await _run_cmd_text(["netstat", "-rn", "-f", "inet6"])
    gw6_list = _parse_macos_default_gateways(route6, netstat6, ipv6=True)
    if gw6_list:
        ndp_out = await _run_cmd_text(["ndp", "-an"])
        for _is_virt, gw6_ip, _iface in gw6_list:
            mac = _parse_macos_ndp_mac(ndp_out, gw6_ip)
            if mac:
                return mac, gw6_ip, "ipv6"

    return None, (gw4_list[0][1] if gw4_list else None), "none"


async def resolve_network_identity_details(target_host: str = "") -> Tuple[str, str]:
    """Computes a stable, home-network-wide 12-char identity hash and a privacy-safe source label.

    Resolution order:
      1. Physical IPv4 default gateway MAC address (skips VPN/TUN routes without ARP MACs).
      2. IPv6 default gateway neighbor MAC address (for IPv6-only networks).
      3. Outbound global IPv6 /64 prefix or public IPv4 (no third-party lookup; for PPPoE/VPS).
      4. Deterministic local fallback hash.
    """
    try:
        if sys.platform == "win32":
            gw_mac, gw_ip, proto = await _resolve_default_gateway_mac_windows()
        elif sys.platform == "darwin":
            gw_mac, gw_ip, proto = await _resolve_default_gateway_mac_macos()
        else:
            gw_mac, gw_ip, proto = await _resolve_default_gateway_mac_linux()

        if gw_mac:
            if is_shared_virtual_router_mac(gw_mac):
                extra = _get_outbound_local_network_material(target_host) or (gw_ip or "lan")
                return hash_network_identity(f"virt:{gw_mac}:{extra}"), f"virtual-gw-mac-{proto}"
            return hash_network_identity(f"gw:{gw_mac}"), f"gateway-mac-{proto}"

        outbound_mat = _get_outbound_local_network_material(target_host)
        if outbound_mat:
            src = "ipv6-prefix" if outbound_mat.startswith("ipv6-prefix:") else "public-ipv4"
            return hash_network_identity(outbound_mat), src

        if gw_ip:
            return hash_network_identity(f"gw-ip:{gw_ip}"), "gateway-ip-fallback"
        return hash_network_identity("local"), "local-fallback"
    except Exception:
        return hash_network_identity("local"), "local-fallback"


async def generate_identity(target_host: str = "") -> str:
    """Computes a deterministic 12-char SHA-256 network identity anchored to the LAN default gateway MAC.

    All computers on the same home LAN produce the identical identity hash while remaining globally
    unique across different users' networks and completely immune to ISP routing/IP churn.
    """
    ident, _source = await resolve_network_identity_details(target_host)
    return ident


def extract_ips_from_traceroute(output_text: str) -> List[str]:
    """Legacy helper retained for backward compatibility: extracts canonical IPv4/IPv6 addresses."""
    ips: List[str] = []
    for raw_tok in re.findall(r"[0-9A-Fa-f:.%]+", output_text or ""):
        cand = raw_tok.strip(".:[]()")
        if "%" in cand:
            cand = cand.split("%", 1)[0]
        if not cand or ("." not in cand and ":" not in cand):
            continue
        try:
            addr = ipaddress.ip_address(cand)
            if getattr(addr, "ipv4_mapped", None) is not None:
                addr = addr.ipv4_mapped
            ips.append(str(addr))
        except ValueError:
            continue
    return ips

# ---------------------------------------------------------------------------
# Banc Cloud Client (TCP / Encrypted)
# ---------------------------------------------------------------------------

def _run_sync(coro):
    """Executes a coroutine synchronously, safely handling environments with an existing running loop."""
    try:
        loop = asyncio.get_running_loop()
    except (RuntimeError, AttributeError):
        try:
            cur = asyncio.get_event_loop()
            loop = cur if (cur is not None and cur.is_running()) else None
        except Exception:
            loop = None
    if loop and loop.is_running():
        import concurrent.futures
        def _thread_worker():
            try:
                return asyncio.run(coro)
            except RuntimeError:
                new_loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(new_loop)
                    return new_loop.run_until_complete(coro)
                finally:
                    try:
                        new_loop.close()
                    finally:
                        asyncio.set_event_loop(None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_thread_worker).result()
    else:
        return asyncio.run(coro)


class BancClient:
    """Client for securely retrieving historical data from the Banc cloud service."""
    
    def __init__(self, host: str, port: int, auth_secret: Optional[str] = None, encryption_key: Optional[str] = None, identity: Optional[str] = "Client", secret_key: Optional[str] = None):
        self.host = normalize_host_arg(host)
        self.port = int(port)
        self.auth_secret = auth_secret or secret_key
        self.identity = identity or "Client"
        key = str(encryption_key or "").strip()
        self.encryption_key = key if len(key) == 44 else None
        if len(key) == 44:
            try:
                self.fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
            except Exception:
                self.fernet = None
        else:
            self.fernet = None

    async def _read_frame(self, reader):
        """Reads and decrypts a length-prefixed frame."""
        if not self.fernet:
            raise ValueError("BancClient encryption_key is missing or invalid (expected 44-character Fernet key)")
        length_bytes = await reader.readexactly(4)
        frame_len = struct.unpack(">I", length_bytes)[0]
        if frame_len > 50 * 1024 * 1024:  # 50MB sanity limit for GET requests
            raise ValueError("Response frame too large")
        enc_data = await reader.readexactly(frame_len)
        return self.fernet.decrypt(enc_data).decode('utf-8')

    async def _write_frame(self, writer, data_str):
        """Encrypts and writes a string with a 4-byte length prefix."""
        if not self.fernet:
            raise ValueError("BancClient encryption_key is missing or invalid (expected 44-character Fernet key)")
        enc_data = self.fernet.encrypt(data_str.encode('utf-8'))
        writer.write(struct.pack(">I", len(enc_data)) + enc_data)
        await writer.drain()

    async def get_packets(self, timeout=15.0, since_ts: Optional[float] = None):
        """
        Connects to Banc, authenticates, and retrieves the recent packets.
        Returns a list of dictionaries representing the database rows.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        
        try:
            # 1. Format and send Header (Identity & Token)
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"GET:{self.identity}:{token}"
            if since_ts is not None and float(since_ts) > 0.0:
                header_str += f":{float(since_ts):.3f}"
            await self._write_frame(writer, header_str)

            # 2. Wait for the encrypted payload response
            payload = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            
            # 3. Parse CSV rows
            parsed_rows = []
            for line in payload.split("\n"):
                if not line.strip(): continue
                cols = line.split(",")
                # ApexBOSBanc.py GET returns: date_str, unix_ts, hex_data, channel, signal_strength, pkt_hash, b1, b2, b3, stamp_received
                if len(cols) == 10:
                    parsed_rows.append({
                        "date_str": cols[0],
                        "unix_ts": float(cols[1]),
                        "hex_data": cols[2],
                        "channel": int(cols[3]),
                        "signal_strength": cols[4],
                        "pkt_hash": int(cols[5]),
                        "b1": float(cols[6]),
                        "b2": int(cols[7]),
                        "b3": cols[8],
                        "stamp_received": float(cols[9])
                    })
            return parsed_rows

        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def upload_layout(self, layout_name: str, layout_dict_or_json: Any, timeout: float = 15.0) -> dict:
        """
        Uploads a layout to Banc Cloud.
        Banc calculates its center mass GPS and radius, stores it, and returns metadata.
        """
        if isinstance(layout_dict_or_json, (dict, list)):
            payload_str = json.dumps(layout_dict_or_json)
        else:
            payload_str = str(layout_dict_or_json)

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"PUT_LAYOUT:{self.identity}:{token}:{layout_name}"
            await self._write_frame(writer, header_str)
            await self._write_frame(writer, payload_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def list_layouts(self, timeout: float = 10.0) -> List[Dict]:
        """
        Retrieves list of all available and unavailable layouts on Banc Cloud,
        including center mass GPS (lat, lon), maximum radius (km), and availability status.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"GET_LAYOUTS:{self.identity}:{token}"
            await self._write_frame(writer, header_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def download_layout(self, layout_name: str, timeout: float = 15.0) -> Dict:
        """
        Downloads a requested layout from Banc Cloud.
        If the layout is marked unavailable, Banc rejects the download and this raises RuntimeError.
        Returns the parsed layout dictionary.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"GET_LAYOUT:{self.identity}:{token}:{layout_name}"
            await self._write_frame(writer, header_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def upload_layout_sync(self, layout_name: str, layout_dict_or_json: Any, timeout: float = 15.0) -> Dict:
        return _run_sync(self.upload_layout(layout_name, layout_dict_or_json, timeout=timeout))

    def list_layouts_sync(self, timeout: float = 10.0) -> List[Dict]:
        return _run_sync(self.list_layouts(timeout=timeout))

    def download_layout_sync(self, layout_name: str, timeout: float = 15.0) -> Optional[Dict]:
        return _run_sync(self.download_layout(layout_name, timeout=timeout))

    async def put_wius(self, wius: Any, timeout: float = 10.0) -> dict:
        """Uploads one or more WIU metadata records to Banc Cloud for storage in the wius table."""
        if isinstance(wius, (dict, list)):
            payload_str = json.dumps(wius)
        else:
            payload_str = str(wius)

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"PUT_WIUS:{self.identity}:{token}"
            await self._write_frame(writer, header_str)
            await self._write_frame(writer, payload_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def get_wius(self, timeout: float = 10.0) -> List[Dict]:
        """Retrieves all stored WIU records from Banc Cloud."""
        connect_timeout = max(0.1, min(5.0, float(timeout)))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=connect_timeout
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"GET_WIUS:{self.identity}:{token}"
            await self._write_frame(writer, header_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def put_wius_sync(self, wius: Any, timeout: float = 10.0) -> dict:
        return _run_sync(self.put_wius(wius, timeout=timeout))

    def get_wius_sync(self, timeout: float = 10.0) -> List[Dict]:
        return _run_sync(self.get_wius(timeout=timeout))

    async def send_chat_message(
        self,
        text: str,
        target_identities: Optional[List[str]] = None,
        timeout: float = 8.0,
    ) -> dict:
        """Encrypts and sends a sanitized short chat message to Banc Cloud."""
        clean_text = sanitize_chat_text(text)
        if not clean_text:
            raise ValueError("Chat message is empty after sanitization")

        mentions = extract_chat_mentions(clean_text)
        targets: List[str] = []
        for t in (target_identities or []) + mentions:
            st = sanitize_chat_identity(t)
            if st and st not in targets:
                targets.append(st)

        payload_obj: Dict[str, Any] = {
            "text": clean_text,
            "target_identities": targets,
            "timestamp": time.time(),
        }
        payload_str = json.dumps(payload_obj)
        if len(payload_str.encode("utf-8")) > MAX_CHAT_PAYLOAD_BYTES:
            raise ValueError("Chat payload exceeds maximum allowed size")

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            header_str = f"PUT_CHAT:{sanitize_chat_identity(self.identity) or 'Client'}:{token}"
            await self._write_frame(writer, header_str)
            await self._write_frame(writer, payload_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            return json.loads(resp_str)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def get_chat_messages(
        self,
        since_id: int = 0,
        since_ts: float = 0.0,
        timeout: float = 8.0,
    ) -> List[Dict]:
        """Retrieves and decrypts chat messages from Banc Cloud addressed to this identity or broadcast to all."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            token = mosfet_auth.generate_token(self.auth_secret)
            ident = sanitize_chat_identity(self.identity) or "Client"
            extra = f"{int(max(0, since_id))}|{float(max(0.0, since_ts)):.6f}"
            header_str = f"GET_CHAT:{ident}:{token}:{extra}"
            await self._write_frame(writer, header_str)
            resp_str = await asyncio.wait_for(self._read_frame(reader), timeout=timeout)
            if resp_str.startswith("ERROR:"):
                raise RuntimeError(resp_str)
            data = json.loads(resp_str)
            if isinstance(data, dict):
                return list(data.get("messages", []))
            if isinstance(data, list):
                return data
            return []
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def send_chat_message_sync(
        self,
        text: str,
        target_identities: Optional[List[str]] = None,
        timeout: float = 8.0,
    ) -> dict:
        return _run_sync(self.send_chat_message(text, target_identities=target_identities, timeout=timeout))

    def get_chat_messages_sync(
        self,
        since_id: int = 0,
        since_ts: float = 0.0,
        timeout: float = 8.0,
    ) -> List[Dict]:
        return _run_sync(self.get_chat_messages(since_id=since_id, since_ts=since_ts, timeout=timeout))

    # Aliases for convenience
    put_chat = send_chat_message
    put_chat_sync = send_chat_message_sync
    get_chat = get_chat_messages
    get_chat_sync = get_chat_messages_sync


# ---------------------------------------------------------------------------
# Chat Security & Sanitization Helpers
# ---------------------------------------------------------------------------

MAX_CHAT_MESSAGE_LEN = 280
MAX_CHAT_PAYLOAD_BYTES = 4096
MAX_CHAT_IDENTITY_LEN = 64

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_HTML_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style|iframe|object|embed|svg|math|form|input|button|meta|link|base)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
_JS_EVENT_OR_SCHEME_RE = re.compile(
    r"(?i)\b(?:javascript\s*:|vbscript\s*:|data\s*:\s*text/html|on(?:load|error|click|mouseover|focus|blur|submit|keydown|keyup)\s*=)"
)
_CODE_INJECTION_RE = re.compile(
    r"(?i)(?:__import__\s*\(|__builtins__|__subclasses__|__globals__|__code__|\beval\s*\(|\bexec\s*\(|\bcompile\s*\(|\bos\.system\s*\(|\bos\.popen\s*\(|\bsubprocess\b|\bimportlib\b|\bpickle\.loads\b|\$\{[^}]*\}|`[^`]*`)"
)
_SQL_INJECTION_RE = re.compile(
    r"(?i)(?:['\";]\s*(?:--\s*|/\*|\b(?:DROP|ALTER|TRUNCATE|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|EXEC(?:UTE)?|XP_CMDSHELL)\b)|\bUNION\s+(?:ALL\s+)?SELECT\b|\bOR\s+['\"]?1['\"]?\s*=\s*['\"]?1['\"]?|\bDROP\s+TABLE\b|\bDELETE\s+FROM\s+\w+)"
)
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts|rules|directions)|disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|prompts)|system\s*prompt\s*:|system\s*override\s*:|developer\s*mode\s*:|you\s+are\s+now\s+(?:a|an|in)\s+(?:unrestricted|dan|developer|system)|<\s*\|(?:im_start|im_end|endoftext|system|user|assistant)\|\s*>|\[/?INST\]|<<\s*/?SYS\s*>>|###\s*(?:Instruction|System|Human|Assistant)\s*:)"
)
_ANGLE_MENTION_RE = re.compile(r"@<\s*([A-Za-z0-9_\-\.]{1,64})\s*>")
_CHAT_MENTION_RE = re.compile(r"(?:^|(?<=\s)|(?<=[\(\[\{,;]))@([A-Za-z0-9_\-\.]{1,64})\b")


def sanitize_chat_identity(raw_identity: Any) -> str:
    """Validates and sanitizes an identity token, preventing injection or path/header delimiter abuse."""
    if raw_identity is None:
        return ""
    s = str(raw_identity).strip()
    s = _ANSI_ESCAPE_RE.sub("", s)
    s = _CONTROL_CHAR_RE.sub("", s)
    if s.startswith("@"):
        s = s[1:].strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    s = re.sub(r"[^A-Za-z0-9_\-\.]", "", s)
    return s[:MAX_CHAT_IDENTITY_LEN]


def sanitize_chat_text(raw_text: Any, max_len: int = MAX_CHAT_MESSAGE_LEN) -> str:
    """
    Sanitizes chat message text against large messages, buffer overruns, control chars,
    HTML/XSS injection, SQL injection, code/command injection, and prompt injection,
    while preserving safe inline formatting ([b], [i], [u], [red], **bold**, *italic*, __underline__)
    and @<identity> / @identity mentions.
    """
    if raw_text is None:
        return ""
    if isinstance(raw_text, bytes):
        raw_text = raw_text[:MAX_CHAT_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    else:
        raw_text = str(raw_text)[:MAX_CHAT_PAYLOAD_BYTES]

    s = _ANGLE_MENTION_RE.sub(r"@\1", raw_text)
    s = _ANSI_ESCAPE_RE.sub("", s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = _CONTROL_CHAR_RE.sub("", s)

    s = _HTML_SCRIPT_STYLE_RE.sub("", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _JS_EVENT_OR_SCHEME_RE.sub("", s)
    s = s.replace("<", "").replace(">", "")

    s = _CODE_INJECTION_RE.sub("[filtered]", s)
    s = _SQL_INJECTION_RE.sub("[filtered]", s)
    s = _PROMPT_INJECTION_RE.sub("[filtered]", s)

    s = re.sub(r" {3,}", "  ", s).strip()
    cap = max(1, min(int(max_len or MAX_CHAT_MESSAGE_LEN), MAX_CHAT_MESSAGE_LEN))
    if len(s) > cap:
        s = s[:cap].rstrip()
    return s


def extract_chat_mentions(text: str) -> List[str]:
    """Extracts unique @<identity> or @identity tokens from a chat message."""
    if not text:
        return []
    normalized = _ANGLE_MENTION_RE.sub(r"@\1", str(text))
    found: List[str] = []
    for m in _CHAT_MENTION_RE.finditer(normalized):
        ident = sanitize_chat_identity(m.group(1))
        if ident and ident not in found:
            found.append(ident)
    return found



_INVALID_BANC_HOSTS = {
    "", "none", "null", "false", "disabled", "pack_host", "*", "auto", "0.0.0.0", "::",
}


def is_valid_banc_client(client: Any) -> bool:
    """Returns True if client has valid target host, port, auth secret, and Fernet cipher."""
    if client is None:
        return False
    raw_host_attr = getattr(client, "host", None)
    if not isinstance(raw_host_attr, str):
        return False
    raw_host = normalize_host_arg(raw_host_attr)
    if raw_host.lower() in _INVALID_BANC_HOSTS:
        return False
    raw_port_attr = getattr(client, "port", None)
    if raw_port_attr is None or isinstance(raw_port_attr, bool):
        return False
    try:
        port = int(raw_port_attr)
    except (TypeError, ValueError):
        return False
    if port < 1 or port > 65535:
        return False
    auth = getattr(client, "auth_secret", None)
    if not isinstance(auth, str) or not auth.strip():
        return False
    # Allow test doubles/mocks providing get_wius_sync without real Fernet instance,
    # while requiring real BancClient instances to hold a valid Fernet cipher.
    if isinstance(client, BancClient) and not isinstance(getattr(client, "fernet", None), Fernet):
        return False
    if not callable(getattr(client, "get_wius_sync", None)) and not callable(getattr(client, "get_wius", None)):
        return False
    return True


def _find_banc_dict(cfg_dict: Any, prefer_section: Optional[str] = None) -> Optional[dict]:
    """
    Extracts a dictionary containing Banc connection parameters from various config schemas:
    1. Unified config: viewer, creator, common.banc, engine.connections, flux
    2. Legacy nested config: connections
    3. Flat config: cfg_dict itself (mosfet_client.json, MosFET.json, etc.)
    Returns the first matching dictionary that defines Banc parameters, or None.
    """
    if not isinstance(cfg_dict, dict):
        return None

    candidates = []
    pref = str(prefer_section).strip().lower() if prefer_section else None
    if pref and isinstance(cfg_dict.get(pref), dict):
        candidates.append(cfg_dict[pref])

    # Viewer & Creator sections (specifically configured for display & studio apps)
    if pref != "viewer" and isinstance(cfg_dict.get("viewer"), dict):
        candidates.append(cfg_dict["viewer"])
    if pref != "creator" and isinstance(cfg_dict.get("creator"), dict):
        candidates.append(cfg_dict["creator"])

    # Engine connections
    eng_conns = cfg_dict.get("engine", {}).get("connections") if isinstance(cfg_dict.get("engine"), dict) else None
    if pref != "engine" and isinstance(eng_conns, dict):
        candidates.append(eng_conns)

    # Common banc section
    common_banc = cfg_dict.get("common", {}).get("banc") if isinstance(cfg_dict.get("common"), dict) else None
    if isinstance(common_banc, dict):
        candidates.append(common_banc)

    # Legacy connections dictionary
    if isinstance(cfg_dict.get("connections"), dict):
        candidates.append(cfg_dict["connections"])

    # Flux section
    if pref != "flux" and isinstance(cfg_dict.get("flux"), dict):
        candidates.append(cfg_dict["flux"])

    # banc section
    if pref != "banc" and isinstance(cfg_dict.get("banc"), dict):
        candidates.append(cfg_dict["banc"])

    # Flat dictionary itself
    candidates.append(cfg_dict)

    # 1. Prefer candidate sections that define an explicit, active host (not invalid / disabled)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        raw_h = cand.get("banc_host", cand.get("cloud_server", cand.get("host", "")))
        if isinstance(raw_h, str) and raw_h.strip():
            norm_h = normalize_host_arg(raw_h).lower()
            if norm_h not in _INVALID_BANC_HOSTS:
                if cand is common_banc and cand.get("enabled") is False:
                    continue
                return cand

    # 2. If common_banc is enabled with a valid host
    if isinstance(common_banc, dict) and common_banc.get("enabled"):
        raw_h = common_banc.get("host", "")
        if isinstance(raw_h, str) and raw_h.strip() and normalize_host_arg(raw_h).lower() not in _INVALID_BANC_HOSTS:
            return common_banc

    # 3. Fallback to any candidate defining banc host or connection parameters
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if any(k in cand for k in ("banc_host", "cloud_server", "host", "auth_secret", "encryption_key")):
            return cand

    return None


def _extract_valid_banc_params(
    cfg_dict: Any,
    require_explicit_host: bool = True,
    prefer_section: Optional[str] = None,
) -> Optional[dict]:
    """Validates and extracts Banc connection parameters from a loaded JSON config dictionary."""
    if not isinstance(cfg_dict, dict):
        return None
    conns = _find_banc_dict(cfg_dict, prefer_section=prefer_section)
    if not isinstance(conns, dict):
        return None

    has_host_key = any(k in conns for k in ("banc_host", "cloud_server", "host"))
    if require_explicit_host and not has_host_key:
        cb = cfg_dict.get("common", {}).get("banc", {}) if isinstance(cfg_dict.get("common"), dict) else {}
        if not any(k in cb for k in ("banc_host", "cloud_server", "host")):
            return None

    raw_host = conns.get("banc_host", conns.get("cloud_server", conns.get("host", "")))
    if not raw_host and isinstance(cfg_dict.get("common"), dict) and isinstance(cfg_dict["common"].get("banc"), dict):
        raw_host = cfg_dict["common"]["banc"].get("host", "")
    if not raw_host and isinstance(cfg_dict.get("banc"), dict):
        raw_host = cfg_dict["banc"].get("host", "")
    if not isinstance(raw_host, str):
        return None
    host = normalize_host_arg(raw_host)
    if host.lower() in _INVALID_BANC_HOSTS:
        return None

    raw_port = conns.get("banc_port", conns.get("cloud_port", conns.get("port")))
    if raw_port is None or isinstance(raw_port, bool):
        if isinstance(cfg_dict.get("common"), dict) and isinstance(cfg_dict["common"].get("banc"), dict):
            raw_port = cfg_dict["common"]["banc"].get("port")
    if raw_port is None or isinstance(raw_port, bool):
        if isinstance(cfg_dict.get("banc"), dict):
            raw_port = cfg_dict["banc"].get("port")
    if raw_port is None or isinstance(raw_port, bool):
        return None
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    if port < 1 or port > 65535:
        return None

    auth = conns.get("auth_secret", conns.get("secret_key"))
    if not auth and isinstance(cfg_dict.get("common"), dict) and isinstance(cfg_dict["common"].get("banc"), dict):
        auth = cfg_dict["common"]["banc"].get("auth_secret")
    if not auth and isinstance(cfg_dict.get("banc"), dict):
        auth = cfg_dict["banc"].get("auth_secret")
    if not auth and isinstance(cfg_dict.get("creator"), dict):
        auth = cfg_dict["creator"].get("auth_secret")
    if not auth and isinstance(cfg_dict.get("viewer"), dict):
        auth = cfg_dict["viewer"].get("auth_secret")
    if not auth and isinstance(cfg_dict.get("flux"), dict):
        auth = cfg_dict["flux"].get("auth_secret")
    if not auth and isinstance(cfg_dict.get("engine"), dict) and isinstance(cfg_dict["engine"].get("connections"), dict):
        auth = cfg_dict["engine"]["connections"].get("auth_secret")
    if not isinstance(auth, str) or not auth.strip():
        return None

    enc = conns.get("encryption_key")
    if not enc and isinstance(cfg_dict.get("common"), dict) and isinstance(cfg_dict["common"].get("banc"), dict):
        enc = cfg_dict["common"]["banc"].get("encryption_key")
    if not enc and isinstance(cfg_dict.get("banc"), dict):
        enc = cfg_dict["banc"].get("encryption_key")
    if not enc and isinstance(cfg_dict.get("creator"), dict):
        enc = cfg_dict["creator"].get("encryption_key")
    if not enc and isinstance(cfg_dict.get("viewer"), dict):
        enc = cfg_dict["viewer"].get("encryption_key")
    if not enc and isinstance(cfg_dict.get("flux"), dict):
        enc = cfg_dict["flux"].get("encryption_key")
    if not enc and isinstance(cfg_dict.get("engine"), dict) and isinstance(cfg_dict["engine"].get("connections"), dict):
        enc = cfg_dict["engine"]["connections"].get("encryption_key")
    if not isinstance(enc, str) or not enc.strip():
        return None
    try:
        Fernet(enc.encode("utf-8"))
    except Exception:
        return None

    ident = None
    if prefer_section and isinstance(cfg_dict.get(prefer_section), dict):
        ident = cfg_dict[prefer_section].get("identity")
    if not ident:
        ident = conns.get("identity")
    if not ident and isinstance(cfg_dict.get("common"), dict) and isinstance(cfg_dict["common"].get("banc"), dict):
        ident = cfg_dict["common"]["banc"].get("identity")
    if not ident and isinstance(cfg_dict.get("banc"), dict):
        ident = cfg_dict["banc"].get("identity")
    if not ident and isinstance(cfg_dict.get("creator"), dict):
        ident = cfg_dict["creator"].get("identity")
    if not ident and isinstance(cfg_dict.get("viewer"), dict):
        ident = cfg_dict["viewer"].get("identity")
    if not ident and isinstance(cfg_dict.get("engine"), dict) and isinstance(cfg_dict["engine"].get("connections"), dict):
        ident = cfg_dict["engine"]["connections"].get("identity")
    if not isinstance(ident, str) or not ident.strip():
        ident = "32bcd336456e"

    return {
        "host": host,
        "port": port,
        "auth_secret": auth,
        "encryption_key": enc,
        "identity": ident.strip(),
    }


def _candidate_banc_config_paths(config_path: Optional[str] = None) -> List[str]:
    candidate_paths: List[str] = []
    if config_path:
        candidate_paths.append(config_path)

    script_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    search_dirs = [
        os.getcwd(),
        os.path.join(os.getcwd(), "config"),
        os.path.join(os.getcwd(), "ApexBOS.220"),
        os.path.join(os.getcwd(), "ApexBOS.220", "config"),
        script_dir,
        os.path.join(script_dir, "config"),
        os.path.dirname(script_dir),
        os.path.join(os.path.dirname(script_dir), "config"),
    ]

    config_names = [
        "config.json",
        "mosfet_client.json",
        "banc.json",
        "MosFET.json",
        "abc.json",
    ]

    for d in search_dirs:
        for name in config_names:
            p = os.path.join(d, name)
            if p not in candidate_paths:
                candidate_paths.append(p)
    return candidate_paths


def get_configured_banc_client(
    config_path: Optional[str] = None,
    prefer_section: Optional[str] = None,
) -> Optional[BancClient]:
    """
    Returns a BancClient only when a Banc client configuration is explicitly present and valid
    (valid host != 'none', valid port 1..65535, non-empty auth_secret, valid Fernet encryption_key).
    When config_path is given, only that file is inspected (no silent fallback).
    Returns None if Banc connection is not configured or invalid.
    """
    if config_path is not None and str(config_path).strip():
        paths = [str(config_path).strip()]
        explicit = True
    else:
        paths = _candidate_banc_config_paths(None)
        explicit = False

    for cp in paths:
        if not os.path.exists(cp):
            if explicit:
                return None
            continue
        try:
            with open(cp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            if explicit:
                return None
            continue
        if not isinstance(data, dict):
            if explicit:
                return None
            continue
        banc_dict = _find_banc_dict(data, prefer_section=prefer_section)
        if banc_dict is None:
            if explicit:
                return None
            continue
        if not explicit:
            raw_h = banc_dict.get("banc_host", banc_dict.get("cloud_server", banc_dict.get("host", "")))
            if not raw_h or normalize_host_arg(raw_h).lower() in _INVALID_BANC_HOSTS:
                continue
        params = _extract_valid_banc_params(data, require_explicit_host=True, prefer_section=prefer_section)
        if params is None:
            if explicit:
                return None
            continue
        try:
            return BancClient(
                host=params["host"],
                port=params["port"],
                auth_secret=params["auth_secret"],
                encryption_key=params["encryption_key"],
                identity=params["identity"],
            )
        except Exception:
            if explicit:
                return None
            continue
    return None


def has_valid_banc_config(
    config_path: Optional[str] = None,
    prefer_section: Optional[str] = None,
) -> bool:
    """Returns True if a valid Banc client connection is configured for the given or discovered config."""
    return get_configured_banc_client(config_path=config_path, prefer_section=prefer_section) is not None


def create_banc_client_from_config(
    config_path: Optional[str] = None,
    prefer_section: Optional[str] = None,
) -> BancClient:
    """
    Factory helper that discovers and initializes a BancClient from available configs.
    Searches config_path, workspace/config dirs, script dir, and falls back to standard defaults.
    Never returns None.
    """
    explicit = bool(config_path and str(config_path).strip())
    if explicit:
        candidate_paths = [str(config_path).strip()]
    else:
        candidate_paths = _candidate_banc_config_paths(None)

    cfg_dict = None
    for cp in candidate_paths:
        if os.path.exists(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        banc_dict = _find_banc_dict(data, prefer_section=prefer_section)
                        if banc_dict is not None:
                            if _extract_valid_banc_params(data, require_explicit_host=True, prefer_section=prefer_section):
                                cfg_dict = data
                                break
                            elif cfg_dict is None:
                                cfg_dict = data
            except Exception:
                continue

    # If an explicit path was supplied but was missing valid host or keys, fallback to standard paths
    if explicit and not cfg_dict:
        fallback_paths = _candidate_banc_config_paths(None)
        for cp in fallback_paths:
            if cp not in candidate_paths and os.path.exists(cp):
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            if _extract_valid_banc_params(data, require_explicit_host=True, prefer_section=prefer_section):
                                cfg_dict = data
                                break
                except Exception:
                    continue

    params = _extract_valid_banc_params(cfg_dict, require_explicit_host=False, prefer_section=prefer_section) if cfg_dict else None

    if params:
        try:
            return BancClient(
                host=params["host"],
                port=params["port"],
                auth_secret=params["auth_secret"],
                encryption_key=params["encryption_key"],
                identity=params["identity"],
            )
        except Exception:
            pass

    # Credentials are missing or invalid (e.g. setup not completed). Return an inert client:
    # no auth secret and a throw-away random key, so it can never talk to a real Banc.
    # No credentials are ever hardcoded in the distribution.
    return BancClient(
        host="127.0.0.1",
        port=23009,
        auth_secret="",
        encryption_key=Fernet.generate_key().decode(),
        identity="32bcd336456e",
    )


create_banc_client_from_config._is_default_impl = True


# ---------------------------------------------------------------------------
# Local Multicast Client (UDP)
# ---------------------------------------------------------------------------

class _MulticastProtocol(asyncio.DatagramProtocol):
    """Internal Asyncio Protocol for handling incoming UDP datagrams."""
    def __init__(self, queue):
        self.queue = queue

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queue.put_nowait((data, addr))


def enforce_multicast_group(group: str) -> str:
    """Enforce valid multicast group IPv4 address, with Windows Winsock link-local enforcement."""
    if not group or not isinstance(group, str):
        raise ValueError(f"Invalid multicast group: '{group}' is empty or not a string")
    group_str = group.strip()
    try:
        octets = [int(p) for p in group_str.split(".")]
        if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
            raise ValueError
    except Exception:
        raise ValueError(f"Invalid IPv4 multicast address '{group_str}'")

    first = octets[0]
    if first < 224 or first > 239:
        raise ValueError(
            f"Invalid multicast group '{group_str}': must be within IPv4 Class D range 224.0.0.0 to 239.255.255.255"
        )

    # Windows-specific enforcement: link-local reserved block 224.0.0.0/24 (e.g., 224.0.0.1 All-Hosts)
    # causes Winsock [WinError 10065] unreachable host on default interface routing.
    if sys.platform == "win32" and first == 224 and octets[1] == 0 and octets[2] == 0:
        raise ValueError(
            f"Multicast group '{group_str}' is restricted on Windows: link-local reserved addresses (224.0.0.0/24) "
            f"fail with WinError 10065. Use an application multicast group such as 239.1.1.1 or 224.1.1.1."
        )

    return group_str


class MulticastClient:
    """Client for listening to live UDP multicast packet streams from ApexBOSFlux nodes."""
    
    def __init__(self, group: str = "239.1.1.1", port: int = 5000):
        self.group = enforce_multicast_group(group)
        self.port = int(port)

    @staticmethod
    def _discover_interfaces() -> List[str]:
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

    async def listen(self):
        """An asynchronous generator that yields raw packet bytes as they arrive."""
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        
        # Setup socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass

        # High-throughput buffer: 8 MB to prevent kernel drops at high packet rates
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass

        try:
            sock.bind(('', self.port))
        except OSError:
            sock.bind((self.group, self.port))
            
        # 1. Join discovered active physical interfaces first
        for ip in self._discover_interfaces():
            try:
                mreq = socket.inet_aton(self.group) + socket.inet_aton(ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

        # 2. Join loopback
        try:
            mreq_loop = socket.inet_aton(self.group) + socket.inet_aton("127.0.0.1")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_loop)
        except OSError:
            pass

        # 3. Join INADDR_ANY (0.0.0.0) safely with standard 8-byte struct ip_mreq
        try:
            mreq_any = socket.inet_aton(self.group) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_any)
        except OSError:
            pass
        
        # Create datagram endpoint
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _MulticastProtocol(queue),
            sock=sock
        )
        
        try:
            while True:
                data, addr = await queue.get()
                yield data, addr
        finally:
            transport.close()


# ---------------------------------------------------------------------------
# Test Harness / CLI Tool
# ---------------------------------------------------------------------------

async def test_multicast(group, port):
    print(f"[*] Starting Multicast Listener on {group}:{port}...")
    client = MulticastClient(group, port)
    
    try:
        async for data, addr in client.listen():
            print(f"[Multicast -> {addr[0]}] {data.hex().upper()}")
    except asyncio.CancelledError:
        print("\n[*] Multicast listener stopped.")

async def test_banc(host, port, secret, key, identity):
    print(f"[*] Connecting to Banc Cloud at {format_host_port(host, port)} as '{identity}'...")
    client = BancClient(host, port, secret, key, identity)
    
    try:
        start = time.time()
        packets = await client.get_packets()
        elapsed = time.time() - start
        
        print(f"[+] Downloaded {len(packets)} packets in {elapsed:.2f}s")
        if packets:
            print("\n--- Latest 5 Packets ---")
            for pkt in packets[:5]:  # Show latest 5
                print(f"[{pkt['date_str']}] Ch {pkt['channel']}: {pkt['hex_data'][:30]}... (Hash: {pkt['pkt_hash']})")
    except Exception as e:
        print(f"[-] Banc GET request failed: {e}")


async def main():
    parser = argparse.ArgumentParser(description="MosFET/Banc Client API Test Harness")
    
    # Configuration Args
    parser.add_argument("--config", type=str, default="mosfet_client.json", help="Path to JSON configuration file")
    parser.add_argument("--gen-config", action="store_true", help="Generate an example configuration file")
    
    # Multicast Args
    parser.add_argument("--multicast", action="store_true", help="Listen to the local MosFET multicast stream")
    parser.add_argument("--mcast-group", type=str, help="Multicast group IP")
    parser.add_argument("--mcast-port", type=int, help="Multicast UDP port")
    
    # Banc Args
    parser.add_argument("--banc", action="store_true", help="Test the Banc cloud GET service")
    parser.add_argument("--banc-host", type=str, help="Banc server IP")
    parser.add_argument("--banc-port", type=int, help="Banc server port")
    parser.add_argument("--secret", type=str, help="Auth secret string")
    parser.add_argument("--key", type=str, help="Fernet encryption key")
    parser.add_argument("--identity", type=str, help="Whitelisted identity (or 'auto' to generate)")
    
    args = parser.parse_args()

    # Handle Config Generation
    if args.gen_config:
        example_config = {
            "mcast_group": "239.1.1.1",
            "mcast_port": 5000,
            "banc_host": "127.0.0.1",
            "banc_port": 9000,
            "auth_secret": "EXAMPLE_SECRET_KEY_123",
            "encryption_key": Fernet.generate_key().decode(),
            "identity": "auto"
        }
        with open(args.config, 'w') as f:
            json.dump(example_config, f, indent=4)
        print(f"[*] Successfully generated example configuration: {args.config}")
        sys.exit(0)

    if not args.multicast and not args.banc:
        print("Please specify --multicast, --banc, or both to run the tests. Use -h for help.")
        sys.exit(1)

    # Load config if it exists
    config = {}
    if os.path.exists(args.config):
        with open(args.config, 'r') as f:
            config = json.load(f)

    # Resolve variables (CLI args override config file, which overrides defaults)
    mcast_group = args.mcast_group or config.get("mcast_group", "239.1.1.1")
    mcast_port = args.mcast_port or config.get("mcast_port", 5000)
    banc_host = normalize_host_arg(args.banc_host or config.get("banc_host", "127.0.0.1"))
    banc_port = args.banc_port or config.get("banc_port", 9000)
    secret = args.secret or config.get("auth_secret", "EXAMPLE_SECRET_KEY_123")
    key = args.key or config.get("encryption_key")
    identity = args.identity or config.get("identity", "auto")

    tasks = []

    if args.banc:
        if not key:
            print("Error: Encryption key is required to test the Banc GET service. Provide via --key or in config.")
            sys.exit(1)
            
        if identity == "auto":
            identity = await generate_identity(banc_host)
            print(f"[+] Auto-generated Node Identity: {identity}")
            
        # Banc test runs once and finishes
        tasks.append(test_banc(banc_host, banc_port, secret, key, identity))

    if args.multicast:
        # Multicast test runs infinitely
        tasks.append(test_multicast(mcast_group, mcast_port))

    if tasks:
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

# ---------------------------------------------------------------------------
# Rebranded Backward Compatibility Aliases
# ---------------------------------------------------------------------------
ApexBOSBancClient = BancClient
ApexBOXBancClient = BancClient
ApexBOSFluxClient = MulticastClient
create_apexbosbanc_client_from_config = create_banc_client_from_config
create_apexboxbanc_client_from_config = create_banc_client_from_config
