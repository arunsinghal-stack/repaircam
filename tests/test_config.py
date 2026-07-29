"""Camera configuration, and the password handling around it."""

from __future__ import annotations

from pathlib import Path

import pytest

from repaircam import config, ffmpeg
from repaircam.config import CameraConfig, ConfigError, load_cameras

SAMPLE = """
cameras:
  WC2:
    name: "Bench 2"
    host: "192.168.0.133"
    username: "admin"
    password: "p@ss word/1"
    model: "TP-Link VIGI C540V"
  WC3:
    name: "Bench 3"
    host: "192.168.0.134"
    password: "other"
    has_audio: false
"""


def write_config(tmp_path: Path, text: str = SAMPLE) -> Path:
    path = tmp_path / "cameras.yaml"
    path.write_text(text)
    return path


def test_loads_every_bench(tmp_path: Path):
    cameras = load_cameras(write_config(tmp_path))
    assert set(cameras) == {"WC2", "WC3"}
    assert cameras["WC2"].model == "TP-Link VIGI C540V"
    assert cameras["WC3"].has_audio is False


def test_builds_the_rtsp_urls(tmp_path: Path):
    camera = load_cameras(write_config(tmp_path))["WC2"]
    assert camera.main_url.endswith("@192.168.0.133:554/stream1")
    assert camera.sub_url.endswith("@192.168.0.133:554/stream2")


def test_password_special_characters_are_escaped(tmp_path: Path):
    """A password with '/' or '@' would otherwise break the URL apart."""
    camera = load_cameras(write_config(tmp_path))["WC2"]
    assert "p%40ss%20word%2F1" in camera.main_url
    assert camera.main_url.count("@") == 1


def test_safe_urls_hide_the_password(tmp_path: Path):
    camera = load_cameras(write_config(tmp_path))["WC2"]
    assert "p@ss" not in camera.safe_main_url
    assert "******" in camera.safe_main_url
    assert "192.168.0.133" in camera.safe_main_url


def test_describe_never_leaks_the_password(tmp_path: Path):
    camera = load_cameras(write_config(tmp_path))["WC2"]
    assert "p@ss word/1" not in str(camera.describe())


def test_missing_file_explains_the_fix(tmp_path: Path):
    with pytest.raises(ConfigError, match="cameras.example.yaml"):
        load_cameras(tmp_path / "nope.yaml")


def test_camera_without_host_is_rejected(tmp_path: Path):
    path = write_config(tmp_path, "cameras:\n  WC9:\n    name: 'no ip'\n")
    with pytest.raises(ConfigError, match="missing 'host'"):
        load_cameras(path)


def test_unknown_bench_lists_the_known_ones(tmp_path: Path):
    with pytest.raises(ConfigError, match="WC2, WC3"):
        config.get_camera("WC7", write_config(tmp_path))


def test_data_dir_follows_the_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REPAIRCAM_DATA_DIR", str(tmp_path / "elsewhere"))
    assert config.data_dir() == (tmp_path / "elsewhere").resolve()


def test_ensure_data_dirs_creates_the_tree(data_root: Path):
    tree = config.ensure_data_dirs()
    assert tree["recordings"].is_dir()
    assert tree["segments"].is_dir()
    assert tree["snapshots"].is_dir()


# --------------------------------------------------------------------------
# ffmpeg command building — no ffmpeg binary needed to check these
# --------------------------------------------------------------------------


def test_record_command_copies_video_and_transcodes_audio():
    """Copying video is what lets one box handle ten benches; VIGI audio is
    pcm_alaw, which MP4 cannot hold, so only audio is re-encoded."""
    command = ffmpeg.record_command("rtsp://x/stream1", Path("/tmp/a.mp4"), audio=True)
    assert "-c:v" in command and command[command.index("-c:v") + 1] == "copy"
    assert "-c:a" in command and command[command.index("-c:a") + 1] == "aac"
    assert "-rtsp_transport" in command


def test_record_command_can_drop_audio():
    command = ffmpeg.record_command("rtsp://x/stream1", Path("/tmp/a.mp4"), audio=False)
    assert "-an" in command
    assert "-c:a" not in command


def test_record_command_honours_duration():
    command = ffmpeg.record_command("rtsp://x/s", Path("/tmp/a.mp4"), duration=20)
    assert command[command.index("-t") + 1] == "20"


def test_redact_masks_credentials_in_a_command():
    command = ["ffmpeg", "-i", "rtsp://admin:hunter2@10.0.0.5:554/stream1", "out.mp4"]
    assert ffmpeg.redact(command) == [
        "ffmpeg",
        "-i",
        "rtsp://admin:******@10.0.0.5:554/stream1",
        "out.mp4",
    ]


def test_redact_leaves_ordinary_arguments_alone():
    assert ffmpeg.redact(["ffmpeg", "-t", "20", "/data/clip.mp4"]) == [
        "ffmpeg",
        "-t",
        "20",
        "/data/clip.mp4",
    ]


