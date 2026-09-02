"""Video library adapter: Radarr (movies) and Sonarr (TV) over a shared media root.

Same shape as `MusicAdapter` — filesystem is the source of truth, every id is reached by
matching a file path, never a parent's configured `path`. Two differences that matter:

1. **Two managers over one root.** `/data/movies` is Radarr's and `/data/tv` is Sonarr's,
   but they share the volume with the download tree, so a plan can span both and each
   file must be routed to the manager that owns it.
2. **Hardlinks are the whole point.** movies, tv and downloads are one ext4 filesystem, so
   an import that hardlinked rather than copied means deleting the library file frees
   nothing until the download-side link goes too. The plan reports that split explicitly
   rather than claiming space it will not reclaim.

Hardlink discovery is limited to the configured download roots. A link outside those
roots remains counted as linked bytes and is not reported as reclaimed space.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.adapters.base import DeletePlan, StalePlanError, Step, TreeEntry
from app.arr import ArrClient, MediaFile
from app.scan import DENIED_NAMES, list_children, resolve_root, resolve_target

log = logging.getLogger(__name__)


@dataclass
class ArrIndex:
    """Managed files keyed by path relative to the media root, plus which *arr owns each."""

    by_rel_path: dict[str, tuple[str, MediaFile]] = field(default_factory=dict)

    @classmethod
    def build(cls, clients: dict[str, ArrClient], arr_root: str) -> ArrIndex:
        root = PurePosixPath(arr_root)
        index: dict[str, tuple[str, MediaFile]] = {}
        for flavour, client in clients.items():
            for media in client.media_files():
                try:
                    rel = PurePosixPath(media.path).relative_to(root)
                except ValueError:
                    log.warning("%s file outside %s: %s", flavour, arr_root, media.path)
                    continue
                index[str(rel)] = (flavour, media)
        return cls(by_rel_path=index)

    def under(self, rel_prefix: str) -> list[tuple[str, MediaFile]]:
        if rel_prefix in self.by_rel_path:
            return [self.by_rel_path[rel_prefix]]
        prefix = f"{rel_prefix}/" if rel_prefix else ""
        return [v for rel, v in self.by_rel_path.items() if rel.startswith(prefix)]


class VideoAdapter:
    key = "video"
    label = "Movies & TV"

    def __init__(
        self,
        root: str | Path,
        clients: dict[str, ArrClient],
        arr_root: str = "/data",
        link_roots: tuple[str, ...] = ("downloads",),
    ) -> None:
        self.root = resolve_root(root)
        self._clients = clients
        self._arr_root = arr_root
        # Directories under the root searched for other links to the same inode.
        self._link_roots = link_roots

    def index(self) -> ArrIndex:
        return ArrIndex.build(self._clients, self._arr_root)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def tree(self, path: str = "", index: ArrIndex | None = None) -> list[TreeEntry]:
        target = self.root if not path else resolve_target(self.root, path)
        idx = index if index is not None else self.index()
        entries: list[TreeEntry] = []
        for node in list_children(self.root, target):
            managed = idx.under(node.path)
            entries.append(
                TreeEntry(
                    name=node.name,
                    path=node.path,
                    is_dir=node.is_dir,
                    size=node.size,
                    file_count=node.file_count,
                    linked_bytes=node.linked_bytes,
                    ownership="managed" if managed else "orphan",
                    album_ids=sorted({m.parent_id for _, m in managed}),
                    track_file_ids=sorted(m.id for _, m in managed),
                    artists=sorted({m.parent_title for _, m in managed if m.parent_title}),
                )
            )
        return entries

    def _sibling_links(self, files: list[Path]) -> dict[Path, list[str]]:
        """Other paths under the root sharing an inode with one of ``files``.

        This is what turns "deleting this frees 8 GB" into the truth. Only files with
        ``st_nlink > 1`` are worth searching for, so the scan is skipped entirely when
        the selected files have no hardlinks.
        """
        wanted: dict[int, Path] = {}
        for f in files:
            try:
                st = f.lstat()
            except OSError:
                continue
            if st.st_nlink > 1:
                wanted[st.st_ino] = f
        if not wanted:
            return {}

        going = {str(f) for f in files}
        found: dict[Path, list[str]] = defaultdict(list)
        for link_root in self._link_roots:
            base = self.root / link_root
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in DENIED_NAMES]
                for filename in filenames:
                    candidate = Path(dirpath) / filename
                    try:
                        st = candidate.lstat()
                    except OSError:
                        continue
                    if st.st_ino in wanted and str(candidate) not in going:
                        found[wanted[st.st_ino]].append(str(candidate.relative_to(self.root)))
        return dict(found)

    def plan(self, paths: list[str], index: ArrIndex | None = None) -> DeletePlan:
        if not paths:
            raise ValueError("a plan needs at least one path")
        idx = index if index is not None else self.index()

        targets = [resolve_target(self.root, p) for p in paths]
        rels = sorted({str(t.relative_to(self.root)) for t in targets})
        pruned = [r for r in rels if not any(r != o and r.startswith(f"{o}/") for o in rels)]

        managed: dict[tuple[str, int], MediaFile] = {}
        for rel in pruned:
            for flavour, media in idx.under(rel):
                managed[(flavour, media.id)] = media
        managed_paths = {
            str(self.root / PurePosixPath(m.path).relative_to(self._arr_root))
            for m in managed.values()
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

        siblings = self._sibling_links(files)
        orphan_files = sorted(
            str(f.relative_to(self.root)) for f in files if str(f) not in managed_paths
        )

        steps: list[Step] = []
        for flavour in ("radarr", "sonarr"):
            ids = sorted(m.id for (fl, _), m in managed.items() if fl == flavour)
            if not ids:
                continue
            unmonitor_ids = sorted(
                {m.parent_id for (fl, _), m in managed.items() if fl == flavour}
                if flavour == "radarr"
                else {e for (fl, _), m in managed.items() if fl == flavour for e in m.episode_ids}
            )
            steps.append(
                Step(
                    kind=f"unmonitor_{flavour}",
                    description=(
                        f"Unmonitor {len(unmonitor_ids)} "
                        f"{'movie' if flavour == 'radarr' else 'episode'}(s) in "
                        f"{flavour.title()} so they are not re-downloaded"
                    ),
                    targets=[str(i) for i in unmonitor_ids],
                )
            )
            steps.append(
                Step(
                    kind=f"delete_files_{flavour}",
                    description=f"Delete {len(ids)} file(s) via {flavour.title()} bulk endpoint",
                    targets=[str(i) for i in ids],
                )
            )
        if orphan_files:
            steps.append(
                Step(
                    kind="unlink",
                    description=f"Unlink {len(orphan_files)} file(s) no *arr manages",
                    targets=orphan_files,
                )
            )
        if siblings:
            steps.append(
                Step(
                    kind="unlink_hardlinks",
                    description=(
                        f"Remove {sum(len(v) for v in siblings.values())} other hardlink(s) "
                        "in the download tree — without these the space is not freed"
                    ),
                    targets=sorted(p for v in siblings.values() for p in v),
                )
            )
        steps.append(
            Step(
                kind="rmdir",
                description="Remove directories left empty, never the library root",
                targets=pruned,
            )
        )

        warnings = [
            "There is no undo. Files are unlinked, not moved to a recycle bin.",
        ]
        if linked and not siblings:
            warnings.append(
                f"{linked} byte(s) are hardlinked but the other link is outside the searched "
                "download roots, so that space will NOT be freed."
            )
        if siblings:
            warnings.append(
                "Hardlinked copies in the download tree are included below. If this content "
                "is still seeding, removing them stops the seed."
            )
        if unreadable:
            warnings.append(
                f"{unreadable} directory/directories could not be read. The counts below are "
                "an UNDERCOUNT of what will be deleted."
            )

        from app.adapters.music import MusicAdapter  # shared digest implementation

        hardlink_targets = [self.root / rel for paths in siblings.values() for rel in paths]
        digest_targets = [*files, *hardlink_targets, *(self.root / rel for rel in pruned)]
        digest = MusicAdapter._digest(pruned, digest_targets, steps)
        confirm = Path(pruned[0]).name if len(pruned) == 1 else f"DELETE {len(pruned)} items"
        return DeletePlan(
            library=self.key,
            id=digest[:12],
            digest=digest,
            paths=pruned,
            file_count=len(files),
            total_bytes=total,
            # Hardlinked bytes become reclaimable only when the sibling links go too.
            reclaimable_bytes=reclaimable + (linked if siblings else 0),
            linked_bytes=linked,
            steps=steps,
            warnings=warnings,
            confirm_phrase=confirm,
        )

    def _collect_files(self, pruned: list[str]) -> tuple[list[Path], int]:
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
                dirnames[:] = [d for d in dirnames if d not in DENIED_NAMES]
                for filename in filenames:
                    if filename in DENIED_NAMES:
                        continue
                    entry = Path(dirpath) / filename
                    if not entry.is_symlink():
                        files.append(entry)
        return sorted(files), unreadable

    def execute(self, plan: DeletePlan, index: ArrIndex | None = None) -> DeletePlan:
        idx = index if index is not None else self.index()
        fresh = self.plan(plan.paths, index=idx)
        if fresh.digest != plan.digest:
            raise StalePlanError("the library changed since this plan was built; re-plan first")

        steps = {s.kind: s for s in fresh.steps}
        for flavour in ("radarr", "sonarr"):
            client = self._clients.get(flavour)
            if client is None:
                continue
            unmon = steps.get(f"unmonitor_{flavour}")
            if unmon:
                ids = [int(t) for t in unmon.targets]
                self._run(unmon, lambda c=client, i=ids: c.unmonitor(i))
            delete = steps.get(f"delete_files_{flavour}")
            if delete:
                ids = [int(t) for t in delete.targets]
                self._run(delete, lambda c=client, i=ids: c.delete_files(i))
        for kind in ("unlink", "unlink_hardlinks"):
            step = steps.get(kind)
            if step:
                self._run(step, lambda s=step: self._unlink_all(s.targets))
        if "rmdir" in steps:
            self._run(steps["rmdir"], lambda: self._prune_empty_dirs(fresh.paths))
        return fresh

    @staticmethod
    def _run(step: Step, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — reported per step, never fatal
            step.status = "failed"
            step.detail = str(exc)[:300]
            log.exception("step %s failed", step.kind)
        else:
            step.status = "ok"

    def _unlink_all(self, rel_paths: list[str]) -> None:
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
        for rel in rel_paths:
            target = self.root / rel
            if target.is_dir():
                for dirpath, _dirnames, _filenames in os.walk(target, topdown=False):
                    here = Path(dirpath)
                    try:
                        if any(here.iterdir()):
                            continue
                        here.rmdir()
                    except OSError as exc:
                        log.warning("cannot remove %s: %s", here, exc)
            current = target
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
