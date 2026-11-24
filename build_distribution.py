"""
build_distribution.py - ApexBOS Release Distribution Packager (v26.1113.01)
Copyright (c) 2026 AB. Licensed under the MIT License.

Packages the ApexBOS repository into a clean, standalone, cross-platform
distribution ZIP archive for Windows, Linux, and macOS.
Sets POSIX execution permissions (0755) on Unix launchers and calculates
cryptographic SHA-256 checksums.
"""

import os
import sys
import glob
import json
import zipfile
import hashlib
import tempfile
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
from datetime import datetime
from typing import Tuple, Optional, Set, List, Dict

import config_loader
from version import VERSION

CORE_SCRIPTS = [
    "ApexBOSGrabber.py",
    "ApexBOSScanner.py",
    "ApexBOSChroma.py",
    "ApexBOSFlux.py",
    "dispatcher_engine.py",
    "View_ApexBOS.220.py",
    "ApexBOSCreator.py",
    "ApexBOSBanc.py",
    "PackFIG.py",
    "ApexBOSSpy.py",
    "ApexBOS.220.py",
    "mosfet_client.py",
    "mosfet_auth.py",
    "parse_packets.py",
    "train_logic.py",
    "emp_decoder.py",
    "emp_phy.py",
    "itcvl_converter.py",
    "atcs_converter.py",
    "version.py",
    "banc_coverage_modal.py",
    "config_loader.py",
    "install_utils.py",
]

LAUNCHER_FILES = [
    "setup.bat",
    "setup.command",
    "setup.sh",
    "setup_wizard.py",
    "ApexBOSLauncher.py",
    "update.py",
    "uninstall.py",
    "update.bat",
    "update.sh",
    "uninstall.bat",
    "uninstall.sh",
    "start_banc.sh",
    "enable_firewall.bat",
    "enable_firewall.ps1",
    "requirements.txt",
]

DATA_FILES = [
    "signals_default.json",
    "ptc_status_parser.json",
    "README.md",
    "GRABBER_README.md",
    "SCANNER_README.md",
    "CHROMA_README.md",
    "BANC_README.md",
    "FLUX_README.md",
    "SPY_README.md",
    "PACKFIG_README.md",
    "ENGINE_README.md",
    "VIEWER_README.md",
    "CREATOR_README.md",
    "LICENSE",
    "PRIVACY.md",
    "thanks.txt",
    "prefs_arrow_layout.json",
]

PARSER_RULE_FILES = [
    "config/x33_rule.json",
    "config/x2_base_rules.json",
    "config/x01_rules.json",
    "config/x05_rules.json",
    "config/x70_rules.json",
    "config/x06_rules.json",
    "config/x04_rules.json",
]

EXCLUDED_EXACT_FILES = {
    "channels.json",
    "complex_layout.json",
    "gps_selector.py",
    "signals_220.json",
    "emp_c_demod.py",
    "whitelist.txt",
    "config.json",
    "MosFET.json",
    "banc.json",
    "config/MosFET.json",
    "config/banc.json",
    "config/mosfet_client.json",
}

FORBIDDEN_SECRET_STRINGS = (
    "OROS_SECRET_KEY_123",
    "9dLFilOswBAP6-nVh3W_L1ebteCClg4c4PnA4TJ8mrc=",
)

EXECUTABLE_SCRIPTS = {"setup.sh", "setup.command", "update.sh", "uninstall.sh", "start_banc.sh"}

ALLOWED_NEW_DIST_FILES = {
    "ApexBOSScanner.py",
    "ApexBOSChroma.py",
    "SCANNER_README.md",
    "CHROMA_README.md",
    "install_utils.py",
    "update.py",
    "uninstall.py",
    "update.bat",
    "update.sh",
    "uninstall.bat",
    "uninstall.sh",
    "start_banc.sh",
    "enable_firewall.bat",
    "enable_firewall.ps1",
    "PRIVACY.md",
    "atcs_converter.py",
}

GENERATED_DIST_FILES = {
    config_loader.CONFIG_DEFAULTS_FILENAME,
    "MANIFEST.json",
}


