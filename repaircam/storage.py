"""Keeping the disk from filling, and getting a second copy off the box.

A bench-hour of copied 4 Mbps video is roughly **1.8 GB**. Nothing here deleted
or moved anything until this module existed, so the recorder's disk filled and
the first thing anyone learned about it was a recording stopping in the middle
of a repair.

Three pieces, and the order between them is the whole safety argument:

1. **A free-space guard.** Recording is refused *before* it starts when the disk
   is nearly full, because a clip that dies half-written is worse than one that
   was never begun — the technician at least knows about the second.
2. **An archive copy.** Clips are copied to a second disk or a NAS mount and
   verified there. Until that exists, the shop's only copy of every repair is
   on one laptop.
3. **Retention, which deletes only what is archived.** Age alone is never
   enough. A clip is removed locally only once a verified copy exists
   elsewhere, and the check is re-run at the moment of deletion.

Archiving is off until ``storage.yaml`` names a destination. The guard is not:
it works with sensible defaults on any install, because the failure it prevents
is the one that costs footage.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config
from .catalogue import Catalogue, Recording, sidecar_for

log = logging.getLogger(__name__)

#: Roughly what one bench-hour of copied video costs, at the 4096 kbps the
#: VIGI is capped to. Used to turn "GB free" into a number anyone can act on.
GB_PER_BENCH_HOUR = 1.8

#: Refuse to start a new recording below this. Deliberately generous: it is
#: about eleven bench-hours, so a bench that starts a repair can finish it.
DEFAULT_MIN_FREE_GB = 20.0

#: Say something is wrong below this, while still recording.
DEFAULT_WARN_FREE_GB = 50.0

#: How long a clip stays on the recorder once it is safely archived, when
#: nothing more specific is set for what it is footage of.
DEFAULT_KEEP_DAYS = 30

#: Retention differs by what the footage is FOR, so it differs by source.
#: A repair is disputable for as long as it is under warranty; a packing
#: complaint — "the box was short an item" — arrives inside the delivery and
#: return window instead. One number for both would be wrong twice.
DEFAULT_KEEP_DAYS_BY_SOURCE = {"repair": 30, "packing": 45}


class StorageError(Exception):
    """Recording cannot start, or an archive copy failed."""


class DiskFull(StorageError):
    """There is not enough room to record. The message is shown to the bench."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    warn_free_gb: float = DEFAULT_WARN_FREE_GB
    #: Where the second copy goes. Empty means archiving is off — and with it,
    #: deletion, because nothing may be deleted that was never copied.
    archive_dir: str = ""
    #: Fallback, and what a clip started by hand in RepairCam gets — it belongs
    #: to no integration, so no integration's window applies to it.
    keep_days: int = DEFAULT_KEEP_DAYS
    #: Per-source overrides: {"repair": 30, "packing": 45}. A source not named
    #: here falls back to keep_days.
    keep_days_by_source: dict = field(default_factory=lambda: dict(DEFAULT_KEEP_DAYS_BY_SOURCE))
    #: Off by default even when a destination is set. Deleting footage is the
    #: one thing here that cannot be undone, so it is opted into explicitly.
    delete_after_archive: bool = False

    @property
    def archive_path(self) -> Path | None:
        return Path(self.archive_dir).expanduser() if self.archive_dir else None

    def keep_days_for(self, source: str) -> int:
        """How long this kind of footage stays on the recorder."""
        return int(self.keep_days_by_source.get(source or "", self.keep_days))

    def describe(self) -> dict:
        return {
            "min_free_gb": self.min_free_gb,
            "warn_free_gb": self.warn_free_gb,
            "archive_dir": self.archive_dir or "not set",
            "keep_days": self.keep_days,
            "keep_days_by_source": dict(self.keep_days_by_source),
            "delete_after_archive": self.delete_after_archive,
        }


def config_file() -> Path:
    raw = os.environ.get("REPAIRCAM_STORAGE")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "storage.yaml"


