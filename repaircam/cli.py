"""RepairCam command line — the bench-proof and troubleshooting tool.

The web UI is what technicians use. This is what you use over SSH when
something is wrong, and what proved the camera works in Phase 0:

    python3 -m repaircam.cli record WC2 --duration 20
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config, ffmpeg, recovery, saarseva, storage
from .backends import CaptureError, build_backend
from .catalogue import Catalogue, JobLabels
from .config import ConfigError
from .recorder import Recorder, RecorderError

log = logging.getLogger("repaircam")

OK = "OK  "
BAD = "FAIL"
WARN = "WARN"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _labels_from_args(args: argparse.Namespace) -> JobLabels:
    return JobLabels(
        mo_name=getattr(args, "mo", "") or "",
        operation=getattr(args, "operation", "") or "",
        device=getattr(args, "device", "") or "",
        imei=getattr(args, "imei", "") or "",
        technician=getattr(args, "technician", "") or "",
        notes=getattr(args, "note", "") or "",
    )


def _add_label_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("job labels (what the clip is about)")
    group.add_argument("--mo", help="Odoo Manufacturing Order, e.g. WH/MO/00042")
    group.add_argument("--operation", help="operation name, e.g. 'Screen replacement'")
    group.add_argument("--device", help="device model, e.g. 'Redmi Note 12'")
    group.add_argument("--imei", help="device IMEI")
    group.add_argument("--technician", help="who is doing the work")
    group.add_argument("--note", help="free-text note")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_cameras(args: argparse.Namespace) -> int:
    """List configured benches, and optionally test each camera."""
    if args.sync and _sync_cameras_now() != 0:
        return 1

    cameras = config.load_cameras()
    print(f"{len(cameras)} bench(es) configured in {config.cameras_file()}\n")
    for work_center, camera in sorted(cameras.items()):
        print(f"  {work_center:<6} {camera.name}")
        print(f"         {camera.model or 'camera'} at {camera.host} via {camera.backend}")
        print(f"         record: {camera.safe_main_url}")
        print(f"         preview: {camera.safe_sub_url}")
        # Printed even when unset: without it the bench is simply never
        # auto-triggered, and a silent opt-out is exactly the sort of thing
        # someone spends an afternoon not finding.
        if camera.odoo_workcenter_id:
            print(f"         odoo work centre: {camera.odoo_workcenter_id} (auto-trigger ready)")
        else:
            print("         odoo work centre: NOT SET — this bench will never auto-record")
        if args.check:
            ok, message = build_backend(camera).check(timeout=args.timeout)
            print(f"         {OK if ok else BAD} {message}")
        print()
    return 0


def _sync_cameras_now() -> int:
    """Pull the central camera list and write cameras.yaml. For `cameras --sync`.

    The trigger does this by itself when the revision changes; this exists for
    when somebody is standing at the box and wants to watch it work, or when
    the trigger is not running at all.
    """
    from . import camerasync
    from .recorder import RecorderPool

    try:
        saar_config = saarseva.load_config()
    except ConfigError as exc:
        print(f"{BAD} {exc}", file=sys.stderr)
        return 1

    client = saarseva.SaarSevaClient(saar_config)
    try:
        payload = client.fetch_camera_config()
    except saarseva.SaarSevaError as exc:
        print(f"{BAD} could not fetch the camera list: {exc}", file=sys.stderr)
        return 1

    # A bench mid-clip is left exactly as it is; rewriting the file under a
    # running ffmpeg helps nobody.
    busy: set[str] = set()
    try:
        pool = RecorderPool(Catalogue())
        busy = {wc for wc, s in pool.statuses().items() if s.get("busy")}
    except Exception:  # no cameras yet, first ever sync
        pass

    try:
        result = camerasync.apply(payload, busy=busy)
    except camerasync.CameraSyncError as exc:
        print(f"{BAD} refused: {exc}", file=sys.stderr)
        print("     cameras.yaml is unchanged.", file=sys.stderr)
        return 1

    print(f"{OK} {result.summary()}")
    if result.deferred:
        print("     Those benches are recording; run this again when they finish.")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Everything that has to be true before a shop relies on this box.

    One command rather than a checklist, because a checklist has to be
    remembered and this does not. Every failure says what to do about it — the
    person running this is standing at the recorder, not reading the source.
    """
    checks: list[tuple[str, str, str]] = []  # (marker, title, detail)

    def add(ok, title, detail="", warn=False):
        checks.append((WARN if warn else (OK if ok else BAD), title, detail))

    print("RepairCam pre-flight\n")

    # -- the box itself -----------------------------------------------------
    add(ffmpeg.available(), "ffmpeg installed",
        "" if ffmpeg.available() else "sudo apt update && sudo apt install -y ffmpeg")

    report = storage.disk_report()
    add(report.state != "full", "disk has room", report.message,
        warn=report.state == "low")

    store_cfg = storage.load_config()
    if store_cfg.archive_dir:
        archive = store_cfg.archive_path
        add(archive.exists(), "archive reachable",
            f"{archive}" if archive.exists() else f"{archive} is not there — is it mounted?")
    else:
        add(False, "second copy of the footage",
            "No archive_dir in storage.yaml — this machine holds the ONLY copy of "
            "every clip. A theft or a dead drive loses all of it.", warn=True)

    # -- benches ------------------------------------------------------------
    try:
        cameras = config.load_cameras()
    except ConfigError as exc:
        add(False, "cameras.yaml", str(exc))
        cameras = {}

    if cameras:
        add(True, f"{len(cameras)} bench(es) configured", ", ".join(sorted(cameras)))
        unmapped = [wc for wc, cam in cameras.items() if cam.odoo_workcenter_id is None]
        add(not unmapped, "every bench has an Odoo work centre",
            "" if not unmapped else
            f"{', '.join(sorted(unmapped))} will never auto-record — no odoo_workcenter_id")

        if not args.quick:
            for work_center in sorted(cameras):
                ok, message = build_backend(cameras[work_center]).check(timeout=args.timeout)
                add(ok, f"camera {work_center} answers", message)

    # -- saar-seva ----------------------------------------------------------
    try:
        saar = saarseva.load_config()
    except ConfigError:
        add(False, "saar-seva auto-trigger",
            "Not set up (no saarseva.yaml). Technicians would have to start every "
            "recording by hand.", warn=True)
        return _print_preflight(checks)

    if not saar.enabled:
        add(False, "saar-seva auto-trigger", "disabled in saarseva.yaml", warn=True)

    staging = "staging" in saar.base_url
    add(not staging, "pointed at production", saar.base_url,
        warn=staging)

    # An allow-list here silently vetoes a bench added centrally.
    if saar.work_centers:
        vetoed = sorted(
            wc for wc, cam in cameras.items()
            if cam.odoo_workcenter_id is not None and wc not in set(saar.work_centers)
        )
        add(not vetoed, "work_centers is not blocking a bench",
            "" if not vetoed else
            f"{', '.join(vetoed)} will never auto-record. Empty work_centers in "
            f"saarseva.yaml to allow every configured bench.")
    else:
        add(True, "work_centers is empty", "every configured bench may auto-record")

    add(bool(saar.link_base), "link_base set",
        saar.link_base or "Not set — the links posted to Odoo would have no address.")

    client = saarseva.SaarSevaClient(saar)
    ids = sorted(c.odoo_workcenter_id for c in cameras.values() if c.odoo_workcenter_id)

    try:
        client.fetch_active(ids or None)
        add(True, "saar-seva accepts the API key", saar.base_url)
        reachable = True
    except saarseva.SaarSevaError as exc:
        hint = str(exc)
        if "503" in hint:
            hint += "\n            REPAIRCAM_API_KEY is not set on that server."
        elif "401" in hint or "403" in hint:
            hint += "\n            The token here and the one on the server differ."
        add(False, "saar-seva accepts the API key", hint)
        reachable = False

    if reachable:
        try:
            client.post_heartbeat([])
            add(True, "recording light reaches saar-seva")
        except saarseva.SaarSevaError as exc:
            add(False, "recording light reaches saar-seva", str(exc),
                warn=getattr(exc, "status", None) == 404)

        try:
            payload = client.fetch_camera_config()
            revision = payload.get("revision")
            count = len(payload.get("cameras") or [])
            if not revision:
                add(False, "central camera list",
                    "Nothing saved in the admin panel yet, so cameras.yaml stays "
                    "hand-edited. That is fine — but it is not the central list.",
                    warn=True)
            else:
                add(True, "central camera list", f"revision {revision}, {count} camera(s)")
        except saarseva.SaarSevaError as exc:
            add(False, "central camera list", str(exc),
                warn=getattr(exc, "status", None) == 404)

    return _print_preflight(checks)


