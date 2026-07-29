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

SCHEMA_VERSION = 3

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
