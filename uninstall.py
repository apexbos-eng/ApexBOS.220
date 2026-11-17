#!/usr/bin/env python3
"""
uninstall.py - ApexBOS Uninstaller (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Removes an ApexBOS installation cleanly on Windows, Linux and macOS:
  1. stops running ApexBOS applications;
  2. removes the Grabber / Flux start-at-boot services (Task Scheduler / systemd / LaunchAgents);
  3. removes Desktop / application shortcuts that point to this installation;
  4. optionally uninstalls the Python packages the setup wizard installed (recorded in install_record.json);
  5. removes the program files - either keeping your data (config.json, databases, layouts, frags/, prefs,
     backups) or everything (--purge).

  python uninstall.py                 interactive
  python uninstall.py --keep-data -y  unattended, keep configuration and data
  python uninstall.py --purge -y      unattended, delete the whole installation folder
  python uninstall.py --dry-run       show what would be done
A git working copy (developer checkout) is never purged.
"""

import os
import sys
import shutil
import argparse
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
from typing import List, Dict, Any

import install_utils as iu

INSTALL_DIR = iu.INSTALL_DIR

# Fallback list used when no manifest exists (installs from before manifests were shipped).
FALLBACK_GLOBS = ("*.py", "*.bat", "*.sh", "*.command")
FALLBACK_FILES = ("requirements.txt", "README.md", "GRABBER_README.md", "LICENSE", "PRIVACY.md", "thanks.txt",
                  "signals_default.json", "ptc_status_parser.json", "config.defaults.json", iu.MANIFEST_NAME)
GENERATED_FILES = (iu.LOCAL_MANIFEST_NAME,)


def program_files() -> List[str]:
    """Relative paths of shipped program files (never user data)."""
    manifest = iu.load_json(os.path.join(INSTALL_DIR, iu.LOCAL_MANIFEST_NAME), {}) or {}
    rels = set(manifest.get("files", {}).keys())
    if not rels:
        import fnmatch
        for fn in os.listdir(INSTALL_DIR):
            if os.path.isfile(os.path.join(INSTALL_DIR, fn)) and (
                    fn in FALLBACK_FILES or any(fnmatch.fnmatch(fn, g) for g in FALLBACK_GLOBS)):
                rels.add(fn)
        cfg_dir = os.path.join(INSTALL_DIR, "config")
        if os.path.isdir(cfg_dir):
            for fn in os.listdir(cfg_dir):
                rels.add("config/" + fn)
    return sorted(r for r in rels if not iu.is_user_owned(r))


def remove_program_files(dry_run: bool) -> Dict[str, Any]:
    manifest = (iu.load_json(os.path.join(INSTALL_DIR, iu.LOCAL_MANIFEST_NAME), {}) or {}).get("files", {})
    removed, kept = [], []
    for rel in program_files():
        p = os.path.join(INSTALL_DIR, rel)
        if not os.path.isfile(p):
            continue
        # Customised data files (config/*.json you edited) are kept
        if iu.is_managed_data(rel) and manifest.get(rel) and iu.sha256_file(p) != manifest[rel]:
            kept.append(rel)
            continue
        if not dry_run:
            try:
                os.remove(p)
            except OSError:
                kept.append(rel)
                continue
        removed.append(rel)
    if not dry_run:
        for root, dirs, _files in os.walk(INSTALL_DIR):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    dirs.remove(d)
        for fn in os.listdir(INSTALL_DIR):
            if fn.endswith(".new") or fn.endswith(".update_tmp"):
                try:
                    os.remove(os.path.join(INSTALL_DIR, fn))
                except OSError:
                    pass
        # remove now-empty directories (bottom-up)
        for root, dirs, files in os.walk(INSTALL_DIR, topdown=False):
            if root != INSTALL_DIR and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    return {"removed": removed, "kept": kept}


def uninstall_packages(packages: List[str], dry_run: bool) -> List[str]:
    done = []
    for pkg in packages:
        if dry_run:
            done.append(pkg)
            continue
        r = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", pkg],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if r.returncode == 0:
            done.append(pkg)
    return done


