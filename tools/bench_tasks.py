"""Runs tasks one after another through the dashboard API and summarizes success, steps and time.

Start the dashboard first (`python -m fenshen web`), then:

    python tools/bench_tasks.py "帮我定一个早上6点30的闹钟" "帮我定一个早上6点20的闹钟"
    python tools/bench_tasks.py --approve-send "给13800001234发短信：我到楼下了"   # test SIM only

Each run's full trace stays in runs/<run dir> as usual.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def call(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


SEND = re.compile(r"\bsend\b|发送", re.I)


def run(task: str, approve_send: bool = False, timeout_s: float = 300) -> dict:
    """One task through the dashboard. With approve_send, the script plays the user for exactly one
    kind of step, sending a message (test numbers on an emulator SIM); everything else that needs
    the user (payment, login, delete, permission grants) ends the task as "needs_user"."""
    call("/api/run", {"task": task})
    t0 = time.time()
    approvals = []
    while True:
        time.sleep(2)
        s = call("/api/state")
        pending = s.get("pending")
        if (approve_send and s["status"] == "needs_user" and pending and SEND.search(pending["label"])
                and len(approvals) < 2):
            approvals.append(pending["label"])
            call("/api/approve", {})
            continue
        if s["status"] not in ("running", "waiting") or time.time() - t0 > timeout_s:
            break
    steps = s["steps"]
    llm = [x["llm_ms"] for x in steps if x["llm_ms"]]
    return {
        "task": task,
        "status": s["status"],
        "message": (s["outcome"] or {}).get("message", ""),
        "steps": len(steps),
        "invalid": sum(x["status"] == "invalid" for x in steps),
        "approved": approvals,
        "wall_s": round(time.time() - t0),
        "llm_ms_median": int(statistics.median(llm)) if llm else 0,
        "run_dir": s["run_dir"],
        "disturbance": s["disturbance"],
    }


def main(argv: list[str]) -> int:
    approve_send = "--approve-send" in argv
    tasks = [a for a in argv if a != "--approve-send"]
    results = []
    for task in tasks:
        r = run(task, approve_send)
        results.append(r)
        print(f"{r['status']:<10} {r['steps']:>2} steps ({r['invalid']} invalid, approved {r['approved']}) {r['wall_s']:>4}s "
              f"llm~{r['llm_ms_median']}ms | {task} | {r['message'][:90]}", flush=True)
    ok = sum(r["status"] == "done" for r in results)
    print(f"\n{ok}/{len(results)} done")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
