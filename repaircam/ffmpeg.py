"""Thin wrapper around the ffmpeg and ffprobe binaries.

Everything that shells out lives here, so the rest of the code never builds a
command line by hand and never accidentally prints a camera password.
"""

from __future__ import annotations

import json
import logging
import re
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


#: Any ``scheme://credentials@host`` inside free text. Deliberately greedy up
#: to the LAST ``@`` before the path, matching what redact() does with
#: rpartition: a password may itself contain an ``@``, and a pattern that stops
#: at the first one leaves the rest of it on screen.
_CREDENTIALS_IN_TEXT = re.compile(r"(\w+://)([^\s/]+)@")


def redact_text(text: str) -> str:
    """Mask passwords in anything ffmpeg said.

    ``redact()`` handles a command we built. This handles output we did not:
    ffmpeg quotes the stream URL in almost every error it produces, so
    "No route to host" arrives with the camera's password attached. That text
    then travels to the terminal, the status page, the catalogue and the log.
    """
    def mask(match: "re.Match[str]") -> str:
        credentials = match.group(2)
        user, sep, _ = credentials.partition(":")
        return f"{match.group(1)}{user}{':******' if sep else ''}@"

    return _CREDENTIALS_IN_TEXT.sub(mask, text or "")


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
        raise FFmpegError(redact_text("ffmpeg failed:\n" + "\n".join(tail)))
    return proc


# --------------------------------------------------------------------------
# Command builders
# --------------------------------------------------------------------------


#: How long to let ffmpeg finalise after 'q' before escalating. Generous: a
#: clean finish is worth waiting for, and the alternative used to be a lost
#: clip.
STOP_QUIT_S = 20.0

#: And after SIGTERM, before the kill nobody wants.
STOP_TERM_S = 10.0

#: Lines ffmpeg prints that say nothing about why a capture failed.
#:
#: "Non-monotonous DTS" is the specific one that cost an afternoon: it is a
#: routine timestamp complaint from an RTSP stream, it is very often the LAST
#: thing printed, and reporting the last line meant a bench that had been
#: killed mid-write displayed a harmless warning as its cause. The real reason
#: is almost always earlier in the output.
_NOISE = (
    "non-monotonous dts",
    "past duration",
    "last message repeated",
    "deprecated",
    "guessed channel layout",
    "timestamps are unset",
    "first timestamp is not",
    "co located pocs",
    "vbv buffer size",
)


def explain_failure(stderr: str, *, returncode: int | None = None, ended_by: str = "") -> str:
    """The most useful one-line reason a capture failed.

    Scans BACKWARDS for the last line that is actually about a problem,
    skipping ffmpeg's routine grumbling. Falls back to the last real line, then
    to the exit code — never to nothing, because "it failed" with no reason is
    what sends somebody to check a camera that was fine all along.
    """
    if ended_by == "killed":
        # This one outranks anything in the output. The file was cut off
        # mid-write; whatever ffmpeg last complained about is beside the point.
        return (
            "ffmpeg had to be killed — it stopped responding, so the recording "
            "was cut off mid-write"
        )

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if not any(noise in line.lower() for noise in _NOISE):
            return line
    if lines:
        return lines[-1]
    if returncode is not None:
        return f"ffmpeg exited with code {returncode}"
    return "the capture failed with no output from ffmpeg"


def repair_command(source: Path, dest: Path) -> list[str]:
    """Remux a damaged recording into a clean file.

    Best effort. A fragmented MP4 that was truncated usually reads back fine,
    which is the whole reason segments are written fragmented. A file with no
    header at all cannot be rescued this way and the caller is told so rather
    than left with a zero-byte "repair".
    """
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-i", str(source),
        "-c", "copy", "-movflags", "+faststart",
        "-y", str(dest),
    ]


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
        # A FRAGMENTED MP4, and this is the difference between losing a
        # recording and keeping it.
        #
        # This used to say +faststart with a comment claiming it kept the file
        # playable if the process was killed. It does the opposite: faststart
        # is a pass that runs when ffmpeg EXITS, moving the index to the front.
        # Kill ffmpeg — or cut the power — and the index was never written at
        # all, so the file has no moov atom and nothing can open it. On
        # 2026-08-05 a stalled RTSP input ignored 'q' and SIGTERM, was killed,
        # and the whole packing session was lost exactly this way.
        #
        # +frag_keyframe+empty_moov writes a usable header up front and closes
        # a fragment at every keyframe, so a truncated file plays up to the
        # last complete fragment. The joined clip is still written with
        # faststart (see concat_command) — that runs on a short-lived process
        # writing to local disk, which is not the one at risk.
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
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


def concat_files(paths: list[Path], dest: Path, *, audio_codec: str = "copy") -> Path:
    """Join MP4 files into one, without re-encoding. Inputs are left alone.

    Lives here rather than on the backend because recovering orphaned segments
    must work even when the camera that recorded them is long gone from
    cameras.yaml.
    """
    if not paths:
        raise FFmpegError("nothing to join")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        shutil.copy2(paths[0], dest)
        return dest

    list_file = dest.with_suffix(".concat.txt")
    # The concat demuxer's own quoting rule: wrap in single quotes and escape
    # any single quote in the path.
    lines = ["file '" + str(p.resolve()).replace("'", r"'\''") + "'" for p in paths]
    list_file.write_text("\n".join(lines) + "\n")
    try:
        run(concat_command(list_file, dest, audio_codec=audio_codec), timeout=max(120, 10 * len(paths)))
    finally:
        list_file.unlink(missing_ok=True)
    return dest


