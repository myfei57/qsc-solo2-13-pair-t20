"""只读存储视图。

事故复盘与历史回放只能「看」现场数据，绝不能借回放通道反向写入。本模块把
:class:`DurableStore` 包一层，只暴露读取方法；任何写方法（``put``、
``commit_intent``、``append``）一调用就抛 :class:`ReadOnlyError`。这样「回放
不改现场」靠结构保证，而不是靠调用方自觉。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ReadOnlyError
from .durable import DurableStore, IntegrityReport, JournalEntry, Record


class ReadOnlyStore:
    """``DurableStore`` 的只读门面：读方法原样代理，写方法一律拒绝。"""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ 只读代理
    def get(self, key: str) -> Record | None:
        return self._store.get(key)

    def require(self, key: str) -> Record:
        return self._store.require(key)

    def exists(self, key: str) -> bool:
        return self._store.exists(key)

    def list_keys(self, prefix: str = "") -> list[str]:
        return self._store.list_keys(prefix)

    def snapshot(self, prefix: str = "") -> dict[str, Mapping[str, Any]]:
        return self._store.snapshot(prefix)

    def versions(self, prefix: str = "") -> dict[str, int]:
        return self._store.versions(prefix)

    def read_stream(
        self,
        stream: str,
        *,
        limit: int = 100,
        since_seq: int = 0,
        verify: bool = True,
    ) -> list[JournalEntry]:
        return self._store.read_stream(stream, limit=limit, since_seq=since_seq, verify=verify)

    def stream_length(self, stream: str) -> int:
        return self._store.stream_length(stream)

    def list_streams(self) -> list[str]:
        return self._store.list_streams()

    def verify(self) -> IntegrityReport:
        return self._store.verify()

    # ------------------------------------------------------------------ 写禁
    def put(self, key: str, payload: Mapping[str, Any]) -> Record:
        raise ReadOnlyError("只读回放视图禁止写入文档", details={"key": key})

    def commit_intent(self, key: str, payload: Mapping[str, Any]) -> Record:
        raise ReadOnlyError("只读回放视图禁止写入工艺意图", details={"key": key})

    def append(self, stream: str, payload: Mapping[str, Any]) -> JournalEntry:
        raise ReadOnlyError("只读回放视图禁止追加流水", details={"stream": stream})


__all__ = ["ReadOnlyStore"]
