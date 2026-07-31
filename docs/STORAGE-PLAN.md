# Storage — the plan

The machinery is built and tested: a free-space guard, a verified archive copy,
retention on the recorder, and retention at the archive. The whole lifecycle
now has an end.

What is left is not code. It is switching it on, in order, and one decision
that only the shop can make: **how long the shop needs to be able to look
back.**

---

## The one thing to get right: there are TWO windows

This is where the money and the mistakes are, so it comes first.

| | Setting | What it means | Who decides it |
|---|---|---|---|
| **Recorder** | `keep_days_local` | How long a clip stays on the laptop after it is safely copied | **Arithmetic.** The disk is 204 GB; three benches use ~43 GB a day. That is about **5 days**, and no setting changes it. |
| **Archive** | `keep_days` + `keep_days_by_source` | How far back anyone can look — the point at which the footage **stops existing** | **Policy.** How long after a repair can a customer come back about it? |

They used to be one number, and that was a bug waiting to happen. Putting the
shop's 30-day policy on the recorder means: the disk fills around day 5, the
free-space guard refuses Start — correctly, it is protecting footage — and
`prune` cannot help, because nothing is 30 days old yet. **Recording stops
mid-week while retention reports itself working perfectly.**

Nothing is lost when the recorder's window expires. The archive still has the
clip, and the links in the Odoo chatter still play it: the clip page falls back
to the archive.

    storage:
      keep_days_local: 5          # the laptop. arithmetic.
      keep_days_by_source:        # the archive. policy.
        repair: 30
        packing: 45

There are two switches for the same reason, and they are not the same act:

- `delete_after_archive` frees the laptop. A verified second copy exists, and is
  re-checked at the moment of deletion. Nothing is lost.
- `delete_from_archive` **ends the footage.** There is no third copy behind it.

---

## What it costs

One bench-hour of recorded video is about **1.8 GB** (4096 kbps, copied not
re-encoded). A "bench-hour" is one bench with a timer running for an hour — not
a bench that exists.

| Benches | Recorded hours per bench per day | Per day | Per month | Per year |
|---:|---:|---:|---:|---:|
| 1 | 4 | 7 GB | 158 GB | 1.9 TB |
| 1 | 8 | 14 GB | 317 GB | 3.7 TB |
| **3** | **4** | **22 GB** | **475 GB** | **5.6 TB** |
| **3** | **8** | **43 GB** | **950 GB** | **11.1 TB** |
| 5 | 8 | 72 GB | 1.5 TB | 18.6 TB |
| 10 | 8 | 144 GB | 3.1 TB | 37.1 TB |

*(22 working days per month.)*

Nobody knows yet which row the shop is on. Technicians only clock time against
operations, so real recorded hours will be well under a full working day — but
that is a guess until it is measured, and the difference between the two rows
for three benches is **5.6 TB a year**.

## The recorder cannot be the answer

204 GB free today, three benches:

| Recorded hours per bench per day | How long before recording stops |
|---:|---:|
| 4 | 9 days |
| 8 | 5 days |

And to keep 30 days locally, three benches at 8h would need **1.3 TB on the
recorder**. That is why `keep_days_local` exists and defaults to 7: whatever
else is decided, **the recorder's own window has to be small — 3 to 7 days** —
and the archive is what holds anything longer.

---

## The three decisions

### 1. How long must footage be available? *(only the shop can answer)*

This is the number everything else follows from. It is worth splitting, because
the three reasons RepairCam exists have different answers:

| Purpose | Realistic retention |
|---|---|
| **Accountability** — settling "what happened on this repair" | As long as a dispute can arrive. 30–90 days? |
| **Training** — showing a new technician how a job is done | A curated handful, kept indefinitely |
| **AI dataset** — labelled footage to learn from | A curated subset, kept indefinitely |

Only the first applies to *everything filmed*. The other two are a small
selection.

**This used to be the archive's open end, and it is now closed.** An earlier
version of this document said `keep_days` bounded the archive; then a correction
said nothing ever removed a file from the archive, and that was true — the
archive grew for ever at ~950 GB a month with nothing leaving, 11 TB in year one
and 11 TB more every year after.

`prune_archive` closes it. With `delete_from_archive: true` the archive settles
at roughly *window × daily rate* — about **1.4 TB** at three busy benches on a
30/45-day policy — plus whatever is marked **keep**, which never expires and is
the only part that still grows without limit.

