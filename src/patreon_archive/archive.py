from __future__ import annotations

import shutil
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

from .errors import PatreonArchiveError

# Video is already compressed, so deflating it burns CPU for ~nothing -- badly so
# on a tablet. Every format here stores by default; the *_gz/_xz variants exist
# for when the user knowingly wants the trade.
FORMATS = ("zip", "tar", "tar.gz", "tar.bz2", "tar.xz")


class ArchiveError(PatreonArchiveError):
    pass


def make_archive(files: list[Path], dest_dir: Path, fmt: str, *,
                 label: str = "patreon-archive") -> Path:
    if fmt not in FORMATS:
        raise ArchiveError(f"unknown archive format {fmt!r}; choose from {', '.join(FORMATS)}")
    paths = [p for p in files if p.is_file()]
    if not paths:
        raise ArchiveError("nothing to archive")

    total = sum(p.stat().st_size for p in paths)
    free = shutil.disk_usage(dest_dir).free
    # Compressed formats can only shrink, so the stored size is the worst case.
    if free < total * 1.05:
        raise ArchiveError(
            f"not enough space: archive needs about {_human(total)}, "
            f"{_human(free)} free. Use -a with fewer entries, or free space first."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"{label}-{stamp}.{fmt}"

    # Flat-ish layout: keep the creator subdirectory, drop the absolute prefix.
    def arcname(p: Path) -> str:
        try:
            return str(p.relative_to(dest_dir))
        except ValueError:
            return p.name

    if fmt == "zip":
        # ZIP_STORED, not DEFLATED: see the note on FORMATS.
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as z:
            for p in paths:
                z.write(p, arcname(p))
    else:
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[fmt]
        with tarfile.open(out, mode) as t:
            for p in paths:
                t.add(p, arcname(p))

    return out


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"
