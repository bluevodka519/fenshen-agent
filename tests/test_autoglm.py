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
        d = self.d._to_decision({"_metadata": "do", "action": "Launch", "app": "支付宝"}, obs(), "", {})
        self.assertEqual(d["package"], "")  # not installed: the loop rejects it instead of guessing
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
