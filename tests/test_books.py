"""Books adapter tests using a synthetic library and source archives."""

from __future__ import annotations

import pytest

from app.adapters.base import StalePlanError
from app.adapters.books import BooksAdapter


@pytest.fixture
def books(tmp_path):
    """Mirrors the real library, including bookportal's .originals archive store."""
    root = tmp_path / "books"
    for rel, size in (
        ("Example Book by Example Author/Example Author - Example Book.epub", 100),
        ("Second Example Book/Meadows, Donella H - Second Example Book.epub", 200),
        (".originals/Laozi - Example Book by Example Author.rar", 500),
        (".originals/Unrelated - Some Other Book.rar", 700),
    ):
        t = root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x" * size)
    return root


@pytest.fixture
def adapter(books):
    return BooksAdapter(root=books)


def test_tree_hides_the_originals_store(adapter):
    names = {e.name for e in adapter.tree("")}
    assert names == {"Example Book by Example Author", "Second Example Book"}
    assert ".originals" not in names


def test_plan_finds_the_matching_source_archive(adapter):
    """bookportal names archives '<author> - <title>.rar'; the series dir is '<title>'."""
    plan = adapter.plan(["Example Book by Example Author"])
    step = next(s for s in plan.steps if s.kind == "unlink_originals")
    assert step.targets == [".originals/Laozi - Example Book by Example Author.rar"]
    # The archive counts toward what is actually freed.
    assert plan.total_bytes == 600
    assert plan.reclaimable_bytes == 600


def test_plan_does_not_match_an_unrelated_archive(adapter):
    plan = adapter.plan(["Second Example Book"])
    assert not any(s.kind == "unlink_originals" for s in plan.steps)
    assert plan.total_bytes == 200


def test_deleting_a_title_removes_the_book_and_its_archive(adapter, books):
    result = adapter.execute(adapter.plan(["Example Book by Example Author"]))
    assert all(s.status == "ok" for s in result.steps)
    assert not (books / "Example Book by Example Author").exists()
    assert not (books / ".originals" / "Laozi - Example Book by Example Author.rar").exists()
    # Neighbours and the archive store itself survive.
    assert (books / "Second Example Book").exists()
    assert (books / ".originals" / "Unrelated - Some Other Book.rar").exists()
    assert (books / ".originals").is_dir()


def test_originals_directory_is_never_pruned_away(adapter, books):
    """Even if emptied, .originals stays — bookportal expects it to exist."""
    (books / ".originals" / "Unrelated - Some Other Book.rar").unlink()
    adapter.execute(adapter.plan(["Example Book by Example Author"]))
    assert (books / ".originals").is_dir()


def test_originals_cannot_be_targeted_directly(adapter, books):
    """The store is not in the tree, so it should not be a delete target either."""
    plan = adapter.plan([".originals/Unrelated - Some Other Book.rar"])
    # It resolves (it is inside the root) but must not drag the whole store with it.
    result = adapter.execute(plan)
    assert all(s.status == "ok" for s in result.steps)
    assert (books / ".originals").is_dir()
    assert (books / "Example Book by Example Author").exists()


def test_warning_explains_kavita_is_scheduled_not_watched(adapter):
    plan = adapter.plan(["Second Example Book"])
    assert any("scheduled scan" in w for w in plan.warnings)


def test_fuzzy_match_is_flagged_as_fuzzy(adapter):
    plan = adapter.plan(["Example Book by Example Author"])
    assert any("match is fuzzy" in w for w in plan.warnings)


def test_digest_and_jail_behave_like_the_other_adapters(adapter, books):
    plan = adapter.plan(["Second Example Book"])
    (books / "Second Example Book" / "extra.epub").write_bytes(b"new")
    with pytest.raises(StalePlanError):
        adapter.execute(plan)
    for candidate in ("../outside", "/etc", ".stversions"):
        with pytest.raises(ValueError):
            adapter.plan([candidate])
