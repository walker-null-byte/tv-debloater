#!/usr/bin/env python3
"""
Build data/tv_rules.json from:
  1. data/curated_extra.json          (hand-curated rules - edit this one)
  2. data/raw_sources/tvtweak_*.conf  (26zl/tv-tweak device profiles)
  3. data/raw_sources/skinny_bravia_README.md (therealhoodboy/skinny-bravia)

Run:  python3 scripts/import_sources.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw_sources"

# ---------------------------------------------------------------- tv-tweak --

TVTWEAK_TIER_MAP = {
    "ads":        ("ads", "safe"),
    "promo":      ("promo", "safe"),
    "ota":        ("ota", "expert"),
    "cruft":      ("cruft", "safe"),
    "assistant":  ("assistant", "caution"),
    "cast":       ("cast", "caution"),
    "alexa":      ("assistant", "caution"),
    "smarthome":  ("vendor", "caution"),
    "aggressive": ("vendor", "expert"),
    "blocked":    ("core", "unsafe"),
}

BLOCKED_HINTS = (
    ("ota", ("ota", "updater")),
    ("ads", ("metrics", "crashmanager", "logmanager", "usagestats", "ftvads")),
)


def parse_tvtweak():
    packages, keep, profiles = {}, [], {}
    for conf in sorted(RAW.glob("tvtweak_*_packages.conf")):
        profile = conf.name.split("_devices_")[1].replace("_packages.conf", "")
        for raw_line in conf.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            tier, pkg = parts[0], parts[1]
            note = ""
            if len(parts) == 3 and parts[2].lstrip().startswith("#"):
                note = parts[2].lstrip("# ").strip()
            if tier not in TVTWEAK_TIER_MAP:
                print(f"  ! unknown tier {tier!r} in {conf.name}", file=sys.stderr)
                continue
            ftier, frisk = TVTWEAK_TIER_MAP[tier]
            blocked = tier == "blocked"
            if blocked:
                # re-tier blocked packages so the UI suggests the right workaround
                for t, hints in BLOCKED_HINTS:
                    if any(h in pkg for h in hints):
                        ftier = t
                        frisk = "safe"
                        break
            entry = {
                "tier": ftier,
                "risk": frisk,
                "desc": (note + (" " if note and not note.endswith(".") else "")).strip()
                        or "Flagged by the tv-tweak profile for this device family.",
                "source": f"tv-tweak:{profile}",
            }
            if blocked:
                entry["blocked"] = True
                entry["desc"] += " [Fire OS refuses to disable this package for the shell user - block it via router/DNS instead.]"
            packages[pkg] = entry
    for conf in sorted(RAW.glob("tvtweak_*_keep.conf")):
        profile = conf.name.split("_devices_")[1].replace("_keep.conf", "")
        for raw_line in conf.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            keep.append({"pkg": line, "reason": f"Never-disabled list of the {profile} tv-tweak profile."})
    for conf in sorted(RAW.glob("tvtweak_*_device.conf")):
        profile = conf.name.split("_devices_")[1].replace("_device.conf", "")
        meta = {}
        for raw_line in conf.read_text().splitlines():
            m = re.match(r'(\w+)="?(.*?)"?$', raw_line.strip())
            if m and not raw_line.strip().startswith("#"):
                meta[m.group(1)] = m.group(2)
        profiles[profile] = meta
    return packages, keep, profiles


# ---------------------------------------------------------- skinny-bravia --

BRAVIA_SECTIONS = {
    "Sony Bloatware":            ("vendor", "safe"),
    "Sony System Services":      ("vendor", "caution"),
    "Sony Enhanced Services":    ("vendor", "safe"),
    "Sony Diagnostics Services": ("ads", "safe"),
    "Sony Applications":         ("vendor", "safe"),
    "Sony Television Services":  ("vendor", "expert"),
    "Accessibility Services":    ("accessibility", "expert"),
    "Android Diagnostic Services": ("ads", "safe"),
    "Android System Services":   ("cruft", "caution"),
    "Google Applications":       ("google_misc", "safe"),
}


def parse_bravia():
    readme = RAW / "skinny_bravia_README.md"
    if not readme.exists():
        return {}
    text = readme.read_text()
    packages, section = {}, None
    for line in text.splitlines():
        m_sec = re.match(r"^### (.+)$", line)
        if m_sec:
            section = m_sec.group(1).strip()
            continue
        if section not in BRAVIA_SECTIONS:
            continue
        m_row = re.match(r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]*?)\s*\|\s*$", line)
        if not m_row:
            continue
        name, pkg, purpose = m_row.group(1).strip(), m_row.group(2).strip(), m_row.group(3).strip()
        if name.lower() in ("app name", "----", ""):
            continue
        tier, risk = BRAVIA_SECTIONS[section]
        packages[pkg] = {
            "tier": tier, "risk": risk, "label": name,
            "desc": purpose or f"Flagged by the skinny-bravia guide ({section}).",
            "source": "skinny-bravia",
        }
    return packages


# ------------------------------------------------------------------ merge --

def main():
    curated = json.loads((DATA / "curated_extra.json").read_text())
    tvtw_pkgs, tvtw_keep, profiles = parse_tvtweak()
    bravia_pkgs = parse_bravia()

    # priority: curated > tv-tweak > skinny-bravia
    packages = {}
    for stage in (bravia_pkgs, tvtw_pkgs, curated["packages"]):
        for pkg, entry in stage.items():
            prev = packages.get(pkg)
            merged = {**(prev or {}), **entry}
            if not entry.get("source") and prev:
                merged["source"] = prev.get("source")
            packages[pkg] = merged

    # keep list: curated first, then tv-tweak, dedup
    seen_keep, keep = set(), []
    for item in curated["keep"] + tvtw_keep:
        key = item["pkg"]
        if key in seen_keep:
            continue
        seen_keep.add(key)
        keep.append(item)

    # sanity checks
    problems = []
    for pkg in packages:
        if not re.match(r"^[A-Za-z0-9_.:+-]+$", pkg):
            problems.append(f"suspicious package name: {pkg}")
    for item in keep:
        pkg = item["pkg"]
        if item.get("soft"):
            continue
        if pkg in packages and packages[pkg].get("risk") not in ("expert", "unsafe"):
            problems.append(f"keep/{pkg} also listed as {packages[pkg]['risk']} in packages")

    out = {
        "meta": {
            "version": curated["meta"]["version"],
            "generated_by": "scripts/import_sources.py - do not edit by hand",
            "sources": [
                "github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation (UAD-ng) uad_lists.json",
                "github.com/26zl/tv-tweak (device profiles: " + ", ".join(sorted(profiles)) + ")",
                "github.com/therealhoodboy/skinny-bravia (Sony Bravia)",
                "tv.cobanov.dev (never-disable list)",
                "github.com/seun-novodev/android-tv-debloat-toolkit (TCL safe list)",
                "tvslim.app package catalogue (descriptions)",
                "hand-curated (data/curated_extra.json)",
            ],
            "counts": {"packages": len(packages), "keep": len(keep), "heuristics": len(curated["heuristics"])},
        },
        "tiers": curated["tiers"],
        "keep": keep,
        "packages": dict(sorted(packages.items())),
        "vendors": curated["vendors"],
        "heuristics": curated["heuristics"],
        "presets": curated["presets"],
        "tuning": curated["tuning"],
        "guidance": curated["guidance"],
        "profiles": profiles,
    }

    if problems:
        print("WARNINGS:")
        for p in problems:
            print(" -", p)
    dest = DATA / "tv_rules.json"
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"wrote {dest}  ({len(packages)} packages, {len(keep)} kept, "
          f"{len(out['profiles'])} tv-tweak profiles)")


if __name__ == "__main__":
    main()
