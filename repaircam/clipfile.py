"""Where a recording's video file can actually be read from.

Two places, in order: the recorder's own copy, and — once retention has
removed that — the archive. Shared by the clip page (which plays it on the
LAN) and the clip sharer (which uploads a disputed clip), so the two can
never disagree about whether a clip still exists.

Both are checked against a permitted root, and the archive one against the
archive configured *now*: a path recorded when archive_dir pointed somewhere
else is not something to start reading files from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config, storage


class OutsideDataDir(ValueError):
    """A catalogue row pointing outside the data directory. A corrupted or
    hand-edited row must not be able to talk anything into reading
    /etc/passwd — the caller refuses rather than serving or uploading it."""


def under(path: Path, root: Path) -> bool:
    """Is ``path`` inside ``root``?

    Path.is_relative_to() would read better but is Python 3.9+, and the shop
    recorder runs 3.8 — relative_to() raising ValueError is the same test and
    works everywhere.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def find_clip(recording) -> Optional[Path]:
    """The readable file for ``recording``, or None if it is nowhere.

    Raises OutsideDataDir for a row whose path escapes the data directory.
    """
    root = config.data_dir()
    local = (root / recording.path).resolve()
    if not under(local, root):
        raise OutsideDataDir("recording path is outside the data directory")
    if storage.reachable(local):
        return local

    archived = getattr(recording, "archive_path", "")
    if archived:
        allowed = storage.load_config().archive_path
        candidate = Path(archived).resolve()
        # storage.reachable, not Path.exists: an unplugged on-demand mount
        # raises rather than returning False.
        if allowed and under(candidate, allowed.resolve()) and storage.reachable(candidate):
            return candidate
    return None
