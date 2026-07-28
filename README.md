# RepairCam

Records repair work at technician benches, tags each clip with the job it belongs
to, and keeps everything on a machine in the shop.

One recorder box pulls video from PoE cameras — one camera per bench. A technician
presses **Start**, **Pause** and **Done** on a phone or tablet; RepairCam saves one
video per operation, labelled with the job number, device and IMEI.

- **What it is and why it is built this way:** [docs/PLAN.md](docs/PLAN.md)
- **Project status:** [CLAUDE.md](CLAUDE.md)

---

## Setup on the recorder box

Do these on the **Linux machine in the shop** — the one that can reach the cameras.
Not the Mac. Run one command at a time and check the result before moving on.

### 1. Install the tools

```bash
sudo apt update
sudo apt install -y ffmpeg python3-pip python3-venv git
```

Check it worked:

```bash
ffmpeg -version
```

You should see a line starting `ffmpeg version`. If you see `command not found`,
the install did not work — do not continue.

Also check Python:

```bash
python3 --version
```

Anything from **3.8** upwards is fine. If the next step complains that
`ensurepip is not available`, install the matching venv package it names — for
example on Ubuntu 20.04 (Python 3.8):

```bash
sudo apt install -y python3.8-venv
```

### 2. Get RepairCam

```bash
git clone https://github.com/arunsinghal-stack/repaircam.git
cd repaircam
```

### 3. Install what it needs

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The last line should say `Successfully installed ...`.

### 4. Tell it about your camera

```bash
cp repaircam/cameras.example.yaml repaircam/cameras.yaml
nano repaircam/cameras.yaml
```

Change **two things**: `host` to your camera's IP address, and `password` to the
password you set in the VIGI app. Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

> This file holds your camera password. It is never uploaded to GitHub — that is
> already set up. Do not copy it anywhere else.

### 5. Check everything is healthy

```bash
.venv/bin/python -m repaircam.cli status
```

Every line should start with `OK`. If the camera line says `FAIL`, the recorder
cannot reach the camera — check the network cable and the IP address before going on.

---

## The focus test

This is the one thing that must pass before trusting the system: can you see enough
detail on a real phone?

1. Put a phone on the bench, roughly 60–80 cm from the camera.
2. Run:

   ```bash
   .venv/bin/python -m repaircam.cli snapshot WC2
   ```

3. Open the image file it prints and look at it.

**Pass:** you can make out the screws and the screen. **Fail:** it is soft or blurry —
adjust the camera's zoom and focus in the VIGI app and take another snapshot.

---

## Recording a test clip

```bash
.venv/bin/python -m repaircam.cli record WC2 --duration 20
```

It records for 20 seconds and prints where the video was saved. Open that file and
check it plays.

---

## Starting the app technicians use

```bash
.venv/bin/python -m repaircam.cli web
```

Leave that running. On any phone or tablet on the shop Wi-Fi, open:

```
http://<the recorder box's IP>:8080
```

To find the IP, run `hostname -I` on the recorder box and use the first number.

### What a technician does

1. Open the bench (for example **WC2**).
2. Type the job number and what they are working on.
3. Press **Start recording**.
4. Press **Pause** if they step away, **Start** again when they return.
5. Press **Done — save clip** when the operation is finished.

Everything between Start and Done becomes **one video** for that operation.

> **Keep this on the shop network only.** There is no password on the web app — the
> shop LAN is what keeps it private. Do not set up port forwarding for it.

---

## Make it start by itself (recommended)

The command above only runs while that terminal window stays open. Once the focus
test has passed and you want RepairCam running permanently, install it as a
**service**: it then starts on its own when the machine boots and restarts itself if
it ever crashes.

From inside the `repaircam` folder:

```bash
./deploy/install-service.sh
```

It will ask for your password (installing a service needs administrator rights).
Every line it prints should say `OK`. At the end it shows the address to open.

> Run it as yourself, **not** with `sudo` in front. The script asks for
> administrator rights only for the steps that need them, so that your recordings
> stay owned by you.

To use a different port or storage location:

```bash
./deploy/install-service.sh --port 9000
./deploy/install-service.sh --data-dir /mnt/archive/repaircam-data
```

To see exactly what it would install without changing anything:

```bash
./deploy/install-service.sh --dry-run
```

### Managing the service afterwards

