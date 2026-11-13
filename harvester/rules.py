"""Source rules: field selection by block, aliases, date formats and timezones.

The same visible label can mean different things in different sources or
blocks, so a rule selects fields by ``(source, section kind)`` **and only then**
maps labels to canonical names.  Rules are declarative data; nothing is solved
by site-specific string replacement.

A rule may forbid a label inside a section (for example a ``日期`` that is the
block's update timestamp rather than an establishment date), which is how
"updated time overwrote 成立日期" is prevented.  A rule may also pin an
``occurrence`` of a repeated label to a canonical field, but only after a human
has confirmed it; without such confirmation a repeated label with distinct
values is quarantined rather than resolved by position.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from .sections import Section

try:  # reuse the pre-existing field mapping interface, unchanged
    from service import map_record as legacy_map_record
except ImportError:  # pragma: no cover - only when used outside the repository
    legacy_map_record = None

# ---------------------------------------------------------------------------
# Labels and section kinds
# ---------------------------------------------------------------------------

SECTION_KIND_ALIASES = {
    "本次登记": "current", "当前登记": "current", "current": "current",
    "历史登记": "history", "历史": "history", "history": "history",
}


def normalise_label_text(text: str) -> str:
    return re.sub(r"[\s:：]+", "", text.strip()).lower()


def normalise_kind(kind: str) -> str:
    stripped = kind.strip()
    return SECTION_KIND_ALIASES.get(stripped, stripped.lower())


# ---------------------------------------------------------------------------
# Date rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DateRule:
    """A declared date format plus a declared timezone.

    ``timezone`` may be ``None`` only when the format itself carries an
    explicit offset.  A format without a time-of-day denotes a calendar day,
    so the declared zone is recorded as interpretation but cannot move the
    date.  A format with a time is converted to UTC before the date is taken.
    If no timezone is declared and none is in the value, parsing fails and the
    caller must quarantine; it is never guessed.
    """

    format: str
    timezone: str | None = None
    label: str | None = None

    @property
    def has_time(self) -> bool:
        return any(token in self.format for token in ("%H", "%I", "%M", "%S", "%f"))

    def parse(self, value: str) -> datetime.date | None:
        text = value.strip()
        try:
            parsed = datetime.datetime.strptime(text, self.format)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            if self.has_time:
                return parsed.astimezone(datetime.timezone.utc).date()
            return parsed.date()
        if not self.timezone:
            return None
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(self.timezone)
        except Exception:
            return None
        if not self.has_time:
            return parsed.date()
        return parsed.replace(tzinfo=zone).astimezone(datetime.timezone.utc).date()

    def describe(self) -> str:
        if self.has_time:
            return f"format {self.format!r}; timezone {self.timezone or 'in value'}"
        return (f"format {self.format!r}; date-only calendar day in "
                f"timezone {self.timezone or 'in value'}")


def date_rule_from(mapping: dict | None) -> DateRule | None:
    if not mapping:
        return None
    if "format" not in mapping:
        raise ValueError(f"date rule needs a 'format': {mapping!r}")
    return DateRule(format=mapping["format"], timezone=mapping.get("timezone"),
                    label=mapping.get("label"))


# ---------------------------------------------------------------------------
# Field / section / source rules
# ---------------------------------------------------------------------------

@dataclass
class FieldRule:
    canonical: str
    aliases: list[str]
    parser: str = "text"             # text | date
    date_rule: DateRule | None = None
    required: bool = False
    # Only claim the n-th occurrence (1-based) of the label inside the block.
    # Set by a human confirmation, never inferred from document order.
    occurrence: int | None = None

    def matches(self, label: str) -> bool:
        normalised = normalise_label_text(label)
        return any(normalise_label_text(alias) == normalised for alias in self.aliases)


@dataclass
class SectionRule:
    kind: str
    fields: list[FieldRule] = field(default_factory=list)
    forbidden_labels: list[str] = field(default_factory=list)
    date_rule: DateRule | None = None

    def matches_forbidden(self, label: str) -> bool:
        normalised = normalise_label_text(label)
        return any(normalise_label_text(item) == normalised for item in self.forbidden_labels)


@dataclass
class SourceRule:
    source: str
    sections: list[SectionRule] = field(default_factory=list)
    company_field: str | None = None
    default_date_rule: DateRule | None = None
    notes: str = ""

    def section_rule(self, kind: str) -> SectionRule | None:
        wanted = normalise_kind(kind)
        for rule in self.sections:
            if normalise_kind(rule.kind) == wanted:
                return rule
        return None

    def field_rules(self, kind: str) -> list[FieldRule]:
        section = self.section_rule(kind)
        return list(section.fields) if section else []


def field_rule_from(mapping: dict) -> FieldRule:
    if "canonical" not in mapping:
        raise ValueError(f"field rule needs 'canonical': {mapping!r}")
    parser = mapping.get("parser", "text")
    if parser not in ("text", "date"):
        raise ValueError(f"unsupported field parser {parser!r}")
    return FieldRule(
        canonical=mapping["canonical"],
        aliases=list(mapping.get("aliases", [])),
        parser=parser,
        date_rule=date_rule_from(mapping.get("date")),
        required=bool(mapping.get("required", False)),
        occurrence=int(mapping["occurrence"]) if mapping.get("occurrence") is not None else None,
    )


def section_rule_from(mapping: dict) -> SectionRule:
    return SectionRule(
        kind=normalise_kind(mapping.get("kind", "")),
        fields=[field_rule_from(item) for item in mapping.get("fields", [])],
        forbidden_labels=list(mapping.get("forbidden_labels", [])),
        date_rule=date_rule_from(mapping.get("date")),
    )


def source_rule_from(mapping: dict) -> SourceRule:
    return SourceRule(
        source=mapping["source"],
        sections=[section_rule_from(item) for item in mapping.get("sections", [])],
        company_field=mapping.get("company_field"),
        default_date_rule=date_rule_from(mapping.get("date")),
        notes=mapping.get("notes", ""),
    )


def rules_from_config(config: dict) -> dict[str, SourceRule]:
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
# Resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedField:
    canonical: str
    value: str
    raw_value: str
    label: str
    section_local: str
    label_span: dict
    value_span: dict
    rule: str = ""
    interpretation: str = ""

    def as_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "value": self.value,
            "raw_value": self.raw_value,
            "label": self.label,
            "section_local": self.section_local,
            "label_span": self.label_span,
            "value_span": self.value_span,
            "rule": self.rule,
            "interpretation": self.interpretation,
        }


@dataclass
class SectionResult:
    section: Section
    kind: str
    resolved: list[ResolvedField]
    rejected: list[dict]
    date_issues: list[dict]
    duplicates: list[dict] = field(default_factory=list)

    def by_canonical(self) -> dict[str, list[ResolvedField]]:
        result: dict[str, list[ResolvedField]] = {}
        for item in self.resolved:
            result.setdefault(item.canonical, []).append(item)
        return result


def select_section_kind(section: Section) -> str:
    if section.identity is None:
        return "unknown"
    return normalise_kind(section.identity[1])


def resolve_section(section: Section, rule: SourceRule,
                    page_kind: str | None = None) -> SectionResult:
    """Resolve one block's labels using only that block's rule.

    Repeated same-name labels are never resolved by position unless a human
    rule pins ``occurrence``.  Two different values for one canonical field
    inside one block are reported in ``duplicates`` and the caller must
    quarantine; the first value is not silently kept.
    """

    kind = page_kind or select_section_kind(section)
    section_rule = rule.section_rule(kind)
    allowed = rule.field_rules(kind)
    resolved: list[ResolvedField] = []
    rejected: list[dict] = []
    date_issues: list[dict] = []
    duplicates: list[dict] = []
    seen_labels: dict[str, int] = {}
    claimed: dict[str, ResolvedField] = {}

    for item in section.fields:
        normalised = normalise_label_text(item.label)
        seen_labels[normalised] = seen_labels.get(normalised, 0) + 1
        occurrence = seen_labels[normalised]

        if section_rule is not None and section_rule.matches_forbidden(item.label):
            rejected.append({
                "label": item.label,
                "value": item.value,
                "reason": "label forbidden for this section",
                "span": item.value_span.as_dict(),
            })
            continue

        matched = [
            field_rule for field_rule in allowed
            if field_rule.matches(item.label)
            and (field_rule.occurrence is None or field_rule.occurrence == occurrence)
        ]
        canonicals = {field_rule.canonical for field_rule in matched}
        if len(canonicals) > 1:
            rejected.append({
                "label": item.label,
                "value": item.value,
                "reason": "label matches conflicting canonical fields",
                "candidates": sorted(canonicals),
                "span": item.value_span.as_dict(),
            })
            continue
        if not matched:
            has_any = any(field_rule.matches(item.label) for field_rule in allowed)
            reason = ("this occurrence of the label is not claimed by any rule"
                      if has_any else "no rule for label in this section")
            rejected.append({
                "label": item.label,
                "value": item.value,
                "reason": reason,
                "span": item.value_span.as_dict(),
            })
            continue

        field_rule = matched[0]
        raw_value = item.value
        if field_rule.parser == "date":
            date_rule = (field_rule.date_rule
                         or (section_rule.date_rule if section_rule else None)
                         or rule.default_date_rule)
            if date_rule is None:
                date_issues.append({
                    "canonical": field_rule.canonical,
                    "raw_value": raw_value,
                    "reason": "no declared date format/timezone for this source and section",
                })
                continue
            parsed = date_rule.parse(raw_value)
            if parsed is None:
                date_issues.append({
                    "canonical": field_rule.canonical,
                    "raw_value": raw_value,
                    "reason": f"does not match declared {date_rule.describe()}",
                })
                continue
            value = parsed.isoformat()
            interpretation = date_rule.describe()
        else:
            value = raw_value
            interpretation = "text"

        previous = claimed.get(field_rule.canonical)
        entry = ResolvedField(
            canonical=field_rule.canonical,
            value=value,
            raw_value=raw_value,
            label=item.label,
            section_local=item.section_local,
            label_span=item.label_span.as_dict(),
            value_span=item.value_span.as_dict(),
            rule=f"{rule.source}:{kind}:{field_rule.canonical}",
            interpretation=interpretation,
        )
        if previous is None:
            claimed[field_rule.canonical] = entry
            resolved.append(entry)
        elif previous.value != value:
            duplicates.append({
                "canonical": field_rule.canonical,
                "first": {"label": previous.label, "raw_value": previous.raw_value,
                          "span": previous.value_span},
                "second": {"label": item.label, "raw_value": raw_value,
                           "span": item.value_span.as_dict()},
                "reason": "two labels resolve to one canonical field with different values",
            })

    resolved = merge_aliases(resolved, duplicates)

    return SectionResult(section=section, kind=kind, resolved=resolved,
                         rejected=rejected, date_issues=date_issues,
                         duplicates=duplicates)


def merge_aliases(entries: list[ResolvedField], duplicates: list[dict]) -> list[ResolvedField]:
    """Coalesce same-canonical entries through the legacy ``map_record``.

    ``map_record`` raises on differing values.  Those cases are already
    recorded in ``duplicates`` so the block is quarantined with full evidence
    instead of raising mid-ingest; equal values merge exactly as before.
    """

    if len({entry.canonical for entry in entries}) == len(entries):
        return entries

    grouped: dict[str, list[ResolvedField]] = {}
    for entry in entries:
        grouped.setdefault(entry.canonical, []).append(entry)

    merged: list[ResolvedField] = []
    for canonical, group in grouped.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        if any(item["canonical"] == canonical for item in duplicates):
            continue
        if legacy_map_record is None:  # pragma: no cover
            merged.append(group[0])
            continue
        keys = [f"{canonical}#{index}" for index in range(len(group))]
        try:
            legacy_map_record({key: entry.value for key, entry in zip(keys, group)},
                              {canonical: keys})
        except ValueError as exc:  # pragma: no cover - guarded by duplicates
            raise ValueError(f"ambiguous aliases in one block: {exc}") from exc
        merged.append(group[0])
    return merged
