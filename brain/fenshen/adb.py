"""Thin adb wrapper pinned to one device serial."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

def _sdk_dirs() -> list[Path]:
    """Where an Android SDK usually is: the standard variables first, then the IDE defaults."""
    dirs = [os.environ.get(v) for v in ("ANDROID_SDK_ROOT", "ANDROID_HOME")]
    home = Path.home()
    dirs += [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
             str(home / "Library" / "Android" / "sdk"), str(home / "Android" / "Sdk")]
    return [Path(d) for d in dirs if d]


def find_adb() -> str:
    explicit = os.environ.get("FENSHEN_ADB")
    if explicit:
        return explicit
    for sdk in _sdk_dirs():
        for name in ("adb.exe", "adb"):
            candidate = sdk / "platform-tools" / name
            if candidate.exists():
                return str(candidate)
    found = shutil.which("adb")
    if found:
        return found
    raise FileNotFoundError("adb not found: set ANDROID_SDK_ROOT (or FENSHEN_ADB), or put adb on PATH")


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, serial: str | None = None, adb_path: str | None = None):
        self.path = adb_path or find_adb()
        self.serial = serial or os.environ.get("FENSHEN_SERIAL") or self._only_device()

    def _only_device(self) -> str:
        out = subprocess.run([self.path, "devices"], capture_output=True, text=True, check=True).stdout
        serials = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
        if len(serials) != 1:
            raise AdbError(f"expected exactly one device, found {serials}; pass a serial or set FENSHEN_SERIAL")
        return serials[0]

    def argv(self, *args: str) -> list[str]:
        return [self.path, "-s", self.serial, *args]

    def run(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        proc = subprocess.run(self.argv(*args), capture_output=True, timeout=timeout)
        out = proc.stdout.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise AdbError(f"adb {' '.join(args)} failed ({proc.returncode}): {err.strip() or out.strip()}")
        return out

    def shell(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        return self.run("shell", *args, timeout=timeout, check=check)

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            self.argv(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
