"""A post is complete only when every video left real, non-empty media on disk."""

from __future__ import annotations

import pytest

from patreon_archive.errors import Outcome

MISSING = "extraction reported success but no output file was produced"


def post_url(pid: str) -> str:
    return f"https://www.patreon.com/posts/{pid}"


def reported(path):
    """An extraction that claims `path` as its download without writing it."""
    def extract(ydl, info, download):
        return {**info, "requested_downloads": [{"filepath": str(path)}]}
    return extract


@pytest.mark.parametrize(("name", "content", "rc", "outcome", "detail"), [
    ("gone.mp4", None, 3, Outcome.INDETERMINATE, MISSING),
    ("index.m3u8", "#EXTM3U\n", 1, Outcome.FAILED,
     "output is a bare manifest, not muxed media: index.m3u8"),
    ("empty.mp4", "", 1, Outcome.FAILED, "output file is empty"),
], ids=["missing", "manifest", "empty"])
def test_output_that_is_not_media_is_not_complete(patreon, tmp_path, name, content,
                                                  rc, outcome, detail):
    path = tmp_path / name
    if content is not None:
        path.write_text(content)
    patreon.post("9101", "Clip")
    patreon.on_extract["9101"] = reported(path)

    r = patreon.run(post_url("9101"))

    assert r.rc == rc, r
    row = patreon.store().get_post("9101")
    assert (row["outcome"], row["outcome_detail"]) == (outcome, detail)


def test_multi_video_post_takes_its_worst_video_outcome(patreon, tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_text("")
    patreon.post("9102", "Pair", videos=["a", "b"])

    def extract(ydl, info, download):
        gone, failed = info["entries"]
        gone["requested_downloads"] = [{"filepath": str(tmp_path / "gone.mp4")}]
        failed["requested_downloads"] = [{"filepath": str(empty)}]
        return info

    patreon.on_extract["9102"] = extract

    r = patreon.run(post_url("9102"))

    assert r.rc == 1, r
    row = patreon.store().get_post("9102")
    assert (row["outcome"], row["outcome_detail"]) == (
        Outcome.FAILED, "9102-2: output file is empty")
