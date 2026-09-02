"""slskd leftover sweep tests."""

from __future__ import annotations

import time

import pytest

from app.adapters.downloads import DownloadsAdapter
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
    for name, size in (("Homework (1997)", 100), ("Night Visions (2012)", 200)):
        d = root / name
        d.mkdir(parents=True)
        (d / "01.mp3").write_bytes(b"x" * size)
    (root / "failed_imports").mkdir()
    (root / "failed_imports" / "junk.mp3").write_bytes(b"j" * 10)
    return root


@pytest.fixture
def adapter(downloads):
    transfers = [
        # Remote paths are Windows-style and prefixed; matching is on the last segment.
        Transfer(
            "t1", "phunky", "@@aeghe\\music\\Daft Punk\\Homework (1997)", "01.mp3", "Completed"
        ),
        Transfer("t2", "other", "music\\ID\\Night Visions (2012)", "01.mp3", "Completed"),
    ]
    return DownloadsAdapter(root=downloads, client=FakeSlskd(transfers))


def test_leftovers_lists_directories_with_sizes_and_transfer_records(adapter):
    by_name = {item.name: item for item in adapter.leftovers()}
    assert by_name["Homework (1997)"].size == 100
    assert by_name["Homework (1997)"].transfer_ids == [("phunky", "t1")]
    assert by_name["Night Visions (2012)"].transfer_ids == [("other", "t2")]


def test_windows_style_remote_paths_match_local_directory_names(adapter):
    """slskd reports `@@user\\music\\Artist\\Album`; the local dir is just `Album`."""
    homework = next(i for i in adapter.leftovers() if i.name == "Homework (1997)")
    assert homework.transfer_ids == [("phunky", "t1")]


def test_failed_imports_is_flagged_protected(adapter):
    failed = next(i for i in adapter.leftovers() if i.name == "failed_imports")
    assert failed.protected is True


def test_purge_refuses_protected_directories(adapter, downloads):
    outcome = adapter.purge(["failed_imports"])
    assert outcome["failed_imports"] == "refused: protected"
    assert (downloads / "failed_imports").exists()


def test_purge_removes_the_directory_and_clears_slskd_records(adapter, downloads):
    outcome = adapter.purge(["Homework (1997)"])
    assert "deleted" in outcome["Homework (1997)"]
    assert not (downloads / "Homework (1997)").exists()
    assert adapter._client.removed == [("phunky", "t1")]
    assert (downloads / "Night Visions (2012)").exists()


def test_purge_refuses_paths_outside_the_download_root(adapter, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    outcome = adapter.purge(["../elsewhere", "/etc", "../../opt"])
    assert all("refused" in v for v in outcome.values())
    assert outside.exists()


def test_min_age_filter_excludes_recent_downloads(adapter, downloads):
    old = downloads / "Homework (1997)"
    long_ago = time.time() - 30 * 86400
    import os

    os.utime(old, (long_ago, long_ago))
    names = {i.name for i in adapter.leftovers(min_age_days=7)}
    assert names == {"Homework (1997)"}


def test_slskd_being_down_does_not_stop_the_listing(downloads):
    """The directories are the point; transfer records are a bonus."""
    adapter = DownloadsAdapter(root=downloads, client=FakeSlskd(fail=True))
    items = adapter.leftovers()
    assert len(items) == 3
    assert all(i.transfer_ids == [] for i in items)


def test_no_slskd_client_still_allows_purging_directories(downloads):
    adapter = DownloadsAdapter(root=downloads, client=None)
    outcome = adapter.purge(["Homework (1997)"])
    assert "deleted" in outcome["Homework (1997)"]
    assert not (downloads / "Homework (1997)").exists()
