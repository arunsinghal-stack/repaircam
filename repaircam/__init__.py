"""RepairCam — bench recording for mobile-phone repair work.

Records each repair operation at a technician bench so the shop has evidence for
accountability and training, and so the clips form a labelled dataset (every clip
carries the job it belongs to: MO, operation, device, IMEI).

The package is deliberately split so the capture hardware can change without the
rest moving:

    backends/   how a clip is captured (RTSP today, Pi camera later)
    recorder.py the Start/Stop/Done state machine, one clip per operation
    catalogue.py the SQLite index of everything recorded
    web/        the Flask UI technicians and the owner actually look at
"""

import sys

MINIMUM_PYTHON = (3, 8)
if sys.version_info < MINIMUM_PYTHON:
    raise RuntimeError(
        f"RepairCam needs Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer, "
        f"but this is Python {'.'.join(map(str, sys.version_info[:3]))}.\n"
        f"On the recorder box:  sudo apt install -y python3 python3-venv"
    )

__version__ = "0.2.0"

SCHEMA_VERSION = 1
"""Version stamped into every sidecar JSON, so a future dataset loader can tell
which layout it is reading."""
