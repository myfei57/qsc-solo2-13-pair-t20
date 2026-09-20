"""文件型持久化包。"""

from __future__ import annotations

from .durable import DurableStore, JournalEntry, Record
from .readonly import ReadOnlyStore

__all__ = ["DurableStore", "Record", "JournalEntry", "ReadOnlyStore"]
