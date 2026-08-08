from __future__ import annotations

from .errors import PatreonArchiveError


class SelectionError(PatreonArchiveError):
    pass


def parse_items(spec: str, total: int) -> list[int]:
    """Parse a yt-dlp style item spec into 1-based indices, order preserved.

    Accepts comma-separated singles, ranges, and negatives counting from the end:
        "1,3,5"     -> [1, 3, 5]
        "2-6"       -> [2, 3, 4, 5, 6]
        "-3"        -> [total - 2]          (third from last)
        "5-"        -> [5 .. total]
        "-4--2"     -> the 4th-from-last through 2nd-from-last
        "3,7-9,-1"  -> combinations

    Duplicates are removed, first occurrence wins.
    """
    if not spec or not spec.strip():
        return list(range(1, total + 1))

    def resolve(tok: str) -> int:
        n = int(tok)
        if n == 0:
            raise SelectionError("item indices are 1-based; 0 is not valid")
        return total + 1 + n if n < 0 else n

    out: list[int] = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            lo_s, sep, hi_s = _split_range(part)
            if not sep:
                idx = resolve(lo_s)
                out.append(idx)
                continue
            lo = resolve(lo_s) if lo_s else 1
            hi = resolve(hi_s) if hi_s else total
            step = 1 if hi >= lo else -1
            out.extend(range(lo, hi + step, step))
        except ValueError as exc:
            raise SelectionError(f"cannot parse item spec {part!r}") from exc

    seen: set[int] = set()
    picked: list[int] = []
    for i in out:
        if i < 1 or i > total:
            raise SelectionError(f"item {i} is out of range (collection has {total})")
        if i not in seen:
            seen.add(i)
            picked.append(i)
    if not picked:
        raise SelectionError(f"item spec {spec!r} selected nothing")
    return picked


def _split_range(part: str) -> tuple[str, str, str]:
    """Split on the range separator, tolerating negative endpoints.

    A leading '-' is a sign, not a separator, so scanning starts at index 1.
    """
    for i in range(1, len(part)):
        if part[i] == "-":
            return part[:i], "-", part[i + 1:]
    return part, "", ""