def _print_preflight(checks: list[tuple[str, str, str]]) -> int:
    failed = warned = 0
    for marker, title, detail in checks:
        print(f"  {marker} {title}")
        for line in (detail or "").splitlines():
            if line.strip():
                print(f"            {line.strip()}")
        failed += marker == BAD
        warned += marker == WARN

    print()
    if failed:
        print(f"  {BAD} {failed} thing(s) must be fixed before going live.")
        return 1
    if warned:
        print(f"  {WARN} Ready, with {warned} thing(s) worth knowing about above.")
        return 0
    print(f"  {OK} Ready.")
    return 0


def cmd_storage(args: argparse.Namespace) -> int:
    """How much room is left, what has a second copy, and what has not."""
    catalogue = Catalogue()
    cfg = storage.load_config()

    if args.archive:
        result = storage.archive_pending(catalogue, cfg, limit=args.limit)
        marker = OK if result.ok else BAD
        print(f"{marker} {result.summary()}")
        for clip_id, reason in result.failed:
            print(f"     clip {clip_id}: {reason}", file=sys.stderr)
        if not result.ok:
            return 1

    if args.prune:
        result = storage.prune(catalogue, cfg)
        print(f"{OK} {result.summary()}")

    info = storage.status(catalogue, cfg)
    disk = info["disk"]
    marker = {"ok": OK, "low": WARN, "full": BAD}.get(disk["state"], OK)
    print(f"\n  {marker} disk      {disk['message']}")

    if not cfg.archive_dir:
        print(f"  {WARN} archive   NOT SET — this laptop holds the only copy of every clip")
        print( "            Set archive_dir in repaircam/storage.yaml.")
    elif info["archive_missing"]:
        print(f"  {BAD} archive   {cfg.archive_dir} is not there — is the disk mounted?")
    else:
        print(f"  {OK} archive   {cfg.archive_dir}")

    print(f"     clips     {info['archived']} with a second copy, "
          f"{info['unarchived']} without")
    if info["unarchived"] and cfg.archive_dir:
        print( "            Run:  python3 -m repaircam.cli storage --archive")
    if cfg.archive_dir and not cfg.delete_after_archive:
        print(f"     deleting  off — clips are kept after archiving "
              f"(keep_days={cfg.keep_days} applies once it is on)")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Record one clip from one bench."""
    recorder = Recorder(args.work_center)
    labels = _labels_from_args(args)

    if args.duration:
        print(f"Recording {args.work_center} for {args.duration:g}s — Ctrl-C to stop early.")
        recording = recorder.record_once(args.duration, labels)
    else:
        print(f"Recording {args.work_center}. Press Ctrl-C to stop and save.")
        recorder.start(labels)
        try:
            while recorder.state.value == "recording":
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopping...")
        recording = recorder.done()

    print(f"\n{OK} saved {recording.duration_hms}  ({recording.size_mb} MB)")
    print(f"     clip:    {recording.absolute_path()}")
    print(f"     sidecar: {config.data_dir() / recording.sidecar_path}")
    print(f"     id:      {recording.id}")
    if recording.labels.is_empty:
        print("     note: this clip has no job labels — tag it in the web UI (Library).")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Grab one still — this is the focus test.

    Point the camera at a real phone at 60–80 cm, take a snapshot, open it, and
    check that the screen and screws are sharp.

    It samples the MAIN stream, the one that gets recorded. The sub-stream is
    far lower resolution, so a perfectly focused camera can look like it cannot
    resolve a screw simply because that image never had the pixels — judging
    focus on it would fail a camera that is fine.
    """
    camera = config.get_camera(args.work_center)
    backend = build_backend(camera)
    dest = Path(args.output) if args.output else None
    if dest is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = config.ensure_data_dirs()["snapshots"] / f"{args.work_center}_{stamp}.jpg"

    stream = "sub" if args.sub else "main"
    backend.snapshot(dest, stream=stream)
    size_kb = round(dest.stat().st_size / 1024)
    print(f"{OK} snapshot saved: {dest}  ({size_kb} KB, {stream} stream{_dimensions(dest)})")
    if stream == "sub":
        print("     NOTE: sub-stream — too low-resolution to judge focus on.")
        print("     Drop --sub for the real focus test.")
    else:
        print("     Open it and check a phone at 60-80 cm is sharp (screws readable).")
    return 0


