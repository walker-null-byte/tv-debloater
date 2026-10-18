"""Action engine: turns UI decisions into adb commands, with hard safety gates,
batching, verification and journaling. Disable (reversible) is the default;
uninstall is expert-mode only."""
from __future__ import annotations

import uuid

from .adb import Adb, AdbError
from .journal import Journal
from .rules import RISK_RANK, Rules

MAX_PER_BATCH = 10

# Home screens shipped by the platform - disabling any of these needs a
# replacement launcher installed & enabled first (else: black screen).
STOCK_LAUNCHERS = {
    "com.google.android.apps.tv.launcherx",   # Google TV Home
    "com.google.android.tvlauncher",          # Android TV Home
    "com.google.android.leanbacklauncher",    # Android TV Home (older)
    "com.amazon.tv.launcher",                 # Fire TV Home
}
ALT_LAUNCHERS = {
    "me.efesser.flauncher",                   # FLauncher
    "com.spocky.projengmenu",                 # Projectivy Launcher
    "ca.dstudio.atvlauncher.free",            # ATV Launcher
    "ca.studio.atvlauncher",
    "com.sideloadchannel.launcher3",          # Sideload Channel Launcher
    "org.cosinus.launchertv",                 # Cosinus
}

INVERSE_ACTION = {"disable": "enable", "enable": "disable",
                  "uninstall": "install-existing", "install-existing": "uninstall"}

ACTION_TEMPLATES = {
    # action -> (command template, undo template, needs_expert)
    "disable":          ("pm disable-user --user 0 {pkg}", "pm enable {pkg}", False),
    "enable":           ("pm enable {pkg}", "pm disable-user --user 0 {pkg}", False),
    "uninstall":        ("pm uninstall --user 0 {pkg}", "pm install-existing --user 0 {pkg}", True),
    "install-existing": ("pm install-existing --user 0 {pkg}", "pm uninstall --user 0 {pkg}", True),
    "force-stop":       ("am force-stop {pkg}", None, False),
    "clear-data":       ("pm clear {pkg}", None, False),
}


class ActionError(RuntimeError):
    pass


def _success(action: str, output: str) -> bool:
    low = output.lower()
    if "error" in low or "exception" in low or "failure" in low or "failed" in low:
        return False
    if action in ("disable", "enable"):
        return "new state" in low
    if action == "uninstall":
        return "success" in low
    if action == "install-existing":
        return "installed for user" in low or "success" in low or output.strip() == ""
    return True  # force-stop / clear-data / settings print nothing on success


