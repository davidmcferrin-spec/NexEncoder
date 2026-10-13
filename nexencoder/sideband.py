"""Recent caption and splice notes for the monitor page.

The console keeps these in memory. A capture helper posts them; the page polls.
SCTE totals only increase, so the Prometheus counter does not go backwards.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class Sideband:
    def __init__(self, limit: int = 200) -> None:
        self._events: deque[dict] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.scte_totals = {"A": 0, "B": 0}
        self.caption_at = {"A": 0.0, "B": 0.0}

    def add(self, event: dict, at: float | None = None) -> dict:
        channel = event.get("channel")
        kind = event.get("kind")
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        if kind not in ("caption", "scte104", "scte35"):
            raise ValueError("kind must be caption, scte104, or scte35")
        text = str(event.get("text") or "")[:500]
        label = str(event.get("label") or kind)[:32]
        row = {"channel": channel, "kind": kind, "label": label, "text": text, "at": time.time() if at is None else at}
        with self._lock:
            self._events.append(row)
            if kind in ("scte104", "scte35"):
                self.scte_totals[channel] += 1
            if kind == "caption" and text:
                self.caption_at[channel] = row["at"]
        return row

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self._events)
