"""Local dashboard: live agent screen, task input, step-by-step evidence, and handoff buttons.

Standard library only (http.server), so `python -m fenshen web` runs without installing anything.
Binds to 127.0.0.1: the dashboard can start tasks on the phone, so it is not exposed to the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .agent import Session
from . import winlayout
from .device import REPO
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


def _work_area() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary screen without the taskbar."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, 1920, 1032


def demo_layout() -> dict:
    """--demo: the user's screen, the agent display and the steps side by side on the primary screen.
    Phones are (x, y, height) for scrcpy, which keeps the ~9:20 aspect ratio; the browser is (x, y, w, h)."""
    left, top, right, bottom = _work_area()
    title, border = 31, 8  # Windows 11 title bar and resize border; scrcpy positions the client area
    h = bottom - top - title - border
    phone_w = int(h * 0.45) + 2 * border
    bx = max(left + (right - left) // 2, left + 2 * phone_w + 20)
    return {"user": (left + border, top + title, h),
            "agent": (left + phone_w + 10 + border, top + title, h),
            "browser": (bx, top, right - bx, bottom - top)}


def _open_demo_browser(url: str, box: tuple[int, int, int, int]) -> subprocess.Popen | None:
    """An app-mode window (no tabs, no address bar) at a fixed place. It gets its own profile, so the
    position flags apply even when the browser is already open. Falls back to the default browser."""
    candidates = [os.environ.get("FENSHEN_BROWSER", "")]
    for base in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                              os.environ.get("LOCALAPPDATA"))):
        candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                       str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    exe = next((c for c in candidates if c and Path(c).exists()), None)
    if exe is None:
        webbrowser.open(url)
        return None
    x, y, w, h = box
    return subprocess.Popen([exe, f"--app={url}", f"--window-position={x},{y}", f"--window-size={w},{h}",
                             f"--user-data-dir={REPO / '.demo-browser'}", "--no-first-run",
                             "--no-default-browser-check"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _minimize_console() -> None:
    """Keeps the terminal from covering the layout (best effort; Windows Terminal may ignore it)."""
    if os.name == "nt":
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


class _DemoMirrors(threading.Thread):
    """Keeps both phone windows open during --demo. scrcpy quits when the adb connection drops (a loose
    cable, an adb server restart); once the phone is back, the windows come back by themselves."""

    def __init__(self, runner: Runner, user_window: tuple[int, int, int]):
        super().__init__(daemon=True)
        self.runner, self.user_window = runner, user_window
        self.user: subprocess.Popen | None = None
        self._stop = threading.Event()

    def open_user(self) -> bool:
        self.user = start_mirror(self.runner.adb, 0, title=winlayout.USER_TITLE, window=self.user_window,
                                 ports="27210:27219")
        return self.user is not None

    def run(self) -> None:
        while not self._stop.wait(5):
            try:
                if self.user is not None and self.user.poll() is not None:
                    if self.runner.adb.run("get-state", check=False).strip() == "device":
                        self.open_user()
                self.runner.ensure_mirror()  # no-op until the helper is back (the next task rebuilds it)
            except Exception:  # cosmetic: never take the dashboard down
                pass

    def stop(self) -> None:
        self._stop.set()
        if self.user:
            self.user.terminate()


def serve(serial: str | None = None, llm_spec: str | None = None, port: int = 8765, demo: bool = False) -> None:
    dash_ref: dict = {}
    layout = demo_layout() if demo else None
    runner = Runner(serial=serial, llm_spec=llm_spec, mirror=demo,
                    mirror_window=layout["agent"] if layout else (20, 60, 720),
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
    mirrors = _DemoMirrors(runner, layout["user"]) if layout else None
    browser = None
    if layout:
        time.sleep(2)  # both scrcpy instances push the same server file to the phone: don't overlap
        if not mirrors.open_user() or runner._mirror_proc is None:
            print("scrcpy not found: set FENSHEN_SCRCPY to scrcpy.exe to get the two phone windows", flush=True)
        mirrors.start()
        winlayout.close_stale_browser()
        browser = _open_demo_browser(url, layout["browser"])
        winlayout.DemoLayout(_work_area()).start()
        print("录屏布局：左 你的手机屏幕 | 中 分身屏 | 右 每一步。按 Ctrl+C 结束，三个窗口会一起关掉。", flush=True)
        _minimize_console()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mirrors:
            mirrors.stop()
        if browser:
            browser.terminate()
        runner.close()
