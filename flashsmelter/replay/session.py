"""只读回放会话。

把「一段时间的状态变化」整理成可以逐步翻阅的时间线：每一步对应一次已落盘的
状态快照，辅以审计流里被联锁挡住/执行失败的动作尝试。翻到任意一步即可物化
当时的全厂工况。

会话只依赖 :class:`ReadOnlyStore`：物化在内存里进行，结构上不存在写回现场
的通道。关键步骤自动标注（联锁拒绝、跳闸/闩锁/复位类动作、人工复盘标记），
并给出每条标注的理由，复盘时不必猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..audit import AUDIT_STREAM
from ..errors import ValidationError
from ..ns import Namespace
from ..runtime import epoch_from_iso
from ..store import ReadOnlyStore
from .timeline import ANNOTATIONS_STREAM, TIMELINE_STREAM, Snapshot, StepMark, mark_from_entry, snapshot_from_entry

# 这些动作动词本身就是事故链上的节点：跳闸、闩锁、复位、回退、停炉。
CRITICAL_VERBS = ("trip", "latch", "reset", "rollback", "stop")

_READ_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class TimelineStep:
    """时间线上的一步：一次状态快照，附带关联到的动作与关键标记。"""

    step: int
    at: str
    epoch: float
    component: str
    key: str
    version: int
    action: str | None
    actor: str | None
    outcome: str | None
    critical: bool
    reasons: tuple[str, ...]
    marks: tuple[StepMark, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "snapshot",
            "step": self.step,
            "at": self.at,
            "component": self.component,
            "key": self.key,
            "version": self.version,
            "action": self.action,
            "actor": self.actor,
            "outcome": self.outcome,
            "critical": self.critical,
            "reasons": list(self.reasons),
            "marks": [mark.to_dict() for mark in self.marks],
        }


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次未改变状态的动作尝试（被联锁拒绝或执行失败），插在时间线上供复盘。"""

    at: str
    epoch: float
    action: str
    target: str
    actor: str
    outcome: str
    reason: str | None
    after_step: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "attempt",
            "step": None,
            "after_step": self.after_step,
            "at": self.at,
            "action": self.action,
            "target": self.target,
            "actor": self.actor,
            "outcome": self.outcome,
            "reason": self.reason,
            "critical": True,
            "reasons": [f"outcome:{self.outcome}"],
        }


@dataclass(slots=True)
class _AuditRecord:
    at: str
    epoch: float
    actor: str
    action: str
    target: str
    outcome: str
    key: str | None
    version: int | None
    reason: str | None