| What you want | Command |
|---|---|
| Is it running? | `systemctl status repaircam` |
| Watch what it is doing | `journalctl -u repaircam -f` (press `Ctrl+C` to stop watching) |
| See today's messages | `journalctl -u repaircam --since today` |
| Restart it | `sudo systemctl restart repaircam` |
| Stop it until the next reboot | `sudo systemctl stop repaircam` |
| Stop it permanently | `sudo systemctl disable --now repaircam` |

After changing `cameras.yaml`, restart the service so it picks up the change:

```bash
sudo systemctl restart repaircam
```

> **One thing to know.** If the machine reboots or the service restarts *while a
> technician is part-way through an operation*, that recording is not saved as a clip
> automatically. Nothing is lost — see **Rescuing unsaved footage** below.

---

## Rescuing unsaved footage

If the recorder restarts part-way through an operation, the video is still on disk but
was never joined into a clip, so it does not appear in the Library. The **Status** page
tells you when this has happened.

To see whether there is any:

```bash
.venv/bin/python -m repaircam.cli recover
```

This only *looks* — it changes nothing. If it finds something, save it with:

```bash
.venv/bin/python -m repaircam.cli recover --all
```

Each rescued recording becomes a normal clip in the Library, **without a job label** —
nobody ever told RepairCam what job it was for. Open it in the Library and fill in the
job details so it stays useful.

> If it says a recording *"may still be recording"*, that is the safety check doing its
> job: joining a video while it is still being written would damage it. Wait a minute
> and run it again. Only add `--force` if you are certain nothing is recording.

---

## Everyday commands

Run these from the `repaircam` folder.

| What you want | Command |
|---|---|
| Is everything working? | `.venv/bin/python -m repaircam.cli status` |
| List the benches | `.venv/bin/python -m repaircam.cli cameras --check` |
| Focus test | `.venv/bin/python -m repaircam.cli snapshot WC2` |
| Record 20 seconds | `.venv/bin/python -m repaircam.cli record WC2 --duration 20` |
| What has been recorded | `.venv/bin/python -m repaircam.cli list` |
| Details of one clip | `.venv/bin/python -m repaircam.cli info 12` |
| Add a job label afterwards | `.venv/bin/python -m repaircam.cli relabel 12 --mo WH/MO/42` |
| Check for unsaved footage | `.venv/bin/python -m repaircam.cli recover` |
| Save unsaved footage | `.venv/bin/python -m repaircam.cli recover --all` |
| Start the web app | `.venv/bin/python -m repaircam.cli web` |

---

## Where the files go

Everything lives in `~/repaircam-data`:

```
~/repaircam-data/
├── repaircam.db                  the index of every clip
├── recordings/2026-07-27/        the videos, in dated folders
│   ├── WC2_..._wh-mo-00042_screen-replacement.mp4
│   └── WC2_..._wh-mo-00042_screen-replacement.json
└── snapshots/                    focus-test images
```

Each video has a `.json` file next to it describing the job it shows. **Keep the two
together** — the `.json` is what makes the video useful later.

To store recordings on a different disk, set `REPAIRCAM_DATA_DIR`:

```bash
export REPAIRCAM_DATA_DIR=/mnt/archive/repaircam-data
```

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `ffmpeg is not installed` | Step 1 did not work. Run `sudo apt install -y ffmpeg`. |
| `No camera configured for work center 'WC2'` | The bench name is not in `cameras.yaml`, or is spelled differently. |
| `No camera config at ...` | Step 4 was skipped. Copy the example file. |
| Camera shows `FAIL` in `status` | The recorder cannot reach the camera: check the PoE cable, the IP, and that the camera is on. |
| Live view stays black | The preview stream is not coming through. Press "take a still" on the bench page to test the camera directly. |
| `Nothing was recorded on WC2` | The camera dropped during recording. Run `status` to check it, then record again. |

---

## For developers

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests run without cameras and without ffmpeg — capture is faked by `StubBackend` in
`tests/conftest.py`.

Layout:

```
repaircam/
├── config.py        data directory + cameras.yaml
├── ffmpeg.py        every subprocess call, and password redaction
├── backends/        base.py (the interface), rtsp.py (IP cameras)
├── recorder.py      the Start/Pause/Done state machine
├── catalogue.py     SQLite index + sidecar JSON
├── cli.py           command line
└── web/             Flask UI (routes, templates, static)
```
