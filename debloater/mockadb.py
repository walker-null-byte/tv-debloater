"""MockAdb - a fake Android TV so the tool can be developed/tested/demoed
without a device. Enabled with TVD_MOCK=1.

The package set is modelled on a Google TV (Android 12, MediaTek SoC) -
the platform that Acer and most current smart TVs run on - plus a few
brand/third-party entries so vendor detection and tiers can be exercised.
"""
from __future__ import annotations

import random
import re
import time

from .adb import Adb, AdbError

MOCK_SERIAL = "192.168.1.57:5555"

PROPS = {
    "ro.product.model": "Acer I-Series 55 (Google TV)",
    "ro.product.brand": "Acer",
    "ro.product.manufacturer": "Acer",
    "ro.product.device": "maniatika",
    "ro.build.version.release": "12",
    "ro.build.version.sdk": "31",
    "ro.build.fingerprint": "Acer/maniatika/maniatika:12/STT2.220604.001/eng:user/release-keys",
    "ro.build.characteristics": "tv",
    "ro.board.platform": "mt9676",
    "ro.serialno": "MOCKACER0001",
    "init.svc.mediadrm": "running",
    "init.svc.drm": "running",
    "ro.vendor.build.security_patch": "2025-01-05",
}

FEATURES = ["android.software.leanback", "android.hardware.hdmi",
            "android.hardware.type.television", "android.software.live_tv",
            "android.software.leanback_only", "com.google.android.tv",
            "android.hardware.wifi", "android.hardware.bluetooth_le"]

# pkg -> (installer, is_user_app)
PKGS: dict[str, tuple[str | None, bool]] = {
    # --- Android / AOSP
    "android": (None, False),
    "com.android.systemui": (None, False),
    "com.android.tv.settings": (None, False),
    "com.android.providers.settings": (None, False),
    "com.android.providers.tv": (None, False),
    "com.android.providers.downloads": (None, False),
    "com.android.providers.media": (None, False),
    "com.android.providers.contacts": (None, False),
    "com.android.providers.calendar": (None, False),
    "com.android.providers.userdictionary": (None, False),
    "com.android.location.fused": (None, False),
    "com.android.printspooler": (None, False),
    "com.android.dreams.basic": (None, False),
    "com.android.settings.intelligence": (None, False),
    "com.android.captiveportallogin": (None, False),
    "com.android.vpndialogs": (None, False),
    "com.android.traceur": (None, False),
    "com.android.dynsystem": (None, False),
    "com.android.adservices.api": (None, False),
    "com.android.ondevicepersonalization.services": (None, False),
    "com.android.federatedcompute.services": (None, False),
    # --- Google TV platform
    "com.google.android.apps.tv.launcherx": (None, False),
    "com.google.android.tungsten.setupwraith": (None, False),
    "com.google.android.tvrecommendations": (None, False),
    "com.google.android.apps.tv.dreamx": (None, False),
    "com.google.android.tv.ads": (None, False),
    "com.google.android.tv.ads.icons": (None, False),
    "com.google.android.katniss": (None, False),
    "com.google.android.videos": (None, False),
    "com.google.android.play.games": (None, False),
    "com.google.android.youtube.tv": (None, False),
    "com.google.android.youtube.tvmusic": (None, False),
    "com.google.android.apps.mediashell": (None, False),
    "com.google.android.gms": (None, False),
    "com.google.android.gsf": (None, False),
    "com.android.vending": (None, False),
    "com.google.android.tts": (None, False),
    "com.google.android.marvin.talkback": (None, False),
    "com.google.android.inputmethod.latin": (None, False),
    "com.google.android.apps.inputmethod.hindi": (None, False),
    "com.google.android.webview": (None, False),
    "com.google.android.packageinstaller": (None, False),
    "com.google.android.permissioncontroller": (None, False),
    "com.google.android.tv.remote.service": (None, False),
    "com.google.android.tv.inputplayer": (None, False),
    "com.google.android.tv.mtc.remotepairer": (None, False),
    "com.google.android.feedback": (None, False),
    "com.google.android.partnersetup": (None, False),
    "com.google.android.onetimeinitializer": (None, False),
    "com.google.android.tv.bugreportsender": (None, False),
    "com.google.android.syncadapters.contacts": (None, False),
    "com.google.android.syncadapters.calendar": (None, False),
    "com.google.android.backuptransport": (None, False),
    "com.google.android.backdrop": (None, False),
    "com.android.tv.feedbackconsent": (None, False),
    # --- MediaTek platform
    "com.mediatek.tv.oneworld.tvcenter": (None, False),
    "com.mediatek.tv.agent": (None, False),
    "com.mediatek.tv.settings": (None, False),
    "com.mediatek.tis": (None, False),
    "com.mediatek.autopair": (None, False),
    "com.mediatek.wwtv.mediaplayer": (None, False),
    "com.mediatek.dtv.tvinput.dvbtuner": (None, False),
    "com.mediatek.tv.retaildemo": (None, False),
    "com.mediatek.tv.systemupdater": (None, False),
    "com.mediatek.dialservice": (None, False),
    "com.mediatek.miracast": (None, False),
    "com.mtc.emanual": (None, False),
    "com.mtc.uploadcsr": (None, False),
    "tv.anoki.acr.anokiacroptin": (None, False),
    "com.dolby.android.audio.service": (None, False),
    # --- brand
    "com.acer.android.tv.about": (None, False),
    "com.acer.tvhelper": (None, False),
    # --- preinstalled streaming
    "com.netflix.ninja": (None, False),
    "com.netflix.tokenmanager": (None, False),
    "com.amazon.amazonvideo.livingroom": (None, False),
    "com.spotify.tv.android": (None, False),
    "com.disney.disneyplus": (None, False),
    "in.startv.hotstar": (None, False),
    "com.sonyliv": (None, False),
    # --- user installed
    "org.xbmc.kodi": ("com.android.vending", True),
    "com.liskovsoft.smarttubetv.beta": (None, True),
    "me.efesser.flauncher": ("com.android.vending", True),
    "com.spocky.projengmenu": ("com.android.vending", True),
    "com.plexapp.android": ("com.android.vending", True),
    "org.videolan.vlc": (None, True),
}

