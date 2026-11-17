#!/usr/bin/env python3
"""
update.py - ApexBOS In-Place Updater (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Applies a newer ApexBOS-vXX.zip to this installation - online or completely offline:

  python update.py                      auto-detect ApexBOS-v*.zip (cwd, install dir, parent, Downloads) or ask
  python update.py --zip C:\\ApexBOS-v26.1114.01.zip
  python update.py --rollback latest    restore the pre-update backup

What it does (stage 1, this file):
  * verifies the ZIP (SHA-256 sidecar if present, layout, version / no accidental downgrade);
  * stops running ApexBOS processes (Windows locks files in use);
  * backs up everything it replaces into backups/update_<old>_<timestamp>/ (enables --rollback);
  * installs new + changed program files; NEVER overwrites user data (config.json, *.db, prefs, frags/ ...);
  * customised data files (config/*.json ...) are replaced only if you did not modify them,
    otherwise your copy is kept and the new one is saved next to it as '<name>.new'.
Stage 2 (--post-update, run with the NEW code in a fresh interpreter):
  * deep-merges config.json with config.defaults.json (adds new keys, keeps all your values, backs up first);
  * installs newly required Python packages (offline from ./wheels if present, otherwise pip).
"""

import os
import re
import sys
import json
import shutil
import zipfile
import argparse
import glob
import subprocess
import tempfile
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import install_utils as iu

INSTALL_DIR = iu.INSTALL_DIR
BACKUPS_DIR = os.path.join(INSTALL_DIR, "backups")


# ---------------------------------------------------------------------------
# ZIP discovery & validation
# ---------------------------------------------------------------------------

def find_candidate_zips() -> List[str]:
    dirs = [os.getcwd(), INSTALL_DIR, os.path.dirname(INSTALL_DIR), os.path.join(os.path.expanduser("~"), "Downloads")]
    seen, out = set(), []
    for d in dirs:
        for p in glob.glob(os.path.join(d, "ApexBOS-v*.zip")):
            rp = os.path.abspath(p)
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    out.sort(key=lambda p: (iu.parse_version(re.sub(r"^.*ApexBOS-", "", os.path.basename(p))), os.path.getmtime(p)), reverse=True)
    return out


def pick_zip_dialog() -> Optional[str]:
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="Select ApexBOS update ZIP", filetypes=[("ZIP archives", "*.zip")])
        root.destroy()
        return path or None
    except Exception:
        return None


def locate_zip(args) -> Optional[str]:
    if args.zip:
        return os.path.abspath(os.path.expanduser(args.zip))
    cands = find_candidate_zips()
    if len(cands) == 1 and args.yes:
        return cands[0]
    if cands:
        print("Update packages found:")
        for i, c in enumerate(cands, 1):
            print(f"  [{i}] {c}")
        print("  [B] Browse...   [P] Enter a path")
        ans = input(f"Select [1-{len(cands)}/B/P] (default 1): ").strip().lower() or "1"
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            return cands[int(ans) - 1]
        if ans == "b":
            return pick_zip_dialog()
    elif not args.no_gui:
        print("No ApexBOS-v*.zip found automatically. Opening file chooser...")
        picked = pick_zip_dialog()
        if picked:
            return picked
    path = input("Path to the ApexBOS update ZIP: ").strip().strip('"')
    return os.path.abspath(os.path.expanduser(path)) if path else None


def verify_sidecar_checksum(zip_path: str) -> Tuple[bool, str]:
    side = zip_path + ".sha256"
    if not os.path.isfile(side):
        return True, "no .sha256 file next to the ZIP (integrity not checked)"
    try:
        expected = open(side, "r", encoding="utf-8").read().split()[0].strip().lower()
    except Exception as exc:
        return False, f"cannot read {side}: {exc}"
    actual = iu.sha256_file(zip_path)
    return (actual == expected), ("SHA-256 verified" if actual == expected else f"SHA-256 MISMATCH (expected {expected[:16]}..., got {actual[:16]}...)")


