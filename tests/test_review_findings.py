"""Regression tests for security and correctness findings from independent review.

Each test reproduces a finding that was proven against the real adapter. They exist to
stop the fix silently regressing, so they assert the behaviour, not the implementation.
"""

from __future__ import annotations

import json
import os

import pytest

from app import audit
from app.adapters.base import StalePlanError
from app.adapters.downloads import DownloadsAdapter
from app.adapters.music import MusicAdapter
from app.lidarr import TrackFile
from app.scan import measure, resolve_root
from tests.test_downloads import FakeSlskd
from tests.test_execute import RecordingLidarr


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    for rel, size in (("Artist/a.mp3", 10), ("Artist/b.mp3", 20)):
        t = root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x" * size)
    return root


# --- 1. HIGH: digest must cover Lidarr-derived steps ------------------------


def test_digest_changes_when_lidarr_adopts_files_without_touching_the_disk(library):
    """The reported failure: user approves 'unlink 2 orphan files', Lidarr adopts them
    between plan and execute, and an identical digest lets an unmonitor + bulk delete +
    artist retire run instead. Adoption changes no size and no mtime."""
    client = RecordingLidarr([], library)
    adapter = MusicAdapter(root=library, client=client, lidarr_root="/music")

    approved = adapter.plan(["Artist"])
    assert {s.kind for s in approved.steps} == {"unlink", "rmdir"}

    # Lidarr now claims both files. Nothing on disk changes.
    client._tracks = [
        TrackFile(1, "/music/Artist/a.mp3", 10, 7, 42, "Someone"),
        TrackFile(2, "/music/Artist/b.mp3", 20, 7, 42, "Someone"),
    ]
    after = adapter.plan(["Artist"])
    assert "delete_trackfiles" in {s.kind for s in after.steps}
    assert after.digest != approved.digest, "digest ignored a change in Lidarr-derived intent"


def test_execute_refuses_a_plan_whose_lidarr_steps_changed(library):
    client = RecordingLidarr([], library)
    adapter = MusicAdapter(root=library, client=client, lidarr_root="/music")
    approved = adapter.plan(["Artist"])

    client._tracks = [TrackFile(1, "/music/Artist/a.mp3", 10, 7, 42, "Someone")]
    with pytest.raises(StalePlanError):
        adapter.execute(approved)
    assert client.deleted_ids == []
    assert client.retired == []
    assert (library / "Artist" / "a.mp3").exists()


# --- 2. MEDIUM: intent must be recorded before the destructive steps --------


