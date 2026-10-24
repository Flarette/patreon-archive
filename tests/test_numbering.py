"""R1: numbers count the whole collection, oldest first, blocked entries included.

D8: a spec that starts with '-' may follow its flag as a separate word.
"""

from __future__ import annotations

import json
import re

import pytest

URL = "https://www.patreon.com/collection/77"

# Entries 2 and 6 are blocked. Pids differ from their numbers, so an
# off-by-one picks a visibly wrong post.
ENTRIES = [("9001", "One"), ("9002", None), ("9003", "Three"),
           ("9004", "Four"), ("9005", "Five"), ("9006", None)]

_ROW = re.compile(r"^\s+(\d+)\s{2}.*?(\[no access\])?$")


@pytest.fixture
def coll(patreon):
    patreon.collection("77", "My Coll", ENTRIES)
    return patreon


def _listed(patreon) -> dict[int, str]:
    """Number -> pid, as -l reports it."""
    r = patreon.run(URL, "-l", "--agent")
    assert r.rc == 0, r
    event = next(json.loads(line) for line in r.out.splitlines()
                 if json.loads(line)["event"] == "collection_listed")
    return {e["index"]: e["post_id"] for e in event["entries"]}


def test_list_numbers_every_entry_and_marks_blocked(coll):
    r = coll.run(URL, "-l")

    assert r.rc == 0, r
    rows = [m for m in map(_ROW.match, r.out.splitlines()) if m]
    assert [int(m[1]) for m in rows] == [1, 2, 3, 4, 5, 6]
    assert [int(m[1]) for m in rows if m[2]] == [2, 6]
    for m, (_, title) in zip(rows, ENTRIES):
        if title:
            assert m[0].endswith(title)
    assert coll.extract_calls == []


def test_list_event_maps_numbers_to_pids(coll):
    assert _listed(coll) == {n: pid for n, (pid, _) in enumerate(ENTRIES, 1)}


@pytest.mark.parametrize(("args", "numbers"), [
    (["-n", "-i=-3-"], [4, 5]),
    (["-n", "-i", "-3-"], [4, 5]),
    (["-n", "--items", "-3-"], [4, 5]),
    (["-n", "-x", "-2-"], [1, 3, 4]),
    (["-n", "-i", "1-3"], [1, 3]),
    (["-n", "--limit", "2", "-y"], [1, 3]),
])
def test_selection_picks_the_listed_pids(coll, args, numbers):
    listed = _listed(coll)

    r = coll.run(URL, *args)

    assert r.rc == 0, r
    assert coll.extract_calls == [listed[n] for n in numbers]


@pytest.mark.parametrize(("args", "number"), [(["-i", "2"], 2), (["-I", "-1"], 6)])
def test_selecting_only_blocked_entries_exits_no_access(coll, args, number):
    r = coll.run(URL, *args)

    # argparse also exits 2, so the skip line is what shows the spec was read.
    assert r.rc == 2, r
    assert f"skipping 1 of 1 selected: no access to {number}" in r.out
    assert coll.extract_calls == []


def test_a_flag_after_items_is_not_taken_as_its_spec(coll):
    r = coll.run("get", "-i", "-l", URL)

    assert r.rc == 2, r
    assert "-i/-I/--items: expected one argument" in r.err
    assert coll.extract_calls == []
