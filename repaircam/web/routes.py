"""Web routes: dashboard, bench recording page, library, status."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import __version__, config, ffmpeg, recovery, saarseva, storage
from ..backends import CaptureError, build_backend
from ..catalogue import Catalogue, JobLabels, read_sidecar
from ..config import ConfigError
from ..recorder import RecorderError, RecorderPool

log = logging.getLogger(__name__)

bp = Blueprint("repaircam", __name__)

MJPEG_BOUNDARY = "repaircamframe"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def catalogue() -> Catalogue:
    return current_app.extensions["catalogue"]


def recorders() -> RecorderPool:
    return current_app.extensions["recorders"]


def labels_from_form(form) -> JobLabels:
    return JobLabels(
        mo_name=(form.get("mo_name") or "").strip(),
        operation=(form.get("operation") or "").strip(),
        device=(form.get("device") or "").strip(),
        imei=(form.get("imei") or "").strip(),
        technician=(form.get("technician") or "").strip(),
        notes=(form.get("notes") or "").strip(),
    )


def clip_path(recording) -> Path:
    """Resolve a catalogue row to a file, refusing anything outside the data dir.

    Paths in the database are relative, but a corrupted or hand-edited row must
    not be able to talk the server into serving ``/etc/passwd``.
    """
    root = config.data_dir()
    path = (root / recording.path).resolve()
    try:
        # Path.is_relative_to() would read better but is Python 3.9+, and the
        # shop recorder runs 3.8. relative_to() raising ValueError is the same
        # test and works everywhere.
        path.relative_to(root)
    except ValueError:
        abort(400, "recording path is outside the data directory")
    if not path.exists():
        abort(404, "the video file for this recording is missing from disk")
    return path


@bp.app_errorhandler(ConfigError)
def handle_config_error(exc: ConfigError):
    return render_template("error.html", title="Camera configuration", message=str(exc)), 500


@bp.app_context_processor
def inject_globals() -> dict:
    return {"version": __version__}


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@bp.route("/")
def dashboard():
    try:
        statuses = recorders().statuses()
    except ConfigError as exc:
        return render_template("error.html", title="No cameras configured", message=str(exc)), 500

    cameras = config.load_cameras()
    return render_template(
        "dashboard.html",
        statuses=statuses,
        cameras=cameras,
        stats=catalogue().stats(),
        recent=catalogue().list(limit=8),
    )


@bp.route("/api/status")
def api_status():
    """Polled by every page so timers and buttons stay live."""
    return jsonify(
        {
            "benches": recorders().statuses(),
            "stats": catalogue().stats(),
        }
    )


# --------------------------------------------------------------------------
# bench: the page a technician uses
# --------------------------------------------------------------------------


@bp.route("/bench/<work_center>")
def bench(work_center: str):
    try:
        camera = config.get_camera(work_center)
    except ConfigError as exc:
        # A mistyped bench in the URL is a missing page, not a broken recorder.
        return render_template("error.html", title="No such bench", message=str(exc)), 404
    recorder = recorders().get(work_center)
    return render_template(
        "bench.html",
        camera=camera,
        status=recorder.status(),
        # The bench is where somebody is about to press Start, so it is where a
        # disk about to refuse them belongs — not only on a status page nobody
        # opens until something has already gone wrong.
        disk=storage.disk_report().as_dict(),
        recent=catalogue().list(work_center=work_center, limit=5),
        events=catalogue().recent_events(limit=12, work_center=work_center),
    )


@bp.post("/bench/<work_center>/start")
def bench_start(work_center: str):
    recorder = recorders().get(work_center)
    try:
        recorder.start(labels_from_form(request.form))
        flash(f"Recording started on {work_center}.", "ok")
    except RecorderError as exc:
        flash(str(exc), "error")
    return redirect(url_for("repaircam.bench", work_center=work_center))


@bp.post("/bench/<work_center>/stop")
def bench_stop(work_center: str):
    recorder = recorders().get(work_center)
    try:
        segment = recorder.stop()
        flash(
            f"Paused after {segment.duration:.0f}s. Press Start to continue, or Done to save.",
            "ok",
        )
    except RecorderError as exc:
        flash(str(exc), "error")
    return redirect(url_for("repaircam.bench", work_center=work_center))


@bp.post("/bench/<work_center>/done")
def bench_done(work_center: str):
    recorder = recorders().get(work_center)
    try:
        recording = recorder.done(labels_from_form(request.form))
    except RecorderError as exc:
        flash(str(exc), "error")
        return redirect(url_for("repaircam.bench", work_center=work_center))
    flash(f"Saved {recording.duration_hms} for {recording.title}.", "ok")
    return redirect(url_for("repaircam.clip", recording_id=recording.id))


@bp.post("/bench/<work_center>/cancel")
def bench_cancel(work_center: str):
    recorders().get(work_center).cancel()
    flash(f"Discarded the recording on {work_center}.", "ok")
    return redirect(url_for("repaircam.bench", work_center=work_center))


@bp.route("/bench/<work_center>/preview.mjpg")
def bench_preview(work_center: str):
    """Live view, as MJPEG.

    Browsers cannot play RTSP, so ffmpeg re-packages the camera's *sub* stream
    into a stream of JPEGs. It is the sub-stream on purpose: the preview must
    never take bandwidth away from a recording in progress.
    """
    camera = config.get_camera(work_center)
    backend = build_backend(camera)

    def frames():
        try:
            for jpeg in backend.preview_frames(fps=6, width=640):
                yield (
                    b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                )
        except (CaptureError, ffmpeg.FFmpegError) as exc:
            log.warning("preview for %s stopped: %s", work_center, exc)
        except GeneratorExit:
            pass  # the technician closed the page; ffmpeg is torn down in the backend

    return Response(
        frames(),
        mimetype=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )


@bp.route("/bench/<work_center>/snapshot.jpg")
def bench_snapshot(work_center: str):
    """A single still — the fallback when MJPEG is not working, and the focus test."""
    camera = config.get_camera(work_center)
    dest = config.ensure_data_dirs()["snapshots"] / f"{work_center}_latest.jpg"
    try:
        # Sub-stream: this is a thumbnail, and it must not steal bandwidth
        # from a recording in progress. The focus test uses the main stream.
        build_backend(camera).snapshot(dest, stream="sub")
    except (CaptureError, ffmpeg.FFmpegError) as exc:
        abort(503, f"could not reach the camera: {exc}")
    return send_file(dest, mimetype="image/jpeg", max_age=0)


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------


@bp.route("/library")
def library():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 25
    recordings = catalogue().list(
        work_center=request.args.get("work_center") or None,
        search=(request.args.get("q") or "").strip() or None,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    return render_template(
        "library.html",
        recordings=recordings,
        page=page,
        has_next=len(recordings) == per_page,
        query=request.args.get("q", ""),
        work_center=request.args.get("work_center", ""),
        cameras=config.load_cameras(),
        stats=catalogue().stats(),
    )


@bp.route("/clip/<int:recording_id>")
def clip(recording_id: int):
    recording = catalogue().get(recording_id)
    if recording is None:
        abort(404)
    path = config.data_dir() / recording.path
    return render_template(
        "clip.html",
        recording=recording,
        exists=path.exists(),
        sidecar=read_sidecar(path),
    )


@bp.route("/clip/<int:recording_id>/video")
def clip_video(recording_id: int):
    recording = catalogue().get(recording_id)
    if recording is None:
        abort(404)
    # conditional=True gives HTTP range support, so the browser can seek in a
    # long clip without downloading all of it first.
    return send_file(clip_path(recording), mimetype="video/mp4", conditional=True)


@bp.route("/clip/<int:recording_id>/download")
def clip_download(recording_id: int):
    recording = catalogue().get(recording_id)
    if recording is None:
        abort(404)
    path = clip_path(recording)
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.route("/clip/<int:recording_id>/sidecar.json")
def clip_sidecar(recording_id: int):
    recording = catalogue().get(recording_id)
    if recording is None:
        abort(404)
    data = read_sidecar(config.data_dir() / recording.path)
    if not data:
        abort(404, "no sidecar for this clip")
    return jsonify(data)


@bp.post("/clip/<int:recording_id>/labels")
def clip_labels(recording_id: int):
    if catalogue().get(recording_id) is None:
        abort(404)
    catalogue().update_labels(recording_id, labels_from_form(request.form))
    catalogue().log_event("relabel", detail=f"clip {recording_id}", recording_id=recording_id)
    flash("Job details updated.", "ok")
    return redirect(url_for("repaircam.clip", recording_id=recording_id))


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


@bp.route("/status")
def status():
    root = config.data_dir()
    # One source of truth for free space: the same report the guard refuses a
    # recording on, so the page can never look healthier than the bench does.
    worker = current_app.extensions.get("storage")
    store = worker.status() if worker else storage.status(catalogue())
    disk = store["disk"]

    checks = []
    if request.args.get("cameras") == "1":
        for work_center, camera in sorted(config.load_cameras().items()):
            ok, message = build_backend(camera).check(
                timeout=request.args.get("timeout", type=float)
            )
            checks.append({"work_center": work_center, "camera": camera, "ok": ok, "message": message})

    # Footage left behind by a restart is invisible everywhere else — it is not
    # in the library, because it never became a clip. Say so here.
    orphans = recovery.find_orphans()

    trigger = current_app.extensions.get("trigger")

    return render_template(
        "status.html",
        trigger=trigger.status() if trigger else None,
        trigger_configured=saarseva.is_configured(),
        orphans=orphans,
        orphan_mb=round(sum(o.size_mb for o in orphans), 1),
        # Clips whose link saar-seva refused for good. Nothing retries these,
        # so this page is the only place they surface.
        link_failures=catalogue().list_link_failures(),
        ffmpeg_version=ffmpeg.version(),
        ffmpeg_ok=ffmpeg.available(),
        data_dir=root,
        disk=disk,
        store=store,
        stats=catalogue().stats(),
        cameras=config.load_cameras(),
        checks=checks,
        checked=request.args.get("cameras") == "1",
        events=catalogue().recent_events(limit=25),
    )
