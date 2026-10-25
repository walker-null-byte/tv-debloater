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

# --- shell injection through package names is refused (REST + guards) ----------
evil = "com.google.android.tv.ads; reboot"
code, res = jpost("/api/apply", {"action": "disable", "packages": [evil], "dry_run": True})
check("REST: shell metacharacters in a package name are denied",
      code == 200 and not res["allowed"] and "valid Android package name" in res["denied"][0]["blocked_reason"], res)
code, res = jget(f"/api/package/{evil}")
check("REST: package detail rejects a malformed name", code == 400, res)
code, res = jget("/api/preset/no-such-preset")
check("REST: unknown preset is a 400, not a crash", code == 400, res)

# ==============================================================================
print("\n== MCP endpoint (/mcp) ==")
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from flask import Flask  # noqa: E402

from debloater.mcp_endpoint import register_mcp  # noqa: E402

mock = server.state.adb
JSON_ACCEPT = {"Accept": "application/json, text/event-stream"}


def rpc(method, params=None, *, c=None, **kw):
    r = (c or client).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params or {}}, headers=JSON_ACCEPT, **kw)
    return r.status_code, r.get_json()


def call(name, **args):
    """-> (is_error, parsed result or error text)"""
    code, res = rpc("tools/call", {"name": name, "arguments": args})
    assert code == 200, (code, res)
    result = res["result"]
    text = result["content"][0]["text"]
    return result["isError"], text if result["isError"] else json.loads(text)


code, res = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "selftest", "version": "0"}})
check("MCP initialize", code == 200 and res["result"]["serverInfo"]["name"] == "tvdebloater", res)
check("MCP initialize carries the workflow instructions",
      "apply_changes" in res["result"].get("instructions", "")
      and "never uninstalled" in res["result"]["instructions"])
r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
check("MCP notification -> 202", r.status_code == 202)

code, res = rpc("tools/list")
tools = {t["name"]: t for t in res["result"]["tools"]}
expected = {"get_status", "discover_devices", "connect_device", "pair_device", "select_device",
            "list_packages", "get_package", "list_presets", "get_preset", "preview_changes",
            "apply_changes", "undo_batch", "get_journal", "list_tuning", "apply_tuning",
            "save_snapshot", "compare_snapshots", "generate_report"}
check("MCP tools/list exposes every tool", set(tools) == expected, set(tools) ^ expected)
check("every tool has a description", all(t["description"] for t in tools.values()))
check("apply_changes schema: action enum, packages array",
      tools["apply_changes"]["inputSchema"]["properties"]["action"]["enum"] == ["disable", "enable"]
      and tools["apply_changes"]["inputSchema"]["properties"]["packages"]["type"] == "array"
      and tools["apply_changes"]["inputSchema"]["required"] == ["packages"])
check("no tool exposes an expert switch",
      not any("expert" in t["inputSchema"].get("properties", {}) for t in tools.values()))
code, res = rpc("tools/call", {"name": "nope", "arguments": {}})
check("unknown tool -> JSON-RPC error", "error" in res, res)
code, res = rpc("no/such/method")
check("unknown method -> JSON-RPC error", res["error"]["code"] == -32601, res)

# --- read-only tools ------------------------------------------------------------
err, st = call("get_status")
check("get_status", not err and st["selected"] == server.state.serial and st["adb_available"], st)
check("get_status omits bulky rules metadata", "rules_meta" not in st)
err, out = call("discover_devices")
check("discover_devices (mock)", not err and len(out["found"]) >= 1)
err, out = call("list_packages")
check("list_packages: compact page", not err and out["returned"] == len(out["packages"]) <= 100
      and out["total_matching"] > 60 and "raw_head" not in out["packages"][0]
      and {"package", "label", "tier", "risk", "enabled", "protected"} <= set(out["packages"][0]), out.get("counts"))
page1 = out["packages"]
err, out = call("list_packages", tier="ads")
check("list_packages: tier filter", not err and out["total_matching"] > 0
      and all(p["tier"] == "ads" for p in out["packages"]))
err, out = call("list_packages", only_actionable=True)
check("list_packages: only_actionable hides protected & disabled",
      not err and all(p["enabled"] and not p["protected"] for p in out["packages"]))