def snapshot(url: str, dest: Path, *, timeout: float = 20) -> Path:
    """Write a single JPEG still from ``url`` to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(snapshot_command(url, dest), timeout=timeout)
    return dest


def probe(path_or_url: str, *, timeout: float = 20, quick: bool = False) -> dict:
    """Return ffprobe's JSON description of a file or stream.

    ``quick`` caps how much of the stream ffprobe will read before answering.
    That is right for a reachability check — we only want to know the camera
    replies — and wrong for a finished clip, where an under-read gives a
    truncated duration. So it is off by default.
    """
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
    ]
    if quick:
        # Enough to see the stream header on a 4MP camera, not enough to sit
        # there buffering. Both options are ancient and safe on any ffmpeg.
        command += ["-analyzeduration", "2000000", "-probesize", "1000000"]
    command += [
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


def media_summary(path_or_url: str, *, timeout: float = 20, quick: bool = False) -> dict:
    """Boil ffprobe down to the handful of fields worth storing in a sidecar."""
    info = probe(path_or_url, timeout=timeout, quick=quick)
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


#: How long to wait for a camera to answer a reachability check. Generous on
#: purpose: opening a 4MP RTSP stream on a modest recorder box genuinely takes
#: several seconds, and a status page that calls a healthy camera broken is
#: worse than one that takes a moment longer to say so.
DEFAULT_CHECK_TIMEOUT = 30.0


def reachable(url: str, *, timeout: float = DEFAULT_CHECK_TIMEOUT) -> tuple[bool, str]:
    """Can we actually open this stream? Returns (ok, message).

    Used by the status page so the owner can tell a camera problem from a
    RepairCam problem without reading a stack trace.
    """
    try:
        summary = media_summary(url, timeout=timeout, quick=True)
    except FFmpegMissing as exc:
        return False, str(exc)
    except FFmpegError as exc:
        message = str(exc).strip()
        lowered = message.lower()
        # Translate the three failures that actually happen at a bench into
        # what to go and check, rather than echoing ffmpeg at someone who
        # cannot act on it.
        if "timed out" in lowered:
            return False, (
                f"no answer within {timeout:g}s — check the camera is powered and "
                f"that its IP in cameras.yaml is still correct (try: ping the camera)"
            )
        if "401" in message or "unauthorized" in lowered:
            return False, "the camera rejected the password in cameras.yaml"
        if "connection refused" in lowered:
            return False, "the camera refused the connection — is RTSP enabled, and the port right?"
        return False, redact_text(message.splitlines()[-1]) if message else "stream did not open"
    size = ""
    if summary.get("width") and summary.get("height"):
        size = f" {summary['width']}x{summary['height']}"
    codec = summary.get("video_codec") or "?"
    # Say WHICH stream answered. This check runs against the sub-stream (it is
    # cheap and never disturbs a recording), so the size shown is the preview's,
    # not the recording's — and a bare "736x416" next to a 4MP camera reads like
    # something is badly wrong when nothing is.
    which = " (sub-stream; recordings use the main one)" if "stream2" in url else ""
    return True, f"OK — {codec}{size}{which}"


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
        #: How this process ended: "" while running, then "quit" (it accepted
        #: 'q' and finalised), "terminated" (SIGTERM) or "killed" (SIGKILL).
        #: Kept because the exit code cannot tell them apart, and the
        #: difference is the difference between a clean clip and a truncated
        #: one — which nobody could see when a kill was reported as whatever
        #: ffmpeg happened to print last.
        self.ended_by = ""

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def stderr(self) -> str:
        """Already redacted. It reaches Segment.error, the catalogue and the UI."""
        return self._stderr

    def wait(self, timeout: float | None = None) -> int:
        """Wait for ffmpeg to exit on its own (used by ``--duration``)."""
        try:
            _, err = self._proc.communicate(timeout=timeout)
            self._stderr = redact_text(err or "")
        except subprocess.TimeoutExpired:
            raise
        return self._proc.returncode

    def stop(self, timeout: float = STOP_QUIT_S) -> int:
        """Ask ffmpeg to finalise the file, then wait for it.

        Three stages with real headroom between them. A stalled RTSP input can
        leave ffmpeg unresponsive to 'q' for a surprisingly long time, and the
        old ten-then-five seconds was not enough: on 2026-08-05 it reached the
        kill, and a killed ffmpeg used to mean a lost recording.

        Every stage is recorded in ``ended_by``, because the exit code cannot
        distinguish "finalised cleanly" from "we killed it" and the caller has
        to be able to say which.
        """
        if not self.running:
            self.ended_by = self.ended_by or "quit"
            return self._proc.returncode

        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.write("q")
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # already gone; the terminate path below handles it

        try:
            _, err = self._proc.communicate(timeout=timeout)
            self._stderr = redact_text(err or "")
            self.ended_by = "quit"
            return self._proc.returncode
        except subprocess.TimeoutExpired:
            log.warning(
                "ffmpeg ignored 'q' for %.0fs on %s, terminating",
                timeout, self.dest.name,
            )

        self._proc.terminate()
        try:
            _, err = self._proc.communicate(timeout=STOP_TERM_S)
            self._stderr = redact_text(err or "")
            self.ended_by = "terminated"
        except subprocess.TimeoutExpired:
            log.error("ffmpeg would not terminate for %s, killing", self.dest.name)
            self._proc.kill()
            _, err = self._proc.communicate()
            self._stderr = redact_text(err or "")
            self.ended_by = "killed"
        return self._proc.returncode
