from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, TransportError

from ..errors import PatreonArchiveError

API = "https://www.patreon.com/api"

# Pinned deliberately, and NOT matched to the browser the cookies came from.
# Patreon serves the app client a media surface it withholds from browsers;
# yt-dlp and gallery-dl both pin this for the same reason. It is a capability
# switch against the API, not a fingerprint dodge.
APP_UA = "Patreon/126.9.0.15 (Android; Android 14; Scale/2.10)"

MAX_RETRIES = 5


class ApiError(PatreonArchiveError):
    pass


@dataclass
class Collection:
    collection_id: str
    title: str | None
    num_posts: int
    post_ids: list[str]


class PatreonApi:
    """Thin JSON:API client.

    Rides yt-dlp's networking stack so it inherits its TLS handling and cookie
    jar rather than maintaining a second one.
    """

    def __init__(self, ydl: yt_dlp.YoutubeDL, *, request_sleep: float = 0.0):
        self.ydl = ydl
        self.request_sleep = request_sleep

    def get(self, path: str, query: dict[str, str] | None = None) -> dict:
        from urllib.parse import urlencode

        url = f"{API}/{path.lstrip('/')}"
        q = dict(query or {})
        q.setdefault("json-api-version", "1.0")
        url = f"{url}?{urlencode(q)}"

        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                req = Request(url, headers={
                    "User-Agent": APP_UA,
                    "Content-Type": "application/vnd.api+json",
                })
                body = self.ydl.urlopen(req).read().decode("utf-8", "replace")
                if self.request_sleep:
                    time.sleep(self.request_sleep)
                return json.loads(body)
            except HTTPError as exc:
                status = exc.status
                if status in (401, 403):
                    raise ApiError(
                        f"Patreon returned {status} for {path}. "
                        "The session is not entitled to this, or the cookies are stale."
                    ) from exc
                if status == 404:
                    raise ApiError(f"Patreon returned 404 for {path}.") from exc
                if status == 429 or 500 <= status < 600:
                    # Exponential backoff with jitter, honouring Retry-After.
                    # Nobody in the surveyed field does both.
                    retry_after = None
                    try:
                        retry_after = float(exc.response.headers.get("Retry-After") or 0) or None
                    except (TypeError, ValueError):
                        retry_after = None
                    delay = retry_after or min(60.0, (2 ** attempt)) + random.uniform(0, 0.5)
                    last = exc
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(delay)
                        continue
                raise ApiError(f"Patreon returned {status} for {path}.") from exc
            except TransportError as exc:
                last = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(min(30.0, 2 ** attempt) + random.uniform(0, 0.5))
                    continue
                raise ApiError(f"Network error calling {path}: {exc}") from exc
        raise ApiError(f"Gave up on {path} after {MAX_RETRIES} attempts: {last}")

    def collection(self, collection_id: str) -> Collection:
        """Collections are not supported by yt-dlp's PatreonCampaignIE at all.

        This endpoint hands back the full ordered post_ids list in one call, so
        no pagination is needed.
        """
        doc = self.get(f"collection/{collection_id}")
        attrs = (doc.get("data") or {}).get("attributes") or {}
        ids = [str(i) for i in (attrs.get("post_ids") or [])]
        if not ids:
            raise ApiError(
                f"Collection {collection_id} returned no post_ids. "
                "It may be empty, or not visible to this account."
            )
        return Collection(
            collection_id=str(collection_id),
            title=attrs.get("title") or attrs.get("name"),
            num_posts=int(attrs.get("num_posts") or len(ids)),
            post_ids=ids,
        )

    def current_user(self) -> dict:
        """Doubles as the liveness probe: 401 here means the session is dead."""
        return self.get("current_user", {
            "include": "active_memberships.campaign",
            "fields[campaign]": "name,vanity,url",
        })


def post_url(post_id: str) -> str:
    return f"https://www.patreon.com/posts/{post_id}"
