"""The saar-seva HTTP client — request building and error handling.

No network: the URL opener is injected.
"""

from __future__ import annotations

import json
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from repaircam.catalogue import JobLabels, Recording, utcnow
from repaircam.config import ConfigError
from repaircam.saarseva import (
    ActiveOperation,
    SaarSevaClient,
    SaarSevaConfig,
    SaarSevaError,
    is_configured,
    load_config,
)

CONFIG_YAML = """
saarseva:
  base_url: "https://saar-seva-api-staging.onrender.com/"
  api_key: "s3rvice-token"
  link_base: "http://192.168.0.50:8080/"
  poll_seconds: 5
  work_centers: ["WC2"]
"""


class FakeOpener:
    """Captures the request and returns a canned response."""

    def __init__(self, payload=None, *, status=200, raw=None, error=None):
        self.payload = payload
        self.raw = raw
        self.error = error
        self.status = status
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeout = timeout
        if self.error:
            raise self.error
        body = self.raw if self.raw is not None else json.dumps(self.payload or {}).encode()

        @contextmanager
        def response():
            class R:
                def read(self_inner):
                    return body

            yield R()

        return response()

    @property
    def last(self):
        return self.requests[-1]


@pytest.fixture
def config() -> SaarSevaConfig:
    return SaarSevaConfig(
        base_url="https://example.test",
        api_key="token123",
        link_base="http://192.168.0.50:8080",
        work_centers=["WC2"],
    )


# --------------------------------------------------------------------------
# config file
# --------------------------------------------------------------------------


def test_loads_the_config(tmp_path: Path):
    path = tmp_path / "saarseva.yaml"
    path.write_text(CONFIG_YAML)

    config = load_config(path)

    assert config.base_url == "https://saar-seva-api-staging.onrender.com"  # trailing / stripped
    assert config.link_base == "http://192.168.0.50:8080"
    assert config.work_centers == ["WC2"]
    assert config.poll_seconds == 5


def test_missing_config_says_the_trigger_is_simply_off(tmp_path: Path):
    """No config is a normal state, not a broken one."""
    with pytest.raises(ConfigError, match="automatic trigger is off"):
        load_config(tmp_path / "nope.yaml")
    assert is_configured(tmp_path / "nope.yaml") is False


def test_config_without_base_url_is_rejected(tmp_path: Path):
    path = tmp_path / "saarseva.yaml"
    path.write_text("saarseva:\n  api_key: 'x'\n")
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path)


def test_the_token_is_never_shown(tmp_path: Path):
    path = tmp_path / "saarseva.yaml"
    path.write_text(CONFIG_YAML)
    config = load_config(path)

    assert config.safe_api_key == "set (hidden)"
    assert "s3rvice-token" not in str(config.describe())


# --------------------------------------------------------------------------
# fetching what is active
# --------------------------------------------------------------------------


def test_fetch_active_calls_the_right_url(config):
    opener = FakeOpener({"active": []})
    SaarSevaClient(config, opener=opener).fetch_active([12, 13])

    assert opener.last.full_url.startswith("https://example.test/trc/active")
    assert "workcenters=12%2C13" in opener.last.full_url


def test_fetch_active_sends_the_token(config):
    opener = FakeOpener({"active": []})
    SaarSevaClient(config, opener=opener).fetch_active()

    assert opener.last.get_header("Authorization") == "Bearer token123"


def test_fetch_active_returns_operations(config):
    opener = FakeOpener(
        {"active": [{"workcenter_id": 12, "mo_name": "WH/MO/7", "operation": "Battery",
                     "time_log_id": "abc"}]}
    )
    operations = SaarSevaClient(config, opener=opener).fetch_active()

    assert len(operations) == 1
    assert operations[0].labels().mo_name == "WH/MO/7"
    assert operations[0].key == "repair:abc"


def test_no_bench_filter_when_none_given():
    config = SaarSevaConfig(base_url="https://example.test")
    opener = FakeOpener({"active": []})
    SaarSevaClient(config, opener=opener).fetch_active()

    assert "workcenters" not in opener.last.full_url


# --------------------------------------------------------------------------
# posting a link
# --------------------------------------------------------------------------


