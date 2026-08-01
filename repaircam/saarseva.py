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
    """saar-seva could not be reached, or answered with something unusable.

    ``status`` is the HTTP status when there was one, and None when the call
    never got an answer at all. The caller needs the difference: a 404 for a
    session saar-seva has never heard of will never succeed however long we
    retry, while an unreachable server is exactly the case that must be retried.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        """True when retrying cannot help. 404 = no such session, ever."""
        return self.status == 404


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


#: What kind of work a clip is of. Decides which endpoint its link is posted
#: back to, and is stored on the catalogue row so a restart does not lose it.
KIND_REPAIR = "repair"
KIND_PACKING = "packing"


@dataclass(frozen=True)
class ActiveOperation:
    """One thing saar-seva says is being recorded right now.

    Covers both integrations: a technician's repair timer, and a packer filming
    an order. They differ only in where the identity comes from and where the
    finished link is posted.
    """

    #: "repair" or "packing".
    kind: str = KIND_REPAIR
    #: saar-seva's repair_time_log id — see `key`. Repair only.
    time_log_id: str = ""
    #: saar-seva's packing_recording id — see `key`. Packing only.
    packing_recording_id: str = ""
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
    def source_ref(self) -> str:
        """saar-seva's own id for this session, whichever kind it is."""
        return self.packing_recording_id if self.kind == KIND_PACKING else self.time_log_id

    @property
    def key(self) -> str:
        """Identity of this recording session, stable from Start to Stop.

        saar-seva creates one row per Start and closes it on Stop, never
        reusing the id — exactly the property needed. One clip per session.
        """
        if self.source_ref:
            return f"{self.kind}:{self.source_ref}"
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


