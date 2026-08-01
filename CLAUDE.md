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
- **ADDRESSES MOVE — do not trust any address written here.** The shop network was renumbered
  from `192.168.1.x` to `192.168.0.x` on 2026-08-01 with nobody touching RepairCam, and every
  address in this file was wrong within a morning. Ask the box, do not read it:
  `ip -4 addr` for the recorder, `cli cameras` for the cameras, and
  `for i in $(seq 1 254); do ( timeout 1 bash -c "</dev/tcp/192.168.0.$i/554" 2>/dev/null && echo 192.168.0.$i ) & done; wait`
  to find every camera on the LAN regardless of what is configured.
  **The fix is DHCP reservations on the router** for the cameras and the recorder; until
  that is done this will happen again.
- As of 2026-08-01: recorder on Wi-Fi at **192.168.0.165** (`http://192.168.0.165:8080`),
  installed as the `repaircam` systemd service (starts on boot). That URL is also what
  `link_base` must be in `saarseva.yaml` — `cli preflight` now checks link_base against the
  addresses this box really answers on, because a renumbering silently turned every link
  posted to the Odoo chatter into a dead end.
- Camera CHOSEN: **TP-Link VIGI C540V** (4MP, 3× optical zoom + autofocus, ONVIF/RTSP, PoE).
  **ONE camera exists in the shop** (verified by port-554 scan, 2026-08-01), at
  `192.168.0.132`, **physically over the PACKING TABLE** and correctly mapped to the
  Packing Station, Odoo work centre 13. There is no camera on any repair bench, so repair
  auto-recording has nothing to film. Bench codes like `WC13` are RepairCam's own labels
  derived from the Odoo work-centre id — `WC13` does NOT mean "bench 1" or "bench 13".
  Working RTSP: `rtsp://admin:<pass>@192.168.0.132:554/stream1` (main) / `/stream2` (sub).
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
- **Phase 5 (LIVE — end-to-end on 2026-07-29):** a technician pressed Start on saar-seva's
  `/trc/time`, RepairCam began filming by itself, Stop ended the clip, and the link appeared
  in the Odoo chatter. Three defects were found and fixed on that first run — all three are
  in `docs/PHASE5-CONTRACT.md` under "Retries, and when RepairCam gives up"; the worst was
  that one un-postable clip at the head of an oldest-first queue silently stopped every later
  clip's link, with nothing in the UI saying so.
  RepairCam side = `saarseva.py`
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

## Recording light on saar-seva (LIVE — verified 2026-07-29)
- The technician's `/trc/time` screen shows what the CAMERA is doing, beside the existing
  `⏱ running` pill which only ever meant the TIMER. They differ whenever the recorder is
  off, the camera is unplugged, the bench is unmapped, or the shop's internet is down.
- RepairCam reports on the poll it already makes: `POST /trc/recorder-heartbeat` →
  `recorder_bench_state`; the screen polls `GET /trc/recorder-state` every 5s.
- **The staleness rule is the feature.** A state older than 20s comes back as `unknown`,
  server-side, so no caller can render a stale light. Verify by stopping the recorder:
  the light must go grey within 20s, never stay red.
