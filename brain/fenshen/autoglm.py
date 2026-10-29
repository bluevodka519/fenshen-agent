"""AutoGLM-Phone (Zhipu, bigmodel.cn) as the decider: a phone-GUI vision model on our hidden-display body.

Open-AutoGLM drives the phone's main screen over ADB and switches the system IME to ADB Keyboard to
type. Here the same model only ever sees and touches the agent's virtual display:
  * screenshot  -> fenshen-helper's own buffer of the agent display (the user's screen is never sent)
  * Tap / Swipe -> input injected on the agent display (E2/E3: no focus or IME theft)
  * Type        -> ACTION_SET_TEXT on the focused field (no IME switch)
  * Take_over / message="..." on Tap -> our handoff / per-step approval
Prompt, request parameters and response parsing follow zai-org/Open-AutoGLM (Apache-2.0):
phone_agent/config/prompts_zh.py, phone_agent/model/client.py, phone_agent/actions/handler.py.

The API key is read from ZHIPU_API_KEY (process env, or the Windows user environment) at call time.
It is never logged, written to disk or put in a trace.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time

from . import skills
from .agent import Decision, Session
from .llm import LLMError, _post_json
from .observation import Observation

BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "autoglm-phone"

# Common apps whose launcher label may differ from what the model says (from Open-AutoGLM's apps.py).
APP_ALIASES = {
    "微信": "com.tencent.mm", "淘宝": "com.taobao.taobao", "美团": "com.sankuai.meituan",
    "美团外卖": "com.sankuai.meituan.takeoutnew", "大众点评": "com.dianping.v1", "饿了么": "me.ele",
    "高德地图": "com.autonavi.minimap", "时钟": "com.google.android.deskclock", "Clock": "com.google.android.deskclock",
    "设置": "com.android.settings", "Settings": "com.android.settings", "Chrome": "com.android.chrome",
    "联系人": "com.google.android.contacts", "Contacts": "com.google.android.contacts",
    "日历": "com.google.android.calendar", "Calendar": "com.google.android.calendar",
    # Chinese names of the stock apps, whose launcher labels are English on this image ("Messages").
    "短信": "com.google.android.apps.messaging", "信息": "com.google.android.apps.messaging",
    "消息": "com.google.android.apps.messaging", "SMS": "com.google.android.apps.messaging",
    "电话": "com.google.android.dialer", "拨号": "com.google.android.dialer", "相机": "com.android.camera2",
    "地图": "com.google.android.apps.maps", "照片": "com.google.android.apps.photos", "文件": "com.google.android.documentsui",
}

_PROMPT_BODY = """
你是一个智能体分析专家，可以根据操作历史和当前状态图执行一系列操作来完成任务。
你必须严格按照要求输出以下格式：
<think>{think}</think>
<answer>{action}</answer>

其中：
- {think} 是对你为什么选择这个操作的简短推理说明。
- {action} 是本次执行的具体操作指令，必须严格遵循下方定义的指令格式。

操作指令及其作用如下：
- do(action="Launch", app="xxx")
    Launch是启动目标app的操作，这比通过主屏幕导航更快。此操作完成后，您将自动收到结果状态的截图。
- do(action="Tap", element=[x,y])
    Tap是点击操作，点击屏幕上的特定点。可用此操作点击按钮、选择项目、从主屏幕打开应用程序，或与任何可点击的用户界面元素进行交互。坐标系统从左上角 (0,0) 开始到右下角（999,999)结束。此操作完成后，您将自动收到结果状态的截图。
- do(action="Tap", element=[x,y], message="重要操作")
    基本功能同Tap，点击涉及财产、支付、隐私等敏感按钮时触发。
- do(action="Type", text="xxx")
    Type是输入操作，在当前聚焦的输入框中输入文本。使用此操作前，请确保输入框已被聚焦（先点击它）。输入的文本将像使用键盘输入一样输入。重要提示：手机可能正在使用 ADB 键盘，该键盘不会像普通键盘那样占用屏幕空间。要确认键盘已激活，请查看屏幕底部是否显示 'ADB Keyboard {ON}' 类似的文本，或者检查输入框是否处于激活/高亮状态。不要仅仅依赖视觉上的键盘显示。自动清除文本：当你使用输入操作时，输入框中现有的任何文本（包括占位符文本和实际输入）都会在输入新文本前自动清除。你无需在输入前手动清除文本——直接使用输入操作输入所需文本即可。操作完成后，你将自动收到结果状态的截图。
