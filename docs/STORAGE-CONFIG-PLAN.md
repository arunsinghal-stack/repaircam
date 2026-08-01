# Central storage config — plan

Set the retention windows from saar-seva's admin panel instead of editing
`storage.yaml` over SSH, the same way the camera list already works.

**Phases 1-5 are BUILT (2026-08-01). Only phase 6 — the admin screen itself —
is missing, so the endpoints exist and no browser calls them.** Written after
the file below was created by hand on the shop recorder.

---

## The file today

```yaml
storage:
  archive_dir: "/mnt/backup-drive/RepairCam"
  keep_days_local: 5
  keep_days: 30
  keep_days_by_source:
    repair: 30
    packing: 45
  delete_after_archive: false
  delete_from_archive: false
```

Changing any of it means SSH, an editor, and `systemctl restart repaircam`.
That is the wrong level of ceremony for "keep packing footage for 60 days
instead of 45", which is a business decision the owner should be able to make.

## The thing to get right first: not all of it should be central

The file looks uniform. It is not. It holds two different kinds of setting, and
syncing them centrally has opposite consequences.

| Setting | Kind | Central? |
|---|---|---|
| `keep_days`, `keep_days_by_source` | **Shop policy** — how far back anyone can look | **Yes** |
| `min_free_gb`, `warn_free_gb` | Shop policy, expressed in GB | Yes |
| `archive_dir` | **This machine's mount path** | **Never** |
| `keep_days_local` | **This machine's disk arithmetic** | **Never** |
| `delete_after_archive` | Commissioning decision, once per box | No — see below |
| `delete_from_archive` | The irreversible switch | No — see below |

### Why `archive_dir` must never be central

It is a path on one machine. A central value is wrong everywhere else by
construction, and wrong in the worst possible way: **a path that does not exist
looks exactly like an unplugged drive, and a path that exists but is not the
drive looks exactly like a working archive.**

That second failure is not hypothetical — it happened during setup on
2026-08-01. `/mnt/backup-drive` existed as an empty folder while the drive was
detached, and the next step would have written "backups" into the laptop's own
disk under a name that says otherwise. It was caught only because a write test
failed for an unrelated reason.

A central `archive_dir` would let one admin, on one screen, silently turn every
recorder's backup into a folder on its own boot disk. There is no version of
that worth the convenience.

### Why `keep_days_local` must never be central

It is arithmetic, not policy: how many days of footage *this* disk holds. The
204 GB recorder holds about five days of three benches. A central value of 30 —
which is a perfectly sensible *policy* number — fills the disk by mid-week, at
which point the free-space guard refuses Start and nothing is old enough to
prune. Recording stops while the setting reads as correct.

This is the exact confusion the two-window split was built to end. Putting one
of the two windows on a shared screen next to the other would rebuild it.

### Why the two delete switches stay local

They are not policy, they are **statements about this machine**: that its
archive has been proven, that a clip has been played back off it, that the
power-cut behaviour is understood. `delete_after_archive` should be turned on by
somebody standing at the box who has just checked those things.

`delete_from_archive` is stronger still — it is the only setting in RepairCam
that destroys footage with nothing behind it. Arming that from a web form, on a
screen shared with whoever else holds the admin role, is not a trade worth
making for saving one SSH session per machine lifetime.

**Both stay in `storage.yaml`, and the central config may not contain them.**
Not "defaults to off" — absent from the payload, and rejected if present.

---

## What the central half looks like

### The payload

```json
{
  "revision": 4,
  "retention": {
    "default_days": 30,
    "by_source": { "repair": 30, "packing": 45 }
  },
  "free_space": { "min_gb": 20, "warn_gb": 50 }
}
```

No paths, no switches, no secrets — so unlike the camera list this needs **no
encryption at rest** and no `REPAIRCAM_CONFIG_KEY`.

### It rides the existing poll

Same mechanism as the camera list, for the same reasons (no write-back, works
with more than one recorder, nothing to get stuck). `GET /trc/active` and
`GET /pack/active` already carry `config_revision` for cameras; add a second
integer beside it:

```json
{ "active": [ … ], "config_revision": 7, "storage_revision": 4 }
```

Steady state costs zero extra requests. When it moves, the recorder fetches
`GET /repaircam/storage-config` once and rewrites the policy half of
`storage.yaml`, preserving every local key it is not allowed to touch.

### Writing the file

The same discipline as `camerasync._write`: temp file, atomic rename, one
`.bak`, and a header saying the file is machine-written. Local-only keys are
read from the existing file and written back unchanged — the sync **merges**,
it does not replace. A hand-edited `archive_dir` survives every sync forever.

---

## The dangerous change, and the guard

Lengthening a window is safe: nothing is deleted, the archive grows.

**Shortening one deletes footage**, and the first prune after the change does it
all at once. Typing `4` instead of `45` in a web form is a plausible slip that,
with `delete_from_archive` on, destroys six weeks of packing footage within ten
minutes and cannot be undone.

Three properties handle it, in order of importance:

1. **Only the recorder knows the impact.** saar-seva has no idea how many clips
   exist or how old they are — the catalogue is on the box. So the impact cannot
   be shown at save time from the server's own knowledge, and any design that
   pretends otherwise is guessing.

