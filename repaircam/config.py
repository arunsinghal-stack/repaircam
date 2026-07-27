"""Configuration: where data lives, and which camera is on which bench.

Two things are configured, and they are kept apart on purpose:

* the **data directory** (recordings + database) — an environment variable, so
  moving to a bigger disk is a one-line change;
* **cameras.yaml** — bench-to-camera mapping *including passwords*. That file is
  gitignored and must never be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

DEFAULT_DATA_DIR = "~/repaircam-data"
DEFAULT_RTSP_PORT = 554


class ConfigError(Exception):
    """cameras.yaml is missing, malformed, or a bench is not described in it."""


# --------------------------------------------------------------------------
# Data directory
# --------------------------------------------------------------------------


def data_dir() -> Path:
    """Root directory for recordings and the catalogue database.

    Override with the REPAIRCAM_DATA_DIR environment variable — that is how the
    recorder gets pointed at the archive HDD or the NAS mount later.
    """
    raw = os.environ.get("REPAIRCAM_DATA_DIR") or DEFAULT_DATA_DIR
    return Path(raw).expanduser().resolve()


def ensure_data_dirs() -> dict[str, Path]:
    """Create the data directory tree if it does not exist yet, and return it."""
    root = data_dir()
    tree = {
        "root": root,
        "recordings": root / "recordings",  # finished clips, one per operation
        "segments": root / "segments",  # in-progress Start/Stop pieces
        "snapshots": root / "snapshots",  # focus-test / preview stills
    }
    for path in tree.values():
        path.mkdir(parents=True, exist_ok=True)
    return tree


def database_path() -> Path:
    """Location of the SQLite catalogue."""
    return data_dir() / "repaircam.db"


# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraConfig:
    """One bench: one Work Center, one camera.

    ``work_center`` is the Odoo Work Center code (e.g. ``WC2``) and is the key
    that ties a clip back to the job it recorded.
    """

    work_center: str
    name: str
    host: str
    username: str = "admin"
    password: str = ""
    port: int = DEFAULT_RTSP_PORT
    backend: str = "rtsp"
    main_path: str = "/stream1"  # full resolution, what we record
    sub_path: str = "/stream2"  # low resolution, what we preview
    model: str = ""
    has_audio: bool = True
    notes: str = ""
    extra: dict = field(default_factory=dict)

    # -- URLs ---------------------------------------------------------------

    def _url(self, path: str, *, redact: bool) -> str:
        if not path.startswith("/"):
            path = "/" + path
        secret = "******" if redact else quote(self.password, safe="")
        user = quote(self.username, safe="")
        credentials = f"{user}:{secret}@" if self.username or self.password else ""
        return f"rtsp://{credentials}{self.host}:{self.port}{path}"

    @property
    def main_url(self) -> str:
        """Full-resolution stream — the one that gets recorded."""
        return self._url(self.main_path, redact=False)

    @property
    def sub_url(self) -> str:
        """Low-resolution stream — used for live preview and snapshots so the
        preview never competes with the recording for camera bandwidth."""
        return self._url(self.sub_path, redact=False)

    @property
    def safe_main_url(self) -> str:
        """Password-masked main URL. **Always** use this in logs and the UI."""
        return self._url(self.main_path, redact=True)

    @property
    def safe_sub_url(self) -> str:
        """Password-masked sub URL."""
        return self._url(self.sub_path, redact=True)

    def describe(self) -> dict:
        """Camera facts worth writing into a clip's sidecar. No password."""
        return {
            "work_center": self.work_center,
            "name": self.name,
            "host": self.host,
            "model": self.model,
            "backend": self.backend,
            "url": self.safe_main_url,
        }


def cameras_file() -> Path:
    """Path to cameras.yaml.

    Overridable with REPAIRCAM_CAMERAS so a test or a second bench layout can
    point somewhere else.
    """
    raw = os.environ.get("REPAIRCAM_CAMERAS")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "cameras.yaml"


def load_cameras(path: Path | None = None) -> dict[str, CameraConfig]:
    """Read cameras.yaml into ``{work_center: CameraConfig}``."""
    path = path or cameras_file()
    if not path.exists():
        raise ConfigError(
            f"No camera config at {path}.\n"
            f"Copy the example and fill in your camera:\n"
            f"    cp {path.parent / 'cameras.example.yaml'} {path}"
        )

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    entries = raw.get("cameras")
    if not isinstance(entries, dict) or not entries:
        raise ConfigError(f"{path} has no 'cameras:' section with at least one bench.")

    cameras: dict[str, CameraConfig] = {}
    for work_center, values in entries.items():
        if not isinstance(values, dict):
            raise ConfigError(f"Camera '{work_center}' in {path} must be a block of settings.")
        host = values.get("host")
        if not host:
            raise ConfigError(f"Camera '{work_center}' in {path} is missing 'host' (its IP address).")

        known = {
            "name",
            "host",
            "username",
            "password",
            "port",
            "backend",
            "main_path",
            "sub_path",
            "model",
            "has_audio",
            "notes",
        }
        cameras[str(work_center)] = CameraConfig(
            work_center=str(work_center),
            name=str(values.get("name") or work_center),
            host=str(host),
            username=str(values.get("username", "admin")),
            password=str(values.get("password", "")),
            port=int(values.get("port", DEFAULT_RTSP_PORT)),
            backend=str(values.get("backend", "rtsp")),
            main_path=str(values.get("main_path", "/stream1")),
            sub_path=str(values.get("sub_path", "/stream2")),
            model=str(values.get("model", "")),
            has_audio=bool(values.get("has_audio", True)),
            notes=str(values.get("notes", "")),
            extra={k: v for k, v in values.items() if k not in known},
        )
    return cameras


def get_camera(work_center: str, path: Path | None = None) -> CameraConfig:
    """Look up one bench, with a helpful error listing the benches that exist."""
    cameras = load_cameras(path)
    try:
        return cameras[work_center]
    except KeyError:
        known = ", ".join(sorted(cameras)) or "(none)"
        raise ConfigError(
            f"No camera configured for work center '{work_center}'. Configured: {known}"
        ) from None
