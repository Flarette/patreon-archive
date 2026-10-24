"""Sidecars are opt-in, and the date travels in the file's own tags."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

URL = "https://www.patreon.com/posts/501"


@pytest.fixture
def with_sidecars(patreon, tmp_path):
    """A post that offers a thumbnail and subtitles, so opting out is a real test."""
    thumb = tmp_path / "cover.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")

    def extract(ydl, info, download):
        info["thumbnail"] = thumb.as_uri()
        info["subtitles"] = {"en": [{"ext": "vtt", "data": "WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n"}]}
        return ydl.process_ie_result(info, download=download)

    patreon.post("501", "Clip", upload_date="20230115")
    patreon.on_extract["501"] = extract
    return patreon


def test_no_sidecars_by_default(with_sidecars):
    r = with_sidecars.run(URL)

    assert r.rc == 0, r
    assert r.files == ["Creator/Clip.mp4"]


def test_sidecar_flags_write_them(with_sidecars):
    r = with_sidecars.run(URL, "--infojson", "--thumbnails", "--subs")

    assert r.rc == 0, r
    assert r.files == ["Creator/Clip.en.vtt", "Creator/Clip.info.json",
                       "Creator/Clip.jpg", "Creator/Clip.mp4"]


def test_upload_date_is_written_into_the_file_tags(with_sidecars):
    r = with_sidecars.run(URL)
    assert r.rc == 0, r

    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(with_sidecars.out / "Creator/Clip.mp4")],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(probe.stdout)["format"]["tags"]["date"] == "20230115"