- do(action="Type_Name", text="xxx")
    Type_Name是输入人名的操作，基本功能同Type。
- do(action="Interact")
    Interact是当有多个满足条件的选项时而触发的交互操作，询问用户如何选择。
- do(action="Swipe", start=[x1,y1], end=[x2,y2])
    Swipe是滑动操作，通过从起始坐标拖动到结束坐标来执行滑动手势。可用于滚动内容、在屏幕之间导航、下拉通知栏以及项目栏或进行基于手势的导航。坐标系统从左上角 (0,0) 开始到右下角（999,999)结束。滑动持续时间会自动调整以实现自然的移动。此操作完成后，您将自动收到结果状态的截图。
- do(action="Note", message="True")
    记录当前页面内容以便后续总结。
- do(action="Call_API", instruction="xxx")
    总结或评论当前页面或已记录的内容。
- do(action="Long Press", element=[x,y])
    Long Pres是长按操作，在屏幕上的特定点长按指定时间。可用于触发上下文菜单、选择文本或激活长按交互。坐标系统从左上角 (0,0) 开始到右下角（999,999)结束。此操作完成后，您将自动收到结果状态的屏幕截图。
- do(action="Double Tap", element=[x,y])
    Double Tap在屏幕上的特定点快速连续点按两次。使用此操作可以激活双击交互，如缩放、选择文本或打开项目。坐标系统从左上角 (0,0) 开始到右下角（999,999)结束。此操作完成后，您将自动收到结果状态的截图。
- do(action="Take_over", message="xxx")
    Take_over是接管操作，表示在登录和验证阶段需要用户协助。
- do(action="Back")
    导航返回到上一个屏幕或关闭当前对话框。相当于按下 Android 的返回按钮。使用此操作可以从更深的屏幕返回、关闭弹出窗口或退出当前上下文。此操作完成后，您将自动收到结果状态的截图。
- do(action="Home")
    Home是回到系统桌面的操作，相当于按下 Android 主屏幕按钮。使用此操作可退出当前应用并返回启动器，或从已知状态启动新任务。此操作完成后，您将自动收到结果状态的截图。
- do(action="Wait", duration="x seconds")
    等待页面加载，x为需要等待多少秒。
- finish(message="xxx")
    finish是结束任务的操作，表示准确完整完成任务，message是终止信息。

