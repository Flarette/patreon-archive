from __future__ import annotations

import shutil
import sys
import threading


def _human(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


class MultilineProgress:
    """One live line per concurrent download, redrawn in place.

    yt-dlp's own progress writes a single carriage-returned line, which is
    unreadable when several downloads run at once -- they overwrite each other.
    This reserves a fixed block of lines and repaints it, so each worker owns a
    row. Falls back to plain appended lines when stderr is not a TTY, so logs
    and pipes stay readable.
    """

    def __init__(self, slots: int, *, stream=None):
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty()
        self.slots = slots
        self.rows: list[str] = [""] * slots
        self.lock = threading.Lock()
        self.started = False

    def __enter__(self):
        if self.tty and self.slots:
            with self.lock:
                self.stream.write("\n" * self.slots)
                self.stream.flush()
                self.started = True
        return self

    def __exit__(self, *exc):
        if self.started:
            with self.lock:
                self._repaint()
                self.stream.write("\n")
                self.stream.flush()
        return False

    def _repaint(self) -> None:
        width = shutil.get_terminal_size((100, 24)).columns - 1
        self.stream.write(f"\033[{self.slots}A")
        for row in self.rows:
            self.stream.write("\033[2K" + row[:width] + "\n")
        self.stream.flush()

    def set(self, slot: int, text: str) -> None:
        if slot >= self.slots:
            return
        with self.lock:
            self.rows[slot] = text
            if self.started:
                self._repaint()

    def log(self, text: str) -> None:
        """Emit a permanent line above the live block."""
        with self.lock:
            if self.started:
                self.stream.write(f"\033[{self.slots}A\033[2K{text}\n")
                for row in self.rows:
                    self.stream.write("\033[2K" + row + "\n")
            else:
                self.stream.write(text + "\n")
            self.stream.flush()

    def hook(self, slot: int, label: str):
        """Build a yt-dlp progress_hook bound to one slot."""
        def _hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                pct = f"{done / total * 100:5.1f}%" if total else "  ?  "
                speed = d.get("speed")
                eta = d.get("eta")
                frag = ""
                if d.get("fragment_index") and d.get("fragment_count"):
                    frag = f" frag {d['fragment_index']}/{d['fragment_count']}"
                self.set(
                    slot,
                    f"  {label:38.38} {pct} {_human(done)}/{_human(total)} "
                    f"{_human(speed)}/s ETA {int(eta) if eta else '?'}s{frag}",
                )
            elif status == "finished":
                self.set(slot, f"  {label:38.38} downloaded, muxing…")
            elif status == "error":
                self.set(slot, f"  {label:38.38} error")
        return _hook
