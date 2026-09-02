"""Execution tests. These assert on what is left on disk after the call."""

from __future__ import annotations

import json

import pytest

from app.adapters.base import StalePlanError
from app.adapters.music import MusicAdapter
from app.lidarr import TrackFile
from tests.test_music_tree import FakeLidarr


class RecordingLidarr(FakeLidarr):
    """FakeLidarr that performs the deletions Lidarr would, and records the calls."""

    def __init__(self, tracks, root):
        super().__init__(tracks)
        self.root = root
        self.deleted_ids: list[int] = []
        self.monitor_calls: list[tuple[list[int], bool]] = []
        self.fail_delete = False
        self.retired: list[int] = []

    def delete_track_files(self, ids):
        if self.fail_delete:
            raise RuntimeError("lidarr exploded")
        self.deleted_ids.extend(ids)
        by_id = {t.id: t for t in self._tracks}
        for i in ids:
            target = self.root / by_id[i].path.removeprefix("/music/")
            target.unlink(missing_ok=True)
        # trackFile/bulk removes the DB rows too, so a later index must not see them.
        self._tracks = [t for t in self._tracks if t.id not in set(ids)]

    def set_albums_monitored(self, ids, monitored):
        self.monitor_calls.append((list(ids), monitored))

    def retire_artist(self, artist_id):
        self.retired.append(artist_id)


@pytest.fixture
def library(tmp_path):
    """Mirrors Example Artist D' real shape: one loose track in the shared Shared folder."""
    root = tmp_path / "music"
    for rel, size in (
        ("Shared/loose-track.opus", 100),  # Example Artist D, loose in the junk drawer
        ("Shared/Album One - Example Artist A/01.opus", 200),  # a neighbour that must survive
        ("Shared/folder.jpg", 50),  # a neighbour that must survive
        ("Example Artist D/First Album/01.opus", 300),
        ("Example Artist D/First Album/cover.jpg", 25),
        ("Example Artist D/Second Album/01.opus", 400),
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
    return root


@pytest.fixture
def adapter(library):
    tracks = [
        TrackFile(1, "/music/Shared/loose-track.opus", 100, 33, 21, "Example Artist D"),
        TrackFile(
            2, "/music/Shared/Album One - Example Artist A/01.opus", 200, 99, 1, "Example Artist A"
        ),
        TrackFile(
            3, "/music/Example Artist D/First Album/01.opus", 300, 128, 21, "Example Artist D"
        ),
        TrackFile(
            4, "/music/Example Artist D/Second Album/01.opus", 400, 127, 21, "Example Artist D"
        ),
    ]
    return MusicAdapter(root=library, client=RecordingLidarr(tracks, library), lidarr_root="/music")


def run(adapter, paths):
    return adapter.execute(adapter.plan(paths))


def test_deleting_a_loose_track_in_the_shared_folder_spares_its_neighbours(adapter, library):
    """The case this whole service exists to get right."""
    result = run(adapter, ["Shared/loose-track.opus"])
    assert all(s.status == "ok" for s in result.steps)
    assert not (library / "Shared" / "loose-track.opus").exists()
    # Everything else in the junk drawer is untouched.
    assert (library / "Shared" / "Album One - Example Artist A" / "01.opus").exists()
    assert (library / "Shared" / "folder.jpg").exists()
    assert (library / "Shared").is_dir()


def test_shared_directory_is_not_removed_when_it_still_holds_other_files(adapter, library):
    run(adapter, ["Shared/loose-track.opus"])
    assert (library / "Shared").exists()


def test_album_delete_removes_managed_files_artwork_and_the_empty_directory(adapter, library):
    result = run(adapter, ["Example Artist D/First Album"])
    assert all(s.status == "ok" for s in result.steps)
    assert not (library / "Example Artist D" / "First Album").exists()
    # The artist directory survives because another album is still there.
    assert (library / "Example Artist D" / "Second Album" / "01.opus").exists()


def test_deleting_the_last_album_prunes_the_artist_directory_too(adapter, library):
    run(adapter, ["Example Artist D/First Album"])
    run(adapter, ["Example Artist D/Second Album"])
    assert not (library / "Example Artist D").exists()
    assert library.is_dir()  # never the root


def test_deleting_the_last_file_in_a_directory_prunes_it(adapter, library):
    """A *file* target: by sweep time it is already gone, so the code must walk up to
    its parent. It did not, and the parent was silently left behind — caught on the
    first real deletion, in /music/Shared."""
    lonely = library / "Example Artist D" / "Second Album" / "01.opus"
    assert lonely.exists()
    run(adapter, ["Example Artist D/Second Album/01.opus"])
    assert not (library / "Example Artist D" / "Second Album").exists()
    assert (library / "Example Artist D" / "First Album").exists()


def test_nested_empty_directories_are_pruned_bottom_up(adapter, library):
    """An artist folder is not empty while it still holds empty album folders.

    Caught on the real Example Artist D deletion: every file went, but 7 empty
    directories survived because the sweep only ever walked upward. Smoke + Mirrors
    nests further still ("12 Vinyl 01"/"12 Vinyl 02").
    """
    deep = library / "Example Artist D" / "Second Album" / "12 Vinyl 01"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "extra.opus").write_bytes(b"d" * 5)
    run(adapter, ["Example Artist D"])
    assert not (library / "Example Artist D").exists()
    assert library.is_dir()