- `trigger.py` reports heartbeat delivery on the status page ("Recording light: reported
  Ns ago"). It failed silently for an afternoon before that existed.
- **Trap:** `work_centers` in `saarseva.yaml` is an allow-list. Leave it **empty**, or a
  bench added centrally is fully configured and still never records. The status page calls
  this out under "Not auto-recording".
- **Trap (cost the shop 2 of its 3 benches on 2026-08-01):** the central list is
  authoritative, so a bench MISSING from it is **deleted** from `cameras.yaml` on the next
  sync. One bench then looks exactly as healthy as three. Now recorded durably
  (`camera_config_removed` in the catalogue) and reported by `preflight` and the status
  page until `cli cameras --clear-removed`.
- **`link_base` being set is not the same as it being right.** It must name an address
  THIS box answers on; `preflight` now checks it against the machine's own addresses,
  because a network renumbering made every link posted to Odoo a dead end and every
  check still said OK.

## Central camera config (BUILT, both halves merged; not yet used in the shop)
- Camera list lives in Admin → TRC settings → "Cameras ↔ work centres" (`trc.manage`).
  A revision number rides `/trc/active`; the recorder re-fetches `GET /repaircam/cameras`
  only when it moves, then rewrites `cameras.yaml` itself. See docs/CAMERA-CONFIG-SYNC-PLAN.md.
- Passwords are central, encrypted at rest under **`REPAIRCAM_CONFIG_KEY`** (Render env
  var). Without it, saving a password is refused — plaintext is never the fallback.
  **Lose that key and every camera password must be re-entered.**
- Addresses are checked against 192.168/16, 10/8, 172.16/12 by membership. NOT
  `ipaddress.is_private`, which also accepts loopback, link-local and the RFC 5737
  documentation ranges — `203.0.113.5` would have passed for a shop camera.

## Packing video (built end to end; never yet run in the shop)
- **"Both halves built" was the wrong phrase and it hid a gap for a day.** It meant
  RepairCam and saar-seva's *backend*. The packer's screen had no Record button at all —
  `Packing.jsx` did not mention recording. Built now (saar-seva PRs #475, #477). When
  describing this feature, say which of the THREE parts is done: recorder, endpoints, UI.
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
  + `PackingRecording` model + packer Record/Stop in `warehouse.py` + the panel in
  `pages/warehouse/Packing.jsx`. RepairCam side: `saarseva.py` + `trigger.py`.
- **Record appears only while the job is `packing`**, enforced in the endpoint too — a clip
  of an empty bench looks like evidence. **Stop is never gated** and the panel stays while a
  clip runs, whatever the status: otherwise a packer who completes the order with the camera
  on cannot stop it and that bench films for ever.
- The camera light comes from the packer's OWN recordings endpoint, not `/trc/recorder-state`
  — that one is technician-authenticated and **a packer is not a technician**. The same role
  split made packing benches unmappable until saar-seva PR #473.
- Setup is three things, all joined on the Odoo work-centre id: the station is a work centre;
  the packer carries the **Packer** tag and is mapped in Admin -> TRC settings -> **People**
  (not "Technicians" — it lists both now); a camera is mapped to the same work centre.
- Catalogue schema v2: `recordings.source` / `source_ref` say which integration a clip came
  from, so link retries survive a restart.
- Full plan: docs/PACKING-VIDEO-PLAN.md.
- **Retention/archive (Phase 3, BUILT — the lifecycle is closed):** `storage.py`.
  Free-space guard refuses **Start** below `min_free_gb` (20 GB ≈ 11 bench-hours); a resume
  is let through. Clips are copied to `archive_dir` and verified there.
  **THERE ARE TWO RETENTION WINDOWS AND THEY ARE NOT THE SAME THING:**
  - `keep_days_local` (default 7) = how long the **recorder** keeps its copy, with
    `delete_after_archive: true`. This is **arithmetic, not policy** — 3 benches ≈ 43 GB/day,
    so this 204 GB laptop holds ~5 days. Putting the shop's 30-day policy here fills the disk
    by mid-week and the guard then refuses Start while nothing is old enough to prune.
    Nothing is lost when it expires: the clip page falls back to the archive, so Odoo links
    still play.
  - `keep_days` + `keep_days_by_source` (**repair 30, packing 45**) = how long the
    **archive** keeps it, with `delete_from_archive: true` (`prune_archive`). This is the
    shop's policy and the point at which **the footage stops existing**. Two switches on
    purpose: one frees the laptop, the other ends the record.
  A clip marked `keep` is never deleted by either pass — enforced in the SQL, not the caller.
  The archived file is re-checked for existence and size at the moment of local deletion,
  and `prune_archive` refuses any path not under the archive configured *now*, refuses an
  unmounted archive, and takes the local copy with it. The catalogue row **outlives its
  footage** (`archive_deleted`, `archive_deleted_at`) so an old Odoo link says "passed its
  retention window and has been deleted" rather than "missing from disk". A background
  worker does all of it every 10 min; `cli storage [--archive] [--prune]` does it by hand.
  Config: `repaircam/storage.yaml` (optional — the guard applies without it); a change needs
  a service restart, since the worker reads it once. Sizing: docs/STORAGE-PLAN.md — 22–43
  GB/day at 3 benches, so the archive settles near 1.4 TB on a 30/45 policy.
  Stage 7 (exporting `keep`-marked clips as a training set) still does not exist.

## Going live (recorder cut over to PRODUCTION 2026-07-29; `preflight` all green)
- `base_url` is `https://saar-seva-api.onrender.com`; production has both
  `REPAIRCAM_API_KEY` and the code. Disk ~204 GB (~113 bench-hours).
- **Three benches were configured on 2026-07-29 (WC1, WC2, WC13); by 2026-08-01 only the
  packing camera remained.** The central list was saved with one camera in it, so the sync
  deleted the other two — the design working as intended, but one bench then looks exactly
  as healthy as three. A port-554 scan confirmed only one camera physically exists, so the
  removal was correct and was acknowledged with `cli cameras --clear-removed`.
- **Outstanding:** the camera password `Admin@321` was printed by a pre-flight run before
  `redact_text()` existed, so it is in a terminal scrollback and a chat log — **rotate it.**
  The power-cut behaviour has never been tested, and it must be before
  `delete_after_archive` is switched on: archiving would faithfully copy a clip a power cut
  had truncated. DHCP reservations are still not set on the router.
- **Second copy is LIVE (2026-08-01).** 3.7 TB Seagate, NTFS (it holds 355 GB of the shop's
  own tool files, so it was NOT reformatted), mounted by UUID from `/etc/fstab` at
  `/mnt/backup-drive`, `archive_dir: /mnt/backup-drive/RepairCam`. **Both deletions off.**
  Notes: `archive_dir` is the SUBFOLDER, not the mount root — if the drive is unplugged the
  empty mount point still exists but the subfolder does not, so RepairCam correctly sees the
  archive as gone. NTFS after a power cut can mount read-only; the fix is
  `sudo ntfsfix /dev/sdX2`. The drive dropped off the USB bus once during setup and came
  back as a different device name (`sdb` -> `sdc`), which is why the fstab entry uses the
  UUID; watch for that recurring before trusting it. `mount -a` will stack a second mount on
  top of a dead one — unmount in a loop until `findmnt` is empty, then `mount /mnt/backup-drive`.
- **docs/GO-LIVE.md** is the runbook. `cli preflight` checks the whole thing from the
  recorder's side and says what to do about anything it finds — run it before believing
  the shop is ready.
- staging and production are **different databases**. The camera list and its passwords
  entered on one do not exist on the other. Production needs its own `REPAIRCAM_API_KEY`
  and `REPAIRCAM_CONFIG_KEY` on the `saar-seva-api` Render service.
- Production already answers **503** on the RepairCam endpoints, which means the code is
  deployed and only the key is missing. 401 would mean the key is set and ours is wrong.

## Conventions
- `repaircam/cameras.yaml` holds camera IPs/passwords — **local only, gitignored.** Never commit it.
  A template lives at `repaircam/cameras.example.yaml`.
- Recordings + DB live under `~/repaircam-data/` (override `REPAIRCAM_DATA_DIR`).
- Run the recorder on the machine that can reach the cameras (the Linux box), not the Mac.
- Never print or template a camera password: use `camera.safe_main_url` and `ffmpeg.redact()`.
- Tests must run with no cameras and no ffmpeg — capture is faked by `StubBackend` in
  `tests/conftest.py`. Run them with `python3 -m pytest`.
