"""状态时间线记录。

组件每次把工艺状态落盘（``Component.persist_state``），都会在这里复制一份不可变
快照追加到 ``timeline/snapshots`` 流水：键、版本、时刻与完整载荷。文档库只留最新
版本，历史工况全靠这条流水还原——事故后「当时是什么状态」的答案就在这里面。

复盘标记（``replay/annotations``）是调查人员对时间线步骤的批注，写在独立流水里，
与任何组件状态键无关：标记可以补，现场不能改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from ..ns import Namespace
from ..runtime import Clock
from ..store import DurableStore, JournalEntry, Record

TIMELINE_STREAM = "timeline/snapshots"
ANNOTATIONS_STREAM = "replay/annotations"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """一次已落盘状态的时间线条目。"""

    seq: int
    key: str
    component: str
    version: int
    at: str
    epoch: float
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "key": self.key,
            "component": self.component,
            "version": self.version,
            "at": self.at,
            "epoch": self.epoch,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class StepMark:
    """调查人员给某一步挂的复盘标记。"""

    seq: int
    step: int
    actor: str
    note: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "step": self.step,
            "actor": self.actor,
            "note": self.note,
            "at": self.at,
        }


def snapshot_from_entry(entry: JournalEntry) -> Snapshot:
    payload = entry.payload
    return Snapshot(
        seq=entry.seq,
        key=str(payload.get("key", "")),
        component=str(payload.get("component", "")),
        version=int(payload.get("version", 0)),
        at=str(payload.get("at", entry.written_at)),
        epoch=float(payload.get("epoch", 0.0)),
        payload=payload.get("payload", {}) or {},
    )


def mark_from_entry(entry: JournalEntry) -> StepMark:
    payload = entry.payload
    return StepMark(
        seq=entry.seq,
        step=int(payload.get("step", 0)),
        actor=str(payload.get("actor", "unknown")),
        note=str(payload.get("note", "")),
        at=str(payload.get("at", entry.written_at)),
    )


class TimelineRecorder:
    """时间线流水与复盘标记的写入端（读取走 :class:`ReplaySession`）。"""

    def __init__(self, store: DurableStore, namespace: Namespace, clock: Clock) -> None:
        self._store = store
        self._namespace = namespace
        self._clock = clock

    def record(self, component: str, record: Record) -> JournalEntry:
        """把一次状态落盘复制进时间线；失败即抛错，与审计同口径，绝不静默丢帧。"""

        payload = {
            "key": record.key,
            "component": component,
            "version": record.version,
            "at": self._clock.timestamp_iso(),
            "epoch": self._clock.timestamp(),
            "payload": dict(record.payload),
        }
        return self._store.append(TIMELINE_STREAM, payload)

    def mark(self, *, step: int, note: str, actor: str) -> StepMark:
        """给时间线某一步挂复盘标记。

        标记只追加到 ``replay/annotations`` 流水，不触碰任何组件状态键——这是
        复盘元数据，不是现场状态。
        """

        if step < 1:
            raise ValidationError("标记的步号必须为正", details={"step": step})
        note = (note or "").strip()
        if not note:
            raise ValidationError("复盘标记必须填写说明")
        actor = (actor or "").strip() or "anonymous"
        entry = self._store.append(
            ANNOTATIONS_STREAM,
            {
                "step": step,
                "actor": actor,
                "note": note,
                "at": self._clock.timestamp_iso(),
            },
        )
        return mark_from_entry(entry)


__all__ = [
    "TimelineRecorder",
    "Snapshot",
    "StepMark",
    "TIMELINE_STREAM",
    "ANNOTATIONS_STREAM",
    "snapshot_from_entry",
    "mark_from_entry",
]
