# RepairCam — design

The decisions behind the code, and why they went that way. Read this before
changing architecture. Short status summary lives in [../CLAUDE.md](../CLAUDE.md).

---

## 1. What the system is for

Three jobs, in priority order:

1. **Accountability.** When a customer says a phone came back worse, there is footage
   of the bench for that job.
2. **Training.** New technicians watch how an experienced one does an operation.
3. **A labelled dataset.** Every clip knows which job, operation, device and IMEI it
   shows, so the archive can later train a model without anyone re-watching and
   tagging thousands of hours.

Goal 3 is what drives most of the design. Video alone is nearly worthless for it;
video *plus reliable labels* is the asset. That is why the system is a
**capture-and-label** system, not a CCTV system, and why a clip is never written
without a sidecar describing it.

## 2. Decisions and the reasons

### One recorder box, many cameras — not one Pi per bench
A Raspberry Pi per bench was the obvious first idea and was rejected: ten Pis mean
ten SD cards to corrupt, ten things to update, and ten power supplies. One box
pulling RTSP from ten PoE cameras has a single point of failure, but it is a single
point of *attention* — one machine to back up, patch and watch.

### Copy, never re-encode
`ffmpeg -c:v copy` writes the camera's own H.264 stream straight to disk. The camera
has already done the encoding in hardware. An i3 laptop cannot re-encode ten 4MP
streams, but it can copy them all while barely working, because copying is disk I/O,
not computation.

The one exception is audio: VIGI cameras send `pcm_alaw`, which the MP4 container
cannot hold. Audio alone is transcoded to AAC — cheap, because it is a tiny stream.

### One clip per operation
A technician presses Start, may Pause and Continue several times, then presses Done.
Every Start→Pause pair is a *segment*; **Done joins the segments into one file**.

This matters for all three goals: one link in the Odoo chatter means one thing, a
trainee watches one video, and the dataset gets one sample per operation instead of
a pile of fragments nobody can reassemble later. Joining uses ffmpeg's concat
demuxer with stream copy — the segments all came from the same camera with identical
settings, so it takes seconds and loses nothing.

### Capture-agnostic backends
Everything above the camera talks only to `CaptureBackend` (`repaircam/backends/base.py`).
RTSP is the only implementation today. If a bench later needs a Pi camera or a USB
microscope, that is a new file in `backends/` and a new `backend:` value in
cameras.yaml — the state machine, catalogue and UI do not change.

### Storage stays local
Video never leaves the shop. Odoo and any cloud service hold **a link**, never the
file. Ten benches × 8 hours × ~1.8 GB/hour is roughly 145 GB a day — nothing anyone
wants to pay to upload or store in a cloud, and it would leak customer devices and
faces off-site. Path: SSD (working) → archive HDD → NAS.

### Cameras never touch the internet
The cameras sit on the shop LAN with no port forwarding, no cloud account, no UPnP.
Only the recorder talks to them. The web UI is on the LAN too and has **no login** —
the LAN *is* the boundary. Do not port-forward it; if remote access is ever needed,
put it behind a VPN, not a public port.

---

## 3. How a recording happens

```
technician              RepairCam                    disk
    |                       |                          |
  Start  ---------------->  Recorder.start()
    |                       |-- ffmpeg -c copy ----->  segments/WC2/<ts>/seg-001.mp4
  Pause  ---------------->  Recorder.stop()            (ffmpeg gets 'q', file is finalised)
    |                       |
  Start  ---------------->  Recorder.start()
    |                       |-- ffmpeg -c copy ----->  segments/WC2/<ts>/seg-002.mp4
    |                       |
  Done   ---------------->  Recorder.done()
                            |-- concat -c copy ---->   recordings/YYYY-MM-DD/WC2_<ts>_<mo>_<op>.mp4
                            |-- sidecar ---------->    recordings/YYYY-MM-DD/WC2_<ts>_<mo>_<op>.json
                            |-- SQLite row
```

