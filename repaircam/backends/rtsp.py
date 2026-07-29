"""RTSP backend — PoE IP cameras on the shop LAN, driven by ffmpeg.

This is the Phase 0/1 capture path: one recorder box pulls RTSP from up to ten
cameras and copies the video straight to disk without re-encoding.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterator

from .. import ffmpeg
from ..config import CameraConfig
from .base import ActiveCapture, CaptureBackend, CaptureError, Segment

log = logging.getLogger(__name__)

# JPEG frame markers, used to slice the MJPEG byte stream into whole frames.
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class RtspCapture(ActiveCapture):
    """One ffmpeg process pulling one RTSP stream to one file."""

    def __init__(self, process: ffmpeg.RecordingProcess):
        self._process = process
        self._segment: Segment | None = None

    @property
    def dest(self) -> Path:
        return self._process.dest

    @property
    def running(self) -> bool:
        return self._process.running

    @property
    def elapsed(self) -> float:
        return self._process.elapsed

    def _finish(self, returncode: int) -> Segment:
        if self._segment is not None:
            return self._segment

        path = self._process.dest
        wrote_something = path.exists() and path.stat().st_size > 0
        # ffmpeg returns 255 when it is asked to quit early with 'q'. That is
        # our normal Stop, not a failure — as long as a file came out of it.
        ok = returncode in (0, 255) and wrote_something

        error = ""
        if not ok:
            error = (self._process.stderr or "").strip().splitlines()[-1:] or [
                f"ffmpeg exited with code {returncode}"
            ]
            error = error[0]
            log.error("capture failed for %s: %s", path.name, error)

        self._segment = Segment(
            path=path,
            # The clock starts at the first frame, not at Popen — otherwise the
            # time spent connecting is counted as footage that does not exist,
            # and every clip's duration is a little long.
            started_at=self.capture_started_at or self._process.started_at,
            ended_at=time.time(),
            ok=ok,
            error=error,
        )
        return self._segment

    def stop(self, timeout: float = 10) -> Segment:
        return self._finish(self._process.stop(timeout=timeout))

    def wait(self, timeout: float | None = None) -> Segment:
        return self._finish(self._process.wait(timeout=timeout))


class RtspBackend(CaptureBackend):
    """Capture from an ONVIF/RTSP IP camera (TP-Link VIGI and friends)."""

    name = "rtsp"

    def __init__(self, camera: CameraConfig, *, transport: str = "tcp"):
        super().__init__(camera)
        # TCP by default: UDP drops packets under shop Wi-Fi/PoE load, and a
        # dropped packet in a copied stream is a visible glitch in the evidence.
        self.transport = transport

    def start(self, dest: Path, *, duration: float | None = None) -> RtspCapture:
        command = ffmpeg.record_command(
            self.camera.main_url,
            dest,
            duration=duration,
            audio=self.camera.has_audio,
            transport=self.transport,
        )
        return RtspCapture(ffmpeg.RecordingProcess(command, dest))

    def snapshot(self, dest: Path) -> Path:
        # Snapshots come off the sub-stream so they cost the camera almost
        # nothing while a recording is running on the main stream.
        try:
            return ffmpeg.snapshot(self.camera.sub_url, dest)
        except ffmpeg.FFmpegError as exc:
            raise CaptureError(f"snapshot from {self.camera.safe_sub_url} failed: {exc}") from exc

    def preview_frames(self, *, fps: int = 6, width: int = 640) -> Iterator[bytes]:
        """Yield whole JPEG frames from the sub-stream until the caller stops."""
        import subprocess

        ffmpeg.require_ffmpeg()
        command = ffmpeg.mjpeg_command(
            self.camera.sub_url, fps=fps, width=width, transport=self.transport
        )
        log.debug("preview: %s", " ".join(ffmpeg.redact(command)))
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        buffer = bytearray()
        try:
            while True:
                chunk = proc.stdout.read(8192) if proc.stdout else b""
                if not chunk:
                    break
                buffer.extend(chunk)
                # A frame is complete once we have seen both markers; emit it
                # and keep whatever of the next frame already arrived.
                while True:
                    start = buffer.find(JPEG_START)
                    end = buffer.find(JPEG_END, start + 2) if start != -1 else -1
                    if start == -1 or end == -1:
                        break
                    yield bytes(buffer[start : end + 2])
                    del buffer[: end + 2]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def check(self, *, timeout: float | None = None) -> tuple[bool, str]:
        if timeout is None:
            return ffmpeg.reachable(self.camera.sub_url)
        return ffmpeg.reachable(self.camera.sub_url, timeout=timeout)

    def concat(self, segments: list[Segment], dest: Path) -> Path:
        """Join this operation's segments with ffmpeg's concat demuxer.

        Every segment came from the same camera with identical encoder settings,
        so they join by stream copy — no re-encode, no quality loss, seconds not
        minutes.
        """
        usable = [s for s in segments if s.ok and s.path.exists() and s.size_bytes > 0]
        if not usable:
            raise CaptureError("no usable segments to join")
        if len(usable) == 1:
            dest.parent.mkdir(parents=True, exist_ok=True)
            usable[0].path.replace(dest)
            return dest

        try:
            ffmpeg.concat_files([s.path for s in usable], dest)
        except ffmpeg.FFmpegError as exc:
            raise CaptureError(f"joining {len(usable)} segments failed: {exc}") from exc

        for segment in usable:
            segment.path.unlink(missing_ok=True)
        return dest