def delete_install_dir(dry_run: bool) -> bool:
    """Removes the whole installation folder (purge). On Windows falls back to a delayed external delete."""
    parent = os.path.dirname(INSTALL_DIR)
    if dry_run:
        return True
    os.chdir(parent)
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    if not os.path.exists(INSTALL_DIR):
        return True
    if sys.platform == "win32":
        # A console window may still have the folder as its working directory: delete after we exit.
        cmd = f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{INSTALL_DIR}"'
        subprocess.Popen(["cmd.exe", "/c", cmd], creationflags=0x08000000 | 0x00000008, close_fds=True)
        print(f"    The folder is in use; it will be deleted a few seconds after this window closes.")
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="ApexBOS uninstaller")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--keep-data", action="store_true", help="Remove program files, keep config.json / databases / layouts / prefs")
    mode.add_argument("--purge", action="store_true", help="Delete the entire installation folder including your data")
    ap.add_argument("--remove-packages", action="store_true", help="Also pip-uninstall the packages ApexBOS setup installed")
    ap.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be done without changing anything")
    args = ap.parse_args()

    print("=" * 70)
    print(f"  ApexBOS Uninstaller - {INSTALL_DIR}")
    print("=" * 70)

    if not iu.looks_like_install():
        print("[!] This folder does not look like an ApexBOS installation. Aborting.")
        return 2

    dev = iu.is_dev_checkout()
    if dev:
        print("[i] This is a git working copy (developer checkout): program files and data will NOT be removed.")
        print("    Only services, shortcuts and (optionally) packages are cleaned up.")

    # Choose mode
    purge = args.purge
    keep_data = args.keep_data
    if not dev and not (purge or keep_data):
        if args.yes:
            keep_data = True
        else:
            print("\nWhat do you want to remove?")
            print("  [1] ApexBOS program files, KEEP my configuration and data (config.json, databases, layouts)")
            print("  [2] EVERYTHING, including my configuration, databases and layouts (deletes this folder)")
            print("  [3] Cancel")
            choice = input("Select [1-3] (default 1): ").strip() or "1"
            if choice == "2":
                purge = True
            elif choice == "1":
                keep_data = True
            else:
                print("Cancelled.")
                return 0

    record = iu.load_json(os.path.join(INSTALL_DIR, iu.INSTALL_RECORD_NAME), {}) or {}
    packages = list(record.get("packages_installed", []))
    remove_packages = args.remove_packages
    if packages and not remove_packages and not args.yes:
        print(f"\nPython packages installed by ApexBOS setup: {', '.join(packages)}")
        remove_packages = iu.confirm("Uninstall these packages too? (other programs may use them)", False)

    if purge and not args.yes:
        print("\n[!] This will permanently delete the folder and everything in it:")
        print(f"    {INSTALL_DIR}")
        if input("Type YES to confirm: ").strip() != "YES":
            print("Cancelled.")
            return 0
    elif keep_data and not args.yes:
        if not iu.confirm("\nProceed with the uninstall?", True):
            print("Cancelled.")
            return 0

    dry = args.dry_run
    tag = "[dry-run] " if dry else ""

    # 1. Stop running applications
    procs = iu.list_apexbos_processes(include_launcher=True)
    if procs:
        print(f"\n{tag}Stopping {len(procs)} running ApexBOS process(es) ...")
        if not dry:
            iu.stop_processes(procs)
            import time
            time.sleep(1.0)

    # 2. Boot services
    import config_loader
    svc = config_loader.remove_boot_services(dry_run=dry)
    for item in svc["removed"]:
        print(f"{tag}Removed boot service: {item}")
    for err in svc["errors"]:
        print(f"[!] {err}")
    if not svc["removed"] and not svc["errors"]:
        print(f"{tag}No boot services were installed.")

    # 3. Shortcuts
    shortcut_candidates = list(dict.fromkeys(list(record.get("shortcuts", [])) + iu.known_shortcut_paths()))
    n_sc = 0
    for sc in shortcut_candidates:
        if os.path.isfile(sc) and (sc in record.get("shortcuts", []) or iu.shortcut_points_to(sc)):
            if not dry:
                try:
                    os.remove(sc)
                except OSError as exc:
                    print(f"[!] Could not remove shortcut {sc}: {exc}")
                    continue
            print(f"{tag}Removed shortcut: {sc}")
            n_sc += 1
    if not n_sc:
        print(f"{tag}No shortcuts to remove.")

    # 4. Python packages
    if remove_packages and packages:
        done = uninstall_packages(packages, dry)
        print(f"{tag}Uninstalled packages: {', '.join(done) if done else 'none'}")

    # 5. Files
    if dev:
        print("\nDeveloper checkout: files left in place.")
    elif purge:
        print(f"\n{tag}Deleting {INSTALL_DIR} ...")
        ok = delete_install_dir(dry)
        print(f"{tag}{'Done.' if ok else 'Could not delete everything - remove the folder manually.'}")
    else:
        res = remove_program_files(dry)
        print(f"\n{tag}Removed {len(res['removed'])} program file(s).")
        for k in res["kept"]:
            print(f"    kept (modified by you): {k}")
        if not dry:
            for rec in (iu.INSTALL_RECORD_NAME, iu.LOCAL_MANIFEST_NAME):
                p = os.path.join(INSTALL_DIR, rec)
                if os.path.isfile(p):
                    os.remove(p)
            left = [f for f in os.listdir(INSTALL_DIR)] if os.path.isdir(INSTALL_DIR) else []
            if left:
                print(f"    Your data remains in {INSTALL_DIR}: {', '.join(sorted(left)[:12])}{' ...' if len(left) > 12 else ''}")
            else:
                try:
                    os.chdir(os.path.dirname(INSTALL_DIR))
                    os.rmdir(INSTALL_DIR)
                except OSError:
                    pass

    print("\n[+] ApexBOS uninstall complete." if not dry else "\n[dry-run] Nothing was changed.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        rc = 1
    sys.exit(rc)
