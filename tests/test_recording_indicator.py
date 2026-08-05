"""The red light must mean *capture*, not *intent*.

A technician glances at the dot and decides whether their work is being filmed.
If it goes red the moment Start is pressed, a bench whose camera is unplugged
looks exactly like a bench that is recording — and a whole repair happens
unfilmed while the screen says otherwise. These tests pin the distinction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repaircam import ffmpeg

from repaircam.recorder import CONNECT_WARN_S, Recorder

from .conftest import StubBackend


@pytest.fixture
def slow_recorder(camera, catalogue, data_root: Path) -> Recorder:
    """A bench whose camera does not answer until told to."""
    backend = StubBackend(camera, slow_start=True)
    return Recorder("WC2", backend=backend, catalogue=catalogue, data_root=data_root)


def test_start_alone_is_not_capturing(slow_recorder: Recorder):
    slow_recorder.start()
    status = slow_recorder.status()

    assert status["recording"] is True  # Start was pressed
    assert status["capturing"] is False  # but no frames have landed
    assert status["connecting"] is True
    assert status["state_label"] == "connecting to camera"


def test_first_frame_turns_the_light_red(slow_recorder: Recorder):
    slow_recorder.start()
    slow_recorder.backend.captures[-1].first_frame()

    status = slow_recorder.status()
    assert status["capturing"] is True
    assert status["connecting"] is False
    assert status["state_label"] == "recording"


def test_a_working_camera_is_capturing_immediately(recorder: Recorder):
    recorder.start()
    status = recorder.status()

    assert status["capturing"] is True
    assert status["connecting"] is False


def test_timer_does_not_run_while_connecting(slow_recorder: Recorder):
    slow_recorder.start()
    assert slow_recorder.status()["elapsed_s"] == 0.0

    slow_recorder.backend.captures[-1].first_frame()
    assert slow_recorder.status()["capturing"] is True


def test_warns_once_the_camera_has_stayed_silent(slow_recorder: Recorder):
    slow_recorder.start()
    assert slow_recorder.status()["camera_slow"] is False

    # Pretend the wait has gone on: the capture started long enough ago that a
    # healthy camera would have answered several times over.
    capture = slow_recorder.backend.captures[-1]
    capture.started_at -= CONNECT_WARN_S + 1

    status = slow_recorder.status()
    assert status["camera_slow"] is True
    assert status["connecting_s"] >= CONNECT_WARN_S


def test_capturing_latches_and_survives_a_vanished_file(slow_recorder: Recorder):
    """A stat that fails later must not make a live recording look dead."""
    slow_recorder.start()
    capture = slow_recorder.backend.captures[-1]
    capture.first_frame()
    assert capture.capturing is True

    capture.dest.unlink()
    assert capture.capturing is True


def test_idle_bench_reports_neither(recorder: Recorder):
    status = recorder.status()
    assert status["capturing"] is False
    assert status["connecting"] is False
    assert status["camera_slow"] is False
    assert status["state_label"] == "idle"


# --------------------------------------------------------------------------
# a stop that had to kill ffmpeg, and what it is allowed to claim afterwards
# --------------------------------------------------------------------------


def test_the_kill_outranks_whatever_ffmpeg_printed_last():
    """On 2026-08-05 a stalled input was killed and the bench reported
    "Non-monotonous DTS" — a routine timestamp grumble that happened to be the
    last line. Somebody then went and checked a camera that was fine."""
    noisy = (
        "[rtsp @ 0x1] max delay reached\n"
        "[mp4 @ 0x2] Non-monotonous DTS in output stream 0:0; previous: 18005,"
        " current: 18000; changing to 18006.\n"
    )

    said = ffmpeg.explain_failure(noisy, returncode=-9, ended_by="killed")

    assert "killed" in said
    assert "cut off mid-write" in said
    assert "DTS" not in said


def test_the_last_line_is_skipped_when_it_is_only_noise():
    stderr = (
        "[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\n"
        "[mp4 @ 0x2] Non-monotonous DTS in output stream 0:0\n"
        "[mp4 @ 0x2] past duration 0.9 too large\n"
    )

    assert "401 Unauthorized" in ffmpeg.explain_failure(stderr, returncode=1)


def test_all_noise_still_says_something():
    """Never "it failed" with no reason — that is what sends somebody to check
    a camera rather than a log."""
    stderr = "[mp4 @ 0x2] Non-monotonous DTS in output stream 0:0\n"
    assert ffmpeg.explain_failure(stderr, returncode=1)
    assert ffmpeg.explain_failure("", returncode=1) == "ffmpeg exited with code 1"
    assert ffmpeg.explain_failure("")


def test_segments_are_written_so_a_kill_survives():
    """+faststart moves the index at EXIT. Killed, or power cut, and no index
    was ever written — the file has no header and nothing can open it. That is
    how a whole packing session was lost.

    A fragmented MP4 carries a usable header from the first byte.
    """
    command = ffmpeg.record_command("rtsp://cam/stream1", Path("/tmp/seg-001.mp4"))
    flags = command[command.index("-movflags") + 1]

    assert "frag_keyframe" in flags
    assert "empty_moov" in flags
    assert "faststart" not in flags


def test_the_joined_clip_still_gets_faststart():
    """The concat runs on local files and finishes in seconds — it is not the
    process at risk, and the library wants a seekable clip."""
    command = ffmpeg.concat_command(Path("/tmp/list.txt"), Path("/tmp/out.mp4"))
    assert "+faststart" in command


# --------------------------------------------------------------------------
# an error is a fact with a time on it
# --------------------------------------------------------------------------


def test_a_stale_failure_stops_being_a_current_fault(recorder, monkeypatch):
    """A bench showed "Camera problem" for two days off one stale string, and
    somebody went to look at a camera that was working."""
    from repaircam import recorder as recorder_module

    recorder._fail("the camera stopped unexpectedly")
    assert recorder.status()["error_is_current"] is True

    # Rewind the clock past the window rather than waiting half an hour.
    recorder._last_error_at -= recorder_module.ERROR_STALE_AFTER_S + 60

    status = recorder.status()
    assert status["error_is_current"] is False
    # Still reported — a fault that happened is worth seeing, dated.
    assert status["last_error"]
    assert status["last_error_age_s"] > recorder_module.ERROR_STALE_AFTER_S


def test_a_stale_failure_does_not_light_the_bench_red():
    """The light says what the camera is doing NOW."""
    from repaircam.trigger import Trigger

    fresh = {"state": "idle", "last_error": "boom", "error_is_current": True}
    stale = {"state": "idle", "last_error": "boom", "error_is_current": False}

    assert Trigger._bench_state(fresh) == "error"
    assert Trigger._bench_state(stale) == "idle"


def test_a_successful_start_clears_the_failure(recorder):
    recorder._fail("boom")
    recorder.start()
    assert recorder.status()["last_error"] == ""
    assert recorder.status()["last_error_age_s"] is None


def test_a_recording_running_implausibly_long_is_reported(recorder, monkeypatch):
    """Nobody packs one order for four hours. A clip that has been open that
    long is a session nobody closed — and until now nothing said so."""
    from repaircam import recorder as recorder_module

    recorder.start()
    assert recorder.status()["running_long_s"] == 0.0

    capture = recorder._capture
    monkeypatch.setattr(
        type(capture), "capturing_elapsed",
        property(lambda _self: recorder_module.RUNNING_LONG_S + 600),
        raising=False,
    )

    assert recorder.status()["running_long_s"] > recorder_module.RUNNING_LONG_S


def test_a_long_recording_is_never_stopped_automatically(recorder, monkeypatch):
    """Reported, not cut. Footage that stops while the work is still going on
    is worse than a wasted disk, and RepairCam does not get to decide the shop
    is wrong about what it is doing."""
    from repaircam import recorder as recorder_module
    from repaircam.recorder import State

    recorder.start()
    capture = recorder._capture
    monkeypatch.setattr(
        type(capture), "capturing_elapsed",
        property(lambda _self: recorder_module.RUNNING_LONG_S * 10),
        raising=False,
    )

    recorder.status()

    assert recorder.state is State.RECORDING
    assert recorder._capture is capture