def load_config(path: Path | None = None) -> StorageConfig:
    """Read storage.yaml, or return the defaults if there is not one.

    Unlike the camera and saar-seva configs, a missing file is not "switched
    off" — the free-space guard still applies. Only archiving needs a setting,
    because only archiving needs somewhere to put things.
    """
    path = path or config_file()
    if not path.exists():
        return StorageConfig()

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise StorageError(f"{path} is not valid YAML: {exc}") from exc

    values = raw.get("storage")
    if not isinstance(values, dict):
        return StorageConfig()

    by_source = dict(DEFAULT_KEEP_DAYS_BY_SOURCE)
    raw_by_source = values.get("keep_days_by_source")
    if isinstance(raw_by_source, dict):
        for source, days in raw_by_source.items():
            try:
                by_source[str(source)] = int(days)
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"{path}: keep_days_by_source['{source}'] is not a number of days"
                ) from exc

    try:
        return StorageConfig(
            min_free_gb=float(values.get("min_free_gb", DEFAULT_MIN_FREE_GB)),
            warn_free_gb=float(values.get("warn_free_gb", DEFAULT_WARN_FREE_GB)),
            archive_dir=str(values.get("archive_dir") or "").strip(),
            keep_days=int(values.get("keep_days", DEFAULT_KEEP_DAYS)),
            keep_days_by_source=by_source,
            delete_after_archive=bool(values.get("delete_after_archive", False)),
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"{path} has a bad value: {exc}") from exc


# --------------------------------------------------------------------------
# How much room is left
# --------------------------------------------------------------------------


@dataclass
class DiskReport:
    free_gb: float
    total_gb: float
    used_percent: int
    bench_hours: float
    state: str  # "ok" | "low" | "full"
    message: str

    def as_dict(self) -> dict:
        return {
            "free_gb": round(self.free_gb, 1),
            "total_gb": round(self.total_gb, 1),
            "used_percent": self.used_percent,
            "bench_hours": round(self.bench_hours),
            "state": self.state,
            "message": self.message,
        }


def disk_report(cfg: StorageConfig | None = None, root: Path | None = None) -> DiskReport:
    """Free space, in the units a shop can act on."""
    cfg = cfg or load_config()
    root = root or config.data_dir()
    root.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(root)
    free_gb = usage.free / 1_073_741_824
    total_gb = usage.total / 1_073_741_824
    hours = free_gb / GB_PER_BENCH_HOUR

    if free_gb < cfg.min_free_gb:
        state = "full"
        message = (
            f"Only {free_gb:.0f} GB left — about {hours:.0f} bench-hours. "
            f"Recording is stopped until space is freed."
        )
    elif free_gb < cfg.warn_free_gb:
        state = "low"
        message = (
            f"{free_gb:.0f} GB left — about {hours:.0f} bench-hours. "
            f"Recording stops below {cfg.min_free_gb:.0f} GB."
        )
    else:
        state = "ok"
        message = f"{free_gb:.0f} GB free — about {hours:.0f} bench-hours."

    return DiskReport(
        free_gb=free_gb,
        total_gb=total_gb,
        used_percent=round(usage.used / usage.total * 100) if usage.total else 0,
        bench_hours=hours,
        state=state,
        message=message,
    )


def check_before_recording(cfg: StorageConfig | None = None, root: Path | None = None) -> None:
    """Raise DiskFull if there is not enough room to start.

    Checked at Start rather than during: a clip that dies half-written is worse
    than one that was never begun, because nobody notices the first until they
    go looking for footage that does not exist.
    """
    report = disk_report(cfg, root)
    if report.state == "full":
        raise DiskFull(report.message)


# --------------------------------------------------------------------------
# The second copy
# --------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    copied: list[int]
    failed: list[tuple[int, str]]
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped_reason

    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        bits = []
        if self.copied:
            bits.append(f"archived {len(self.copied)}")
        if self.failed:
            bits.append(f"FAILED {len(self.failed)}")
        return "; ".join(bits) or "nothing to archive"


