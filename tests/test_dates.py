"""来源规则驱动的日期解析测试：无规则不猜测。"""

import unittest

from harvest.dates import DateParseError, parse_date

RULE = {
    "timezone": "UTC+08:00",
    "date_fields": {
        "founded": {
            "formats": ["%d/%m/%Y", "%Y-%m-%d"],
            "timezone": "UTC+08:00",
            "required": True,
        },
        "updated": {
            "formats": ["%Y-%m-%d"],
            "timezone": "UTC+08:00",
            "required": False,
        },
    },
}


class DateRuleTests(unittest.TestCase):
    def test_day_month_year_order_comes_from_rule(self) -> None:
        # 01/02/2020 在规则 %d/%m/%Y 下是 2020-02-01，而不是美式 1 月 2 日。
        result = parse_date("01/02/2020", "founded", RULE)
        self.assertTrue(result.resolved)
        self.assertEqual(result.calendar_date, "2020-02-01")
        self.assertEqual(result.timezone, "UTC+08:00")
        self.assertIn("%d/%m/%Y", result.parse_basis)

    def test_iso_format_uses_second_declared_pattern(self) -> None:
        result = parse_date("2019-12-31", "founded", RULE)
        self.assertTrue(result.resolved)
        self.assertEqual(result.calendar_date, "2019-12-31")
        self.assertEqual(result.format_id, "founded.format_2")

    def test_undeclared_format_is_kept_raw_and_never_guessed(self) -> None:
        result = parse_date("2020年2月1日", "updated", RULE)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.calendar_date)
        self.assertEqual(result.raw, "2020年2月1日")
        self.assertIn("no source-declared format", result.note)

    def test_required_field_with_unknown_format_raises(self) -> None:
        with self.assertRaises(DateParseError):
            parse_date("2020年2月1日", "founded", RULE)

    def test_field_without_any_rule_never_guesses(self) -> None:
        result = parse_date("01/02/2020", "mystery_field", {})
        self.assertFalse(result.resolved)
        self.assertEqual(result.raw, "01/02/2020")
        self.assertIsNone(result.calendar_date)

    def test_empty_value_is_not_a_date_guess(self) -> None:
        result = parse_date("", "updated", RULE)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.calendar_date)


if __name__ == "__main__":
    unittest.main()
