"""E3: can the agent read and write its own display (incl. Chinese text) without touching the user's?

The "user" types into Settings search on display 0. Meanwhile the agent, through fenshen-helper, opens
Contacts > new contact on its virtual display, focuses the name fields (node click and injected tap),
and writes Chinese text with ACTION_SET_TEXT. After every step we sample focus and IME ownership.

    python tools/e3_probe.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "brain"))

from fenshen.adb import Adb  # noqa: E402
from fenshen.device import VirtualDisplayDevice  # noqa: E402
from fenshen.disturbance import sample, uid_of  # noqa: E402
from fenshen.helper_client import ConflictError  # noqa: E402

USER_PKG = "com.google.android.settings.intelligence"
CONTACTS_PKG = "com.google.android.contacts"
FIRST, LAST = "张三", "分身测试 Fenshen"


def user_field_text(adb: Adb) -> str | None:
    """Reads the user's search box. Only valid once the helper's UiAutomation is gone (it is exclusive)."""
    adb.shell("uiautomator", "dump", "--windows", "/sdcard/fenshen_probe.xml", check=False)
    xml = adb.shell("cat", "/sdcard/fenshen_probe.xml", check=False)
    for node in xml.split("<node "):
        if f'package="{USER_PKG}"' in node and 'class="android.widget.EditText"' in node:
            m = re.search(r' text="([^"]*)"', node)
            return m.group(1) if m else ""
    return None


def user_setup(adb: Adb) -> None:
    for pkg in (CONTACTS_PKG, "com.google.android.deskclock", USER_PKG, "com.android.settings"):
        adb.shell("am", "force-stop", pkg)
    adb.shell("input", "-d", "0", "keyevent", "HOME")
    time.sleep(1)
    adb.shell("am", "start", "-W", "-n", "com.android.settings/.Settings")
    time.sleep(1.5)
    adb.shell("input", "-d", "0", "tap", "540", "250")  # homepage search bar -> search with keyboard
    time.sleep(2)
    adb.shell("input", "-d", "0", "text", "wifi")
    time.sleep(1)


def main() -> int:
    adb = Adb()
    rows: list[dict] = []

    def log(step: str) -> None:
        s = sample(adb)
        rows.append({"step": step, **s.__dict__})
        print(f"{step:<52} | {s.row()}")

    user_setup(adb)
    user_uid, contacts_uid = uid_of(adb, USER_PKG), uid_of(adb, CONTACTS_PKG)
    print(f"user app uid={user_uid}  agent target (Contacts) uid={contacts_uid}\n")
    log("baseline: user typing in Settings search")

    checks: dict[str, object] = {}
    device = VirtualDisplayDevice(serial=adb.serial).start()
    try:
        print(f"agent display: {device.display_id}")
        log("agent: helper + UiAutomation connected")

        device.am_start("-a", "android.intent.action.INSERT", "-t", "vnd.android.cursor.dir/contact")
        device.wait_idle(800, 8000)
        obs = device.observe()
        print("\n--- agent observation (compressed) ---\n" + obs.render() + "\n---\n")
        log("agent: opened Contacts > new contact")

        first = obs.find("First name", role="edit")
        last = obs.find("Last name", role="edit")
        if first is None or last is None:
            print("STOP: name fields not found on the agent screen (see observation above)")
            return 2

        how = device.click(first)
        device.wait_idle(500, 4000)
        log(f"agent: {how} on 'First name'")

        obs = device.observe()
        device.fill(obs.find("First name", role="edit"), FIRST)
        device.wait_idle(500, 4000)
        log(f"agent: set_text {FIRST!r}")

        obs = device.observe()
        x, y = obs.find("Last name", role="edit").center
        device.tap(x, y)
        device.wait_idle(500, 4000)
        log(f"agent: injected tap on 'Last name' ({x},{y})")

        obs = device.observe()
        device.fill(obs.find("Last name", role="edit"), LAST)
        device.wait_idle(500, 4000)
        log(f"agent: set_text {LAST!r}")

        obs = device.observe()
        texts = [e.text for e in obs.elements if e.editable]
        checks["agent_fields"] = texts
        checks["chinese_written"] = FIRST in texts and LAST in texts
        checks["ime_window_on_agent_display"] = [w for w in obs.windows if w.get("type") == "ime"]

        adb.shell("input", "text", "XYZ")  # the user keeps typing on a hardware keyboard
        time.sleep(1)
        log("user: types 'XYZ' (hardware keyboard)")

        jpeg = device.screenshot_jpeg(quality=80)
        if jpeg:
            out = REPO / "docs" / "img" / "e3-agent-contacts.jpg"
            out.write_bytes(jpeg)
            checks["screenshot"] = str(out.relative_to(REPO))

        try:
            device.launch("com.android.settings")
            checks["conflict_guard"] = "NOT TRIGGERED (bad)"
        except ConflictError as e:
            checks["conflict_guard"] = f"refused: {e}"
        log("agent: tried to launch Settings (user has it open)")
    finally:
        device.close()

    time.sleep(1.5)
    checks["user_field_text"] = user_field_text(adb)
    log("after helper closed")
    checks["contacts_alive_after_close"] = "com.google.android.contacts" in adb.shell("dumpsys", "activity", "activities")

    print()
    for k, v in checks.items():
        print(f"{k}: {v}")
    print(f"user's field expected 'wifiXYZ' -> {'OK' if checks['user_field_text'] == 'wifiXYZ' else 'MISMATCH'}")

    runs = REPO / "runs"
    runs.mkdir(exist_ok=True)
    report = runs / f"e3-{time.strftime('%Y%m%d-%H%M%S')}.json"
    report.write_text(json.dumps({"rows": rows, "checks": checks, "user_uid": user_uid, "contacts_uid": contacts_uid},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
