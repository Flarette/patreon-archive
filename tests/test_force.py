"""--force fetches a post again and replaces its file, and only its own."""

from __future__ import annotations

import shutil
import subprocess

import pytest

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