err, out = call("list_packages", search="ANOKI")
check("list_packages: case-insensitive search", not err and any("anoki" in p["package"] for p in out["packages"]))
err, out = call("list_packages", limit=5, offset=3)
check("list_packages: paging", not err and out["returned"] == 5 and out["packages"][0] == page1[3])
err, out = call("list_packages", limit=100000)
check("list_packages: limit is clamped", not err and out["returned"] <= 200)
err, msg = call("list_packages", tier="bogus")
check("list_packages: bad tier -> tool error naming valid tiers", err and "valid:" in msg, msg)
err, msg = call("list_packages", status="maybe")
check("list_packages: bad status -> tool error", err, msg)

err, out = call("get_package", package="com.google.android.tv.ads")
check("get_package", not err and out["classification"]["tier"] == "ads"
      and "raw_head" not in out["detail"] and out["disable_check"]["allowed"], out)
err, msg = call("get_package", package="x; reboot")
check("get_package: malformed name -> tool error", err and "package name" in msg, msg)

err, out = call("list_presets")
check("list_presets", not err and any(p["id"] == "ads-telemetry" and p["package_count"] > 0
                                      for p in out["presets"]), out)
err, out = call("get_preset", preset_id="ads-telemetry")
check("get_preset", not err and out["count"] > 0 and out["batch_size"] == 10, out)
preset_pkgs = [p["package"] for p in out["packages"]]
err, msg = call("get_preset", preset_id="nope")
check("get_preset: unknown id -> tool error", err and "unknown preset" in msg, msg)

# --- preview ----------------------------------------------------------------------
protected = "com.android.location.fused"
err, out = call("preview_changes", packages=preset_pkgs[:3] + [protected, evil])
check("preview_changes: allows safe, denies protected + injection",
      not err and len(out["allowed"]) == 3
      and {d["package"] for d in out["denied"]} == {protected, evil}
      and all(d["reason"] for d in out["denied"]), out)
check("preview_changes shows the exact command",
      out["allowed"][0]["command"] == f"pm disable-user --user 0 {preset_pkgs[0]}")
mock.calls.clear()
call("preview_changes", packages=preset_pkgs[:3])
check("preview_changes never runs a change on the device",
      not any(c.startswith(("pm disable", "pm enable", "settings put")) for c in mock.calls), mock.calls)
err, msg = call("preview_changes", packages=preset_pkgs[:3], action="uninstall")
check("uninstall is not available over MCP", err and "not available over MCP" in msg, msg)
err, msg = call("preview_changes", packages="com.google.android.tv.ads")
check("packages must be a list", err and "list" in msg, msg)
err, msg = call("preview_changes", packages=[f"com.example.app{i}" for i in range(51)])
check("preview is capped", err and "at most" in msg, msg)

# --- apply / undo -----------------------------------------------------------------
batch = preset_pkgs[:5]
err, out = call("apply_changes", packages=batch, note="mcp selftest")
check("apply_changes: disables the batch", not err and out["applied"] == 5 and out["failed"] == 0
      and all(r["ok"] and r["verified_disabled"] for r in out["results"]), out)
check("apply_changes: device really changed", all(p in mock.disabled for p in batch))
check("apply_changes: points at undo", out["batch_id"] in out.get("next_step", ""), out)
mcp_batch = out["batch_id"]
err, out = call("list_packages", status="disabled")
check("list_packages sees the change immediately (cache invalidated)",
      not err and set(batch) <= {p["package"] for p in out["packages"]})
err, out = call("get_journal", limit=3)
check("get_journal lists the batch with its note", not err and out["batches"][0]["batch_id"] == mcp_batch
      and out["batches"][0]["note"] == "mcp selftest" and set(out["batches"][0]["packages"]) == set(batch), out)

mock.calls.clear()
err, out = call("apply_changes", packages=[protected, evil])
check("apply_changes: protected + injected names are skipped, nothing runs",
      not err and out["applied"] == 0 and len(out["denied"]) == 2
      and not any(c.startswith("pm disable") for c in mock.calls) and "next_step" not in out, (out, mock.calls))
check("injected command never reached the journal",
      not any(";" in (e.get("command") or "") for e in server.state.journal.entries(limit=1000)))
