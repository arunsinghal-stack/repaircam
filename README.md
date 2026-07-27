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