def inspect_zip(zf: zipfile.ZipFile) -> Dict[str, Any]:
    names = [n for n in zf.namelist() if not n.endswith("/")]
    for n in names:
        parts = n.replace("\\", "/").split("/")
        if n.startswith("/") or ".." in parts or (len(parts[0]) == 2 and parts[0].endswith(":")):
            raise ValueError(f"Unsafe path in ZIP: {n}")
    roots = {n.replace("\\", "/").split("/")[0] for n in names}
    # ZIP is either "<root>/<files>" or flat
    prefix = ""
    if len(roots) == 1 and all("/" in n.replace("\\", "/") for n in names):
        prefix = roots.pop() + "/"
    rel = {n: n.replace("\\", "/")[len(prefix):] for n in names}
    if "ApexBOSLauncher.py" not in rel.values() or "version.py" not in rel.values():
        raise ValueError("This ZIP is not an ApexBOS distribution (ApexBOSLauncher.py / version.py missing).")
    ver_name = next(n for n, r in rel.items() if r == "version.py")
    new_version = iu.read_version_text(zf.read(ver_name).decode("utf-8", "replace"))
    manifest = None
    man_name = next((n for n, r in rel.items() if r == iu.MANIFEST_NAME), None)
    if man_name:
        try:
            manifest = json.loads(zf.read(man_name).decode("utf-8"))
        except Exception:
            manifest = None
    return {"prefix": prefix, "rel": rel, "new_version": new_version, "manifest": manifest}


# ---------------------------------------------------------------------------
# Stage 1: stop apps, back up, install files
# ---------------------------------------------------------------------------

