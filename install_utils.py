"""
install_utils.py - Shared helpers for ApexBOS update.py and uninstall.py (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Standard-library only, so the updater/uninstaller run even when third-party packages are missing.
"""

import os
import re
import sys
import json
import hashlib
import fnmatch
import subprocess
from typing import List, Tuple, Optional, Dict, Any

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

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_NAME = "MANIFEST.json"                 # inside the distribution zip
LOCAL_MANIFEST_NAME = ".apexbos_manifest.json"  # written into the install dir by update.py / setup
INSTALL_RECORD_NAME = "install_record.json"

# Scripts whose processes belong to an ApexBOS installation.
APEXBOS_SCRIPTS = (
    "ApexBOSGrabber.py", "ApexBOSLatch.py", "ApexBOSFred.py", "ApexBOSFlux.py",
    "ApexBOS.220.py", "View_ApexBOS.220.py", "ApexBOSCreator.py", "ApexBOSBanc.py",
    "ApexBOSChroma.py", "ApexBOSSpy.py", "PackFIG.py", "ApexBOSScanner.py",
)

# Files that belong to the user: never overwritten by an update, only created if missing.
USER_OWNED_PATTERNS = (
    "config.json", "config.json.bak.*", INSTALL_RECORD_NAME, LOCAL_MANIFEST_NAME,
    "grabber_config.json", "MosFET.json", "banc.json",
    "prefs_*.json", "user_display_prefs.json",
    "*.db", "*.itcvl", "frags/*", "backups/*",
)

# Shipped data files the user may have customised: replaced only if unmodified since the last install
# (3-way compare against the previous manifest); otherwise the new version is saved as '<name>.new'.
MANAGED_DATA_PATTERNS = (
    "config/*", "*rules.json", "*_rule.json", "signals_default.json", "ptc_status_parser.json", "thanks.txt",
)


def is_rule_file(rel_path: str) -> bool:
    """True if `rel_path` is a packet parser rule file (e.g. config/ctc_rules.json, config/*.rules.json)."""
    rp = rel_path.replace("\\", "/")
    base = rp.rsplit("/", 1)[-1].lower()
    return base.endswith("rules.json") or base.endswith("_rule.json") or base.endswith(".rule.json")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def matches_any(rel_path: str, patterns) -> bool:
    rp = rel_path.replace("\\", "/")
    base = rp.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rp, p) or ("/" not in p and fnmatch.fnmatch(base, p) and "/" not in rp) for p in patterns)


def is_user_owned(rel_path: str) -> bool:
    return matches_any(rel_path, USER_OWNED_PATTERNS)


def is_managed_data(rel_path: str) -> bool:
    return matches_any(rel_path, MANAGED_DATA_PATTERNS)


def parse_version(ver: Any) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(ver or "")))


def read_version_text(text: str) -> Optional[str]:
    m = re.search(r"""VERSION\s*=\s*["']([^"']+)["']""", text)
    return m.group(1) if m else None


def installed_version(install_dir: str = INSTALL_DIR) -> Optional[str]:
    p = os.path.join(install_dir, "version.py")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return read_version_text(f.read())
    except OSError:
        return None


def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_dev_checkout(install_dir: str = INSTALL_DIR) -> bool:
    """A git working copy (developer checkout) must never be purged by the uninstaller."""
    return os.path.exists(os.path.join(install_dir, ".git"))


def looks_like_install(install_dir: str = INSTALL_DIR) -> bool:
    return all(os.path.isfile(os.path.join(install_dir, n)) for n in ("ApexBOSLauncher.py", "version.py"))


# ---------------------------------------------------------------------------
# Running-process detection / termination (no third-party dependencies)
# ---------------------------------------------------------------------------

def _protected_pids() -> set:
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
    except Exception:
        pass
    return pids


