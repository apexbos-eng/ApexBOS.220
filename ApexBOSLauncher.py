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


def spawn_in_new_terminal(
    title: str,
    script_path: str,
    args: List[str],
    cwd: Optional[str] = None,
    keep_open: bool = True,
) -> Optional[subprocess.Popen]:
    """
    Spawns an interactive script in its own native terminal window across
    Windows, macOS, and Linux. When keep_open=True, the terminal shell stays
    open after the script exits so residual messages remain readable.
    """
    work_dir = os.path.abspath(cwd or os.getcwd())
    target_script = os.path.abspath(os.path.join(work_dir, script_path)) if not os.path.isabs(script_path) else os.path.abspath(script_path)
    py_exe = sys.executable

    cmd = [py_exe, target_script] + args
    child_env = os.environ.copy()
    child_env.pop("SDL_WINDOWID", None)
    child_env.pop("SDL_VIDEO_WINDOW_POS", None)

    if sys.platform == "win32":
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
        if keep_open:
            shell_cmd = (
                f"cd {shlex.quote(work_dir)} && {quoted_cmd}; "
                'rc=$?; echo ""; echo "[Process exited with code $rc. Close this terminal when done.]"'
            )
        else:
            shell_cmd = f"cd {shlex.quote(work_dir)} && {quoted_cmd}"
        osa_escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
        osa_script = f'tell application "Terminal" to do script "{osa_escaped}"'
        return subprocess.Popen(["osascript", "-e", osa_script], env=child_env)

    else:
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        if keep_open:
            shell_bin = "bash" if shutil.which("bash") else "sh"
            inner = (
                f"{quoted_cmd}; rc=$?; echo ''; "
                'echo "[Process exited with code $rc. Close this terminal when done.]"; '
                f"exec {shell_bin}"
            )
            launch_cmd = [shell_bin, "-c", inner]
        else:
            launch_cmd = cmd

        terminals = [
            ("gnome-terminal", ["gnome-terminal", "--title", title, "--"]),
            ("x-terminal-emulator", ["x-terminal-emulator", "-T", title, "-e"]),
            ("konsole", ["konsole", "-p", f"tabtitle={title}", "-e"]),
            ("xfce4-terminal", ["xfce4-terminal", "-T", title, "-x"]),
            ("xterm", ["xterm", "-T", title, "-e"]),
        ]
        for term_bin, term_prefix in terminals:
            if shutil.which(term_bin):
                return subprocess.Popen(term_prefix + launch_cmd, cwd=work_dir, env=child_env)
        # Fallback to background process if no graphical terminal found
        return subprocess.Popen(cmd, cwd=work_dir, env=child_env)


SUBSYSTEM_SCRIPTS = {
    "grabber": "ApexBOSGrabber.py",
    "flux": "ApexBOSFlux.py",
    "engine": "ApexBOS.220.py",
    "viewer": "View_ApexBOS.220.py",
    "banc": "ApexBOSBanc.py",
    "creator": "ApexBOSCreator.py",
    "spy": "ApexBOSSpy.py",
    "packfig": "PackFIG.py",
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
            for skey in ("grabber", "flux", "banc", "creator", "spy", "packfig"):
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
        """Stops a single managed process cleanly (leaving its terminal shell open if spawned in one)."""
        aliases = [name]
        if name == "engine_1":
            aliases.append("engine")
        elif name == "engine":
            aliases.append("engine_1")
        elif name == "viewer_1":
            aliases.append("viewer")
        elif name == "viewer":
            aliases.append("viewer_1")

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
        p = spawn_in_new_terminal(title, "ApexBOSGrabber.py", args, cwd=self.work_dir)
        if p:
            self.procs["grabber"] = p
            with self._lock:
                self._starting_until["grabber"] = time.time() + 2.5
            self.set_toast(f"Started ApexBOSGrabber (SDR #{device_idx}, {num_procs} procs, {ch_count} chans).")

    def start_grabber_proc(self, proc_idx: int = 0):
        """Backwards compatibility shim for starting Grabber."""
        self.start_grabber()

    def start_flux(self):
        """Starts ApexBOSFlux in its own terminal."""
        if self.is_running("flux"):
            self.set_toast("ApexBOSFlux is already running.")
            return
        args = ["--config", self.config_path, "--display"]
        p = spawn_in_new_terminal("ApexBOSFlux Relay", "ApexBOSFlux.py", args, cwd=self.work_dir)
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
        p = spawn_in_new_terminal(title, "ApexBOS.220.py", args, cwd=self.work_dir)
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
        p = spawn_in_new_terminal("ApexBOSBanc Cloud Server", "ApexBOSBanc.py", args, cwd=self.work_dir)
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
        p = spawn_in_new_terminal("ApexBOSSpy Rule Inspector", "ApexBOSSpy.py", args, cwd=self.work_dir)
        if p:
            self.procs["spy"] = p
            self.set_toast("Launched ApexBOSSpy.")

    def start_packfig(self):
        """Starts PackFIG in a terminal."""
        p = spawn_in_new_terminal("PackFIG Binary Decoder", "PackFIG.py", [], cwd=self.work_dir)
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
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        self._handle_click(event.pos)
                    elif event.button == 3:
                        self._handle_right_click(event.pos)

            self._draw()
            pygame.display.flip()

        pygame.quit()

    def _handle_click(self, pos):
        for btn_key, entry in list(self.buttons.items()):
            rect = entry[0]
            action_fn = entry[1]
            if rect.collidepoint(pos):
                if action_fn:
                    action_fn()
                break

    def _handle_right_click(self, pos):
        for btn_key, entry in list(self.buttons.items()):
            rect = entry[0]
            if rect.collidepoint(pos):
                cmd_key = getattr(entry, "cmd_key", btn_key)
                label = getattr(entry, "label", btn_key)
                self.copy_command_to_clipboard(cmd_key, label)
                break

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

        hint_txt = "[Tip: Right-click any button or click [CMD] to copy manual CLI command]"
        lbl_hint = self.font_tiny.render(hint_txt, True, (110, 175, 235))
        self.screen.blit(lbl_hint, (self.width - lbl_hint.get_width() - 25, 66))

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
            ("spy", "ApexBOSSpy Packet Inspector", self.mgr.start_spy),
            ("packfig", "PackFIG Binary Decoder", self.mgr.start_packfig),
            ("wizard", "Re-Run Setup Wizard", self._run_wizard_external),
            ("reload", "Reload config.json", self.mgr.reload_config),
        ]
        ux = 25
        ubtn_w = (self.width - 50 - 3 * 12) // 4
        for ukey, utext, ufn in ut_btns:
            urect = pygame.Rect(ux, y, ubtn_w, 32)
            pygame.draw.rect(self.screen, COLOR_BTN_BG, urect, border_radius=4)
            pygame.draw.rect(self.screen, COLOR_BORDER, urect, 1, border_radius=4)
            ulbl = self.font_sm.render(utext, True, COLOR_TEXT)
            self.screen.blit(ulbl, ulbl.get_rect(center=urect.center))
            self.buttons[ukey] = ButtonEntry(urect, ufn, cmd_key=ukey, label=utext)
            ux += ubtn_w + 12

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
            mx += ubtn_w + 12

        # Bottom Toast / Notification Bar
        pygame.draw.rect(self.screen, (10, 14, 20), (0, self.height - 35, self.width, 35))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, self.height - 35), (self.width, self.height - 35), 1)
        toast_lbl = self.font_sm.render(self.mgr.status_toast, True, COLOR_ACCENT)
        self.screen.blit(toast_lbl, (20, self.height - 25))

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
