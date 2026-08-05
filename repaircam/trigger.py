"""The auto-trigger: start and stop recording from what saar-seva reports.

Today a technician presses Start twice — once in saar-seva for time tracking,
once in RepairCam for video. This removes the second one by polling saar-seva and
driving the recorders to match.

**The trigger is a convenience, never a dependency.** Render sleeps, home internet
drops, tokens expire. None of that may stop the shop recording, so every failure
here is logged and skipped, and the web UI keeps working exactly as before.

Switched off until saarseva.yaml exists and saar-seva has REPAIRCAM_API_KEY set.
See docs/PHASE5-CONTRACT.md.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from . import camerasync, storagesync
from . import config as camera_config
from .catalogue import Catalogue, Recording, utcnow
from .recorder import RecorderError, RecorderPool, State
from .saarseva import (
    KIND_PACKING,
    KIND_REPAIR,
    ActiveOperation,
    SaarSevaClient,
    SaarSevaConfig,
    SaarSevaError,
)

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    """What one poll did. Returned for tests, the CLI and the status page."""

    ok: bool = True
    error: str = ""
    active: int = 0
    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    links_posted: list[int] = field(default_factory=list)
    #: Clips whose link saar-seva refused for good. Not a transient failure —
    #: nothing will retry these, so they have to be visible.
    links_failed: list[int] = field(default_factory=list)
    #: Kinds of work whose poll did not answer this tick. Their benches were
    #: deliberately left alone rather than treated as finished.
    partial: list[str] = field(default_factory=list)
    #: Set when the central camera list was applied on this tick.
    camera_sync: str = ""
    #: Set when the central retention policy was applied on this tick.
    storage_sync: str = ""
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.started or self.finished or self.links_posted
            or self.links_failed or self.camera_sync or self.storage_sync
        )

    def summary(self) -> str:
        if not self.ok:
            return f"poll failed: {self.error}"
        bits = [f"{self.active} active"]
        for label, items in (
            ("started", self.started),
            ("finished", self.finished),
            ("skipped", self.skipped),
        ):
            if items:
                bits.append(f"{label}: {', '.join(items)}")
        if self.links_posted:
            bits.append(f"links posted: {len(self.links_posted)}")
        if self.links_failed:
            bits.append(f"links GIVEN UP ON: {len(self.links_failed)}")
        if self.partial:
            bits.append(f"NO ANSWER for {', '.join(self.partial)} — those benches left alone")
        if self.camera_sync:
            bits.append(f"cameras {self.camera_sync}")
        if self.storage_sync:
            bits.append(f"storage {self.storage_sync}")
        return "; ".join(bits)


class Trigger:
    """Keeps the recorders in step with what saar-seva says is running."""

    def __init__(
        self,
        pool: RecorderPool,
        client: SaarSevaClient,
        *,
        catalogue: Catalogue | None = None,
        config: SaarSevaConfig | None = None,
        storage_worker=None,
    ):
        self.pool = pool
        self.client = client
        self.config = config or client.config
        self.catalogue = catalogue or pool.catalogue
        #: The background storage worker, when the web app started one. A
        #: policy arriving from saar-seva is handed straight to it: otherwise
        #: the change would sit unread for up to ten minutes and the status
        #: page would show settings nobody had asked for any more.
        self.storage_worker = storage_worker

        # Benches this trigger started, and which operation each is recording.
        # Only these are ever stopped automatically — see _is_ours.
        self._owned: dict[str, str] = {}
        # The operation currently being recorded on each owned bench...
        self._operation_by_bench: dict[str, ActiveOperation] = {}
        # ...and, once filed, the operation behind a clip, so its work order id
        # can go with the link when it is posted.
        self._operations: dict[int, ActiveOperation] = {}
        # Odoo work-centre id -> bench code. saar-seva talks in Odoo ids;
        # everything else here talks in bench codes.
        self._bench_by_workcenter: dict[int, str] = {}
        #: Benches fully configured for auto-recording that saarseva.yaml's
        #: work_centers list is nonetheless excluding. Reported, because the
        #: bench looks perfectly set up from every other angle.
        self.vetoed_benches: list[str] = []
        self.reload_benches()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: TickResult | None = None
        self.last_tick_at: float = 0.0
        self.last_sync: camerasync.SyncResult | None = None
        self.last_sync_error: str = ""
        self.last_storage_sync: storagesync.SyncResult | None = None
        self.last_storage_error: str = ""
        self.last_heartbeat_at: float = 0.0
        self.last_heartbeat_error: str = ""

    def reload_benches(self) -> dict[int, str]:
        """Rebuild the work-centre -> bench map from cameras.yaml.

        Benches with no ``odoo_workcenter_id`` are simply absent, which is how a
        camera opts out of being auto-triggered.
        """
        mapping: dict[int, str] = {}
        try:
            cameras = camera_config.load_cameras()
        except Exception as exc:  # a bad cameras.yaml must not kill the loop
            log.warning("could not read cameras.yaml for the trigger: %s", exc)
            return self._bench_by_workcenter

        allowed = set(self.config.work_centers)
        vetoed: list[str] = []
        for work_center, camera in cameras.items():
            if camera.odoo_workcenter_id is None:
                continue
            if allowed and work_center not in allowed:
                # The bench has a camera and an Odoo id — everything it needs —
                # and saarseva.yaml is the only reason it will not be filmed.
                # With the camera list maintained centrally this is a trap:
                # adding a bench in the admin panel is supposed to be enough,
                # and a stale local allow-list silently vetoes it.
                vetoed.append(work_center)
                continue
            mapping[camera.odoo_workcenter_id] = work_center

        if vetoed and vetoed != self.vetoed_benches:
            log.warning(
                "%s %s a camera and an Odoo work centre but %s NOT in work_centers "
                "in saarseva.yaml, so %s will never auto-record. Empty that list to "
                "allow every configured bench.",
                ", ".join(sorted(vetoed)),
                "has" if len(vetoed) == 1 else "have",
                "is" if len(vetoed) == 1 else "are",
                "it" if len(vetoed) == 1 else "they",
            )
        self.vetoed_benches = sorted(vetoed)

        if not mapping:
            log.warning(
                "no bench in cameras.yaml has an odoo_workcenter_id, so nothing "
                "can be auto-triggered"
            )
        self._bench_by_workcenter = mapping
        return mapping

    @property
    def workcenter_ids(self) -> list[int]:
        return sorted(self._bench_by_workcenter)

    # -- one poll -----------------------------------------------------------

    def tick(self) -> TickResult:
        """Poll once and make the recorders match. Never raises."""
        result = TickResult()
        operations, answered, error = self._fetch_all()

        if not answered:
            # Nothing answered, so we have no picture at all. Crucially, do not
            # touch any recorder: a failed poll read as "nothing is running"
            # would end every recording in the shop the moment the internet
            # hiccups.
            result.ok = False
            result.error = error
            log.warning("saar-seva poll failed, leaving recorders alone: %s", error)
            self._remember(result)
            return result

        result.partial = sorted({KIND_REPAIR, KIND_PACKING} - answered)
        result.error = error
        wanted = self._by_bench(operations)
        result.active = len(wanted)

        with self._lock:
            for work_center, operation in wanted.items():
                self._reconcile_start(work_center, operation, result)
            for work_center in list(self._owned):
                if work_center in wanted:
                    continue
                # Only end a bench whose OWN kind of work we heard about. If the
                # packing poll failed, a packing bench being absent from the
                # answer means nothing — ending it there would cut a clip in
                # half over an unrelated outage.
                kind = self._kind_of(work_center)
                if kind not in answered:
                    log.debug(
                        "%s left alone: the %s poll did not answer", work_center, kind
                    )
                    continue
                self._finish(work_center, result, reason="no longer active")

        # Before the links, and before the heartbeat, so a bench added centrally
        # can start being filmed on this very tick.
        self._sync_cameras(result)
        self._sync_storage(result)

        self._post_pending_links(result)
        # After reconciling, so what is reported is the state the benches are
        # actually in. Not sent when the poll failed: saar-seva is the same
        # server, and its screen going "unknown" is the honest answer when
        # RepairCam cannot reach it.
        self._send_heartbeat()
        self._remember(result)
        if result.changed:
            log.info("trigger: %s", result.summary())
        return result

    def _fetch_all(self) -> list[ActiveOperation]:
        """What saar-seva says is being recorded, across both integrations.

        Repair benches and packing benches are both Odoo work centres, so they
        share one id space and one camera map.

        Returns ``(operations, answered, error)`` — ``answered`` being the kinds
        whose poll actually succeeded. A partial picture must never be mistaken
        for "nothing is running", so the caller may only act on benches whose
        own kind is in ``answered``. Reporting that per kind, rather than
        failing the whole tick, is what stops a broken packing endpoint from
        delaying every repair recording in the shop.
        """
        benches = self.workcenter_ids
        operations: list[ActiveOperation] = []
        answered: set[str] = set()
        errors: list[str] = []

        for kind, fetch in (
            (KIND_REPAIR, self.client.fetch_active),
            (KIND_PACKING, self.client.fetch_active_packing),
        ):
            try:
                operations += list(fetch(benches))
            except SaarSevaError as exc:
                if kind == KIND_PACKING and exc.status == 404:
                    # A saar-seva without the packing endpoints. A deployment
                    # state, not a fault, and not worth reporting every 5s.
                    log.debug("packing endpoints not deployed yet: %s", exc)
                    answered.add(kind)
                    continue
                log.warning("%s poll failed: %s", kind, exc)
                errors.append(f"{kind}: {exc}")
                continue
            answered.add(kind)

        return operations, answered, "; ".join(errors)

    def _kind_of(self, work_center: str) -> str:
        """Which integration owns this bench's current recording."""
        operation = self._operation_by_bench.get(work_center)
        if operation is not None:
            return operation.kind
        # Fall back to the ownership key, which is "<kind>:<session id>".
        return (self._owned.get(work_center, "") or "").split(":", 1)[0] or KIND_REPAIR

    def _by_bench(self, operations: list[ActiveOperation]) -> dict[str, ActiveOperation]:
        """One operation per bench, keyed by bench code.

        Operations on work centres with no camera are dropped: saar-seva knows
        about every work centre in the shop, RepairCam only about the ones with
        a camera pointed at them.
        """
        wanted: dict[str, ActiveOperation] = {}
        for operation in operations:
            work_center = self._bench_by_workcenter.get(operation.workcenter_id or -1)
            if work_center is None:
                continue
            if work_center in wanted:
                # Two things claiming one bench — two technicians, or a repair
                # timer and a packing record on the same work centre. One
                # camera cannot film two jobs, so keep the first and say so.
                log.warning(
                    "saar-seva reports two sessions on %s; recording only %s",
                    work_center,
                    wanted[work_center].key,
                )
                continue
            wanted[work_center] = operation
        return wanted

    def _reconcile_start(
        self, work_center: str, operation: ActiveOperation, result: TickResult
    ) -> None:
        current = self._owned.get(work_center)
        if current == operation.key:
            return  # already recording the right thing

        if current:
            # The technician moved to a different operation without the previous
            # one disappearing first. File what we have, then start the new one.
            self._finish(work_center, result, reason="operation changed")

        try:
            recorder = self.pool.get(work_center)
        except Exception as exc:
            result.skipped.append(f"{work_center} (no camera: {exc})")
            return

        if recorder.state is not State.IDLE:
            # Someone is already recording here by hand. Leave it completely
            # alone — taking over would cut their clip in half.
            result.skipped.append(f"{work_center} (already recording by hand)")
            return

        try:
            recorder.start(operation.labels())
        except RecorderError as exc:
            log.error("trigger could not start %s: %s", work_center, exc)
            result.skipped.append(f"{work_center} ({exc})")
            return

        self._owned[work_center] = operation.key
        self._operation_by_bench[work_center] = operation
        result.started.append(work_center)

    def _finish(self, work_center: str, result: TickResult, *, reason: str) -> None:
        """Press Done on a bench this trigger started."""
        operation = self._operation_by_bench.pop(work_center, None)
        self._owned.pop(work_center, None)

        try:
            recorder = self.pool.get(work_center)
            recording = recorder.done()
        except RecorderError as exc:
            # Nothing usable, or the join failed. The footage is not lost — the
            # segments stay on disk for `cli recover`.
            log.warning("trigger could not finish %s (%s): %s", work_center, reason, exc)
            result.skipped.append(f"{work_center} ({exc})")
            return
        except Exception as exc:
            log.error("unexpected error finishing %s: %s", work_center, exc)
            result.skipped.append(f"{work_center} ({exc})")
            return

        if recording.id is not None:
            if operation:
                self._operations[recording.id] = operation
                # Stored on the row, not just here, so a restart before the
                # link is posted still knows where it belongs.
                self.catalogue.set_source(recording.id, operation.kind, operation.source_ref)
        result.finished.append(work_center)

    # -- links --------------------------------------------------------------

    def _post_pending_links(self, result: TickResult) -> None:
        """Send links for clips whose chatter line has not been written yet.

        Driven from the catalogue rather than from memory, so a clip whose post
        failed — or one finished before a restart — is retried later instead of
        being forgotten.
        """
        if not self.config.link_base:
            return

        for recording in self.catalogue.list_unposted(limit=10):
            try:
                if recording.source == KIND_PACKING:
                    # saar-seva works out which Delivery Order this belongs on.
                    self.client.post_packing_recording(recording)
                else:
                    self.client.post_recording(
                        recording, operation=self._operations.get(recording.id)
                    )
            except SaarSevaError as exc:
                if exc.permanent:
                    # saar-seva has no such session and never will. Retrying is
                    # pointless, and leaving it at the head of the queue would
                    # starve every clip behind it — which is how one stale clip
                    # silently stops the whole shop's links from being posted.
                    log.error(
                        "giving up on the link for clip %s: %s", recording.id, exc
                    )
                    self.catalogue.mark_link_failed(recording.id, str(exc))
                    result.links_failed.append(recording.id)
                    continue
                log.warning("could not post the link for clip %s: %s", recording.id, exc)
                if exc.status is None:
                    return  # saar-seva is unreachable; the rest will fail too
                continue  # this clip is not ready (e.g. no delivery order yet)

            self.catalogue.mark_link_posted(recording.id)
            self._operations.pop(recording.id, None)
            result.links_posted.append(recording.id)

    #: Catalogue key holding the camera-list revision this box has applied, so
    #: a restart does not re-sync a config that has not changed.
    REVISION_KEY = "camera_config_revision"

    #: Catalogue key holding the last sync that DELETED benches. A removal is
    #: not an address change — the bench stops existing — so it outlives the
    #: log line that reported it.
    REMOVED_KEY = "camera_config_removed"

    @property
    def applied_revision(self) -> int | None:
        raw = self.catalogue.get_setting(self.REVISION_KEY, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def removed_benches(self) -> dict:
        """The last sync that deleted benches, until it is acknowledged."""
        raw = self.catalogue.get_setting(self.REMOVED_KEY, "")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return payload if payload.get("benches") else {}

    #: Catalogue key holding the storage-policy revision this box has applied.
    STORAGE_REVISION_KEY = "storage_config_revision"

    @property
    def applied_storage_revision(self) -> int | None:
        raw = self.catalogue.get_setting(self.STORAGE_REVISION_KEY, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _sync_storage(self, result: TickResult) -> None:
        """Fetch and apply the central retention policy, if it has changed.

        Same shape and the same rules as the camera sync, and free in steady
        state for the same reason: the revision rides the poll that already
        happened, so this returns immediately unless it moved.

        Two things it will not do. It never touches this recorder's own
        settings — where the archive is, how many days this disk holds, whether
        anything is deleted — and a policy naming one of them is refused whole.
        And it never deletes: a shortened window is written to the file but
        HELD by storage.note_window_changes, which makes the archive pass
        refuse to run until a person at the shop accepts the cost. A number
        typed on a web form cannot end footage on its own.
        """
        seen = getattr(self.client, "last_storage_revision", None)
        if seen is None:
            return  # a saar-seva too old to send one; nothing to do
        if not seen:
            # Revision 0: nobody has ever saved a policy. Applying defaults
            # here would silently replace whatever this shop chose for itself.
            return
        if seen == self.applied_storage_revision:
            return

        try:
            payload = self.client.fetch_storage_config()
        except SaarSevaError as exc:
            if exc.status == 404:
                log.debug("saar-seva has no central storage policy yet")
                return
            self.last_storage_error = str(exc)
            log.warning("could not fetch the storage policy: %s", exc)
            return

        try:
            sync = storagesync.apply(payload)
        except storagesync.StorageSyncError as exc:
            self.last_storage_error = str(exc)
            log.error("refused the storage policy: %s", exc)
            return
        except OSError as exc:
            self.last_storage_error = f"could not write storage.yaml: {exc}"
            log.error("%s", self.last_storage_error)
            return

        self.last_storage_error = ""
        self.last_storage_sync = sync
        result.storage_sync = sync.summary()
        self.catalogue.set_setting(self.STORAGE_REVISION_KEY, str(sync.revision))

        if sync.changed:
            self.catalogue.log_event("storage-sync", detail=sync.summary())
            # Hand it over rather than let the worker find it in up to ten
            # minutes: this is also what stages a shortened window, and a
            # warning that arrives ten minutes late is a warning about
            # something that has already happened.
            worker = self.storage_worker
            if worker is not None:
                try:
                    from . import storage

                    worker.apply_config(storage.load_config())
                except Exception as exc:  # never let a config read kill the poll
                    log.warning("storage policy written but not adopted yet: %s", exc)

    def _sync_cameras(self, result: TickResult) -> None:
        """Fetch and apply the central camera list, if it has changed.

        Costs nothing in steady state: the revision rides the poll that just
        happened, and this returns immediately unless it moved.

        Nothing here may cost footage. A refused or unreachable list leaves
        cameras.yaml exactly as it was, and the next revision change tries
        again — the same rule the rest of the trigger follows.
        """
        seen = getattr(self.client, "last_config_revision", None)
        if seen is None:
            return  # a saar-seva too old to send one; nothing to do
        if not seen:
            # Revision 0 means nobody has ever saved a camera list. That is the
            # normal state of a shop not using the feature, not something to
            # fetch and then refuse for being empty on every single poll.
            return
        if seen == self.applied_revision:
            return

        try:
            payload = self.client.fetch_camera_config()
        except SaarSevaError as exc:
            if exc.status == 404:
                log.debug("saar-seva has no central camera list yet")
                return
            self.last_sync_error = str(exc)
            log.warning("could not fetch the camera list: %s", exc)
            return

        # A bench mid-clip is never rewritten under ffmpeg. Those benches are
        # reported as deferred, and the revision stays unapplied so this runs
        # again when they go idle.
        busy = {
            work_center
            for work_center, status in self.pool.statuses().items()
            if status.get("busy")
        }

        try:
            sync = camerasync.apply(payload, busy=busy)
        except camerasync.CameraSyncError as exc:
            self.last_sync_error = str(exc)
            log.error("refused the camera list: %s", exc)
            return
        except OSError as exc:
            self.last_sync_error = f"could not write cameras.yaml: {exc}"
            log.error("%s", self.last_sync_error)
            return

        self.last_sync_error = ""
        self.last_sync = sync
        result.camera_sync = sync.summary()

        if sync.removed:
            # A removal deletes a bench: it stops recording and nothing on this
            # box refers to it any more. That is a much bigger event than an
            # address change, and until now it was one WARNING in a log that
            # scrolls. The shop lost two of its three benches this way and
            # every check afterwards said OK, because one configured bench is
            # a perfectly healthy-looking thing to be.
            #
            # So it is written down, and preflight and the status page keep
            # saying it until somebody acknowledges it.
            self.catalogue.set_setting(self.REMOVED_KEY, json.dumps({
                "at": utcnow(),
                "revision": sync.revision,
                "benches": sorted(sync.removed),
            }))

        if sync.changed:
            self.reload_benches()
        if sync.complete:
            self.catalogue.set_setting(self.REVISION_KEY, str(sync.revision))
            self.catalogue.log_event("camera-sync", detail=sync.summary())
        else:
            log.info("camera sync partly held back: %s", sync.summary())

    #: What a bench's light should show, from what the camera is really doing.
    #: `recording` is deliberately NOT "the timer is running" — see the note in
    #: SaarSevaClient.post_heartbeat.
    @staticmethod
    def _bench_state(status: dict) -> str:
        if status.get("capturing"):
            return "recording"
        if status.get("camera_slow"):
            return "camera_not_responding"
        if status.get("connecting"):
            return "connecting"
        if status.get("state") == "paused":
            return "paused"
        # A failure from hours ago is history. Reporting it as a current fault
        # left a bench showing "Camera problem" for two days off one stale
        # string, and sent somebody to check a camera that was working.
        if status.get("state") == "error" or status.get("error_is_current"):
            return "error"
        return "idle"

    def _send_heartbeat(self) -> None:
        """Report every mapped bench's real capture state to saar-seva.

        Best-effort by design. saar-seva not having the endpoint, or being
        unreachable, must never disturb recording — the shop filming its work
        matters, a light on a screen does not.
        """
        benches = []
        statuses = self.pool.statuses()
        for workcenter_id, work_center in sorted(self._bench_by_workcenter.items()):
            status = statuses.get(work_center)
            if status is None:
                continue
            benches.append({
                "workcenter_id": workcenter_id,
                "work_center": work_center,
                "state": self._bench_state(status),
                "message": status.get("last_error", ""),
            })
        if not benches:
            return

        # Best-effort, and separately so: a catalogue read failing must not
        # cost the shop its bench lights.
        report = None
        try:
            from . import storage as storage_module

            report = storage_module.report(self.catalogue)
        except Exception as exc:
            log.debug("no storage report this tick: %s", exc)

        try:
            self.client.post_heartbeat(benches, report)
        except SaarSevaError as exc:
            # Never fatal — a light on a screen does not outrank filming the
            # work. But it must be VISIBLE: a heartbeat failing quietly is
            # indistinguishable, from the technician's side, from a recorder
            # that has died, and that is exactly the wrong place to guess.
            if exc.status == 404:
                self.last_heartbeat_error = (
                    "this saar-seva has no recorder-heartbeat endpoint yet, so the "
                    "recording light on the technician screen cannot work"
                )
                log.debug("saar-seva has no recorder-heartbeat endpoint yet")
            else:
                self.last_heartbeat_error = str(exc)
                log.warning("heartbeat not delivered: %s", exc)
            return

        self.last_heartbeat_error = ""
        self.last_heartbeat_at = time.time()

    # -- background loop ----------------------------------------------------

    def _remember(self, result: TickResult) -> None:
        self.last_result = result
        self.last_tick_at = time.time()

    def run_forever(self) -> None:
        log.info(
            "trigger polling %s every %ss", self.config.base_url, self.config.poll_seconds
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # a bug here must not kill the recorder
                log.exception("unexpected error in the trigger loop: %s", exc)
            self._stop.wait(self.config.poll_seconds)

    def start(self) -> None:
        """Run the loop on a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="repaircam-trigger", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        """For the status page."""
        result = self.last_result
        sync = self.last_sync
        return {
            "config_revision_applied": self.applied_revision,
            "config_revision_seen": getattr(self.client, "last_config_revision", None),
            "config_sync_summary": sync.summary() if sync else "",
            "config_sync_error": self.last_sync_error,
            "config_sync_waiting": list(sync.deferred) if sync else [],
            "storage_revision_applied": self.applied_storage_revision,
            "storage_revision_seen": getattr(self.client, "last_storage_revision", None),
            "storage_sync_summary": (
                self.last_storage_sync.summary() if self.last_storage_sync else ""
            ),
            "storage_sync_error": self.last_storage_error,
            # Benches the central list DELETED. Not a passing event: they stop
            # existing, and one bench looks as healthy as three.
            "config_removed": self.removed_benches,
            "vetoed_benches": list(self.vetoed_benches),
            "heartbeat_error": self.last_heartbeat_error,
            "heartbeat_seconds_ago": (
                round(time.time() - self.last_heartbeat_at, 1) if self.last_heartbeat_at else None
            ),
            "running": self.running,
            "base_url": self.config.base_url,
            "poll_seconds": self.config.poll_seconds,
            "owned": dict(self._owned),
            "benches": dict(self._bench_by_workcenter),
            "last_tick_at": self.last_tick_at,
            "seconds_since_tick": round(time.time() - self.last_tick_at, 1) if self.last_tick_at else None,
            "ok": result.ok if result else None,
            "summary": result.summary() if result else "not polled yet",
        }
