"""The adapter protocol every library implements.

Music is the only implementation in Phase 1. Video (Jellyfin/Radarr/Sonarr/qBittorrent)
and books (Kavita) are later phases and must not require changing this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Ownership = Literal["managed", "orphan"]
StepStatus = Literal["pending", "ok", "skipped", "failed"]


class StalePlanError(RuntimeError):
    """The library changed between planning and executing, so the plan is void."""


@dataclass
class TreeEntry:
    """One row in the browser: a filesystem node plus what the library manager knows."""

    name: str
    path: str  # relative to the library root
    is_dir: bool
    size: int
    file_count: int
    linked_bytes: int
    ownership: Ownership
    # Populated for managed entries; the ids a delete would act on.
    album_ids: list[int] = field(default_factory=list)
    track_file_ids: list[int] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)


@dataclass
class Step:
    """One side effect in a plan, reported independently of the others."""

    kind: str  # "unmonitor" | "delete_trackfiles" | "unlink" | "rmdir" | "slskd_clear"
    description: str
    targets: list[str] = field(default_factory=list)
    status: StepStatus = "pending"
    detail: str = ""


@dataclass
class DeletePlan:
    """What a deletion would do. Produced by ``plan``, consumed by ``execute``.

    ``digest`` hashes the resolved paths, their sizes and mtimes, **and every step kind
    with its targets**. ``execute`` recomputes it and refuses a mismatch.

    The steps are in there deliberately. A filesystem-only digest was a real hole: Lidarr
    adopting existing files changes no size and no mtime, so a plan the user approved as
    "unlink 2 orphan files" could execute as an album unmonitor, a bulk delete and a full
    artist retire under an identical digest. Anything that changes what the plan *does*
    must invalidate it, not just anything that changes the bytes on disk.
    """

    library: str
    id: str
    digest: str
    paths: list[str]
    file_count: int
    total_bytes: int
    reclaimable_bytes: int
    linked_bytes: int
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)
    confirm_phrase: str = ""


class LibraryAdapter(Protocol):
    key: str
    label: str

    def tree(self, path: str) -> list[TreeEntry]:
        """One level of the library under ``path`` ("" is the root)."""
        ...

    def plan(self, paths: list[str]) -> DeletePlan:
        """Build a plan. Writes nothing, calls no destructive API."""
        ...

    def execute(self, plan: DeletePlan) -> DeletePlan:
        """Run a plan, returning it with each step's status filled in."""
        ...
