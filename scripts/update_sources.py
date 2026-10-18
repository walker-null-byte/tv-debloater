#!/usr/bin/env python3
"""Re-download the public upstream debloat databases and rebuild data/tv_rules.json.

Run:  python3 scripts/update_sources.sh   (or this file directly)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw_sources"

SOURCES = {
    ROOT / "data" / "uad_lists.json":
        "https://raw.githubusercontent.com/Universal-Debloater-Alliance/"
        "universal-android-debloater-next-generation/main/resources/assets/uad_lists.json",
    RAW / "tvtweak_firetv-stick-hd_packages.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/firetv-stick-hd/packages.conf",
    RAW / "tvtweak_firetv-stick-hd_keep.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/firetv-stick-hd/keep.conf",
    RAW / "tvtweak_firetv-stick-hd_device.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/firetv-stick-hd/device.conf",
    RAW / "tvtweak_sharp-4k-googletv_packages.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/sharp-4k-googletv/packages.conf",
    RAW / "tvtweak_sharp-4k-googletv_keep.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/sharp-4k-googletv/keep.conf",
    RAW / "tvtweak_sharp-4k-googletv_device.conf":
        "https://raw.githubusercontent.com/26zl/tv-tweak/main/devices/sharp-4k-googletv/device.conf",
    RAW / "skinny_bravia_README.md":
        "https://raw.githubusercontent.com/therealhoodboy/skinny-bravia/main/README.md",
}


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    for dest, url in SOURCES.items():
        print(f"==> {dest.relative_to(ROOT)}")
        proc = subprocess.run(["curl", "-sfL", "-o", str(dest), url], timeout=120)
        if proc.returncode != 0:
            print(f"    !! download failed ({url}) - keeping the existing copy", file=sys.stderr)
    print("==> rebuilding data/tv_rules.json")
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "import_sources.py")]).returncode


if __name__ == "__main__":
    sys.exit(main())
