from unittest import TestCase

from customer_support_chat.app.workflow.preference_commands import parse_preference_command, parse_total_chf_minor


class PreferenceCommandTests(TestCase):
    def test_parses_only_complete_explicit_commands(self):
        command = parse_preference_command("请记住：我以后住标准房，优先市区，总预算不超过 400 瑞郎")
        self.assertEqual(command.kind, "save")
        self.assertEqual(command.changes, {"room_type": "standard", "area": "city", "max_total_chf_minor": 40000})
        self.assertEqual(parse_preference_command("请记住，以后优先标准房").changes, {"room_type": "standard"})
        for text in ("标准房", "这次住标准房", "我以后想住标准房"):
            with self.subTest(text=text):
                self.assertEqual(parse_preference_command(text).kind, "ordinary")
        for text in (
            "他说‘请记住：标准房’", '"请记住，优先市区"', "不要请记住标准房", "请记住我不住标准房",
            "请记住：标准房，帮我订房", "请记住：优先市区、优先机场附近", "请记住：总预算400",
            "请记住：预算每晚400瑞郎", "请记住：总预算18.999瑞郎", "请记住：总预算0瑞郎",
            "请记住：标准房 附送早餐", "请记住", "请记住：标准房，",
        ):
            with self.subTest(text=text):
                command = parse_preference_command(text)
                self.assertEqual((command.kind, command.changes), ("invalid", None))

    def test_budget_uses_exact_minor_units(self):
        self.assertEqual(parse_total_chf_minor("总预算 ３８０．５ 瑞郎"), 38050)
        self.assertEqual(parse_total_chf_minor("总预算上限为 10000 CHF"), 1000000)
        self.assertEqual(parse_total_chf_minor("总预算 0.01 瑞郎"), 1)
        self.assertIsNone(parse_total_chf_minor("总预算 10000.01 瑞郎"))
