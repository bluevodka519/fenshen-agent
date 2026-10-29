"""Local dashboard: live agent screen, task input, step-by-step evidence, and handoff buttons.

Standard library only (http.server), so `python -m fenshen web` runs without installing anything.
Binds to 127.0.0.1: the dashboard can start tasks on the phone, so it is not exposed to the network.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .agent import Session
from .runner import RUNS, Runner, start_mirror

STATIC = Path(__file__).with_name("static")


class _Server(ThreadingHTTPServer):
    # http.server sets SO_REUSEADDR, which on Windows lets a second dashboard bind the same port
    # silently; requests then land on whichever one, e.g. a stale one whose phone connection is gone
    # (seen 2026-10-29). Fail loudly instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class Dashboard:
    def __init__(self, runner: Runner):
        self.runner = runner
        self.lock = threading.Lock()
        self.session: Session | None = None
        self.thread: threading.Thread | None = None
        self.baseline = ""
        self.disturbance: dict | None = None
        self.started_at: float | None = None

    # events from the runner/agent (called on the agent thread)
    def on_event(self, kind: str, data: dict) -> None:
        if kind == "baseline":
            self.baseline = data["text"]
        elif kind == "disturbance":
            self.disturbance = data

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start_task(self, task: str) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("a task is already running")
            self.session = self.runner.new_session(task)
            self.baseline, self.disturbance = "", None
            self._spawn()

    def resume(self, note: str) -> None:
        with self.lock:
            s = self.session
            if self.running or s is None or s.outcome is None or s.outcome.status != "needs_user":
                raise RuntimeError("nothing is waiting for you")
            s.resume_note(note)
            self._spawn()

    def approve(self) -> None:
        with self.lock:
            s = self.session
            if self.running or s is None or s.outcome is None or s.outcome.status != "needs_user":
                raise RuntimeError("nothing is waiting for approval")
            s.approve()
            self._spawn()

    def abort(self) -> None:
        s = self.session
        if s is None:
            return
        s.abort.set()
        if not self.running and s.outcome and s.outcome.status == "needs_user":
            from .agent import Outcome

            s.outcome = Outcome("aborted", "你结束了这个任务", len(s.steps))

    def _spawn(self) -> None:
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._run, args=(self.session,), daemon=True)
        self.thread.start()

    def _run(self, session: Session) -> None:
        try:
            self.runner.run(session)
        except Exception as e:  # surface crashes on the dashboard instead of dying silently
            from .agent import Outcome

            session.outcome = Outcome("failed", f"内部错误：{e}", len(session.steps))
        if session.outcome and session.outcome.status == "needs_user":
            # The user may finish the last step on their own screen; notice it without a button press.
            run_thread = threading.current_thread()

            def keep_going() -> bool:
                return (self.session is session and session.outcome is not None
                        and session.outcome.status == "needs_user"
                        and (self.thread is run_thread or not self.running))

            threading.Thread(target=self.runner.watch_user_completion, args=(session, keep_going), daemon=True).start()

    def state(self) -> dict:
        s = self.session
        status = "idle"
        if s is not None:
            status = "running" if self.running else (s.outcome.status if s.outcome else "running")
            if self.running and s.waiting:
                status = "waiting"
        return {
            "status": status,
            "display_id": self.runner.device.display_id,
            "serial": self.runner.adb.serial,
            "model": self.runner.model_name,
            "task": s.task if s else "",
            "verifier": s.verifier.description if s else "",
            "baseline": self.baseline,
            "run_dir": s.run_dir.name if s else "",
            "outcome": asdict(s.outcome) if s and s.outcome else None,
            "waiting": s.waiting if s else None,
            "pending": ({"label": s.pending["label"], "message": s.pending["message"],
                         "app": self.runner.label(s.steps[-1].package) if s.steps else ""}
                        if s and s.pending and not self.running else None),
            "steps": [asdict(st) for st in s.steps] if s else [],
            "disturbance": (self.runner.live_disturbance(s) if s and self.running else None) or self.disturbance,
            "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at and self.running else None,
        }


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet; the run directory has the trace
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:  # e.g. curl from a Chinese-locale Windows console sends GBK
                text = raw.decode("gbk", errors="replace")
            return json.loads(text or "{}")

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(dash.state())
            elif path == "/api/screen.jpg":
                try:
                    jpeg = dash.runner.device.screenshot_jpeg(quality=65, max_width=420)
                except Exception:
                    jpeg = None
                if jpeg:
                    self._send(200, jpeg, "image/jpeg")
                else:
                    self._send(HTTPStatus.NO_CONTENT, b"", "image/jpeg")
            elif path.startswith("/runs/"):
                # step screenshots: /runs/<run dir>/<file>, confined to the runs directory
                target = (RUNS / unquote(path[len("/runs/"):])).resolve()
                if RUNS.resolve() in target.parents and target.is_file() and target.suffix == ".jpg":
                    self._send(200, target.read_bytes(), "image/jpeg")
                else:
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/run":
                    task = (body.get("task") or "").strip()
                    if not task:
                        return self._json({"error": "task is empty"}, 400)
                    dash.start_task(task)
                elif path == "/api/resume":
                    dash.resume((body.get("note") or "").strip())
                elif path == "/api/approve":
                    dash.approve()
                elif path == "/api/abort":
                    dash.abort()
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except RuntimeError as e:
                self._json({"error": str(e)}, 409)

    return Handler


# --demo on a 1920x1080 screen: the user's screen left, the agent display next to it, and the browser
# snapped to the right half (Win + Right) with the steps and the model's reasoning
DEMO_USER_WINDOW = (8, 31, 990)
DEMO_AGENT_WINDOW = (478, 31, 990)


def serve(serial: str | None = None, llm_spec: str | None = None, port: int = 8765, demo: bool = False) -> None:
    dash_ref: dict = {}
    runner = Runner(serial=serial, llm_spec=llm_spec, mirror=demo, mirror_window=DEMO_AGENT_WINDOW,
                    on_event=lambda kind, data: dash_ref["d"].on_event(kind, data))
    dash = Dashboard(runner)
    dash_ref["d"] = dash
    try:  # bind before touching the phone: a second dashboard must not take over the device
        server = _Server(("127.0.0.1", port), _make_handler(dash))
    except OSError as e:
        raise SystemExit(f"port {port} is in use (another dashboard still running?): {e}")
    runner.start()
    url = f"http://127.0.0.1:{port}" + ("/?demo" if demo else "")
    print(f"Fenshen dashboard: {url}  (agent display {runner.device.display_id}, model {runner.model_name})",
          flush=True)
    user_mirror = None
    if demo:
        user_mirror = start_mirror(runner.adb, 0, title="我的手机屏幕（实时投屏，只看不控）", window=DEMO_USER_WINDOW)
        if user_mirror is None or runner._mirror_proc is None:
            print("scrcpy not found: set FENSHEN_SCRCPY to scrcpy.exe to get the two phone windows", flush=True)
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if user_mirror:
            user_mirror.terminate()
        runner.close()
