"""Wires device + model + agent together, and records a disturbance timeline while a task runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .adb import Adb, AdbError
from .agent import Agent, ElementDecider, Outcome, Session
from .device import REPO, VirtualDisplayDevice
from .disturbance import sample
from .llm import from_env
from .notify import Notifier

RUNS = REPO / "runs"
CONNECTION_LOST = ("和手机的连接断了（数据线松动、电脑上的 adb 服务重启，或者有别的程序用了另一个版本的 adb），"
                   "分身屏随之被系统收回，这个任务没法接着做了。重新下达任务就行，我会重新建屏。")


def launcher_packages(adb: Adb) -> list[str]:
    out = adb.shell("cmd", "package", "query-activities", "--brief",
                    "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER")
    pkgs: list[str] = []
    for line in out.splitlines():
        m = re.match(r"\s*([\w.]+)/", line)
        if m and m.group(1) not in pkgs:
            pkgs.append(m.group(1))
    return pkgs


class DisturbanceMonitor:
    """Samples focus/IME ownership once a second (README §7). Only reads dumpsys; never touches the UI."""

    def __init__(self, adb: Adb, path: Path, agent_display: int, interval: float = 1.0):
        self.adb, self.path, self.agent_display, self.interval = adb, path, agent_display, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.samples = 0
        self.focus_on_agent = 0
        self.ime_on_agent = 0
        self.ime_shown_at_start: bool | None = None
        self.user_activity_at_start: str | None = None
        # samples where the user's keyboard was up at start and is now hidden although the user is
        # still on the same screen (if they navigated away themselves, the keyboard closing is theirs)
        self.ime_lost = 0

    def start(self) -> "DisturbanceMonitor":
        self._thread.start()
        return self

    def _loop(self) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            while not self._stop.is_set():
                try:
                    s = sample(self.adb)
                except Exception as e:  # monitoring must never break the task
                    f.write(json.dumps({"t": time.time(), "error": str(e)}) + "\n")
                else:
                    self.samples += 1
                    if self.ime_shown_at_start is None:
                        self.ime_shown_at_start = s.ime_shown
                        self.user_activity_at_start = s.user_top_activity
                    self.focus_on_agent += s.top_focused_display == self.agent_display
                    self.ime_on_agent += s.ime_display == self.agent_display or s.ime_client_display == self.agent_display
                    self.ime_lost += bool(self.ime_shown_at_start and not s.ime_shown
                                          and s.user_top_activity == self.user_activity_at_start)
                    f.write(json.dumps({"t": time.time(), **asdict(s)}, ensure_ascii=False) + "\n")
                f.flush()
                self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=5)
        return self.counts()

    def counts(self) -> dict:
        """The tallies so far; safe to call while sampling (the dashboard shows them live)."""
        return {
            "samples": self.samples,
            "top_focus_on_agent_display": self.focus_on_agent,
            "ime_on_agent_display": self.ime_on_agent,
            "user_ime_shown_at_start": self.ime_shown_at_start,
            "user_ime_lost_samples": self.ime_lost,
        }


def start_mirror(adb: Adb, display_id: int, title: str = "", window: tuple[int, int, int] = (20, 60, 720),
                 ports: str = "27200:27209") -> subprocess.Popen | None:
    """Opens a scrcpy window showing a display live (view only), so people can watch it work.
    The agent display is invisible on the phone by design; this window is for the demo and debugging.
    `window` is (x, y, height); the width follows the display's aspect ratio. View only: scrcpy then
    refuses --show-touches, so finger dots on the user's screen come from the phone's own developer
    option ("Show taps"), which the user turns on themselves.
    `ports`: scrcpy's default 27183 is the helper's forward; two mirrors sharing the fallback port race
    for it and one of them quits, so each mirror gets its own range.
    Optional: skipped when scrcpy is neither in FENSHEN_SCRCPY nor on PATH."""
    exe = os.environ.get("FENSHEN_SCRCPY") or shutil.which("scrcpy")
    if not exe or not Path(exe).exists():
        return None
    x, y, height = window
    return subprocess.Popen(
        [exe, "--serial", adb.serial, f"--display-id={display_id}", "--no-audio", "--no-control", f"--port={ports}",
         "--window-title", title or f"Fenshen agent display {display_id} (live)",
         "--window-x", str(x), "--window-y", str(y), "--window-height", str(height)],
        # scrcpy's bundled adb differs in version from the SDK's: mixing them restarts the adb server
        # and drops the helper's port forward, so make scrcpy use ours
        env={**os.environ, "ADB": adb.path},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class Runner:
    def __init__(self, serial: str | None = None, llm_spec: str | None = None, monitor: bool = True,
                 on_event: Callable[[str, dict], None] | None = None, max_steps: int = 0, mirror: bool = True,
                 notify: bool = True, mirror_window: tuple[int, int, int] = (20, 60, 720)):
        self.adb = Adb(serial)
        self.device = VirtualDisplayDevice(serial=self.adb.serial)
        self.llm_spec = llm_spec or os.environ.get("FENSHEN_LLM") or "ollama:qwen2.5:7b"
        self.llm = None if self.llm_spec.startswith("autoglm") else from_env(self.llm_spec)
        self.decider = None
        self.monitor = monitor
        self.mirror = mirror
        self.mirror_window = mirror_window
        self.user_event = on_event or (lambda kind, data: None)
        self.notifier = Notifier(self.adb) if notify else None
        self._labels: dict[str, str] | None = None
        self.max_steps = max_steps
        self._tasks_started = 0
        self.agent: Agent | None = None
        self._mirror_proc: subprocess.Popen | None = None
        self._mirror_display: int | None = None
        self._mirror_lock = threading.Lock()
        self._monitor: DisturbanceMonitor | None = None

    def on_event(self, kind: str, data: dict) -> None:
        self.user_event(kind, data)
        if kind == "waiting" and self.notifier:
            self.notifier.post("分身在等你", data["message"])

    def label(self, package: str) -> str:
        if self._labels is None:
            try:
                self._labels = {a["package"]: a["label"] for a in self.device.apps()}
            except Exception:
                self._labels = {}
        return self._labels.get(package, package)

    def notify_outcome(self, session: Session, outcome: Outcome) -> None:
        """One ordinary notification on the user's phone; never a full-screen prompt (README §5)."""
        if not self.notifier or outcome.status == "aborted":
            return
        msg = outcome.message if len(outcome.message) <= 160 else outcome.message[:159] + "…"
        if outcome.status == "done":
            self.notifier.post("分身办好了", f"{session.task}\n{msg}")
        elif outcome.status == "needs_user" and session.pending:
            app = self.label(session.steps[-1].package) if session.steps else "那个 App"
            self.notifier.post("分身需要你确认", f"{session.task}\n{msg}\n"
                               f"在面板上点「允许」，分身就去点；或者自己打开「{app}」，分身做好的部分会出现在你的屏幕上，最后一步由你来点。")
        elif outcome.status == "needs_user":
            self.notifier.post("分身需要你", f"{session.task}\n{msg}")
        else:
            self.notifier.post("分身没能完成", f"{session.task}\n{msg}")

    def watch_user_completion(self, session: Session, keep_going: Callable[[], bool],
                              timeout_s: float = 900, interval_s: float = 2.0) -> None:
        """After a handoff the user may do the last step themselves (e.g. open Messages, which brings
        the agent's draft to their screen, and press Send). Watch the system state, not the screen:
        once the goal is reached, verify it and close the task, so nobody has to press "continue"."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if session.abort.wait(interval_s) or not keep_going():
                return
            try:
                if not session.verifier.goal_reached(self.device) or not keep_going():
                    continue
                verdict = session.verifier.check(self.device, self.device.observe(), "")
            except Exception:
                continue
            session.pending = None
            prefix = "你在自己的屏幕上完成了最后一步。" + ("系统确认：" if verdict.ok else "但校验发现了问题：")
            outcome = self.agent._finish(session, "done" if verdict.ok else "needs_user", prefix + verdict.evidence)
            self.notify_outcome(session, outcome)
            return

    def start(self) -> "Runner":
        self.device.start()
        self.ensure_mirror()
        if self.llm is None:
            from .autoglm import AutoGLMDecider

            self.decider = AutoGLMDecider(self.device)
        else:
            self.decider = ElementDecider(self.llm, apps=launcher_packages(self.adb))
        # 0 = by decider: a vision model is meant for real-app flows (e.g. food ordering), which take more steps
        steps = self.max_steps or (40 if self.decider.vision else 25)
        self.agent = Agent(self.device, self.decider, max_steps=steps, on_event=self.on_event)
        return self

    @property
    def model_name(self) -> str:
        return self.decider.name if self.decider else self.llm_spec

    def new_session(self, task: str) -> Session:
        slug = re.sub(r"[^\w一-鿿]+", "-", task)[:24].strip("-")
        return Session(task, RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}")

    def run(self, session: Session) -> Outcome:
        assert self.agent is not None, "call start() first"
        if getattr(session, "prepared", False) and not self.device.alive():
            # resuming (approve / "I handled it") a task whose agent display went away with the connection
            outcome = self.agent._finish(session, "failed", CONNECTION_LOST)
            self._notify_quietly(session, outcome)
            return outcome
        if not getattr(session, "prepared", False):
            if self._tasks_started or not self.device.alive():
                # Every task starts on a fresh agent display, like a phone on its home screen. Reusing it
                # left the previous task's app (and an open permission dialog) in front: an SMS task then
                # spent 24 steps searching Contacts (2026-10-29). DESTROY_CONTENT_ON_REMOVAL cleans up.
                # This also brings the helper back after the connection to the phone dropped.
                try:
                    self.device.close()
                    self.device.start()
                except (AdbError, OSError, RuntimeError) as e:
                    outcome = self.agent._finish(session, "failed", f"连不上手机，任务没有开始（检查数据线和 USB 调试授权）：{e}")
                    return outcome
                self.ensure_mirror()  # the open window still points at the display that was just removed
            self._tasks_started += 1
            try:
                baseline = session.verifier.prepare(self.device)
                blocked = getattr(session.verifier, "blocked_by", None)
                if blocked:
                    # The baseline needs the app the user has open: wait for them, like the agent would.
                    msg = f"开始前需要先看一眼 {blocked} 里现有的内容，但它正在你的屏幕上。等你按 Home 退出它，我会自动开始"
                    if self.agent._wait_until_free(session, blocked, msg):
                        baseline = session.verifier.prepare(self.device)
            except Exception as e:  # no baseline, no trustworthy verdict: do not start the task
                outcome = self.agent._finish(session, "needs_user", f"开始前读取基线失败，任务没有开始：{e}")
                self.notify_outcome(session, outcome)
                return outcome
            session.prepared = True
            (session.run_dir / "baseline.txt").write_text(baseline, encoding="utf-8")
            self.on_event("baseline", {"text": baseline})
        mon = None
        if self.monitor:
            mon = self._monitor = DisturbanceMonitor(self.adb, session.run_dir / "monitor.jsonl", self.device.display_id).start()
        try:
            outcome = self.agent.run(session)
        except (ConnectionError, OSError, AdbError):
            if self.device.alive():
                raise
            outcome = self.agent._finish(session, "failed", CONNECTION_LOST)
        finally:
            if mon:
                self._monitor = None
                # A task paused for the user resumes in a new run(): report the whole task, not the last segment.
                part = mon.stop()
                total = getattr(session, "disturbance", None)
                if total is None:
                    total = session.disturbance = dict(part)
                else:
                    for k, v in part.items():
                        if isinstance(v, int) and not isinstance(v, bool):
                            total[k] = total.get(k, 0) + v
                (session.run_dir / "disturbance.json").write_text(json.dumps(total, indent=2), encoding="utf-8")
                self.on_event("disturbance", total)
        self._notify_quietly(session, outcome)
        return outcome

    def _notify_quietly(self, session: Session, outcome: Outcome) -> None:
        try:
            self.notify_outcome(session, outcome)
        except (AdbError, OSError):  # the phone may be the thing that went away
            pass

    def live_disturbance(self, session: Session) -> dict | None:
        """Disturbance tallies while the task runs: earlier segments of this task plus the current one."""
        mon = self._monitor
        if mon is None:
            return None
        total = dict(getattr(session, "disturbance", None) or {})
        for k, v in mon.counts().items():
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
            else:
                total.setdefault(k, v)
        return total

    def ensure_mirror(self) -> None:
        """Opens the agent display window if it is missing or shows a display that is gone: after each
        task's fresh display, and after the adb connection dropped (scrcpy quits with it). Safe to call
        from a watchdog thread; does nothing without mirroring or while the helper is down."""
        with self._mirror_lock:
            if not self.mirror or not self.device.alive():
                return
            running = self._mirror_proc is not None and self._mirror_proc.poll() is None
            if running and self._mirror_display == self.device.display_id:
                return
            if running:
                self._mirror_proc.terminate()
            self._mirror_display = self.device.display_id
            self._mirror_proc = start_mirror(self.adb, self.device.display_id, window=self.mirror_window,
                                             title=f"分身屏（手机里的隐藏虚拟屏 display {self.device.display_id}）")  # winlayout.AGENT_TITLE_PREFIX

    def close(self) -> None:
        if self._mirror_proc:
            self._mirror_proc.terminate()
            self._mirror_proc = None
        self.device.close()
