"""登记网页采集入口：原始响应 -> 结构化档案。

公开能力：
- 流式接收分块字节，多字节字符跨块不被误判为坏页（``stream``）；
- 编码判定区分 BOM / 响应头 / 页面声明 / 来源配置 / 人工确认，冲突或无法
  无损解释的样本进入隔离区，禁止用替换字符吞错后入库（``evidence``、``pipeline``）；
- 按来源与页面区块解析字段，同区块同名标签语义冲突时隔离而不是取第一个值
  （``blocks``、``mapping``）；
- 日期仅按来源规则解析，无法确定则保留原值并标注，绝不猜测（``dates``）；
- 隔离样本补齐规则后只重放受影响样本，重放幂等，不重复发档案（``pipeline``、``store``）。
"""

from harvest.blocks import Fragment, PageSection, extract_fragments
from harvest.dates import DateParseError, parse_date
from harvest.evidence import (
    BOM_SIGNATURES,
    EncodingEvidence,
    EncodingDecision,
    QuarantineReason,
    detect_bom,
    decide_encoding,
    scan_meta_charset,
)
from harvest.mapping import apply_mapping, map_record
from harvest.pipeline import HarvestPipeline, PipelineReport, ReplayRejected
from harvest.records import ArchiveRecord, QuarantineRecord
from harvest.stream import ByteStream, decode_stream
from harvest.store import ArchiveStore, QuarantineStore

__all__ = [
    "Fragment",
    "PageSection",
    "extract_fragments",
    "DateParseError",
    "parse_date",
    "BOM_SIGNATURES",
    "EncodingEvidence",
    "EncodingDecision",
    "QuarantineReason",
    "detect_bom",
    "decide_encoding",
    "scan_meta_charset",
    "apply_mapping",
    "map_record",
    "HarvestPipeline",
    "PipelineReport",
    "ReplayRejected",
    "ArchiveRecord",
    "QuarantineRecord",
    "ByteStream",
    "decode_stream",
    "ArchiveStore",
    "QuarantineStore",
]