**States** (`repaircam/recorder.py`): `idle → recording ⇄ paused → finalising → idle`.

Details that are easy to get wrong and are handled deliberately:

- **Stopping sends `q`, not a kill.** An MP4 only becomes playable when ffmpeg writes
  its index at the end. Killing the process leaves a corrupt file — and the footage
  is not reproducible.
- **A dead camera is noticed.** `Recorder.state` checks whether ffmpeg is still alive
  and drops the bench to `paused` if it is not, so the UI cannot show a counting timer
  over a dead process.
- **Done can be retried.** If joining fails, the segments are kept and the bench stays
  `paused` — the footage is never thrown away because of a save error.
- **Empty sessions are refused.** If nothing usable was captured, `done()` raises
  rather than filing a zero-byte clip, and logs an `abandoned` event.
- **Resuming does not overwrite labels.** The Continue button posts the same form; an
  empty form must not wipe the job the operation is already tagged with.

## 4. On disk

```
~/repaircam-data/                     (override with REPAIRCAM_DATA_DIR)
├── repaircam.db                      SQLite catalogue
├── recordings/2026-07-27/
│   ├── WC2_20260727-101500_wh-mo-00042_screen-replacement.mp4
│   └── WC2_20260727-101500_wh-mo-00042_screen-replacement.json   ← sidecar
├── segments/WC2/20260727-101500/     in-progress pieces, removed after Done
└── snapshots/                        focus-test stills
```

Filenames are readable on purpose: bench, timestamp, MO, operation. The archive can
be browsed in a file manager with no database and no software.

**Database paths are relative** to the data directory, so moving the archive to
another disk does not invalidate every row.

### The sidecar

The clip plus its sidecar is a complete dataset sample — no database lookup, no Odoo
call needed to know what the footage shows. The database is an *index* over the
sidecars and can be rebuilt from them.

```json
{
  "schema_version": 1,
  "clip": "recordings/2026-07-27/WC2_20260727-101500_wh-mo-00042_screen-replacement.mp4",
  "work_center": "WC2",
  "camera": { "name": "Bench 2", "model": "TP-Link VIGI C540V", "host": "192.168.0.133",
              "url": "rtsp://admin:******@192.168.0.133:554/stream1" },
  "job": { "mo_name": "WH/MO/00042", "operation": "Screen replacement",
           "device": "Redmi Note 12", "imei": "350123456789012",
           "technician": "Ramesh", "notes": "" },
  "recorded": { "started_at": "...", "ended_at": "...", "duration_s": 412.5,
                "segments": 2, "segment_durations_s": [210.1, 202.4] },
  "media": { "video_codec": "h264", "width": 2560, "height": 1440, "frame_rate": 15.0,
             "audio_codec": "aac", "duration_s": 412.5, "bit_rate": 4096000 },
  "recorder": { "version": "0.2.0", "host": "shop-recorder" }
}
```

The camera URL is **always** password-masked — sidecars ship with the dataset, and a
leaked camera password would ship with them. A test enforces this.

## 5. The web UI (Phase 2)

Flask, server-rendered, no build step and no framework — it has to keep working on
whatever browser is on the bench tablet, for years, with nobody maintaining a
toolchain.

| Page | What it is for |
|---|---|
| `/` | Every bench, its state and a live timer. Poll-updated. |
| `/bench/<WC>` | The technician's page: live preview, job form, Start / Pause / Done. |
| `/library` | Everything recorded; search by MO, device, IMEI, technician. |
| `/clip/<id>` | Play, download, read the sidecar, fix the job labels. |
| `/status` | ffmpeg, disk free (in *bench-hours*), camera reachability, activity log. |

**Live preview** is MJPEG: a browser cannot play RTSP, so ffmpeg re-packages the
camera's *sub* stream into JPEG frames. Deliberately the sub-stream at 6 fps — the
preview must never take bandwidth from a recording in progress.

