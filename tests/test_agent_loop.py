"""The agent loop against a fake phone and a scripted decider: guards, approval, completion, stops.

The fake screen is a minimal "messages" page (a text field and a Send button, optionally a Pay
button). The decider plays back fixed actions, so these tests pin down what the loop does with
them, independent of any model.
"""

import tempfile
import unittest
from pathlib import Path

from fenshen.agent import HANDOFF_ONLY, NEEDS_CONFIRM, Agent, Decision, Session
from fenshen.observation import Element, Observation
from fenshen.verify import Verdict


class FakePhone:
    def __init__(self, with_pay=False):
        self.adb = None
        self.text = ""
        self.sent = False
        self.with_pay = with_pay
        self.actions = []
        self.seq = 0

    # what the agent sees -------------------------------------------------------------
    def observe(self):
        self.seq += 1
        els = [Element(1, 11, "edit", "Text message", (40, 1400, 560, 1480), 1, text=self.text,
                       clickable=True, editable=True, focused=True),
               Element(2, 12, "button", "Send SMS", (580, 1400, 700, 1480), 1, clickable=True)]
        if self.with_pay:
            els.append(Element(3, 13, "button", "Pay ¥25", (40, 1500, 700, 1580), 1, clickable=True))
        if self.sent:
            els.append(Element(len(els) + 1, 14, "text", f"sent: {self.text}", (40, 600, 700, 660), 1))
        return Observation(self.seq, 7, 720, 1600, "com.example.messages", [], els, raw={})

    def screenshot_jpeg(self, quality=70, max_width=0):
        return None

    def wait_frames_idle(self, *a, **k):
        pass

    def wait_idle(self, *a, **k):
        return True

    def is_running(self, package):
        return True

    def foreign_visible_packages(self):
        return []

    # what the agent does -------------------------------------------------------------
    def click(self, el):
        self.actions.append(("click", el.label))
        if el.label == "Send SMS" and self.text:
            self.sent = True
        return "node_click"

    def fill(self, el, text):
        self.actions.append(("fill", el.label, text))
        self.text = text
        return "set_text"


class SentVerifier:
    description = "a message was sent"

    def __init__(self, phone):
        self.phone = phone

    def prepare(self, device):
        return ""

    def goal_reached(self, device):
        return self.phone.sent

    def unintended_change(self, device):
        return None

    def check(self, device, obs, claimed):
        return Verdict(self.phone.sent, "fake", "sent" if self.phone.sent else "nothing sent")


class Script:
    """Decider that plays back actions; each is (action, target, text)."""

    vision = False
    name = "script"

    def __init__(self, *actions):
        self.actions = list(actions)
        self.feedback = []

    def decide(self, session, obs, feedback):
        self.feedback.append(feedback)
        action, target, text = self.actions.pop(0)
        return Decision({"thought": "", "action": action, "target": target, "text": text, "package": "", "direction": ""})


def make(phone, script, max_steps=10):
    tmp = Path(tempfile.mkdtemp())
    session = Session("给王小明发短信：你好", tmp / "run")
    session.verifier = SentVerifier(phone)
    agent = Agent(phone, script, max_steps=max_steps)
    agent._settle = lambda before, grace_s=2.0: phone.observe()  # no real UI to wait for
    return agent, session


