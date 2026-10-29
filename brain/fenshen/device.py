"""Where the agent acts. The brain only talks to `Device`; README D10 explains the two backends."""

from __future__ import annotations

import base64
import os
import queue
import threading
import time
from pathlib import Path
from typing import Protocol

from .adb import Adb, AdbError
from .helper_client import HelperClient
from .observation import Element, Observation, compress

REPO = Path(__file__).resolve().parents[2]  # works from a checkout or `pip install -e .`
HELPER_JAR = Path(os.environ.get("FENSHEN_HELPER_JAR") or REPO / "helper" / "build" / "fenshen-helper.jar")
DEVICE_JAR = "/data/local/tmp/fenshen-helper.jar"

KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_ENTER = 66


def _size_from_env() -> tuple[int, int, int]:
    """FENSHEN_VD_SIZE=WIDTHxHEIGHT/DPI. Default 720x1600/320: a modern 20:9 phone (360x800 dp),
    which is what screenshot-trained models such as AutoGLM-Phone have seen."""
    spec = os.environ.get("FENSHEN_VD_SIZE", "720x1600/320")
    size, _, dpi = spec.partition("/")
    w, _, h = size.partition("x")
    return int(w), int(h), int(dpi or 320)


class Device(Protocol):
    def observe(self) -> Observation: ...
    def click(self, element: Element) -> str: ...
    def fill(self, element: Element, text: str) -> str: ...
    def scroll(self, element: Element | None, direction: str) -> str: ...
    def back(self) -> str: ...
    def launch(self, package: str) -> str: ...
    def wait_idle(self, idle_ms: int = 500, timeout_ms: int = 5000) -> bool: ...
    def screenshot_jpeg(self, quality: int = 70, max_width: int = 0) -> bytes | None: ...
    def close(self) -> None: ...


