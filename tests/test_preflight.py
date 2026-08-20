"""The pre-flight check.

It is the last thing anyone runs before a shop starts relying on this box, so
what matters is that it FAILS when it should. A check that is quietly lenient is
worse than no check — it converts "we did not look" into "we looked and it was
fine".
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from repaircam import cli, config, saarseva, storage
from repaircam.config import ConfigError


CAMERAS = """
cameras:
  WC2:
    name: "Bench 2"
    host: "192.168.1.184"
    password: "s3cret"
    odoo_workcenter_id: 2
"""

SAARSEVA = """
saarseva:
  base_url: "https://saar-seva-api.onrender.com"
  api_key: "token"
  link_base: "http://192.168.1.163:8080"
  work_centers: []
"""


class FakeClient:
    """A saar-seva that answers, unless told otherwise."""

    def __init__(self, config, **kw):
        self.config = config

    def fetch_active(self, ids=None):
        return []

    def post_heartbeat(self, benches, storage=None):
        return True

    def fetch_camera_config(self):
        return {"revision": 3, "cameras": [{"odoo_workcenter_id": 2}]}

    def fetch_storage_config(self):
        return {
            "revision": 2,
            "retention": {"default_days": 30, "by_source": {"repair": 30, "packing": 45}},
            "free_space": {"min_gb": 20, "warn_gb": 50},
        }


@pytest.fixture
def shop(tmp_path: Path, monkeypatch, data_root: Path):
    """A box that is fully set up, so each test can break one thing."""
    cams = tmp_path / "cameras.yaml"
    cams.write_text(CAMERAS)
    monkeypatch.setenv("REPAIRCAM_CAMERAS", str(cams))

    saar = tmp_path / "saarseva.yaml"
    saar.write_text(SAARSEVA)
    monkeypatch.setenv("REPAIRCAM_SAARSEVA", str(saar))

    store = tmp_path / "storage.yaml"
    archive = tmp_path / "archive"
    archive.mkdir()
    store.write_text(f"storage:\n  archive_dir: {archive}\n")
    monkeypatch.setenv("REPAIRCAM_STORAGE", str(store))

    # A box whose link_base really is one of its own addresses. Without this
    # the "healthy" fixture would carry a warning, and a fixture that is
    # already warning cannot prove a check fires.
    monkeypatch.setattr(
        config, "local_ipv4_addresses", lambda: {"127.0.0.1", "192.168.1.163"}
    )
    monkeypatch.setattr(cli.ffmpeg, "available", lambda: True)
    monkeypatch.setattr(cli.ffmpeg, "version", lambda: "ffmpeg 4.2.7")
    monkeypatch.setattr(saarseva, "SaarSevaClient", FakeClient)
    monkeypatch.setattr(
        storage, "disk_report",
        lambda *a, **k: storage.DiskReport(500, 900, 44, 277, "ok", "plenty"),
    )
    return {"cameras": cams, "saarseva": saar, "storage": store}


def run(**kw) -> int:
    args = argparse.Namespace(quick=True, timeout=5.0)
    for key, value in kw.items():
        setattr(args, key, value)
    return cli.cmd_preflight(args)


def test_a_healthy_box_passes(shop, capsys):
    assert run() == 0
    assert "Ready" in capsys.readouterr().out


def test_no_ffmpeg_fails(shop, monkeypatch, capsys):
    monkeypatch.setattr(cli.ffmpeg, "available", lambda: False)
    assert run() == 1
    assert "apt install" in capsys.readouterr().out


def test_a_full_disk_fails(shop, monkeypatch, capsys):
    monkeypatch.setattr(
        storage, "disk_report",
        lambda *a, **k: storage.DiskReport(3, 900, 99, 1, "full", "Only 3 GB left"),
    )
    assert run() == 1
    assert "3 GB left" in capsys.readouterr().out


def test_a_low_disk_only_warns(shop, monkeypatch, capsys):
    """Low is not a reason to refuse to open the shop; full is."""
    monkeypatch.setattr(
        storage, "disk_report",
        lambda *a, **k: storage.DiskReport(30, 900, 96, 16, "low", "30 GB left"),
    )
    assert run() == 0
    assert "WARN" in capsys.readouterr().out


def test_a_bench_with_no_work_centre_fails(shop, capsys):
    """It is fully configured in every other respect and will never record."""
    shop["cameras"].write_text(
        'cameras:\n  WC2:\n    name: "Bench 2"\n    host: "192.168.1.184"\n'
    )
    assert run() == 1
    assert "never auto-record" in capsys.readouterr().out


def test_a_bench_vetoed_by_work_centers_fails(shop, capsys):
    """The trap of the central camera list: everything looks right and the
    bench is excluded by a file on this box."""
    shop["saarseva"].write_text(SAARSEVA.replace("work_centers: []", 'work_centers: ["WC9"]'))
    assert run() == 1
    out = capsys.readouterr().out
    assert "WC2" in out and "Empty work_centers" in out


def test_pointing_at_staging_warns(shop, capsys):
    """Not a failure — staging is where it is tested. But going live against it
    by accident is exactly the mistake this command exists to catch."""
    shop["saarseva"].write_text(
        SAARSEVA.replace("saar-seva-api.onrender.com", "saar-seva-api-staging.onrender.com")
    )
    assert run() == 0
    out = capsys.readouterr().out
    assert "staging" in out and "WARN" in out


def test_a_rejected_api_key_fails_and_says_which_end(shop, monkeypatch, capsys):
    class Rejecting(FakeClient):
        def fetch_active(self, ids=None):
            raise saarseva.SaarSevaError("GET /trc/active failed: HTTP 401", status=401)

    monkeypatch.setattr(saarseva, "SaarSevaClient", Rejecting)
    assert run() == 1
    assert "token here and the one on the server differ" in capsys.readouterr().out


def test_an_unconfigured_server_says_so(shop, monkeypatch, capsys):
    """503 means REPAIRCAM_API_KEY is unset THERE, which looks nothing like a
    wrong token here."""
    class Unconfigured(FakeClient):
        def fetch_active(self, ids=None):
            raise saarseva.SaarSevaError("GET /trc/active failed: HTTP 503", status=503)

    monkeypatch.setattr(saarseva, "SaarSevaClient", Unconfigured)
    assert run() == 1
    assert "REPAIRCAM_API_KEY is not set on that server" in capsys.readouterr().out


def test_no_archive_warns_but_does_not_block(shop, capsys):
    shop["storage"].write_text("storage:\n  archive_dir: ''\n")
    assert run() == 0
    assert "ONLY copy" in capsys.readouterr().out


def test_an_unmounted_archive_fails(shop, capsys):
    shop["storage"].write_text("storage:\n  archive_dir: /nowhere/at/all\n")
    assert run() == 1
    assert "is not there" in capsys.readouterr().out


def test_no_central_camera_list_only_warns(shop, monkeypatch, capsys):
    """Hand-edited cameras.yaml is a legitimate way to run; it is just not the
    central list, and saying nothing would let someone believe it was."""
    class Empty(FakeClient):
        def fetch_camera_config(self):
            return {"revision": 0, "cameras": []}

    monkeypatch.setattr(saarseva, "SaarSevaClient", Empty)
    assert run() == 0
    assert "Nothing saved in the admin panel yet" in capsys.readouterr().out


def test_no_saarseva_config_warns_and_stops_early(shop, monkeypatch, capsys):
    """With no auto-trigger there is nothing further to check, but recording by
    hand still works — so this is not a failure."""
    monkeypatch.setattr(saarseva, "load_config", lambda *a, **k: (_ for _ in ()).throw(
        ConfigError("No saar-seva config")
    ))
    assert run() == 0
    assert "start every recording by hand" in capsys.readouterr().out


# --------------------------------------------------------------------------
# link_base: set is not the same as correct
# --------------------------------------------------------------------------


def test_a_link_base_pointing_at_another_machine_warns(shop, monkeypatch, capsys):
    """The shop's network was renumbered overnight and link_base still named
    the old address. preflight said 'link_base set — OK', because it was set.
    Every clip link posted to Odoo after that was a dead end, and nothing
    anywhere said so."""
    monkeypatch.setattr(
        config, "local_ipv4_addresses", lambda: {"127.0.0.1", "192.168.0.165"}
    )

    assert run() == 0  # a warning: the shop still records, links just break
    out = capsys.readouterr().out
    assert "link_base points at this machine" in out
    assert "192.168.0.165" in out
    assert "dead end" in out


def test_a_link_base_naming_this_machine_passes(shop, monkeypatch, capsys):
    monkeypatch.setattr(
        config, "local_ipv4_addresses", lambda: {"127.0.0.1", "192.168.1.163"}
    )
    assert run() == 0
    out = capsys.readouterr().out
    assert "dead end" not in out
    # And it must say OK, not WARN. The check carried warn=True unconditionally,
    # so a link_base that was exactly right still printed WARN for ever — and a
    # permanent warning against a healthy setting is one nobody reads the day it
    # means something.
    assert "OK   link_base points at this machine" in out
    assert "WARN link_base points at this machine" not in out


def test_a_link_base_by_hostname_is_accepted(shop, monkeypatch, capsys):
    """Naming the box rather than its address is the more robust way to do it,
    and must not be reported as wrong."""
    shop["saarseva"].write_text(
        SAARSEVA.replace("http://192.168.1.163:8080", "http://recorder.local:8080")
    )
    monkeypatch.setattr(
        config, "local_ipv4_addresses",
        lambda: {"127.0.0.1", "192.168.1.163", "recorder.local"},
    )
    assert run() == 0
    assert "dead end" not in capsys.readouterr().out


def test_an_unset_link_base_still_fails(shop, capsys):
    """Different fault, different message: nothing to post at all."""
    shop["saarseva"].write_text(SAARSEVA.replace(
        'link_base: "http://192.168.1.163:8080"', 'link_base: ""'
    ))
    assert run() == 1
    assert "would have no address" in capsys.readouterr().out


# --------------------------------------------------------------------------
# a shop that lost benches must not read as a small shop
# --------------------------------------------------------------------------


def test_benches_deleted_by_the_central_list_are_reported(shop, monkeypatch, capsys):
    monkeypatch.setattr(cli, "camera_removals", lambda *a, **k: {
        "at": "2026-07-31T12:25:31+00:00", "revision": 2, "benches": ["WC1", "WC2"],
    })

    assert run() == 0  # a warning, not a refusal — WC13 still records fine
    out = capsys.readouterr().out
    assert "benches removed by the central camera list" in out
    assert "WC1, WC2" in out
    assert "recording nothing" in out
    assert "--clear-removed" in out


def test_an_acknowledged_removal_stops_being_reported(shop, monkeypatch, capsys):
    monkeypatch.setattr(cli, "camera_removals", lambda *a, **k: {})
    assert run() == 0
    assert "removed by the central camera list" not in capsys.readouterr().out