def parse_active_packing(payload: Any) -> list[ActiveOperation]:
    """Read the /pack/active response — packers currently filming an order.

    Labelled by order rather than by device: a packing clip's job is the order
    it is packing, so the order/SO reference goes where the MO would.
    """
    if isinstance(payload, dict):
        rows = payload.get("active", payload.get("recordings", []))
    else:
        rows = payload
    if not isinstance(rows, list):
        raise SaarSevaError(f"expected a list of active packing, got {type(payload).__name__}")

    operations: list[ActiveOperation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        workcenter_id = _int(row.get("workcenter_id"))
        if workcenter_id is None:
            log.warning("ignoring active packing with no workcenter_id: %s", row)
            continue
        operations.append(
            ActiveOperation(
                kind=KIND_PACKING,
                packing_recording_id=_text(row.get("recording_id")),
                workcenter_id=workcenter_id,
                workcenter_name=_text(row.get("workcenter_name")),
                job_id=_text(row.get("packing_job_id")),
                # The order is what a packing clip is "about".
                mo_name=_text(row.get("so_names") or row.get("order_ref")),
                operation="Packing",
                device=_text(row.get("ship_to_name")),
                technician=_text(row.get("packer")),
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
        #: Camera-list revision last seen on a poll. None until saar-seva sends
        #: one — an older server that never does must not look like revision 0.
        self.last_config_revision: int | None = None
        #: Storage-policy revision, tracked separately from the camera list:
        #: the two change on entirely different schedules, and one number for
        #: both would re-fetch a camera list because a retention window moved.
        self.last_storage_revision: int | None = None

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
            raise SaarSevaError(
                f"{method} {path} failed: HTTP {exc.code}"
                f"{' — ' + detail if detail else ''}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SaarSevaError(f"could not reach saar-seva at {self.config.base_url}: {exc}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SaarSevaError(f"{path} did not return JSON: {exc}") from exc

    def _note_revision(self, payload: Any) -> None:
        """Remember the config revisions that rode in on this poll.

        Kept on the client rather than threaded through ``parse_active`` so
        those stay pure functions over the wire format — and so a saar-seva too
        old to send it simply leaves the last value alone rather than looking
        like revision 0, which would trigger a pointless re-sync.
        """
        if not isinstance(payload, dict):
            return
        for field, attribute in (
            ("config_revision", "last_config_revision"),
            ("storage_revision", "last_storage_revision"),
        ):
            if field not in payload:
                continue
            try:
                setattr(self, attribute, int(payload[field]))
            except (TypeError, ValueError):
                log.debug("ignoring an unreadable %s", field)

    # -- contract -----------------------------------------------------------

    def fetch_active(self, workcenter_ids: list[int] | None = None) -> list[ActiveOperation]:
        """Which operations are being worked on right now.

        ``workcenter_ids`` limits the answer to benches that have a camera, so
        saar-seva does not describe work RepairCam could never record.
        """
        params = {}
        if workcenter_ids:
            params["workcenters"] = ",".join(str(i) for i in sorted(set(workcenter_ids)))
        payload = self._request("GET", "/trc/active", params=params or None)
        self._note_revision(payload)
        return parse_active(payload)

    def fetch_active_packing(self, workcenter_ids: list[int] | None = None) -> list[ActiveOperation]:
        """Which packing benches are filming right now."""
        params = {}
        if workcenter_ids:
            params["workcenters"] = ",".join(str(i) for i in sorted(set(workcenter_ids)))
        payload = self._request("GET", "/pack/active", params=params or None)
        self._note_revision(payload)
        return parse_active_packing(payload)

    def post_packing_recording(self, recording: Recording) -> bool:
        """Hand a finished packing clip's link to saar-seva.

        saar-seva decides which Delivery Order it belongs on — one-step orders
        have a single outgoing picking, two-step ones an internal pick as well,
        and only the outgoing one is the customer's delivery.
        """
        if not self.config.link_base:
            raise SaarSevaError(
                "link_base is not set in saarseva.yaml, so there is no address to send."
            )
        if not recording.source_ref:
            raise SaarSevaError(f"clip {recording.id} has no packing session to post against")

        self._request("POST", "/pack/recordings", body={
            "recording_id": recording.source_ref,
            "repaircam_recording_id": recording.id,
            "url": f"{self.config.link_base}/clip/{recording.id}",
            "duration_s": recording.duration_s,
            "recorded_at": recording.started_at,
        })
        return True

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
        # The time log is what saar-seva keys the chatter post on, and what
        # makes a retry idempotent there. It comes from the CATALOGUE
        # (source_ref, written when the clip was filed), not from the in-memory
        # map: retries are driven from the catalogue precisely so a clip
        # survives a restart, and a retry that arrives without the session id is
        # a 404 no matter how many times it is sent.
        time_log_id = recording.source_ref or (operation.time_log_id if operation else "")
        if not time_log_id:
            raise SaarSevaError(
                f"clip {recording.id} has no saar-seva session to post against"
            )
        body["time_log_id"] = time_log_id
        if operation:
            body["job_id"] = operation.job_id

        self._request("POST", "/trc/recordings", body=body)
        return True

    def fetch_camera_config(self) -> dict:
        """The whole camera list, including passwords.

        Only called when the ``config_revision`` seen on an ordinary poll
        differs from the one this box last applied, so in steady state it is
        never called at all.
        """
        payload = self._request("GET", "/repaircam/cameras")
        if not isinstance(payload, dict):
            raise SaarSevaError(
                f"expected a camera list, got {type(payload).__name__}"
            )
        return payload

    def fetch_storage_config(self) -> dict:
        """How long the shop keeps footage, as the admin panel has it.

        Only called when the ``storage_revision`` seen on an ordinary poll
        differs from the one this box last applied, so in steady state it is
        never called at all.
        """
        payload = self._request("GET", "/repaircam/storage-config")
        if not isinstance(payload, dict):
            raise SaarSevaError(
                f"expected a storage policy, got {type(payload).__name__}"
            )
        return payload

    def post_heartbeat(self, benches: list[dict], storage: dict | None = None) -> bool:
        """Tell saar-seva what each bench's camera is ACTUALLY doing.

        saar-seva cannot see into the shop, so without this its technician
        screen can only show "a timer is running" — which is not the same thing
        as "you are being filmed", and differs in every case that matters: the
        recorder box off, the camera unplugged, the bench missing its
        odoo_workcenter_id, the shop's internet down. A light driven by the
        timer would be confidently red through all of them.

        Sent on the poll RepairCam already makes. Freshness is the point, so
        saar-seva must treat a heartbeat it has not heard for a few polls as
        "unknown", never as the last state it saw.
        """
        body = {"recorder": self.config.link_base, "benches": benches}
        if storage:
            # What this recorder actually holds — free space, clip counts, how
            # far back the shop can really look, and whether anything is ever
            # deleted at all. An admin panel that can set a retention window
            # without this is guessing: the footage and the catalogue are both
            # on the recorder, and nothing else can answer for them.
            #
            # An older saar-seva ignores the extra field, which is why this
            # rides the heartbeat rather than needing an endpoint of its own.
            body["storage"] = storage
        self._request("POST", "/trc/recorder-heartbeat", body=body)
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
