# Packing video — plan

Record the packing of an order, and put the video's link on the Delivery Order's
Odoo chatter. Same idea as the repair-bench recording (Phase 5), different job.

**Both halves are built.** saar-seva's are in
`arunsinghal-stack/saar-seva-app` branch `claude/packing-video-endpoints`;
RepairCam's are in `saarseva.py` / `trigger.py`. Not yet run against a real
camera or a real Odoo.

---

## What the packer does

1. Opens their packing job in saar-seva (already exists).
2. Presses **Record** — RepairCam starts filming the packing bench.
3. Presses **Stop** — the clip is saved and its link goes to the Odoo DO chatter.
4. Can do this **as many times as needed** on the same order. Each Start→Stop is
   its own clip, and each gets its own line in the chatter.

Recording is deliberately *not* tied to the packing status. A packer can film a
box being sealed, stop, film another box, stop. It is an evidence button, not a
workflow step.

---

## Which picking gets the link

There are two shapes an order can take after the SO is confirmed:

| | Pickings created | Where the link goes |
|---|---|---|
| **One-step** | one `outgoing` picking (WH/OUT) | that one |
| **Two-step** | `internal` (WH/PICK) **+** `outgoing` (WH/OUT) | the **OUT** only |

**The rule is one line: always post to the *outgoing* picking.** In the two-step
case the PICK is an internal transfer — it is not the customer's delivery, and a
video on it would be filed against the wrong document.

saar-seva already knows how to tell them apart. `_is_outgoing()` exists twice
(`routers/warehouse.py:1917`, `routers/admin.py:4437`) and checks
`picking_type_code == "outgoing"`, falling back to the type name for Odoo builds
that do not expose the code. **Reuse it — do not write a third copy.**

### The link can be posted immediately

The outgoing picking is created when the SO is confirmed, in both shapes. In the
two-step case it sits in `waiting` until the PICK completes, but it **exists**,
so it can be found and posted to during packing.

This matters because `PackingJob.odoo_do_picking_id` is only filled in at
*dispatch* (`warehouse.py:8940`) — long after packing. Waiting for that would
leave the video invisible in Odoo for hours or days. So:

- **Resolve the outgoing picking at Stop time** from the order's
  `sale.order.picking_ids` (`odoo.py:2834` already reads these) and post at once.
- **If it cannot be found**, keep the clip unposted and retry later — the same
  mechanism the repair flow already uses. The footage is never lost; only its
  chatter line is late.

---

## Part A — saar-seva backend

### A1. New table `packing_recording`

`PackingJob` has single-value columns and cannot hold several recordings, so
this needs its own table. It mirrors `repair_time_log`, which the repair flow
already keys on.

| column | why |
|---|---|
| `id` | identity of one recording session — what RepairCam keys on |
| `packing_job_id` | FK to `packing_job` |
| `station` | which packing bench/camera (see Part C) |
| `started_at`, `ended_at`, `active` | the Start/Stop state RepairCam polls |
| `started_by_exec_id` | who filmed it |
| `repaircam_recording_id`, `repaircam_url` | the clip, once it exists |
| `odoo_picking_id`, `odoo_picking_name` | the OUT it was posted to |
| `posted_at` | set once the chatter line is written — makes retries idempotent |

Added via the existing `_COLUMN_PATCHES` / `Base.metadata.create_all` path, like
every other table in that repo.

### A2. The two buttons

```
POST /warehouse/packer/jobs/{job_id}/record/start   -> creates a row, active=true
POST /warehouse/packer/jobs/{job_id}/record/stop    -> closes it, active=false
```

Both gated on `_require_packer`, the same dependency the rest of that screen
uses. `start` refuses if that station already has an active recording.

### A3. What RepairCam polls

```
GET /pack/active        (auth: REPAIRCAM_API_KEY, already merged in PR #461)
```

Returns the recordings currently `active`, each with: `recording_id` (the
`packing_recording.id`), `station`, `order_ref`, `so_name`, `packing_job_id`,
`ship_to_name`, `started_at`.

Kept separate from `/trc/active` rather than merged into it: the two have
different identities and different labels, and one endpoint answering for two
unrelated jobs gets confusing fast.

### A4. Where the link lands

```
POST /pack/recordings   {recording_id, url, duration_s, recorded_at}
```

1. Find the `packing_recording` row.
2. Already has `posted_at`? Return `{"ok": true, "duplicate": true}`, write nothing.
3. Resolve the order's **outgoing** picking (reusing `_is_outgoing`).
4. `odoo.post_do_chatter(picking_id, note)` — the helper already exists at
   `odoo.py:6791` and already handles the Odoo 19 `message_post` marshalling fault.