PRE_DISABLED = {"com.spotify.tv.android"}  # something already off, for realism

VERSION_NAMES = {
    "com.google.android.youtube.tv": "4.20.24",
    "com.netflix.ninja": "10.5.0",
    "com.google.android.gms": "24.26.32",
    "com.android.vending": "41.5.24",
}

PATH_PREFIXES = {
    "com.netflix.ninja": "/product/app/Netflix",
    "in.startv.hotstar": "/product/app/Hotstar",
    "com.sonyliv": "/product/app/SonyLIV",
    "com.acer.tvhelper": "/oem/app/TVHelper",
    "com.acer.android.tv.about": "/oem/app/AboutAcer",
}


class MockAdb(Adb):
    """In-memory stand-in for a real TV."""

    name = "adb (mock)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = False
        self.disabled = set(PRE_DISABLED)
        self.settings: dict[str, str] = {
            "window_animation_scale": "1.0",
            "transition_animation_scale": "1.0",
            "animator_duration_scale": "1.0",
        }
        self.calls: list[str] = []

    # ---------------------------------------------------------------- basic --

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "Android Debug Bridge version 1.0.41 (mock)"

    def devices(self) -> list[dict]:
        if not self._connected:
            return []
        return [{"serial": MOCK_SERIAL, "state": "device",
                 "model": PROPS["ro.product.model"], "product": "maniatika",
                 "device": "maniatika", "transport_id": "1"}]

    def connect(self, host: str, port: int = 5555) -> tuple[bool, str]:
        self._connected = True
        self.serial = f"{host}:{port}"
        return True, f"connected to {host}:{port}"

    def pair(self, host: str, port: int | str, code: str) -> tuple[bool, str]:
        if str(code).strip() == "000000":
            return False, "Failed: Wrong password"
        self._connected = True
        return True, f"Successfully paired to {host}:{port}"

    def disconnect(self, target: str | None = None) -> tuple[bool, str]:
        self._connected = False
        return True, "disconnected"

    def mdns_services(self) -> list[dict]:
        return [{"type": "_adb-tls-pairing._tcp", "name": "mock-tv", "address": "192.168.1.57", "port": "41235"},
                {"type": "_adb-tls-connect._tcp", "name": "mock-tv", "address": "192.168.1.57", "port": "38001"}]

    def shell_quiet(self, command: str, *, serial: str | None = None,
                    timeout: int | None = None) -> tuple[bool, str]:
        return True, self.shell(command, serial=serial, timeout=timeout).strip()

    # ---------------------------------------------------------------- shell --

    def shell(self, command: str, *, serial: str | None = None,
              timeout: int | None = None, check: bool = False) -> str:
        self.calls.append(command)
        cmd = command.strip()

        if cmd == "getprop":
            return "".join(f"[{k}]: [{v}]\n" for k, v in PROPS.items())
        m = re.match(r"getprop\s+(\S+)$", cmd)
        if m:
            return PROPS.get(m.group(1), "") + "\n"
        if cmd == "id":
            return "uid=2000(shell) gid=2000(shell)\n"
        if cmd == "cat /proc/meminfo":
            return "MemTotal:        2097152 kB\nMemFree:          412640 kB\nMemAvailable:     903168 kB\n"
        if cmd.startswith("df "):
            return ("Filesystem     1K-blocks    Used Available Use% Mounted on\n"
                    "/dev/block/dm-4   7818112 3153168   4597504  41% /data\n")
        if cmd == "wm size":
            return "Physical size: 3840x2160\n"
        if cmd.startswith("pm list features"):
            return "\n".join(f"feature:{f}" for f in FEATURES) + "\n"
        if cmd.startswith("pm list packages"):
            return self._pm_list(cmd)
        if cmd.startswith("pm disable-user"):
            pkg = cmd.split()[-1]
            self.disabled.add(pkg)
            return f"Package {pkg} new state: disabled-user\n"
        if cmd.startswith("pm enable"):
            pkg = cmd.split()[-1]
            self.disabled.discard(pkg)
            return f"Package {pkg} new state: enabled\n"
        if cmd.startswith("pm uninstall"):
            pkg = cmd.split()[-1]
            self.disabled.add(pkg)
            PKGS.pop(pkg, None)
            return "Success\n"
        if cmd.startswith("pm install-existing"):
            pkg = cmd.split()[-1]
            self.disabled.discard(pkg)
            return f"Package {pkg} installed for user: 0\n"
        if cmd.startswith("pm clear"):
            pkg = cmd.split()[-1]
            return "Success\n"
        if cmd.startswith("pm trim-caches"):
            time.sleep(0.2)
            return "Success\n"
        if cmd.startswith("am force-stop"):
            return ""
        if cmd.startswith("settings get"):
            key = cmd.split()[-1]
            return self.settings.get(key, "null") + "\n"
        if cmd.startswith("settings put"):
            parts = cmd.split()
            if len(parts) >= 5:
                self.settings[parts[-2]] = parts[-1]
            return ""
        if cmd.startswith("dumpsys package"):
            return self._dumpsys_package(cmd)
        if cmd.startswith("dumpsys meminfo"):
            return self._meminfo()
        if cmd.startswith("dumpsys cpuinfo"):
            return ("0.8% 1000/system_server: 0.5% user + 0.3% kernel\n"
                    "0.4% 3411/com.google.android.apps.tv.launcherx: 0.3% user + 0.1% kernel\n"
                    "0.2% 3802/com.google.android.gms.persistent: 0.1% user + 0.1% kernel\n"
                    "0.1% 4102/com.netflix.ninja: 0.1% user + 0.0% kernel\n")
        if cmd.startswith("dumpsys media.drm"):
            return "MediaDrm instance: {vendor=Google, description=Widevine L1}\n"
        if cmd.startswith("ime list -s"):
            return "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
        if cmd.startswith("cmd package resolve-activity"):
            return ""
        return ""

    # ----------------------------------------------------------------- mock --

    def _pm_list(self, cmd: str) -> str:
        user_only = "-3" in cmd
        system_only = "-s" in cmd
        disabled_only = "-d" in cmd
        with_path = "-f" in cmd
        with_installer = "-i" in cmd

        lines = []
        for pkg, (installer, is_user) in sorted(PKGS.items()):
            if user_only and not is_user:
                continue
            if system_only and is_user:
                continue
            if disabled_only and pkg not in self.disabled:
                continue
            if with_path:
                prefix = PATH_PREFIXES.get(pkg, "/data/app" if is_user else "/system/app")
                lines.append(f"package:{prefix}/{pkg}/base.apk={pkg}")
                continue
            if with_installer:
                installer_txt = f" installer={installer}" if installer else " installer=null"
                lines.append(f"package:{pkg}{installer_txt}")
                continue
            lines.append(f"package:{pkg}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _dumpsys_package(self, cmd: str) -> str:
        pkg = cmd.split()[-1]
        if pkg not in PKGS:
            return f"Unable to find package: {pkg}\n"
        installer, is_user = PKGS[pkg]
        version = VERSION_NAMES.get(pkg, "1.0.%d" % (abs(hash(pkg)) % 50))
        state = "disabled-user" if pkg in self.disabled else "enabled"
        return f"""Package [{pkg}] ({(abs(hash(pkg)) % 0xFFFFFF):x}):
  userId={2000 + abs(hash(pkg)) % 5000}
  pkg=Package{{{pkg} {version} ({(abs(hash(pkg)) % 900000) + 100000})}}
  codePath={PATH_PREFIXES.get(pkg, '/data/app' if is_user else '/system/app')}/{pkg}
  resourcePath=...
  versionName={version}
  versionCode={(abs(hash(pkg)) % 900000) + 100000}
  minSdk=28 targetSdk=30
  flags= [ SYSTEM ] [ ALLOW_CLEAR_USER_DATA ]{' [ STOPPED ]' if pkg in self.disabled else ''}
  privateFlags= [ PRIVATE_FLAG_IS_RESOURCE ]
  installerPackageName={'null' if not installer else installer}
  firstInstallTime=2024-03-11 09:41:22
  lastUpdateTime=2025-08-02 18:03:55
  signatures=PackageSignatures{{842b9b0e [9fca]}}
  sharedUser=SharedUserSetting{{a01b2f3 {pkg}_uid/10123}}
  User 0: ceDataInode=51234 installed=true hidden=false suspended=false stopped=false notLaunched=false enabled=0 instant=false virtual=false
    gids=[3003, 1028, 1015]
    runtime permissions:
      android.permission.INTERNET: granted=true
      android.permission.ACCESS_NETWORK_STATE: granted=true
    Package Changes:
      seq=1234, packageName={pkg}, user=0, {state}
"""

    def _meminfo(self) -> str:
        used_mb = 1240 + len(self.disabled) * 3
        return f"""Applications Memory Usage (in Kilobytes):
Uptime: {random.randint(3, 9)} Realtime: {random.randint(3, 9)} minutes

Total PSS by process:
  {320 - len(self.disabled) * 2} MB: com.google.android.apps.tv.launcherx (pid 3411)
  {210 + len(self.disabled) * 2} MB: com.google.android.gms.persistent (pid 3802)
  180 MB: system_server (pid 1000)
  120 MB: com.netflix.ninja (pid 4102)
   95 MB: com.google.android.youtube.tv (pid 4230)
   60 MB: com.google.android.tvrecommendations (pid 4310)
   40 MB: com.mediatek.tv.agent (pid 4402)

Total PSS by OOM adjustment:
  {used_mb} MB: Total
Total RAM: {2097152} kB
 Free RAM: {903168} kB
Used RAM: {1193984} kB
"""
