"""Rules engine: merges the curated TV rules with the UAD-ng database and
classifies every installed package into tier + risk + keep status."""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RISK_RANK = {"safe": 0, "caution": 1, "expert": 2, "unsafe": 3}
RANK_RISK = {v: k for k, v in RISK_RANK.items()}

UAD_TIER_MAP = {
    "Google": "google_misc",
    "Misc": "vendor",
    "Oem": "vendor",
    "Aosp": "cruft",
    "Carrier": "vendor",
    "Pending": "unclassified",
}
UAD_RISK_MAP = {
    "Recommended": "safe",
    "Advanced": "caution",
    "Expert": "expert",
    "Unsafe": "unsafe",
}


def _short(text: str, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Rules:
    def __init__(self, data_dir: Path | None = None):
        data = data_dir or (ROOT / "data")
        self.tv = json.loads((data / "tv_rules.json").read_text())
        uad_path = data / "uad_lists.json"
        self.uad: dict = json.loads(uad_path.read_text()) if uad_path.exists() else {}

        self.tiers: dict = self.tv["tiers"]
        self.presets: list = self.tv["presets"]
        self.tuning: dict = self.tv["tuning"]
        self.guidance: list = self.tv["guidance"]
        self.vendors: dict = self.tv["vendors"]
        self.profiles: dict = self.tv.get("profiles", {})

        self.keep_hard: dict[str, dict] = {}
        self.keep_soft: dict[str, dict] = {}
        for item in self.tv["keep"]:
            (self.keep_soft if item.get("soft") else self.keep_hard)[item["pkg"]] = item

        self._heuristics = [(re.compile(fnmatch.translate(h["pattern"])), h)
                            for h in self.tv["heuristics"]]

    # ------------------------------------------------------------- classify --

    def vendor_of(self, pkg: str) -> str | None:
        for key, meta in self.vendors.items():
            if key == "google":
                continue
            for prefix in meta["match"]:
                if pkg.startswith(prefix):
                    return key
        if pkg.startswith(("com.google.", "com.android.")) or pkg == "android":
            return "google"
        return None

    def classify(self, pkg: str) -> dict:
        """-> tier, risk, label, desc, source, keep, blocked, deps..."""
        out = {
            "package": pkg, "tier": "unclassified", "risk": "expert",
            "label": pkg, "desc": "", "source": "none",
            "keep": False, "keep_reason": None, "soft_keep": False,
            "blocked": False, "dependencies": [], "needed_by": [], "vendor": self.vendor_of(pkg),
        }

        keep = self.keep_hard.get(pkg)
        if keep:
            out.update(tier="core", risk="unsafe", keep=True,
                       keep_reason=keep["reason"], label=_humanize(pkg),
                       desc=keep["reason"], source="keep-list",
                       vendor=out["vendor"] or "core")
            return out
        soft = self.keep_soft.get(pkg)
        if soft:
            out["soft_keep"] = True
            out["keep_reason"] = soft["reason"]

        entry = self.tv["packages"].get(pkg)
        if entry:
            out.update(tier=entry["tier"], risk=entry["risk"],
                       label=entry.get("label") or _humanize(pkg),
                       desc=entry.get("desc", ""), source=entry.get("source", "curated"),
                       blocked=bool(entry.get("blocked")),
                       dependencies=entry.get("deps", []), needed_by=entry.get("neededBy", []))
            return out

        uad = self.uad.get(pkg)
        if uad:
            removal = uad.get("removal") or "Expert"
            out.update(
                tier=UAD_TIER_MAP.get(uad.get("list", "Misc"), "vendor"),
                risk=UAD_RISK_MAP.get(removal, "expert"),
                label=_humanize(pkg),
                desc=_short(uad.get("description") or "Listed in the UAD-ng database.", 400),
                source="UAD-ng", blocked=False,
                dependencies=[d for d in (uad.get("dependencies") or []) if d],
                needed_by=[d for d in (uad.get("neededBy") or []) if d],
            )
            return out

        if out["vendor"] and out["vendor"] != "google":
            label = self.vendors[out["vendor"]]["label"]
            out.update(risk="expert", label=_humanize(pkg),
                       desc=f"Undocumented {label} package. Not in any public database - "
                            f"review it manually (open the details to see its permissions).",
                       source="vendor-prefix")
            return out

        for regex, h in self._heuristics:
            if regex.match(pkg):
                out.update(tier=h["tier"], risk=h["risk"], label=h["label"],
                           desc=h["desc"], source="heuristic")
                return out

        if pkg.startswith(("com.google.", "com.android.")) or pkg == "android":
            out.update(risk="caution", label=_humanize(pkg),
                       desc="Unclassified platform package - not in any public database. "
                            "Keep it unless you know what it does.",
                       source="unclassified")
        else:
            out.update(risk="expert", label=_humanize(pkg),
                       desc="Unclassified third-party/brand package - not in any public database.",
                       source="unclassified")
        return out

    # ------------------------------------------------------------- annotate --

    def annotate(self, installed: dict, vendor: dict, installed_only: bool = True) -> list[dict]:
        rows = []
        for pkg, meta in installed["packages"].items():
            if meta.get("uninstalled") and installed_only:
                continue
            c = self.classify(pkg)
            row = {**meta}
            row.update({
                "label": c["label"], "tier": c["tier"], "risk": c["risk"],
                "tier_label": self.tiers.get(c["tier"], {}).get("label", c["tier"]),
                "desc": c["desc"], "source": c["source"],
                "keep": c["keep"], "keep_reason": c["keep_reason"],
                "soft_keep": c["soft_keep"], "blocked": c["blocked"],
                "dependencies": c["dependencies"], "needed_by": c["needed_by"],
                "vendor": c["vendor"], "risk_rank": RISK_RANK[c["risk"]],
            })
            rows.append(row)
        rows.sort(key=lambda r: (r["tier"] != "ads", r["tier"] != "promo",
                                 r["risk_rank"], r["label"].lower()))
        return rows

    # ---------------------------------------------------------------- misc ---

    def recommended(self, rows: list[dict]) -> list[dict]:
        """The default 'select recommended' set for the UI."""
        return [r for r in rows
                if r["tier"] in ("ads", "promo", "cruft", "google_misc", "games")
                and r["risk_rank"] <= RISK_RANK["safe"]
                and not r["keep"] and not r["blocked"] and r["enabled"]]

    def resolve_preset(self, preset_id: str, rows: list[dict], vendor_key: str) -> dict:
        preset = next((p for p in self.presets if p["id"] == preset_id), None)
        if not preset:
            raise KeyError(f"unknown preset: {preset_id}")
        max_rank = RISK_RANK[preset.get("max_risk", "safe")]
        tiers = set(preset["tiers"])
        vendor_only = preset.get("vendor_only", False)
        selected = [
            r for r in rows
            if r["tier"] in tiers and r["risk_rank"] <= max_rank
            and not r["keep"] and not r["blocked"] and r["enabled"]
            and (not vendor_only or (r["vendor"] == vendor_key and r["vendor"] != "google"))
        ]
        # never include hard dependencies of kept things by accident: exclude protected
        return {"preset": preset, "packages": selected}

    def stats(self, rows: list[dict]) -> dict:
        tiers: dict[str, int] = {}
        risks: dict[str, int] = {}
        for r in rows:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
            risks[r["risk"]] = risks.get(r["risk"], 0) + 1
        return {
            "tiers": tiers, "risks": risks,
            "recommended": [r["package"] for r in self.recommended(rows)],
            "tier_meta": self.tiers,
        }

    def safety_check(self, pkg: str, row: dict | None = None) -> tuple[bool, str]:
        c = self.classify(pkg)
        if c["keep"]:
            return False, f"PROTECTED: {c['keep_reason']}"
        if c["blocked"]:
            return False, ("This package is protected by the OS (Fire OS/similar). "
                           "Block its domains at the router/DNS instead.")
        if c["risk"] == "unsafe":
            return False, "Marked UNSAFE by the public database - do not disable."
        return True, ""


def _humanize(pkg: str) -> str:
    tail = pkg.split(".")[-1]
    tail = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tail)
    return tail.replace("_", " ").strip().capitalize() or pkg
