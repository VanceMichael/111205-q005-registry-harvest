"""字段别名与区块感知映射。

``map_record`` 与仓库原有 ``service.map_record`` 的语义完全一致，已有调用方与
回归测试不受影响。新增的 ``apply_mapping`` 在 *区块内* 做别名解析：

- 同名标签出现在不同区块是正常现象，按区块身份分别取值，互不覆盖
  （例如“本次登记”和“历史登记”各自的“日期”）；
- 同一区块内同名标签出现多次、或不同别名解析出不同值，属于语义冲突，
  返回冲突明细交由上层隔离，绝不取第一个值了事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from harvest.blocks import Fragment, PageSection


def map_record(raw, aliases):
    """保留的历史接口：扁平字典上的别名合并，冲突即抛错。"""

    mapped = {}
    for target, candidates in aliases.items():
        values = [
            raw[key]
            for key in candidates
            if key in raw and raw[key] not in (None, "")
        ]
        if len(set(map(str, values))) > 1:
            raise ValueError(f"ambiguous value for {target}")
        if values:
            mapped[target] = values[0]
    return mapped


@dataclass
class MappedField:
    target: str
    value: str
    fragment: Fragment
    aliases: list[str]

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "value": self.value,
            "aliases": list(self.aliases),
            "fragment": self.fragment.to_dict(),
        }


@dataclass
class SectionRecord:
    section: PageSection
    fields: dict[str, MappedField] = field(default_factory=dict)
    extras: list[Fragment] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "section": self.section.to_dict(),
            "fields": {name: item.to_dict() for name, item in self.fields.items()},
            "extras": [fragment.to_dict() for fragment in self.extras],
            "conflicts": list(self.conflicts),
        }


def _field_specs(section_rule: dict) -> dict[str, dict]:
    specs = {}
    for target, spec in dict(section_rule.get("fields", {})).items():
        specs[target] = {
            "aliases": list(spec.get("aliases", [])),
            "type": spec.get("type", "text"),
            "required": bool(spec.get("required", False)),
        }
    return specs


def apply_mapping(
    fragments: list[Fragment],
    sections: list[PageSection],
    source_rule: dict,
) -> list[SectionRecord]:
    """按来源的区块规则把片段映射成字段，冲突以明细形式返回，不抛掉数据。"""

    section_rules = dict(source_rule.get("sections", {}))
    section_index: dict[str, PageSection] = {
        section.section_id: section for section in sections
    }
    grouped: dict[str, list[Fragment]] = {}
    for fragment in fragments:
        grouped.setdefault(fragment.section.section_id, []).append(fragment)

    records: list[SectionRecord] = []
    for section_id, section_fragments in grouped.items():
        section_meta = section_index.get(
            section_id, section_fragments[0].section
        )
        section_rule = section_rules.get(section_id, {})
        specs = _field_specs(section_rule)
        record = SectionRecord(section=section_meta)

        # 标签 -> 片段（同区块同名标签出现多次即冲突，不允许取第一个）。
        by_label: dict[str, list[Fragment]] = {}
        for fragment in section_fragments:
            by_label.setdefault(fragment.label, []).append(fragment)

        consumed_labels: set[str] = set()
        for target, spec in specs.items():
            candidates: list[Fragment] = []
            matched_aliases: list[str] = []
            for alias in spec["aliases"]:
                if alias in by_label:
                    candidates.extend(by_label[alias])
                    matched_aliases.append(alias)
                    consumed_labels.add(alias)
            if not candidates:
                continue

            label_counts: dict[str, int] = {}
            for candidate in candidates:
                label_counts[candidate.label] = label_counts.get(candidate.label, 0) + 1
            duplicate_labels = [
                label for label, count in label_counts.items() if count > 1
            ]
            distinct_values = {candidate.value for candidate in candidates}
            if duplicate_labels:
                record.conflicts.append(
                    {
                        "target": target,
                        "kind": "duplicate_label_in_block",
                        "duplicate_labels": duplicate_labels,
                        "values": [candidate.to_dict() for candidate in candidates],
                    }
                )
                continue
            if len(distinct_values) > 1:
                record.conflicts.append(
                    {
                        "target": target,
                        "kind": "alias_value_conflict",
                        "aliases": matched_aliases,
                        "values": [candidate.to_dict() for candidate in candidates],
                    }
                )
                continue

            record.fields[target] = MappedField(
                target=target,
                value=candidates[0].value,
                fragment=candidates[0],
                aliases=matched_aliases,
            )

        for label, label_fragments in by_label.items():
            if label not in consumed_labels:
                record.extras.extend(label_fragments)
        records.append(record)
    return records
