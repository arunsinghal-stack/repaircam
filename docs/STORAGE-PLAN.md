# Storage — the plan

The machinery is built and tested: a free-space guard, a verified archive copy,
and retention that only ever deletes what it has confirmed exists elsewhere.
None of it is switched on, because there is nowhere to copy to.

What is left is not code. It is three decisions, and they turn on one number
nobody has picked yet: **how long the shop needs to be able to look back.**

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

And to keep `keep_days: 30` locally, three benches at 8h would need **1.3 TB on
the recorder**. The default in `storage.example.yaml` does not fit this machine
and never will. Whatever else is decided, **`keep_days` on the recorder has to
be small — 3 to 7 days** — and the archive is what holds anything longer.

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

**A correction, because an earlier version of this document had it wrong.**
`keep_days` bounds the **recorder**, not the archive. Nothing in RepairCam ever
removes a file from the archive — `prune` reads the archived copy to verify it
and then leaves it alone. So today the archive does **not** settle at a month's
worth. It grows for ever, at the full rate in the table above: three benches at
8h is ~950 GB a month arriving and nothing leaving, which is 11 TB in year one
and 11 TB more every year after.

Closing that is the last open piece of the lifecycle — see "Where the lifecycle
stops" below. Until it is closed, size the archive for **how long you are
willing to go before doing something about it**, not for the retention window.

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

`prune` runs one pass per window, so each kind of footage expires on its own
clock. A source not named there falls back to `keep_days` — which is also what a
clip somebody started by hand in RepairCam gets, since it belongs to no
integration and no integration's window fits it.

To be exact about what those numbers do: after 30 days a repair clip stops
taking up room **on the recorder**. It still takes up the same room on the
archive, for ever.

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

worker ->  older than its source's window AND verifiably still at the archive
       ->  local .mp4 and .json deleted, local_deleted = 1
           the row stays: it is the only record of where the footage went
```

Three things are worth noticing about that shape:

- **The archive mirrors the same relative path.** `recordings/2026-07-29/x.mp4`
  is at the same place under `archive_dir`, so the archive is browsable in a
  file manager and means something without the database.
- **The database is an index, not the truth.** A clip plus its sidecar is a
  complete dataset sample on its own. `prune` re-checks the archived file at the
  moment of deletion precisely because the row is not evidence.
- **Nothing prunes the archive.** Only the recorder's own copy is ever removed.

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

Seven stages. Five are built, one has never run, and the last does not exist.

| # | Stage | State |
|---|---|---|
| 1 | Record — segments per Start/Stop | **live** |
| 2 | Join, label, sidecar, catalogue | **live** |
| 3 | Copy to the archive, verified | built; never run against a real disk |
| 4 | Remove the recorder's copy once its window passes | built; switched off |
| 5 | Serve a removed clip from the archive, so its Odoo link still works | built |
| 6 | **Remove anything from the archive** | **does not exist** |
| 7 | **Take curated clips out for training / the AI dataset** | **does not exist** |

Stages 1–5 make a loop that keeps the *recorder* healthy for ever. **They do not
close the lifecycle**, because everything the shop films ends up in one place
that nothing ever empties.

### Stage 6 — the archive needs an end

Three ways to give it one, and they are not exclusive:

1. **By hand, on a reminder.** Once a quarter, somebody deletes the oldest month
   from the archive. No code. Works, until the quarter somebody forgets.
2. **A second retention pass at the archive** — the same rule `prune` already
   applies locally, with a longer window and the same refusals: never a clip
   marked keep, never one that has no third copy if there is one. This is the
   natural extension, and the machinery for it already exists.
3. **Tiering.** The archive holds the retention window; anything older that is
   still wanted moves to something slower and cheaper — a second disk that lives
   off-site and is plugged in occasionally.

**Nothing here is urgent yet**, because the archive does not exist. It becomes
urgent about a month after it does. Deciding it now costs an afternoon; deciding
it when the archive is full costs whatever gets deleted in a hurry.

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
6. **Then set the recorder's own `keep_days` (3–7) and
   `delete_after_archive: true`**, and watch the first prune closely. The
   per-source windows above govern the archive's size; the recorder itself can
   only hold days, whatever they say.

Steps 1 and 2 cost nothing and unblock everything else. Step 4 is the one that
ends the real exposure today: **right now every clip the shop has exists on one
laptop, and nothing else.**