class Actions:
    def __init__(self, adb: Adb, rules: Rules, journal: Journal):
        self.adb = adb
        self.rules = rules
        self.journal = journal

    def _require_journal(self):
        """Never change the device if the change cannot be recorded for undo."""
        try:
            self.journal.ensure_writable()
        except OSError as exc:
            raise ActionError(
                f"the journal is not writable ({exc}) - refusing to change the TV "
                "without a record of it. Fix the journal folder and try again.") from exc

    # -------------------------------------------------------- device state --

    def _alt_launcher(self, serial: str) -> str:
        try:
            enabled = self.adb.shell("pm list packages -e", serial=serial, timeout=30)
        except AdbError:
            return ""
        for line in enabled.splitlines():
            pkg = line.replace("package:", "").strip()
            if pkg in ALT_LAUNCHERS:
                return pkg
        return ""

    def _current_home(self, serial: str) -> str:
        try:
            out = self.adb.shell(
                "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                "-c android.intent.category.HOME", serial=serial, timeout=20)
            for line in out.splitlines():
                line = line.strip()
                if "/" in line and not line.startswith("No "):
                    return line.split("/")[0]
        except AdbError:
            pass
        return ""

    def _enabled_imes(self, serial: str) -> list[str]:
        try:
            out = self.adb.shell("ime list -s", serial=serial, timeout=20)
            return [l.split("/")[0].strip() for l in out.splitlines()
                    if "." in l and not l.startswith("com.android.internal")]
        except AdbError:
            return []

    # ------------------------------------------------------------ planning --

    def check_package(self, pkg: str, action: str, serial: str, expert: bool,
                      row: dict | None = None) -> tuple[bool, str]:
        if action not in ACTION_TEMPLATES:
            return False, f"unknown action {action}"
        ok, reason = self.rules.safety_check(pkg)
        if not ok:
            return False, reason
        c = self.rules.classify(pkg)
        needs_expert = ACTION_TEMPLATES[action][2]
        if needs_expert and not expert:
            return False, f"'{action}' is an expert action - tick Expert mode to enable it."
        if action == "disable" and c["risk"] == "expert" and not expert:
            return False, ("Expert-level package. Tick Expert mode to touch it, and test in "
                           "small batches - this one is not verified safe.")

        if action == "disable":
            # launcher guard: never disable the only home screen
            home = self._current_home(serial)
            if pkg in STOCK_LAUNCHERS or (home and pkg == home):
                alt = self._alt_launcher(serial)
                if not alt:
                    return False, ("Refusing to disable the home screen: no alternative launcher "
                                   "(FLauncher / Projectivy) is installed and enabled. Install one "
                                   "first, set it as default, then disable this. Otherwise you get "
                                   "a black screen.")
                return True, f"alternative launcher detected: {alt}"
            # IME guard: never disable the only keyboard
            imes = self._enabled_imes(serial)
            if pkg in imes and len(imes) <= 1:
                return False, ("Refusing to disable the only enabled keyboard - you could not "
                               "type on the TV anymore. Install another IME first.")
        return True, ""

    def plan(self, action: str, packages: list[str], serial: str, expert: bool = False,
             rows: dict[str, dict] | None = None) -> dict:
        rows = rows or {}
        allowed, denied = [], []
        for pkg in packages:
            ok, reason = self.check_package(pkg, action, serial, expert, rows.get(pkg))
            c = self.rules.classify(pkg)
            item = {
                "package": pkg, "label": c["label"], "risk": c["risk"], "tier": c["tier"],
                "command": ACTION_TEMPLATES[action][0].format(pkg=pkg),
                "undo_command": (ACTION_TEMPLATES[action][1] or "").format(pkg=pkg) or None,
                "reason": c["desc"],
            }
            if ok:
                if action == "disable" and reason:
                    item["guard"] = reason  # e.g. "alternative launcher detected"
                allowed.append(item)
            else:
                item["blocked_reason"] = reason
                denied.append(item)
        return {"action": action, "allowed": allowed, "denied": denied,
                "max_batch": MAX_PER_BATCH}

    # ------------------------------------------------------------- applying --

    def apply(self, action: str, packages: list[str], serial: str, *,
              expert: bool = False, dry_run: bool = False,
              rows: dict[str, dict] | None = None,
              note: str = "", device_label: str = "") -> dict:
        if action not in ACTION_TEMPLATES:
            raise ActionError(f"unknown action: {action}")
        if not packages:
            raise ActionError("no packages selected")
        if not dry_run and len(packages) > MAX_PER_BATCH:
            raise ActionError(
                f"batch too large: {len(packages)} > {MAX_PER_BATCH}. Small batches are a safety "
                "feature - apply, test the TV (Inputs button, HDMI, Netflix, keyboard), then continue.")
        if dry_run and len(packages) > 500:
            raise ActionError("too many packages for one preview - narrow the selection down.")

        plan = self.plan(action, packages, serial, expert=expert, rows=rows)
        if dry_run:
            return {"dry_run": True, **plan}
        self._require_journal()

        batch_id = uuid.uuid4().hex[:10]
        results = []
        for item in plan["allowed"]:
            cmd = ACTION_TEMPLATES[action][0].format(pkg=item["package"])
            ok, output = False, ""
            try:
                ok, output = self.adb.shell_quiet(cmd, serial=serial, timeout=40)
                ok = ok and _success(action, output)
            except AdbError as exc:
                output = str(exc)
            entry = self.journal.append({
                "batch_id": batch_id, "serial": serial, "device": device_label,
                "action": action, "package": item["package"],
                "label": item["label"], "risk": item["risk"], "tier": item["tier"],
                "command": cmd,
                "undo_command": ACTION_TEMPLATES[action][1].format(pkg=item["package"])
                                 if ACTION_TEMPLATES[action][1] else None,
                "ok": ok, "output": output[-500:], "note": note,
            })
            results.append({**item, "ok": ok, "output": output[-500:], "journal_id": entry["id"]})

        # one verification call per batch
        verified = None
        try:
            out = self.adb.shell("pm list packages -d --user 0", serial=serial, timeout=30)
            verified = {l.replace("package:", "").strip() for l in out.splitlines()
                        if l.startswith("package:")}
        except AdbError:
            pass
        if verified is not None:
            for r in results:
                r["verified_disabled"] = r["package"] in verified

        return {"batch_id": batch_id, "action": action, "results": results,
                "denied": plan["denied"], "applied": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"])}

    # ---------------------------------------------------------------- undo --

    def undo_batch(self, batch_id: str, expert: bool = False) -> dict:
        batch = next((b for b in self.journal.batches() if b["batch_id"] == batch_id), None)
        if not batch:
            raise ActionError(f"unknown batch {batch_id}")
        items = [e for e in batch["items"] if e.get("ok") and e.get("undo_command")]
        # never re-enable a package that is protected... undo only reverses what we did
        self._require_journal()
        undo_batch_id = f"undo-{uuid.uuid4().hex[:8]}"
        results = []
        for e in sorted(items, key=lambda x: x["ts"], reverse=True):
            ok, output = False, ""
            try:
                ok, output = self.adb.shell_quiet(e["undo_command"], serial=e["serial"], timeout=40)
                ok = ok and _success(INVERSE_ACTION.get(e["action"], "tuning"), output)
            except AdbError as exc:
                output = str(exc)
            self.journal.append({
                "batch_id": undo_batch_id, "serial": e["serial"], "device": e.get("device"),
                "action": "undo", "package": e["package"], "label": e.get("label"),
                "command": e["undo_command"], "undo_command": e.get("command"),
                "ok": ok, "output": output[-500:], "note": f"undo of {e['id']}",
            })
            results.append({"package": e["package"], "command": e["undo_command"],
                            "ok": ok, "output": output[-300:]})
        return {"batch_id": undo_batch_id, "results": results,
                "applied": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"])}

    # -------------------------------------------------------------- tuning ---

    def tuning_plan(self, tuning_id: str) -> dict:
        t = self.rules.tuning.get(tuning_id)
        if not t:
            raise ActionError(f"unknown tuning action {tuning_id}")
        return t

    def apply_tuning(self, tuning_id: str, serial: str, device_label: str = "",
                     dry_run: bool = False) -> dict:
        t = self.tuning_plan(tuning_id)
        plan = []
        for item in t["commands"]:
            old = None
            if "settings put" in item["cmd"]:
                parts = item["cmd"].split()
                ns, key = parts[2], parts[3]
                try:
                    old = self.adb.shell(f"settings get {ns} {key}", serial=serial, timeout=15).strip()
                except AdbError:
                    old = None
            plan.append({**item, "old_value": old})
        if dry_run:
            return {"dry_run": True, "tuning": t, "commands": plan}

        self._require_journal()
        batch_id = uuid.uuid4().hex[:10]
        results = []
        for item in plan:
            ok, output = False, ""
            try:
                ok, output = self.adb.shell_quiet(item["cmd"], serial=serial, timeout=40)
            except AdbError as exc:
                output = str(exc)
            undo = item.get("undo")
            if "settings put" in item["cmd"] and item.get("old_value") not in (None, "", "null"):
                parts = item["cmd"].split()
                undo = f"settings put {parts[2]} {parts[3]} {item['old_value']}"
            self.journal.append({
                "batch_id": batch_id, "serial": serial, "device": device_label,
                "action": "tuning", "package": tuning_id, "label": t["name"],
                "command": item["cmd"], "undo_command": undo, "ok": ok,
                "output": output[-300:], "note": "tuning",
            })
            results.append({**item, "undo": undo, "ok": ok, "output": output[-300:]})
        return {"batch_id": batch_id, "tuning": t, "results": results,
                "applied": sum(1 for r in results if r["ok"])}
