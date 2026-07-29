# Central camera config — plan

Maintain the bench→camera list in saar-seva's admin panel instead of by editing
`cameras.yaml` over SSH. The recorder picks changes up on the poll it already
makes.

**BUILT — 2026-07-29.** Both halves are in their repos; nothing has yet run in
the shop. This document is now the description, not the plan.

---

## The idea

Today, adding a camera means SSHing into the shop box and editing a YAML file.
With ten benches that is ten trips. Instead:

1. Admin opens the panel, picks a work centre, types its IP. Saves.
2. saar-seva bumps a **config revision** number.
3. The recorder's existing 5-second poll sees the number changed.
4. It fetches the list, rewrites `cameras.yaml`, and carries on.

No new polling loop, no push into the shop, no port opened.

## The flag: a revision number, not a boolean

The obvious design is a dirty flag the recorder turns off after syncing. A
counter is the same idea and strictly better:

- **No write-back.** The recorder never has to tell saar-seva it is done, so it
  needs no write permission for config, and there is no window where a crash
  between "synced" and "cleared" loses the change.
- **More than one recorder works.** With a boolean, whichever box syncs first
  clears the flag and the others never learn. Each recorder simply remembers the
  revision it last applied.
- **Nothing to get stuck.** A flag that fails to clear syncs forever; a flag
  cleared too early never syncs. A number has neither failure mode.

So: `config_revision` is an integer in `SystemSetting`, bumped on every save of
the camera list.

## It rides the existing poll — no extra request

The recorder already calls `GET /trc/active` every ~5s. The revision is added to
that response:

```json
{ "active": [ … ], "config_revision": 7 }
```

The recorder holds the last revision it applied. `7 != 6` → fetch the full list
once. Steady state costs **zero extra requests**; the number is a handful of
bytes on a call that was happening anyway.

`GET /pack/active` carries it too, so a shop that only uses packing still syncs.

---

## What is central, and what stays local

**Everything is central, including passwords** — the owner's decision, made
after the trade-off below was raised. `cameras.yaml` becomes a fully generated
file; nothing has to be edited on the recorder at all.

### The trade-off, and how it is reduced

saar-seva's database holds **no secrets today** — Odoo API key, PIN pepper,
admin password, RepairCam key, all environment variables, none in a table.
Camera passwords will be the first. That puts the keys to the shop's cameras on
a host on the internet, where a database dump or a compromised admin login would
expose them.

Two things reduce it, and both should be built:

1. **Encrypt at rest.** Passwords are stored encrypted in `SystemSetting`, with
   the key held in a Render environment variable (`REPAIRCAM_CONFIG_KEY`), never
   in the database. A DB dump alone then yields nothing, and saar-seva's "no
   plaintext secrets in tables" property is preserved.
2. **Write-only in the admin UI.** The panel shows `••••••••` and a "change"
   box; it never renders an existing password back to the browser. An admin can
   set one, not read one.

The blast radius is also bounded by what a camera password actually is: it opens
an RTSP stream **on the shop LAN only**. It is not a customer credential and it
does not reach Odoo or any money. That is what makes this a reasonable call —
but it is a deliberate one, not a default.

---

## What already exists (checked 2026-07-29)

Less has to be built than this plan first assumed:

- **`SystemSetting`** is a key/value store whose `value` is `Text`, already used to
  hold a JSON blob (`trc_config`), with `get_config` / `save_config` helpers in
  `backend/app/trc_config.py`. The camera list needs no migration and no new table.
- **`GET /admin/trc-workcenters`** already returns `odoo.list_workcenters()` for the
  admin panel, so the work-centre dropdown needs no new endpoint.
- **The admin page and its gate exist** — Admin → TRC settings → "Technicians & work
  centres", behind `trc.manage`. The camera table goes beside it, under the same gate.
- **Adding `config_revision` to `/trc/active` is backward compatible.** RepairCam's
  `parse_active()` reads `payload["active"]` and ignores every other key, so an
  older recorder keeps working against a newer saar-seva.

## The one local line that can still veto a central change

`saarseva.yaml` has a `work_centers:` list. Naming benches in it turns it into
an **allow-list**, and a bench added in the admin panel then arrives in
`cameras.yaml` correctly, has its Odoo id, has its camera — and still never
records, because a file on the recorder excludes it. Nothing about the bench
looks wrong.