5. Only on success, store the url + picking id + `posted_at`. **On failure return
   502 and store nothing**, so the retry is not mistaken for a duplicate.

---

## Part B — saar-seva frontend

On the packer's job screen: a **Record** / **Stop** button and a short list of
the clips already taken for this job, each a link. Small change — the screen and
its API calls already exist.

---

## Part C — RepairCam

Small changes; the recorder itself is untouched.

- **`cameras.yaml` needs no new field.** Packing benches are Odoo work centres,
  exactly like repair benches, so the existing `odoo_workcenter_id` covers both
  and several packing stations work from day one.
- **The trigger polls `/pack/active` as well as `/trc/active`** and merges them:
  one bench map, one reconcile. A saar-seva without the packing endpoints
  answers 404, which is treated as "not deployed yet" so repair recording
  carries on; any other packing failure still aborts the tick, because a
  partial picture must never read as "nothing is running".
- **The catalogue remembers the source.** `recordings.source` ('repair' /
  'packing') and `source_ref` (saar-seva's own id) are stored on the row, so a
  restart before the link is posted still knows which endpoint it belongs to.
  Schema v2; existing databases are migrated with ALTER TABLE on open.

Everything else — capture, catalogue, sidecars, the never-lose-footage rules,
the link retry — is reused as-is.

---

## Part D — how it gets verified

Same approach as Phase 5, which caught three real contract bugs before they
reached the shop:

- saar-seva side against a live app with SQLite and Odoo mocked: auth, the active
  list, **one-step vs two-step picking selection**, idempotency, and that a failed
  Odoo write leaves the row unposted so the retry works.
- RepairCam's real client driven against the real endpoints in one process:
  Record → film → Stop → clip filed → link posted → chatter written once.
- RepairCam's own suite stays green (141 tests).

The two-step case deserves an explicit test with **both** pickings present,
asserting the note lands on the OUT and never on the PICK.

---

## Open question raised by this correction

**Is the e-way bill one of the required documents?** The owner named four; the
code also has `courier_eway_number` and an `eway_generator` setting (SAAR or the
courier). If an e-way bill has to be in the box too, it joins the list — and if
the courier generates it, "ready" may depend on something SAAR does not hold.
Not assumed either way.

## Open questions

1. **How many packing stations?** The plan assumes one, named in `cameras.yaml`.
   More than one means the packer must say which bench they are at — a UI change
   and a field on the recording.
2. **Split orders.** A `PackingJob` can be per-warehouse (`so_ref_id`), so an
   order can have several outgoing pickings. The plan posts to the OUT belonging
   to that job's warehouse. Confirm this is what you want.
3. **Retention.** Packing clips land on the same disk as repair clips, and there
   is still no retention job. Packing video will make it fill faster.

---

## Sequencing

This is a second integration on a recorder that **has not yet captured a single
real frame**. If the focus test fails or real MP4 handling misbehaves, both
integrations get reshaped.

Recommended order:

1. The three bench checks (focus, a real 20s clip, a real pause-and-resume) — 30 minutes.
2. Phase 5 switched on and proven with one real repair.
3. This.

---

# Correction: we have been filming the wrong stage

**Raised 2026-08-01 by the owner. Nothing built yet.**

The plan above puts Record on the packer's screen while the job is `packing`.
That is the wrong moment. The physical boxing — invoice in the box, AWB on the
outside, box sealed — happens **after logistics has punched the shipment and
requested the invoice.** What the current button films is serial verification,
which is not what anyone will want to look at when a customer says the box was
short an item.

## What the workflow actually is

| # | Who | What | `PackingJob` |
|---|---|---|---|
| 1 | packer | Start packing | `packing` |
| 2 | packer | serials into boxes, labels printed, complete | `packed` |
| 3 | **dispatcher** | courier cost + COD per shipment, **request invoice** | `invoice_requested_at` set |
| 4 | accounts | posts the invoice | invoice number exists |
| 5 | **packer or dispatcher** | **the actual boxing** | still `packed` |
| 6 | dispatcher | AWB, dispatch complete | `dispatched` |

**Step 5 is what needs filming.** Steps 1–2 do not.

## The part that makes this more than a one-line change

At step 5 **there is no screen with a Record button on it.**

- The packer's queue (`GET /packer/jobs`) filters `status IN ('ready','packing')`.
  The job vanished from the packer's screen at step 2.
- The Dispatch screen does list `packed` jobs — but it is a different role and
  has never had a camera panel.

So the gate cannot simply be moved; something has to *show* the job during the
window we want filmed.

## The window — documents, not a status

**Corrected again, 2026-08-01.** "Invoice requested" is not the gate either.
Requesting an invoice is asking accounts for one; the box cannot be packed until
the paperwork physically exists, because the paperwork goes *in and on the box*:

> AWB number, invoice number, the invoice document itself, and the box label.

All four are produced at the logistics stage, and the order matters — the
dispatcher requests the invoice *in order to get a number with which to
generate the AWB*. So `invoice_requested_at` is the beginning of that stretch,
not the end of it. Gating on it would have re-opened Record too early, just
less early than before.

| Document | Where it lives | Ready when |
|---|---|---|
| **AWB number** | `Shipment.awb_number` | set on every shipment — **except** `is_self_pickup`, where the customer collects and there is no courier at all |
| **Invoice number** | `PackingJob.odoo_invoice_name` / `odoo_invoice_move_id` | accounts has POSTED it. `invoice_requested_at` only means somebody asked |
| **Invoice document** | fetched from Odoo by move id (`get_invoice_pdf_b64`) | no separate field: **the number existing is the document existing** |
| **Label** | `PackingBox.label_printed_at` | printed on every box |

Start is allowed when all of those hold and the job is still `packed`. After
`dispatched` the box has left the building.

**Stop stays ungated**, as it already is. A job that reaches `dispatched` while
the camera is running must still be stoppable, or that bench films for ever.

### One definition, in one place

The button's rule and the screen's rule must be the same rule. Write it once:

```python
def packing_ready(db, job) -> tuple[bool, list[str]]:
    """Can the box actually be packed yet, and if not, what is missing?"""
```

used by three callers that would otherwise each grow their own copy:

1. `packer_record_start` — refuses, **naming the missing document**;
2. the packer and dispatch queue payloads — so the row can say *"waiting for:
   AWB"* rather than showing a dead button;
3. the panel's enabled state.

Two hand-written copies of a four-part condition will drift, and the drift is
invisible: the button works, the label lies, or the reverse. This project has
already paid for that twice — `link_base` reported as set when it pointed
nowhere, and a bench count reported healthy while two benches were gone.

**A refusal must name the missing document.** "You can't record yet" sends
somebody to find a manager. "Waiting for the AWB on shipment 2" sends them to
the dispatcher.

### Self-pickup is not a missing AWB

`is_self_pickup` means the customer collects at the counter: no courier, no AWB,
by design. Treating a blank AWB there as "not ready" would make self-pickup
orders permanently unfilmable, and nobody would connect the two. The dispatch
gate already draws this distinction; the recording gate must draw the same one.

## Who presses it

**Either the packer or the dispatcher — decided 2026-08-01, it varies by day.**
So both screens carry the panel and both roles may record:

- the record endpoints accept packer **or** dispatcher, the way
  `save_bench_workcenters` already accepts technician **or** packer;
- **dispatchers must therefore be mappable to a work centre** in Admin → TRC
  settings → People, exactly as packers became mappable in saar-seva PR #473.
  An unmapped dispatcher pressing Record gets the same "no bench" refusal a
  packer does, and that refusal has to name the fix.

The "one camera cannot film two jobs" guard already exists, and now earns its
keep for a second reason: two *people* can reach for the same bench.

## What has to change

**saar-seva backend**
1. `packing_ready()` — the one definition above — plus
   `packer_record_start` replacing its `status != "packing"` refusal with it,
   quoting whichever documents are missing. Add a refusal for `dispatched`.
2. Both record endpoints and the recordings/camera-light endpoint: accept
   packer **or** dispatcher.
3. `GET /packer/jobs`: also return jobs that are `packed` and not yet
   dispatched, under a clearly separate heading, each carrying its
   `packing_ready` verdict. A packer who sees a job they already completed
   sitting back in their queue with no explanation will reasonably think
   something went wrong — and one that is waiting on a document needs to say
   which, or it looks broken rather than pending.
4. People mapping: include dispatchers.

**saar-seva frontend**
5. `Packing.jsx`: the panel appears in the new window only, not during
   `packing`.
6. `Dispatch.jsx`: the same panel, same component.

**RepairCam** — nothing. The recorder polls `/pack/active`, which is driven by
`PackingRecording` rows, not by job status. The clip's `source` stays
`packing`, so the 45-day archive window still applies. The link still goes to
the **outgoing** Delivery Order's chatter.

## What this costs if we get it wrong

Filming step 1–2 instead of step 5 produces clips that look like evidence and
answer no dispute anybody actually raises. Filming nothing at all — which is
what happens if the gate moves without step 3 — is at least honest, and the
status page would say so. **Of the two, do not ship the gate change without the
queue change.**

## Sequencing

Backend gate + roles + queue first (it is testable on staging with no camera),
then the two screens, then the People mapping. Nothing here touches recording
that already works: repair benches are unaffected, and packing video has never
run in the shop, so there is no in-flight behaviour to preserve.
