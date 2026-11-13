"""End-to-end tests for the registry ingestion pipeline.

These tests use only the repository fixtures (``fixtures/responses.json`` and
``fixtures/source_rules.json``).  They cover:

* the GB18030 page that lies in its HTTP header, including characters split
  across chunk boundaries;
* the same-label / two-block page, where each block is resolved by its own
  rule and the block identity is part of every output;
* conflicting repeated labels and a document byte that cannot be explained
  losslessly, both of which must be quarantined instead of imported;
* per-source identity separation for the same company number;
* human-confirmed replay of one quarantined sample, idempotency of replay,
  and the fact that other sources keep their records.
"""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

from harvester.pipeline import (
    Collector,
    rules_from_fixture,
    sample_from_fixture,
)
from harvester.sections import extract
from harvester.streaming import (
    DecodeFailure,
    cumulative_chunks,
    decode_stream,
    decode_with_evidence,
    detect_bom,
    detect_page_declaration,
    normalise_label,
)

ROOT = Path(__file__).resolve().parents[1]
RESPONSES = ROOT / "fixtures" / "responses.json"
RULES = ROOT / "fixtures" / "source_rules.json"


def load_samples():
    payload = json.loads(RESPONSES.read_text(encoding="utf-8"))
    return {sample["source"]: sample_from_fixture(sample) for sample in payload["samples"]}


def collector():
    return Collector(rules_from_fixture(RULES))


FIXED_KEYS = {
    "a": "a:a-garbled-meta",
    "b": "b:b-two-blocks",
    "c": "c:c-same-number-other-source",
    "d": "d:d-conflicting-label",
    "e": "e:e-ambiguous",
    "f": "f:f-invalid-byte",
}


def sample_key(source: str) -> str:
    sample = load_samples()[source]
    return sample.key()


