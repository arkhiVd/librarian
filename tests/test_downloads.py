"""slskd cleanup tests using synthetic directories and transfer records."""

from __future__ import annotations

import os
import time

import pytest

from app.adapters.base import StalePlanError
from app.adapters.downloads import DownloadsAdapter
from app.scan import PathJailError
from app.slskd import SlskdError, Transfer


class FakeSlskd:
    def __init__(self, transfers=None, fail=False):
        self._transfers = transfers or []
        self.fail = fail
        self.removed: list[tuple[str, str]] = []

    def downloads(self):
        if self.fail:
            raise SlskdError("slskd down")
        return self._transfers

    def remove(self, username, transfer_id):
        self.removed.append((username, transfer_id))

    def close(self):
        pass


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "slskd"
    for name, size in (("Example Download A", 100), ("Example Download B", 200)):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "01.audio").write_bytes(b"x" * size)
    (root / "failed_imports").mkdir()
    (root / "failed_imports" / "failed.audio").write_bytes(b"j" * 10)
    return root


@pytest.fixture
def adapter(downloads):
    transfers = [
        Transfer(
            "t1",
            "user-a",
            "@@user\\music\\Example Artist\\Example Download A",
            "01.audio",
            "Completed",
        ),
        Transfer("t2", "user-b", "music\\Example Download B", "01.audio", "Completed"),
    ]
    return DownloadsAdapter(root=downloads, client=FakeSlskd(transfers))


def test_leftovers_lists_sizes_and_transfer_records(adapter):
    by_name = {item.name: item for item in adapter.leftovers()}
    assert by_name["Example Download A"].size == 100
    assert by_name["Example Download A"].transfer_ids == [("user-a", "t1")]
    assert by_name["Example Download B"].transfer_ids == [("user-b", "t2")]


def test_windows_style_remote_paths_match_local_directory_names(adapter):
    item = next(i for i in adapter.leftovers() if i.name == "Example Download A")
    assert item.transfer_ids == [("user-a", "t1")]


def test_failed_imports_is_flagged_and_refused(adapter, downloads):
    failed = next(i for i in adapter.leftovers() if i.name == "failed_imports")
    assert failed.protected is True
    with pytest.raises(ValueError, match="protected"):
        adapter.plan(["failed_imports"])
    assert (downloads / "failed_imports").exists()


def test_plan_lists_directory_and_transfer_actions(adapter):
    plan = adapter.plan(["Example Download A"])
    assert plan.confirm_phrase == "PURGE 1"
    assert [step.kind for step in plan.steps] == ["rmtree", "slskd_clear"]
    assert plan.file_count == 1
    assert plan.total_bytes == 100


def test_execute_removes_directory_and_clears_records(adapter, downloads):
    result = adapter.execute(adapter.plan(["Example Download A"]))
    assert [step.status for step in result.steps] == ["ok", "ok"]
    assert not (downloads / "Example Download A").exists()
    assert adapter._client.removed == [("user-a", "t1")]
    assert (downloads / "Example Download B").exists()


def test_plan_refuses_paths_outside_the_download_root(adapter, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for candidate in ("../elsewhere", "/etc", "../../srv"):
        with pytest.raises(PathJailError):
            adapter.plan([candidate])
    assert outside.exists()


def test_plan_refuses_nested_syncthing_control_paths(adapter, downloads):
    versions = downloads / "Example Download A" / ".stversions"
    versions.mkdir()
    (versions / "old.audio").write_bytes(b"history")

    with pytest.raises(ValueError, match="Syncthing control path"):
        adapter.plan(["Example Download A"])
    assert (versions / "old.audio").exists()


def test_changed_directory_invalidates_plan(adapter, downloads):
    approved = adapter.plan(["Example Download A"])
    (downloads / "Example Download A" / "02.audio").write_bytes(b"new")
    with pytest.raises(StalePlanError):
        adapter.execute(approved)
    assert (downloads / "Example Download A").exists()


def test_changed_transfer_actions_invalidate_plan(adapter, downloads):
    approved = adapter.plan(["Example Download A"])
    adapter._client._transfers.append(
        Transfer("t3", "user-c", "music\\Example Download A", "02.audio", "Completed")
    )
    with pytest.raises(StalePlanError):
        adapter.execute(approved)
    assert (downloads / "Example Download A").exists()


def test_replaced_file_with_restored_mtime_invalidates_plan(adapter, downloads):
    target = downloads / "Example Download A" / "01.audio"
    approved = adapter.plan(["Example Download A"])
    original = target.stat()
    replacement = target.with_suffix(".replacement")
    replacement.write_bytes(b"y" * original.st_size)
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(target)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    with pytest.raises(StalePlanError):
        adapter.execute(approved)
    assert target.exists()


def test_min_age_filter_excludes_recent_downloads(adapter, downloads):
    old = downloads / "Example Download A"
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))
    names = {item.name for item in adapter.leftovers(min_age_days=7)}
    assert names == {"Example Download A"}


def test_slskd_being_down_does_not_stop_listing(downloads):
    adapter = DownloadsAdapter(root=downloads, client=FakeSlskd(fail=True))
    items = adapter.leftovers()
    assert len(items) == 3
    assert all(item.transfer_ids == [] for item in items)


def test_no_slskd_client_still_allows_a_planned_purge(downloads):
    adapter = DownloadsAdapter(root=downloads, client=None)
    result = adapter.execute(adapter.plan(["Example Download A"]))
    assert [step.kind for step in result.steps] == ["rmtree"]
    assert result.steps[0].status == "ok"
    assert not (downloads / "Example Download A").exists()