def test_partial_artist_delete_does_not_retire_the_artist(adapter):
    """Evolve goes, LOOM stays — Example Artist D is still wanted."""
    result = run(adapter, ["Example Artist D/First Album"])
    assert "retire_artist" not in {s.kind for s in result.steps}
    assert adapter._client.retired == []


def test_removing_an_artists_last_file_retires_them(adapter, library):
    """Deleting every file is not enough on its own.

    Example Artist D had two albums with zero files that were still monitored, so
    soularr would have re-downloaded the artist on its next 6-hour cycle. Retiring
    unmonitors every album and stops monitorNewItems.
    """
    run(adapter, ["Shared/loose-track.opus"])
    run(adapter, ["Example Artist D/First Album"])
    result = run(adapter, ["Example Artist D/Second Album"])
    retire = next(s for s in result.steps if s.kind == "retire_artist")
    assert retire.status == "ok"
    assert retire.targets == ["21:Example Artist D"]
    assert adapter._client.retired == [21]
    # Kendrick still has a file in Shared, so he is untouched.
    assert 1 not in adapter._client.retired


def test_retiring_only_covers_artists_with_nothing_left(library):
    """Deleting Shared must not retire an artist who still has files elsewhere."""
    outside = library / "Example Artist A" / "Other Album" / "01.opus"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"k" * 10)
    tracks = [
        TrackFile(1, "/music/Shared/loose-track.opus", 100, 33, 21, "Example Artist D"),
        TrackFile(
            2, "/music/Shared/Album One - Example Artist A/01.opus", 200, 99, 1, "Example Artist A"
        ),
        TrackFile(5, "/music/Example Artist A/Other Album/01.opus", 10, 98, 1, "Example Artist A"),
    ]
    adapter = MusicAdapter(
        root=library, client=RecordingLidarr(tracks, library), lidarr_root="/music"
    )
    plan = adapter.plan(["Shared"])
    retired = {t for s in plan.steps if s.kind == "retire_artist" for t in s.targets}
    assert retired == {"21:Example Artist D"}  # every ID file is under Shared
    assert "1:Example Artist A" not in retired  # Other Album survives outside Shared


def test_unmonitor_runs_before_the_files_go(adapter):
    result = run(adapter, ["Example Artist D/First Album"])
    client = adapter._client
    assert client.monitor_calls == [([128], False)]
    assert client.deleted_ids == [3]
    assert [s.kind for s in result.steps][:2] == ["unmonitor", "delete_trackfiles"]


def test_orphan_artwork_is_unlinked_directly_not_sent_to_lidarr(adapter, library):
    run(adapter, ["Example Artist D/First Album"])
    assert adapter._client.deleted_ids == [3]  # the .opus only
    assert not (library / "Example Artist D" / "First Album" / "cover.jpg").exists()


def test_a_stale_plan_is_refused_before_anything_is_touched(adapter, library):
    plan = adapter.plan(["Example Artist D/First Album"])
    (library / "Example Artist D" / "First Album" / "02.opus").write_bytes(b"new")
    with pytest.raises(StalePlanError):
        adapter.execute(plan)
    assert (library / "Example Artist D" / "First Album" / "01.opus").exists()
    assert adapter._client.deleted_ids == []


def test_a_failing_lidarr_step_is_reported_not_raised(adapter, library):
    adapter._client.fail_delete = True
    result = run(adapter, ["Example Artist D/First Album"])
    failed = next(s for s in result.steps if s.kind == "delete_trackfiles")
    assert failed.status == "failed"
    assert "lidarr exploded" in failed.detail
    assert (library / "Example Artist D" / "First Album" / "01.opus").exists()
    # The unmonitor before it still succeeded and is reported as such.
    assert next(s for s in result.steps if s.kind == "unmonitor").status == "ok"


def test_execute_never_escapes_the_root(adapter):
    plan = adapter.plan(["Example Artist D/First Album"])
    plan.paths = ["../outside"]
    with pytest.raises(Exception):  # noqa: B017 — jail or stale-plan, both are refusals
        adapter.execute(plan)


def test_audit_entry_records_what_happened(adapter, tmp_path):
    from app import audit

    log_path = tmp_path / "audit.log"
    result = run(adapter, ["Example Artist D/First Album"])
    audit.record(log_path, result, actor="tester", confirmed="First Album")
    entry = json.loads(log_path.read_text().strip())
    assert entry["actor"] == "tester"
    assert entry["paths"] == ["Example Artist D/First Album"]
    assert entry["file_count"] == 2
    assert entry["total_bytes"] == 325
    assert {s["kind"]: s["status"] for s in entry["steps"]}["delete_trackfiles"] == "ok"
    assert audit.tail(log_path)[0]["plan_id"] == result.id
