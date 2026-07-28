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

    app.extensions["trigger"] = _maybe_start_trigger(app)

    from .routes import bp

    app.register_blueprint(bp)
    return app


def _maybe_start_trigger(app: Flask):
    """Start the saar-seva auto-trigger, if it has been set up.

    Off unless repaircam/saarseva.yaml exists, which is the normal state today:
    saar-seva has the endpoints but REPAIRCAM_API_KEY is not set on it yet, so
    technicians start recordings themselves. A broken trigger must never prevent
    the web UI from starting — recording by hand has to keep working no matter
    what the cloud is doing.
    """
    from .. import saarseva

    if not saarseva.is_configured():
        log.info("saar-seva auto-trigger: not configured, technicians start recordings by hand")
        return None

    try:
        config = saarseva.load_config()
        if not config.enabled:
            log.info("saar-seva auto-trigger: disabled in saarseva.yaml")
            return None

        from ..trigger import Trigger

        trigger = Trigger(
            app.extensions["recorders"],
            saarseva.SaarSevaClient(config),
            catalogue=app.extensions["catalogue"],
            config=config,
        )
        trigger.start()
        log.info("saar-seva auto-trigger: polling %s every %ss", config.base_url, config.poll_seconds)
        return trigger
    except Exception as exc:
        log.error("saar-seva auto-trigger could not start (recording is unaffected): %s", exc)
        return None
