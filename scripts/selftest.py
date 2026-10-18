#!/usr/bin/env python3
"""Self-test: exercises the whole pipeline against the mock device AND the
Flask API (test client). Run:  python3 scripts/selftest.py"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="tvd-selftest-"))
os.environ["TVD_MOCK"] = "1"
os.environ["TVD_JOURNAL"] = str(TMP / "journal.jsonl")
os.environ["TVD_REPORTS"] = str(TMP / "reports")

from debloater import device  # noqa: E402
from debloater.actions import ActionError, Actions  # noqa: E402
from debloater.journal import Journal  # noqa: E402
from debloater.mockadb import PKGS, MockAdb  # noqa: E402
from debloater.rules import Rules  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


print("== unit level (mock device) ==")
PKGS["com.totally.unknown.app"] = (None, True)  # to test unclassified handling

adb = MockAdb()
adb.connect("192.168.1.57")
serial = adb.serial
rules = Rules()
journal = Journal(TMP / "journal.jsonl")
actions = Actions(adb, rules, journal)

# --- inventory ---------------------------------------------------------------
inv = device.inventory(adb, serial)
check("inventory lists packages", inv["counts"]["total"] > 60, inv["counts"])
check("counting system/user", inv["counts"]["system"] > 40 and inv["counts"]["user"] >= 5, inv["counts"])

vendor = device.detect_vendor(list(inv["packages"]), "Acer", rules.vendors)
check("vendor by brand string", vendor["key"] == "acer", vendor)
vendor = device.detect_vendor(list(inv["packages"]), "UnknownBrand", rules.vendors)
check("vendor falls back to package prefixes", vendor["key"] == "mediatek", vendor)

rows = rules.annotate(inv, vendor)
by_pkg = {r["package"]: r for r in rows}

# --- classification ----------------------------------------------------------
check("launcher soft-protected (guarded at action time)",
      by_pkg["com.google.android.apps.tv.launcherx"]["soft_keep"]
      and not by_pkg["com.google.android.apps.tv.launcherx"]["keep"]
      and by_pkg["com.google.android.apps.tv.launcherx"]["tier"] == "core")
check("fused-location protected (bootloop)", by_pkg["com.android.location.fused"]["keep"])
check("MTK input switcher protected", by_pkg["com.mediatek.tv.oneworld.tvcenter"]["keep"])
check("anoki ACR → ads/safe", (by_pkg["tv.anoki.acr.anokiacroptin"]["tier"],
                               by_pkg["tv.anoki.acr.anokiacroptin"]["risk"]) == ("ads", "safe"))
check("tvrecommendations → promo/caution",
      (by_pkg["com.google.android.tvrecommendations"]["tier"],
       by_pkg["com.google.android.tvrecommendations"]["risk"]) == ("promo", "caution"))
check("netflix soft-protected (user choice)", not by_pkg["com.netflix.ninja"]["keep"]
      and by_pkg["com.netflix.ninja"]["soft_keep"])
check("acer TV helper → vendor", by_pkg["com.acer.tvhelper"]["tier"] == "vendor")
check("unknown third-party → unclassified expert",
      (by_pkg["com.totally.unknown.app"]["tier"], by_pkg["com.totally.unknown.app"]["risk"])
      == ("unclassified", "expert"))
check("UAD fallback used when curated miss",
      by_pkg["com.android.traceur"]["source"] in ("UAD-ng", "curated", "community"))

# --- safety checks -----------------------------------------------------------
ok, reason = rules.safety_check("com.android.location.fused")
check("safety: blocks fused location", not ok and "boot loop" in reason, reason)
ok, reason = rules.safety_check("com.mediatek.tv.retaildemo")
check("safety: allows retail demo", ok, reason)

# --- presets -----------------------------------------------------------------
preset = rules.resolve_preset("ads-telemetry", rows, vendor["key"])
check("ads preset resolves", len(preset["packages"]) > 0, len(preset["packages"]))
check("ads preset is only safe risk", all(p["risk"] == "safe" for p in preset["packages"]))
check("ads preset excludes protected", not any(p["keep"] for p in preset["packages"]))
check("ads preset excludes already-disabled", not any(p["disabled"] for p in preset["packages"]))

targets = [p["package"] for p in preset["packages"]][:10]
check("preset size fits a batch", 0 < len(targets) <= 10, len(targets))

# --- dry run -----------------------------------------------------------------
plan = actions.plan("disable", targets, serial)
check("dry run allows selected", len(plan["allowed"]) == len(targets), plan["denied"])
check("dry run includes exact commands", all("pm disable-user --user 0" in a["command"] for a in plan["allowed"]))

# --- apply -------------------------------------------------------------------
res = actions.apply("disable", targets, serial, device_label="Mock Acer TV")
check("apply disables all", res["applied"] == len(targets), res)
check("mock state updated", all(p in adb.disabled for p in targets))
check("verification via pm list -d", all(r.get("verified_disabled") for r in res["results"]))
inv2 = device.inventory(adb, serial)
check("inventory reflects disabled", inv2["packages"][targets[0]]["disabled"])

# --- guards ------------------------------------------------------------------
try:
    actions.apply("disable", targets + ["com.acer.tvhelper"], serial)
    check("batch limit enforced", False)
except ActionError as exc:
    check("batch limit enforced (10)", "batch too large" in str(exc))
try:
    big = actions.apply("disable", targets + ["com.acer.tvhelper"], serial, dry_run=True)
    check("dry-run preview allows >10", len(big["allowed"]) == 11, big)
except ActionError as exc:
    check("dry-run preview allows >10", False, exc)

plan = actions.plan("disable", ["com.android.location.fused"], serial)
check("keep-list blocks disable", len(plan["denied"]) == 1
      and "PROTECTED" in plan["denied"][0]["blocked_reason"], plan)

plan = actions.plan("disable", ["com.google.android.apps.tv.launcherx"], serial, expert=True)
check("launcher allowed when alt launcher installed", len(plan["allowed"]) == 1
      and "alternative launcher" in plan["allowed"][0].get("guard", ""), plan)

_orig_alt = actions._alt_launcher
actions._alt_launcher = lambda s: ""
plan = actions.plan("disable", ["com.google.android.apps.tv.launcherx"], serial, expert=True)
check("launcher refused without replacement launcher", len(plan["denied"]) == 1
      and "black screen" in plan["denied"][0]["blocked_reason"], plan)
actions._alt_launcher = _orig_alt

plan = actions.plan("disable", ["com.google.android.apps.tv.launcherx"], serial)
check("launcher refused without expert mode", len(plan["denied"]) == 1, plan)

plan = actions.plan("disable", ["com.totally.unknown.app"], serial)
check("expert package refused without expert flag", len(plan["denied"]) == 1, plan)
plan = actions.plan("disable", ["com.totally.unknown.app"], serial, expert=True)
check("expert package allowed with expert flag", len(plan["allowed"]) == 1, plan)

plan = actions.plan("uninstall", ["com.sonyliv"], serial)
check("uninstall refused without expert", len(plan["denied"]) == 1, plan)
plan = actions.plan("uninstall", ["com.sonyliv"], serial, expert=True)
check("uninstall allowed with expert", len(plan["allowed"]) == 1, plan)

# --- undo --------------------------------------------------------------------
undo = actions.undo_batch(res["batch_id"])
check("undo restores everything", undo["applied"] == len(targets), undo)
check("mock state restored", not any(p in adb.disabled for p in targets))

# --- resilience: journal folder deleted while the server is running -----------
# Regression: an externally deleted journal folder used to crash the apply loop
# AFTER the first adb command had already run on the TV - so only package #1
# was disabled, the rest were silently skipped, and nothing was journaled.
jdir = TMP / "resilience"
jres = Journal(jdir / "journal.jsonl")
ares = Actions(adb, rules, jres)
shutil.rmtree(jdir, ignore_errors=True)
res_r = ares.apply("disable", targets[:3], serial, device_label="Mock Acer TV")
check("deleted journal dir: apply survives (dir self-heals)",
      res_r["applied"] == 3 and (jdir / "journal.jsonl").exists(), res_r)
check("deleted journal dir: all 3 disabled (not just the first)",
      all(p in adb.disabled for p in targets[:3]), sorted(adb.disabled))
und_r = ares.undo_batch(res_r["batch_id"])
check("deleted journal dir: undo still works", und_r["applied"] == 3, und_r)
check("deleted journal dir: state restored", not any(p in adb.disabled for p in targets[:3]))

# --- safety: an unwritable journal must refuse BEFORE touching the device -----
blocked = TMP / "blocked_file"
blocked.write_text("not a directory")
jbad = Journal(TMP / "ok" / "journal.jsonl")
jbad.path = blocked / "journal.jsonl"
abad = Actions(adb, rules, jbad)
try:
    abad.apply("disable", targets[:1], serial)
    check("unwritable journal refuses to apply", False)
except ActionError as exc:
    check("unwritable journal refuses to apply", "refusing" in str(exc), exc)
check("unwritable journal: device untouched", targets[0] not in adb.disabled)

# --- detail parse ------------------------------------------------------------
det = device.package_detail(adb, serial, "com.netflix.ninja")
check("dumpsys parse: version", bool(det.get("version_name")), det.get("version_name"))
check("dumpsys parse: system flag", det.get("is_system") is True)
check("dumpsys parse: permissions", isinstance(det.get("permissions"), list))

# --- metrics -----------------------------------------------------------------
m1 = device.metrics(adb, serial)
check("metrics snapshot", "ram_total_mb" in m1 and m1["counts"]["total"] > 0, m1.get("meminfo_error"))
diff = device.compare_metrics(m1, m1)
check("metrics compare", diff["counts"]["total"]["delta"] == 0)

# --- tuning ------------------------------------------------------------------
tr = actions.apply_tuning("animations", serial, dry_run=True)
check("tuning dry-run plans commands", len(tr["commands"]) == 3)
tr = actions.apply_tuning("animations", serial, device_label="Mock")
check("tuning applies", tr["applied"] == 3, tr)
check("setting changed on device", adb.settings["window_animation_scale"] == "0.5")
check("tuning undo captured old value",
      any(e["action"] == "tuning" and e["undo_command"] and "1.0" in (e["undo_command"] or "")
          for e in journal.entries(serial)))
tr = actions.apply_tuning("trim-caches", serial)
check("trim caches runs", tr["applied"] == 1)

# --- journal / report --------------------------------------------------------
script = journal.undo_script(serial)
check("undo script generated", "pm enable" in script or "settings put" in script)
report = journal.markdown_report(serial, {"model": "Mock Acer TV"})
check("report renders", "Applied changes" in report and "Mock Acer TV" in report)
batches = journal.batches(serial)
check("batches grouped", len(batches) >= 3, len(batches))

print("\n== API level (Flask test client) ==")
import app as server  # noqa: E402  (imports with TVD_MOCK already set)

server.state.journal = Journal(TMP / "api-journal.jsonl")
server.state.actions = Actions(server.state.adb, server.state.rules, server.state.journal)
client = server.app.test_client()


def jget(path: str):
    r = client.get(path)
    return r.status_code, r.get_json()


def jpost(path: str, body: dict):
    r = client.post(path, json=body)
    return r.status_code, r.get_json()


code, st = jget("/api/status")
check("GET /api/status", code == 200 and st["mock"] is True, st)
code, res = jget("/api/discover")
check("GET /api/discover (mock)", code == 200 and len(res["found"]) >= 1)
code, res = jpost("/api/connect", {"host": "192.168.1.57", "port": 5555})
check("POST /api/connect", code == 200 and res["device"]["brand"] == "Acer", res.get("error"))
code, res = jget("/api/packages")
check("GET /api/packages", code == 200 and len(res["packages"]) > 60)
check("vendor detected = acer", res["vendor"]["key"] == "acer", res["vendor"])
code, res = jget("/api/packages")
check("package cache serves quickly", code == 200)

pkg = "com.google.android.tv.ads"
code, res = jget(f"/api/package/{pkg}")
check("GET /api/package detail", code == 200 and res["classification"]["tier"] == "ads", res.get("error"))

code, res = jget("/api/presets")
check("GET /api/presets", code == 200 and any(p["id"] == "ads-telemetry" for p in res["presets"]))
code, res = jget("/api/preset/ads-telemetry")
check("GET /api/preset/<id>", code == 200 and len(res["packages"]) > 0, res.get("error"))
targets2 = [p["package"] for p in res["packages"]][:5]

code, res = jpost("/api/apply", {"action": "disable", "packages": targets2, "dry_run": True})
check("POST /api/apply dry-run", code == 200 and res["dry_run"] and len(res["allowed"]) == 5, res)
code, res = jpost("/api/apply", {"action": "disable", "packages": targets2})
check("POST /api/apply real", code == 200 and res["applied"] == 5, res)
batch_id = res["batch_id"]

code, res = jpost("/api/apply", {"action": "disable", "packages": ["com.android.location.fused"]})
check("API blocks protected package", code == 200 and len(res["denied"]) == 1, res)

code, res = jget("/api/journal")
check("GET /api/journal", code == 200 and len(res["batches"]) >= 1)
code, res = jpost("/api/undo", {"batch_id": batch_id})
check("POST /api/undo", code == 200 and res["applied"] == 5, res)

code, res = jpost("/api/metrics/snapshot", {"label": "before"})
check("POST metrics snapshot", code == 200 and "ram_total_mb" in res)
code, res = jget("/api/metrics/compare?before=before&after=before")
check("GET metrics compare", code == 200 and res["diff"]["counts"]["total"]["delta"] == 0)

code, res = jget("/api/tuning")
check("GET /api/tuning", code == 200 and "animations" in res["tuning"])
code, res = jpost("/api/tuning", {"id": "animations", "dry_run": True})
check("POST tuning dry-run", code == 200 and len(res["commands"]) == 3)

code, res = jget("/api/report")
check("GET /api/report", code == 200 and "Applied changes" in res["markdown"])

code, res = jpost("/api/apply", {"action": "disable", "packages": []})
check("API rejects empty selection", code == 400)

# --- unexpected crashes must still answer with readable JSON ------------------
def _boom(*a, **k):
    raise RuntimeError("boom")


_orig_apply = server.state.actions.apply
server.state.actions.apply = _boom
with contextlib.redirect_stderr(io.StringIO()):  # the handler prints a traceback
    code, res = jpost("/api/apply", {"action": "disable", "packages": ["com.google.android.tv.ads"]})
server.state.actions.apply = _orig_apply
check("unexpected errors return readable JSON",
      code == 500 and "boom" in (res or {}).get("error", ""), res)

print(f"\n{'='*54}\n{PASS} passed, {FAIL} failed   (tmp: {TMP})")
sys.exit(1 if FAIL else 0)