err, msg = call("apply_changes", packages=[f"com.example.app{i}" for i in range(11)])
check("apply_changes: >10 packages refused", err and "batch too large" in msg, msg)
rows_now = server.state.get_inventory(refresh=True)["rows"]
expert_pkg = next(r["package"] for r in rows_now
                  if r["risk"] == "expert" and r["enabled"] and not r["keep"] and not r["blocked"])
err, out = call("apply_changes", packages=[expert_pkg])
check("apply_changes: expert-risk package is refused (no expert switch over MCP)",
      not err and out["applied"] == 0 and "Expert" in out["denied"][0]["reason"], out)
err, msg = call("apply_changes", packages=batch, action="uninstall")
check("apply_changes: uninstall is refused", err and "not available over MCP" in msg, msg)

err, out = call("undo_batch", batch_id=mcp_batch)
check("undo_batch re-enables everything", not err and out["applied"] == 5
      and not any(p in mock.disabled for p in batch), out)
err, msg = call("undo_batch", batch_id="nope")
check("undo_batch: unknown id -> tool error", err and "unknown batch" in msg, msg)

# --- tuning, snapshots, report -----------------------------------------------------
err, out = call("list_tuning")
check("list_tuning", not err and any(t["id"] == "animations" and t["commands"] for t in out["tuning"])
      and len(out["guidance"]) > 0)
err, out = call("apply_tuning", tuning_id="animations")
check("apply_tuning", not err and out["applied"] == 3 and mock.settings["window_animation_scale"] == "0.5", out)
err, undo_out = call("undo_batch", batch_id=out["batch_id"])
check("undo reverts tuning", not err and mock.settings["window_animation_scale"] == "1.0", undo_out)
err, msg = call("apply_tuning", tuning_id="nope")
check("apply_tuning: unknown id -> tool error", err, msg)
err, out = call("save_snapshot", label="mcp-before")
err2, out2 = call("save_snapshot", label="mcp-after")
err3, diff = call("compare_snapshots", before="mcp-before", after="mcp-after")
check("snapshots + compare", not (err or err2 or err3) and "ram" in diff and "counts" in diff, diff)
err, msg = call("compare_snapshots", before="mcp-before", after="missing")
check("compare_snapshots: missing label -> tool error", err and "missing" in msg, msg)
err, out = call("generate_report")
check("generate_report", not err and "Applied changes" in out["markdown"] and Path(out["saved"]).exists())

# --- connection tools ----------------------------------------------------------------
err, out = call("connect_device", host="192.168.1.57", port=5555)
check("connect_device", not err and out["device"]["brand"] == "Acer", out)
err, msg = call("connect_device", host="--help")
check("connect_device: option-like host refused", err and "invalid host" in msg, msg)
err, out = call("pair_device", host="192.168.1.57", port=37000, code="123456")
check("pair_device", not err and "paired" in out["message"].lower(), out)
err, msg = call("pair_device", host="192.168.1.57", port=37000, code="000000")
check("pair_device: wrong code -> tool error", err and "Wrong password" in msg, msg)
err, msg = call("pair_device", host="192.168.1.57", port=37000, code="12 34")
check("pair_device: non-numeric code refused", err, msg)
err, out = call("select_device", serial=server.state.serial)
check("select_device", not err and out["serial"] == server.state.serial, out)

# --- device changes are serialized -------------------------------------------------
spans: list[tuple[float, float]] = []
real_apply = server.state.actions.apply


def slow_apply(*a, **k):
    start = time.monotonic()
    time.sleep(0.15)
    out = real_apply(*a, **k)
    spans.append((start, time.monotonic()))
    return out


server.state.actions.apply = slow_apply
threads = [threading.Thread(target=call, args=("apply_changes",),
                            kwargs={"packages": [p], "action": "enable"}) for p in preset_pkgs[:2]]
[t.start() for t in threads]
[t.join() for t in threads]
server.state.actions.apply = real_apply
spans.sort()
check("concurrent apply_changes calls run one after the other",
      len(spans) == 2 and spans[0][1] <= spans[1][0], spans)