def compute_sha256(filepath: str) -> str:
    """Calculates SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_git_tracked_files() -> Set[str]:
    """Returns normalized relative paths of all files tracked by git."""
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
        )
        files = {line.strip().replace("\\", "/") for line in res.stdout.splitlines() if line.strip()}
        return files
    except Exception as exc:
        print(f"  [WARN] Failed to query git ls-files: {exc}")
        return set()


def collect_package_files() -> List[str]:
    """
    Collects and filters files to package:
    - Includes core scripts, launchers, data files
    - Includes layouts/arrow_layout.json
    - Includes config/signals*.json
    - Includes the 7 parser rules
    - Strictly excludes channels.json, complex_layout.json, gps_selector.py, signals_220.json
    - Strictly excludes *.itcvl and all files inside frags/
    - Strictly excludes any file not tracked as a git object (except generated config.json)
    """
    git_files = get_git_tracked_files()

    candidates: Set[str] = set()
    candidates.update(CORE_SCRIPTS)
    candidates.update(LAUNCHER_FILES)
    candidates.update(DATA_FILES)
    candidates.add("layouts/arrow_layout.json")
    candidates.update(PARSER_RULE_FILES)

    # Add layouts/*.json
    layout_files = glob.glob(os.path.join("layouts", "*.json"))
    for lf in layout_files:
        candidates.add(lf.replace("\\", "/"))

    # Add config/signals*.json
    config_signals = glob.glob(os.path.join("config", "signals*.json"))
    for cs in config_signals:
        candidates.add(cs.replace("\\", "/"))

    filtered: List[str] = []
    for f in sorted(candidates):
        norm = os.path.normpath(f).replace("\\", "/")
        base = os.path.basename(norm)

        # 1. Check exact exclusions
        if base in EXCLUDED_EXACT_FILES or norm in EXCLUDED_EXACT_FILES:
            continue

        # 2. Exclude ITCVL / ATCS .lay files and files inside frags/
        if norm.endswith(".itcvl") or norm.endswith(".lay") or norm.startswith("frags/"):
            continue

        # 3. Exclude test files, temp files, pycache, and databases
        if base.startswith("test_") or base.endswith(".pyc") or base.endswith(".db"):
            continue

        # 4. Strictly exclude anything that is not a git object (allow new release dist scripts)
        if git_files and norm not in git_files and norm not in ALLOWED_NEW_DIST_FILES:
            print(f"  [SKIP Non-Git Object] {norm}")
            continue

        if os.path.exists(norm):
            filtered.append(norm)
        else:
            print(f"  [WARN] File '{norm}' not found, skipping.")

    return filtered


def build_distribution_zip(
    output_dir: str = "dist",
    include_wheels: bool = False,
    base_folder_name: str = "ApexBOS.220",
) -> Tuple[str, str]:
    """
    Builds the distribution ZIP archive and its SHA-256 checksum file.
    Returns (zip_path, sha256_path).
    """
    os.makedirs(output_dir, exist_ok=True)
    zip_filename = f"ApexBOS-{VERSION}.zip"
    zip_path = os.path.join(output_dir, zip_filename)
    folder_prefix = base_folder_name or "ApexBOS.220"

    print(f"[*] Packaging ApexBOS {VERSION} -> {zip_path}")
    files_to_pack = collect_package_files()

    manifest_files: Dict[str, str] = {}

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 1. Package verified files
        for fname in files_to_pack:
            arcname = f"{folder_prefix}/{fname}".replace("\\", "/")
            zinfo = zipfile.ZipInfo(arcname)
            zinfo.date_time = datetime.now().timetuple()[:6]

            # Set POSIX permission bits (0755 for unix scripts, 0644 for regular files)
            base_fname = os.path.basename(fname)
            if base_fname in EXECUTABLE_SCRIPTS:
                zinfo.external_attr = (0o755 << 16) | 0o100000
            else:
                zinfo.external_attr = (0o644 << 16) | 0o100000

            with open(fname, "rb") as fp:
                data = fp.read()
            zf.writestr(zinfo, data)
            manifest_files[fname.replace("\\", "/")] = hashlib.sha256(data).hexdigest()

        # 2. Generate clean config.defaults.json (no secrets)
        defaults_bytes = (
            json.dumps(config_loader.generate_template_config(), indent=2) + "\n"
        ).encode("utf-8")
        defaults_arc = f"{folder_prefix}/{config_loader.CONFIG_DEFAULTS_FILENAME}".replace("\\", "/")
        dinfo = zipfile.ZipInfo(defaults_arc)
        dinfo.date_time = datetime.now().timetuple()[:6]
        dinfo.external_attr = (0o644 << 16) | 0o100000
        zf.writestr(dinfo, defaults_bytes)
        manifest_files[config_loader.CONFIG_DEFAULTS_FILENAME] = hashlib.sha256(defaults_bytes).hexdigest()

        # 3. Layout fragments directory: create empty frags/ folder entry ONLY
        frags_entry = zipfile.ZipInfo(f"{folder_prefix}/frags/")
        frags_entry.date_time = datetime.now().timetuple()[:6]
        frags_entry.external_attr = (0o755 << 16) | 0o040000  # directory attribute
        zf.writestr(frags_entry, "")

        # 4. Optional offline wheels directory
        if include_wheels and os.path.exists("wheels"):
            print("  [*] Bundling offline wheels/ directory...")
            for w in os.listdir("wheels"):
                if w.endswith(".whl"):
                    src = os.path.join("wheels", w)
                    rel_w = f"wheels/{w}".replace("\\", "/")
                    dest = f"{folder_prefix}/{rel_w}".replace("\\", "/")
                    with open(src, "rb") as wf:
                        wdata = wf.read()
                    winfo = zipfile.ZipInfo(dest)
                    winfo.date_time = datetime.now().timetuple()[:6]
                    winfo.external_attr = (0o644 << 16) | 0o100000
                    zf.writestr(winfo, wdata)
                    manifest_files[rel_w] = hashlib.sha256(wdata).hexdigest()

        # 5. Write MANIFEST.json with SHA-256 of every packaged file
        manifest_obj = {
            "version": VERSION,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "files": manifest_files,
        }
        manifest_bytes = (json.dumps(manifest_obj, indent=2) + "\n").encode("utf-8")
        minfo = zipfile.ZipInfo(f"{folder_prefix}/MANIFEST.json")
        minfo.date_time = datetime.now().timetuple()[:6]
        minfo.external_attr = (0o644 << 16) | 0o100000
        zf.writestr(minfo, manifest_bytes)

    # 4. Generate SHA-256 Checksum file
    checksum = compute_sha256(zip_path)
    sha_path = f"{zip_path}.sha256"
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{checksum}  {zip_filename}\n")

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[+] Built {zip_filename} ({size_mb:.2f} MB)")
    print(f"    SHA256: {checksum}")
    print(f"    Checksum: {sha_path}")
    return zip_path, sha_path


def verify_distribution_zip(zip_path: str) -> bool:
    """Verifies that the ZIP archive is valid, contains all required assets, and enforces exclusions."""
    print(f"[*] Verifying package integrity: {zip_path}")
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(td)
            all_names = zf.namelist()

        root_entries = os.listdir(td)
        if not root_entries:
            raise ValueError("ZIP extracted to an empty directory!")

        extracted_dir = os.path.join(td, root_entries[0])

        # 1. Verify required launcher and entry point files
        required_files = [
            "setup.bat",
            "setup.command",
            "setup.sh",
            "setup_wizard.py",
            "ApexBOSLauncher.py",
            "install_utils.py",
            "update.py",
            "uninstall.py",
            "update.bat",
            "update.sh",
            "uninstall.bat",
            "uninstall.sh",
            "start_banc.sh",
            config_loader.CONFIG_DEFAULTS_FILENAME,
            "MANIFEST.json",
            "requirements.txt",
            "ApexBOSGrabber.py",
            "ApexBOSFlux.py",
            "View_ApexBOS.220.py",
            "dispatcher_engine.py",
            "ApexBOSCreator.py",
            "ApexBOSBanc.py",
            "ApexBOSSpy.py",
            "PackFIG.py",
        ]
        for req in required_files:
            p = os.path.join(extracted_dir, req)
            if not os.path.isfile(p):
                raise FileNotFoundError(f"Missing required distribution file in ZIP: {req}")

        # 2. Verify layouts/arrow_layout.json, config/signals_340.json, and the 7 rules
        required_config_files = [
            "layouts/arrow_layout.json",
            "config/signals_340.json",
            "config/x33_rule.json",
            "config/x2_base_rules.json",
            "config/x01_rules.json",
            "config/x05_rules.json",
            "config/x70_rules.json",
            "config/x06_rules.json",
            "config/x04_rules.json",
        ]
        for req in required_config_files:
            p = os.path.join(extracted_dir, req)
            if not os.path.isfile(p):
                raise FileNotFoundError(f"Missing required config file in ZIP: {req}")

        # 3. Verify frags/ directory exists and is EMPTY
        frags_dir = os.path.join(extracted_dir, "frags")
        if not os.path.isdir(frags_dir):
            raise FileNotFoundError("Missing empty 'frags/' folder in ZIP extraction!")
        frags_contents = os.listdir(frags_dir)
        if len(frags_contents) > 0:
            raise ValueError(f"'frags/' directory must be empty, but contains: {frags_contents}")

        # 4. Verify strictly forbidden files do not exist
        git_files = get_git_tracked_files()
        for name in all_names:
            base = os.path.basename(name.rstrip("/"))

            if base in EXCLUDED_EXACT_FILES:
                raise ValueError(f"Forbidden file found in ZIP: {name}")

            if name.endswith(".itcvl"):
                raise ValueError(f"Forbidden ITCVL file found in ZIP: {name}")

            # Check that frags contains no items
            if "/frags/" in name and not name.endswith("/frags/"):
                raise ValueError(f"Forbidden file inside frags/ in ZIP: {name}")

            # Check that test files are not present
            if base.startswith("test_"):
                raise ValueError(f"Forbidden test file found in ZIP: {name}")

            # Verify git object status
            if not name.endswith("/"):
                # Strip root prefix (e.g. "ApexBOS-v26.1113.01/")
                rel_in_repo = "/".join(name.split("/")[1:])
                if (
                    rel_in_repo
                    and git_files
                    and rel_in_repo not in git_files
                    and rel_in_repo not in ALLOWED_NEW_DIST_FILES
                    and rel_in_repo not in GENERATED_DIST_FILES
                    and not rel_in_repo.startswith("wheels/")
                ):
                    raise ValueError(f"Non-git object found in ZIP: {name} ({rel_in_repo})")

        # 5. Verify no secrets are packaged in any JSON file and config.defaults.json has empty secrets
        defaults_path = os.path.join(extracted_dir, config_loader.CONFIG_DEFAULTS_FILENAME)
        with open(defaults_path, "r", encoding="utf-8") as df:
            defaults_cfg = json.load(df)
        if defaults_cfg.get("banc", {}).get("auth_secret") not in ("", None):
            raise ValueError("config.defaults.json must not contain a non-empty auth_secret")
        if defaults_cfg.get("banc", {}).get("encryption_key") not in ("", None):
            raise ValueError("config.defaults.json must not contain a non-empty encryption_key")

        for root, _, files in os.walk(extracted_dir):
            for fn in files:
                if fn.endswith(".json"):
                    fpath = os.path.join(root, fn)
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as jf:
                        txt = jf.read()
                    for secret in FORBIDDEN_SECRET_STRINGS:
                        if secret in txt:
                            raise ValueError(f"Hardcoded secret found in packaged file: {fpath}")

        # 6. Verify MANIFEST.json integrity
        manifest_path = os.path.join(extracted_dir, "MANIFEST.json")
        with open(manifest_path, "r", encoding="utf-8") as mf:
            manifest_data = json.load(mf)
        for rel_path, expected_sha in (manifest_data.get("files") or {}).items():
            full_path = os.path.join(extracted_dir, rel_path)
            if not os.path.isfile(full_path):
                raise FileNotFoundError(f"MANIFEST entry missing from ZIP: {rel_path}")
            actual_sha = compute_sha256(full_path)
            if actual_sha != expected_sha:
                raise ValueError(f"MANIFEST SHA-256 mismatch for {rel_path}")

    print("[+] Archive verification passed: all entry points, rules, layouts verified; exclusions enforced.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ApexBOS Release Distribution Packager")
    parser.add_argument("--output-dir", default="dist", help="Output directory (default: dist/)")
    parser.add_argument("--include-wheels", action="store_true", help="Bundle wheels/ directory for offline installs")
    parser.add_argument("--folder-name", default="ApexBOS.220", help="Top-level folder name inside ZIP (default: ApexBOS.220)")
    parser.add_argument("--no-verify", action="store_true", help="Skip archive verification step")
    args = parser.parse_args()

    out_zip, out_sha = build_distribution_zip(
        output_dir=args.output_dir,
        include_wheels=args.include_wheels,
        base_folder_name=args.folder_name,
    )
    if not args.no_verify:
        verify_distribution_zip(out_zip)
