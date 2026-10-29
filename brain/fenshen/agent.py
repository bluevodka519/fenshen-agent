"""The agent loop: observe -> decide one action -> validate -> guard -> re-observe -> act -> check.

How the next action is chosen is pluggable (a "decider"):
  * ElementDecider: a text model picks from the numbered element list (works with small local models)
  * AutoGLMDecider (autoglm.py): a phone-GUI vision model looks at the screenshot and answers with coordinates
Everything else is shared and model-independent: the action must fit the current screen, sensitive
steps are guarded deterministically, the screen is re-read right before acting, the environment (not
the model) decides when the goal is reached, and every step is recorded with its evidence.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import skills
from .adb import Adb
from .device import VirtualDisplayDevice
from .helper_client import ConflictError, HelperError
from .llm import LLM, LLMError
from .observation import Element, Observation
from .verify import Verifier, verifier_for, wanted_quantity

# Actions the loop can execute. Point-based ones carry d["point"] (agent-display pixels).
ACTIONS = ["launch", "tap", "fill", "submit", "scroll", "back", "wait", "ask_user", "done",
           "swipe", "long_press", "double_tap", "home", "note"]
POINT_ACTIONS = ("long_press", "double_tap")

# ------------------------------------------------------------------ element decider (text models)

ELEMENT_ACTIONS = ["launch", "tap", "fill", "submit", "scroll", "back", "wait", "ask_user", "done"]

# Kept deliberately small: on an 8 GB GPU shared with the emulator, decoding (~20 tok/s) dominates
# each step, so every output token costs time. `text` carries whatever the action needs.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ELEMENT_ACTIONS},
        "target": {"type": "integer"},
        "text": {"type": "string"},
    },
    "required": ["thought", "action", "target", "text"],
}

SYSTEM_PROMPT = """你是"分身"：在用户手机的一块隐藏屏幕上，替用户操作 App 完成任务的助手。用户此刻正在用自己的屏幕，你看不到也碰不到它。
每一步你会看到当前屏幕的元素列表（带编号），你必须只返回一个动作（JSON）。

动作（target 填元素编号，不需要时填 0）：
- launch：打开 App，text 填包名。
- tap：点击元素。
- fill：往输入框直接写入文字，text 填内容（不需要先点击，也不需要键盘；要先选中才能输入的字段也可以直接 fill）。
- submit：在输入框上执行"回车/搜索"。
- scroll：滚动列表，text 填 down 或 up。
- back：返回上一页。
- wait：页面在加载时等一下。
- ask_user：你无法继续时交给用户，text 写原因。
- done：任务已完成且屏幕上能看到结果，text 写屏幕上能证明完成的原文。

