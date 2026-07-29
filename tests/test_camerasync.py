"""Applying the central camera list.

This is the one path where a cloud service changes what the recorder points at,
and the one that can wipe the file holding the camera passwords. Almost every
test here is about refusing something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from repaircam import camerasync
from repaircam.camerasync import CameraSyncError


def payload(*cameras, revision=1):
    return {"revision": revision, "cameras": list(cameras)}


def cam(workcenter_id=2, **kw):
    base = {
        "odoo_workcenter_id": workcenter_id,
        "name": f"Bench {workcenter_id}",
        "host": "192.168.1.184",
        "username": "admin",
        "password": "s3cret",
        "has_audio": True,
        "enabled": True,
    }
    base.update(kw)
    return base


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    return tmp_path / "cameras.yaml"


def written(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["cameras"]


# --------------------------------------------------------------------------
# what it refuses
# --------------------------------------------------------------------------


def test_a_public_address_is_refused(yaml_path):
    """A compromised or mistyped saar-seva must not be able to aim the recorder
    at a host on the internet."""
    with pytest.raises(CameraSyncError, match="shop-network"):
        camerasync.apply(payload(cam(host="8.8.8.8")), path=yaml_path)
    assert not yaml_path.exists()


def test_a_hostname_is_refused(yaml_path):
    with pytest.raises(CameraSyncError, match="not an IP address"):
        camerasync.apply(payload(cam(host="camera.example.com")), path=yaml_path)


def test_an_empty_list_is_refused(yaml_path):
    """Zero cameras is a bug or a wiped setting, not a shop that removed every
    camera it owns."""
    camerasync.apply(payload(cam()), path=yaml_path)
    before = yaml_path.read_text()

    with pytest.raises(CameraSyncError, match="empty"):
        camerasync.apply(payload(revision=2), path=yaml_path)
    assert yaml_path.read_text() == before


def test_one_bad_row_rejects_the_whole_payload(yaml_path):
    """A half-applied camera list is worse than a stale one — nothing tells you
    which half took."""
    camerasync.apply(payload(cam(2)), path=yaml_path)
    before = written(yaml_path)

    with pytest.raises(CameraSyncError):
        camerasync.apply(payload(cam(2), cam(3, host="1.2.3.4"), revision=2), path=yaml_path)
    assert written(yaml_path) == before


def test_a_payload_with_no_revision_is_refused(yaml_path):
    with pytest.raises(CameraSyncError, match="revision"):
        camerasync.apply({"cameras": [cam()]}, path=yaml_path)


def test_the_same_work_centre_twice_is_refused(yaml_path):
    with pytest.raises(CameraSyncError, match="twice"):
        camerasync.apply(payload(cam(2), cam(2)), path=yaml_path)


# --------------------------------------------------------------------------
# what it writes
# --------------------------------------------------------------------------


def test_a_new_bench_is_added(yaml_path):
    result = camerasync.apply(payload(cam(2)), path=yaml_path)

    assert result.added == ["WC2"]
    entry = written(yaml_path)["WC2"]
    assert entry["host"] == "192.168.1.184"
    assert entry["odoo_workcenter_id"] == 2
    assert entry["password"] == "s3cret"


def test_an_existing_bench_keeps_its_code(yaml_path):
    """Bench codes are in every clip filename and every path on disk, so
    renaming one would orphan its recordings."""
    yaml_path.write_text(yaml.safe_dump({"cameras": {
        "BENCH-A": {"name": "Bench A", "host": "192.168.1.10", "odoo_workcenter_id": 2,
                    "password": "old"},
    }}))

    result = camerasync.apply(payload(cam(2, host="192.168.1.99")), path=yaml_path)

    assert result.updated == ["BENCH-A"]
    assert "WC2" not in written(yaml_path)
    assert written(yaml_path)["BENCH-A"]["host"] == "192.168.1.99"


def test_local_only_fields_survive(yaml_path):
    """The camera model is not saar-seva's to know about, or to drop."""
    yaml_path.write_text(yaml.safe_dump({"cameras": {
        "WC2": {"name": "Bench 2", "host": "192.168.1.184", "odoo_workcenter_id": 2,
                "model": "TP-Link VIGI C540V", "password": "old"},
    }}))

    camerasync.apply(payload(cam(2)), path=yaml_path)

    assert written(yaml_path)["WC2"]["model"] == "TP-Link VIGI C540V"


def test_nothing_is_rewritten_when_nothing_changed(yaml_path):
    camerasync.apply(payload(cam(2)), path=yaml_path)
    stamp = yaml_path.stat().st_mtime_ns

    result = camerasync.apply(payload(cam(2), revision=2), path=yaml_path)

    assert result.unchanged
    assert yaml_path.stat().st_mtime_ns == stamp


