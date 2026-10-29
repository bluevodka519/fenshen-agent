"""Puts the --demo windows in place on Windows.

scrcpy and the browser only roughly honour their position flags: they are off by the invisible resize
borders, and some launches landed tens of pixels away. So each window is placed once when it appears,
by its visible frame, and then left alone (the user can still move it). The agent mirror is reopened
for every task (fresh display) and gets placed again. Does nothing on other systems.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

USER_TITLE = "我的手机屏幕（实时投屏，只看不控）"
AGENT_TITLE_PREFIX = "分身屏（"
BROWSER_TITLE = "分身 · Agent 面板"
GAP = 10


def _windows() -> dict[int, str]:
    """Visible top-level windows: handle -> title."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                found[hwnd] = buf.value
        return True

    user32.EnumWindows(visit, 0)
    return found


def _rects(hwnd: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """(outer rect, visible frame); the difference is the invisible resize border."""
    from ctypes import wintypes

    outer, frame = wintypes.RECT(), wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(outer))
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)):
        frame = outer  # DWMWA_EXTENDED_FRAME_BOUNDS unavailable
    return (outer.left, outer.top, outer.right, outer.bottom), (frame.left, frame.top, frame.right, frame.bottom)


def _place(hwnd: int, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
    """Moves the window so its visible frame starts at (x, y); resizes the frame to w x h if given."""
    (ol, ot, orr, ob), (fl, ft, fr, fb) = _rects(hwnd)
    dl, dt, dr, db = fl - ol, ft - ot, orr - fr, ob - fb
    flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
    cx = cy = 0
    if w is None or h is None:
        flags |= 0x0001  # SWP_NOSIZE
    else:
        cx, cy = w + dl + dr, h + dt + db
    ctypes.windll.user32.SetWindowPos(hwnd, None, x - dl, y - dt, cx, cy, flags)


def close_stale_browser() -> None:
    """Closes demo browser windows left over from a run whose terminal was closed instead of Ctrl+C
    (the mirrors die with the terminal, the browser does not). App-mode windows carry exactly the
    page title; a normal browser window adds its own name, so the user's tabs are not matched."""
    if os.name != "nt":
        return
    for hwnd, title in _windows().items():
        if title == BROWSER_TITLE:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


class DemoLayout(threading.Thread):
    """Your screen | agent display | steps, left to right in the work area (left, top, right, bottom)."""

    def __init__(self, area: tuple[int, int, int, int]):
        super().__init__(daemon=True)
        self.area = area
        self.placed: set[int] = set()
        self.started = time.time()

    def run(self) -> None:
        if os.name != "nt":
            return
        while True:
            try:
                self._tick()
            except Exception:  # cosmetic: never take the dashboard down
                pass
            time.sleep(0.5)

    def _tick(self) -> None:
        left, top, right, bottom = self.area
        wins = _windows()
        user = next((h for h, t in wins.items() if t == USER_TITLE), None)
        agent = next((h for h, t in wins.items() if t.startswith(AGENT_TITLE_PREFIX)), None)
        browser = next((h for h, t in wins.items() if t == BROWSER_TITLE), None)

        x = left
        if user:
            if user not in self.placed:
                _place(user, left, top)
                self.placed.add(user)
            x = _rects(user)[1][2] + GAP
        # the agent mirror usually opens first: wait for the user's window to know where it goes
        if agent and agent not in self.placed and (user or time.time() - self.started > 10):
            _place(agent, x, top)
            self.placed.add(agent)
        if browser and browser not in self.placed:
            bx = left + (right - left) // 2
            if agent in self.placed:
                bx = max(bx, _rects(agent)[1][2] + GAP)
            _place(browser, bx, top, right - bx, bottom - top)
            self.placed.add(browser)
