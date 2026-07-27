"""Thin wrapper around the ffmpeg and ffprobe binaries.

Everything that shells out lives here, so the rest of the code never builds a
command line by hand and never accidentally prints a camera password.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class FFmpegError(Exception):
    """ffmpeg is missing, or a command failed."""


class FFmpegMissing(FFmpegError):
    """The ffmpeg binary is not installed on this machine."""


def redact(command: list[str]) -> list[str]:
    """Mask passwords in an ffmpeg command so it is safe to log or display.

    RTSP credentials ride inside the URL (``rtsp://admin:secret@host/...``), so
    any log line containing a raw command would leak the camera password.
    """
    cleaned: list[str] = []
    for token in command:
        if "://" in token and "@" in token:
            scheme, _, rest = token.partition("://")
            credentials, _, tail = rest.rpartition("@")
            if credentials:
                user, sep, _ = credentials.partition(":")
                credentials = f"{user}:******" if sep else user
                token = f"{scheme}://{credentials}@{tail}"
        cleaned.append(token)
    return cleaned


def require_ffmpeg() -> None:
    """Raise a plain-English error if ffmpeg is not installed."""
    if shutil.which(FFMPEG) is None:
        raise FFmpegMissing(
            "ffmpeg is not installed. On the recorder box run:\n"
            "    sudo apt update && sudo apt install -y ffmpeg"
        )


def available() -> bool:
    """True if ffmpeg can be run."""
    return shutil.which(FFMPEG) is not None


def version() -> str:
    """First line of ``ffmpeg -version``, or '' if unavailable."""
    if not available():
        return ""
    try:
        out = subprocess.run(
            [FFMPEG, "-version"], capture_output=True, text=True, timeout=10, check=False
        )
        return out.stdout.splitlines()[0] if out.stdout else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command to completion, raising on failure."""
    require_ffmpeg()
    log.debug("running: %s", " ".join(redact(command)))
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s") from exc
    except OSError as exc:
        raise FFmpegError(f"could not run ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise FFmpegError("ffmpeg failed:\n" + "\n".join(tail))
    return proc


# --------------------------------------------------------------------------
# Command builders
# --------------------------------------------------------------------------


def record_command(
    url: str,
    dest: Path,
    *,
    duration: float | None = None,
    audio: bool = True,
    transport: str = "tcp",
) -> list[str]:
    """Build the recording command.

    The video is copied, never re-encoded — that is what lets one modest box
    record ten benches at once. Audio is a different story: VIGI cameras send
    ``pcm_alaw``, which an MP4 container cannot hold, so audio (and only audio)
    is transcoded to AAC.
    """
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        transport,
        "-i",
        url,
    ]
    if duration is not None:
        command += ["-t", f"{duration:g}"]
    command += ["-c:v", "copy"]
    command += ["-c:a", "aac", "-b:a", "64k"] if audio else ["-an"]
    command += [
        # Keeps the file playable if the process is ever killed mid-write, and
        # puts the index at the front so the browser can seek without a full
        # download.
        "-movflags",
        "+faststart",
        "-y",
        str(dest),
    ]
    return command


def snapshot_command(url: str, dest: Path, *, transport: str = "tcp") -> list[str]:
    """Grab a single still — used for the focus test and bench thumbnails."""
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        "-i",
        url,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        str(dest),
    ]


def mjpeg_command(url: str, *, fps: int = 6, width: int = 640, transport: str = "tcp") -> list[str]:
    """Stream the camera as MJPEG on stdout, for the browser live preview.

    A browser cannot play RTSP, so the sub-stream is re-packaged as MJPEG. This
    reads the *sub* stream at low frame rate on purpose: preview must never
    steal bandwidth from an in-progress recording.
    """
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        "-i",
        url,
        "-f",
        "mjpeg",
        "-q:v",
        "7",
        "-r",
        str(fps),
        "-vf",
        f"scale={width}:-2",
        "-",
    ]