class ApprovalTest(unittest.TestCase):
    def test_send_waits_for_the_user_then_completes_without_another_model_turn(self):
        phone = FakePhone()
        agent, session = make(phone, Script(("fill", 1, "你好"), ("tap", 2, "")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertFalse(phone.sent, "the agent must not press Send by itself")
        self.assertEqual(session.pending["label"], "Send SMS")
        self.assertEqual(session.steps[-1].status, "guarded")

        session.approve()
        out = agent.run(session)  # the script is empty now: any model call would fail
        self.assertEqual(out.status, "done")
        self.assertTrue(phone.sent)
        self.assertEqual(phone.actions[-1], ("click", "Send SMS"))

    def test_declining_tells_the_model_not_to_work_around_it(self):
        phone = FakePhone()
        script = Script(("fill", 1, "你好"), ("tap", 2, ""), ("ask_user", 0, "用户不让发"))
        agent, session = make(phone, script)
        agent.run(session)
        session.resume_note("")
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertFalse(phone.sent)
        self.assertIn("不要再尝试它", session.history[-1])


class GuardTest(unittest.TestCase):
    def test_payment_is_never_done_by_the_agent(self):
        phone = FakePhone(with_pay=True)
        agent, session = make(phone, Script(("tap", 3, "")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertIsNone(session.pending, "payment cannot be approved for the agent to do")
        self.assertEqual(phone.actions, [])

    def test_patterns(self):
        for label in ["Pay ¥25", "提交订单", "Log in", "验证码", "Place order", "立即下单", "去支付", "立即购买"]:
            self.assertTrue(HANDOFF_ONLY.search(label), label)
        # sales counts on a product card (Meituan, real phone): tapping "+" there is not paying
        for label in ["70+回头客推荐 46人觉得味道赞 14人觉得分量足 近期400+人已下单", "1000+人付款", "已购买"]:
            self.assertFalse(HANDOFF_ONLY.search(label), label)
        for label in ["Send SMS", "Delete", "Allow", "允许", "Only this time", "同意并继续"]:
            self.assertTrue(NEEDS_CONFIRM.search(label), label)
        for label in ["Don’t allow", "Don't allow", "不允许", "OK", "Cancel", "Start chat", "Send to (380) 000-1234 +13800001234"]:
            self.assertFalse(NEEDS_CONFIRM.search(label) or HANDOFF_ONLY.search(label), label)


class LoginTest(unittest.TestCase):
    def test_login_tapped_by_mistake_is_blocked_once_then_handed_off(self):
        phone = FakePhone()
        base = phone.observe

        def with_avatar():
            o = base()
            o.elements.append(Element(9, 19, "button", "Sign in", (620, 40, 700, 110), 1, clickable=True))
            return o

        phone.observe = with_avatar
        # first tap on "Sign in": not executed, the model is told to take another route (it fills the text);
        # second tap on it: login is really wanted, so the user takes over
        agent, session = make(phone, Script(("tap", 9, ""), ("fill", 1, "你好"), ("tap", 9, "")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertIsNone(session.pending)
        self.assertNotIn(("click", "Sign in"), phone.actions)
        self.assertIn(("fill", "Text message", "你好"), phone.actions)
        self.assertIn("登录入口", agent.decider.feedback[1])


class IntentGuardTest(unittest.TestCase):
    def test_unlabelled_pay_button_is_caught_by_the_models_own_words(self):
        # commerce apps may draw the submit button themselves: no label in the tree to match
        phone = FakePhone()
        base = phone.observe

        def opaque_button():
            o = base()
            o.elements.append(Element(9, 19, "button", "", (40, 1500, 700, 1580), 1, clickable=True))
            return o

        phone.observe = opaque_button

        class Says(Script):
            def decide(self, session, obs, feedback):
                dec = super().decide(session, obs, feedback)
                dec.d["intent"] = "商品和数量都对了，现在点击底部的提交订单按钮"
                return dec

        agent, session = make(phone, Says(("tap", 9, "")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertIsNone(session.pending, "paying cannot be approved for the agent to do")
        self.assertNotIn(("click", ""), phone.actions)


class ChangedWhileThinkingTest(unittest.TestCase):
    def test_rotating_placeholder_is_the_same_element(self):
        # Meituan's search box changes its hint text every few seconds (real phone, E10)
        agent, _ = make(FakePhone(), Script())
        point = {"point": [360, 150]}
        before = Observation(1, 7, 720, 1600, "m", [], [Element(1, 1, "button", "宋二爷·乡下土菜馆", (40, 110, 680, 190), 1, clickable=True)], raw={})
        after = Observation(2, 7, 720, 1600, "m", [], [Element(1, 2, "button", "营养又健康的快餐盒饭", (40, 110, 680, 190), 1, clickable=True)], raw={})
        self.assertTrue(agent._point_still_valid(point, before, after))

    def test_a_different_element_is_not(self):
        agent, _ = make(FakePhone(), Script())
        point = {"point": [360, 150]}
        before = Observation(1, 7, 720, 1600, "m", [], [Element(1, 1, "button", "搜索", (40, 110, 680, 190), 1, clickable=True)], raw={})
        dialog = Observation(2, 7, 720, 1600, "m", [], [Element(1, 2, "button", "领取红包", (0, 0, 720, 900), 1, clickable=True)], raw={})
        self.assertFalse(agent._point_still_valid(point, before, dialog))


class QuantityTest(unittest.TestCase):
    TASK = "帮我在美团上点一份烧烤,需要2份烤土豆片,3份烤韭菜,4份烤生蚝"

    def shop(self, oysters):
        # the Meituan search result list as the accessibility tree had it (real phone, 2026-10-29)
        els = [Element(1, 1, "button", "烤锡纸蒜蓉生蚝（招牌必点） ¥ 6", (20, 160, 700, 420), 1, clickable=True),
               Element(2, 2, "button", "单点不送", (300, 260, 420, 290), 1, clickable=True),
               Element(3, 3, "button", f"减少，已添加{oysters}份", (440, 300, 500, 340), 1, clickable=True),
               Element(4, 4, "button", str(oysters), (500, 300, 560, 340), 1, clickable=True),
               Element(5, 5, "button", f"增加，已添加{oysters}份", (650, 295, 700, 340), 1, clickable=True),
               Element(6, 6, "button", "锡纸蒜蓉生蚝 ¥ 6", (20, 450, 700, 700), 1, clickable=True),
               Element(7, 7, "button", "5份起购", (560, 640, 700, 690), 1, clickable=True)]
        return Observation(1, 7, 720, 1600, "com.sankuai.meituan", [], els, raw={})

    def check(self, oysters):
        agent, session = make(FakePhone(), Script())
        session.task = self.TASK
        d = {"action": "tap", "point": [673, 315], "target": 0, "text": ""}
        obs = self.shop(oysters)
        agent._bind_point_target(d, obs)
        return agent._quantity_check(session, d, obs)

    def test_stops_adding_at_the_asked_amount(self):
        self.assertIsNone(self.check(3))
        problem = self.check(4)
        self.assertIn("已经加了 4 份", problem)
        self.assertIn("任务要 4 份「烤生蚝」", problem)

    def test_a_shop_minimum_above_the_amount_is_accepted_not_topped_up(self):
        problem = self.check(5)  # one tap on a "5份起购" item
        self.assertIn("起购量", problem)

    def test_parsing(self):
        from fenshen.verify import parse_quantities, wanted_quantity
        self.assertIn(("烤生蚝", 4), parse_quantities(self.TASK))
        self.assertEqual(parse_quantities("点两杯美式和十二串羊肉串然后结算"), [("美式", 2), ("羊肉串", 12)])
        self.assertEqual(wanted_quantity(self.TASK, "烤土豆 ¥ 5"), (2, "烤土豆片"))  # shops shorten names
        self.assertEqual(wanted_quantity(self.TASK, "烤木签青椒 ¥ 3"), (4, ""))  # not asked for: capped
        self.assertEqual(wanted_quantity("帮我点瑞幸的生椰拿铁", "生椰拿铁（原创） ¥ 12.9"), (1, ""))


class ConnectionLostTest(unittest.TestCase):
    def test_phone_going_away_is_reported_plainly_and_not_resumed(self):
        # 2026-10-29: the adb server restarted mid-task; the helper died with it and "continue" then
        # failed with a bare WinError 10054
        from fenshen.runner import CONNECTION_LOST, Runner

        phone = FakePhone()
        state = {"alive": True}

        def gone():
            state["alive"] = False
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

        phone.observe = gone
        phone.alive = lambda: state["alive"]
        runner = Runner.__new__(Runner)  # no adb: only the parts run() touches
        runner.agent, session = make(phone, Script(("tap", 1, "")))
        runner.device, runner.monitor, runner.notifier, runner._monitor = phone, False, None, None
        session.prepared = True

        out = runner.run(session)
        self.assertEqual((out.status, out.message), ("failed", CONNECTION_LOST))
        out = runner.run(session)  # "continue" on the dead display
        self.assertEqual((out.status, out.message), ("failed", CONNECTION_LOST))


class StopTest(unittest.TestCase):
    def test_three_invalid_actions_in_a_row(self):
        phone = FakePhone()
        agent, session = make(phone, Script(("tap", 99, ""), ("tap", 99, ""), ("tap", 99, "")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertIn("连续 3 次", out.message)
        self.assertEqual(phone.actions, [])

    def test_same_action_on_the_same_screen_three_times(self):
        phone = FakePhone()
        phone.observe = lambda: Observation(1, 7, 720, 1600, "com.example.messages", [],
                                            [Element(1, 11, "button", "Retry", (0, 0, 100, 100), 1, clickable=True)], raw={})
        agent, session = make(phone, Script(*[("tap", 1, "")] * 5))
        phone.click = lambda el: "node_click"
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user")
        self.assertIn("重复", out.message)

    def test_done_claim_is_checked_by_the_verifier(self):
        phone = FakePhone()
        agent, session = make(phone, Script(("done", 0, "已经发了")))
        out = agent.run(session)
        self.assertEqual(out.status, "needs_user", "a claim without the system state is not done")
        self.assertEqual(session.steps[-1].status, "rejected_done")

    def test_environment_decides_completion(self):
        phone = FakePhone()
        agent, session = make(phone, Script(("fill", 1, "你好")))  # no "done" in the script

        class TextWritten(SentVerifier):
            def goal_reached(self, device):
                return self.phone.text == "你好"

            def check(self, device, obs, claimed):
                return Verdict(True, "fake", "text written")

        session.verifier = TextWritten(phone)
        out = agent.run(session)
        self.assertEqual(out.status, "done")
        self.assertEqual(len(session.steps), 1)


if __name__ == "__main__":
    unittest.main()
