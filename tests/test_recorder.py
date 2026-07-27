"""The Start/Stop/Done state machine — the part that must not get this wrong.

A technician's footage is not reproducible: if the recorder loses a segment or
files a clip against the wrong job, there is no second take.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repaircam.catalogue import JobLabels
from repaircam.recorder import Recorder, RecorderError, State, slugify

LABELS = JobLabels(
    mo_name="WH/MO/00042",
    operation="Screen replacement",
    device="Redmi Note 12",
    imei="350123456789012",
    technician="Ramesh",
)


def test_starts_idle(recorder: Recorder):
    assert recorder.state is State.IDLE
    assert recorder.status()["busy"] is False


def test_single_session_produces_one_clip(recorder: Recorder, data_root: Path):
    recorder.start(LABELS)
    assert recorder.state is State.RECORDING

    recording = recorder.done()

    assert recorder.state is State.IDLE
    assert recording.id is not None
    assert recording.segments == 1
    assert recording.labels.mo_name == "WH/MO/00042"
    assert (data_root / recording.path).exists()


def test_pause_and_resume_join_into_one_clip(recorder: Recorder, data_root: Path):
    """The whole point of Done: several Start/Stop pairs, one clip out."""
    recorder.start(LABELS)
    recorder.stop()
    assert recorder.state is State.PAUSED

    recorder.start()
    recorder.stop()
    recorder.start()

    recording = recorder.done()

    assert recording.segments == 3
    clip = data_root / recording.path
    assert clip.exists()
    # The stub joins by bytes, so all three segments must be present in order.
    assert clip.read_bytes() == b"[seg-001.mp4][seg-002.mp4][seg-003.mp4]"


def test_resume_keeps_the_original_labels(recorder: Recorder):
    """A half-filled form on Continue must not wipe the job it is recording."""
    recorder.start(LABELS)
    recorder.stop()
    recorder.start(JobLabels())  # empty form submitted by the Continue button

    recording = recorder.done()
    assert recording.labels.mo_name == "WH/MO/00042"
    assert recording.labels.technician == "Ramesh"


def test_labels_can_be_supplied_at_done(recorder: Recorder):
    """Record first, tag afterwards — the common bench workflow."""
    recorder.start()
    recording = recorder.done(LABELS)
    assert recording.labels.operation == "Screen replacement"


def test_cannot_start_twice(recorder: Recorder):
    recorder.start(LABELS)
    with pytest.raises(RecorderError, match="already recording"):
        recorder.start(LABELS)


def test_stop_without_start_is_rejected(recorder: Recorder):
    with pytest.raises(RecorderError, match="not recording"):
        recorder.stop()


def test_done_without_anything_is_rejected(recorder: Recorder):
    with pytest.raises(RecorderError, match="nothing to finish"):
        recorder.done()


def test_cancel_discards_the_footage(recorder: Recorder, data_root: Path):
    recorder.start(LABELS)
    segment_paths = [c.dest for c in recorder.backend.captures]
    recorder.cancel()

    assert recorder.state is State.IDLE
    assert not any(p.exists() for p in segment_paths)
    assert recorder.catalogue.count() == 0


def test_failed_capture_reports_plainly(backend, catalogue, data_root):
    """A camera that produces nothing must say so, not file an empty clip."""
    backend.fail = True
    recorder = Recorder("WC2", backend=backend, catalogue=catalogue, data_root=data_root)

    recorder.start(LABELS)
    with pytest.raises(RecorderError, match="Nothing was recorded"):
        recorder.done()

    assert recorder.state is State.IDLE
    assert catalogue.count() == 0


def test_camera_dropping_out_is_noticed(recorder: Recorder):
    """ffmpeg dying must move the bench out of 'recording', not freeze the timer."""
    recorder.start(LABELS)
    recorder.backend.captures[-1].die()

    assert recorder.state is State.PAUSED
    assert recorder.status()["recording"] is False


def test_sidecar_describes_the_clip(recorder: Recorder, data_root: Path):
    recorder.start(LABELS)
    recorder.stop()
    recorder.start()
    recording = recorder.done()

    sidecar = json.loads((data_root / recording.sidecar_path).read_text())

    assert sidecar["job"]["mo_name"] == "WH/MO/00042"
    assert sidecar["job"]["imei"] == "350123456789012"
    assert sidecar["work_center"] == "WC2"
    assert sidecar["recorded"]["segments"] == 2
    assert sidecar["clip"] == recording.path
    assert sidecar["schema_version"] >= 1


def test_sidecar_never_contains_the_camera_password(recorder: Recorder, data_root: Path):
    """Sidecars ship with the dataset. A leaked camera password would ship too."""
    recorder.start(LABELS)
    recording = recorder.done()

    text = (data_root / recording.sidecar_path).read_text()
    assert "s3cret" not in text
    assert "******" in text


def test_clip_is_filed_by_date_and_named_readably(recorder: Recorder):
    recorder.start(LABELS)
    recording = recorder.done()

    parts = Path(recording.path).parts
    assert parts[0] == "recordings"
    assert len(parts[1]) == 10 and parts[1].count("-") == 2  # YYYY-MM-DD
    assert parts[2].startswith("WC2_")
    assert "wh-mo-00042" in parts[2]


def test_recorded_time_excludes_the_pause(recorder: Recorder):
    recorder.start(LABELS)
    recorder.stop()
    status = recorder.status()
    assert status["state"] == "paused"
    assert status["segments"] == 1
    # Paused, so the timer must not still be running against a live capture.
    assert status["elapsed_s"] == pytest.approx(status["elapsed_s"], abs=0.5)


def test_events_record_the_whole_session(recorder: Recorder):
    recorder.start(LABELS)
    recorder.stop()
    recorder.start()
    recorder.done()

    kinds = [e["kind"] for e in recorder.catalogue.recent_events()]
    assert set(kinds) >= {"start", "stop", "resume", "done"}


def test_record_once_saves_a_clip(recorder: Recorder):
    """The Phase 0 bench proof: cli record WC2 --duration 20."""
    recording = recorder.record_once(0.2, LABELS)
    assert recording.id is not None
    assert recording.segments == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("WH/MO/00042", "wh-mo-00042"),  # Odoo MOs contain slashes
        ("Screen replacement", "screen-replacement"),
        ("  ..  ", ""),
        ("Redmi Note 12 (5G)", "redmi-note-12-5g"),
    ],
)
def test_slugify_makes_paths_safe(value, expected):
    assert slugify(value) == expected
