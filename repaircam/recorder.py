"""The Start / Stop / Done state machine.

This is the heart of RepairCam. A technician presses **Start** when they begin an
operation, **Stop** if they step away, **Start** again when they return, and
**Done** when the operation is finished. Every Start→Stop pair is a *segment*;
Done joins the segments into exactly **one clip per operation**, writes the
sidecar that labels it, and files it in the catalogue.

One clip per operation is the rule the whole design serves: it is what makes the
footage useful as a training dataset and what makes a single link in the Odoo MO
chatter meaningful.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from . import SCHEMA_VERSION, config, ffmpeg, storage
from .backends import CaptureBackend, CaptureError, Segment, build_backend
from .catalogue import Catalogue, JobLabels, Recording, utcnow, write_sidecar

log = logging.getLogger(__name__)


class RecorderError(Exception):
    """An operation was requested that the recorder's current state forbids."""


class State(str, Enum):
    """Where a bench is right now."""

    IDLE = "idle"  # nothing in progress
    RECORDING = "recording"  # ffmpeg is writing a segment
    PAUSED = "paused"  # segments recorded, waiting for Start again or Done
    FINALISING = "finalising"  # joining segments on Done


#: How long a bench may sit between Start and the first frame before the UI
#: says something is wrong. A healthy VIGI opens its stream in well under a
#: second; several seconds means the camera is not answering.
CONNECT_WARN_S = 5.0


