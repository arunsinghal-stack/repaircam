"""Rescue footage that was recorded but never filed.

If the recorder restarts part-way through an operation — a reboot, a power cut,
the systemd service restarting — the in-memory session is lost. The segments
themselves survive: each is a complete, playable MP4 sitting in
``segments/<WC>/<timestamp>/``. What is lost is the knowledge that they belong
together, so nothing joins them and they never reach the library.

This module finds those orphans and files them as ordinary clips. They come out
unlabelled, because nobody ever told RepairCam what job they were for, and the
library already surfaces unlabelled clips so they can be tagged afterwards.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, ffmpeg
from .backends.base import Segment
from .catalogue import Catalogue, JobLabels, Recording
from .config import ConfigError
from .recorder import Session, finalise_session

log = logging.getLogger(__name__)

#: A session whose newest file changed this recently might still be recording.
#: Joining a file ffmpeg is still writing would corrupt it, so leave it alone.
DEFAULT_GRACE_SECONDS = 120

SEGMENT_DIR_FORMAT = "%Y%m%d-%H%M%S"


class RecoveryError(Exception):
    """An orphaned session could not be filed."""


@dataclass
class OrphanSession:
    """Segments on disk that no clip was ever made from."""

    work_center: str
    directory: Path
    segments: list[Path] = field(default_factory=list)
    started_at: float = 0.0
    started_estimated: bool = False

    @property
    def last_modified(self) -> float:
        return max((p.stat().st_mtime for p in self.segments), default=0.0)

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_modified

    @property
    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.segments)

    @property
    def size_mb(self) -> float:
        return round(self.total_bytes / 1_048_576, 1)

    @property
    def started_iso(self) -> str:
        return datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    def looks_active(self, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> bool:
        """Might ffmpeg still be writing into this folder?"""
        return self.idle_seconds < grace_seconds

    @property
    def label(self) -> str:
        """Short identifier a person can type: ``WC2/20260727-101500``."""
        return f"{self.work_center}/{self.directory.name}"


def find_orphans(
    data_root: Path | None = None, *, include_empty: bool = False
) -> list[OrphanSession]:
    """Find every segment folder that never became a clip.

    A folder only exists here if Done was not reached: the normal path removes
    it once the segments have been joined.
    """
    root = (data_root or config.data_dir()) / "segments"
    if not root.is_dir():
        return []

    orphans: list[OrphanSession] = []
    for bench_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for session_dir in sorted(p for p in bench_dir.iterdir() if p.is_dir()):
            segments = sorted(p for p in session_dir.glob("*.mp4") if p.stat().st_size > 0)
            if not segments and not include_empty:
                continue

            started_at, estimated = _started_at_for(session_dir, segments)
            orphans.append(
                OrphanSession(
                    work_center=bench_dir.name,
                    directory=session_dir,
                    segments=segments,
                    started_at=started_at,
                    started_estimated=estimated,
                )
            )
    return orphans


def _started_at_for(session_dir: Path, segments: list[Path]) -> tuple[float, bool]:
    """Work out when the operation began.

    The folder is named for its start time, which is the best answer available.
    If that name cannot be read, fall back to the oldest segment's timestamp and
    say the value is estimated.
    """
    try:
        parsed = datetime.strptime(session_dir.name, SEGMENT_DIR_FORMAT)
        return parsed.replace(tzinfo=timezone.utc).timestamp(), False
    except ValueError:
        oldest = min((p.stat().st_mtime for p in segments), default=time.time())
        return oldest, True


def _segments_for(orphan: OrphanSession) -> list[Segment]:
    """Describe each file as a Segment, with an honest duration where possible."""
    segments: list[Segment] = []
    for path in orphan.segments:
        ended_at = path.stat().st_mtime
        # ffprobe gives the real length; without it the duration of the joined
        # clip is still read later, so this only affects the per-segment list.
        duration = ffmpeg.duration_of(path) if ffmpeg.available() else None
        segments.append(
            Segment(
                path=path,
                started_at=ended_at - (duration or 0.0),
                ended_at=ended_at,
                ok=True,
            )
        )
    return segments


def _concat(segments: list[Segment], dest: Path) -> Path:
    """Join and then remove the originals, so the folder can be cleaned up."""
    ffmpeg.concat_files([s.path for s in segments], dest)
    for segment in segments:
        segment.path.unlink(missing_ok=True)
    return dest


def _camera_info(work_center: str) -> tuple[dict, str]:
    """Describe the camera if it is still configured; say so plainly if not.

    Recovery must work for a bench that has since been removed from
    cameras.yaml — the footage is no less real for it.
    """
    try:
        camera = config.get_camera(work_center)
    except (ConfigError, OSError):
        return {"work_center": work_center, "note": "camera no longer configured"}, ""
    return camera.describe(), camera.name


def recover(
    orphan: OrphanSession,
    *,
    catalogue: Catalogue | None = None,
    data_root: Path | None = None,
    labels: JobLabels | None = None,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    force: bool = False,
) -> Recording:
    """Join one orphaned session and file it as a clip."""
    if not orphan.segments:
        raise RecoveryError(f"{orphan.label} has no usable footage in it")

    if orphan.looks_active(grace_seconds) and not force:
        raise RecoveryError(
            f"{orphan.label} was written to {orphan.idle_seconds:.0f}s ago and may still be "
            f"recording. Wait a moment and try again, or pass --force if you are sure the "
            f"recorder is stopped."
        )

    root = data_root or config.data_dir()
    catalogue = catalogue or Catalogue()
    camera_info, camera_name = _camera_info(orphan.work_center)

    session = Session(
        work_center=orphan.work_center,
        labels=labels or JobLabels(),
        started_at=orphan.started_at,
        started_iso=orphan.started_iso,
        segments=_segments_for(orphan),
        segment_dir=orphan.directory,
    )

    try:
        recording = finalise_session(
            session,
            session.segments,
            data_root=root,
            catalogue=catalogue,
            concat=_concat,
            camera_info=camera_info,
            camera_name=camera_name,
            recovered=True,
        )
    except (ffmpeg.FFmpegError, OSError) as exc:
        # Leave the segments where they are. Half-recovered footage that has
        # been deleted is worse than footage that is merely still orphaned.
        raise RecoveryError(f"could not join {orphan.label}: {exc}") from exc

    catalogue.log_event(
        "recovered",
        work_center=orphan.work_center,
        detail=f"{len(orphan.segments)} segment(s) from {orphan.directory.name}",
        recording_id=recording.id,
    )
    log.info("recovered %s -> %s", orphan.label, recording.path)
    return recording


def recover_all(
    data_root: Path | None = None,
    *,
    catalogue: Catalogue | None = None,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    force: bool = False,
) -> tuple[list[Recording], list[tuple[OrphanSession, str]]]:
    """Recover every orphan, returning what worked and what did not.

    One unrecoverable folder must not stop the rest: they are independent
    operations that happen to share a fate.
    """
    catalogue = catalogue or Catalogue()
    recovered: list[Recording] = []
    failed: list[tuple[OrphanSession, str]] = []

    for orphan in find_orphans(data_root):
        try:
            recovered.append(
                recover(
                    orphan,
                    catalogue=catalogue,
                    data_root=data_root,
                    grace_seconds=grace_seconds,
                    force=force,
                )
            )
        except RecoveryError as exc:
            failed.append((orphan, str(exc)))
    return recovered, failed
