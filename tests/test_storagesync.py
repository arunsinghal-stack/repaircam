"""Applying the retention policy saar-seva holds.

A cloud service telling a shop machine how long to keep the shop's only record
of its own work is the direction that deserves two opinions, so most of this
file is about what the recorder refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from repaircam import storage, storagesync
from repaircam.storagesync import StorageSyncError


def payload(revision=3, **kw) -> dict:
    body = {
        "revision": revision,
        "retention": {"default_days": 30, "by_source": {"repair": 30, "packing": 45}},
        "free_space": {"min_gb": 20, "warn_gb": 50},
    }
    body.update(kw)
    return body


LOCAL = """
storage:
  archive_dir: "/mnt/backup-drive/RepairCam"
  keep_days_local: 5
  delete_after_archive: false
  delete_from_archive: false
  keep_days: 14
"""


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "storage.yaml"
    path.write_text(LOCAL)
    return path


def read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["storage"]


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


def test_the_policy_is_written(store):
    result = storagesync.apply(payload(), path=store)

    assert result.revision == 3
    values = read(store)
    assert values["keep_days"] == 30
    assert values["keep_days_by_source"] == {"repair": 30, "packing": 45}
    assert values["min_free_gb"] == 20
    assert values["warn_free_gb"] == 50


def test_this_machines_own_settings_survive_every_sync(store):
    """The whole reason the sync merges rather than replaces. An archive_dir
    lost to a sync is a shop that has quietly stopped making second copies."""
    storagesync.apply(payload(), path=store)

    values = read(store)
    assert values["archive_dir"] == "/mnt/backup-drive/RepairCam"
    assert values["keep_days_local"] == 5
    assert values["delete_after_archive"] is False
    assert values["delete_from_archive"] is False


def test_the_file_says_which_half_is_which(store):
    """Somebody opening it to change a number needs to know which of their
    edits will survive. An unlabelled merged file teaches that by silently
    reverting a change a week later."""
    storagesync.apply(payload(revision=7), path=store)

    text = store.read_text()
    assert "this recorder's own" in text
    assert "revision 7" in text
    assert "overwritten" in text
    assert "storage.example.yaml" in text  # where the explanations went


def test_the_result_is_still_readable_by_the_recorder(store):
    storagesync.apply(payload(), path=store)

    cfg = storage.load_config(store)
    assert cfg.archive_dir == "/mnt/backup-drive/RepairCam"
    assert cfg.keep_days_local == 5
    assert cfg.keep_days_for("packing") == 45


def test_an_unrelated_key_somebody_added_is_kept(store):
    store.write_text(LOCAL + "  something_we_invented_later: yes\n")
    storagesync.apply(payload(), path=store)
    assert read(store)["something_we_invented_later"] is True


def test_a_second_identical_sync_changes_nothing(store):
    storagesync.apply(payload(), path=store)
    before = store.read_text()

    again = storagesync.apply(payload(revision=4), path=store)

    assert again.unchanged
    assert store.read_text() == before


def test_the_previous_file_is_kept_as_a_backup(store):
    storagesync.apply(payload(), path=store)
    assert "keep_days: 14" in store.with_suffix(".yaml.bak").read_text()


def test_no_file_yet_is_not_an_error(tmp_path):
    path = tmp_path / "storage.yaml"
    storagesync.apply(payload(), path=path)
    assert read(path)["keep_days"] == 30


def test_an_unreadable_file_does_not_stop_a_policy_landing(store):
    """It is about to be replaced by one that parses."""
    store.write_text("storage:\n  keep_days: [broken\n")
    storagesync.apply(payload(), path=store)
    assert read(store)["keep_days"] == 30


# --------------------------------------------------------------------------
# revision 0 — nobody has saved a policy
# --------------------------------------------------------------------------


def test_revision_zero_applies_nothing(store):
    """A shop not using the admin panel must not have its retention replaced by
    somebody else's defaults."""
    before = store.read_text()

    result = storagesync.apply(payload(revision=0), path=store)

    assert result.unchanged
    assert store.read_text() == before


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("archive_dir", "/mnt/somewhere-else"),
    ("keep_days_local", 30),
    ("delete_after_archive", True),
    ("delete_from_archive", True),
])
def test_a_policy_naming_this_machines_settings_is_refused_whole(store, field, value):
    """Not filtered — refused. A server sending these is a version mismatch or
    something worse, and dropping the field quietly would hide both.

    delete_from_archive is the sharp one: accepted, it would let a web form
    start destroying footage on a machine nobody was standing at.
    """
    before = store.read_text()

    with pytest.raises(StorageSyncError, match="belongs to this recorder"):
        storagesync.apply(payload(**{field: value}), path=store)

    assert store.read_text() == before


def test_the_same_key_nested_in_the_payload_is_also_refused(store):
    with pytest.raises(StorageSyncError, match="belongs to this recorder"):
        storagesync.apply(
            payload(retention={"default_days": 30, "delete_from_archive": True}),
            path=store,
        )


@pytest.mark.parametrize("days", [0, 1, 6, -5])
def test_a_window_under_a_week_is_refused(store, days):
    """0 means 'delete everything' and must not be reachable by a typo."""
    with pytest.raises(StorageSyncError, match="outside the"):
        storagesync.apply(
            payload(retention={"default_days": days, "by_source": {}}), path=store
        )


def test_an_absurd_window_is_refused(store):
    with pytest.raises(StorageSyncError, match="outside the"):
        storagesync.apply(
            payload(retention={"default_days": 99999, "by_source": {}}), path=store
        )


def test_a_window_that_is_not_a_number_is_refused(store):
    with pytest.raises(StorageSyncError, match="not a number of days"):
        storagesync.apply(
            payload(retention={"default_days": "soon", "by_source": {}}), path=store
        )


def test_a_warning_below_the_stop_level_is_refused(store):
    with pytest.raises(StorageSyncError, match="could never warn"):
        storagesync.apply(
            payload(free_space={"min_gb": 50, "warn_gb": 20}), path=store
        )


def test_a_free_space_floor_too_low_to_finish_a_repair_is_refused(store):
    with pytest.raises(StorageSyncError, match="below"):
        storagesync.apply(payload(free_space={"min_gb": 1, "warn_gb": 50}), path=store)


def test_something_that_is_not_a_policy_at_all_is_refused(store):
    with pytest.raises(StorageSyncError, match="expected a storage policy"):
        storagesync.apply(["not", "a", "policy"], path=store)


def test_a_refused_policy_leaves_no_temp_file_behind(store, tmp_path):
    with pytest.raises(StorageSyncError):
        storagesync.apply(payload(archive_dir="/elsewhere"), path=store)
    assert not list(tmp_path.glob(".storage-*"))


# --------------------------------------------------------------------------
# the two halves together: a shortened window is written but HELD
# --------------------------------------------------------------------------


def test_a_shortened_window_arriving_centrally_is_still_held(store, catalogue):
    """The point of building the guard before this. A number typed on a web
    form reaches the file, and then stops: nothing is deleted until somebody at
    the shop accepts what it would cost."""
    storagesync.apply(payload(), path=store)
    storage.note_window_changes(catalogue, storage.load_config(store))

    storagesync.apply(
        payload(revision=4, retention={"default_days": 30, "by_source": {"packing": 8}}),
        path=store,
    )
    storage.note_window_changes(catalogue, storage.load_config(store))

    hold = storage.retention_hold(catalogue)
    assert hold["changes"] == [{"source": "packing", "from": 45, "to": 8}]
