# Central camera config — plan

Maintain the bench→camera list in saar-seva's admin panel instead of by editing
`cameras.yaml` over SSH. The recorder picks changes up on the poll it already
makes.

**Nothing here is built yet.** This is the plan.

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

A table: **work centre** (dropdown from `odoo.list_workcenters()`, which already
exists and already feeds the TRC Team tab), then IP, port, stream paths, audio,
enabled. Same shape as the Team tab that maps technicians to work centres, so
the admin sees one consistent idea: *a work centre is a bench; it has people and
it has a camera.*

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
   the camera list is not exposed at manager level.
2. **Removing a bench sets `enabled: false`**, it does not delete the row. Clips
   already recorded still reference that work centre, and a deleted row would
   orphan them.
3. **Passwords are central**, encrypted at rest and write-only in the UI — see
   above.

---

## Sequencing

This is a convenience feature. It saves SSH trips when there are several
benches; it unblocks nothing. Today there is **one camera, which has not yet
recorded a frame**.

Worth building when bench 2 and 3 arrive. Before that, the focus test and a real
recording matter more.
