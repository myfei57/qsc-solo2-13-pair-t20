"""历史工况只读回放。

* :class:`TimelineRecorder` 把每次状态落盘复制成不可变快照，串成时间线；
* :class:`ReplaySession` 把一段时间的状态变化整理成可逐步翻阅的时间线，
  翻到任意一步看当时工况，关键步骤自动标注，且结构上无法写回现场。
"""

from __future__ import annotations

from .session import Attempt, CRITICAL_VERBS, ReplaySession, TimelineStep
from .timeline import (
    ANNOTATIONS_STREAM,
    TIMELINE_STREAM,
    Snapshot,
    StepMark,
    TimelineRecorder,
    mark_from_entry,
    snapshot_from_entry,
)

__all__ = [
    "TimelineRecorder",
    "ReplaySession",
    "TimelineStep",
    "Attempt",
    "Snapshot",
    "StepMark",
    "CRITICAL_VERBS",
    "TIMELINE_STREAM",
    "ANNOTATIONS_STREAM",
    "snapshot_from_entry",
    "mark_from_entry",
]
