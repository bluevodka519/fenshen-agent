"""Independent completion checks. The model saying "done" is a claim; these decide whether it is true."""

from __future__ import annotations

import datetime as dt
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from .adb import Adb
from .observation import Observation

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass
class Verdict:
    ok: bool
    method: str
    evidence: str


class Verifier(Protocol):
    description: str

    def prepare(self, device) -> str: ...

    def goal_reached(self, device) -> bool:
        """Cheap, system-level check run after every step: has the world reached the goal state?"""
        ...

    def check(self, device, obs: Observation, claimed: str) -> Verdict: ...


def _cn_to_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if not s or any(ch not in _CN_NUM for ch in s):
        return None
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_NUM[s[1]]
    if s.endswith("十"):
        return _CN_NUM[s[0]] * 10
    if "十" in s:
        a, b = s.split("十")
        return _CN_NUM[a] * 10 + _CN_NUM[b]
    return _CN_NUM[s] if len(s) == 1 else None


def parse_time(task: str) -> tuple[int, int] | None:
    """'7:30', '早上7点半', '下午三点十五', '7:30 pm' -> (hour24, minute)."""
    hour = minute = None
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", task)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"([\d零一二两三四五六七八九十]{1,3})\s*点\s*(半|一刻|三刻|([\d零一二三四五六七八九十]{1,3})\s*分?)?", task)
        if m:
            hour = _cn_to_int(m.group(1))
            tail = m.group(2) or ""
            minute = {"半": 30, "一刻": 15, "三刻": 45}.get(tail, _cn_to_int(m.group(3)) if m.group(3) else 0)
    if hour is None or minute is None or not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    if re.search(r"下午|晚上|傍晚|夜里|\bpm\b|p\.m\.", task, re.I) and hour < 12:
        hour += 12
    elif re.search(r"中午", task) and hour < 6:
        hour += 12
    elif re.search(r"凌晨|\bam\b|a\.m\.", task, re.I) and hour == 12:
        hour = 0
    return hour % 24, minute


