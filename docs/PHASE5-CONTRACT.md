# Phase 5 — the saar-seva integration contract

**Both halves are now built.** RepairCam's side is `saarseva.py` + `trigger.py` in
this repo; saar-seva's side is `GET /trc/active` and `POST /trc/recordings` in
`arunsinghal-stack/saar-seva-app` (`backend/app/routers/trc.py`).

This describes what they actually do. Design reasoning is in [PLAN.md §6](PLAN.md).

---

## Why RepairCam polls

saar-seva runs on Render, in the cloud. The cameras and the recorder are on the shop
LAN behind a home router, deliberately unreachable from the internet. **The cloud
cannot open a connection into the shop**, so RepairCam asks saar-seva what is
happening, roughly every 5 seconds. There is no webhook and there cannot be one
without exposing the shop, which the whole design refuses to do.

```
technician taps Start on /trc/time (saar-seva)
        │
        ├─ saar-seva opens a repair_time_log row (active = true)
        │
RepairCam ── GET /trc/active ────────────────────────►  every ~5s
        │    ◄── work centre 12, MO/00042, "Screen replacement", IMEI 3501…
        │
        └─ Recorder.start(labels)
        …
technician taps Stop  → saar-seva closes the row (active = false)
        │
RepairCam ── GET /trc/active ────────────────────────►
        │    ◄── the row is gone
        │
        ├─ Recorder.done()  → one clip, sidecar, catalogue row
        └─ POST /trc/recordings {time_log_id, url, …}
                 │
                 └─ saar-seva writes the LINK onto the job's Odoo chatter
```

## One clip per timer session

A `repair_time_log` row is created on Start and closed on Stop. RepairCam records
exactly while that row is active, so **one clip per timer session**.

The consequence, chosen deliberately: a technician who pauses to fetch a part
produces two clips and two chatter links, not one paused recording. If that turns
out to be noisy in practice, the fix is on the saar-seva side — keep the job listed
in `/trc/active` across a Stop and drop it only on finish — and RepairCam's own
Pause/Continue then maps onto it with no change here.

## Identity: `time_log_id`

RepairCam decides "is this the same recording as last poll?" from `time_log_id`.
saar-seva creates one per Start and never reuses it, which is exactly the property
needed. It is also the key saar-seva uses to make a repeated `POST /trc/recordings`
idempotent.

## Benches: Odoo work-centre **ids**, not codes

saar-seva identifies a bench by `odoo_workcenter_id` (an integer). There is no code
like "WC2" anywhere in it — `workcenter_name` is free text a person can rename in
Odoo at any time. So `cameras.yaml` carries the mapping explicitly:

```yaml
cameras:
  WC2:
    name: "Bench 2"
    host: "192.168.0.133"
    odoo_workcenter_id: 12      # <- this is the join
```

A bench with no `odoo_workcenter_id` is never auto-triggered; that is how a camera
opts out. RepairCam sends only the ids it has cameras for, so saar-seva never
describes work it could not record.

---

## `GET /trc/active`

**Auth.** `X-API-Key: <token>` or `Authorization: Bearer <token>`, compared against
`REPAIRCAM_API_KEY`. **While that setting is empty — every deployment today — both
endpoints return 503 to everyone**, so an unconfigured server cannot be polled by
anyone who guesses the URL.

**Query.** `?workcenters=12,13` — limits the answer to benches with cameras.

**Response.**

```json
{
  "active": [
    {
      "time_log_id": "3f2b…",
      "job_id": "9a1c…",
      "workcenter_id": 12,
      "workcenter_name": "Bench 2",
      "operation": "Screen replacement",
      "mo_name": "WH/MO/00042",
      "workorder_id": 987,
      "object_type": "mo",
      "device": "Redmi Note 12",
      "imei": "350123456789012",
      "technician": "Ramesh",
      "started_at": "2026-07-27T10:15:00"
    }
  ]
}
```

`workcenter_id` is the only field RepairCam cannot work without — it is how a clip
finds its camera. Rows without one are dropped. Unknown extra fields are ignored, so
saar-seva can add to this freely. A bare `[…]` array is also accepted.

Fields map to saar-seva's tables as: `operation` ← `repair_time_log.step`,
`mo_name` ← `repair_job.odoo_ref`, `device` ← `repair_job.product_name`,
`imei` ← `repair_job.serial`, `technician` ← `exec_profile.display_name`.

**An empty list means nothing is being worked on**, and RepairCam finishes whatever
it was recording. That makes this endpoint the authority on what is running.

## `POST /trc/recordings`

```json
{
  "time_log_id": "3f2b…",
  "job_id": "9a1c…",
  "recording_id": 41,
  "url": "http://192.168.0.50:8080/clip/41",
  "duration_s": 412.5,
  "recorded_at": "2026-07-27T10:15:00+00:00"
}
```

`url` is a **LAN address**. It opens inside the shop and nowhere else, which is the
point: the chatter carries a pointer, and the footage never leaves the premises.

**Where the link goes depends on the job.** `repair_job.odoo_object_type` is `mo`
*or* `repair_order` — ticketed repairs are repair orders with no MO and no work
order. Keying this on the MO alone would silently drop their video:

| `odoo_object_type` | Odoo model | id field |
|---|---|---|
| `mo` | `mrp.production` | `odoo_mo_id` |
| `repair_order` | `repair.order` | `odoo_repair_id` |

**Idempotency.** The link is stored on `repair_time_log.repaircam_url` once Odoo
accepts it. A repeat for a time log that already has one returns
`{"ok": true, "duplicate": true}` and writes no second chatter line.

**Failures stay retryable.** If the Odoo write fails, saar-seva returns 502 and does
**not** store the URL — otherwise the retry would look like a duplicate and the link
would be lost for good. RepairCam keeps `link_posted = 0` and tries again later.

Responses: `200` accepted · `404` no such time log · `409` the job has no Odoo record
yet · `502` Odoo refused the write · `401` bad token · `503` not configured.

---

## When saar-seva is unreachable

Render sleeps, home internet drops, tokens expire. **None of that may stop the shop
recording.**

- A failed poll touches **no recorder**. Treating an error as "nothing is running"
  would end every recording in the shop the moment the internet hiccups.
- A malformed response is refused rather than acted on, for the same reason.
- A bench already recording keeps recording through an outage.
- Recording is always startable by hand in the web UI, whatever the trigger is doing.
- A recording started **by hand** is never taken over or stopped — the trigger only
  ends benches it started itself.
- Unposted links are not lost: retries are driven from `Catalogue.list_unposted()`,
  so a clip finished just before a restart is still posted afterwards.

---

## Turning it on

1. **saar-seva:** set `REPAIRCAM_API_KEY` in the Render environment. Until then both
   endpoints return 503.
2. **cameras.yaml:** add `odoo_workcenter_id` to each bench that should auto-record.
   Find it in Odoo under Manufacturing → Configuration → Work Centers; the number is
   at the end of the address bar.
3. **saarseva.yaml:** copy `repaircam/saarseva.example.yaml`, set `base_url`,
   `api_key` (the same token) and `link_base` (this recorder's LAN URL).
4. Check it: `python3 -m repaircam.cli trigger`.
5. Restart the recorder: `sudo systemctl restart repaircam`.

## What has been verified

Both halves were run against each other in a test harness: RepairCam's real client
driving saar-seva's real endpoints, with a stub camera and Odoo mocked. The full
cycle — Start → record → Stop → file the clip → post the link → chatter written, and
not written twice — passes, as do the auth and retry paths.

**Not verified:** anything touching a real camera, a real Odoo, or Render. Those need
the shop LAN.