class DecodingTests(unittest.TestCase):
    def test_gb18030_page_is_read_despite_utf8_header(self):
        sample = load_samples()["a"]
        decisions = decode_with_evidence(
            raw=sample.raw, boundaries=sample.boundaries,
            http_charset=sample.http_charset,
            source_charset=sample.source_charset,
        )
        self.assertEqual(decisions.status, "decoded")
        self.assertEqual(decisions.decoded.encoding, "gb18030")
        self.assertEqual(decisions.decoded.evidence, ["page-declaration:gb18030"])
        self.assertIn("企业名称", decisions.decoded.text)
        self.assertNotIn("�", decisions.decoded.text)
        self.assertIn("conflict", decisions.conflicts[0])
        # The UTF-8 header candidate fails at a precise byte offset.
        self.assertEqual(decisions.failures[0]["encoding"], "utf-8")
        self.assertEqual(decisions.failures[0]["absolute_offset"], 59)

    def test_chunk_boundary_inside_multibyte_character(self):
        sample = load_samples()["a"]
        chunks = cumulative_chunks(sample.raw, sample.boundaries)
        # Fixture chunk 0 ends on the first byte of 企 (0xC6 in GB18030).
        self.assertEqual(chunks[0].data[-1:], b"\xc6")
        self.assertEqual(chunks[1].data, b"\xf3")
        decisions = decode_with_evidence(
            raw=sample.raw, boundaries=sample.boundaries, http_charset="utf-8"
        )
        ledger = decisions.decoded.chunks
        self.assertEqual(ledger[0]["carried_out"], 1)
        self.assertEqual(ledger[1]["carried_in"], 1)
        self.assertEqual(ledger[1]["carried_out"], 0)
        # 企 was recovered, not replaced or split.
        self.assertIn("企业名称", decisions.decoded.text)

    def test_transient_incompleteness_is_not_a_bad_page(self):
        text = '<article data-section="current"><b>企业名称</b>合成公司</article>'
        raw = text.encode("gb18030")
        chunks = cumulative_chunks(raw, [len(raw) - 2])  # ends inside 司
        decisions = decode_with_evidence(raw=raw, boundaries=[len(raw) - 2],
                                         http_charset="gb18030")
        self.assertEqual(decisions.status, "decoded")
        self.assertIn("合成公司", decisions.decoded.text)

    def test_stream_that_really_ends_mid_character_is_quarantined(self):
        # 司 (0xCB 0xBE in GB18030) is cut after its lead byte.
        raw = ('<article data-section="current"><b>企业名称</b>合成公司'.encode("gb18030")
               + b"\xcb")
        decisions = decode_with_evidence(raw=raw, boundaries=[], http_charset="gb18030")
        self.assertEqual(decisions.status, "failed")
        failure = decisions.failures[0]
        self.assertEqual(failure["reason"], "truncated-multibyte")
        self.assertEqual(failure["boundary"], "end-of-stream")
        self.assertEqual(failure["absolute_offset"], len(raw) - 1)

    def test_no_replacement_character_is_ever_produced(self):
        sample = load_samples()["f"]
        decisions = decode_with_evidence(raw=sample.raw, boundaries=sample.boundaries,
                                         http_charset=sample.http_charset)
        self.assertEqual(decisions.status, "failed")
        self.assertIsNone(decisions.decoded)
        self.assertEqual(decisions.failures[0]["reason"], "invalid-byte")

    def test_evidence_precedence_bom_then_header_then_declaration_then_source(self):
        body = '<article data-section="current"><b>成立日期</b>2020-01-02</article>'
        bom_raw = b"\xef\xbb\xbf" + body.encode("utf-8")
        decisions = decode_with_evidence(raw=bom_raw, boundaries=[], http_charset="gb18030",
                                         source_charset="big5")
        self.assertEqual(decisions.decoded.evidence, ["bom:utf-8"])
        self.assertTrue(any("BOM wins" in note for note in decisions.conflicts))
        # BOM offsets point into the original payload, after the BOM.
        self.assertEqual(decisions.decoded.byte_offsets[0], 3)

        declared = '<meta charset="gb18030">' + body
        decisions = decode_with_evidence(raw=declared.encode("gb18030"), boundaries=[],
                                         http_charset="utf-8")
        self.assertEqual(decisions.decoded.evidence, ["page-declaration:gb18030"])

        conflict = decode_with_evidence(raw=body.encode("gb18030"), boundaries=[],
                                        http_charset="utf-8", source_charset="gb18030")
        self.assertEqual(conflict.status, "decoded")
        self.assertEqual(conflict.decoded.encoding, "gb18030")
        self.assertEqual(conflict.decoded.evidence, ["source-config:gb18030"])
        self.assertTrue(any("header charset" in note for note in conflict.conflicts))

    def test_ambiguous_encodings_are_not_guessed(self):
        sample = load_samples()["e"]
        decisions = decode_with_evidence(
            raw=sample.raw, boundaries=sample.boundaries,
            http_charset=sample.http_charset,
            source_charset=sample.source_charset,
        )
        self.assertEqual(decisions.status, "ambiguous")
        self.assertIsNone(decisions.decoded)
        encodings = sorted(item["encoding"] for item in decisions.ambiguity)
        self.assertEqual(encodings, ["gb18030", "utf-8"])

    def test_chunk_boundaries_must_be_cumulative_and_in_range(self):
        raw = b"abcdef"
        for bad in ([0], [5, 5], [7], [3, 2], [True]):
            with self.assertRaises(ValueError):
                cumulative_chunks(raw, bad)
        chunks = cumulative_chunks(raw, [2, 4])
        self.assertEqual([(c.start, c.end) for c in chunks], [(0, 2), (2, 4), (4, 6)])
        self.assertEqual(len(cumulative_chunks(raw, [])), 1)

    def test_unsupported_single_byte_labels_are_not_evidence(self):
        self.assertIsNone(normalise_label("iso-8859-1"))
        self.assertIsNone(normalise_label("latin1"))
        self.assertIsNone(normalise_label("utf-16"))  # needs a BOM to be trusted