**The error is not symmetric, so do not split the difference.** Too long costs
money and can be shortened at any time. Too short cannot be undone — the
footage is gone, and it is gone precisely for the repairs old enough that
somebody is now arguing about them. Pick a window longer than seems necessary
and shorten it once there is a year of evidence about when disputes actually
arrive.

The question that decides the number is not a storage question at all:
**how long after a repair can a customer still come back about it?** The
retention window is that, plus a margin. A shop that warrants a screen for 90
days cannot hold footage for 30 and expect it to be there when it is needed.

**Decided (2026-07-29): repair 30 days, packing 45 days.** Both configurable:

```yaml
storage:
  keep_days: 30            # fallback, and clips started by hand
  keep_days_by_source:
    repair: 30
    packing: 45
```

`prune_archive` runs one pass per window, so each kind of footage expires on its
own clock. A source not named there falls back to `keep_days` — which is also
what a clip somebody started by hand in RepairCam gets, since it belongs to no
integration and no integration's window fits it.

To be exact about what those numbers do now: after 30 days a repair clip is
**gone**. Not moved, not on the other disk — gone. That is the intent, and it is
why the switch enabling it is separate from the one that frees the laptop, and
why a clip marked **keep** is excluded in the SQL rather than in the code that
does the deleting.

### 2. Where does the second copy live?

Cloud is out — the design has always been that video never leaves the shop, and
that is what keeps customer devices and faces off the internet.

| Option | For | Against |
|---|---|---|
| **USB disk on the recorder** | Cheapest. Works today with one line of config. | Same room, same table. A theft or a fire takes both copies. |
| **NAS on the shop LAN** | Survives the laptop being stolen or dying. Several machines can reach it. | Costs more; someone has to keep it running. |
| **Both** | The recorder keeps a few days, the NAS keeps the retention period, a USB disk is rotated off-site occasionally | Most work. |

RepairCam does not care which — `archive_dir` is a path. A mounted NAS share and
a USB disk look identical to it. **An unmounted NAS does not**, and is refused
rather than archived into, so a disk that quietly unmounts cannot cause footage
to be deleted against an empty folder.

### 3. What happens when the archive fills?

This is the open one. See below.

---

## How it works today

The life of one clip:

```
Start  ->  segments/WC2/20260729-104200/seg-001.mp4     one file per Start->Stop
Stop                                    seg-002.mp4
Done   ->  ffmpeg concat (stream copy, no re-encode)
       ->  recordings/2026-07-29/WC2_20260729-104200_wh-mo-42_screen.mp4
       +   the same name .json  — the sidecar that makes it self-describing
       ->  a catalogue row holding the path RELATIVE to the data directory
       ->  the empty segment folder is removed

worker ->  copy to  <archive_dir>/recordings/2026-07-29/<same name>.mp4
           written as .part, size-checked, then renamed
       ->  archived_at + archive_path on the row

worker ->  older than keep_days_local AND verifiably still at the archive
       ->  local .mp4 and .json deleted, local_deleted = 1
           the row stays: it is the only record of where the footage went
           links still play — the clip page falls back to the archive

worker ->  older than its SOURCE's window (repair 30, packing 45), not "keep"
       ->  the archived .mp4 and .json deleted, archive_deleted = 1
           the footage is now gone. the row stays, holding the date it went
```

Three things are worth noticing about that shape:

- **The archive mirrors the same relative path.** `recordings/2026-07-29/x.mp4`
  is at the same place under `archive_dir`, so the archive is browsable in a
  file manager and means something without the database.
- **The database is an index, not the truth.** A clip plus its sidecar is a
  complete dataset sample on its own. `prune` re-checks the archived file at the
  moment of deletion precisely because the row is not evidence.
- **The archive has its own, longer window**, run by `prune_archive`. That pass
  is where footage finally ends; everything before it only ever moves a copy.

## Two gaps — both now closed

Retention today is purely by age. There is **no way to mark a clip "keep this
one"** — the training example, the disputed repair, the one that becomes a
dataset sample. Under `delete_after_archive`, an important clip ages out
exactly like every other.

**Built.** A `keep` column on the catalogue row and a **Keep this clip** button
on the clip page. The exclusion lives in the SQL `prune` reads from, not in
`prune` itself, so no future caller can forget it. The status page and
`cli storage` count kept clips, because they are the part of the archive that
only ever grows.

### 2. A pruned clip's Odoo link breaks

`clip_path()` resolves a catalogue row only under the data directory, and
`aborts` with *"the video file for this recording is missing from disk"* when it
is not there. The archived copy is never consulted.

So the first prune silently turns every older chatter link in Odoo into a dead
end — for footage that still exists, a few centimetres away on the archive disk.
Nobody would find that until they went looking for an old repair, which is the
one moment retention exists to serve.

