"""Disk space, the second copy, and the rules that guard deleting footage.

Deletion is the only operation in RepairCam that destroys something that cannot
be made again, so most of this file is about the conditions under which it
refuses.
"""

from __future__ import annotations

import os
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
    return StorageConfig(
        archive_dir=str(archive), keep_days_local=7, delete_after_archive=True
    )


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


# --------------------------------------------------------------------------
# retention differs by what the footage is of
# --------------------------------------------------------------------------


def by_source_cfg(archive: Path) -> StorageConfig:
    """The shop's real shape: a short window on the recorder, long ones at the
    archive, and both deletions switched on."""
    return StorageConfig(
        archive_dir=str(archive),
        keep_days_local=7,
        keep_days=7,
        keep_days_by_source={"repair": 30, "packing": 45},
        delete_after_archive=True,
        delete_from_archive=True,
    )


def make_sourced(catalogue, data_root, *, name, days_old, source, ref="x"):
    clip = make_clip(catalogue, data_root, name=name, days_old=days_old)
    catalogue.set_source(clip.id, source, ref)
    return catalogue.get(clip.id)


def test_repair_and_packing_expire_on_their_own_clocks(catalogue, data_root, archive):
    """A repair is disputable while it is under warranty; a packing complaint
    arrives inside the delivery window. One number would be wrong twice.

    These are the ARCHIVE's windows: the point at which footage stops existing.
    """
    cfg = by_source_cfg(archive)
    old_repair = make_sourced(catalogue, data_root, name="r-old.mp4", days_old=40, source="repair")
    new_repair = make_sourced(catalogue, data_root, name="r-new.mp4", days_old=20, source="repair")
    old_pack = make_sourced(catalogue, data_root, name="p-old.mp4", days_old=50, source="packing")
    new_pack = make_sourced(catalogue, data_root, name="p-new.mp4", days_old=40, source="packing")
    storage.archive_pending(catalogue, cfg, root=data_root, limit=99)

    result = storage.prune_archive(catalogue, cfg, root=data_root)

    assert sorted(result.deleted) == sorted([old_repair.id, old_pack.id])
    # 40 days is past a repair's 30 and inside a packing clip's 45.
    assert not (archive / old_repair.path).exists()
    assert (archive / new_repair.path).exists()
    assert not (archive / old_pack.path).exists()
    assert (archive / new_pack.path).exists()


def test_the_recorder_uses_one_window_for_everything(catalogue, data_root, archive):
    """A repair's 30 days and a packing clip's 45 are archive policy. The
    recorder's own window is arithmetic — this laptop holds about five days —
    so applying the policy here would fill the disk long before anything
    expired, and the free-space guard would stop recording mid-week."""
    cfg = by_source_cfg(archive)  # keep_days_local = 7
    repair = make_sourced(catalogue, data_root, name="r.mp4", days_old=10, source="repair")
    packing = make_sourced(catalogue, data_root, name="p.mp4", days_old=10, source="packing")
    storage.archive_pending(catalogue, cfg, root=data_root, limit=99)

    result = storage.prune(catalogue, cfg, root=data_root)

    # Both go locally at 7 days, despite their 30- and 45-day archive windows.
    assert sorted(result.deleted) == sorted([repair.id, packing.id])
    # And the footage is untouched, which is why that is safe.
    assert (archive / repair.path).exists()
    assert (archive / packing.path).exists()


def test_a_hand_started_clip_uses_the_default(catalogue, data_root, archive):
    """It belongs to no integration, so no integration's window applies."""
    cfg = by_source_cfg(archive)  # archive default is 7 days
    clip = make_clip(catalogue, data_root, name="byhand.mp4", days_old=10)
    storage.archive_pending(catalogue, cfg, root=data_root)

    result = storage.prune_archive(catalogue, cfg, root=data_root)

    assert result.deleted == [clip.id]


def test_a_source_with_no_window_of_its_own_falls_back(catalogue, data_root, archive):
    cfg = by_source_cfg(archive)
    clip = make_sourced(catalogue, data_root, name="other.mp4", days_old=10, source="something")
    storage.archive_pending(catalogue, cfg, root=data_root)

    assert storage.prune_archive(catalogue, cfg, root=data_root).deleted == [clip.id]
    assert cfg.keep_days_for("something") == 7


