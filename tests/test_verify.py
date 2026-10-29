"""Task parsing and the provider-based verifiers, against canned `content query` output (no device)."""

import unittest

from fenshen.verify import (_parse_next_alarm, ContactVerifier, SmsVerifier, AllOf, content_query, parse_contact, parse_sms,
                            parse_time, verifier_for, AlarmVerifier, ScreenEvidenceVerifier)


class ParseTimeTest(unittest.TestCase):
    def test_phrasings_from_the_benchmark(self):
        cases = {
            "帮我定一个早上7点15的闹钟": (7, 15),
            "帮我定一个晚上9点半的闹钟": (21, 30),
            "帮我定一个下午三点十五的闹钟": (15, 15),
            "帮我定一个早上6点05的闹钟": (6, 5),
            "帮我定一个中午12点的闹钟": (12, 0),
            "帮我定一个晚上11点45的闹钟": (23, 45),
            "明天早上八点叫我起床，帮我设个闹钟": (8, 0),
            "设一个下午4点20的闹钟": (16, 20),
            "set an alarm at 7:30 pm": (19, 30),
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                self.assertEqual(parse_time(task), expected)

    def test_no_time(self):
        self.assertIsNone(parse_time("帮我删除所有闹钟"))


class NextAlarmTest(unittest.TestCase):
    def test_empty_block_means_no_alarm(self):
        # The pending-alarm list below has other apps' timestamps; they are not the next alarm clock.
        dump = ("  Next alarm clock information: \n  \n  47 pending alarms: \n"
                "    RTC_WAKEUP #1: Alarm{1 type 0 origWhen 1793163600000 com.google.android.gms}\n"
                "      type=RTC_WAKEUP origWhen=2026-10-29 17:00:00.000 window=0\n")
        self.assertIsNone(_parse_next_alarm(dump))

    def test_block_with_time(self):
        dump = ("  Next alarm clock information: \n    user:0 pendingSend:false time:1793142900000 = 2026-10-29 07:15:00\n"
                "  \n  3 pending alarms: \n      type=RTC_WAKEUP origWhen=2026-10-29 17:00:00.000\n")
        self.assertEqual(_parse_next_alarm(dump).timestamp(), 1793142900)


class ParseContactSmsTest(unittest.TestCase):
    def test_contact(self):
        self.assertEqual(parse_contact("新建联系人王小明，电话13800001234"), ("王小明", "13800001234"))
        self.assertEqual(parse_contact("把王小明（13800001234）存进联系人"), ("王小明", "13800001234"))
        self.assertEqual(parse_contact("新建一个联系人 李雷 电话 138-0000-5678"), ("李雷", "13800005678"))
        self.assertIsNone(parse_contact("给王小明发短信：你好"))

    def test_sms(self):
        self.assertEqual(parse_sms("给王小明发短信：明天下午三点开会"), ("王小明", "明天下午三点开会"))
        self.assertEqual(parse_sms("给13800001234发一条短信说「我到了」"), ("13800001234", "我到了"))
        # "他" refers to the contact named earlier in the same task
        self.assertEqual(parse_sms("把王小明（13800001234）存进联系人，然后给他发短信：开会别迟到", "王小明"),
                         ("王小明", "开会别迟到"))

    def test_dispatch(self):
        self.assertIsInstance(verifier_for("帮我定一个晚上9点半的闹钟"), AlarmVerifier)
        self.assertIsInstance(verifier_for("新建联系人王小明，电话13800001234"), ContactVerifier)
        self.assertIsInstance(verifier_for("给王小明发短信：你好"), SmsVerifier)
        self.assertIsInstance(verifier_for("把王小明（13800001234）存进联系人，然后给他发短信：你好"), AllOf)
        self.assertIsInstance(verifier_for("打开设置看看"), ScreenEvidenceVerifier)


class FakeAdb:
    """Answers `content query --uri <uri> ...` from a dict of canned outputs."""

    def __init__(self, tables):
        self.tables = tables

    def shell(self, *args, **kwargs):
        uri = args[args.index("--uri") + 1]
        return self.tables.get(uri, "No result found.")


class FakeDevice:
    def __init__(self, tables):
        self.adb = FakeAdb(tables)

    def observe(self):
        return None


CONTACTS = "content://com.android.contacts/data"


def contact_rows(*rows):
    lines = []
    for i, (rid, name, kind, data) in enumerate(rows):
        lines.append(f"Row: {i} raw_contact_id={rid}, display_name={name}, mimetype=vnd.android.cursor.item/{kind}, data1={data}")
    return "\n".join(lines)


class ContentQueryTest(unittest.TestCase):
    def test_values_may_contain_commas(self):
        out = "Row: 0 _id=3, address=+8613800001234, type=2, body=明天下午三点开会, 别迟到\nRow: 1 _id=4, address=10086, type=1, body=hi"
        rows = content_query(FakeAdb({"u": out}), "u", ["_id", "address", "type", "body"])
        self.assertEqual(rows[0]["body"], "明天下午三点开会, 别迟到")
        self.assertEqual(rows[1], {"_id": "4", "address": "10086", "type": "1", "body": "hi"})


BAD_ADDRESS = '13900001111")\n`'  # what E9 actually stored: the model's trailing code fence leaked in


class MalformedValueTest(unittest.TestCase):
    def test_value_with_newline_stays_in_its_row(self):
        out = f"Row: 0 _id=3, address={BAD_ADDRESS}, type=2, body=周五晚上一起吃饭吗\nRow: 1 _id=4, address=10086, type=1, body=hi"
        rows = content_query(FakeAdb({"u": out}), "u", ["_id", "address", "type", "body"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "2")
        self.assertEqual(rows[0]["address"], BAD_ADDRESS)

    def test_garbage_around_digits_is_not_the_number(self):
        from fenshen.verify import _same_number
        self.assertTrue(_same_number("+86 139-0000-1111", "13900001111"))
        self.assertTrue(_same_number("1 (390) 000-1111", "13900001111"))
        self.assertFalse(_same_number(BAD_ADDRESS, "13900001111"))

    def test_sms_to_a_malformed_address_stops_the_task(self):
        tables = {CONTACTS: contact_rows(("3", "孙悦", "phone_v2", "13900001111")),
                  "content://sms": ""}
        dev = FakeDevice(tables)
        v = SmsVerifier("孙悦", "周五晚上一起吃饭吗")
        v.prepare(dev)
        tables["content://sms"] = f"Row: 0 _id=3, address={BAD_ADDRESS}, type=2, body=周五晚上一起吃饭吗"
        self.assertFalse(v.goal_reached(dev))
        self.assertIn("不对的短信", v.unintended_change(dev))


class ContactVerifierTest(unittest.TestCase):
    def test_exactly_one_new_contact(self):
        tables = {CONTACTS: ""}
        dev = FakeDevice(tables)
        v = ContactVerifier("王小明", "13800001234")
        v.prepare(dev)
        self.assertFalse(v.goal_reached(dev))
        tables[CONTACTS] = contact_rows(("7", "王小明", "name", "王小明"), ("7", "王小明", "phone_v2", "138 0000 1234"))
        self.assertTrue(v.goal_reached(dev))
        self.assertTrue(v.check(dev, None, "").ok)

    def test_wrong_number_is_not_the_contact(self):
        dev = FakeDevice({CONTACTS: contact_rows(("7", "王小明", "phone_v2", "13900009999"))})
        v = ContactVerifier("王小明", "13800001234")
        v.before = 0
        self.assertFalse(v.check(dev, None, "").ok)

    def test_duplicate_fails(self):
        dev = FakeDevice({CONTACTS: contact_rows(("7", "王小明", "phone_v2", "13800001234"),
                                                 ("8", "王小明", "phone_v2", "+86 138 0000 1234"))})
        v = ContactVerifier("王小明", "13800001234")
        v.before = 0
        verdict = v.check(dev, None, "")
        self.assertFalse(verdict.ok)
        self.assertIn("重复", verdict.evidence)


class SmsVerifierTest(unittest.TestCase):
    def setUp(self):
        self.tables = {
            CONTACTS: contact_rows(("7", "王小明", "phone_v2", "13800001234")),
            "content://sms": "Row: 0 _id=1, address=10086, type=1, body=旧短信",
        }
        self.dev = FakeDevice(self.tables)
        self.v = SmsVerifier("王小明", "明天下午三点开会，别迟到")
        self.v.prepare(self.dev)

    def test_sent_message_to_the_contact(self):
        self.assertFalse(self.v.goal_reached(self.dev))
        self.tables["content://sms"] += "\nRow: 1 _id=2, address=13800001234, type=2, body=明天下午三点开会, 别迟到"
        self.assertTrue(self.v.goal_reached(self.dev))
        self.assertIsNone(self.v.unintended_change(self.dev))
        self.assertTrue(self.v.check(self.dev, None, "").ok)

    def test_draft_is_not_sent(self):
        self.tables["content://sms"] += "\nRow: 1 _id=2, address=13800001234, type=3, body=明天下午三点开会，别迟到"
        self.assertFalse(self.v.goal_reached(self.dev))

    def test_message_to_someone_else_stops_the_task(self):
        self.tables["content://sms"] += "\nRow: 1 _id=2, address=13900009999, type=2, body=明天下午三点开会，别迟到"
        self.assertFalse(self.v.goal_reached(self.dev))
        self.assertIn("不对的短信", self.v.unintended_change(self.dev))


if __name__ == "__main__":
    unittest.main()
