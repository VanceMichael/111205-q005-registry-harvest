"""离线端到端演示：原始分块响应 -> 隔离/档案 -> 人工解隔离重放。

只读取仓库内 fixtures，不发起任何网络请求；所有状态留在内存，重复执行
结果完全一致。原有 ``map_record`` 演示与原始字节断言保持不变。
"""

import base64
import json
from pathlib import Path

from harvest.pipeline import HarvestPipeline
from harvest.stream import split_frames
from service import map_record

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _stream_evidence(samples: list[dict]) -> list[dict]:
    """展示分块边界确实切在多字节字符内部，且增量解码不在中途报错。"""

    evidence = []
    for sample in samples:
        raw = base64.b64decode(sample["body_base64"], validate=True)
        frames = split_frames(raw, sample["chunks"])
        evidence.append(
            {
                "source": sample["source"],
                "raw_bytes": len(raw),
                "frame_sizes": [len(frame) for frame in frames],
                "cumulative_bounds": list(sample["chunks"]),
                "reassembled_matches": b"".join(frames) == raw,
            }
        )
    return evidence


def _summarize_quarantine(store) -> list[dict]:
    summary = []
    for record in store.all():
        summary.append(
            {
                "quarantine_id": record.quarantine_id,
                "sample_id": record.sample_id,
                "source_id": record.source_id,
                "reason": record.reason.value,
                "detail": record.detail,
                "evidence": record.encoding_decision.get("evidence", []),
                "candidate_results": record.encoding_decision.get(
                    "candidate_results", {}
                ),
                "mojibake_preview_not_for_ingest": record.diagnostic_preview,
                "resolved": record.resolved,
                "resolution": record.resolution,
            }
        )
    return summary


def _summarize_archives(store) -> list[dict]:
    summary = []
    for record in store.to_list():
        summary.append(
            {
                "archive_id": record["archive_id"],
                "source_id": record["source_id"],
                "local_company_id": record["local_company_id"],
                "section": record["section"]["section_id"],
                "fields": [
                    {
                        "field": field["field"],
                        "value": field["value"],
                        "calendar_date": field.get("date", {}).get("calendar_date"),
                        "date_timezone": field.get("date", {}).get("timezone"),
                        "parse_basis": (
                            field.get("date", {}).get("parse_basis")
                            or field["parse_basis"]
                        ),
                        "value_byte_span": field["fragment"]["byte_span"],
                        "label_crossed_chunk": field["fragment"][
                            "label_crossed_chunk"
                        ],
                        "value_crossed_chunk": field["fragment"]["crossed_chunk"],
                        "aliases": field["aliases"],
                    }
                    for field in record["fields"]
                ],
                "encoding_manual": record["encoding_basis"]["manual"],
                "replay_generation": record["replay_generation"],
            }
        )
    return summary


def run() -> dict:
    # 1) 原有字段映射接口与原始样本断言保持不变。
    fixture = _load_json("rules.json")
    mapped = map_record(fixture["extracted"], fixture["draft_v1"])
    assert mapped == {"founded": "2020-01-01", "updated": "2026-11-11"}
    responses = _load_json("responses.json")
    samples = responses["samples"]
    raw_sizes = [
        len(base64.b64decode(sample["body_base64"], validate=True))
        for sample in samples
    ]
    assert all(size > 0 for size in raw_sizes)

    source_rules = _load_json("harvest_rules.json")

    # 2) 首遍采集：a 因 HTTP(UTF-8) 与 meta(GB18030) 冲突进隔离区；
    #    b 正常解码，但 current/history 两个区块各自成档案，互不覆盖。
    pipeline = HarvestPipeline(source_rules)
    first_report = pipeline.run(samples).to_dict()
    quarantine_after_first_pass = _summarize_quarantine(pipeline.quarantine)
    archives_after_first_pass = _summarize_archives(pipeline.archives)

    # 3) 人工确认 a 的编码为 GB18030，只重放 a；b 不参与重放。
    open_quarantine = pipeline.quarantine.open()
    assert len(open_quarantine) == 1
    target = open_quarantine[0]
    replay_report = pipeline.replay(
        target.quarantine_id, {"manual_encoding": "gb18030"}
    ).to_dict()

    # 4) 重放幂等性：再跑一轮全量，档案一份不多，隔离区不再有未决单据。
    rerun_report = pipeline.run(samples).to_dict()
    final_archives = _summarize_archives(pipeline.archives)
    final_quarantine = _summarize_quarantine(pipeline.quarantine)

    return {
        "mapped": mapped,
        "raw_extension_candidate": fixture["extracted"]["unrecognised"],
        "raw_sample_sizes": raw_sizes,
        "stream_evidence": _stream_evidence(samples),
        "first_pass": first_report,
        "quarantine_after_first_pass": quarantine_after_first_pass,
        "archives_after_first_pass": archives_after_first_pass,
        "manual_replay": replay_report,
        "rerun_idempotency": rerun_report,
        "final_archives": final_archives,
        "final_quarantine": final_quarantine,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
