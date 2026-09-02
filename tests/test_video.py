"""Video adapter tests.

The hardlink path is the reason this phase exists and there are no hardlinks on the real
box, so it is exercised here with real `os.link` calls against a tmpdir rather than mocks.
"""

from __future__ import annotations

import pytest

from app.adapters.base import StalePlanError
from app.adapters.video import VideoAdapter
from app.arr import MediaFile


class FakeArr:
    def __init__(self, flavour, files, root):
        self.flavour = flavour
        self._files = list(files)
        self.root = root
        self.deleted: list[int] = []
        self.unmonitored: list[int] = []
        self.fail = False

    def media_files(self):
        return list(self._files)

    def delete_files(self, ids):
        if self.fail:
            raise RuntimeError(f"{self.flavour} exploded")
        self.deleted.extend(ids)
        by_id = {f.id: f for f in self._files}
        for i in ids:
            (self.root / by_id[i].path.removeprefix("/data/")).unlink(missing_ok=True)
        self._files = [f for f in self._files if f.id not in set(ids)]

    def unmonitor(self, ids):
        self.unmonitored.extend(ids)

    def close(self):
        pass


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    for rel, size in (
        ("movies/Dridam (2026)/Dridam.mkv", 800),
        ("movies/Dridam (2026)/poster.jpg", 20),
        ("tv/Show/Season 01/Show S01E01.mkv", 400),
        ("downloads/complete/other.mkv", 50),
    ):
        t = root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x" * size)
    return root


@pytest.fixture
def adapter(media):
    radarr = FakeArr(
        "radarr",
        [MediaFile(1, "/data/movies/Dridam (2026)/Dridam.mkv", 800, 10, "Dridam")],
        media,
    )
    sonarr = FakeArr(
        "sonarr",
        [MediaFile(2, "/data/tv/Show/Season 01/Show S01E01.mkv", 400, 20, "Show", (99,))],
        media,
    )
    return VideoAdapter(root=media, clients={"radarr": radarr, "sonarr": sonarr}, arr_root="/data")


def test_tree_tags_radarr_and_sonarr_files_as_managed(adapter):
    by_name = {e.name: e for e in adapter.tree("")}
    assert by_name["movies"].ownership == "managed"
    assert by_name["tv"].ownership == "managed"
    assert by_name["downloads"].ownership == "orphan"


def test_movie_plan_routes_to_radarr_and_unmonitors_the_movie(adapter):
    plan = adapter.plan(["movies/Dridam (2026)"])
    kinds = {s.kind: s for s in plan.steps}
    assert kinds["unmonitor_radarr"].targets == ["10"]
    assert kinds["delete_files_radarr"].targets == ["1"]
    assert kinds["unlink"].targets == ["movies/Dridam (2026)/poster.jpg"]
    assert "unmonitor_sonarr" not in kinds


def test_episode_plan_unmonitors_episodes_not_the_series(adapter):
    """A series stays monitored for episodes never grabbed, so Sonarr needs episode ids."""
    plan = adapter.plan(["tv/Show"])
    kinds = {s.kind: s for s in plan.steps}
    assert kinds["unmonitor_sonarr"].targets == ["99"]
    assert kinds["delete_files_sonarr"].targets == ["2"]


def test_a_selection_spanning_both_managers_produces_steps_for_each(adapter):
    plan = adapter.plan(["movies/Dridam (2026)", "tv/Show"])
    kinds = {s.kind for s in plan.steps}
    assert {
        "unmonitor_radarr",
        "delete_files_radarr",
        "unmonitor_sonarr",
        "delete_files_sonarr",
    } <= kinds


# --- the hardlink path, the reason this phase exists ------------------------


def test_hardlinked_file_reports_the_download_side_link(adapter, media):
    link = media / "downloads" / "complete" / "Dridam.mkv"
    link.hardlink_to(media / "movies" / "Dridam (2026)" / "Dridam.mkv")
    plan = adapter.plan(["movies/Dridam (2026)"])
    step = next(s for s in plan.steps if s.kind == "unlink_hardlinks")
    assert step.targets == ["downloads/complete/Dridam.mkv"]
    assert plan.linked_bytes == 800
    # With the sibling scheduled for removal the space really is reclaimable.
    assert plan.reclaimable_bytes == 820
    assert any("still seeding" in w for w in plan.warnings)


def test_deleting_a_hardlinked_movie_removes_both_links(adapter, media):
    link = media / "downloads" / "complete" / "Dridam.mkv"
    link.hardlink_to(media / "movies" / "Dridam (2026)" / "Dridam.mkv")
    result = adapter.execute(adapter.plan(["movies/Dridam (2026)"]))
    assert all(s.status == "ok" for s in result.steps)
    assert not (media / "movies" / "Dridam (2026)").exists()
    assert not link.exists(), "the download-side link survived; space was not freed"


def test_unlinked_file_does_not_claim_reclaimable_space_it_cannot_free(adapter, media, tmp_path):
    """A link outside the searched download roots means the bytes are NOT reclaimed."""
    outside = tmp_path / "elsewhere.mkv"
    outside.hardlink_to(media / "movies" / "Dridam (2026)" / "Dridam.mkv")
    plan = adapter.plan(["movies/Dridam (2026)"])
    assert not any(s.kind == "unlink_hardlinks" for s in plan.steps)
    assert plan.linked_bytes == 800
    assert plan.reclaimable_bytes == 20  # only the poster
    assert any("will NOT be freed" in w for w in plan.warnings)


def test_no_hardlinks_means_no_extra_step_and_no_scan(adapter):
    plan = adapter.plan(["movies/Dridam (2026)"])
    assert not any(s.kind == "unlink_hardlinks" for s in plan.steps)
    assert plan.linked_bytes == 0
    assert plan.reclaimable_bytes == plan.total_bytes


# --- shared guarantees ------------------------------------------------------


def test_digest_covers_arr_derived_steps(adapter):
    approved = adapter.plan(["movies/Dridam (2026)"])
    adapter._clients["radarr"]._files = []
    changed = adapter.plan(["movies/Dridam (2026)"])
    assert changed.digest != approved.digest
    with pytest.raises(StalePlanError):
        adapter.execute(approved)


def test_a_failing_arr_step_is_reported_not_raised(adapter, media):
    adapter._clients["radarr"].fail = True
    result = adapter.execute(adapter.plan(["movies/Dridam (2026)"]))
    failed = next(s for s in result.steps if s.kind == "delete_files_radarr")
    assert failed.status == "failed"
    assert (media / "movies" / "Dridam (2026)" / "Dridam.mkv").exists()


def test_path_jail_applies_to_the_video_root(adapter):
    for candidate in ("../outside", "/etc", ".stversions"):
        with pytest.raises(ValueError):
            adapter.plan([candidate])
