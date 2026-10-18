"""LAN discovery: find Android TVs with ADB-over-network enabled.

Two mechanisms:
  1. mDNS: `adb mdns services` (works when the TV advertises _adb-tls-connect)
  2. Port scan of the local /24s on the classic 5555 port
Everything is best-effort - manual IP entry is always available.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

ADB_PORT = 5555
SCAN_TIMEOUT = 0.4
SCAN_WORKERS = 128


def local_ipv4_networks() -> list[dict]:
    """Parse `ip -4 -o addr show` into [{interface, ip, prefix}] (no deps)."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    nets = []
    for line in out.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        iface, ip, prefix = m.group(1), m.group(2), int(m.group(3))
        if iface == "lo" or ip.startswith("127."):
            continue
        nets.append({"interface": iface, "ip": ip, "prefix": prefix})
    return nets


def _hosts(net: dict, cap_24: bool = True) -> list[str]:
    network = ipaddress.ip_network(f"{net['ip']}/{net['prefix']}", strict=False)
    if cap_24 and network.prefixlen < 24:
        network = ipaddress.ip_network(f"{net['ip']}/24", strict=False)
    return [str(h) for h in network.hosts()][:512]


def scan_network(nets: list[dict], port: int = ADB_PORT,
                 timeout: float = SCAN_TIMEOUT, workers: int = SCAN_WORKERS) -> list[dict]:
    targets: list[tuple[str, dict]] = []
    for net in nets:
        for host in _hosts(net):
            targets.append((host, net))

    def probe(host: str) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, host): (host, net) for host, net in targets}
        for fut in as_completed(futures):
            host, net = futures[fut]
            try:
                if fut.result():
                    found.append({"ip": host, "port": port, "interface": net["interface"]})
            except Exception:
                continue
    return found


def ip_neighbors() -> dict[str, str]:
    """ip -> MAC from the ARP/neighbour table (for prettier results)."""
    out = {}
    try:
        text = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return out
    for line in text.splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+.*lladdr\s+([0-9a-f:]{17})", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def discover(adb, deep: bool = False) -> dict:
    nets = local_ipv4_networks()
    result: dict = {"interfaces": nets, "mdns": [], "found": [], "notes": []}

    # 1. mDNS through the adb daemon (cheap, no network traffic from us)
    try:
        services = adb.mdns_services()
        result["mdns"] = services
    except Exception as exc:  # AdbError or anything else
        result["notes"].append(f"mDNS lookup failed: {exc}")

    # 2. port scan
    scanned = scan_network(nets)
    neigh = ip_neighbors()
    for item in scanned:
        item["mac"] = neigh.get(item["ip"], "")
        item["hostname"] = reverse_dns(item["ip"])
        item["source"] = "scan"
    result["found"] = scanned
    if not scanned:
        result["notes"].append(
            "No device with port 5555 open found. Make sure ADB/Network debugging is ON "
            "on the TV and both devices are on the same network (not a guest/5GHz-6GHz split).")

    known = {item["ip"] for item in scanned}
    for svc in result["mdns"]:
        ip = svc.get("address", "")
        if ip and ip not in known:
            result["found"].append({"ip": ip, "port": int(svc.get("port") or ADB_PORT),
                                    "mac": neigh.get(ip, ""), "hostname": reverse_dns(ip),
                                    "source": "mdns", "name": svc.get("name", "")})
    return result


def adb_port_of(serial: str) -> int:
    m = re.search(r":(\d+)$", serial or "")
    return int(m.group(1)) if m else ADB_PORT
