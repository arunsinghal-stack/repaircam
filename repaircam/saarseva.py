"""Client for saar-seva, the shop's existing job app.

RepairCam **polls** saar-seva; saar-seva never calls RepairCam. saar-seva runs on
Render in the cloud, the recorder sits on the shop LAN behind a home router, and
the cloud cannot open a connection into the shop — which is exactly the property
that keeps the cameras safe. See docs/PHASE5-CONTRACT.md.

Built on ``urllib`` from the standard library rather than ``requests`` so the
recorder box needs nothing extra installed.

Both halves exist: saar-seva's `/trc/active` and `/trc/recordings` are merged to
its **staging** branch. This stays switched off until saarseva.yaml is created AND
saar-seva has REPAIRCAM_API_KEY set (without it those endpoints 503 everyone).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .catalogue import JobLabels, Recording
from .config import ConfigError, data_dir

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT = 10.0


class SaarSevaError(Exception):
    """saar-seva could not be reached, or answered with something unusable."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SaarSevaConfig:
    """Contents of saarseva.yaml. Holds a service token — gitignored."""

    base_url: str
    api_key: str = ""
    link_base: str = ""
    poll_seconds: float = DEFAULT_POLL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT
    work_centers: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def safe_api_key(self) -> str:
        """For logs and the status page. Never show the token itself."""
        return "set (hidden)" if self.api_key else "NOT SET"

    def describe(self) -> dict:
        return {
            "base_url": self.base_url,
            "link_base": self.link_base,
            "poll_seconds": self.poll_seconds,
            "work_centers": self.work_centers or "all configured benches",
            "api_key": self.safe_api_key,
            "enabled": self.enabled,
        }


def config_file() -> Path:
    raw = os.environ.get("REPAIRCAM_SAARSEVA")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "saarseva.yaml"


