"""The capture interface every backend implements.

Keeping this deliberately small is the point: RTSP cameras today, Pi cameras or
USB webcams later, and the recorder above never notices the difference.
"""

from __future__ import annotations

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

    @property
    @abstractmethod
    def running(self) -> bool:
        """Is the capture still writing?"""

    @property
    @abstractmethod
    def elapsed(self) -> float:
        """Seconds since the capture started."""

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
    def check(self) -> tuple[bool, str]:
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
