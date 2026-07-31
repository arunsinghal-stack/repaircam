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
selection. If accountability is 60 days, three benches at 8h needs about
**2.6 TB** of archive — and it stops growing, because 60-day-old footage leaves
as new footage arrives.

That is a completely different purchase from "keep everything for ever", which
at the same rate is 11 TB in year one and more every year after.

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

Sizing at those numbers, three benches: repair footage settles at roughly
**30 days' worth** on the archive and packing at 45, so the archive stops
growing rather than climbing for ever.

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

Nothing in RepairCam prunes the archive — deliberately, because the archive is
the copy that survives. When it fills, either somebody deletes from it by hand,
or a policy is added. Worth deciding before it is urgent rather than after.

---

## A gap worth naming now

Retention today is purely by age. There is **no way to mark a clip "keep this
one"** — the training example, the disputed repair, the one that becomes a
dataset sample. Under `delete_after_archive`, an important clip ages out
exactly like every other.

That is fine while nothing is being deleted, which is the state today. It stops
being fine the day retention is switched on. The fix is small — a flag on the
catalogue row that `prune` refuses to delete, and a button in the library — and
it should land **before** `delete_after_archive: true`, not after.

---

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
5. **Add the "keep this one" flag** before deletion is ever enabled.
6. **Then set the recorder's own `keep_days` (3–7) and
   `delete_after_archive: true`**, and watch the first prune closely. The
   per-source windows above govern the archive's size; the recorder itself can
   only hold days, whatever they say.

Steps 1 and 2 cost nothing and unblock everything else. Step 4 is the one that
ends the real exposure today: **right now every clip the shop has exists on one
laptop, and nothing else.**
