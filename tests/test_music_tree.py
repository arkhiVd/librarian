"""Music adapter tests.

The cases that matter are the ones the real library exhibits: files that live outside
their artist's folder, and a single directory holding several artists.
"""

from __future__ import annotations

import pytest

from app.adapters.music import LidarrIndex, MusicAdapter
from app.lidarr import TrackFile


class FakeLidarr:
    """Stands in for LidarrClient. Only ``all_track_files`` is used by the index."""

    def __init__(self, tracks: list[TrackFile]) -> None:
        self._tracks = tracks

    def all_track_files(self) -> list[TrackFile]:
        return self._tracks

    def close(self) -> None:
        pass


@pytest.fixture
def library(tmp_path):
    """Mirrors the shape of the real /music, including the Shared junk drawer."""
    root = tmp_path / "music"
    for rel in (
        "Example Artist A/Album One/01.opus",
        "Shared/Album Two/01.opus",
        "Shared/Album Three/01.opus",
        "Example Artist C/Album Four/01.opus",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 50)
    (root / ".stfolder").mkdir()
    return root


@pytest.fixture
def adapter(library):
    tracks = [
        TrackFile(1, "/music/Example Artist A/Album One/01.opus", 50, 10, 1, "Example Artist A"),
        TrackFile(2, "/music/Shared/Album Two/01.opus", 50, 11, 1, "Example Artist A"),
        TrackFile(3, "/music/Shared/Album Three/01.opus", 50, 12, 2, "Example Artist B"),
        # Example Artist C is deliberately absent: it is an orphan.
    ]
    return MusicAdapter(root=library, client=FakeLidarr(tracks), lidarr_root="/music")


def test_root_tree_tags_managed_and_orphan(adapter):
    entries = {e.name: e for e in adapter.tree("")}
    assert set(entries) == {"Example Artist A", "Shared", "Example Artist C"}  # .stfolder hidden
    assert entries["Example Artist A"].ownership == "managed"
    assert entries["Shared"].ownership == "managed"
    assert entries["Example Artist C"].ownership == "orphan"
    assert entries["Example Artist C"].track_file_ids == []


def test_managed_entry_carries_the_ids_a_delete_would_use(adapter):
    kendrick = next(e for e in adapter.tree("") if e.name == "Example Artist A")
    assert kendrick.track_file_ids == [1]
    assert kendrick.album_ids == [10]


def test_junk_drawer_directory_reports_every_artist_inside_it(adapter):
    """/music/Shared holds two artists; the tree must say so rather than imply one."""
    echo = next(e for e in adapter.tree("") if e.name == "Shared")
    assert echo.artists == ["Example Artist A", "Example Artist B"]
    assert echo.track_file_ids == [2, 3]


def test_shared_directories_flags_multi_artist_folders(adapter):
    shared = adapter.shared_directories()
    assert shared == {"Shared": ["Example Artist A", "Example Artist B"]}
    assert "Example Artist A" not in shared


def test_descending_into_a_shared_directory_separates_the_albums(adapter):
    entries = {e.name: e for e in adapter.tree("Shared")}
    assert entries["Album Three"].track_file_ids == [3]
    assert entries["Album Three"].artists == ["Example Artist B"]
    assert entries["Album Two"].artists == ["Example Artist A"]


def test_index_ignores_trackfiles_outside_the_configured_root(library, caplog):
    """A path Lidarr reports outside /music is dropped, loudly, not silently mapped."""
    tracks = [TrackFile(9, "/elsewhere/Thing/01.opus", 50, 1, 1, "Whoever")]
    index = LidarrIndex.build(FakeLidarr(tracks), "/music")
    assert index.by_rel_path == {}
    assert "trackfile outside" in caplog.text


def test_prefix_matching_does_not_leak_between_sibling_names(library):
    """ "Shared" must not match "SharedExtra" — under() splits on a path separator."""
    tracks = [
        TrackFile(1, "/music/Shared/Album Three/01.opus", 50, 12, 2, "Example Artist B"),
        TrackFile(2, "/music/SharedExtra/Other/01.opus", 50, 13, 3, "Someone"),
    ]
    index = LidarrIndex.build(FakeLidarr(tracks), "/music")
    assert [t.id for t in index.under("Shared")] == [1]
    assert [t.id for t in index.under("SharedExtra")] == [2]