def _device_now(adb: Adb) -> dt.datetime:
    out = adb.shell("date", "'+%s %z'").split()  # adb shell joins args with spaces: quote for the device shell
    epoch, offset = int(out[0]), out[1]
    sign = 1 if offset[0] == "+" else -1
    tz = dt.timezone(sign * dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
    return dt.datetime.fromtimestamp(epoch, tz)


def next_alarm_clock(adb: Adb) -> dt.datetime | None:
    """AlarmManager.getNextAlarmClock() as reported by dumpsys: the alarm the status bar icon shows."""
    return _parse_next_alarm(adb.shell("dumpsys", "alarm"), adb)


def _parse_next_alarm(dump: str, adb: Adb | None = None) -> dt.datetime | None:
    # Only the lines right under the header, up to the first blank line. An empty block means no
    # alarm clock is set; reading further picked up another app's pending alarm (a phantom 17:00).
    lines = dump.splitlines()
    start = next((i for i, l in enumerate(lines) if "Next alarm clock information:" in l), None)
    if start is None:
        return None
    body = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        body.append(line)
    if not body:
        return None
    block = "\n".join(body)
    tz = _device_now(adb).tzinfo if adb is not None else dt.timezone.utc
    ms = re.search(r"time[=:](\d{12,})", block)
    if ms:
        return dt.datetime.fromtimestamp(int(ms.group(1)) / 1000, tz)
    stamp = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", block)
    if stamp:
        return dt.datetime.fromisoformat(f"{stamp.group(1)}T{stamp.group(2)}").replace(tzinfo=tz)
    return None


CLOCK_PKG = "com.google.android.deskclock"


def read_alarms(device) -> Counter | None:
    """Inventory of the Clock app's alarm rows as (row label, enabled), read on the agent display.

    Read-only: launches Clock, opens the Alarms tab, scrolls through the whole list (rows are
    deduplicated by their adapter index, so a row visible on two pages counts once), then presses
    back so the agent does not start from a prepared screen. Each row label holds schedule, time and
    state, e.g. "Alarm Tomorrow 7:30 AM Alarm is currently enabled.". Returns None when Clock is open
    on the user's screen (we never take it from them).
    """
    from .helper_client import ConflictError  # local import: keep verify importable without a device

    try:
        open_clock(device)
    except ConflictError:
        return None
    rows = scan_alarm_rows(device)
    device.back()
    # Leave the display clean: the agent's first screenshot must not catch Clock mid-exit
    # (bench 20261028-1110: the model tapped a "+" that was already gone and then saw a bare launcher).
    deadline = time.time() + 5
    while time.time() < deadline and device.observe().package == CLOCK_PKG:
        time.sleep(0.2)
    device.wait_idle(400, 3000)
    return Counter((label, "currently enabled" in label) for label in rows.values())


def open_clock(device) -> None:
    """Launch Clock on the agent display and wait until its alarm page is really there."""
    device.launch(CLOCK_PKG)
    # A cold start (first launch after boot) takes ~4 s before any window exists, and "idle" is
    # already true while nothing is drawn: wait for the Alarms tab itself, or the list reads as empty.
    deadline = time.time() + 12
    while time.time() < deadline:
        device.wait_idle(400, 3000)
        obs = device.observe()
        if obs.package == CLOCK_PKG and obs.find("Alarms"):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("时钟启动后 12 秒内没有出现闹钟页")
    device.wait_idle(800, 4000)  # right after saving, Clock scrolls to the new alarm and shows a snackbar


def open_alarms_tab(device) -> None:
    obs = device.observe()
    tab = obs.find("Alarms", role="button")
    if tab and not tab.selected:
        device.click(tab)
        device.wait_idle(500, 4000)


def scan_alarm_rows(device, on_page=None) -> dict[int, str]:
    """Walks the alarm list top to bottom; returns {adapter row index: row label}.
    `on_page(obs)` is called for every page (used by the cleanup tool)."""
    open_alarms_tab(device)
    # Start from the top: after saving, Clock scrolls the list to the new alarm, so a scan that only
    # goes down misses the rows above it (bench 20261028-1103..1106: an untouched alarm "disappeared").
    # Stop on "row 0 is visible", not on "the screen stopped changing": right after saving, Clock is
    # still smooth-scrolling to the new alarm and fights our scroll (bench 20261028-1111: rows 0-4 missed).
    for _ in range(20):
        obs = device.observe()
        lst = next((e for e in obs.elements if e.role == "list" and e.scrollable), None)
        visible = [e.row for e in obs.elements if e.label.startswith("Alarm ") and e.row is not None]
        if lst is None or (visible and min(visible) == 0):
            break
        device.scroll(lst, "up")
        device.wait_idle(300, 3000)
    rows: dict[int, str] = {}
    stalls = 0
    for _ in range(40):
        obs = device.observe()
        if on_page:
            on_page(obs)
            obs = device.observe()
        before = len(rows)
        for e in obs.elements:
            if e.role == "button" and e.label.startswith("Alarm ") and e.row is not None:
                rows[e.row] = e.label
        # The end is "two scrolls in a row brought no new row", not "the screen did not change":
        # a scroll can still be animating when we look (bench 20261028-1125: bottom rows missed).
        stalls = stalls + 1 if len(rows) == before else 0
        lst = next((e for e in obs.elements if e.role == "list" and e.scrollable), None)
        if lst is None or stalls >= 2:
            break
        # Half a list per move, slowly (no fling): pages overlap. A full-page scroll_forward can skip
        # a row that straddles the page boundary (row 12 of 22 was never seen in one scan).
        l, t, r, b = lst.bounds
        device.swipe((l + r) // 2, b - (b - t) * 15 // 100, (l + r) // 2, t + (b - t) * 40 // 100, ms=700)
        device.wait_idle(400, 3000)
    return rows


class AlarmVerifier:
    """Done means: the system's next alarm is the requested time, and exactly that one alarm was added."""

    def __init__(self, hour: int, minute: int):
        self.hour, self.minute = hour, minute
        h12 = hour % 12 or 12
        self.ui_time = f"{h12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
        self.description = (f"系统 AlarmManager 的下一个闹钟应为 {hour:02d}:{minute:02d}，"
                            f"且时钟里恰好新增一个已开启的 {self.ui_time} 闹钟、没有其他变化")
        self.before: Counter | None = None
        self.before_next: dt.datetime | None = None
        self.blocked_by: str | None = None  # set when the baseline could not be read because the user has the app open

    def _is_target(self, when: dt.datetime | None) -> bool:
        return when is not None and (when.hour, when.minute) == (self.hour, self.minute)

    def goal_reached(self, device) -> bool:
        # Only meaningful if the target was not already the next alarm before the task started.
        return not self._is_target(self.before_next) and self._is_target(next_alarm_clock(device.adb))

    def unintended_change(self, device) -> str | None:
        """Cheap check after every step: did the agent schedule an alarm at the wrong time?"""
        def suspicious(when: dt.datetime | None) -> bool:
            if when is None or self._is_target(when):
                return False
            if self.before_next is not None and when == self.before_next:
                return False
            return self.before_next is None or when < self.before_next

        nxt = next_alarm_clock(device.adb)
        if not suspicious(nxt):
            return None
        # Clock briefly reports intermediate values while it refreshes alarm instances
        # (UPDATE_ALARM_INSTANCES); only a value that is still there a moment later counts.
        time.sleep(1.5)
        again = next_alarm_clock(device.adb)
        if suspicious(again) and again == nxt:
            return (f"刚才建出了一个 {nxt:%H:%M} 的闹钟，不是任务要求的 {self.hour:02d}:{self.minute:02d}。"
                    "为了不继续制造错误的闹钟，我先停下来；这个闹钟需要你确认怎么处理")
        return None

    def prepare(self, device) -> str:
        self.before_next = next_alarm_clock(device.adb)
        if "package:" not in device.adb.shell("pm", "path", CLOCK_PKG, check=False):
            # Vendor phones ship their own clock app; its list UI is not something we know how to read.
            # The system-level check (AlarmManager's next alarm clock) works on any Android phone.
            self.before, self.blocked_by, self.inventory = None, None, False
            return "基线：这台手机没有 Google 时钟，只核对系统的下一个闹钟，不对比闹钟清单"
        self.inventory = True
        self.before = read_alarms(device)
        self.blocked_by = CLOCK_PKG if self.before is None else None
        if self.before is None:
            return "基线：时钟正在用户屏幕上使用，暂时读不到闹钟清单"
        return "基线闹钟清单：" + "; ".join(f"{k[0]}×{v}" for k, v in sorted(self.before.items())) if self.before else "基线：没有闹钟"

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        nxt = next_alarm_clock(device.adb)
        if nxt is None:
            return Verdict(False, "dumpsys alarm", "系统里没有已开启的下一个闹钟")
        if (nxt.hour, nxt.minute) != (self.hour, self.minute):
            return Verdict(False, "dumpsys alarm",
                           f"系统下一个闹钟是 {nxt:%Y-%m-%d %H:%M}，不是 {self.hour:02d}:{self.minute:02d}")
        system_note = f"系统下一个闹钟：{nxt:%Y-%m-%d %H:%M}"

        if self.before is None:
            why = "这台手机的时钟不是 Google 时钟" if not getattr(self, "inventory", True) else "没有基线"
            return Verdict(True, "dumpsys alarm", system_note + f"（{why}，未检查副作用）")
        after = read_alarms(device)
        if after is None:
            return Verdict(True, "dumpsys alarm", system_note + "（时钟在用户屏上，未检查副作用）")
        added = after - self.before
        removed = self.before - after
        added_enabled = [k for k, v in added.items() for _ in range(v) if k[1]]
        target = [k for k in added_enabled if self.ui_time in k[0]]
        extra = [k[0] for k in added.elements() if self.ui_time not in k[0] or not k[1]]
        # Switching on an existing, disabled alarm at the same time is also a valid way to do the task.
        reenabled = [k for k in removed.elements() if self.ui_time in k[0] and not k[1]]
        other_removed = [k[0] for k in removed.elements() if k not in reenabled[:1]]
        changes = f"新增：{[k[0] for k in added.elements()] or '无'}；消失：{[k[0] for k in removed.elements()] or '无'}"
        if len(target) != 1:
            return Verdict(False, "dumpsys alarm + 闹钟清单对比", f"{system_note}，但新增的已开启 {self.ui_time} 闹钟有 {len(target)} 个。{changes}")
        if extra or other_removed or len(reenabled) > 1:
            return Verdict(False, "dumpsys alarm + 闹钟清单对比", f"{system_note}，但有任务以外的变化。{changes}")
        return Verdict(True, "dumpsys alarm + 闹钟清单对比", f"{system_note}；{changes}")


# ------------------------------------------------------------------ contacts and SMS (system providers)


def content_query(adb: Adb, uri: str, projection: list[str]) -> list[dict]:
    """`content query` as the shell user, parsed. Shell may read the contacts and SMS providers, so
    these checks do not depend on what any app shows on screen."""
    out = adb.shell("content", "query", "--uri", uri, "--projection", ":".join(projection), check=False)
    records: list[str] = []
    for line in out.splitlines():  # a value with a newline continues on the next line(s)
        if line.startswith("Row:"):
            records.append(line)
        elif records:
            records[-1] += "\n" + line
    rows = []
    for line in records:
        row, pos = {}, line.find(f" {projection[0]}=")
        for i, key in enumerate(projection):  # values may contain ", ": split on the known keys, in order
            start = pos + len(key) + 2
            nxt = line.find(f", {projection[i + 1]}=", start) if i + 1 < len(projection) else len(line)
            row[key] = line[start:nxt if nxt >= 0 else len(line)]
            pos = nxt + 1
        rows.append(row)
    return rows


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


_PHONE_LIKE = re.compile(r"\+?[\d\s\-().]+")


def _same_number(a: str, b: str) -> bool:
    """Same phone number, ignoring country code and formatting, but only if both look like phone
    numbers: E9 stored `13900001111")` + newline + backtick, whose digits alone would have matched."""
    if not (_PHONE_LIKE.fullmatch((a or "").strip()) and _PHONE_LIKE.fullmatch((b or "").strip())):
        return False
    a, b = _digits(a), _digits(b)
    return bool(a) and bool(b) and (a[-8:] == b[-8:])


def _norm_text(s: str) -> str:
    return re.sub(r"[\s，,。.!！?？:：;；\"'“”「」]", "", s or "")


_NAME = r"([^\s，,：:（()）0-9]{1,10}?)"
_PHONE = r"(\+?\d[\d\- ]{5,}\d)"


def parse_contact(task: str) -> tuple[str, str] | None:
    """'新建联系人王小明，电话13800001234' / '把王小明（13800001234）存进联系人' -> (name, phone)."""
    if not re.search(r"联系人|通讯录|contact", task, re.I):
        return None
    phone = re.search(_PHONE, task)
    name = (re.search(r"把\s*" + _NAME + r"\s*[（(]?\s*\+?\d", task)
            or re.search(r"(?:新建|添加|保存|存)(?:一个)?联系人[：:\s]*" + _NAME + r"\s*[，,（(\s]", task))
    if not phone or not name:
        return None
    return name.group(1), _digits(phone.group(1))


def parse_sms(task: str, default_name: str | None = None) -> tuple[str, str] | None:
    """'给王小明发短信：明天下午三点开会' -> (recipient, body). '他/她' means the contact named in the task."""
    m = re.search(r"给\s*([^\s，,：:]{1,12}?)\s*发(?:一条|个)?(?:短信|信息|消息)\s*[，,：:]?\s*(?:说|内容是)?\s*[：:]?\s*(.+)$", task)
    if not m:
        return None
    who, body = m.group(1), m.group(2).strip().strip("「」“”\"'").rstrip("。.")
    if who in ("他", "她", "TA", "ta") and default_name:
        who = default_name
    return (who, body) if body else None


class ContactVerifier:
    """Done means: exactly one new contact with this name and this phone number (contacts provider)."""

    def __init__(self, name: str, phone: str):
        self.name, self.phone = name, phone
        self.description = f"联系人库里恰好新增一个「{name}」，电话 {phone}"
        self.before = 0

    def _matches(self, device) -> list[str]:
        rows = content_query(device.adb, "content://com.android.contacts/data",
                             ["raw_contact_id", "display_name", "mimetype", "data1"])
        named = {r["raw_contact_id"] for r in rows if r["display_name"].replace(" ", "") == self.name}
        return sorted({r["raw_contact_id"] for r in rows if r["raw_contact_id"] in named
                       and r["mimetype"].endswith("/phone_v2") and _same_number(r["data1"], self.phone)})

    def prepare(self, device) -> str:
        self.before = len(self._matches(device))
        return f"「{self.name} {self.phone}」原有 {self.before} 个"

    def goal_reached(self, device) -> bool:
        return len(self._matches(device)) > self.before

    def unintended_change(self, device) -> str | None:
        return None

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        n = len(self._matches(device)) - self.before
        if n == 1:
            return Verdict(True, "contacts provider", f"联系人库新增了「{self.name}」，电话 {self.phone}")
        if n == 0:
            return Verdict(False, "contacts provider", f"联系人库里没有新的「{self.name} {self.phone}」（名字或号码不一致也算没有）")
        return Verdict(False, "contacts provider", f"「{self.name} {self.phone}」新增了 {n} 个，重复了")


class SmsVerifier:
    """Done means: a new SENT message to the right number with the right text (SMS provider).
    A message that went out to anyone else or with other text stops the task immediately."""

    SENT, OUTGOING = "2", ("2", "4", "5", "6")  # sent; sent/outbox/failed/queued

    def __init__(self, recipient: str, body: str):
        self.recipient, self.body = recipient, body
        self.description = f"短信库里出现一条发给「{recipient}」、内容含「{body}」的已发送短信"
        self.max_id = 0

    def _numbers(self, device) -> list[str]:
        if _digits(self.recipient) and len(_digits(self.recipient)) >= 5:
            return [self.recipient]
        rows = content_query(device.adb, "content://com.android.contacts/data", ["display_name", "mimetype", "data1"])
        return [r["data1"] for r in rows if r["display_name"].replace(" ", "") == self.recipient
                and r["mimetype"].endswith("/phone_v2")]

    def _new(self, device) -> list[dict]:
        rows = content_query(device.adb, "content://sms", ["_id", "address", "type", "body"])
        return [r for r in rows if r["_id"].isdigit() and int(r["_id"]) > self.max_id]

    def _ok(self, r: dict, numbers: list[str]) -> bool:
        to_right = any(_same_number(r["address"], n) for n in numbers) if numbers else True
        return to_right and _norm_text(self.body) in _norm_text(r["body"])

    def prepare(self, device) -> str:
        ids = [int(r["_id"]) for r in content_query(device.adb, "content://sms", ["_id"]) if r["_id"].isdigit()]
        self.max_id = max(ids, default=0)
        return f"短信库原有 {len(ids)} 条"

    def goal_reached(self, device) -> bool:
        numbers = self._numbers(device)
        return any(r["type"] == self.SENT and self._ok(r, numbers) for r in self._new(device))

    def unintended_change(self, device) -> str | None:
        numbers = self._numbers(device)
        wrong = [r for r in self._new(device) if r["type"] in self.OUTGOING and not self._ok(r, numbers)]
        if wrong:
            r = wrong[0]
            return f"发出了一条不对的短信（发给 {r['address']}：「{r['body']}」），我先停下来，需要你看一下"
        return None

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        numbers = self._numbers(device)
        new = self._new(device)
        sent = [r for r in new if r["type"] == self.SENT and self._ok(r, numbers)]
        if not sent:
            pending = [r for r in new if r["type"] in self.OUTGOING]
            return Verdict(False, "SMS provider", "短信库里没有符合要求的已发送短信"
                           + (f"（有 {len(pending)} 条正在发送或发送失败）" if pending else ""))
        r = sent[-1]
        note = "" if numbers else "（没有在联系人里找到这个名字，只核对了内容）"
        return Verdict(True, "SMS provider", f"已发送给 {r['address']}：「{r['body']}」{note}")


class AllOf:
    """Several verifiers for one task (e.g. save a contact, then text them): all must hold."""

    def __init__(self, parts: list):
        self.parts = parts
        self.description = "；并且".join(p.description for p in parts)
        self.blocked_by = None

    def prepare(self, device) -> str:
        return "；".join(p.prepare(device) for p in self.parts)

    def goal_reached(self, device) -> bool:
        return all(p.goal_reached(device) for p in self.parts)

    def unintended_change(self, device) -> str | None:
        return next((w for p in self.parts if (w := p.unintended_change(device))), None)

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        verdicts = [p.check(device, obs, claimed) for p in self.parts]
        return Verdict(all(v.ok for v in verdicts), " + ".join(v.method for v in verdicts),
                       "；".join(v.evidence for v in verdicts))


# ------------------------------------------------------------------ takeout up to the checkout boundary

MEITUAN_PKG = "com.sankuai.meituan"


_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_QUANTITY = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*(?:份|个|杯|串|件|瓶|盒|碗|只|根|块|听|罐|袋)\s*"
                       r"([^\d，,。、；;和及与\s]+?)(?=停|然后|并|再|[，,。、；;和及与\s]|$)")


def _cn_number(s: str) -> int:
    if s.isdigit():
        return int(s)
    if "十" not in s:
        return _CN_DIGITS.get(s, 1)
    tens, _, ones = s.partition("十")
    return (_CN_DIGITS.get(tens, 1) if tens else 1) * 10 + _CN_DIGITS.get(ones, 0)


def parse_quantities(task: str) -> list[tuple[str, int]]:
    """'需要2份烤土豆片,3份烤韭菜,4份烤生蚝' -> [('烤土豆片', 2), ('烤韭菜', 3), ('烤生蚝', 4)]."""
    return [(name, _cn_number(n)) for n, name in _QUANTITY.findall(task)]


def _common_run(a: str, b: str) -> int:
    """Length of the longest common substring."""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def wanted_quantity(task: str, product: str) -> tuple[int, str]:
    """How many of the product on screen the task asks for, and which task item it matched ('' if none).
    Shops name things their own way ('烤锡纸蒜蓉生蚝' for '烤生蚝', '烤土豆' for '烤土豆片'), so the best
    match is the item sharing the longest run of at least two characters. Unmatched: the largest amount
    in the task (a cap, not a target); a task without amounts means one."""
    items = parse_quantities(task)
    best, score = None, 1
    for name, n in items:
        core = name.rpartition("的")[2]  # '瑞幸的生椰拿铁' -> '生椰拿铁'
        s = _common_run(core, product)
        if s > score:
            best, score = (n, name), s
    if best:
        return best
    return (max(n for _, n in items) if items else 1), ""


def parse_order(task: str) -> tuple[str, str] | None:
    """'在美团点一杯瑞幸的生椰拿铁，停在提交订单前' -> (item, shop). Only for tasks that end before paying."""
    if not (re.search(r"美团|外卖", task) and re.search(r"(提交订单|付款|支付|下单)前", task)):
        return None
    m = re.search(r"(?:点|买|订)(?:一|两|三|1|2|3)?(?:杯|份|个|碗|盒)?\s*([^，,。\s]+?)\s*(?:[，,。]|停|然后|并|$)", task)
    if not m:
        return None
    phrase = m.group(1)
    shop, _, item = phrase.rpartition("的")
    return (item or phrase), shop


class CheckoutVerifier:
    """Done means: the agent display shows Meituan's order confirmation with the requested item, and
    nothing was submitted or paid. Read from the accessibility tree, so weaker than a system database:
    there is no system record of a cart or a draft order.

    The page is recognised by its structure, not by the pay button: on the real app (Seeker, E10) the
    button says 立即支付 and the whole bottom bar is drawn without accessibility nodes, so a check that
    required a button label rejected a correct stop."""

    SUBMIT = re.compile(r"提交订单|去支付|立即支付|确认支付")
    CHECKOUT_ONLY = re.compile(r"提交订单|确认订单|立即支付|实付|应付")
    ORDER_DETAILS = (re.compile(r"预计\s*\d{1,2}:\d{2}\s*送达|外卖配送|到店自取"),  # delivery
                     re.compile(r"商品原价|打包费|配送费"),                          # price breakdown
                     re.compile(r"请填写您的要求|备注|餐具"))                        # order notes
    NOT_CHECKOUT = re.compile(r"去结算|加入购物车|选规格")  # the shop page and its cart
    SUBMITTED = re.compile(r"支付成功|订单已提交|等待商家接单|商家已接单|收银台|选择支付方式|输入支付密码")

    def __init__(self, item: str, shop: str = ""):
        self.item, self.shop = item, shop
        self.description = (f"agent 屏停在美团确认订单页：页面上有「{item}」"
                            + (f"（{shop}）" if shop else "") + "，没有点支付，没有进入支付页")

    @staticmethod
    def _text(obs: Observation) -> str:
        return " ".join(f"{e.label} {e.text}" for e in obs.elements)

    @classmethod
    def _is_confirm_page(cls, text: str) -> bool:
        if cls.NOT_CHECKOUT.search(text):
            return False
        return bool(cls.CHECKOUT_ONLY.search(text)) or sum(bool(p.search(text)) for p in cls.ORDER_DETAILS) >= 2

    def prepare(self, device) -> str:
        return "基线：无（下单类任务没有系统级记录，完成判定读确认订单页）"

    def goal_reached(self, device) -> bool:
        obs = device.observe()
        text = self._text(obs)
        return (obs.package == MEITUAN_PKG and self.item in text and self._is_confirm_page(text)
                and not self.SUBMITTED.search(text))

    def unintended_change(self, device) -> str | None:
        obs = device.observe()
        if obs.package == MEITUAN_PKG and self.SUBMITTED.search(self._text(obs)):
            return "看起来订单已经提交或进入了支付页面，这一步本应由你来做，我先停下来，请你马上看一下"
        return None

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        obs = device.observe()
        text = self._text(obs)
        if obs.package != MEITUAN_PKG:
            return Verdict(False, "确认订单页", f"agent 屏上不是美团（{obs.package or '桌面'}）")
        if self.SUBMITTED.search(text):
            return Verdict(False, "确认订单页", "页面显示订单已提交或进入了支付，这不应该发生")
        if not self._is_confirm_page(text):
            return Verdict(False, "确认订单页", "当前不是确认订单页（没有订单明细、配送信息这些确认页才有的内容）")
        if self.item not in text:
            return Verdict(False, "确认订单页", f"确认订单页上找不到「{self.item}」")
        # Quote the item and shop names only: the same list node also holds the user's name and address.
        item = re.search(re.escape(self.item) + r"[^\s¥￥]{0,12}", text).group(0)
        after = text[text.index(item):]
        prices = [re.sub(r"\s", "", p) for p in re.findall(r"[¥￥]\s?\d+(?:\.\d+)?", after)[:1]]
        shop = re.search(re.escape(self.shop) + r"[^\s¥￥]{0,12}", text) if self.shop else None
        shop_note = f"，{shop.group(0)}" if shop else (f"（页面上没看到店名「{self.shop}」）" if self.shop else "")
        submit = [e.label for e in obs.elements if self.SUBMIT.search(e.label)]
        button = f"「{submit[0]}」未点击" if submit else "支付按钮没有被点（美团的支付栏不在界面树里）"
        return Verdict(True, "确认订单页（界面树）",
                       f"停在确认订单页：「{item}」{' '.join(prices)}{shop_note}；{button}")


class ScreenEvidenceVerifier:
    """Fallback: the evidence the model cites must actually be on the current screen."""

    description = "模型给出的完成证据必须出现在当前屏幕上（任务里本来就有的数字和文字不算证据）"

    def __init__(self, task: str = ""):
        self.task = task

    def prepare(self, device) -> str:
        return ""

    def goal_reached(self, device) -> bool:
        return False  # no system-level signal: rely on the model's done + screen evidence

    def unintended_change(self, device) -> str | None:
        return None

    def check(self, device, obs: Observation, claimed: str) -> Verdict:
        claimed = (claimed or "").strip().strip('"“”')
        if not claimed:
            return Verdict(False, "screen", "没有给出证据")
        screen = " ".join(f"{e.label} {e.text}" for e in obs.elements)
        if claimed in screen:
            return Verdict(True, "screen", f"屏幕上找到了证据文字：{claimed!r}（仅界面层面，未做系统级核实）")
        # Screenshot models answer in a sentence ("结果是 8760"); look for the concrete bits instead:
        # numbers (thousands separators ignored, "8,760" on screen) and quoted text.
        flat = screen.replace(",", "")
        bits = [n.replace(",", "") for n in re.findall(r"\d[\d,.]*\d", claimed)]
        bits += re.findall(r"[「“\"]([^」”\"]{2,})[」”\"]", claimed)
        # Inputs are not outcomes: "365" and "24" from "365 乘以 24" are on screen whether or not the
        # answer is right (seeker run 20261029-175437 "passed" on exactly those).
        task_flat = self.task.replace(",", "")
        hits = [b for b in bits if b and b not in task_flat and (b in flat or b in screen)]
        if hits:
            return Verdict(True, "screen", f"屏幕上找到了模型给出的证据：{hits[:3]}（仅界面层面，未做系统级核实）")
        return Verdict(False, "screen", f"屏幕上找不到证据文字 {claimed[:80]!r}")


def verifier_for(task: str) -> Verifier:
    if re.search(r"闹钟|alarm", task, re.I):
        t = parse_time(task)
        if t:
            return AlarmVerifier(*t)
    order = parse_order(task)
    if order:
        return CheckoutVerifier(*order)
    parts: list = []
    contact = parse_contact(task)
    if contact:
        parts.append(ContactVerifier(*contact))
    sms = parse_sms(task, default_name=contact[0] if contact else None)
    if sms:
        parts.append(SmsVerifier(*sms))
    if parts:
        return parts[0] if len(parts) == 1 else AllOf(parts)
    return ScreenEvidenceVerifier(task)
