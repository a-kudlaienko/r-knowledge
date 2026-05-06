"""Byte-bounded LRU cache.

The plan calls for a size-bounded cache holding the embedding model, hot
chunks, and recent search results. In the current CLI model each invocation
is a short-lived Python process, so most of what a cache would hold is
either (a) already held sticky by module-level singletons (embedder.py)
or (b) gone at process exit. The LRU shines in a future daemon mode
where many queries share a process.

This module exists so that daemon mode is a one-file rewire away rather
than a design change. For now it's tested in isolation but not wired into
the hot path.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable


class ByteBoundedLRU:
    """Ordered map that evicts least-recently-used entries over a byte budget.

    Callers supply a ``sizer`` callable to measure each value's memory
    footprint. Default is ``len`` (works for bytes / str).
    """

    def __init__(
        self,
        budget_bytes: int,
        sizer: Callable[[Any], int] = len,
    ) -> None:
        self._budget = budget_bytes
        self._sizer = sizer
        self._data: OrderedDict[Any, tuple[Any, int]] = OrderedDict()
        self._total = 0
        self._hits = 0
        self._misses = 0

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            self._misses += 1
            return default
        self._hits += 1
        self._data.move_to_end(key)
        return self._data[key][0]

    def put(self, key: Any, value: Any) -> None:
        size = self._sizer(value)
        if key in self._data:
            _old_value, old_size = self._data[key]
            self._total -= old_size
        self._data[key] = (value, size)
        self._data.move_to_end(key)
        self._total += size
        self._evict()

    def clear(self) -> None:
        self._data.clear()
        self._total = 0

    def stats(self) -> dict[str, float]:
        total_lookups = self._hits + self._misses
        hit_rate = (self._hits / total_lookups) if total_lookups else 0.0
        return {
            "entries": len(self._data),
            "bytes": self._total,
            "budget": self._budget,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 3),
        }

    def _evict(self) -> None:
        while self._total > self._budget and self._data:
            _key, (_value, size) = self._data.popitem(last=False)
            self._total -= size