def test_the_windows_are_read_from_the_file(tmp_path):
    path = tmp_path / "storage.yaml"
    path.write_text(
        "storage:\n  keep_days_local: 5\n  keep_days: 14\n"
        "  keep_days_by_source:\n    repair: 30\n    packing: 45\n"
    )
    cfg = storage.load_config(path)

    assert cfg.keep_days_local == 5
    assert cfg.keep_days_for("repair") == 30
    assert cfg.keep_days_for("packing") == 45
    assert cfg.keep_days_for("") == 14
    assert cfg.keep_days_for("anything-else") == 14


def test_a_missing_local_window_does_not_inherit_the_archive_policy(tmp_path):
    """An older storage.yaml has keep_days meaning "on the recorder". Reading
    that as the recorder's window again would put 30 days on a disk that holds
    five. The recorder falls back to its own small default instead."""
    path = tmp_path / "storage.yaml"
    path.write_text("storage:\n  keep_days: 30\n")
    cfg = storage.load_config(path)

    assert cfg.keep_days == 30
    assert cfg.keep_days_local == storage.DEFAULT_KEEP_DAYS_LOCAL
    assert cfg.keep_days_local < 30


# --------------------------------------------------------------------------
# the end of the line: deleting from the archive
# --------------------------------------------------------------------------