class VirtualDisplayDevice:
    """Same phone, hidden virtual display owned by fenshen-helper (README §2, E2)."""

    def __init__(
        self,
        serial: str | None = None,
        port: int = 27183,
        size: tuple[int, int, int] | None = None,
        ime_policy: str = "hide",
        push_jar: bool = True,
    ):
        self.adb = Adb(serial)
        self.port = port
        self.size = size or _size_from_env()
        self.ime_policy = ime_policy
        self.push_jar = push_jar
        self.display_id: int | None = None
        self._proc = None
        self._client: HelperClient | None = None
        self._log: list[str] = []

    # ------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 20) -> "VirtualDisplayDevice":
        if self.adb.run("get-state").strip() != "device":
            raise AdbError(f"{self.adb.serial} is not ready")
        if self.push_jar:
            if not HELPER_JAR.exists():
                raise FileNotFoundError(f"{HELPER_JAR} missing; run helper/build.sh")
            self.adb.run("push", str(HELPER_JAR), DEVICE_JAR)
        self.adb.shell("pkill", "-f", "com.fenshen.helper", check=False)

        width, height, dpi = self.size
        self._proc = self.adb.popen(
            "shell", f"CLASSPATH={DEVICE_JAR}", "app_process", "/", "com.fenshen.helper.Main", "serve",
            "--width", str(width), "--height", str(height), "--dpi", str(dpi), "--ime-policy", self.ime_policy,
        )
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, args=(lines,), daemon=True).start()

        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("helper did not start:\n" + "\n".join(self._log[-20:]))
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line.startswith("DISPLAY_ID="):
                self.display_id = int(line.split("=", 1)[1])
            if line.startswith("RPC="):
                break
            if self._proc.poll() is not None:
                raise RuntimeError("helper exited:\n" + "\n".join(self._log[-20:]))

        self.adb.run("forward", f"tcp:{self.port}", "localabstract:fenshen")
        self._client = HelperClient(port=self.port)
        info = self._client.call("info")
        if info["display_id"] != self.display_id or self.display_id == 0:
            raise RuntimeError(f"helper reports display {info['display_id']}, expected {self.display_id}")
        return self

    def _pump(self, lines: "queue.Queue[str]") -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.rstrip()
            self._log.append(line)
            lines.put(line)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        self.adb.shell("pkill", "-f", "com.fenshen.helper", check=False)
        self.adb.run("forward", "--remove", f"tcp:{self.port}", check=False)
        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "VirtualDisplayDevice":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def rpc(self) -> HelperClient:
        if self._client is None:
            raise RuntimeError("device not started")
        return self._client

    # ------------------------------------------------------------ observation

    def observe(self) -> Observation:
        return compress(self.rpc.call("snapshot"))

    def wait_idle(self, idle_ms: int = 500, timeout_ms: int = 5000) -> bool:
        return self.rpc.call("wait_idle", idle_ms=idle_ms, timeout_ms=timeout_ms)["idle"]

    def wait_frames_idle(self, idle_ms: int = 200, timeout_ms: int = 1500) -> None:
        """Wait until the display has stopped producing frames for idle_ms: the accessibility tree
        settles before the last frame is composed, and transient windows (e.g. the task-snapshot
        starting window shown when an app relaunches) are replaced by the real content."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            since = self.rpc.call("info")["ms_since_last_frame"]
            if since < 0 or since >= idle_ms:
                return
            time.sleep(max(0.03, (idle_ms - since) / 1000))

    def screenshot_jpeg(self, quality: int = 70, max_width: int = 0) -> bytes | None:
        result = self.rpc.call("screenshot", quality=quality, max_width=max_width)
        b64 = result.get("jpeg_b64")
        return base64.b64decode(b64) if b64 else None

    def foreign_visible_packages(self) -> list[str]:
        return self.rpc.call("foreign_visible_packages")["packages"]

    # ------------------------------------------------------------ actions

    def click(self, element: Element) -> str:
        """Node click first (no input event at all); injected tap at the center as fallback."""
        if element.clickable and self.rpc.call("node_action", node=element.node, action="click")["performed"]:
            return "node_click"
        x, y = element.center
        self.rpc.call("tap", x=x, y=y)
        return f"tap({x},{y})"

    def fill(self, element: Element, text: str) -> str:
        """Sets the text directly on the node: no IME, no clipboard, Unicode-safe."""
        if not self.rpc.call("node_action", node=element.node, action="set_text", text=text)["performed"]:
            raise RuntimeError(f"set_text refused by [{element.idx}] {element.label!r}")
        return "set_text"

    def submit(self, element: Element) -> str:
        """Equivalent of pressing Enter/Search on the keyboard, without a keyboard."""
        if self.rpc.call("node_action", node=element.node, action="ime_enter")["performed"]:
            return "ime_enter"
        self.rpc.call("key", code=KEYCODE_ENTER)
        return "key_enter"

    def scroll(self, element: Element | None, direction: str) -> str:
        if element is not None and element.scrollable:
            action = "scroll_forward" if direction in ("down", "forward", "right") else "scroll_backward"
            if self.rpc.call("node_action", node=element.node, action=action)["performed"]:
                return action
        width, height, _ = self.size
        x = width // 2
        y1, y2 = (int(height * 0.75), int(height * 0.3)) if direction in ("down", "forward") else (int(height * 0.3), int(height * 0.75))
        self.rpc.call("swipe", x1=x, y1=y1, x2=x, y2=y2, ms=300)
        return f"swipe({x},{y1}->{x},{y2})"

    def tap(self, x: int, y: int) -> str:
        self.rpc.call("tap", x=x, y=y)
        return f"tap({x},{y})"

    def back(self) -> str:
        # Never GLOBAL_ACTION_BACK: it goes to the top focused display, i.e. the user's (README D6).
        self.rpc.call("key", code=KEYCODE_BACK)
        return "key_back"

    def home(self) -> str:
        self.rpc.call("key", code=KEYCODE_HOME)  # stamped with the agent display id by the helper
        return "key_home"

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> str:
        self.rpc.call("swipe", x1=x1, y1=y1, x2=x2, y2=y2, ms=ms)
        return f"swipe({x1},{y1}->{x2},{y2})"

    def long_press(self, x: int, y: int, ms: int = 800) -> str:
        self.rpc.call("swipe", x1=x, y1=y, x2=x, y2=y, ms=ms)
        return f"long_press({x},{y})"

    def double_tap(self, x: int, y: int) -> str:
        self.rpc.call("tap", x=x, y=y)
        time.sleep(0.08)
        self.rpc.call("tap", x=x, y=y)
        return f"double_tap({x},{y})"

    def is_running(self, package: str) -> bool:
        return bool(self.adb.shell("pidof", package, check=False).strip())

    def apps(self) -> list[dict]:
        """[{package, label}] of launcher apps, labels as the user sees them (e.g. 时钟, 美团)."""
        return self.rpc.call("apps")["apps"]

    def launch(self, package: str) -> str:
        result = self.rpc.call("launch", package=package)
        return result["component"]

    def am_start(self, *args: str) -> str:
        result = self.rpc.call("am_start", args=list(args))
        return result["component"]
