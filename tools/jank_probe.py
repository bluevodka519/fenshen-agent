"""E8: does the agent make the user's app janky?

A simulated user keeps scrolling Settings on the main display while the agent is (A) not running,
(B) running but idle (helper, virtual display, UiAutomation connected), (C) doing a real task on
its own display. For each window, `dumpsys gfxinfo` of the user's app gives frame counts, janky
frames and frame-time percentiles. Conditions are interleaved (A B C A B C ...) so drift in the
emulator's host load spreads over all of them.

    python tools/jank_probe.py --reps 3 --seconds 20 --task "帮我定一个早上7点15的闹钟" --llm autoglm

Writes runs/e8-<time>.json. Note: an emulator renders through the host GPU, so absolute numbers say
little about a phone; the comparison between conditions is the point.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))

from fenshen.adb import Adb  # noqa: E402
from fenshen.device import VirtualDisplayDevice  # noqa: E402
from fenshen.disturbance import sample  # noqa: E402

USER_PKG = "com.android.settings"
RUNS = Path(__file__).resolve().parents[1] / "runs"


def gfx(adb: Adb) -> dict:
    out = adb.shell("dumpsys", "gfxinfo", USER_PKG)

    def num(pattern: str, default: float = 0.0) -> float:
        m = re.search(pattern, out)
        return float(m.group(1)) if m else default

    return {
        "frames": int(num(r"Total frames rendered: (\d+)")),
        "janky": int(num(r"Janky frames: (\d+)")),
        "p50_ms": num(r"50th percentile: (\d+)ms"),
        "p90_ms": num(r"90th percentile: (\d+)ms"),
        "p99_ms": num(r"99th percentile: (\d+)ms"),
    }


def user_scrolls(adb: Adb, seconds: float) -> int:
    """The simulated user: flings the Settings list down and up, on display 0 only."""
    n, deadline = 0, time.time() + seconds
    while time.time() < deadline:
        y1, y2 = (1800, 700) if n % 2 == 0 else (700, 1800)
        adb.shell("input", "-d", "0", "swipe", "540", str(y1), "540", str(y2), "220", check=False)
        n += 1
        time.sleep(0.15)
    return n


def user_app_in_front(adb: Adb) -> bool:
    out = adb.shell("dumpsys", "activity", "activities", check=False)
    # one section per display; only display 0 (the user's) counts
    section = re.search(r"Display #0 .*?(?=\nDisplay #|\Z)", out, re.S)
    m = re.search(r"(?:topResumedActivity|ResumedActivity):? ?=?ActivityRecord\{\S+ \S+ (\S+)/", section.group(0) if section else "")
    return bool(m) and m.group(1) == USER_PKG


def bring_user_app(adb: Adb) -> None:
    """Fresh Settings task in front of the main display (a stale task can refuse to come forward)."""
    if user_app_in_front(adb):
        return
    adb.shell("input", "-d", "0", "keyevent", "HOME", check=False)
    adb.shell("am", "force-stop", USER_PKG, check=False)
    adb.shell("am", "start", "-W", "--display", "0", "-f", "0x10008000", "-n", f"{USER_PKG}/.Settings", check=False)
    time.sleep(1.5)


def measure(adb: Adb, seconds: float) -> dict:
    bring_user_app(adb)
    in_front = user_app_in_front(adb)
    adb.shell("dumpsys", "gfxinfo", USER_PKG, "reset", check=False)
    swipes = user_scrolls(adb, seconds)
    s = sample(adb)
    g = gfx(adb)
    g["swipes"] = swipes
    g["janky_pct"] = round(100 * g["janky"] / g["frames"], 1) if g["frames"] else None
    g["top_focused_display_after"] = s.top_focused_display
    g["valid"] = in_front and user_app_in_front(adb) and g["frames"] > 0
    return g


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--task", default="帮我定一个早上7点15的闹钟")
    ap.add_argument("--llm", default="autoglm")
    args = ap.parse_args()

    adb = Adb()
    adb.shell("pkill", "-f", "com.fenshen.helper", check=False)
    bring_user_app(adb)

    results: list[dict] = []
    for rep in range(args.reps):
        # A: nothing of ours on the phone
        adb.shell("pkill", "-f", "com.fenshen.helper", check=False)
        time.sleep(2)
        results.append({"condition": "A_no_agent", "rep": rep, **measure(adb, args.seconds)})
        print(results[-1], flush=True)

        # B: helper + virtual display + UiAutomation, idle
        with VirtualDisplayDevice(serial=adb.serial) as dev:
            dev.observe()
            time.sleep(2)
            results.append({"condition": "B_agent_idle", "rep": rep, **measure(adb, args.seconds)})
            print(results[-1], flush=True)

        # C: a real task on the agent display while the user scrolls
        from fenshen.runner import Runner

        runner = Runner(serial=adb.serial, llm_spec=args.llm, mirror=False, notify=False)
        runner.start()
        try:
            session = runner.new_session(args.task)
            done = threading.Event()
            t = threading.Thread(target=lambda: (runner.run(session), done.set()), daemon=True)
            t.start()
            time.sleep(6)  # let it get past the baseline read and into the app
            m = measure(adb, args.seconds)
            results.append({"condition": "C_agent_working", "rep": rep, "steps_during": len(session.steps),
                            "task_status": session.outcome.status if session.outcome else "running", **m})
            print(results[-1], flush=True)
            session.abort.set()
            t.join(timeout=60)
        finally:
            runner.close()

    summary = {}
    for cond in ("A_no_agent", "B_agent_idle", "C_agent_working"):
        rows = [r for r in results if r["condition"] == cond and r["valid"]]
        if rows:
            summary[cond] = {
                "windows": len(rows),
                "discarded": sum(1 for r in results if r["condition"] == cond and not r["valid"]),
                "frames_median": int(statistics.median(r["frames"] for r in rows)),
                "janky_pct_median": statistics.median(r["janky_pct"] for r in rows),
                "p90_ms_median": statistics.median(r["p90_ms"] for r in rows),
                "p99_ms_median": statistics.median(r["p99_ms"] for r in rows),
                "focus_ever_on_agent": any(r["top_focused_display_after"] not in (0, None) for r in rows),
            }
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"e8-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("written", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
