"""内存档案库与隔离区，带幂等键，可导出 JSON 供检查与持久化。

重放语义：
- 档案按内容哈希幂等，重复重放同一样本不会多发一份档案；
- 隔离区按样本去重，同一样本再次失败只更新一次，不产生重复隔离单；
- 人工解隔离后原隔离单保留并标记 resolution，便于审计，而不是静默删除。
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from harvest.records import ArchiveRecord, QuarantineRecord


class ArchiveStore:
    def __init__(self) -> None:
        self._records: dict[str, ArchiveRecord] = {}
        self.duplicate_puts = 0

    def put(self, record: ArchiveRecord) -> bool:
        """写入一条档案；archive_id 已存在则跳过并计数，返回是否新增。"""

        if record.archive_id in self._records:
            self.duplicate_puts += 1
            return False
        self._records[record.archive_id] = record
        return True

    def get(self, archive_id: str) -> Optional[ArchiveRecord]:
        return self._records.get(archive_id)

    def all(self) -> list[ArchiveRecord]:
        return [self._records[key] for key in sorted(self._records)]

    def by_source(self, source_id: str) -> list[ArchiveRecord]:
        return [
            record
            for record in self.all()
            if record.source_id == source_id
        ]

    def to_list(self) -> list[dict]:
        return [record.to_dict() for record in self.all()]

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_list(), handle, ensure_ascii=False, indent=2)


class QuarantineStore:
    def __init__(self) -> None:
        self._records: dict[str, QuarantineRecord] = {}

    def put(self, record: QuarantineRecord) -> bool:
        existing = self._records.get(record.quarantine_id)
        if existing is not None and not existing.resolved:
            # 同一样本重复失败：保持首次隔离单，避免重放刷出重复单据。
            return False
        if existing is None:
            self._records[record.quarantine_id] = record
            return True
        # 已解决的单据再次失败，按新的一代隔离单处理。
        record.quarantine_id = f"{record.quarantine_id}#{record.replay_generation}"
        self._records[record.quarantine_id] = record
        return True

    def get(self, quarantine_id: str) -> Optional[QuarantineRecord]:
        return self._records.get(quarantine_id)

    def open(self) -> list[QuarantineRecord]:
        return [
            record
            for record in self._records.values()
            if not record.resolved
        ]

    def all(self) -> list[QuarantineRecord]:
        return list(self._records.values())

    def resolve(
        self,
        quarantine_id: str,
        resolution: dict,
        resolved_at: str,
    ) -> QuarantineRecord:
        record = self._records[quarantine_id]
        record.resolved = True
        record.resolution = dict(resolution)
        record.resolved_at = resolved_at
        return record

    def to_list(self) -> list[dict]:
        return [record.to_dict() for record in self._records.values()]

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_list(), handle, ensure_ascii=False, indent=2)