def slugify(value: str, *, max_length: int = 40) -> str:
    """Make a string safe for a filename.

    Odoo MO names contain slashes (``WH/MO/00042``), which would silently create
    directories if pasted into a path.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-")
    return cleaned[:max_length].lower()


@dataclass
class Session:
    """One operation being recorded: its labels and its segments so far."""

    work_center: str
    labels: JobLabels
    started_at: float = field(default_factory=time.time)
    started_iso: str = field(default_factory=utcnow)
    segments: list[Segment] = field(default_factory=list)
    segment_dir: Path | None = None

    @property
    def recorded_seconds(self) -> float:
        """Seconds actually captured — pauses do not count."""
        return sum(segment.duration for segment in self.segments)

    def clip_name(self) -> str:
        """Filename for the finished clip.

        Deliberately readable: bench, timestamp, then whatever job labels exist,
        so the archive is browsable in a file manager without the database.
        """
        stamp = datetime.fromtimestamp(self.started_at, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        parts = [self.work_center, stamp]
        for value in (self.labels.mo_name, self.labels.operation, self.labels.device):
            slug = slugify(value, max_length=24)
            if slug:
                parts.append(slug)
        return "_".join(parts) + ".mp4"


class Recorder:
    """Drives one bench's camera through the Start/Stop/Done cycle.

    Thread-safe: the web UI hits it from several request threads at once.
    """

    def __init__(
        self,
        work_center: str,
        *,
        backend: CaptureBackend | None = None,
        catalogue: Catalogue | None = None,
        data_root: Path | None = None,
    ):
        self.work_center = work_center
        self.camera = None if backend else config.get_camera(work_center)
        self.backend = backend or build_backend(self.camera)
        if self.camera is None:
            self.camera = self.backend.camera
        self.catalogue = catalogue or Catalogue()
        self.data_root = data_root or config.data_dir()

        self._lock = threading.RLock()
        self._state = State.IDLE
        self._session: Session | None = None
        self._capture = None
        self._last_error = ""

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> State:
        """Current state, corrected for an ffmpeg that died on its own.

        A camera can be unplugged mid-recording. Without this check the UI would
        keep showing a happily counting timer over a dead process.
        """
        with self._lock:
            if self._state is State.RECORDING and self._capture and not self._capture.running:
                log.warning("%s: capture ended unexpectedly", self.work_center)
                self._close_segment(unexpected=True)
            return self._state

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict:
        """Everything the dashboard needs about this bench, in one call."""
        state = self.state  # property has the side effect of noticing a crash
        session = self._session
        elapsed = 0.0
        capturing = False
        waiting = 0.0
        if session:
            elapsed = session.recorded_seconds
            if state is State.RECORDING and self._capture:
                capturing = self._capture.capturing
                if capturing:
                    # Count from the first frame, so the timer never runs ahead
                    # of the footage it claims to describe.
                    elapsed += self._capture.capturing_elapsed
                else:
                    waiting = self._capture.elapsed

        connecting = state is State.RECORDING and not capturing

        return {
            "work_center": self.work_center,
            "camera": self.camera.name if self.camera else "",
            "state": state.value,
            # `recording` means "Start has been pressed"; `capturing` means
            # "frames are being written". They differ for the seconds it takes
            # to open the stream — and forever, if the camera never answers.
            # The red light must follow `capturing`.
            "recording": state is State.RECORDING,
            "capturing": capturing,
            "connecting": connecting,
            "connecting_s": round(waiting, 1),
            "camera_slow": connecting and waiting >= CONNECT_WARN_S,
            "state_label": "connecting to camera" if connecting else state.value,
            "busy": state is not State.IDLE,
            "segments": len(session.segments) if session else 0,
            "elapsed_s": round(elapsed, 1),
            "elapsed_hms": _hms(elapsed),
            "labels": session.labels.as_dict() if session else JobLabels().as_dict(),
            "started_at": session.started_iso if session else None,
            "last_error": self._last_error,
        }

    # -- the three buttons --------------------------------------------------

    def start(self, labels: JobLabels | None = None) -> Session:
        """Begin recording, or resume after a Stop.

        Labels are only accepted when a new operation begins; resuming keeps the
        labels the operation already has, so a half-filled form cannot wipe them.
        """
        with self._lock:
            state = self.state
            if state is State.RECORDING:
                raise RecorderError(f"{self.work_center} is already recording.")
            if state is State.FINALISING:
                raise RecorderError(f"{self.work_center} is still saving the last clip.")

            # Checked here rather than during: a clip that dies half-written is
            # worse than one that was never begun, because nobody notices the
            # first until they go looking for footage that does not exist.
            # Resuming an operation is allowed through — its earlier segments
            # are already on the disk and abandoning them helps nobody.
            if state is State.IDLE:
                try:
                    storage.check_before_recording()
                except storage.DiskFull as exc:
                    self._last_error = str(exc)
                    self.catalogue.log_event(
                        "error", work_center=self.work_center, detail=f"disk full: {exc}"
                    )
                    raise RecorderError(str(exc)) from exc

            if state is State.IDLE or self._session is None:
                session = Session(self.work_center, labels or JobLabels())
                session.segment_dir = (
                    self.data_root
                    / "segments"
                    / self.work_center
                    / datetime.fromtimestamp(session.started_at, tz=timezone.utc).strftime(
                        "%Y%m%d-%H%M%S"
                    )
                )
                session.segment_dir.mkdir(parents=True, exist_ok=True)
                self._session = session
                self.catalogue.log_event(
                    "start",
                    work_center=self.work_center,
                    detail=session.labels.mo_name or "(unlabelled)",
                )
            else:
                session = self._session
                if labels and not labels.is_empty and session.labels.is_empty:
                    session.labels = labels  # first time the form was filled in
                self.catalogue.log_event(
                    "resume",
                    work_center=self.work_center,
                    detail=f"segment {len(session.segments) + 1}",
                )

            assert session.segment_dir is not None
            dest = session.segment_dir / f"seg-{len(session.segments) + 1:03d}.mp4"
            try:
                self._capture = self.backend.start(dest)
            except (CaptureError, ffmpeg.FFmpegError) as exc:
                self._last_error = str(exc)
                self.catalogue.log_event(
                    "error", work_center=self.work_center, detail=f"start failed: {exc}"
                )
                raise RecorderError(f"Could not start recording on {self.work_center}: {exc}") from exc

            self._last_error = ""
            self._state = State.RECORDING
            log.info("%s: recording segment %d", self.work_center, len(session.segments) + 1)
            return session

    def stop(self) -> Segment:
        """End the current segment. The operation stays open until Done."""
        with self._lock:
            if self.state is not State.RECORDING:
                raise RecorderError(f"{self.work_center} is not recording.")
            segment = self._close_segment()
            self.catalogue.log_event(
                "stop",
                work_center=self.work_center,
                detail=f"segment {len(self._session.segments)}, {segment.duration:.0f}s",
            )
            return segment

    def done(self, labels: JobLabels | None = None) -> Recording:
        """Finish the operation: join segments, write the sidecar, catalogue it.

        Labels may be supplied here for the common case where a technician
        records first and fills in the job afterwards.
        """
        with self._lock:
            if self.state is State.RECORDING:
                self._close_segment()

            session = self._session
            if session is None:
                raise RecorderError(f"{self.work_center} has nothing to finish.")
            if labels and not labels.is_empty:
                session.labels = labels

            usable = [s for s in session.segments if s.ok and s.size_bytes > 0]
            if not usable:
                self._reset()
                self.catalogue.log_event(
                    "abandoned", work_center=self.work_center, detail="no usable footage"
                )
                raise RecorderError(
                    f"Nothing was recorded on {self.work_center} — check the camera and try again."
                )

            self._state = State.FINALISING

        # The join runs outside the lock: it can take seconds on a long
        # operation, and the dashboard must stay responsive while it does.
        try:
            recording = self._finalise(session, usable)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._state = State.PAUSED  # segments survive; Done can be retried
            self.catalogue.log_event(
                "error", work_center=self.work_center, detail=f"finalise failed: {exc}"
            )
            raise RecorderError(f"Could not save the clip for {self.work_center}: {exc}") from exc

        with self._lock:
            self._reset()
        self.catalogue.log_event(
            "done",
            work_center=self.work_center,
            detail=f"{recording.duration_hms} -> {Path(recording.path).name}",
            recording_id=recording.id,
        )
        log.info("%s: saved %s", self.work_center, recording.path)
        return recording

    def cancel(self) -> None:
        """Throw away the current operation and its footage."""
        with self._lock:
            if self.state is State.RECORDING:
                self._close_segment()
            session = self._session
            if session:
                for segment in session.segments:
                    segment.path.unlink(missing_ok=True)
                if session.segment_dir:
                    _remove_dir_if_empty(session.segment_dir)
            self._reset()
        self.catalogue.log_event("cancelled", work_center=self.work_center)

    # -- one-shot -----------------------------------------------------------

    def record_once(self, duration: float, labels: JobLabels | None = None) -> Recording:
        """Record for a fixed number of seconds and file the clip.

        This is the bench-proof path: ``cli.py record WC2 --duration 20``.
        """
        self.start(labels)
        try:
            deadline = time.time() + duration
            while time.time() < deadline:
                if self.state is not State.RECORDING:
                    break  # camera dropped; done() will report what survived
                time.sleep(min(0.25, max(0.0, deadline - time.time())))
        except KeyboardInterrupt:
            log.info("interrupted — saving what was recorded")
        return self.done()

    # -- internals ----------------------------------------------------------

    def _close_segment(self, *, unexpected: bool = False) -> Segment:
        """Stop ffmpeg and record the resulting segment. Caller holds the lock."""
        capture, self._capture = self._capture, None
        session = self._session
        if capture is None or session is None:
            self._state = State.PAUSED if session else State.IDLE
            raise RecorderError("no capture in progress")

        segment = capture.stop() if capture.running else capture.wait()
        session.segments.append(segment)
        self._state = State.PAUSED

        if not segment.ok:
            self._last_error = segment.error or "the camera stopped unexpectedly"
            self.catalogue.log_event(
                "error", work_center=self.work_center, detail=self._last_error
            )
        elif unexpected:
            self._last_error = "recording ended on its own — check the camera and cable"
        return segment

    def _finalise(self, session: Session, usable: list[Segment]) -> Recording:
        """Join segments, write the sidecar, insert the catalogue row."""
        return finalise_session(
            session,
            usable,
            data_root=self.data_root,
            catalogue=self.catalogue,
            concat=self.backend.concat,
            camera_info=self.backend.describe(),
            camera_name=self.camera.name if self.camera else "",
        )

    def _reset(self) -> None:
        self._session = None
        self._capture = None
        self._state = State.IDLE


# --------------------------------------------------------------------------
# Turning a finished session into a filed clip
# --------------------------------------------------------------------------


def finalise_session(
    session: Session,
    usable: list[Segment],
    *,
    data_root: Path,
    catalogue: Catalogue,
    concat: Callable[[list[Segment], Path], Path],
    camera_info: dict,
    camera_name: str = "",
    recovered: bool = False,
) -> Recording:
    """Join a session's segments into one clip, describe it, and file it.

    Shared by the normal Done path and by recovery of orphaned segments, so a
    recovered clip is indistinguishable from a normal one apart from the
    ``recovered`` flag in its sidecar.
    """
    day = datetime.fromtimestamp(session.started_at, tz=timezone.utc).strftime("%Y-%m-%d")
    dest_dir = data_root / "recordings" / day
    dest = dest_dir / session.clip_name()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists():  # two operations started in the same second
        dest = dest.with_name(f"{dest.stem}_{int(time.time() * 1000) % 1000:03d}.mp4")

    concat(usable, dest)
    if session.segment_dir:
        _remove_dir_if_empty(session.segment_dir)

    media = _media_summary(dest)
    # Prefer the container's own duration; fall back to wall-clock if ffprobe
    # is unavailable.
    duration = media.get("duration_s") or round(session.recorded_seconds, 2)
    relative = dest.relative_to(data_root).as_posix()

    recording = Recording(
        work_center=session.work_center,
        camera_name=camera_name,
        path=relative,
        started_at=session.started_iso,
        ended_at=utcnow(),
        duration_s=duration,
        segments=len(usable),
        size_bytes=dest.stat().st_size if dest.exists() else 0,
        width=media.get("width"),
        height=media.get("height"),
        frame_rate=media.get("frame_rate"),
        video_codec=media.get("video_codec") or "",
        audio_codec=media.get("audio_codec") or "",
        labels=session.labels,
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "clip": relative,
        "work_center": session.work_center,
        "camera": camera_info,
        "job": session.labels.as_dict(),
        "recorded": {
            "started_at": session.started_iso,
            "ended_at": recording.ended_at,
            "duration_s": duration,
            "segments": len(usable),
            "segment_durations_s": [round(s.duration, 2) for s in usable],
        },
        "media": media,
        "recorder": {"version": _package_version(), "host": _hostname()},
    }
    if recovered:
        # Say so honestly: the start time is inferred from the segment folder,
        # and nobody pressed Done on this clip.
        payload["recovered"] = True
        payload["recorded"]["started_at_is_estimated"] = True

    sidecar = write_sidecar(dest, payload)
    recording.sidecar_path = sidecar.relative_to(data_root).as_posix()
    return catalogue.add(recording)


# --------------------------------------------------------------------------
# One recorder per bench, shared across web requests
# --------------------------------------------------------------------------


class RecorderPool:
    """Keeps one :class:`Recorder` per bench alive for the process lifetime.

    The web UI is stateless per request, but a recording is not — the same
    Recorder object has to answer the next request, or Stop would not find the
    ffmpeg that Start began.
    """

    def __init__(self, catalogue: Catalogue | None = None):
        self._recorders: dict[str, Recorder] = {}
        self._lock = threading.Lock()
        self._catalogue = catalogue

    @property
    def catalogue(self) -> Catalogue:
        if self._catalogue is None:
            self._catalogue = Catalogue()
        return self._catalogue

    def get(self, work_center: str) -> Recorder:
        with self._lock:
            recorder = self._recorders.get(work_center)
            if recorder is None:
                recorder = Recorder(work_center, catalogue=self.catalogue)
                self._recorders[work_center] = recorder
            return recorder

    def active(self) -> list[Recorder]:
        """Recorders that currently have an operation open."""
        with self._lock:
            recorders = list(self._recorders.values())
        return [r for r in recorders if r.state is not State.IDLE]

    def statuses(self) -> dict[str, dict]:
        """Status for every configured bench, whether or not it has been used."""
        out: dict[str, dict] = {}
        for work_center in sorted(config.load_cameras()):
            try:
                out[work_center] = self.get(work_center).status()
            except Exception as exc:  # a bad camera entry must not blank the page
                out[work_center] = {
                    "work_center": work_center,
                    "state": "error",
                    "state_label": "error",
                    "recording": False,
                    "capturing": False,
                    "connecting": False,
                    "connecting_s": 0.0,
                    "camera_slow": False,
                    "busy": False,
                    "elapsed_hms": "0:00:00",
                    "last_error": str(exc),
                    "labels": JobLabels().as_dict(),
                    "segments": 0,
                }
        return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _hms(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _media_summary(path: Path) -> dict:
    """ffprobe details, but never fatal — a clip without metadata still counts."""
    try:
        return ffmpeg.media_summary(str(path))
    except ffmpeg.FFmpegError as exc:
        log.debug("could not probe %s: %s", path.name, exc)
        return {}


def _remove_dir_if_empty(path: Path) -> None:
    try:
        next(path.iterdir())
    except StopIteration:
        path.rmdir()
    except OSError:
        pass


def _package_version() -> str:
    from . import __version__

    return __version__


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return ""
