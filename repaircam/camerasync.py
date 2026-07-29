"""Apply the camera list saar-seva holds to this recorder's cameras.yaml.

Adding a bench used to mean SSHing into the shop box and editing YAML. The list
now lives in the admin panel, and this is the half that receives it — on the
poll the trigger already makes, so keeping in step costs no extra request.

**This is the one path where a cloud service changes what the recorder points
at**, so it is deliberately suspicious of what it is given:

- **Private addresses only.** saar-seva checks too; this checks again, because
  the whole point of the check is that saar-seva might be wrong.
- **All or nothing.** One bad row rejects the whole payload. A half-applied
  camera list is worse than a stale one — nothing tells you which half took.
- **An empty list is refused.** Zero cameras almost certainly means a bug or a
  wiped setting, not "we removed every camera in the shop".
- **A bench that is recording is never touched.** Footage in flight cannot be
  reproduced; a config change can wait until the clip is finished.
- **A blank password never overwrites a real one.** A bench that loses its
  password stops recording, and does it quietly.
- **Failing is not fatal.** The old file stays in place and the next revision
  change tries again. Recording must survive saar-seva being wrong.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config

log = logging.getLogger(__name__)


#: The only addresses a shop camera can have — RFC 1918, and nothing else.
#: ``ipaddress.is_private`` is not the same test: it also accepts loopback,
#: link-local and the RFC 5737 documentation ranges, none of which is a camera
#: on a shop LAN, and one of which (203.0.113.x) reads as perfectly ordinary.
SHOP_NETWORKS = (
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
)


class CameraSyncError(Exception):
    """The payload was refused. cameras.yaml is unchanged."""


@dataclass
class SyncResult:
    """What one sync did, for the log, the CLI and the status page."""

    revision: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Benches left alone because they were mid-recording. While this is
    #: non-empty the revision is NOT recorded as applied, so the sync runs
    #: again once the bench goes idle.
    deferred: list[str] = field(default_factory=list)
    unchanged: bool = False

    @property
    def complete(self) -> bool:
        return not self.deferred

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def summary(self) -> str:
        if self.unchanged:
            return f"revision {self.revision}: no change"
        bits = []
        for label, items in (
            ("added", self.added), ("updated", self.updated), ("removed", self.removed)
        ):
            if items:
                bits.append(f"{label} {', '.join(sorted(items))}")
        if self.deferred:
            bits.append(f"WAITING for {', '.join(sorted(self.deferred))} to finish recording")
        return f"revision {self.revision}: " + ("; ".join(bits) or "no change")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _private_host(host: str) -> str:
    """Reject anything that is not a private IP.

    A compromised or mistyped saar-seva must not be able to aim the recorder at
    a host on the internet — that would turn a shop camera client into an
    outbound connection to somewhere nobody chose.
    """
    host = str(host or "").strip()
    if not host:
        raise CameraSyncError("a camera has no host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise CameraSyncError(
            f"{host!r} is not an IP address; cameras are named by IP on the shop LAN"
        ) from None
    if not any(address in net for net in SHOP_NETWORKS):
        raise CameraSyncError(
            f"{host} is not a shop-network address — refusing to point at it. "
            f"Cameras live on 192.168.x, 10.x or 172.16-31.x."
        )
    return host


def validate(payload: dict) -> tuple[int, list[dict]]:
    """Check the whole payload, or raise. Returns ``(revision, cameras)``."""
    if not isinstance(payload, dict):
        raise CameraSyncError(f"expected an object, got {type(payload).__name__}")

    try:
        revision = int(payload.get("revision"))
    except (TypeError, ValueError):
        raise CameraSyncError("the camera list has no revision number") from None

    cameras = payload.get("cameras")
    if not isinstance(cameras, list):
        raise CameraSyncError("the camera list is missing")
    if not cameras:
        # Not a legitimate state: a shop with no cameras would not be running a
        # recorder. Far more likely a wiped setting or a bug at the other end.
        raise CameraSyncError("the camera list is empty — refusing to wipe the local one")

    checked: list[dict] = []
    seen: set[int] = set()
    for entry in cameras:
        if not isinstance(entry, dict):
            raise CameraSyncError(f"a camera entry is {type(entry).__name__}, not an object")
        try:
            workcenter_id = int(entry.get("odoo_workcenter_id"))
        except (TypeError, ValueError):
            raise CameraSyncError("a camera has no odoo_workcenter_id") from None
        if workcenter_id in seen:
            raise CameraSyncError(f"work centre {workcenter_id} appears twice")
        seen.add(workcenter_id)

        checked.append({
            "odoo_workcenter_id": workcenter_id,
            "name": str(entry.get("name") or f"Work centre {workcenter_id}")[:80],
            "host": _private_host(entry.get("host")),
            "port": int(entry.get("port") or config.DEFAULT_RTSP_PORT),
            "username": str(entry.get("username") or "admin"),
            "password": str(entry.get("password") or ""),
            "main_path": str(entry.get("main_path") or "/stream1"),
            "sub_path": str(entry.get("sub_path") or "/stream2"),
            "has_audio": bool(entry.get("has_audio", True)),
            "enabled": bool(entry.get("enabled", True)),
            "notes": str(entry.get("notes") or ""),
        })
    return revision, checked


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


def _existing(path: Path) -> dict:
    """The raw cameras.yaml mapping, or {} if there is not one yet."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise CameraSyncError(f"the current {path.name} is not valid YAML: {exc}") from exc
    cameras = raw.get("cameras")
    return dict(cameras) if isinstance(cameras, dict) else {}


