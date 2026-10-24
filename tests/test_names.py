"""R4, D1-D3, D5, D10: same-title posts keep distinct files; a multi-video post is whole."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from patreon_archive.errors import Outcome

COLL = "https://www.patreon.com/collection/88"


def post_url(pid: str) -> str:
    return f"https://www.patreon.com/posts/{pid}"


def date_tag(path) -> str:
    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(probe.stdout)["format"]["tags"]["date"]


def media_rows(patreon, pid: str) -> list[tuple[str | None, str | None]]:
    """(media_id, output_path relative to the destination) per recorded video."""
    rows = patreon.store().media_for(pid)
    return sorted(
        (r["media_id"],
         r["output_path"] and Path(r["output_path"]).relative_to(patreon.out).as_posix())
        for r in rows
    )


@pytest.fixture
def pair(patreon):
    """Two posts sharing a title, with different media and dates."""
    patreon.post("2001", "Live Q&A", media="a", upload_date="20240101")
    patreon.post("2002", "Live Q&A", media="b", upload_date="20240202")
    patreon.collection("88", "Talks", [("2001", "Live Q&A"), ("2002", "Live Q&A")])
    return patreon


def test_same_title_posts_get_their_own_files(pair):
    r = pair.run(COLL, "-i", "1-2")

    assert r.rc == 0, r
    assert r.files == ["Talks/Live Q&A [2002].mp4", "Talks/Live Q&A.mp4"]
    assert media_rows(pair, "2001") == [("2001", "Talks/Live Q&A.mp4")]
    assert media_rows(pair, "2002") == [("2002", "Talks/Live Q&A [2002].mp4")]
    assert date_tag(pair.out / "Talks/Live Q&A.mp4") == "20240101"
    assert date_tag(pair.out / "Talks/Live Q&A [2002].mp4") == "20240202"


def test_same_title_posts_get_their_own_files_in_parallel(pair):
    r = pair.run(COLL, "-i", "1-2", "-j", "2")

    assert r.rc == 0, r
    # Which post wins the title-only name is a race; that each gets its own is not.
    owned = {pid: media_rows(pair, pid) for pid in ("2001", "2002")}
    suffixed = next(pid for pid, rows in owned.items()
                    if rows == [(pid, f"Talks/Live Q&A [{pid}].mp4")])
    plain = "2002" if suffixed == "2001" else "2001"
    assert owned[plain] == [(plain, "Talks/Live Q&A.mp4")]
    assert r.files == sorted(["Talks/Live Q&A.mp4", f"Talks/Live Q&A [{suffixed}].mp4"])
    dates = {"2001": "20240101", "2002": "20240202"}
    assert date_tag(pair.out / "Talks/Live Q&A.mp4") == dates[plain]
    assert date_tag(pair.out / f"Talks/Live Q&A [{suffixed}].mp4") == dates[suffixed]


def test_rerun_of_same_title_posts_skips_both(pair):
    first = pair.run(COLL, "-i", "1-2")
    assert first.rc == 0, first
    before = {f: (pair.out / f).read_bytes() for f in first.files}

    r = pair.run(COLL, "-i", "1-2")

    assert r.rc == 0, r
    assert "skip 2001: already complete" in r.out
    assert "skip 2002: already complete" in r.out
    assert pair.extract_calls == ["2001", "2002"]
    assert {f: (pair.out / f).read_bytes() for f in r.files} == before


def test_unrecorded_file_on_disk_takes_the_name(patreon, media):
    (patreon.out / "Creator").mkdir(parents=True)
    stranger = patreon.out / "Creator/Clip.mp4"
    shutil.copyfile(media["c"], stranger)
    patreon.post("501", "Clip")

    r = patreon.run(post_url("501"))

    assert r.rc == 0, r
    assert r.files == ["Creator/Clip [501].mp4", "Creator/Clip.mp4"]
    assert stranger.read_bytes() == media["c"].read_bytes()
    assert media_rows(patreon, "501") == [("501", "Creator/Clip [501].mp4")]


def test_multi_video_post_is_one_complete_post(patreon):
    patreon.post("3001", "Episode 5", videos=["a", "b"])

    r = patreon.run(post_url("3001"))

    assert r.rc == 0, r
    assert r.files == ["Creator/Episode 5 [3001-2].mp4", "Creator/Episode 5.mp4"]
    store = patreon.store()
    assert store.get_post("3001")["outcome"] == Outcome.COMPLETE
    assert store.get_post("3001-1") is None
    assert media_rows(patreon, "3001") == [("3001-1", "Creator/Episode 5.mp4"),
                                           ("3001-2", "Creator/Episode 5 [3001-2].mp4")]
    assert patreon.run("status", "--post", "3001").rc == 0

    again = patreon.run(post_url("3001"))

    assert again.rc == 0, again
    assert "skip 3001: already complete" in again.out
    assert patreon.extract_calls == ["3001"]


def test_multi_video_post_with_a_failed_video_resumes(patreon, tmp_path):
    patreon.post("3002", "Episode 6", videos=["a", "b"])

    def second_is_missing(ydl, info, download):
        info["entries"][1]["url"] = (tmp_path / "missing.mp4").as_uri()
        return ydl.process_ie_result(info, download=download)

    patreon.on_extract["3002"] = second_is_missing

    first = patreon.run(post_url("3002"))

    assert first.rc == 1, first
    assert patreon.store().get_post("3002")["outcome"] == Outcome.FAILED
    # The video that finished is recorded, so a re-run does not take its name.
    assert media_rows(patreon, "3002") == [("3002-1", "Creator/Episode 6.mp4")]
    assert first.files == ["Creator/Episode 6.mp4"]

    del patreon.on_extract["3002"]
    r = patreon.run(post_url("3002"))

    assert r.rc == 0, r
    assert patreon.extract_calls == ["3002", "3002"]
    assert r.files == ["Creator/Episode 6 [3002-2].mp4", "Creator/Episode 6.mp4"]
    assert patreon.store().get_post("3002")["outcome"] == Outcome.COMPLETE
    assert media_rows(patreon, "3002") == [("3002-1", "Creator/Episode 6.mp4"),
                                           ("3002-2", "Creator/Episode 6 [3002-2].mp4")]


@pytest.mark.parametrize(("args", "name"), [
    (("--keep-id",), "Creator/Clip [501].mp4"),
    (("--outtmpl", "%(channel)s/%(title)s.%(ext)s"), "Creator/Clip.mp4"),
], ids=["keep-id", "outtmpl"])
def test_explicit_templates_never_get_a_suffix(patreon, media, args, name):
    # The name is already on disk and unrecorded: the default template would
    # suffix it, these must not.
    (patreon.out / "Creator").mkdir(parents=True)
    shutil.copyfile(media["c"], patreon.out / name)
    patreon.post("501", "Clip")

    r = patreon.run(post_url("501"), *args)

    assert r.rc == 0, r
    assert r.files == [name]


def test_legacy_row_keeps_its_name_and_is_replaced(patreon, media):
    (patreon.out / "Creator").mkdir(parents=True)
    path = patreon.out / "Creator/Legacy.mp4"
    shutil.copyfile(media["a"], path)
    patreon.post("601", "Legacy")
    # As a pre-0.4 run wrote it: no media id, keyed by its output path.
    store = patreon.store()
    store.upsert_post(post_id="601", outcome=Outcome.COMPLETE, tool_version="0.3.0",
                      ytdlp_version="2026.07.04", title="Legacy")
    store.record_media(post_id="601", source_kind="ytdlp_post_url", state="complete",
                       output_path=str(path))

    r = patreon.run(post_url("601"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Legacy.mp4"]
    rows = patreon.store().media_for("601")
    assert [(m["media_id"], m["dedup_key"]) for m in rows] == [("601", "601")]
