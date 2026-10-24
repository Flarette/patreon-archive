"""Collection entries sit flat in <dest>/<collection>/, and resume spans URL kinds."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from patreon_archive.api.client import PatreonApi

COLL = "https://www.patreon.com/collection/77"


def post_url(pid: str) -> str:
    return f"https://www.patreon.com/posts/{pid}"


@pytest.fixture
def coll(patreon):
    patreon.post("101", "Alpha", media="a")
    patreon.post("102", "Beta", media="b")
    patreon.collection("77", "My Coll", [("101", "Alpha"), ("102", "Beta")])
    return patreon


def test_collection_lands_flat_in_its_folder(coll):
    r = coll.run(COLL, "-i", "1-2")

    assert r.rc == 0, r
    assert r.files == ["My Coll/Alpha.mp4", "My Coll/Beta.mp4"]


def test_single_post_in_a_collection_lands_in_its_folder(coll):
    r = coll.run(post_url("101"))

    assert r.rc == 0, r
    assert r.files == ["My Coll/Alpha.mp4"]


def test_single_post_in_no_collection_lands_under_creator(patreon):
    patreon.post("301", "Loose")

    r = patreon.run(post_url("301"))

    assert r.rc == 0, r
    assert r.files == ["Creator/Loose.mp4"]


def test_failed_collection_lookup_falls_back_to_creator(coll):
    coll.lookup_fails.add("101")

    r = coll.run(post_url("101"))

    assert r.rc == 0, r
    assert r.files == ["Creator/Alpha.mp4"]
    assert "collection lookup failed" in r.err


@pytest.mark.parametrize("args", [(COLL, "-i", "1"), (post_url("101"),)],
                         ids=["collection", "single"])
def test_keep_id_appends_the_post_id(coll, args):
    r = coll.run(*args, "--keep-id")

    assert r.rc == 0, r
    assert r.files == ["My Coll/Alpha [101].mp4"]


def test_explicit_outtmpl_is_left_untouched(coll):
    r = coll.run(COLL, "-i", "1", "--outtmpl", "%(channel)s/%(id)s.%(ext)s")

    assert r.rc == 0, r
    assert r.files == ["My Coll/Creator/101.mp4"]


def test_multi_video_post_with_keep_id_gets_one_file_per_video(patreon):
    patreon.post("401", "Pair", videos=["a", "b"])

    r = patreon.run(post_url("401"), "--keep-id")

    assert r.files == ["Creator/Pair [401-1].mp4", "Creator/Pair [401-2].mp4"]


def test_single_download_resumes_in_a_later_collection_run(coll):
    first = coll.run(post_url("101"))
    assert first.rc == 0, first
    assert coll.extract_calls == ["101"]

    r = coll.run(COLL, "-i", "1-2")

    assert r.rc == 0, r
    assert "skip 101: already complete" in r.out
    assert coll.extract_calls == ["101", "102"]
    assert r.files == ["My Coll/Alpha.mp4", "My Coll/Beta.mp4"]


class _Ydl:
    """Answers PatreonApi's request with one canned JSON:API document."""

    def __init__(self, doc: dict):
        self.body = json.dumps(doc).encode()
        self.urls: list[str] = []

    def urlopen(self, req):
        self.urls.append(req.url)
        body = self.body

        class Response:
            def read(self):
                return body

        return Response()


def test_post_collections_pairs_each_collection_with_its_title():
    ydl = _Ydl({
        "data": {"id": "101", "type": "post", "relationships": {"collections": {"data": [
            {"id": "77", "type": "collection"}, {"id": "78", "type": "collection"},
        ]}}},
        "included": [{"id": "77", "type": "collection", "attributes": {"title": "My Coll"}}],
    })

    assert PatreonApi(ydl).post_collections("101") == [("77", "My Coll"), ("78", None)]
    [url] = ydl.urls
    assert urlsplit(url).path.endswith("/posts/101")
    assert parse_qs(urlsplit(url).query)["include"] == ["collections"]