def _backup_file(rel_path: str, backup_dir: str):
    src = os.path.join(INSTALL_DIR, rel_path)
    if os.path.isfile(src):
        dst = os.path.join(backup_dir, rel_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def _write_file(rel_path: str, data: bytes, mode: Optional[int] = None):
    dst = os.path.join(INSTALL_DIR, rel_path)
    os.makedirs(os.path.dirname(dst) or INSTALL_DIR, exist_ok=True)
    tmp = dst + ".update_tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, dst)


def plan_and_apply(zf: zipfile.ZipFile, info: Dict[str, Any], backup_dir: str, dry_run: bool) -> Dict[str, List[str]]:
    old_manifest = (iu.load_json(os.path.join(INSTALL_DIR, iu.LOCAL_MANIFEST_NAME), {}) or {}).get("files", {})
    new_files = (info["manifest"] or {}).get("files", {})
    res: Dict[str, List[str]] = {"added": [], "updated": [], "unchanged": [], "kept_user": [], "conflicts": [], "removed": []}

    for zname, rel in sorted(info["rel"].items(), key=lambda kv: kv[1]):
        if rel in (iu.MANIFEST_NAME,):
            continue
        data = zf.read(zname)
        new_hash = iu.sha256_bytes(data)
        zinfo = zf.getinfo(zname)
        mode = (zinfo.external_attr >> 16) & 0o777 or None
        local = os.path.join(INSTALL_DIR, rel)
        exists = os.path.isfile(local)

        if iu.is_user_owned(rel):
            if exists:
                res["kept_user"].append(rel)
                continue
            if not dry_run:
                _write_file(rel, data, mode)
            res["added"].append(rel)
            continue

        if not exists:
            if not dry_run:
                _write_file(rel, data, mode)
            res["added"].append(rel)
            continue

        local_hash = iu.sha256_file(local)
        if local_hash == new_hash:
            res["unchanged"].append(rel)
            continue

        if iu.is_managed_data(rel) and old_manifest.get(rel) != local_hash:
            # User modified this data file (or no baseline known): keep theirs, offer the new one as .new
            if not dry_run:
                _write_file(rel + ".new", data, mode)
            res["conflicts"].append(rel)
            continue

        if not dry_run:
            _backup_file(rel, backup_dir)
            _write_file(rel, data, mode)
        res["updated"].append(rel)

    # Files shipped by the previous version but dropped by this one (only if the user never touched them)
    for rel, old_hash in old_manifest.items():
        if new_files and rel not in new_files and not iu.is_user_owned(rel):
            local = os.path.join(INSTALL_DIR, rel)
            if os.path.isfile(local) and iu.sha256_file(local) == old_hash:
                if not dry_run:
                    _backup_file(rel, backup_dir)
                    os.remove(local)
                res["removed"].append(rel)

    if not dry_run:
        manifest_out = info["manifest"] or {"version": info["new_version"], "files": {
            rel: iu.sha256_bytes(zf.read(z)) for z, rel in info["rel"].items() if rel != iu.MANIFEST_NAME}}
        iu.save_json(os.path.join(INSTALL_DIR, iu.LOCAL_MANIFEST_NAME), manifest_out)
    return res


def do_stage1(args) -> int:
    zip_path = locate_zip(args)
    if not zip_path or not os.path.isfile(zip_path):
        print(f"[!] Update ZIP not found: {zip_path}")
        return 2

    print(f"[*] Update package: {zip_path}")
    ok, msg = verify_sidecar_checksum(zip_path)
    print(f"    {msg}")
    if not ok and not args.force:
        print("[!] Aborting (use --force to ignore).")
        return 3

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if zf.testzip() is not None:
                print("[!] ZIP archive is corrupt.")
                return 3
            info = inspect_zip(zf)

            old_v = iu.installed_version() or "unknown"
            new_v = info["new_version"] or "unknown"
            print(f"[*] Installed version: {old_v}   ->   New version: {new_v}")
            cmp_old, cmp_new = iu.parse_version(old_v), iu.parse_version(new_v)
            if cmp_old and cmp_new and cmp_new < cmp_old and not args.force:
                print("[!] The ZIP is OLDER than the installed version. Use --force to downgrade.")
                return 4
            if cmp_old and cmp_new and cmp_new == cmp_old and not args.force:
                if not iu.confirm("Same version already installed. Re-apply it anyway?", False, args.yes):
                    return 0

            procs = [] if args.no_stop else iu.list_apexbos_processes()
            if procs and not args.dry_run:
                print("[!] ApexBOS applications are running:")
                for p in procs:
                    print(f"    PID {p['pid']}: {p['cmd'][:100]}")
                if iu.confirm("Stop them now? (required to update)", True, args.yes):
                    iu.stop_processes(procs)
                    import time as _t
                    _t.sleep(1.5)
                else:
                    print("[!] Update cancelled.")
                    return 5

            if not args.dry_run and not args.yes:
                if not iu.confirm(f"Install {new_v} into {INSTALL_DIR}? Your configuration and data are preserved.", True):
                    print("Cancelled.")
                    return 0

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_old = re.sub(r"[^0-9A-Za-z._-]", "_", old_v)
            backup_dir = os.path.join(BACKUPS_DIR, f"update_{safe_old}_{stamp}")
            if not args.dry_run:
                os.makedirs(backup_dir, exist_ok=True)
                cfg = os.path.join(INSTALL_DIR, "config.json")
                if os.path.isfile(cfg):
                    shutil.copy2(cfg, os.path.join(backup_dir, "config.json"))

            result = plan_and_apply(zf, info, backup_dir, args.dry_run)
    except (zipfile.BadZipFile, ValueError) as exc:
        print(f"[!] Invalid update package: {exc}")
        return 3

    if not args.dry_run:
        iu.save_json(os.path.join(backup_dir, "changes.json"), {"from": old_v, "to": new_v, **result})

    print("\n" + ("DRY RUN - nothing was changed.\n" if args.dry_run else ""))
    print(f"  added: {len(result['added'])}   updated: {len(result['updated'])}   unchanged: {len(result['unchanged'])}"
          f"   removed(obsolete): {len(result['removed'])}")
    print(f"  your data kept untouched: {len(result['kept_user'])} file(s)")
    for c in result["conflicts"]:
        print(f"  [!] You modified '{c}': kept YOUR copy; the new version is saved as '{c}.new' (merge by hand if wanted)")
    if not args.dry_run:
        print(f"  backup of replaced files: {backup_dir}   (undo: python update.py --rollback latest)")

    if args.dry_run:
        return 0

    # Stage 2 runs the NEW code in a fresh interpreter.
    cmd = [sys.executable, os.path.join(INSTALL_DIR, "update.py"), "--post-update"]
    if args.no_deps:
        cmd.append("--no-deps")
    if args.yes:
        cmd.append("--yes")
    rc = subprocess.call(cmd, cwd=INSTALL_DIR)
    return rc


# ---------------------------------------------------------------------------
# Stage 2: config migration + dependencies (runs with the new code)
# ---------------------------------------------------------------------------

def do_post_update(args) -> int:
    os.chdir(INSTALL_DIR)
    sys.path.insert(0, INSTALL_DIR)
    import config_loader

    print("\n[*] Migrating configuration ...")
    rep = config_loader.migrate_config_file(os.path.join(INSTALL_DIR, config_loader.CONFIG_FILENAME))
    st = rep.get("status")
    if st == "no_config":
        print("    No config.json yet - run setup (setup.bat / setup.sh / setup.command) to configure ApexBOS.")
    elif st == "current":
        print("    config.json is already up to date.")
    elif st == "migrated":
        print(f"    config.json updated {rep.get('old_version') or '?'} -> {rep.get('new_version')}; "
              f"{len(rep['added'])} new setting(s) added, all your values kept.")
        for a in rep["added"]:
            print(f"      + {a}")
        for n in rep.get("notes", []):
            print(f"      ! {n}")
        print(f"    backup: {rep.get('backup')}")
    else:
        print(f"    [!] Could not migrate config.json: {rep.get('error')}")

    installed_pkgs: List[str] = []
    if not args.no_deps:
        print("\n[*] Checking Python packages ...")
        import setup_wizard
        missing = setup_wizard.check_missing_dependencies()
        if not missing:
            print("    All required packages are installed.")
        wheels = os.path.join(INSTALL_DIR, "wheels")
        have_wheels = os.path.isdir(wheels) and any(f.endswith(".whl") for f in os.listdir(wheels))
        failed = []
        for pkg, _imp, _desc in missing:
            print(f"    Installing {pkg} ...")
            ok = False
            if have_wheels:
                ok = setup_wizard.install_package_stream(pkg, output_callback=lambda l: print(f"      {l}"), find_links=wheels)
            if not ok:
                ok = setup_wizard.install_package_stream(pkg, output_callback=lambda l: print(f"      {l}"))
            (installed_pkgs if ok else failed).append(pkg)
        if failed:
            print("\n[!] These packages could not be installed (offline?): " + ", ".join(failed))
            print("    On a connected PC run:  python -m pip download " + " ".join(failed) + " -d wheels")
            print("    copy the 'wheels' folder into this directory and run update.py --post-update again.")

        if installed_pkgs:
            rec_path = os.path.join(INSTALL_DIR, iu.INSTALL_RECORD_NAME)
            rec = iu.load_json(rec_path, {}) or {}
            rec["packages_installed"] = sorted(set(rec.get("packages_installed", [])) | set(installed_pkgs))
            rec["version"] = iu.installed_version()
            rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
            iu.save_json(rec_path, rec)

    print(f"\n[+] ApexBOS is now at {iu.installed_version()}.")
    return 0


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def do_rollback(target: str) -> int:
    if target == "latest":
        dirs = sorted(glob.glob(os.path.join(BACKUPS_DIR, "update_*")), key=os.path.getmtime, reverse=True)
        if not dirs:
            print("[!] No update backups found.")
            return 2
        target = dirs[0]
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        print(f"[!] Backup not found: {target}")
        return 2
    changes = iu.load_json(os.path.join(target, "changes.json"), {}) or {}
    procs = iu.list_apexbos_processes()
    if procs:
        iu.stop_processes(procs)
    restored = 0
    for root, _dirs, files in os.walk(target):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, target).replace("\\", "/")
            if rel == "changes.json":
                continue
            shutil.copy2(src, os.path.join(INSTALL_DIR, rel))
            restored += 1
    for rel in changes.get("added", []):
        if iu.is_user_owned(rel):
            continue
        p = os.path.join(INSTALL_DIR, rel)
        if os.path.isfile(p):
            os.remove(p)
    for rel in changes.get("conflicts", []):
        p = os.path.join(INSTALL_DIR, rel + ".new")
        if os.path.isfile(p):
            os.remove(p)
    print(f"[+] Rolled back to {changes.get('from', '?')}: restored {restored} file(s) from {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ApexBOS in-place updater (online or offline via ZIP)")
    ap.add_argument("--zip", help="Path to the ApexBOS-vXX.zip update package")
    ap.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    ap.add_argument("--force", action="store_true", help="Allow downgrade / re-apply / ignore checksum mismatch")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without touching anything")
    ap.add_argument("--no-stop", action="store_true", help="Do not look for / stop running ApexBOS processes")
    ap.add_argument("--no-deps", action="store_true", help="Skip Python package installation")
    ap.add_argument("--no-gui", action="store_true", help="Never open a file chooser dialog")
    ap.add_argument("--relaunch", action="store_true", help="Restart the Control Center when finished")
    ap.add_argument("--post-update", action="store_true", help="(internal) stage 2: migrate config + install packages")
    ap.add_argument("--rollback", metavar="DIR|latest", help="Restore files from an update backup")
    args = ap.parse_args()

    rc = 0
    try:
        if args.post_update:
            rc = do_post_update(args)
        elif args.rollback:
            rc = do_rollback(args.rollback)
        else:
            print("=" * 70)
            print(f"  ApexBOS Updater - installed: {iu.installed_version() or 'unknown'}")
            print("=" * 70)
            rc = do_stage1(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        rc = 1
    if sys.stdin and sys.stdin.isatty() and not args.yes and not args.post_update:
        try:
            input("\nPress Enter to close ...")
        except EOFError:
            pass
    if args.relaunch and not args.post_update:
        iu.launch_launcher_detached(INSTALL_DIR)
    return rc


if __name__ == "__main__":
    sys.exit(main())