def concat_command(list_file: Path, dest: Path, *, audio_codec: str = "copy") -> list[str]:
    """Join the Start/Stop segments of one operation into a single clip.

    Uses the concat *demuxer* with stream copy: all the segments came from the
    same camera with identical settings, so they join without re-encoding.
    """
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-movflags",
        "+faststart",
        "-y",
        str(dest),
    ]


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def snapshot(url: str, dest: Path, *, timeout: float = 20) -> Path:
    """Write a single JPEG still from ``url`` to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(snapshot_command(url, dest), timeout=timeout)
    return dest


def probe(path_or_url: str, *, timeout: float = 20) -> dict:
    """Return ffprobe's JSON description of a file or stream."""
    require_ffmpeg()
    if shutil.which(FFPROBE) is None:
        raise FFmpegMissing("ffprobe is not installed (it ships with ffmpeg).")
    command = [
        FFPROBE,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path_or_url,
    ]
    proc = run(command, timeout=timeout)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"could not read ffprobe output: {exc}") from exc


def media_summary(path_or_url: str, *, timeout: float = 20) -> dict:
    """Boil ffprobe down to the handful of fields worth storing in a sidecar."""
    info = probe(path_or_url, timeout=timeout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})

    frame_rate = None
    raw_rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
    if "/" in raw_rate:
        num, _, den = raw_rate.partition("/")
        try:
            frame_rate = round(int(num) / int(den), 2) if int(den) else None
        except (ValueError, ZeroDivisionError):
            frame_rate = None

    duration = fmt.get("duration")
    return {
        "duration_s": round(float(duration), 2) if duration else None,
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": frame_rate,
        "audio_codec": audio.get("codec_name"),
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate", "").isdigit() else None,
    }


def duration_of(path: Path, *, timeout: float = 20) -> float | None:
    """Duration of a finished file in seconds, or None if it cannot be read."""
    try:
        return media_summary(str(path), timeout=timeout).get("duration_s")
    except FFmpegError:
        return None


def reachable(url: str, *, timeout: float = 10) -> tuple[bool, str]:
    """Can we actually open this stream? Returns (ok, message).

    Used by the status page so the owner can tell a camera problem from a
    RepairCam problem without reading a stack trace.
    """
    try:
        summary = media_summary(url, timeout=timeout)
    except FFmpegMissing as exc:
        return False, str(exc)
    except FFmpegError as exc:
        return False, str(exc).splitlines()[-1] if str(exc) else "stream did not open"
    size = ""
    if summary.get("width") and summary.get("height"):
        size = f" {summary['width']}x{summary['height']}"
    codec = summary.get("video_codec") or "?"
    return True, f"OK — {codec}{size}"


# --------------------------------------------------------------------------
# Long-running recording process
# --------------------------------------------------------------------------


class RecordingProcess:
    """A running ffmpeg recording that can be stopped cleanly.

    Stopping matters more than it looks: an MP4 only becomes playable when
    ffmpeg writes its index at the end. Killing the process leaves a corrupt
    file, so ``stop()`` sends ``q`` on stdin — ffmpeg's "finish up now" signal —
    and only escalates to a kill if that is ignored.
    """

    def __init__(self, command: list[str], dest: Path):
        self.dest = dest
        self.command = command
        self.started_at = time.time()
        dest.parent.mkdir(parents=True, exist_ok=True)
        require_ffmpeg()
        log.info("recording to %s", dest.name)
        log.debug("command: %s", " ".join(redact(command)))
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise FFmpegError(f"could not start ffmpeg: {exc}") from exc
        self._stderr = ""

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def stderr(self) -> str:
        return self._stderr

    def wait(self, timeout: float | None = None) -> int:
        """Wait for ffmpeg to exit on its own (used by ``--duration``)."""
        try:
            _, err = self._proc.communicate(timeout=timeout)
            self._stderr = err or ""
        except subprocess.TimeoutExpired:
            raise
        return self._proc.returncode

    def stop(self, timeout: float = 10) -> int:
        """Ask ffmpeg to finalise the file, then wait for it."""
        if not self.running:
            return self._proc.returncode

        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.write("q")
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # already gone; the terminate path below handles it

        try:
            _, err = self._proc.communicate(timeout=timeout)
            self._stderr = err or ""
            return self._proc.returncode
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg ignored 'q' for %s, terminating", self.dest.name)

        self._proc.terminate()
        try:
            _, err = self._proc.communicate(timeout=5)
            self._stderr = err or ""
        except subprocess.TimeoutExpired:
            log.error("ffmpeg would not terminate for %s, killing", self.dest.name)
            self._proc.kill()
            _, err = self._proc.communicate()
            self._stderr = err or ""
        return self._proc.returncode