So: **leave `work_centers` empty**, which means "every bench in cameras.yaml
that has an `odoo_workcenter_id`". That is the setting that makes adding a bench
centrally genuinely sufficient. The example file now ships empty and says why.

When it is not empty and it is excluding a fully-configured bench, the recorder
logs a warning and the status page says so under **"Not auto-recording"** — a
bench with no `odoo_workcenter_id` is deliberately not reported there, since
`work_centers` is not the reason it is absent and saying so would send someone
editing the wrong file.

## What can never be central, and why

`base_url` and `api_key` must stay in `saarseva.yaml` on the recorder **permanently**.
The recorder needs the key to talk to saar-seva at all, so a key fetched *from*
saar-seva is circular — there would be no way to make the first call. Same for the
address of the server holding the config.

`link_base` stays local too, but for a different reason: it is a property of the
*recorder* (its own LAN URL — `http://192.168.1.163:8080` today), not of a bench.
Ten benches share one link_base; a central per-bench table is the wrong shape for it.

So "central config" means **the camera list, and only the camera list**. Three lines
of `saarseva.yaml` are edited once per recorder and never again.

## Part A — saar-seva

### A1. The stored list

One `SystemSetting` blob under `repaircam_cameras`, exactly as `trc_config`
already does for the TRC console's Odoo selections:

```json
{
  "revision": 7,
  "cameras": [
    {"odoo_workcenter_id": 12, "name": "Bench 2", "host": "192.168.0.133",
     "port": 554, "main_path": "/stream1", "sub_path": "/stream2",
     "has_audio": true, "enabled": true, "notes": ""}
  ]
}
```

Reusing `SystemSetting` rather than a new table: this is admin-edited
configuration, which is exactly what that store is for, and it needs no
migration.

### A2. Admin screen

**Where it goes.** saar-seva moved the technician↔work-centre mapping out of the
exec-side TRC console (the Team tab is gone) into **Admin → TRC settings →
“Technicians & work centres”**, gated on the `trc.manage` permission
(commit `b8f4b22`). The camera list belongs **right next to it, in the same
settings page, behind the same gate** — which is exactly the admin-only decision
below, already enforced by existing code rather than by a new rule.

That move also leaves a work-centre picker ready to reuse:
`GET /admin/trc-workcenters` already returns `odoo.list_workcenters()` for the
admin panel, so the camera table's dropdown needs no new endpoint.

A table: **work centre** (that dropdown), then IP, port, stream paths, audio,
enabled. Sitting beside the technician mapping, the admin sees one consistent
idea: *a work centre is a bench; it has people and it has a camera.*

Saving validates and **bumps `revision`**.

### A3. Endpoints

```
GET  /repaircam/cameras     -> {revision, cameras: [...]}   (REPAIRCAM_API_KEY)
```

plus `config_revision` added to the existing `/trc/active` and `/pack/active`
responses.

Admin CRUD sits behind the normal admin auth, not the RepairCam key.

---

## Part B — RepairCam

### B1. Noticing

`Trigger.tick()` already reads both poll responses. It compares
`config_revision` against the last applied value (kept in the catalogue, so a
restart does not re-sync needlessly) and calls the sync when it differs.

### B2. Applying

1. `GET /repaircam/cameras`.
2. Validate every row (see below). **Reject the whole payload if any row is
   bad** — a half-applied camera list is worse than a stale one.
3. Merge passwords from `camera-secrets.yaml` by work-centre id.
4. Write `cameras.yaml` atomically (temp file + rename), keeping a `.bak`.
5. Reload the bench map.

### B3. Rules that keep it safe

These are the ones worth being strict about, because this path lets a remote
service change what the recorder points at:

- **Private addresses only.** Reject anything outside `192.168.x`, `10.x`,
  `172.16–31.x`. A compromised or mistyped saar-seva must not be able to aim the
  recorder at a host on the internet.
- **Never disturb a bench that is recording.** If a camera's settings change
  while a clip is in progress, apply it to that bench when it goes idle. Footage
  in flight is not reproducible; a config change can wait a few minutes.
- **Refuse an empty list.** Zero cameras almost certainly means a bug or a wiped
  setting, not "we removed all the cameras". Log loudly and keep the old file.
