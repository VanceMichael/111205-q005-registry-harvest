"""Offline demonstration of the registry ingestion pipeline.

Runs only repository fixtures: raw bytes and chunk boundaries come from
``fixtures/responses.json``, field rules from ``fixtures/source_rules.json``.
Nothing is downloaded and no historical log is replayed.

The output shows, in order:

* decoding evidence and the chunk-level carry-over ledger for the GB18030
  page whose HTTP header claims UTF-8;
* block-separated field resolution for the page carrying both 本次登记 and
  历史登记 with the same 日期 label;
* quarantined samples (conflicting labels, ambiguous encodings, an invalid
  byte);
* a human-confirmed replay of one quarantined sample, and a repeat replay
  proving no duplicate archive record is produced.
"""

from __future__ import annotations

import json
from pathlib import Path

from harvester.pipeline import (
    Collector,
    rules_from_fixture,
    sample_from_fixture,
)
from service import map_record

ROOT = Path(__file__).parent
RESPONSES = ROOT / "fixtures" / "responses.json"
RULES = ROOT / "fixtures" / "source_rules.json"


def _samples() -> dict:
    payload = json.loads(RESPONSES.read_text(encoding="utf-8"))
    return {sample["source"]: sample_from_fixture(sample) for sample in payload["samples"]}


def run() -> dict:
    # The pre-existing field mapping interface is still exercised verbatim.
    fixture = json.loads((ROOT / "fixtures" / "rules.json").read_text(encoding="utf-8"))
    mapped = map_record(fixture["extracted"], fixture["draft_v1"])
    assert mapped == {"founded": "2020-01-01", "updated": "2026-11-11"}

    samples = _samples()
    collector = Collector(rules_from_fixture(RULES))
    first = collector.ingest(samples.values())

    demonstration: dict = {
        "legacy_mapping": {
            "mapped": mapped,
            "draft_v2": map_record(fixture["extracted"], fixture["draft_v2"]),
            "raw_extension_candidate": fixture["extracted"]["unrecognised"],
        },
        "decoding": [
            {
                "source": item["source"],
                "status": item["status"],
                "encoding": item["encoding"],
                "evidence": item["evidence"],
                "conflicts": item["conflicts"],
                "chunk_carry_over": item.get("chunk_carry_over", []),
                "first_section_span": (
                    item["sections"][0]["span"] if item.get("sections") else None
                ),
            }
            for item in first.decoded
        ],
        "archive": [record.as_dict() for record in first.records],
        "quarantine": [
            {
                "source": item.source,
                "reason": item.reason,
                "sample_key": item.sample_key,
                "raw_sha256": item.as_dict()["raw_sha256"],
                "detail": (
                    item.details.get("failures")
                    or item.details.get("ambiguity")
                    or [entry["duplicates"]
                        for entry in item.details.get("resolve", {}).values()
                        if entry.get("duplicates")]
                ),
            }
            for item in first.quarantine
        ],
        "replay": None,
    }

    # Human confirmation: in source d the *second* 成立日期 is the correct one.
    d_key = samples["d"].key()
    confirmation = {
        d_key: {
            "section_rules": [{
                "kind": "current",
                "fields": [{
                    "canonical": "founded",
                    "aliases": ["成立日期"],
                    "parser": "date",
                    "occurrence": 2,
                }],
            }],
        }
    }
    replayed = collector.replay(confirmation)
    repeated = collector.replay(confirmation)
    demonstration["replay"] = {
        "confirmed_sample": d_key,
        "archived": [record.as_dict() for record in replayed.records],
        "still_quarantined": [
            {"source": item.source, "reason": item.reason}
            for item in replayed.quarantine
        ],
        "untouched_quarantine": replayed.untouched,
        "repeat_archived": len(repeated.records),
        "repeat_skipped_duplicates": repeated.skipped_duplicates,
        "archive_size_after_replay": len(collector.archive),
    }
    return demonstration


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
