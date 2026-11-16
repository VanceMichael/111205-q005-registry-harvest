"""编码证据与判定。

每条证据都记录 *依据类型* 与 *字节位置*，判定时区分：

``bom``         字节序标记，优先级最高且自带编码；
``http``        HTTP 响应头 ``Content-Type`` 声明的 charset；
``meta``        页面内 ``<meta charset>`` / ``http-equiv`` 声明；
``source``      来源配置里登记的默认编码；
``manual``      人工确认，仅用于隔离样本重放，覆盖一切自动结论。

规则要点：
- 自动通道之间出现互相冲突的声明，或所有候选都无法对 *完整字节* 做严格
  无损解码时，结论是 ``quarantine``，由调用方把样本放进隔离区；
- 任何路径都不允许使用 ``errors='replace'`` 把解码错误吞掉后入库；
- ``mojibake_preview`` 只作为隔离样本的人工诊断材料，标注清楚不可入库。
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceKind(str, Enum):
    BOM = "bom"
    HTTP = "http"
    META = "meta"
    SOURCE = "source"
    MANUAL = "manual"


class QuarantineReason(str, Enum):
    CONFLICTING_EVIDENCE = "conflicting_encoding_evidence"
    UNDECODABLE = "no_lossless_candidate"
    NO_EVIDENCE = "no_encoding_evidence"
    BLOCK_RULE_UNRESOLVED = "block_rule_unresolved"
    DUPLICATE_LABEL = "duplicate_label_in_block"
    DATE_UNRESOLVED = "date_format_unresolved"
    PARSE_FAILURE = "parse_failure"


# 归一化：别名 -> 标准 codecs 名称。GB18030 超集兼容 GBK/GB2312。
_ENCODING_ALIASES = {
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "gb18030": "gb18030",
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "gb_2312-80": "gb18030",
}

# BOM 签名：匹配后解码时需要剥掉的前缀长度与对应编码。
BOM_SIGNATURES = (
    (b"\xef\xbb\xbf", "utf-8-sig", 3),
    (b"\xff\xfe\x00\x00", "utf-32-le", 4),
    (b"\x00\x00\xfe\xff", "utf-32-be", 4),
    (b"\xff\xfe", "utf-16-le", 2),
    (b"\xfe\xff", "utf-16-be", 2),
)

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*?
      (?:
        charset\s*=\s*["']?\s*([A-Za-z0-9_\-:]+)
        |
        http-equiv\s*=\s*["']?content-type["']?[^>]*?
        charset\s*=\s*["']?\s*([A-Za-z0-9_\-:]+)
      )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class EncodingEvidence:
    """单条编码依据。``byte_offset`` 是其在原始字节中的位置（无位置时为 -1）。"""

    kind: EvidenceKind
    encoding: str
    byte_offset: int = -1
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "encoding": self.encoding,
            "byte_offset": self.byte_offset,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EncodingDecision:
    """一次编码判定的完整结论。

    ``accepted`` 为 False 时 ``encoding`` 为 None，样本必须隔离；
    ``evidence`` 与 ``candidate_results`` 原样保留，供人工审查与重放使用。
    """

    accepted: bool
    encoding: Optional[str]
    evidence: tuple[EncodingEvidence, ...]
    candidate_results: dict[str, str]
    quarantine_reason: Optional[QuarantineReason]
    bom_prefix_len: int = 0
    manual: bool = False

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "encoding": self.encoding,
            "evidence": [e.to_dict() for e in self.evidence],
            "candidate_results": dict(self.candidate_results),
            "quarantine_reason": (
                self.quarantine_reason.value if self.quarantine_reason else None
            ),
            "bom_prefix_len": self.bom_prefix_len,
            "manual": self.manual,
        }


def normalize_encoding(name: str) -> Optional[str]:
    """把声明里的写法归一化到 codecs 能识别的名称，无法识别返回 None。"""

    if not name:
        return None
    key = name.strip().lower().replace(" ", "")
    if key in _ENCODING_ALIASES:
        return _ENCODING_ALIASES[key]
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return codecs.lookup(name).name


def detect_bom(raw: bytes) -> Optional[tuple[str, int, int]]:
    """返回 (编码, BOM 长度, 起始偏移)；没有 BOM 返回 None。"""

    for signature, encoding, prefix_len in BOM_SIGNATURES:
        if raw.startswith(signature):
            return encoding, prefix_len, 0
    return None


def scan_meta_charset(raw: bytes) -> Optional[EncodingEvidence]:
    """只在原始字节上扫描 meta 声明，避免先解码再找声明的循环依赖。"""

    match = _META_CHARSET_RE.search(raw)
    if not match:
        return None
    declared = (match.group(1) or match.group(2)).decode("ascii", errors="strict")
    encoding = normalize_encoding(declared)
    if encoding is None:
        return None
    return EncodingEvidence(
        kind=EvidenceKind.META,
        encoding=encoding,
        byte_offset=match.start(),
        detail=f"meta declared {declared!r}",
    )


def strict_decodable(raw: bytes, encoding: str) -> tuple[bool, str]:
    """对完整字节做一次严格解码，返回 (是否无损, 失败原因)。"""

    try:
        raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        return False, f"{exc.reason} at byte {exc.start}"
    except LookupError:
        return False, "unknown codec"
    return True, "ok"


def diagnostic_preview(raw: bytes, encoding: str, limit: int = 120) -> str:
    """仅供隔离区人工诊断的替换解码预览，明确不允许作为入库内容。"""

    try:
        return raw[:limit].decode(encoding, errors="replace")
    except LookupError:
        return raw[:limit].decode("utf-8", errors="replace")


def _automatic_evidence(
    raw: bytes,
    http_charset: Optional[str],
    source_encoding: Optional[str],
) -> tuple[list[EncodingEvidence], int]:
    evidence: list[EncodingEvidence] = []
    bom = detect_bom(raw)
    bom_prefix_len = 0
    if bom:
        bom_encoding, bom_prefix_len, bom_offset = bom
        evidence.append(
            EncodingEvidence(
                kind=EvidenceKind.BOM,
                encoding=bom_encoding,
                byte_offset=bom_offset,
                detail="byte order mark",
            )
        )
    if http_charset:
        normalized = normalize_encoding(http_charset)
        if normalized:
            evidence.append(
                EncodingEvidence(
                    kind=EvidenceKind.HTTP,
                    encoding=normalized,
                    byte_offset=-1,
                    detail=f"Content-Type charset={http_charset!r}",
                )
            )
    meta = scan_meta_charset(raw)
    if meta:
        evidence.append(meta)
    if source_encoding:
        normalized = normalize_encoding(source_encoding)
        if normalized:
            evidence.append(
                EncodingEvidence(
                    kind=EvidenceKind.SOURCE,
                    encoding=normalized,
                    byte_offset=-1,
                    detail="source rule encoding",
                )
            )
    return evidence, bom_prefix_len


def decide_encoding(
    raw: bytes,
    http_charset: Optional[str] = None,
    source_encoding: Optional[str] = None,
    manual_encoding: Optional[str] = None,
) -> EncodingDecision:
    """按证据链给出编码判定。

    人工确认（仅在隔离重放时提供）直接成为结论，但仍然要对完整字节做一次
    严格解码验证——人工编码若无法无损解释样本，依旧不能入库。
    """

    evidence, bom_prefix_len = _automatic_evidence(raw, http_charset, source_encoding)

    if manual_encoding:
        manual_name = normalize_encoding(manual_encoding)
        manual_evidence = EncodingEvidence(
            kind=EvidenceKind.MANUAL,
            encoding=manual_name or manual_encoding,
            byte_offset=-1,
            detail="manual quarantine resolution",
        )
        all_evidence = tuple(evidence + [manual_evidence])
        if manual_name is None:
            return EncodingDecision(
                accepted=False,
                encoding=None,
                evidence=all_evidence,
                candidate_results={manual_encoding: "unknown codec"},
                quarantine_reason=QuarantineReason.UNDECODABLE,
                bom_prefix_len=bom_prefix_len,
                manual=True,
            )
        ok, note = strict_decodable(raw, manual_name)
        results = {manual_name: note}
        if not ok:
            return EncodingDecision(
                accepted=False,
                encoding=None,
                evidence=all_evidence,
                candidate_results=results,
                quarantine_reason=QuarantineReason.UNDECODABLE,
                bom_prefix_len=bom_prefix_len,
                manual=True,
            )
        return EncodingDecision(
            accepted=True,
            encoding=manual_name,
            evidence=all_evidence,
            candidate_results=results,
            quarantine_reason=None,
            bom_prefix_len=bom_prefix_len,
            manual=True,
        )

    if not evidence:
        # 没有任何声明：先尝试 UTF-8 严格解码，失败就隔离，绝不静默替换。
        ok, note = strict_decodable(raw, "utf-8")
        return EncodingDecision(
            accepted=ok,
            encoding="utf-8" if ok else None,
            evidence=(),
            candidate_results={"utf-8": note},
            quarantine_reason=(
                None if ok else QuarantineReason.NO_EVIDENCE
            ),
            bom_prefix_len=0,
        )

    # BOM 自带编码且不可被声明覆盖；其余自动证据之间要求结论一致。
    # utf-8-sig / utf-16 等带签名写法与同族基础编码是同一字符集，
    # 差别仅在是否剥 BOM，不应判成“相互冲突的编码”。
    bom_evidence = next((e for e in evidence if e.kind == EvidenceKind.BOM), None)

    def _family(name: str) -> str:
        return name.replace("-sig", "")

    if bom_evidence:
        declared = {_family(e.encoding) for e in evidence}
        conflicting = declared != {_family(bom_evidence.encoding)}
        candidates = sorted({_family(e.encoding) for e in evidence}
                            | {_family(bom_evidence.encoding)})
    else:
        declared = {e.encoding for e in evidence}
        conflicting = len(declared) > 1
        candidates = sorted(declared)

    candidate_results = {
        encoding: strict_decodable(raw, encoding)[1] for encoding in candidates
    }

    if conflicting:
        return EncodingDecision(
            accepted=False,
            encoding=None,
            evidence=tuple(evidence),
            candidate_results=candidate_results,
            quarantine_reason=QuarantineReason.CONFLICTING_EVIDENCE,
            bom_prefix_len=bom_prefix_len,
        )

    encoding = candidates[0]
    # 有 BOM 时保留带签名的编码名，交给流式层剥掉 BOM 前缀。
    if bom_evidence and _family(bom_evidence.encoding) == encoding:
        effective_encoding = bom_evidence.encoding
    else:
        effective_encoding = encoding
    ok, note = strict_decodable(raw, effective_encoding)
    candidate_results[effective_encoding] = note
    return EncodingDecision(
        accepted=ok,
        encoding=effective_encoding if ok else None,
        evidence=tuple(evidence),
        candidate_results=candidate_results,
        quarantine_reason=None if ok else QuarantineReason.UNDECODABLE,
        bom_prefix_len=bom_prefix_len,
    )
