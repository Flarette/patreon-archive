from __future__ import annotations

from .errors import PatreonArchiveError


class SelectionError(PatreonArchiveError):
    pass


def parse_items(spec: str, total: int, exclude: str | None = None) -> list[int]:
    """Parse a yt-dlp style item spec into 1-based indices, order preserved.

    Accepts comma-separated singles, ranges, and negatives counting from the end:
        "1,3,5"     -> [1, 3, 5]
        "2-6"       -> [2, 3, 4, 5, 6]
        "-3"        -> [total - 2]          (third from last)
        "5-"        -> [5 .. total]
        "-4--2"     -> the 4th-from-last through 2nd-from-last
        "3,7-9,-1"  -> combinations

    `exclude` takes the same syntax and is subtracted from the result. It is a
    separate argument rather than an inline marker because every bracket-ish
    spelling is shell syntax: '(2-3)' errors unquoted, '{2-3,7}' silently
    brace-expands in bash and zsh, '^' is a zsh glob operator.

    An empty `spec` with a non-empty `exclude` means "everything but those".
    Duplicates are removed, first occurrence wins.
    """
    include_toks = _split_tokens(spec) if spec and spec.strip() else []
    exclude_toks = _split_tokens(exclude) if exclude and exclude.strip() else []

    def resolve(tok: str) -> int:
        n = int(tok)
        if n == 0:
            raise SelectionError("item indices are 1-based; 0 is not valid")
        return total + 1 + n if n < 0 else n

    def expand(tokens: list[str]) -> list[int]:
        out: list[int] = []
        for part in tokens:
            try:
                lo_s, sep, hi_s = _split_range(part)
                if not sep:
                    out.append(resolve(lo_s))
                    continue
                lo = resolve(lo_s) if lo_s else 1
                hi = resolve(hi_s) if hi_s else total
                step = 1 if hi >= lo else -1
                out.extend(range(lo, hi + step, step))
            except ValueError as exc:
                raise SelectionError(f"cannot parse item spec {part!r}") from exc
        return out

    chosen = expand(include_toks) if include_toks else list(range(1, total + 1))
    dropped = set(expand(exclude_toks))

    seen: set[int] = set()
    picked: list[int] = []
    for i in chosen:
        if i < 1 or i > total:
            raise SelectionError(f"item {i} is out of range (collection has {total})")
        if i in dropped or i in seen:
            continue
        seen.add(i)
        picked.append(i)
    if not picked:
        raise SelectionError(
            f"item spec {spec!r} with exclusions {exclude!r} selected nothing"
            if exclude else f"item spec {spec!r} selected nothing"
        )
    return picked


def _split_tokens(spec: str) -> list[str]:
    return [t.strip() for t in spec.split(",") if t.strip()]


def _split_range(part: str) -> tuple[str, str, str]:
    """Split on the range separator, tolerating negative endpoints.

    A leading '-' is a sign, not a separator, so scanning starts at index 1.
    """
    for i in range(1, len(part)):
        if part[i] == "-":
            return part[:i], "-", part[i + 1:]
    return part, "", ""
