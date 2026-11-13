"""End-to-end collection: raw response -> structured, provenance-bearing archive.

The flow is split so each concern can be replayed or quarantined independently:

1. decode raw bytes (streaming, strict, evidence-ledgered);
2. extract labelled blocks, keeping their declared section identity;
3. resolve fields per ``(source, section)`` rule, with per-field provenance;
4. file the result as an archive record keyed by ``(source, source-local id)``;
5. quarantine anything that cannot be resolved losslessly;
6. replay only the quarantined samples after a human confirmation.

No replacement character is ever emitted, and no field is attributed to an
ambiguous block.  Within one source and one block, repeated aliases for a
canonical field are merged with the pre-existing :func:`service.map_record`,
so the legacy conflict-on-different-values behaviour is preserved.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .rules import (
    SectionResult,
    SourceRule,
    resolve_section,
    section_rule_from,
    source_rule_from,
)
from .sections import Section, extract
from .streaming import decode_with_evidence, normalise_label


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def raw_of(sample: dict) -> bytes:
    return base64.b64decode(sample["body_base64"], validate=True)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class ArchiveRecord:
    source: str
    source_key: str
    section: SectionResult
    raw_digest: str
    decode_evidence: list[str]
    encoding: str
    page_sequence: int
    raw: bytes = b""
    sample_key: str = ""

    def as_dict(self) -> dict:
        fields_by_name: dict[str, list[dict]] = {}
        for item in self.section.resolved:
            fields_by_name.setdefault(item.canonical, []).append(item.as_dict())
        block = self.section.section
        return {
            "source": self.source,
            "source_key": self.source_key,
            "origin": {
                "raw_sha256": self.raw_digest,
                "raw_base64": base64.b64encode(self.raw).decode("ascii") if self.raw else None,
                "encoding": self.encoding,
                "evidence": list(self.decode_evidence),
                "sample_key": self.sample_key,
            },
            "block": {
                "section_id": block.section_id,
                "kind": self.section.kind,
                "identity": (
                    {"attribute": block.identity[0], "value": block.identity[1]}
                    if block.identity else None
                ),
                "company_raw": block.company_raw,
                "attrs": dict(block.attrs),
                "page_sequence": self.page_sequence,
                "char_span": [block.start, block.end],
                "byte_span": [block.byte_start, block.byte_end],
            },
            "fields": fields_by_name,
            "rejected": list(self.section.rejected),
            "date_issues": list(self.section.date_issues),
        }


@dataclass
class QuarantineRecord:
    source: str
    reason: str
    raw: bytes
    details: dict
    sample_key: str

    def key(self) -> str:
        return self.sample_key

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "sample_key": self.sample_key,
            "reason": self.reason,
            "raw_sha256": digest(self.raw),
            "raw_base64": base64.b64encode(self.raw).decode("ascii"),
            "details": self.details,
        }


@dataclass
class RunResult:
    records: list[ArchiveRecord] = field(default_factory=list)
    quarantine: list[QuarantineRecord] = field(default_factory=list)
    decoded: list[dict] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    skipped_duplicates: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "records": [record.as_dict() for record in self.records],
            "quarantine": [item.as_dict() for item in self.quarantine],
            "decoded": list(self.decoded),
            "replayed": list(self.replayed),
            "skipped_duplicates": list(self.skipped_duplicates),
            "untouched": list(self.untouched),
        }


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSample:
    """One raw response from one source, with cumulative chunk boundaries."""

    source: str
    raw: bytes
    boundaries: tuple[int, ...]
    http_charset: str | None = None
    source_charset: str | None = None
    company_id: str | None = None  # scoped per source; never merged across sources
    label: str = ""

    def key(self) -> str:
        label = self.label or "-".join(map(str, self.boundaries)) or "0"
        return f"{self.source}:{label}:{digest(self.raw)}"


def sample_from_fixture(sample: dict) -> SourceSample:
    return SourceSample(
        source=sample["source"],
        raw=raw_of(sample),
        boundaries=tuple(int(item) for item in sample.get("chunks", [])),
        http_charset=sample.get("http_charset"),
        source_charset=sample.get("source_charset"),
        company_id=str(sample["company_id"]) if sample.get("company_id") is not None else None,
        label=sample.get("label", ""),
    )


def rules_from_fixture(path: str | Path) -> dict[str, SourceRule]:
    config = load_json(path)
    sources = config.get("sources", config)
    result: dict[str, SourceRule] = {}
    for key, mapping in sources.items():
        if not isinstance(mapping, dict):
            continue
        mapping = dict(mapping)
        mapping.setdefault("source", key)
        result[key] = source_rule_from(mapping)
    return result


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class Collector:
    """Ingest raw responses into archive records, with explicit quarantine.

    Archive identity is ``(source, source-local key)``; identical company
    numbers from two sources therefore can never collapse into one record.
    Replaying the same sample or confirmation is idempotent.

    Replay scope is recomputed on every pass: a sample that now decodes and
    resolves cleanly is archived even if an earlier pass quarantined it (for
    example after a charset was confirmed).  Samples still failing stay in
    quarantine, and samples not named in the confirmation are untouched.
    """

    def __init__(self, rules: dict[str, SourceRule] | None = None,
                 archive: dict[tuple[str, str], ArchiveRecord] | None = None):
        self.rules: dict[str, SourceRule] = dict(rules or {})
        self.archive: dict[tuple[str, str], ArchiveRecord] = dict(archive or {})
        self.quarantine: dict[str, QuarantineRecord] = {}
        self._samples: dict[str, SourceSample] = {}
        self._archived_sample_keys: set[str] = set()

    # -- ingest -----------------------------------------------------------
    def ingest(self, samples: Iterable[SourceSample],
               confirmations: dict[str, dict] | None = None) -> RunResult:
        """Process samples.

        ``confirmations`` is keyed by ``SourceSample.key()`` and may carry a
        ``charset`` and/or ``section_rules``.  A manual charset applies to that
        sample only; confirmed section rules replace the same section kind in
        that source only.
        """

        confirmations = confirmations or {}
        result = RunResult()
        for sample in samples:
            key = sample.key()
            self._samples[key] = sample
            confirmation = confirmations.get(key, {})
            manual_charset = confirmation.get("charset")
            if manual_charset is not None and normalise_label(
                manual_charset, allow_generic_utf=True
            ) is None:
                raise ValueError(f"confirmed charset {manual_charset!r} is unsupported")
            if confirmation.get("section_rules"):
                self._apply_section_rules(sample.source, confirmation["section_rules"])

            outcome = self._process(sample, manual_charset=manual_charset, key=key)
            if isinstance(outcome, QuarantineRecord):
                result.quarantine.append(outcome)
            else:
                records, duplicates, summary = outcome
                result.decoded.append(summary)
                result.records.extend(records)
                result.skipped_duplicates.extend(duplicates)
                if key in confirmations and records:
                    result.replayed.append(key)
        return result

    def _apply_section_rules(self, source: str, section_rules: list[dict] | None) -> None:
        if not section_rules:
            return
        current = self.rules.get(source)
        if current is None:
            current = source_rule_from({"source": source})
        existing = {rule.kind for rule in current.sections}
        for mapping in section_rules:
            rule = section_rule_from(mapping)
            if rule.kind in existing:
                current.sections = [item for item in current.sections if item.kind != rule.kind]
            current.sections.append(rule)
            existing.add(rule.kind)
        self.rules[source] = current

    def _process(self, sample: SourceSample, *, manual_charset: str | None, key: str):
        decisions = decode_with_evidence(
            raw=sample.raw,
            boundaries=sample.boundaries,
            http_charset=sample.http_charset,
            source_charset=sample.source_charset,
            manual_charset=manual_charset,
        )
        summary = decisions.as_dict()
        summary.update({"source": sample.source, "sample_key": key, "label": sample.label})

        if decisions.decoded is None:
            return self._quarantine(sample, decisions.status, summary, key)
        decoded = decisions.decoded
        summary["chunk_carry_over"] = list(decoded.chunks)
        extraction = extract(decoded.text, decoded.byte_offsets)
        summary["sections"] = [section.as_dict() for section in extraction.sections]
        summary["structural_issues"] = extraction.issues
        summary["unclosed"] = extraction.unclosed
        if extraction.issues:
            # A label outside a block must not be attributed to a neighbour.
            return self._quarantine(sample, "unsectioned-content", summary, key)
        if not extraction.sections:
            return self._quarantine(sample, "no-section", summary, key)

        rule = self.rules.get(sample.source)
        if rule is None:
            return self._quarantine(sample, "no-source-rule", summary, key)

        records: list[ArchiveRecord] = []
        duplicates: list[str] = []
        for index, section in enumerate(extraction.sections):
            resolved = resolve_section(section, rule)
            summary.setdefault("resolve", {})[section.section_id] = {
                "kind": resolved.kind,
                "resolved": [item.as_dict() for item in resolved.resolved],
                "rejected": resolved.rejected,
                "date_issues": resolved.date_issues,
                "duplicates": resolved.duplicates,
            }
            if resolved.duplicates:
                # Two labels in one block claim one canonical field with
                # different values; a human must decide, so nothing is filed.
                return self._quarantine(sample, "conflicting-labels", summary, key)
            if resolved.date_issues:
                # Never guess a date.  The block waits for a human date rule;
                # a sibling block is not silently dropped in its favour.
                return self._quarantine(sample, "unresolved-date", summary, key)
            if not resolved.resolved:
                return self._quarantine(sample, "no-field-resolved", summary, key)

            archive_key = (sample.source, _company_key(sample, section, resolved, rule))
            existing = self.archive.get(archive_key)
            if existing is not None:
                same = (
                    existing.raw_digest == digest(sample.raw)
                    and existing.section.section.section_id == section.section_id
                )
                if same:
                    duplicates.append(key)
                    continue
                summary["archive_conflict"] = {
                    "archive_key": list(archive_key),
                    "existing_raw_sha256": existing.raw_digest,
                    "existing_section": existing.section.section.section_id,
                    "incoming_section": section.section_id,
                }
                return self._quarantine(sample, "archive-key-conflict", summary, key)

            records.append(ArchiveRecord(
                source=sample.source,
                source_key=archive_key[1],
                section=resolved,
                raw_digest=digest(sample.raw),
                decode_evidence=decoded.evidence,
                encoding=decoded.encoding,
                page_sequence=index,
                raw=sample.raw,
                sample_key=key,
            ))

        for record in records:
            self.archive[(record.source, record.source_key)] = record
        if records:
            self.quarantine.pop(key, None)
            self._archived_sample_keys.add(key)
        return records, duplicates, summary

    def _quarantine(self, sample: SourceSample, reason: str, details: dict, key: str):
        record = QuarantineRecord(source=sample.source, reason=reason, raw=sample.raw,
                                  details=details, sample_key=key)
        self.quarantine[key] = record
        return record

    # -- replay -----------------------------------------------------------
    def replay(self, confirmations: dict[str, dict]) -> RunResult:
        """Replay exactly the samples named in ``confirmations``.

        Only samples previously seen are eligible; everything else, including
        other quarantined samples, is left untouched.  Repeating the same
        confirmation archives nothing new: the result lists the key under
        ``skipped_duplicates``.
        """

        if not confirmations:
            raise ValueError("replay needs at least one confirmation")
        unknown = [key for key in confirmations if key not in self._samples]
        if unknown:
            raise KeyError(f"no raw response retained for samples: {unknown}")

        result = RunResult()
        for key in confirmations:
            sample = self._samples[key]
            confirmation = confirmations[key]
            if confirmation.get("section_rules"):
                self._apply_section_rules(sample.source, confirmation["section_rules"])
            outcome = self._process(sample, manual_charset=confirmation.get("charset"),
                                    key=key)
            if isinstance(outcome, QuarantineRecord):
                result.quarantine.append(outcome)
            elif outcome:
                records, duplicates, summary = outcome
                result.decoded.append(summary)
                result.records.extend(records)
                result.skipped_duplicates.extend(duplicates)
                if records:
                    result.replayed.append(key)
            else:
                result.skipped_duplicates.append(key)
        result.untouched = sorted(set(self.quarantine) - set(confirmations))
        return result


def _company_key(sample: SourceSample, section: Section, resolved: SectionResult,
                 rule: SourceRule) -> str:
    """Source-local archive identity.

    Precedence: explicit sample company id, then the company field resolved
    from *this block*, then the block identity.  The source is part of the
    caller's archive key, so equal ids never merge across sources.
    """

    if sample.company_id:
        return f"company:{sample.company_id}"
    if rule.company_field:
        for item in resolved.resolved:
            if item.canonical == rule.company_field:
                return f"name:{item.value}"
    return f"block:{section.section_id}"
