"""Path jail tests.

This suite stands between a browser request and destructive filesystem operations.
Every case corresponds to a way the jail could be
escaped, not to a line of code that needed covering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scan import (
    DENIED_NAMES,
    Node,
    PathJailError,
    list_children,
    measure,
    resolve_root,
    resolve_target,
)


@pytest.fixture
def library(tmp_path):
    """A miniature /music: two artists, an album, a Syncthing dir, and a sibling outside."""
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.opus").write_bytes(b"x" * 100)
    (album / "02.opus").write_bytes(b"y" * 200)
    (root / ".stfolder").mkdir()
    (root / ".stversions" / "Artist").mkdir(parents=True)
    (root / ".stversions" / "Artist" / "old.opus").write_bytes(b"z" * 999)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.env").write_bytes(b"TOKEN=1")
    # A directory whose name merely prefixes the root: /musicXX must not count as inside /music.
    sibling = tmp_path / "musicXX"
    sibling.mkdir()
    (sibling / "decoy.opus").write_bytes(b"n")
    return root


def test_happy_path_relative_and_absolute(library):
    root = resolve_root(library)
    assert resolve_target(root, "Artist/Album") == library / "Artist" / "Album"
    assert resolve_target(root, str(library / "Artist" / "Album")) == library / "Artist" / "Album"


def test_root_itself_is_never_a_target(library):
    root = resolve_root(library)
    for candidate in (".", "", str(library), "Artist/.."):
        with pytest.raises(PathJailError):
            resolve_target(root, candidate)


def test_dotdot_traversal_is_rejected(library):
    root = resolve_root(library)
    for candidate in ("../outside", "Artist/../../outside", "../outside/secret.env"):
        with pytest.raises(PathJailError):
            resolve_target(root, candidate)


def test_absolute_path_injection_is_rejected(library, tmp_path):
    root = resolve_root(library)
    for candidate in (str(tmp_path / "outside"), "/etc/passwd", "/srv/private/.env"):
        with pytest.raises(PathJailError):
            resolve_target(root, candidate)


def test_sibling_directory_sharing_a_name_prefix_is_rejected(library, tmp_path):
    """/musicXX starts with the same characters as /music but is not inside it."""
    root = resolve_root(library)
    with pytest.raises(PathJailError):
        resolve_target(root, str(tmp_path / "musicXX" / "decoy.opus"))


def test_symlink_escaping_the_root_is_refused(library, tmp_path):
    root = resolve_root(library)
    (library / "Escape").symlink_to(tmp_path / "outside")
    with pytest.raises(PathJailError):
        resolve_target(root, "Escape")
    with pytest.raises(PathJailError):
        resolve_target(root, "Escape/secret.env")


def test_symlink_staying_inside_the_root_is_still_refused(library):
    """Refused, not followed: a link is never a delete target even when it resolves inside."""
    root = resolve_root(library)
    (library / "Alias").symlink_to(library / "Artist" / "Album")
    with pytest.raises(PathJailError):
        resolve_target(root, "Alias")


@pytest.mark.parametrize("denied", sorted(DENIED_NAMES))
def test_denied_names_are_rejected(library, denied):
    root = resolve_root(library)
    with pytest.raises(PathJailError):
        resolve_target(root, denied)
    with pytest.raises(PathJailError):
        resolve_target(root, f"{denied}/anything")


def test_stversions_is_rejected_even_when_it_exists_and_holds_data(library):
    """Syncthing version history under a library must not be reachable."""
    root = resolve_root(library)
    assert (library / ".stversions" / "Artist" / "old.opus").exists()
    with pytest.raises(PathJailError):
        resolve_target(root, ".stversions/Artist/old.opus")


@pytest.mark.parametrize("candidate", ["\x00", "Artist/\x00", "Artist/Album\x00.opus"])
def test_nul_byte_raises_pathjailerror_not_bare_valueerror(library, candidate):
    """pathlib raises a bare ValueError on NUL bytes.

    PathJailError subclasses ValueError, so ``except PathJailError`` would not catch the
    bare one and malformed input would surface as a 500 instead of a 400. Found in review.
    """
    root = resolve_root(library)
    with pytest.raises(PathJailError):
        resolve_target(root, candidate)


def test_nonexistent_path_is_rejected(library):
    root = resolve_root(library)
    with pytest.raises(PathJailError):
        resolve_target(root, "Artist/No Such Album")


def test_resolve_root_requires_a_real_directory(tmp_path):
    with pytest.raises(PathJailError):
        resolve_root(tmp_path / "missing")


def test_measure_counts_sizes_and_files(library):
    root = resolve_root(library)
    node = measure(root, resolve_target(root, "Artist/Album"))
    assert isinstance(node, Node)
    assert node.is_dir and node.file_count == 2 and node.size == 300
    assert node.linked_bytes == 0


def test_measure_reports_hardlinked_bytes_separately(library):
    """Deleting a hardlinked file frees nothing until its other links go."""
    root = resolve_root(library)
    album = library / "Artist" / "Album"
    (library / "Artist" / "linked.opus").hardlink_to(album / "01.opus")
    node = measure(root, resolve_target(root, "Artist/Album"))
    assert node.size == 300
    assert node.linked_bytes == 100


def test_unreadable_files_are_counted_not_silently_skipped(library, monkeypatch):
    """A stat failure must show up, because `size` is the number a deletion is judged on."""
    real_lstat = Path.lstat

    def flaky(self):
        if self.name == "02.opus":
            raise PermissionError(self)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", flaky)
    root = resolve_root(library)
    node = measure(root, root / "Artist" / "Album")
    assert node.unreadable == 1
    assert node.file_count == 1
    assert node.size == 100  # the undercount is visible via `unreadable`, not hidden


def test_list_children_hides_denied_names_and_symlinks(library, tmp_path):
    root = resolve_root(library)
    (library / "Escape").symlink_to(tmp_path / "outside")
    names = {child.name for child in list_children(root, root)}
    assert names == {"Artist"}
