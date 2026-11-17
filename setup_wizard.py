"""
setup_wizard.py - ApexBOS Cross-Platform Installation & Setup Wizard (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Guides the user through:
  1. Python package dependency verification and live-progress installation.
  2. Deployment topology selection:
       Option 1: Local network only (IPv4, no Banc).
       Option 2: With Cloud support (Banc present: Local Banc vs Remote Cloud Banc with IPv4/IPv6 auto-detection).
  3. Banc host / port.
  4. Security: authentication secret (typed by you) and a freshly generated encryption key
     (or an existing key when joining an existing Banc). Neither is ever shipped in the distribution.
  5. Grabber radio channel allocation across N worker processes for channels [77..172].
  6. SDR hardware: RTL-SDR device index (--device) and, when rtl_tcp is not on PATH, the path to the
     rtl_tcp / rtl_sdr executable or to the rtlsdr DLL folder (Windows).
  7. System boot automation (Windows Task Scheduler, Linux systemd, macOS launchd) + shortcuts.
Writes config.json, the legacy config files and install_record.json (used by uninstall.py).
"""

import os
import sys

# --- LINUX NVIDIA / NOUVEAU & XORG GLAMOR CRASH FIX ---
# MUST BE SET BEFORE PYGAME IS IMPORTED!
# 1. Forces SDL2/Mesa in the Python processes to use CPU software rendering.
# 2. Disables X11 MIT-SHM shared-memory DMA transfers (SDL_VIDEO_X11_XSHM=0) so Xorg's
#    Glamor/Nouveau Copy Engine (CE2) doesn't hit a Page Table Entry (PTE) fault when
#    two processes push frames simultaneously.
# 3. Disables X11 compositor bypass so Cinnamon/Muffin doesn't thrash VRAM page tables.
if sys.platform.startswith('linux'):
    os.environ['SDL_RENDER_DRIVER'] = 'software'
    os.environ['SDL_FRAMEBUFFER_ACCELERATION'] = '0'
    os.environ['LIBGL_ALWAYS_SOFTWARE'] = '1'
    os.environ['GALLIUM_DRIVER'] = 'llvmpipe'
    os.environ['SDL_VIDEO_X11_XSHM'] = '0'
    os.environ['SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR'] = '0'
import json
import time
import hashlib
import asyncio
import zipfile
import tempfile
import getpass
import importlib.util
import shutil
import socket
import ipaddress
import subprocess
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set

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

import config_loader
from version import VERSION

# Required package definitions
DEPENDENCIES = [
    ("pygame-ce", "pygame", "Core UI Rendering & Audio Engine (pygame-ce / pygame)"),
    ("pyzmq", "zmq", "ZeroMQ High-Speed Message Bus for Grabber, Flux, & Spy"),
    ("numpy", "numpy", "SDR Baseband DSP, Fast Demodulation, & Reed-Solomon"),
    ("cryptography", "cryptography", "Fernet Token & Packet Cryptography for Banc/MosFET"),
    ("lz4", "lz4", "High-Throughput State Compression between Engine & Viewer"),
]
if sys.platform == "win32":
    DEPENDENCIES.append(("windows-curses", "curses", "Console TUI Support for Windows"))

COLOR_BG = (14, 18, 24)
COLOR_CARD = (20, 26, 36)
COLOR_BORDER = (65, 85, 115)
COLOR_ACCENT = (255, 185, 40)
COLOR_GREEN = (50, 225, 110)
COLOR_RED = (245, 50, 50)
COLOR_TEXT = (225, 235, 248)
COLOR_TEXT_DIM = (130, 142, 160)
COLOR_BTN_HOVER = (40, 55, 78)

# Packages pip-installed by the wizard during this run (recorded for the uninstaller).
INSTALLED_PACKAGES: List[str] = []


# ---------------------------------------------------------------------------
# Dependency Verification & Installer Worker
# ---------------------------------------------------------------------------

def check_missing_dependencies() -> List[Tuple[str, str, str]]:
    """Returns list of (pkg_name, import_name, description) that are missing."""
    missing = []
    for pkg_name, import_name, desc in DEPENDENCIES:
        try:
            __import__(import_name)
        except ImportError:
            # Check if pygame was imported instead of pygame-ce
            if import_name == "pygame":
                try:
                    import pygame
                    continue
                except ImportError:
                    pass
            missing.append((pkg_name, import_name, desc))
    return missing


def install_package_stream(pkg_name: str, output_callback=None, find_links: Optional[str] = None) -> bool:
    """
    Installs a python package via pip streaming output.
    With find_links the install is fully offline (--no-index --find-links <folder of wheels>).
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if find_links:
        cmd += ["--no-index", "--find-links", find_links]
    cmd.append(pkg_name)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
        for line in proc.stdout:
            clean_l = line.strip()
            if output_callback and clean_l:
                output_callback(clean_l)
        proc.wait()
        ok = proc.returncode == 0
        if ok and pkg_name not in INSTALLED_PACKAGES:
            INSTALLED_PACKAGES.append(pkg_name)
        return ok
    except Exception as e:
        if output_callback:
            output_callback(f"Installation error: {e}")
        return False


def ensure_pyrtlsdr(output_callback=None) -> bool:
    """Installs the optional 'pyrtlsdr' package (needed for direct rtlsdr mode) if it is missing."""
    if importlib.util.find_spec("rtlsdr") is not None:
        return True
    return install_package_stream("pyrtlsdr", output_callback=output_callback)


# ---------------------------------------------------------------------------
# Desktop Shortcut Creation
# ---------------------------------------------------------------------------

def create_desktop_shortcuts(working_dir: str) -> List[str]:
    """Creates OS-native shortcuts on Desktop for ApexBOSLauncher. Returns the shortcut paths created."""
    wdir = os.path.abspath(working_dir)
    launcher_script = os.path.join(wdir, "ApexBOSLauncher.py")
    py_exe = sys.executable
    created: List[str] = []

    if sys.platform == "win32":
        desktop_dir = ""
        try:
            desktop_dir = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10,
            ).stdout.strip()
        except Exception:
            desktop_dir = ""
        if not desktop_dir or not os.path.exists(desktop_dir):
            desktop_dir = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        if os.path.exists(desktop_dir):
            lnk = os.path.join(desktop_dir, 'ApexBOS Control Center.lnk')
            ps_script = f"""
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{lnk}")
$Shortcut.TargetPath = "{py_exe}"
$Shortcut.Arguments = '"{launcher_script}"'
$Shortcut.WorkingDirectory = "{wdir}"
$Shortcut.Description = "ApexBOS Railroad Dispatcher Control Center"
$Shortcut.Save()
"""
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if os.path.isfile(lnk):
                    created.append(lnk)
            except Exception:
                pass

    elif sys.platform == "darwin":
        desktop_dir = os.path.expanduser("~/Desktop")
        if os.path.exists(desktop_dir):
            shortcut_cmd = os.path.join(desktop_dir, "ApexBOS Control Center.command")
            try:
                with open(shortcut_cmd, "w", encoding="utf-8") as f:
                    f.write(f"#!/bin/bash\ncd '{wdir}' && '{py_exe}' '{launcher_script}'\n")
                os.chmod(shortcut_cmd, 0o755)
                created.append(shortcut_cmd)
            except Exception:
                pass

    else:
        desktop_dir = ""
        try:
            desktop_dir = subprocess.run(
                ["xdg-user-dir", "DESKTOP"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            desktop_dir = ""
        if not desktop_dir or not os.path.exists(desktop_dir):
            desktop_dir = os.path.expanduser("~/Desktop")
        apps_dir = os.path.expanduser("~/.local/share/applications")
        desktop_entry = f"""[Desktop Entry]
Type=Application
Name=ApexBOS Control Center
Comment=ApexBOS Railroad Dispatcher Control Center
Exec={py_exe} {launcher_script}
Path={wdir}
Terminal=false
Categories=Utility;
"""
        for d in (desktop_dir, apps_dir):
            if os.path.exists(d):
                p = os.path.join(d, "ApexBOS.desktop")
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(desktop_entry)
                    os.chmod(p, 0o755)
                    created.append(p)
                except Exception:
                    pass
    return created


# ---------------------------------------------------------------------------
# Shared Helpers (CLI + GUI)
# ---------------------------------------------------------------------------

def parse_endpoint(text: str, default_port: int = 23009) -> Tuple[str, int]:
    """Parses 'host', 'host:port', '[ipv6]:port' or a bare IPv6 address into (host, port)."""
    t = (text or "").strip()
    if t.startswith("[") and "]" in t:
        host = t[1:t.index("]")]
        rest = t[t.index("]") + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if t.count(":") == 1:
        host, p = t.split(":")
        return host, int(p) if p.isdigit() else default_port
    return t, default_port


def load_existing_config(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Returns the current config.json (unified format) or None."""
    p = path or config_loader.CONFIG_FILENAME
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "common" in data:
                return data
        except Exception:
            return None
    return None


def existing_secrets(cfg: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not cfg:
        return "", ""
    auth, key = config_loader._canonical_secrets(cfg)
    return auth, (key if config_loader.is_valid_encryption_key(key) else "")


def copy_to_clipboard(text: str) -> bool:
    """Copies text to the system clipboard across platforms (Windows clip.exe or pygame.scrap fallback)."""
    if sys.platform == "win32":
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=False)
            p.communicate(input=text.encode("utf-16"))
            if p.returncode == 0:
                return True
        except Exception:
            pass
    try:
        import pygame.scrap
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
        return True
    except Exception:
        pass
    return False


def prompt_editable(prompt: str, default: str) -> str:
    """
    Prompts the user in the CLI with an editable default string.
    Supports in-place arrow key navigation (Left/Right, Home/End), Delete, and Backspace.
    Falls back gracefully to standard input() in non-interactive / piped environments.
    """
    if not sys.stdin.isatty():
        try:
            val = input(f"{prompt} [{default}]: ").strip()
            return val if val else default
        except (EOFError, IOError):
            return default

    if sys.platform == "win32":
        try:
            import msvcrt
            buf = list(default)
            pos = len(buf)
            p_str = f"{prompt}: "
            sys.stdout.write(p_str + "".join(buf))
            sys.stdout.flush()
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    res = "".join(buf).strip()
                    return res if res else default
                elif ch == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt
                elif ch in ("\x00", "\xe0"):  # Extended key
                    code = msvcrt.getwch()
                    if code == "K":    # Left arrow
                        if pos > 0:
                            pos -= 1
                    elif code == "M":  # Right arrow
                        if pos < len(buf):
                            pos += 1
                    elif code == "G":  # Home
                        pos = 0
                    elif code == "O":  # End
                        pos = len(buf)
                    elif code == "S":  # Delete
                        if pos < len(buf):
                            buf.pop(pos)
                elif ch == "\x08":     # Backspace
                    if pos > 0:
                        pos -= 1
                        buf.pop(pos)
                elif ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1

                # Redraw line with prompt, text, and place cursor at pos
                line = "".join(buf)
                sys.stdout.write("\r" + " " * (len(p_str) + len(line) + 8) + "\r")
                sys.stdout.write(p_str + line)
                back_count = len(line) - pos
                if back_count > 0:
                    sys.stdout.write("\b" * back_count)
                sys.stdout.flush()
        except Exception:
            try:
                val = input(f"{prompt} [{default}]: ").strip()
                return val if val else default
            except (EOFError, IOError):
                return default
    else:
        try:
            import readline
            readline.set_startup_hook(lambda: readline.insert_text(default))
            try:
                val = input(f"{prompt}: ")
            finally:
                readline.set_startup_hook()
            return val.strip() if val.strip() else default
        except Exception:
            try:
                val = input(f"{prompt} [{default}]: ").strip()
                return val if val else default
            except (EOFError, IOError):
                return default


def finalize_install(
    cfg: Dict[str, Any],
    autostart: bool,
    working_dir: Optional[str] = None,
    create_shortcuts: Optional[bool] = None,
) -> Dict[str, Any]:
    """Writes config.json + legacy configs + install record; sets up boot services and shortcuts."""
    wdir = os.path.abspath(working_dir or os.getcwd())
    config_loader.save_unified_config(cfg, os.path.join(wdir, config_loader.CONFIG_FILENAME))
    config_loader.sync_legacy_configs(cfg, target_dir=wdir)
    fw_files = config_loader.save_windows_firewall_files(cfg, target_dir=wdir)

    boot = None
    if autostart:
        is_ev = (cfg.get("common", {}).get("deployment_mode") == "engines_viewers")
        boot = config_loader.configure_boot_services(
            enable_grabber=not is_ev, enable_flux=not is_ev, working_dir=wdir,
            config_path=os.path.join(wdir, config_loader.CONFIG_FILENAME),
        )

    # Shortcuts only make sense when running inside a real installation folder.
    if create_shortcuts is None:
        create_shortcuts = os.path.isfile(os.path.join(wdir, "ApexBOSLauncher.py"))
    shortcuts = create_desktop_shortcuts(wdir) if create_shortcuts else []

    rec_path = os.path.join(wdir, config_loader.INSTALL_RECORD_FILENAME)
    rec: Dict[str, Any] = {}
    if os.path.isfile(rec_path):
        try:
            with open(rec_path, "r", encoding="utf-8") as f:
                rec = json.load(f) or {}
        except Exception:
            rec = {}
    rec.setdefault("installed_at", datetime.now().isoformat(timespec="seconds"))
    rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
    rec["version"] = VERSION
    rec["install_dir"] = wdir
    rec["python"] = sys.executable
    rec["platform"] = sys.platform
    rec["packages_installed"] = sorted(set(rec.get("packages_installed", [])) | set(INSTALLED_PACKAGES))
    rec["shortcuts"] = sorted(set(rec.get("shortcuts", [])) | set(shortcuts))
    rec["firewall_files"] = [fw_files[0], fw_files[1]]
    if boot is not None:
        rec["boot_services"] = {"grabber": boot.get("grabber", False), "flux": boot.get("flux", False),
                                "created": boot.get("created", [])}
    try:
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
    except OSError:
        pass
    return {"boot": boot, "shortcuts": shortcuts, "record": rec_path, "firewall_files": fw_files}


def _describe_sdr(sdr: Dict[str, Any], device: int) -> str:
    parts = [f"mode={sdr.get('mode', 'auto')}", f"device={device}"]
    for k in ("rtl_tcp_bin", "rtl_sdr_bin", "rtlsdr_dll_dir"):
        if sdr.get(k):
            parts.append(f"{k}={sdr[k]}")
    return ", ".join(parts)


def collect_local_identities(target_host: str, extra_identities: Optional[List[str]] = None) -> List[str]:
    """Computes node identity hashes for this installation targeting `target_host`."""
    identities: List[str] = []

    def _add(val: str):
        if val and val not in identities:
            identities.append(val)

    if extra_identities:
        for eid in extra_identities:
            if eid and eid != "ApexNode_01":
                _add(str(eid).strip())

    try:
        import mosfet_client
        clean_host = mosfet_client.normalize_host_arg(target_host)
        ident = asyncio.run(mosfet_client.generate_identity(clean_host))
        _add(ident)
    except Exception:
        pass

    return identities