def test_the_previous_file_is_kept(yaml_path):
    camerasync.apply(payload(cam(2)), path=yaml_path)
    camerasync.apply(payload(cam(2, host="192.168.1.50"), revision=2), path=yaml_path)

    backup = yaml_path.with_suffix(yaml_path.suffix + ".bak")
    assert yaml.safe_load(backup.read_text())["cameras"]["WC2"]["host"] == "192.168.1.184"


def test_the_file_is_not_world_readable(yaml_path):
    """It holds the camera passwords."""
    camerasync.apply(payload(cam(2)), path=yaml_path)
    assert yaml_path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def test_a_blank_password_never_wipes_a_real_one(yaml_path):
    """A bench that loses its password stops recording, and does it quietly."""
    camerasync.apply(payload(cam(2, password="s3cret")), path=yaml_path)

    camerasync.apply(payload(cam(2, password=""), revision=2), path=yaml_path)

    assert written(yaml_path)["WC2"]["password"] == "s3cret"


def test_a_new_password_does_replace_it(yaml_path):
    camerasync.apply(payload(cam(2, password="old")), path=yaml_path)
    camerasync.apply(payload(cam(2, password="new"), revision=2), path=yaml_path)

    assert written(yaml_path)["WC2"]["password"] == "new"


# --------------------------------------------------------------------------
# removal, and benches that are mid-recording
# --------------------------------------------------------------------------


def test_a_disabled_bench_is_dropped(yaml_path):
    camerasync.apply(payload(cam(2), cam(3)), path=yaml_path)

    result = camerasync.apply(payload(cam(2), cam(3, enabled=False), revision=2), path=yaml_path)

    assert result.removed == ["WC3"]
    assert set(written(yaml_path)) == {"WC2"}


def test_a_bench_saar_seva_does_not_know_is_dropped(yaml_path):
    yaml_path.write_text(yaml.safe_dump({"cameras": {
        "WC2": {"name": "Bench 2", "host": "192.168.1.184", "odoo_workcenter_id": 2},
        "WC9": {"name": "Old bench", "host": "192.168.1.9", "odoo_workcenter_id": 9},
    }}))

    result = camerasync.apply(payload(cam(2)), path=yaml_path)

    assert result.removed == ["WC9"]
    assert set(written(yaml_path)) == {"WC2"}


def test_a_recording_bench_is_never_rewritten(yaml_path):
    """Footage in flight cannot be reproduced; a config change can wait."""
    camerasync.apply(payload(cam(2, host="192.168.1.184")), path=yaml_path)

    result = camerasync.apply(
        payload(cam(2, host="192.168.1.50"), revision=2), busy={"WC2"}, path=yaml_path
    )

    assert result.deferred == ["WC2"]
    assert written(yaml_path)["WC2"]["host"] == "192.168.1.184"


def test_a_recording_bench_is_never_removed(yaml_path):
    camerasync.apply(payload(cam(2), cam(3)), path=yaml_path)

    result = camerasync.apply(
        payload(cam(2), cam(3, enabled=False), revision=2), busy={"WC3"}, path=yaml_path
    )

    assert result.deferred == ["WC3"]
    assert set(written(yaml_path)) == {"WC2", "WC3"}


def test_a_deferred_sync_is_not_complete(yaml_path):
    """`complete` is what tells the caller not to record this revision as
    applied — otherwise the held-back bench would never be retried."""
    camerasync.apply(payload(cam(2)), path=yaml_path)

    result = camerasync.apply(
        payload(cam(2, host="192.168.1.50"), revision=2), busy={"WC2"}, path=yaml_path
    )
    assert result.complete is False

    idle = camerasync.apply(payload(cam(2, host="192.168.1.50"), revision=2), path=yaml_path)
    assert idle.complete is True
    assert written(yaml_path)["WC2"]["host"] == "192.168.1.50"


def test_a_recording_bench_does_not_block_the_others(yaml_path):
    camerasync.apply(payload(cam(2), cam(3)), path=yaml_path)

    result = camerasync.apply(
        payload(cam(2, host="192.168.1.50"), cam(3, host="192.168.1.51"), revision=2),
        busy={"WC2"}, path=yaml_path,
    )

    assert result.deferred == ["WC2"]
    assert result.updated == ["WC3"]
    assert written(yaml_path)["WC3"]["host"] == "192.168.1.51"


def test_addresses_that_only_look_private_are_refused(yaml_path):
    """ipaddress.is_private would wave these through. None of them is a camera
    on a shop LAN, and 203.0.113.x reads as a perfectly ordinary address."""
    for host in ("203.0.113.5", "198.51.100.7", "127.0.0.1", "169.254.1.1", "172.32.1.1"):
        with pytest.raises(CameraSyncError, match="shop-network"):
            camerasync.apply(payload(cam(host=host)), path=yaml_path)
    assert not yaml_path.exists()


def test_every_real_shop_range_is_allowed(yaml_path):
    for host in ("192.168.1.184", "10.0.0.5", "172.16.0.1", "172.31.255.254"):
        camerasync.apply(payload(cam(host=host), revision=1), path=yaml_path)
        assert written(yaml_path)["WC2"]["host"] == host
