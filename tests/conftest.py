"""Test fixtures.

The tests must run anywhere — including a laptop with no cameras and no ffmpeg
installed — so capture is faked with a stub backend that writes small files.
Everything above the backend (the state machine, the catalogue, the sidecars)
is the real code.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from repaircam.backends.base import ActiveCapture, CaptureBackend, CaptureError, Segment
from repaircam.catalogue import Catalogue
from repaircam.config import CameraConfig


class StubCapture(ActiveCapture):
    """Pretends to record by writing a few bytes to the destination file."""

    def __init__(
        self, dest: Path, payload: bytes, *, fail: bool = False, slow_start: bool = False
    ):
        self.dest = dest
        self.payload = payload
        self.fail = fail
        self.started_at = time.time()
        self._running = True
        self._segment: Segment | None = None
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not fail and not slow_start:
            dest.write_bytes(payload)

    def first_frame(self) -> None:
        """Simulate the camera finally answering, for a ``slow_start`` capture."""
        self.dest.write_bytes(self.payload)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def _finish(self) -> Segment:
        if self._segment is None:
            self._running = False
            self._segment = Segment(
                path=self.dest,
                started_at=self.started_at,
                ended_at=time.time(),
                ok=not self.fail,
                error="stub failure" if self.fail else "",
            )
        return self._segment

    def stop(self, timeout: float = 10) -> Segment:
        return self._finish()

    def wait(self, timeout: float | None = None) -> Segment:
        return self._finish()

    def die(self) -> None:
        """Simulate the camera being unplugged mid-recording."""
        self._running = False


class StubBackend(CaptureBackend):
    """A camera that always works (unless told not to), without ffmpeg."""

    name = "stub"

    def __init__(self, camera: CameraConfig, *, fail: bool = False, slow_start: bool = False):
        super().__init__(camera)
        self.fail = fail
        #: Return captures that have not written a byte yet — the real gap
        #: between ffmpeg starting and the camera answering.
        self.slow_start = slow_start
        self.captures: list[StubCapture] = []

    def start(self, dest: Path, *, duration: float | None = None) -> StubCapture:
        if self.fail == "start":
            raise CaptureError("stub camera refused to start")
        capture = StubCapture(
            dest, f"[{dest.name}]".encode(), fail=bool(self.fail), slow_start=self.slow_start
        )
        self.captures.append(capture)
        return capture

    def snapshot(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return dest

    def preview_frames(self, *, fps: int = 6, width: int = 640):
        yield b"jpeg"

    def check(self) -> tuple[bool, str]:
        return (not self.fail, "stub OK" if not self.fail else "stub is broken")

    def concat(self, segments: list[Segment], dest: Path) -> Path:
        """Join by concatenating bytes — enough to prove the wiring."""
        usable = [s for s in segments if s.ok and s.path.exists()]
        if not usable:
            raise CaptureError("no usable segments")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"".join(s.path.read_bytes() for s in usable))
        for segment in usable:
            segment.path.unlink(missing_ok=True)
        return dest


@pytest.fixture
def camera() -> CameraConfig:
    return CameraConfig(
        work_center="WC2",
        name="Bench 2",
        host="192.168.0.133",
        username="admin",
        password="s3cret",
        model="TP-Link VIGI C540V",
    )


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repaircam-data"
    root.mkdir()
    monkeypatch.setenv("REPAIRCAM_DATA_DIR", str(root))
    return root


@pytest.fixture
def catalogue(data_root: Path) -> Catalogue:
    return Catalogue(data_root / "test.db")


@pytest.fixture
def backend(camera: CameraConfig) -> StubBackend:
    return StubBackend(camera)


@pytest.fixture
def recorder(backend: StubBackend, catalogue: Catalogue, data_root: Path):
    from repaircam.recorder import Recorder

    return Recorder("WC2", backend=backend, catalogue=catalogue, data_root=data_root)