2. **A shortening is staged, not applied. BUILT.** When any window is reduced —
   by a sync later, by a hand edit today — the recorder computes what the new
   window *would* delete (clip count, GB, oldest date), records it in the
   catalogue, and **does not delete**. `prune_archive` refuses to run while it
   is held, which is the last gate before footage stops existing and the only
   one no caller can forget. The status page and `cli storage` show:

   > Retention for packing was shortened from 45 to 4 days. Applying it would
   > delete 214 clips (380 GB), going back to 12 June. Nothing has been deleted.
   > Confirm with `cli storage --accept-retention`, or set it back in the admin
   > panel.

   Lengthening applies immediately and silently. Only reductions stage, and
   putting the window back clears the hold at no cost. A reduction made in
   steps — 45 to 20 to 4, none of them accepted — is measured against the
   longest window the source is known to have had, or the rest would slip
   through unremarked. `keep_days_local` is deliberately never held: shortening
   it removes copies that are verifiably at the archive, which costs nothing.

   The comparison is against windows stored in the CATALOGUE, not against
   whatever the worker read at startup, so a file edited while the service was
   stopped is caught on the next start.

3. **The recorder reports its state upward** so the admin screen is not blind.
   It already sends a heartbeat every poll; add free space, clip count, oldest
   clip date and archive health to it. The panel then shows, per recorder,
   what the shop actually holds — and a pending staged reduction, so the person
   who typed `4` sees the consequence on the screen where they typed it.

A floor is worth having too: refuse a window below **7 days** outright, with a
message rather than a silent clamp. Nothing legitimate needs a shorter one, and
`0` — which means "delete everything" — must never be reachable by a typo.

---

## Validation, recorder side

Refuse the whole payload rather than half-apply, exactly as `camerasync` does:

- every window is an integer ≥ 7
- `min_gb` ≥ 5, `warn_gb` ≥ `min_gb`
- no unknown keys — specifically, a payload containing `archive_dir`,
  `keep_days_local`, `delete_after_archive` or `delete_from_archive` is
  **rejected entirely**, not filtered. A server trying to set those is either a
  version mismatch or something worse, and quietly ignoring the fields would
  hide both.
- an archive window shorter than this box's `keep_days_local` is accepted but
  reported — the recorder would be asked to hold footage longer than the shop
  wants it to exist. `StorageConfig.local_outlives_archive` already detects it.

## One existing wart this forced us to fix — BUILT

`StorageWorker` read the config **once**, at startup, which is why changing
`storage.yaml` needed `systemctl restart repaircam`. Tolerable for a file
somebody edits over SSH. Not tolerable for a setting changed from a web panel,
where nothing would appear to happen and the obvious conclusion is that the
panel is broken.

Built as `StorageWorker.reload_if_changed()`, called at the top of every pass —
before the work, since a pass that archives under the old settings and then
notices they changed has already done the wrong thing once. It fingerprints the
file by mtime *and* size, because an edit that keeps the byte count is ordinary
(`30` -> `45`). A file that will not parse is **refused and the running config
kept**: the alternative is one typo silently stopping the shop's only backup.
That divergence — file says one thing, worker doing another — is the one state
nothing else on the status page could express, so it has its own line there.

`apply_config()` exists for phase 3: a caller that has just written the file
itself hands the config straight over, so the change lands immediately rather
than up to ten minutes later, and the two paths cannot fight over it.

---

## The admin screen

Admin → TRC settings → **"Recording storage"**, beside "Cameras ↔ work centres",
same `trc.manage` role.

- Retention: one row per source (repair, packing) plus a default, in days
- Free space: minimum and warning, in GB
- Read-only, per recorder, from the heartbeat: free space, clips held, oldest
  clip, archive reachable, and **whether deletion is switched on at all** —
  because a shop reading "packing: 45 days" on this screen should not have to
  guess that nothing is ever actually deleted
- A banner when a recorder has a staged reduction waiting

The read-only half is the part that earns its keep. The windows are three
numbers; what the shop cannot see today is what those numbers are doing.

---

## Phasing

Each phase is useful alone and safe to stop after.

1. ~~**Recorder reloads config without a restart.**~~ **BUILT 2026-08-01.**
   No protocol change; useful whether or not the rest happens.
2. ~~**saar-seva: `SystemSetting` + `GET /repaircam/storage-config` +
   `storage_revision` on both poll endpoints.**~~ **BUILT 2026-08-01** as
   `backend/app/storage_config.py`, with the admin GET/PUT alongside it so
   phase 6 is only the screen.
3. ~~**Recorder: fetch, validate, merge-write, report.**~~ **BUILT 2026-08-01**
   as `storagesync.py`. Both revisions are tracked apart, so a retention change
   does not re-read the camera list. The written file is in two labelled
   halves — this box's own settings, and the admin panel's — because somebody
   opening it to change a number needs to know which of their edits survives.
4. ~~**The staging guard for reductions.**~~ **BUILT 2026-08-01**, ahead of
   2 and 3: the same risk already exists through a hand edit to storage.yaml.
5. ~~**Recorder state in the heartbeat, and the read-only half of the panel.**~~
   **BUILT 2026-08-01.** `storage.report()` rides the existing heartbeat;
   saar-seva stores it per recorder and marks anything over two minutes old as
   not fresh, server-side. The archive PATH is deliberately not reported.
6. **The admin UI itself.**

Phases 2–6 are worth doing when there is more than one recorder, or when
somebody actually wants to change a window — which has not happened yet, and
may not for months.

## What this does not solve

The shop has **one** recorder. Central config pays for itself at three or ten,
where SSH-per-box stops scaling. At one box, `nano storage.yaml` is genuinely
competitive, and the honest reason to build this is that changing retention
should not require knowing what SSH is — not that it saves time.

Worth weighing against the thing this same afternoon showed: there is no camera
on any repair bench. Central storage config makes an existing capability easier
to administer. A camera makes the shop record something it currently does not.
