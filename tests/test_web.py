"""The web UI — the pages a technician actually taps."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask is only needed for the web UI")

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
