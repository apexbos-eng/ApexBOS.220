"""
ApexBOSLauncher.py - Unified Cross-Platform Control Center & Dashboard (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

A Pygame-native dashboard for managing all applications in the ApexBOS suite:
  - Top-level macro workflows:
      * Start Local Monitoring (Grabber procs + Flux + Engine + Viewer)
      * Start Remote Monitoring (Flux + Engine + Viewer over Cloud Banc)
      * Edit Layouts (ApexBOSCreator)
      * Stop All Running Processes
  - Individual process start / stop / restart / status monitoring:
      * Grabber (Processes 1..N)
      * Flux Relay
      * DispatcherEngine Host
      * Pygame Railroad Viewer
      * ApexBOSBanc Cloud Server
      * Utilities: ApexBOSSpy, PackFIG
  - Setup Wizard re-configuration launcher (setup_wizard.py)
  - Native multi-window terminal launcher for Windows, Linux, and macOS.
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
import shlex
import signal
import shutil
import tempfile
import threading
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
from typing import Dict, Any, List, Optional, Tuple

import config_loader
import install_utils
from version import VERSION

COLOR_BG = (14, 18, 24)
COLOR_CARD = (20, 26, 36)
COLOR_CARD_HOVER = (28, 36, 50)
COLOR_BORDER = (65, 85, 115)
COLOR_ACCENT = (255, 185, 40)
COLOR_GREEN = (50, 225, 110)
COLOR_RED = (245, 50, 50)
COLOR_TEXT = (225, 235, 248)
COLOR_TEXT_DIM = (130, 142, 160)
COLOR_BTN_BG = (30, 40, 56)
COLOR_STOP_BG = (95, 32, 36)


class ButtonEntry(tuple):
    """
    Subclass of tuple (rect, action_fn) that preserves backward-compatible
    tuple unpacking `(rect, fn) = self.buttons[k]` while attaching .cmd_key
    and .label for clipboard command-line copying.
    """
    def __new__(cls, rect, action_fn, cmd_key=None, label=None):
        return super().__new__(cls, (rect, action_fn))

    def __init__(self, rect, action_fn, cmd_key=None, label=None):
        self.rect = rect
        self.action = action_fn
        self.cmd_key = cmd_key or ""
        self.label = label or ""


def copy_to_clipboard(text: str) -> bool:
    """Copies text to the system clipboard across platforms (Windows clip.exe or pygame.scrap)."""
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


def get_stop_marker_path(subsystem_key: str) -> str:
    """Returns a deterministic temp file path used to mark a Launcher-initiated stop."""
    safe_key = "".join(c if (c.isalnum() or c in ("_", "-")) else "_" for c in str(subsystem_key).strip().lower())
    return os.path.join(tempfile.gettempdir(), f"apexbos_stop_{safe_key}.flag")


def _clear_stop_marker(marker_path: Optional[str]):
    if marker_path:
        try:
            if os.path.exists(marker_path):
                os.remove(marker_path)
        except Exception:
            pass


def _touch_stop_marker(marker_path: Optional[str]):
    if marker_path:
        try:
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write("stopped\n")
        except Exception:
            pass


def _quote_bat_arg(arg: str, force_quote: bool = False) -> str:
    """Quotes an argument safely for execution inside a Windows .bat wrapper."""
    s = str(arg).replace("%", "%%")
    if force_quote or not s or any(ch in s for ch in ' \t"&|<>^()'):
        escaped = s.replace('"', '\\"')
        if escaped.endswith("\\"):
            escaped += "\\"
        return f'"{escaped}"'
    return s


def _write_windows_terminal_wrapper(
    py_exe: str,
    target_script: str,
    args: List[str],
    stop_marker: Optional[str] = None,
) -> str:
    """
    Creates a temporary Windows .bat wrapper that runs py_exe target_script args,
    closes the terminal cleanly when exit code == 0 or stop_marker exists, and
    keeps the shell open (cmd /k) ONLY if a non-zero error exit is detected.
    """
    cmd_parts = [
        _quote_bat_arg(py_exe, force_quote=True),
        _quote_bat_arg(target_script, force_quote=True),
    ] + [_quote_bat_arg(a) for a in args]
    run_line = " ".join(cmd_parts)

    lines = [
        "@echo off",
        run_line,
        'set "APEX_RC=%ERRORLEVEL%"',
    ]
    if stop_marker:
        safe_marker = str(stop_marker).replace("%", "%%")
        lines.append(f'if exist "{safe_marker}" goto :clean_exit')
    lines.extend([
        'if "%APEX_RC%"=="0" goto :clean_exit',
        "echo.",
        "echo [Process exited with error code %APEX_RC%. Close this terminal when done.]",
        'del /f /q "%~f0" >nul 2>&1 & cmd /k & exit 0',
        ":clean_exit",
    ])
    if stop_marker:
        safe_marker = str(stop_marker).replace("%", "%%")
        lines.append(f'if exist "{safe_marker}" del /f /q "{safe_marker}" >nul 2>&1')
    lines.append('del /f /q "%~f0" >nul 2>&1 & exit 0')

    fd, bat_path = tempfile.mkstemp(prefix="apexbos_run_", suffix=".bat")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    return bat_path


def spawn_in_new_terminal(
    title: str,
    script_path: str,
    args: List[str],
    cwd: Optional[str] = None,
    keep_open: Any = "on_error",
    stop_marker: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    """
    Spawns an interactive script in its own native terminal window across
    Windows, macOS, and Linux.
    - keep_open="on_error" (default): closes the terminal automatically when the
      script exits cleanly (code 0) or is stopped via stop_marker, and keeps the
      terminal open ONLY if a non-zero error exit is detected.
    - keep_open=True: always leaves the terminal shell open after exit.
    - keep_open=False: always closes the terminal after exit.
    """
    work_dir = os.path.abspath(cwd or os.getcwd())
    target_script = os.path.abspath(os.path.join(work_dir, script_path)) if not os.path.isabs(script_path) else os.path.abspath(script_path)
    py_exe = sys.executable
    if sys.platform == "win32" and os.path.basename(py_exe).lower() == "pythonw.exe":
        cand_py = os.path.join(os.path.dirname(py_exe), "python.exe")
        if os.path.exists(cand_py):
            py_exe = cand_py

    cmd = [py_exe, target_script] + args
    child_env = os.environ.copy()
    child_env.pop("SDL_WINDOWID", None)
    child_env.pop("SDL_VIDEO_WINDOW_POS", None)

    if sys.platform == "win32":
        if keep_open == "on_error":
            bat_path = _write_windows_terminal_wrapper(py_exe, target_script, args, stop_marker=stop_marker)
            if shutil.which("wt.exe"):
                full_cmd = ["wt.exe", "-w", "0", "nt", "-d", work_dir, "--title", title, "cmd.exe", "/c", bat_path]
                try:
                    return subprocess.Popen(full_cmd, cwd=work_dir, env=child_env)
                except Exception:
                    pass
            full_cmd = ["cmd.exe", "/c", bat_path]
            try:
                return subprocess.Popen(
                    full_cmd,
                    cwd=work_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    env=child_env,
                )
            except Exception:
                return subprocess.Popen(full_cmd, cwd=work_dir, env=child_env)
        else:
            shell_flag = "/k" if keep_open else "/c"
            if shutil.which("wt.exe"):
                full_cmd = ["wt.exe", "-w", "0", "nt", "-d", work_dir, "--title", title, "cmd.exe", shell_flag, py_exe, target_script] + args
                try:
                    return subprocess.Popen(full_cmd, cwd=work_dir, env=child_env)
                except Exception:
                    pass
            full_cmd = ["cmd.exe", shell_flag, py_exe, target_script] + args
            try:
                return subprocess.Popen(
                    full_cmd,
                    cwd=work_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    env=child_env,
                )
            except Exception:
                return subprocess.Popen(full_cmd, cwd=work_dir, env=child_env)

    elif sys.platform == "darwin":
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        if keep_open == "on_error":
            marker_check = ""
            if stop_marker:
                qm = shlex.quote(stop_marker)
                marker_check = f"if [ -f {qm} ]; then rm -f {qm}; exit 0; fi; "
            shell_cmd = (
                f"cd {shlex.quote(work_dir)} && {quoted_cmd}; rc=$?; "
                f"{marker_check}"
                'if [ "$rc" -ne 0 ]; then '
                'echo ""; echo "[Process exited with error code $rc. Close this terminal when done.]"; '
                "else exit 0; fi"
            )
        elif keep_open:
            shell_cmd = (
                f"cd {shlex.quote(work_dir)} && {quoted_cmd}; "
                'rc=$?; echo ""; echo "[Process exited with code $rc. Close this terminal when done.]"'
            )
        else:
            shell_cmd = f"cd {shlex.quote(work_dir)} && {quoted_cmd}; exit 0"
        osa_escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
        osa_script = f'tell application "Terminal" to do script "{osa_escaped}"'
        return subprocess.Popen(["osascript", "-e", osa_script], env=child_env)

    else:
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        shell_bin = "bash" if shutil.which("bash") else "sh"
        title_esc = f'printf "\\033]2;%s\\007" {shlex.quote(title)}; ' if title else ""
        if keep_open == "on_error":
            marker_check = ""
            if stop_marker:
                qm = shlex.quote(stop_marker)
                marker_check = f"if [ -f {qm} ]; then rm -f {qm}; exit 0; fi; "
            inner = (
                f"{title_esc}{quoted_cmd}; rc=$?; "
                f"{marker_check}"
                'if [ "$rc" -ne 0 ]; then '
                "echo ''; "
                'echo "[Process exited with error code $rc. Close this terminal when done.]"; '
                f"exec {shell_bin}; "
                "fi"
            )
            launch_cmd = [shell_bin, "-c", inner]
        elif keep_open:
            inner = (
                f"{title_esc}{quoted_cmd}; rc=$?; echo ''; "
                'echo "[Process exited with code $rc. Close this terminal when done.]"; '
                f"exec {shell_bin}"
            )
            launch_cmd = [shell_bin, "-c", inner]
        else:
            inner = f"{title_esc}exec {quoted_cmd}"
            launch_cmd = [shell_bin, "-c", inner]

        terminals = [
            ("gnome-terminal", ["gnome-terminal", "--"]),
            ("x-terminal-emulator", ["x-terminal-emulator", "-e"]),
            ("mate-terminal", ["mate-terminal", "--"]),
            ("xfce4-terminal", ["xfce4-terminal", "-x"]),
            ("konsole", ["konsole", "-e"]),
            ("alacritty", ["alacritty", "-e"]),
            ("kitty", ["kitty", "--"]),
            ("tilix", ["tilix", "-e"]),
            ("xterm", ["xterm", "-e"]),
        ]
        for term_bin, term_prefix in terminals:
            if shutil.which(term_bin):
                try:
                    return subprocess.Popen(term_prefix + launch_cmd, cwd=work_dir, env=child_env)
                except Exception:
                    continue
        # Fallback to background process if no graphical terminal found
        try:
            return subprocess.Popen(cmd, cwd=work_dir, env=child_env)
        except Exception:
            return None


SUBSYSTEM_SCRIPTS = {
    "grabber": "ApexBOSGrabber.py",
    "flux": "ApexBOSFlux.py",
    "engine": "ApexBOS.220.py",
    "viewer": "View_ApexBOS.220.py",
    "banc": "ApexBOSBanc.py",
    "creator": "ApexBOSCreator.py",
    "spy": "ApexBOSSpy.py",
    "packfig": "PackFIG.py",
    "scanner": "ApexBOSScanner.py",
}


class ProcessManager:
    """Tracks and supervises all managed child processes."""

    def __init__(self, config_path: Optional[str] = None):
        self.work_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path or os.path.join(self.work_dir, config_loader.CONFIG_FILENAME)
        migrated_msg = ""
        if config_loader.needs_migration(self.config_path):
            try:
                report = config_loader.migrate_config_file(self.config_path)
                if report.get("status") == "migrated":
                    added_n = len(report.get("added", []))
                    migrated_msg = f" (migrated {added_n} config item(s))"
            except Exception as e:
                migrated_msg = f" (config migration warning: {e})"
        self.config = config_loader.load_unified_config(self.config_path)
        dll_dir = (self.config.get("grabber") or {}).get("rtlsdr_dll_dir", "")
        if dll_dir:
            config_loader.apply_rtlsdr_dll_dir(dll_dir)
        self.procs: Dict[str, subprocess.Popen] = {}
        self._live_pids: Dict[str, int] = {}
        self._starting_until: Dict[str, float] = {}
        self.monitoring_mode: str = "local"
        self._lock = threading.Lock()
        self._stop_scanner = threading.Event()
        self._scanner_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scanner_thread.start()
        self.status_toast: str = f"ApexBOS Control Center ready (v{VERSION}){migrated_msg}."
        self.toast_time: float = time.time()
        self._scanner_launched: bool = False
        self._scanner_seen: bool = False
        self._scanner_start_time: float = 0.0
        self._scanner_results_applied: bool = False

    def _scan_loop(self):
        while not self._stop_scanner.is_set():
            try:
                self.refresh_live_processes()
            except Exception:
                pass
            self._stop_scanner.wait(1.0)

    def refresh_live_processes(self):
        """Scans OS process table so terminal-wrapped scripts stay accurately tracked."""
        live: Dict[str, int] = {}
        for p in install_utils.list_apexbos_processes(include_launcher=False):
            cmd_low = str(p.get("cmd") or "").lower()
            exe_low = str(p.get("name") or "").lower()
            # Ignore terminal/shell wrapper processes (cmd.exe / bash / sh / wt.exe)
            if exe_low in ("cmd.exe", "wt.exe", "bash", "sh", "zsh", "osascript"):
                continue

            # Check single subsystems
            for skey in ("grabber", "flux", "banc", "creator", "spy", "packfig", "scanner"):
                script = SUBSYSTEM_SCRIPTS.get(skey)
                if script and script.lower() in cmd_low and skey not in live:
                    live[skey] = int(p["pid"])

            # Disambiguate Viewer pairs (View_ApexBOS.220.py) first (to prevent substring collision with ApexBOS.220.py)
            if "view_apexbos.220.py" in cmd_low:
                pid = int(p["pid"])
                if "5038" in cmd_low or "5037" in cmd_low:
                    live["viewer_4"] = pid
                elif "5028" in cmd_low or "5027" in cmd_low:
                    live["viewer_3"] = pid
                elif "5018" in cmd_low or "5017" in cmd_low:
                    live["viewer_2"] = pid
                else:
                    live["viewer_1"] = pid
                    if "viewer" not in live:
                        live["viewer"] = pid

            # Disambiguate DispatcherEngine pairs (ApexBOS.220.py)
            elif "apexbos.220.py" in cmd_low:
                pid = int(p["pid"])
                if "5038" in cmd_low or "5037" in cmd_low:
                    live["engine_4"] = pid
                elif "5028" in cmd_low or "5027" in cmd_low:
                    live["engine_3"] = pid
                elif "5018" in cmd_low or "5017" in cmd_low:
                    live["engine_2"] = pid
                else:
                    live["engine_1"] = pid
                    if "engine" not in live:
                        live["engine"] = pid

        with self._lock:
            self._live_pids = live

    def reload_config(self):
        """Reloads unified configuration from disk."""
        self.config = config_loader.load_unified_config(self.config_path)
        self.set_toast("Configuration reloaded.")

    def set_toast(self, msg: str):
        self.status_toast = msg
        self.toast_time = time.time()

    def is_running(self, name: str) -> bool:
        aliases = [name]
        if name == "engine_1":
            aliases.append("engine")
        elif name == "engine":
            aliases.append("engine_1")
        elif name == "viewer_1":
            aliases.append("viewer")
        elif name == "viewer":
            aliases.append("viewer_1")

        with self._lock:
            for alias in aliases:
                if alias in self._live_pids:
                    return True
                if time.time() < self._starting_until.get(alias, 0.0):
                    return True
        for alias in aliases:
            p = self.procs.get(alias)
            if p is not None and p.poll() is None:
                return True
        return False

    def is_starting(self, name: str) -> bool:
        aliases = [name]
        if name == "engine_1":
            aliases.append("engine")
        elif name == "engine":
            aliases.append("engine_1")
        elif name == "viewer_1":
            aliases.append("viewer")
        elif name == "viewer":
            aliases.append("viewer_1")
        with self._lock:
            for alias in aliases:
                if time.time() < self._starting_until.get(alias, 0.0):
                    return True
        return False

    def get_pid(self, name: str) -> Optional[int]:
        aliases = [name]
        if name == "engine_1":
            aliases.append("engine")
        elif name == "engine":
            aliases.append("engine_1")
        elif name == "viewer_1":
            aliases.append("viewer")
        elif name == "viewer":
            aliases.append("viewer_1")

        with self._lock:
            for alias in aliases:
                if alias in self._live_pids:
                    return self._live_pids[alias]
        for alias in aliases:
            p = self.procs.get(alias)
            if p and p.poll() is None:
                return p.pid
        return None

    def stop_process(self, name: str):
        """Stops a single managed process cleanly (closing its terminal window automatically)."""
        aliases = [name]
        if name == "engine_1":
            aliases.append("engine")
        elif name == "engine":
            aliases.append("engine_1")
        elif name == "viewer_1":
            aliases.append("viewer")
        elif name == "viewer":
            aliases.append("viewer_1")

        # Mark Launcher-initiated stop before signalling so terminal wrappers close cleanly
        for a in aliases:
            _touch_stop_marker(get_stop_marker_path(a))

        stopped_any = False
        for a in aliases:
            with self._lock:
                self._starting_until.pop(a, None)
                live_pid = self._live_pids.pop(a, None)

            p = self.procs.pop(a, None)

            if live_pid is not None:
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/PID", str(live_pid), "/F"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=10,
                        )
                    else:
                        os.kill(live_pid, signal.SIGINT)
                    stopped_any = True
                except Exception:
                    pass

            if p and p.poll() is None:
                try:
                    if sys.platform == "win32":
                        p.terminate()
                    else:
                        p.send_signal(signal.SIGINT)
                    p.wait(timeout=2.0)
                    stopped_any = True
                except Exception:
                    try:
                        p.kill()
                        stopped_any = True
                    except Exception:
                        pass

        if not stopped_any:
            self.set_toast(f"{name} is not running.")
        else:
            self.set_toast(f"Stopped {name}.")

    def stop_all(self):
        """Stops all running managed processes."""
        with self._lock:
            keys = set(self.procs.keys()) | set(self._live_pids.keys()) | set(self._starting_until.keys())
        for i in range(1, 5):
            keys.add(f"engine_{i}")
            keys.add(f"viewer_{i}")
        keys.add("engine")
        keys.add("viewer")
        for name in list(keys):
            if self.is_running(name):
                self.stop_process(name)
        self.set_toast("Stopped all managed processes.")

    def start_grabber(self):
        """Starts a single Grabber process on the single RTL-SDR."""
        if self.is_running("grabber"):
            self.set_toast("ApexBOSGrabber is already running.")
            return

        grabber_cfg = self.config.get("grabber", {})
        chans = grabber_cfg.get("channels", config_loader.DEFAULT_CHANNELS)
        chans_raw = grabber_cfg.get("chans")
        if chans_raw and str(chans_raw).strip():
            chans_str = str(chans_raw).strip()
        else:
            if isinstance(chans, (list, tuple)):
                chans_str = ",".join(str(c) for c in chans)
            else:
                chans_str = str(chans)
        num_procs = grabber_cfg.get("process_count", 2)
        procs_arg = grabber_cfg.get("procs") or ",".join(str(p) for p in grabber_cfg.get("ports", [18077, 18081]))
        device_idx = int(grabber_cfg.get("device", 0) or 0)
        ch_count = len(chans) if isinstance(chans, (list, tuple)) else len([c for c in chans_str.split(",") if c.strip()])

        args = [
            "--chans", chans_str,
            "--procs", str(procs_arg),
            "--device", str(device_idx),
            "--bias-tee", "1",
        ]
        if grabber_cfg.get("auto_tune"):
            args.append("--auto-tune")

        sdr_mode = str(grabber_cfg.get("sdr_mode") or "auto").strip().lower()
        if sdr_mode and sdr_mode != "auto":
            args.extend(["--sdr-mode", sdr_mode])
        if sdr_mode == "tcp":
            if "--rtl-tcp" not in args:
                args.append("--rtl-tcp")
            tcp_host = str(grabber_cfg.get("rtl_tcp_host") or "127.0.0.1").strip()
            tcp_port = str(grabber_cfg.get("rtl_tcp_port") or 1234).strip()
            args.extend(["--host", tcp_host, "--rtl-port", tcp_port])
        else:
            if grabber_cfg.get("rtl_tcp_host"):
                args.extend(["--host", str(grabber_cfg["rtl_tcp_host"])])
            if grabber_cfg.get("rtl_tcp_port"):
                args.extend(["--rtl-port", str(grabber_cfg["rtl_tcp_port"])])
        if grabber_cfg.get("rtl_sdr_bin"):
            args.extend(["--rtl-sdr-bin", str(grabber_cfg["rtl_sdr_bin"])])
        if grabber_cfg.get("rtl_tcp_bin"):
            args.extend(["--rtl-tcp-bin", str(grabber_cfg["rtl_tcp_bin"])])
        if grabber_cfg.get("rtlsdr_dll_dir"):
            args.extend(["--rtlsdr-dll-dir", str(grabber_cfg["rtlsdr_dll_dir"])])
        if grabber_cfg.get("gain") is not None and str(grabber_cfg.get("gain")).strip():
            args.extend(["--gain", str(grabber_cfg["gain"]).strip()])
        ppm_val = grabber_cfg.get("ppm") if grabber_cfg.get("ppm") is not None else grabber_cfg.get("frequency_correction")
        if ppm_val is not None and str(ppm_val).strip():
            args.extend(["--ppm", str(ppm_val).strip()])

        title = f"ApexBOSGrabber (SDR #{device_idx} | {num_procs} Procs | Bias-T ON)"
        marker = get_stop_marker_path("grabber")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal(title, "ApexBOSGrabber.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["grabber"] = p
            with self._lock:
                self._starting_until["grabber"] = time.time() + 2.5
            self.set_toast(f"Started ApexBOSGrabber (SDR #{device_idx}, {num_procs} procs, {ch_count} chans).")

    def start_scanner(self, dwell: float = 3.5, update_config: bool = False) -> Optional[subprocess.Popen]:
        """
        Launches ApexBOSScanner in its own terminal window.
        Inherits gain, bias-tee, ppm, and device index from active Grabber config.
        """
        if self.is_running("scanner"):
            self.set_toast("ApexBOSScanner is already running.")
            return None

        grabber_cfg = self.config.get("grabber", {})
        ppm_val = grabber_cfg.get("ppm") if grabber_cfg.get("ppm") is not None else grabber_cfg.get("frequency_correction")
        gain_val = grabber_cfg.get("gain")
        bias_tee = bool(grabber_cfg.get("bias_tee", 1))
        device_idx = int(grabber_cfg.get("device", 0) or 0)

        args = ["--dwell", f"{float(dwell):.1f}"]
        if ppm_val is not None and str(ppm_val).strip() != "":
            args.extend(["--ppm", str(ppm_val).strip()])
        if gain_val is not None and str(gain_val).strip() != "" and str(gain_val).strip().lower() != "auto":
            args.extend(["--gain", str(gain_val).strip()])
        if device_idx:
            args.extend(["--device", str(device_idx)])
        if bias_tee:
            args.append("--bias-tee")
        if update_config:
            args.append("--update-config")

        title = f"ApexBOS 220 MHz Scanner (Dwell {dwell}s | PPM {ppm_val or 0} | Bias-T {'ON' if bias_tee else 'OFF'})"
        marker = get_stop_marker_path("scanner")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal(title, "ApexBOSScanner.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["scanner"] = p
            with self._lock:
                self._starting_until["scanner"] = time.time() + 10.0
            self._scanner_launched = True
            self._scanner_seen = False
            self._scanner_start_time = time.time()
            self._scanner_results_applied = False
            self.set_toast(f"Started 220 MHz Channel Scanner (dwell={dwell}s, base PPM={ppm_val or 0}).")
            return p
        return None

    def get_latest_scan_results(self) -> Optional[Dict[str, Any]]:
        """
        Reads apexbos_scan_results.json from temp directory or work_dir.
        Returns data dict if file exists and was modified since scanner was started.
        """
        try:
            cand_paths = [
                os.path.join(tempfile.gettempdir(), "apexbos_scan_results.json"),
                os.path.join(self.work_dir, "apexbos_scan_results.json"),
            ]
            for p in cand_paths:
                if os.path.exists(p):
                    try:
                        mtime = os.path.getmtime(p)
                        min_time = (self._scanner_start_time - 5.0) if self._scanner_start_time > 0 else 0.0
                        if mtime >= min_time:
                            with open(p, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            if isinstance(data, dict) and "active_channels" in data:
                                return data
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    def apply_scan_results(self, results_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Updates Grabber channels (--chans), procs (--procs), ports, and PPM in
        config.json and grabber_config.json from scanner results.
        """
        active_chans = results_data.get("active_channels", [])
        if not active_chans:
            return False, "No active channels found in scan results."

        rec_ppm = int(results_data.get("recommended_ppm", 0))
        rec_gain = results_data.get("recommended_gain")

        grabber_cfg = self.config.setdefault("grabber", {})
        sorted_chans = sorted(set(int(c) for c in active_chans))
        locked_chans_raw = results_data.get("locked_channels")
        if locked_chans_raw is None:
            locked_set = set(sorted_chans)
        else:
            locked_set = set(int(c) for c in locked_chans_raw) & set(sorted_chans)
        chans_tokens = [f"{c}." if c in locked_set else str(c) for c in sorted_chans]
        chans_str = ", ".join(chans_tokens)
        grabber_cfg["channels"] = sorted_chans
        grabber_cfg["chans"] = chans_str
        grabber_cfg["locked_channels"] = sorted(locked_set)
        grabber_cfg["ppm"] = rec_ppm
        grabber_cfg["frequency_correction"] = rec_ppm
        if rec_gain is not None:
            grabber_cfg["gain"] = float(rec_gain)

        # Calculate new worker processes & ports matching channel count
        existing_procs = int(grabber_cfg.get("process_count", 2) or 2)
        worker_count = min(existing_procs, max(1, len(sorted_chans)))
        ports, _ = config_loader.calculate_grabber_procs(sorted_chans, worker_count)
        grabber_cfg["ports"] = ports
        procs_str = ",".join(str(p) for p in ports)
        grabber_cfg["procs"] = procs_str
        grabber_cfg["process_count"] = len(ports)

        # Synchronize Flux relay sources if configured
        flux_cfg = self.config.get("flux")
        if isinstance(flux_cfg, dict):
            flux_cfg["sources"] = [f"tcp://127.0.0.1:{p}" for p in ports]
            flux_cfg["ports"] = list(ports)

        config_loader.save_unified_config(self.config, self.config_path)
        config_loader.sync_legacy_configs(self.config, self.work_dir)
        self.reload_config()
        self._scanner_results_applied = True
        msg = f"Updated Grabber config: {len(sorted_chans)} chans ({chans_str}), {len(ports)} procs ({procs_str}), PPM {rec_ppm:+d}."
        self.set_toast(msg)
        return True, msg

    def start_grabber_proc(self, proc_idx: int = 0):
        """Backwards compatibility shim for starting Grabber."""
        self.start_grabber()

    def start_flux(self):
        """Starts ApexBOSFlux in its own terminal."""
        if self.is_running("flux"):
            self.set_toast("ApexBOSFlux is already running.")
            return
        args = ["--config", self.config_path, "--display"]
        marker = get_stop_marker_path("flux")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal("ApexBOSFlux Relay", "ApexBOSFlux.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["flux"] = p
            with self._lock:
                self._starting_until["flux"] = time.time() + 2.5
            self.set_toast("Started ApexBOSFlux Relay.")

    def get_mcast_port(self, pair_idx: int = 1) -> int:
        """Returns outbound UDP Multicast port for Engine/Viewer pair (default: 5007 + 10*(pair_idx-1))."""
        base = int((self.config.get("engine") or {}).get("mcast_port") or 5007)
        return base + 10 * max(0, pair_idx - 1)

    def get_tcp_port(self, pair_idx: int = 1) -> int:
        """Returns TCP layout server port for Engine/Viewer pair (default: 5008 + 10*(pair_idx-1))."""
        base = int((self.config.get("engine") or {}).get("layout_port") or 5008)
        return base + 10 * max(0, pair_idx - 1)

    def is_banc_source(self) -> bool:
        """Returns True if the active configuration or deployment mode uses Banc Cloud as packet source."""
        common = self.config.get("common", {}) if isinstance(self.config.get("common"), dict) else {}
        conns = self.config.get("connections", {}) if isinstance(self.config.get("connections"), dict) else {}
        eng_conns = (self.config.get("engine", {}) if isinstance(self.config.get("engine"), dict) else {}).get("connections", {})
        if not isinstance(eng_conns, dict):
            eng_conns = {}

        pkt_src = str(common.get("packet_source") or conns.get("packet_source") or eng_conns.get("packet_source") or "").lower()
        if pkt_src == "banc":
            return True
        dep_mode = str(common.get("deployment_mode") or "").lower()
        if dep_mode in ("cloud", "remote"):
            return True
        if getattr(self, "monitoring_mode", "") == "remote":
            return True
        return False

    def _resolve_layout_file(self, layout_name: str) -> str:
        for cand in (
            layout_name,
            os.path.join(self.work_dir, layout_name),
            os.path.join(self.work_dir, "layouts", os.path.basename(layout_name)),
            os.path.join(self.work_dir, "config", os.path.basename(layout_name)),
            os.path.join(self.work_dir, os.path.basename(layout_name)),
        ):
            if os.path.exists(cand):
                return os.path.abspath(cand)
        return os.path.abspath(os.path.join(self.work_dir, "layouts", "arrow_layout.json"))

    def get_layout_for_pair(self, pair_idx: int = 1) -> str:
        """
        Resolves layout file path for pair_idx (1..4).
        Pair 1 uses default layout (layouts/arrow_layout.json).
        Pairs 2..4 pick unique layouts from layouts/ if available, or fallback.
        """
        eng_cfg = self.config.get("engine", {})
        custom_layout = eng_cfg.get(f"layout_pair_{pair_idx}")
        if custom_layout:
            return self._resolve_layout_file(custom_layout)

        layouts_dir = os.path.join(self.work_dir, "layouts")
        available = []
        if os.path.isdir(layouts_dir):
            for f in sorted(os.listdir(layouts_dir)):
                if f.startswith("."):
                    continue
                if f.lower().endswith(".json"):
                    available.append(os.path.join(layouts_dir, f))

        default_layout = eng_cfg.get("default_layout") or eng_cfg.get("layout") or "layouts/arrow_layout.json"
        resolved_default = self._resolve_layout_file(default_layout)

        if pair_idx == 1:
            return resolved_default

        non_default = [p for p in available if os.path.abspath(p) != os.path.abspath(resolved_default)]
        target_idx = pair_idx - 2
        if 0 <= target_idx < len(non_default):
            return non_default[target_idx]

        return resolved_default

    def start_engine(self, pair_idx: int = 1, remote_banc_mode: Optional[bool] = None, layout_override: Optional[str] = None):
        """Starts DispatcherEngine in its own terminal for the specified pair (1..4)."""
        proc_key = f"engine_{pair_idx}"
        if self.is_running(proc_key) or (pair_idx == 1 and self.is_running("engine")):
            self.set_toast(f"DispatcherEngine (Pair #{pair_idx}) is already running.")
            return

        mcast_port = self.get_mcast_port(pair_idx)
        tcp_port = self.get_tcp_port(pair_idx)
        layout_path = layout_override or self.get_layout_for_pair(pair_idx)

        args = [
            "--config", self.config_path,
            "--layout", layout_path,
            "--engine-mcast-port", str(mcast_port),
            "--layout-port", str(tcp_port),
        ]
        use_banc = remote_banc_mode if remote_banc_mode is not None else self.is_banc_source()
        if use_banc:
            args.extend(["--source", "banc"])

        title = f"ApexBOS DispatcherEngine (Pair #{pair_idx} | UDP {mcast_port} | TCP {tcp_port})"
        marker = get_stop_marker_path(proc_key)
        _clear_stop_marker(marker)
        if pair_idx == 1:
            _clear_stop_marker(get_stop_marker_path("engine"))
        p = spawn_in_new_terminal(title, "ApexBOS.220.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs[proc_key] = p
            if pair_idx == 1:
                self.procs["engine"] = p
            with self._lock:
                self._starting_until[proc_key] = time.time() + 2.5
                if pair_idx == 1:
                    self._starting_until["engine"] = time.time() + 2.5
            self.set_toast(f"Started DispatcherEngine (Pair #{pair_idx}, layout: {os.path.basename(layout_path)}).")

    def stop_engine(self, pair_idx: int = 1):
        """Stops DispatcherEngine for the given pair (1..4)."""
        self.stop_process(f"engine_{pair_idx}")
        if pair_idx == 1:
            self.stop_process("engine")

    def start_viewer(self, pair_idx: int = 1):
        """Starts View_ApexBOS.220 for the specified pair (1..4) in a graphical window."""
        proc_key = f"viewer_{pair_idx}"
        if self.is_running(proc_key) or (pair_idx == 1 and self.is_running("viewer")):
            self.set_toast(f"Railroad Viewer (Pair #{pair_idx}) is already running.")
            return

        mcast_port = self.get_mcast_port(pair_idx)
        tcp_port = self.get_tcp_port(pair_idx)

        cmd = [
            sys.executable,
            os.path.join(self.work_dir, "View_ApexBOS.220.py"),
            "--config", self.config_path,
            "--banc-config", self.config_path,
            "--mcast-port", str(mcast_port),
            "--engine-port", str(tcp_port),
        ]
        child_env = os.environ.copy()
        child_env.pop("SDL_WINDOWID", None)
        child_env.pop("SDL_VIDEO_WINDOW_POS", None)
        p = subprocess.Popen(cmd, cwd=self.work_dir, env=child_env, stdin=subprocess.DEVNULL)
        self.procs[proc_key] = p
        if pair_idx == 1:
            self.procs["viewer"] = p
        with self._lock:
            self._starting_until[proc_key] = time.time() + 2.5
            if pair_idx == 1:
                self._starting_until["viewer"] = time.time() + 2.5
        self.set_toast(f"Started Railroad Viewer (Pair #{pair_idx}, UDP {mcast_port} | TCP {tcp_port}).")

    def stop_viewer(self, pair_idx: int = 1):
        """Stops Railroad Viewer for the given pair (1..4)."""
        self.stop_process(f"viewer_{pair_idx}")
        if pair_idx == 1:
            self.stop_process("viewer")

    def start_pair(self, pair_idx: int = 1, remote_banc_mode: Optional[bool] = None):
        """Starts both Engine and Viewer for the given pair index (1..4)."""
        if not self.is_running(f"engine_{pair_idx}"):
            self.start_engine(pair_idx=pair_idx, remote_banc_mode=remote_banc_mode)
            time.sleep(0.3)
        if not self.is_running(f"viewer_{pair_idx}"):
            self.start_viewer(pair_idx=pair_idx)
        self.set_toast(f"Started Pair #{pair_idx} (Engine & Viewer).")

    def stop_pair(self, pair_idx: int = 1):
        """Stops both Engine and Viewer for the given pair index (1..4)."""
        self.stop_engine(pair_idx)
        self.stop_viewer(pair_idx)
        self.set_toast(f"Stopped Pair #{pair_idx}.")

    def restart_pair(self, pair_idx: int = 1):
        """Restarts both Engine and Viewer for the given pair index (1..4)."""
        self.stop_pair(pair_idx)
        time.sleep(0.4)
        self.start_pair(pair_idx)

    def is_pair_running(self, pair_idx: int = 1) -> bool:
        return self.is_running(f"engine_{pair_idx}") or self.is_running(f"viewer_{pair_idx}")

    def is_pair_fully_running(self, pair_idx: int = 1) -> bool:
        return self.is_running(f"engine_{pair_idx}") and self.is_running(f"viewer_{pair_idx}")

    def start_banc(self):
        """Starts ApexBOSBanc Cloud Server."""
        if self.is_running("banc"):
            self.set_toast("ApexBOSBanc is already running.")
            return
        args = ["--config", self.config_path, "--display"]
        marker = get_stop_marker_path("banc")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal("ApexBOSBanc Cloud Server", "ApexBOSBanc.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["banc"] = p
            with self._lock:
                self._starting_until["banc"] = time.time() + 2.5
            self.set_toast("Started ApexBOSBanc Cloud Server.")

    def start_creator(self):
        """Starts ApexBOSCreator Studio."""
        cmd = [sys.executable, os.path.join(self.work_dir, "ApexBOSCreator.py"), "--banc-config", self.config_path]
        eng_cfg = self.config.get("engine", {})
        creator_cfg = self.config.get("creator", {})
        default_layout = creator_cfg.get("default_layout") or eng_cfg.get("default_layout", "layouts/arrow_layout.json")
        resolved_layout = self._resolve_layout_file(default_layout)
        if resolved_layout and os.path.exists(resolved_layout):
            cmd.extend(["--layout", resolved_layout])
        child_env = os.environ.copy()
        child_env.pop("SDL_WINDOWID", None)
        child_env.pop("SDL_VIDEO_WINDOW_POS", None)
        p = subprocess.Popen(cmd, cwd=self.work_dir, env=child_env, stdin=subprocess.DEVNULL)
        self.procs["creator"] = p
        self.set_toast(f"Launched ApexBOSCreator Studio ({os.path.basename(resolved_layout)}).")

    def start_spy(self):
        """Starts ApexBOSSpy connected to all Grabber ZMQ ports with default parsers."""
        grabber_cfg = self.config.get("grabber", {})
        ports = grabber_cfg.get("ports")
        if not ports:
            procs_val = grabber_cfg.get("procs", "18077,18081")
            ports = [p.strip() for p in str(procs_val).split(",") if p.strip()]
        ports_str = ",".join(str(p) for p in ports)
        args = ["--connect", ports_str]
        spy_cfg = self.config.get("spy", {})
        rules = spy_cfg.get("rules", [])
        if rules:
            args.append("--rules")
            for r in rules:
                rule_path = r if os.path.isabs(r) or os.path.exists(r) else os.path.join("config", r)
                args.append(rule_path)
        marker = get_stop_marker_path("spy")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal("ApexBOSSpy Rule Inspector", "ApexBOSSpy.py", args, cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["spy"] = p
            self.set_toast("Launched ApexBOSSpy.")

    def start_packfig(self):
        """Starts PackFIG in a terminal."""
        marker = get_stop_marker_path("packfig")
        _clear_stop_marker(marker)
        p = spawn_in_new_terminal("PackFIG Binary Decoder", "PackFIG.py", [], cwd=self.work_dir, stop_marker=marker)
        if p:
            self.procs["packfig"] = p
            self.set_toast("Launched PackFIG.")

    def start_local_monitoring(self):
        """
        Macro workflow: Starts single Grabber, Flux, Engine,
        and Viewer for full local dispatching.
        """
        dep_mode = str(self.config.get("common", {}).get("deployment_mode") or "local").lower()
        if dep_mode in ("engines_viewers", "client", "display"):
            self.monitoring_mode = "local"
            self.start_pair(pair_idx=1, remote_banc_mode=False if not self.is_banc_source() else None)
            self.set_toast("Started Engine & Viewer (Pair #1).")
            return

        self.monitoring_mode = "local"
        self.start_grabber()
        time.sleep(0.3)
        self.start_flux()
        time.sleep(0.3)
        self.start_pair(pair_idx=1, remote_banc_mode=False if not self.is_banc_source() else None)
        self.set_toast("Started full Local Monitoring stack.")

    def start_remote_monitoring(self):
        """
        Macro workflow: Starts Flux, Engine, and Viewer connected to Cloud Banc.
        """
        dep_mode = str(self.config.get("common", {}).get("deployment_mode") or "local").lower()
        if dep_mode in ("engines_viewers", "client", "display"):
            self.monitoring_mode = "remote"
            self.start_pair(pair_idx=1, remote_banc_mode=True)
            self.set_toast("Started Engine & Viewer (Pair #1).")
            return

        self.monitoring_mode = "remote"
        self.start_flux()
        time.sleep(0.3)
        self.start_pair(pair_idx=1, remote_banc_mode=True)
        self.set_toast("Started Remote Monitoring stack (Cloud Banc).")

    def get_command_line(self, key: str) -> str:
        """
        Returns the exact command line string to run a subsystem, pair, macro,
        utility, or tool manually in a terminal or shell.
        """
        key_low = str(key).lower().strip()
        py_exe = "python"

        if key_low in ("start_local", "local", "local_monitoring"):
            return f"{py_exe} ApexBOSLauncher.py --mode local"

        if key_low in ("start_remote", "remote", "remote_monitoring"):
            return f"{py_exe} ApexBOSLauncher.py --mode remote"

        if key_low in ("edit_layouts", "creator"):
            layout = self.get_layout_for_pair(1)
            return subprocess.list2cmdline([py_exe, "ApexBOSCreator.py", "--banc-config", "config.json", "--layout", layout])

        if key_low in ("stop_all", "stop"):
            if sys.platform == "win32":
                return 'taskkill /F /FI "WINDOWTITLE eq ApexBOS*"'
            return 'pkill -f "ApexBOS|View_ApexBOS|PackFIG"'

        if key_low == "grabber":
            grabber_cfg = self.config.get("grabber", {})
            chans = grabber_cfg.get("channels", config_loader.DEFAULT_CHANNELS)
            chans_raw = grabber_cfg.get("chans")
            chans_str = str(chans_raw).strip() if chans_raw and str(chans_raw).strip() else (",".join(str(c) for c in chans) if isinstance(chans, (list, tuple)) else str(chans))
            procs_arg = grabber_cfg.get("procs") or ",".join(str(p) for p in grabber_cfg.get("ports", [18077, 18081]))
            device_idx = int(grabber_cfg.get("device", 0) or 0)
            args = [py_exe, "ApexBOSGrabber.py", "--chans", chans_str, "--procs", str(procs_arg), "--device", str(device_idx), "--bias-tee", "1"]
            if grabber_cfg.get("auto_tune"):
                args.append("--auto-tune")
            sdr_mode = str(grabber_cfg.get("sdr_mode") or "auto").strip().lower()
            if sdr_mode and sdr_mode != "auto":
                args.extend(["--sdr-mode", sdr_mode])
            if sdr_mode == "tcp":
                if "--rtl-tcp" not in args:
                    args.append("--rtl-tcp")
                tcp_host = str(grabber_cfg.get("rtl_tcp_host") or "127.0.0.1").strip()
                tcp_port = str(grabber_cfg.get("rtl_tcp_port") or 1234).strip()
                args.extend(["--host", tcp_host, "--rtl-port", tcp_port])
            else:
                if grabber_cfg.get("rtl_tcp_host"):
                    args.extend(["--host", str(grabber_cfg["rtl_tcp_host"])])
                if grabber_cfg.get("rtl_tcp_port"):
                    args.extend(["--rtl-port", str(grabber_cfg["rtl_tcp_port"])])
            if grabber_cfg.get("rtl_sdr_bin"):
                args.extend(["--rtl-sdr-bin", str(grabber_cfg["rtl_sdr_bin"])])
            if grabber_cfg.get("rtl_tcp_bin"):
                args.extend(["--rtl-tcp-bin", str(grabber_cfg["rtl_tcp_bin"])])
            if grabber_cfg.get("rtlsdr_dll_dir"):
                args.extend(["--rtlsdr-dll-dir", str(grabber_cfg["rtlsdr_dll_dir"])])
            if grabber_cfg.get("gain") is not None and str(grabber_cfg.get("gain")).strip():
                args.extend(["--gain", str(grabber_cfg["gain"]).strip()])
            ppm_val = grabber_cfg.get("ppm") if grabber_cfg.get("ppm") is not None else grabber_cfg.get("frequency_correction")
            if ppm_val is not None and str(ppm_val).strip():
                args.extend(["--ppm", str(ppm_val).strip()])
            return subprocess.list2cmdline(args)

        if key_low == "flux":
            return subprocess.list2cmdline([py_exe, "ApexBOSFlux.py", "--config", "config.json", "--display"])

        if key_low == "banc":
            return subprocess.list2cmdline([py_exe, "ApexBOSBanc.py", "--config", "config.json", "--display"])

        # Disambiguate Pair / Engine / Viewer keys
        for i in range(1, 5):
            if key_low in (f"pair_{i}", f"pair{i}"):
                eng_cmd = self.get_command_line(f"engine_{i}")
                view_cmd = self.get_command_line(f"viewer_{i}")
                sep = " && " if sys.platform == "win32" else " ; "
                return f"{eng_cmd}{sep}{view_cmd}"
            if key_low in (f"engine_{i}", f"engine{i}") or (i == 1 and key_low == "engine"):
                mcast = self.get_mcast_port(i)
                tcp = self.get_tcp_port(i)
                layout = self.get_layout_for_pair(i)
                cmd_args = [py_exe, "ApexBOS.220.py", "--config", "config.json", "--layout", layout, "--engine-mcast-port", str(mcast), "--layout-port", str(tcp)]
                if self.is_banc_source() or self.monitoring_mode == "remote":
                    cmd_args.extend(["--source", "banc"])
                return subprocess.list2cmdline(cmd_args)
            if key_low in (f"viewer_{i}", f"viewer{i}") or (i == 1 and key_low == "viewer"):
                mcast = self.get_mcast_port(i)
                tcp = self.get_tcp_port(i)
                return subprocess.list2cmdline([py_exe, "View_ApexBOS.220.py", "--config", "config.json", "--banc-config", "config.json", "--mcast-port", str(mcast), "--engine-port", str(tcp)])
            if key_low in (f"viewer_{i}", f"viewer{i}") or (i == 1 and key_low == "viewer"):
                mcast = self.get_mcast_port(i)
                tcp = self.get_tcp_port(i)
                return subprocess.list2cmdline([py_exe, "View_ApexBOS.220.py", "--config", "config.json", "--banc-config", "config.json", "--mcast-port", str(mcast), "--engine-port", str(tcp)])

        if key_low in ("scanner", "scan"):
            grabber_cfg = self.config.get("grabber", {})
            ppm_val = grabber_cfg.get("ppm") if grabber_cfg.get("ppm") is not None else grabber_cfg.get("frequency_correction")
            gain_val = grabber_cfg.get("gain")
            bias_tee = bool(grabber_cfg.get("bias_tee", 1))
            device_idx = int(grabber_cfg.get("device", 0) or 0)
            args = [py_exe, "ApexBOSScanner.py", "--dwell", "3.5"]
            if ppm_val is not None and str(ppm_val).strip() != "":
                args.extend(["--ppm", str(ppm_val).strip()])
            if gain_val is not None and str(gain_val).strip() != "" and str(gain_val).strip().lower() != "auto":
                args.extend(["--gain", str(gain_val).strip()])
            if device_idx:
                args.extend(["--device", str(device_idx)])
            if bias_tee:
                args.append("--bias-tee")
            return subprocess.list2cmdline(args)

        if key_low == "spy":
            grabber_cfg = self.config.get("grabber", {})
            ports = grabber_cfg.get("ports")
            if not ports:
                procs_val = grabber_cfg.get("procs", "18077,18081")
                ports = [p.strip() for p in str(procs_val).split(",") if p.strip()]
            ports_str = ",".join(str(p) for p in ports)
            args = [py_exe, "ApexBOSSpy.py", "--connect", ports_str]
            spy_cfg = self.config.get("spy", {})
            rules = spy_cfg.get("rules", [])
            if rules:
                args.append("--rules")
                for r in rules:
                    rule_path = r if os.path.isabs(r) or os.path.exists(r) else os.path.join("config", r)
                    args.append(rule_path)
            return subprocess.list2cmdline(args)

        if key_low == "packfig":
            return subprocess.list2cmdline([py_exe, "PackFIG.py"])

        if key_low in ("wizard", "setup"):
            return subprocess.list2cmdline([py_exe, "setup_wizard.py"])

        if key_low == "update":
            return subprocess.list2cmdline([py_exe, "update.py", "--relaunch"])

        if key_low == "uninstall":
            return subprocess.list2cmdline([py_exe, "uninstall.py"])

        if key_low == "firewall":
            fw = config_loader.generate_windows_firewall_commands(self.config)
            return fw.get("ps_oneliner", "enable_firewall.bat")

        if key_low == "reload":
            return "# Configuration reload is performed internally by ApexBOSLauncher"

        return f"{py_exe} {key}"


# ---------------------------------------------------------------------------
# Pygame Control Center Dashboard GUI
# ---------------------------------------------------------------------------

class ApexBOSLauncherApp:
    """Pygame-based graphical management dashboard."""

    def __init__(self, config_path: Optional[str] = None):
        import pygame
        pygame.init()
        pygame.font.init()

        self.width = 960
        self.height = 760
        self.screen = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption(f"ApexBOS Control Center ({VERSION})")

        self.clock = pygame.time.Clock()
        self.font_title = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 20, bold=True)
        self.font_h2 = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 13, bold=True)
        self.font_body = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 12)
        self.font_sm = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 11)
        self.font_tiny = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 10)

        self.mgr = ProcessManager(config_path)
        self.buttons: Dict[str, ButtonEntry] = {}
        self.modal_buttons: Dict[str, ButtonEntry] = {}
        self._modal_mode: Optional[str] = None  # None, "dwell_prompt", or "scan_complete"
        self._dwell_input_text: str = "3.5"
        self._scan_results_pending: Optional[Dict[str, Any]] = None
        self._running = True

    def run(self):
        import pygame
        self._running = True
        while self._running:
            self.clock.tick(30)
            events = pygame.event.get()
            for event in events:
                if event.type == pygame.QUIT:
                    self._running = False
                elif self._modal_mode == "dwell_prompt":
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_ESCAPE:
                            self._modal_mode = None
                        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                            self._start_scanner_from_modal()
                        elif event.key == pygame.K_BACKSPACE:
                            self._dwell_input_text = self._dwell_input_text[:-1]
                        elif event.unicode and event.unicode in "0123456789." and len(self._dwell_input_text) < 5:
                            self._dwell_input_text += event.unicode
                    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        self._handle_modal_click(event.pos)
                elif self._modal_mode == "scan_complete":
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                            self._apply_scanner_results_from_modal()
                        elif event.key == pygame.K_ESCAPE:
                            self._dismiss_scanner_results_modal()
                    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        self._handle_modal_click(event.pos)
                else:
                    if event.type == pygame.MOUSEBUTTONDOWN:
                        if event.button == 1:
                            self._handle_click(event.pos)
                        elif event.button == 3:
                            self._handle_right_click(event.pos)

            # Check if background scanner has completed and reload config
            if getattr(self.mgr, "_scanner_launched", False):
                scanner_active = self.mgr.is_running("scanner")
                if scanner_active:
                    self.mgr._scanner_seen = True
                elif getattr(self.mgr, "_scanner_seen", False) or (time.time() - getattr(self.mgr, "_scanner_start_time", 0.0) > 15.0):
                    self.mgr._scanner_launched = False
                    self.mgr._scanner_seen = False
                    try:
                        self.mgr.reload_config()
                    except Exception:
                        pass
                    try:
                        res = self.mgr.get_latest_scan_results()
                    except Exception:
                        res = None
                    ch_count = len(res.get("active_channels", [])) if res else 0
                    if ch_count > 0:
                        self.mgr.set_toast(f"Scanner finished ({ch_count} active chans). Reloaded config.json.")
                    else:
                        self.mgr.set_toast("Scanner finished. Reloaded config.json.")

            self._draw()
            pygame.display.flip()

        pygame.quit()

    def _open_scanner_prompt(self):
        """Opens modal dialog prompting user for Phase 1 observation dwell time."""
        self._dwell_input_text = "3.5"
        self._modal_mode = "dwell_prompt"

    def _start_scanner_from_modal(self):
        """Starts the scanner using the entered dwell time."""
        try:
            d = float(self._dwell_input_text.strip() or "3.5")
        except ValueError:
            d = 3.5
        self._modal_mode = None
        self.mgr.start_scanner(dwell=d)

    def _apply_scanner_results_from_modal(self):
        """Applies discovered channels and consensus PPM to Grabber configuration."""
        if self._scan_results_pending:
            self.mgr.apply_scan_results(self._scan_results_pending)
            self._scan_results_pending = None
        self._modal_mode = None

    def _dismiss_scanner_results_modal(self):
        """Dismisses the scan completion dialog without updating configuration."""
        self.mgr._scanner_results_applied = True
        self._scan_results_pending = None
        self._modal_mode = None

    def _handle_click(self, pos):
        try:
            if self._modal_mode:
                self._handle_modal_click(pos)
                return
            for btn_key, entry in list(self.buttons.items()):
                rect = entry[0]
                action_fn = entry[1]
                if rect.collidepoint(pos):
                    if action_fn:
                        action_fn()
                    break
        except Exception as e:
            self.mgr.set_toast(f"Action error: {e}")

    def _handle_modal_click(self, pos):
        try:
            for btn_key, entry in list(self.modal_buttons.items()):
                rect = entry[0]
                action_fn = entry[1]
                if rect.collidepoint(pos):
                    if action_fn:
                        action_fn()
                    break
        except Exception as e:
            self.mgr.set_toast(f"Modal action error: {e}")

    def _handle_right_click(self, pos):
        try:
            for btn_key, entry in list(self.buttons.items()):
                rect = entry[0]
                if rect.collidepoint(pos):
                    cmd_key = getattr(entry, "cmd_key", btn_key)
                    label = getattr(entry, "label", btn_key)
                    self.copy_command_to_clipboard(cmd_key, label)
                    break
        except Exception as e:
            self.mgr.set_toast(f"Clipboard action error: {e}")

    def copy_command_to_clipboard(self, cmd_key: str, label: str):
        cmd = self.mgr.get_command_line(cmd_key)
        if copy_to_clipboard(cmd):
            self.mgr.set_toast(f"Copied command line for '{label}' to clipboard!")
        else:
            self.mgr.set_toast(f"Failed to copy command for '{label}'.")

    def _draw(self):
        import pygame
        self.screen.fill(COLOR_BG)
        self.buttons.clear()

        # Header Bar
        pygame.draw.rect(self.screen, COLOR_CARD, (0, 0, self.width, 60))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, 60), (self.width, 60), 1)

        title = self.font_title.render("ApexBOS Railroad Dispatcher - Control Center", True, COLOR_TEXT)
        ver_lbl = self.font_sm.render(f"{VERSION}", True, COLOR_ACCENT)
        self.screen.blit(title, (25, 14))
        self.screen.blit(ver_lbl, (self.width - 130, 18))

        # Status Bar under Header
        dep_mode = self.mgr.config.get("common", {}).get("deployment_mode", "local").upper()
        ip_ver = self.mgr.config.get("common", {}).get("ip_version", "ipv4").upper()
        banc_info = self.mgr.config.get("common", {}).get("banc", {})
        banc_str = f"Banc: {banc_info.get('host', '127.0.0.1')}:{banc_info.get('port', 23009)}" if banc_info.get("enabled") else "Banc: Disabled"

        if dep_mode in ("ENGINES_VIEWERS", "CLIENT", "DISPLAY"):
            pkt_src = str(self.mgr.config.get("common", {}).get("packet_source", "flux")).upper()
            if pkt_src == "BANC":
                b_host = (self.mgr.config.get("engine", {}).get("connections", {})).get("banc_host", banc_info.get("host", "127.0.0.1"))
                b_port = (self.mgr.config.get("engine", {}).get("connections", {})).get("banc_port", banc_info.get("port", 23009))
                st_bar = f"Mode: ENGINES & VIEWERS ONLY | Source: BANC ({b_host}:{b_port}) | Config: config.json"
            else:
                m_grp = (self.mgr.config.get("engine", {}).get("connections", {})).get("mcast_group", "239.1.1.1")
                m_prt = (self.mgr.config.get("engine", {}).get("connections", {})).get("mcast_port", 23003)
                st_bar = f"Mode: ENGINES & VIEWERS ONLY | Source: FLUX ({m_grp}:{m_prt}) | Config: config.json"
        else:
            st_bar = f"Mode: {dep_mode} ({ip_ver}) | {banc_str} | Config: config.json"
        pygame.draw.rect(self.screen, (22, 28, 40), (0, 61, self.width, 24))
        lbl_info = self.font_sm.render(st_bar, True, COLOR_TEXT_DIM)
        self.screen.blit(lbl_info, (25, 65))

        # Top Macro Workflows Section
        y = 96
        w_lbl = self.font_h2.render("Global Monitoring Workflows:", True, COLOR_ACCENT)
        self.screen.blit(w_lbl, (25, y))
        y += 22

        macro_btns = [
            ("start_local", "Start Local Monitoring", COLOR_GREEN, self.mgr.start_local_monitoring),
            ("start_remote", "Start Remote Monitoring", (70, 190, 255), self.mgr.start_remote_monitoring),
            ("edit_layouts", "Edit Layouts (Creator)", COLOR_ACCENT, self.mgr.start_creator),
            ("stop_all", "Stop All Running", COLOR_RED, self.mgr.stop_all),
        ]
        bx = 25
        btn_w = (self.width - 50 - 3 * 12) // 4
        for bkey, text, col, fn in macro_btns:
            rect = pygame.Rect(bx, y, btn_w, 36)
            pygame.draw.rect(self.screen, COLOR_CARD, rect, border_radius=4)
            pygame.draw.rect(self.screen, col, rect, 2, border_radius=4)
            lbl = self.font_h2.render(text, True, col)
            self.screen.blit(lbl, lbl.get_rect(center=rect.center))
            self.buttons[bkey] = ButtonEntry(rect, fn, cmd_key=bkey, label=text)
            bx += btn_w + 12

        # Subsystems Status & Control Section
        y += 46
        sub_lbl = self.font_h2.render("Managed Subsystems & Multi-Layout Monitoring Pairs:", True, COLOR_ACCENT)
        self.screen.blit(sub_lbl, (25, y))
        y += 22

        # 1. Grabber and Flux rows (only if not client-only Engines & Viewers mode)
        grabber_cfg = self.mgr.config.get("grabber", {})
        procs_val = grabber_cfg.get("procs", "18077,18081")
        ports_str = str(procs_val)
        num_procs = grabber_cfg.get("process_count", len(grabber_cfg.get("ports", [])) or 2)
        dev_idx = grabber_cfg.get("device", 0)

        is_ev = str(self.mgr.config.get("common", {}).get("deployment_mode", "")).lower() in ("engines_viewers", "client", "display")
        single_items = []
        if not is_ev:
            single_items.extend([
                (
                    "grabber",
                    f"ApexBOSGrabber SDR #{dev_idx} ({num_procs} procs, ZMQ {ports_str})",
                    self.mgr.start_grabber,
                    lambda: self.mgr.stop_process("grabber"),
                ),
                (
                    "flux",
                    "ApexBOSFlux Relay",
                    self.mgr.start_flux,
                    lambda: self.mgr.stop_process("flux"),
                ),
            ])
        if self.mgr.config.get("common", {}).get("banc", {}).get("enabled"):
            single_items.append((
                "banc",
                "ApexBOSBanc Cloud Server",
                self.mgr.start_banc,
                lambda: self.mgr.stop_process("banc"),
            ))

        for skey, sname, start_fn, stop_fn in single_items:
            row_r = pygame.Rect(25, y, self.width - 50, 32)
            pygame.draw.rect(self.screen, COLOR_CARD, row_r, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, row_r, 1, border_radius=4)

            running = self.mgr.is_running(skey)
            pid = self.mgr.get_pid(skey)
            pygame.draw.circle(self.screen, COLOR_GREEN if running else COLOR_RED, (row_r.x + 16, row_r.centery), 5)

            title_txt = f"{sname} - RUNNING [PID: {pid}]" if (running and pid) else (f"{sname} - STARTING..." if running else f"{sname} - STOPPED")
            lbl = self.font_body.render(title_txt, True, COLOR_TEXT if running else COLOR_TEXT_DIM)
            self.screen.blit(lbl, (row_r.x + 32, row_r.centery - 7))

            # CMD button to copy manual command line
            btn_cmd = pygame.Rect(row_r.right - 54, row_r.y + 3, 50, 26)
            pygame.draw.rect(self.screen, COLOR_BTN_BG, btn_cmd, border_radius=3)
            pygame.draw.rect(self.screen, (70, 190, 255), btn_cmd, 1, border_radius=3)
            lbl_cmd = self.font_tiny.render("CMD", True, (70, 190, 255))
            self.screen.blit(lbl_cmd, lbl_cmd.get_rect(center=btn_cmd.center))
            self.buttons[f"{skey}_cmd"] = ButtonEntry(btn_cmd, lambda k=skey, l=sname: self.copy_command_to_clipboard(k, l), cmd_key=skey, label=sname)

            if running:
                btn_act = pygame.Rect(row_r.right - 200, row_r.y + 3, 68, 26)
                pygame.draw.rect(self.screen, COLOR_RED, btn_act, border_radius=3)
                lbl_btn = self.font_sm.render("STOP", True, (10, 10, 10))
                self.screen.blit(lbl_btn, lbl_btn.get_rect(center=btn_act.center))
                self.buttons[f"{skey}_action"] = ButtonEntry(btn_act, stop_fn, cmd_key=skey, label=sname)

                btn_term = pygame.Rect(row_r.right - 128, row_r.y + 3, 70, 26)
                pygame.draw.rect(self.screen, COLOR_BORDER, btn_term, border_radius=3)
                lbl_term = self.font_sm.render("RESTART", True, COLOR_TEXT)
                self.screen.blit(lbl_term, lbl_term.get_rect(center=btn_term.center))
                def _restart(fn_stop=stop_fn, fn_start=start_fn):
                    fn_stop()
                    time.sleep(0.3)
                    fn_start()
                self.buttons[f"{skey}_restart"] = ButtonEntry(btn_term, _restart, cmd_key=skey, label=f"Restart {sname}")
            else:
                btn_act = pygame.Rect(row_r.right - 132, row_r.y + 74, 74, 26)
                btn_act = pygame.Rect(row_r.right - 132, row_r.y + 3, 74, 26)
                pygame.draw.rect(self.screen, COLOR_GREEN, btn_act, border_radius=3)
                lbl_btn = self.font_sm.render("START", True, (10, 10, 10))
                self.screen.blit(lbl_btn, lbl_btn.get_rect(center=btn_act.center))
                self.buttons[f"{skey}_action"] = ButtonEntry(btn_act, start_fn, cmd_key=skey, label=sname)

            if skey == "grabber":
                btn_scan = pygame.Rect(row_r.right - 264 if running else row_r.right - 196, row_r.y + 3, 58, 26)
                pygame.draw.rect(self.screen, COLOR_BTN_BG, btn_scan, border_radius=3)
                pygame.draw.rect(self.screen, COLOR_ACCENT, btn_scan, 1, border_radius=3)
                lbl_scan = self.font_tiny.render("SCAN", True, COLOR_ACCENT)
                self.screen.blit(lbl_scan, lbl_scan.get_rect(center=btn_scan.center))
                self.buttons["grabber_scan"] = ButtonEntry(btn_scan, self._open_scanner_prompt, cmd_key="scanner", label="Scan 220 MHz Channels")

            y += 36

        # 2. The 4 Monitoring Pairs: Engine & Viewer with dedicated UDP Multicast & TCP Layout Ports
        for pair_idx in range(1, 5):
            row_r = pygame.Rect(25, y, self.width - 50, 32)
            pygame.draw.rect(self.screen, COLOR_CARD, row_r, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, row_r, 1, border_radius=4)

            mcast_port = self.mgr.get_mcast_port(pair_idx)
            tcp_port = self.mgr.get_tcp_port(pair_idx)
            layout_file = self.mgr.get_layout_for_pair(pair_idx)
            layout_base = os.path.basename(layout_file)

            eng_running = self.mgr.is_running(f"engine_{pair_idx}")
            view_running = self.mgr.is_running(f"viewer_{pair_idx}")
            both_running = eng_running and view_running
            either_running = eng_running or view_running

            eng_pid = self.mgr.get_pid(f"engine_{pair_idx}")
            view_pid = self.mgr.get_pid(f"viewer_{pair_idx}")

            # LED Indicator: Green if both running, Yellow if one running, Red if stopped
            if both_running:
                led_col = COLOR_GREEN
                state_str = f"RUNNING [E:{eng_pid} V:{view_pid}]"
            elif eng_running:
                led_col = (245, 200, 40)
                state_str = f"ENGINE RUNNING [PID: {eng_pid}] | VIEWER OFF"
            elif view_running:
                led_col = (245, 200, 40)
                state_str = f"VIEWER RUNNING [PID: {view_pid}] | ENGINE OFF"
            elif self.mgr.is_starting(f"engine_{pair_idx}") or self.mgr.is_starting(f"viewer_{pair_idx}"):
                led_col = (245, 200, 40)
                state_str = "STARTING..."
            else:
                led_col = COLOR_RED
                state_str = "STOPPED"

            pygame.draw.circle(self.screen, led_col, (row_r.x + 16, row_r.centery), 5)

            pair_label = f"Pair #{pair_idx}: Engine & Viewer (MCAST {mcast_port} | TCP {tcp_port}) - [{layout_base}] - {state_str}"
            lbl = self.font_body.render(pair_label, True, COLOR_TEXT if either_running else COLOR_TEXT_DIM)
            self.screen.blit(lbl, (row_r.x + 32, row_r.centery - 7))

            # Pair Control Buttons:
            # [STOP PAIR / START PAIR] [Eng] [View] [CMD]
            btn_cmd = pygame.Rect(row_r.right - 54, row_r.y + 3, 50, 26)
            pygame.draw.rect(self.screen, COLOR_BTN_BG, btn_cmd, border_radius=3)
            pygame.draw.rect(self.screen, (70, 190, 255), btn_cmd, 1, border_radius=3)
            lbl_cmd = self.font_tiny.render("CMD", True, (70, 190, 255))
            self.screen.blit(lbl_cmd, lbl_cmd.get_rect(center=btn_cmd.center))
            self.buttons[f"pair_{pair_idx}_cmd"] = ButtonEntry(
                btn_cmd,
                lambda idx=pair_idx: self.copy_command_to_clipboard(f"pair_{idx}", f"Pair #{idx}"),
                cmd_key=f"pair_{pair_idx}",
                label=f"Pair #{pair_idx}",
            )

            # View individual toggle button
            btn_view = pygame.Rect(row_r.right - 108, row_r.y + 3, 50, 26)
            view_bg = (24, 70, 42) if view_running else COLOR_BTN_BG
            view_border = COLOR_GREEN if view_running else COLOR_BORDER
            pygame.draw.rect(self.screen, view_bg, btn_view, border_radius=3)
            pygame.draw.rect(self.screen, view_border, btn_view, 1, border_radius=3)
            lbl_v = self.font_tiny.render("View" if view_running else "+View", True, COLOR_GREEN if view_running else COLOR_TEXT_DIM)
            self.screen.blit(lbl_v, lbl_v.get_rect(center=btn_view.center))
            def _toggle_view(idx=pair_idx, is_run=view_running):
                if is_run:
                    self.mgr.stop_viewer(idx)
                else:
                    self.mgr.start_viewer(idx)
            view_entry = ButtonEntry(
                btn_view,
                _toggle_view,
                cmd_key=f"viewer_{pair_idx}",
                label=f"Pair #{pair_idx} Viewer",
            )
            self.buttons[f"pair_{pair_idx}_view"] = view_entry
            if pair_idx == 1:
                self.buttons["viewer_action"] = view_entry
                self.buttons["viewer"] = view_entry

            # Eng individual toggle button
            btn_eng = pygame.Rect(row_r.right - 162, row_r.y + 3, 50, 26)
            eng_bg = (24, 70, 42) if eng_running else COLOR_BTN_BG
            eng_border = COLOR_GREEN if eng_running else COLOR_BORDER
            pygame.draw.rect(self.screen, eng_bg, btn_eng, border_radius=3)
            pygame.draw.rect(self.screen, eng_border, btn_eng, 1, border_radius=3)
            lbl_e = self.font_tiny.render("Eng" if eng_running else "+Eng", True, COLOR_GREEN if eng_running else COLOR_TEXT_DIM)
            self.screen.blit(lbl_e, lbl_e.get_rect(center=btn_eng.center))
            def _toggle_eng(idx=pair_idx, is_run=eng_running):
                if is_run:
                    self.mgr.stop_engine(idx)
                else:
                    self.mgr.start_engine(idx)
            eng_entry = ButtonEntry(
                btn_eng,
                _toggle_eng,
                cmd_key=f"engine_{pair_idx}",
                label=f"Pair #{pair_idx} Engine",
            )
            self.buttons[f"pair_{pair_idx}_eng"] = eng_entry
            if pair_idx == 1:
                self.buttons["engine_action"] = eng_entry
                self.buttons["engine"] = eng_entry

            # Start / Stop Pair button
            btn_pair_act = pygame.Rect(row_r.right - 262, row_r.y + 3, 96, 26)
            if either_running:
                pygame.draw.rect(self.screen, COLOR_RED, btn_pair_act, border_radius=3)
                lbl_pa = self.font_tiny.render("STOP PAIR", True, (10, 10, 10))
                self.screen.blit(lbl_pa, lbl_pa.get_rect(center=btn_pair_act.center))
                self.buttons[f"pair_{pair_idx}_action"] = ButtonEntry(
                    btn_pair_act,
                    lambda idx=pair_idx: self.mgr.stop_pair(idx),
                    cmd_key=f"pair_{pair_idx}",
                    label=f"Pair #{pair_idx}",
                )
            else:
                pygame.draw.rect(self.screen, COLOR_GREEN, btn_pair_act, border_radius=3)
                lbl_pa = self.font_tiny.render("START PAIR", True, (10, 10, 10))
                self.screen.blit(lbl_pa, lbl_pa.get_rect(center=btn_pair_act.center))
                self.buttons[f"pair_{pair_idx}_action"] = ButtonEntry(
                    btn_pair_act,
                    lambda idx=pair_idx: self.mgr.start_pair(idx),
                    cmd_key=f"pair_{pair_idx}",
                    label=f"Pair #{pair_idx}",
                )

            y += 36

        # Utilities & Settings Row
        y += 8
        ut_lbl = self.font_h2.render("Utilities & Settings:", True, COLOR_ACCENT)
        self.screen.blit(ut_lbl, (25, y))
        y += 22

        ut_btns = [
            ("scanner", "Scan 220 MHz Channels", self._open_scanner_prompt),
            ("spy", "ApexBOSSpy Inspector", self.mgr.start_spy),
            ("packfig", "PackFIG Binary Decoder", self.mgr.start_packfig),
            ("wizard", "Re-Run Setup Wizard", self._run_wizard_external),
            ("reload", "Reload config.json", self.mgr.reload_config),
        ]
        ux = 25
        ubtn_w = (self.width - 50 - 4 * 10) // 5
        for ukey, utext, ufn in ut_btns:
            urect = pygame.Rect(ux, y, ubtn_w, 32)
            pygame.draw.rect(self.screen, COLOR_BTN_BG, urect, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, urect, 1, border_radius=4)
            ulbl = self.font_sm.render(utext, True, COLOR_TEXT)
            if ulbl.get_width() > urect.width - 6:
                ulbl = self.font_tiny.render(utext, True, COLOR_TEXT)
            self.screen.blit(ulbl, ulbl.get_rect(center=urect.center))
            self.buttons[ukey] = ButtonEntry(urect, ufn, cmd_key=ukey, label=utext)
            ux += ubtn_w + 10

        y += 38
        maint_btns = [
            ("update", "Update (ZIP)", self._run_update_external, COLOR_BTN_BG),
            ("uninstall", "Uninstall", self._run_uninstall_external, COLOR_STOP_BG),
        ]
        if sys.platform == "win32":
            maint_btns.append(("firewall", "Windows Firewall", self._apply_firewall_external, COLOR_BTN_BG))
        mx = 25
        for mkey, mtext, mfn, mbg in maint_btns:
            mrect = pygame.Rect(mx, y, ubtn_w, 30)
            pygame.draw.rect(self.screen, mbg, mrect, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, mrect, 1, border_radius=4)
            mlbl = self.font_sm.render(mtext, True, COLOR_TEXT)
            self.screen.blit(mlbl, mlbl.get_rect(center=mrect.center))
            self.buttons[mkey] = ButtonEntry(mrect, mfn, cmd_key=mkey, label=mtext)
            mx += ubtn_w + 10

        # Bottom Hint & Toast / Notification Bar
        hint_txt = "[Tip: Right-click any button or click [CMD] to copy manual CLI command]"
        lbl_hint = self.font_tiny.render(hint_txt, True, (110, 175, 235))
        self.screen.blit(lbl_hint, (25, self.height - 54))

        pygame.draw.rect(self.screen, (10, 14, 20), (0, self.height - 35, self.width, 35))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, self.height - 35), (self.width, self.height - 35), 1)
        toast_lbl = self.font_sm.render(self.mgr.status_toast, True, COLOR_ACCENT)
        self.screen.blit(toast_lbl, (20, self.height - 25))

        # Modals (Dwell prompt & Scan completion)
        self.modal_buttons.clear()
        if self._modal_mode == "dwell_prompt":
            self._draw_dwell_modal()
        elif self._modal_mode == "scan_complete":
            self._draw_scan_complete_modal()

    def _draw_dwell_modal(self):
        import pygame
        # Dark dimming overlay
        overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        overlay.fill((10, 14, 22, 215))
        self.screen.blit(overlay, (0, 0))

        # Centered card
        cw, ch = 640, 320
        cx = (self.width - cw) // 2
        cy = (self.height - ch) // 2
        card_r = pygame.Rect(cx, cy, cw, ch)
        pygame.draw.rect(self.screen, COLOR_CARD, card_r, border_radius=8)
        pygame.draw.rect(self.screen, COLOR_ACCENT, card_r, 2, border_radius=8)

        # Title
        title_lbl = self.font_title.render("ApexBOS 220 MHz Channel Scanner", True, COLOR_ACCENT)
        self.screen.blit(title_lbl, (cx + 25, cy + 20))

        # Sub-header: Inherited Grabber configuration
        grabber_cfg = self.mgr.config.get("grabber", {})
        g_val = grabber_cfg.get("gain")
        g_txt = f"{g_val} dB" if (g_val is not None and str(g_val).strip() and str(g_val).lower() != "auto") else "AGC (Auto)"
        bt_txt = "ON (4.5V)" if grabber_cfg.get("bias_tee", 1) else "OFF"
        ppm_val = grabber_cfg.get("ppm") if grabber_cfg.get("ppm") is not None else grabber_cfg.get("frequency_correction", 0)
        dev_val = grabber_cfg.get("device", 0)
        sub_txt = f"Using Grabber Config: Gain: {g_txt} | Bias-T: {bt_txt} | PPM: {ppm_val or 0:+d} | SDR: #{dev_val}"
        sub_lbl = self.font_sm.render(sub_txt, True, COLOR_TEXT_DIM)
        self.screen.blit(sub_lbl, (cx + 25, cy + 54))

        pygame.draw.line(self.screen, COLOR_BORDER, (cx + 20, cy + 80), (cx + cw - 20, cy + 80), 1)

        # Prompt text
        prompt_txt = "Observation Dwell Duration per Channel (Phase 1):"
        p_lbl = self.font_h2.render(prompt_txt, True, COLOR_TEXT)
        self.screen.blit(p_lbl, (cx + 25, cy + 96))

        # Input box with text & cursor
        in_r = pygame.Rect(cx + (cw - 160) // 2, cy + 130, 160, 38)
        pygame.draw.rect(self.screen, (10, 14, 20), in_r, border_radius=4)
        pygame.draw.rect(self.screen, (80, 170, 255), in_r, 2, border_radius=4)
        disp_txt = f"{self._dwell_input_text}s"
        in_lbl = self.font_title.render(disp_txt, True, COLOR_TEXT)
        self.screen.blit(in_lbl, in_lbl.get_rect(center=in_r.center))

        # Preset buttons: [2.0s Quick] [3.5s Default] [5.0s Deep] [10.0s Long] [20.0s Max]
        presets = [
            ("2.0s Quick", "2.0"),
            ("3.5s Default", "3.5"),
            ("5.0s Deep", "5.0"),
            ("10.0s Long", "10.0"),
            ("20.0s Max", "20.0"),
        ]
        pre_w = 110
        gap = 10
        pre_start_x = cx + (cw - (len(presets) * pre_w + (len(presets) - 1) * gap)) // 2
        for idx, (plabel, pval) in enumerate(presets):
            pr = pygame.Rect(pre_start_x + idx * (pre_w + gap), cy + 184, pre_w, 28)
            is_active = (self._dwell_input_text == pval)
            pygame.draw.rect(self.screen, (25, 50, 75) if is_active else COLOR_BTN_BG, pr, border_radius=4)
            pygame.draw.rect(self.screen, (80, 170, 255) if is_active else COLOR_BORDER, pr, 1, border_radius=4)
            pl = self.font_sm.render(plabel, True, COLOR_TEXT if is_active else COLOR_TEXT_DIM)
            self.screen.blit(pl, pl.get_rect(center=pr.center))
            def _set_dwell(v=pval):
                self._dwell_input_text = v
            self.modal_buttons[f"preset_{pval}"] = ButtonEntry(pr, _set_dwell)

        # Bottom buttons: [Start Scanner] [Cancel]
        act_y = cy + ch - 54
        btn_start = pygame.Rect(cx + cw - 260, act_y, 140, 34)
        pygame.draw.rect(self.screen, COLOR_GREEN, btn_start, border_radius=4)
        lbl_s = self.font_h2.render("START SCANNER", True, (10, 10, 10))
        self.screen.blit(lbl_s, lbl_s.get_rect(center=btn_start.center))
        self.modal_buttons["start_scan"] = ButtonEntry(btn_start, self._start_scanner_from_modal)

        btn_cancel = pygame.Rect(cx + cw - 105, act_y, 85, 34)
        pygame.draw.rect(self.screen, COLOR_BTN_BG, btn_cancel, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, btn_cancel, 1, border_radius=4)
        lbl_c = self.font_sm.render("Cancel", True, COLOR_TEXT)
        self.screen.blit(lbl_c, lbl_c.get_rect(center=btn_cancel.center))
        self.modal_buttons["cancel_scan"] = ButtonEntry(btn_cancel, lambda: setattr(self, "_modal_mode", None))

        hint_lbl = self.font_tiny.render("[Enter: Start | Esc: Cancel | Type numbers or select preset]", True, COLOR_TEXT_DIM)
        self.screen.blit(hint_lbl, (cx + 25, act_y + 10))

    def _draw_scan_complete_modal(self):
        import pygame
        overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        overlay.fill((10, 14, 22, 220))
        self.screen.blit(overlay, (0, 0))

        cw, ch = 660, 360
        cx = (self.width - cw) // 2
        cy = (self.height - ch) // 2
        card_r = pygame.Rect(cx, cy, cw, ch)
        pygame.draw.rect(self.screen, COLOR_CARD, card_r, border_radius=8)
        pygame.draw.rect(self.screen, COLOR_GREEN, card_r, 2, border_radius=8)

        # Title
        title_lbl = self.font_title.render("Channel Scan Complete!", True, COLOR_GREEN)
        self.screen.blit(title_lbl, (cx + 25, cy + 20))

        sub_lbl = self.font_sm.render("Scanner discovered active 220 MHz telemetry channels & calibrated tuner PPM.", True, COLOR_TEXT_DIM)
        self.screen.blit(sub_lbl, (cx + 25, cy + 50))
        pygame.draw.line(self.screen, COLOR_BORDER, (cx + 20, cy + 74), (cx + cw - 20, cy + 74), 1)

        res = self._scan_results_pending or {}
        chans = res.get("active_channels", [])
        ppm = int(res.get("recommended_ppm", 0))
        gain = float(res.get("recommended_gain", 20.7))
        chans_str = ", ".join(str(c) for c in sorted(chans))

        # Calculate worker processes & ports
        existing_procs = int((self.mgr.config.get("grabber") or {}).get("process_count", 2) or 2)
        worker_count = min(existing_procs, max(1, len(chans)))
        ports, _ = config_loader.calculate_grabber_procs(chans, worker_count)
        procs_str = ",".join(str(p) for p in ports)

        lines = [
            (f"Active Channels ({len(chans)}):", f"[{chans_str}]", COLOR_ACCENT),
            ("Consensus Tuner PPM:", f"{ppm:+d} PPM", COLOR_GREEN),
            ("Recommended RF Gain:", f"{gain:.1f} dB (Headroom >= 12 dB)", COLOR_TEXT),
            ("Calculated Workers:", f"{len(ports)} processes (ZMQ Ports: {procs_str})", (80, 190, 255)),
        ]
        ly = cy + 90
        for label, val, val_col in lines:
            lbl_l = self.font_body.render(f"{label:<25}", True, COLOR_TEXT_DIM)
            lbl_v = self.font_body.render(val, True, val_col)
            self.screen.blit(lbl_l, (cx + 30, ly))
            self.screen.blit(lbl_v, (cx + 240, ly))
            ly += 26

        pygame.draw.line(self.screen, COLOR_BORDER, (cx + 20, ly + 6), (cx + cw - 20, ly + 6), 1)

        prompt_txt = "Update Grabber configuration (--chans & --procs) in config.json?"
        p_lbl = self.font_h2.render(prompt_txt, True, COLOR_TEXT)
        self.screen.blit(p_lbl, (cx + 30, ly + 18))

        act_y = cy + ch - 54
        btn_update = pygame.Rect(cx + cw - 380, act_y, 240, 34)
        pygame.draw.rect(self.screen, COLOR_GREEN, btn_update, border_radius=4)
        lbl_u = self.font_h2.render("UPDATE GRABBER CONFIG", True, (10, 10, 10))
        self.screen.blit(lbl_u, lbl_u.get_rect(center=btn_update.center))
        self.modal_buttons["update_config"] = ButtonEntry(btn_update, self._apply_scanner_results_from_modal)

        btn_dismiss = pygame.Rect(cx + cw - 125, act_y, 105, 34)
        pygame.draw.rect(self.screen, COLOR_BTN_BG, btn_dismiss, border_radius=4)
        pygame.draw.rect(self.screen, COLOR_BORDER, btn_dismiss, 1, border_radius=4)
        lbl_d = self.font_sm.render("Dismiss", True, COLOR_TEXT)
        self.screen.blit(lbl_d, lbl_d.get_rect(center=btn_dismiss.center))
        self.modal_buttons["dismiss_scan"] = ButtonEntry(btn_dismiss, self._dismiss_scanner_results_modal)

        hint_lbl = self.font_tiny.render("[Press Enter to Update | Esc to Dismiss]", True, COLOR_TEXT_DIM)
        self.screen.blit(hint_lbl, (cx + 30, act_y + 10))

    def _run_wizard_external(self):
        """Invokes setup_wizard.py in an external window."""
        child_env = os.environ.copy()
        child_env.pop("SDL_WINDOWID", None)
        child_env.pop("SDL_VIDEO_WINDOW_POS", None)
        subprocess.Popen([sys.executable, "setup_wizard.py"], env=child_env)
        self.mgr.set_toast("Opened Setup Wizard.")

    def _run_update_external(self):
        """Stops managed processes, launches update.py in a console, and exits Launcher."""
        self.mgr.stop_all()
        spawn_in_new_terminal("ApexBOS Updater", "update.py", ["--relaunch"])
        self._running = False

    def _run_uninstall_external(self):
        """Stops managed processes, launches uninstall.py in a console, and exits Launcher."""
        self.mgr.stop_all()
        spawn_in_new_terminal("ApexBOS Uninstaller", "uninstall.py", [])
        self._running = False

    def _apply_firewall_external(self):
        """Launches the Windows Firewall configuration script with Admin rights."""
        if config_loader.launch_firewall_script():
            self.mgr.set_toast("Launched Windows Firewall & Multicast configuration.")
        else:
            fw = config_loader.generate_windows_firewall_commands(self.mgr.config)
            from setup_wizard import copy_to_clipboard
            if copy_to_clipboard(fw["ps_oneliner"]):
                self.mgr.set_toast("Firewall commands copied to clipboard! Paste into Admin PowerShell.")
            else:
                self.mgr.set_toast("Check enable_firewall.bat in installation folder.")


def main():
    if "--scan" in sys.argv or "-d" in sys.argv or "--dwell" in sys.argv:
        dwell = 3.5
        for flag in ("--scan", "-d", "--dwell"):
            if flag in sys.argv:
                idx = sys.argv.index(flag) + 1
                if idx < len(sys.argv) and not sys.argv[idx].startswith("-"):
                    try:
                        dwell = float(sys.argv[idx])
                        break
                    except ValueError:
                        pass
        app = ApexBOSLauncherApp()
        app.mgr.start_scanner(dwell=dwell)
        app.run()
        return

    if "--mode" in sys.argv:
        # Direct CLI macro execution (e.g. from desktop shortcuts: --mode local or --mode remote)
        mgr = ProcessManager()
        mode_idx = sys.argv.index("--mode") + 1
        target_mode = sys.argv[mode_idx].lower() if mode_idx < len(sys.argv) else "local"
        if target_mode == "remote":
            mgr.start_remote_monitoring()
        else:
            mgr.start_local_monitoring()
        return

    app = ApexBOSLauncherApp()
    app.run()


if __name__ == "__main__":
    main()
