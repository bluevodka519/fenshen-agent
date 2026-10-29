"""Turns a raw helper snapshot into a short, numbered element list the model can act on."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

MAX_LABEL = 80

_ROLE_BY_CLASS = {
    "EditText": "edit",
    "AutoCompleteTextView": "edit",
    "Button": "button",
    "ImageButton": "button",
    "FloatingActionButton": "button",
    "CheckBox": "check",
    "Switch": "switch",
    "SwitchCompat": "switch",
    "ToggleButton": "switch",
    "RadioButton": "radio",
    "Spinner": "dropdown",
    "SeekBar": "slider",
    "RecyclerView": "list",
    "ListView": "list",
    "ScrollView": "list",
    "HorizontalScrollView": "list",
    "ViewPager": "list",
    "TextView": "text",
    "ImageView": "image",
    "WebView": "web",
}


@dataclass
class Element:
    idx: int
    node: int
    role: str
    label: str
    bounds: tuple[int, int, int, int]
    window: int
    rid: str = ""
    text: str = ""
    hint: str = ""
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    focused: bool = False
    selected: bool = False
    enabled: bool = True
    password: bool = False
    row: int | None = None  # position in its list (adapter index), stable across scrolling

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def actionable(self) -> bool:
        return self.clickable or self.editable or self.scrollable or self.checkable

    def render(self) -> str:
        parts = [f"[{self.idx}] {self.role}"]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.editable:
            parts.append(f'text="{self.text}"' if self.text and self.text != self.label else 'text=""')
            if self.hint and self.hint != self.label:
                parts.append(f'hint="{self.hint}"')
        flags = [
            name
            for name, on in (
                ("checked", self.checkable and self.checked),
                ("unchecked", self.checkable and not self.checked),
                ("selected", self.selected),
                ("focused", self.focused),
                ("disabled", not self.enabled),
                ("password", self.password),
                ("scrollable", self.scrollable),
            )
            if on
        ]
        if flags:
            parts.append("(" + ", ".join(flags) + ")")
        if not self.actionable:
            parts.append("·static")
        return " ".join(parts)


@dataclass
class Observation:
    seq: int
    display_id: int
    width: int
    height: int
    package: str
    windows: list[dict]
    elements: list[Element]
    raw: dict = field(repr=False)
    taken_at: float = field(default_factory=time.time)

    @property
    def signature(self) -> str:
        """Stable fingerprint of what is on screen, for loop detection and change checks."""
        h = hashlib.sha1(self.package.encode())
        for e in self.elements:
            left, top, right, bottom = e.bounds
            h.update(f"{e.role}|{e.label}|{e.text}|{e.checked}|{left // 8},{top // 8},{right // 8},{bottom // 8};".encode())
        return h.hexdigest()[:16]

    def element(self, idx: int) -> Element:
        for e in self.elements:
            if e.idx == idx:
                return e
        raise KeyError(f"no element [{idx}] on this screen")

    def find(self, label: str, role: str | None = None) -> Element | None:
        """First element whose label/text/hint contains `label` (case-insensitive)."""
        needle = label.lower()
        for e in self.elements:
            if role and e.role != role:
                continue
            if needle in e.label.lower() or needle in e.text.lower() or needle in e.hint.lower():
                return e
        return None

    def render(self) -> str:
        lines = [f"App: {self.package or '(none)'}   screen {self.width}x{self.height}"]
        for w in self.windows:
            if w.get("type") not in ("app", None):
                lines.append(f"  window: {w.get('type')} {w.get('pkg', '')} {w.get('title', '')}".rstrip())
        lines.extend(e.render() for e in self.elements)
        if not self.elements:
            lines.append("（屏幕上没有可操作的元素；如果是空白桌面，请用 launch 打开需要的 App）")
        return "\n".join(lines)


def _role(node: dict) -> str:
    if node.get("edt"):
        return "edit"
    cls = node.get("c", "")
    role = _ROLE_BY_CLASS.get(cls)
    if role is None:
        for suffix, r in _ROLE_BY_CLASS.items():
            if cls.endswith(suffix):
                role = r
                break
    if role in (None, "text", "image") and node.get("chkb"):
        return "check"
    if role in (None, "text", "image") and node.get("clk"):
        return "button"
    if role is None and node.get("scr"):
        return "list"
    return role or "item"


def _visible(node: dict, width: int, height: int) -> bool:
    if node.get("inv"):
        return False
    left, top, right, bottom = node["b"]
    if right <= left or bottom <= top:
        return False
    return right > 0 and bottom > 0 and left < width and top < height


def compress(snapshot: dict) -> Observation:
    width, height = snapshot["width"], snapshot["height"]
    nodes = {n["i"]: n for n in snapshot["nodes"]}
    children: dict[int, list[int]] = {}
    for n in snapshot["nodes"]:
        children.setdefault(n["p"], []).append(n["i"])

    def own_label(n: dict) -> str:
        return (n.get("t") or n.get("d") or n.get("h") or "").strip()

    def descendant_texts(i: int, out: list[str], consumed: set[int]) -> None:
        for c in children.get(i, []):
            cn = nodes[c]
            if cn.get("clk") or cn.get("edt") or cn.get("scr") or cn.get("chkb"):
                continue  # a nested control gets its own element
            text = own_label(cn)
            if text and _visible(cn, width, height):
                out.append(text)
                consumed.add(c)
            descendant_texts(c, out, consumed)

    def row_of(i: int) -> int | None:
        while i != -1 and i in nodes:
            if "row" in nodes[i]:
                return nodes[i]["row"]
            i = nodes[i]["p"]
        return None

    def has_actionable_ancestor(i: int) -> bool:
        p = nodes[i]["p"]
        while p != -1 and p in nodes:
            pn = nodes[p]
            if pn.get("clk") or pn.get("edt") or pn.get("chkb"):
                return True
            p = pn["p"]
        return False

    # Only application windows (incl. the app's own dialogs and popups). System decorations on the
    # agent display (taskbar, nav buttons) are not part of the task and are often not even drawn.
    window_type = {w["id"]: w.get("type") for w in snapshot["windows"]}

    elements: list[Element] = []
    consumed: set[int] = set()
    for n in snapshot["nodes"]:  # document order
        if window_type.get(n["w"]) != "app" or not _visible(n, width, height):
            continue
        actionable = bool(n.get("clk") or n.get("lclk") or n.get("edt") or n.get("scr") or n.get("chkb"))
        if actionable:
            if n.get("edt"):
                label = (n.get("h") or n.get("d") or "").strip()
                if not label:
                    parts: list[str] = []
                    descendant_texts(n["i"], parts, consumed)
                    label = " ".join(parts)
            else:
                label = own_label(n)
                if not label:
                    parts = []
                    descendant_texts(n["i"], parts, consumed)
                    label = " ".join(parts)
        else:
            if n["i"] in consumed or has_actionable_ancestor(n["i"]):
                continue
            label = own_label(n)
            if not label:
                continue
        if len(label) > MAX_LABEL:
            label = label[: MAX_LABEL - 1] + "…"
        elements.append(
            Element(
                idx=len(elements) + 1,
                node=n["i"],
                role=_role(n),
                label=label,
                bounds=tuple(n["b"]),
                window=n["w"],
                rid=n.get("r", "").split("/")[-1],
                text=n.get("t", "") if n.get("edt") else "",
                hint=n.get("h", ""),
                clickable=bool(n.get("clk") or n.get("lclk")),
                editable=bool(n.get("edt")),
                scrollable=bool(n.get("scr")),
                checkable=bool(n.get("chkb")),
                checked=bool(n.get("chk")),
                focused=bool(n.get("foc")),
                selected=bool(n.get("sel")),
                enabled=not n.get("dis"),
                password=bool(n.get("pwd")),
                row=row_of(n["i"]),
            )
        )

    app_windows = [w for w in snapshot["windows"] if w.get("type") == "app" and w.get("pkg")]
    focused = [w for w in app_windows if w.get("focused") or w.get("active")]
    package = (focused or app_windows or [{}])[0].get("pkg", "")
    return Observation(
        seq=snapshot["seq"],
        display_id=snapshot["display_id"],
        width=width,
        height=height,
        package=package,
        windows=snapshot["windows"],
        elements=elements,
        raw=snapshot,
    )
