"""Samples who owns focus and the keyboard, to measure whether the agent disturbs the user (README §7)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adb import Adb

_TOP_FOCUS = re.compile(r"mTopFocusedDisplayId=(-?\d+)")
_IME_SHOWN = re.compile(r"mInputShown=(\w+)")
_IME_CLIENT = re.compile(r"mCurClient=ClientState\{\S+ mUid=(\d+) mPid=\d+ mSelfReportedDisplayId=(-?\d+)")
_IME_DISPLAY = re.compile(r"mCurDisplayId=(-?\d+)")
_DISPLAY0_TOP = re.compile(r"Display #0 .*?topResumedActivity=ActivityRecord\{\S+ u\d+ (\S+?)[ }]", re.S)


@dataclass
class FocusSample:
    top_focused_display: int
    ime_shown: bool
    ime_client_uid: int
    ime_client_display: int
    ime_display: int
    user_top_activity: str

    def row(self) -> str:
        return (
            f"top focus={self.top_focused_display:<3} ime shown={str(self.ime_shown).lower():<5} "
            f"ime client uid={self.ime_client_uid} (display {self.ime_client_display}) ime display={self.ime_display}"
        )


def _int(pattern: re.Pattern, text: str, group: int = 1, default: int = -1) -> int:
    m = pattern.search(text)
    return int(m.group(group)) if m else default


def sample(adb: Adb) -> FocusSample:
    window = adb.shell("dumpsys", "window")
    ime = adb.shell("dumpsys", "input_method")
    activities = adb.shell("dumpsys", "activity", "activities")
    shown = _IME_SHOWN.search(ime)
    top = _DISPLAY0_TOP.search(activities)
    return FocusSample(
        top_focused_display=_int(_TOP_FOCUS, window),
        ime_shown=bool(shown and shown.group(1) == "true"),
        ime_client_uid=_int(_IME_CLIENT, ime, 1),
        ime_client_display=_int(_IME_CLIENT, ime, 2),
        ime_display=_int(_IME_DISPLAY, ime),
        user_top_activity=top.group(1) if top else "",
    )


def uid_of(adb: Adb, package: str) -> int:
    out = adb.shell("dumpsys", "package", package)
    m = re.search(r"userId=(\d+)", out) or re.search(r"appId=(\d+)", out)
    return int(m.group(1)) if m else -1
