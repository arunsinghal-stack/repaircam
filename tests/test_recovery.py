"""Recovering footage that a restart left behind.

The stakes here are the opposite of the rest of the system: recovery touches
footage that already exists and cannot be re-recorded. A bug that files a clip
badly is annoying; a bug that deletes segments it failed to join is permanent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from repaircam import ffmpeg, recovery
from repaircam.catalogue import Catalogue, JobLabels
from repaircam.recovery import RecoveryError, find_orphans, recover, recover_all

CAMERAS = """
cameras:
  WC2:
    name: "Bench 2"
    host: "192.168.0.133"
    password: "s3cret"
"""


@pytest.fixture(autouse=True)
def no_real_cameras(tmp_path: Path, monkeypatch):
    """Point at a cameras.yaml that does not exist unless a test makes one."""
    monkeypatch.setenv("REPAIRCAM_CAMERAS", str(tmp_path / "cameras.yaml"))


@pytest.fixture
def fake_concat(monkeypatch):
    """Join by concatenating bytes, so the tests need no ffmpeg."""

    def _concat(paths, dest, *, audio_codec="copy"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"".join(Path(p).read_bytes() for p in paths))
        return dest

    monkeypatch.setattr(ffmpeg, "concat_files", _concat)
    monkeypatch.setattr(ffmpeg, "available", lambda: False)
    return _concat


def make_orphan(data_root: Path, work_center: str, stamp: str, pieces: int = 2, *, age: float = 3600):
    """Create a segment folder as a crashed recorder would have left it."""
    directory = data_root / "segments" / work_center / stamp
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(1, pieces + 1):
        path = directory / f"seg-{index:03d}.mp4"
        path.write_bytes(f"[{work_center}-{index}]".encode())
        paths.append(path)
    if age:
        old = time.time() - age
        for path in paths:
            import os

            os.utime(path, (old, old))
    return directory


# --------------------------------------------------------------------------
# finding
# --------------------------------------------------------------------------


def test_nothing_to_find_on_a_clean_machine(data_root: Path):
    assert find_orphans(data_root) == []


def test_finds_each_abandoned_session(data_root: Path):
    make_orphan(data_root, "WC2", "20260727-101500", pieces=2)
    make_orphan(data_root, "WC3", "20260727-113000", pieces=1)

    orphans = find_orphans(data_root)

    assert [o.label for o in orphans] == ["WC2/20260727-101500", "WC3/20260727-113000"]
    assert [len(o.segments) for o in orphans] == [2, 1]


def test_start_time_comes_from_the_folder_name(data_root: Path):
    make_orphan(data_root, "WC2", "20260727-101500")
    orphan = find_orphans(data_root)[0]

    assert orphan.started_iso.startswith("2026-07-27T10:15:00")
    assert orphan.started_estimated is False


def test_unreadable_folder_name_falls_back_and_says_so(data_root: Path):
    make_orphan(data_root, "WC2", "not-a-timestamp")
    orphan = find_orphans(data_root)[0]

    assert orphan.started_estimated is True
    assert orphan.started_iso  # still produced something usable


def test_empty_folders_are_ignored(data_root: Path):
    (data_root / "segments" / "WC2" / "20260727-090000").mkdir(parents=True)
    # A zero-byte segment is not footage.
    (data_root / "segments" / "WC2" / "20260727-090000" / "seg-001.mp4").touch()

    assert find_orphans(data_root) == []


def test_a_folder_still_being_written_looks_active(data_root: Path):
    make_orphan(data_root, "WC2", "20260727-101500", age=0)
    assert find_orphans(data_root)[0].looks_active() is True


def test_an_old_folder_does_not_look_active(data_root: Path):
    make_orphan(data_root, "WC2", "20260727-101500", age=3600)
    assert find_orphans(data_root)[0].looks_active() is False


# --------------------------------------------------------------------------
# recovering
# --------------------------------------------------------------------------


def test_recovers_and_joins_the_segments(data_root: Path, catalogue: Catalogue, fake_concat):
    make_orphan(data_root, "WC2", "20260727-101500", pieces=3)
    orphan = find_orphans(data_root)[0]

    recording = recover(orphan, catalogue=catalogue, data_root=data_root)

    clip = data_root / recording.path
    assert clip.exists()
    assert clip.read_bytes() == b"[WC2-1][WC2-2][WC2-3]"
    assert recording.segments == 3
    assert recording.work_center == "WC2"


def test_recovered_clip_is_filed_under_its_original_date(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """The clip belongs to the day it was recorded, not the day it was rescued."""
    make_orphan(data_root, "WC2", "20260727-101500")
    recording = recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    assert Path(recording.path).parts[1] == "2026-07-27"
    assert recording.started_at.startswith("2026-07-27T10:15:00")


def test_segment_folder_is_cleaned_up_after_success(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    directory = make_orphan(data_root, "WC2", "20260727-101500")
    recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    assert not directory.exists()
    assert find_orphans(data_root) == []


def test_recovered_clip_reaches_the_catalogue_unlabelled(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """Nobody said what job it was, so it must be visible as needing a label."""
    make_orphan(data_root, "WC2", "20260727-101500")
    recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    assert catalogue.count() == 1
    assert catalogue.stats()["unlabelled"] == 1
    assert catalogue.list()[0].labels.is_empty


def test_labels_can_be_supplied_when_recovering(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    make_orphan(data_root, "WC2", "20260727-101500")
    labels = JobLabels(mo_name="WH/MO/77", operation="Battery")

    recording = recover(
        find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root, labels=labels
    )

    assert recording.labels.mo_name == "WH/MO/77"
    assert catalogue.stats()["unlabelled"] == 0


def test_sidecar_admits_the_clip_was_recovered(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """A dataset consumer must be able to tell a rescued clip from a normal one:
    its start time is inferred and nobody pressed Done on it."""
    make_orphan(data_root, "WC2", "20260727-101500")
    recording = recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    sidecar = json.loads((data_root / recording.sidecar_path).read_text())

    assert sidecar["recovered"] is True
    assert sidecar["recorded"]["started_at_is_estimated"] is True
    assert sidecar["recorded"]["segments"] == 2
    assert sidecar["work_center"] == "WC2"


def test_recovers_a_bench_no_longer_in_the_config(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """Footage from a removed bench is no less real."""
    make_orphan(data_root, "WC9", "20260727-101500")
    recording = recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    sidecar = json.loads((data_root / recording.sidecar_path).read_text())
    assert sidecar["camera"]["note"] == "camera no longer configured"
    assert recording.work_center == "WC9"


def test_uses_the_camera_details_when_it_is_still_configured(
    tmp_path: Path, data_root: Path, catalogue: Catalogue, fake_concat, monkeypatch
):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text(CAMERAS)
    monkeypatch.setenv("REPAIRCAM_CAMERAS", str(config_path))

    make_orphan(data_root, "WC2", "20260727-101500")
    recording = recover(find_orphans(data_root)[0], catalogue=catalogue, data_root=data_root)

    sidecar = json.loads((data_root / recording.sidecar_path).read_text())
    assert sidecar["camera"]["name"] == "Bench 2"
    assert recording.camera_name == "Bench 2"
    # Even here, the password must not reach the sidecar.
    assert "s3cret" not in json.dumps(sidecar)


# --------------------------------------------------------------------------
# refusing to do damage
# --------------------------------------------------------------------------


def test_refuses_a_session_that_may_still_be_recording(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """Joining a file ffmpeg is still writing would corrupt it."""
    make_orphan(data_root, "WC2", "20260727-101500", age=0)
    orphan = find_orphans(data_root)[0]

    with pytest.raises(RecoveryError, match="may still be recording"):
        recover(orphan, catalogue=catalogue, data_root=data_root)

    assert catalogue.count() == 0
    assert all(p.exists() for p in orphan.segments)


def test_force_overrides_the_active_check(data_root: Path, catalogue: Catalogue, fake_concat):
    make_orphan(data_root, "WC2", "20260727-101500", age=0)
    orphan = find_orphans(data_root)[0]

    recording = recover(orphan, catalogue=catalogue, data_root=data_root, force=True)
    assert recording.id is not None


def test_a_failed_join_keeps_the_footage(data_root: Path, catalogue: Catalogue, monkeypatch):
    """The one thing recovery must never do is lose what it could not save."""
    directory = make_orphan(data_root, "WC2", "20260727-101500", pieces=2)

    def explode(paths, dest, *, audio_codec="copy"):
        raise ffmpeg.FFmpegError("ffmpeg is not installed")

    monkeypatch.setattr(ffmpeg, "concat_files", explode)
    monkeypatch.setattr(ffmpeg, "available", lambda: False)
    orphan = find_orphans(data_root)[0]

    with pytest.raises(RecoveryError, match="could not join"):
        recover(orphan, catalogue=catalogue, data_root=data_root)

    assert directory.exists()
    assert len(list(directory.glob("*.mp4"))) == 2
    assert catalogue.count() == 0
    # Still discoverable, so it can be retried once ffmpeg is installed.
    assert len(find_orphans(data_root)) == 1


# --------------------------------------------------------------------------
# recover_all
# --------------------------------------------------------------------------


def test_recover_all_handles_each_session(data_root: Path, catalogue: Catalogue, fake_concat):
    make_orphan(data_root, "WC2", "20260727-101500")
    make_orphan(data_root, "WC3", "20260727-113000")

    recovered, failed = recover_all(data_root, catalogue=catalogue)

    assert len(recovered) == 2
    assert failed == []
    assert catalogue.count() == 2


def test_one_bad_session_does_not_stop_the_others(
    data_root: Path, catalogue: Catalogue, fake_concat
):
    """They are independent operations that happen to share a fate."""
    make_orphan(data_root, "WC2", "20260727-101500", age=3600)
    make_orphan(data_root, "WC3", "20260727-113000", age=0)  # too fresh, will be skipped

    recovered, failed = recover_all(data_root, catalogue=catalogue)

    assert len(recovered) == 1
    assert len(failed) == 1
    assert failed[0][0].work_center == "WC3"
    assert "may still be recording" in failed[0][1]


def test_recovered_events_are_logged(data_root: Path, catalogue: Catalogue, fake_concat):
    make_orphan(data_root, "WC2", "20260727-101500")
    recover_all(data_root, catalogue=catalogue)

    assert "recovered" in [e["kind"] for e in catalogue.recent_events()]