- **Never overwrite a password with a blank.** A bench missing from
  `camera-secrets.yaml` keeps whatever `cameras.yaml` already had.
- **A failed sync is not fatal.** Keep the current config and retry on the next
  revision change. Recording must survive saar-seva being wrong or unreachable —
  the same rule the trigger already follows.

### B4. Visible

- `status` shows the revision in use and when it last synced.
- The web status page shows the same, plus any bench with no password.
- `cli.py cameras --sync` forces one immediately, for when someone is standing
  at the box and wants to see it work.

---

## Part C — how it gets verified

- saar-seva: saving bumps the revision; the endpoint returns what was saved;
  the revision appears on both active polls.
- RepairCam: a changed revision triggers exactly one fetch; an unchanged one
  triggers none; passwords survive a sync; a public IP is rejected; an empty
  list is refused; a bench mid-recording is left alone until idle; a failed
  fetch leaves the old config working.
- Cross-repo: admin saves → recorder's next poll rewrites `cameras.yaml` → the
  new bench appears, with its password preserved.

---

## Decided

1. **Admin panel only.** TRC managers who need it are given admin access;
   the camera list is not exposed at manager level. In practice this means the
   `trc.manage` permission, the same gate the technician↔work-centre mapping
   now sits behind.
2. **Removing a bench sets `enabled: false`**, it does not delete the row. Clips
   already recorded still reference that work centre, and a deleted row would
   orphan them.
3. **Passwords are central**, encrypted at rest and write-only in the UI — see
   above.

---

## What was actually built

**saar-seva** — `backend/app/camera_config.py` (the store, the encryption and
the validation), `routers/repaircam_config.py` (`GET /repaircam/cameras`),
`GET`/`PUT /admin/repaircam-cameras` in `routers/admin_trc.py`, a "Cameras ↔
work centres" card in `AdminTrcSettings.jsx`, and `config_revision` on both
active polls. `REPAIRCAM_CONFIG_KEY` in `config.py`; `cryptography` named in
requirements rather than relied on transitively.

**RepairCam** — `camerasync.py` (validate, merge, atomic write), the revision
check in `trigger.py`, `fetch_camera_config()` in `saarseva.py`, a `settings`
table in the catalogue holding the applied revision, `cli.py cameras --sync`,
and the sync state on the status page.

### Two things the build changed from the plan

- **`ipaddress.is_private` is not the rule.** It also accepts loopback,
  link-local and the RFC 5737 documentation ranges — `203.0.113.5` reads as a
  perfectly ordinary address and would have been accepted by both ends. Both
  now check membership of 192.168/16, 10/8 and 172.16/12 explicitly.
- **Revision 0 means "no central list", not "an empty one".** Fetching and then
  refusing an empty list on every poll is what a shop that has not adopted this
  would otherwise see in its log, forever.

### And one the plan had backwards

The plan said to merge passwords from a local `camera-secrets.yaml`. That was
left over from before passwords went central; there is no such file. Passwords
come from the central payload, and a blank one keeps whatever `cameras.yaml`
already had — the rule that matters is unchanged, its source is not.

## Sequencing

This is a convenience feature. It saves SSH trips when there are several benches;
it unblocks nothing.

The two things this plan said should come first — the focus test, and a real
recording — are both **done** (2026-07-29), and Phase 5 is live. But there is still
**one camera**, and with one camera a central list saves exactly one SSH trip.

What the live run did change is the *argument* for it. Switching Phase 5 on meant
hand-adding `odoo_workcenter_id: 2` to `cameras.yaml`, and a bench whose id is
missing or mistyped is never auto-triggered — silently. That is a ten-times-over
risk at ten benches, and it is the same failure shape as the three defects the first
live run turned up: the system was wrong and said nothing. (It now at least prints
`NOT SET — this bench will never auto-record`.)

Still not the next thing to build. **Nothing deletes or moves old clips**: 204 GB
free is ~113 bench-hours, which is three weeks at one bench and under a week at
three. A full disk stops recording mid-repair. Central config is convenience;
storage is data loss.

Built anyway, on the owner's call. It is ready for bench 2 and 3 rather than
waiting for them.

That leaves **storage** as the next real item, and it is the one that loses
data: nothing deletes or moves old clips, 204 GB free is ~113 bench-hours, and a
full disk stops a recording mid-repair.