def test_the_archive_keeps_everything_until_asked(catalogue, data_root, archive):
    """The switch that ends footage is separate from the one that frees the
    laptop, and off until someone sets it."""
    cfg = StorageConfig(
        archive_dir=str(archive), keep_days=1, delete_after_archive=True
    )  # delete_from_archive left off
    clip = make_clip(catalogue, data_root, name="old.mp4", days_old=99)
    storage.archive_pending(catalogue, cfg, root=data_root)

    result = storage.prune_archive(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert "delete_from_archive" in result.skipped_reason
    assert (archive / clip.path).exists()


def test_an_unmounted_archive_is_never_pruned(catalogue, data_root, tmp_path):
    """An absent directory is the shape of an unmounted disk. Marking rows
    deleted against it would declare footage gone while it sat safe in a
    drawer."""
    cfg = StorageConfig(
        archive_dir=str(tmp_path / "not-mounted"), keep_days=1, delete_from_archive=True
    )
    make_clip(catalogue, data_root, name="old.mp4", days_old=99)

    result = storage.prune_archive(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert "mounted" in result.skipped_reason


def test_a_kept_clip_never_expires_from_the_archive(catalogue, data_root, archive):
    cfg = by_source_cfg(archive)
    clip = make_sourced(catalogue, data_root, name="keeper.mp4", days_old=400, source="repair")
    storage.archive_pending(catalogue, cfg, root=data_root)
    catalogue.set_keep(clip.id)

    assert storage.prune_archive(catalogue, cfg, root=data_root).deleted == []
    assert (archive / clip.path).exists()


def test_a_clip_archived_to_a_different_disk_is_left_alone(catalogue, data_root, archive, tmp_path):
    """The row names a file on a disk this config never wrote to. Deleting it
    is how a footgun goes off on a machine that has had two archive drives."""
    elsewhere = tmp_path / "old-archive"
    elsewhere.mkdir()
    old_cfg = StorageConfig(archive_dir=str(elsewhere))
    clip = make_clip(catalogue, data_root, name="moved.mp4", days_old=99)
    storage.archive_pending(catalogue, old_cfg, root=data_root)

    now_cfg = StorageConfig(archive_dir=str(archive), keep_days=1, delete_from_archive=True)
    result = storage.prune_archive(catalogue, now_cfg, root=data_root)

    assert result.deleted == []
    assert (elsewhere / clip.path).exists()


def test_expiring_from_the_archive_takes_the_local_copy_too(catalogue, data_root, archive):
    """The window is over: it would be strange to declare the footage's life
    ended and leave a copy on the recorder — one that nothing could ever remove
    afterwards, because prune only deletes what it can verify at the archive."""
    cfg = StorageConfig(archive_dir=str(archive), keep_days=1, delete_from_archive=True)
    clip = make_clip(catalogue, data_root, name="old.mp4", days_old=99)
    storage.archive_pending(catalogue, cfg, root=data_root)
    assert (data_root / clip.path).exists()  # local prune has not run

    result = storage.prune_archive(catalogue, cfg, root=data_root)

    assert result.deleted == [clip.id]
    assert not (archive / clip.path).exists()
    assert not (data_root / clip.path).exists()
    assert not sidecar_for(archive / clip.path).exists()


def test_the_row_survives_its_own_footage(catalogue, data_root, archive):
    """An Odoo link from eight months ago still resolves to this row. "Removed
    on 12 March under the 30-day policy" is an answer; a dead link is not."""
    cfg = StorageConfig(archive_dir=str(archive), keep_days=1, delete_from_archive=True)
    clip = make_clip(catalogue, data_root, name="old.mp4", days_old=99)
    storage.archive_pending(catalogue, cfg, root=data_root)

    storage.prune_archive(catalogue, cfg, root=data_root)

    row = catalogue.get(clip.id)
    assert row is not None
    assert row.archive_deleted == 1
    assert row.archive_deleted_at
    assert row.local_deleted == 1
    assert catalogue.count_archive_deleted() == 1


def test_an_expired_clip_is_not_offered_up_again(catalogue, data_root, archive):
    """Its files are gone, so a second pass has nothing to do and must not
    report deleting it twice."""
    cfg = StorageConfig(archive_dir=str(archive), keep_days=1, delete_from_archive=True)
    make_clip(catalogue, data_root, name="old.mp4", days_old=99)
    storage.archive_pending(catalogue, cfg, root=data_root)

    assert len(storage.prune_archive(catalogue, cfg, root=data_root).deleted) == 1
    assert storage.prune_archive(catalogue, cfg, root=data_root).deleted == []


def test_a_shorter_archive_window_than_the_recorder_is_reported(archive):
    """Not an error — it resolves itself, because the archive pass takes the
    local copy too. But somebody typed a number they did not mean."""
    cfg = StorageConfig(
        archive_dir=str(archive),
        keep_days_local=30,
        keep_days=30,
        keep_days_by_source={"repair": 30, "packing": 7},
    )
    assert cfg.local_outlives_archive == ["packing"]
    assert StorageConfig().local_outlives_archive == []


def test_only_the_named_sources_are_overridden(tmp_path):
    """Setting one must not silently drop the other."""
    path = tmp_path / "storage.yaml"
    path.write_text("storage:\n  keep_days_by_source:\n    packing: 60\n")
    cfg = storage.load_config(path)

    assert cfg.keep_days_for("packing") == 60
    assert cfg.keep_days_for("repair") == 30  # still the shipped default


def test_a_window_that_is_not_a_number_is_refused(tmp_path):
    path = tmp_path / "storage.yaml"
    path.write_text("storage:\n  keep_days_by_source:\n    repair: soon\n")

    with pytest.raises(storage.StorageError, match="not a number of days"):
        storage.load_config(path)


# --------------------------------------------------------------------------
# "keep this one" — age knows nothing about which clips matter
# --------------------------------------------------------------------------


def test_a_kept_clip_is_never_deleted(catalogue, data_root, cfg):
    """The training example and the disputed repair are exactly the clips
    somebody looks for long after the routine ones have gone."""
    clip = make_clip(catalogue, data_root, days_old=999)
    storage.archive_pending(catalogue, cfg, root=data_root)
    catalogue.set_keep(clip.id)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == []
    assert (data_root / clip.path).exists()


def test_keeping_can_be_undone(catalogue, data_root, cfg):
    clip = make_clip(catalogue, data_root, days_old=999)
    storage.archive_pending(catalogue, cfg, root=data_root)
    catalogue.set_keep(clip.id)
    storage.prune(catalogue, cfg, root=data_root)

    catalogue.set_keep(clip.id, False)
    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == [clip.id]


def test_keeping_one_does_not_spare_the_others(catalogue, data_root, cfg):
    kept = make_clip(catalogue, data_root, name="kept.mp4", days_old=999)
    ordinary = make_clip(catalogue, data_root, name="ordinary.mp4", days_old=999)
    storage.archive_pending(catalogue, cfg, root=data_root, limit=99)
    catalogue.set_keep(kept.id)

    result = storage.prune(catalogue, cfg, root=data_root)

    assert result.deleted == [ordinary.id]
    assert (data_root / kept.path).exists()
    assert not (data_root / ordinary.path).exists()


# --------------------------------------------------------------------------
# the worker picks up a changed storage.yaml without a restart
# --------------------------------------------------------------------------


def write_config(path: Path, body: str) -> Path:
    path.write_text(f"storage:\n{body}")
    # stat() has coarse timestamps on some filesystems, and two writes in the
    # same test can land in the same tick. Nudge mtime so the test is testing
    # the worker, not the clock.
    stamp = path.stat().st_mtime
    os.utime(path, (stamp + 10, stamp + 10))
    return path


def test_an_edited_config_is_picked_up_without_a_restart(catalogue, tmp_path):
    """The worker read its settings once, at startup, so every change needed
    `systemctl restart repaircam` — and forgetting it meant the status page
    reported settings the worker was not using."""
    path = write_config(tmp_path / "storage.yaml", "  keep_days_local: 5\n")
    worker = storage.StorageWorker(catalogue, config_path=path)
    assert worker.cfg.keep_days_local == 5

    write_config(path, "  keep_days_local: 9\n")

    assert worker.reload_if_changed() is True
    assert worker.cfg.keep_days_local == 9


def test_an_untouched_config_is_not_re_read(catalogue, tmp_path):
    """Steady state must be one stat() call, not a YAML parse every pass."""
    path = write_config(tmp_path / "storage.yaml", "  keep_days_local: 5\n")
    worker = storage.StorageWorker(catalogue, config_path=path)

    assert worker.reload_if_changed() is False
    assert worker.reload_if_changed() is False


def test_a_touched_but_unchanged_config_reports_no_change(catalogue, tmp_path):
    path = write_config(tmp_path / "storage.yaml", "  keep_days_local: 5\n")
    worker = storage.StorageWorker(catalogue, config_path=path)

    write_config(path, "  keep_days_local: 5\n")  # rewritten, same content

    assert worker.reload_if_changed() is False
    assert worker.cfg.keep_days_local == 5


def test_a_broken_config_keeps_the_settings_in_use(catalogue, tmp_path, archive):
    """One typo must not switch the shop's only backup off. The running config
    stays, and the divergence is reported rather than swallowed."""
    path = write_config(tmp_path / "storage.yaml", f"  archive_dir: {archive}\n")
    worker = storage.StorageWorker(catalogue, config_path=path)
    assert worker.cfg.archive_dir == str(archive)

    path.write_text("storage:\n  keep_days: [this is not\n")
    stamp = path.stat().st_mtime
    os.utime(path, (stamp + 10, stamp + 10))

    assert worker.reload_if_changed() is False
    assert worker.cfg.archive_dir == str(archive)   # still archiving
    assert "not valid YAML" in worker.config_error
    assert worker.status()["config_error"]


def test_a_fixed_config_clears_the_error(catalogue, tmp_path):
    path = write_config(tmp_path / "storage.yaml", "  keep_days_local: 5\n")
    worker = storage.StorageWorker(catalogue, config_path=path)
    path.write_text("storage:\n  keep_days: [broken\n")
    stamp = path.stat().st_mtime
    os.utime(path, (stamp + 10, stamp + 10))
    worker.reload_if_changed()
    assert worker.config_error

    write_config(path, "  keep_days_local: 8\n")

    assert worker.reload_if_changed() is True
    assert worker.config_error == ""
    assert worker.cfg.keep_days_local == 8


def test_a_config_file_appearing_later_is_adopted(catalogue, tmp_path, archive):
    """The normal way this gets set up: the worker starts with no storage.yaml
    at all, and somebody creates one afterwards."""
    path = tmp_path / "storage.yaml"
    worker = storage.StorageWorker(catalogue, config_path=path)
    assert worker.cfg.archive_dir == ""

    write_config(path, f"  archive_dir: {archive}\n")

    assert worker.reload_if_changed() is True
    assert worker.cfg.archive_dir == str(archive)


def test_a_run_reloads_before_it_works_not_after(catalogue, data_root, tmp_path, archive):
    """A pass that archives under the old settings and then notices they
    changed has already done the wrong thing once."""
    path = write_config(tmp_path / "storage.yaml", "  archive_dir: ''\n")
    worker = storage.StorageWorker(catalogue, config_path=path)
    clip = make_clip(catalogue, data_root, name="a.mp4")

    write_config(path, f"  archive_dir: {archive}\n")
    worker.run_once()

    assert catalogue.get(clip.id).archived_at   # archived on THIS pass
    assert worker.cfg.archive_dir == str(archive)


def test_config_handed_straight_to_the_worker_wins(catalogue, tmp_path, archive):
    """For a caller that has just written the file itself — a central sync,
    later — so the change lands now rather than up to ten minutes later, and
    the two paths do not then fight over it."""
    path = write_config(tmp_path / "storage.yaml", "  keep_days_local: 5\n")
    worker = storage.StorageWorker(catalogue, config_path=path)

    write_config(path, "  keep_days_local: 12\n")
    worker.apply_config(storage.load_config(path))

    assert worker.cfg.keep_days_local == 12
    assert worker.reload_if_changed() is False  # already up to date


# --------------------------------------------------------------------------
# an unplugged on-demand mount raises; every caller must read that as "gone"
# --------------------------------------------------------------------------


class Unplugged:
    """A path whose exists() raises ENODEV, like autofs with no drive behind it.

    Path.exists() swallows "no such file" and re-raises everything else. With a
    plain mount an absent drive gives the first; with x-systemd.automount it
    gives ENODEV, and the status page answered a question about the archive
    with a 500 — at the one moment that page has a job to do.
    """

    def __init__(self, path: Path):
        self._path = path

    def exists(self):
        raise OSError(19, "No such device")

    def resolve(self):
        return self

    def __truediv__(self, other):
        return Unplugged(self._path / other)

    def __str__(self):
        return str(self._path)


def test_a_raising_path_reads_as_not_there(tmp_path):
    assert storage.reachable(tmp_path) is True
    assert storage.reachable(tmp_path / "nope") is False
    assert storage.reachable(None) is False
    assert storage.reachable(Unplugged(tmp_path / "archive")) is False


def test_the_status_of_an_unplugged_archive_does_not_raise(catalogue, monkeypatch, tmp_path):
    """This is the regression. Asking "is the archive there?" must answer, not
    explode — the answer is what the status page exists to show."""
    cfg = StorageConfig(archive_dir=str(tmp_path / "archive"))
    monkeypatch.setattr(
        type(cfg), "archive_path",
        property(lambda self: Unplugged(tmp_path / "archive")),
    )

    info = storage.status(catalogue, cfg)

    assert info["archive_ready"] is False
    assert info["archive_missing"] is True


def test_an_unplugged_archive_is_refused_not_raised(catalogue, data_root, monkeypatch, tmp_path):
    """And the same for the three passes. Refusing is already the correct
    behaviour for a missing archive; it must not become a crash just because
    the mount reports absence differently."""
    cfg = StorageConfig(
        archive_dir=str(tmp_path / "archive"),
        delete_after_archive=True,
        delete_from_archive=True,
    )
    monkeypatch.setattr(
        type(cfg), "archive_path",
        property(lambda self: Unplugged(tmp_path / "archive")),
    )
    make_clip(catalogue, data_root, days_old=999)

    assert storage.archive_pending(catalogue, cfg, root=data_root).copied == []
    assert storage.prune(catalogue, cfg, root=data_root).deleted == []
    assert "mounted" in storage.prune_archive(catalogue, cfg, root=data_root).skipped_reason