def recording(**kw) -> Recording:
    defaults = dict(
        id=41,
        work_center="WC2",
        path="recordings/2026-07-27/a.mp4",
        started_at=utcnow(),
        duration_s=412.5,
        labels=JobLabels(mo_name="WH/MO/42", operation="Screen replacement", imei="350111"),
        # Every clip that legitimately reaches /trc/recordings came from a
        # saar-seva session; source_ref is that session's id, stored when the
        # clip was filed so a retry survives a restart.
        source="repair",
        source_ref="3f2b",
    )
    defaults.update(kw)
    return Recording(**defaults)


def test_post_recording_sends_a_link_not_a_video(config):
    """The chatter carries a pointer; the footage never leaves the shop."""
    opener = FakeOpener({})
    SaarSevaClient(config, opener=opener).post_recording(recording())

    body = json.loads(opener.last.data)
    assert body["url"] == "http://192.168.0.50:8080/clip/41"
    assert body["recording_id"] == 41
    assert body["duration_s"] == 412.5
    assert "video" not in body and "data" not in body


def test_post_recording_identifies_the_timer_it_belongs_to(config):
    """saar-seva keys the chatter post on the time log, and that is what makes
    a retry idempotent there."""
    opener = FakeOpener({})
    operation = ActiveOperation(time_log_id="3f2b", job_id="9a1c", workcenter_id=12)
    SaarSevaClient(config, opener=opener).post_recording(recording(), operation=operation)

    body = json.loads(opener.last.data)
    assert body["time_log_id"] == "3f2b"
    assert body["job_id"] == "9a1c"


def test_post_recording_takes_the_session_id_from_the_catalogue(config):
    """Not from the in-memory map — retries are driven from the catalogue so a
    clip finished before a restart still gets posted, and it can only do that if
    the session id survived the restart too."""
    opener = FakeOpener({})
    SaarSevaClient(config, opener=opener).post_recording(recording(source_ref="from-db"))

    assert json.loads(opener.last.data)["time_log_id"] == "from-db"


def test_post_recording_refuses_a_clip_with_no_session(config):
    """A hand-started clip has nothing for saar-seva to match. Sending it would
    404 on every poll forever, so it is refused here instead."""
    opener = FakeOpener({})
    client = SaarSevaClient(config, opener=opener)

    with pytest.raises(SaarSevaError, match="no saar-seva session"):
        client.post_recording(recording(source="", source_ref=""))
    assert opener.requests == []  # nothing was sent at all

def test_post_recording_uses_post_and_json(config):
    opener = FakeOpener({})
    SaarSevaClient(config, opener=opener).post_recording(recording())

    assert opener.last.method == "POST"
    assert opener.last.get_header("Content-type") == "application/json"


def test_post_without_a_link_base_explains_itself():
    config = SaarSevaConfig(base_url="https://example.test", link_base="")
    with pytest.raises(SaarSevaError, match="link_base"):
        SaarSevaClient(config, opener=FakeOpener({})).post_recording(recording())


# --------------------------------------------------------------------------
# when things go wrong
# --------------------------------------------------------------------------


def test_a_rejected_token_says_so(config):
    opener = FakeOpener(error=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
    with pytest.raises(SaarSevaError, match="API key was rejected"):
        SaarSevaClient(config, opener=opener).fetch_active()


def test_a_missing_endpoint_says_it_is_not_built_yet(config):
    """Until saar-seva ships these routes, this is the error people will see."""
    opener = FakeOpener(error=urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    with pytest.raises(SaarSevaError, match="does not exist on the server yet"):
        SaarSevaClient(config, opener=opener).fetch_active()


def test_an_unreachable_server_says_so(config):
    opener = FakeOpener(error=urllib.error.URLError("Name or service not known"))
    with pytest.raises(SaarSevaError, match="could not reach saar-seva"):
        SaarSevaClient(config, opener=opener).fetch_active()


def test_a_non_json_response_is_refused(config):
    opener = FakeOpener(raw=b"<html>Render is waking up</html>")
    with pytest.raises(SaarSevaError, match="did not return JSON"):
        SaarSevaClient(config, opener=opener).fetch_active()


def test_check_reports_success(config):
    opener = FakeOpener({"active": [{"workcenter_id": 12}]})
    ok, message = SaarSevaClient(config, opener=opener).check()
    assert ok is True
    assert "1 operation" in message


def test_check_reports_failure_without_raising(config):
    opener = FakeOpener(error=urllib.error.URLError("down"))
    ok, message = SaarSevaClient(config, opener=opener).check()
    assert ok is False
    assert "could not reach" in message
