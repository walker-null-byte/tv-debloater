"""MCP tools: let an agent drive TVDebloater through the same State the web UI uses.

Everything that changes the TV ends up in Actions.apply / undo_batch / apply_tuning,
so the keep-list, launcher and keyboard guards, the batch limit and the journal apply
exactly as they do in the UI. Expert actions (uninstall, expert-risk packages) are
deliberately not exposed: that consent belongs to a human.

No `from __future__ import annotations` here: TurboMCP builds each tool's JSON Schema
from the real annotation objects.
"""
import functools
import json
from typing import Literal

from turbomcp import TurboMCP

from . import __version__
from .actions import MAX_PER_BATCH
from .rules import RISK_RANK

INSTRUCTIONS = f"""\
TVDebloater disables unwanted packages (ads, telemetry, promo, vendor cruft) on an \
Android / Google TV over ADB. Everything it does is reversible.

Workflow
1. get_status. If no device is selected: discover_devices, then connect_device \
(pair_device first for Android 11+ wireless debugging). A device in state \
'unauthorized' means the user must accept the "Allow USB debugging?" prompt on the TV.
2. Decide what to change with list_presets / get_preset (preferred, start with the \
safest) or list_packages.
3. preview_changes, then apply_changes with at most {MAX_PER_BATCH} packages per call.
4. After each batch, tell the user what changed and ask them to check the TV (Inputs \
button, HDMI, Netflix, on-screen keyboard) before the next batch, unless they told you \
to keep going.
5. If anything misbehaves: undo_batch(batch_id). get_journal lists every batch.

The server enforces these rules; do not try to work around them: protected packages \
cannot be touched; the home launcher cannot be disabled unless an alternative launcher \
is installed and enabled; the only keyboard cannot be disabled; unclassified and \
expert-risk packages are out of reach; packages are only ever disabled or re-enabled, \
never uninstalled.
"""

DESC_MAX = 160       # characters of description per package in list-style results
LIST_MAX = 200       # most packages list_packages returns per call
PREVIEW_MAX = 50     # most packages preview_changes accepts per call (each costs adb round-trips)
SAFE_ACTIONS = ("disable", "enable")


def _packages(value) -> list[str]:
    """Validated, de-duplicated package list (order kept)."""
    if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) for p in value):
        raise ValueError("packages must be a list of package-name strings")
    return list(dict.fromkeys(p.strip() for p in value if p.strip()))


def _action(value) -> str:
    if value not in SAFE_ACTIONS:
        raise ValueError(f"action must be one of {list(SAFE_ACTIONS)} - uninstall and other "
                         "expert actions are not available over MCP")
    return value


def _row(r: dict) -> dict:
    return {
        "package": r["package"], "label": r["label"], "tier": r["tier"], "risk": r["risk"],
        "enabled": r["enabled"], "system": r["system"],
        "protected": bool(r["keep"] or r["blocked"]),  # can never be touched
        "guarded": bool(r["soft_keep"]),               # allowed only if the guards pass
        "desc": r["desc"][:DESC_MAX],
    }


