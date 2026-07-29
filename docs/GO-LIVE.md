# Going live

Moving the shop from "RepairCam is being tested" to "the shop depends on it".

Everything up to now has run against saar-seva's **staging** server. Going live
means production, and the two are separate in a way that catches people out:
**they have different databases.** Anything typed into staging's admin panel —
the camera list and its passwords above all — does not exist in production and
has to be entered again.

Run `python3 -m repaircam.cli preflight` at any point. It checks all of this
from the recorder's side and says what to do about anything it finds.

---

## Before the day

### 1. Production needs its own environment variables

Both live on the **`saar-seva-api`** Render service (not `saar-seva-api-staging`).

| Variable | Without it |
|---|---|
| `REPAIRCAM_API_KEY` | Every RepairCam endpoint answers **503 to everyone**. Nothing records automatically. |
| `REPAIRCAM_CONFIG_KEY` | The camera list works, but **saving a camera password is refused**. Plaintext is never the fallback. |

They may be the same values staging uses or different ones — nothing is shared
between the two servers. What matters is that `REPAIRCAM_API_KEY` matches the
`api_key` in the recorder's `saarseva.yaml` exactly.

Generate a config key with:

```
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Write it down somewhere safe.** Lose it and every camera password has to be
typed in again — it decrypts nothing without it.

### 2. Production needs the code

`main` is the production branch. Merge `staging` into it (the "prod cutover"
PR). The endpoints themselves have been on `main` for a while — that is why
production answers 503 rather than 404 — but the admin screens and the newer
fixes ride along with everything else.

### 3. The camera list has to be entered on production

Admin → TRC settings → **Cameras ↔ work centres**, on the production admin
panel. Separate database: staging's list is not there.

While you are on that page, check **People ↔ work centres** too — technicians
and packers both, mapped to the benches they actually work at. An unmapped
technician sees no jobs; an unmapped packer cannot film.

---

## On the day

### 4. Point the recorder at production

On the recorder box:

```
nano ~/repaircam/repaircam/saarseva.yaml
```

Change one line:

```yaml
  base_url: "https://saar-seva-api.onrender.com"
```

and make sure `api_key` is production's `REPAIRCAM_API_KEY`.

Leave **`work_centers: []`**. Naming benches there turns it into an allow-list,
and a bench added centrally is then fully configured and still never records.

Then:

```
sudo systemctl restart repaircam
```

### 5. Check it

```
python3 -m repaircam.cli preflight
```

Every line should be `OK`, except any you have decided to live with. In
particular it will now say **pointed at production** rather than warning about
staging.

### 6. Watch one real repair through

A technician presses Start on `/trc/time`. Then:

- their screen shows a red **Recording** light beside the `⏱ running` pill
- the bench page at `http://192.168.1.163:8080` shows the same
- Stop ends the clip
- the link appears in that job's Odoo chatter

### 7. The test that matters more than the others

Stop the recorder while a technician's timer is running:

```
sudo systemctl stop repaircam
```

Within **20 seconds** the light on their screen must go grey —
*"Recorder not reporting"* — and never stay red. A light that stays red over a
bench nobody is filming is the exact failure this whole mechanism exists to
prevent.

```
sudo systemctl start repaircam
```

---

## Known, and accepted

Nothing here blocks going live. All of it is worth knowing on the day.

- **There is no second copy of anything.** `archive_dir` is unset, so every clip
  exists only on the recorder. A theft or a dead drive loses all of it. The
  machinery is built and tested and needs a disk and one line of config.
- **The disk is the clock.** At roughly 1.8 GB per bench-hour, and with nothing
  being archived away, free space only ever falls. Below `min_free_gb` the
  recorder refuses to start a recording — correctly, and with no way to recover
  except making room by hand.
- **A power cut mid-recording is untested.** `-movflags +faststart` runs *after*
  a recording finishes, so a killed ffmpeg probably leaves a file with no index.
  Two minutes on the shop machine would settle it:

  ```
  # with a recording running
  pkill -9 ffmpeg
  find ~/repaircam-data/segments -name '*.mp4' -newermt '-3 minutes' \
      -exec ffprobe -hide_banner {} \;
  ```

  `moov atom not found` means every power cut costs the clip in flight.

- **Packing video has never run in the shop.** All three parts exist — recorder,
  endpoints, packer screen — and none of them has been used by a real packer.

---

## If something is wrong

| What you see | What it means |
|---|---|
| Everything 503s | `REPAIRCAM_API_KEY` is not set on that server |
| Everything 401s | The token here and the one on the server differ |
| Light stuck on *"Recorder not reporting"* | The recorder is not sending heartbeats — check `/status` on the recorder, it now says why |
| A bench never records | It has no `odoo_workcenter_id`, or `work_centers` in `saarseva.yaml` is excluding it. `/status` lists both under **Not auto-recording** |
| A clip's link never reaches Odoo | `/status` → **Links that never reached Odoo** names the clip and the reason |

The recorder's own status page is `http://192.168.1.163:8080/status`, and it is
the first place to look for all of these.