def test_camera_config_defaults():
    camera = CameraConfig(work_center="WC1", name="Bench 1", host="10.0.0.9")
    assert camera.port == 554
    assert camera.backend == "rtsp"
    assert camera.main_url.startswith("rtsp://admin:@10.0.0.9:554/stream1")


# --------------------------------------------------------------------------
# camera reachability checks — the status page's honesty
# --------------------------------------------------------------------------


def test_quick_probe_caps_how_much_is_read():
    """A reachability check only needs the stream header. Reading further makes
    a healthy 4MP camera look like a broken one on a modest recorder."""
    import subprocess
    from unittest import mock

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    with mock.patch.object(ffmpeg, "run", fake_run), \
         mock.patch.object(ffmpeg, "require_ffmpeg", lambda: None), \
         mock.patch("shutil.which", lambda _: "/usr/bin/ffprobe"):
        ffmpeg.probe("rtsp://x/stream2", quick=True)

    assert "-analyzeduration" in captured["command"]
    assert "-probesize" in captured["command"]


def test_full_probe_does_not_cap_reading():
    """A finished clip must be probed fully, or its duration comes out short."""
    import subprocess
    from unittest import mock

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    with mock.patch.object(ffmpeg, "run", fake_run), \
         mock.patch.object(ffmpeg, "require_ffmpeg", lambda: None), \
         mock.patch("shutil.which", lambda _: "/usr/bin/ffprobe"):
        ffmpeg.probe("/data/clip.mp4")

    assert "-analyzeduration" not in captured["command"]


def test_the_check_timeout_is_generous():
    """Ten seconds was too tight for a real 4MP camera on an i3 laptop: the
    status page called a working camera broken."""
    assert ffmpeg.DEFAULT_CHECK_TIMEOUT >= 25


def test_a_timeout_says_what_to_go_and_check(monkeypatch):
    def explode(*a, **k):
        raise ffmpeg.FFmpegError("ffmpeg timed out after 30s")

    monkeypatch.setattr(ffmpeg, "media_summary", explode)
    ok, message = ffmpeg.reachable("rtsp://x/stream2")

    assert ok is False
    assert "powered" in message and "cameras.yaml" in message


def test_a_rejected_password_is_named_as_such(monkeypatch):
    def explode(*a, **k):
        raise ffmpeg.FFmpegError("ffmpeg failed:\n401 Unauthorized")

    monkeypatch.setattr(ffmpeg, "media_summary", explode)
    ok, message = ffmpeg.reachable("rtsp://x/stream2")

    assert ok is False
    assert "password" in message


def test_a_refused_connection_is_named_as_such(monkeypatch):
    def explode(*a, **k):
        raise ffmpeg.FFmpegError("Connection refused")

    monkeypatch.setattr(ffmpeg, "media_summary", explode)
    ok, message = ffmpeg.reachable("rtsp://x/stream2")

    assert ok is False
    assert "RTSP" in message


# --------------------------------------------------------------------------
# passwords must not escape, in either direction
# --------------------------------------------------------------------------


def test_ffmpeg_output_is_redacted_before_anyone_sees_it():
    """ffmpeg quotes the whole stream URL in its errors, so "No route to host"
    arrives with the camera password attached — and that text goes on to the
    terminal, the status page, the catalogue and the log."""
    from repaircam.ffmpeg import redact_text

    leak = "rtsp://admin:Admin%40321@192.168.1.183:554/stream2: No route to host"
    safe = redact_text(leak)

    assert "Admin%40321" not in safe
    assert "admin:******@192.168.1.183" in safe
    assert "No route to host" in safe  # the useful part survives


def test_redaction_leaves_ordinary_text_alone():
    from repaircam.ffmpeg import redact_text

    assert redact_text("Connection timed out") == "Connection timed out"
    assert redact_text("") == ""
    assert "192.168.1.184" in redact_text("rtsp://192.168.1.184:554/stream1 failed")


def test_a_url_with_no_password_keeps_its_user():
    from repaircam.ffmpeg import redact_text

    assert redact_text("rtsp://admin@10.0.0.1/x") == "rtsp://admin@10.0.0.1/x"


def test_a_password_containing_an_at_sign_is_fully_masked():
    """RTSP URLs we build percent-encode it, but ffmpeg may echo anything, and a
    pattern that stops at the first @ leaves the rest of the password on screen."""
    from repaircam.ffmpeg import redact_text

    safe = redact_text("Could not open rtsp://admin:p@ss:word@10.0.0.5:554/stream1")
    assert "p@ss" not in safe and "word" not in safe
    assert "rtsp://admin:******@10.0.0.5:554/stream1" in safe


def test_several_urls_in_one_message_are_all_masked():
    from repaircam.ffmpeg import redact_text

    safe = redact_text("rtsp://admin:x@a/1 and rtsp://root:y@b/2 both failed")
    assert "x@" not in safe and ":y@" not in safe
    assert safe.count("******") == 2
