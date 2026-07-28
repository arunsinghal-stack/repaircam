# Phase 5 — the saar-seva integration contract

What the two sides promise each other. RepairCam's half is built (`saarseva.py`,
`trigger.py`); **saar-seva's half is not** — the two endpoints below have to be added
to `arunsinghal-stack/saar-seva-app` before any of this does anything.

Design reasoning is in [PLAN.md §6](PLAN.md). This file is the precise contract.

---

## Why RepairCam polls

saar-seva runs on Render, in the cloud. The cameras and the recorder are on the shop
LAN behind a home router, deliberately unreachable from the internet. **The cloud
cannot open a connection into the shop**, so RepairCam asks saar-seva what is
happening, roughly every 5 seconds. There is no webhook and there cannot be one
without exposing the shop, which the whole design refuses to do.

```
technician taps Start on /trc/time (saar-seva, existing screen)
        │
        ├─ saar-seva records the time entry in Odoo   (already works today)
        │
RepairCam ── GET /trc/active ────────────────────────►  every ~5s
        │    ◄── WC2 is running MO/00042, "Screen replacement", IMEI 3501…
        │
        └─ Recorder.start(labels)
        …
RepairCam ── GET /trc/active ────────────────────────►
        │    ◄── WC2 no longer listed
        │
        ├─ Recorder.done()  → one clip, sidecar, catalogue row
        └─ POST /trc/recordings {mo_name, url, …}
                 │
                 └─ saar-seva writes the LINK into the Odoo MO chatter
```

---

## Endpoint 1 — `GET /trc/active`

**Purpose.** Tell RepairCam which operations are being worked on right now.

**Auth.** `Authorization: Bearer <service token>`. A service token, *not* a
technician login — this is machine-to-machine and must not depend on a person being
signed in.

**Query parameters.** Optional `work_centers=WC2,WC3` so the recorder only asks about
benches that actually have a camera.

**Response.** `200 OK`, JSON. Either shape is accepted — RepairCam tolerates both, so
saar-seva can use whichever fits its existing handlers:

```json
{
  "active": [
    {
      "work_center": "WC2",
      "job_id": 1234,
      "mo_name": "WH/MO/00042",
      "workorder_id": 987,
      "operation": "Screen replacement",
      "device": "Redmi Note 12",
      "imei": "350123456789012",
      "technician": "Ramesh",
      "started_at": "2026-07-27T10:15:00Z"
    }
  ]
}
```

…or a bare `[ … ]` array of the same objects.

### Field requirements

| Field | Required | Notes |
|---|---|---|
| `work_center` | **yes** | Must match the key in `cameras.yaml` (`WC2`). This is the only field RepairCam cannot work without — it is how a clip finds its camera. |
| `mo_name` | strongly wanted | The Odoo MO. Without it the clip is unlabelled and nearly useless for the dataset. |
| `operation` | strongly wanted | One clip per operation is the whole model. |
| `workorder_id` | wanted | Used as the operation's identity — see *Identity* below. |
| `device`, `imei` | wanted | From the existing `stock.lot` lookup in `backend/app/odoo.py`. |
| `technician` | optional | |
| `job_id`, `started_at` | optional | Recorded in the sidecar if present. |

Unknown extra fields are ignored, so saar-seva can add to this freely.

### Identity — the part that must be right

RepairCam decides "is this the same operation as last poll?" from, in order of
preference:

1. `workorder_id`
2. `mo_name` + `operation`

**This identity must be stable for the life of one operation.** If it changes between
polls, RepairCam sees the old operation end and a new one begin — it will file the
first clip and start a second. If two genuinely different operations reuse an
identity, their footage merges into one clip.

**Empty list means nothing is being worked on**, and RepairCam finishes whatever it
was recording. That makes this endpoint the *authority* on what is running: if it is
wrong, the video is wrong.

---

## Endpoint 2 — `POST /trc/recordings`

**Purpose.** Hand a finished clip's link to saar-seva, which writes it into the Odoo
**MO chatter**. The chatter gets a link only — never the video, never an attachment.

**Request.**

```json
{
  "mo_name": "WH/MO/00042",
  "workorder_id": 987,
  "work_center": "WC2",
  "operation": "Screen replacement",
  "url": "http://192.168.0.50:8080/clip/41",
  "duration_s": 412.5,
  "recorded_at": "2026-07-27T10:15:00+00:00",
  "recording_id": 41,
  "imei": "350123456789012"
}
```

`url` is a **LAN address**. It opens inside the shop and nowhere else, which is the
point: the chatter carries a pointer, and the footage never leaves the premises.

**Response.** Any `2xx` means accepted. RepairCam then sets `link_posted = 1` on the
recording and never sends it again.

**Idempotency is saar-seva's job.** RepairCam retries on failure, so the same
`recording_id` can arrive twice — for example if the chatter write succeeds but the
response is lost. saar-seva should treat `recording_id` as a key and not post a
duplicate line into the chatter.

---

## How RepairCam behaves when saar-seva is unreachable

Render sleeps, home internet drops, tokens expire. **None of that may stop the shop
recording.**

- Poll failures are logged and retried at the next tick. Nothing else changes.
- A bench already recording **keeps recording** through an outage — the trigger is a
  convenience, never a dependency.
- Recording always remains startable by hand from the web UI, whatever the trigger is
  doing.
- Unposted links are not lost: `link_posted` stays `0` and the clip is retried later.
- If the poll returns garbage, RepairCam ignores that tick rather than acting on it —
  a malformed response must not be read as "nothing is running", which would end every
  recording in the shop.

---

## What saar-seva has to build

1. `GET /trc/active` — read the open time entries it already creates on
   `POST /trc/jobs/{job}/time/start`, join to the MO and work order, and add the
   device/IMEI from the existing `stock.lot` lookup.
2. `POST /trc/recordings` — write one chatter line on the MO, keyed on `recording_id`
   so retries do not duplicate.
3. A service token for the recorder, checked on both endpoints.

Both reuse the existing `backend/app/odoo.py` client. Known real endpoints on that
side today, for reference: `POST /trc/jobs/{job}/time/start` | `/time/stop`, and
`POST /trc/jobs/{job}/workorders/{wo}/finish`.

---

## Open question for the saar-seva side

**What ends an operation?** RepairCam treats *disappearing from `/trc/active`* as the
end. Whether that means Stop was pressed, or only Done/finish, is saar-seva's call —
but it decides where clips get cut. If Stop removes an entry, a technician pausing for
a part produces two clips rather than one paused recording, which loses the
one-clip-per-operation property.

**Recommendation:** keep an operation in `/trc/active` across a Stop, and drop it only
on finish. RepairCam's own Pause/Continue then maps onto it naturally.