def _copy_verified(source: Path, dest: Path) -> int:
    """Copy one file and prove it arrived. Returns the size copied.

    Written to a ``.part`` name and renamed, so an interrupted copy can never
    be mistaken for a complete one — which matters enormously here, because a
    file that merely *exists* at the archive is what later permits deleting the
    original.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    shutil.copy2(source, partial)

    expected = source.stat().st_size
    actual = partial.stat().st_size
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise StorageError(
            f"{source.name} copied as {actual} bytes, expected {expected} — "
            f"the destination may be full"
        )
    partial.replace(dest)
    return actual


def archive_pending(
    catalogue: Catalogue | None = None,
    cfg: StorageConfig | None = None,
    *,
    limit: int = 20,
    root: Path | None = None,
) -> ArchiveResult:
    """Copy clips that have no verified second copy yet.

    One clip at a time, oldest first, and a failure on one never stops the
    others — the failure modes here are a full destination and an unmounted
    NAS, and neither is a reason to stop copying what still fits.
    """
    cfg = cfg or load_config()
    catalogue = catalogue or Catalogue()
    root = root or config.data_dir()

    destination = cfg.archive_path
    if destination is None:
        return ArchiveResult([], [], "archiving is off — no archive_dir in storage.yaml")
    if not destination.exists():
        # An unmounted NAS looks exactly like an empty directory that we would
        # happily "archive" into and then delete originals against. Refuse.
        return ArchiveResult(
            [], [], f"the archive at {destination} is not there — is the disk mounted?"
        )

    result = ArchiveResult([], [])
    for recording in catalogue.list_unarchived(limit=limit):
        source = root / recording.path
        if not source.exists():
            result.failed.append((recording.id, "the clip is missing from this disk"))
            continue
        try:
            target = destination / recording.path
            _copy_verified(source, target)
            # The sidecar travels with the clip. A clip without one is not a
            # dataset sample, it is a video file.
            side = sidecar_for(source)
            if side.exists():
                _copy_verified(side, sidecar_for(target))
            catalogue.mark_archived(recording.id, str(target))
            result.copied.append(recording.id)
        except (OSError, StorageError) as exc:
            log.warning("could not archive clip %s: %s", recording.id, exc)
            result.failed.append((recording.id, str(exc)))

    if result.copied:
        log.info("archived %d clip(s) to %s", len(result.copied), destination)
    return result


# --------------------------------------------------------------------------
# Deleting, and everything that has to be true first
# --------------------------------------------------------------------------


@dataclass
class PruneResult:
    deleted: list[int]
    freed_bytes: int = 0
    skipped_reason: str = ""

    @property
    def freed_gb(self) -> float:
        return self.freed_bytes / 1_073_741_824

    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        if not self.deleted:
            return "nothing old enough to remove"
        return f"removed {len(self.deleted)} clip(s), freeing {self.freed_gb:.1f} GB"


def prune(
    catalogue: Catalogue | None = None,
    cfg: StorageConfig | None = None,
    *,
    root: Path | None = None,
) -> PruneResult:
    """Delete local clips that are old AND verifiably archived.

    How long is "old" depends on what the footage is of: a repair stays as long
    as it can be disputed under warranty, a packing clip only as long as a
    delivery complaint can arrive. ``keep_days_by_source`` holds the difference
    and ``keep_days`` covers anything not named — including a clip somebody
    started by hand, which belongs to no integration at all.

    Every condition here is a refusal, because this is the only operation in
    RepairCam that destroys footage:

    - archiving must be configured, and deletion explicitly enabled;
    - the clip must be older than its own source's window;
    - the catalogue must say it was archived;
    - **and the archived file must still be there, at the right size, checked
      now** — not trusted from a database row written weeks ago. A NAS that was
      wiped, or a disk swapped out, would otherwise take the shop's only
      remaining copy with it.

    The catalogue row is kept. It records where the footage went, which is the
    only way anyone finds it later.
    """
    cfg = cfg or load_config()
    catalogue = catalogue or Catalogue()
    root = root or config.data_dir()

    if not cfg.archive_dir:
        return PruneResult([], skipped_reason="nothing is deleted while archiving is off")
    if not cfg.delete_after_archive:
        return PruneResult(
            [], skipped_reason="deletion is off — set delete_after_archive in storage.yaml"
        )

    result = PruneResult([])
    now = datetime.now(timezone.utc)

    def cutoff(days: int) -> str:
        return (now - timedelta(days=days)).isoformat()

    # One pass per retention window. Named sources first, then everything else
    # — including clips started by hand, which belong to no integration and so
    # get the default rather than any integration's window.
    named = sorted(cfg.keep_days_by_source)
    groups: list[dict] = [
        {"cutoff_iso": cutoff(cfg.keep_days_for(source)), "source": source}
        for source in named
    ]
    groups.append({"cutoff_iso": cutoff(cfg.keep_days), "exclude_sources": named or None})

    candidates: list[Recording] = []
    for group in groups:
        candidates.extend(catalogue.list_archived_before(**group))

    for recording in candidates:
        source = root / recording.path
        if not source.exists():
            continue  # already gone; the row still says where it went

        archived = Path(recording.archive_path or "")
        if not archived.exists():
            log.warning(
                "clip %s says it was archived to %s, which is not there — keeping the "
                "local copy", recording.id, archived,
            )
            continue
        try:
            if archived.stat().st_size != source.stat().st_size:
                log.warning(
                    "clip %s differs from its archived copy — keeping the local one",
                    recording.id,
                )
                continue
        except OSError as exc:
            log.warning("could not check the archived copy of clip %s: %s", recording.id, exc)
            continue

        freed = source.stat().st_size
        try:
            source.unlink()
            sidecar_for(source).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove clip %s: %s", recording.id, exc)
            continue

        catalogue.mark_local_deleted(recording.id)
        result.deleted.append(recording.id)
        result.freed_bytes += freed

    if result.deleted:
        log.info("pruned %s", result.summary())
    return result


def status(catalogue: Catalogue | None = None, cfg: StorageConfig | None = None) -> dict:
    """Everything the status page and `cli storage` need, in one call."""
    cfg = cfg or load_config()
    catalogue = catalogue or Catalogue()
    report = disk_report(cfg)

    destination = cfg.archive_path
    return {
        "disk": report.as_dict(),
        "config": cfg.describe(),
        "archive_ready": bool(destination and destination.exists()),
        "archive_missing": bool(destination and not destination.exists()),
        "unarchived": catalogue.count_unarchived(),
        "archived": catalogue.count_archived(),
        # Kept clips never expire, so they are the part of the archive that only
        # ever grows. Worth watching, not hiding.
        "kept": catalogue.count_kept(),
    }


# --------------------------------------------------------------------------
# Doing it without being asked
# --------------------------------------------------------------------------


#: How often the worker looks. Archiving is not urgent — a clip that waits ten
#: minutes for its second copy has lost nothing — and copying to a NAS over the
#: shop network should never compete with recording for bandwidth.
DEFAULT_INTERVAL_S = 600.0


class StorageWorker:
    """Copies clips to the archive, and prunes, in the background.

    Started by the web app so the shop does not have to remember. Every failure
    is logged and retried on the next pass: an unmounted NAS, a full archive
    disk and a network blip all look the same from here, and none of them is a
    reason to stop recording or to stop trying.
    """

    def __init__(
        self,
        catalogue: Catalogue | None = None,
        cfg: StorageConfig | None = None,
        *,
        interval: float = DEFAULT_INTERVAL_S,
    ):
        import threading

        self.catalogue = catalogue or Catalogue()
        self.cfg = cfg or load_config()
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_run_at: float = 0.0
        self.last_summary: str = ""
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_once(self) -> str:
        import time as _time

        try:
            archived = archive_pending(self.catalogue, self.cfg)
            pruned = prune(self.catalogue, self.cfg)
            parts = [archived.summary()]
            if pruned.deleted or pruned.skipped_reason == "":
                parts.append(pruned.summary())
            self.last_summary = "; ".join(p for p in parts if p)
            self.last_error = "" if archived.ok else archived.summary()
        except Exception as exc:  # never let this thread die
            self.last_error = str(exc)
            self.last_summary = ""
            log.exception("storage worker failed: %s", exc)
        self.last_run_at = _time.time()
        return self.last_summary

    def start(self) -> None:
        import threading

        if self.running:
            return
        self._thread = threading.Thread(target=self._loop, name="storage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("storage worker: every %.0fs, archiving to %s",
                 self.interval, self.cfg.archive_dir or "(nowhere — archiving is off)")
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval)

    def status(self) -> dict:
        import time as _time

        info = status(self.catalogue, self.cfg)
        info.update({
            "running": self.running,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "seconds_since_run": (
                round(_time.time() - self.last_run_at) if self.last_run_at else None
            ),
        })
        return info
