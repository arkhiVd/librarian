"""Filesystem walk and the path jail.

Every deletable path in this service comes from ``resolve_target``. No other module
may construct one. See SPEC.md § Security and privacy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Names that must never appear in a tree or be accepted as a target, at any depth.
# ``.stversions`` can hold Syncthing version history and ``.stfolder`` is its folder
# marker. Both may sit directly under a library and must never appear as delete targets.
DENIED_NAMES = frozenset({".stfolder", ".stversions", ".stignore"})


class PathJailError(ValueError):
    """A candidate path is not a legal target under its root."""


def resolve_root(root: str | os.PathLike[str]) -> Path:
    """Resolve a configured library root, which must already exist as a real directory."""
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise PathJailError(f"root is not a directory: {root}")
    return resolved


def resolve_target(root: Path, candidate: str | os.PathLike[str]) -> Path:
    """Resolve ``candidate`` against ``root`` and prove it is a legal delete target.

    ``candidate`` may be relative to ``root`` or absolute. It must resolve to a strict
    descendant of ``root``, must exist, must not be a symlink at any level below the
    root, and must not touch a denied name.

    Symlinks are *refused*, never followed: following one would let a link inside
    /music point anywhere on the host and turn this into an arbitrary-delete primitive.
    """
    # A NUL byte makes every pathlib operation raise a bare ValueError. PathJailError
    # subclasses ValueError, so `except PathJailError` would NOT catch it and malformed
    # input would surface as a 500 instead of a 400. Reject it explicitly.
    text = os.fspath(candidate)
    if "\x00" in text:
        raise PathJailError("path contains a NUL byte")

    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root / raw

    # Reject denied names before resolution, so a symlink cannot smuggle one in.
    if DENIED_NAMES.intersection(joined.parts):
        raise PathJailError(f"denied name in path: {candidate}")

    try:
        resolved = joined.resolve()
    except (OSError, ValueError) as exc:
        # Anything pathlib refuses to resolve is illegal input, not a server fault.
        raise PathJailError(f"cannot resolve path: {candidate}") from exc

    if resolved == root:
        raise PathJailError("the library root is never a valid target")

    # ``relative_to`` on resolved paths is the containment check. Comparing strings
    # would let "/musicXX" pass as inside "/music".
    if not resolved.is_relative_to(root):
        raise PathJailError(f"path escapes the library root: {candidate}")

    if DENIED_NAMES.intersection(resolved.parts):
        raise PathJailError(f"denied name in resolved path: {candidate}")

    # Any symlink between the root and the target is a refusal, including the target
    # itself. ``resolved`` has already followed them, so walk the unresolved chain.
    probe = joined if joined.is_absolute() else joined.absolute()
    for parent in [probe, *probe.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise PathJailError(f"symlinks are not followed: {parent}")

    if not resolved.exists():
        raise PathJailError(f"path does not exist: {candidate}")

    return resolved


@dataclass
class Node:
    """One entry in a library tree."""

    name: str
    path: str  # relative to the root, the only form the API exposes
    is_dir: bool
    size: int = 0
    file_count: int = 0
    linked_bytes: int = 0  # bytes in files with st_nlink > 1, i.e. not reclaimable alone
    # Files that could not be stat'd. Non-zero means `size` is an UNDERCOUNT, which would
    # otherwise silently understate what a deletion is about to remove.
    unreadable: int = 0
    children: list[Node] = field(default_factory=list)


def _stat_file(path: Path) -> tuple[int, int]:
    """Return ``(size, linked_bytes)`` for one file."""
    st = path.lstat()
    return st.st_size, st.st_size if st.st_nlink > 1 else 0


def measure(root: Path, target: Path) -> Node:
    """Build a single node with recursive size, file count and hardlinked bytes.

    Hardlinked bytes are tracked because /music, /movies and the download tree share one
    ext4 volume: deleting a file with ``st_nlink > 1`` frees nothing until the other
    links go too. Reporting raw size as "space freed" would be a lie.
    """
    rel = str(target.relative_to(root))
    if target.is_file():
        size, linked = _stat_file(target)
        return Node(
            name=target.name, path=rel, is_dir=False, size=size, file_count=1, linked_bytes=linked
        )

    total = files = linked_total = unreadable = 0
    counter = {"n": 0}

    def _on_error(exc: OSError) -> None:
        # os.walk swallows directory-level errors by default, so an unreadable subtree
        # vanished from the totals with `unreadable` still reporting zero — exactly the
        # undercount this field exists to expose.
        counter["n"] += 1
        log.warning("cannot read directory during walk: %s", exc)

    for dirpath, dirnames, filenames in os.walk(target, followlinks=False, onerror=_on_error):
        dirnames[:] = [d for d in dirnames if d not in DENIED_NAMES]
        for filename in filenames:
            entry = Path(dirpath) / filename
            try:
                # is_symlink() stats too, so it belongs inside the guard: an unreadable
                # file must be counted, not raise out of the whole walk.
                if entry.is_symlink():
                    continue
                size, linked = _stat_file(entry)
            except OSError:
                # Counted, not swallowed: a silently-skipped file understates the size a
                # plan reports, and "space freed" is a number the user acts on.
                unreadable += 1
                log.warning("cannot stat %s", entry)
                continue
            total += size
            linked_total += linked
            files += 1
    return Node(
        name=target.name,
        path=rel,
        is_dir=True,
        size=total,
        file_count=files,
        linked_bytes=linked_total,
        unreadable=unreadable + counter["n"],
    )


def list_children(root: Path, target: Path) -> list[Node]:
    """One level of the tree under ``target``, denied names removed, sorted by name."""
    children = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if entry.name in DENIED_NAMES:
            continue
        try:
            if entry.is_symlink():
                continue
        except OSError:
            log.warning("cannot stat %s", entry)
            continue
        children.append(measure(root, entry))
    return children
