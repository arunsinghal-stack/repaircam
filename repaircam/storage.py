"""Keeping the disk from filling, and getting a second copy off the box.

A bench-hour of copied 4 Mbps video is roughly **1.8 GB**. Nothing here deleted
or moved anything until this module existed, so the recorder's disk filled and
the first thing anyone learned about it was a recording stopping in the middle
of a repair.

Four pieces, and the order between them is the whole safety argument:

1. **A free-space guard.** Recording is refused *before* it starts when the disk
   is nearly full, because a clip that dies half-written is worse than one that
   was never begun — the technician at least knows about the second.
2. **An archive copy.** Clips are copied to a second disk or a NAS mount and
   verified there. Until that exists, the shop's only copy of every repair is
   on one laptop.
3. **Retention on the recorder, which deletes only what is archived.** Age
   alone is never enough. A clip is removed locally only once a verified copy
   exists elsewhere, and the check is re-run at the moment of deletion.
4. **Retention at the archive**, which is where footage finally ends. Without
   it the archive only ever grows — three benches at eight hours is ~950 GB a
   month arriving and nothing leaving.

**The two retention windows are different things and must not be confused.**
The recorder's (``keep_days_local``) is arithmetic: this laptop holds about
five days of three benches and no setting changes that. The archive's
(``keep_days_by_source``) is the shop's policy: how far back anyone can look.
Putting the policy on the recorder — 30 days on a disk that holds five — fills
it by mid-week, and then the free-space guard refuses Start while retention
reports itself working perfectly.

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

#: How long a clip stays on the RECORDER once it is safely archived.
#:
#: This is not a policy, it is arithmetic. Three benches at eight recorded
#: hours is ~43 GB a day, so a 200 GB laptop holds about five days and no
#: setting can change that. Keeping the shop's real retention window here
#: instead — 30 days, say — would need 1.3 TB, so the disk would simply fill,
#: the free-space guard would refuse Start, and nothing would be old enough to
#: prune. Recording would stop mid-week with the retention window "working".
DEFAULT_KEEP_DAYS_LOCAL = 7

#: How long a clip stays in the ARCHIVE. THIS is the shop's policy: how far
#: back anyone can look. It applies to the last copy of the footage, so when it
#: expires the footage is gone for good.
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
    #: How long the RECORDER keeps its copy after archiving it. One number for
    #: everything: the recorder's limit is how big its disk is, and that does
    #: not vary with what the footage is of.
    keep_days_local: int = DEFAULT_KEEP_DAYS_LOCAL
    #: The ARCHIVE's fallback window, and what a clip started by hand in
    #: RepairCam gets — it belongs to no integration, so no integration's
    #: window applies to it.
    keep_days: int = DEFAULT_KEEP_DAYS
    #: Per-source archive windows: {"repair": 30, "packing": 45}. A source not
    #: named here falls back to keep_days.
    keep_days_by_source: dict = field(default_factory=lambda: dict(DEFAULT_KEEP_DAYS_BY_SOURCE))
    #: Off by default even when a destination is set. Deleting footage is the
    #: one thing here that cannot be undone, so it is opted into explicitly.
    delete_after_archive: bool = False
    #: And this one is opted into separately again, because it is a different
    #: act. delete_after_archive removes a copy; this removes the footage.
    delete_from_archive: bool = False

    @property
    def archive_path(self) -> Path | None:
        return Path(self.archive_dir).expanduser() if self.archive_dir else None

    def keep_days_for(self, source: str) -> int:
        """How long this kind of footage stays IN THE ARCHIVE."""
        return int(self.keep_days_by_source.get(source or "", self.keep_days))

    @property
    def local_outlives_archive(self) -> list[str]:
        """Sources whose archive window is shorter than the recorder's own.

        A contradiction rather than an error — the recorder would be asked to
        hold footage longer than the shop wants it to exist. It resolves
        itself, because the archive pass removes the local copy too, but it
        means somebody typed a number they did not mean, so it is reported.
        """
        windows = {**{"": self.keep_days}, **self.keep_days_by_source}
        return sorted(
            source for source, days in windows.items() if int(days) < self.keep_days_local
        )

    def describe(self) -> dict:
        return {
            "min_free_gb": self.min_free_gb,
            "warn_free_gb": self.warn_free_gb,
            "archive_dir": self.archive_dir or "not set",
            "keep_days_local": self.keep_days_local,
            "keep_days": self.keep_days,
            "keep_days_by_source": dict(self.keep_days_by_source),
            "delete_after_archive": self.delete_after_archive,
            "delete_from_archive": self.delete_from_archive,
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
            keep_days_local=int(values.get("keep_days_local", DEFAULT_KEEP_DAYS_LOCAL)),
            keep_days=int(values.get("keep_days", DEFAULT_KEEP_DAYS)),
            keep_days_by_source=by_source,
            delete_after_archive=bool(values.get("delete_after_archive", False)),
            delete_from_archive=bool(values.get("delete_from_archive", False)),
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


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _under(path: Path, root: Path) -> bool:
    """Is ``path`` inside ``root``? (Path.is_relative_to is 3.9+; this box is 3.8.)"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
    """Delete the RECORDER's copy of clips that are old AND verifiably archived.

    One window for everything — ``keep_days_local``. What the footage is of has
    no bearing on how much disk this laptop has. The shop's real retention
    windows live at the archive, in ``prune_archive``; asking the recorder to
    honour them would fill it long before the first one expired.

    Nothing is lost here: every clip removed has a verified copy elsewhere, and
    its Odoo link keeps working because the clip page falls back to the archive.

    Every condition is a refusal, because this operation destroys files:

    - archiving must be configured, and deletion explicitly enabled;
    - the clip must be older than ``keep_days_local``;
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
    cutoff = _cutoff(cfg.keep_days_local)

    for recording in catalogue.list_archived_before(cutoff):
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


def prune_archive(
    catalogue: Catalogue | None = None,
    cfg: StorageConfig | None = None,
    *,
    root: Path | None = None,
) -> PruneResult:
    """Delete footage from the archive once its retention window has passed.

    This is the end of a clip's life, and the only deletion in RepairCam with
    nothing behind it. Everywhere else "delete" means "remove one of two
    copies"; here it means the footage stops existing. So the refusals are
    stricter, and one of them is new:

    - archiving must be configured **and the archive must be mounted**. An
      absent directory is the shape of an unmounted disk, and an unmounted disk
      has nothing to delete — it only has rows that would get marked deleted
      while the footage sat safely on a disk in a drawer.
    - ``delete_from_archive`` must be on. Deliberately not the same switch as
      ``delete_after_archive``: one frees the laptop, this one ends the record.
    - the clip must be older than **its own source's** window — repair 30 days,
      packing 45, whatever the shop set. Here the per-source windows finally
      mean what the shop thought they meant.
    - it must not be marked keep. Enforced in the SQL, not here.
    - **its archived path must be under the archive configured now.** A row
      written when ``archive_dir`` pointed at a different disk names a file on
      that disk, and deleting a path this config never wrote is how a footgun
      goes off in a machine with two archive drives.

    The local copy goes too if it is still there. The window has expired: it
    would be strange to declare the footage's life over and leave a copy on the
    recorder — and, since ``prune`` only ever deletes clips it can verify at the
    archive, that leftover copy could never be removed by anything again.

    The catalogue row survives its footage, holding the date and the reason.
    """
    cfg = cfg or load_config()
    catalogue = catalogue or Catalogue()
    root = root or config.data_dir()

    destination = cfg.archive_path
    if destination is None:
        return PruneResult([], skipped_reason="the archive is not set up")
    if not cfg.delete_from_archive:
        return PruneResult(
            [],
            skipped_reason=(
                "nothing expires from the archive — set delete_from_archive in storage.yaml"
            ),
        )
    if not destination.exists():
        return PruneResult(
            [], skipped_reason=f"the archive at {destination} is not there — is the disk mounted?"
        )

    allowed = destination.resolve()
    result = PruneResult([])

    # One pass per window. Named sources first, then everything else — which
    # includes clips started by hand in RepairCam, belonging to no integration
    # and so getting the fallback rather than any integration's window.
    named = sorted(cfg.keep_days_by_source)
    groups: list[dict] = [
        {"cutoff_iso": _cutoff(cfg.keep_days_for(source)), "source": source} for source in named
    ]
    groups.append({"cutoff_iso": _cutoff(cfg.keep_days), "exclude_sources": named or None})

    candidates: list[Recording] = []
    for group in groups:
        candidates.extend(catalogue.list_archive_expired(**group))

    for recording in candidates:
        archived = Path(recording.archive_path or "")
        if not recording.archive_path:
            continue
        try:
            resolved = archived.resolve()
        except OSError as exc:
            log.warning("clip %s: cannot resolve %s: %s", recording.id, archived, exc)
            continue
        if not _under(resolved, allowed):
            log.warning(
                "clip %s is archived at %s, which is not under the archive configured now "
                "(%s) — leaving it alone", recording.id, resolved, allowed,
            )
            continue

        freed = 0
        try:
            if resolved.exists():
                freed = resolved.stat().st_size
                resolved.unlink()
            sidecar_for(resolved).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove the archived clip %s: %s", recording.id, exc)
            continue

        # The recorder's copy, if the local window was longer than this one.
        local = root / recording.path
        try:
            if local.exists():
                freed += local.stat().st_size
                local.unlink()
            sidecar_for(local).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("removed the archived clip %s but not the local copy: %s",
                        recording.id, exc)

        catalogue.mark_archive_deleted(recording.id)
        result.deleted.append(recording.id)
        result.freed_bytes += freed

    if result.deleted:
        log.info("archive retention: %s", result.summary())
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
        # Footage that no longer exists. Shown because "the archive is smaller
        # than you expect" should have a number attached to it, not be a
        # discovery.
        "expired": catalogue.count_archive_deleted(),
        # A window somebody typed that contradicts another one.
        "window_conflicts": cfg.local_outlives_archive,
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
        config_path: Path | None = None,
    ):
        import threading

        self.catalogue = catalogue or Catalogue()
        self.config_path = config_path or config_file()
        self.cfg = cfg or load_config(self.config_path)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_run_at: float = 0.0
        self.last_summary: str = ""
        self.last_error: str = ""
        #: Why the last attempt to re-read storage.yaml was refused. The
        #: previous config stays in force — a typo must not switch archiving
        #: off — so this is the only thing that says the file on disk and the
        #: settings in use have diverged.
        self.config_error: str = ""
        self.config_reloaded_at: float = 0.0
        self._config_stamp = self._stamp()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- picking up a changed storage.yaml ----------------------------------

    def _stamp(self) -> tuple | None:
        """Cheap "has the file changed" fingerprint, or None if there isn't one.

        mtime AND size, because an edit that keeps the byte count is ordinary
        (``30`` -> ``45``) and one that keeps the timestamp is not.
        """
        try:
            info = self.config_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def reload_if_changed(self) -> bool:
        """Re-read storage.yaml if it has been edited. Returns True if it moved.

        The worker used to read its config once, at startup, so every change
        needed `systemctl restart repaircam` — and forgetting meant the status
        page reported settings the worker was not using. That is survivable for
        a file edited over SSH by the person who just wrote it. It is not
        survivable for a setting changed from a web panel, where nothing
        appears to happen and the panel looks broken.

        A file that will not parse is refused and the running config is kept:
        the alternative is that one typo silently stops the shop's only backup.
        """
        import time as _time

        stamp = self._stamp()
        if stamp == self._config_stamp:
            return False

        # Recorded even on failure, so a file that stays broken is reported
        # once rather than every ten minutes. Fixing it changes the stamp
        # again, which is what brings it back.
        self._config_stamp = stamp

        try:
            fresh = load_config(self.config_path)
        except StorageError as exc:
            self.config_error = str(exc)
            log.error("storage.yaml was NOT reloaded, keeping the settings in use: %s", exc)
            return False

        self.config_error = ""
        if fresh == self.cfg:
            return False  # touched, not changed

        was, now = self.cfg.describe(), fresh.describe()
        changed = sorted(key for key, value in now.items() if was.get(key) != value)
        log.info(
            "storage.yaml reloaded — %s",
            "; ".join(f"{key}: {was.get(key)} -> {now[key]}" for key in changed) or "no change",
        )
        self.cfg = fresh
        self.config_reloaded_at = _time.time()
        return True

    def apply_config(self, cfg: StorageConfig) -> None:
        """Adopt settings handed straight to the worker.

        For a caller that has just written storage.yaml itself and should not
        have to wait up to ``interval`` for the change to take: it stamps the
        file as already read, so this and ``reload_if_changed`` cannot fight.
        """
        import time as _time

        self.cfg = cfg
        self._config_stamp = self._stamp()
        self.config_error = ""
        self.config_reloaded_at = _time.time()

    def run_once(self) -> str:
        import time as _time

        # Before the work, not after: a pass that archives under the old
        # settings and then notices they changed has done the wrong thing once.
        self.reload_if_changed()

        try:
            archived = archive_pending(self.catalogue, self.cfg)
            pruned = prune(self.catalogue, self.cfg)
            expired = prune_archive(self.catalogue, self.cfg)
            parts = [archived.summary()]
            if pruned.deleted or pruned.skipped_reason == "":
                parts.append(pruned.summary())
            if expired.deleted:
                parts.append(f"archive: {expired.summary()}")
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
            # The file on disk says one thing and the worker is doing another.
            # Nothing else would ever say so.
            "config_error": self.config_error,
            "config_path": str(self.config_path),
        })
        return info