def build_registry(app_state) -> TurboMCP:
    """Register the TVDebloater tools; `app_state` is app.State."""
    mcp = TurboMCP(name="tvdebloater", version=__version__)

    def tool(fn):
        """Register fn as a tool whose result is compact JSON (indentation costs tokens)."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return json.dumps(fn(*args, **kwargs), separators=(",", ":"),
                              ensure_ascii=False, default=str)
        mcp.register(wrapper)
        return fn

    # ---------------------------------------------------------- connection --

    @tool
    def get_status():
        """Start here. Reports whether adb works, which devices adb can see, which one is
        selected, and the device's model / Android version once connected."""
        s = app_state.status()
        s.pop("rules_meta", None)
        return s

    @tool
    def discover_devices():
        """Scan the local network (mDNS and a port scan) for TVs with ADB debugging on.
        Slow: can take 10-30 seconds."""
        return app_state.discover()

    @tool
    def connect_device(host: str, port: int = 5555):
        """Connect to a TV over the network and select it as the active device.

        Args:
            host: IP address or hostname of the TV
            port: ADB port (5555 for classic debugging, the 'main port' on the Wireless debugging screen for Android 11+)
        """
        return app_state.connect(host, int(port))

    @tool
    def pair_device(host: str, port: int, code: str):
        """Pair with a TV using Android 11+ Wireless debugging. Needs the 6-digit code shown
        on the TV's 'Pair device with pairing code' screen, which only the user can read.
        Afterwards call connect_device with the main port, which differs from the pairing port.

        Args:
            host: IP address of the TV
            port: the PAIRING port shown next to the code
            code: the 6-digit pairing code
        """
        return {"message": app_state.pair(host, port, code)}

    @tool
    def select_device(serial: str):
        """Select a device adb already knows about (e.g. over USB) as the active device.

        Args:
            serial: serial as listed by get_status
        """
        return app_state.select(serial)

    # ------------------------------------------------------------ packages --

    @tool
    def list_packages(search: str = "", tier: str = "", risk: str = "", status: str = "",
                      only_actionable: bool = False, limit: int = 100, offset: int = 0,
                      refresh: bool = False):
        """List the packages installed on the active TV, each classified by tier (ads, promo,
        cruft, vendor, streaming, core, ...) and removal risk (safe, caution, expert, unsafe).
        Ads and promo come first, then by risk. Returns a page plus counts for the whole TV.

        Args:
            search: only packages whose name or label contains this text
            tier: only this tier, e.g. ads, promo, cruft, vendor, streaming, core
            risk: only this risk level: safe, caution, expert or unsafe
            status: 'enabled' or 'disabled'
            only_actionable: only enabled packages that are not protected, i.e. candidates for disabling
            limit: page size, at most 200
            offset: how many matching packages to skip, for paging
            refresh: re-read the package list from the TV instead of using the short-lived cache
        """
        inv = app_state.get_inventory(refresh=bool(refresh))
        if tier and tier not in app_state.rules.tiers:
            raise ValueError(f"unknown tier '{tier}'; valid: {sorted(app_state.rules.tiers)}")
        if risk and risk not in RISK_RANK:
            raise ValueError(f"unknown risk '{risk}'; valid: {sorted(RISK_RANK)}")
        if status not in ("", "enabled", "disabled"):
            raise ValueError("status must be 'enabled' or 'disabled'")
        needle = (search or "").strip().lower()
        rows = [
            r for r in inv["rows"]
            if (not tier or r["tier"] == tier)
            and (not risk or r["risk"] == risk)
            and (not status or r["enabled"] == (status == "enabled"))
            and (not only_actionable or (r["enabled"] and not r["keep"] and not r["blocked"]))
            and (not needle or needle in r["package"].lower() or needle in r["label"].lower())
        ]
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), LIST_MAX)
        page = rows[offset:offset + limit]
        return {
            "serial": inv["serial"], "vendor": inv["vendor"]["key"], "counts": inv["counts"],
            "tiers": inv["stats"]["tiers"], "risks": inv["stats"]["risks"],
            "total_matching": len(rows), "offset": offset, "returned": len(page),
            "packages": [_row(r) for r in page],
        }

    @tool
    def get_package(package: str):
        """Everything known about one package: classification and why, dependencies, version,
        permissions, and whether it can be disabled right now (with the reason if not).

        Args:
            package: exact package name, e.g. com.google.android.tvrecommendations
        """
        info = app_state.package_info(package)
        info["detail"].pop("raw_head", None)
        return info

    # ------------------------------------------------------------- presets --

    @tool
    def list_presets():
        """List the cleanup presets with how many packages each would touch on this TV.
        Presets never apply anything by themselves; use get_preset to see the packages.
        Presets with max_risk 'expert' cannot be applied over MCP."""
        data = app_state.presets()
        return {
            "vendor": data["vendor"]["key"],
            "presets": [{"id": p["id"], "name": p["name"], "desc": p["desc"],
                         "max_risk": p.get("max_risk", "safe"), "package_count": p["_count"]}
                        for p in data["presets"]],
            "recommended_count": len(data["recommended"]),
        }

    @tool
    def get_preset(preset_id: str):
        """The packages a preset would disable on this TV (enabled, not protected, within the
        preset's risk limit). Apply them with apply_changes in batches.

        Args:
            preset_id: id from list_presets
        """
        data = app_state.preset(preset_id)
        pkgs = [{"package": p["package"], "label": p["label"], "tier": p["tier"],
                 "risk": p["risk"], "desc": p["desc"][:DESC_MAX]} for p in data["packages"]]
        return {"preset": {"id": data["preset"]["id"], "name": data["preset"]["name"],
                           "desc": data["preset"]["desc"]},
                "count": len(pkgs), "batch_size": MAX_PER_BATCH, "packages": pkgs}

    # ------------------------------------------------------------- changes --

    @tool
    def preview_changes(packages: list[str], action: Literal["disable", "enable"] = "disable"):
        """Dry run: shows exactly which adb commands apply_changes would run and which
        packages the safety rules refuse (with the reason). Changes nothing.

        Args:
            packages: package names, at most 50
            action: 'disable' (default) or 'enable'
        """
        pkgs = _packages(packages)
        if len(pkgs) > PREVIEW_MAX:
            raise ValueError(f"preview at most {PREVIEW_MAX} packages at a time")
        plan = app_state.apply(_action(action), pkgs, dry_run=True)
        return {
            "action": plan["action"], "max_batch": plan["max_batch"],
            "allowed": [{k: i[k] for k in ("package", "label", "risk", "command", "guard")
                         if k in i} for i in plan["allowed"]],
            "denied": [{"package": i["package"], "label": i["label"],
                        "reason": i["blocked_reason"]} for i in plan["denied"]],
        }

    @tool
    def apply_changes(packages: list[str], action: Literal["disable", "enable"] = "disable",
                      note: str = ""):
        """CHANGES THE TV. Disable (or re-enable) up to 10 packages in one journaled batch.
        Preview first. Packages the safety rules refuse are skipped and reported. Disabled apps
        stay on the TV and can be undone with undo_batch. After a successful batch, ask the
        user to check the TV before applying the next one.

        Args:
            packages: package names, at most 10 per call
            action: 'disable' (default) or 'enable'
            note: short reason, stored in the journal
        """
        action = _action(action)
        result = app_state.apply(action, _packages(packages), note=note or "")
        out = {
            "batch_id": result["batch_id"], "action": result["action"],
            "applied": result["applied"], "failed": result["failed"],
            "results": [{"package": r["package"], "label": r["label"], "ok": r["ok"],
                         **({"verified_disabled": r["verified_disabled"]}
                            if action == "disable" and "verified_disabled" in r else {}),
                         **({} if r["ok"] else {"output": r["output"]})}
                        for r in result["results"]],
            "denied": [{"package": i["package"], "reason": i["blocked_reason"]}
                       for i in result["denied"]],
        }
        if result["applied"]:
            out["next_step"] = ("Ask the user to check the TV (Inputs button, HDMI, Netflix, "
                                f"keyboard) before the next batch. Undo with "
                                f"undo_batch('{result['batch_id']}').")
        return out

    @tool
    def undo_batch(batch_id: str):
        """CHANGES THE TV. Reverse a batch from get_journal / apply_changes: re-enables what it
        disabled and vice versa, newest change first.

        Args:
            batch_id: id of the batch to reverse
        """
        result = app_state.undo(batch_id)
        return {"undo_batch_id": result["batch_id"], "applied": result["applied"],
                "failed": result["failed"],
                "results": [{"package": r["package"], "ok": r["ok"]} for r in result["results"]]}

    @tool
    def get_journal(limit: int = 10):
        """The log of every change made to this TV, newest batch first, with the batch ids
        undo_batch needs.

        Args:
            limit: how many batches to return
        """
        batches = app_state.journal.batches(app_state.serial)
        return {"batches": [
            {"batch_id": b["batch_id"], "ts": b["ts"], "action": b["action"], "note": b["note"],
             "device": b["device"], "ok": b["ok"], "failed": b["failed"],
             "packages": [i["package"] for i in b["items"]]}
            for b in batches[:max(1, int(limit))]], "total_batches": len(batches)}

    # -------------------------------------------------------------- tuning --

    @tool
    def list_tuning():
        """Optional non-package tweaks (e.g. shorter animations, cache trim) with the exact
        commands each one runs, plus short written guidance cards worth relaying to the user
        (launcher swap, DNS-level ad blocking, ...)."""
        return {
            "tuning": [{"id": tid, "name": t["name"], "desc": t["desc"],
                        "commands": [c["cmd"] for c in t["commands"]]}
                       for tid, t in app_state.rules.tuning.items()],
            "guidance": app_state.rules.guidance,
        }

    @tool
    def apply_tuning(tuning_id: str):
        """CHANGES THE TV. Run one tuning action from list_tuning. Journaled with an undo, so
        undo_batch reverses it.

        Args:
            tuning_id: id from list_tuning
        """
        result = app_state.apply_tuning(tuning_id)
        return {"batch_id": result["batch_id"], "applied": result["applied"],
                "results": [{"cmd": r["cmd"], "ok": r["ok"]} for r in result["results"]]}

    # ------------------------------------------------------ metrics, report --

    @tool
    def save_snapshot(label: str = "snapshot"):
        """Record RAM and package counts under a label, e.g. 'before' and later 'after', to
        measure what the cleanup achieved with compare_snapshots.

        Args:
            label: name for this snapshot
        """
        return app_state.save_snapshot((label or "snapshot").strip())

    @tool
    def compare_snapshots(before: str = "before", after: str = "after"):
        """Difference between two saved snapshots (RAM and package counts).

        Args:
            before: label of the earlier snapshot
            after: label of the later snapshot
        """
        return app_state.compare_snapshots(before, after)["diff"]

    @tool
    def generate_report():
        """Write a markdown report of every change plus a reports/undo.sh script that reverses
        them all, and return the report text and where both were saved."""
        return app_state.report()

    return mcp
