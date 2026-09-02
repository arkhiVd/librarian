"""Deletion-plan tests.

The plan is what the user reads before agreeing to an irreversible action, so these
assert on what it *says*, not just that it runs.
"""

from __future__ import annotations

import pytest

from app.adapters.music import MusicAdapter
from app.lidarr import TrackFile
from app.scan import PathJailError
from tests.test_music_tree import FakeLidarr  # noqa: F401  (fixtures below reuse it)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    for rel, size in (
        ("Example Artist A/Album One/01.opus", 100),
        ("Shared/Album Two/01.opus", 200),
        ("Shared/Album Three/01.opus", 300),
        ("Shared/Pray for Paris/01.opus", 400),  # orphan sitting in the junk drawer
        ("Example Artist C/Album Four/01.opus", 500),
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
    return root


@pytest.fixture
def adapter(library):
    from tests.test_music_tree import FakeLidarr as Fake

    tracks = [
        TrackFile(1, "/music/Example Artist A/Album One/01.opus", 100, 10, 1, "Example Artist A"),
        TrackFile(2, "/music/Shared/Album Two/01.opus", 200, 11, 1, "Example Artist A"),
        TrackFile(3, "/music/Shared/Album Three/01.opus", 300, 12, 2, "Example Artist B"),
    ]
    return MusicAdapter(root=library, client=Fake(tracks), lidarr_root="/music")


def test_deleting_one_album_inside_a_shared_folder_touches_only_that_album(adapter):
    """The single most important behaviour in the service.

    /music/Shared holds several artists. A plan for one album inside it must not reach the
    neighbours — this is the case that would otherwise destroy other artists' music.
    """
    plan = adapter.plan(["Shared/Album Three"])
    assert plan.paths == ["Shared/Album Three"]
    assert plan.file_count == 1
    assert plan.total_bytes == 300
    targets = {t for step in plan.steps for t in step.targets}
    assert not any("good kid" in t or "Pray for Paris" in t for t in targets)


def test_plan_separates_managed_files_from_orphans(adapter):
    """A selection can contain both; each needs a different deletion path."""
    plan = adapter.plan(["Shared"])
    kinds = {s.kind: s for s in plan.steps}
    assert sorted(kinds["delete_trackfiles"].targets) == [
        "Shared/Album Three/01.opus",
        "Shared/Album Two/01.opus",
    ]
    assert kinds["unlink"].targets == ["Shared/Pray for Paris/01.opus"]
    assert plan.total_bytes == 900


def test_orphan_only_plan_has_no_lidarr_steps(adapter):
    plan = adapter.plan(["Example Artist C"])
    kinds = {s.kind for s in plan.steps}
    assert "unmonitor" not in kinds
    assert "delete_trackfiles" not in kinds
    assert "unlink" in kinds


def test_unmonitor_step_names_the_albums_that_would_be_re_downloaded(adapter):
    plan = adapter.plan(["Example Artist A"])
    unmonitor = next(s for s in plan.steps if s.kind == "unmonitor")
    assert unmonitor.targets == ["10"]


def test_overlapping_selection_is_pruned_so_bytes_are_not_double_counted(adapter):
    """Selecting a folder and a file inside it must not count the file twice."""
    plan = adapter.plan(["Shared", "Shared/Album Three"])
    assert plan.paths == ["Shared"]
    assert plan.total_bytes == 900


def test_sibling_prefix_is_not_treated_as_a_child(library):
    """ "Shared" must not swallow "SharedExtra" during pruning."""
    from tests.test_music_tree import FakeLidarr as Fake

    (library / "SharedExtra" / "Album").mkdir(parents=True)
    (library / "SharedExtra" / "Album" / "01.opus").write_bytes(b"z" * 10)
    adapter = MusicAdapter(root=library, client=Fake([]), lidarr_root="/music")
    plan = adapter.plan(["Shared", "SharedExtra"])
    assert plan.paths == ["Shared", "SharedExtra"]


def test_hardlinked_bytes_are_excluded_from_reclaimable(adapter, library):
    (library / "elsewhere.opus").hardlink_to(library / "Shared" / "Album Three" / "01.opus")
    plan = adapter.plan(["Shared/Album Three"])
    assert plan.total_bytes == 300
    assert plan.linked_bytes == 300
    assert plan.reclaimable_bytes == 0


def test_warnings_always_cover_syncthing_and_the_absence_of_undo(adapter):
    plan = adapter.plan(["Example Artist C"])
    joined = " ".join(plan.warnings)
    assert "Revert local changes" in joined
    assert "no undo" in joined.lower()


def test_deleting_a_whole_shared_folder_warns_about_the_other_artists(adapter):
    plan = adapter.plan(["Shared"])
    joined = " ".join(plan.warnings)
    assert "different artists" in joined
    assert "Example Artist B" in joined and "Example Artist A" in joined


def test_confirm_phrase_is_the_name_for_one_item_and_a_count_for_many(adapter):
    assert adapter.plan(["Shared/Album Three"]).confirm_phrase == "Album Three"
    assert (
        adapter.plan(["Shared/Album Three", "Example Artist C"]).confirm_phrase == "DELETE 2 items"
    )


def test_digest_changes_when_the_tree_changes_underneath_the_plan(adapter, library):
    before = adapter.plan(["Shared/Album Three"]).digest
    (library / "Shared" / "Album Three" / "02.opus").write_bytes(b"new")
    assert adapter.plan(["Shared/Album Three"]).digest != before


def test_plan_refuses_paths_outside_the_jail(adapter):
    for candidate in ("../outside", "/etc", ".stversions"):
        with pytest.raises(PathJailError):
            adapter.plan([candidate])


def test_empty_selection_is_rejected(adapter):
    with pytest.raises(ValueError):
        adapter.plan([])


def test_planning_never_calls_a_destructive_api(adapter, monkeypatch):
    """A plan is a promise that nothing happened yet."""

    def explode(*args, **kwargs):
        raise AssertionError("plan() must not call a destructive endpoint")

    monkeypatch.setattr(type(adapter._client), "all_track_files", lambda self: [], raising=False)
    for name in ("delete_track_files", "set_albums_monitored"):
        monkeypatch.setattr(type(adapter._client), name, explode, raising=False)
    adapter.plan(["Example Artist C"])
