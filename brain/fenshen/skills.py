"""Short per-app know-how ("skills"), added to the prompt only when relevant.

These are hints about how an app's UI works, not click scripts: the model still decides every
step from the live screen, and the guard / verifier apply as usual. WhalePhone does the same
for its featured apps (WeChat, Meituan, Amap).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Skill:
    package: str
    keywords: str  # regex matched against the task
    text: str  # for text models: refers to the element list and its actions (fill, check "00"...)
    visual: str = ""  # for screenshot models: what the screen looks like, in Tap/Type terms
    # The task's own parameters in the app's format (e.g. 24h -> the picker's 12h + AM/PM). This is
    # the actual target, not an example, so there is nothing wrong to copy.
    params: Callable[[str], str] | None = None


def _alarm_params(task: str) -> str:
    from .verify import parse_time  # same parser the verifier checks against

    t = parse_time(task)
    if t is None:
        return ""
    hour, minute = t
    h12, ampm = hour % 12 or 12, "AM" if hour < 12 else "PM"
    return (f"这次任务的目标时间，换成这个时钟的格式是 {h12}:{minute:02d} {ampm}："
            f"小时框填 {h12}，分钟框填 {minute:02d}，并确认选中的是 {ampm}（打开时默认选中的不一定对）。")


SKILLS = [
    Skill(
        package="com.google.android.deskclock",
        keywords=r"闹钟|alarm|叫我起床",
        text=(
            "时钟 App（com.google.android.deskclock）新建闹钟的界面要点：\n"
            "- 在底部 Alarms 页点 \"Add alarm\"，会弹出 \"Select time\" 时间选择框（弹出需要一点时间）。\n"
            "- 最简单的做法：点 \"Switch to text input mode for the time input.\" 切到文本输入，"
            "然后对 \"Hour\" 输入框 fill 任务要求的小时（12 小时制）；分钟字段在 \"Minute\" 文字上方，"
            "没选中时显示为 check \"00\"，直接对它 fill 任务要求的分钟即可。小时和分钟一定要用任务里的数字。\n"
            "- 上午选 AM、下午选 PM；已经是 checked 的不用再点。\n"
            "- 小时、分钟、AM/PM 三项都对了，下一步就是点 \"OK\"，不要再改任何一项。\n"
            "- 点 \"OK\" 保存。保存后列表里出现这个时间的闹钟，开关自动打开，这时任务就完成了，用 done。\n"
            "- 新建时不要点已有闹钟的开关，也不要重复点 Add alarm。\n"
            "- 删除闹钟：点那个闹钟的时间（如 \"7:30 AM\"）展开它，再点出现的 \"Delete\"（系统会请用户确认）。"
            "关掉开关只是停用，不等于删除。删除多个就一个一个来。"
        ),
        # Written from AutoGLM's failure modes in runs 20261028-1056..1100 (no example times on purpose:
        # the 7B model copied example numbers from the first version of the text skill).
        visual=(
            "时钟 App 新建闹钟要点：Alarms 页右下角的 + 新建闹钟，会弹出 Select time。"
            "小时和分钟是两个数字输入框，不是滚轮，滑动它们没有任何作用：先点一下要改的框让它高亮，再用 Type 输入数字；"
            "Type 只会写进当前高亮的那个框，所以改另一个框之前要先点它。"
            "它是 12 小时制，小时框只能填 1 到 12；下午和晚上的时间，小时框填 12 小时制的钟点，再点 PM（上午点 AM）。"
            "小时框的数字加上选中的 PM，就是下午的那个钟点，不需要也不能填 13 到 23。"
            "点 OK 之前看一眼 AM/PM 高亮的是哪个：打开时默认选中的不一定对。"
            "三项都对了就点 OK；保存后列表里出现这个闹钟且开关是开的，任务就完成了。不要动已有闹钟的开关。"
        ),
        params=_alarm_params,
    ),
    Skill(
        package="com.google.android.contacts",
        keywords=r"联系人|通讯录|contact",
        text=(
            "联系人 App（com.google.android.contacts）新建联系人：点 \"Create contact\"（右下角 +）；"
            "如果问保存到哪里，选设备（Device）；中文姓名整个 fill 进 \"First name\"，号码 fill 进 \"Phone\"；"
            "填完点 \"Save\"。保存后会显示这个联系人的详情页，任务的这一部分就完成了。"
        ),
        visual=(
            "联系人 App 新建联系人：点右下角的 +（Create contact）；如果问保存到哪个账号，选设备（Device）。"
            "中文姓名整个填进 First name（点一下再 Type），号码填进 Phone；填完点右上角 Save。"
            "保存后出现这个联系人的详情页，这一步就完成了，不要重复新建。"
        ),
    ),
    Skill(
        package="com.google.android.apps.messaging",
        keywords=r"短信|信息|消息|sms",
        text=(
            "短信 App（com.google.android.apps.messaging）发短信：首页点 \"Start chat\"；"
            "在 To 输入框 fill 联系人名字或号码，然后点下面列表里对应的联系人；"
            "进入对话后对底部输入框 fill 短信内容，再点发送按钮（Send SMS）。发送前系统会请用户确认。"
            "如果弹出聊天功能、RCS 之类的推广，点 \"Not now\" / \"No thanks\" 跳过。"
        ),
        visual=(
            "短信 App 发短信：首页右下角点 Start chat 新建对话；在顶部 To 输入框里输入联系人名字或号码，"
            "再点下面列表里出现的那个联系人；进入对话后点底部的输入框（Text message）再 Type 短信内容，"
            "最后点右边的发送按钮。发送前系统会请用户确认，这是正常的。"
            "输入号码后如果打开的是某个联系人的对话，说明这个号码就是那个联系人的，直接在这个对话里发即可。"
            "如果弹出聊天功能、RCS 一类的推广，点 Not now / No thanks 跳过，不要开启任何功能。"
        ),
    ),
]


SKILLS.append(Skill(
    package="com.sankuai.meituan",
    keywords=r"美团|外卖",
    text=(
        "美团（com.sankuai.meituan）点外卖：首页点「外卖」进入外卖频道，或用顶部搜索框搜店名、商品；进店后找到商品，"
        "点「选规格」或「+」，选好规格后点「加入购物车」/「选好了」；购物车里有别的商品就先清空；然后点「去结算」进入确认订单页。"
        "确认订单页的「提交订单」和任何支付按钮由用户本人点，到这一页就用 done。弹出红包、优惠券、会员开通之类的弹窗就关掉，"
        "不要开通任何会员或付费服务。"
    ),
    visual=(
        "美团点外卖要点：首页点「外卖」进入外卖频道，也可以直接用顶部搜索框搜店名或商品。进店后找到商品，点「选规格」或「+」，"
        "选好杯型、温度、甜度后点「加入购物车」或「选好了」。如果这家店的购物车里已经有别的商品，先清空再加。"
        "然后点右下角「去结算」，进入确认订单页。确认订单页底部的「提交订单」和任何支付按钮都由用户本人来点："
        "到了确认订单页、商品和数量都对，任务就完成了，用 finish，不要点「提交订单」。"
        "弹出红包、优惠券、开通会员、定位授权一类的弹窗时，点 × 或「暂不」「跳过」关掉，不要领取需要付费的东西，不要开通任何会员。"
    ),
))


# System permission dialogs: shown by package only (never matched by task words). Declining is the
# default; granting needs the user anyway (NEEDS_CONFIRM matches "Allow"). In the 2026-10-29 benchmark
# the model chose "Allow" for a notification prompt and two contact tasks stopped for the user.
_PERMISSION_TIP = (
    "这是系统的权限请求弹窗。任务用不到这个权限时（例如通知权限），点 Don't allow / 不允许，然后继续任务；"
    "只有任务确实离不开这个权限时才选允许，那一步会交给用户确认。"
)
for _pkg in ("com.google.android.permissioncontroller", "com.android.permissioncontroller"):
    SKILLS.append(Skill(package=_pkg, keywords=r"(?!)", text=_PERMISSION_TIP, visual=_PERMISSION_TIP))


def task_note(task: str) -> str:
    """A short normalization appended to the task itself: models weigh the task text more than tips
    (AutoGLM typed "22" into a 1-12 hour box although the tip said 10)."""
    if re.search(r"闹钟|alarm|叫我起床", task, re.I):
        from .verify import parse_time

        t = parse_time(task)
        if t:
            hour, minute = t
            return f"（目标时间是 {hour:02d}:{minute:02d}，12 小时制写作 {hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}）"
    if re.search(r"美团|外卖|饿了么", task):
        from .verify import parse_quantities

        if parse_quantities(task):
            # 2026-10-29: asked for 4 oysters, the model counted its own taps on "+" and reached 16
            return ("（数量：每样东西加了几份，看商品那一行「-」和「+」中间的数字，点到任务要的份数就停；"
                    "写着「N份起购」的商品点一次就会加 N 份，够了就别再点「+」）")
    if re.search(r"短信|sms", task, re.I):
        # E9: "发短信" made the model reach for WeChat, and once even leave Google Messages for it.
        return "（这是手机短信：用系统自带的短信 App，也就是 Messages，不是微信）"
    return ""


def relevant(task: str, package: str, visual: bool = False, installed: set[str] | None = None) -> list[str]:
    """Skills for the app on screen, or for apps the task talks about. `installed` (launcher packages)
    drops skills for apps this phone does not have: the Google Clock tips would mislead a model
    driving a vendor clock app."""
    hits = [s for s in SKILLS if s.package == package
            or (re.search(s.keywords, task, re.I) and (installed is None or s.package in installed))]
    out = []
    for s in hits:
        text = (s.visual or s.text) if visual else s.text
        extra = s.params(task) if s.params else ""
        out.append(f"{text}\n{extra}" if extra else text)
    return out
