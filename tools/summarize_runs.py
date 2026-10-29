"""Summarizes run records (runs/<dir>/) into one table: outcome, steps, model latency, disturbance.

    python tools/summarize_runs.py 20261028-1110 20261028-1130     # runs whose name sorts in [from, to)
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


def load(d: Path) -> dict | None:
    if not (d / "outcome.json").exists():
        return None
    outcome = json.loads((d / "outcome.json").read_text(encoding="utf-8"))
    steps = [json.loads(line) for line in (d / "steps.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (d / "steps.jsonl").exists() else []
    task = json.loads((d / "task.json").read_text(encoding="utf-8"))
    dist = json.loads((d / "disturbance.json").read_text()) if (d / "disturbance.json").exists() else {}
    llm = [s["llm_ms"] for s in steps if s.get("llm_ms")]
    wall = (steps[-1]["t"] - task["started"]) if steps else 0
    return {
        "run": d.name,
        "task": task["task"],
        "status": outcome["status"],
        "steps": len(steps),
        "invalid": sum(s["status"] == "invalid" for s in steps),
        "llm_ms_median": int(statistics.median(llm)) if llm else 0,
        "prompt_tokens_median": int(statistics.median([s["prompt_tokens"] for s in steps if s.get("prompt_tokens")] or [0])),
        "wall_s": round(wall),
        "focus_on_agent": dist.get("top_focus_on_agent_display"),
        "ime_on_agent": dist.get("ime_on_agent_display"),
        "user_ime_lost": dist.get("user_ime_lost_samples"),
        "samples": dist.get("samples"),
        "message": outcome["message"],
    }


def main(lo: str, hi: str) -> int:
    rows = [r for d in sorted(RUNS.iterdir()) if d.is_dir() and lo <= d.name < hi and (r := load(d))]
    for r in rows:
        print(f"{r['status']:<10} {r['steps']:>2} steps {r['wall_s']:>4}s llm~{r['llm_ms_median']}ms "
              f"disturb={r['focus_on_agent']}/{r['ime_on_agent']}/{r['user_ime_lost']} ({r['samples']} samples) | "
              f"{r['task']} | {r['message'][:80]}")
    done = [r for r in rows if r["status"] == "done"]
    print(f"\n{len(done)}/{len(rows)} done; steps of successful runs: {[r['steps'] for r in done]}; "
          f"median llm {int(statistics.median([r['llm_ms_median'] for r in rows])) if rows else 0} ms; "
          f"disturbance samples total {sum((r['focus_on_agent'] or 0) + (r['ime_on_agent'] or 0) + (r['user_ime_lost'] or 0) for r in rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