class SectionTests(unittest.TestCase):
    def test_same_label_in_two_blocks_is_not_taken_from_the_first(self):
        sample = load_samples()["b"]
        decisions = decode_with_evidence(raw=sample.raw, boundaries=sample.boundaries,
                                         http_charset=sample.http_charset)
        extraction = extract(decisions.decoded.text, decisions.decoded.byte_offsets)
        self.assertEqual([section.section_id for section in extraction.sections],
                         ["data-section=current#1", "data-section=history#2"])
        current, history = extraction.sections
        self.assertEqual((current.start, current.end), (0, 51))
        self.assertEqual(current.fields[0].value, "01/02/2020")
        self.assertEqual(history.fields[0].value, "2019-12-31")
        # Byte spans land on the original payload, even after chunk splits.
        span = history.fields[0].value_span
        self.assertEqual(sample.raw[span.byte_start:span.byte_end], b"2019-12-31")

    def test_label_outside_a_section_is_flagged_not_attributed(self):
        extraction = extract("<b>成立日期</b>2020-01-02<article data-section='current'></article>")
        self.assertTrue(any(issue["kind"] == "unsectioned-label" for issue in extraction.issues))


class EndToEndTests(unittest.TestCase):
    def test_archive_keeps_source_identity_and_provenance(self):
        samples = load_samples()
        result = collector().ingest(samples.values())
        self.assertEqual(len(result.records), 4)
        sources = {(record.source, record.source_key) for record in result.records}
        # Same company number 7 in two sources yields two distinct records.
        self.assertIn(("a", "company:7"), sources)
        self.assertIn(("c", "company:7"), sources)
        self.assertEqual(len(sources), 4)

        a_record = next(record for record in result.records if record.source == "a")
        payload = a_record.as_dict()
        self.assertEqual(payload["origin"]["encoding"], "gb18030")
        self.assertEqual(payload["block"]["section_id"], "data-section=current#1")
        self.assertEqual(payload["fields"]["company_name"][0]["value"], "合成公司")
        self.assertEqual(payload["fields"]["founded"][0]["value"], "2020-01-02")
        # Raw byte span of the garbage-free name maps back to the original bytes.
        span = payload["fields"]["company_name"][0]["value_span"]["byte"]
        self.assertEqual(samples["a"].raw[span[0]:span[1]], "合成公司".encode("gb18030"))

    def test_updated_time_does_not_overwrite_founded_date(self):
        result = collector().ingest(load_samples().values())
        c_record = next(record for record in result.records if record.source == "c")
        fields = c_record.as_dict()["fields"]
        self.assertEqual(fields["founded"][0]["raw_value"], "2020-01-02")
        self.assertEqual(fields["updated"][0]["raw_value"], "2026-11-11")

    def test_per_block_date_format_and_timezone(self):
        result = collector().ingest(load_samples().values())
        b_records = [record.as_dict() for record in result.records if record.source == "b"]
        current = next(record for record in b_records if record["block"]["kind"] == "current")
        history = next(record for record in b_records if record["block"]["kind"] == "history")
        self.assertEqual(current["fields"]["founded"][0]["raw_value"], "01/02/2020")
        self.assertEqual(current["fields"]["founded"][0]["value"], "2020-02-01")
        self.assertEqual(history["fields"]["founded"][0]["value"], "2019-12-31")
        self.assertIn("Asia/Shanghai", current["fields"]["founded"][0]["interpretation"])

    def test_quarantine_reasons_are_specific(self):
        result = collector().ingest(load_samples().values())
        reasons = {item.source: item.reason for item in result.quarantine}
        self.assertEqual(reasons, {"d": "conflicting-labels", "e": "ambiguous", "f": "failed"})
        for item in result.quarantine:
            payload = item.as_dict()
            self.assertIn("raw_base64", payload)
            self.assertEqual(base64.b64decode(payload["raw_base64"]), item.raw)

    def test_undecidable_date_is_quarantined_not_guessed(self):
        from harvester.pipeline import SourceSample
        body = '<article data-section="current"><b>成立日期</b>02/30/2020</article>'
        sample = SourceSample(source="x", raw=body.encode("utf-8"), boundaries=(len(body.encode()),),
                              http_charset="utf-8")
        rules = rules_from_fixture(RULES)
        rules["x"] = rules["a"]
        rules["x"].source = "x"
        result = Collector(rules).ingest([sample])
        self.assertEqual(result.records, [])
        self.assertEqual(result.quarantine[0].reason, "unresolved-date")

    def test_replay_only_confirmed_sample_and_is_idempotent(self):
        collector_instance = collector()
        samples = load_samples()
        first = collector_instance.ingest(samples.values())
        archive_before = dict(collector_instance.archive)
        self.assertEqual(len(archive_before), 4)

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
        replayed = collector_instance.replay(confirmation)
        self.assertEqual(replayed.replayed, [d_key])
        self.assertEqual(len(replayed.records), 1)
        self.assertEqual(replayed.records[0].source, "d")
        self.assertEqual(replayed.records[0].as_dict()["fields"]["founded"][0]["value"],
                         "2021-03-04")
        # Other sources were untouched, and the still-unresolved ones stay.
        self.assertEqual(len(collector_instance.archive), 5)
        self.assertEqual(replayed.untouched, sorted({samples["e"].key(), samples["f"].key()}))
        for key, record in archive_before.items():
            self.assertIs(collector_instance.archive[key], record)

        second = collector_instance.replay(confirmation)
        self.assertEqual(second.records, [])
        self.assertEqual(second.replayed, [])
        self.assertEqual(second.skipped_duplicates, [d_key])
        self.assertEqual(len(collector_instance.archive), 5)

    def test_manual_charset_unblocks_one_sample_only(self):
        collector_instance = collector()
        samples = load_samples()
        collector_instance.ingest(samples.values())
        e_key = samples["e"].key()
        replayed = collector_instance.replay({e_key: {"charset": "utf-8"}})
        self.assertEqual(replayed.replayed, [e_key])
        record = replayed.records[0].as_dict()
        self.assertEqual(record["origin"]["evidence"], ["manual-confirm:utf-8"])
        self.assertEqual(record["fields"]["company_name"][0]["value"], "隔离企业")
        # The untouched quarantine list still contains f and still-failing d.
        self.assertEqual(replayed.untouched, sorted({samples["d"].key(), samples["f"].key()}))
        # A second manual confirmation must not produce a duplicate archive.
        again = collector_instance.replay({e_key: {"charset": "utf-8"}})
        self.assertEqual(again.records, [])
        self.assertEqual(again.skipped_duplicates, [e_key])

    def test_replay_rejects_unknown_sample_keys(self):
        with self.assertRaises(KeyError):
            collector().replay({"a:missing:deadbeef": {"charset": "utf-8"}})
        with self.assertRaises(ValueError):
            collector().replay({})

    def test_legacy_interface_still_works(self):
        from service import map_record
        fixture = json.loads((ROOT / "fixtures" / "rules.json").read_text(encoding="utf-8"))
        self.assertEqual(map_record(fixture["extracted"], fixture["draft_v1"]),
                         {"founded": "2020-01-01", "updated": "2026-11-11"})
        self.assertEqual(map_record(fixture["extracted"], fixture["draft_v2"]),
                         {"founded": "2020-01-01", "updated": "2026-11-11"})


