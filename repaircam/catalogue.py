"""SQLite catalogue of everything RepairCam has recorded.

The video files and their sidecar JSON are the real record — the database is an
index over them, so it can be rebuilt from the sidecars if it is ever lost. That
is why every clip gets a sidecar even though the row here duplicates it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_center   TEXT    NOT NULL,
    camera_name   TEXT    DEFAULT '',
    mo_name       TEXT    DEFAULT '',
    operation     TEXT    DEFAULT '',
    device        TEXT    DEFAULT '',
    imei          TEXT    DEFAULT '',
    technician    TEXT    DEFAULT '',
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    duration_s    REAL,
    segments      INTEGER DEFAULT 1,
    path          TEXT    NOT NULL,
    sidecar_path  TEXT    DEFAULT '',
    size_bytes    INTEGER DEFAULT 0,
    width         INTEGER,
    height        INTEGER,
    frame_rate    REAL,
    video_codec   TEXT    DEFAULT '',
    audio_codec   TEXT    DEFAULT '',
    notes         TEXT    DEFAULT '',
    link_posted   INTEGER DEFAULT 0,
    -- Why a link will never be posted. Set when saar-seva says it has no such
    -- session: retrying that forever achieves nothing and, worse, blocks every
    -- clip queued behind it.
    link_error    TEXT    DEFAULT '',
    -- Where a verified second copy of this clip lives, and when it got there.
    -- Nothing is ever deleted locally without both, AND a fresh check that the
    -- file is still at the other end.
    archived_at   TEXT    DEFAULT '',
    archive_path  TEXT    DEFAULT '',
    -- Set when the local file has been removed. The row stays: it is the only
    -- record of where the footage went.
    local_deleted INTEGER DEFAULT 0,
    -- Set when the ARCHIVE copy has been removed too, at the end of the
    -- retention window. That is the one deletion with nothing behind it, so it
    -- is recorded plainly: the row is what lets the library say "deleted on
    -- this date under the 30-day policy" instead of "missing from disk", which
    -- would send somebody hunting for footage that no longer exists.
    archive_deleted    INTEGER DEFAULT 0,
    archive_deleted_at TEXT    DEFAULT '',
    -- "Keep this one." A training example, a disputed repair, a dataset sample.
    -- Retention is by age, and age knows nothing about which clips matter, so
    -- without this the important ones expire exactly like the routine ones.
    keep          INTEGER DEFAULT 0,
    -- Which integration asked for this clip: '' (started by hand), 'repair'
    -- or 'packing'. source_ref is that system's own id for the session.
    -- Kept in the database, not just in memory, so a clip whose link has not
    -- been posted yet still knows where to post it after a restart.
    source        TEXT    DEFAULT '',
    source_ref    TEXT    DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recordings_wc      ON recordings(work_center);
CREATE INDEX IF NOT EXISTS idx_recordings_mo      ON recordings(mo_name);
CREATE INDEX IF NOT EXISTS idx_recordings_imei    ON recordings(imei);
CREATE INDEX IF NOT EXISTS idx_recordings_started ON recordings(started_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    work_center  TEXT DEFAULT '',
    kind         TEXT NOT NULL,
    detail       TEXT DEFAULT '',
    recording_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
"""


