"""
    python -m fenshen run "帮我定一个早上7点30的闹钟"     run one task in the terminal
    python -m fenshen web [--port 8765]                   local dashboard (live agent screen, steps, handoff)

Run from the brain/ directory (or put brain/ on PYTHONPATH).
Environment: FENSHEN_SERIAL (device), FENSHEN_LLM (default ollama:qwen2.5:7b), OPENAI_API_KEY / OPENAI_BASE_URL.
"""

from __future__ import annotations

import argparse
import sys


def _print_event(kind: str, data: dict) -> None:
    if kind == "thinking":
        print(f"\n[{data['step']}] thinking…", flush=True)
    elif kind == "step":
        d = data["decision"]
        print(f"    screen: {data['package']}  ({data['signature']})")
        if d:
            print(f"    model ({data['llm_ms']} ms, {data.get('prompt_tokens')}+{data.get('output_tokens')} tok): "
                  f"{d.get('action')} target={d.get('target')} "
                  f"text={d.get('text')!r} pkg={d.get('package')!r}")
            print(f"      why: {d.get('thought')}")
            print(f"      expect: {d.get('expect')}")
        line = f"    -> {data['status']}"
        if data.get("executed"):
            line += f" via {data['executed']}"
        if data.get("changed") is not None:
            line += f", screen {'changed' if data['changed'] else 'UNCHANGED'}"
        if data.get("detail"):
            line += f" | {data['detail']}"
        print(line, flush=True)
    elif kind == "outcome":
        print(f"\n== {data['status'].upper()}: {data['message']} ({data['steps']} steps)", flush=True)
    elif kind == "waiting":
        print(f"    .. waiting: {data['message']}", flush=True)
    elif kind == "resumed":
        print(f"    .. {data['package']} is free again, continuing", flush=True)
    elif kind == "baseline":
        print(f"== verifier baseline: {data['text']}", flush=True)
    elif kind == "disturbance":
        print(f"== disturbance monitor: {data}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fenshen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one task")
    run.add_argument("task")
    run.add_argument("--serial")
    run.add_argument("--llm")
    run.add_argument("--max-steps", type=int, default=0, help="0 = by model")
    run.add_argument("--no-monitor", action="store_true")
    web = sub.add_parser("web", help="local dashboard")
    web.add_argument("--serial")
    web.add_argument("--llm")
    web.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        from .runner import Runner

        runner = Runner(serial=args.serial, llm_spec=args.llm, monitor=not args.no_monitor,
                        on_event=_print_event, max_steps=args.max_steps)
        runner.start()
        try:
            print(f"agent display {runner.device.display_id} on {runner.adb.serial}, model {runner.model_name}")
            session = runner.new_session(args.task)
            print(f"verifier: {session.verifier.description}\ntrace: {session.run_dir}")
            outcome = runner.run(session)
        finally:
            runner.close()
        return 0 if outcome.status == "done" else 1

    if args.cmd == "web":
        from .web import serve

        serve(serial=args.serial, llm_spec=args.llm, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
