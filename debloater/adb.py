"""Thin wrapper around the adb binary."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 30


class AdbError(RuntimeError):
    """Raised when the adb binary is missing or a command fails hard."""

    def __init__(self, message: str, *, cmd: list[str] | None = None,
                 stdout: str = "", stderr: str = "", code: int | None = None):
        super().__init__(message)
        self.cmd = cmd or []
        self.stdout = stdout
        self.stderr = stderr
        self.code = code

    def to_dict(self) -> dict:
        return {"error": str(self), "cmd": self.cmd, "stdout": self.stdout[-4000:],
                "stderr": self.stderr[-4000:], "code": self.code}


def resolve_adb_binary() -> str:
    """Where is adb? Env override > project-local platform-tools > PATH."""
    env = os.environ.get("ADB_PATH")
    candidates: list[str] = []
    if env:
        candidates.append(env)
    candidates.append(str(ROOT / "tools" / "platform-tools" / "adb"))
    on_path = shutil.which("adb")
    if on_path:
        candidates.append(on_path)
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return "adb"  # let the OS raise a clear FileNotFoundError later


def parse_devices_output(text: str) -> list[dict]:
    """Parse `adb devices -l` output into structured rows."""
    devices = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        extra = {}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                extra[k] = v
        devices.append({
            "serial": serial, "state": state,
            "model": extra.get("model", ""), "product": extra.get("product", ""),
            "device": extra.get("device", ""), "transport_id": extra.get("transport_id", ""),
        })
    return devices


class Adb:
    """Synchronous adb wrapper. Thread-safe enough for a single-user tool."""

    name = "adb"

    def __init__(self, path: str | None = None, serial: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT):
        self.path = path or resolve_adb_binary()
        self.serial = serial
        self.timeout = timeout
        self._lock = threading.Lock()

    # ------------------------------------------------------------ plumbing --

    def run(self, args: list[str], *, timeout: int | None = None,
            serial: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        use_serial = serial if serial is not None else self.serial
        cmd = [self.path]
        if use_serial:
            cmd += ["-s", use_serial]
        cmd += args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout or self.timeout)
        except FileNotFoundError as exc:
            raise AdbError(
                "adb binary not found. Run ./run.sh once to auto-download it into "
                "tools/platform-tools, or install android-tools and/or set ADB_PATH.",
                cmd=cmd) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"command timed out after {timeout or self.timeout}s",
                           cmd=cmd, stdout=(exc.stdout or b"").decode(errors="replace")
                           if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                           stderr="") from exc
        if check and proc.returncode != 0:
            raise AdbError("adb command failed", cmd=cmd, stdout=proc.stdout,
                           stderr=proc.stderr, code=proc.returncode)
        return proc

    def available(self) -> bool:
        try:
            return self.run(["version"], timeout=10).returncode == 0
        except AdbError:
            return False

    def version(self) -> str:
        try:
            out = self.run(["version"], timeout=10).stdout.strip()
            return out.splitlines()[0] if out else ""
        except AdbError as exc:
            return f"unavailable ({exc})"

    # ------------------------------------------------------------ transport --

    def devices(self) -> list[dict]:
        return parse_devices_output(self.run(["devices", "-l"]).stdout)

    def connect(self, host: str, port: int = 5555) -> tuple[bool, str]:
        target = host if ":" in host else f"{host}:{port}"
        with self._lock:
            proc = self.run(["connect", target], timeout=20)
        text = (proc.stdout + "\n" + proc.stderr).strip()
        low = text.lower()
        ok = ("connected to" in low and "failed" not in low and "unable" not in low) \
            or "already connected" in low
        return ok, text or ("connect failed" if not ok else "connected")

    def pair(self, host: str, port: int | str, code: str) -> tuple[bool, str]:
        target = f"{host}:{port}"
        with self._lock:
            proc = self.run(["pair", target, str(code)], timeout=25)
        text = (proc.stdout + "\n" + proc.stderr).strip()
        ok = "successfully paired" in text.lower()
        return ok, text

    def disconnect(self, target: str | None = None) -> tuple[bool, str]:
        with self._lock:
            proc = self.run(["disconnect"] + ([target] if target else []))
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()

    def mdns_services(self) -> list[dict]:
        """`adb mdns services` -> [{name, type, address, port}]"""
        proc = self.run(["mdns", "services"], timeout=15)
        found = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[0].startswith("_"):
                continue
            addr, port = parts[-2], parts[-1]
            found.append({"type": parts[0], "name": " ".join(parts[1:-2]),
                          "address": addr, "port": port})
        return found

    # ---------------------------------------------------------------- shell --

    def shell(self, command: str, *, serial: str | None = None,
              timeout: int | None = None, check: bool = False) -> str:
        proc = self.run(["shell", command], serial=serial,
                        timeout=timeout, check=check)
        return proc.stdout

    def shell_quiet(self, command: str, *, serial: str | None = None,
                    timeout: int | None = None) -> tuple[bool, str]:
        """Run a shell command and report (success, combined output)."""
        proc = self.run(["shell", command], serial=serial, timeout=timeout)
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, out


def looks_like_tv(props: dict) -> bool:
    chars = (props.get("ro.build.characteristics") or "").lower()
    return "tv" in chars or props.get("ro.product.device", "").lower() in ("atv", "tv")


def parse_getprop(text: str) -> dict:
    props = {}
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]$", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props