def list_apexbos_processes(include_launcher: bool = False) -> List[Dict[str, Any]]:
    """Lists running ApexBOS application processes: [{'pid': int, 'name': str, 'cmd': str}, ...] (self/parent excluded)."""
    scripts = list(APEXBOS_SCRIPTS) + (["ApexBOSLauncher.py"] if include_launcher else [])
    protected = _protected_pids()
    found: List[Dict[str, Any]] = []
    rows: List[Tuple[int, str, str]] = []

    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | Where-Object { $_.CommandLine } | "
                  "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
            kw: Dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "universal_newlines": True,
                "timeout": 30,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kw["creationflags"] = 0x08000000
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                **kw
            ).stdout.strip()
            if out:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                rows = [(int(d["ProcessId"]), str(d.get("Name") or ""), str(d["CommandLine"])) for d in data]
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 2)
                if len(parts) >= 3 and parts[0].isdigit():
                    rows.append((int(parts[0]), parts[1], parts[2]))
    except Exception:
        return []

    for pid, name, cmd in rows:
        if pid in protected:
            continue
        low = cmd.lower()
        if "python" not in low:
            continue
        if any(s.lower() in low for s in scripts):
            found.append({"pid": pid, "name": name, "cmd": cmd})
    return found


def stop_processes(procs: List[Dict[str, Any]]) -> List[int]:
    """Terminates the given processes (and their children). Returns the PIDs that were signalled."""
    stopped = []
    for p in procs:
        pid = p["pid"]
        try:
            if sys.platform == "win32":
                tk_kw: Dict[str, Any] = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "timeout": 30,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    tk_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    tk_kw["creationflags"] = 0x08000000
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    **tk_kw
                )
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except Exception:
            pass
    return stopped


def known_shortcut_paths() -> List[str]:
    """Locations where setup_wizard.create_desktop_shortcuts() may have written shortcuts."""
    home = os.path.expanduser("~")
    paths: List[str] = []
    if sys.platform == "win32":
        desktop_dirs = [
            os.path.join(os.environ.get("USERPROFILE", home), "Desktop"),
            os.path.join(os.environ.get("USERPROFILE", home), "OneDrive", "Desktop"),
        ]
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=10,
            ).stdout.strip()
            if out:
                desktop_dirs.insert(0, out)
        except Exception:
            pass
        seen = set()
        for d in desktop_dirs:
            norm_d = os.path.normcase(os.path.abspath(d))
            if norm_d not in seen:
                seen.add(norm_d)
                paths.append(os.path.join(d, "ApexBOS Control Center.lnk"))
        return paths
    if sys.platform == "darwin":
        return [os.path.join(home, "Desktop", "ApexBOS Control Center.command")]
    desktop_dirs = [os.path.join(home, "Desktop")]
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=5,
        ).stdout.strip()
        if out and out not in desktop_dirs:
            desktop_dirs.insert(0, out)
    except Exception:
        pass
    for d in desktop_dirs:
        paths.append(os.path.join(d, "ApexBOS.desktop"))
    paths.append(os.path.join(home, ".local", "share", "applications", "ApexBOS.desktop"))
    return paths


def shortcut_points_to(path: str, install_dir: str = INSTALL_DIR) -> bool:
    """True if the shortcut at `path` launches this installation (so we never delete another install's shortcut)."""
    if not os.path.isfile(path):
        return False
    targets = {
        os.path.normcase(os.path.abspath(install_dir)),
        os.path.normcase(os.path.realpath(install_dir)),
    }
    try:
        if path.lower().endswith(".lnk"):
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                  "Write-Output ($s.TargetPath+'|'+$s.WorkingDirectory+'|'+$s.Arguments)" % path.replace("'", "''"))
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            ).stdout
            norm_out = os.path.normcase(out)
            return any(t in norm_out for t in targets)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            norm_txt = os.path.normcase(f.read())
            return any(t in norm_txt for t in targets)
    except Exception:
        return False


def confirm(prompt: str, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def launch_launcher_detached(install_dir: str = INSTALL_DIR):
    """Restarts the Control Center after an update/cancel."""
    script = os.path.join(install_dir, "ApexBOSLauncher.py")
    if not os.path.isfile(script):
        return
    kwargs: Dict[str, Any] = {
        "cwd": install_dir,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # Avoid DETACHED_PROCESS (0x08) which breaks subsequent console and GUI allocations;
        # CREATE_NEW_PROCESS_GROUP (0x200) allows clean, independent process execution.
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, script], **kwargs)
    except Exception as exc:
        print(f"[!] Could not restart the Control Center: {exc}")