**Un-labelled clips are surfaced, not hidden.** The dashboard and library both count
them, because an unlabelled clip is nearly useless for goal 3 and the count is the
nudge to fix it.

## 6. Phase 5 — the saar-seva / Odoo trigger (not built)

Today a technician presses Start in two places: saar-seva (for time tracking) and
RepairCam (for video). Phase 5 removes the second one.

**Direction matters.** saar-seva runs on Render, in the cloud. The cameras and the
recorder are on the shop LAN behind a home router. The cloud cannot open a connection
into the shop, so **RepairCam polls saar-seva** — not the other way round.

```
  technician taps Start on /trc/time  (saar-seva, existing screen)
            |
            v
  saar-seva records the time entry in Odoo
            |
  RepairCam polls  GET /trc/active   every ~5s   <-- endpoint to be added
            |
            v
  sees WC2 active with MO + operation + IMEI -> Recorder.start(labels)
  sees it gone                                -> Recorder.done()
            |
            v
  POST /trc/recordings {mo, operation, url}   <-- endpoint to be added
            |
            v
  saar-seva writes the link into the Odoo MO chatter (never the video itself)
```

Two endpoints have to be added to saar-seva:

- `GET /trc/active` → the operations currently running, each with work center, MO
  name, operation, and the device/IMEI from the existing `stock.lot` lookup.
- `POST /trc/recordings` → `{mo_name, operation, url, duration_s, recorded_at}`;
  writes a link into the **MO chatter only**.

Both reuse saar-seva's existing `backend/app/odoo.py` client. The known real
endpoints on that side today are `POST /trc/jobs/{job}/time/start` | `/time/stop` and
`POST /trc/jobs/{job}/workorders/{wo}/finish`.

Planned config (`repaircam/saarseva.yaml`, gitignored like cameras.yaml):

```yaml
saarseva:
  base_url: "https://saar-seva-api-staging.onrender.com"
  api_key: "..."            # a service token, not a technician login
  poll_seconds: 5
  link_base: "http://192.168.0.50:8080"   # LAN URL the chatter link points at
  work_centers: ["WC2"]     # only poll benches that have a camera
```

Points to get right when building it:

- **The poll must be safe to fail.** If Render is down or the shop internet drops,
  recording must keep working from the web UI. The trigger is a convenience, never a
  dependency.
- **The link is a LAN URL.** It only opens inside the shop — which is the point.
- **`link_posted` already exists** on the recordings table for marking a clip whose
  link has reached the chatter, so a retry does not post it twice.

## 7. Hardware and capacity

- **Recorder:** the existing Linux i3 laptop, 8 GB RAM. Copying streams is I/O-bound,
  so this is adequate for the POC and probably for all ten benches.
- **Camera:** TP-Link VIGI C540V — 4MP, 3× optical zoom with autofocus, ONVIF/RTSP, PoE.
  Bench cams are capped at 4096 kbps in the VIGI app.
- **Buying rule:** motorized varifocal only. A lens spec with a *range* (2.8–12 mm) can
  be zoomed to fill the frame with a phone; a single number (3.6 mm) is fixed — reject it.
  This is the difference between footage where a screw is visible and footage where it is not.
- **POC wiring:** a single PoE injector, no switch. A switch arrives when bench 2 does.

**Storage maths.** 4096 kbps ≈ 1.8 GB per bench-hour.

| | per hour | 8-hour day | 30 days |
|---|---|---|---|
| 1 bench | 1.8 GB | 14 GB | 430 GB |
| 10 benches | 18 GB | 145 GB | 4.3 TB |

The status page reports free space in **bench-hours** rather than gigabytes, because
that is the number that answers "can we record tomorrow?".

## 8. Testing

Tests run on any machine — **no cameras and no ffmpeg required**. `StubBackend` in
`tests/conftest.py` fakes capture by writing small files; everything above the
backend is the real code, including the state machine, catalogue and sidecars.

```bash
python3 -m pytest
```