def build_banc_deploy_zip(
    output_zip: str,
    banc_port: int,
    auth_secret: str,
    encryption_key: str,
    identities: List[str],
    ip_mode: str = "auto",
) -> str:
    """Packages ApexBOSBanc.py, version.py, mosfet_auth.py, mosfet_client.py, banc.json, whitelist.txt, and requirements into a ZIP."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    banc_cfg = {
        "port": int(banc_port),
        "ui_port": int(banc_port) + 1,
        "ip_mode": ip_mode,
        "auth_secret": auth_secret,
        "encryption_key": encryption_key,
        "whitelist_file": "whitelist.txt",
        "db_path": "banc.db",
        "db_retention_sec": 86400,
        "stats_interval_sec": 300,
        "mirrors": [],
    }
    whitelist_txt = "\n".join(identities) + ("\n" if identities else "")
    reqs_txt = "cryptography\ncolorama\n"

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fn in ("ApexBOSBanc.py", "version.py", "mosfet_auth.py", "mosfet_client.py", "start_banc.sh"):
            src = os.path.join(base_dir, fn)
            if os.path.isfile(src):
                zinfo = zipfile.ZipInfo.from_file(src, arcname=fn)
                if fn.endswith(".sh"):
                    zinfo.external_attr = 0o755 << 16
                with open(src, "rb") as f_in:
                    zf.writestr(zinfo, f_in.read())
        zf.writestr("banc.json", json.dumps(banc_cfg, indent=2) + "\n")
        zf.writestr("whitelist.txt", whitelist_txt)
        zf.writestr("requirements_banc.txt", reqs_txt)
    return output_zip


def deploy_banc_via_ssh(
    host: str,
    ssh_user: str,
    ssh_port: int = 22,
    ssh_key: str = "",
    banc_port: int = 23009,
    auth_secret: str = "",
    encryption_key: str = "",
    identities: Optional[List[str]] = None,
    progress_cb=None,
) -> Tuple[bool, str]:
    """
    Packages ApexBOSBanc with whitelisted identities, SCPs the ZIP to the remote
    user's home directory, unzips it via SSH, installs cryptography if needed,
    and starts ApexBOSBanc.py in the background.
    """
    def _log(msg: str):
        if progress_cb:
            progress_cb(msg)

    clean_host = host.strip().strip("[]")
    if not clean_host or not ssh_user.strip():
        return False, "Remote host and SSH username are required."

    if not shutil.which("scp") or not shutil.which("ssh"):
        return False, "OpenSSH client ('ssh' and 'scp') not found on PATH."

    _log("Computing local node identities for whitelist...")
    idents = list(identities) if identities is not None else collect_local_identities(clean_host)
    ip_mode = config_loader.detect_ip_version(clean_host)

    with tempfile.TemporaryDirectory(prefix="apexbos_banc_deploy_") as td:
        zip_path = os.path.join(td, "banc_deploy.zip")
        _log("Packaging ApexBOSBanc + banc.json + whitelist.txt...")
        build_banc_deploy_zip(
            output_zip=zip_path,
            banc_port=banc_port,
            auth_secret=auth_secret,
            encryption_key=encryption_key,
            identities=idents,
            ip_mode=ip_mode,
        )

        scp_host = f"[{clean_host}]" if ":" in clean_host else clean_host
        scp_cmd = [
            "scp",
            "-P", str(int(ssh_port)),
            "-o", "ConnectTimeout=12",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
        ]
        if ssh_key.strip():
            scp_cmd.extend(["-i", os.path.expanduser(ssh_key.strip())])
        scp_cmd.extend([zip_path, f"{ssh_user.strip()}@{scp_host}:~/banc_deploy.zip"])

        _log(f"Uploading banc_deploy.zip to {ssh_user.strip()}@{scp_host}:~/ ...")
        try:
            res_scp = subprocess.run(
                scp_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=45,
            )
        except Exception as exc:
            return False, f"SCP connection error: {exc}"
        if res_scp.returncode != 0:
            err = (res_scp.stderr or res_scp.stdout or f"exit code {res_scp.returncode}").strip()
            return False, f"SCP failed: {err}"

        remote_cmd = (
            "mkdir -p ~/ApexBOSBanc && cd ~/ApexBOSBanc && "
            "(unzip -o ~/banc_deploy.zip >/dev/null 2>&1 || python3 -m zipfile -e ~/banc_deploy.zip .) && "
            "chmod +x start_banc.sh 2>/dev/null || true; "
            "bash start_banc.sh"
        )
        ssh_cmd = [
            "ssh",
            "-p", str(int(ssh_port)),
            "-o", "ConnectTimeout=12",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
        ]
        if ssh_key.strip():
            ssh_cmd.extend(["-i", os.path.expanduser(ssh_key.strip())])
        ssh_cmd.extend([f"{ssh_user.strip()}@{clean_host}", remote_cmd])

        cmd_display = f"ssh -p {ssh_port} {ssh_user.strip()}@{clean_host} \"{remote_cmd}\""
        _log(f"Starting ApexBOSBanc on {clean_host}:{banc_port} via SSH...")
        _log(f"Command: {cmd_display}")
        try:
            res_ssh = subprocess.run(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=60,
            )
        except Exception as exc:
            err_msg = f"SSH execution error: {exc}\nCommand: {cmd_display}"
            _log(f"[!] {err_msg}")
            return False, err_msg
        if res_ssh.returncode != 0:
            err_out = (res_ssh.stderr or res_ssh.stdout or f"exit code {res_ssh.returncode}").strip()
            if res_ssh.stdout and res_ssh.stdout.strip() and res_ssh.stdout.strip() not in err_out:
                err_out = f"{err_out}\n{res_ssh.stdout.strip()}"
            err_msg = f"SSH remote start failed. Exit code {res_ssh.returncode}.\nCommand: {cmd_display}\nDetails: {err_out}"
            _log(f"[!] {err_msg}")
            return False, err_msg
        else:
            if res_ssh.stdout and res_ssh.stdout.strip():
                for out_line in res_ssh.stdout.strip().splitlines():
                    _log(f"  {out_line}")

    return True, f"Cloud Banc deployed & started on {clean_host}:{banc_port} (whitelisted {len(idents)} identity/ies)."


# ---------------------------------------------------------------------------
# CLI Setup Wizard (for Headless, SSH, or --cli mode)
# ---------------------------------------------------------------------------

def _ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def run_cli_wizard(
    auto_defaults: bool = False,
    auth_secret: Optional[str] = None,
    encryption_key: Optional[str] = None,
    device_index: Optional[int] = None,
    sdr_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs the full interactive or automated CLI setup wizard."""
    print("=" * 78)
    print(f"  ApexBOS Setup & Deployment Wizard ({VERSION})")
    print("=" * 78)

    existing = load_existing_config()
    ex_auth, ex_key = existing_secrets(existing)
    ex_grab = (existing or {}).get("grabber", {})
    ex_ident = (
        ((existing or {}).get("common") or {}).get("banc", {}).get("identity")
        or ((existing or {}).get("engine", {}).get("connections", {})).get("identity")
    )
    if ex_ident and ex_ident != "ApexNode_01":
        node_identity = ex_ident
    else:
        node_identity = config_loader.generate_unique_identity()
    if existing:
        print(f"[i] Existing configuration found (version {existing.get('version', '?')}); "
              f"your previous settings are offered as defaults.")

    # 1. Dependencies
    missing = check_missing_dependencies()
    if missing:
        print(f"\n[!] Detected {len(missing)} missing Python package(s):")
        for pkg, _, desc in missing:
            print(f"    - {pkg:16s} ({desc})")
        if not auto_defaults:
            ans = input("\n[?] Install missing packages now via pip? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                for pkg, _, _ in missing:
                    print(f"[*] Installing {pkg}...")
                    ok = install_package_stream(pkg, output_callback=lambda l: print(f"    {l}"))
                    if not ok:
                        print(f"[!] Warning: Installation of {pkg} reported issues.")
        else:
            for pkg, _, _ in missing:
                install_package_stream(pkg)
    else:
        print("[+] All core dependencies verified and installed.")

    # 2. Deployment Topology
    print("\n--- Step 2: Deployment Topology ---")
    print("  [1] Local Network Only (IPv4, No Banc)")
    print("  [2] With Cloud Support (ApexBOSBanc Present)")
    print("  [3] Engines and Viewers Only (Display Workstation / Client)")
    if auto_defaults:
        dep_choice = "2"
    else:
        dep_choice = input("[?] Select deployment option [1-3] (default: 2): ").strip() or "2"

    if dep_choice == "3":
        deployment_mode = "engines_viewers"
    elif dep_choice == "1":
        deployment_mode = "local"
    else:
        deployment_mode = "cloud"

    banc_location = "remote"
    banc_host = "127.0.0.1"
    banc_port = 23009
    ip_ver = "ipv4"
    packet_source = "flux"
    flux_mcast_group = "239.1.1.1"
    flux_mcast_port = 23003

    if deployment_mode == "engines_viewers":
        print("\n--- Step 3: Packet Source & Network Connection ---")
        print("  Select the source of packets for Engines and Viewers:")
        print("    [1] Network Flux Relay (UDP Multicast)")
        print("    [2] ApexBOSBanc Cloud Server (TCP)")
        if auto_defaults:
            src_choice = "1"
        else:
            src_choice = input("[?] Select packet source [1-2] (default: 1): ").strip() or "1"

        packet_source = "banc" if src_choice == "2" else "flux"

        if packet_source == "flux":
            ex_conn = (existing or {}).get("engine", {}).get("connections", {})
            ex_mcast = (existing or {}).get("common", {}).get("multicast", {})
            default_grp = ex_conn.get("mcast_group") or ex_mcast.get("flux_group") or "239.1.1.1"
            default_prt = str(ex_conn.get("mcast_port") or ex_mcast.get("flux_port") or 23003)

            if auto_defaults:
                flux_mcast_group = default_grp
                flux_mcast_port = int(default_prt)
            else:
                grp_in = input(f"[?] Flux Multicast Group (default: {default_grp}): ").strip() or default_grp
                flux_mcast_group = grp_in
                prt_in = input(f"[?] Flux Multicast Port (default: {default_prt}): ").strip() or default_prt
                flux_mcast_port = int(prt_in) if prt_in.isdigit() else 23003

            print(f"[+] Configured Flux packet source: {flux_mcast_group}:{flux_mcast_port}")
            auth = auth_secret or ex_auth or config_loader.generate_auth_secret()
            key = encryption_key if config_loader.is_valid_encryption_key(encryption_key) else (ex_key or config_loader.generate_encryption_key())
            banc_host = "none"
            banc_port = 23009
            banc_location = "none"
        else:
            flux_mcast_group = None
            flux_mcast_port = None
            ex_conn = (existing or {}).get("engine", {}).get("connections", {})
            ex_banc = (existing or {}).get("common", {}).get("banc", {})
            default_b_host = ex_conn.get("banc_host") or ex_banc.get("host") or "127.0.0.1"
            if str(default_b_host).strip().lower() in ("none", "null"):
                default_b_host = "127.0.0.1"
            default_b_port = str(ex_conn.get("banc_port") or ex_banc.get("port") or 23009)
            default_ep = f"{default_b_host}:{default_b_port}"

            if auto_defaults:
                host_in = default_ep
            else:
                host_in = input(f"[?] Remote Banc IP/Host and Port (default: {default_ep}): ").strip() or default_ep
            banc_host, banc_port = parse_endpoint(host_in, default_port=int(default_b_port) if default_b_port.isdigit() else 23009)
            banc_location = "remote"

            if auth_secret:
                auth = auth_secret
            elif auto_defaults:
                auth = ex_auth or config_loader.generate_auth_secret()
            else:
                hint = "Enter keeps the existing one" if ex_auth else "Enter generates a random one"
                while True:
                    first = _ask_secret(f"[?] Banc authentication secret (auth_secret; {hint}): ")
                    if not first:
                        auth = ex_auth or config_loader.generate_auth_secret()
                        if not ex_auth:
                            print(f"    Generated auth_secret: {auth}")
                        break
                    if len(first) < config_loader.MIN_AUTH_SECRET_LEN:
                        print(f"    Too short: use at least {config_loader.MIN_AUTH_SECRET_LEN} characters.")
                        continue
                    if _ask_secret("[?] Repeat the secret: ") != first:
                        print("    The two entries do not match; try again.")
                        continue
                    auth = first
                    break

            if encryption_key and config_loader.is_valid_encryption_key(encryption_key):
                key = encryption_key.strip()
            elif auto_defaults:
                key = ex_key or config_loader.generate_encryption_key()
            else:
                print("  Encryption key (Fernet):")
                print("    [1] Paste an EXISTING key from Banc server (Recommended)")
                print("    [2] Generate a NEW key")
                if ex_key:
                    print("    [3] Keep the key already in config.json")
                default_choice = "3" if ex_key else "1"
                choice = input(f"[?] Select [1-{3 if ex_key else 2}] (default {default_choice}): ").strip() or default_choice
                if choice == "3" and ex_key:
                    key = ex_key
                elif choice == "2":
                    key = config_loader.generate_encryption_key()
                    print(f"    Generated encryption_key: {key}")
                else:
                    while True:
                        typed = input("[?] Paste the existing encryption key: ").strip()
                        if config_loader.is_valid_encryption_key(typed):
                            key = typed
                            break
                        print("    Not a valid key (must be 44 characters of URL-safe base64).")

            ip_ver = config_loader.detect_ip_version(banc_host)
            print(f"[+] Configured Banc packet source at {banc_host}:{banc_port} (Detected protocol: {ip_ver.upper()})")
            idents = collect_local_identities(banc_host, extra_identities=[node_identity])
            print("\n  [!] Reminder: Ensure your workstation identity is whitelisted on the remote Banc server (whitelist.txt):")
            for ident in idents:
                print(f"        - {ident}")

        # Set defaults for bypassed steps
        num_procs = 0
        chans_input = None
        parsed_ports = None
        dev = 0
        gain_val = "auto"
        ppm_val = 0
        sdr = {"mode": "auto", "rtl_sdr_bin": None, "rtl_tcp_bin": None, "rtlsdr_dll_dir": None}

    elif deployment_mode == "cloud":
        print("\n--- Step 3: Cloud Banc Configuration ---")
        print("  [A] Local Banc (Runs locally or on LAN on IPv4: 127.0.0.1)")
        print("  [B] Remote Cloud Banc (Hosted on remote cloud computer)")
        if auto_defaults:
            sub_choice = "B"
        else:
            sub_choice = input("[?] Select cloud location [A-B] (default: B): ").strip().upper() or "B"

        if sub_choice == "A":
            banc_location = "local"
            banc_host = "127.0.0.1"
        else:
            banc_location = "remote"
            if auto_defaults:
                remote_mode = "1"
                host_in = "127.0.0.1:23009"
                banc_host, banc_port = parse_endpoint(host_in)
            else:
                print("\n  Remote Cloud Banc Setup:")
                print("    [1] Already have Banc installed (enter host, port, secret & encryption key)")
                print("    [2] Setup Cloud Banc from scratch (deploy via SSH/SCP to a cloud Linux VM)")
                remote_mode = input("[?] Select remote Banc option [1-2] (default: 1): ").strip() or "1"

                if remote_mode == "2":
                    print("\n  --- Cloud Linux VM Prerequisites ---")
                    print("  A cloud Linux VM must be running with SSH access in a cloud such as AWS, Azure, or Google Cloud.")
                    print("  Ensure Python 3 is installed and inbound TCP ports for SSH (22) and Banc (23009) are open.")
                    input("  Press Enter once your Linux VM is ready (Next)... ")
                    while True:
                        vm_host = input("[?] Remote VM Host / IP (default 127.0.0.1): ").strip() or "127.0.0.1"
                        ssh_user = input("[?] SSH Username (e.g. ubuntu, ec2-user, azureuser): ").strip()
                        ssh_port_in = input("[?] SSH Port (default 22): ").strip() or "22"
                        banc_port_in = input("[?] Banc Port (default 23009): ").strip() or "23009"
                        ssh_key = input("[?] Path to SSH private key (.pem/id_rsa, optional): ").strip()
                        ssh_port = int(ssh_port_in) if ssh_port_in.isdigit() else 22
                        banc_port = int(banc_port_in) if banc_port_in.isdigit() else 23009
                        banc_host = vm_host.strip("[]")

                        auth_secret = auth_secret or ex_auth or config_loader.generate_auth_secret()
                        encryption_key = (
                            encryption_key if config_loader.is_valid_encryption_key(encryption_key)
                            else (ex_key or config_loader.generate_encryption_key())
                        )
                        ok_dep, dep_msg = deploy_banc_via_ssh(
                            host=banc_host,
                            ssh_user=ssh_user,
                            ssh_port=ssh_port,
                            ssh_key=ssh_key,
                            banc_port=banc_port,
                            auth_secret=auth_secret,
                            encryption_key=encryption_key,
                            identities=collect_local_identities(banc_host, extra_identities=[node_identity]),
                            progress_cb=lambda m: print(f"    [*] {m}"),
                        )
                        if ok_dep:
                            print(f"  [+] {dep_msg}")
                            break
                        print(f"\n  [!] Deployment Error: {dep_msg}")
                        retry = input("  [?] Retry / edit connection [R], proceed anyway [P], or switch to existing Banc [E]? [R/p/e]: ").strip().lower()
                        if retry in ("p", "proceed"):
                            print("  [!] Proceeding with setup anyway. Remote Banc configuration will be saved to config.json.")
                            banc_host = vm_host.strip("[]")
                            banc_port = int(banc_port_in) if str(banc_port_in).isdigit() else 23009
                            break
                        elif retry in ("e", "existing"):
                            host_in = input("[?] Enter remote Banc IP/Host and Port: ").strip() or "127.0.0.1:23009"
                            banc_host, banc_port = parse_endpoint(host_in)
                            break
                else:
                    host_in = input("[?] Enter remote Banc IP/Host and Port [e.g. [2600:1f18::1]:23009 or 127.0.0.1:23009]: ").strip() or "127.0.0.1:23009"
                    banc_host, banc_port = parse_endpoint(host_in)
                    idents = collect_local_identities(banc_host, extra_identities=[node_identity])
                    print("\n  [!] Reminder: Ensure your applications (Flux, Engine, Viewer, Creator) are whitelisted")
                    print("      on the remote Banc server (whitelist.txt). Local node identities:")
                    for ident in idents:
                        print(f"        - {ident}")

        ip_ver = config_loader.detect_ip_version(banc_host)
        print(f"[+] Configured Banc at {banc_host}:{banc_port} (Detected protocol: {ip_ver.upper()})")

    if deployment_mode != "engines_viewers":
        # 4. Security
        print("\n--- Step 4: Security (authentication secret & encryption key) ---")
        if deployment_mode == "local":
            auth = auth_secret or ex_auth or config_loader.generate_auth_secret()
            key = encryption_key if config_loader.is_valid_encryption_key(encryption_key) else (ex_key or config_loader.generate_encryption_key())
            print("  Local-only mode: unique credentials were generated automatically and stored in config.json.")
        else:
            # --- auth_secret ---
            if auth_secret:
                auth = auth_secret
            elif auto_defaults:
                auth = ex_auth or config_loader.generate_auth_secret()
            else:
                hint = "Enter keeps the existing one" if ex_auth else "Enter generates a random one"
                while True:
                    first = _ask_secret(f"[?] Banc authentication secret (auth_secret; {hint}): ")
                    if not first:
                        auth = ex_auth or config_loader.generate_auth_secret()
                        if not ex_auth:
                            print(f"    Generated auth_secret: {auth}")
                            print("    (write it down - every node using this Banc needs the same secret)")
                        break
                    if len(first) < config_loader.MIN_AUTH_SECRET_LEN:
                        print(f"    Too short: use at least {config_loader.MIN_AUTH_SECRET_LEN} characters.")
                        continue
                    if _ask_secret("[?] Repeat the secret: ") != first:
                        print("    The two entries do not match; try again.")
                        continue
                    auth = first
                    break

            # --- encryption key ---
            if encryption_key and config_loader.is_valid_encryption_key(encryption_key):
                key = encryption_key.strip()
            elif auto_defaults:
                key = ex_key or config_loader.generate_encryption_key()
            else:
                print("  Encryption key (Fernet):")
                print("    [1] Generate a NEW key (setting up a new Banc deployment)")
                print("    [2] Enter an EXISTING key (joining an existing Banc / other nodes)")
                if ex_key:
                    print("    [3] Keep the key already in config.json")
                default_choice = "3" if ex_key else "1"
                choice = input(f"[?] Select [1-{3 if ex_key else 2}] (default {default_choice}): ").strip() or default_choice
                if choice == "3" and ex_key:
                    key = ex_key
                elif choice == "2":
                    while True:
                        typed = input("[?] Paste the existing encryption key: ").strip()
                        if config_loader.is_valid_encryption_key(typed):
                            key = typed
                            break
                        print("    Not a valid key (must be 44 characters of URL-safe base64).")
                else:
                    key = config_loader.generate_encryption_key()
                    print(f"    Generated encryption_key: {key}")
                    print("    (stored in config.json; copy it to every node that talks to this Banc)")

        # 5. Grabber Channels & Processes
        print("\n--- Step 5: Radio Channels & Multiprocessing Setup (1 RTL-SDR, Bias-T ON, Auto-tune OFF) ---")
        max_procs = config_loader.get_max_allowed_procs()
        cpu_threads = os.cpu_count()
        if cpu_threads:
            print(f"  CPU detected: {cpu_threads} logical threads (max recommended Grabber processes: {max_procs})")
        else:
            print(f"  Max recommended Grabber processes: {max_procs}")

        # Channels
        default_chans_str = str(ex_grab.get("chans") or "")
        if not default_chans_str:
            if isinstance(ex_grab.get("channels"), (list, tuple)):
                default_chans_str = ", ".join(str(c) for c in ex_grab.get("channels"))
            else:
                default_chans_str = config_loader.DEFAULT_CHANNELS_STR

        print("  Specify PTC channels to monitor (comma-separated or ranges).")
        print("  Tip: Lock a channel to prevent re-tuning by appending '.' or 'l' (e.g. 101, 102., 107l).")
        if auto_defaults:
            chans_input = default_chans_str
        else:
            chans_input = prompt_editable(f"[?] PTC channels list (use arrows/del/backspace to edit)", default_chans_str)

        raw_chans, clean_channels, locked_channels = config_loader.parse_channels_with_locks(chans_input)
        print(f"  Configured: {len(clean_channels)} channels ({len(locked_channels)} locked): {clean_channels[:6]}... (locked: {sorted(list(locked_channels)) if locked_channels else 'none'})")

        # Number of processes
        default_procs = str(min(int(ex_grab.get("process_count", 2)), max_procs))
        if auto_defaults:
            procs_in = default_procs
        else:
            procs_in = input(f"[?] Number of Grabber processes for the single RTL-SDR? [1-{max_procs}] (default: {default_procs}): ").strip() or default_procs
        try:
            num_procs = max(1, min(max_procs, int(procs_in)))
        except ValueError:
            num_procs = min(2, max_procs)

        # Network ports per process
        # Automatically suggest ports corresponding to the specified channel numbers (18000 + channel_no)
        suggested_ports = config_loader.generate_default_ports(clean_channels, num_procs)
        default_ports_str = ", ".join(str(p) for p in suggested_ports)

        print(f"  Suggested --procs ports based on channels: {default_ports_str}")
        print(f"  Select network port for each process (ZMQ PUB --procs option for Grabber).")
        if auto_defaults:
            ports_input = default_ports_str
        else:
            ports_input = prompt_editable(f"[?] Network ports for {num_procs} process(es) (comma-separated)", default_ports_str)

        parsed_ports = []
        for p in ports_input.split(","):
            p = p.strip()
            if p.isdigit() and 1024 <= int(p) <= 65535:
                parsed_ports.append(int(p))
        while len(parsed_ports) < num_procs:
            fallback_p = suggested_ports[len(parsed_ports)] if len(parsed_ports) < len(suggested_ports) else ((max(parsed_ports) + 1) if parsed_ports else 18071)
            next_p = fallback_p
            while next_p in parsed_ports:
                next_p += 1
            parsed_ports.append(next_p)
        parsed_ports = parsed_ports[:num_procs]

        print(f"  * Grabber processes ({num_procs}) will broadcast on ZMQ ports: {', '.join(str(p) for p in parsed_ports)}")
        print(f"  * (Note: Grabber automatically re-arranges channels for each proc at runtime)")

        # 6. SDR hardware
        print("\n--- Step 6: SDR Hardware & Interface ---")
        default_dev = int(ex_grab.get("device", 0)) if str(ex_grab.get("device", 0)).isdigit() else 0
        if device_index is not None:
            dev = max(0, int(device_index))
        elif auto_defaults:
            dev = default_dev
        else:
            dev_in = input(f"[?] RTL-SDR device index for the Grabber (--device; 0 = first dongle) (default: {default_dev}): ").strip()
            dev = max(0, int(dev_in)) if dev_in.isdigit() else default_dev

        default_gain = str(ex_grab.get("gain", "auto"))
        if auto_defaults:
            gain_val = default_gain
        else:
            gain_in = input(f"[?] RTL-SDR tuner gain in dB, or 'auto' for AGC (default: {default_gain}): ").strip()
            gain_val = gain_in if gain_in else default_gain

        default_ppm = int(ex_grab.get("ppm", ex_grab.get("frequency_correction", 0)))
        if auto_defaults:
            ppm_val = default_ppm
        else:
            ppm_in = input(f"[?] RTL-SDR frequency correction in PPM (default: {default_ppm}): ").strip()
            try:
                ppm_val = int(ppm_in) if ppm_in else default_ppm
            except ValueError:
                ppm_val = default_ppm

        existing_sdr = config_loader._sdr_from_cfg(ex_grab)
        default_sdr_mode = str(ex_grab.get("sdr_mode") or existing_sdr.get("mode") or "auto").strip().lower()
        if default_sdr_mode not in ("auto", "pipe", "tcp", "rtlsdr", "stdin"):
            default_sdr_mode = "auto"

        print("  Select RTL-SDR hardware access mode:")
        print("    [1] auto    (probe and auto-detect available interface: pipe, tcp, rtlsdr)")
        print("    [2] pipe    (read raw I/Q from rtl_sdr subprocess pipe)")
        print("    [3] tcp     (connect to rtl_tcp server locally or over network)")
        print("    [4] rtlsdr  (read raw I/Q directly using Python rtlsdr package)")
        print("    [5] stdin   (read raw I/Q from standard input)")

        if auto_defaults:
            sdr_mode_choice = default_sdr_mode
        else:
            mode_in = input(f"[?] RTL-SDR access mode [auto/pipe/tcp/rtlsdr/stdin] (default: {default_sdr_mode}): ").strip().lower()
            mode_map = {"1": "auto", "2": "pipe", "3": "tcp", "4": "rtlsdr", "5": "stdin"}
            sdr_mode_choice = mode_map.get(mode_in, mode_in) if mode_in else default_sdr_mode
            if sdr_mode_choice not in ("auto", "pipe", "tcp", "rtlsdr", "stdin"):
                sdr_mode_choice = default_sdr_mode

        default_tcp_host = str(ex_grab.get("rtl_tcp_host") or existing_sdr.get("rtl_tcp_host") or "127.0.0.1").strip()
        default_tcp_port = int(ex_grab.get("rtl_tcp_port") or existing_sdr.get("rtl_tcp_port") or 1234)

        if sdr_mode_choice == "tcp":
            if auto_defaults:
                tcp_host_val = default_tcp_host
                tcp_port_val = default_tcp_port
            else:
                host_in = input(f"[?] RTL-TCP server host/IP (default: {default_tcp_host}): ").strip()
                tcp_host_val = host_in if host_in else default_tcp_host
                port_in = input(f"[?] RTL-TCP server port (default: {default_tcp_port}): ").strip()
                try:
                    tcp_port_val = int(port_in) if port_in else default_tcp_port
                except ValueError:
                    tcp_port_val = default_tcp_port
        else:
            tcp_host_val = default_tcp_host
            tcp_port_val = default_tcp_port

        has_existing_paths = any(existing_sdr.get(k) for k in ("rtl_sdr_bin", "rtl_tcp_bin", "rtlsdr_dll_dir"))
        env = config_loader.detect_sdr_environment(existing_sdr.get("rtlsdr_dll_dir"))
        print(f"  rtl_sdr  on PATH : {env['rtl_sdr_on_path'] or 'not found'}")
        print(f"  rtl_tcp  on PATH : {env['rtl_tcp_on_path'] or 'not found'}")
        print(f"  pyrtlsdr library : {'OK' if env['pyrtlsdr_ok'] else 'not available (' + str(env['pyrtlsdr_error']) + ')'}")

        sdr: Dict[str, Any]
        if sdr_path:
            sdr = config_loader.build_sdr_settings(sdr_path, env)
            if sdr.get("error"):
                print(f"[!] {sdr['error']}")
        elif has_existing_paths:
            sdr = dict(existing_sdr)
            print(f"  Keeping previously configured SDR paths: {_describe_sdr(sdr, dev)}")
        elif env["rtl_tcp_on_path"] or auto_defaults:
            sdr = config_loader.build_sdr_settings(None, env)
        else:
            print("  rtl_tcp was not found on PATH. Enter the path to the rtl_tcp executable (or rtl_sdr),")
            print("  or to the folder containing them / rtlsdr.dll + libusb-1.0.dll (Windows).")
            sdr = config_loader.build_sdr_settings(None, env)
            while True:
                p_in = input("[?] SDR tools path (Enter to skip): ").strip()
                if not p_in:
                    break
                cand = config_loader.build_sdr_settings(p_in, env)
                if cand.get("error"):
                    print(f"    {cand['error']}")
                    continue
                sdr = cand
                break
        sdr["mode"] = sdr_mode_choice
        sdr["rtl_tcp_host"] = tcp_host_val
        sdr["rtl_tcp_port"] = tcp_port_val
        print(f"[+] Grabber SDR settings: {_describe_sdr(sdr, dev)} (mode: {sdr_mode_choice})")

        if sdr.get("mode") == "rtlsdr" and importlib.util.find_spec("rtlsdr") is None:
            do_inst = True if auto_defaults else (input("[?] Direct mode needs the 'pyrtlsdr' package. Install it now? [Y/n]: ").strip().lower() in ("", "y", "yes"))
            if do_inst:
                ensure_pyrtlsdr(output_callback=lambda l: print(f"    {l}"))

    # 7. Boot Services
    print("\n--- Step 7: System Boot Automation ---")
    if auto_defaults:
        boot_in = "n"
    else:
        if deployment_mode == "engines_viewers":
            boot_in = input("[?] Configure ApexBOS Control Center to start automatically on system boot? [y/N]: ").strip().lower()
        else:
            boot_in = input("[?] Configure Grabber & Flux to start automatically on system boot? [y/N]: ").strip().lower()
    autostart = boot_in in ("y", "yes")

    # Generate and save
    ex_flux = (existing or {}).get("flux", {})
    burst_max = int(ex_flux["burst_max_packets"]) if ("burst_max_packets" in ex_flux and ex_flux["burst_max_packets"] != 1000) else 100
    burst_int = int(ex_flux["burst_interval_sec"]) if ("burst_interval_sec" in ex_flux and ex_flux["burst_interval_sec"] != 600) else 120

    ex_eng_conns = (existing or {}).get("engine", {}).get("connections", {})
    banc_init_hist = int(ex_eng_conns.get("banc_initial_history_sec", 60))

    cfg = config_loader.generate_unified_config(
        deployment_mode=deployment_mode,
        banc_location=banc_location,
        banc_host=banc_host,
        banc_port=banc_port,
        auth_secret=auth,
        encryption_key=key,
        identity=node_identity,
        process_count=num_procs,
        channels=chans_input,
        ports=parsed_ports,
        device_index=dev,
        sdr=sdr,
        gain=gain_val,
        ppm=ppm_val,
        packet_source=packet_source,
        flux_mcast_group=flux_mcast_group,
        flux_mcast_port=flux_mcast_port,
        burst_max_packets=burst_max,
        burst_interval_sec=burst_int,
        banc_initial_history_sec=banc_init_hist,
        sdr_mode=sdr_mode_choice if deployment_mode != "engines_viewers" else None,
        rtl_tcp_host=tcp_host_val if deployment_mode != "engines_viewers" else None,
        rtl_tcp_port=tcp_port_val if deployment_mode != "engines_viewers" else None,
    )
    res = finalize_install(cfg, autostart)
    print("\n[+] Saved master configuration -> config.json")
    print("[+] Synchronized legacy configs (grabber_config.json, MosFET.json, banc.json)")
    if res["boot"]:
        print(f"[+] Configured boot services for {res['boot']['platform']}: {res['boot']['details']}")
    if res["shortcuts"]:
        print(f"[+] Created Desktop shortcuts: {len(res['shortcuts'])}")
    if sys.platform == "win32":
        fw = config_loader.generate_windows_firewall_commands(cfg)
        print("\n" + "=" * 78)
        print("  Windows 11 Firewall & Multicast Configuration")
        print("=" * 78)
        print("  Notice: Windows Defender Firewall blocks Grabber TCP ports and Multicast UDP")
        print("  packets by default on clean Windows 11 installations.")
        print()
        print("  To allow Flux and Engine to receive all packets, copy and paste this command")
        print("  into an Administrator PowerShell prompt:")
        print()
        print("    " + fw["ps_oneliner"])
        print()
        print("  Or run the auto-elevating script generated in this folder:")
        print("    .\\enable_firewall.bat")
        print("=" * 78)
    print("=" * 78)
    print("  Setup complete! Run 'python ApexBOSLauncher.py' to launch.")
    print("=" * 78)
    return cfg


# ---------------------------------------------------------------------------
# Pygame GUI Setup Wizard
# ---------------------------------------------------------------------------

STEP_TITLES = [
    "",
    "Step 1 of 7: System Dependencies & Python Packages",
    "Step 2 of 7: Deployment Topology Selection",
    "Step 3 of 7: Banc Cloud & Network Configuration",
    "Step 4 of 7: Security - Authentication Secret & Encryption Key",
    "Step 5 of 7: Radio Channels & Multiprocessing Setup",
    "Step 6 of 7: SDR Hardware - Device Index & rtl_tcp / DLL Paths",
    "Step 7 of 7: Boot Automation & Desktop Shortcuts",
]


class PygameSetupWizard:
    """Rich graphical setup wizard using Pygame."""

    def __init__(self):
        import pygame
        pygame.init()
        pygame.font.init()

        self.width = 860
        self.height = 640
        self.screen = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption(f"ApexBOS Setup & Deployment Wizard ({VERSION})")

        self.clock = pygame.time.Clock()
        self.font_title = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 20, bold=True)
        self.font_h2 = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 15, bold=True)
        self.font_body = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 13)
        self.font_sm = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 11)

        self.step = 1
        self.total_steps = 7

        # Existing configuration provides defaults when re-running the wizard
        self.existing = load_existing_config()
        self.ex_auth, self.ex_key = existing_secrets(self.existing)
        ex_grab = (self.existing or {}).get("grabber", {})
        ex_ident = (
            ((self.existing or {}).get("common") or {}).get("banc", {}).get("identity")
            or ((self.existing or {}).get("engine", {}).get("connections", {})).get("identity")
        )
        if ex_ident and ex_ident != "ApexNode_01":
            self.node_identity = ex_ident
        else:
            self.node_identity = config_loader.generate_unique_identity()

        # Form state
        self.missing_deps = check_missing_dependencies()
        self.install_log: List[str] = []
        self.installing: bool = False
        self.install_progress: float = 0.0

        common_cfg = ((self.existing or {}).get("common") or {})
        ex_dep = common_cfg.get("deployment_mode", "cloud")
        if ex_dep in ("engines_viewers", "client", "display"):
            self.deployment_mode = "engines_viewers"
        elif ex_dep == "local":
            self.deployment_mode = "local"
        else:
            self.deployment_mode = "cloud"
        self.packet_source = common_cfg.get("packet_source", "flux")
        self.banc_location = "remote"        # "local" or "remote"
        self.remote_banc_mode = "existing"   # "existing" | "scratch"
        self.remote_substep: Optional[str] = None  # None | "vm_prereq" | "ssh_deploy"
        self.banc_ip_version = "ipv4"
        self.cpu_threads = os.cpu_count()
        self.max_allowed_procs = config_loader.get_max_allowed_procs()
        self.process_count = max(1, min(self.max_allowed_procs, int(ex_grab.get("process_count", min(2, self.max_allowed_procs)))))
        self.autostart_boot = False
        self.completed = False

        # Channel defaults
        ex_chans_str = str(ex_grab.get("chans") or "")
        if not ex_chans_str:
            if isinstance(ex_grab.get("channels"), (list, tuple)):
                ex_chans_str = ", ".join(str(c) for c in ex_grab.get("channels"))
            else:
                ex_chans_str = config_loader.DEFAULT_CHANNELS_STR

        # Port defaults
        ex_ports_list = ex_grab.get("ports")
        if not ex_ports_list and ex_grab.get("procs"):
            try:
                ex_ports_list = [int(p.strip()) for p in str(ex_grab.get("procs")).split(",") if p.strip().isdigit()]
            except Exception:
                ex_ports_list = None
        if not ex_ports_list or len(ex_ports_list) != self.process_count:
            _, clean_init_ch, _ = config_loader.parse_channels_with_locks(ex_chans_str)
            ex_ports_list = config_loader.generate_default_ports(clean_init_ch, self.process_count)

        ex_ports_str = ", ".join(str(p) for p in ex_ports_list)

        banc_cfg = common_cfg.get("banc") or {}
        host_default = "127.0.0.1:23009"
        if banc_cfg.get("enabled") and banc_cfg.get("host"):
            host_default = config_loader.format_endpoint(banc_cfg["host"], banc_cfg.get("port", 23009))

        ex_conn = ((self.existing or {}).get("engine", {}).get("connections", {}))
        ex_mcast = common_cfg.get("multicast", {})
        default_flux_grp = ex_conn.get("mcast_group") or ex_mcast.get("flux_group") or "239.1.1.1"
        default_flux_prt = str(ex_conn.get("mcast_port") or ex_mcast.get("flux_port") or 23003)

        self.fields: Dict[str, str] = {
            "banc_host": host_default,
            "auth_secret": self.ex_auth,
            "enc_key": self.ex_key or config_loader.generate_encryption_key(),
            "flux_mcast_group": default_flux_grp,
            "flux_mcast_port": default_flux_prt,
            "grabber_chans": ex_chans_str,
            "grabber_ports": ex_ports_str,
            "sdr_path": "",
            "sdr_gain": str(ex_grab.get("gain", "auto")),
            "sdr_ppm": str(ex_grab.get("ppm", ex_grab.get("frequency_correction", 0))),
            "ssh_host": "",
            "ssh_user": "ubuntu",
            "ssh_port": "22",
            "ssh_banc_port": "23009",
            "ssh_key": "",
        }
        self.ssh_deploying = False
        self.ssh_deploy_ok = False
        self.ssh_deploy_attempted = False
        self.ssh_deploy_error: Optional[str] = None
        self.ssh_deploy_log: List[str] = []
        self.local_identities: List[str] = [self.node_identity]
        self.custom_ports_edited = False
        if self.deployment_mode == "engines_viewers":
            self.key_mode = "existing"
            if not self.ex_key:
                self.fields["enc_key"] = ""
        else:
            self.key_mode = "existing" if self.ex_key else "generate"   # "generate" (new key) | "existing"
        self.show_secret = False
        self.active_field: Optional[str] = None
        self.cursor_pos: int = 0
        self.field_scroll: Dict[str, int] = {}
        self.select_all_active: bool = False

        self.device_index = int(ex_grab.get("device", 0)) if str(ex_grab.get("device", 0)).isdigit() else 0
        self.existing_sdr = config_loader._sdr_from_cfg(ex_grab)
        self.sdr_mode = str(ex_grab.get("sdr_mode") or self.existing_sdr.get("mode") or "auto").strip().lower()
        if self.sdr_mode not in ("auto", "pipe", "tcp", "rtlsdr", "stdin"):
            self.sdr_mode = "auto"
        self.fields["rtl_tcp_host"] = str(ex_grab.get("rtl_tcp_host") or self.existing_sdr.get("rtl_tcp_host") or "127.0.0.1")
        self.fields["rtl_tcp_port"] = str(ex_grab.get("rtl_tcp_port") or self.existing_sdr.get("rtl_tcp_port") or "1234")
        self.sdr: Dict[str, Any] = dict(self.existing_sdr) if any(
            self.existing_sdr.get(k) for k in ("rtl_sdr_bin", "rtl_tcp_bin", "rtlsdr_dll_dir")) else {
            "mode": self.sdr_mode, "rtl_sdr_bin": None, "rtl_tcp_bin": None, "rtlsdr_dll_dir": None, "error": None}
        self.sdr["mode"] = self.sdr_mode
        self.sdr["rtl_tcp_host"] = self.fields["rtl_tcp_host"]
        self.sdr["rtl_tcp_port"] = int(self.fields["rtl_tcp_port"]) if self.fields["rtl_tcp_port"].isdigit() else 1234
        self.sdr_env: Optional[Dict[str, Any]] = None

        self.status_msg = ""
        self.firewall_toast: Optional[str] = None

    # ----- layout ---------------------------------------------------------
    def _rect(self, name: str):
        import pygame
        layout = {
            "next": (self.width - 150, self.height - 55, 125, 38),
            "back": (25, self.height - 55, 100, 38),
            "install": (35, 480, 260, 38),
            "card_local": (35, 130, 790, 92),
            "card_cloud": (35, 234, 790, 92),
            "card_client": (35, 338, 790, 95),
            "loc_local": (50, 150, 500, 28),
            "loc_remote": (50, 185, 500, 28),
            "remote_existing": (75, 228, 620, 28),
            "remote_scratch": (75, 260, 620, 28),
            "banc_host": (75, 330, 500, 36),
            "src_flux": (50, 142, 500, 28),
            "src_banc": (50, 174, 500, 28),
            "flux_mcast_group": (50, 240, 350, 36),
            "flux_mcast_port": (430, 240, 200, 36),
            "ssh_host": (50, 145, 420, 34),
            "ssh_port": (490, 145, 110, 34),
            "ssh_banc_port": (620, 145, 150, 34),
            "ssh_user": (50, 212, 290, 34),
            "ssh_key": (360, 212, 410, 34),
            "ssh_deploy_btn": (50, 260, 270, 36),
            "ssh_proceed_btn": (335, 260, 180, 36),
            "auth_secret": (50, 140, 500, 36),
            "auth_show": (565, 140, 90, 36),
            "auth_gen": (665, 140, 150, 36),
            "key_gen": (50, 235, 600, 28),
            "key_exist": (50, 268, 600, 28),
            "enc_key": (50, 305, 520, 36),
            "key_paste": (580, 305, 100, 36),
            "key_regen": (690, 305, 120, 36),
            "grabber_chans": (50, 155, 760, 34),
            "procs_minus": (50, 252, 40, 34),
            "procs_val": (95, 252, 70, 34),
            "procs_plus": (170, 252, 40, 34),
            "proc_preset_1": (230, 252, 60, 34),
            "proc_preset_2": (298, 252, 65, 34),
            "proc_preset_4": (371, 252, 65, 34),
            "proc_preset_max": (444, 252, 95, 34),
            "grabber_ports": (50, 325, 620, 34),
            "ports_auto": (680, 325, 130, 34),
            "dev_minus": (50, 140, 40, 36),
            "dev_val": (95, 140, 70, 36),
            "dev_plus": (170, 140, 40, 36),
            "sdr_gain": (260, 140, 230, 36),
            "sdr_ppm": (530, 140, 260, 36),
            "sdr_mode_auto": (50, 205, 140, 32),
            "sdr_mode_pipe": (198, 205, 140, 32),
            "sdr_mode_tcp": (346, 205, 140, 32),
            "sdr_mode_rtlsdr": (494, 205, 140, 32),
            "sdr_mode_stdin": (642, 205, 148, 32),
            "rtl_tcp_host": (185, 246, 320, 32),
            "rtl_tcp_port": (565, 246, 120, 32),
            "sdr_path": (50, 372, 745, 36),
            "boot_cb": (40, 138, 600, 30),
            "firewall_copy": (55, 272, 250, 34),
            "firewall_apply": (320, 272, 210, 34),
        }
        if getattr(self, "step", None) == 6 and getattr(self, "sdr_mode", "auto") == "tcp":
            layout["sdr_path"] = (50, 392, 745, 34)
        if getattr(self, "deployment_mode", None) == "engines_viewers" and getattr(self, "step", None) == 3 and getattr(self, "packet_source", None) == "banc":
            layout["banc_host"] = (50, 230, 480, 36)
            layout["auth_secret"] = (50, 298, 480, 36)
            layout["auth_show"] = (545, 298, 90, 36)
            layout["auth_gen"] = (645, 298, 165, 36)
            layout["enc_key"] = (50, 366, 520, 36)
            layout["key_paste"] = (580, 366, 100, 36)
            layout["key_regen"] = (690, 366, 120, 36)
        return pygame.Rect(*layout[name])

    def _proc_rect(self, n: int):
        import pygame
        return pygame.Rect(35 + (n - 1) * 85, 195, 75, 38)

    # ----- main loop -------------------------------------------------------
    def run(self) -> Optional[Dict[str, Any]]:
        import pygame
        running = True
        while running:
            self.clock.tick(30)
            events = pygame.event.get()
            for event in events:
                if event.type == pygame.QUIT:
                    return None
                elif event.type == pygame.KEYDOWN:
                    self._handle_keydown(event)
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    self._handle_mouse(event)

            self._draw()
            pygame.display.flip()

            if self.completed:
                time.sleep(0.3)
                running = False

        return self._build_and_install()

    def _build_and_install(self) -> Dict[str, Any]:
        dep_mode_val = self.deployment_mode
        banc_loc_val = self.banc_location
        pkt_src_val = getattr(self, "packet_source", "flux")
        flux_grp = self.fields.get("flux_mcast_group", "239.1.1.1").strip() or "239.1.1.1"
        flux_prt_raw = self.fields.get("flux_mcast_port", "23003").strip()
        flux_prt = int(flux_prt_raw) if flux_prt_raw.isdigit() else 23003

        if dep_mode_val == "engines_viewers":
            if pkt_src_val == "flux":
                host, port = "none", 23009
                banc_loc_val = "none"
                auth = self.fields.get("auth_secret") or self.ex_auth or config_loader.generate_auth_secret()
                key = self.fields.get("enc_key") or self.ex_key or config_loader.generate_encryption_key()
            else:
                host, port = parse_endpoint(self.fields["banc_host"])
                banc_loc_val = "remote"
                auth = self.fields["auth_secret"]
                key = self.fields["enc_key"]
                flux_grp = None
                flux_prt = None
            procs_count = 0
            chans_val = None
            ports_list = None
            dev_idx = 0
            sdr_val = None
            gain_str = "auto"
            ppm_int = 0
        elif dep_mode_val == "local":
            host, port = "127.0.0.1", 23009
            banc_loc_val = "none"
            auth = self.fields["auth_secret"] or self.ex_auth or config_loader.generate_auth_secret()
            key = self.ex_key or config_loader.generate_encryption_key()
            self._eval_sdr()
            if self.sdr.get("mode") == "rtlsdr":
                ensure_pyrtlsdr()
            gain_str = self.fields.get("sdr_gain", "auto").strip() or "auto"
            ppm_raw = self.fields.get("sdr_ppm", "0").strip()
            ppm_int = int(ppm_raw) if ppm_raw.lstrip("-").isdigit() else 0
            ports_list = [int(p.strip()) for p in self.fields.get("grabber_ports", "").split(",") if p.strip().isdigit()]
            procs_count = self.process_count
            chans_val = self.fields.get("grabber_chans")
            dev_idx = self.device_index
            sdr_val = self.sdr
        else: # cloud
            if self.banc_location == "local":
                host, port = "127.0.0.1", 23009
            else:
                host, port = parse_endpoint(self.fields["banc_host"])
            auth = self.fields["auth_secret"]
            key = self.fields["enc_key"]
            self._eval_sdr()
            if self.sdr.get("mode") == "rtlsdr":
                ensure_pyrtlsdr()
            gain_str = self.fields.get("sdr_gain", "auto").strip() or "auto"
            ppm_raw = self.fields.get("sdr_ppm", "0").strip()
            ppm_int = int(ppm_raw) if ppm_raw.lstrip("-").isdigit() else 0
            ports_list = [int(p.strip()) for p in self.fields.get("grabber_ports", "").split(",") if p.strip().isdigit()]
            procs_count = self.process_count
            chans_val = self.fields.get("grabber_chans")
            dev_idx = self.device_index
            sdr_val = self.sdr

        mode_val = getattr(self, "sdr_mode", "auto")
        tcp_h = self.fields.get("rtl_tcp_host", "127.0.0.1").strip() or "127.0.0.1"
        tcp_p_raw = self.fields.get("rtl_tcp_port", "1234").strip()
        tcp_p = int(tcp_p_raw) if tcp_p_raw.isdigit() else 1234
        if sdr_val is not None and isinstance(sdr_val, dict):
            sdr_val["mode"] = mode_val
            sdr_val["rtl_tcp_host"] = tcp_h
            sdr_val["rtl_tcp_port"] = tcp_p

        ex_flux = (self.existing or {}).get("flux", {})
        burst_max = int(ex_flux["burst_max_packets"]) if ("burst_max_packets" in ex_flux and ex_flux["burst_max_packets"] != 1000) else 100
        burst_int = int(ex_flux["burst_interval_sec"]) if ("burst_interval_sec" in ex_flux and ex_flux["burst_interval_sec"] != 600) else 120

        ex_eng_conns = (self.existing or {}).get("engine", {}).get("connections", {})
        banc_init_hist = int(ex_eng_conns.get("banc_initial_history_sec", 60))

        cfg = config_loader.generate_unified_config(
            deployment_mode=dep_mode_val,
            banc_location=banc_loc_val,
            banc_host=host,
            banc_port=port,
            auth_secret=auth,
            encryption_key=key,
            identity=self.node_identity,
            process_count=procs_count,
            channels=chans_val,
            ports=ports_list,
            device_index=dev_idx,
            sdr=sdr_val,
            gain=gain_str,
            ppm=ppm_int,
            packet_source=pkt_src_val,
            flux_mcast_group=flux_grp,
            flux_mcast_port=flux_prt,
            burst_max_packets=burst_max,
            burst_interval_sec=burst_int,
            banc_initial_history_sec=banc_init_hist,
            sdr_mode=mode_val if dep_mode_val != "engines_viewers" else None,
            rtl_tcp_host=tcp_h if dep_mode_val != "engines_viewers" else None,
            rtl_tcp_port=tcp_p if dep_mode_val != "engines_viewers" else None,
        )
        finalize_install(cfg, self.autostart_boot)
        return cfg

    # ----- input -------------------------------------------------------------
    def _clipboard_text(self) -> str:
        import pygame
        # 1. Primary: Use pygame.scrap (native SDL clipboard, cross-platform Windows/Linux/macOS)
        try:
            raw = pygame.scrap.get_text()
            if raw:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8", errors="replace")
                    except Exception:
                        raw = raw.decode("latin1", errors="replace")
                clean = str(raw).replace("\ufeff", "").replace("\x00", "").strip()
                if clean:
                    return clean.splitlines()[0]
        except Exception:
            pass

        # 2. Windows fallback: direct Win32 API via ctypes with strict 64-bit argument/return types
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32

                user32.OpenClipboard.argtypes = [wintypes.HWND]
                user32.OpenClipboard.restype = wintypes.BOOL
                user32.CloseClipboard.argtypes = []
                user32.CloseClipboard.restype = wintypes.BOOL
                user32.GetClipboardData.argtypes = [wintypes.UINT]
                user32.GetClipboardData.restype = wintypes.HANDLE
                kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
                kernel32.GlobalLock.restype = wintypes.LPVOID
                kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
                kernel32.GlobalUnlock.restype = wintypes.BOOL

                CF_UNICODETEXT = 13
                if user32.OpenClipboard(None):
                    try:
                        h_clip = user32.GetClipboardData(CF_UNICODETEXT)
                        if h_clip:
                            p_clip = kernel32.GlobalLock(h_clip)
                            if p_clip:
                                try:
                                    txt = ctypes.c_wchar_p(p_clip).value
                                    if txt:
                                        clean = str(txt).replace("\ufeff", "").replace("\x00", "").strip()
                                        if clean:
                                            return clean.splitlines()[0]
                                finally:
                                    kernel32.GlobalUnlock(h_clip)
                    finally:
                        user32.CloseClipboard()
            except Exception:
                pass

        return ""

    def _field_editable(self, name: str) -> bool:
        if name == "enc_key":
            if getattr(self, "deployment_mode", None) == "engines_viewers":
                return True
            if getattr(self, "step", None) == 3 and getattr(self, "packet_source", None) == "banc":
                return True
            return self.key_mode == "existing"
        return True

    def _set_active(self, name: Optional[str]):
        prev = self.active_field
        self.active_field = name
        self.select_all_active = False
        if name and name in self.fields:
            self.cursor_pos = len(self.fields[name])
        if prev == "sdr_path" and name != "sdr_path":
            self._eval_sdr()
        if prev == "grabber_chans" and name != "grabber_chans":
            self._on_grabber_chans_changed()

    def _on_grabber_chans_changed(self):
        """When user finishes specifying --chans, automatically suggest procs ports corresponding to channel numbers."""
        raw_val = self.fields.get("grabber_chans", "").strip()
        if not raw_val:
            return
        _, clean_chans, _ = config_loader.parse_channels_with_locks(raw_val)
        if clean_chans:
            ports = config_loader.generate_default_ports(clean_chans, self.process_count)
            self.fields["grabber_ports"] = ", ".join(str(p) for p in ports)
            self.custom_ports_edited = False

    def _calc_cursor_pos(self, name: str, click_x: int) -> int:
        """Calculates character index corresponding to click_x inside field name."""
        text = self.fields.get(name, "")
        scroll = self.field_scroll.get(name, 0)
        r = self._rect(name)
        rel_x = click_x - (r.x + 10)
        if rel_x <= 0:
            return scroll
        best_pos = scroll
        min_dist = float("inf")
        for i in range(scroll, len(text) + 1):
            substr = text[scroll:i]
            w = self.font_body.size(substr)[0]
            dist = abs(w - rel_x)
            if dist < min_dist:
                min_dist = dist
                best_pos = i
            elif w > rel_x:
                break
        return best_pos

    def _handle_keydown(self, event):
        import pygame
        name = self.active_field
        if not name or not self._field_editable(name):
            return
        buf = self.fields[name]
        pos = max(0, min(len(buf), self.cursor_pos))

        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE, pygame.K_TAB):
            self._set_active(None)
            self.select_all_active = False
            return
        elif (event.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)) and event.key == pygame.K_a:
            self.select_all_active = True
            return
        elif (event.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)) and event.key == pygame.K_v:
            clip_val = self._clipboard_text()
            pasted = "".join(ch for ch in clip_val if ch.isprintable()).strip().strip("'\"")
            if pasted:
                if name == "enc_key" and (len(pasted) >= 40 or getattr(self, "select_all_active", False)):
                    buf = pasted
                    self.cursor_pos = len(buf)
                elif getattr(self, "select_all_active", False):
                    buf = pasted
                    self.cursor_pos = len(buf)
                else:
                    buf = buf[:pos] + pasted + buf[pos:]
                    self.cursor_pos = pos + len(pasted)
            self.select_all_active = False
        elif event.key == pygame.K_BACKSPACE:
            if getattr(self, "select_all_active", False):
                buf = ""
                self.cursor_pos = 0
                self.select_all_active = False
            elif pos > 0:
                buf = buf[:pos - 1] + buf[pos:]
                self.cursor_pos = pos - 1
        elif event.key == pygame.K_DELETE:
            if getattr(self, "select_all_active", False):
                buf = ""
                self.cursor_pos = 0
                self.select_all_active = False
            elif pos < len(buf):
                buf = buf[:pos] + buf[pos + 1:]
                # cursor_pos stays pos
        elif event.key == pygame.K_LEFT:
            self.select_all_active = False
            self.cursor_pos = max(0, pos - 1)
            return
        elif event.key == pygame.K_RIGHT:
            self.select_all_active = False
            self.cursor_pos = min(len(buf), pos + 1)
            return
        elif event.key == pygame.K_HOME:
            self.select_all_active = False
            self.cursor_pos = 0
            return
        elif event.key == pygame.K_END:
            self.select_all_active = False
            self.cursor_pos = len(buf)
            return
        elif event.unicode and event.unicode.isprintable() and not (event.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)):
            if getattr(self, "select_all_active", False):
                buf = event.unicode
                self.cursor_pos = len(buf)
                self.select_all_active = False
            else:
                buf = buf[:pos] + event.unicode + buf[pos:]
                self.cursor_pos = pos + len(event.unicode)

        self.fields[name] = buf
        if name == "grabber_ports":
            self.custom_ports_edited = True
        elif name == "banc_host":
            self.banc_ip_version = config_loader.detect_ip_version(parse_endpoint(buf)[0])

    def _eval_sdr(self):
        """Re-evaluates the SDR settings from the typed path (and PATH detection)."""
        path = self.fields["sdr_path"].strip()
        if self.sdr_env is None:
            self.sdr_env = config_loader.detect_sdr_environment(self.sdr.get("rtlsdr_dll_dir"))
        if path:
            self.sdr = config_loader.build_sdr_settings(path, self.sdr_env)
        elif not any(self.existing_sdr.get(k) for k in ("rtl_sdr_bin", "rtl_tcp_bin", "rtlsdr_dll_dir")):
            self.sdr = config_loader.build_sdr_settings(None, self.sdr_env)
        else:
            self.sdr = dict(self.existing_sdr)
            self.sdr["error"] = None
        if hasattr(self, "sdr_mode") and self.sdr_mode:
            self.sdr["mode"] = self.sdr_mode
            self.sdr["rtl_tcp_host"] = self.fields.get("rtl_tcp_host", "127.0.0.1").strip() or "127.0.0.1"
            try:
                self.sdr["rtl_tcp_port"] = int(self.fields.get("rtl_tcp_port", "1234").strip() or "1234")
            except ValueError:
                self.sdr["rtl_tcp_port"] = 1234

    def _can_advance(self) -> Tuple[bool, str]:
        if self.deployment_mode == "engines_viewers":
            if self.step == 3:
                if getattr(self, "packet_source", "flux") == "flux":
                    if not self.fields.get("flux_mcast_group", "").strip():
                        return False, "Enter the Flux Multicast Group (e.g. 239.1.1.1)."
                    prt = self.fields.get("flux_mcast_port", "").strip()
                    if not prt.isdigit() or not (1024 <= int(prt) <= 65535):
                        return False, "Enter a valid Flux Multicast Port (1024-65535)."
                    return True, ""
                else: # banc
                    if not parse_endpoint(self.fields.get("banc_host", ""))[0]:
                        return False, "Enter the Banc host / IP (e.g. 192.168.1.10:23009)."
                    if len(self.fields.get("auth_secret", "")) < config_loader.MIN_AUTH_SECRET_LEN:
                        return False, f"The authentication secret needs at least {config_loader.MIN_AUTH_SECRET_LEN} characters."
                    if not config_loader.is_valid_encryption_key(self.fields.get("enc_key", "")):
                        return False, "The encryption key is not valid (44 characters, URL-safe base64). Press 'New Key' or paste a valid key."
                    return True, ""
            return True, ""

        if self.step == 3 and self.deployment_mode == "cloud" and self.banc_location == "remote":
            if self.remote_substep is None and self.remote_banc_mode == "existing":
                if not parse_endpoint(self.fields["banc_host"])[0]:
                    return False, "Enter the Banc host / IP (and optional port)."
            elif self.remote_substep == "ssh_deploy":
                if self.ssh_deploying:
                    return False, "Cloud Banc deployment is in progress..."
                if not self.ssh_deploy_ok:
                    if self.ssh_deploy_attempted:
                        return True, ""
                    return False, "Click 'Deploy & Start Cloud Banc' first, or click '< Back' to edit/switch."
        if self.step == 4 and self.deployment_mode == "cloud":
            if len(self.fields["auth_secret"]) < config_loader.MIN_AUTH_SECRET_LEN:
                return False, f"The authentication secret needs at least {config_loader.MIN_AUTH_SECRET_LEN} characters."
            if not config_loader.is_valid_encryption_key(self.fields["enc_key"]):
                return False, "The encryption key is not valid (44 characters, URL-safe base64). Generate a new one or paste a valid key."
        if self.step == 5:
            if not self.fields.get("grabber_chans", "").strip():
                return False, "Specify at least one valid radio channel (e.g. 101, 102., 107l)."
            raw_ch, clean_chans, _ = config_loader.parse_channels_with_locks(self.fields.get("grabber_chans", ""))
            if not clean_chans:
                return False, "Specify at least one valid radio channel (e.g. 101, 102., 107l)."
            ports = [p.strip() for p in self.fields.get("grabber_ports", "").split(",") if p.strip()]
            valid_ports = []
            for p in ports:
                if not p.isdigit() or not (1024 <= int(p) <= 65535):
                    return False, f"Invalid port '{p}'. Ports must be integers between 1024 and 65535."
                valid_ports.append(int(p))
            if len(valid_ports) != self.process_count:
                return False, f"Process count is {self.process_count}, but {len(valid_ports)} port(s) were specified. Click 'Auto Ports' or provide {self.process_count} comma-separated ports."
            if len(set(valid_ports)) != len(valid_ports):
                return False, "Duplicate ports detected. Each Grabber process requires a unique port."
        if self.step == 6:
            self._eval_sdr()
            if getattr(self, "sdr_mode", "auto") == "tcp":
                h = self.fields.get("rtl_tcp_host", "").strip()
                if not h:
                    return False, "Enter the RTL-TCP server host or IP address (e.g. 127.0.0.1)."
                p = self.fields.get("rtl_tcp_port", "").strip()
                if not p.isdigit() or not (1 <= int(p) <= 65535):
                    return False, "Enter a valid RTL-TCP server port (1-65535)."
            if self.fields["sdr_path"].strip() and self.sdr.get("error"):
                return False, self.sdr["error"]
        return True, ""

    def _handle_mouse(self, event):
        import pygame
        if isinstance(event, (tuple, list)):
            pos = tuple(event)
        elif hasattr(event, "button"):
            if event.button != 1:
                return
            pos = event.pos
        elif hasattr(event, "pos"):
            pos = event.pos
        else:
            return

        # Navigation buttons (Next / Back)
        if self._rect("next").collidepoint(pos):
            if self.active_field:
                self._set_active(None)
            ok, msg = self._can_advance()
            self.status_msg = msg
            if ok:
                if self.deployment_mode == "engines_viewers":
                    if self.step == 2:
                        self.step = 3
                        return
                    elif self.step == 3:
                        if getattr(self, "packet_source", "flux") == "banc":
                            h = parse_endpoint(self.fields["banc_host"])[0]
                            threading.Thread(
                                target=lambda: setattr(self, "local_identities", collect_local_identities(h, [self.node_identity])),
                                daemon=True,
                            ).start()
                        self.step = 7
                        return
                    elif self.step >= 7:
                        self.completed = True
                        return

                if self.step == 3 and self.deployment_mode == "cloud" and self.banc_location == "remote":
                    if self.remote_substep is None and self.remote_banc_mode == "scratch":
                        if not self.fields["ssh_host"]:
                            cur_h = parse_endpoint(self.fields["banc_host"])[0]
                            if cur_h and cur_h != "127.0.0.1":
                                self.fields["ssh_host"] = cur_h
                        self.remote_substep = "vm_prereq"
                        return
                    elif self.remote_substep == "vm_prereq":
                        self.remote_substep = "ssh_deploy"
                        return
                    elif self.remote_substep == "ssh_deploy":
                        host = self.fields["ssh_host"].strip().strip("[]") or "127.0.0.1"
                        b_port = int(self.fields["ssh_banc_port"]) if self.fields["ssh_banc_port"].strip().isdigit() else 23009
                        self.fields["banc_host"] = config_loader.format_endpoint(host, b_port)
                        self.banc_ip_version = config_loader.detect_ip_version(host)
                        self.remote_substep = None
                    elif self.remote_substep is None and self.remote_banc_mode == "existing":
                        h = parse_endpoint(self.fields["banc_host"])[0]
                        threading.Thread(
                            target=lambda: setattr(self, "local_identities", collect_local_identities(h, [self.node_identity])),
                            daemon=True,
                        ).start()
                if self.step < self.total_steps:
                    self.step += 1
                else:
                    self.completed = True
            return
        if self._rect("back").collidepoint(pos):
            self._set_active(None)
            self.status_msg = ""
            if self.deployment_mode == "engines_viewers":
                if self.step >= 7:
                    self.step = 3
                    return
                elif self.step == 3:
                    self.step = 2
                    return
                elif self.step == 2:
                    self.step = 1
                    return
            if self.step == 3 and self.remote_substep == "ssh_deploy":
                self.remote_substep = "vm_prereq"
                return
            if self.step == 3 and self.remote_substep == "vm_prereq":
                self.remote_substep = None
                return
            if self.step == 4 and self.deployment_mode == "cloud" and self.banc_location == "remote" and self.remote_banc_mode == "scratch":
                self.step = 3
                self.remote_substep = "ssh_deploy"
                return
            if self.step > 1:
                self.step -= 1
            return

        if self.step == 1:
            if self._rect("install").collidepoint(pos) and not self.installing and self.missing_deps:
                self._trigger_dependency_install()

        elif self.step == 2:
            if self._rect("card_local").collidepoint(pos):
                self.deployment_mode = "local"
            elif self._rect("card_cloud").collidepoint(pos):
                self.deployment_mode = "cloud"
            elif self._rect("card_client").collidepoint(pos):
                self.deployment_mode = "engines_viewers"
                self.key_mode = "existing"
                if not self.ex_key:
                    self.fields["enc_key"] = ""

        elif self.step == 3:
            if self.deployment_mode == "engines_viewers":
                if self._rect("src_flux").collidepoint(pos):
                    self.packet_source = "flux"
                    self._set_active(None)
                elif self._rect("src_banc").collidepoint(pos):
                    self.packet_source = "banc"
                    self.key_mode = "existing"
                    self._set_active(None)
                elif getattr(self, "packet_source", "flux") == "flux":
                    for fn in ("flux_mcast_group", "flux_mcast_port"):
                        if self._rect(fn).collidepoint(pos):
                            self._set_active(fn)
                            self.cursor_pos = self._calc_cursor_pos(fn, pos[0])
                            return
                    self._set_active(None)
                else: # banc
                    if self._rect("auth_show").collidepoint(pos):
                        self.show_secret = not self.show_secret
                    elif self._rect("auth_gen").collidepoint(pos):
                        self.fields["auth_secret"] = config_loader.generate_auth_secret()
                        self.show_secret = True
                    elif self._rect("key_paste").collidepoint(pos):
                        clip = self._clipboard_text()
                        if clip:
                            self.fields["enc_key"] = clip.strip().strip("'\"")
                            self.key_mode = "existing"
                            self._set_active("enc_key")
                            self.cursor_pos = len(self.fields["enc_key"])
                            self.status_msg = "Encryption key pasted from clipboard."
                        else:
                            self.status_msg = "Clipboard is empty. Copy key to clipboard first."
                    elif self._rect("key_regen").collidepoint(pos):
                        self.fields["enc_key"] = config_loader.generate_encryption_key()
                        self.key_mode = "existing"
                        self._set_active("enc_key")
                        self.cursor_pos = len(self.fields["enc_key"])
                        self.status_msg = "Generated new random Fernet encryption key."
                    for fn in ("banc_host", "auth_secret", "enc_key"):
                        if self._rect(fn).collidepoint(pos):
                            if fn == "enc_key":
                                self.key_mode = "existing"
                            self._set_active(fn)
                            self.cursor_pos = self._calc_cursor_pos(fn, pos[0])
                            return
                    self._set_active(None)
                return

            if self.remote_substep == "vm_prereq":
                self._set_active(None)
            elif self.remote_substep == "ssh_deploy":
                if self._rect("ssh_deploy_btn").collidepoint(pos) and not self.ssh_deploying:
                    self._trigger_ssh_banc_deploy()
                if (
                    not self.ssh_deploy_ok
                    and self.ssh_deploy_attempted
                    and not self.ssh_deploying
                    and self._rect("ssh_proceed_btn").collidepoint(pos)
                ):
                    host = self.fields["ssh_host"].strip().strip("[]") or "127.0.0.1"
                    b_port = int(self.fields["ssh_banc_port"]) if self.fields["ssh_banc_port"].strip().isdigit() else 23009
                    self.fields["banc_host"] = config_loader.format_endpoint(host, b_port)
                    self.banc_ip_version = config_loader.detect_ip_version(host)
                    self.remote_substep = None
                    self.step += 1
                    return
                for fname in ("ssh_host", "ssh_port", "ssh_banc_port", "ssh_user", "ssh_key"):
                    if self._rect(fname).collidepoint(pos):
                        self._set_active(fname)
                        self.cursor_pos = self._calc_cursor_pos(fname, pos[0])
                        return
                self._set_active(None)
            else:
                if self._rect("loc_local").collidepoint(pos):
                    self.banc_location = "local"
                    self.fields["banc_host"] = "127.0.0.1:23009"
                    self.banc_ip_version = "ipv4"
                elif self._rect("loc_remote").collidepoint(pos):
                    self.banc_location = "remote"
                elif self.banc_location == "remote" and self._rect("remote_existing").collidepoint(pos):
                    self.remote_banc_mode = "existing"
                elif self.banc_location == "remote" and self._rect("remote_scratch").collidepoint(pos):
                    self.remote_banc_mode = "scratch"
                if (
                    self.deployment_mode == "cloud"
                    and self.banc_location == "remote"
                    and self.remote_banc_mode == "existing"
                    and self._rect("banc_host").collidepoint(pos)
                ):
                    self._set_active("banc_host")
                    self.cursor_pos = self._calc_cursor_pos("banc_host", pos[0])
                else:
                    self._set_active(None)

        elif self.step == 4 and self.deployment_mode == "cloud":
            if self._rect("auth_show").collidepoint(pos):
                self.show_secret = not self.show_secret
            elif self._rect("auth_gen").collidepoint(pos):
                self.fields["auth_secret"] = config_loader.generate_auth_secret()
                self.show_secret = True
            elif self._rect("key_gen").collidepoint(pos):
                self.key_mode = "generate"
                self.fields["enc_key"] = config_loader.generate_encryption_key()
            elif self._rect("key_exist").collidepoint(pos):
                if self.key_mode != "existing":
                    self.key_mode = "existing"
                    self.fields["enc_key"] = self.ex_key
            elif self._rect("key_paste").collidepoint(pos):
                clip = self._clipboard_text()
                if clip:
                    self.key_mode = "existing"
                    self.fields["enc_key"] = clip.strip().strip("'\"")
                    self.cursor_pos = len(self.fields["enc_key"])
                    self._set_active("enc_key")
                    self.status_msg = "Encryption key pasted from clipboard."
                else:
                    self.status_msg = "Clipboard is empty. Copy key to clipboard first."
            elif self._rect("key_regen").collidepoint(pos) and self.key_mode == "generate":
                self.fields["enc_key"] = config_loader.generate_encryption_key()
            if self._rect("auth_secret").collidepoint(pos):
                self._set_active("auth_secret")
                self.cursor_pos = self._calc_cursor_pos("auth_secret", pos[0])
            elif self._rect("enc_key").collidepoint(pos):
                self.key_mode = "existing"
                self._set_active("enc_key")
                self.cursor_pos = self._calc_cursor_pos("enc_key", pos[0])
            else:
                self._set_active(None)

        elif self.step == 5:
            if self._rect("grabber_chans").collidepoint(pos):
                self._set_active("grabber_chans")
                self.cursor_pos = self._calc_cursor_pos("grabber_chans", pos[0])
                return
            elif self._rect("grabber_ports").collidepoint(pos):
                self._set_active("grabber_ports")
                self.cursor_pos = self._calc_cursor_pos("grabber_ports", pos[0])
                return
            elif self._rect("procs_minus").collidepoint(pos):
                self._set_active(None)
                if self.process_count > 1:
                    self.process_count -= 1
                    self._sync_ports_for_procs()
                return
            elif self._rect("procs_plus").collidepoint(pos):
                self._set_active(None)
                if self.process_count < self.max_allowed_procs:
                    self.process_count += 1
                    self._sync_ports_for_procs()
                return
            elif self._rect("proc_preset_1").collidepoint(pos):
                self._set_active(None)
                self.process_count = 1
                self._sync_ports_for_procs()
                return
            elif self._rect("proc_preset_2").collidepoint(pos):
                self._set_active(None)
                self.process_count = min(2, self.max_allowed_procs)
                self._sync_ports_for_procs()
                return
            elif self._rect("proc_preset_4").collidepoint(pos):
                self._set_active(None)
                self.process_count = min(4, self.max_allowed_procs)
                self._sync_ports_for_procs()
                return
            elif self._rect("proc_preset_max").collidepoint(pos):
                self._set_active(None)
                self.process_count = self.max_allowed_procs
                self._sync_ports_for_procs()
                return
            elif self._rect("ports_auto").collidepoint(pos):
                self._set_active(None)
                self._regenerate_auto_ports()
                return
            else:
                self._set_active(None)

        elif self.step == 6:
            if self._rect("dev_minus").collidepoint(pos):
                self.device_index = max(0, self.device_index - 1)
            elif self._rect("dev_plus").collidepoint(pos):
                self.device_index = min(15, self.device_index + 1)
            elif self._rect("sdr_gain").collidepoint(pos):
                self._set_active("sdr_gain")
                self.cursor_pos = self._calc_cursor_pos("sdr_gain", pos[0])
            elif self._rect("sdr_ppm").collidepoint(pos):
                self._set_active("sdr_ppm")
                self.cursor_pos = self._calc_cursor_pos("sdr_ppm", pos[0])
            elif self._rect("sdr_mode_auto").collidepoint(pos):
                self.sdr_mode = "auto"
                self._set_active(None)
            elif self._rect("sdr_mode_pipe").collidepoint(pos):
                self.sdr_mode = "pipe"
                self._set_active(None)
            elif self._rect("sdr_mode_tcp").collidepoint(pos):
                self.sdr_mode = "tcp"
                self._set_active(None)
            elif self._rect("sdr_mode_rtlsdr").collidepoint(pos):
                self.sdr_mode = "rtlsdr"
                self._set_active(None)
            elif self._rect("sdr_mode_stdin").collidepoint(pos):
                self.sdr_mode = "stdin"
                self._set_active(None)
            elif getattr(self, "sdr_mode", "auto") == "tcp" and self._rect("rtl_tcp_host").collidepoint(pos):
                self._set_active("rtl_tcp_host")
                self.cursor_pos = self._calc_cursor_pos("rtl_tcp_host", pos[0])
            elif getattr(self, "sdr_mode", "auto") == "tcp" and self._rect("rtl_tcp_port").collidepoint(pos):
                self._set_active("rtl_tcp_port")
                self.cursor_pos = self._calc_cursor_pos("rtl_tcp_port", pos[0])
            elif self._rect("sdr_path").collidepoint(pos):
                self._set_active("sdr_path")
                self.cursor_pos = self._calc_cursor_pos("sdr_path", pos[0])
            else:
                self._set_active(None)

        elif self.step == 7:
            if self._rect("boot_cb").collidepoint(pos):
                self.autostart_boot = not self.autostart_boot
            elif self._rect("firewall_copy").collidepoint(pos):
                fw = config_loader.generate_windows_firewall_commands(self.existing)
                copied = copy_to_clipboard(fw["ps_oneliner"])
                self.firewall_toast = "Copied to clipboard!" if copied else "Copy failed (script saved)"
            elif self._rect("firewall_apply").collidepoint(pos):
                config_loader.launch_firewall_script()
                self.firewall_toast = "Launched enable_firewall.bat!"

    def _sync_ports_for_procs(self):
        """Adjusts the ports list when process_count changes, deriving ports from channels."""
        _, clean_chans, _ = config_loader.parse_channels_with_locks(self.fields.get("grabber_chans", ""))
        suggested = config_loader.generate_default_ports(clean_chans, self.process_count)
        if getattr(self, "custom_ports_edited", False):
            raw_ports = [p.strip() for p in self.fields.get("grabber_ports", "").split(",") if p.strip().isdigit()]
            valid_ports = [int(p) for p in raw_ports if 1024 <= int(p) <= 65535]
            if len(valid_ports) == self.process_count:
                return
            elif len(valid_ports) < self.process_count:
                for s in suggested:
                    if s not in valid_ports:
                        valid_ports.append(s)
                    if len(valid_ports) == self.process_count:
                        break
            else:
                valid_ports = valid_ports[:self.process_count]
            self.fields["grabber_ports"] = ", ".join(str(p) for p in valid_ports)
        else:
            self.fields["grabber_ports"] = ", ".join(str(p) for p in suggested)

    def _regenerate_auto_ports(self):
        """Forces regeneration of default ports for the current process_count and channels."""
        self.custom_ports_edited = False
        _, clean_chans, _ = config_loader.parse_channels_with_locks(self.fields.get("grabber_chans", ""))
        ports = config_loader.generate_default_ports(clean_chans, self.process_count)
        self.fields["grabber_ports"] = ", ".join(str(p) for p in ports)

    def _trigger_ssh_banc_deploy(self):
        """Packages Banc + whitelist and deploys to the cloud Linux VM via SCP/SSH."""
        host = self.fields["ssh_host"].strip().strip("[]")
        user = self.fields["ssh_user"].strip()
        if not host or not user:
            self.status_msg = "Enter both the VM Host/IP and SSH Username."
            return
        ssh_port = int(self.fields["ssh_port"]) if self.fields["ssh_port"].strip().isdigit() else 22
        banc_port = int(self.fields["ssh_banc_port"]) if self.fields["ssh_banc_port"].strip().isdigit() else 23009
        ssh_key = self.fields["ssh_key"].strip()

        if len(self.fields["auth_secret"]) < config_loader.MIN_AUTH_SECRET_LEN:
            self.fields["auth_secret"] = self.ex_auth or config_loader.generate_auth_secret()
        if not config_loader.is_valid_encryption_key(self.fields["enc_key"]):
            self.fields["enc_key"] = self.ex_key or config_loader.generate_encryption_key()
            self.key_mode = "generate"

        self.ssh_deploying = True
        self.ssh_deploy_ok = False
        self.ssh_deploy_attempted = True
        self.ssh_deploy_error = None
        self.status_msg = ""
        self.ssh_deploy_log = [f"Starting Cloud Banc deployment to {user}@{host}:{banc_port}..."]

        def _worker():
            deployed_idents = self.local_identities if self.local_identities else collect_local_identities(host, [self.node_identity])
            ok, msg = deploy_banc_via_ssh(
                host=host,
                ssh_user=user,
                ssh_port=ssh_port,
                ssh_key=ssh_key,
                banc_port=banc_port,
                auth_secret=self.fields["auth_secret"],
                encryption_key=self.fields["enc_key"],
                identities=deployed_idents,
                progress_cb=lambda m: self.ssh_deploy_log.append(m),
            )
            self.ssh_deploy_log.append(msg)
            self.ssh_deploy_ok = ok
            self.ssh_deploying = False
            self.fields["banc_host"] = config_loader.format_endpoint(host, banc_port)
            self.banc_ip_version = config_loader.detect_ip_version(host)
            if ok:
                self.ssh_deploy_error = None
                self.status_msg = ""
            else:
                self.ssh_deploy_error = msg
                self.status_msg = f"Deployment error: {msg[:100]}"

        threading.Thread(target=_worker, daemon=True).start()

    def _trigger_dependency_install(self):
        """Spawns background installation thread."""
        self.installing = True
        self.install_log.append("Starting automated package installation via pip...")

        def _worker():
            total = len(self.missing_deps)
            for idx, (pkg, _, _) in enumerate(self.missing_deps):
                self.install_log.append(f"Downloading & installing {pkg}...")
                install_package_stream(pkg, output_callback=lambda l: self.install_log.append(l[-80:]))
                self.install_progress = (idx + 1) / float(total)
            self.missing_deps = check_missing_dependencies()
            self.installing = False
            self.install_log.append("Dependency installation completed.")

        th = threading.Thread(target=_worker, daemon=True)
        th.start()

    # ----- drawing helpers -------------------------------------------------------
    def _draw_field(self, name: str, masked: bool = False, placeholder: str = ""):
        import pygame
        r = self._rect(name)
        editable = self._field_editable(name)
        active = (self.active_field == name)
        pygame.draw.rect(self.screen, (24, 32, 45) if editable else (18, 24, 33), r, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_ACCENT if active else COLOR_BORDER, r, 2 if active else 1, border_radius=4)

        text = self.fields[name]
        shown = ("*" * len(text)) if (masked and not self.show_secret) else text
        max_w = r.w - 20

        if not shown and not active and placeholder:
            lbl = self.font_body.render(placeholder, True, COLOR_TEXT_DIM)
            self.screen.blit(lbl, (r.x + 10, r.y + 9))
            return

        scroll = self.field_scroll.get(name, 0)
        scroll = max(0, min(len(shown), scroll))

        if active:
            pos = max(0, min(len(shown), self.cursor_pos))
            if pos < scroll:
                scroll = pos
            while scroll < pos and self.font_body.size(shown[scroll:pos])[0] > max_w - 15:
                scroll += 1
            while scroll > 0 and self.font_body.size(shown[scroll:])[0] < max_w - 25:
                scroll = max(0, scroll - 1)
        else:
            if self.font_body.size(shown)[0] <= max_w:
                scroll = 0

        self.field_scroll[name] = scroll

        visible_slice = shown[scroll:]
        while visible_slice and self.font_body.size(visible_slice)[0] > max_w:
            visible_slice = visible_slice[:-1]

        if active and getattr(self, "select_all_active", False) and visible_slice:
            sel_w = min(max_w, self.font_body.size(visible_slice)[0] + 4)
            sel_r = pygame.Rect(r.x + 8, r.y + 6, sel_w, r.h - 12)
            pygame.draw.rect(self.screen, (35, 75, 125), sel_r, border_radius=2)

        lbl = self.font_body.render(visible_slice, True, COLOR_TEXT if editable else COLOR_TEXT_DIM)
        self.screen.blit(lbl, (r.x + 10, r.y + 9))

        # Render vertical blinking cursor bar
        if active and (int(time.time() * 2) % 2 == 0):
            pos = max(0, min(len(shown), self.cursor_pos))
            cursor_in_slice = pos - scroll
            if 0 <= cursor_in_slice <= len(visible_slice):
                cx = r.x + 10 + self.font_body.size(visible_slice[:cursor_in_slice])[0]
                pygame.draw.line(self.screen, COLOR_ACCENT, (cx, r.y + 7), (cx, r.y + r.h - 8), 2)

    def _draw_button(self, name: str, text: str, color=COLOR_BORDER, text_color=COLOR_TEXT):
        import pygame
        r = self._rect(name)
        pygame.draw.rect(self.screen, color, r, border_radius=4)
        lbl = self.font_sm.render(text, True, text_color)
        self.screen.blit(lbl, lbl.get_rect(center=r.center))

    def _draw_radio(self, name: str, selected: bool, text: str):
        import pygame
        r = self._rect(name)
        pygame.draw.circle(self.screen, COLOR_ACCENT if selected else COLOR_BORDER, (r.x + 12, r.centery), 7, 0 if selected else 2)
        lbl = self.font_body.render(text, True, COLOR_TEXT)
        self.screen.blit(lbl, (r.x + 32, r.y + 6))

    def _draw_wrapped_text(
        self,
        text: str,
        x: int,
        y: int,
        max_width: int,
        color,
        font=None,
        line_spacing: int = 18,
        max_lines: int = 4,
    ) -> int:
        """Renders multiline or word-wrapped text within max_width, returning the ending y position."""
        if font is None:
            font = self.font_sm
        lines = []
        for raw_line in str(text).splitlines():
            words = raw_line.split(" ")
            current_line = ""
            for word in words:
                test_line = f"{current_line} {word}".strip() if current_line else word
                if font.size(test_line)[0] <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line)
                    while font.size(word)[0] > max_width and len(word) > 10:
                        lines.append(word[: max_width // 8])
                        word = word[max_width // 8 :]
                    current_line = word
            if current_line:
                lines.append(current_line)

        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            if not lines[-1].endswith("..."):
                lines[-1] = lines[-1][: max(1, len(lines[-1]) - 3)] + "..."

        curr_y = y
        for l in lines:
            rendered = font.render(l, True, color)
            self.screen.blit(rendered, (x, curr_y))
            curr_y += line_spacing
        return curr_y

    def _draw(self):
        import pygame
        self.screen.fill(COLOR_BG)

        # Header Bar
        pygame.draw.rect(self.screen, COLOR_CARD, (0, 0, self.width, 60))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, 60), (self.width, 60), 1)

        title = self.font_title.render("ApexBOS Setup & Deployment Wizard", True, COLOR_TEXT)
        ver_lbl = self.font_sm.render(f"Version: {VERSION}", True, COLOR_ACCENT)
        self.screen.blit(title, (25, 12))
        self.screen.blit(ver_lbl, (self.width - 150, 20))

        # Subheader Step Counter
        pygame.draw.rect(self.screen, (22, 28, 40), (0, 61, self.width, 32))
        if self.deployment_mode == "engines_viewers":
            ev_titles = {
                1: "Step 1 of 4: System Dependencies & Python Packages",
                2: "Step 2 of 4: Deployment Topology Selection",
                3: "Step 3 of 4: Packet Source & Network Connection",
                7: "Step 4 of 4: Boot Automation & Windows Firewall",
            }
            lbl_text = ev_titles.get(self.step, f"Step {self.step} of 4")
        else:
            lbl_text = STEP_TITLES[self.step]
        lbl_step = self.font_h2.render(lbl_text, True, COLOR_ACCENT)
        self.screen.blit(lbl_step, (25, 68))

        getattr(self, f"_draw_step{self.step}")()

        # Status line (suppressed during ssh_deploy to prevent collision with detailed error & instructions)
        if self.status_msg and self.remote_substep != "ssh_deploy":
            msg = self.font_sm.render(self.status_msg[:120], True, COLOR_RED)
            self.screen.blit(msg, (35, self.height - 92))

        # Bottom Bar & Navigation Buttons
        pygame.draw.rect(self.screen, COLOR_CARD, (0, self.height - 70, self.width, 70))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, self.height - 70), (self.width, self.height - 70), 1)

        btn_next = self._rect("next")
        next_txt = "Finish" if (self.step == self.total_steps or (self.deployment_mode == "engines_viewers" and self.step == 7)) else "Next Step >"
        pygame.draw.rect(self.screen, COLOR_ACCENT, btn_next, border_radius=4)
        lbl_next = self.font_h2.render(next_txt, True, (10, 10, 10))
        self.screen.blit(lbl_next, lbl_next.get_rect(center=btn_next.center))

        if self.step > 1:
            btn_back = self._rect("back")
            pygame.draw.rect(self.screen, COLOR_BORDER, btn_back, border_radius=4)
            lbl_back = self.font_body.render("< Back", True, COLOR_TEXT)
            self.screen.blit(lbl_back, lbl_back.get_rect(center=btn_back.center))

    def _draw_step1(self):
        import pygame
        txt = self.font_body.render("Checking required runtime packages for your platform:", True, COLOR_TEXT)
        self.screen.blit(txt, (35, 110))

        y = 145
        for pkg_name, import_name, desc in DEPENDENCIES:
            card_r = pygame.Rect(35, y, 790, 48)
            pygame.draw.rect(self.screen, COLOR_CARD, card_r, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, card_r, 1, border_radius=4)

            p_lbl = self.font_h2.render(pkg_name, True, COLOR_TEXT)
            d_lbl = self.font_sm.render(desc, True, COLOR_TEXT_DIM)
            self.screen.blit(p_lbl, (50, y + 8))
            self.screen.blit(d_lbl, (50, y + 27))

            is_missing = any(pkg_name == m[0] for m in self.missing_deps)
            status_txt = "MISSING" if is_missing else "INSTALLED"
            color = COLOR_RED if is_missing else COLOR_GREEN
            st_lbl = self.font_h2.render(status_txt, True, color)
            self.screen.blit(st_lbl, (card_r.right - 120, y + 15))
            y += 56

        if self.missing_deps:
            btn_r = self._rect("install")
            pygame.draw.rect(self.screen, COLOR_GREEN if not self.installing else COLOR_BORDER, btn_r, border_radius=4)
            btn_lbl = self.font_h2.render("Install Missing Packages" if not self.installing else "Installing...", True, (10, 10, 10))
            self.screen.blit(btn_lbl, btn_lbl.get_rect(center=btn_r.center))

        if self.install_log:
            last_line = self.install_log[-1]
            log_lbl = self.font_sm.render(last_line[:85], True, COLOR_ACCENT)
            self.screen.blit(log_lbl, (35, 525))

    def _draw_step2(self):
        import pygame
        txt = self.font_body.render("Choose how you want to deploy and network the ApexBOS modules:", True, COLOR_TEXT)
        self.screen.blit(txt, (35, 105))

        r1 = self._rect("card_local")
        sel1 = (self.deployment_mode == "local")
        pygame.draw.rect(self.screen, (28, 36, 50) if sel1 else COLOR_CARD, r1, border_radius=6)
        pygame.draw.rect(self.screen, COLOR_ACCENT if sel1 else COLOR_BORDER, r1, 2 if sel1 else 1, border_radius=6)

        c1 = self.font_h2.render("Option 1: Local Network Only (No Banc)", True, COLOR_ACCENT if sel1 else COLOR_TEXT)
        d1 = self.font_body.render("Pure local IPv4 operation with local RTL-SDR radio, Grabber, Flux and Engine over UDP multicast.", True, COLOR_TEXT_DIM)
        d2 = self.font_sm.render("Banc cloud syncing, remote layouts, and remote Foamer Chat are disabled.", True, COLOR_TEXT_DIM)
        self.screen.blit(c1, (55, 138))
        self.screen.blit(d1, (55, 166))
        self.screen.blit(d2, (55, 188))

        r2 = self._rect("card_cloud")
        sel2 = (self.deployment_mode == "cloud")
        pygame.draw.rect(self.screen, (28, 36, 50) if sel2 else COLOR_CARD, r2, border_radius=6)
        pygame.draw.rect(self.screen, COLOR_ACCENT if sel2 else COLOR_BORDER, r2, 2 if sel2 else 1, border_radius=6)

        c2 = self.font_h2.render("Option 2: With Cloud Support (ApexBOSBanc Present) [Recommended for Full Nodes]", True, COLOR_ACCENT if sel2 else COLOR_TEXT)
        d3 = self.font_body.render("Runs local radio Grabber & Flux, and connects to Banc for layout synchronization and multi-user chat.", True, COLOR_TEXT_DIM)
        d4 = self.font_sm.render("Supports running Banc locally on IPv4 or in the cloud with automatic IPv4/IPv6 detection.", True, COLOR_TEXT_DIM)
        self.screen.blit(c2, (55, 242))
        self.screen.blit(d3, (55, 270))
        self.screen.blit(d4, (55, 292))

        r3 = self._rect("card_client")
        sel3 = (self.deployment_mode == "engines_viewers")
        pygame.draw.rect(self.screen, (28, 36, 50) if sel3 else COLOR_CARD, r3, border_radius=6)
        pygame.draw.rect(self.screen, COLOR_ACCENT if sel3 else COLOR_BORDER, r3, 2 if sel3 else 1, border_radius=6)

        c3 = self.font_h2.render("Option 3: Engines & Viewers Only (Display Workstation / Client)", True, COLOR_ACCENT if sel3 else COLOR_TEXT)
        d5 = self.font_body.render("Deploys Dispatcher Engines and Railroad Viewers without a local radio receiver or Grabber processes.", True, COLOR_TEXT_DIM)
        d6 = self.font_sm.render("Receives packets remotely from a Network Flux Relay (UDP multicast) or Banc Cloud Server (TCP).", True, COLOR_TEXT_DIM)
        self.screen.blit(c3, (55, 346))
        self.screen.blit(d5, (55, 374))
        self.screen.blit(d6, (55, 396))

    def _draw_step3_engines_viewers(self):
        import pygame
        txt = self.font_body.render("Select the source of packets for Dispatcher Engines and Railroad Viewers:", True, COLOR_TEXT)
        self.screen.blit(txt, (35, 112))

        self._draw_radio("src_flux", self.packet_source == "flux", "1) Network Flux Relay (UDP Multicast)")
        self._draw_radio("src_banc", self.packet_source == "banc", "2) ApexBOSBanc Cloud Server (TCP)")

        if self.packet_source == "flux":
            self.screen.blit(self.font_h2.render("Flux Multicast Group:", True, COLOR_TEXT), (50, 218))
            self.screen.blit(self.font_h2.render("Flux Multicast Port:", True, COLOR_TEXT), (430, 218))
            self._draw_field("flux_mcast_group", placeholder="e.g. 239.1.1.1")
            self._draw_field("flux_mcast_port", placeholder="e.g. 23003")

            hint = self.font_sm.render("Local Engines subscribe to this multicast stream to receive raw packet telemetry from your network Flux.", True, COLOR_TEXT_DIM)
            self.screen.blit(hint, (50, 285))

            card = pygame.Rect(50, 320, 760, 150)
            pygame.draw.rect(self.screen, COLOR_CARD, card, border_radius=6)
            pygame.draw.rect(self.screen, COLOR_BORDER, card, 1, border_radius=6)

            self.screen.blit(self.font_h2.render("Network Multicast Reception Architecture:", True, COLOR_ACCENT), (65, 332))
            self.screen.blit(self.font_sm.render("• No local RTL-SDR radio hardware or Grabber processes are executed on this workstation.", True, COLOR_TEXT), (65, 360))
            self.screen.blit(self.font_sm.render("• Local Engines join the configured multicast group to ingest live packet bursts from the LAN.", True, COLOR_TEXT), (65, 382))
            self.screen.blit(self.font_sm.render("• Railroad Viewers connect to local Engines over dedicated local ports (5007..5037, 5008..5038).", True, COLOR_TEXT_DIM), (65, 404))
            self.screen.blit(self.font_sm.render("• Windows Firewall multicast routing rules will be generated automatically in Step 4.", True, COLOR_GREEN), (65, 426))

        else: # banc
            self.screen.blit(self.font_h2.render("ApexBOSBanc Host / IP and Port:", True, COLOR_TEXT), (50, 208))
            self._draw_field("banc_host", placeholder="e.g. 192.168.1.10:23009 or [2600:1f18::1]:23009")

            badge_color = COLOR_GREEN if self.banc_ip_version == "ipv6" else COLOR_ACCENT
            b_lbl = self.font_h2.render(f"Detected: {self.banc_ip_version.upper()} Network", True, badge_color)
            self.screen.blit(b_lbl, (550, 238))

            self.screen.blit(self.font_h2.render("Authentication Secret (auth_secret):", True, COLOR_TEXT), (50, 276))
            self._draw_field("auth_secret", masked=True, placeholder="Banc authentication secret (min 8 characters)")
            self._draw_button("auth_show", "Hide" if self.show_secret else "Show")
            self._draw_button("auth_gen", "Generate random", COLOR_BTN_HOVER)

            self.screen.blit(self.font_h2.render("Fernet Encryption Key (encryption_key):", True, COLOR_TEXT), (50, 344))
            self._draw_field("enc_key", placeholder="Paste 44-character Fernet key from Banc (or click Paste)")
            self._draw_button("key_paste", "📋 Paste", COLOR_BTN_HOVER)
            self._draw_button("key_regen", "New Key", COLOR_BTN_HOVER)

            card = pygame.Rect(50, 415, 760, 95)
            pygame.draw.rect(self.screen, COLOR_CARD, card, border_radius=6)
            pygame.draw.rect(self.screen, COLOR_BORDER, card, 1, border_radius=6)
            self.screen.blit(self.font_h2.render("Banc Whitelist Configuration:", True, COLOR_ACCENT), (65, 425))
            self.screen.blit(self.font_sm.render("Ensure this client workstation identity is listed in whitelist.txt on the Banc server:", True, COLOR_TEXT), (65, 448))
            id_txt = ", ".join(self.local_identities[:4]) if self.local_identities else self.node_identity
            self.screen.blit(self.font_body.render(f"Identity: {id_txt}", True, COLOR_GREEN), (65, 472))

    def _draw_step3(self):
        import pygame
        if self.deployment_mode == "engines_viewers":
            self._draw_step3_engines_viewers()
            return

        if self.deployment_mode == "local":
            txt = self.font_body.render("Local Network Only mode is selected. Banc is disabled.", True, COLOR_TEXT)
            self.screen.blit(txt, (35, 120))
            return

        if self.remote_substep == "vm_prereq":
            h1 = self.font_h2.render("Setup Cloud Banc from Scratch - Cloud Linux VM Prerequisites", True, COLOR_ACCENT)
            self.screen.blit(h1, (35, 115))
            box = pygame.Rect(35, 150, 790, 290)
            pygame.draw.rect(self.screen, COLOR_CARD, box, border_radius=6)
            pygame.draw.rect(self.screen, COLOR_BORDER, box, 1, border_radius=6)
            lines = [
                ("Before deploying ApexBOSBanc, ensure you have a cloud Linux VM running:", COLOR_TEXT),
                ("  1. Provision a Linux VM in AWS (EC2), Microsoft Azure, or Google Cloud (GCE).", COLOR_TEXT_DIM),
                ("  2. Ensure SSH access is enabled (port 22) and Python 3 is installed on the VM.", COLOR_TEXT_DIM),
                ("  3. Open inbound TCP ports in your cloud Security Group / Firewall:", COLOR_TEXT_DIM),
                ("       - SSH (default 22) for automated upload & startup", COLOR_TEXT_DIM),
                ("       - Banc Data Port (default 23009) and optional UI Port (23010)", COLOR_TEXT_DIM),
                ("  4. Have your SSH username (e.g. ubuntu, ec2-user, azureuser) and SSH key/agent ready.", COLOR_TEXT_DIM),
                ("", COLOR_TEXT),
                ("Once your Linux VM is running and reachable via SSH, click 'Next Step >' to deploy.", COLOR_GREEN),
            ]
            ly = 170
            for text_line, col in lines:
                self.screen.blit(self.font_body.render(text_line, True, col), (55, ly))
                ly += 28
            return

        if self.remote_substep == "ssh_deploy":
            h1 = self.font_h2.render("Deploy & Start Cloud Banc on Linux VM (via SCP + SSH)", True, COLOR_ACCENT)
            self.screen.blit(h1, (35, 105))

            self.screen.blit(self.font_sm.render("VM Host / IP:", True, COLOR_TEXT), (50, 127))
            self.screen.blit(self.font_sm.render("SSH Port:", True, COLOR_TEXT), (490, 127))
            self.screen.blit(self.font_sm.render("Banc Port:", True, COLOR_TEXT), (620, 127))
            self._draw_field("ssh_host", placeholder="e.g. 54.12.34.56 or [2600:1f18::1]")
            self._draw_field("ssh_port", placeholder="22")
            self._draw_field("ssh_banc_port", placeholder="23009")

            self.screen.blit(self.font_sm.render("SSH Username:", True, COLOR_TEXT), (50, 194))
            self.screen.blit(self.font_sm.render("SSH Key Path (optional .pem / id_rsa):", True, COLOR_TEXT), (360, 194))
            self._draw_field("ssh_user", placeholder="ubuntu / ec2-user / root")
            self._draw_field("ssh_key", placeholder="Leave blank for default SSH key / agent")

            btn_col = COLOR_GREEN if self.ssh_deploy_ok else (COLOR_BORDER if self.ssh_deploying else COLOR_ACCENT)
            btn_txt = "Deployed & Running" if self.ssh_deploy_ok else ("Deploying..." if self.ssh_deploying else "Deploy & Start Cloud Banc")
            self._draw_button("ssh_deploy_btn", btn_txt, btn_col, (10, 10, 10) if not self.ssh_deploying else COLOR_TEXT)

            if not self.ssh_deploy_ok and self.ssh_deploy_attempted and not self.ssh_deploying:
                self._draw_button("ssh_proceed_btn", "Proceed Anyway >", (200, 110, 40), (255, 255, 255))
            elif not self.ssh_deploy_ok and not self.ssh_deploying:
                info = self.font_sm.render("Packages ApexBOSBanc + whitelist.txt, SCPs to ~/, unzips & starts.", True, COLOR_TEXT_DIM)
                self.screen.blit(info, (335, 270))
            elif self.ssh_deploy_ok:
                info = self.font_sm.render("Deployment successful! Click 'Next Step >' to continue.", True, COLOR_GREEN)
                self.screen.blit(info, (335, 270))

            log_box = pygame.Rect(50, 305, 760, 130)
            pygame.draw.rect(self.screen, (10, 14, 20), log_box, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, log_box, 1, border_radius=4)
            ly = 313
            for line in self.ssh_deploy_log[-6:]:
                col = COLOR_GREEN if ("deployed & started" in line.lower() or "[+]" in line) else (
                    COLOR_RED if ("failed" in line.lower() or "error" in line.lower() or "[!]" in line) else COLOR_TEXT_DIM
                )
                self.screen.blit(self.font_sm.render(line[:100], True, col), (60, ly))
                ly += 19

            if not self.ssh_deploy_ok and self.ssh_deploy_attempted and not self.ssh_deploying:
                err_text = self.ssh_deploy_error or "Deployment failed."
                ey = self._draw_wrapped_text(f"Error: {err_text}", 50, 444, 760, COLOR_RED, line_spacing=17, max_lines=3)
                instruction = "Deployment encountered an error. Click 'Proceed Anyway >' or 'Next Step >' to finish setup."
                self._draw_wrapped_text(instruction, 50, ey + 4, 760, (255, 190, 80), line_spacing=17, max_lines=2)
            return

        txt = self.font_body.render("Select where ApexBOSBanc is hosted and how you want to configure it:", True, COLOR_TEXT)
        self.screen.blit(txt, (35, 112))

        self._draw_radio("loc_local", self.banc_location == "local", "Local Banc (Runs locally on IPv4: 127.0.0.1:23009)")
        self._draw_radio("loc_remote", self.banc_location == "remote", "Remote Cloud Banc (Hosted on a remote cloud server)")

        if self.banc_location == "remote":
            self._draw_radio("remote_existing", self.remote_banc_mode == "existing", "1) Already have Banc installed (enter host, port, secret & key)")
            self._draw_radio("remote_scratch", self.remote_banc_mode == "scratch", "2) Setup Cloud Banc from scratch (deploy via SSH to AWS/Azure/GCP VM)")

            if self.remote_banc_mode == "existing":
                p_lbl = self.font_h2.render("Remote Cloud Host / IP and Port:", True, COLOR_TEXT)
                self.screen.blit(p_lbl, (75, 302))
                self._draw_field("banc_host")

                badge_color = COLOR_GREEN if self.banc_ip_version == "ipv6" else COLOR_ACCENT
                b_lbl = self.font_h2.render(f"Detected: {self.banc_ip_version.upper()} Network", True, badge_color)
                self.screen.blit(b_lbl, (595, 338))

                hint = self.font_sm.render("Accepts IPv4 (e.g. 192.168.1.10:23009), IPv6 (e.g. [2600:1f18::1]:23009), or hostnames.", True, COLOR_TEXT_DIM)
                self.screen.blit(hint, (75, 378))
            else:
                hint = self.font_sm.render("Click 'Next Step >' to review Linux VM requirements and deploy ApexBOSBanc via SCP/SSH.", True, COLOR_ACCENT)
                self.screen.blit(hint, (75, 310))

    def _draw_step4(self):
        if self.deployment_mode == "local":
            t1 = self.font_body.render("Local Network Only mode: Banc is not used.", True, COLOR_TEXT)
            t2 = self.font_sm.render("A unique authentication secret and encryption key are generated automatically and stored in config.json.", True, COLOR_TEXT_DIM)
            self.screen.blit(t1, (35, 120))
            self.screen.blit(t2, (35, 145))
            return

        l1 = self.font_h2.render("Authentication secret (auth_secret) - shared by every node using this Banc:", True, COLOR_TEXT)
        self.screen.blit(l1, (50, 112))
        self._draw_field("auth_secret", masked=True, placeholder="Type a secret (min 8 characters) or press Generate")
        self._draw_button("auth_show", "Hide" if self.show_secret else "Show")
        self._draw_button("auth_gen", "Generate random", COLOR_BTN_HOVER)

        l2 = self.font_h2.render("Encryption key (encryption_key):", True, COLOR_TEXT)
        self.screen.blit(l2, (50, 205))
        self._draw_radio("key_gen", self.key_mode == "generate", "Generate a NEW key (setting up a new Banc deployment)")
        self._draw_radio("key_exist", self.key_mode == "existing", "Use an EXISTING key (joining an existing Banc / keep current)")
        self._draw_field("enc_key", placeholder="Paste the existing 44-character key (Ctrl+V)")
        self._draw_button("key_paste", "📋 Paste", COLOR_BTN_HOVER)
        if self.key_mode == "generate":
            self._draw_button("key_regen", "New key", COLOR_BTN_HOVER)

        n1 = self.font_sm.render("The secret and key are written only to your local config.json - they are never part of the distribution ZIP.", True, COLOR_TEXT_DIM)
        n2 = self.font_sm.render("Copy the same secret + key to every Flux / Engine / Viewer / Creator node that connects to this Banc.", True, COLOR_TEXT_DIM)
        self.screen.blit(n1, (50, 365))
        self.screen.blit(n2, (50, 385))

        if self.banc_location == "remote" and self.remote_banc_mode == "existing":
            import pygame
            wbox = pygame.Rect(50, 415, 765, 110)
            pygame.draw.rect(self.screen, COLOR_CARD, wbox, border_radius=5)
            pygame.draw.rect(self.screen, COLOR_ACCENT, wbox, 1, border_radius=5)
            self.screen.blit(self.font_h2.render("Reminder: Whitelist Applications on Remote Banc", True, COLOR_ACCENT), (65, 425))
            self.screen.blit(self.font_sm.render("Add your local node identities to whitelist.txt on the remote Banc for Flux, Engine, Viewer & Creator:", True, COLOR_TEXT), (65, 450))
            id_txt = ", ".join(self.local_identities[:4]) if self.local_identities else "(computing local identities...)"
            self.screen.blit(self.font_body.render(f"Identities: {id_txt}", True, COLOR_GREEN), (65, 475))

    def _draw_step5(self):
        import pygame
        # 1. Channels
        lbl_ch = self.font_h2.render("Radio Channels (--chans):", True, COLOR_TEXT)
        self.screen.blit(lbl_ch, (50, 108))
        tip_ch = self.font_sm.render(
            "Comma-separated numbers or ranges. Append '.' or 'l' to lock a channel (e.g. 101, 102., 107l).",
            True, COLOR_TEXT_DIM
        )
        self.screen.blit(tip_ch, (50, 131))
        self._draw_field("grabber_chans", placeholder="e.g. 101, 102., 107l, 111-120")

        # Live channel stats
        chans_input = self.fields.get("grabber_chans", "").strip()
        if chans_input:
            raw_ch, clean_chans, locked_chans = config_loader.parse_channels_with_locks(chans_input)
            locked_text = f"Locked ({len(locked_chans)}): {', '.join(str(c) for c in sorted(list(locked_chans)))}" if locked_chans else "No locked channels"
            stats_txt = self.font_sm.render(
                f"Total Channels: {len(clean_chans)}  |  {locked_text}",
                True, COLOR_ACCENT
            )
        else:
            stats_txt = self.font_sm.render("No channels specified. Enter channel numbers.", True, COLOR_ERR)
        self.screen.blit(stats_txt, (50, 194))

        # 2. Worker Processes
        cpu_str = f"CPU threads: {self.cpu_threads or 'unknown'}"
        lbl_pr = self.font_h2.render(
            f"Grabber Worker Processes:  ({cpu_str}, max allowed: {self.max_allowed_procs})",
            True, COLOR_TEXT
        )
        self.screen.blit(lbl_pr, (50, 225))

        self._draw_button("procs_minus", "-", COLOR_BTN_HOVER)
        pygame.draw.rect(self.screen, (24, 32, 45), self._rect("procs_val"), border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, self._rect("procs_val"), 1, border_radius=4)
        pv = self.font_h2.render(str(self.process_count), True, COLOR_ACCENT)
        self.screen.blit(pv, pv.get_rect(center=self._rect("procs_val").center))
        self._draw_button("procs_plus", "+", COLOR_BTN_HOVER)

        # Presets
        presets = [
            ("proc_preset_1", 1, "1 Proc"),
            ("proc_preset_2", 2, "2 Procs"),
            ("proc_preset_4", 4, "4 Procs"),
            ("proc_preset_max", self.max_allowed_procs, f"Max ({self.max_allowed_procs})"),
        ]
        for r_name, p_val, label in presets:
            sel = (self.process_count == p_val)
            self._draw_button(r_name, label, COLOR_ACCENT if sel else COLOR_CARD, (10, 10, 10) if sel else COLOR_TEXT)

        # 3. Ports
        lbl_po = self.font_h2.render(
            f"Network Ports per Process (--procs; {self.process_count} required):",
            True, COLOR_TEXT
        )
        self.screen.blit(lbl_po, (50, 298))
        self._draw_field("grabber_ports", placeholder="e.g. 18071, 18072")
        self._draw_button("ports_auto", "Auto Ports", COLOR_BTN_HOVER)

        # 4. Architecture & Runtime balancing info card
        card = pygame.Rect(50, 375, 760, 180)
        pygame.draw.rect(self.screen, COLOR_CARD, card, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, card, 1, border_radius=4)

        t_card = self.font_h2.render("Architecture & Runtime Channel Balancing:", True, COLOR_ACCENT)
        self.screen.blit(t_card, (65, 387))

        note1 = self.font_sm.render(
            "• Grabber automatically re-arranges channels across worker processes at runtime based on spectrum.",
            True, COLOR_TEXT
        )
        self.screen.blit(note1, (65, 410))
        note2 = self.font_sm.render(
            "• Locked channels (marked with '.' or 'l') will remain fixed to their designated worker process.",
            True, COLOR_TEXT
        )
        self.screen.blit(note2, (65, 428))
        note3 = self.font_sm.render(
            "• Each process broadcasts on its assigned network port for Flux and Engine to receive (18000 + channel).",
            True, COLOR_TEXT_DIM
        )
        self.screen.blit(note3, (65, 446))

        # Show ports preview
        ports_raw = [p.strip() for p in self.fields.get("grabber_ports", "").split(",") if p.strip()]
        y_proc = 472
        if len(ports_raw) <= 4:
            items = list(enumerate(ports_raw))
        else:
            items = list(enumerate(ports_raw[:3])) + [(-1, "..."), (len(ports_raw) - 1, ports_raw[-1])]

        for idx, p in items:
            if idx == -1:
                p_text = "... additional worker processes ..."
                color = COLOR_TEXT_DIM
            else:
                p_text = f"Worker #{idx}: ZMQ PUB tcp://127.0.0.1:{p}  (Flux source endpoint)"
                color = COLOR_TEXT
            r_txt = self.font_sm.render(p_text, True, color)
            self.screen.blit(r_txt, (75, y_proc))
            y_proc += 18

    def _draw_step6(self):
        import pygame
        if self.sdr_env is None:
            self.sdr_env = config_loader.detect_sdr_environment(self.sdr.get("rtlsdr_dll_dir"))
        env = self.sdr_env

        l1 = self.font_h2.render("Device Index:", True, COLOR_TEXT)
        self.screen.blit(l1, (50, 112))
        self._draw_button("dev_minus", "-", COLOR_BTN_HOVER)
        pygame.draw.rect(self.screen, (24, 32, 45), self._rect("dev_val"), border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, self._rect("dev_val"), 1, border_radius=4)
        dv = self.font_h2.render(str(self.device_index), True, COLOR_ACCENT)
        self.screen.blit(dv, dv.get_rect(center=self._rect("dev_val").center))
        self._draw_button("dev_plus", "+", COLOR_BTN_HOVER)

        l_gain = self.font_h2.render("Tuner Gain (dB or 'auto'):", True, COLOR_TEXT)
        self.screen.blit(l_gain, (260, 112))
        self._draw_field("sdr_gain", placeholder="e.g. auto, 42.0")

        l_ppm = self.font_h2.render("Freq Correction (PPM):", True, COLOR_TEXT)
        self.screen.blit(l_ppm, (530, 112))
        self._draw_field("sdr_ppm", placeholder="e.g. 0")

        # RTL-SDR Access Mode selector
        l_mode = self.font_h2.render("RTL-SDR Access Mode (--sdr-mode):", True, COLOR_TEXT)
        self.screen.blit(l_mode, (50, 182))
        modes = [
            ("sdr_mode_auto", "auto", "Auto (Detect)"),
            ("sdr_mode_pipe", "pipe", "Pipe (rtl_sdr)"),
            ("sdr_mode_tcp", "tcp", "TCP (rtl_tcp)"),
            ("sdr_mode_rtlsdr", "rtlsdr", "Direct (rtlsdr)"),
            ("sdr_mode_stdin", "stdin", "Stdin (Raw I/Q)"),
        ]
        cur_mode = getattr(self, "sdr_mode", "auto")
        for btn_key, mode_val, mode_label in modes:
            is_sel = (cur_mode == mode_val)
            bg = COLOR_ACCENT if is_sel else COLOR_CARD
            fg = (10, 10, 10) if is_sel else COLOR_TEXT
            self._draw_button(btn_key, mode_label, bg, fg)

        if cur_mode == "tcp":
            l_thost = self.font_body.render("RTL-TCP Host / IP:", True, COLOR_TEXT)
            self.screen.blit(l_thost, (50, 252))
            self._draw_field("rtl_tcp_host", placeholder="e.g. 127.0.0.1")
            l_tport = self.font_body.render("Port:", True, COLOR_TEXT)
            self.screen.blit(l_tport, (520, 252))
            self._draw_field("rtl_tcp_port", placeholder="1234")
            card_y = 288
            card_h = 76
            path_lbl_y = 370
        else:
            card_y = 248
            card_h = 92
            path_lbl_y = 348

        card = pygame.Rect(35, card_y, 790, card_h)
        pygame.draw.rect(self.screen, COLOR_CARD, card, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, card, 1, border_radius=4)
        rows = [
            ("rtl_sdr  (pipe mode)", env.get("rtl_sdr_on_path")),
            ("rtl_tcp  (TCP mode)", env.get("rtl_tcp_on_path")),
            ("pyrtlsdr (direct mode)", "OK" if env.get("pyrtlsdr_ok") else None),
        ]
        row_y = card_y + 8
        row_spacing = 22 if cur_mode == "tcp" else 26
        for name, found in rows:
            n_lbl = self.font_body.render(name, True, COLOR_TEXT)
            s_lbl = self.font_body.render(str(found)[:70] if found else "not found on PATH", True, COLOR_GREEN if found else COLOR_RED)
            self.screen.blit(n_lbl, (50, row_y))
            self.screen.blit(s_lbl, (260, row_y))
            row_y += row_spacing

        l2 = self.font_h2.render("Path to rtl_tcp / rtl_sdr executable, or folder with rtlsdr.dll + libusb DLLs:", True, COLOR_TEXT)
        self.screen.blit(l2, (50, path_lbl_y))
        self._draw_field("sdr_path", placeholder="(optional) path ...")

        y = path_lbl_y + 60
        if self.fields["sdr_path"].strip() and self.sdr.get("error"):
            e = self.font_sm.render(str(self.sdr["error"])[:110], True, COLOR_RED)
            self.screen.blit(e, (50, y))
            y += 20
        else:
            for k, label in (("rtl_tcp_bin", "rtl_tcp"), ("rtl_sdr_bin", "rtl_sdr"), ("rtlsdr_dll_dir", "DLL folder")):
                if self.sdr.get(k):
                    t = self.font_sm.render(f"{label}: {self.sdr[k]}"[:110], True, COLOR_GREEN)
                    self.screen.blit(t, (50, y))
                    y += 18
        gain_disp = self.fields.get("sdr_gain", "auto")
        ppm_disp = self.fields.get("sdr_ppm", "0")
        if cur_mode == "tcp":
            thost_disp = self.fields.get("rtl_tcp_host", "127.0.0.1").strip() or "127.0.0.1"
            tport_disp = self.fields.get("rtl_tcp_port", "1234").strip() or "1234"
            mode_desc = f"mode = tcp ({thost_disp}:{tport_disp})"
        else:
            mode_desc = f"mode = {cur_mode}"
        mode_lbl = self.font_h2.render(f"Grabber will use: {mode_desc}, dev = {self.device_index}, gain = {gain_disp}, ppm = {ppm_disp}", True, COLOR_ACCENT)
        self.screen.blit(mode_lbl, (50, max(y + 4, 465)))

    def _draw_step7(self):
        import pygame
        txt = self.font_body.render("Configure automatic startup services, firewall permissions, and shortcuts:", True, COLOR_TEXT)
        self.screen.blit(txt, (35, 105))

        # Boot Service Checkbox
        box_r = pygame.Rect(40, 138, 20, 20)
        pygame.draw.rect(self.screen, (24, 32, 45), box_r, border_radius=3)
        pygame.draw.rect(self.screen, COLOR_ACCENT if self.autostart_boot else COLOR_BORDER, box_r, 1, border_radius=3)
        if self.autostart_boot:
            pygame.draw.rect(self.screen, COLOR_ACCENT, (box_r.x + 3, box_r.y + 3, 14, 14), border_radius=2)

        lbl_boot_txt = "Automatically start ApexBOS Control Center on system boot" if self.deployment_mode == "engines_viewers" else "Automatically start Grabber and Flux on system boot"
        lbl = self.font_h2.render(lbl_boot_txt, True, COLOR_TEXT)
        self.screen.blit(lbl, (72, 138))

        os_name = "Windows Task Scheduler" if sys.platform == "win32" else ("macOS LaunchAgents" if sys.platform == "darwin" else "Linux systemd")
        sub = self.font_sm.render(f"Configures native daemon background services using {os_name}.", True, COLOR_TEXT_DIM)
        self.screen.blit(sub, (72, 162))

        # Windows Firewall & Multicast Card
        fw_card = pygame.Rect(40, 190, 780, 175)
        pygame.draw.rect(self.screen, COLOR_CARD, fw_card, border_radius=6)
        pygame.draw.rect(self.screen, COLOR_BORDER, fw_card, 1, border_radius=6)

        fw_title = self.font_h2.render("Windows 11 Firewall & Multicast Groups (Required on Windows 11)", True, COLOR_ACCENT)
        self.screen.blit(fw_title, (55, 202))
        fw_d1 = self.font_body.render("Windows Defender blocks Grabber TCP ports and Multicast UDP by default.", True, COLOR_TEXT)
        self.screen.blit(fw_d1, (55, 226))
        fw_d2 = self.font_sm.render("enable_firewall.bat and enable_firewall.ps1 were generated in the install folder.", True, COLOR_TEXT_DIM)
        self.screen.blit(fw_d2, (55, 246))

        self._draw_button("firewall_copy", "📋 Copy PowerShell Command", COLOR_BTN_HOVER)
        self._draw_button("firewall_apply", "⚡ Apply Rules (Admin)", COLOR_BTN_HOVER)

        if getattr(self, "firewall_toast", None):
            t_msg = self.font_sm.render(self.firewall_toast, True, COLOR_GREEN)
            self.screen.blit(t_msg, (545, 280))

        p_box = pygame.Rect(55, 318, 750, 36)
        pygame.draw.rect(self.screen, (15, 20, 28), p_box, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, p_box, 1, border_radius=4)
        p_txt = self.font_sm.render(
            "New-NetFirewallRule -DisplayName 'ApexBOS Inbound UDP' -LocalPort 23003,5007 ...; route add 224.0.0.0 ...",
            True, (170, 215, 255)
        )
        self.screen.blit(p_txt, (65, 328))

        # Desktop Shortcuts & Control Center
        card = pygame.Rect(40, 378, 780, 155)
        pygame.draw.rect(self.screen, COLOR_CARD, card, border_radius=6)
        pygame.draw.rect(self.screen, COLOR_BORDER, card, 1, border_radius=6)

        sc_title = self.font_h2.render("Desktop Shortcuts & Control Center", True, COLOR_ACCENT)
        sc_d1 = self.font_body.render("The wizard will generate one-click Desktop and Application shortcuts for:", True, COLOR_TEXT)
        sc_d2 = self.font_sm.render(" - ApexBOS Control Center (ApexBOSLauncher.py) - Central Management Dashboard", True, COLOR_TEXT_DIM)
        sc_d3 = self.font_sm.render(" - Start Local Monitoring / Start Remote Monitoring / ApexBOS Creator Studio", True, COLOR_TEXT_DIM)
        self.screen.blit(sc_title, (55, 390))
        self.screen.blit(sc_d1, (55, 414))
        self.screen.blit(sc_d2, (55, 434))
        self.screen.blit(sc_d3, (55, 454))

        u = self.font_sm.render("Update:    python update.py [--zip ApexBOS-vXX.zip]   (keeps your configuration)", True, COLOR_TEXT_DIM)
        v = self.font_sm.render("Uninstall: python uninstall.py   (or the 'Uninstall' button in the Control Center)", True, COLOR_TEXT_DIM)
        self.screen.blit(u, (55, 482))
        self.screen.blit(v, (55, 502))


def _arg_value(flag: str) -> Optional[str]:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    if "--cli" in sys.argv or os.environ.get("SDL_VIDEODRIVER") == "dummy":
        dev = _arg_value("--device")
        run_cli_wizard(
            auto_defaults=("--yes" in sys.argv),
            auth_secret=_arg_value("--auth-secret"),
            encryption_key=_arg_value("--encryption-key"),
            device_index=int(dev) if dev and dev.isdigit() else None,
            sdr_path=_arg_value("--sdr-path"),
        )
    else:
        try:
            wiz = PygameSetupWizard()
            wiz.run()
        except Exception as e:
            print(f"[!] Pygame UI failed ({e}); falling back to CLI setup wizard.", file=sys.stderr)
            run_cli_wizard()


if __name__ == "__main__":
    main()
