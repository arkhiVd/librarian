"""Books adapter: Kavita's library on disk.

There is no manager to unmonitor, so deletion is files plus directory pruning.

1. **`.originals/`** may hold a source archive for each title. Deleting the readable
   copy can leave that archive behind. The plan offers a fuzzy-matched archive as a
   separate, visible step.
2. **No Kavita API call.** Kavita reconciles missing files during a scheduled scan, so
   a deleted series may remain visible until that scan runs.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from app.adapters.base import DeletePlan, StalePlanError, Step, TreeEntry
from app.scan import DENIED_NAMES, list_children, resolve_root, resolve_target

log = logging.getLogger(__name__)

ORIGINALS = ".originals"


def _normalise(name: str) -> str:
    """Strip extension, author prefix and punctuation so titles compare sensibly."""
    stem = Path(name).stem
    stem = re.sub(r"^.*?\s+-\s+", "", stem)  # "Example Author - Example Book" -> "Example Book"
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


class BooksAdapter:
    key = "books"
    label = "Books"

    def __init__(self, root: str | Path) -> None:
        self.root = resolve_root(root)

    def close(self) -> None:  # symmetry with the other adapters; nothing to close
        return

    def tree(self, path: str = "") -> list[TreeEntry]:
        target = self.root if not path else resolve_target(self.root, path)
        entries: list[TreeEntry] = []
        for node in list_children(self.root, target):
            if node.name == ORIGINALS:
                continue  # bookportal's archive store, surfaced per-title instead
            entries.append(
                TreeEntry(
                    name=node.name,
                    path=node.path,
                    is_dir=node.is_dir,
                    size=node.size,
                    file_count=node.file_count,
                    linked_bytes=node.linked_bytes,
                    ownership="managed",  # Kavita indexes everything under its root
                )
            )
        return entries

    def _matching_originals(self, titles: list[str]) -> list[str]:
        """Archives in `.originals/` that correspond to the titles being deleted.

        Matched on a normalised title rather than an exact filename, because bookportal
        writes `<author> - <title>.rar` while the series directory is just `<title>`.
        A close match is *offered*, never assumed — it is a separate step the preview
        shows in full.
        """
        originals_dir = self.root / ORIGINALS
        if not originals_dir.is_dir():
            return []
        wanted = [_normalise(t) for t in titles]
        found: list[str] = []
        for entry in sorted(originals_dir.iterdir()):
            if not entry.is_file() or entry.is_symlink():
                continue
            candidate = _normalise(entry.name)
            for target in wanted:
                if (
                    candidate == target
                    or difflib.SequenceMatcher(None, candidate, target).ratio() >= 0.85
                ):
                    found.append(str(entry.relative_to(self.root)))
                    break
        return found

    def plan(self, paths: list[str]) -> DeletePlan:
        if not paths:
            raise ValueError("a plan needs at least one path")
        targets = [resolve_target(self.root, p) for p in paths]
        rels = sorted({str(t.relative_to(self.root)) for t in targets})
        pruned = [r for r in rels if not any(r != o and r.startswith(f"{o}/") for o in rels)]

        files, unreadable = self._collect_files(pruned)
        total = reclaimable = linked = 0
        for f in files:
            st = f.lstat()
            total += st.st_size
            if st.st_nlink > 1:
                linked += st.st_size
            else:
                reclaimable += st.st_size

        # Exclude anything already scheduled by the main unlink step. Without this, a
        # target inside .originals lands in both steps and the second unlink fails on a
        # file that is already gone.
        already = {str(f.relative_to(self.root)) for f in files}
        originals = [
            o for o in self._matching_originals([Path(r).name for r in pruned]) if o not in already
        ]
        originals_bytes = sum((self.root / o).lstat().st_size for o in originals)

        steps = [
            Step(
                kind="unlink",
                description=f"Delete {len(files)} book file(s)",
                targets=[str(f.relative_to(self.root)) for f in files],
            )
        ]
        if originals:
            steps.append(
                Step(
                    kind="unlink_originals",
                    description=(
                        f"Also delete {len(originals)} source archive(s) in {ORIGINALS}/ "
                        f"({originals_bytes} bytes) — without these the space is not freed"
                    ),
                    targets=originals,
                )
            )
        steps.append(
            Step(
                kind="rmdir",
                description="Remove directories left empty, never the library root",
                targets=pruned,
            )
        )

        warnings = ["There is no undo. Files are unlinked, not moved to a recycle bin."]
        if originals:
            warnings.append(
                f"{ORIGINALS}/ archives matched by title are included. Check the list — the "
                "match is fuzzy, because bookportal names them '<author> - <title>'."
            )
        warnings.append(
            "Kavita reconciles on its scheduled scan, not a filesystem watcher, so the "
            "series may linger in its UI until the next run."
        )
        if unreadable:
            warnings.append(
                f"{unreadable} directory/directories could not be read. The counts are an "
                "UNDERCOUNT of what will be deleted."
            )

        from app.adapters.music import MusicAdapter  # shared digest implementation

        digest = MusicAdapter._digest(pruned, files, steps)
        confirm = Path(pruned[0]).name if len(pruned) == 1 else f"DELETE {len(pruned)} items"
        return DeletePlan(
            library=self.key,
            id=digest[:12],
            digest=digest,
            paths=pruned,
            file_count=len(files) + len(originals),
            total_bytes=total + originals_bytes,
            reclaimable_bytes=reclaimable + originals_bytes,
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

    def execute(self, plan: DeletePlan) -> DeletePlan:
        fresh = self.plan(plan.paths)
        if fresh.digest != plan.digest:
            raise StalePlanError("the library changed since this plan was built; re-plan first")
        steps = {s.kind: s for s in fresh.steps}
        for kind in ("unlink", "unlink_originals"):
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
                # .originals is never pruned away by a title deletion.
                if current.name == ORIGINALS:
                    break
                try:
                    if any(current.iterdir()):
                        break
                    current.rmdir()
                except OSError as exc:
                    log.warning("cannot remove %s: %s", current, exc)
                    break
                current = current.parent
