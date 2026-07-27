"""The SQLite catalogue: search, stats and re-labelling."""

from __future__ import annotations

from repaircam.catalogue import Catalogue, JobLabels, Recording, utcnow


def make(catalogue: Catalogue, **overrides) -> Recording:
    defaults = dict(
        work_center="WC2",
        path="recordings/2026-07-27/WC2_clip.mp4",
        started_at=utcnow(),
        duration_s=120.0,
        size_bytes=50 * 1_048_576,
        labels=JobLabels(mo_name="WH/MO/1", operation="Screen", device="Redmi", imei="111"),
    )
    defaults.update(overrides)
    return catalogue.add(Recording(**defaults))


def test_add_and_get_round_trip(catalogue: Catalogue):
    recording = make(catalogue)
    assert recording.id is not None

    loaded = catalogue.get(recording.id)
    assert loaded is not None
    assert loaded.labels.mo_name == "WH/MO/1"
    assert loaded.work_center == "WC2"
    assert loaded.duration_hms == "0:02:00"
    assert loaded.size_mb == 50.0


def test_list_filters_by_bench(catalogue: Catalogue):
    make(catalogue, work_center="WC2")
    make(catalogue, work_center="WC3")
    assert len(catalogue.list(work_center="WC3")) == 1
    assert len(catalogue.list()) == 2


def test_search_covers_every_label(catalogue: Catalogue):
    make(catalogue, labels=JobLabels(mo_name="WH/MO/7", device="Galaxy A14", technician="Ramesh"))
    make(catalogue, labels=JobLabels(mo_name="WH/MO/8", device="Redmi 12", technician="Sunil"))

    assert len(catalogue.list(search="Galaxy")) == 1
    assert len(catalogue.list(search="Ramesh")) == 1
    assert len(catalogue.list(search="WH/MO")) == 2


def test_find_every_clip_for_one_device(catalogue: Catalogue):
    """Looking up a device's whole repair history is the point of storing IMEI."""
    make(catalogue, labels=JobLabels(mo_name="WH/MO/1", imei="350123"))
    make(catalogue, labels=JobLabels(mo_name="WH/MO/2", imei="350123"))
    make(catalogue, labels=JobLabels(mo_name="WH/MO/3", imei="999999"))

    assert len(catalogue.list(imei="350123")) == 2


def test_relabel_an_untagged_clip(catalogue: Catalogue):
    recording = make(catalogue, labels=JobLabels())
    assert catalogue.stats()["unlabelled"] == 1

    catalogue.update_labels(recording.id, JobLabels(mo_name="WH/MO/99", operation="Battery"))

    assert catalogue.get(recording.id).labels.mo_name == "WH/MO/99"
    assert catalogue.stats()["unlabelled"] == 0


def test_stats_add_up(catalogue: Catalogue):
    make(catalogue, duration_s=1800.0, size_bytes=1_073_741_824)
    make(catalogue, duration_s=1800.0, size_bytes=1_073_741_824)

    stats = catalogue.stats()
    assert stats["clips"] == 2
    assert stats["hours"] == 1.0
    assert stats["gigabytes"] == 2.0


def test_events_are_kept_in_order(catalogue: Catalogue):
    catalogue.log_event("start", work_center="WC2", detail="first")
    catalogue.log_event("done", work_center="WC2", detail="second")

    events = catalogue.recent_events()
    assert [e["kind"] for e in events][:2] == ["done", "start"]


def test_events_can_be_filtered_by_bench(catalogue: Catalogue):
    catalogue.log_event("start", work_center="WC2")
    catalogue.log_event("start", work_center="WC3")
    assert len(catalogue.recent_events(work_center="WC3")) == 1


def test_delete_removes_the_row(catalogue: Catalogue):
    recording = make(catalogue)
    assert catalogue.delete(recording.id) is not None
    assert catalogue.get(recording.id) is None
    assert catalogue.count() == 0


def test_title_falls_back_to_the_filename(catalogue: Catalogue):
    recording = make(catalogue, labels=JobLabels(), path="recordings/2026-07-27/WC2_20260727.mp4")
    assert catalogue.get(recording.id).title == "WC2_20260727"


def test_link_posted_flag_for_odoo_writeback(catalogue: Catalogue):
    """Phase 5 marks a clip once its link reaches the MO chatter."""
    recording = make(catalogue)
    assert catalogue.get(recording.id).link_posted == 0
    catalogue.mark_link_posted(recording.id)
    assert catalogue.get(recording.id).link_posted == 1