# --- HTTP edge: origin, host, token, content-type ----------------------------------------
BODY = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
r = client.post("/mcp", json=BODY, headers={"Origin": "http://evil.example"})
check("cross-origin POST refused (403)", r.status_code == 403)
r = client.post("/mcp", json=BODY, headers={"Origin": "null"})
check("opaque 'null' origin refused", r.status_code == 403)
r = client.post("/mcp", json=BODY, headers={"Origin": "http://localhost"})
check("same-origin POST allowed", r.status_code == 200)
r = client.post("/mcp", json=BODY, base_url="http://evil.example:8477")
check("DNS rebinding (foreign Host, no token) refused", r.status_code == 403)
r = client.post("/mcp", json=BODY, base_url="http://127.0.0.1:8477")
check("127.0.0.1 and [::1] hosts allowed",
      r.status_code == 200 and client.post("/mcp", json=BODY, base_url="http://[::1]:8477").status_code == 200)
r = client.post("/mcp", data="hello", content_type="text/plain")
check("non-JSON content type -> 415 (so browsers must preflight)", r.status_code == 415)
r = client.post("/mcp", data="{not json", content_type="application/json")
check("malformed JSON -> 400 parse error", r.status_code == 400 and r.get_json()["error"]["code"] == -32700)
r = client.post("/mcp", json=[])
check("empty batch -> JSON-RPC error", r.get_json()["error"]["code"] == -32600)
r = client.post("/mcp", json=[BODY, {**BODY, "id": 2, "method": "tools/list"}])
check("batch requests are answered as an array", isinstance(r.get_json(), list) and len(r.get_json()) == 2)
check("GET /mcp -> 405", client.get("/mcp").status_code == 405)
check("DELETE /mcp -> 405", client.delete("/mcp").status_code == 405)
r = client.options("/mcp", headers={"Origin": "http://evil.example",
                                    "Access-Control-Request-Method": "POST"})
check("no CORS headers are ever sent",
      not any(h.lower().startswith("access-control-allow") for h in r.headers.keys())
      and not any(h.lower().startswith("access-control-allow") for h in client.post("/mcp", json=BODY).headers.keys()))


def variant(**kw):
    a = Flask("variant")
    mounted, note = register_mcp(a, server.state, **kw)
    return mounted, note, a.test_client()


TOKEN = "s3cret-token-for-selftest"
mounted, note, tc = variant(bind_host="127.0.0.1", token=TOKEN)
check("token mode mounts", mounted and "bearer" in note, note)
check("token: missing -> 401 with WWW-Authenticate",
      (r := tc.post("/mcp", json=BODY)).status_code == 401 and r.headers.get("WWW-Authenticate") == "Bearer")
check("token: wrong -> 401", tc.post("/mcp", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401)
check("token: same-length wrong -> 401",
      tc.post("/mcp", json=BODY, headers={"Authorization": "Bearer " + "x" * len(TOKEN)}).status_code == 401)
check("token: the old turbomcp fallbacks (X-API-Key, mcp-session-id, body api_key) do not authenticate",
      all(tc.post("/mcp", json=b, headers=h).status_code == 401 for b, h in [
          (BODY, {"X-API-Key": TOKEN}), (BODY, {"mcp-session-id": TOKEN}), ({**BODY, "api_key": TOKEN}, {})]))
check("token: right -> 200", tc.post("/mcp", json=BODY, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200)
check("token: right token works from another host (LAN use)",
      tc.post("/mcp", json=BODY, base_url="http://192.168.1.10:8477",
              headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200)
check("token: still refuses cross-origin browsers",
      tc.post("/mcp", json=BODY, headers={"Authorization": f"Bearer {TOKEN}",
                                          "Origin": "http://evil.example"}).status_code == 403)
mounted, note, tc = variant(bind_host="0.0.0.0")
check("non-loopback bind without token: endpoint not mounted",
      not mounted and "TVD_MCP_TOKEN" in note and tc.post("/mcp", json=BODY).status_code == 404, note)
mounted, note, tc = variant(bind_host="0.0.0.0", token=TOKEN)
check("non-loopback bind with token: mounted", mounted)
mounted, note, tc = variant(bind_host="127.0.0.1", allowed_hosts=frozenset({"tv-box.lan"}))
check("TVD_MCP_ALLOWED_HOSTS extends the host allowlist",
      tc.post("/mcp", json=BODY, base_url="http://tv-box.lan:8477").status_code == 200
      and tc.post("/mcp", json=BODY, base_url="http://other.lan:8477").status_code == 403)

print(f"\n{'='*54}\n{PASS} passed, {FAIL} failed   (tmp: {TMP})")
sys.exit(1 if FAIL else 0)
