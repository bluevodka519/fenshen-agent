"""Turning a raw accessibility snapshot into the short element list a model acts on."""

import unittest

from fenshen.observation import compress


def node(i, p, w, cls, b, **kw):
    return {"i": i, "p": p, "w": w, "c": cls, "b": b, **kw}


SNAPSHOT = {
    "seq": 3, "display_id": 7, "width": 720, "height": 1600,
    "windows": [
        {"id": 1, "type": "app", "pkg": "com.google.android.contacts", "focused": True},
        {"id": 2, "type": "system", "pkg": "com.android.systemui"},
    ],
    "nodes": [
        node(0, -1, 1, "FrameLayout", [0, 0, 720, 1600]),
        node(1, 0, 1, "TextView", [40, 100, 400, 160], t="Create contact"),
        node(2, 0, 1, "EditText", [40, 300, 680, 380], edt=True, h="First name", foc=True),
        # a clickable row whose label lives in a child TextView
        node(3, 0, 1, "LinearLayout", [40, 400, 680, 480], clk=True),
        node(4, 3, 1, "TextView", [60, 410, 300, 470], t="Save to Device"),
        node(5, 0, 1, "Button", [520, 80, 680, 160], clk=True, t="Save"),
        node(6, 0, 1, "TextView", [40, 2000, 400, 2100], t="off screen"),
        node(7, 0, 1, "Switch", [600, 500, 680, 560], chkb=True, chk=True, d="Sync"),
        # system decoration on the agent display: not part of the task
        node(8, -1, 2, "ImageButton", [0, 1500, 100, 1600], clk=True, d="Back"),
    ],
}


class CompressTest(unittest.TestCase):
    def setUp(self):
        self.obs = compress(SNAPSHOT)

    def test_package_from_the_focused_app_window(self):
        self.assertEqual(self.obs.package, "com.google.android.contacts")

    def test_elements(self):
        rendered = [e.render() for e in self.obs.elements]
        self.assertEqual(rendered, [
            '[1] text "Create contact" ·static',
            '[2] edit "First name" text="" (focused)',
            '[3] button "Save to Device"',  # label taken from the child; the child is not repeated
            '[4] button "Save"',
            '[5] switch "Sync" (checked)',
        ])

    def test_off_screen_and_system_windows_are_dropped(self):
        labels = {e.label for e in self.obs.elements}
        self.assertNotIn("off screen", labels)
        self.assertNotIn("Back", labels)

    def test_signature_changes_with_content(self):
        changed = dict(SNAPSHOT, nodes=[dict(n) for n in SNAPSHOT["nodes"]])
        changed["nodes"][2]["t"] = "王小明"
        self.assertNotEqual(compress(changed).signature, self.obs.signature)
        self.assertEqual(compress(SNAPSHOT).signature, self.obs.signature)


if __name__ == "__main__":
    unittest.main()