def _bench_code(workcenter_id: int, existing: dict) -> str:
    """Keep the code a bench already has; invent one only for a new bench.

    Codes appear in clip filenames and in every path under the data directory,
    so renaming a bench would orphan its recordings on disk.
    """
    for code, entry in existing.items():
        if isinstance(entry, dict) and entry.get("odoo_workcenter_id") == workcenter_id:
            return str(code)
    return f"WC{workcenter_id}"


def _entry(camera: dict, previous: dict | None) -> dict:
    """One cameras.yaml block, with the password rule applied."""
    password = camera["password"]
    if not password:
        # Central has none for this bench. Whatever is already on the box is
        # better than nothing: a bench with no password simply stops recording.
        password = str((previous or {}).get("password") or "")

    entry = {
        "name": camera["name"],
        "host": camera["host"],
        "port": camera["port"],
        "username": camera["username"],
        "password": password,
        "main_path": camera["main_path"],
        "sub_path": camera["sub_path"],
        "has_audio": camera["has_audio"],
        "odoo_workcenter_id": camera["odoo_workcenter_id"],
    }
    if camera["notes"]:
        entry["notes"] = camera["notes"]
    # Local-only fields nobody central knows about (the camera model, say) are
    # not ours to drop.
    for key, value in (previous or {}).items():
        entry.setdefault(key, value)
    return entry


def apply(
    payload: dict,
    *,
    busy: set[str] | frozenset[str] = frozenset(),
    path: Path | None = None,
) -> SyncResult:
    """Write the camera list to cameras.yaml. Raises rather than half-apply.

    ``busy`` is the set of bench codes currently recording. Those keep whatever
    they already have, and the result reports them as deferred so the caller
    knows not to mark this revision as applied.
    """
    path = path or config.cameras_file()
    revision, cameras = validate(payload)
    existing = _existing(path)

    result = SyncResult(revision=revision)
    new_map: dict[str, dict] = {}

    for camera in cameras:
        code = _bench_code(camera["odoo_workcenter_id"], existing)
        previous = existing.get(code)

        if not camera["enabled"]:
            # Removed centrally. Disabled rather than deleted there, absent
            # here — a bench with no camera entry simply is not a bench.
            if previous is None:
                continue
            if code in busy:
                new_map[code] = previous
                result.deferred.append(code)
                continue
            result.removed.append(code)
            continue

        entry = _entry(camera, previous)
        if code in busy and previous is not None and entry != previous:
            # Never rewrite a bench mid-clip. ffmpeg is already attached to the
            # old address; changing the file under it achieves nothing good.
            new_map[code] = previous
            result.deferred.append(code)
            continue

        new_map[code] = entry
        if previous is None:
            result.added.append(code)
        elif entry != previous:
            result.updated.append(code)

    # Benches on this box that saar-seva no longer lists. The central list is
    # authoritative for the benches it can express, but a bench mid-clip is
    # never dropped out from under it.
    known = {c["odoo_workcenter_id"] for c in cameras}
    for code, previous in existing.items():
        if code in new_map or not isinstance(previous, dict):
            continue
        local_id = previous.get("odoo_workcenter_id")
        if local_id in known:
            continue
        if local_id is None:
            # No work-centre id, so the central list cannot describe this bench
            # and its absence there means nothing. Somebody added it by hand;
            # leave it be.
            new_map[code] = previous
            continue
        if code in busy:
            new_map[code] = previous
            result.deferred.append(code)
        else:
            result.removed.append(code)
            log.warning("%s is not in the central camera list; removing it", code)

    if not new_map:
        raise CameraSyncError("applying this list would leave no cameras at all")

    if new_map == existing:
        result.unchanged = True
        return result

    _write(path, new_map)
    log.info("camera config synced — %s", result.summary())
    return result


def _write(path: Path, cameras: dict) -> None:
    """Replace cameras.yaml atomically, keeping one backup.

    Temp file plus rename, so a crash mid-write cannot leave a truncated file
    where the camera passwords used to be.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    body = yaml.safe_dump({"cameras": cameras}, sort_keys=False, allow_unicode=True)
    header = (
        "# Written by RepairCam from the central camera list in saar-seva.\n"
        "# Edits here are overwritten on the next change there — use the admin\n"
        "# panel (Admin -> TRC settings -> Cameras) instead.\n"
        "# The previous version is kept alongside as cameras.yaml.bak.\n"
    )
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(header + body)
    # Passwords live in here, so it is nobody else's business on a shared box.
    os.chmod(temp, 0o600)
    temp.replace(path)