规则：
1. target 只能用当前列表里的编号。标着 ·static 的是纯文字，不能点。
2. 付款、密码、验证码、登录必须由用户本人完成；删除、同意协议这类操作，直接选那个按钮即可，系统会自动请用户确认。
3. 看不懂当前页面、或者不知道怎么继续，用 ask_user 说明原因，不要乱点。
4. 上一步"屏幕无变化"说明没起作用，换一个做法。
5. 结果已经出现在屏幕上就用 done，不要再问用户。
6. 只做任务要求的事：不要关掉或修改与任务无关的东西，不要重复创建。用户拒绝过的操作不要换别的方法绕过。
7. thought 不超过 20 个字。"""

# Must be done by the user themself: the agent never performs these, even with approval.
HANDOFF_ONLY = re.compile(
    # "近期400+人已下单", "1000人付款", "已购买": sales counts and order states on product cards, not buttons
    # (a Meituan run was stopped tapping "+" on such a card)
    r"(?<![已人])(?:支付|付款|购买|下单)|提交订单|确认订单|登录|登入|注册|密码|验证码|(?<!不)开通|(?<!不)续费|免密"
    r"|\bpay\b|payment|purchase|\bbuy\b|place order|checkout|sign in|log in|login|password|verification code|\botp\b",
    re.I,
)
# The model's own statement of what it is about to tap (its last sentence). Used when the button has no
# readable label: commerce apps often draw their own UI, and a guard that only reads labels would let a
# screenshot model tap "提交订单" unseen.
INTENT_HANDOFF = re.compile(r"(点|按|tap|click)[^。.!！?？]{0,12}(提交订单|去支付|立即支付|确认支付|付款|下单|pay)", re.I)
# The "+" of a quantity stepper; Meituan labels it "增加，已添加16份" (the count is right there).
STEPPER_ADD = re.compile(r"^(?:增加|加一份|\+|＋)\D{0,8}?(\d+)\s*(?:份|件|个|杯|串)")
# A shop's required-item sheet ("请选择必选品后才能下单"): one choice for the whole order, e.g. a spice level.
REQUIRED_HEADER = re.compile(r"必选品")
SHEET_END = re.compile(r"^(?:到手约|去结算|未点必选品|购物车，)")
ADD_NEW = re.compile(r"^\d+份起购$|^选规格$|^加入购物车$")
# The button that leaves the shop for the order page. When it does nothing, a hint the model cannot see
# (a toast: required item missing, below the minimum, purchase limit) is the usual cause.
CHECKOUT = re.compile(r"去结算|^结算|去下单")
# Subset of HANDOFF_ONLY: a first tap is blocked with feedback instead of ending the task (see run()).
LOGIN = re.compile(r"登录|登入|注册|sign in|log in|login", re.I)

# Irreversible but fine to delegate: the agent may do these after the user approves that one step.
# Sending a message speaks for the user to someone else, so it is approved step by step as well.
# Granting a runtime permission ("Allow", "允许") counts too; declining ("Don't allow", "不允许") does not
# (run 20261028-120009: a notification-permission dialog came up and only the model's choice kept it safe).
# "Send to (380) 000-1234" is Messages' recipient suggestion, not sending (E9): excluded.
NEEDS_CONFIRM = re.compile(r"删除|移除|清空|同意|授权|发送(?!给)|(?<!不)允许|delete|remove|erase|\bagree\b|accept|\bsend\b(?!\s+to\b)"
                           r"|(?<![’']t )\ballow\b|only this time|仅此一次", re.I)


@dataclass
class Decision:
    d: dict
    llm_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    raw: str = ""


class Decider(Protocol):
    name: str
    vision: bool  # True: works from pixels, so screens without accessibility nodes are fine

    def decide(self, session: "Session", obs: Observation, feedback: str) -> Decision: ...


class ElementDecider:
    """A text model chooses one action against the numbered element list."""

    vision = False

    def __init__(self, llm: LLM, apps: list[str] | None = None):
        self.llm = llm
        self.name = llm.name
        self.apps = apps or []

    def decide(self, session: "Session", obs: Observation, feedback: str) -> Decision:
        reply = self.llm.decide(SYSTEM_PROMPT, self._prompt(session, obs, feedback), ACTION_SCHEMA)
        return Decision(self._normalize(reply.data), reply.latency_ms, reply.prompt_tokens, reply.output_tokens, reply.raw)

    def _prompt(self, session: "Session", obs: Observation, feedback: str) -> str:
        parts = [f"任务：{session.task}"]
        if self.apps and Agent._is_home(obs):  # only needed to pick what to launch
            parts.append("可用的 App 包名：" + ", ".join(self.apps))
        for hint in skills.relevant(session.task, obs.package, installed=set(self.apps) or None):
            parts.append(hint)
        if session.history:
            parts.append("已执行的步骤：\n" + "\n".join(session.history[-8:]))
        if feedback:
            parts.append("提示：" + feedback)
        parts.append("当前屏幕：\n" + obs.render())
        parts.append("请给出下一步动作。")
        return "\n\n".join(parts)

    @staticmethod
    def _normalize(data: dict) -> dict:
        d = {k: data.get(k) for k in ACTION_SCHEMA["properties"]}
        d["action"] = str(d.get("action") or "").strip()
        try:
            d["target"] = int(d.get("target") or 0)
        except (TypeError, ValueError):
            d["target"] = 0
        for k in ("thought", "text"):
            d[k] = str(d.get(k) or "").strip()
        # `text` carries the per-action argument; expand it into explicit fields for the executor.
        d["package"] = d["text"] if d["action"] == "launch" else ""
        d["direction"] = d["text"].lower() if d["action"] == "scroll" and d["text"].lower() in ("up", "down") else ""
        return d


# ------------------------------------------------------------------ records


@dataclass
class StepRecord:
    index: int
    screen: str
    package: str
    signature: str
    screenshot: str | None
    decision: dict
    llm_ms: int
    status: str  # executed | approved | invalid | guarded | ask_user | done | rejected_done | error
    detail: str = ""
    executed: str = ""
    changed: bool | None = None
    verdict: dict | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    raw: str = ""
    t: float = field(default_factory=time.time)


@dataclass
class Outcome:
    status: str  # done | needs_user | failed | aborted
    message: str
    steps: int


class Session:
    """State of one task, so it can pause for the user and resume."""

    def __init__(self, task: str, run_dir: Path):
        self.task = task
        self.run_dir = run_dir
        self.history: list[str] = []
        self.steps: list[StepRecord] = []
        self.used_packages: set[str] = set()
        self.waiting: dict | None = None  # set while waiting for the user to leave an app
        self.pending: dict | None = None  # a guarded action the user may approve (NEEDS_CONFIRM only)
        self.approved = False
        self.login_blocks: dict[str, int] = {}  # login buttons the model tapped once already
        self.checkout_noops = 0  # taps on "去结算" that changed nothing
        self.decider_state: dict = {}  # private memory of the decider (e.g. AutoGLM's conversation)
        self.outcome: Outcome | None = None
        self.verifier: Verifier = verifier_for(task)
        self.abort = threading.Event()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.json").write_text(
            json.dumps({"task": task, "verifier": self.verifier.description, "started": time.time()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def note(self, text: str) -> None:
        """Something the decider must know on its next turn (user decisions, waits)."""
        self.history.append(text)
        self.decider_state.setdefault("notes", []).append(text)

    def resume_note(self, note: str) -> None:
        if self.pending:
            # Resumed without approving: the model must not try the blocked step another way.
            self.note(f"（用户没有允许「{self.pending['label']}」这一步。不要再尝试它，也不要用别的方法绕过；"
                      "做不了就交给用户说明原因）")
            self.pending = None
        else:
            self.note(f"（用户已接手处理：{note or '已处理'}，请重新观察屏幕后继续）")
        self.approved = False
        self.outcome = None

    def approve(self) -> None:
        if not self.pending:
            raise RuntimeError("没有等待确认的操作")
        self.approved = True
        self.outcome = None


# ------------------------------------------------------------------ the loop


class Agent:
    def __init__(
        self,
        device: VirtualDisplayDevice,
        decider: Decider,
        max_steps: int = 25,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.device = device
        self.adb: Adb = device.adb
        self.decider = decider
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, data: None)

    def run(self, session: Session) -> Outcome:
        feedback = ""
        invalid_streak = 0
        seen: dict[tuple[str, str], int] = {}
        if session.pending and session.approved:
            feedback = self._run_approved(session)
            if session.verifier.goal_reached(self.device):
                # Usually the approved step is the last one (e.g. "Send"): the system state, not
                # another model turn, decides that the task is complete.
                verdict = session.verifier.check(self.device, self.device.observe(), "")
                if session.steps:
                    session.steps[-1].verdict = asdict(verdict)
                return self._finish(session, "done" if verdict.ok else "needs_user",
                                    ("用户允许的那一步执行后，系统确认：" if verdict.ok else "目标已达成，但校验发现了问题：")
                                    + verdict.evidence)
        obs = self.device.observe()

        while len(session.steps) < self.max_steps:
            if session.abort.is_set():
                return self._finish(session, "aborted", "用户中止了任务")
            index = len(session.steps) + 1
            shot = self._screenshot(session, index)
            if not obs.elements and not self._is_home(obs) and not self.decider.vision:
                # An app is open but exposes nothing a text model can read (WebView/game/canvas): stop.
                return self._pause(session, index, obs, shot, f"认不出当前页面：{obs.package} 的屏幕上没有可识别的控件")

            self.on_event("thinking", {"step": index})
            try:
                dec = self.decider.decide(session, obs, feedback)
            except (LLMError, ValueError) as e:
                return self._finish(session, "failed", f"模型调用失败：{e}")
            d = dec.d
            self._bind_point_target(d, obs)
            record = StepRecord(index, obs.render(), obs.package, obs.signature, shot, d, dec.llm_ms, "",
                                prompt_tokens=dec.prompt_tokens, output_tokens=dec.output_tokens, raw=dec.raw)

            # 1) the action must make sense on this screen
            problem = (self._validate(d, obs) or self._required_item_check(d, obs)
                       or self._quantity_check(session, d, obs))
            if problem:
                invalid_streak += 1
                record.status, record.detail = "invalid", problem
                self._record(session, record)
                feedback = f"上一步无效：{problem}"
                if invalid_streak >= 3:
                    return self._pause(session, index, obs, shot, f"模型连续 3 次给出无效动作（最后一次：{problem}）", record=False)
                continue
            invalid_streak = 0

            # 2) deterministic guard, independent of the model
            guard = self._guard(d, obs)
            if guard:
                kind, message = guard
                el = self._target_element(d, obs)
                if (kind == "handoff" and el is not None and LOGIN.search(el.label) and not el.password
                        and session.login_blocks.get(el.label, 0) == 0):
                    # A login button tapped by mistake (E9: the account avatar "Sign in" taken for a
                    # contacts icon). Never executed; the model gets one chance to take another route.
                    # A second attempt means login is really needed, and that is the user's.
                    session.login_blocks[el.label] = 1
                    record.status, record.detail = "guarded", f"已拦下「{el.label}」（登录类，没有执行）"
                    self._record(session, record)
                    feedback = (f"上一步没有执行：「{el.label}」是登录入口，分身不能替用户登录。"
                                "如果任务不需要登录，请换别的路径；如果确实必须登录才能继续，请交给用户。")
                    continue
                record.status, record.detail = "guarded", message
                self._record(session, record)
                if kind == "confirm":
                    el = self._target_element(d, obs)
                    session.pending = {"decision": d, "role": el.role if el else "", "label": el.label if el else message,
                                       "bounds": list(el.bounds) if el else [], "message": message}
                    session.approved = False
                return self._finish(session, "needs_user", message)

            if d["action"] == "ask_user":
                record.status, record.detail = "ask_user", d["text"] or d["thought"]
                self._record(session, record)
                return self._finish(session, "needs_user", record.detail)

            if d["action"] == "done":
                verdict = session.verifier.check(self.device, obs, d["text"])
                record.verdict = asdict(verdict)
                if verdict.ok:
                    record.status, record.detail = "done", verdict.evidence
                    self._record(session, record)
                    return self._finish(session, "done", verdict.evidence)
                record.status, record.detail = "rejected_done", verdict.evidence
                self._record(session, record)
                # Do not say "keep going": on a creation task that makes the model create the thing again.
                return self._finish(session, "needs_user", f"我以为做完了，但独立校验没通过：{verdict.evidence}")

            if d["action"] == "note":
                record.status, record.detail = "executed", "记录当前页面（无界面操作）"
                self._record(session, record)
                continue

            # 3) loop detection
            key = (obs.signature, json.dumps([d["action"], self._target_key(d, obs), d["text"]], ensure_ascii=False))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 3:
                record.status, record.detail = "guarded", "在同一个页面上重复同样的操作 3 次"
                self._record(session, record)
                return self._finish(session, "needs_user", "我在同一个页面上重复了同样的操作 3 次，可能卡住了")

            # 4) re-observe right before acting; the model took time to think
            fresh = self.device.observe()
            target = None
            if d.get("point") is not None:
                if not self._point_still_valid(d, obs, fresh):
                    record.status, record.detail = "invalid", "思考期间页面变了，要点的位置上已经是别的东西"
                    self._record(session, record)
                    feedback = "上一步没有执行：你思考时页面变了。请看新的屏幕。"
                    obs = fresh
                    continue
                target = self._element_at(fresh, d["point"])
            elif d["action"] in ("tap", "fill", "submit") or (d["action"] == "scroll" and d["target"]):
                target = self._relocate(obs.element(d["target"]), fresh)
                if target is None:
                    record.status, record.detail = "invalid", "思考期间页面变了，目标元素已不在屏幕上"
                    self._record(session, record)
                    feedback = "上一步没有执行：你思考时页面变了，目标元素不见了。请看新的屏幕。"
                    obs = fresh
                    continue

            # 5) act
            try:
                record.executed = self._execute(d, target)
            except ConflictError as e:
                pkg = d["package"]
                if pkg in session.used_packages:
                    # The user opened the app the agent was using: Android moves the agent's task, with
                    # its half-done screen, onto the user's display (E4). Treat it as a handoff.
                    msg = (f"你在自己的屏幕上打开了分身正在用的 {pkg}，系统把分身做到一半的界面移到了你的屏幕上。"
                           "你可以在那里自己接着完成；或者按 Home 退出它，分身会自动接着做")
                else:
                    msg = f"需要用的 {pkg} 正在你的屏幕上使用，我不会抢。等你按 Home 退出它，我会自动继续"
                record.status, record.detail = "guarded", str(e)
                self._record(session, record)
                if not self._wait_until_free(session, pkg, msg):
                    if session.abort.is_set():
                        return self._finish(session, "aborted", "用户中止了任务")
                    return self._finish(session, "needs_user", msg)
                session.note(f"（{pkg} 已经不在用户的屏幕上了，可以打开它继续）")
                feedback = ""
                obs = self.device.observe()
                continue
            except (HelperError, RuntimeError, KeyError) as e:
                record.status, record.detail = "error", str(e)
                self._record(session, record)
                feedback = f"上一步执行失败：{e}"
                obs = self.device.observe()
                continue

            after = self._settle(fresh.signature)
            record.changed = after.signature != fresh.signature
            record.status = "executed"
            for pkg in (obs.package, after.package):
                if pkg and not self._is_home_pkg(pkg):
                    session.used_packages.add(pkg)

            # 6) the environment, not the model, decides whether the goal state has been reached
            if session.verifier.goal_reached(self.device):
                verdict = session.verifier.check(self.device, after, "")
                record.verdict = asdict(verdict)
                self._record(session, record)
                if verdict.ok:
                    return self._finish(session, "done", "系统检测到目标已达成：" + verdict.evidence)
                return self._finish(session, "needs_user", "目标已达成，但校验发现了任务以外的变化：" + verdict.evidence)
            wrong = session.verifier.unintended_change(self.device)
            if wrong:
                record.detail = wrong
                self._record(session, record)
                return self._finish(session, "needs_user", wrong)

            reclaimed = self._reclaimed(d, fresh, after)
            if reclaimed:
                record.detail = reclaimed
            self._record(session, record)
            checkout_noop = d["action"] == "tap" and not record.changed and bool(CHECKOUT.search(self._tap_label(d, obs)))
            if checkout_noop:
                session.checkout_noops += 1
                if session.checkout_noops >= 2:
                    # Leaving to look for the cart lost it once (E11): stop while everything is still there.
                    return self._finish(session, "needs_user",
                                        "点了两次「去结算」页面都没有反应，可能弹出了我看不到的提示（必选品没选、没到起送价、限购之类）。"
                                        "购物车里的东西都还在：请你在手机上打开这个 App 看一眼，分身做到一半的页面会出现在你的屏幕上")
            effect = reclaimed or self._effect(d, target, after)
            if not effect and d["action"] == "swipe" and not record.changed:
                effect = self._unscrollable(d["point"], fresh)
            if not reclaimed:
                effect = "，".join(x for x in (effect, self._appeared(fresh, after)) if x)
            session.history.append(f"{index}. {self._describe(d, obs)} → {'屏幕已变化' if record.changed else '屏幕无变化'}"
                                   + (f"，{effect}" if effect else ""))
            feedback = "" if record.changed or d["action"] in ("wait", "note") else "上一步执行后屏幕没有变化。"
            if effect and self.decider.vision:
                # A vision decider keeps its own conversation and never sees session.history. Say where
                # the text went: Type goes to the focused field, which may not be the one the model meant.
                feedback = f"{feedback}上一步的结果：{effect}"
            if reclaimed:
                feedback = reclaimed
            if checkout_noop:
                feedback = ("上一步点了「去结算」，页面没有变化：可能有一闪而过的提示（必选品、起送价、限购）。"
                            "不要按返回离开这个页面，购物车可能会丢；先看看页面上有没有要选的必选品，再点一次「去结算」，还不行就交给用户。")
            obs = after

        return self._finish(session, "needs_user", f"已经执行了 {self.max_steps} 步还没完成，先停下来")

    # ------------------------------------------------------------------ points and elements

    @staticmethod
    def _element_at(obs: Observation, point) -> Element | None:
        """The innermost (smallest) element under a point: what a tap there would hit."""
        x, y = point
        hits = [e for e in obs.elements if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
        if not hits:
            return None
        return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))

    @staticmethod
    def _labeled_at(obs: Observation, point) -> str:
        """Text of the innermost element *with a label* under a point. Taps often land on an unlabeled
        inner node of a labeled button: Meituan's 去结算 (E11), commerce pay bars."""
        x, y = point
        hits = [e for e in obs.elements if e.label and e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
        if not hits:
            return ""
        return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1])).label

    def _tap_label(self, d: dict, obs: Observation) -> str:
        el = self._target_element(d, obs)
        if el is not None and el.label:
            return el.label
        return self._labeled_at(obs, d["point"]) if d.get("point") is not None else ""

    def _bind_point_target(self, d: dict, obs: Observation) -> None:
        """For coordinate actions, remember which element sits under the point (for guards and logs)."""
        if d.get("point") is not None and not d.get("target"):
            el = self._element_at(obs, d["point"])
            d["target"] = el.idx if el else 0

    def _target_element(self, d: dict, obs: Observation) -> Element | None:
        try:
            return obs.element(d["target"]) if d.get("target") else None
        except KeyError:
            return None

    def _point_still_valid(self, d: dict, before: Observation, fresh: Observation) -> bool:
        """Is the thing under the point still the thing the model saw? Same kind of element in about
        the same place counts, even if its text changed: Meituan's search box rotates its placeholder
        every few seconds, and comparing labels refused the same tap 7 times (E10)."""
        old = self._element_at(before, d["point"])
        new = self._element_at(fresh, d["point"])
        if old is None:
            return True  # nothing readable there (canvas/WebView): trust the screenshot the model saw
        if new is None or new.role != old.role:
            return False
        return new.label == old.label or self._overlap(old.bounds, new.bounds) >= 0.6

    @staticmethod
    def _overlap(a, b) -> float:
        """Intersection over union of two (left, top, right, bottom) boxes."""
        iw = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        ih = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = iw * ih
        union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / union if union > 0 else 0.0

    # ------------------------------------------------------------------ pieces

    def _validate(self, d: dict, obs: Observation) -> str | None:
        action = d["action"]
        if action == "invalid":  # the decider already knows why (unparseable output, app not installed…)
            return d["text"] or "模型输出的动作无效"
        if action not in ACTIONS:
            return f"未知动作 {action!r}"
        if action == "fill" and not d["target"] and d.get("point") is None:
            return "没有找到已聚焦的输入框：请先点击要输入的输入框"
        width, height = obs.width, obs.height
        for key in ("point", "point2"):
            p = d.get(key)
            if p is not None and not (0 <= p[0] < width and 0 <= p[1] < height):
                return f"坐标 {p} 超出屏幕"
        if action in ("tap", "fill", "submit") and d.get("point") is None:
            try:
                el = obs.element(d["target"])
            except KeyError:
                return f"编号 [{d['target']}] 不在当前屏幕上"
            if action == "tap" and not el.actionable:
                return f"[{el.idx}] {el.label!r} 是纯文字（·static），点它不会有反应；请点按钮、开关或输入框"
            if action == "tap" and el.role == "radio" and el.checked:
                return f"[{el.idx}] {el.label!r} 已经是选中状态，不用再点"
            if action == "submit" and not el.editable:
                return f"[{el.idx}] {el.label!r} 不是输入框，不能 submit"
            if action == "fill" and not (el.editable or el.clickable or el.checkable):
                return f"[{el.idx}] {el.label!r} 不是输入框，也不能点击，不能 fill"
            if action == "fill" and not d["text"]:
                return "fill 需要 text"
        if action in POINT_ACTIONS and d.get("point") is None:
            return f"{action} 需要坐标"
        if action == "swipe" and (d.get("point") is None or d.get("point2") is None):
            return "swipe 需要起点和终点"
        if action == "scroll" and d["target"]:
            try:
                obs.element(d["target"])
            except KeyError:
                return f"编号 [{d['target']}] 不在当前屏幕上"
        if action == "launch" and not d["package"]:
            return "launch 需要 package"
        return None

    def _quantity_check(self, session: Session, d: dict, obs: Observation) -> str | None:
        """'+' on a quantity stepper stops at the amount the task asks for. The model counts its own taps
        instead of reading the count on screen: asked for 4 oysters, it kept tapping to 16 (real phone,
        2026-10-29). Shop minimums ("5份起购") can add more than asked in one tap; that is accepted."""
        if d["action"] not in ("tap", "double_tap"):
            return None
        el = self._target_element(d, obs)
        m = STEPPER_ADD.match(el.label) if el else None
        if not m:
            return None
        have = int(m.group(1))
        product = self._product_for(el, obs)
        want, item = wanted_quantity(session.task, product)
        if have < want:
            return None
        name = re.split(r"\s*[¥￥]", product)[0] or "这个商品"
        asked = f"任务要 {want} 份" + (f"「{item}」" if item else "")
        more = "多出来的是店家的起购量，保持不动，最后告诉用户。" if have > want else ""
        return f"「{name}」已经加了 {have} 份，{asked}，够了，不要再点「+」。{more}接着去加任务里的下一样东西；都加好了就去结算。"

    def _required_item_check(self, d: dict, obs: Observation) -> str | None:
        """A shop's required item (未点必选品: spice level, cutlery) takes one choice for the whole order.
        The model picked three spice levels, three of each, "one for each dish" (real phone, 2026-10-29).
        Once an option in the sheet is added, adding another one or more of it is refused."""
        if d["action"] not in ("tap", "double_tap"):
            return None
        els = obs.elements
        start = next((k for k, e in enumerate(els) if REQUIRED_HEADER.search(e.label)), None)
        if start is None:
            return None
        # the sheet's own rows only: the shop page behind it is in the tree too
        end = next((k for k in range(start + 1, len(els)) if SHEET_END.match(els[k].label)), len(els))
        sheet = els[start + 1:end]
        el = self._target_element(d, obs)
        if el is None or not any(e is el for e in sheet) or not (ADD_NEW.match(el.label) or STEPPER_ADD.match(el.label)):
            return None
        chosen = [e for e in sheet if (m := STEPPER_ADD.match(e.label)) and int(m.group(1)) > 0]
        if not chosen:
            return None
        name = re.split(r"\s*[¥￥]", self._product_for(chosen[0], obs))[0].strip("◆ ") or "一样"
        return (f"必选品整单只要选一样（比如辣度选一种，不是每个菜各配一种），已经选了「{name}」，"
                "份数按店家的起购量就行，不要再加别的。直接点「去结算」。")

    @staticmethod
    def _product_for(el: Element, obs: Observation) -> str:
        """The product a stepper belongs to: the nearest priced title before it in the screen order."""
        i = next((k for k, e in enumerate(obs.elements) if e is el), -1)
        for e in reversed(obs.elements[max(0, i - 15):i]):
            if re.search(r"[¥￥]", e.label) and not STEPPER_ADD.match(e.label):
                return e.label
        return ""

    def _guard(self, d: dict, obs: Observation) -> tuple[str, str] | None:
        """('handoff', msg): only the user may do this. ('confirm', msg): the user can approve this one step."""
        if d["action"] not in ("tap", "fill", "submit", "long_press", "double_tap"):
            return None
        el = self._target_element(d, obs)
        label = (el.label if el else "") or self._tap_label(d, obs)
        if el is not None and el.password:
            return "handoff", f"「{el.label}」是密码框，密码必须由你本人输入"
        if label and HANDOFF_ONLY.search(label):
            return "handoff", f"下一步是「{label}」（付款/下单/登录/验证码类）。按约定我停在这里，必须由你本人在手机上完成"
        intent = INTENT_HANDOFF.search(d.get("intent", ""))
        if d["action"] in ("tap", "long_press", "double_tap") and intent:
            return "handoff", f"模型说下一步要「{intent.group(0)}」（付款/下单类）。按约定我停在这里，必须由你本人在手机上完成"
        if label and NEEDS_CONFIRM.search(label):
            return "confirm", f"下一步要点「{label}」，这是删除、同意、发送这一类撤不回的操作，需要你允许"
        if d.get("sensitive"):
            # The model itself flagged the tap as touching money/privacy (AutoGLM's message=...).
            what = label or d.get("sensitive")
            return "confirm", f"模型把下一步标记为敏感操作（{d.get('sensitive')}）：「{what}」，需要你允许"
        return None

    def _run_approved(self, session: Session) -> str:
        """Executes exactly the step the user approved, on the same element/point, then clears the approval."""
        p, d = session.pending, session.pending["decision"]
        session.pending, session.approved = None, False
        fresh = self.device.observe()
        index = len(session.steps) + 1
        record = StepRecord(index, fresh.render(), fresh.package, fresh.signature,
                            self._screenshot(session, index), d, 0, "approved")
        target = None
        if p["role"]:
            old = Element(idx=0, node=-1, role=p["role"], label=p["label"], bounds=tuple(p["bounds"]), window=-1)
            target = self._relocate(old, fresh)
            if target is None:
                record.status, record.detail = "error", f"用户允许了「{p['label']}」，但它已经不在屏幕上了"
                self._record(session, record)
                return f"用户允许了「{p['label']}」，但屏幕已经变了，找不到它。请看新的屏幕。"
            if d.get("point") is not None:
                d = {**d, "point": list(target.center)}
        try:
            record.executed = self._execute(d, target)
        except (HelperError, RuntimeError) as e:
            record.status, record.detail = "error", f"执行用户允许的「{p['label']}」失败：{e}"
            self._record(session, record)
            return f"执行用户允许的「{p['label']}」失败：{e}"
        after = self._settle(fresh.signature)
        record.changed = after.signature != fresh.signature
        record.detail = f"用户已允许「{p['label']}」"
        self._record(session, record)
        session.note(f"{index}. 用户允许后执行了「{p['label']}」 → {'屏幕已变化' if record.changed else '屏幕无变化'}")
        return ""

    def _wait_until_free(self, session: Session, package: str, message: str, timeout_s: float = 600) -> bool:
        """The app is in use on the user's screen: wait politely in the background, then carry on."""
        session.waiting = {"package": package, "message": message, "since": time.time()}
        self.on_event("waiting", dict(session.waiting))
        try:
            deadline = time.time() + timeout_s
            free = 0
            while time.time() < deadline:
                if session.abort.wait(1.0):
                    return False
                # Free on two checks in a row: while the user switches apps, the old task can drop
                # out of the visible list for a moment (bench 20261028-1138: resumed too early).
                free = free + 1 if package not in self.device.foreign_visible_packages() else 0
                if free >= 2:
                    self.on_event("resumed", {"package": package})
                    return True
            return False
        finally:
            session.waiting = None

    def _settle(self, before_signature: str, grace_s: float = 2.0) -> Observation:
        """Wait for the result of an action. Dialogs often appear a moment after the click, so an
        unchanged screen is only reported after a grace period; otherwise the model re-clicks."""
        self.device.wait_idle(400, 4000)
        after = self.device.observe()
        deadline = time.time() + grace_s
        while after.signature == before_signature and time.time() < deadline:
            time.sleep(0.25)
            after = self.device.observe()
        if after.signature != before_signature:
            self.device.wait_idle(300, 3000)  # let the new screen finish appearing
            after = self.device.observe()
        return after

    def _reclaimed(self, d: dict, before: Observation, after: Observation) -> str:
        """The agent's app vanished after an action that should not close it: was its process killed?

        Apps on the agent display run at "visible" priority, below the user's foreground app, so under
        memory pressure the low-memory killer reclaims the agent's app first (seen with Meituan open
        on the user's screen). That is the right order for the user; the agent just has to notice."""
        if self._is_home_pkg(before.package) or not self._is_home_pkg(after.package):
            return ""
        if d["action"] in ("back", "home", "launch") or self.device.is_running(before.package):
            return ""
        return f"{before.package} 的进程被系统回收了（多半是手机内存不足），屏幕回到了空白桌面；需要重新打开它，从头继续"

    @staticmethod
    def _is_home_pkg(package: str) -> bool:
        return not package or "launcher" in package

    @classmethod
    def _is_home(cls, obs: Observation) -> bool:
        """Agent display showing no app: only the secondary-display launcher (or nothing) is there."""
        return cls._is_home_pkg(obs.package)

    @staticmethod
    def _relocate(old: Element, fresh: Observation) -> Element | None:
        """Find the same element in a newer snapshot (node ids change on every snapshot)."""
        candidates = [e for e in fresh.elements if e.role == old.role and e.label == old.label]
        if not candidates:
            return None
        ox, oy = old.center
        return min(candidates, key=lambda e: abs(e.center[0] - ox) + abs(e.center[1] - oy))

    def _execute(self, d: dict, target: Element | None) -> str:
        action, point = d["action"], d.get("point")
        if action == "launch":
            return "launch " + self.device.launch(d["package"])
        if action == "tap":
            if point is not None:
                return self.device.tap(*point)
            return self.device.click(target)
        if action == "fill":
            if target is None:
                raise RuntimeError("没有可写入的输入框（请先点击输入框）")
            if target.editable:
                return self.device.fill(target, d["text"])
            return self._focus_then_fill(target, d["text"])
        if action == "submit":
            return self.device.submit(target)
        if action == "scroll":
            return self.device.scroll(target, d["direction"] or "down")
        if action == "swipe":
            return self.device.swipe(*d["point"], *d["point2"], ms=int(d.get("ms") or 400))
        if action == "long_press":
            return self.device.long_press(*point)
        if action == "double_tap":
            return self.device.double_tap(*point)
        if action == "back":
            return self.device.back()
        if action == "home":
            return self.device.home()
        if action == "wait":
            seconds = min(float(d.get("seconds") or 2), 10)
            time.sleep(seconds)
            return f"wait {seconds:g}s"
        raise RuntimeError(f"cannot execute {action}")

    @staticmethod
    def _effect(d: dict, target: Element | None, after: Observation) -> str:
        """Spell out what the action changed, so a small model does not have to diff two screens."""
        if target is None:
            return ""
        l, t, r, b = target.bounds

        def overlaps(e: Element) -> bool:
            return e.bounds[0] < r and e.bounds[2] > l and e.bounds[1] < b and e.bounds[3] > t

        if d["action"] == "fill":
            field_ = next((e for e in after.elements if e.editable and overlaps(e)), None)
            if field_ is not None:
                return f"「{field_.label or target.label}」现在是 \"{field_.text}\""
        if d["action"] == "tap" and target.checkable:
            same = next((e for e in after.elements if e.role == target.role and e.label == target.label and overlaps(e)), None)
            if same is not None:
                return f"「{same.label}」现在{'已选中' if same.checked else '未选中'}"
        return ""

    @staticmethod
    def _appeared(before: Observation, after: Observation) -> str:
        """Short texts that appeared on the same page (validation errors, hints). A new page brings
        many new texts and is not reported: the model sees that page anyway."""
        old = {e.label for e in before.elements}
        new = [e.label for e in after.elements if e.label and not e.actionable and e.label not in old]
        if not new or len(new) > 2 or before.package != after.package:
            return ""
        return "屏幕上新出现了：" + "、".join(f"「{t}」" for t in new)

    def _unscrollable(self, point, obs: Observation) -> str:
        """A swipe changed nothing: if the tree shows nothing scrollable under its start, say so.
        Screenshot models tend to treat any number box as a wheel and keep swiping it."""
        under = self._element_at(obs, point)
        if under is None or under.role == "web":
            return ""  # no tree there (WebView/canvas): we cannot tell, so say nothing
        x, y = point
        if any(e.scrollable and e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3] for e in obs.elements):
            return ""
        return f"滑动起点是 {under.role}「{under.label}」，那里没有可以滚动的列表或滚轮，滑动不会改变它"

    def _focus_then_fill(self, target: Element, text: str) -> str:
        """fill on a field that only becomes editable once selected (e.g. a time-picker chip):
        click it, wait, then write into the editable field that appeared in the same place."""
        before = self.device.observe().signature
        how = self.device.click(target)
        after = self._settle(before)
        l, t, r, b = target.bounds

        def overlaps(e: Element) -> bool:
            el, et, er, eb = e.bounds
            return el < r and er > l and et < b and eb > t

        editable = [e for e in after.elements if e.editable]
        field_ = next((e for e in editable if overlaps(e)), None) or next((e for e in editable if e.focused), None)
        if field_ is None:
            raise RuntimeError(f"点了 {target.label!r} 之后也没有出现输入框")
        return f"{how}+{self.device.fill(field_, text)}"

    @staticmethod
    def _target_key(d: dict, obs: Observation) -> str:
        if d.get("point") is not None:
            x, y = d["point"]
            return f"{d['action']}@{x // 24},{y // 24}"
        try:
            el = obs.element(d["target"])
            return f"{el.role}:{el.label}"
        except KeyError:
            return str(d["target"])

    @staticmethod
    def _describe(d: dict, obs: Observation) -> str:
        a = d["action"]
        if a in ("tap", "fill", "submit", "scroll", "long_press", "double_tap") and d.get("target"):
            try:
                el = obs.element(d["target"])
                what = f'{el.role} "{el.label}"'
            except KeyError:
                what = f"[{d['target']}]"
            return f'{a} {what}' + (f' 写入 "{d["text"]}"' if a == "fill" else "") + (f" {d['direction']}" if a == "scroll" else "")
        if d.get("point") is not None:
            return f"{a} @{tuple(d['point'])}" + (f' 写入 "{d["text"]}"' if a == "fill" else "")
        if a == "launch":
            return f"launch {d['package']}"
        return a

    def _screenshot(self, session: Session, index: int) -> str | None:
        try:
            self.device.wait_frames_idle()
            jpeg = self.device.screenshot_jpeg(quality=60, max_width=360)
        except HelperError:
            return None
        if not jpeg:
            return None
        path = session.run_dir / f"step-{index:02d}.jpg"
        path.write_bytes(jpeg)
        return path.name

    def _record(self, session: Session, record: StepRecord) -> None:
        session.steps.append(record)
        with (session.run_dir / "steps.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        self.on_event("step", asdict(record))

    def _pause(self, session, index, obs, shot, reason: str, record: bool = True) -> Outcome:
        if record:
            self._record(session, StepRecord(index, obs.render(), obs.package, obs.signature, shot, {}, 0, "ask_user", reason))
        return self._finish(session, "needs_user", reason)

    def _finish(self, session: Session, status: str, message: str) -> Outcome:
        session.outcome = Outcome(status, message, len(session.steps))
        (session.run_dir / "outcome.json").write_text(
            json.dumps(asdict(session.outcome), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.on_event("outcome", asdict(session.outcome))
        return session.outcome
