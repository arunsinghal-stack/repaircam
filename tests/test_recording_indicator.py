"""The red light must mean *capture*, not *intent*.

A technician glances at the dot and decides whether their work is being filmed.
If it goes red the moment Start is pressed, a bench whose camera is unplugged
looks exactly like a bench that is recording — and a whole repair happens
unfilmed while the screen says otherwise. These tests pin the distinction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