What is covered: the Start/Pause/Done cycle, multi-segment joins, label handling,
camera dropout, failed captures, path safety, password masking, config parsing,
ffmpeg command construction, and every web route.

What is **not** covered and can only be checked on the shop LAN: real RTSP capture,
real concat of real MP4s, MJPEG preview, and focus.

## 9. Running it as a service

`deploy/repaircam.service` is a template; `deploy/install-service.sh` fills in the
user, paths, port and data directory and installs it. The script checks that the
virtualenv, Flask, ffmpeg and cameras.yaml are actually in place *before* handing the
unit to systemd, because a service that fails at boot is much harder for a
non-technical owner to diagnose than a script that refuses up front.

Choices worth keeping:

- **Runs as the ordinary shop user, not root.** Recordings live in that user's home
  directory and nothing here needs administrator powers.
- **`Wants=network-online.target`**, not plain `network.target` — the latter is
  satisfied before an address is assigned, and RepairCam is useless until it can
  reach the cameras.
- **`StartLimitIntervalSec=0` in `[Unit]`.** systemd's default gives up permanently
  after a few rapid restarts; a camera unplugged overnight would leave the shop with
  a dead recorder in the morning. Note the section: systemd *silently ignores* this
  key under `[Service]`, which is easy to get wrong and impossible to notice without
  `systemd-analyze verify`.
- **`TimeoutStopSec=30`.** On stop, systemd signals every process in the cgroup,
  including ffmpeg, which finalises its MP4 on SIGTERM. Rushing this corrupts clips.
- **Hardening stops at `ProtectSystem=full`.** ffmpeg spawns subprocesses and writes
  video into the home directory; a lockdown that breaks recording is worse than no
  lockdown.

**A restart part-way through an operation loses the in-memory session.** The segments
survive in `segments/` and each is a valid MP4, but nothing joins or catalogues them.
`repaircam/recovery.py` and `cli.py recover` adopt those orphans — see section 10.

## 10. Recovering orphaned footage

`recovery.py` finds segment folders that never became a clip — a folder only survives
there if Done was never reached — and files them through the *same* `finalise_session`
path as a normal Done, so a rescued clip is indistinguishable from an ordinary one
apart from `"recovered": true` in its sidecar.

The stakes are the opposite of the rest of the system: recovery only ever touches
footage that already exists and cannot be re-recorded. So it is built to refuse rather
than risk:

- **A failed join keeps the segments.** Files are deleted only after the join succeeds.
  Half-recovered footage that has been deleted is worse than footage still orphaned, and
  a failure (usually a missing ffmpeg) stays retryable.
- **A folder written to recently is left alone.** Joining a file ffmpeg is still writing
  would corrupt it, so anything touched within 120s is skipped unless `--force`.
- **One bad folder does not stop the rest** — they are independent operations that happen
  to share a fate.
- **Listing is the default.** `recover` shows what it found and changes nothing;
  `recover --all` acts.
- **Rescued clips are unlabelled**, because nobody said what job they were for. The
  library already surfaces unlabelled clips, which is the nudge to tag them.
- **Start time comes from the folder name**, which is the recording's start; the sidecar
  marks it `started_at_is_estimated` because nobody pressed Done to confirm the end.
- **A bench removed from cameras.yaml still recovers.** The footage is no less real; the
  sidecar records that the camera is no longer configured.

The `/status` page lists orphans, because they appear in no other page — they are not in
the library, having never become clips.

## 11. Open items

- **Focus test not yet passed** (Phase 0 gate): a real phone at 60–80 cm must be sharp
  enough to read screws. `python3 -m repaircam.cli snapshot WC2`, then look at the image.
- **Retention/archive job.** Nothing deletes or moves old clips yet; the SSD will fill.
- **Phase 5** as above.
- **Rebuild-from-sidecars command** — the design says the database can be rebuilt from
  sidecars, and it can, but the command to do it is not written.
