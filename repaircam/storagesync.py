"""Apply the retention policy saar-seva holds, without letting it touch this box.

The shop's admin panel decides how long footage is kept. This takes what it
sends and writes the policy half of ``storage.yaml`` — and nothing else.

**The refusals are the point.** A cloud service is telling a machine in the shop
how long to keep the shop's only record of its own work, so it gets a second
opinion at this end, exactly as the camera list does:

- Four settings describe THIS RECORDER and may never arrive from anywhere else:
  ``archive_dir``, ``keep_days_local``, and both delete switches. A payload that
  so much as names one is rejected whole, not filtered. A server sending them is
  either a version mismatch or something worse, and quietly dropping the fields
  would hide both. saar-seva refuses them at its end too; neither end trusts the
  other to have remembered.
- A window under seven days is refused. ``0`` means "delete everything" and must
  not be reachable by a typo in a web form.
- Revision 0 means nobody has ever saved a policy centrally. Nothing is applied:
  a shop not using the feature must not have its retention replaced by
  somebody's defaults.

Nothing here deletes anything. Writing a shorter window does not shorten
retention on the spot — ``storage.note_window_changes`` holds any reduction and
makes the recorder refuse to act on it until a person at the shop agrees, which
is the whole reason that guard was built before this was.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import storage

log = logging.getLogger(__name__)

#: Settings that belong to this machine. Never accepted from the network.
LOCAL_ONLY = (
    "archive_dir",
    "keep_days_local",
    "delete_after_archive",
    "delete_from_archive",
)

#: The keys this sync owns. Everything else in storage.yaml is left alone.
CENTRAL_KEYS = ("keep_days", "keep_days_by_source", "min_free_gb", "warn_free_gb")

MIN_KEEP_DAYS = 7
MAX_KEEP_DAYS = 3650
MIN_FREE_FLOOR_GB = 5.0


class StorageSyncError(Exception):
    """The policy was refused. storage.yaml is untouched."""


@dataclass
class SyncResult:
    """What one sync did, for the log, the CLI and the status page."""

    revision: int = 0
    changed: dict = field(default_factory=dict)
    unchanged: bool = False

    def summary(self) -> str:
        if self.unchanged:
            return f"revision {self.revision}: no change"
        if not self.changed:
            return f"revision {self.revision}: nothing to apply"
        bits = [f"{key} {before} -> {after}" for key, (before, after) in sorted(self.changed.items())]
        return f"revision {self.revision}: {', '.join(bits)}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _days(value, what: str) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise StorageSyncError(f"{what} is not a number of days: {value!r}") from None
    if not MIN_KEEP_DAYS <= days <= MAX_KEEP_DAYS:
        raise StorageSyncError(
            f"{what} is {days} days, outside the {MIN_KEEP_DAYS}-{MAX_KEEP_DAYS} "
            f"range this recorder will accept."
        )
    return days


def _gb(value, what: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise StorageSyncError(f"{what} is not a number of GB: {value!r}") from None
    if amount < MIN_FREE_FLOOR_GB:
        raise StorageSyncError(f"{what} is below {MIN_FREE_FLOOR_GB:g} GB.")
    return amount


def validate(payload: dict) -> tuple[int, dict]:
    """Check a payload and return ``(revision, settings)``.

    ``settings`` uses this recorder's own key names, so the caller never has to
    translate the wire format twice.
    """
    if not isinstance(payload, dict):
        raise StorageSyncError(f"expected a storage policy, got {type(payload).__name__}")

    # Anywhere in the payload, at any depth we look at — a local-only key here
    # is not something to tidy away silently.
    for scope in (payload, payload.get("retention") or {}, payload.get("free_space") or {}):
        if isinstance(scope, dict):
            named = sorted(key for key in LOCAL_ONLY if key in scope)
            if named:
                raise StorageSyncError(
                    f"the central policy tried to set {', '.join(named)}, which belongs "
                    f"to this recorder. Refusing the whole policy."
                )

    try:
        revision = int(payload.get("revision") or 0)
    except (TypeError, ValueError):
        raise StorageSyncError("the policy has no usable revision number") from None

    retention = payload.get("retention") or {}
    free_space = payload.get("free_space") or {}
    if not isinstance(retention, dict) or not isinstance(free_space, dict):
        raise StorageSyncError("retention and free_space must both be objects")

    by_source_in = retention.get("by_source") or {}
    if not isinstance(by_source_in, dict):
        raise StorageSyncError("retention.by_source must be an object")

    by_source = {
        str(source): _days(days, f"the {source} window")
        for source, days in by_source_in.items()
        if str(source).strip()
    }

    min_gb = _gb(free_space.get("min_gb", storage.DEFAULT_MIN_FREE_GB), "the free-space floor")
    warn_gb = _gb(free_space.get("warn_gb", storage.DEFAULT_WARN_FREE_GB), "the free-space warning")
    if warn_gb < min_gb:
        raise StorageSyncError(
            "the free-space warning is below the level at which recording stops, "
            "so it could never warn anybody."
        )

    return revision, {
        "keep_days": _days(
            retention.get("default_days", storage.DEFAULT_KEEP_DAYS),
            "the default window",
        ),
        "keep_days_by_source": by_source,
        "min_free_gb": min_gb,
        "warn_free_gb": warn_gb,
    }


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _read_existing(path: Path) -> dict:
    """Whatever is in storage.yaml now, or {} — never an exception.

    A file that will not parse must not stop a policy being applied; it would
    also be about to be replaced by one that does parse.
    """
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        log.warning("could not read %s, treating it as empty: %s", path, exc)
        return {}
    values = raw.get("storage")
    return values if isinstance(values, dict) else {}


def apply(payload: dict, *, path: Path | None = None) -> SyncResult:
    """Write the central half of storage.yaml. Raises rather than half-apply."""
    path = path or storage.config_file()
    revision, settings = validate(payload)

    if revision <= 0:
        # Nobody has saved a policy. Adopting our defaults here would silently
        # replace whatever this shop chose for itself.
        return SyncResult(revision=revision, unchanged=True)

    existing = _read_existing(path)
    changed = {
        key: (existing.get(key), value)
        for key, value in settings.items()
        if existing.get(key) != value
    }
    if not changed:
        return SyncResult(revision=revision, unchanged=True)

    _write(path, existing, settings, revision)
    result = SyncResult(revision=revision, changed=changed)
    log.info("storage policy synced — %s", result.summary())
    return result


def _write(path: Path, existing: dict, settings: dict, revision: int) -> None:
    """Replace storage.yaml atomically, keeping one backup.

    The file is rebuilt rather than patched, so it is written in two labelled
    halves: what this box decided, and what came from the admin panel. Somebody
    opening it to change a number needs to know which of their edits will
    survive — an unlabelled merged file teaches that the hard way, by silently
    reverting a change a week later.

    The explanatory comments a hand-written storage.yaml carries cannot survive
    a machine rewrite, so the header says where they went.
    """
    local = {key: existing[key] for key in LOCAL_ONLY if key in existing}
    for key, value in existing.items():
        # Anything else somebody put in the file and we know nothing about.
        if key not in LOCAL_ONLY and key not in CENTRAL_KEYS:
            local[key] = value

    body = [
        "# RepairCam storage settings.",
        "#",
        "# The second half of this file is written by RepairCam from saar-seva's",
        "# admin panel (Admin -> TRC settings). Editing those lines here does",
        "# nothing lasting: the next sync overwrites them.",
        "#",
        "# The first half belongs to THIS recorder and is never sent or received.",
        "# Edit it here, by hand — nothing central can change it.",
        "#",
        "# What each setting means is explained in storage.example.yaml, which a",
        "# machine-written file cannot keep comments for.",
        "",
        "storage:",
        "  # ---- this recorder's own. Yours to edit; nothing central touches these.",
    ]
    body.extend(_lines(local) or ["  # (none set)"])
    body.extend([
        "",
        f"  # ---- from the admin panel, revision {revision}. Edits here are overwritten.",
    ])
    body.extend(_lines(settings))
    text = "\n".join(body) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # One backup, so a policy that turns out wrong can be put back by hand.
        path.with_suffix(path.suffix + ".bak").write_text(path.read_text())

    # Temp file plus rename: a crash mid-write must not leave a truncated
    # storage.yaml, which would take the archive settings down with it.
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".storage-", suffix=".yaml")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        os.replace(str(temp), str(path))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _lines(values: dict) -> list[str]:
    """Two-space-indented YAML for one block of settings."""
    if not values:
        return []
    dumped = yaml.safe_dump(values, sort_keys=True, default_flow_style=False).rstrip("\n")
    return [f"  {line}" if line.strip() else line for line in dumped.split("\n")]