**Built.** `resolve_clip()` tries the recorder's own copy, then the archive.
Both are checked against a permitted root — and the archived one against the
archive configured *now*, so a path recorded when `archive_dir` pointed
elsewhere is not something to start serving files from. The clip page says when
it is playing from the archive, and when the archive is configured but
unreadable it says *that* instead of "missing from disk", which would send
somebody looking for footage that is fine on a disk that is merely unmounted.

---

## Where the lifecycle stops

Seven stages. Six are built, and the last does not exist.

| # | Stage | State |
|---|---|---|
| 1 | Record — segments per Start/Stop | **live** |
| 2 | Join, label, sidecar, catalogue | **live** |
| 3 | Copy to the archive, verified | built; switched on when `archive_dir` is set |
| 4 | Remove the recorder's copy after `keep_days_local` | built; switched off |
| 5 | Serve a removed clip from the archive, so its Odoo link still works | built |
| 6 | Remove footage from the archive when its window passes | built; switched off |
| 7 | **Take curated clips out for training / the AI dataset** | **does not exist** |

Stages 1–6 are a closed loop: footage arrives, is copied, ages off the laptop,
then ages out of the archive. Nothing grows without bound except clips marked
**keep**, which is the point of the mark.

### Stage 6 — the archive's end, as built

`prune_archive` is the same shape as `prune`, with stricter refusals, because it
is the only deletion in RepairCam with nothing behind it:

- the archive must be **mounted**. An absent directory is the shape of an
  unmounted disk, and marking rows deleted against one would declare footage
  gone while it sat safe in a drawer;
- `delete_from_archive` must be on — a different switch from the one that frees
  the laptop, because it is a different act;
- the clip must be past **its own source's** window;
- it must not be marked **keep** (enforced in the SQL, so no future caller can
  forget);
- and its archived path must be **under the archive configured now** — a row
  written when `archive_dir` pointed at another disk names a file on that disk.

The local copy goes with it if it is still there. The window is over; leaving a
copy on the recorder would also leave one that nothing could ever remove
afterwards, since `prune` only deletes clips it can verify at the archive.

The catalogue row outlives its own footage, holding the date it went. An Odoo
link from eight months ago still resolves to it, and the clip page says *"This
footage has passed its retention window and has been deleted"* — an answer,
where a dead end would send somebody hunting for a file nobody will ever find.

Two things it deliberately does **not** do, and they remain available if the
shop ever wants them: deleting the oldest month by hand on a reminder, and
tiering old footage to a slower off-site disk instead of deleting it.

### Stage 7 — clips that leave for a reason

"Training" and "AI dataset" are in the project's purpose, and neither is served
by footage sitting in an archive. Both imply somebody *selects* clips and takes
them somewhere — a training folder, a labelled dataset.

The **keep** flag is the beginning of that: it marks which clips matter. What
does not exist is any way to get them out as a set. A clip and its sidecar are
deliberately self-describing, so an export is a small piece of work — but it is
work, and until it is done "keep" only means "do not delete", not "use this".

## What to do, in order

1. **Measure for a week.** Recording is live and the guard protects the disk, so
   the shop can simply run. Then `cli storage` gives the real GB/day for this
   shop rather than the range above. A week of real numbers is worth more than
   any estimate here.
2. **Decide the accountability window** — the single number in decision 1.
3. **Buy for that number**, doubled. Storage estimates are always low, and a
   disk that is 80% full is a disk that is about to be a problem.
4. **Set `archive_dir`**, leave `delete_after_archive: false`. Clips get a
   second home and nothing is deleted. Watch it for a few days; open the archive
   and play a clip from it.
5. ~~Fix both gaps above~~ — **done**. Keeping a clip, and serving a pruned one
   from the archive, both work.
6. **Then set `keep_days_local` (3–7) and `delete_after_archive: true`**, and
   watch the first prune closely. Open an older clip from the library
   afterwards: it should still play, and say it is playing from the archive.
7. **Last, and only when the archive is close to the size you want it to
   settle at, set `delete_from_archive: true`.** This is the irreversible one.
   Before it: mark anything worth keeping, because kept clips are the only ones
   it will not touch.

Steps 1 and 2 cost nothing and unblock everything else. Step 4 is the one that
ends the real exposure: **until it is done, every clip the shop has exists on
one laptop and nothing else.** Step 7 is the only one that can lose footage, and
there is no hurry about it — an archive with nothing expiring is merely
expensive, and it can be switched on any time.