必须遵循的规则：
1. 在执行任何操作前，先检查当前app是否是目标app，如果不是，先执行 Launch。
2. 如果进入到了无关页面，先执行 Back。如果执行Back后页面没有变化，请点击页面左上角的返回键进行返回，或者右上角的X号关闭。
3. 如果页面未加载出内容，最多连续 Wait 三次，否则执行 Back重新进入。
4. 如果页面显示网络问题，需要重新加载，请点击重新加载。
5. 如果当前页面找不到目标联系人、商品、店铺等信息，可以尝试 Swipe 滑动查找。
6. 遇到价格区间、时间区间等筛选条件，如果没有完全符合的，可以放宽要求。
7. 在做小红书总结类任务时一定要筛选图文笔记。
8. 购物车全选后再点击全选可以把状态设为全不选，在做购物车任务时，如果购物车里已经有商品被选中时，你需要点击全选后再点击取消全选，再去找需要购买或者删除的商品。
9. 在做外卖任务时，如果相应店铺购物车里已经有其他商品你需要先把购物车清空再去购买用户指定的外卖。
10. 在做点外卖任务时，如果用户需要点多个外卖，请尽量在同一店铺进行购买，如果无法找到可以下单，并说明某个商品未找到。
11. 请严格遵循用户意图执行任务，用户的特殊要求可以执行多次搜索，滑动查找。比如（i）用户要求点一杯咖啡，要咸的，你可以直接搜索咸咖啡，或者搜索咖啡后滑动查找咸的咖啡，比如海盐咖啡。（ii）用户要找到XX群，发一条消息，你可以先搜索XX群，找不到结果后，将"群"字去掉，搜索XX重试。（iii）用户要找到宠物友好的餐厅，你可以搜索餐厅，找到筛选，找到设施，选择可带宠物，或者直接搜索可带宠物，必要时可以使用AI搜索。
12. 在选择日期时，如果原滑动方向与预期日期越来越远，请向反方向滑动查找。
13. 执行任务过程中如果有多个可选择的项目栏，请逐个查找每个项目栏，直到完成任务，一定不要在同一项目栏多次查找，从而陷入死循环。
14. 在执行下一步操作前请一定要检查上一步的操作是否生效，如果点击没生效，可能因为app反应较慢，请先稍微等待一下，如果还是不生效请调整一下点击位置重试，如果仍然不生效请跳过这一步继续任务，并在finish message说明点击不生效。
15. 在执行任务中如果遇到滑动不生效的情况，请调整一下起始点位置，增大滑动距离重试，如果还是不生效，有可能是已经滑到底了，请继续向反方向滑动，直到顶部或底部，如果仍然没有符合要求的结果，请跳过这一步继续任务，并在finish message说明但没找到要求的项目。
16. 在做游戏任务时如果在战斗页面如果有自动战斗一定要开启自动战斗，如果多轮历史状态相似要检查自动战斗是否开启。
17. 如果没有合适的搜索结果，可能是因为搜索页面不对，请返回到搜索页面的上一级尝试重新搜索，如果尝试三次返回上一级搜索后仍然没有符合要求的结果，执行 finish(message="原因")。
18. 在结束任务前请一定要仔细检查任务是否完整准确的完成，如果出现错选、漏选、多选的情况，请返回之前的步骤进行纠正。
"""

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def system_prompt(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return "今天的日期是: " + today.strftime("%Y年%m月%d日") + " " + _WEEKDAYS[today.weekday()] + _PROMPT_BODY


KEYBOARD_CUE = "ADB Keyboard {ON}"


def input_ready_cue(jpeg: bytes, obs: Observation, focused) -> bytes:
    """Tell the model, in the visual language it was trained on, that typing is available.

    Open-AutoGLM types through the ADB Keyboard IME, whose input view is a bar reading
    "ADB Keyboard {ON}", and its prompt tells the model to look for that bar before Type. Our agent
    display has IME policy HIDE (no keyboard ever shows, D4), so after a tap focuses a field the
    model sees no change and keeps tapping/swiping (run 20261027-203504). When the tree reports a
    focused editable field, draw the same bar where an IME would sit. Typing itself is still
    ACTION_SET_TEXT. Skipped (text hint only) without Pillow, or if the bar would hide the field."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return jpeg
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    scale = img.width / obs.width
    bar_h = int(img.height * 0.045)
    top = img.height - int(img.height * 0.03) - bar_h  # above the gesture handle
    if focused is not None and focused.bounds[3] * scale > top:
        return jpeg
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, top, img.width, top + bar_h], fill=(48, 48, 48))
    size = int(bar_h * 0.5)
    try:
        font = ImageFont.truetype("arial.ttf", size)
    except OSError:
        font = ImageFont.load_default(size=size)
    l, t, r, b = draw.textbbox((0, 0), KEYBOARD_CUE, font=font)
    draw.text(((img.width - (r - l)) / 2 - l, top + (bar_h - (b - t)) / 2 - t), KEYBOARD_CUE,
              fill=(235, 235, 235), font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


DESKTOP_CUE = ("HOME SCREEN - phone is unlocked", "use Launch to open an app")


def desktop_cue(jpeg: bytes) -> bytes:
    """The agent display's desktop is a bare wallpaper with a taskbar, unlike a phone home screen
    full of icons, and AutoGLM kept taking it for a lock screen (swiping to "unlock", tapping the
    wallpaper) even with a text hint saying otherwise (E9). Like the keyboard cue, say it visually,
    on the image sent to the model only."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return jpeg
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    size = max(12, img.width // 22)
    try:
        font = ImageFont.truetype("arial.ttf", size)
    except OSError:
        font = ImageFont.load_default(size=size)
    top = int(img.height * 0.38)
    draw.rectangle([0, top, img.width, top + int(size * 3.6)], fill=(32, 32, 32))
    for i, line in enumerate(DESKTOP_CUE):
        l, t, r, b = draw.textbbox((0, 0), line, font=font)
        draw.text(((img.width - (r - l)) / 2 - l, top + size * 0.5 + i * size * 1.4 - t), line,
                  fill=(240, 240, 240), font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def blank_frame(obs: Observation) -> bytes:
    """No frame yet: nothing has been drawn on the agent display (e.g. right after boot there is no
    launcher on it), so what it shows is literally black."""
    try:
        from PIL import Image
    except ImportError:
        raise LLMError("no frame from the agent display yet (install Pillow to send a blank one)")
    out = io.BytesIO()
    Image.new("RGB", (obs.width, obs.height)).save(out, "JPEG", quality=85)
    return out.getvalue()


def _api_key() -> str:
    key = os.environ.get("ZHIPU_API_KEY")
    if not key and sys.platform == "win32":
        # Set with `setx` after this process started: read the user environment directly.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "ZHIPU_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise LLMError("ZHIPU_API_KEY is not set")
    return key


class AutoGLMClient:
    def __init__(self, model: str = MODEL, base_url: str | None = None):
        self.model = model
        self.base_url = (base_url or os.environ.get("ZHIPU_BASE_URL") or BASE_URL).rstrip("/")

    def chat(self, messages: list[dict], retries: int = 2) -> tuple[str, int, dict]:
        """One completion. Transient server errors are retried (E9: a single HTTP 500 failed a task);
        client errors such as a bad key or quota are not."""
        for attempt in range(retries + 1):
            try:
                return self._chat_once(messages)
            except LLMError as e:
                transient = re.search(r"HTTP 5\d\d|HTTP 429|unreachable|timed out", str(e))
                if not transient or attempt == retries:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def _chat_once(self, messages: list[dict]) -> tuple[str, int, dict]:
        t = time.perf_counter()
        resp = _post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                # same sampling as Open-AutoGLM's ModelConfig defaults
                "max_tokens": 3000,
                "temperature": 0.0,
                "top_p": 0.85,
                "frequency_penalty": 0.2,
                "stream": False,
            },
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=120,
        )
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response: {str(resp)[:300]}") from e
        return content, int((time.perf_counter() - t) * 1000), resp.get("usage", {})


def split_response(content: str) -> tuple[str, str]:
    """Thinking vs action, in Open-AutoGLM's order: finish( / do( markers, then legacy <answer> tags."""
    for marker in ("finish(message=", "do(action="):
        if marker in content:
            before, after = content.split(marker, 1)
            thinking = before.replace("<think>", "").replace("</think>", "").replace("<answer>", "").strip()
            return thinking, (marker + after).replace("</answer>", "").strip()
    if "<answer>" in content:
        before, after = content.split("<answer>", 1)
        return before.replace("<think>", "").replace("</think>", "").strip(), after.replace("</answer>", "").strip()
    return "", content.strip()


def _strip_trailer(s: str) -> str:
    """Drop what models sometimes append after the call: a closing code fence, stray backticks."""
    s = s.strip()
    while s.endswith("```") or s.endswith("`"):
        s = s.rstrip("`").rstrip()
    return s


def parse_action(s: str) -> dict:
    """do(action="X", k=literal, ...) or finish(message="..."), after Open-AutoGLM's parse_action.

    Upstream takes Type's text as `s.split("text=")[1][1:-2]`, i.e. "everything but the last two
    characters". When the model appends a code fence, that leaks `")` + newline + backtick into the
    text, and E9 typed exactly that into a contact's phone number. Here the text ends at the last `")`."""
    s = _strip_trailer(s)
    if s.startswith('do(action="Type"') or s.startswith('do(action="Type_Name"'):
        body = s.split("text=", 1)[1]
        end = body.rfind('")')
        if not body.startswith('"') or end <= 0:
            raise ValueError(f"cannot parse Type action {s!r}")
        return {"_metadata": "do", "action": "Type", "text": body[1:end]}
    if s.startswith("do"):
        escaped = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        try:
            call = ast.parse(escaped, mode="eval").body
        except SyntaxError as e:
            raise ValueError(f"cannot parse action {s!r}") from e
        if not isinstance(call, ast.Call):
            raise ValueError(f"not a call: {s!r}")
        out = {"_metadata": "do"}
        for kw in call.keywords:
            out[kw.arg] = ast.literal_eval(kw.value)
        return out
    if s.startswith("finish"):
        return {"_metadata": "finish", "message": s.replace("finish(message=", "", 1)[1:-2]}
    raise ValueError(f"unknown action format: {s!r}")


class AutoGLMDecider:
    vision = True

    def __init__(self, device, client: AutoGLMClient | None = None):
        self.device = device
        self.client = client or AutoGLMClient()
        self.name = f"zhipu:{self.client.model}"
        self._apps: dict[str, str] | None = None
        self._labels: dict[str, str] = {}

    # ------------------------------------------------------------ app names

    def _app_table(self) -> dict[str, str]:
        if self._apps is None:
            self._apps = dict(APP_ALIASES)
            for app in self.device.apps():
                self._apps[app["label"]] = app["package"]
                self._labels[app["package"]] = app["label"]
        return self._apps

    def _resolve(self, name: str) -> str:
        """Package for an app name, only if that app is installed (aliases cover names the model uses
        for apps whose launcher label differs, e.g. 短信 -> Messages)."""
        table = self._app_table()
        installed = set(self._labels)
        if name in table and table[name] in installed:
            return table[name]
        low = name.lower()
        for label, pkg in table.items():
            if pkg in installed and (label.lower() == low or low in label.lower() or label.lower() in low):
                return pkg
        return ""

    def installed_labels(self) -> str:
        self._app_table()
        return "、".join(sorted(set(self._labels.values())))

    def _current_app(self, obs: Observation) -> str:
        self._app_table()
        if not obs.package or "launcher" in obs.package:
            return "System Home"
        return self._labels.get(obs.package, obs.package)

    # ------------------------------------------------------------ decide

    def decide(self, session: Session, obs: Observation, feedback: str) -> Decision:
        st = session.decider_state
        ctx: list[dict] = st.setdefault("context", [])
        first = not ctx
        if first:
            ctx.append({"role": "system", "content": system_prompt()})

        info = {"current_app": self._current_app(obs)}
        notes = st.get("notes", [])[st.get("notes_sent", 0):]
        st["notes_sent"] = len(st.get("notes", []))
        focused = next((e for e in obs.elements if e.editable and e.focused), None)
        hints = [n for n in notes] + ([feedback] if feedback else [])
        sent: list[str] = st.setdefault("skills_sent", [])
        self._app_table()
        new = [tip for tip in skills.relevant(session.task, obs.package, visual=True, installed=set(self._labels))
               if tip not in sent]
        if new:  # once; it stays in the conversation
            sent.extend(new)
            info["App 经验"] = "\n".join(new)
        if focused is not None:
            hints.append(f"{KEYBOARD_CUE}：输入框「{focused.label or focused.hint}」已聚焦"
                         f"（当前内容 \"{focused.text}\"），可以直接 Type")
        if info["current_app"] == "System Home":
            # The agent display's own desktop (wallpaper + taskbar, or black before anything is drawn)
            # is easily taken for a lock screen; it never is one (FLAG_ALWAYS_UNLOCKED).
            hints.append("当前是桌面，不是锁屏，手机已解锁；需要的 App 用 Launch 打开")
        if hints:
            info["提示"] = "；".join(hints)
        screen_info = json.dumps(info, ensure_ascii=False)
        task = session.task + skills.task_note(session.task)
        text = f"{task}\n\n{screen_info}" if first else f"** Screen Info **\n\n{screen_info}"

        self.device.wait_frames_idle()
        jpeg = self.device.screenshot_jpeg(quality=85) or blank_frame(obs)
        cued = False
        if focused is not None:
            jpeg, cued = input_ready_cue(jpeg, obs, focused), True
        elif info["current_app"] == "System Home":
            jpeg, cued = desktop_cue(jpeg), True
        if cued:  # the trace keeps exactly what the model was shown
            (session.run_dir / f"step-{len(session.steps) + 1:02d}-model.jpg").write_bytes(jpeg)
        user = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
            {"type": "text", "text": text},
        ]}
        ctx.append(user)
        try:
            content, ms, usage = self.client.chat(ctx)
        finally:
            # keep only the current screenshot in the conversation, like Open-AutoGLM
            user["content"] = [p for p in user["content"] if p["type"] == "text"]
        thinking, action = split_response(content)
        ctx.append({"role": "assistant", "content": f"<think>{thinking}</think><answer>{action}</answer>"})

        try:
            parsed = parse_action(action)
        except ValueError as e:
            parsed = {"_metadata": "invalid", "error": str(e)}
        d = self._to_decision(parsed, obs, thinking, st)
        d["raw_action"] = action
        return Decision(d, ms, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), raw=content)

    def _to_decision(self, a: dict, obs: Observation, thinking: str, st: dict) -> dict:
        d = {"thought": thinking[:160], "action": "", "target": 0, "text": "", "package": "", "direction": ""}
        w, h = obs.width, obs.height

        def px(e) -> list[int]:
            return [min(int(e[0] / 1000 * w), w - 1), min(int(e[1] / 1000 * h), h - 1)]

        meta, act = a.get("_metadata"), a.get("action")
        if meta == "finish":
            d.update(action="done", text=a.get("message", ""))
        elif meta == "invalid":
            d.update(action="invalid", text=f"无法解析模型输出的动作：{a.get('error', '')}")
        elif act == "Tap":
            d.update(action="tap", point=px(a["element"]))
            st["last_point"] = d["point"]
            if "message" in a:
                d["sensitive"] = a.get("message") or "重要操作"
        elif act in ("Type", "Type_Name"):
            d.update(action="fill", text=a.get("text", ""))
            field_ = self._typing_target(obs, st.get("last_point"))
            if field_ is not None:
                d["target"] = field_.idx
        elif act == "Swipe":
            d.update(action="swipe", point=px(a["start"]), point2=px(a["end"]))
        elif act == "Long Press":
            d.update(action="long_press", point=px(a["element"]))
        elif act == "Double Tap":
            d.update(action="double_tap", point=px(a["element"]))
        elif act == "Launch":
            name = a.get("app", "")
            pkg = self._resolve(name)
            if pkg:
                d.update(action="launch", text=name, package=pkg)
            else:
                # E9: "发短信" made the model launch 微信, which is not installed; the raw launch error
                # did not tell it so, and it ended up flailing on the agent display's desktop.
                d.update(action="invalid",
                         text=f"「{name}」没有安装在这台手机上。这台手机上有：{self.installed_labels()}")
        elif act == "Back":
            d.update(action="back")
        elif act == "Home":
            d.update(action="home")
        elif act == "Wait":
            try:
                seconds = float(str(a.get("duration", "1")).replace("seconds", "").strip())
            except ValueError:
                seconds = 1.0
            d.update(action="wait", seconds=seconds)
        elif act == "Take_over":
            d.update(action="ask_user", text=a.get("message") or "需要你来处理（登录/验证等）")
        elif act == "Interact":
            d.update(action="ask_user", text="有多个符合条件的选项，需要你来选择")
        elif act in ("Note", "Call_API"):
            d.update(action="note", text=a.get("instruction") or a.get("message") or "")
        else:
            d.update(action=str(act or "unknown"))
        return d

    @staticmethod
    def _typing_target(obs: Observation, last_point):
        """Type goes to the focused field; if focus is not reported, the field under the last tap."""
        editable = [e for e in obs.elements if e.editable]
        focused = [e for e in editable if e.focused]
        if focused:
            return focused[0]
        if last_point:
            x, y = last_point
            under = [e for e in editable if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
            if under:
                return under[0]
        return editable[0] if len(editable) == 1 else None
