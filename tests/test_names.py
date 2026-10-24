"""Same-title posts keep distinct files; a multi-video post is whole."""

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


def legacy_row(store, pid: str, title: str, path: Path) -> None:
    """Record `pid` as a pre-0.4 run did: no media id, keyed by its output path."""
    store.upsert_post(post_id=pid, outcome=Outcome.COMPLETE, tool_version="0.3.0",
                      ytdlp_version="2026.07.04", title=title)
    store.record_media(post_id=pid, source_kind="ytdlp_post_url", state="complete",
                       output_path=str(path))


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

    r = patreon.run(post_url("3001"), "--agent")

    assert r.rc == 0, r
    assert r.files == ["Creator/Episode 5 [3001-2].mp4", "Creator/Episode 5.mp4"]
    events = [json.loads(line) for line in r.out.splitlines()]
    assert [e["path"] for e in events if e["event"] == "post_done"] == [
        str(patreon.out / "Creator/Episode 5.mp4")]
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
    """The video that finished is recorded, so the re-run keeps its name."""
    patreon.post("3002", "Episode 6", videos=["a", "b"])

    def second_is_missing(ydl, info, download):
        info["entries"][1]["url"] = (tmp_path / "missing.mp4").as_uri()
        return ydl.process_ie_result(info, download=download)

    patreon.on_extract["3002"] = second_is_missing

    first = patreon.run(post_url("3002"))

    assert first.rc == 1, first
    assert patreon.store().get_post("3002")["outcome"] == Outcome.FAILED
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
    """The name is on disk and unrecorded: the default template would suffix it."""
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
    legacy_row(patreon.store(), "601", "Legacy", path)

    r = patreon.run(post_url("601"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Legacy.mp4"]
    rows = patreon.store().media_for("601")
    assert [(m["media_id"], m["dedup_key"]) for m in rows] == [("601", "601")]


@pytest.fixture
def legacy_pair(patreon, media) -> Path:
    """Before 0.4, a second same-title post found the name on disk and recorded it
    as its own. The file holds the first post's download, so the first row owns it.
    The ids run opposite to the recording order.
    """
    (patreon.out / "Creator").mkdir(parents=True)
    path = patreon.out / "Creator/Live.mp4"
    shutil.copyfile(media["a"], path)
    patreon.post("2202", "Live", media="a")
    patreon.post("2201", "Live", media="b")
    store = patreon.store()
    legacy_row(store, "2202", "Live", path)
    legacy_row(store, "2201", "Live", path)
    return path


def test_first_recorded_legacy_owner_keeps_the_shared_name(patreon, legacy_pair):
    r = patreon.run(post_url("2202"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Live.mp4"]
    assert media_rows(patreon, "2202") == [("2202", "Creator/Live.mp4")]


def test_second_recorded_legacy_owner_gets_its_own_file(patreon, media, legacy_pair):
    r = patreon.run(post_url("2201"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Live [2201].mp4", "Creator/Live.mp4"]
    assert legacy_pair.read_bytes() == media["a"].read_bytes()


def test_rerecorded_first_owner_keeps_the_name_from_the_legacy_row(patreon, media, legacy_pair):
    """Re-recording upgrades the first owner's legacy row in place, so it keeps
    its rank over the second post's legacy row for the same file."""
    assert patreon.run(post_url("2202"), "--force").rc == 0

    r = patreon.run(post_url("2201"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Live [2201].mp4", "Creator/Live.mp4"]
    assert media_rows(patreon, "2201") == [("2201", "Creator/Live [2201].mp4")]


CUSTOM = ("--outtmpl", "%(channel)s/%(title)s.%(ext)s")


def test_custom_template_reuse_does_not_take_a_legacy_owners_file(patreon, legacy_pair):
    """A user template never claims a name, so yt-dlp's reuse of the shared file
    records it without outranking the post whose download the file holds."""
    assert patreon.run(post_url("2201"), *CUSTOM, "--force").rc == 0

    r = patreon.run(post_url("2202"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Live.mp4"]
    assert media_rows(patreon, "2202") == [("2202", "Creator/Live.mp4")]


def test_custom_template_reuse_does_not_take_a_lone_legacy_file(patreon, media):
    (patreon.out / "Creator").mkdir(parents=True)
    path = patreon.out / "Creator/Live.mp4"
    shutil.copyfile(media["a"], path)
    patreon.post("2202", "Live", media="a")
    patreon.post("2201", "Live", media="b")
    legacy_row(patreon.store(), "2202", "Live", path)
    assert patreon.run(post_url("2201"), *CUSTOM).rc == 0

    r = patreon.run(post_url("2202"), "--force")

    assert r.rc == 0, r
    assert r.files == ["Creator/Live.mp4"]


def test_long_creator_and_title_keep_the_suffix(patreon):
    # Past yt-dlp's 180-character trim of the rendered path, which would cut the
    # suffix off and send both posts to one file.
    creator, title = "C" * 120, "T" * 160
    patreon.post("2001", title, channel=creator, upload_date="20240101")
    patreon.post("2002", title, channel=creator, upload_date="20240202")

    first = patreon.run(post_url("2001"))
    r = patreon.run(post_url("2002"))

    assert first.rc == 0, first
    assert r.rc == 0, r
    folder, name = creator[:100], title[:150]
    assert r.files == [f"{folder}/{name} [2002].mp4", f"{folder}/{name}.mp4"]
    assert date_tag(patreon.out / folder / f"{name}.mp4") == "20240101"


def test_user_template_keeps_ytdlps_trim(patreon):
    patreon.post("2101", "T" * 200)

    r = patreon.run(post_url("2101"), "--outtmpl", "%(title)s.%(ext)s")

    assert r.rc == 0, r
    assert r.files == ["T" * 180 + ".mp4"]


def test_taken_suffixed_name_fails_the_video(patreon):
    patreon.post("1", "Clip", media="a")
    patreon.post("3", "Clip [2]", media="c")
    patreon.post("2", "Clip", media="b")
    assert patreon.run(post_url("1")).rc == 0
    assert patreon.run(post_url("3")).rc == 0
    theirs = patreon.out / "Creator/Clip [2].mp4"
    before = theirs.read_bytes()

    r = patreon.run(post_url("2"))

    assert r.rc == 1, r
    assert f"no free name for 2: {theirs} is taken" in r.err
    assert patreon.store().get_post("2")["outcome"] == Outcome.FAILED
    assert media_rows(patreon, "2") == [(None, None)]
    assert media_rows(patreon, "3") == [("3", "Creator/Clip [2].mp4")]
    assert theirs.read_bytes() == before
    assert r.files == ["Creator/Clip [2].mp4", "Creator/Clip.mp4"]


def test_names_differing_only_in_case_get_their_own_files_in_parallel(patreon):
    patreon.post("701", "Clip", media="a")
    patreon.post("702", "clip", media="b")
    patreon.collection("89", "Cases", [("701", "Clip"), ("702", "clip")])

    r = patreon.run("https://www.patreon.com/collection/89", "-i", "1-2", "-j", "2")

    assert r.rc == 0, r
    paths = {}
    for pid in ("701", "702"):
        [(media_id, path)] = media_rows(patreon, pid)
        assert media_id == pid
        paths[pid] = path
    assert r.files == sorted(paths.values())
    # Which one wins the title-only name is a race; that one is suffixed is not.
    assert sorted(path.endswith(f" [{pid}].mp4") for pid, path in paths.items()) == [False, True]


def test_multi_video_post_with_a_missing_entry_resumes(patreon):
    patreon.post("3003", "Episode 7", videos=["a", "b"])

    def lose_second(ydl, info, download):
        # Only the first video is processed; the second is left as None, as
        # yt-dlp leaves an entry whose extraction failed under --ignore-errors.
        del info["entries"][1]
        res = ydl.process_ie_result(info, download=download)
        res["entries"].append(None)
        return res

    patreon.on_extract["3003"] = lose_second

    first = patreon.run(post_url("3003"))

    assert first.rc == 1, first
    assert patreon.store().get_post("3003")["outcome"] == Outcome.FAILED
    assert media_rows(patreon, "3003") == [("3003-1", "Creator/Episode 7.mp4"),
                                           ("3003-2", None)]

    del patreon.on_extract["3003"]
    r = patreon.run(post_url("3003"))

    assert r.rc == 0, r
    assert patreon.extract_calls == ["3003", "3003"]
    assert r.files == ["Creator/Episode 7 [3003-2].mp4", "Creator/Episode 7.mp4"]
    assert patreon.store().get_post("3003")["outcome"] == Outcome.COMPLETE


# Non-ASCII too: SQLite's NOCASE folds ASCII only. And one spelling in NFC, the
# other in NFD: APFS names one file either way. The Greek pair differs in both
# case and form, and only matches when normalizing after casefolding as well.
@pytest.mark.parametrize(("theirs", "mine"), [("Clip", "clip"), ("Été", "été"),
                                              ("Été", "Été"),
                                              ("ΐ", "Ϊ́")],
                         ids=["ascii", "non-ascii", "nfd", "case-and-form"])
def test_moved_away_file_still_owns_its_case_alias(patreon, theirs, mine):
    """The owner moves finished files off the device; the record stays."""
    patreon.post("1", theirs, media="a")
    patreon.post("2", mine, media="b")
    assert patreon.run(post_url("1")).rc == 0
    (patreon.out / f"Creator/{theirs}.mp4").unlink()

    r = patreon.run(post_url("2"))

    assert r.rc == 0, r
    assert r.files == [f"Creator/{mine} [2].mp4"]
    assert media_rows(patreon, "1") == [("1", f"Creator/{theirs}.mp4")]


def test_title_case_change_keeps_the_recorded_file(patreon):
    post = patreon.post("501", "Clip")
    assert patreon.run(post_url("501")).rc == 0
    post.title = "clip"

    r = patreon.run(post_url("501"), "--force")

    assert r.rc == 0, r
    assert not any("[501]" in f for f in r.files), r.files
    if (patreon.out / "Creator/CLIP.mp4").exists():
        # Case-insensitive storage: the alias is the recorded file, so nothing
        # is fetched again.
        assert r.files == ["Creator/Clip.mp4"]
        assert "has already been downloaded" in r.out
