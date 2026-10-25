"""termux-setup.sh installs the highest wheel version next to it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "termux-setup.sh"


@pytest.mark.parametrize(("versions", "picked"), [
    (["0.2.0", "0.3.0", "0.10.0"], "0.10.0"),
    (["0.2.0", "0.3.0"], "0.3.0"),
    ([], None),
])
def test_wheel_pick_takes_the_highest_version(tmp_path, versions, picked):
    # The script itself only runs on Termux, so run its selection line alone,
    # under the same -euo pipefail.
    line = next(s.strip() for s in SCRIPT.read_text().splitlines()
                if s.strip().startswith("WHEEL="))
    for v in versions:
        (tmp_path / f"patreon_archive-{v}-py3-none-any.whl").touch()

    out = subprocess.run(
        [shutil.which("bash"), "-c",
         f'set -euo pipefail\nHERE="$1"\n{line}\nprintf %s "$WHEEL"',
         "termux-setup", str(tmp_path)],
        check=True, capture_output=True, text=True,
    ).stdout

    assert out == (str(tmp_path / f"patreon_archive-{picked}-py3-none-any.whl")
                   if picked else "")
