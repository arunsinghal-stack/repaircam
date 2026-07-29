# RepairCam — project context for Claude

> This file is auto-loaded by Claude Code. It lets a **fresh session on any machine**
> (clone the repo, start Claude) pick up the full project. The detailed design is in
> [docs/PLAN.md](docs/PLAN.md) — read it before making design decisions.

## Working with the owner
- The owner (Arun, SAAR Enterprise) is **non-technical**. Give **exact, copy-paste, step-by-step**
  instructions, one action at a time, and say how to tell each step worked. Define jargon briefly.
- Recorder machine runs **Linux** (Ubuntu/Debian assumed). The owner also uses a **Mac** for Claude.
- Camera work must be tested on the **shop LAN** (the Mac can't reach shop cameras).

## What RepairCam is
Records mobile-phone repair work at technician benches for **staff accountability**, **training**,
and a **labelled AI training dataset**. It's a *capture-and-label* system: each clip is tagged with
the job (MO/operation/device/IMEI) and gets a sidecar JSON so the dataset is self-describing.

## Architecture (decided)
- **One recorder box + up to 10 PoE IP cameras** (NOT one Pi per bench). Recorder does
  `ffmpeg -c copy` (no re-encode). **1 Work Center = 1 bench = 1 camera.**
- **Capture-agnostic:** app talks only to a `CaptureBackend` interface (RTSP now, Pi-cam later).
- **Storage = local** (SSD → archive HDD → NAS). Cloud/Odoo only ever hold a **link**, never video.
- **Cameras are never internet-exposed;** only the LAN recorder (RepairCam) talks to them.

## Integration with the existing stack
- Shop runs **Odoo 19 (Custom plan)** + a customer app **saar-seva-app**
  (repo `arunsinghal-stack/saar-seva-app`, private; FastAPI backend on Render, React frontend on
  Cloudflare Workers). Odoo is source of truth.
- Technicians press **Start/Stop/Done per operation** on saar-seva's **TRC "Repair time"** screen
  (`/trc/time`, warehouse role). Real endpoints found: `POST /trc/jobs/{job}/time/start` | `/time/stop`,
  `POST /trc/jobs/{job}/workorders/{wo}/finish`. Backend: `https://saar-seva-api-staging.onrender.com`.
- **Trigger = RepairCam POLLS saar-seva** (cloud can't push into the shop LAN). Add two endpoints to
  saar-seva: `GET /trc/active` (poll) + `POST /trc/recordings` (post video link to the Odoo **MO chatter**).
  Reuse saar-seva's `backend/app/odoo.py` client + its IMEI→device lookup (`stock.lot`).
- One clip **per operation** (sessions concatenated on Done); links go to the **MO chatter only**.

## Hardware (POC — frozen)
- Recorder: existing **Linux i3 laptop** (8 GB RAM OK). POC uses a **single PoE injector** (no switch).
  On the shop LAN at **192.168.1.163**; the web UI is `http://192.168.1.163:8080`, installed as
  the `repaircam` systemd service (starts on boot). That URL is also what `link_base` must be
  set to in `saarseva.yaml` when Phase 5 is switched on — the links posted to Odoo point at it.
- Camera CHOSEN: **TP-Link VIGI C540V** (4MP, 3× optical zoom + autofocus, ONVIF/RTSP, PoE).
  Live on the shop LAN at **192.168.1.184** (the shop is a `192.168.1.x` network; older notes
  saying `192.168.0.133` are stale — test fixtures still use that as a dummy, which is fine).
  Working RTSP: `rtsp://admin:<pass>@192.168.1.184:554/stream1` (main) / `/stream2` (sub).
  Bench cam bitrate capped at 4096 kbps in the VIGI app; audio is `pcm_alaw` → must transcode to AAC for MP4.
- Buying rule: **motorized varifocal only** (a lens focal RANGE like 2.8–12mm). A single mm number = fixed = reject.

## Status / phases
- **Phase 0 (bench proof): PASSED 2026-07-29.** On the shop bench, on real hardware:
  focus test passed (a real phone at ~60–80 cm, sharp), a 40s clip recorded, played back
  clean, **with audio**. Nothing in the VIGI app was changed — the camera was always fine.
  The test only failed before because `snapshot` sampled the SUB-stream, which lacks the
  resolution to judge "readable screws" and so fails a good camera. It now samples the
  main stream. Do not go hunting for camera settings that were never touched.
- **Phase 1 (DONE, in repo):** recorder core in `repaircam/` — `CaptureBackend`+`RtspBackend`, `Recorder`
  state machine, SQLite catalogue, `cli.py`. Test: `python3 -m repaircam.cli record WC2 --duration 20`.
- **Phase 2 (DONE, in repo):** Flask web UI in `repaircam/web/` — dashboard, bench page with live
  MJPEG preview and Start/Pause/Done, library with search and re-labelling, status page.
  Start it with `python3 -m repaircam.cli web`, or install it permanently with
  `./deploy/install-service.sh` (systemd unit + installer in `deploy/`).
- **Recovery + deploy (DONE, in repo):** `deploy/` installs the web UI as a systemd
  service; `repaircam/recovery.py` + `cli.py recover` re-file segments a restart left
  orphaned (listed on the status page). Note Phases 3 and 4 are undefined anywhere.
- **Phase 5 (BOTH halves built, not yet run for real):** RepairCam side = `saarseva.py`
  (polling client, stdlib urllib) + `trigger.py` + `cli.py trigger`. saar-seva side =
  `GET /trc/active` + `POST /trc/recordings` in `backend/app/routers/trc.py` of
  `arunsinghal-stack/saar-seva-app` — MERGED to its `staging` branch in PR #461
  (add that repo to the session to work on it; note staging -> main is a separate
  "prod cutover" PR there).
  Contract + how to switch it on: docs/PHASE5-CONTRACT.md.
  Key facts: benches join on **`odoo_workcenter_id`** (in cameras.yaml), NOT a "WC2" code —
  saar-seva has no such code. Identity of a recording is saar-seva's `time_log_id`;
  **one clip per timer session**, so a Pause in saar-seva ends the clip. Links go to
  `mrp.production` OR `repair.order` chatter depending on `odoo_object_type`.
  Off until `repaircam/saarseva.yaml` exists AND saar-seva has `REPAIRCAM_API_KEY` set
  (without it those endpoints 503 everyone).

## Packing video (BOTH halves built, not yet run for real)
- Packer presses **Record/Stop** on a saar-seva packing job; each clip's link goes to the
  **outgoing** Delivery Order's chatter. Several clips per order is normal.
- **The DO rule:** an order is one-step (a single `outgoing` picking) or two-step (`internal`
  PICK **+** `outgoing` OUT). The link ALWAYS goes to the **outgoing** one — the PICK is an
  internal transfer, not the customer's delivery.
- Posted at Stop time by resolving the OUT from `sale.order.picking_ids`; NOT from
  `PackingJob.odoo_do_picking_id`, which is only filled at dispatch, long after packing.
- **Packing benches are Odoo work centres**, same as repair benches — `cameras.yaml` needs no
  new field, and multiple packing stations work.
- saar-seva side: `routers/repaircam_pack.py` (`GET /pack/active`, `POST /pack/recordings`)
  + `PackingRecording` model + packer Record/Stop in `warehouse.py`, on branch
  `claude/packing-video-endpoints`. RepairCam side: `saarseva.py` + `trigger.py`.
- Catalogue schema v2: `recordings.source` / `source_ref` say which integration a clip came
  from, so link retries survive a restart.
- Full plan: docs/PACKING-VIDEO-PLAN.md.
- **Retention/archive:** nothing deletes or moves old clips. ~1.8 GB per bench-hour, so the
  SSD will fill and recording will stop mid-repair. Next real build item.

## Conventions
- `repaircam/cameras.yaml` holds camera IPs/passwords — **local only, gitignored.** Never commit it.
  A template lives at `repaircam/cameras.example.yaml`.
- Recordings + DB live under `~/repaircam-data/` (override `REPAIRCAM_DATA_DIR`).
- Run the recorder on the machine that can reach the cameras (the Linux box), not the Mac.
- Never print or template a camera password: use `camera.safe_main_url` and `ffmpeg.redact()`.
- Tests must run with no cameras and no ffmpeg — capture is faked by `StubBackend` in
  `tests/conftest.py`. Run them with `python3 -m pytest`.