def utcnow() -> str:
    """Timestamps are stored as UTC ISO-8601 so clips from different days and
    machines sort correctly."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobLabels:
    """What a clip is *about* — the labels that make the dataset useful.

    These come from Odoo via saar-seva (Phase 5) or are typed by hand in the web
    UI today.
    """

    mo_name: str = ""  # Odoo Manufacturing Order, e.g. WH/MO/00042
    operation: str = ""  # e.g. "Screen replacement"
    device: str = ""  # e.g. "Redmi Note 12"
    imei: str = ""
    technician: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def is_empty(self) -> bool:
        return not any(self.as_dict().values())


@dataclass
class Recording:
    """One row of the catalogue — one finished clip for one operation."""

    work_center: str
    path: str
    started_at: str
    id: int | None = None
    camera_name: str = ""
    ended_at: str | None = None
    duration_s: float | None = None
    segments: int = 1
    sidecar_path: str = ""
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    video_codec: str = ""
    audio_codec: str = ""
    link_posted: int = 0
    #: Which integration asked for this clip ('', 'repair', 'packing') and that
    #: system's own id for the session, so the link can be posted back to the
    #: right place even after a restart.
    source: str = ""
    source_ref: str = ""
    link_error: str = ""
    archived_at: str = ""
    archive_path: str = ""
    local_deleted: int = 0
    #: The archive copy is gone too — the footage no longer exists anywhere.
    archive_deleted: int = 0
    archive_deleted_at: str = ""
    #: Never delete this clip, locally or at the archive, whatever its age.
    keep: int = 0
    created_at: str = field(default_factory=utcnow)
    labels: JobLabels = field(default_factory=JobLabels)

    @property
    def duration_hms(self) -> str:
        total = int(self.duration_s or 0)
        return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1_048_576, 1)

    @property
    def title(self) -> str:
        """Best human label available, for lists and page titles."""
        bits = [b for b in (self.labels.mo_name, self.labels.operation) if b]
        return " — ".join(bits) if bits else Path(self.path).stem

    def absolute_path(self, root: Path | None = None) -> Path:
        """Recording paths are stored relative to the data dir, so moving the
        archive to another disk does not invalidate every row."""
        root = root or config.data_dir()
        return root / self.path


class Catalogue:
    """The SQLite index. Safe to open per request; one file, no server."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        # WAL lets the web UI read while a recording is being written.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Additive migrations for databases made by an earlier version.
            # SQLite has no ADD COLUMN IF NOT EXISTS, so ask first.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(recordings)")}
            for column, ddl in (
                ("source", "ALTER TABLE recordings ADD COLUMN source TEXT DEFAULT ''"),
                ("source_ref", "ALTER TABLE recordings ADD COLUMN source_ref TEXT DEFAULT ''"),
                ("link_error", "ALTER TABLE recordings ADD COLUMN link_error TEXT DEFAULT ''"),
                ("archived_at", "ALTER TABLE recordings ADD COLUMN archived_at TEXT DEFAULT ''"),
                ("archive_path", "ALTER TABLE recordings ADD COLUMN archive_path TEXT DEFAULT ''"),
                ("local_deleted", "ALTER TABLE recordings ADD COLUMN local_deleted INTEGER DEFAULT 0"),
                ("keep", "ALTER TABLE recordings ADD COLUMN keep INTEGER DEFAULT 0"),
                ("archive_deleted",
                 "ALTER TABLE recordings ADD COLUMN archive_deleted INTEGER DEFAULT 0"),
                ("archive_deleted_at",
                 "ALTER TABLE recordings ADD COLUMN archive_deleted_at TEXT DEFAULT ''"),
            ):
                if column not in existing:
                    conn.execute(ddl)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _to_recording(row: sqlite3.Row) -> Recording:
        return Recording(
            id=row["id"],
            work_center=row["work_center"],
            camera_name=row["camera_name"],
            path=row["path"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            duration_s=row["duration_s"],
            segments=row["segments"],
            sidecar_path=row["sidecar_path"],
            size_bytes=row["size_bytes"],
            width=row["width"],
            height=row["height"],
            frame_rate=row["frame_rate"],
            video_codec=row["video_codec"],
            audio_codec=row["audio_codec"],
            link_posted=row["link_posted"],
            source=(row["source"] if "source" in row.keys() else "") or "",
            source_ref=(row["source_ref"] if "source_ref" in row.keys() else "") or "",
            link_error=(row["link_error"] if "link_error" in row.keys() else "") or "",
            archived_at=(row["archived_at"] if "archived_at" in row.keys() else "") or "",
            archive_path=(row["archive_path"] if "archive_path" in row.keys() else "") or "",
            local_deleted=int(row["local_deleted"] if "local_deleted" in row.keys() else 0) or 0,
            archive_deleted=int(
                row["archive_deleted"] if "archive_deleted" in row.keys() else 0
            ) or 0,
            archive_deleted_at=(
                row["archive_deleted_at"] if "archive_deleted_at" in row.keys() else ""
            ) or "",
            keep=int(row["keep"] if "keep" in row.keys() else 0) or 0,
            created_at=row["created_at"],
            labels=JobLabels(
                mo_name=row["mo_name"],
                operation=row["operation"],
                device=row["device"],
                imei=row["imei"],
                technician=row["technician"],
                notes=row["notes"],
            ),
        )

    # -- writes -------------------------------------------------------------

    def add(self, recording: Recording) -> Recording:
        """Insert a finished clip and return it with its assigned id."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO recordings (
                    work_center, camera_name, mo_name, operation, device, imei,
                    technician, started_at, ended_at, duration_s, segments, path,
                    sidecar_path, size_bytes, width, height, frame_rate,
                    video_codec, audio_codec, notes, link_posted, source,
                    source_ref, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    recording.work_center,
                    recording.camera_name,
                    recording.labels.mo_name,
                    recording.labels.operation,
                    recording.labels.device,
                    recording.labels.imei,
                    recording.labels.technician,
                    recording.started_at,
                    recording.ended_at,
                    recording.duration_s,
                    recording.segments,
                    recording.path,
                    recording.sidecar_path,
                    recording.size_bytes,
                    recording.width,
                    recording.height,
                    recording.frame_rate,
                    recording.video_codec,
                    recording.audio_codec,
                    recording.labels.notes,
                    recording.link_posted,
                    recording.source,
                    recording.source_ref,
                    recording.created_at,
                ),
            )
            recording.id = cursor.lastrowid
        return recording

    def update_labels(self, recording_id: int, labels: JobLabels) -> None:
        """Re-label a clip — used when a technician recorded first and tagged
        the job afterwards."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE recordings
                   SET mo_name=?, operation=?, device=?, imei=?, technician=?, notes=?
                 WHERE id=?
                """,
                (
                    labels.mo_name,
                    labels.operation,
                    labels.device,
                    labels.imei,
                    labels.technician,
                    labels.notes,
                    recording_id,
                ),
            )

    def set_source(self, recording_id: int, source: str, source_ref: str) -> None:
        """Record which integration a clip came from, and its id there.

        Written when the clip is filed rather than kept in memory, so a restart
        before the link is posted does not lose where it should go.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET source=?, source_ref=? WHERE id=?",
                (source, source_ref, recording_id),
            )

    def mark_link_posted(self, recording_id: int) -> None:
        """Phase 5: the clip's link has been written to the Odoo MO chatter."""
        with self.connect() as conn:
            conn.execute("UPDATE recordings SET link_posted=1 WHERE id=?", (recording_id,))

    def delete(self, recording_id: int) -> Recording | None:
        """Remove a row. The caller decides what to do with the files."""
        recording = self.get(recording_id)
        if recording is None:
            return None
        with self.connect() as conn:
            conn.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
        return recording

    def log_event(
        self, kind: str, *, work_center: str = "", detail: str = "", recording_id: int | None = None
    ) -> None:
        """Append to the audit trail — start/stop/done/errors.

        Accountability is the point of the system, so the trail is kept even for
        sessions that never produced a usable clip.
        """
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, work_center, kind, detail, recording_id) VALUES (?,?,?,?,?)",
                (utcnow(), work_center, kind, detail, recording_id),
            )

    # -- reads --------------------------------------------------------------

    def get(self, recording_id: int) -> Recording | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
        return self._to_recording(row) if row else None

    def list(
        self,
        *,
        work_center: str | None = None,
        mo_name: str | None = None,
        imei: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Recording]:
        clauses: list[str] = []
        params: list[Any] = []
        if work_center:
            clauses.append("work_center = ?")
            params.append(work_center)
        if mo_name:
            clauses.append("mo_name = ?")
            params.append(mo_name)
        if imei:
            clauses.append("imei = ?")
            params.append(imei)
        if search:
            clauses.append(
                "(mo_name LIKE ? OR operation LIKE ? OR device LIKE ? OR imei LIKE ?"
                " OR technician LIKE ? OR notes LIKE ?)"
            )
            params.extend([f"%{search}%"] * 6)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT * FROM recordings {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        with self.connect() as conn:
            rows = conn.execute(query, [*params, limit, offset]).fetchall()
        return [self._to_recording(row) for row in rows]

    def list_unposted(self, limit: int = 10) -> list[Recording]:
        """Clips whose link has not reached Odoo yet, and still could.

        Only clips that came from an integration qualify — ``source`` and
        ``source_ref`` both set. saar-seva matches a clip to a job by ITS OWN
        session id, so a clip a technician started by hand in RepairCam has
        nothing for it to match, however carefully the MO number was typed into
        our form. Queueing those guaranteed a 404 on every poll, forever.

        Clips that already failed permanently are excluded too, so one of them
        cannot sit at the head of the queue and starve the rest.

        Driving retries from here rather than from memory means a clip finished
        just before a restart is still posted afterwards.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE link_posted = 0"
                "   AND link_error = ''"
                "   AND source != '' AND source_ref != ''"
                " ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_recording(row) for row in rows]

    def mark_link_failed(self, recording_id: int, reason: str) -> None:
        """Stop retrying a clip saar-seva will never accept, and say why.

        The clip and its footage are untouched — only the automatic link post
        gives up. The status page surfaces these so they are not lost silently.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET link_error=? WHERE id=?", (reason[:300], recording_id)
            )

    def list_link_failures(self, limit: int = 50) -> list[Recording]:
        """Clips whose link could not be posted and will not be retried."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE link_error != '' AND link_posted = 0"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_recording(row) for row in rows]

    # -- archiving ----------------------------------------------------------

    def list_unarchived(self, limit: int = 20) -> list[Recording]:
        """Clips with no verified second copy yet, oldest first.

        Oldest first because those are the ones closest to being deleted, and
        deleting something that was never copied is the failure this whole
        column exists to prevent.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE archived_at = '' AND local_deleted = 0"
                " ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_recording(row) for row in rows]

    def list_archived_before(
        self,
        cutoff_iso: str,
        *,
        source: str | None = None,
        exclude_sources: list[str] | None = None,
        limit: int = 200,
    ) -> list[Recording]:
        """Archived clips whose recording started before ``cutoff_iso``.

        ``source`` narrows to one integration, ``exclude_sources`` to everything
        else. Retention differs by what the footage is of — a packing dispute
        and a repair warranty are not the same length — so the caller asks for
        one group at a time rather than applying one age to everything.
        """
        sql = [
            # keep = 0 is part of the query, not the caller's job: a clip
            # somebody marked worth keeping must not depend on every future
            # caller remembering to filter it out.
            "SELECT * FROM recordings",
            " WHERE archived_at != '' AND local_deleted = 0 AND keep = 0",
            "  AND started_at < ?",
        ]
        params: list = [cutoff_iso]
        if source is not None:
            sql.append("  AND source = ?")
            params.append(source)
        if exclude_sources:
            marks = ",".join("?" for _ in exclude_sources)
            sql.append(f"  AND source NOT IN ({marks})")
            params.extend(exclude_sources)
        sql.append(" ORDER BY id ASC LIMIT ?")
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute("\n".join(sql), params).fetchall()
        return [self._to_recording(row) for row in rows]

    def list_archive_expired(
        self,
        cutoff_iso: str,
        *,
        source: str | None = None,
        exclude_sources: list[str] | None = None,
        limit: int = 200,
    ) -> list[Recording]:
        """Archived clips past the end of their retention window.

        The same shape as ``list_archived_before``, and deliberately a separate
        method, because it answers a much heavier question: not "may the
        recorder's copy go" but "may the LAST copy go". So it asks for more —
        the clip must have reached the archive, must not already have been
        removed from it, and must not be marked keep. Nothing here depends on
        whether the local copy survives; that is the caller's business.
        """
        sql = [
            "SELECT * FROM recordings",
            # keep = 0 lives in the SQL, not in the caller, for the same reason
            # as in list_archived_before — only more so. This is the delete that
            # cannot be undone by fetching the file from somewhere else.
            " WHERE archived_at != '' AND archive_deleted = 0 AND keep = 0",
            "   AND started_at < ?",
        ]
        params: list = [cutoff_iso]
        if source is not None:
            sql.append("   AND source = ?")
            params.append(source)
        if exclude_sources:
            marks = ",".join("?" for _ in exclude_sources)
            sql.append(f"   AND source NOT IN ({marks})")
            params.extend(exclude_sources)
        sql.append(" ORDER BY id ASC LIMIT ?")
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute("\n".join(sql), params).fetchall()
        return [self._to_recording(row) for row in rows]

    def mark_archive_deleted(self, recording_id: int) -> None:
        """The footage is gone from the world, and the date it went.

        The row survives its own footage on purpose. An Odoo chatter link from
        eight months ago still resolves to this row, and "removed on 12 March
        under the 30-day policy" is an answer; a dead link is not.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET archive_deleted=1, archive_deleted_at=?, "
                "local_deleted=1 WHERE id=?",
                (utcnow(), recording_id),
            )

    def count_archive_deleted(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE archive_deleted = 1"
            ).fetchone()["n"])

    def mark_archived(self, recording_id: int, archive_path: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET archived_at=?, archive_path=? WHERE id=?",
                (utcnow(), archive_path, recording_id),
            )

    def set_keep(self, recording_id: int, keep: bool = True) -> None:
        """Mark a clip as one to keep, or let it go back to expiring by age."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET keep=? WHERE id=?", (1 if keep else 0, recording_id)
            )

    def count_kept(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE keep = 1"
            ).fetchone()["n"])

    def mark_local_deleted(self, recording_id: int) -> None:
        """The footage is gone from this disk, not from the world."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE recordings SET local_deleted=1 WHERE id=?", (recording_id,)
            )

    def count_unarchived(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM recordings "
                "WHERE archived_at = '' AND local_deleted = 0"
            ).fetchone()["n"])

    def count_archived(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE archived_at != ''"
            ).fetchone()["n"])

    # -- small persistent settings -----------------------------------------
    #
    # Only for things that must survive a restart but are not worth a file of
    # their own — today, the camera-list revision this box has applied, so a
    # restart does not re-sync a config that has not changed.

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()["n"])

    def stats(self) -> dict[str, Any]:
        """Numbers for the dashboard."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)             AS clips,
                       COALESCE(SUM(duration_s), 0) AS seconds,
                       COALESCE(SUM(size_bytes), 0) AS bytes,
                       SUM(CASE WHEN mo_name = '' THEN 1 ELSE 0 END) AS unlabelled
                  FROM recordings
                """
            ).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE substr(started_at,1,10) = ?",
                (datetime.now(timezone.utc).date().isoformat(),),
            ).fetchone()
        return {
            "clips": row["clips"] or 0,
            "hours": round((row["seconds"] or 0) / 3600, 1),
            "gigabytes": round((row["bytes"] or 0) / 1_073_741_824, 2),
            "unlabelled": row["unlabelled"] or 0,
            "today": today["n"] or 0,
        }

    def recent_events(self, limit: int = 50, work_center: str | None = None) -> list[dict]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if work_center:
            query += " WHERE work_center = ?"
            params.append(work_center)
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]


# --------------------------------------------------------------------------
# Sidecars
# --------------------------------------------------------------------------


def sidecar_for(video_path: Path) -> Path:
    """``clip.mp4`` -> ``clip.json``."""
    return video_path.with_suffix(".json")


def write_sidecar(video_path: Path, payload: dict) -> Path:
    """Write the JSON that makes a clip self-describing.

    A clip plus its sidecar is a complete dataset sample: no database, no Odoo
    lookup needed to know what the footage shows.
    """
    path = sidecar_for(video_path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return path


def read_sidecar(video_path: Path) -> dict:
    path = sidecar_for(video_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
