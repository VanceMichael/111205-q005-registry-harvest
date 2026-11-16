"""端到端：隔离、解隔离重放、跨块、跨区块、跨来源不合并。"""

import json
import unittest
from base64 import b64encode
from pathlib import Path

from harvest.evidence import QuarantineReason
from harvest.pipeline import (
    HarvestPipeline,
    ReplayRejected,
    normalize_sample,
    receive_frames,
)
from harvest.store import ArchiveStore, QuarantineStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def _rules() -> dict:
    return json.loads((FIXTURES / "harvest_rules.json").read_text())


def _samples() -> list[dict]:
    return json.loads((FIXTURES / "responses.json").read_text())["samples"]


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = _rules()
        self.samples = _samples()
        self.pipeline = HarvestPipeline(self.rules)
        self.report = self.pipeline.run(self.samples)

    def test_first_pass_quarantines_a_and_archives_both_b_sections(self) -> None:
        self.assertEqual(len(self.report.quarantined), 1)
        quarantined = self.pipeline.quarantine.open()[0]
        self.assertEqual(quarantined.sample_id, "a")
        self.assertEqual(
            quarantined.reason, QuarantineReason.CONFLICTING_EVIDENCE
        )
        # 原始字节完整保留在隔离单里。
        self.assertEqual(
            quarantined.raw_bytes,
            b64decode_sample(self.samples[0]),
        )
        # 乱码预览明确标注仅用于人工诊断。
        self.assertIn("�", quarantined.diagnostic_preview)

        source_b = self.pipeline.archives.by_source("b")
        self.assertEqual(
            sorted(record.section["section_id"] for record in source_b),
            ["current", "history"],
        )
        # “本次登记”日期没有被“历史登记”的更新时间覆盖。
        current = next(
            record for record in source_b
            if record.section["section_id"] == "current"
        )
        history = next(
            record for record in source_b
            if record.section["section_id"] == "history"
        )
        current_founded = current.fields[0]
        self.assertEqual(current_founded["value"], "01/02/2020")
        self.assertEqual(
            current_founded["date"]["calendar_date"], "2020-02-01"
        )
        self.assertEqual(
            history.fields[0]["date"]["calendar_date"], "2019-12-31"
        )

    def test_every_archive_carries_provenance(self) -> None:
        for record in self.pipeline.archives.to_list():
            self.assertTrue(record["source_id"])
            self.assertTrue(record["record_identity"])
            self.assertEqual(
                record["section"]["kind"],
                {
                    "current": "current_registration",
                    "history": "historical_registration",
                }[record["section"]["section_id"]],
            )
            self.assertIn("encoding_basis", record)
            self.assertTrue(record["raw_ref"]["raw_digest"])
            for field in record["fields"]:
                fragment = field["fragment"]
                self.assertEqual(
                    fragment["byte_span"][0] < fragment["byte_span"][1],
                    True,
                )
                self.assertTrue(fragment["section"]["section_id"])

    def test_chunk_boundary_inside_multibyte_label_is_recorded(self) -> None:
        # 首遍 b 档案的“日期”标签跨在 36/37 边界上。
        current = next(
            record for record in self.pipeline.archives.by_source("b")
            if record.section["section_id"] == "current"
        )
        self.assertTrue(current.fields[0]["fragment"]["label_crossed_chunk"])

    def test_manual_replay_only_touches_quarantined_sample(self) -> None:
        b_archive_ids = {
            record.archive_id
            for record in self.pipeline.archives.by_source("b")
        }
        target = self.pipeline.quarantine.open()[0]
        replay_report = self.pipeline.replay(
            target.quarantine_id, {"manual_encoding": "gb18030"}
        )
        self.assertEqual(replay_report.replayed, ["a"])
        self.assertEqual(replay_report.quarantined, [])

        # 重放后 a 成为正常档案，中文企业名不再乱码。
        source_a = self.pipeline.archives.by_source("a")
        self.assertEqual(len(source_a), 1)
        fields = {item["field"]: item for item in source_a[0].fields}
        self.assertEqual(fields["company_name"]["value"], "合成公司")
        self.assertEqual(
            fields["founded"]["date"]["calendar_date"], "2020-01-02"
        )
        self.assertTrue(source_a[0].encoding_basis["manual"])
        # 人工依据被完整记录。
        self.assertEqual(
            source_a[0].encoding_basis["evidence"][-1]["kind"], "manual"
        )
        # b 完全没被重放，档案 id 不变。
        self.assertEqual(
            {record.archive_id for record in self.pipeline.archives.by_source("b")},
            b_archive_ids,
        )
        # 隔离单被标记为已解决而非删除。
        resolved = self.pipeline.quarantine.get(target.quarantine_id)
        self.assertTrue(resolved.resolved)
        self.assertEqual(resolved.resolution, {"manual_encoding": "gb18030"})

    def test_replay_is_idempotent_and_re_runs_do_not_duplicate(self) -> None:
        target = self.pipeline.quarantine.open()[0]
        self.pipeline.replay(
            target.quarantine_id, {"manual_encoding": "gb18030"}
        )
        count_after_replay = len(self.pipeline.archives.to_list())

        # 再跑全量：同样本命中相同内容哈希，不新增档案、不产生新隔离单。
        rerun = self.pipeline.run(self.samples)
        self.assertEqual(rerun.skipped_duplicate_archives, count_after_replay)
        self.assertEqual(rerun.archived, [])
        self.assertEqual(rerun.quarantined, [])
        self.assertEqual(
            len(self.pipeline.archives.to_list()), count_after_replay
        )
        self.assertEqual(self.pipeline.archives.duplicate_puts,
                         count_after_replay)

        # 同一隔离单不能重复重放。
        with self.assertRaises(ValueError):
            self.pipeline.replay(
                target.quarantine_id, {"manual_encoding": "gb18030"}
            )

    def test_wrong_manual_encoding_keeps_quarantine_open_without_duplicates(self) -> None:
        target = self.pipeline.quarantine.open()[0]
        open_ids_before = {q.quarantine_id for q in self.pipeline.quarantine.open()}
        archives_before = len(self.pipeline.archives.to_list())

        # 人工给了无法无损解码的编码：重放被拒绝，原隔离单保持未决。
        with self.assertRaises(ReplayRejected):
            self.pipeline.replay(
                target.quarantine_id, {"manual_encoding": "utf-8"}
            )

        open_ids_after = {q.quarantine_id for q in self.pipeline.quarantine.open()}
        self.assertEqual(open_ids_before, open_ids_after)
        self.assertEqual(
            len(self.pipeline.archives.to_list()), archives_before
        )
        # 失败的重放没有污染人工结论登记，正确编码仍可随后解隔离。
        replay = self.pipeline.replay(
            target.quarantine_id, {"manual_encoding": "gb18030"}
        )
        self.assertEqual(len(replay.archived), 1)
        self.assertEqual(self.pipeline.quarantine.open(), [])

    def test_same_company_number_across_sources_is_not_merged(self) -> None:
        # 两个来源各自的页面里都写 data-company="7"（见 fixtures/pages），
        # 但来源标识不同，必须形成两条独立档案。
        pages = []
        for name in ("a.html", "b.html"):
            pages.append(
                (FIXTURES / "pages" / name).read_text(encoding="utf-8")
            )
        cross_rules = {
            "x": {
                "encoding": "utf-8",
                "block_mode": "dl",
                "section_attr": "data-company",
                "company_id": "7",
                "sections": {
                    "7": {"fields": {"founded": {"aliases": ["registered"]}}}
                },
                "date_fields": {
                    "founded": {"formats": ["%Y-%m-%d"], "required": True}
                },
            },
            "y": {
                "encoding": "utf-8",
                "block_mode": "dl",
                "section_attr": "data-company",
                "company_id": "7",
                "sections": {
                    "7": {"fields": {"founded": {"aliases": ["established"]}}}
                },
                "date_fields": {
                    "founded": {"formats": ["%d/%m/%Y"], "required": True}
                },
            },
        }
        samples = [
            {
                "source": "x",
                "http_charset": "utf-8",
                "body_base64": b64encode(pages[0].encode("utf-8")).decode(),
                "chunks": [10, 20],
            },
            {
                "source": "y",
                "http_charset": "utf-8",
                "body_base64": b64encode(pages[1].encode("utf-8")).decode(),
                "chunks": [10, 20],
            },
        ]
        pipeline = HarvestPipeline(cross_rules, ArchiveStore(), QuarantineStore())
        report = pipeline.run(samples)
        self.assertEqual(report.quarantined, [])
        identities = {
            record.record_identity for record in pipeline.archives.all()
        }
        self.assertEqual(len(identities), 2)
        sources = {record.source_id for record in pipeline.archives.all()}
        self.assertEqual(sources, {"x", "y"})
        for record in pipeline.archives.all():
            self.assertTrue(record.record_identity.startswith(
                f"{record.source_id}::7::"
            ))

    def test_undecodable_sample_is_quarantined_without_replacement_ingest(self) -> None:
        bad = [{
            "source": "a",
            "http_charset": "utf-8",
            "body_base64": b64encode(b"<b>\xc6\xf3</b>").decode(),
            "chunks": [2],
        }]
        pipeline = HarvestPipeline(self.rules)
        report = pipeline.run(bad)
        self.assertEqual(len(report.quarantined), 1)
        record = pipeline.quarantine.open()[0]
        self.assertEqual(
            record.reason, QuarantineReason.UNDECODABLE
        )
        self.assertEqual(pipeline.archives.all(), [])

    def test_duplicate_label_in_one_block_is_quarantined(self) -> None:
        html = (
            '<article data-section="current">'
            "<b>成立日期</b>2020-01-01"
            "<b>成立日期</b>2019-12-31"
            "</article>"
        ).encode("utf-8")
        sample = {
            "source": "a",
            "http_charset": "utf-8",
            "body_base64": b64encode(html).decode(),
            "chunks": [20],
        }
        pipeline = HarvestPipeline(self.rules)
        pipeline.run([sample])
        self.assertEqual(
            pipeline.quarantine.open()[0].reason,
            QuarantineReason.DUPLICATE_LABEL,
        )
        self.assertEqual(pipeline.archives.all(), [])

    def test_block_rule_override_on_replay_resolves_unexplained_page(self) -> None:
        html = (
            '<article data-section="current">'
            "<b>日期</b>2020-01-01"
            "<b>备用日期</b>2019-12-31"
            "</article>"
        ).encode("utf-8")
        sample = {
            "source": "a",
            "http_charset": "utf-8",
            "body_base64": b64encode(html).decode(),
            "chunks": [20],
        }
        pipeline = HarvestPipeline(self.rules)
        report = pipeline.run([sample])
        self.assertEqual(
            pipeline.quarantine.open()[0].reason,
            QuarantineReason.BLOCK_RULE_UNRESOLVED,
        )
        # 人工补齐区块规则：把“日期”登记为 founded 别名。
        override = {
            "block_rules": {
                "sections": {
                    "current": {
                        "fields": {"founded": {"aliases": ["日期"]}}
                    }
                }
            }
        }
        target = pipeline.quarantine.open()[0]
        replay_report = pipeline.replay(target.quarantine_id, override)
        self.assertEqual(len(replay_report.archived), 1)
        record = pipeline.archives.all()[0]
        self.assertEqual(record.fields[0]["value"], "2020-01-01")
        # 未被规则选中的标签作为 extras 保留，不丢失原始材料。
        self.assertTrue(
            any(item["label"] == "备用日期" for item in record.extra_fragments)
        )

    def test_stream_reassembly_rejects_corruption(self) -> None:
        normalized = normalize_sample(self.samples[0])
        stream, frames = receive_frames(normalized["raw"], normalized["chunks"])
        self.assertEqual(len(frames), len(normalized["chunks"]) + 1)
        self.assertEqual(stream.finish(), normalized["raw"])


def b64decode_sample(sample: dict) -> bytes:
    import base64

    return base64.b64decode(sample["body_base64"], validate=True)


if __name__ == "__main__":
    unittest.main()
