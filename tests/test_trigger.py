"""The saar-seva auto-trigger.

The trigger drives recording from a cloud service over the shop's home internet.
It will be unreachable regularly, so most of what matters here is what happens
when the poll fails — the answer must always be "the shop keeps recording".

No network is touched: the client is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repaircam.catalogue import Catalogue, JobLabels, Recording, utcnow
from repaircam.recorder import Recorder, RecorderPool, State
from repaircam.saarseva import ActiveOperation, SaarSevaConfig, SaarSevaError, parse_active
from repaircam.trigger import Trigger

from .conftest import StubBackend


class FakeClient:
    """Stands in for SaarSevaClient without any HTTP."""

    def __init__(self, config: SaarSevaConfig):
        self.config = config
        self.active: list[ActiveOperation] = []
        self.fail_with: str | None = None
        self.post_fail_with: str | None = None
        self.posted: list[int] = []

    def fetch_active(self):
        if self.fail_with:
            raise SaarSevaError(self.fail_with)
        return list(self.active)

    def post_recording(self, recording, *, operation=None):
        if self.post_fail_with:
            raise SaarSevaError(self.post_fail_with)
        self.posted.append(recording.id)
        return True


def op(work_center="WC2", *, mo="WH/MO/42", operation="Screen replacement", wo="", **kw):
    return ActiveOperation(
        work_center=work_center, mo_name=mo, operation=operation, workorder_id=wo, **kw
    )


@pytest.fixture
def config() -> SaarSevaConfig:
    return SaarSevaConfig(
        base_url="https://example.test",
        api_key="token",
        link_base="http://192.168.0.50:8080",
        work_centers=["WC2", "WC3"],
    )


@pytest.fixture
def pool(catalogue: Catalogue, data_root: Path, camera) -> RecorderPool:
    """A pool with stub-backed recorders, so nothing needs a camera."""
    pool = RecorderPool(catalogue)
    for work_center in ("WC2", "WC3"):
        pool._recorders[work_center] = Recorder(
            work_center,
            backend=StubBackend(camera),
            catalogue=catalogue,
            data_root=data_root,
        )
    return pool


@pytest.fixture
def client(config) -> FakeClient:
    return FakeClient(config)


@pytest.fixture
def trigger(pool, client, catalogue, config) -> Trigger:
    return Trigger(pool, client, catalogue=catalogue, config=config)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_starts_recording_when_an_operation_appears(trigger, client, pool):
    client.active = [op("WC2")]

    result = trigger.tick()

    assert result.started == ["WC2"]
    assert pool.get("WC2").state is State.RECORDING


def test_labels_come_from_saar_seva(trigger, client, pool):
    client.active = [op("WC2", mo="WH/MO/99", device="Redmi 12", imei="350111", technician="Sunil")]
    trigger.tick()

    labels = pool.get("WC2").status()["labels"]
    assert labels["mo_name"] == "WH/MO/99"
    assert labels["device"] == "Redmi 12"
    assert labels["imei"] == "350111"
    assert labels["technician"] == "Sunil"


def test_finishes_when_the_operation_disappears(trigger, client, pool, catalogue):
    client.active = [op("WC2")]
    trigger.tick()

    client.active = []
    result = trigger.tick()

    assert result.finished == ["WC2"]
    assert pool.get("WC2").state is State.IDLE
    assert catalogue.count() == 1
    assert catalogue.list()[0].labels.mo_name == "WH/MO/42"


def test_a_steady_operation_is_left_alone(trigger, client, pool):
    """Polling every 5s must not restart anything."""
    client.active = [op("WC2")]
    trigger.tick()

    for _ in range(3):
        result = trigger.tick()
        assert result.started == []
        assert result.finished == []

    assert pool.get("WC2").status()["segments"] == 0  # never stopped
    assert pool.get("WC2").state is State.RECORDING


def test_switching_operation_cuts_one_clip_and_starts_another(trigger, client, catalogue):
    client.active = [op("WC2", mo="WH/MO/1", wo="11")]
    trigger.tick()

    client.active = [op("WC2", mo="WH/MO/2", wo="22")]
    result = trigger.tick()

    assert result.finished == ["WC2"]
    assert result.started == ["WC2"]
    assert catalogue.count() == 1
    assert catalogue.list()[0].labels.mo_name == "WH/MO/1"


def test_benches_are_independent(trigger, client, pool):
    client.active = [op("WC2", mo="WH/MO/1"), op("WC3", mo="WH/MO/2")]
    trigger.tick()
    assert pool.get("WC2").state is State.RECORDING
    assert pool.get("WC3").state is State.RECORDING

    client.active = [op("WC3", mo="WH/MO/2")]
    trigger.tick()
    assert pool.get("WC2").state is State.IDLE
    assert pool.get("WC3").state is State.RECORDING


# --------------------------------------------------------------------------
# when saar-seva misbehaves — the part that matters
# --------------------------------------------------------------------------


def test_a_failed_poll_never_stops_a_recording(trigger, client, pool):
    """The shop's internet dropping must not end a technician's recording."""
    client.active = [op("WC2")]
    trigger.tick()
    assert pool.get("WC2").state is State.RECORDING

    client.fail_with = "could not reach saar-seva"
    result = trigger.tick()

    assert result.ok is False
    assert result.finished == []
    assert pool.get("WC2").state is State.RECORDING


def test_recording_resumes_being_tracked_after_an_outage(trigger, client, pool):
    client.active = [op("WC2")]
    trigger.tick()

    client.fail_with = "offline"
    trigger.tick()
    client.fail_with = None

    result = trigger.tick()
    assert result.started == []  # still the same operation, not restarted
    assert pool.get("WC2").state is State.RECORDING


def test_a_manual_recording_is_never_taken_over(trigger, client, pool):
    """A technician recording by hand must not have their clip cut in half."""
    pool.get("WC2").start(JobLabels(mo_name="manual job"))

    client.active = [op("WC2", mo="WH/MO/42")]
    result = trigger.tick()

    assert result.started == []
    assert any("by hand" in s for s in result.skipped)
    assert pool.get("WC2").status()["labels"]["mo_name"] == "manual job"


def test_a_manual_recording_is_never_stopped(trigger, client, pool, catalogue):
    pool.get("WC2").start(JobLabels(mo_name="manual job"))
    client.active = []

    trigger.tick()

    assert pool.get("WC2").state is State.RECORDING
    assert catalogue.count() == 0


def test_benches_without_a_camera_are_skipped(trigger, client, pool):
    """saar-seva knows about work centers that have no camera."""
    client.active = [op("WC9")]  # not in work_centers
    result = trigger.tick()

    assert result.started == []
    assert result.active == 0


def test_two_operations_on_one_bench_records_the_first(trigger, client, pool):
    """One camera cannot record two jobs; this is a saar-seva data problem."""
    client.active = [op("WC2", mo="WH/MO/1", wo="1"), op("WC2", mo="WH/MO/2", wo="2")]
    trigger.tick()

    assert pool.get("WC2").status()["labels"]["mo_name"] == "WH/MO/1"


def test_a_failed_start_does_not_claim_the_bench(trigger, client, pool, camera):
    """If ffmpeg will not start, the next tick must try again."""
    pool._recorders["WC2"].backend = StubBackend(camera, fail="start")
    client.active = [op("WC2")]

    result = trigger.tick()
    assert result.started == []
    assert any("WC2" in s for s in result.skipped)

    pool._recorders["WC2"].backend = StubBackend(camera)
    assert trigger.tick().started == ["WC2"]


def test_nothing_usable_recorded_is_reported_not_crashed(trigger, client, pool, camera, catalogue):
    """The camera produced nothing. done() refuses; the trigger must survive."""
    client.active = [op("WC2")]
    trigger.tick()

    # The camera dies mid-clip: the capture in flight yields nothing usable, so
    # done() refuses rather than filing an empty recording.
    pool.get("WC2").backend.captures[-1].fail = True

    client.active = []
    result = trigger.tick()

    assert result.finished == []
    assert result.skipped
    assert catalogue.count() == 0


# --------------------------------------------------------------------------
# posting links back
# --------------------------------------------------------------------------


def test_posts_the_link_after_a_clip_is_filed(trigger, client, catalogue):
    client.active = [op("WC2")]
    trigger.tick()
    client.active = []
    result = trigger.tick()

    assert result.links_posted
    assert catalogue.list()[0].link_posted == 1


def test_a_failed_post_is_retried_next_tick(trigger, client, catalogue):
    client.active = [op("WC2")]
    trigger.tick()
    client.active = []
    client.post_fail_with = "Render is asleep"

    trigger.tick()
    assert catalogue.list()[0].link_posted == 0

    client.post_fail_with = None
    trigger.tick()
    assert catalogue.list()[0].link_posted == 1


def test_unlabelled_clips_are_not_posted(trigger, client, catalogue):
    """With no MO there is no chatter to post to."""
    catalogue.add(
        Recording(work_center="WC2", path="recordings/a.mp4", started_at=utcnow(), labels=JobLabels())
    )
    client.active = []
    trigger.tick()

    assert client.posted == []


def test_a_clip_is_never_posted_twice(trigger, client, catalogue):
    client.active = [op("WC2")]
    trigger.tick()
    client.active = []
    trigger.tick()
    trigger.tick()
    trigger.tick()

    assert len(client.posted) == 1


# --------------------------------------------------------------------------
# parsing what saar-seva sends
# --------------------------------------------------------------------------


def test_parses_the_wrapped_shape():
    ops = parse_active({"active": [{"work_center": "WC2", "mo_name": "WH/MO/1"}]})
    assert len(ops) == 1 and ops[0].work_center == "WC2"


def test_parses_a_bare_list():
    ops = parse_active([{"work_center": "WC2"}])
    assert len(ops) == 1


def test_empty_means_nothing_is_running():
    assert parse_active({"active": []}) == []
    assert parse_active([]) == []


def test_entries_without_a_work_center_are_dropped():
    """Without a bench there is no camera and nothing to record."""
    ops = parse_active([{"mo_name": "WH/MO/1"}, {"work_center": "WC2"}])
    assert [o.work_center for o in ops] == ["WC2"]


def test_unknown_fields_are_ignored():
    ops = parse_active([{"work_center": "WC2", "something_new": 1}])
    assert ops[0].work_center == "WC2"


def test_a_non_list_response_is_refused():
    """Garbage must not be read as 'nothing is running'."""
    with pytest.raises(SaarSevaError):
        parse_active({"active": "everything is fine"})


def test_identity_prefers_the_work_order_id():
    assert op(wo="987").key == "wo:987"
    assert op(wo="").key == "mo:WH/MO/42|op:Screen replacement"


def test_missing_fields_do_not_become_the_string_none():
    ops = parse_active([{"work_center": "WC2", "imei": None, "device": None}])
    assert ops[0].imei == ""
    assert ops[0].device == ""
