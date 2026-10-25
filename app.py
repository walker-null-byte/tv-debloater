#!/usr/bin/env python3
"""TVDebloater - Flask backend.

Run:  ./run.sh          (auto-installs adb + flask, then serves on :5555-ui)
      TVD_MOCK=1 ./run.sh   (demo mode with a fake Google TV)
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from debloater import __version__, discovery
from debloater.actions import PACKAGE_NAME_RE, ActionError, Actions
from debloater.adb import Adb, AdbError
from debloater.device import compare_metrics, detect_vendor, device_info, inventory, metrics, package_detail
from debloater.journal import Journal
from debloater.mcp_endpoint import register_from_env
from debloater.mockadb import MockAdb
from debloater.rules import Rules

ROOT = Path(__file__).resolve().parent
MOCK = os.environ.get("TVD_MOCK", "") not in ("", "0", "false")
REPORTS = Path(os.environ.get("TVD_REPORTS", ROOT / "reports"))
INV_TTL = 90  # seconds before the package cache is considered stale

app = Flask(__name__, static_folder=None)


class ConnectError(ActionError):
    """adb could not reach the device (REST answers 502)."""


# ------------------------------------------------------------------ state --

class State:
    """Everything the REST API and the MCP tools do goes through here, so both
    front-ends share the same behaviour and the same safety gates."""

    def __init__(self):
        self.adb = MockAdb() if MOCK else Adb()
        self.rules = Rules()
        self.journal = Journal()
        self.actions = Actions(self.adb, self.rules, self.journal)
        self.serial: str | None = None
        self.info: dict | None = None
        self._inv: dict | None = None
        self._inv_ts: float = 0.0
        self.lock = threading.Lock()        # inventory refresh
        self.write_lock = threading.Lock()  # one device-changing operation at a time

    # ------------------------------------------------------- connection ---

    def status(self) -> dict:
        try:
            devices = self.adb.devices()
        except AdbError as exc:
            devices = [{"error": str(exc)}]
        return {
            "version": __version__,
            "mock": MOCK,
            "adb_path": getattr(self.adb, "path", ""),
            "adb_available": self.adb.available(),
            "adb_version": self.adb.version(),
            "devices": devices,
            "selected": self.serial,
            "device": self.info,
            "rules_meta": self.rules.tv["meta"],
        }

    def discover(self) -> dict:
        if MOCK:
            return {
                "interfaces": [{"interface": "wlan0", "ip": "192.168.1.140", "prefix": 24}],
                "mdns": self.adb.mdns_services(),
                "found": [{"ip": "192.168.1.57", "port": 5555, "interface": "wlan0",
                           "mac": "aa:bb:cc:dd:ee:ff", "hostname": "mock-acer-tv",
                           "source": "mock"}],
                "notes": ["MOCK MODE: this is a fake device."],
            }
        return discovery.discover(self.adb)

    @staticmethod
    def _check_host(host: str) -> str:
        host = (host or "").strip()
        if not host:
            raise ActionError("host is required")
        if host.startswith("-") or any(c.isspace() for c in host):  # would read as an adb option
            raise ActionError("invalid host")
        return host

    def pair(self, host: str, port: int | str, code: str) -> str:
        host = self._check_host(host)
        code = str(code or "").strip()
        if not (port and code.isdigit()):
            raise ActionError("host, port and the numeric pairing code are required")
        success, message = self.adb.pair(host, int(port), code)
        if not success:
            raise ConnectError(message or "pairing failed")
        return message

    def connect(self, host: str, port: int = 5555) -> dict:
        host = self._check_host(host)
        success, message = self.adb.connect(host, port)
        if not success:
            raise ConnectError(message or "could not connect")
        serial = host if host.endswith(f":{port}") else f"{host}:{port}"
        # adb may normalize the serial
        for dev in self.adb.devices():
            if dev.get("state") == "device" and (dev["serial"] in serial or serial in dev["serial"]):
                serial = dev["serial"]
                break
        self.serial = serial
        self.invalidate()
        try:
            self.info = device_info(self.adb, serial)
        except AdbError as exc:
            self.info = {"serial": serial, "info_error": str(exc)}
        return {"message": message, "serial": serial, "device": self.info,
                "unauthorized": any(d.get("state") == "unauthorized" for d in self.adb.devices())}

    def select(self, serial: str) -> dict:
        serial = (serial or "").strip()
        if not serial:
            raise ActionError("serial is required")
        self.serial = serial
        self.invalidate()
        self.info = device_info(self.adb, serial)
        return {"serial": serial, "device": self.info}

    # -------------------------------------------------------- inventory ---

    def ensure_connected(self) -> str:
        if not self.serial:
            raise ActionError("not connected to a device - connect first")
        return self.serial

    def get_inventory(self, refresh: bool = False) -> dict:
        serial = self.ensure_connected()
        fresh = self._inv is not None and (time.time() - self._inv_ts) < INV_TTL
        if self._inv and self._inv.get("serial") == serial and fresh and not refresh:
            return self._inv
        with self.lock:
            inv = inventory(self.adb, serial)
            vendor = detect_vendor(list(inv["packages"]), (self.info or {}).get("brand", ""),
                                   self.rules.vendors)
            rows = self.rules.annotate(inv, vendor)
            data = {
                "serial": serial,
                "vendor": vendor,
                "counts": inv["counts"],
                "rows": rows,
                "rows_by_pkg": {r["package"]: r for r in rows},
                "stats": self.rules.stats(rows),
                "ts": time.time(),
            }
            self._inv = data
            self._inv_ts = time.time()
            return data

    def invalidate(self):
        self._inv = None
        self._inv_ts = 0.0

    def package_info(self, pkg: str, expert: bool = False) -> dict:
        serial = self.ensure_connected()
        if not PACKAGE_NAME_RE.match(pkg):  # goes into `dumpsys package <pkg>` on the TV
            raise ActionError("not a valid Android package name")
        classification = self.rules.classify(pkg)
        safety_ok, safety_reason = self.rules.safety_check(pkg)
        detail = {}
        try:
            detail = package_detail(self.adb, serial, pkg)
        except AdbError as exc:
            detail = {"error": str(exc)}
        can_disable, disable_reason = False, str(safety_reason or "")
        try:
            can_disable, disable_reason = self.actions.check_package(pkg, "disable", serial, expert)
        except Exception as exc:  # guards query device state - never 500 the detail view
            disable_reason = f"could not evaluate: {exc}"
        return {"package": pkg, "classification": classification, "detail": detail,
                "safety": {"allowed": safety_ok, "reason": safety_reason},
                "disable_check": {"allowed": can_disable, "reason": disable_reason}}

    # ----------------------------------------------------------- changes ---

    def apply(self, action: str, packages: list[str], *, expert: bool = False,
              dry_run: bool = False, note: str = "") -> dict:
        serial = self.ensure_connected()
        if not packages:
            raise ActionError("no packages selected")
        inv = self.get_inventory()
        kwargs = dict(expert=expert, dry_run=dry_run, rows=inv["rows_by_pkg"], note=note,
                      device_label=(self.info or {}).get("model", ""))
        if dry_run:
            return self.actions.apply(action, packages, serial, **kwargs)
        with self.write_lock:
            result = self.actions.apply(action, packages, serial, **kwargs)
        self.invalidate()
        return result

    def undo(self, batch_id: str) -> dict:
        with self.write_lock:
            result = self.actions.undo_batch(batch_id)
        self.invalidate()
        return result

    def apply_tuning(self, tuning_id: str, dry_run: bool = False) -> dict:
        serial = self.ensure_connected()
        with self.write_lock:
            return self.actions.apply_tuning(
                tuning_id, serial, device_label=(self.info or {}).get("model", ""),
                dry_run=dry_run)

    # ----------------------------------------------------------- presets ---

    def presets(self) -> dict:
        inv = self.get_inventory()
        out = []
        for preset in self.rules.presets:
            resolved = self.rules.resolve_preset(preset["id"], inv["rows"], inv["vendor"]["key"])
            out.append({**preset, "_count": len(resolved["packages"])})
        guided = self.rules.recommended(inv["rows"])
        return {"presets": out, "recommended": [r["package"] for r in guided],
                "vendor": inv["vendor"]}

    def preset(self, preset_id: str) -> dict:
        inv = self.get_inventory()
        try:
            resolved = self.rules.resolve_preset(preset_id, inv["rows"], inv["vendor"]["key"])
        except KeyError as exc:
            raise ActionError(exc.args[0]) from exc
        return {"preset": resolved["preset"],
                "packages": [{k: r[k] for k in ("package", "label", "tier", "tier_label",
                                                "risk", "risk_rank", "desc", "source",
                                                "keep", "enabled", "system", "vendor")}
                             for r in resolved["packages"]]}

    # ------------------------------------------------------------ report ---

    def report(self) -> dict:
        md = self.journal.markdown_report(self.serial, self.info)
        REPORTS.mkdir(exist_ok=True, parents=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = REPORTS / f"debloat-report-{stamp}.md"
        path.write_text(md)
        undo = REPORTS / "undo.sh"
        undo.write_text(self.journal.undo_script(self.serial))
        undo.chmod(0o755)
        return {"markdown": md, "saved": str(path), "undo_script_saved": str(undo)}

    # --------------------------------------------------------- snapshots ---

    @property
    def snapshot_path(self) -> Path:
        return REPORTS / "snapshots.json"

    def snapshots(self) -> dict:
        if self.snapshot_path.exists():
            try:
                return json.loads(self.snapshot_path.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    def save_snapshot(self, label: str) -> dict:
        snap = metrics(self.adb, self.ensure_connected())
        snap["label"] = label
        all_snaps = self.snapshots()
        all_snaps.setdefault(self.serial or "unknown", {})[label] = snap
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.write_text(json.dumps(all_snaps, indent=1))
        return snap

    def compare_snapshots(self, before: str, after: str) -> dict:
        snaps = self.snapshots().get(self.serial or "unknown", {})
        if before not in snaps or after not in snaps:
            raise ActionError(f"snapshot '{before}' or '{after}' missing")
        return {"before": snaps[before], "after": snaps[after],
                "diff": compare_metrics(snaps[before], snaps[after])}


state = State()
MCP_MOUNTED, MCP_NOTE = register_from_env(app, state)  # POST /mcp for agents


# ----------------------------------------------------------------- helpers --

def ok(data):
    return jsonify(data)


def fail(message: str, code: int = 400, **extra):
    return jsonify({"error": message, **extra}), code


@app.errorhandler(AdbError)
def handle_adb_error(exc: AdbError):
    return jsonify(exc.to_dict()), 500


@app.errorhandler(ActionError)
def handle_action_error(exc: ActionError):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(ConnectError)
def handle_connect_error(exc: ConnectError):
    return jsonify({"error": str(exc)}), 502


@app.errorhandler(Exception)
def handle_unexpected(exc: Exception):
    """Unexpected crashes must still be readable JSON - not an HTML 500 page."""
    if isinstance(exc, HTTPException):
        return exc
    traceback.print_exc()
    return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


# ------------------------------------------------------------------ static --

@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/static/<path:path>")
def static_files(path: str):
    return send_from_directory(ROOT / "static", path)


# ------------------------------------------------------------------ status --

@app.get("/api/status")
def api_status():
    return ok(state.status())


@app.get("/api/discover")
def api_discover():
    return ok(state.discover())


@app.post("/api/connect")
def api_connect():
    body = request.get_json(force=True, silent=True) or {}
    return ok(state.connect(body.get("host") or "", int(body.get("port") or 5555)))


@app.post("/api/pair")
def api_pair():
    body = request.get_json(force=True, silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 0
    code = (body.get("code") or "").strip()
    if not (host and port and code):
        return fail("host, port and the pairing code are required")
    success, message = state.adb.pair(host, int(port), code)
    return (ok if success else (lambda m: fail(m, 502)))(message)


@app.post("/api/disconnect")
def api_disconnect():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("serial") or state.serial
    success, message = state.adb.disconnect(target if target else None)
    if target and target == state.serial:
        state.serial = None
        state.info = None
        state.invalidate()
    return ok({"message": message})


@app.post("/api/select")
def api_select():
    body = request.get_json(force=True, silent=True) or {}
    return ok(state.select(body.get("serial") or ""))


@app.get("/api/device")
def api_device():
    serial = state.ensure_connected()
    if not state.info or request.args.get("refresh") == "1":
        state.info = device_info(state.adb, serial)
    return ok(state.info)


# ---------------------------------------------------------------- packages --

@app.get("/api/packages")
def api_packages():
    refresh = request.args.get("refresh") == "1"
    data = state.get_inventory(refresh=refresh)
    return ok({
        "serial": data["serial"], "vendor": data["vendor"], "counts": data["counts"],
        "stats": data["stats"], "packages": data["rows"], "cached_ts": data["ts"],
    })


@app.get("/api/package/<path:pkg>")
def api_package(pkg: str):
    return ok(state.package_info(pkg, expert=request.args.get("expert") == "1"))


# ----------------------------------------------------------------- actions --

@app.post("/api/apply")
def api_apply():
    body = request.get_json(force=True, silent=True) or {}
    return ok(state.apply(
        body.get("action") or "disable",
        [p for p in (body.get("packages") or []) if p],
        expert=bool(body.get("expert")),
        dry_run=bool(body.get("dry_run")),
        note=body.get("note") or "",
    ))


@app.get("/api/journal")
def api_journal():
    serial = request.args.get("serial") or state.serial
    return ok({"batches": state.journal.batches(serial),
               "undo_script": state.journal.undo_script(serial)})


@app.post("/api/undo")
def api_undo():
    body = request.get_json(force=True, silent=True) or {}
    batch_id = body.get("batch_id")
    if not batch_id:
        return fail("batch_id is required")
    return ok(state.undo(batch_id))


# ----------------------------------------------------------------- presets --

@app.get("/api/presets")
def api_presets():
    return ok(state.presets())


@app.get("/api/preset/<preset_id>")
def api_preset(preset_id: str):
    return ok(state.preset(preset_id))


# ------------------------------------------------------------------ tuning --

@app.get("/api/tuning")
def api_tuning():
    return ok({"tuning": state.rules.tuning,
               "guidance": state.rules.guidance})


@app.post("/api/tuning")
def api_tuning_apply():
    body = request.get_json(force=True, silent=True) or {}
    return ok(state.apply_tuning(body.get("id", ""), dry_run=bool(body.get("dry_run"))))


# ----------------------------------------------------------------- metrics --

@app.get("/api/metrics")
def api_metrics():
    serial = state.ensure_connected()
    return ok(metrics(state.adb, serial))


@app.post("/api/metrics/snapshot")
def api_metrics_snapshot():
    body = request.get_json(force=True, silent=True) or {}
    label = (body.get("label") or "snapshot").strip()
    return ok(state.save_snapshot(label))


@app.get("/api/metrics/snapshots")
def api_metrics_snapshots():
    return ok({"snapshots": state.snapshots().get(state.serial or "unknown", {})})


@app.get("/api/metrics/compare")
def api_metrics_compare():
    return ok(state.compare_snapshots(request.args.get("before", "before"),
                                      request.args.get("after", "after")))


# ------------------------------------------------------------------ report --

@app.get("/api/report")
def api_report():
    return ok(state.report())


@app.get("/api/export/undo-script")
def api_export_undo():
    script = state.journal.undo_script(state.serial)
    return app.response_class(script, mimetype="text/x-shellscript",
                              headers={"Content-Disposition": "attachment; filename=undo-tvdebloat.sh"})


if __name__ == "__main__":
    port = int(os.environ.get("TVD_PORT", "8477"))
    host = os.environ.get("TVD_HOST", "127.0.0.1")
    banner = f"TVDebloater {__version__} - http://{host}:{port}"
    if MOCK:
        banner += "   [MOCK MODE - no real device]"
    print(banner)
    print(f"MCP: http://{host}:{port}/mcp - {MCP_NOTE}" if MCP_MOUNTED else f"MCP: {MCP_NOTE}")
    app.run(host=host, port=port, threaded=True, debug=False)
