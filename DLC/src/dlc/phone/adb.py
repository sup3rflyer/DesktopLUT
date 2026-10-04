"""Thin adb wrapper for the measurement phone.

Everything here is a subprocess call with an argument *list* (never a shell string), so the Git-Bash
``/sdcard/...`` path mangling that bites ``adb`` from bash cannot happen. ``adb`` is not on PATH on the rig:
``C:\\platform-tools\\adb.exe`` (override with ``DLC_ADB`` or the ``exe`` argument).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ADB = r"C:\platform-tools\adb.exe"


class AdbError(RuntimeError):
    pass


def find_adb(exe: str | None = None) -> str:
    for cand in (exe, os.environ.get("DLC_ADB"), DEFAULT_ADB, shutil.which("adb")):
        if cand and Path(cand).exists():
            return str(cand)
    raise AdbError("adb not found - set DLC_ADB or install platform-tools at C:\\platform-tools")


@dataclass
class Adb:
    """One device. ``serial=None`` = the only attached device (adb errors if there are several)."""

    serial: str | None = None
    exe: str | None = None

    def __post_init__(self) -> None:
        self.exe = find_adb(self.exe)

    # -- plumbing ---------------------------------------------------------------------------------------
    def _argv(self, args: tuple[str, ...]) -> list[str]:
        base = [self.exe]
        if self.serial:
            base += ["-s", self.serial]
        return base + list(args)

    def run(self, *args: str, timeout: float = 60.0, binary: bool = False, check: bool = True):
        r = subprocess.run(self._argv(args), capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AdbError(f"adb {' '.join(args)} -> rc {r.returncode}: {r.stderr.decode(errors='replace').strip()}")
        return r.stdout if binary else r.stdout.decode(errors="replace").replace("\r", "")

    def shell(self, cmd: str, timeout: float = 60.0, check: bool = True) -> str:
        return self.run("shell", cmd, timeout=timeout, check=check)

    # -- device state -----------------------------------------------------------------------------------
    def devices(self) -> list[str]:
        out = self.run("devices")
        return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]

    def ensure_device(self) -> str:
        devs = self.devices()
        if not devs:
            raise AdbError("no authorised device - plug in the phone and accept the USB-debugging prompt")
        if self.serial and self.serial not in devs:
            raise AdbError(f"device {self.serial} not attached (have {devs})")
        return self.serial or devs[0]

    def top_activity(self) -> str:
        for ln in self.shell("dumpsys activity activities").splitlines():
            if "topResumedActivity" in ln:
                return ln.strip()
        return ""

    def free_gb(self, path: str = "/sdcard") -> float:
        row = self.shell(f"df -k {path}").splitlines()[-1].split()
        return int(row[3]) / 2**20

    def battery_pct(self) -> int | None:
        for ln in self.shell("dumpsys battery").splitlines():
            if ln.strip().startswith("level:"):
                return int(ln.split(":")[1])
        return None

    def wlan_ip(self) -> str | None:
        """The phone's Wi-Fi IPv4 address (needed by the Blackmagic REST server), or None."""
        for ln in self.shell("ip -4 addr show wlan0", check=False).splitlines():
            ln = ln.strip()
            if ln.startswith("inet "):
                return ln.split()[1].split("/")[0]
        return None

    def focus_window(self) -> str:
        out = self.shell("dumpsys window | grep mCurrentFocus", check=False).strip()
        return out.split("mCurrentFocus=")[-1] if out else ""

    # -- input ------------------------------------------------------------------------------------------
    def wake(self) -> None:
        self.shell("input keyevent KEYCODE_WAKEUP")

    def unlock(self, tries: int = 3) -> bool:
        """Wake the screen and dismiss a swipe-lock or Samsung's 'Accidental touch protection' (proximity sensor
        covered - typical when the phone sits on a mount). Returns True when an app window has focus.
        A secure lock (PIN) is not handled: that needs the owner."""
        guards = ("UnintentionalLcdOn", "Keyguard", "NotificationShade", "StatusBar", "Bouncer")
        for _ in range(tries):
            self.wake()
            time.sleep(0.6)
            focus = self.focus_window()
            if focus and not any(g in focus for g in guards):
                return True
            self.shell("wm dismiss-keyguard", check=False)
            self.swipe(540, 1476, 540, 700, 450)   # protection screen: drag the ring up
            time.sleep(0.8)
            self.swipe(540, 1900, 540, 500, 350)   # plain swipe lock
            time.sleep(0.8)
        focus = self.focus_window()
        return bool(focus) and not any(g in focus for g in guards)

    def key(self, name: str) -> None:
        self.shell(f"input keyevent {name}")

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x0: int, y0: int, x1: int, y1: int, ms: int = 300) -> None:
        self.shell(f"input swipe {int(x0)} {int(y0)} {int(x1)} {int(y1)} {int(ms)}")

    def text(self, s: str) -> None:
        self.shell("input text " + s.replace(" ", "%s"))

    def launch(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {package}")

    # -- files ------------------------------------------------------------------------------------------
    def screencap(self) -> bytes:
        return self.run("exec-out", "screencap", "-p", binary=True)

    def pull(self, remote: str, local: str | Path) -> Path:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", "-a", remote, str(local), timeout=900.0)
        if not local.exists():
            raise AdbError(f"pull produced no file: {remote}")
        return local

    def push(self, local: str | Path, remote: str) -> None:
        self.run("push", str(local), remote, timeout=900.0)

    def rm(self, remote: str) -> None:
        self.shell(f"rm -f '{remote}'")

    def stat_dir(self, remote_dir: str) -> dict[str, tuple[int, int]]:
        """``{name: (size_bytes, mtime_epoch)}`` for every regular file in a phone directory (one adb call)."""
        out = self.shell(f"cd '{remote_dir}' 2>/dev/null && stat -c '%s %Y %n' * 2>/dev/null", check=False)
        res: dict[str, tuple[int, int]] = {}
        for ln in out.splitlines():
            parts = ln.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                res[parts[2]] = (int(parts[0]), int(parts[1]))
        return res

    def media_scan(self, remote: str) -> None:
        self.shell(
            "content call --method scan_file --uri content://media/external/file --arg '%s'" % remote, check=False)

    # -- time -------------------------------------------------------------------------------------------
    def clock_offset(self, samples: int = 7) -> tuple[float, float]:
        """``(phone_epoch - host_epoch, uncertainty)`` in seconds; midpoint of the tightest adb round trip.

        The uncertainty is half that round trip (typically 10-20 ms) - good enough to place a clip's wall-clock
        start; frame-accurate sync still comes from an on-screen sync patch, never from this.
        """
        best: tuple[float, float] | None = None
        for _ in range(samples):
            t0 = time.time()
            out = self.shell("date +%s.%N").strip()
            t1 = time.time()
            rtt = t1 - t0
            off = float(out) - (t0 + t1) / 2
            if best is None or rtt < best[1] * 2:
                best = (off, rtt / 2)
        assert best is not None
        return best
