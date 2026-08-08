"""Error taxonomy. The four outcomes are the spine of the state model."""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    COMPLETE = "complete"
    NO_ACCESS = "no_access"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


class PatreonArchiveError(Exception):
    pass


class PreflightError(PatreonArchiveError):
    """Environment or session is not usable. Raised before any network work."""


class SessionDeadError(PreflightError):
    """Cookies are absent, expired, or rejected."""


class NoAccessError(PatreonArchiveError):
    """The authenticated account is not entitled to view this post.

    A hard gate, never something to work around.
    """


class IndeterminateError(PatreonArchiveError):
    """Extraction produced no media but we cannot conclude the post has none.

    yt-dlp's embed preflight runs with fatal=False and errnote=False, so a
    rate-limited or 403'd preflight is indistinguishable from an empty post at
    the API surface. Treating that as "empty" silently loses content, so it is
    recorded as a distinct outcome and retried on the next run.
    """
