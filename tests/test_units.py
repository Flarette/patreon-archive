from __future__ import annotations

from pathlib import Path

import pytest

from patreon_archive.cli import _pretty_date, _resolve_output
from patreon_archive.config import (CREATOR_DIR, DEFAULT_OUTTMPL, OUTTMPL_WITH_ID,
                                    is_builtin_outtmpl)
from patreon_archive.selection import parse_items


@pytest.mark.parametrize(("value", "expected"), [
    (None, "downloads"),
    ("films", "downloads/films"),
    ("./x", "x"),
])
def test_resolve_output_relative_to_cwd(value, expected):
    assert _resolve_output(value) == Path.cwd().resolve() / expected


def test_resolve_output_expands_home():
    assert _resolve_output("~/x") == Path.home().resolve() / "x"


def test_resolve_output_takes_absolute_path_as_given(tmp_path):
    target = tmp_path.resolve() / "abs" / "dest"

    assert _resolve_output(str(target)) == target


def test_resolve_output_root_from_env(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "root"
    monkeypatch.setenv("PATREON_ARCHIVE_OUTPUT", str(root))

    assert _resolve_output(None) == root
    assert _resolve_output("films") == root / "films"


def test_pretty_date():
    assert _pretty_date("2024-06-06") == "June 6, 2024"


@pytest.mark.parametrize(("spec", "exclude", "expected"), [
    ("-3-", None, [4, 5, 6]),
    ("1-3,-2-", None, [1, 2, 3, 5, 6]),
    ("", "2-3", [1, 4, 5, 6]),
])
def test_parse_items(spec, exclude, expected):
    assert parse_items(spec, 6, exclude) == expected


@pytest.mark.parametrize(("tmpl", "ours"), [
    (DEFAULT_OUTTMPL, True),
    (DEFAULT_OUTTMPL.removeprefix(CREATOR_DIR), True),
    (OUTTMPL_WITH_ID, True),
    (OUTTMPL_WITH_ID.removeprefix(CREATOR_DIR), True),
    ("%(title)s.%(ext)s", False),
], ids=["default", "default-collection", "keep-id", "keep-id-collection", "user"])
def test_is_builtin_outtmpl_knows_both_forms(tmpl, ours):
    assert is_builtin_outtmpl(tmpl) is ours
