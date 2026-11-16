"""区块解析、别名映射与同名标签冲突测试。"""

import unittest

from harvest.blocks import (
    DEFAULT_SECTION_ID,
    extract_fragments,
)
from harvest.mapping import apply_mapping, map_record
from harvest.stream import decode_with_origins


def _parse(html: str, rule: dict, encoding: str = "utf-8"):
    raw = html.encode(encoding)
    text, origins = decode_with_origins(raw, encoding)
    return extract_fragments(text, origins, len(raw), rule)


LABELED_RULE = {
    "block_mode": "labeled_text",
    "section_attr": "data-section",
    "label_tags": ["b"],
    "section_kinds": {"current": "current_registration",
                      "history": "historical_registration"},
    "sections": {
        "current": {"fields": {"founded": {"aliases": ["日期", "注册日期"]}}},
        "history": {"fields": {"founded": {"aliases": ["日期"]}}},
    },
}

DL_RULE = {
    "block_mode": "dl",
    "section_attr": "data-company",
    "sections": {
        "7": {
            "fields": {
                "founded": {"aliases": ["registered", "established"]},
                "updated": {"aliases": ["modified", "updated_at"]},
            }
        }
    },
}


class BlockExtractionTests(unittest.TestCase):
    def test_same_label_in_different_sections_stays_separate(self) -> None:
        html = (
            '<article data-section="current"><b>日期</b>01/02/2020</article>'
            '<article data-section="history"><b>日期</b>2019-12-31</article>'
        )
        fragments, sections = _parse(html, LABELED_RULE)
        section_ids = {fragment.section.section_id for fragment in fragments}
        self.assertEqual(section_ids, {"current", "history"})
        self.assertEqual(len(sections), 2)
        # 片段带着原始字节位置。
        current = next(f for f in fragments if f.section.section_id == "current")
        self.assertEqual(
            html.encode("utf-8")[current.byte_start:current.byte_end].decode(),
            "01/02/2020",
        )

    def test_mapping_never_takes_first_value_across_sections(self) -> None:
        html = (
            '<article data-section="current"><b>日期</b>01/02/2020</article>'
            '<article data-section="history"><b>日期</b>2019-12-31</article>'
        )
        fragments, sections = _parse(html, LABELED_RULE)
        records = apply_mapping(fragments, sections, LABELED_RULE)
        values = {
            record.section.section_id: record.fields["founded"].value
            for record in records
        }
        self.assertEqual(
            values,
            {"current": "01/02/2020", "history": "2019-12-31"},
        )

    def test_duplicate_same_label_in_one_block_is_conflict(self) -> None:
        html = (
            '<article data-section="current">'
            "<b>日期</b>2020-01-01"
            "<b>日期</b>2019-12-31"
            "</article>"
        )
        fragments, sections = _parse(html, LABELED_RULE)
        records = apply_mapping(fragments, sections, LABELED_RULE)
        self.assertEqual(len(records), 1)
        conflict = records[0].conflicts[0]
        self.assertEqual(conflict["kind"], "duplicate_label_in_block")
        self.assertEqual(conflict["duplicate_labels"], ["日期"])

    def test_distinct_aliases_with_distinct_values_is_conflict(self) -> None:
        html = (
            '<article data-section="current">'
            "<b>日期</b>2020-01-01"
            "<b>注册日期</b>2019-12-31"
            "</article>"
        )
        fragments, sections = _parse(html, LABELED_RULE)
        records = apply_mapping(fragments, sections, LABELED_RULE)
        conflict = records[0].conflicts[0]
        self.assertEqual(conflict["kind"], "alias_value_conflict")

    def test_dl_mode_extracts_dt_dd_pairs(self) -> None:
        html = (
            '<article data-company="7">'
            "<dl>"
            "<dt>registered</dt><dd>2020-01-01</dd>"
            "<dt>modified</dt><dd>2026-11-11</dd>"
            "<dt>unrecognised</dt><dd>retain-me</dd>"
            "</dl></article>"
        )
        fragments, sections = _parse(html, DL_RULE)
        records = apply_mapping(fragments, sections, DL_RULE)
        self.assertEqual(records[0].section.section_id, "7")
        self.assertEqual(records[0].fields["founded"].value, "2020-01-01")
        self.assertEqual(records[0].fields["updated"].value, "2026-11-11")
        # 未识别标签保留在 extras，不丢失。
        self.assertEqual(
            [fragment.label for fragment in records[0].extras],
            ["unrecognised"],
        )

    def test_unsectioned_content_uses_default_identity(self) -> None:
        fragments, _ = _parse("<b>标签</b>值", LABELED_RULE)
        self.assertEqual(fragments[0].section.section_id, DEFAULT_SECTION_ID)

    def test_legacy_map_record_behavior_unchanged(self) -> None:
        self.assertEqual(
            map_record({"registered": "2020"}, {"founded": ["registered"]}),
            {"founded": "2020"},
        )
        with self.assertRaises(ValueError):
            map_record({"a": "1", "b": "2"}, {"date": ["a", "b"]})
        # 同一目标的多个别名值一致时允许合并。
        self.assertEqual(
            map_record(
                {"registered": "2020", "established": "2020"},
                {"founded": ["registered", "established"]},
            ),
            {"founded": "2020"},
        )


if __name__ == "__main__":
    unittest.main()