class ReplaySession:
    """只读回放：从流水物化历史工况，永不写回现场。"""

    def __init__(self, store: ReadOnlyStore, namespace: Namespace) -> None:
        if not isinstance(store, ReadOnlyStore):
            raise ValidationError(
                "回放会话只接受只读存储视图",
                details={"type": type(store).__name__},
            )
        self._store = store
        self._namespace = namespace
        self._snapshots: list[Snapshot] = [
            snapshot_from_entry(entry)
            for entry in store.read_stream(TIMELINE_STREAM, limit=_READ_LIMIT)
        ]
        self._marks: list[StepMark] = [
            mark_from_entry(entry)
            for entry in store.read_stream(ANNOTATIONS_STREAM, limit=_READ_LIMIT)
        ]
        self._audit: list[_AuditRecord] = self._load_audit()
        self._steps: list[TimelineStep] = self._build_steps()

    # ------------------------------------------------------------------ 时间线
    def steps(self, *, limit: int = 200, critical_only: bool = False) -> list[Mapping[str, Any]]:
        """时间有序的事件列表：状态步 + 未改变状态的动作尝试。"""

        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        events: list[Mapping[str, Any]] = []
        events.extend(step.to_dict() for step in self._steps)
        events.extend(attempt.to_dict() for attempt in self._build_attempts())
        events.sort(key=lambda item: (item.get("at") or "", item.get("step") or 0))
        if critical_only:
            events = [event for event in events if event.get("critical")]
        return events[-limit:]

    def marks(self) -> list[Mapping[str, Any]]:
        return [mark.to_dict() for mark in self._marks]

    def length(self) -> int:
        return len(self._steps)

    # ------------------------------------------------------------------ 定位
    def step_at_time(self, at: str) -> int:
        """``at`` 时刻所处的步号：最后一个不晚于该时刻的快照步；之前无快照返回 0。"""

        epoch = epoch_from_iso(at)
        step = 0
        for snapshot in self._snapshots:
            if snapshot.epoch <= epoch:
                step = snapshot.seq
            else:
                break
        return step

    def state_at(self, step: int) -> Mapping[str, Any]:
        """物化第 ``step`` 步结束时的全厂工况（``0`` 表示时间线起点之前）。"""

        if step < 0 or step > len(self._snapshots):
            raise ValidationError(
                "步号超出时间线范围",
                details={"step": step, "available": len(self._snapshots)},
            )
        materialized: dict[str, Snapshot] = {}
        for snapshot in self._snapshots[:step]:
            materialized[snapshot.key] = snapshot
        components: dict[str, Any] = {}
        for snapshot in sorted(materialized.values(), key=lambda item: item.component):
            components[snapshot.component] = {
                "key": snapshot.key,
                "version": snapshot.version,
                "recorded_at": snapshot.at,
                "snapshot": dict(snapshot.payload),
            }
        current = self._steps[step - 1] if step >= 1 else None
        return {
            "namespace": self._namespace.prefix,
            "step": step,
            "at": None if current is None else current.at,
            "components": components,
            "component_count": len(components),
            "read_only": True,
        }

    def state_at_time(self, at: str) -> Mapping[str, Any]:
        """按时刻定位并物化工况；返回里带定位到的步号。"""

        return self.state_at(self.step_at_time(at))

    # ------------------------------------------------------------------ 内部
    def _load_audit(self) -> list[_AuditRecord]:
        records: list[_AuditRecord] = []
        for entry in self._store.read_stream(AUDIT_STREAM, limit=_READ_LIMIT):
            payload = entry.payload
            details = payload.get("details", {}) or {}
            key = details.get("key")
            version = details.get("version")
            at = str(payload.get("at", entry.written_at))
            try:
                epoch = epoch_from_iso(at)
            except ValidationError:
                epoch = 0.0
            records.append(
                _AuditRecord(
                    at=at,
                    epoch=epoch,
                    actor=str(payload.get("actor", "unknown")),
                    action=str(payload.get("action", "")),
                    target=str(payload.get("target", "")),
                    outcome=str(payload.get("outcome", "ok")),
                    key=None if key is None else str(key),
                    version=None if version is None else int(version),
                    reason=None if details.get("reason") is None else str(details.get("reason")),
                )
            )
        return records

    def _build_steps(self) -> list[TimelineStep]:
        by_record: dict[tuple[str, int], _AuditRecord] = {}
        for record in self._audit:
            if record.key is not None and record.version is not None:
                by_record[(record.key, record.version)] = record
        marks_by_step: dict[int, list[StepMark]] = {}
        for mark in self._marks:
            marks_by_step.setdefault(mark.step, []).append(mark)
        steps: list[TimelineStep] = []
        for snapshot in self._snapshots:
            audit = by_record.get((snapshot.key, snapshot.version))
            marks = tuple(marks_by_step.get(snapshot.seq, ()))
            reasons: list[str] = []
            if audit is not None:
                verb = audit.action.rsplit(".", 1)[-1]
                if audit.outcome != "ok":
                    reasons.append(f"outcome:{audit.outcome}")
                if verb in CRITICAL_VERBS:
                    reasons.append(f"verb:{verb}")
            if marks:
                reasons.append("marked")
            steps.append(
                TimelineStep(
                    step=snapshot.seq,
                    at=snapshot.at,
                    epoch=snapshot.epoch,
                    component=snapshot.component,
                    key=snapshot.key,
                    version=snapshot.version,
                    action=None if audit is None else audit.action,
                    actor=None if audit is None else audit.actor,
                    outcome=None if audit is None else audit.outcome,
                    critical=bool(reasons),
                    reasons=tuple(reasons),
                    marks=marks,
                )
            )
        return steps

    def _build_attempts(self) -> list[Attempt]:
        attempts: list[Attempt] = []
        for record in self._audit:
            if record.outcome == "ok":
                continue
            after_step = 0
            for snapshot in self._snapshots:
                if snapshot.epoch <= record.epoch:
                    after_step = snapshot.seq
                else:
                    break
            attempts.append(
                Attempt(
                    at=record.at,
                    epoch=record.epoch,
                    action=record.action,
                    target=record.target,
                    actor=record.actor,
                    outcome=record.outcome,
                    reason=record.reason,
                    after_step=after_step,
                )
            )
        return attempts


__all__ = ["ReplaySession", "TimelineStep", "Attempt", "CRITICAL_VERBS"]
