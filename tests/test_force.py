"""--force fetches a post again and replaces its file, and only its own."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from patreon_archive.errors import Outcome

COLL = "https://www.patreon.com/collection/99"


def post_url(pid: str) -> str:
    return f"https://www.patreon.com/posts/{pid}"


def picture(path) -> str:
    """The decoded video's hash. FFmpegMetadata rewrites the container's tags on
    every run, so the file's own bytes differ even when the content does not.
    """
    probe = subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-f", "md5", "-"],
        check=True, capture_output=True, text=True,
    )
    return probe.stdout.strip()


@pytest.mark.parametrize("jobs", ["1", "2"])
def test_force_replaces_the_content_under_the_same_name(patreon, media, jobs):
    patreon.collection("99", "Clips", [("8001", "One"), ("8002", "Two")])
    first = patreon.run(COLL)
    assert first.rc == 0, first
    two = patreon.out / "Clips/Two.mp4"
    assert picture(two) == picture(media["a"])
    patreon.posts["8002"].media = "b"

    r = patreon.run(COLL, "-j", jobs, "--force")

    assert r.rc == 0, r
    assert sorted(patreon.extract_calls) == ["8001", "8001", "8002", "8002"]
    assert r.files == ["Clips/One.mp4", "Clips/Two.mp4"]
    assert picture(two) == picture(media["b"])
    assert picture(patreon.out / "Clips/One.mp4") == picture(media["a"])


def test_forced_rerun_of_a_suffixed_post_leaves_the_other_file(patreon, media):
    """The claim still names the file, so --force overwrites only the post's own."""
    patreon.post("2001", "Live Q&A", media="a")
    second = patreon.post("2002", "Live Q&A", media="b")
    patreon.collection("88", "Talks", [("2001", "Live Q&A"), ("2002", "Live Q&A")])
    first = patreon.run("https://www.patreon.com/collection/88")
    assert first.rc == 0, first
    plain = patreon.out / "Talks/Live Q&A.mp4"
    before = plain.read_bytes()
    second.media = "c"

    r = patreon.run(post_url("2002"), "--force")

    assert r.rc == 0, r
    assert r.files == first.files == ["Talks/Live Q&A [2002].mp4", "Talks/Live Q&A.mp4"]
    assert picture(patreon.out / "Talks/Live Q&A [2002].mp4") == picture(media["c"])
    assert plain.read_bytes() == before


def test_forced_run_staged_in_another_collections_folder_leaves_its_file(patreon, media):
    """With --temp-dir on another collection's folder, the forced post's staging
    path would be that collection's finished file, which yt-dlp clears first."""
    patreon.collection("1", "First", [("9001", "Clip")])
    patreon.collection("2", "Second", [("9002", "Clip")])
    patreon.posts["9002"].media = "b"
    assert patreon.run("https://www.patreon.com/collection/1").rc == 0
    assert patreon.run("https://www.patreon.com/collection/2").rc == 0
    theirs = patreon.out / "First/Clip.mp4"
    before = theirs.read_bytes()
    patreon.posts["9002"].media = "c"

    r = patreon.run(post_url("9002"), "--force", "--temp-dir", str(patreon.out / "First"))

    assert r.rc == 0, r
    assert r.files == ["First/Clip.mp4", "Second/Clip.mp4"]
    assert theirs.read_bytes() == before
    assert picture(patreon.out / "Second/Clip.mp4") == picture(media["c"])


def test_output_inside_the_staging_folder_fails(patreon):
    """A creator named like the default staging folder would put the post there."""
    patreon.post("9101", "Clip", channel=".tmp")

    r = patreon.run(post_url("9101"))

    assert r.rc == 1, r
    row = patreon.store().get_post("9101")
    path = patreon.out / ".tmp/Clip.mp4"
    assert (row["outcome"], row["outcome_detail"]) == (
        Outcome.FAILED, f"output path is inside the staging folder: {path}")
    assert [p for p in patreon.out.rglob("*")
            if p.is_file() and not p.name.startswith(".patreon-archive.db")] == []


def test_forced_run_leaves_an_earlier_output_in_the_staging_folder(patreon, media):
    """An earlier run, staging elsewhere, recorded A where this run stages: B's
    staging path is A's file."""
    theirs = patreon.out / ".patreon-archive-tmp/Clip.mp4"
    theirs.parent.mkdir(parents=True)
    shutil.copyfile(media["a"], theirs)
    store = patreon.store()
    store.upsert_post(post_id="9201", outcome=Outcome.COMPLETE, tool_version="0.4.0",
                      ytdlp_version="2026.07.04", title="Clip")
    store.record_media(post_id="9201", source_kind="ytdlp_post_url", state="complete",
                       media_id="9201", output_path=str(theirs))
    patreon.collection("2", "Second", [("9202", "Clip")])

    r = patreon.run(post_url("9202"), "--force", "--temp-dir", str(patreon.out))

    assert r.rc == 1, r
    row = patreon.store().get_post("9202")
    assert (row["outcome"], row["outcome_detail"]) == (
        Outcome.FAILED, f"staging path is another video's file: {theirs}")
    assert theirs.read_bytes() == media["a"].read_bytes()
    assert r.files == [".patreon-archive-tmp/Clip.mp4"]
