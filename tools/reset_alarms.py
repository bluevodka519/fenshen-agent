"""Test-device cleanup: switch OFF every enabled alarm in the Clock app (nothing is deleted).

Runs on the agent's virtual display like everything else, so it never touches the user's screen.

    python tools/reset_alarms.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))

from fenshen.device import VirtualDisplayDevice  # noqa: E402
from fenshen.verify import next_alarm_clock, open_clock, scan_alarm_rows  # noqa: E402


def main() -> int:
    with VirtualDisplayDevice() as dev:
        open_clock(dev)

        def switch_off(obs):
            for e in obs.elements:
                if e.role == "switch" and e.label.endswith(" alarm") and e.checked:
                    print("switching off:", e.label)
                    dev.click(e)
                    dev.wait_idle(400, 3000)

        rows = scan_alarm_rows(dev, on_page=switch_off)
        print(f"{len(rows)} alarms in the list")
        print("next alarm clock now:", next_alarm_clock(dev.adb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
