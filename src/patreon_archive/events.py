from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

# Progress is throttled so a long download does not emit thousands of lines an
# agent has to page through.
PROGRESS_INTERVAL = 2.0


_EMITTER: "Emitter | None" = None


def set_emitter(em: "Emitter") -> None:
    global _EMITTER
    _EMITTER = em


def ev() -> "Emitter":
    """The active emitter. Disabled no-op emitter until main() installs one."""
    global _EMITTER
    if _EMITTER is None:
        _EMITTER = Emitter(False)
    return _EMITTER


class Emitter:
    """Structured JSONL output for non-human consumers.

    One JSON object per line on stdout, flushed immediately, so a caller running
    this as a background task can tail the stream and react. Human-readable
    chatter goes to stderr instead, keeping stdout a clean machine channel.
    """

    def __init__(self, enabled: bool, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()
        self._last: dict[Any, float] = {}

    def emit(self, event: str, **fields) -> None:
        if not self.enabled:
            return
        rec = {"event": event, "ts": round(time.time(), 3), **fields}
        with self.lock:
            self.stream.write(json.dumps(rec, default=str) + "\n")
            self.stream.flush()

    def progress(self, key, **fields) -> None:
        """Throttled per-key progress."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._last.get(key, 0.0) < PROGRESS_INTERVAL:
            return
        self._last[key] = now
        self.emit("progress", **fields)

    def hook(self, index: int, post_id: str):
        """yt-dlp progress_hook that emits throttled structured progress."""
        def _hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                self.progress(
                    post_id,
                    index=index, post_id=post_id,
                    downloaded_bytes=done, total_bytes=total,
                    percent=round(done / total * 100, 1) if total else None,
                    speed_bps=d.get("speed"), eta_s=d.get("eta"),
                    fragment=d.get("fragment_index"),
                    fragment_count=d.get("fragment_count"),
                )
            elif status == "finished":
                self.emit("post_downloaded", index=index, post_id=post_id,
                          note="muxing")
            elif status == "error":
                self.emit("post_error", index=index, post_id=post_id)
        return _hook
