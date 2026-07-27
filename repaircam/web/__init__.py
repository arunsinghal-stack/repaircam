"""The RepairCam web UI (Phase 2).

Runs on the recorder box and is opened from any browser on the shop LAN — the
technician's phone or a tablet at the bench.

There is deliberately **no login**: the shop LAN is the security boundary, the
same one that keeps the cameras off the internet. Do not port-forward this.
"""

from __future__ import annotations

import logging

from flask import Flask

from .. import config
from ..catalogue import Catalogue
from ..recorder import RecorderPool

log = logging.getLogger(__name__)


def _secret_key() -> bytes:
    """Key for signing the session cookie that carries flash messages.

    Kept in the data directory rather than generated per start, so restarting
    the recorder does not log everyone out mid-shift.
    """
    path = config.data_dir() / ".secret_key"
    if path.exists():
        return path.read_bytes()
    import os

    key = os.urandom(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def create_app(**overrides) -> Flask:
    app = Flask(__name__)
    config.ensure_data_dirs()
    app.config.update(
        SECRET_KEY=_secret_key(),
        # Videos are streamed from disk; keep the response window generous for
        # long clips over shop Wi-Fi.
        SEND_FILE_MAX_AGE_DEFAULT=0,
        JSON_SORT_KEYS=False,
        **overrides,
    )

    # One catalogue and one recorder pool for the whole process: a recording
    # started by one request must still be there for the request that stops it.
    app.extensions["catalogue"] = Catalogue()
    app.extensions["recorders"] = RecorderPool(app.extensions["catalogue"])

    from .routes import bp

    app.register_blueprint(bp)
    return app
