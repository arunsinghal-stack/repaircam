"""The web UI — the pages a technician actually taps."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask is only needed for the web UI")

from repaircam import storage  # noqa: E402
from repaircam.catalogue import JobLabels, Recording, utcnow  # noqa: E402
from repaircam.recorder import Recorder  # noqa: E402
from repaircam.web import create_app  # noqa: E402

from .conftest import StubBackend  # noqa: E402

CAMERAS = """
cameras:
  WC2:
    name: "Bench 2"
    host: "192.168.0.133"
    password: "s3cret"
"""


@pytest.fixture
def app(tmp_path: Path, data_root: Path, monkeypatch, camera):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text(CAMERAS)
    monkeypatch.setenv("REPAIRCAM_CAMERAS", str(config_path))

    application = create_app(TESTING=True)

    # Swap in the stub camera so the tests never touch ffmpeg or the network.
    pool = application.extensions["recorders"]
    pool._recorders["WC2"] = Recorder(
        "WC2",
        backend=StubBackend(camera),
        catalogue=application.extensions["catalogue"],
        data_root=data_root,
    )
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def test_dashboard_lists_the_benches(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"WC2" in response.data
    assert b"Bench 2" in response.data


def test_dashboard_never_shows_the_camera_password(client):
    assert b"s3cret" not in client.get("/").data


def test_bench_page_renders(client):
    response = client.get("/bench/WC2")
    assert response.status_code == 200
    assert b"Start recording" in response.data


def test_start_stop_done_through_the_ui(client, app):
    started = client.post("/bench/WC2/start", data={"mo_name": "WH/MO/5", "operation": "Battery"})
    assert started.status_code == 302
    assert app.extensions["recorders"].get("WC2").status()["recording"] is True

    client.post("/bench/WC2/stop")
    assert app.extensions["recorders"].get("WC2").status()["state"] == "paused"

    done = client.post("/bench/WC2/done", data={})
    assert done.status_code == 302
    assert "/clip/" in done.headers["Location"]

    recordings = app.extensions["catalogue"].list()
    assert len(recordings) == 1
    assert recordings[0].labels.mo_name == "WH/MO/5"


def test_done_without_recording_shows_a_message_not_a_crash(client):
    response = client.post("/bench/WC2/done", data={}, follow_redirects=True)
    assert response.status_code == 200
    assert b"nothing to finish" in response.data


def test_cancel_discards(client, app):
    client.post("/bench/WC2/start", data={"mo_name": "WH/MO/6"})
    client.post("/bench/WC2/cancel")
    assert app.extensions["recorders"].get("WC2").status()["busy"] is False
    assert app.extensions["catalogue"].count() == 0


def test_api_status_is_json(client):
    payload = client.get("/api/status").get_json()
    assert "WC2" in payload["benches"]
    assert payload["benches"]["WC2"]["state"] == "idle"
    assert "clips" in payload["stats"]


def test_library_and_search(client, app):
    app.extensions["catalogue"].add(
        Recording(
            work_center="WC2",
            path="recordings/2026-07-27/a.mp4",
            started_at=utcnow(),
            duration_s=60,
            labels=JobLabels(mo_name="WH/MO/9", device="Galaxy A14"),
        )
    )
    assert b"Galaxy A14" in client.get("/library").data
    assert b"Galaxy A14" in client.get("/library?q=Galaxy").data
    assert b"Galaxy A14" not in client.get("/library?q=Nokia").data


def test_clip_page_and_relabelling(client, app, data_root: Path):
    clip = data_root / "recordings" / "2026-07-27" / "a.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"video")

    recording = app.extensions["catalogue"].add(
        Recording(
            work_center="WC2",
            path="recordings/2026-07-27/a.mp4",
            started_at=utcnow(),
            duration_s=60,
            labels=JobLabels(),
        )
    )

    assert client.get(f"/clip/{recording.id}").status_code == 200
    assert client.get(f"/clip/{recording.id}/video").status_code == 200

    client.post(f"/clip/{recording.id}/labels", data={"mo_name": "WH/MO/12", "device": "Redmi"})
    assert app.extensions["catalogue"].get(recording.id).labels.mo_name == "WH/MO/12"


def test_missing_clip_file_is_reported_not_served(client, app):
    recording = app.extensions["catalogue"].add(
        Recording(work_center="WC2", path="recordings/gone.mp4", started_at=utcnow())
    )
    assert client.get(f"/clip/{recording.id}").status_code == 200  # page still renders
    assert client.get(f"/clip/{recording.id}/video").status_code == 404


def test_path_traversal_is_refused(client, app):
    """A hand-edited or corrupted row must not turn into a file server."""
    recording = app.extensions["catalogue"].add(
        Recording(work_center="WC2", path="../../../../etc/passwd", started_at=utcnow())
    )
    assert client.get(f"/clip/{recording.id}/video").status_code in (400, 404)


def test_unknown_clip_is_404(client):
    assert client.get("/clip/4242").status_code == 404


def test_status_page_renders(client):
    response = client.get("/status")
    assert response.status_code == 200
    assert b"Data directory" in response.data


def _configure_storage(app, tmp_path: Path, monkeypatch, body: str) -> Path:
    """Write a storage.yaml and make the running app use it.

    The worker picks a changed file up on its next pass, which these tests are
    not going to wait ten minutes for, so its copy is set directly.
    """
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    path = tmp_path / "storage.yaml"
    path.write_text(f"storage:\n  archive_dir: {archive}\n{body}")
    monkeypatch.setenv("REPAIRCAM_STORAGE", str(path))
    worker = app.extensions.get("storage")
    if worker:
        worker.cfg = storage.load_config(path)
    return archive


def test_status_states_the_two_retention_windows_apart(client, app, tmp_path: Path, monkeypatch):
    """They are not the same promise. One frees the laptop and loses nothing;
    the other ends the footage. Shown as one number, somebody reads the shop's
    30-day policy as what this disk holds."""
    _configure_storage(app, tmp_path, monkeypatch, (
        "  keep_days_local: 5\n"
        "  delete_after_archive: true\n  delete_from_archive: true\n"
        "  keep_days_by_source:\n    repair: 30\n    packing: 45\n"
    ))

    page = client.get("/status").data.decode()

    assert "On this recorder:" in page
    assert "5 days" in page
    assert "In the archive:" in page
    assert "repair" in page and "30 days" in page
    assert "packing" in page and "45 days" in page
    assert "gone for good" in page


def test_status_warns_when_nothing_ever_leaves_the_archive(client, app, tmp_path: Path, monkeypatch):
    """The default, and the honest thing to say about it: ~950 GB a month
    arriving at three busy benches, and nothing leaving."""
    _configure_storage(app, tmp_path, monkeypatch, "")

    page = client.get("/status").data.decode()

    assert "delete_from_archive" in page
    assert "only grows" in page


def test_status_page_surfaces_unsaved_footage(client, data_root: Path):
    """Orphaned segments are in no other page — the status page is where the
    owner finds out footage exists but never became a clip."""
    directory = data_root / "segments" / "WC2" / "20260727-101500"
    directory.mkdir(parents=True)
    (directory / "seg-001.mp4").write_bytes(b"video")

    page = client.get("/status").data
    assert b"Unsaved footage" in page
    assert b"recover --all" in page


def test_status_page_stays_quiet_when_nothing_is_orphaned(client):
    assert b"Unsaved footage" not in client.get("/status").data


def test_status_says_the_trigger_is_off_when_unconfigured(client, monkeypatch, tmp_path: Path):
    """The normal state today — it must read as fine, not broken."""
    monkeypatch.setenv("REPAIRCAM_SAARSEVA", str(tmp_path / "nope.yaml"))
    page = client.get("/status").data
    assert b"Automatic start/stop" in page
    assert b"technicians start and stop recordings themselves" in page


def test_bench_thumbnail_stays_on_the_cheap_stream(client, camera, monkeypatch):
    """The page thumbnail must not steal bandwidth from a live recording.

    The focus test is the opposite case and uses the main stream — see
    tests/test_snapshot_stream.py.
    """
    stub = StubBackend(camera)
    monkeypatch.setattr("repaircam.web.routes.build_backend", lambda _c: stub)

    assert client.get("/bench/WC2/snapshot.jpg").status_code == 200
    assert stub.snapshots == ["sub"]


def test_status_page_shows_links_that_never_reached_odoo(client, app):
    """Nothing retries these, so if the page does not show them, nobody ever
    learns a video is missing from its job."""
    from repaircam.catalogue import JobLabels, Recording, utcnow

    cat = app.extensions["catalogue"]
    clip = cat.add(
        Recording(
            work_center="WC2",
            path="recordings/a.mp4",
            started_at=utcnow(),
            labels=JobLabels(mo_name="WH/MO/42"),
            source="repair",
            source_ref="gone",
        )
    )
    cat.mark_link_failed(clip.id, "POST /trc/recordings failed: HTTP 404")

    page = client.get("/status").data
    assert b"Links that never reached Odoo" in page
    assert b"WH/MO/42" in page
    assert b"HTTP 404" in page


# --------------------------------------------------------------------------
# a clip that has aged off this machine
# --------------------------------------------------------------------------


def _archived_clip(app, tmp_path, monkeypatch, *, still_there=True):
    """A clip whose local copy has been pruned, with a copy on the archive."""
    from repaircam.catalogue import JobLabels, Recording, utcnow

    archive = tmp_path / "archive"
    (archive / "recordings").mkdir(parents=True)
    store = tmp_path / "storage.yaml"
    store.write_text(f"storage:\n  archive_dir: {archive}\n")
    monkeypatch.setenv("REPAIRCAM_STORAGE", str(store))

    copied = archive / "recordings" / "old.mp4"
    if still_there:
        copied.write_bytes(b"archived-video")

    cat = app.extensions["catalogue"]
    clip = cat.add(Recording(
        work_center="WC2",
        path="recordings/old.mp4",          # deliberately not on the local disk
        started_at=utcnow(),
        labels=JobLabels(mo_name="WH/MO/42"),
    ))
    cat.mark_archived(clip.id, str(copied))
    return clip


def test_a_pruned_clip_still_plays_from_the_archive(client, app, tmp_path, monkeypatch):
    """Otherwise the first prune turns every older link in the Odoo chatter into
    a dead end, for footage sitting on the archive disk."""
    clip = _archived_clip(app, tmp_path, monkeypatch)

    response = client.get(f"/clip/{clip.id}/video")

    assert response.status_code == 200
    assert response.data == b"archived-video"


def test_the_page_says_it_is_playing_from_the_archive(client, app, tmp_path, monkeypatch):
    clip = _archived_clip(app, tmp_path, monkeypatch)
    assert b"Playing from the archive" in client.get(f"/clip/{clip.id}").data


def test_expired_footage_says_it_expired(client, app, tmp_path, monkeypatch):
    """Retention working is not a fault, and must not be reported as one. Someone
    following an old link deserves an answer, not a dead end that sends them
    hunting for a file nobody will ever find."""
    clip = _archived_clip(app, tmp_path, monkeypatch, still_there=False)
    app.extensions["catalogue"].mark_archive_deleted(clip.id)

    page = client.get(f"/clip/{clip.id}").data

    assert b"passed its retention window" in page
    assert b"missing from disk" not in page
    # Nothing to keep any more, so the button that promises it is not offered.
    # (The words still appear, in the advice about marking clips before they go.)
    assert f"/clip/{clip.id}/keep".encode() not in page


def test_an_unreachable_archive_says_so_rather_than_just_404(client, app, tmp_path, monkeypatch):
    """'Missing from disk' would send somebody looking for footage that is fine,
    on a disk that is merely unmounted."""
    clip = _archived_clip(app, tmp_path, monkeypatch, still_there=False)

    page = client.get(f"/clip/{clip.id}").data

    assert b"is not readable right now" in page
    assert b"mounted" in page


def test_a_clip_outside_the_archive_is_refused(client, app, tmp_path, monkeypatch):
    """A hand-edited archive_path must not talk the server into serving
    anything it likes."""
    clip = _archived_clip(app, tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"not yours")
    app.extensions["catalogue"].mark_archived(clip.id, str(outside))

    assert client.get(f"/clip/{clip.id}/video").status_code == 404


def test_keeping_a_clip_from_the_page(client, app):
    from repaircam.catalogue import JobLabels, Recording, utcnow

    cat = app.extensions["catalogue"]
    clip = cat.add(Recording(work_center="WC2", path="recordings/a.mp4",
                             started_at=utcnow(), labels=JobLabels(mo_name="WH/MO/1")))

    client.post(f"/clip/{clip.id}/keep", data={"keep": "1"}, follow_redirects=True)
    assert cat.get(clip.id).keep == 1

    client.post(f"/clip/{clip.id}/keep", data={"keep": "0"}, follow_redirects=True)
    assert cat.get(clip.id).keep == 0


def test_a_stale_archive_error_is_dated_not_contradicted(client, app, tmp_path, monkeypatch):
    """The green 'archive reachable' line is measured on page load; last_error
    is up to ten minutes old. Shown undated side by side, the page contradicts
    itself — which is how people learn to stop reading it."""
    import time

    _configure_storage(app, tmp_path, monkeypatch, "")
    worker = app.extensions["storage"]
    worker.last_error = "the archive at /mnt/backup-drive/RepairCam is not there"
    worker.last_run_at = time.time() - 120

    page = client.get("/status").data.decode()

    assert "at the last check" in page
    assert "reachable again now" in page
    assert "storage --archive" in page


def test_a_current_archive_error_is_not_softened(client, app, tmp_path, monkeypatch):
    """When the archive really is gone right now, it stays an error."""
    import time

    store = tmp_path / "storage.yaml"
    store.write_text(f"storage:\n  archive_dir: {tmp_path / 'not-mounted'}\n")
    monkeypatch.setenv("REPAIRCAM_STORAGE", str(store))
    worker = app.extensions["storage"]
    worker.cfg = storage.load_config(store)
    worker.last_error = "the archive is not there"
    worker.last_run_at = time.time() - 120

    page = client.get("/status").data.decode()

    assert "reachable again now" not in page
    assert 'class="err small"' in page


def test_a_held_retention_reduction_is_on_the_status_page(client, app, tmp_path, monkeypatch):
    """The one change that destroys footage. It is held, not applied, and this
    is where the shop finds out it is waiting."""
    _configure_storage(app, tmp_path, monkeypatch, (
        "  keep_days: 30\n  delete_from_archive: true\n"
        "  keep_days_by_source:\n    repair: 30\n    packing: 45\n"
    ))
    cat = app.extensions["catalogue"]
    storage.note_window_changes(cat, storage.load_config())
    short = tmp_path / "short.yaml"
    short.write_text(
        f"storage:\n  archive_dir: {tmp_path / 'archive'}\n  keep_days: 30\n"
        "  delete_from_archive: true\n"
        "  keep_days_by_source:\n    repair: 30\n    packing: 4\n"
    )
    storage.note_window_changes(cat, storage.load_config(short))

    page = client.get("/status").data.decode()

    assert "was shortened and is NOT in force" in page
    assert "45" in page and "4 days" in page
    assert "Nothing has been deleted" in page
    assert "--accept-retention" in page