def _dimensions(path: Path) -> str:
    """``, 2560x1440`` if ffprobe can say, else nothing. Never fatal."""
    try:
        streams = ffmpeg.probe(str(path)).get("streams") or []
    except Exception:
        return ""
    for s in streams:
        if s.get("width") and s.get("height"):
            return f", {s['width']}x{s['height']}"
    return ""


def cmd_list(args: argparse.Namespace) -> int:
    """Show what has been recorded."""
    catalogue = Catalogue()
    recordings = catalogue.list(
        work_center=args.work_center, mo_name=args.mo, imei=args.imei,
        search=args.search, limit=args.limit,
    )
    if not recordings:
        print("No recordings yet.")
        return 0

    print(f"{'ID':>4}  {'BENCH':<6} {'WHEN':<20} {'LENGTH':>8} {'SIZE':>8}  JOB")
    for recording in recordings:
        when = recording.started_at.replace("T", " ")[:19]
        job = recording.title
        if recording.labels.device:
            job += f"  [{recording.labels.device}]"
        print(
            f"{recording.id:>4}  {recording.work_center:<6} {when:<20} "
            f"{recording.duration_hms:>8} {recording.size_mb:>7.1f}M  {job}"
        )
    stats = catalogue.stats()
    print(f"\n{stats['clips']} clips, {stats['hours']}h, {stats['gigabytes']} GB total")
    if stats["unlabelled"]:
        print(f"{stats['unlabelled']} clip(s) have no job label yet.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Everything known about one clip."""
    recording = Catalogue().get(args.id)
    if recording is None:
        print(f"No recording with id {args.id}.", file=sys.stderr)
        return 1

    path = recording.absolute_path()
    print(f"Recording {recording.id}")
    print(f"  bench      {recording.work_center}  ({recording.camera_name})")
    print(f"  started    {recording.started_at}")
    print(f"  length     {recording.duration_hms}  ({recording.segments} segment(s))")
    print(f"  size       {recording.size_mb} MB")
    print(f"  video      {recording.video_codec} {recording.width}x{recording.height}"
          f" @ {recording.frame_rate or '?'} fps")
    print(f"  audio      {recording.audio_codec or 'none'}")
    print(f"  file       {path}{'' if path.exists() else '   *** MISSING ***'}")
    print("  job labels:")
    for key, value in recording.labels.as_dict().items():
        print(f"    {key:<12} {value or '-'}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """File footage that was recorded but never saved as a clip.

    This happens when the recorder restarts part-way through an operation: the
    segments survive on disk, but nothing joined them, so they never reached the
    library. Safe by default — it lists what it found and only acts on --all.
    """
    orphans = recovery.find_orphans()
    if not orphans:
        print(f"{OK} Nothing to recover — no unsaved footage on this machine.")
        return 0

    print(f"Found {len(orphans)} unsaved recording(s):\n")
    print(f"  {'WHICH':<26} {'WHEN':<20} {'PIECES':>6} {'SIZE':>8}")
    for orphan in orphans:
        active = "  (still recording?)" if orphan.looks_active(args.grace) else ""
        when = orphan.started_iso.replace("T", " ")[:19]
        print(
            f"  {orphan.label:<26} {when:<20} {len(orphan.segments):>6} "
            f"{orphan.size_mb:>7.1f}M{active}"
        )

    if not args.all:
        print("\nNothing has been changed. To save these as clips, run:")
        print("    python3 -m repaircam.cli recover --all")
        print("\nThey will be saved without a job label — tag them afterwards in the Library.")
        return 0

    print()
    recovered, failed = recovery.recover_all(grace_seconds=args.grace, force=args.force)

    for recording in recovered:
        print(f"  {OK} saved {recording.duration_hms:>8}  {recording.path}")
    for orphan, reason in failed:
        print(f"  {BAD} {orphan.label}: {reason}")

    print(f"\n{len(recovered)} saved, {len(failed)} left alone.")
    if recovered:
        print("These clips have no job label yet. Tag them in the Library so they stay useful.")
    return 1 if failed and not recovered else 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Test or run the automatic trigger that starts recording from saar-seva.

    Nothing is switched on until repaircam/saarseva.yaml exists, and saar-seva's
    saar-seva also needs REPAIRCAM_API_KEY set — see docs/PHASE5-CONTRACT.md.
    """
    from .recorder import RecorderPool
    from .trigger import Trigger

    if not saarseva.is_configured():
        print("The automatic trigger is OFF — technicians start recordings by hand.")
        print("\nTo switch it on:")
        print(f"    cp {saarseva.config_file().parent / 'saarseva.example.yaml'} "
              f"{saarseva.config_file()}")
        print(f"\nWhen you fill it in, this recorder's address is probably:")
        print(f"    link_base: \"{saarseva.default_link_base()}\"")
        print("\nNote: saar-seva also needs REPAIRCAM_API_KEY set, or it will answer 503.")
        return 0

    config = saarseva.load_config()
    client = saarseva.SaarSevaClient(config)

    print("Automatic trigger settings:")
    for key, value in config.describe().items():
        print(f"    {key:<14} {value}")

    print("\nAsking saar-seva what is running...")
    ok, message = client.check()
    print(f"    {OK if ok else BAD} {message}")
    if not ok:
        return 1

    for operation in client.fetch_active():
        print(f"      {operation.work_center:<6} {operation.mo_name or '(no MO)':<16} "
              f"{operation.operation or '(no operation)'}")

    if not (args.once or args.run):
        print("\nNothing was changed. To act on this once:")
        print("    python3 -m repaircam.cli trigger --once")
        return 0

    trigger = Trigger(RecorderPool(Catalogue()), client, config=config)
    if args.once:
        print("\nRunning one cycle...")
        print(f"    {trigger.tick().summary()}")
        return 0

    print(f"\nPolling every {config.poll_seconds}s. Press Ctrl-C to stop.")
    try:
        while True:
            result = trigger.tick()
            if result.changed or not result.ok:
                print(f"    {result.summary()}")
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Is this box healthy enough to record? Checks tools, disk and cameras."""
    print("RepairCam status\n")

    print(f"  version      {__version__}")
    binary = shutil.which("ffmpeg")
    print(f"  {OK if binary else BAD} ffmpeg    {ffmpeg.version() or 'NOT INSTALLED — sudo apt install ffmpeg'}")

    root = config.data_dir()
    print(f"  data dir     {root}{'' if root.exists() else '  (will be created)'}")
    if root.exists():
        usage = shutil.disk_usage(root)
        free_gb = usage.free / 1_073_741_824
        # ~4 Mbps copied stream is roughly 1.8 GB/hour per bench.
        hours = free_gb / 1.8
        marker = OK if free_gb > 20 else BAD
        print(f"  {marker} disk      {free_gb:.1f} GB free  (~{hours:.0f} bench-hours)")

    catalogue = Catalogue()
    stats = catalogue.stats()
    print(f"  catalogue    {stats['clips']} clips, {stats['hours']}h, {stats['gigabytes']} GB"
          f", {stats['today']} today")

    print("\n  cameras:")
    try:
        cameras = config.load_cameras()
    except ConfigError as exc:
        print(f"  {BAD} {exc}")
        return 1

    failures = 0
    for work_center, camera in sorted(cameras.items()):
        if args.quick:
            print(f"    {work_center:<6} {camera.name} at {camera.host}")
            continue
        ok, message = build_backend(camera).check(timeout=args.timeout)
        failures += 0 if ok else 1
        print(f"    {OK if ok else BAD} {work_center:<6} {camera.name:<28} {message}")

    return 1 if failures else 0


def cmd_web(args: argparse.Namespace) -> int:
    """Start the web UI that technicians use."""
    try:
        from .web import create_app
    except ImportError as exc:
        print(f"Flask is not installed: {exc}\n  python3 -m pip install -r requirements.txt",
              file=sys.stderr)
        return 1

    app = create_app()
    print(f"RepairCam web UI: http://{args.host}:{args.port}")
    print("On another device on the shop network, use this box's IP instead of 0.0.0.0.")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


def cmd_relabel(args: argparse.Namespace) -> int:
    """Attach or correct the job labels on an existing clip."""
    catalogue = Catalogue()
    recording = catalogue.get(args.id)
    if recording is None:
        print(f"No recording with id {args.id}.", file=sys.stderr)
        return 1

    labels = recording.labels
    for field_name in ("mo", "operation", "device", "imei", "technician", "note"):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(labels, {"mo": "mo_name", "note": "notes"}.get(field_name, field_name), value)

    catalogue.update_labels(args.id, labels)
    print(f"{OK} recording {args.id} relabelled: {labels.mo_name or '(no MO)'}")
    print("     Note: the sidecar JSON keeps its original labels; the catalogue is now the truth.")
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m repaircam.cli",
        description="RepairCam — record repair work at the bench.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Common uses:\n"
            "  python3 -m repaircam.cli status              is everything healthy?\n"
            "  python3 -m repaircam.cli snapshot WC2        focus test\n"
            "  python3 -m repaircam.cli record WC2 --duration 20\n"
            "  python3 -m repaircam.cli web                 start the UI for technicians\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"RepairCam {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cameras", help="list configured benches")
    p.add_argument("--check", action="store_true", help="also test each camera is reachable")
    p.add_argument(
        "--timeout", type=float, default=ffmpeg.DEFAULT_CHECK_TIMEOUT,
        help="seconds to wait for a camera to answer (default: %(default)s)",
    )
    p.add_argument(
        "--sync", action="store_true",
        help="fetch the central camera list from saar-seva and apply it now",
    )
    p.set_defaults(func=cmd_cameras)

    p = sub.add_parser("record", help="record a clip from one bench")
    p.add_argument("work_center", help="bench code, e.g. WC2")
    p.add_argument("--duration", type=float, help="seconds to record (default: until Ctrl-C)")
    _add_label_arguments(p)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("snapshot", help="grab one still image (the focus test)")
    p.add_argument("work_center")
    p.add_argument("-o", "--output", help="where to write the JPEG")
    p.add_argument(
        "--sub", action="store_true",
        help="use the low-resolution sub-stream (cheap, but no good for judging focus)",
    )
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("list", help="list recorded clips")
    p.add_argument("--work-center", help="only this bench")
    p.add_argument("--mo", help="only this Manufacturing Order")
    p.add_argument("--imei", help="only this device")
    p.add_argument("--search", help="free-text search over labels")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="show one clip in detail")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("relabel", help="set the job labels on an existing clip")
    p.add_argument("id", type=int)
    _add_label_arguments(p)
    p.set_defaults(func=cmd_relabel)

    p = sub.add_parser("recover", help="save footage left behind by a restart")
    p.add_argument("--all", action="store_true", help="actually save them (default: just list)")
    p.add_argument(
        "--grace",
        type=float,
        default=recovery.DEFAULT_GRACE_SECONDS,
        help="seconds of inactivity before a folder counts as finished (default: %(default)s)",
    )
    p.add_argument("--force", action="store_true", help="recover even if it may still be recording")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("trigger", help="the saar-seva automatic start/stop (Phase 5)")
    p.add_argument("--once", action="store_true", help="run a single cycle and stop")
    p.add_argument("--run", action="store_true", help="keep polling until Ctrl-C")
    p.set_defaults(func=cmd_trigger)

    p = sub.add_parser("status", help="health check: ffmpeg, disk, cameras")
    p.add_argument("--quick", action="store_true", help="skip the camera network tests")
    p.add_argument(
        "--timeout", type=float, default=ffmpeg.DEFAULT_CHECK_TIMEOUT,
        help="seconds to wait for a camera to answer (default: %(default)s)",
    )
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("preflight", help="is this box ready for the shop to rely on?")
    p.add_argument("--quick", action="store_true", help="skip the camera network tests")
    p.add_argument(
        "--timeout", type=float, default=ffmpeg.DEFAULT_CHECK_TIMEOUT,
        help="seconds to wait for a camera to answer (default: %(default)s)",
    )
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("storage", help="disk space, archive copies and clean-up")
    p.add_argument("--archive", action="store_true",
                   help="copy clips that have no second copy yet")
    p.add_argument("--prune", action="store_true",
                   help="delete local clips that are old AND verified at the archive")
    p.add_argument("--limit", type=int, default=20,
                   help="how many clips to archive in one run (default: %(default)s)")
    p.set_defaults(func=cmd_storage)

    p = sub.add_parser("web", help="start the web UI")
    p.add_argument("--host", default="0.0.0.0", help="default 0.0.0.0 (whole shop LAN)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (
        ConfigError,
        RecorderError,
        CaptureError,
        recovery.RecoveryError,
        saarseva.SaarSevaError,
        ffmpeg.FFmpegError,
    ) as exc:
        # These are the expected, explainable failures — show the message, not a
        # traceback, because the person reading it is not a programmer.
        print(f"\n{BAD} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
