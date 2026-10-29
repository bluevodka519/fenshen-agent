"""The AutoGLM-Phone adapter: response parsing, coordinates, where Type goes, the input-ready cue."""

import io
import unittest

from fenshen.autoglm import AutoGLMDecider, input_ready_cue, parse_action, split_response
from fenshen.observation import Element, Observation

try:
    from PIL import Image
except ImportError:  # optional dependency (pip install .[vision])
    Image = None


class FakeDevice:
    def apps(self):
        return [{"package": "com.google.android.deskclock", "label": "时钟"},
                {"package": "com.google.android.apps.messaging", "label": "Messages"}]


def obs(*elements, package="com.google.android.deskclock"):
    return Observation(1, 7, 720, 1600, package, [], list(elements), raw={})


HOUR = Element(2, 21, "edit", "Hour", (80, 684, 272, 844), 1, text="09", clickable=True, editable=True, focused=True)
MINUTE = Element(5, 25, "edit", "Minute", (320, 684, 512, 844), 1, text="00", clickable=True, editable=True)


class SkillTest(unittest.TestCase):
    def test_tips_only_for_installed_apps(self):
        from fenshen import skills
        google = {"com.google.android.deskclock"}
        self.assertTrue(skills.relevant("帮我定一个早上7点的闹钟", "", True, installed=google))
        # a vendor phone (e.g. com.android.deskclock): the Google Clock tips would mislead
        self.assertFalse(skills.relevant("帮我定一个早上7点的闹钟", "", True, installed={"com.android.deskclock"}))
        # on screen anyway: still relevant
        self.assertTrue(skills.relevant("x", "com.google.android.deskclock", True, installed=set()))


class TaskNoteTest(unittest.TestCase):
    def test_alarm_target_in_12h(self):
        from fenshen import skills
        self.assertIn("10:00 PM", skills.task_note("设一个晚上10点的闹钟"))
        self.assertIn("12:00 PM", skills.task_note("帮我定一个中午12点的闹钟"))
        self.assertIn("不是微信", skills.task_note("给张伟发短信：你好"))  # E9: "短信" made it reach for WeChat
        self.assertEqual(skills.task_note("新建联系人李雷，电话13900001111"), "")

    def test_permission_tip_by_screen_only(self):
        from fenshen import skills
        self.assertTrue(skills.relevant("新建联系人李雷", "com.google.android.permissioncontroller", True))
        self.assertFalse(any("权限请求" in t for t in skills.relevant("新建联系人李雷", "", True)))


class ParseTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_response('<think>点加号</think><answer>do(action="Tap", element=[842,775])</answer>'),
                         ("点加号", 'do(action="Tap", element=[842,775])'))
        self.assertEqual(split_response('想一想 finish(message="好了")'), ("想一想", 'finish(message="好了")'))

    def test_actions(self):
        self.assertEqual(parse_action('do(action="Tap", element=[842,775])'),
                         {"_metadata": "do", "action": "Tap", "element": [842, 775]})
        self.assertEqual(parse_action('do(action="Type", text="明天下午三点开会，别迟到")')["text"], "明天下午三点开会，别迟到")
        self.assertEqual(parse_action('do(action="Swipe", start=[500,800], end=[500,200])')["end"], [500, 200])
        self.assertEqual(parse_action('finish(message="完成")'), {"_metadata": "finish", "message": "完成"})
        with self.assertRaises(ValueError):
            parse_action("tap somewhere")

    def test_type_with_a_trailing_code_fence(self):
        # E9: upstream's slicing typed '13900001111")' + newline + backtick into a phone number
        a = parse_action('do(action="Type", text="13900001111")\n```')
        self.assertEqual(a["text"], "13900001111")
        # quotes inside the text survive: it ends at the last '")'
        self.assertEqual(parse_action('do(action="Type", text="他说"好的"")')["text"], '他说"好的"')


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.d = AutoGLMDecider(FakeDevice())

    def test_coordinates_are_0_to_999(self):
        d = self.d._to_decision({"_metadata": "do", "action": "Tap", "element": [500, 500]}, obs(), "", {})
        self.assertEqual(d["point"], [360, 800])
        edge = self.d._to_decision({"_metadata": "do", "action": "Tap", "element": [999, 999]}, obs(), "", {})
        self.assertEqual(edge["point"], [719, 1598])

    def test_sensitive_tap_is_marked(self):
        d = self.d._to_decision({"_metadata": "do", "action": "Tap", "element": [1, 1], "message": "重要操作"}, obs(), "", {})
        self.assertEqual(d["sensitive"], "重要操作")

    def test_type_goes_to_the_focused_field_not_the_one_the_model_means(self):
        # E6 round A: the model swiped the minute box, then typed; the hour box still had focus
        d = self.d._to_decision({"_metadata": "do", "action": "Type", "text": "15"}, obs(HOUR, MINUTE), "", {})
        self.assertEqual(d["target"], HOUR.idx)

    def test_launch_resolves_labels(self):
        d = self.d._to_decision({"_metadata": "do", "action": "Launch", "app": "时钟"}, obs(), "", {})
        self.assertEqual(d["package"], "com.google.android.deskclock")
        d = self.d._to_decision({"_metadata": "do", "action": "Launch", "app": "短信"}, obs(), "", {})
        self.assertEqual(d["package"], "com.google.android.apps.messaging")  # label is "Messages" on this image
        # not installed, even when an alias exists (微信): tell the model what this phone does have
        for name in ("支付宝", "微信"):
            d = self.d._to_decision({"_metadata": "do", "action": "Launch", "app": name}, obs(), "", {})
            self.assertEqual(d["action"], "invalid")
            self.assertIn("没有安装", d["text"])
            self.assertIn("Messages", d["text"])
        d = self.d._to_decision({"_metadata": "do", "action": "Launch", "app": "messages"}, obs(), "", {})
        self.assertEqual(d["package"], "com.google.android.apps.messaging")

    def test_take_over_is_a_handoff(self):
        d = self.d._to_decision({"_metadata": "do", "action": "Take_over", "message": "请登录"}, obs(), "", {})
        self.assertEqual((d["action"], d["text"]), ("ask_user", "请登录"))


@unittest.skipIf(Image is None, "Pillow not installed")
class CueTest(unittest.TestCase):
    def frame(self):
        out = io.BytesIO()
        Image.new("RGB", (360, 800), (0, 0, 0)).save(out, "JPEG")
        return out.getvalue()

    def test_bar_is_drawn_where_a_keyboard_would_be(self):
        img = Image.open(io.BytesIO(input_ready_cue(self.frame(), obs(HOUR), HOUR)))
        self.assertGreater(img.getpixel((180, 755))[0], 30)  # gray bar near the bottom
        self.assertLess(img.getpixel((180, 100))[0], 10)  # the rest is untouched

    def test_skipped_when_it_would_hide_the_field(self):
        low = Element(1, 1, "edit", "Text message", (40, 1500, 560, 1580), 1, editable=True, focused=True)
        src = self.frame()
        self.assertEqual(input_ready_cue(src, obs(low), low), src)


if __name__ == "__main__":
    unittest.main()
