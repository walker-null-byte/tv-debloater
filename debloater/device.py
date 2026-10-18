"""Device interrogation: info, package inventory, package details, metrics."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .adb import Adb, AdbError, looks_like_tv, parse_getprop

SYSTEM_PATH_PREFIXES = ("/system", "/system_ext", "/product", "/vendor", "/oem",
                        "/apex", "/odm", "/privi-app", "/remove_pkg")


def device_info(adb: Adb, serial: str) -> dict:
    props = parse_getprop(adb.shell("getprop", serial=serial, timeout=25))

    def prop(*keys: str) -> str:
        for k in keys:
            if props.get(k):
                return props[k]
        return ""

    info = {
        "serial": serial,
        "adb_serial": serial,
        "model": prop("ro.product.model", "ro.product.vendor.model"),
        "brand": prop("ro.product.brand", "ro.product.vendor.brand"),
        "manufacturer": prop("ro.product.manufacturer"),
        "device": prop("ro.product.device"),
        "android_version": prop("ro.build.version.release"),
        "sdk": prop("ro.build.version.sdk"),
        "fingerprint": prop("ro.build.fingerprint"),
        "security_patch": prop("ro.build.version.security_patch", "ro.vendor.build.security_patch"),
        "soc": prop("ro.board.platform", "ro.hardware"),
        "is_tv": looks_like_tv(props),
        "root_adbd": prop("service.adb.root") == "1",
        "drm_running": prop("init.svc.mediadrm", "init.svc.drm"),
    }

    # feature flags (leanback = Android TV platform)
    try:
        features = adb.shell("pm list features", serial=serial, timeout=20)
        info["features"] = [f.split(":", 1)[1] for f in features.splitlines() if f.startswith("feature:")]
    except AdbError:
        info["features"] = []
    info["leanback"] = "android.software.leanback" in info["features"] or \
        "android.hardware.type.television" in info["features"]
    info["is_tv"] = info["is_tv"] or info["leanback"]

    # memory & storage (best effort)
    try:
        for line in adb.shell("cat /proc/meminfo", serial=serial, timeout=10).splitlines():
            m = re.match(r"MemTotal:\s+(\d+) kB", line)
            if m:
                info["ram_mb"] = round(int(m.group(1)) / 1024)
                break
    except AdbError:
        pass
    try:
        df = adb.shell("df /data", serial=serial, timeout=10)
        rows = [l.split() for l in df.splitlines() if l.strip()]
        if len(rows) >= 2 and len(rows[1]) >= 4:
            info["data_total_mb"] = round(int(rows[1][1]) / 1024)
            info["data_free_mb"] = round(int(rows[1][3]) / 1024)
    except (AdbError, ValueError):
        pass
    try:
        info["resolution"] = adb.shell("wm size", serial=serial, timeout=10).strip().splitlines()[0]
    except (AdbError, IndexError):
        info["resolution"] = ""
    return info


# --------------------------------------------------------------- inventory --

def _parse_path_packages(text: str) -> dict[str, str]:
    """`pm list packages -f` -> {pkg: apk_path}"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        if "=" in body:
            path, pkg = body.rsplit("=", 1)
            out[pkg.strip()] = path
        else:
            out[body] = ""
    return out


