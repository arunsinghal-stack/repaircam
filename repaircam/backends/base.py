"""The capture interface every backend implements.

Keeping this deliberately small is the point: RTSP cameras today, Pi cameras or
USB webcams later, and the recorder above never notices the difference.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import CameraConfig


class CaptureError(Exception):
    """A camera could not be opened, or a capture failed."""


@dataclass
class Segment:
    """One continuous piece of recording — one press of Start to one of Stop.

    An operation usually has exactly one, but a technician who pauses to fetch a
    part produces several, and they are joined on Done.
    """

    path: Path
    started_at: float
    ended_at: float | None = None
    ok: bool = True
    error: str = ""

    @property
    def duration(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


class ActiveCapture(ABC):
    """A capture in flight. Returned by :meth:`CaptureBackend.start`."""

    #: The file being written, when the backend writes to one. Subclasses set
    #: it (or override it with a property). It is what makes ``capturing``
    #: below work without the recorder knowing anything about ffmpeg.
    dest: Path | None = None

    #: Latched when the first byte appears. Never cleared.
    _capture_began_at: float | None = None

    @property
    @abstractmethod
    def running(self) -> bool:
        """Is the capture still writing?"""

    @property
    @abstractmethod
    def elapsed(self) -> float:
        """Seconds since the capture started."""

    @property
    def capturing(self) -> bool:
        """Are frames actually landing on disk yet?

        ``running`` only says the capture process exists. For RTSP there are
        seconds between that and the first frame: ffmpeg still has to resolve
        the host, open the stream, authenticate and wait for a keyframe — and
        if the camera is unplugged or the password is wrong it sits in exactly
        that gap and then dies. Showing a technician a red "recording" light
        during the gap tells them they are filmed when they may not be, which
        is the one thing an accountability system must never do.

        The signal is the destination file gaining its first byte, which works
        for any backend that writes a file. Once true it stays true — a stat
        that fails later must not make a live recording look dead.
        """
        if self._capture_began_at is not None:
            return True
        if self.dest is None:
            return self.running  # backend cannot tell us; take its word
        try:
            if self.dest.stat().st_size > 0:
                self._capture_began_at = time.time()
                return True
        except OSError:
            pass
        return False

    @property
    def capture_started_at(self) -> float | None:
        """When the first byte landed, or None while still connecting."""
        self.capturing  # refreshes the latch
        return self._capture_began_at

    @property
    def capturing_elapsed(self) -> float:
        """Seconds of *actual* footage — zero until frames start."""
        began = self.capture_started_at
        return 0.0 if began is None else max(0.0, time.time() - began)

    @abstractmethod
    def stop(self, timeout: float = 10) -> Segment:
        """Finish the capture cleanly and return the resulting segment."""

    @abstractmethod
    def wait(self, timeout: float | None = None) -> Segment:
        """Block until the capture ends on its own (fixed-duration captures)."""


class CaptureBackend(ABC):
    """How clips are captured from one bench's camera."""

    #: value used in cameras.yaml under ``backend:``
    name: str = "base"

    def __init__(self, camera: CameraConfig):
        self.camera = camera

    @property
    def work_center(self) -> str:
        return self.camera.work_center

    @abstractmethod
    def start(self, dest: Path, *, duration: float | None = None) -> ActiveCapture:
        """Begin writing a clip to ``dest``."""

    @abstractmethod
    def snapshot(self, dest: Path) -> Path:
        """Write a single still image to ``dest``."""

    @abstractmethod
    def preview_frames(self, *, fps: int = 6, width: int = 640):
        """Yield JPEG frames as bytes, for the browser live preview."""

    @abstractmethod
    def check(self, *, timeout: float | None = None) -> tuple[bool, str]:
        """Is the camera reachable right now? Returns (ok, human message)."""

    def concat(self, segments: list[Segment], dest: Path) -> Path:
        """Join several segments into the single clip for an operation.

        The default handles the common cases; RTSP overrides it to use ffmpeg's
        concat demuxer for the multi-segment case.
        """
        if not segments:
            raise CaptureError("nothing to join — no segments were recorded")
        if len(segments) == 1:
            dest.parent.mkdir(parents=True, exist_ok=True)
            segments[0].path.replace(dest)
            return dest
        raise CaptureError(f"{self.name} backend cannot join {len(segments)} segments")

    def describe(self) -> dict:
        return {"backend": self.name, **self.camera.describe()}
