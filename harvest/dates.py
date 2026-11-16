"""来源规则驱动的日期解析。

格式与时区只能来自来源规则：规则里声明哪些 ``strftime`` 模式、按什么顺序
尝试，就严格按规则解析。任何无法确定的日期保留原始字符串并标注
``resolved=False``，调用方据此隔离，绝不猜测格式或时区。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class DateParseError(ValueError):
    """规则声明该字段必须是日期，但没有任何来源格式能解释原值。"""


@dataclass(frozen=True)
class DateResolution:
    raw: str
    resolved: bool
    calendar_date: Optional[str]
    timezone: Optional[str]
    format_id: Optional[str]
    parse_basis: str
    note: str

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "resolved": self.resolved,
            "calendar_date": self.calendar_date,
            "timezone": self.timezone,
            "format_id": self.format_id,
            "parse_basis": self.parse_basis,
            "note": self.note,
        }


def parse_date(raw: str, field_name: str, source_rule: dict) -> DateResolution:
    """按来源规则解析单个日期值。

    规则结构（均为显式声明，无隐式默认猜测）::

        {"date_fields": {"founded": {"formats": ["%d/%m/%Y"],
                                     "timezone": "UTC+08:00",
                                     "required": true}}}
    """

    value = (raw or "").strip()
    field_rules = dict(source_rule.get("date_fields", {}))
    field_rule = field_rules.get(field_name, {})
    timezone = field_rule.get("timezone") or source_rule.get("timezone")
    formats = list(field_rule.get("formats", []))
    format_names = {
        index + 1: pattern for index, pattern in enumerate(formats)
    }

    if not value:
        return DateResolution(
            raw=raw,
            resolved=False,
            calendar_date=None,
            timezone=timezone,
            format_id=None,
            parse_basis="source date rule",
            note="empty date value",
        )

    for index, pattern in enumerate(formats, start=1):
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        return DateResolution(
            raw=raw,
            resolved=True,
            calendar_date=parsed.date().isoformat(),
            timezone=timezone,
            format_id=f"{field_name}.format_{index}",
            parse_basis=f"source rule {field_name} format {index}: {pattern!r}",
            note="ok",
        )

    note = (
        f"no source-declared format for {field_name!r} explains {value!r}; "
        f"declared formats={format_names or {}}"
    )
    if field_rule.get("required", False):
        raise DateParseError(note)
    return DateResolution(
        raw=raw,
        resolved=False,
        calendar_date=None,
        timezone=timezone,
        format_id=None,
        parse_basis="source date rule",
        note=note,
    )
