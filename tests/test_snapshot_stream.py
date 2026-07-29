"""The focus test must sample the stream that actually gets recorded.

Focus is a lens property, so both streams go soft together — but the sub-stream
is far lower resolution, and "can I read the screws" is a resolution question as
much as a focus one. Judging the camera on the sub-stream would fail one that is
perfectly fine.
"""

from __future__ import annotations

from pathlib import Path

from repaircam.backends.rtsp import RtspBackend
from repaircam.config import CameraConfig


def test_snapshot_defaults_to_the_main_stream(camera: CameraConfig, backend, tmp_path: Path):
    backend.snapshot(tmp_path / "a.jpg")
    assert backend.snapshots == ["main"]


def test_sub_stream_is_available_when_asked_for(backend, tmp_path: Path):
    backend.snapshot(tmp_path / "b.jpg", stream="sub")
    assert backend.snapshots == ["sub"]


def test_rtsp_picks_the_right_url(camera: CameraConfig, monkeypatch, tmp_path: Path):
    """No ffmpeg here — check which URL would be handed to it."""
    seen: list[str] = []

    def fake_snapshot(url, dest, **kw):
        seen.append(url)
        return dest

    monkeypatch.setattr("repaircam.ffmpeg.snapshot", fake_snapshot)
    rtsp = RtspBackend(camera)

    rtsp.snapshot(tmp_path / "main.jpg")
    rtsp.snapshot(tmp_path / "sub.jpg", stream="sub")

    assert seen == [camera.main_url, camera.sub_url]
    assert camera.main_url != camera.sub_url