class FixtureIntegrityTests(unittest.TestCase):
    def test_responses_json_is_untouched_base64_of_readable_pages(self):
        payload = json.loads(RESPONSES.read_text(encoding="utf-8"))
        samples = {sample["source"]: sample for sample in payload["samples"]}
        for source in ("a", "b", "c", "d", "e", "f"):
            raw = base64.b64decode(samples[source]["body_base64"], validate=True)
            page_bytes = (ROOT / samples[source]["fixture_page"]).read_bytes()
            self.assertTrue(page_bytes.endswith(b"\n"))
            if source == "f":
                # f's readable page documents the one unexplainable byte.
                self.assertEqual(raw[:54], page_bytes.rstrip(b"\n")[:54])
                continue
            encoding = "gb18030" if source == "a" else "utf-8"
            self.assertEqual(raw, page_bytes.rstrip(b"\n"), source)
            self.assertEqual(raw.decode(encoding), page_bytes.decode(encoding).rstrip("\n"))

    def test_chunk_boundaries_are_recorded_for_split_characters(self):
        payload = json.loads(RESPONSES.read_text(encoding="utf-8"))
        for sample in payload["samples"]:
            self.assertEqual(sample["chunk_semantics"],
                             "cumulative byte boundaries; final remainder follows")


if __name__ == "__main__":
    unittest.main()
