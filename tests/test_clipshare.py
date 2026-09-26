"""Sharing a disputed clip: the poll payload, the streamed upload, the worker.

No network and no threads: the URL opener is injected and the sharer runs
its work inline.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from repaircam import clipfile, clipshare
from repaircam.clipshare import ClipSharer
from repaircam.saarseva import (
    ClipUpload,
    SaarSevaClient,
    SaarSevaConfig,
    SaarSevaError,
    _MultipartFile,
    load_config,
    parse_clip_uploads,
)


class FakeOpener:
    def __init__(self, payload=None):
        self.payload = payload
        self.requests = []
        self.bodies = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        data = request.data
        if hasattr(data, "read"):
            chunks = []
            while True:
                c = data.read(7)  # a deliberately odd block size
                if not c:
                    break
                chunks.append(c)
            self.bodies.append(b"".join(chunks))
        else:
            self.bodies.append(data)
        body = json.dumps(self.payload or {}).encode()

        @contextmanager
        def response():
            class R:
                def read(self_inner):
                    return body

            yield R()

        return response()


@pytest.fixture
def config() -> SaarSevaConfig:
    return SaarSevaConfig(base_url="https://example.test", api_key="token123",
                          link_base="http://192.168.0.50:8080")


# -- the poll payload ---------------------------------------------------------


def test_clip_uploads_are_read_off_the_packing_poll(config):
    opener = FakeOpener({"active": [], "clip_uploads": [
        {"upload_id": "u1", "repaircam_recording_id": 90, "kind": "open"},
        {"upload_id": "", "repaircam_recording_id": 91},          # no id: skipped
        {"upload_id": "u3", "repaircam_recording_id": "x"},       # bad id: skipped
    ]})
    client = SaarSevaClient(config, opener=opener)
    client.fetch_active_packing()
    assert client.last_clip_uploads == [ClipUpload(upload_id="u1", recording_id=90, kind="open")]


def test_an_older_server_without_the_key_changes_nothing(config):
    client = SaarSevaClient(config, opener=FakeOpener({"active": []}))
    client.last_clip_uploads = [ClipUpload("keep-me", 1)]
    client.fetch_active_packing()
    assert client.last_clip_uploads == [ClipUpload("keep-me", 1)]


def test_parse_ignores_nonsense():
    assert parse_clip_uploads(None) == []
    assert parse_clip_uploads({"clip_uploads": "no"}) == []


def test_the_shop_switch_defaults_on_and_can_be_turned_off(tmp_path: Path):
    path = tmp_path / "saarseva.yaml"
    path.write_text("saarseva:\n  base_url: https://x.test\n")
    assert load_config(path).share_disputed_clips is True
    path.write_text("saarseva:\n  base_url: https://x.test\n  share_disputed_clips: false\n")
    assert load_config(path).share_disputed_clips is False


# -- the streamed upload --------------------------------------------------------


def test_the_multipart_body_streams_the_file_whole(tmp_path: Path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00\x01VIDEO" * 5000)
    body = _MultipartFile([("key", "k/1.mp4"), ("policy", "p")], clip,
                          filename="c.mp4", mime="video/mp4")
    raw = b""
    while True:
        chunk = body.read(1000)
        if not chunk:
            break
        raw += chunk
    assert len(raw) == body.length, "Content-Length must be exactly what is sent"
    assert clip.read_bytes() in raw
    assert raw.index(b'name="key"') < raw.index(b'name="policy"') < raw.index(b'name="file"'), \
        "the signed fields go first, in Shopify's order, and the file last"
    assert raw.endswith(f"--{body.boundary}--\r\n".encode())


def test_upload_goes_to_the_slot_and_never_carries_the_service_token(config, tmp_path: Path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"abc" * 100)
    opener = FakeOpener({})
    client = SaarSevaClient(config, opener=opener)
    client.upload_to_slot({"url": "https://storage.example/upload",
                           "parameters": [{"name": "key", "value": "k"}]},
                          clip, filename="c.mp4")
    req = opener.requests[-1]
    assert req.full_url == "https://storage.example/upload"
    assert req.get_header("Authorization") is None, "the saar-seva token must never go to Google"
    assert int(req.get_header("Content-length")) == len(opener.bodies[-1])
    assert clip.read_bytes() in opener.bodies[-1]


def test_slot_confirm_and_failure_hit_the_right_endpoints(config):
    opener = FakeOpener({"url": "https://storage.example/upload", "parameters": []})
    client = SaarSevaClient(config, opener=opener)
    client.request_clip_slot("u1", size_bytes=42, filename="c.mp4")
    client.confirm_clip_upload("u1")
    client.report_clip_failed("u1", "gone", missing=True)
    urls = [r.full_url for r in opener.requests]
    assert urls == [
        "https://example.test/pack/clip-uploads/u1/slot",
        "https://example.test/pack/clip-uploads/u1/done",
        "https://example.test/pack/clip-uploads/u1/failed",
    ]
    assert json.loads(opener.bodies[0]) == {"size_bytes": 42, "filename": "c.mp4", "mime": "video/mp4"}
    assert json.loads(opener.bodies[2])["missing"] is True


# -- the worker ---------------------------------------------------------------


class FakeRecording:
    def __init__(self, rid, path="clips/c.mp4"):
        self.id = rid
        self.path = path
        self.archive_path = ""


class FakeCatalogue:
    def __init__(self, recordings):
        self.recordings = {r.id: r for r in recordings}
        self.kept = []
        self.events = []

    def get(self, rid):
        return self.recordings.get(rid)

    def set_keep(self, rid, keep=True):
        self.kept.append((rid, keep))

    def log_event(self, kind, **kw):
        self.events.append(kind)


class FakeClient:
    def __init__(self, *, slot_error=None, upload_error=None):
        self.slot_error = slot_error
        self.upload_error = upload_error
        self.calls = []

    def request_clip_slot(self, upload_id, **kw):
        self.calls.append(("slot", upload_id, kw))
        if self.slot_error:
            raise self.slot_error
        return {"url": "https://storage.example", "parameters": []}

    def upload_to_slot(self, slot, path, **kw):
        self.calls.append(("upload", str(path)))
        if self.upload_error:
            raise self.upload_error

    def confirm_clip_upload(self, upload_id):
        self.calls.append(("done", upload_id))
        return {}

    def report_clip_failed(self, upload_id, reason, *, missing=False):
        self.calls.append(("failed", upload_id, missing))


def _inline(fn):
    fn()


def _file(tmp_path, monkeypatch, exists=True):
    f = tmp_path / "c.mp4"
    if exists:
        f.write_bytes(b"x" * 1234)
    monkeypatch.setattr(clipshare.clipfile, "find_clip", lambda rec: f if exists else None)
    return f


def test_happy_path_keeps_then_uploads_exactly_that_file(tmp_path, monkeypatch):
    f = _file(tmp_path, monkeypatch)
    cat, client = FakeCatalogue([FakeRecording(90)]), FakeClient()
    sharer = ClipSharer(cat, client, spawn=_inline)
    assert sharer.offer([ClipUpload("u1", 90, "open")]) == 1
    assert sharer.results["u1"] == "shared"
    assert cat.kept == [(90, True)], "kept before anything else"
    kinds = [c[0] for c in client.calls]
    assert kinds == ["slot", "upload", "done"]
    assert client.calls[0][2]["size_bytes"] == 1234 and client.calls[1][1] == str(f)


def test_a_missing_file_is_still_kept_and_reported_as_final(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, exists=False)
    cat, client = FakeCatalogue([FakeRecording(90)]), FakeClient()
    ClipSharer(cat, client, spawn=_inline).offer([ClipUpload("u1", 90)])
    assert cat.kept == [(90, True)]
    assert client.calls == [("failed", "u1", True)]


def test_an_unknown_recording_is_reported_missing(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch)
    client = FakeClient()
    ClipSharer(FakeCatalogue([]), client, spawn=_inline).offer([ClipUpload("u1", 404)])
    assert client.calls == [("failed", "u1", True)]


def test_a_refused_slot_means_nobody_wants_it_any_more(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch)
    client = FakeClient(slot_error=SaarSevaError("closed", status=409))
    sharer = ClipSharer(FakeCatalogue([FakeRecording(90)]), client, spawn=_inline)
    sharer.offer([ClipUpload("u1", 90)])
    assert sharer.results["u1"].startswith("not wanted")
    assert [c[0] for c in client.calls] == ["slot"], "no failure reported for a closed dispute"


def test_a_failed_upload_is_reported_as_retryable(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch)
    client = FakeClient(upload_error=SaarSevaError("timed out"))
    ClipSharer(FakeCatalogue([FakeRecording(90)]), client, spawn=_inline).offer([ClipUpload("u1", 90)])
    assert client.calls[-1] == ("failed", "u1", False)


def test_the_shop_switch_off_uploads_nothing(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch)
    client = FakeClient()
    cat = FakeCatalogue([FakeRecording(90)])
    assert ClipSharer(cat, client, enabled=False, spawn=_inline).offer([ClipUpload("u1", 90)]) == 0
    assert client.calls == [] and cat.kept == []


def test_one_upload_is_never_queued_twice(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch)
    started = []
    sharer = ClipSharer(FakeCatalogue([FakeRecording(90)]), FakeClient(), spawn=started.append)
    assert sharer.offer([ClipUpload("u1", 90)]) == 1
    assert sharer.offer([ClipUpload("u1", 90)]) == 0, "still queued: not added again"
    assert len(started) == 1, "one worker, not one per poll"


# -- where the file is --------------------------------------------------------


def test_find_clip_refuses_a_path_outside_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIRCAM_DATA_DIR", str(tmp_path))
    with pytest.raises(clipfile.OutsideDataDir):
        clipfile.find_clip(FakeRecording(1, path="../../etc/passwd"))


def test_find_clip_finds_the_recorder_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIRCAM_DATA_DIR", str(tmp_path))
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / "c.mp4").write_bytes(b"x")
    assert clipfile.find_clip(FakeRecording(1)) == (tmp_path / "clips" / "c.mp4").resolve()
    assert clipfile.find_clip(FakeRecording(2, path="clips/gone.mp4")) is None


def test_the_tick_consumes_the_list_once():
    """A poll that fails next time must not replay an old request."""
    from types import SimpleNamespace

    from repaircam.trigger import Trigger

    offered = []
    fake = SimpleNamespace(
        client=SimpleNamespace(last_clip_uploads=[ClipUpload("u1", 90)]),
        clip_sharer=SimpleNamespace(offer=lambda ups: offered.append(ups) or len(ups)),
    )
    Trigger._share_clips(fake)
    Trigger._share_clips(fake)
    assert offered == [[ClipUpload("u1", 90)]] and fake.client.last_clip_uploads == []
