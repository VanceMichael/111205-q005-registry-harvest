"""采集编排：原始响应分块 -> 编码判定 -> 区块解析 -> 结构化档案/隔离区。

编排器本身不做持久化决策，档案库与隔离区由调用方注入；但完整实现了：

1. 分块流式接收（多字节字符截在分块边界时，未结束前不判坏页）；
2. 编码证据链判定，冲突/无法无损解释即隔离，保留原字节与诊断预览；
3. 区块感知的字段映射，同区块同名标签冲突隔离，不取第一个值；
4. 日期只按来源规则解析，无法确定则隔离，不猜测；
5. 人工解隔离后 *只重放受影响样本*，其他来源不动；
6. 档案按内容哈希幂等，重复重放不会多发一份档案。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from harvest.blocks import extract_fragments
from harvest.dates import DateParseError, parse_date
from harvest.evidence import (
    EncodingDecision,
    QuarantineReason,
    decide_encoding,
)
from harvest.mapping import SectionRecord, apply_mapping
from harvest.records import (
    ArchiveRecord,
    QuarantineRecord,
    bytes_digest,
    make_quarantine,
)
from harvest.stream import ByteStream, decode_stream, decode_with_origins, split_frames
from harvest.store import ArchiveStore, QuarantineStore

Clock = Callable[[], str]


def _fixed_clock() -> str:
    return "2026-11-16T00:00:00Z"


def normalize_sample(sample: dict) -> dict:
    """把 fixtures 里的样本描述规范化为内部样本结构。"""

    raw = base64.b64decode(sample["body_base64"], validate=True)
    return {
        "id": sample.get("id", sample["source"]),
        "source": sample["source"],
        "http_charset": sample.get("http_charset"),
        "chunks": list(sample.get("chunks", [])),
        "raw": raw,
        "raw_digest": bytes_digest(raw),
    }


def receive_frames(raw: bytes, chunk_bounds: list[int]) -> tuple[ByteStream, list[bytes]]:
    """模拟流式接收：逐帧 append，返回接收器与帧序列。"""

    frames = split_frames(raw, chunk_bounds)
    stream = ByteStream()
    for frame in frames:
        stream.append(frame)
    if stream.finish() != raw:
        raise AssertionError("reassembled stream differs from original bytes")
    return stream, frames


@dataclass
class PipelineReport:
    processed: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    skipped_duplicate_archives: int = 0
    replayed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "processed": list(self.processed),
            "archived": list(self.archived),
            "quarantined": list(self.quarantined),
            "skipped_duplicate_archives": self.skipped_duplicate_archives,
            "replayed": list(self.replayed),
            "notes": list(self.notes),
        }


class ReplayRejected(RuntimeError):
    """人工解隔离重放仍无法产出档案，原隔离单保持未决。"""


class HarvestPipeline:
    def __init__(
        self,
        source_rules: dict,
        archives: Optional[ArchiveStore] = None,
        quarantine: Optional[QuarantineStore] = None,
        clock: Clock = _fixed_clock,
    ) -> None:
        self.source_rules = dict(source_rules)
        self.archives = archives or ArchiveStore()
        self.quarantine = quarantine or QuarantineStore()
        self._clock = clock
        self._generations: dict[str, int] = {}
        # 样本级人工解隔离结论：只由 replay 登记，之后同进程重跑同样本时
        # 自动沿用，避免已解隔离样本再次被关进隔离区。
        self._manual_resolutions: dict[str, dict] = {}

    # -- 入口 ---------------------------------------------------------------

    def run(self, samples: list[dict]) -> PipelineReport:
        report = PipelineReport()
        for fixture_sample in samples:
            sample = normalize_sample(fixture_sample)
            manual = self._manual_resolutions.get(sample["id"])
            self.process_sample(
                sample,
                report,
                generation=self._generations.get(sample["id"], 0),
                manual_encoding=(manual or {}).get("manual_encoding"),
                block_overrides=(manual or {}).get("block_rules"),
            )
        return report

    def replay(
        self,
        quarantine_id: str,
        resolution: dict,
    ) -> PipelineReport:
        """人工解隔离：只重放该隔离单对应的样本，其他来源不受影响。

        人工结论若仍无法让样本无损入库，抛出 ``ReplayRejected``，隔离单
        与已登记结论保持原样，不产生半截状态或重复单据。
        """

        record = self.quarantine.get(quarantine_id)
        if record is None:
            raise KeyError(f"unknown quarantine id: {quarantine_id}")
        if record.resolved:
            raise ValueError(f"quarantine {quarantine_id} already resolved")

        sample = {
            "id": record.sample_id,
            "source": record.source_id,
            "http_charset": record.http_charset,
            "chunks": list(record.chunk_bounds),
            "raw": record.raw_bytes,
            "raw_digest": bytes_digest(record.raw_bytes),
        }
        generation = record.replay_generation + 1
        report = PipelineReport()
        report.replayed.append(sample["id"])
        self.process_sample(
            sample,
            report,
            generation=generation,
            manual_encoding=resolution.get("manual_encoding"),
            block_overrides=resolution.get("block_rules"),
            tentative=True,
        )
        if not report.archived:
            raise ReplayRejected(
                f"replay of {quarantine_id} produced no archive; "
                f"quarantine stays open: {report.quarantined}"
            )
        # 成功后才登记人工结论与世代，并把隔离单标记为已解决。
        self._generations[sample["id"]] = generation
        self._manual_resolutions[sample["id"]] = dict(resolution)
        self.quarantine.resolve(quarantine_id, resolution, self._clock())
        return report

    # -- 单样本处理 ----------------------------------------------------------

    def process_sample(
        self,
        sample: dict,
        report: PipelineReport,
        generation: int,
        manual_encoding: Optional[str] = None,
        block_overrides: Optional[dict] = None,
        tentative: bool = False,
    ) -> None:
        report.processed.append(sample["id"])
        source_id = sample["source"]
        rule = dict(self.source_rules.get(source_id, {}))
        if block_overrides:
            merged = dict(rule)
            # sections 是结构化区块规则，人工补齐时整体替换对应键，避免旧
            # 别名与新别名并存再次触发冲突；其余规则保持来源配置不变。
            for key, value in block_overrides.items():
                if key == "sections":
                    # 人工对某个区块补齐的规则整体替换该区块，避免旧别名与
                    # 新别名并存再次触发冲突；其他区块保持来源配置不变。
                    merged_sections = dict(rule.get("sections", {}))
                    merged_sections.update(value)
                    merged["sections"] = merged_sections
                else:
                    merged[key] = value
            rule = merged

        # 1) 流式接收 + 增量解码（未结束前容忍跨块多字节序列）。
        stream, frames = receive_frames(sample["raw"], sample["chunks"])
        decision = decide_encoding(
            sample["raw"],
            http_charset=sample["http_charset"],
            source_encoding=rule.get("encoding"),
            manual_encoding=manual_encoding,
        )
        if not decision.accepted:
            self._quarantine(
                sample, decision, decision.quarantine_reason,
                f"encoding decision rejected: {decision.candidate_results}",
                report, generation, tentative=tentative,
            )
            return

        # 用逐帧增量解码器证明分块边界不影响结果；再用带原始字节位置的
        # 严格解码得到字符->字节映射。有 BOM 时把映射整体平移 BOM 长度，
        # 使字节位置始终指向未剥 BOM 的原始响应。
        streaming_text = decode_stream(frames, decision.encoding)
        body = sample["raw"][decision.bom_prefix_len:]
        text, origins = decode_with_origins(body, decision.encoding)
        if decision.bom_prefix_len:
            origins = [
                offset + decision.bom_prefix_len for offset in origins
            ]
        if streaming_text != text:
            self._quarantine(
                sample, decision, QuarantineReason.PARSE_FAILURE,
                "streaming decode disagrees with origin decode",
                report, generation, tentative=tentative,
            )
            return

        # 2) 区块解析。origins 已按原始响应（含 BOM）重新定基。
        try:
            fragments, sections = extract_fragments(
                text, origins, len(sample["raw"]),
                rule, stream=stream,
            )
        except Exception as exc:  # 解析器异常同样隔离，不让坏页流入档案。
            self._quarantine(
                sample, decision, QuarantineReason.PARSE_FAILURE,
                f"block parse failure: {exc!r}", report, generation,
                tentative=tentative,
            )
            return

        # 3) 区块感知映射，同区块同名标签冲突即隔离。
        section_records = apply_mapping(fragments, sections, rule)
        blocking_conflict = self._first_blocking_conflict(section_records)
        if blocking_conflict is not None:
            self._quarantine(
                sample, decision, QuarantineReason.DUPLICATE_LABEL,
                "same label with distinct semantics inside one block",
                report, generation, conflict=blocking_conflict,
                tentative=tentative,
            )
            return

        # 页面抽出了片段，却没有任何一个能被来源区块规则解释：规则与页面
        # 结构不匹配，隔离等待人工补齐区块规则，而不是静默产空档案。
        if fragments and not any(
            record.fields for record in section_records
        ):
            self._quarantine(
                sample, decision, QuarantineReason.BLOCK_RULE_UNRESOLVED,
                "no fragment matches any source block field rule; "
                f"labels seen: {sorted({frag.label for frag in fragments})}",
                report, generation, tentative=tentative,
            )
            return

        # 4) 日期严格按来源规则解析，无法确定即隔离，不猜测。
        try:
            archive_records = self._build_archives(
                sample, rule, section_records, decision, generation,
            )
        except DateParseError as exc:
            self._quarantine(
                sample, decision, QuarantineReason.DATE_UNRESOLVED,
                str(exc), report, generation, tentative=tentative,
            )
            return

        # 5) 幂等入库。
        for archive in archive_records:
            if self.archives.put(archive):
                report.archived.append(archive.archive_id)
            else:
                report.skipped_duplicate_archives += 1

    # -- 内部辅助 ------------------------------------------------------------

    @staticmethod
    def _first_blocking_conflict(
        section_records: list[SectionRecord],
    ) -> Optional[dict]:
        for record in section_records:
            if record.conflicts:
                return {
                    "section": record.section.to_dict(),
                    "conflicts": record.conflicts,
                }
        return None

    def _build_archives(
        self,
        sample: dict,
        rule: dict,
        section_records: list[SectionRecord],
        decision: EncodingDecision,
        generation: int,
    ) -> list[ArchiveRecord]:
        date_field_types = {"date"}
        built: list[ArchiveRecord] = []
        local_company = rule.get("company_id") or f"local:{sample['id']}"

        for section_record in section_records:
            # 区块里没有任何可识别字段时不产档案（未识别标签留在 extras
            # 并随其他字段一起保留；整段为空则跳过）。
            if not section_record.fields:
                continue
            field_payloads: list[dict] = []
            for name, mapped in sorted(section_record.fields.items()):
                section_rule = rule.get("sections", {}).get(
                    section_record.section.section_id, {}
                )
                spec = section_rule.get("fields", {}).get(name, {})
                payload: dict = {
                    "field": name,
                    "value": mapped.value,
                    "aliases": list(mapped.aliases),
                    "fragment": mapped.fragment.to_dict(),
                    "parse_basis": "source block field rule and alias list",
                }
                if spec.get("type") in date_field_types:
                    resolution = parse_date(mapped.value, name, rule)
                    payload["date"] = resolution.to_dict()
                field_payloads.append(payload)

            identity_material = "|".join(
                f"{item['field']}={item['value']}" for item in field_payloads
            )
            record_identity = (
                f"{sample['source']}::{local_company}::"
                f"{section_record.section.section_id}::{identity_material}"
            )
            archive_id = hashlib.sha256(
                record_identity.encode("utf-8")
            ).hexdigest()[:20]
            built.append(
                ArchiveRecord(
                    archive_id=archive_id,
                    source_id=sample["source"],
                    sample_id=sample["id"],
                    local_company_id=local_company,
                    record_identity=record_identity,
                    section=section_record.section.to_dict(),
                    fields=field_payloads,
                    extra_fragments=[
                        fragment.to_dict() for fragment in section_record.extras
                    ],
                    encoding_basis=decision.to_dict(),
                    raw_ref={
                        "raw_digest": sample["raw_digest"],
                        "sample_id": sample["id"],
                        "frame_bounds": list(sample["chunks"]),
                    },
                    created_at=self._clock(),
                    replay_generation=generation,
                )
            )
        return built

    def _quarantine(
        self,
        sample: dict,
        decision: Optional[EncodingDecision],
        reason: QuarantineReason,
        detail: str,
        report: PipelineReport,
        generation: int,
        conflict: Optional[dict] = None,
        tentative: bool = False,
    ) -> None:
        fixture_sample = {
            "id": sample["id"],
            "source": sample["source"],
            "http_charset": sample["http_charset"],
            "chunks": sample["chunks"],
            "body_base64": base64.b64encode(sample["raw"]).decode("ascii"),
        }
        record = make_quarantine(
            sample=fixture_sample,
            raw=sample["raw"],
            reason=reason,
            detail=detail,
            decision=decision,
            created_at=self._clock(),
            conflict=conflict,
        )
        record.replay_generation = generation
        if tentative:
            # 重放尝试失败：只在报告里挂“待定”单据，不写入隔离区，
            # 原隔离单保持未决，等待下一次人工确认。
            report.quarantined.append(f"tentative:{record.quarantine_id}")
            report.notes.append(f"{reason.value}: {detail}")
            return
        if self.quarantine.put(record):
            report.quarantined.append(record.quarantine_id)
