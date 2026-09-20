"""工况回放。

事故复盘要回答的不是「现在是什么状态」，而是「当时是怎么一步步走到这儿的」。
本模块把每次动作之后的完整工况落成一帧，追加到 ``replay/frames`` 流水（与审计
同一套单调序号 + 逐行校验和），事后可以按序号或按时间翻到任意一步，看当时的
工况；关键步由规则自动标记，调查时也可以人工补标。

* 捕获侧 :class:`TimelineRecorder` 挂在组件动作记账之后：动作成功、被联锁拒绝、
  执行失败都会留帧——被挡下的尝试恰恰是复盘时最想看到的；
* 读取侧 :class:`Replay` 只经 :class:`ReadOnlyStore` 门面访问存储，门面没有写
  方法，回放只能看，从结构上杜绝「回放反过来改现场状态」；
* 人工标记追加到 ``replay/marks`` 流水，只增不改，且每条标记都进审计。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from ..errors import NotFoundError, ValidationError
from ..ns import Namespace
from ..runtime import RuntimeContext, epoch_from_iso
from ..store import DurableStore, JournalEntry

LOGGER = logging.getLogger("flashsmelter.replay")

FRAMES_STREAM = "replay/frames"
MARKS_STREAM = "replay/marks"

# 各组件表示「故障保持」的状态名：进入这些状态的一步必然是关键步。
LATCH_STATES = ("latched", "fault_latched")

# 余热锅炉在役状态：只有这些状态下汽包水位/排烟温度越限才算异常，
# 未投运（idle/cooling）时测点为零是正常工况，不标记。
WASTE_ACTIVE_STATES = ("circulating", "heat_exchanging", "latched")

_READ_LIMIT = 1_000_000


def evaluate_critical(
    trigger: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """判定一帧是否关键步，返回命中的规则列表。

    ``previous`` 为 ``None`` 表示时间轴上的第一帧：没有可对比的基线，跃迁类规则
    跳过，越限类规则按当前工况直接判定。
    """

    marks: list[dict[str, Any]] = []
    outcome = str(trigger.get("outcome", ""))
    if outcome == "rejected":
        marks.append(
            {
                "rule": "action-rejected",
                "component": trigger.get("component"),
                "detail": f"动作被联锁拒绝：{trigger.get('reason') or '未注明原因'}",
            }
        )
    elif outcome == "failed":
        marks.append(
            {
                "rule": "action-failed",
                "component": trigger.get("component"),
                "detail": f"动作执行失败：{trigger.get('reason') or '未注明原因'}",
            }
        )
    prior = previous if isinstance(previous, Mapping) else {}
    for name, condition in current.items():
        if not isinstance(condition, Mapping):
            continue
        state = condition.get("state")
        before_condition = prior.get(name)
        before = before_condition.get("state") if isinstance(before_condition, Mapping) else None
        if previous is not None and state in LATCH_STATES and before != state:
            marks.append(
                {
                    "rule": "fault-latch",
                    "component": name,
                    "detail": f"{before or '∅'} → {state}",
                }
            )
    furnace = current.get("furnace")
    if previous is not None and isinstance(furnace, Mapping):
        before_furnace = prior.get("furnace")
        before = before_furnace.get("state") if isinstance(before_furnace, Mapping) else None
        if furnace.get("state") != before:
            marks.append(
                {
                    "rule": "furnace-transition",
                    "component": "furnace",
                    "detail": f"{before or '∅'} → {furnace.get('state')}",
                }
            )
    waste = current.get("waste")
    if isinstance(waste, Mapping) and waste.get("state") in WASTE_ACTIVE_STATES:
        prior_waste = prior.get("waste")
        prior_waste = prior_waste if isinstance(prior_waste, Mapping) else {}
        if waste.get("tube_leak") and not prior_waste.get("tube_leak", False):
            marks.append({"rule": "tube-leak", "component": "waste", "detail": "检测到管束泄漏"})
        _limit_mark(
            marks,
            waste,
            prior_waste,
            key="drum_level",
            limit_key="drum_level_min",
            rule="drum-level-low",
            below=True,
            label="汽包水位",
        )
        _limit_mark(
            marks,
            waste,
            prior_waste,
            key="exhaust_temp_c",
            limit_key="exhaust_temp_max_c",
            rule="exhaust-over-temperature",
            below=False,
            label="排烟温度",
        )
    return marks


def _limit_mark(
    marks: list[dict[str, Any]],
    current: Mapping[str, Any],
    prior: Mapping[str, Any],
    *,
    key: str,
    limit_key: str,
    rule: str,
    below: bool,
    label: str,
) -> None:
    """测点越限判定：只在这一帧「新出现」的越限才标记，持续越限不重复刷屏。"""

    value = current.get(key)
    limit = current.get(limit_key)
    if not isinstance(value, (int, float)) or not isinstance(limit, (int, float)):
        return
    breached = value < limit if below else value > limit
    if not breached:
        return
    before = prior.get(key)
    if isinstance(before, (int, float)) and (before < limit if below else before > limit):
        return
    marks.append(
        {
            "rule": rule,
            "component": "waste",
            "detail": f"{label}越限：{value}（限值 {limit}）",
        }
    )


class TimelineRecorder:
    """捕获侧：每个动作落审计之后，把动作后的完整工况追加成一帧。"""

    def __init__(self, ctx: RuntimeContext, *, conditions: Callable[[], Mapping[str, Any]]) -> None:
        self._store = ctx.store
        self._namespace = ctx.namespace
        self._clock = ctx.clock
        self._metrics = ctx.metrics
        self._conditions = conditions
        self._previous = self._tail_conditions()

    def capture(
        self,
        *,
        component: str,
        action: str,
        target: str,
        actor: str,
        correlation_id: str,
        outcome: str,
        reason: str,
        audit_seq: int | None,
        details: Mapping[str, Any],
    ) -> None:
        """追加一帧；捕获失败只计指标，绝不让复盘功能反过来影响现场动作。"""

        try:
            self._capture(
                component=component,
                action=action,
                target=target,
                actor=actor,
                correlation_id=correlation_id,
                outcome=outcome,
                reason=reason,
                audit_seq=audit_seq,
                details=details,
            )
        except Exception:  # pragma: no cover - 存储故障时动作本身已经执行完毕
            LOGGER.exception("回放帧捕获失败", extra={"component": component, "action": action})
            self._metrics.inc("replay.capture_failed")

    def _capture(
        self,
        *,
        component: str,
        action: str,
        target: str,
        actor: str,
        correlation_id: str,
        outcome: str,
        reason: str,
        audit_seq: int | None,
        details: Mapping[str, Any],
    ) -> None:
        snapshot = self._conditions()
        components = snapshot.get("components", {})
        trigger = {
            "action": action,
            "qualified": f"{component}.{action}",
            "component": component,
            "actor": actor,
            "target": target,
            "outcome": outcome,
            "reason": reason,
            "correlation_id": correlation_id,
            "audit_seq": audit_seq,
            "details": dict(details),
        }
        payload = {
            "at": self._clock.timestamp_iso(),
            "namespace": self._namespace.prefix,
            "trigger": trigger,
            "generation": snapshot.get("generation"),
            "critical": evaluate_critical(trigger, self._previous, components),
            "conditions": components,
            "heat": snapshot.get("heat"),
        }
        self._store.append(FRAMES_STREAM, payload)
        self._previous = components

    def _tail_conditions(self) -> Mapping[str, Any] | None:
        """启动时用时间轴上最后一帧做对比基线，保证重启后跃迁判定不断档。"""

        entries = self._store.read_stream(FRAMES_STREAM, limit=1)
        if not entries:
            return None
        conditions = entries[-1].payload.get("conditions")
        return conditions if isinstance(conditions, Mapping) else None


class ReadOnlyStore:
    """回放专用只读门面。

    只暴露读方法：回放链路拿着它也碰不到 ``put``/``append``，「回放只能看、不能
    反过来改现场状态」这条约束在结构上成立，而不是靠自觉。
    """

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def read_stream(self, stream: str, *, limit: int = 100, since_seq: int = 0) -> list[JournalEntry]:
        return self._store.read_stream(stream, limit=limit, since_seq=since_seq)

    def stream_length(self, stream: str) -> int:
        return self._store.stream_length(stream)

    def list_streams(self) -> list[str]:
        return self._store.list_streams()


class Replay:
    """只读回放视图：按序号翻帧、按时间定位、筛关键步。"""

    def __init__(self, store: ReadOnlyStore, namespace: Namespace) -> None:
        self._store = store
        self._namespace = namespace

    def length(self) -> int:
        return self._store.stream_length(FRAMES_STREAM)

    def overview(self) -> dict[str, Any]:
        frames = self._frames()
        marks = self._marks_by_frame()
        return {
            "stream": FRAMES_STREAM,
            "frames": len(frames),
            "first_at": frames[0].payload.get("at") if frames else None,
            "last_at": frames[-1].payload.get("at") if frames else None,
            "critical_steps": sum(1 for entry in frames if entry.payload.get("critical")),
            "marked_steps": len(marks),
            "marks": sum(len(items) for items in marks.values()),
            "readonly": True,
        }

    def steps(
        self,
        *,
        limit: int = 50,
        since_seq: int = 0,
        only_critical: bool = False,
    ) -> list[dict[str, Any]]:
        """时间轴列表：每一步一行摘要（动作、结果、各组件状态、关键标记）。"""

        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        marks = self._marks_by_frame()
        steps = [self._summarize(entry, marks) for entry in self._frames(since_seq=since_seq)]
        if only_critical:
            steps = [step for step in steps if step["critical"] or step["marks"]]
        return steps[-limit:]

    def critical(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """只看关键步：自动规则命中的，加上人工补标的。"""

        return self.steps(limit=limit, only_critical=True)

    def frame(self, seq: int) -> dict[str, Any]:
        """翻到指定一步，看当时的完整工况。"""

        seq = parse_frame_seq(seq)
        marks = self._marks_by_frame()
        for entry in self._frames():
            if entry.seq == seq:
                return self._detail(entry, marks)
        raise NotFoundError("回放帧不存在", details={"seq": seq, "frames": self.length()})

    def seek(self, when: str) -> dict[str, Any]:
        """按时间定位：返回该时刻正在生效的一帧（不晚于该时刻的最后一帧）。"""

        epoch = epoch_from_iso(when)
        chosen: JournalEntry | None = None
        for entry in self._frames():
            try:
                frame_epoch = epoch_from_iso(str(entry.payload.get("at", "")))
            except ValidationError:
                continue
            if frame_epoch <= epoch:
                chosen = entry
        if chosen is None:
            raise NotFoundError("该时刻之前还没有回放帧", details={"when": when})
        return self._detail(chosen, self._marks_by_frame())

    def marks(self) -> list[dict[str, Any]]:
        entries = self._store.read_stream(MARKS_STREAM, limit=_READ_LIMIT)
        return [self._mark_dict(entry) for entry in entries]

    # ------------------------------------------------------------------ 内部
    def _frames(self, *, since_seq: int = 0) -> list[JournalEntry]:
        return self._store.read_stream(FRAMES_STREAM, limit=_READ_LIMIT, since_seq=since_seq)

    def _summarize(self, entry: JournalEntry, marks: Mapping[int, list[dict[str, Any]]]) -> dict[str, Any]:
        payload = entry.payload
        trigger = payload.get("trigger", {})
        conditions = payload.get("conditions", {})
        return {
            "seq": entry.seq,
            "at": payload.get("at"),
            "action": trigger.get("qualified"),
            "actor": trigger.get("actor"),
            "outcome": trigger.get("outcome"),
            "audit_seq": trigger.get("audit_seq"),
            "critical": list(payload.get("critical", [])),
            "marks": list(marks.get(entry.seq, [])),
            "states": {
                name: condition.get("state")
                for name, condition in conditions.items()
                if isinstance(condition, Mapping)
            },
        }

    def _detail(self, entry: JournalEntry, marks: Mapping[int, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "seq": entry.seq,
            "written_at": entry.written_at,
            "checksum": entry.checksum,
            **dict(entry.payload),
            "marks": list(marks.get(entry.seq, [])),
        }

    def _marks_by_frame(self) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for entry in self._store.read_stream(MARKS_STREAM, limit=_READ_LIMIT):
            mark = self._mark_dict(entry)
            frame_seq = mark.get("frame_seq")
            if isinstance(frame_seq, int):
                grouped.setdefault(frame_seq, []).append(mark)
        return grouped

    @staticmethod
    def _mark_dict(entry: JournalEntry) -> dict[str, Any]:
        return {"seq": entry.seq, **dict(entry.payload)}


def parse_frame_seq(raw: Any) -> int:
    """把控制台/CLI 传入的帧序号收敛成正整数。"""

    try:
        seq = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError("帧序号必须是整数", details={"seq": repr(raw)}) from exc
    if seq < 1:
        raise ValidationError("帧序号必须是正整数", details={"seq": repr(raw)})
    return seq


__all__ = [
    "TimelineRecorder",
    "Replay",
    "ReadOnlyStore",
    "FRAMES_STREAM",
    "MARKS_STREAM",
    "LATCH_STATES",
    "evaluate_critical",
    "parse_frame_seq",
]
