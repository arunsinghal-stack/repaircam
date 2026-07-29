"""Disk space, the second copy, and the rules that guard deleting footage.

Deletion is the only operation in RepairCam that destroys something that cannot
be made again, so most of this file is about the conditions under which it
refuses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repaircam import storage
from repaircam.catalogue import Catalogue, JobLabels, Recording, sidecar_for, utcnow
from repaircam.storage import DiskFull, StorageConfig


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "archive"
    path.mkdir()
    return path


@pytest.fixture
def cfg(archive: Path) -> StorageConfig:
    return StorageConfig(archive_dir=str(archive), keep_days=7, delete_after_archive=True)


def make_clip(catalogue: Catalogue, data_root: Path, *, name="a.mp4", days_old=0,
              body=b"video-bytes") -> Recording:
    """A clip on disk with its sidecar, and a catalogue row for it."""
    relative = f"recordings/2026-07-01/{name}"
    path = data_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    sidecar_for(path).write_text('{"labels": {}}')
    return catalogue.add(Recording(
        work_center="WC2",
        path=relative,
        started_at=iso_days_ago(days_old),
        sidecar_path=str(sidecar_for(Path(relative))),
        size_bytes=len(body),
        labels=JobLabels(mo_name="WH/MO/42"),
    ))


# --------------------------------------------------------------------------
# the free-space guard
# --------------------------------------------------------------------------


def test_the_guard_refuses_when_the_disk_is_nearly_full(monkeypatch, data_root: Path):
    """Refused BEFORE starting: a clip that dies half-written is worse than one
    that was never begun, because nobody notices the first."""
    monkeypatch.setattr(
        storage.shutil, "disk_usage",
        lambda _p: type("U", (), {"free": 5 * 1_073_741_824, "total": 500 * 1_073_741_824,
                                  "used": 495 * 1_073_741_824})(),
    )
    with pytest.raises(DiskFull, match="Recording is stopped"):
        storage.check_before_recording(StorageConfig(min_free_gb=20))


def test_the_guard_allows_a_healthy_disk(monkeypatch, data_root: Path):
    monkeypatch.setattr(
        storage.shutil, "disk_usage",
        lambda _p: type("U", (), {"free": 200 * 1_073_741_824, "total": 500 * 1_073_741_824,
                                  "used": 300 * 1_073_741_824})(),
    )
    storage.check_before_recording(StorageConfig(min_free_gb=20))  # does not raise


def test_a_full_disk_stops_a_bench_starting(monkeypatch, recorder):
    """The whole point: the technician is told, instead of getting a clip that
    quietly stops when the disk runs out."""
    def full(_cfg=None, _root=None):
        raise DiskFull("Only 3 GB left — about 2 bench-hours.")

    monkeypatch.setattr(storage, "check_before_recording", full)

    from repaircam.recorder import RecorderError

    with pytest.raises(RecorderError, match="3 GB left"):
        recorder.start()
    assert "3 GB left" in recorder.last_error


def test_resuming_an_operation_is_not_blocked(monkeypatch, recorder):
    """Its earlier segments are already on the disk; abandoning them helps
    nobody, and the technician is mid-repair."""
    recorder.start()
    recorder.stop()

    calls = []
    monkeypatch.setattr(
        storage, "check_before_recording",
        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(DiskFull("full")),
    )
    recorder.start()  # resumes without consulting the guard
    assert calls == []


def test_the_report_speaks_in_bench_hours(monkeypatch, data_root: Path):
    monkeypatch.setattr(
        storage.shutil, "disk_usage",
        lambda _p: type("U", (), {"free": 18 * 1_073_741_824, "total": 500 * 1_073_741_824,
                                  "used": 482 * 1_073_741_824})(),
    )
    report = storage.disk_report(StorageConfig(min_free_gb=20, warn_free_gb=50))
    assert report.state == "full"
    assert "bench-hours" in report.message


# --------------------------------------------------------------------------
# the second copy
# --------------------------------------------------------------------------


def test_a_clip_and_its_sidecar_are_copied(catalogue, data_root, cfg, archive):
    clip = make_clip(catalogue, data_root)

    result = storage.archive_pending(catalogue, cfg, root=data_root)

    assert result.copied == [clip.id]
    copied = archive / clip.path
    assert copied.read_bytes() == b"video-bytes"
    # A clip without its sidecar is a video file, not a dataset sample.
    assert sidecar_for(copied).exists()
    assert catalogue.get(clip.id).archived_at


def test_a_clip_is_not_copied_twice(catalogue, data_root, cfg):
    make_clip(catalogue, data_root)
    storage.archive_pending(catalogue, cfg, root=data_root)

    again = storage.archive_pending(catalogue, cfg, root=data_root)
    assert again.copied == []


def test_an_unmounted_archive_is_refused(catalogue, data_root, tmp_path):
    """An unmounted NAS looks exactly like an empty directory we would happily
    'archive' into and then delete originals against."""
    make_clip(catalogue, data_root)
    missing = StorageConfig(archive_dir=str(tmp_path / "not-mounted"))

    result = storage.archive_pending(catalogue, missing, root=data_root)

    assert result.copied == []
    assert "not there" in result.skipped_reason


def test_with_no_archive_configured_nothing_is_copied(catalogue, data_root):
    make_clip(catalogue, data_root)
    result = storage.archive_pending(catalogue, StorageConfig(), root=data_root)
    assert "archiving is off" in result.skipped_reason


def test_a_short_copy_is_not_treated_as_archived(catalogue, data_root, cfg, monkeypatch):
    """A file that merely EXISTS at the archive is what later permits deleting
    the original, so an interrupted copy must never be mistaken for a whole one."""
    clip = make_clip(catalogue, data_root)

    def truncating_copy(src, dst, **kw):
        Path(dst).write_bytes(b"trunc")

    monkeypatch.setattr(storage.shutil, "copy2", truncating_copy)

    result = storage.archive_pending(catalogue, cfg, root=data_root)

    assert result.copied == []
    assert result.failed and "expected" in result.failed[0][1]
    assert catalogue.get(clip.id).archived_at == ""


def test_one_bad_clip_does_not_stop_the_others(catalogue, data_root, cfg):
    missing = make_clip(catalogue, data_root, name="gone.mp4")
    (data_root / missing.path).unlink()
    good = make_clip(catalogue, data_root, name="fine.mp4")

    result = storage.archive_pending(catalogue, cfg, root=data_root)

    assert result.copied == [good.id]
    assert result.failed and result.failed[0][0] == missing.id


# --------------------------------------------------------------------------
# deleting — every one of these is a refusal
# --------------------------------------------------------------------------


def test_an_unarchived_clip_is_never_deleted(catalogue, data_root, cfg):
    """Age alone is never enough."""
    clip = make_clip(catalogue, data_root, days_old=999)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert (data_root / clip.path).exists()


def test_a_recent_clip_is_kept_even_when_archived(catalogue, data_root, cfg):
    clip = make_clip(catalogue, data_root, days_old=1)
    storage.archive_pending(catalogue, cfg, root=data_root)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert (data_root / clip.path).exists()


def test_an_old_archived_clip_is_removed_locally(catalogue, data_root, cfg, archive):
    clip = make_clip(catalogue, data_root, days_old=30)
    storage.archive_pending(catalogue, cfg, root=data_root)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == [clip.id]
    assert not (data_root / clip.path).exists()
    assert not sidecar_for(data_root / clip.path).exists()
    # The copy, and the row saying where it went, both survive.
    assert (archive / clip.path).exists()
    assert catalogue.get(clip.id).local_deleted == 1
    assert catalogue.get(clip.id).archive_path


def test_a_vanished_archive_copy_stops_the_deletion(catalogue, data_root, cfg, archive):
    """The database row is not evidence. A NAS that was wiped, or a disk swapped
    out, would otherwise take the only remaining copy with it."""
    clip = make_clip(catalogue, data_root, days_old=30)
    storage.archive_pending(catalogue, cfg, root=data_root)
    (archive / clip.path).unlink()

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert (data_root / clip.path).exists()


def test_an_archive_copy_of_the_wrong_size_stops_the_deletion(catalogue, data_root, cfg, archive):
    clip = make_clip(catalogue, data_root, days_old=30)
    storage.archive_pending(catalogue, cfg, root=data_root)
    (archive / clip.path).write_bytes(b"short")

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert (data_root / clip.path).exists()


def test_deletion_is_off_until_it_is_asked_for(catalogue, data_root, archive):
    """The one thing here that cannot be undone is opted into, not out of."""
    clip = make_clip(catalogue, data_root, days_old=30)
    cfg = StorageConfig(archive_dir=str(archive), keep_days=7, delete_after_archive=False)
    storage.archive_pending(catalogue, cfg, root=data_root)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert "deletion is off" in result.skipped_reason
    assert (data_root / clip.path).exists()


def test_nothing_is_deleted_while_archiving_is_off(catalogue, data_root):
    clip = make_clip(catalogue, data_root, days_old=999)
    cfg = StorageConfig(delete_after_archive=True)  # but no archive_dir

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert "archiving is off" in result.skipped_reason
    assert (data_root / clip.path).exists()


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_a_missing_config_still_guards_the_disk(tmp_path):
    """Unlike the camera and saar-seva configs, absent is not 'switched off' —
    the guard is what stops a repair being half-recorded onto a full disk."""
    cfg = storage.load_config(tmp_path / "nothing.yaml")
    assert cfg.min_free_gb == storage.DEFAULT_MIN_FREE_GB
    assert cfg.archive_dir == ""
    assert cfg.delete_after_archive is False


def test_the_config_is_read(tmp_path):
    path = tmp_path / "storage.yaml"
    path.write_text(
        "storage:\n  min_free_gb: 5\n  archive_dir: /mnt/arch\n"
        "  keep_days: 3\n  delete_after_archive: true\n"
    )
    cfg = storage.load_config(path)
    assert cfg.min_free_gb == 5
    assert cfg.archive_dir == "/mnt/arch"
    assert cfg.keep_days == 3
    assert cfg.delete_after_archive is True