def test_intent_is_written_before_execution_so_a_crash_leaves_a_trace(library, tmp_path):
    log_path = tmp_path / "audit.log"
    adapter = MusicAdapter(root=library, client=RecordingLidarr([], library), lidarr_root="/music")
    plan = adapter.plan(["Artist"])

    audit.record_intent(log_path, plan, actor="tester", confirmed="Artist")
    # Simulate the process dying here: no outcome line is ever written.
    entries = [json.loads(x) for x in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["event"] == "intent"
    assert entries[0]["paths"] == ["Artist"]

    orphaned = [e for e in entries if e["event"] == "intent"] and not [
        e for e in entries if e["event"] == "outcome"
    ]
    assert orphaned, "an interrupted deletion must be visible as intent without outcome"


def test_intent_and_outcome_pair_up_on_a_normal_run(library, tmp_path):
    log_path = tmp_path / "audit.log"
    adapter = MusicAdapter(root=library, client=RecordingLidarr([], library), lidarr_root="/music")
    plan = adapter.plan(["Artist"])
    audit.record_intent(log_path, plan, actor="t", confirmed="Artist")
    audit.record(log_path, adapter.execute(plan), actor="t", confirmed="Artist")
    events = [json.loads(x)["event"] for x in log_path.read_text().splitlines()]
    assert events == ["intent", "outcome"]


# --- 3. MEDIUM: failed_imports protection was bypassable --------------------


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "slskd"
    (root / "failed_imports").mkdir(parents=True)
    (root / "failed_imports" / "junk.mp3").write_bytes(b"j" * 10)
    (root / "failed_imports" / "nested").mkdir()
    (root / "failed_imports" / "nested" / "x.mp3").write_bytes(b"n")
    return root


@pytest.mark.parametrize(
    "name",
    [
        "failed_imports",
        "./failed_imports",
        "failed_imports/",
        "failed_imports/.",
        "sub/../failed_imports",
        "failed_imports/nested",
    ],
)
def test_protected_directory_cannot_be_reached_by_any_spelling(downloads, name):
    (downloads / "sub").mkdir(exist_ok=True)
    adapter = DownloadsAdapter(root=downloads, client=FakeSlskd())
    outcome = adapter.purge([name])
    assert "refused" in outcome[name], f"{name!r} bypassed the protection"
    assert (downloads / "failed_imports").exists()
    assert (downloads / "failed_imports" / "junk.mp3").exists()


# --- 4. LOW: a denied name must not abort the rest of an unlink batch -------


def test_denied_name_under_a_target_is_excluded_from_the_plan(tmp_path):
    """Reachable as soon as Syncthing versions a subfolder."""
    root = tmp_path / "music"
    (root / "Artist" / "zz" / ".stfolder").mkdir(parents=True)
    (root / "Artist" / "zz" / ".stfolder" / "x").write_bytes(b"s")
    (root / "Artist" / "a.mp3").write_bytes(b"a" * 10)
    (root / "Artist" / "zzz.mp3").write_bytes(b"z" * 10)

    adapter = MusicAdapter(root=root, client=RecordingLidarr([], root), lidarr_root="/music")
    plan = adapter.plan(["Artist"])
    targets = [t for s in plan.steps if s.kind == "unlink" for t in s.targets]
    assert not any(".stfolder" in t for t in targets)
    assert plan.file_count == 2, "the denied file was counted in the preview"

    result = adapter.execute(plan)
    assert next(s for s in result.steps if s.kind == "unlink").status == "ok"
    assert not (root / "Artist" / "a.mp3").exists()
    assert not (root / "Artist" / "zzz.mp3").exists()


def test_one_unlink_failure_does_not_abandon_the_rest(tmp_path, monkeypatch):
    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    for n in ("a.mp3", "b.mp3", "c.mp3"):
        (root / "Artist" / n).write_bytes(b"x" * 5)
    adapter = MusicAdapter(root=root, client=RecordingLidarr([], root), lidarr_root="/music")

    real_unlink = os.unlink

    def flaky(p, *a, **k):
        if str(p).endswith("b.mp3"):
            raise PermissionError(p)
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(os, "unlink", flaky)
    result = adapter.execute(adapter.plan(["Artist"]))
    step = next(s for s in result.steps if s.kind == "unlink")
    assert step.status == "failed"
    # a and c still went; only b survived.
    assert not (root / "Artist" / "a.mp3").exists()
    assert not (root / "Artist" / "c.mp3").exists()
    assert (root / "Artist" / "b.mp3").exists()


# --- 5. LOW: unreadable directories must surface, not vanish ----------------


def test_unreadable_directory_is_counted_rather_than_silently_skipped(tmp_path):
    root = tmp_path / "music"
    locked = root / "Artist" / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden.mp3").write_bytes(b"h" * 100)
    (root / "Artist" / "visible.mp3").write_bytes(b"v" * 10)
    locked.chmod(0o000)
    try:
        node = measure(resolve_root(root), root / "Artist")
        assert node.unreadable >= 1, "an unreadable subtree vanished from the totals"
    finally:
        locked.chmod(0o755)


def test_plan_warns_when_it_could_not_read_everything(tmp_path):
    root = tmp_path / "music"
    locked = root / "Artist" / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden.mp3").write_bytes(b"h" * 100)
    (root / "Artist" / "visible.mp3").write_bytes(b"v" * 10)
    locked.chmod(0o000)
    try:
        adapter = MusicAdapter(root=root, client=RecordingLidarr([], root), lidarr_root="/music")
        plan = adapter.plan(["Artist"])
        assert any("UNDERCOUNT" in w for w in plan.warnings)
    finally:
        locked.chmod(0o755)


# --- 6. LOW: retire must not stop at the first failing artist ---------------


def test_one_failing_retire_does_not_skip_the_others(library):
    class PartialLidarr(RecordingLidarr):
        def retire_artist(self, artist_id):
            if artist_id == 2:
                raise RuntimeError("lidarr says no")
            self.retired.append(artist_id)

    tracks = [
        TrackFile(1, "/music/Artist/a.mp3", 10, 7, 1, "One"),
        TrackFile(2, "/music/Artist/b.mp3", 20, 8, 2, "Two"),
    ]
    (library / "Artist" / "c.mp3").write_bytes(b"c" * 5)
    tracks.append(TrackFile(3, "/music/Artist/c.mp3", 5, 9, 3, "Three"))

    client = PartialLidarr(tracks, library)
    adapter = MusicAdapter(root=library, client=client, lidarr_root="/music")
    result = adapter.execute(adapter.plan(["Artist"]))

    step = next(s for s in result.steps if s.kind == "retire_artist")
    assert step.status == "failed"
    assert "2" in step.detail
    assert client.retired == [1, 3], "a failing artist aborted the remaining retires"


# --- Phase 4: the audit log must not grow without bound -------------------


def test_audit_log_rotates_and_keeps_history(tmp_path):
    from app import audit

    log_path = tmp_path / "audit.log"
    log_path.write_text("x" * (audit.MAX_BYTES + 1))
    audit._append(log_path, {"event": "outcome", "marker": "after-rotation"})
    assert log_path.with_suffix(".log.1").exists(), "the old log was destroyed, not rotated"
    assert log_path.stat().st_size < audit.MAX_BYTES
    assert "after-rotation" in log_path.read_text()


def test_rotation_drops_only_the_oldest_generation(tmp_path):
    from app import audit

    log_path = tmp_path / "audit.log"
    for n in range(1, audit.KEEP + 1):
        log_path.with_suffix(f".log.{n}").write_text(f'{{"gen": {n}}}\n')
    log_path.write_text("x" * (audit.MAX_BYTES + 1))
    audit._append(log_path, {"event": "outcome"})
    assert json.loads(log_path.with_suffix(".log.2").read_text())["gen"] == 1
    assert log_path.with_suffix(f".log.{audit.KEEP}").exists()
    assert not log_path.with_suffix(f".log.{audit.KEEP + 1}").exists()


def test_tail_still_sees_entries_from_just_before_a_rotation(tmp_path):
    from app import audit

    log_path = tmp_path / "audit.log"
    log_path.with_suffix(".log.1").write_text('{"event":"outcome","plan_id":"older"}\n')
    log_path.write_text('{"event":"outcome","plan_id":"newer"}\n')
    ids = [e["plan_id"] for e in audit.tail(log_path)]
    assert ids == ["newer", "older"]
