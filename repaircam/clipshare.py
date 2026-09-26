"""Upload a disputed clip that SAAR has approved for sharing.

Video does not leave the shop — with one exception, decided by the owner on
26 Sep 2026: when a reseller DISPUTES a parcel (what our bench found when it
opened theirs, or what arrived back after a repair) and a person at SAAR
approves it, the opening or packing clip that answers the dispute is uploaded
to Shopify and shown on that reseller's ticket for 30 days.

How it reaches this box: saar-seva cannot call into the shop, so the request
rides the /pack/active poll as ``clip_uploads``. For each one this:

1. marks the clip **keep** first, so retention can never delete evidence
   somebody is disputing — even if the upload itself then fails;
2. finds the file (the recorder's copy, then the archive — ``clipfile``, the
   same answer the clip page gives);
3. asks saar-seva for a one-time upload slot (saar-seva holds the Shopify
   key; this box never does);
4. streams the file to the slot and tells saar-seva it is there.

A clip that is not on disk any more is reported as MISSING, which is final.
Anything else is reported as a failure and saar-seva offers it again after a
pause, a few times. Uploads run one at a time on a background thread: the
poll's first job is starting and stopping recordings, and a slow upload over
the shop's internet must never delay one.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from . import clipfile
from .saarseva import ClipUpload, SaarSevaError

log = logging.getLogger(__name__)


class ClipSharer:
    def __init__(self, catalogue, client, *, enabled: bool = True,
                 spawn: Optional[Callable] = None):
        self.catalogue = catalogue
        self.client = client
        self.enabled = enabled
        #: Injectable so tests run the work inline, without a thread.
        self._spawn = spawn or self._thread
        self._lock = threading.Lock()
        self._queue: list[ClipUpload] = []
        self._busy: set[str] = set()
        self._running = False
        #: What happened last, per upload id — for the status page and tests.
        self.results: dict[str, str] = {}

    @staticmethod
    def _thread(target) -> None:
        threading.Thread(target=target, name="clip-sharer", daemon=True).start()

    def offer(self, uploads: list[ClipUpload]) -> int:
        """Queue what saar-seva asked for. Returns how many were new."""
        if not self.enabled or not uploads:
            return 0
        added = 0
        with self._lock:
            for up in uploads:
                if up.upload_id in self._busy:
                    continue  # already queued or uploading
                self._busy.add(up.upload_id)
                self._queue.append(up)
                added += 1
            start = added and not self._running
            if start:
                self._running = True
        if start:
            self._spawn(self._drain)
        return added

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                up = self._queue.pop(0)
            try:
                self.results[up.upload_id] = self.share(up)
            except Exception as exc:  # the worker must never die
                log.exception("clip share %s crashed", up.upload_id)
                self.results[up.upload_id] = f"crashed: {exc}"
            finally:
                with self._lock:
                    self._busy.discard(up.upload_id)

    def share(self, up: ClipUpload) -> str:
        """Do one upload. Returns a short outcome; never raises SaarSevaError."""
        recording = self.catalogue.get(up.recording_id)
        if recording is None:
            return self._report(up, f"RepairCam has no recording {up.recording_id}.", missing=True)

        # Keep first: a disputed clip must survive retention whatever happens
        # to the upload.
        self.catalogue.set_keep(up.recording_id, True)
        self.catalogue.log_event(
            "clip-share", detail=f"clip {up.recording_id} approved for a dispute; kept",
            recording_id=up.recording_id,
        )

        try:
            path = clipfile.find_clip(recording)
        except clipfile.OutsideDataDir:
            return self._report(up, "The recording's path is outside the data directory.", missing=True)
        if path is None:
            return self._report(
                up, "The video file is on neither the recorder nor the archive.", missing=True
            )

        filename = f"repaircam-{up.kind or 'clip'}-{up.recording_id}{path.suffix or '.mp4'}"
        mime = "video/mp4"
        try:
            slot = self.client.request_clip_slot(
                up.upload_id, size_bytes=path.stat().st_size, filename=filename, mime=mime
            )
        except SaarSevaError as exc:
            if exc.status == 409:
                # The dispute was closed or the clip already went: nothing to do.
                return f"not wanted any more: {exc}"
            return self._report(up, f"could not get an upload slot: {exc}")
        except OSError as exc:
            return self._report(up, f"could not read the file: {exc}")

        try:
            self.client.upload_to_slot(slot, path, filename=filename, mime=mime)
        except SaarSevaError as exc:
            return self._report(up, str(exc))

        try:
            self.client.confirm_clip_upload(up.upload_id)
        except SaarSevaError as exc:
            # The bytes are up but saar-seva did not take the confirmation. It
            # re-offers a stale slot, so the next attempt uploads again.
            log.warning("clip %s uploaded but not confirmed: %s", up.recording_id, exc)
            return f"uploaded, confirmation failed: {exc}"
        self.catalogue.log_event(
            "clip-shared", detail=f"clip {up.recording_id} uploaded for a dispute",
            recording_id=up.recording_id,
        )
        return "shared"

    def _report(self, up: ClipUpload, reason: str, *, missing: bool = False) -> str:
        log.warning("clip %s not shared: %s", up.recording_id, reason)
        try:
            self.client.report_clip_failed(up.upload_id, reason, missing=missing)
        except SaarSevaError as exc:
            log.warning("could not report the failure either: %s", exc)
        return ("missing: " if missing else "failed: ") + reason
