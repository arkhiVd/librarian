"""Music library adapter: filesystem tree joined against Lidarr.

The filesystem is the source of truth for what exists. Lidarr is consulted only to
answer "does Lidarr own this, and which ids would a delete act on". Every id is reached
by matching ``trackfile.path``, never ``artist.path``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.adapters.base import DeletePlan, StalePlanError, Step, TreeEntry
from app.lidarr import LidarrClient, TrackFile
from app.scan import DENIED_NAMES, list_children, resolve_root, resolve_target

log = logging.getLogger(__name__)


@dataclass
class LidarrIndex:
    """Lidarr's trackfiles keyed by their path relative to the library root.

    Built once per request. Rebuilding costs one request per artist, but a stale cache
    would be riskier for a deletion plan.
    """

    by_rel_path: dict[str, TrackFile] = field(default_factory=dict)

    @classmethod
    def build(cls, client: LidarrClient, lidarr_root: str) -> LidarrIndex:
        root = PurePosixPath(lidarr_root)
        index: dict[str, TrackFile] = {}
        for track in client.all_track_files():
            candidate = PurePosixPath(track.path)
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                # A trackfile outside the configured root: real, and worth seeing.
                log.warning("trackfile outside %s: %s", lidarr_root, track.path)
                continue
            index[str(rel)] = track
        return cls(by_rel_path=index)

    def under(self, rel_prefix: str) -> list[TrackFile]:
        """Every trackfile at or below ``rel_prefix`` (a path relative to the root)."""
        if rel_prefix in self.by_rel_path:
            return [self.by_rel_path[rel_prefix]]
        prefix = f"{rel_prefix}/" if rel_prefix else ""
        return [track for rel, track in self.by_rel_path.items() if rel.startswith(prefix)]


class MusicAdapter:
    key = "music"
    label = "Music"

    def __init__(self, root: str | Path, client: LidarrClient, lidarr_root: str = "/music") -> None:
        self.root = resolve_root(root)
        self._client = client
        self._lidarr_root = lidarr_root

    def index(self) -> LidarrIndex:
        return LidarrIndex.build(self._client, self._lidarr_root)

    def close(self) -> None:
        self._client.close()

    def tree(self, path: str = "", index: LidarrIndex | None = None) -> list[TreeEntry]:
        """One level of the library. ``path`` is relative to the root; "" is the root."""
        target = self.root if not path else resolve_target(self.root, path)
        idx = index if index is not None else self.index()

        entries: list[TreeEntry] = []
        for node in list_children(self.root, target):
            tracks = idx.under(node.path)
            album_ids = sorted({t.album_id for t in tracks if t.album_id})
            artists = sorted({t.artist_name for t in tracks if t.artist_name})
            entries.append(
                TreeEntry(
                    name=node.name,
                    path=node.path,
                    is_dir=node.is_dir,
                    size=node.size,
                    file_count=node.file_count,
                    linked_bytes=node.linked_bytes,
                    ownership="managed" if tracks else "orphan",
                    album_ids=album_ids,
                    track_file_ids=sorted(t.id for t in tracks),
                    artists=artists,
                )
            )
        return entries

    def plan(self, paths: list[str], index: LidarrIndex | None = None) -> DeletePlan:
        """Build a deletion plan. Writes nothing and calls no destructive API.

        Managed files are handed to Lidarr's ``trackFile/bulk`` so its database never ends
        up pointing at files that are gone; anything on disk that Lidarr does not know
        about is unlinked directly. A selection can contain both.
        """
        if not paths:
            raise ValueError("a plan needs at least one path")
        idx = index if index is not None else self.index()

        targets = [resolve_target(self.root, p) for p in paths]
        rels = sorted({str(t.relative_to(self.root)) for t in targets})

        # Drop any path already covered by an ancestor in the same selection, so a file
        # is never counted twice in the byte total or unlinked twice.
        pruned = [r for r in rels if not any(r != o and r.startswith(f"{o}/") for o in rels)]

        managed: dict[int, TrackFile] = {}
        for rel in pruned:
            for track in idx.under(rel):
                managed[track.id] = track
        managed_paths = {
            str(self.root / PurePosixPath(t.path).relative_to(self._lidarr_root))
            for t in managed.values()
        }

        files, unreadable = self._collect_files(pruned)

        total = reclaimable = linked = 0
        for f in files:
            st = f.lstat()
            total += st.st_size
            if st.st_nlink > 1:
                linked += st.st_size
            else:
                reclaimable += st.st_size

        orphan_files = sorted(
            str(f.relative_to(self.root)) for f in files if str(f) not in managed_paths
        )
        album_ids = sorted({t.album_id for t in managed.values() if t.album_id})
        artists = sorted({t.artist_name for t in managed.values() if t.artist_name})

        steps: list[Step] = []
        if album_ids:
            steps.append(
                Step(
                    kind="unmonitor",
                    description=(
                        f"Unmonitor {len(album_ids)} album(s) in Lidarr so soularr does not "
                        "re-download them"
                    ),
                    targets=[str(a) for a in album_ids],
                )
            )
        if managed:
            steps.append(
                Step(
                    kind="delete_trackfiles",
                    description=f"Delete {len(managed)} file(s) via Lidarr trackFile/bulk",
                    targets=sorted(str(t.relative_to(self.root)) for t in map(Path, managed_paths)),
                )
            )
        # If this removes everything an artist still has on disk, retiring the artist is
        # the only thing that actually stops soularr: albums with zero files stay
        # monitored and would be fetched on the next cycle.
        emptied = self._artists_fully_removed(managed.values(), idx)
        if emptied:
            steps.append(
                Step(
                    kind="retire_artist",
                    description=(
                        f"Retire {len(emptied)} artist(s) in Lidarr — unmonitor every album "
                        "(including ones with no files) and stop monitoring new releases"
                    ),
                    targets=[f"{aid}:{name}" for aid, name in sorted(emptied.items())],
                )
            )
        if orphan_files:
            steps.append(
                Step(
                    kind="unlink",
                    description=f"Unlink {len(orphan_files)} file(s) Lidarr does not manage",
                    targets=orphan_files,
                )
            )
        steps.append(
            Step(
                kind="rmdir",
                description="Remove directories left empty, never the library root",
                targets=pruned,
            )
        )

        warnings = self._warnings(pruned, idx, linked, artists)
        if unreadable:
            warnings.append(
                f"{unreadable} directory/directories could not be read. The file count and "
                "sizes below are an UNDERCOUNT of what will be deleted."
            )
        digest = self._digest(pruned, files, steps)
        confirm = Path(pruned[0]).name if len(pruned) == 1 else f"DELETE {len(pruned)} items"

        return DeletePlan(
            library=self.key,
            id=digest[:12],
            digest=digest,
            paths=pruned,
            file_count=len(files),
            total_bytes=total,
            reclaimable_bytes=reclaimable,
            linked_bytes=linked,
            steps=steps,
            warnings=warnings,
            confirm_phrase=confirm,
        )

    def execute(self, plan: DeletePlan, index: LidarrIndex | None = None) -> DeletePlan:
        """Run a plan. Irreversible.

        The digest is recomputed first and a mismatch aborts before anything is touched,
        so a plan cannot be applied to a tree that changed while the confirm dialog was
        open. After that each step is attempted independently: a failing Lidarr call must
        not leave the caller unsure whether the files went.
        """
        idx = index if index is not None else self.index()
        fresh = self.plan(plan.paths, index=idx)
        if fresh.digest != plan.digest:
            raise StalePlanError("the library changed since this plan was built; re-plan first")

        steps = {step.kind: step for step in fresh.steps}

        # Unmonitor first. If the process dies here the worst outcome is an unmonitored
        # album whose files still exist — harmless, and re-runnable.
        if "unmonitor" in steps:
            ids = [int(a) for a in steps["unmonitor"].targets]
            self._run(steps["unmonitor"], lambda: self._client.set_albums_monitored(ids, False))
        if "delete_trackfiles" in steps:
            managed = sorted(self._managed_ids(fresh.paths, idx))
            self._run(steps["delete_trackfiles"], lambda: self._client.delete_track_files(managed))
        if "retire_artist" in steps:
            artist_ids = [int(t.split(":", 1)[0]) for t in steps["retire_artist"].targets]
            self._run(steps["retire_artist"], lambda: self._retire_all(artist_ids))
        if "unlink" in steps:
            targets = steps["unlink"].targets
            self._run(steps["unlink"], lambda: self._unlink_all(targets))
        if "rmdir" in steps:
            self._run(steps["rmdir"], lambda: self._prune_empty_dirs(fresh.paths))
        return fresh

    @staticmethod
    def _run(step: Step, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — a failed step is reported, never fatal
            step.status = "failed"
            step.detail = str(exc)[:300]
            log.exception("step %s failed", step.kind)
        else:
            step.status = "ok"

    @staticmethod
    def _artists_fully_removed(going: Iterable[TrackFile], idx: LidarrIndex) -> dict[int, str]:
        """Artists whose every remaining trackfile is in this deletion."""
        doomed: dict[int, set[int]] = defaultdict(set)
        names: dict[int, str] = {}
        for track in going:
            doomed[track.artist_id].add(track.id)
            names[track.artist_id] = track.artist_name
        remaining: dict[int, set[int]] = defaultdict(set)
        for track in idx.by_rel_path.values():
            remaining[track.artist_id].add(track.id)
        return {aid: names[aid] for aid, ids in doomed.items() if ids >= remaining.get(aid, set())}

    def _collect_files(self, pruned: list[str]) -> tuple[list[Path], int]:
        """Every file a deletion would touch, plus a count of directories it could not read.

        ``rglob`` silently skips unreadable directories, which would understate the
        deletion in the preview the user approves. ``os.walk(onerror=…)`` surfaces them so
        the plan can warn instead of quietly lying about its own size.
        """
        files: list[Path] = []
        unreadable = 0

        def _on_error(exc: OSError) -> None:
            nonlocal unreadable
            unreadable += 1
            log.warning("cannot read directory while planning: %s", exc)

        for rel in pruned:
            target = self.root / rel
            if target.is_file():
                files.append(target)
                continue
            for dirpath, dirnames, filenames in os.walk(
                target, followlinks=False, onerror=_on_error
            ):
                # Denied names are filtered here as well as in the tree. Without this the
                # plan counts a file the jail will later refuse to unlink, so the preview
                # overstates the deletion and the unlink step fails partway through.
                dirnames[:] = [d for d in dirnames if d not in DENIED_NAMES]
                for filename in filenames:
                    if filename in DENIED_NAMES:
                        continue
                    entry = Path(dirpath) / filename
                    if not entry.is_symlink():
                        files.append(entry)
        return sorted(files), unreadable

    def _retire_all(self, artist_ids: list[int]) -> None:
        """Retire each artist independently; one failure must not skip the rest."""
        failures: list[str] = []
        for artist_id in artist_ids:
            try:
                self._client.retire_artist(artist_id)
            except Exception as exc:  # noqa: BLE001 — collected and reported per artist
                log.warning("cannot retire artist %s: %s", artist_id, exc)
                failures.append(f"{artist_id}: {exc}")
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(artist_ids)} artist(s) not retired: "
                + "; ".join(failures[:5])
            )

    def _managed_ids(self, paths: list[str], idx: LidarrIndex) -> set[int]:
        return {track.id for rel in paths for track in idx.under(rel)}

    def _unlink_all(self, rel_paths: list[str]) -> None:
        """Unlink files Lidarr does not manage. Each goes back through the path jail.

        Failures are collected per file rather than raised on the first one: aborting the
        batch would leave a half-deleted folder under a single ``failed`` status, with no
        way to tell which files actually went.
        """
        failures: list[str] = []
        for rel in rel_paths:
            try:
                resolve_target(self.root, rel).unlink()
            except (OSError, ValueError) as exc:
                log.warning("cannot unlink %s: %s", rel, exc)
                failures.append(f"{rel}: {exc}")
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(rel_paths)} file(s) not unlinked: "
                + "; ".join(failures[:5])
            )

    def _prune_empty_dirs(self, rel_paths: list[str]) -> None:
        """Remove directories left empty, bottom-up, never passing the library root.

        Lidarr's ``deleteEmptyFolders`` is false on this instance, so nothing else does
        this. It stops at the first non-empty parent — in /music/Eng and /music/Echo that
        is immediately, which is exactly right: those hold other artists' files.
        """
        for rel in rel_paths:
            target = self.root / rel
            # Descend first. An artist folder is not "empty" while it still holds empty
            # album folders, and albums here nest further (Smoke + Mirrors has two
            # "12 Vinyl 0N" subdirectories). Bottom-up, so children go before parents.
            if target.is_dir():
                for dirpath, _dirnames, _filenames in os.walk(target, topdown=False):
                    # Emptiness is checked live, not from os.walk's lists: bottom-up walk
                    # captures a parent's children before its children are removed, so
                    # `_dirnames` still names directories that no longer exist.
                    here = Path(dirpath)
                    try:
                        if any(here.iterdir()):
                            continue
                        here.rmdir()
                    except OSError as exc:
                        log.warning("cannot remove %s: %s", here, exc)

            current = target
            # By the time this runs the target is usually gone — Lidarr deleted it, or we
            # unlinked it. Walk up to the nearest surviving ancestor, or a file target's
            # parent directory never gets considered for pruning at all.
            while current != self.root and not current.is_dir():
                current = current.parent
            while current != self.root and current.is_relative_to(self.root):
                try:
                    if any(current.iterdir()):
                        break
                    current.rmdir()
                except OSError as exc:
                    log.warning("cannot remove %s: %s", current, exc)
                    break
                current = current.parent

    def _warnings(
        self, pruned: list[str], idx: LidarrIndex, linked: int, artists: list[str]
    ) -> list[str]:
        warnings = [
            "This folder is Syncthing receive-only. The deletion stays on this machine, but "
            "pressing 'Revert local changes' in the Syncthing UI would restore every file. "
            "Do not press it.",
            "There is no undo. Lidarr's Recycle Bin is not configured, so files are unlinked.",
        ]
        if linked:
            warnings.append(
                f"{linked} byte(s) are in hardlinked files and will not be freed until their "
                "other links go too."
            )
        shared = self.shared_directories(index=idx)
        for rel in pruned:
            top = rel.split("/", 1)[0]
            if top in shared and rel == top:
                warnings.append(
                    f"'{top}' holds files for {len(shared[top])} different artists "
                    f"({', '.join(shared[top])}). Deleting the whole folder removes all of them."
                )
        if len(artists) > 1:
            warnings.append(f"This selection spans {len(artists)} artists: {', '.join(artists)}.")
        return warnings

    @staticmethod
    def _digest(pruned: list[str], files: list[Path], steps: list[Step]) -> str:
        """Hash of everything the user was shown, so execute can refuse a changed plan.

        **The steps are part of this, not just the files.** A filesystem-only digest was
        the original bug: Lidarr adopting existing files changes no size and no mtime, so
        a plan the user approved as "unlink 2 orphan files" could execute as an album
        unmonitor, a bulk delete and a full artist retire under an identical digest. Any
        change in Lidarr-derived intent must invalidate the plan.
        """
        h = hashlib.sha256()
        for rel in pruned:
            h.update(rel.encode())
            h.update(b"\0")
        for f in sorted(files):
            st = f.lstat()
            h.update(str(f).encode())
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
            h.update(b"\0")
        h.update(b"steps\0")
        for step in steps:
            h.update(step.kind.encode())
            h.update(b"\0")
            for target in step.targets:
                h.update(target.encode())
                h.update(b"\0")
        return h.hexdigest()

    def shared_directories(self, index: LidarrIndex | None = None) -> dict[str, list[str]]:
        """Top-level directories holding more than one artist's files.

        ``/music/Echo`` and ``/music/Eng`` are the known cases: 1.7 G belonging to a dozen
        artists apiece. The UI flags these, because "delete this folder" is exactly the
        wrong mental model there.
        """
        idx = index if index is not None else self.index()
        by_dir: dict[str, set[str]] = defaultdict(set)
        for rel, track in idx.by_rel_path.items():
            top = rel.split("/", 1)[0]
            if track.artist_name:
                by_dir[top].add(track.artist_name)
        return {d: sorted(names) for d, names in by_dir.items() if len(names) > 1}
