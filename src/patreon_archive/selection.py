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

    A parenthesised token is subtracted from the set instead of added:
        "1-,(2-3)"  -> everything except 2 and 3
        "(5)"       -> everything except 5 (no includes means "all")

    Duplicates are removed, first occurrence wins. `exclude` is the same syntax
    without the parens, for shells where '(' would need quoting.
    """
    if not spec or not spec.strip():
        include_toks, exclude_toks = [], []
    else:
        include_toks, exclude_toks = _classify(spec)

    if exclude:
        # -x takes the same syntax without parens, for shells where '(' bites.
        inc, exc = _classify(exclude)
        exclude_toks.extend(inc)
        exclude_toks.extend(exc)

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

    # No include tokens at all means "everything", so '(2-3)' alone reads as
    # "all but 2 and 3" rather than as an empty selection.
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
        raise SelectionError(f"item spec {spec!r} selected nothing")
    return picked


def _split_tokens(spec: str) -> list[str]:
    """Split on commas that are not inside parens, so '(2-3,7)' stays one token."""
    out: list[str] = []
    depth = 0
    buf = ""
    for ch in spec:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise SelectionError(f"unbalanced ')' in item spec {spec!r}")
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
            continue
        buf += ch
    if depth:
        raise SelectionError(f"unbalanced '(' in item spec {spec!r}")
    out.append(buf)
    return [t.strip() for t in out if t.strip()]


def _classify(spec: str) -> tuple[list[str], list[str]]:
    """Separate include tokens from parenthesised exclusion tokens."""
    include: list[str] = []
    exclude: list[str] = []
    for tok in _split_tokens(spec):
        if tok.startswith("(") and tok.endswith(")"):
            # A paren group may hold several comma-separated ranges of its own.
            exclude.extend(_split_tokens(tok[1:-1]))
        elif tok[:1] in ("x", "X"):
            # Letters are the only prefix no shell rewrites: '(' is syntax,
            # '{2-3,7}' silently brace-expands, '^' is a zsh glob operator.
            exclude.append(tok[1:])
        else:
            include.append(tok)
    return include, exclude


def _split_range(part: str) -> tuple[str, str, str]:
    """Split on the range separator, tolerating negative endpoints.

    A leading '-' is a sign, not a separator, so scanning starts at index 1.
    """
    for i in range(1, len(part)):
        if part[i] == "-":
            return part[:i], "-", part[i + 1:]
    return part, "", ""
