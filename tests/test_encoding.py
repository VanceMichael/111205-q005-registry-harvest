"""编码证据链、冲突隔离与人工解隔离测试。"""

import unittest
from base64 import b64decode

from harvest.evidence import (
    EvidenceKind,
    QuarantineReason,
    decide_encoding,
    detect_bom,
    normalize_encoding,
    scan_meta_charset,
    strict_decodable,
)


class EncodingEvidenceTests(unittest.TestCase):
    GB_BYTES = "<meta charset=\"gb18030\"><b>企业</b>".encode("gb18030")
    UTF8_BYTES = "<meta charset=\"utf-8\"><b>日期</b>".encode("utf-8")

    def test_normalize_aliases(self) -> None:
        self.assertEqual(normalize_encoding("GBK"), "gb18030")
        self.assertEqual(normalize_encoding("gb2312"), "gb18030")
        self.assertEqual(normalize_encoding("UTF8"), "utf-8")
        self.assertIsNone(normalize_encoding("x-not-a-codec"))

    def test_meta_scan_runs_on_raw_bytes_with_position(self) -> None:
        evidence = scan_meta_charset(self.GB_BYTES)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.kind, EvidenceKind.META)
        self.assertEqual(evidence.encoding, "gb18030")
        self.assertEqual(evidence.byte_offset, 0)

    def test_bom_detection(self) -> None:
        self.assertEqual(detect_bom(b"\xef\xbb\xbfx"), ("utf-8-sig", 3, 0))
        self.assertIsNone(detect_bom(b"plain"))

    def test_http_vs_meta_conflict_is_quarantined(self) -> None:
        decision = decide_encoding(self.GB_BYTES, http_charset="utf-8")
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.quarantine_reason,
            QuarantineReason.CONFLICTING_EVIDENCE,
        )
        kinds = [item.kind for item in decision.evidence]
        self.assertEqual(kinds, [EvidenceKind.HTTP, EvidenceKind.META])
        # 两个候选都做了完整字节严格验证，gb18030 可无损而 utf-8 失败。
        self.assertEqual(decision.candidate_results["gb18030"], "ok")
        self.assertNotEqual(decision.candidate_results["utf-8"], "ok")

    def test_consistent_evidence_accepts_without_guessing(self) -> None:
        decision = decide_encoding(
            self.UTF8_BYTES, http_charset="utf-8", source_encoding="utf-8"
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.encoding, "utf-8")
        self.assertIsNone(decision.quarantine_reason)

    def test_undecodable_even_with_consistent_declaration_is_quarantined(self) -> None:
        # 声明一致为 utf-8，但字节本身不是合法 utf-8。
        raw = b"<b>\xc6\xf3</b>"
        decision = decide_encoding(raw, http_charset="utf-8",
                                   source_encoding="utf-8")
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.quarantine_reason, QuarantineReason.UNDECODABLE
        )

    def test_no_evidence_falls_back_to_strict_utf8_then_quarantine(self) -> None:
        ok = decide_encoding(b"plain ascii")
        self.assertTrue(ok.accepted)
        bad = decide_encoding(b"\xc6\xf3\xd2\xb5")
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.quarantine_reason, QuarantineReason.NO_EVIDENCE)

    def test_manual_encoding_overrides_and_is_validated(self) -> None:
        decision = decide_encoding(
            self.GB_BYTES,
            http_charset="utf-8",  # 自动通道本应冲突
            manual_encoding="gb18030",
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.encoding, "gb18030")
        self.assertTrue(decision.manual)
        self.assertIn(EvidenceKind.MANUAL,
                      [item.kind for item in decision.evidence])

        # 人工编码同样必须无损解释，错误的人工确认不能强行入库。
        wrong = decide_encoding(self.GB_BYTES, manual_encoding="utf-8")
        self.assertFalse(wrong.accepted)
        self.assertEqual(
            wrong.quarantine_reason, QuarantineReason.UNDECODABLE
        )

    def test_bom_beats_conflicting_declarations(self) -> None:
        raw = b"\xef\xbb\xbf" + "<b>ok</b>".encode("utf-8")
        decision = decide_encoding(raw, http_charset="gb18030",
                                   source_encoding="gb18030")
        # BOM 与声明冲突：仍然隔离，而不是擅自挑一个。
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.quarantine_reason,
            QuarantineReason.CONFLICTING_EVIDENCE,
        )
        self.assertEqual(decision.bom_prefix_len, 3)

    def test_utf8_sig_bom_is_not_conflict_with_utf8_declarations(self) -> None:
        # 同一字符集的带签名/不带签名写法不是编码冲突，BOM 前缀长度保留。
        raw = b"\xef\xbb\xbf" + "<b>ok</b>".encode("utf-8")
        decision = decide_encoding(raw, http_charset="utf-8",
                                   source_encoding="utf-8")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.encoding, "utf-8-sig")
        self.assertEqual(decision.bom_prefix_len, 3)

    def test_bom_pipeline_byte_spans_are_rebased_to_raw_response(self) -> None:
        import base64

        from harvest.pipeline import HarvestPipeline

        html = (
            '<article data-section="current"><b>日期</b>2020-01-05</article>'
        ).encode("utf-8")
        raw = b"\xef\xbb\xbf" + html
        rule = {
            "encoding": "utf-8",
            "block_mode": "labeled_text",
            "section_attr": "data-section",
            "label_tags": ["b"],
            "company_id": "bom-co",
            "sections": {
                "current": {"fields": {"founded": {"aliases": ["日期"]}}}
            },
            "date_fields": {
                "founded": {"formats": ["%Y-%m-%d"], "required": True}
            },
        }
        pipeline = HarvestPipeline({"z": rule})
        report = pipeline.run(
            [
                {
                    "source": "z",
                    "http_charset": "utf-8",
                    "body_base64": base64.b64encode(raw).decode(),
                    "chunks": [4, 5],
                }
            ]
        )
        self.assertEqual(report.quarantined, [])
        record = pipeline.archives.all()[0]
        start, end = record.fields[0]["fragment"]["byte_span"]
        self.assertEqual(
            raw[start:end].decode("utf-8"),
            record.fields[0]["value"],
        )

    def test_strict_decode_never_swaps_in_replacement(self) -> None:
        ok, note = strict_decodable(b"\xc6", "utf-8")
        self.assertFalse(ok)
        self.assertIn("end of data", note)


class FixtureEncodingTests(unittest.TestCase):
    def test_fixture_a_is_quarantined_and_b_is_accepted(self) -> None:
        import json
        from pathlib import Path

        samples = json.loads(
            (Path(__file__).resolve().parents[1]
             / "fixtures" / "responses.json").read_text()
        )["samples"]
        by_source = {
            sample["source"]: b64decode(sample["body_base64"], validate=True)
            for sample in samples
        }
        a = decide_encoding(by_source["a"], http_charset="utf-8")
        self.assertFalse(a.accepted)
        self.assertEqual(
            a.quarantine_reason,
            QuarantineReason.CONFLICTING_EVIDENCE,
        )
        b = decide_encoding(
            by_source["b"], http_charset="utf-8", source_encoding="utf-8"
        )
        self.assertTrue(b.accepted)
        self.assertEqual(b.encoding, "utf-8")


if __name__ == "__main__":
    unittest.main()
