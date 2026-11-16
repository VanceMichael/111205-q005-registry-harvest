"""结构化档案与隔离样本的数据结构。

设计约束：
- 每条输出都带来源标识、原始片段位置、区块身份和解析依据；
- 企业身份始终限定在来源内，不同来源即使编号相同也不合并；
- 隔离样本保留原始字节（Base64）与全部判定依据，供人工确认后重放。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from harvest.evidence import EncodingDecision, QuarantineReason, diagnostic_preview



def bytes_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ArchiveRecord:
    archive_id: str
    source_id: str
    sample_id: str
    local_company_id: str
    record_identity: str
    section: dict
    fields: list[dict]
    extra_fragments: list[dict]
    encoding_basis: dict
    raw_ref: dict
    created_at: str
    replay_generation: int

    def to_dict(self) -> dict:
        return {
            "archive_id": self.archive_id,
            "source_id": self.source_id,
            "sample_id": self.sample_id,
            "local_company_id": self.local_company_id,
            "record_identity": self.record_identity,
            "section": dict(self.section),
            "fields": list(self.fields),
            "extra_fragments": list(self.extra_fragments),
            "encoding_basis": dict(self.encoding_basis),
            "raw_ref": dict(self.raw_ref),
            "created_at": self.created_at,
            "replay_generation": self.replay_generation,
        }


@dataclass
class QuarantineRecord:
    quarantine_id: str
    sample_id: str
    source_id: str
    reason: QuarantineReason
    detail: str
    raw_body_base64: str
    chunk_bounds: list[int]
    http_charset: Optional[str]
    encoding_decision: dict
    diagnostic_preview: str
    conflict: Optional[dict]
    created_at: str
    resolved: bool = False
    resolution: Optional[dict] = None
    resolved_at: Optional[str] = None
    replay_generation: int = 0

    @property
    def raw_bytes(self) -> bytes:
        return base64.b64decode(self.raw_body_base64, validate=True)

    def to_dict(self) -> dict:
        return {
            "quarantine_id": self.quarantine_id,
            "sample_id": self.sample_id,
            "source_id": self.source_id,
            "reason": self.reason.value,
            "detail": self.detail,
            "raw_body_base64": self.raw_body_base64,
            "chunk_bounds": list(self.chunk_bounds),
            "http_charset": self.http_charset,
            "encoding_decision": dict(self.encoding_decision),
            "diagnostic_preview": self.diagnostic_preview,
            "diagnostic_preview_note": (
                "replacement-decoded preview for human triage only; "
                "never ingest this text"
            ),
            "conflict": self.conflict,
            "created_at": self.created_at,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at,
            "replay_generation": self.replay_generation,
        }


def make_quarantine(
    *,
    sample: dict,
    raw: bytes,
    reason: QuarantineReason,
    detail: str,
    decision: Optional[EncodingDecision],
    created_at: str,
    conflict: Optional[dict] = None,
) -> QuarantineRecord:
    """构建隔离记录，原始字节与编码依据完整保留。"""

    preview_encoding = "utf-8"
    if decision is not None and decision.evidence:
        preview_encoding = decision.evidence[0].encoding
    quarantine_id = hashlib.sha256(
        f"quarantine:{sample['id']}:{reason.value}".encode("utf-8")
    ).hexdigest()[:16]
    return QuarantineRecord(
        quarantine_id=quarantine_id,
        sample_id=sample["id"],
        source_id=sample["source"],
        reason=reason,
        detail=detail,
        raw_body_base64=base64.b64encode(raw).decode("ascii"),
        chunk_bounds=list(sample.get("chunks", [])),
        http_charset=sample.get("http_charset"),
        encoding_decision=decision.to_dict() if decision else {},
        diagnostic_preview=diagnostic_preview(raw, preview_encoding),
        conflict=conflict,
        created_at=created_at,
    )