def load_config(path: Path | None = None) -> SaarSevaConfig:
    """Read saarseva.yaml.

    Raises ConfigError if it is absent — the caller decides whether that is a
    problem. Today it usually is not: no config simply means the auto-trigger is
    switched off and technicians press Start themselves.
    """
    path = path or config_file()
    if not path.exists():
        raise ConfigError(
            f"No saar-seva config at {path}.\n"
            f"The automatic trigger is off; technicians start recordings by hand.\n"
            f"To turn it on, copy the example:\n"
            f"    cp {path.parent / 'saarseva.example.yaml'} {path}"
        )

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    values = raw.get("saarseva")
    if not isinstance(values, dict):
        raise ConfigError(f"{path} has no 'saarseva:' section.")

    base_url = str(values.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ConfigError(f"{path} is missing 'base_url'.")

    centers = values.get("work_centers") or []
    if isinstance(centers, str):
        centers = [c.strip() for c in centers.split(",") if c.strip()]

    return SaarSevaConfig(
        base_url=base_url,
        api_key=str(values.get("api_key") or ""),
        link_base=str(values.get("link_base") or "").rstrip("/"),
        poll_seconds=float(values.get("poll_seconds", DEFAULT_POLL_SECONDS)),
        timeout_seconds=float(values.get("timeout_seconds", DEFAULT_TIMEOUT)),
        work_centers=[str(c) for c in centers],
        enabled=bool(values.get("enabled", True)),
    )


def is_configured(path: Path | None = None) -> bool:
    """True if the auto-trigger has been set up at all."""
    return (path or config_file()).exists()


# --------------------------------------------------------------------------
# What saar-seva tells us
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveOperation:
    """One timer a technician currently has running in saar-seva."""

    #: saar-seva's repair_time_log id — see `key`.
    time_log_id: str = ""
    #: Odoo work-centre id. This, not a name, is how a bench is identified;
    #: cameras.yaml carries the same number per bench.
    workcenter_id: int | None = None
    workcenter_name: str = ""
    job_id: str = ""
    mo_name: str = ""
    operation: str = ""
    device: str = ""
    imei: str = ""
    technician: str = ""
    workorder_id: str = ""
    object_type: str = ""  # "mo" or "repair_order"
    started_at: str = ""

    @property
    def key(self) -> str:
        """Identity of this recording session, stable from Start to Stop.

        The time-log id is exactly right: saar-seva creates one per Start and
        closes it on Stop, and never reuses it. One clip per timer session.
        """
        if self.time_log_id:
            return f"log:{self.time_log_id}"
        # Older/partial responses: fall back to something stable-ish rather
        # than treating every poll as a new operation.
        return f"job:{self.job_id}|op:{self.operation}"

    def labels(self) -> JobLabels:
        return JobLabels(
            mo_name=self.mo_name,
            operation=self.operation,
            device=self.device,
            imei=self.imei,
            technician=self.technician,
        )


def _int(value: Any) -> int | None:
    """Coerce a JSON value to an int, or None if it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    """Coerce a JSON value to a string without turning None into 'None'."""
    if value is None:
        return ""
    return str(value).strip()


def parse_active(payload: Any) -> list[ActiveOperation]:
    """Read the /trc/active response.

    Accepts either ``{"active": [...]}`` or a bare list. Entries with no work
    centre are dropped: without one there is no bench and so no camera.
    """
    if isinstance(payload, dict):
        rows = payload.get("active", payload.get("operations", payload.get("results", [])))
    else:
        rows = payload
    if not isinstance(rows, list):
        raise SaarSevaError(f"expected a list of active operations, got {type(payload).__name__}")

    operations: list[ActiveOperation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        workcenter_id = _int(row.get("workcenter_id") or row.get("odoo_workcenter_id"))
        if workcenter_id is None:
            log.warning("ignoring an active operation with no workcenter_id: %s", row)
            continue
        operations.append(
            ActiveOperation(
                time_log_id=_text(row.get("time_log_id")),
                workcenter_id=workcenter_id,
                workcenter_name=_text(row.get("workcenter_name")),
                job_id=_text(row.get("job_id")),
                mo_name=_text(row.get("mo_name") or row.get("odoo_ref")),
                operation=_text(row.get("operation") or row.get("step")),
                device=_text(row.get("device") or row.get("product_name")),
                imei=_text(row.get("imei") or row.get("serial")),
                technician=_text(row.get("technician")),
                workorder_id=_text(row.get("workorder_id")),
                object_type=_text(row.get("object_type")),
                started_at=_text(row.get("started_at")),
            )
        )
    return operations


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class SaarSevaClient:
    """Talks to saar-seva over HTTPS. One method per contract endpoint."""

    def __init__(self, config: SaarSevaConfig, *, opener=None):
        self.config = config
        # Injectable so tests never touch the network.
        self._opener = opener or urllib.request.urlopen

    # -- plumbing -----------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        url = f"{self.config.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.config.api_key:
            request.add_header("Authorization", f"Bearer {self.config.api_key}")

        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Say something useful about the common ones. A 401 here means the
            # service token is wrong, which looks nothing like an outage.
            detail = {401: "the API key was rejected", 403: "the API key is not allowed here",
                      404: f"{path} does not exist on the server yet"}.get(exc.code, "")
            raise SaarSevaError(f"{method} {path} failed: HTTP {exc.code}"
                                f"{' — ' + detail if detail else ''}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SaarSevaError(f"could not reach saar-seva at {self.config.base_url}: {exc}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SaarSevaError(f"{path} did not return JSON: {exc}") from exc

    # -- contract -----------------------------------------------------------

    def fetch_active(self, workcenter_ids: list[int] | None = None) -> list[ActiveOperation]:
        """Which operations are being worked on right now.

        ``workcenter_ids`` limits the answer to benches that have a camera, so
        saar-seva does not describe work RepairCam could never record.
        """
        params = {}
        if workcenter_ids:
            params["workcenters"] = ",".join(str(i) for i in sorted(set(workcenter_ids)))
        return parse_active(self._request("GET", "/trc/active", params=params or None))

    def post_recording(self, recording: Recording, *, operation: ActiveOperation | None = None) -> bool:
        """Hand a finished clip's link to saar-seva for the Odoo MO chatter.

        Returns True when saar-seva accepted it. The video itself never moves —
        only this link.
        """
        if not self.config.link_base:
            raise SaarSevaError(
                "link_base is not set in saarseva.yaml, so there is no address to send. "
                "Set it to this recorder's LAN URL, e.g. http://192.168.0.50:8080"
            )

        body = {
            "recording_id": recording.id,
            "url": f"{self.config.link_base}/clip/{recording.id}",
            "duration_s": recording.duration_s,
            "recorded_at": recording.started_at,
        }
        if operation:
            # The time log is what saar-seva keys the chatter post on, and what
            # makes a retry idempotent there.
            body["time_log_id"] = operation.time_log_id
            body["job_id"] = operation.job_id

        self._request("POST", "/trc/recordings", body=body)
        return True

    def check(self, workcenter_ids: list[int] | None = None) -> tuple[bool, str]:
        """Is saar-seva reachable and is the token accepted? For the status page."""
        try:
            active = self.fetch_active(workcenter_ids)
        except SaarSevaError as exc:
            return False, str(exc)
        return True, f"OK — {len(active)} operation(s) running"


def build_client(path: Path | None = None) -> SaarSevaClient:
    return SaarSevaClient(load_config(path))


def default_link_base(port: int = 8080) -> str:
    """Best guess at this recorder's LAN URL, to help fill in saarseva.yaml."""
    import socket

    try:
        # Does not actually send anything; just asks the OS which local address
        # would be used to reach the outside world.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return f"http://{sock.getsockname()[0]}:{port}"
    except OSError:
        return f"http://<this machine's IP>:{port}"


__all__ = [
    "ActiveOperation",
    "SaarSevaClient",
    "SaarSevaConfig",
    "SaarSevaError",
    "build_client",
    "config_file",
    "data_dir",
    "default_link_base",
    "is_configured",
    "load_config",
    "parse_active",
]
