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
- Camera CHOSEN: **TP-Link VIGI C540V** (4MP, 3× optical zoom + autofocus, ONVIF/RTSP, PoE).
  Working RTSP: `rtsp://admin:<pass>@192.168.0.133:554/stream1` (main) / `/stream2` (sub).
  Bench cam bitrate capped at 4096 kbps in the VIGI app; audio is `pcm_alaw` → must transcode to AAC for MP4.
- Buying rule: **motorized varifocal only** (a lens focal RANGE like 2.8–12mm). A single mm number = fixed = reject.

## Status / phases
- **Phase 0 (bench proof):** recording via ffmpeg confirmed working on Linux. The **focus test**
  (sharp on a real phone at ~60–80 cm) is the remaining gate — not yet reported passed.
  Run it with `python3 -m repaircam.cli snapshot WC2`.
- **Phase 1 (DONE, in repo):** recorder core in `repaircam/` — `CaptureBackend`+`RtspBackend`, `Recorder`
  state machine, SQLite catalogue, `cli.py`. Test: `python3 -m repaircam.cli record WC2 --duration 20`.
- **Phase 2 (DONE, in repo):** Flask web UI in `repaircam/web/` — dashboard, bench page with live
  MJPEG preview and Start/Pause/Done, library with search and re-labelling, status page.
  Start it with `python3 -m repaircam.cli web`, or install it permanently with
  `./deploy/install-service.sh` (systemd unit + installer in `deploy/`).
- **Recovery + deploy (DONE, in repo):** `deploy/` installs the web UI as a systemd
  service; `repaircam/recovery.py` + `cli.py recover` re-file segments a restart left
  orphaned (listed on the status page). Note Phases 3 and 4 are undefined anywhere.
- **Phase 5 (RepairCam half DONE, saar-seva half NOT):** `saarseva.py` (polling client, stdlib
  urllib) + `trigger.py` (reconciles recorders against `GET /trc/active`) + `cli.py trigger`.
  Off until `repaircam/saarseva.yaml` exists (gitignored; template alongside it).
  **Blocked on saar-seva:** `GET /trc/active` and `POST /trc/recordings` do not exist yet —
  the exact contract for them is docs/PHASE5-CONTRACT.md. That work is in the
  `arunsinghal-stack/saar-seva-app` repo, which must be added to the session first.

## Conventions
- `repaircam/cameras.yaml` holds camera IPs/passwords — **local only, gitignored.** Never commit it.
  A template lives at `repaircam/cameras.example.yaml`.
- Recordings + DB live under `~/repaircam-data/` (override `REPAIRCAM_DATA_DIR`).
- Run the recorder on the machine that can reach the cameras (the Linux box), not the Mac.
- Never print or template a camera password: use `camera.safe_main_url` and `ffmpeg.redact()`.
- Tests must run with no cameras and no ffmpeg — capture is faked by `StubBackend` in
  `tests/conftest.py`. Run them with `python3 -m pytest`.