def _parse_plain_packages(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            out.add(line[len("package:"):].strip())
    return out


def _parse_installers(text: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        m = re.match(r"([^\s]+)(?:\s+installer=(.*))?$", body)
        if m:
            inst = m.group(2)
            out[m.group(1)] = None if inst in (None, "null") else inst
    return out


def _is_system_path(path: str) -> bool:
    if not path:
        return True
    if path.startswith("/data/"):
        return False
    return True


def inventory(adb: Adb, serial: str) -> dict:
    """One pass over the device -> everything the UI needs to render the list."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_paths = pool.submit(adb.shell, "pm list packages -f --user 0", serial=serial, timeout=60)
        f_sys = pool.submit(adb.shell, "pm list packages -s", serial=serial, timeout=60)
        f_user = pool.submit(adb.shell, "pm list packages -3", serial=serial, timeout=60)
        f_disabled = pool.submit(adb.shell, "pm list packages -d", serial=serial, timeout=60)
        f_installer = pool.submit(adb.shell, "pm list packages -i", serial=serial, timeout=60)
        f_uninst = pool.submit(adb.shell, "pm list packages -u", serial=serial, timeout=60)

        paths = _parse_path_packages(f_paths.result())
        system_pkgs = _parse_plain_packages(f_sys.result())
        user_pkgs = _parse_plain_packages(f_user.result())
        disabled = _parse_plain_packages(f_disabled.result())
        installers = _parse_installers(f_installer.result())
        all_with_uninstalled = _parse_plain_packages(f_uninst.result())

    packages = {}
    for pkg, path in paths.items():
        system = (pkg in system_pkgs) or _is_system_path(path)
        if pkg in user_pkgs:
            system = False
        packages[pkg] = {
            "package": pkg,
            "path": path,
            "system": system,
            "enabled": pkg not in disabled,
            "disabled": pkg in disabled,
            "installer": installers.get(pkg),
        }
    # packages that exist but are uninstalled-for-user (recoverable)
    for pkg in all_with_uninstalled - set(packages):
        packages[pkg] = {
            "package": pkg, "path": "", "system": True, "enabled": False,
            "disabled": True, "uninstalled": True, "installer": installers.get(pkg),
        }
    return {"packages": packages, "counts": {
        "total": len(packages),
        "system": sum(1 for p in packages.values() if p["system"]),
        "user": sum(1 for p in packages.values() if not p["system"]),
        "disabled": sum(1 for p in packages.values() if p["disabled"]),
    }}


# ------------------------------------------------------------------ detail --

def package_detail(adb: Adb, serial: str, pkg: str) -> dict:
    text = adb.shell(f"dumpsys package {pkg}", serial=serial, timeout=30)
    detail: dict = {"package": pkg, "raw_lines": len(text.splitlines())}

    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, text, re.M)
        return m.group(1).strip() if m else default

    detail["version_name"] = grab(r"versionName=(\S+)")
    detail["version_code"] = grab(r"versionCode=(\S+)")
    detail["code_path"] = grab(r"codePath=(\S+)")
    detail["installer"] = grab(r"installerPackageName=(\S+)")
    detail["first_install"] = grab(r"firstInstallTime=(.+)")
    detail["last_update"] = grab(r"lastUpdateTime=(.+)")
    detail["shared_user"] = grab(r"sharedUser=\S+ (\S+?)/")
    detail["min_sdk"] = grab(r"minSdk=(\S+)")
    detail["target_sdk"] = grab(r"targetSdk=(\S+)")
    m = re.search(r"flags=\s*\[([^\]]+)\]", text)
    detail["flags"] = [f.strip() for f in m.group(1).split("] [")] if m else []
    detail["is_system"] = "SYSTEM" in detail["flags"] or (detail["code_path"] or "").startswith("/system")
    detail["user_state"] = grab(r"User 0: .*?enabled=(\S+)")
    detail["stopped"] = "STOPPED" in detail["flags"]
    perms = re.findall(r"^\s+(android\.permission\.[A-Z_0-9]+): granted=(\w+)", text, re.M)
    detail["permissions"] = [{"permission": p, "granted": g == "true"} for p, g in perms[:40]]
    detail["dangerous_perms"] = [p["permission"] for p in detail["permissions"]
                                 if any(x in p["permission"] for x in
                                        ("CAMERA", "RECORD_AUDIO", "ACCESS_FINE_LOCATION",
                                         "READ_CONTACTS", "READ_SMS", "CALL_PHONE"))]
    detail["raw_head"] = "\n".join(text.splitlines()[:60])
    return detail


# ----------------------------------------------------------------- metrics --

def metrics(adb: Adb, serial: str) -> dict:
    snap = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "serial": serial}
    try:
        mem = adb.shell("dumpsys meminfo", serial=serial, timeout=45)
        totals = []
        for line in mem.splitlines():
            m = re.match(r"\s*([\d,]+) MB:\s+(.+?)\s*\(pid\s+\d+\)", line)
            if m:
                totals.append({"mb": int(m.group(1).replace(",", "")), "process": m.group(2)})
        snap["top_processes"] = totals[:12]
        m = re.search(r"Total RAM:\s+(\d+) kB", mem)
        if m:
            snap["ram_total_mb"] = round(int(m.group(1)) / 1024)
        m = re.search(r"Free RAM:\s+(\d+) kB", mem)
        if m:
            snap["ram_free_mb"] = round(int(m.group(1)) / 1024)
        m = re.search(r"Used RAM:\s+(\d+) kB", mem)
        if m:
            snap["ram_used_mb"] = round(int(m.group(1)) / 1024)
    except AdbError as exc:
        snap["meminfo_error"] = str(exc)

    try:
        cpu = adb.shell("dumpsys cpuinfo", serial=serial, timeout=25)
        top = []
        for line in cpu.splitlines()[:15]:
            m = re.match(r"\s*([\d.]+)%\s+(\d+)/(\S+?):", line)
            if m:
                top.append({"cpu": float(m.group(1)), "pid": m.group(2), "process": m.group(3)})
        snap["top_cpu"] = top[:10]
    except AdbError:
        pass

    try:
        inv = inventory(adb, serial)
        snap["counts"] = inv["counts"]
    except AdbError as exc:
        snap["counts_error"] = str(exc)
    return snap


def compare_metrics(before: dict, after: dict) -> dict:
    out: dict = {"ram": {}, "counts": {}}
    for key in ("ram_free_mb", "ram_used_mb", "ram_total_mb"):
        b, a = before.get(key), after.get(key)
        if b is not None and a is not None:
            out["ram"][key] = {"before": b, "after": a, "delta": a - b}
    for key in ("total", "system", "user", "disabled"):
        b = (before.get("counts") or {}).get(key)
        a = (after.get("counts") or {}).get(key)
        if b is not None and a is not None:
            out["counts"][key] = {"before": b, "after": a, "delta": a - b}
    return out


def detect_vendor(packages: list[str], brand: str, manufacturers: list[dict]) -> dict:
    """Decide which vendor rule-set applies: brand string first, then most
    common vendor prefix among installed packages."""
    brand_l = (brand or "").lower()
    for key, meta in manufacturers.items():
        if key in brand_l or meta["label"].lower() in brand_l:
            return {"key": key, "label": meta["label"], "source": "brand string"}
    counts: dict[str, int] = {}
    for pkg in packages:
        for key, meta in manufacturers.items():
            if key == "google":
                continue
            if any(pkg.startswith(prefix) for prefix in meta["match"]):
                counts[key] = counts.get(key, 0) + 1
    if counts:
        best = max(counts, key=counts.get)
        return {"key": best, "label": manufacturers[best]["label"],
                "source": f"{counts[best]} installed packages"}
    return {"key": "google", "label": "Google TV (generic)", "source": "default"}
