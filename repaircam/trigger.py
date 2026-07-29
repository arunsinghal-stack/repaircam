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

import logging
import threading
import time
from dataclasses import dataclass, field

from . import config as camera_config
from .catalogue import Catalogue, Recording
from .recorder import RecorderError, RecorderPool, State
from .saarseva import (
    KIND_PACKING,
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
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.started or self.finished or self.links_posted)

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
    ):
        self.pool = pool
        self.client = client
        self.config = config or client.config
        self.catalogue = catalogue or pool.catalogue

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
        self.reload_benches()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: TickResult | None = None
        self.last_tick_at: float = 0.0

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
        for work_center, camera in cameras.items():
            if allowed and work_center not in allowed:
                continue
            if camera.odoo_workcenter_id is None:
                continue
            mapping[camera.odoo_workcenter_id] = work_center

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
        try:
            operations = self._fetch_all()
        except SaarSevaError as exc:
            # Crucially, do not touch any recorder. A failed poll must never be
            # read as "nothing is running" — that would end every recording in
            # the shop the moment the internet hiccups.
            result.ok = False
            result.error = str(exc)
            log.warning("saar-seva poll failed, leaving recorders alone: %s", exc)
            self._remember(result)
            return result

        wanted = self._by_bench(operations)
        result.active = len(wanted)

        with self._lock:
            for work_center, operation in wanted.items():
                self._reconcile_start(work_center, operation, result)
            for work_center in list(self._owned):
                if work_center not in wanted:
                    self._finish(work_center, result, reason="no longer active")

        self._post_pending_links(result)
        self._remember(result)
        if result.changed:
            log.info("trigger: %s", result.summary())
        return result

    def _fetch_all(self) -> list[ActiveOperation]:
        """What saar-seva says is being recorded, across both integrations.

        Repair benches and packing benches are both Odoo work centres, so they
        share one id space and one camera map. A failure in either poll raises
        — a partial picture must never be mistaken for "nothing is running",
        which would end every recording in the shop.
        """
        benches = self.workcenter_ids
        operations = list(self.client.fetch_active(benches))
        try:
            operations += list(self.client.fetch_active_packing(benches))
        except SaarSevaError as exc:
            # A saar-seva without the packing endpoints answers 404. That is a
            # deployment state, not a fault: keep the repair trigger working.
            if "does not exist on the server yet" not in str(exc):
                raise
            log.debug("packing endpoints not deployed yet: %s", exc)
        return operations

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
                log.warning("could not post the link for clip %s: %s", recording.id, exc)
                return  # saar-seva is unhappy; stop hammering it until next tick

            self.catalogue.mark_link_posted(recording.id)
            self._operations.pop(recording.id, None)
            result.links_posted.append(recording.id)

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
        return {
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
