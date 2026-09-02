"""Plan and remove completed slskd download directories."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.base import DeletePlan, StalePlanError, Step
from app.adapters.music import MusicAdapter
from app.scan import DENIED_NAMES, measure, resolve_root, resolve_target
from app.slskd import SlskdClient, SlskdError

log = logging.getLogger(__name__)

# slskd's bookkeeping directory is never a valid cleanup target.
PROTECTED = frozenset({"failed_imports"})


@dataclass
class Leftover:
    name: str
    path: str
    size: int
    file_count: int
    age_days: float
    protected: bool = False
    transfer_ids: list[tuple[str, str]] = field(default_factory=list)


class DownloadsAdapter:
    key = "slskd"
    label = "slskd downloads"

    def __init__(self, root: str | Path, client: SlskdClient | None = None) -> None:
        self.root = resolve_root(root)
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _transfers_by_directory(self) -> dict[str, list[tuple[str, str]]]:
        """Map slskd's remote directory leaf to ``(username, transfer id)`` pairs."""
        if self._client is None:
            return {}
        try:
            transfers = self._client.downloads()
        except SlskdError:
            log.exception("cannot list slskd transfers; continuing without them")
            return {}
        mapping: dict[str, list[tuple[str, str]]] = {}
        for transfer in transfers:
            leaf = transfer.directory.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            mapping.setdefault(leaf, []).append((transfer.username, transfer.id))
        return mapping

    def leftovers(self, min_age_days: float = 0.0) -> list[Leftover]:
        """List download directories, newest last, with matching slskd records."""
        by_directory = self._transfers_by_directory()
        now = time.time()
        results: list[Leftover] = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            node = measure(self.root, entry)
            age = (now - entry.stat().st_mtime) / 86400
            if age < min_age_days:
                continue
            results.append(
                Leftover(
                    name=entry.name,
                    path=node.path,
                    size=node.size,
                    file_count=node.file_count,
                    age_days=round(age, 1),
                    protected=entry.name in PROTECTED,
                    transfer_ids=by_directory.get(entry.name, []),
                )
            )
        return results

    def plan(self, names: list[str]) -> DeletePlan:
        """Build a side-effect-free purge plan bound to files and transfer actions."""
        if not names:
            raise ValueError("a plan needs at least one directory")

        targets = [resolve_target(self.root, name) for name in names]
        if any(not target.is_dir() for target in targets):
            raise ValueError("slskd cleanup accepts directories only")
        rels = sorted({str(target.relative_to(self.root)) for target in targets})
        pruned = [
            rel
            for rel in rels
            if not any(rel != other and rel.startswith(f"{other}/") for other in rels)
        ]

        snapshot = self._snapshot(pruned)
        protected = [
            str(path.relative_to(self.root))
            for path in snapshot
            if PROTECTED.intersection(path.relative_to(self.root).parts)
        ]
        if protected:
            raise ValueError(f"protected directory cannot be deleted: {protected[0]}")
        denied = [
            str(path.relative_to(self.root))
            for path in snapshot
            if DENIED_NAMES.intersection(path.relative_to(self.root).parts)
        ]
        if denied:
            raise ValueError(f"Syncthing control path cannot be deleted: {denied[0]}")

        nodes = [measure(self.root, self.root / rel) for rel in pruned]
        total = sum(node.size for node in nodes)
        linked = sum(node.linked_bytes for node in nodes)
        unreadable = sum(node.unreadable for node in nodes)

        by_directory = self._transfers_by_directory()
        clear_targets = sorted(
            {
                json.dumps([rel, username, transfer_id], separators=(",", ":"))
                for rel in pruned
                for username, transfer_id in by_directory.get(Path(rel).name, [])
            }
        )
        steps = [
            Step(
                kind="rmtree",
                description=f"Delete {len(pruned)} completed download directorie(s)",
                targets=pruned,
            )
        ]
        if clear_targets:
            steps.append(
                Step(
                    kind="slskd_clear",
                    description=f"Clear {len(clear_targets)} matching slskd transfer record(s)",
                    targets=clear_targets,
                )
            )

        warnings = [
            "There is no undo. These directories and every entry below them are removed.",
        ]
        if linked:
            warnings.append(
                f"{linked} byte(s) are hardlinked elsewhere and will not be reclaimed "
                "by this purge."
            )
        if unreadable:
            warnings.append(
                f"{unreadable} entry or subtree could not be read. The displayed totals "
                "are an undercount."
            )

        digest = MusicAdapter._digest(pruned, snapshot, steps)
        return DeletePlan(
            library=self.key,
            id=digest[:12],
            digest=digest,
            paths=pruned,
            file_count=sum(node.file_count for node in nodes),
            total_bytes=total,
            reclaimable_bytes=total - linked,
            linked_bytes=linked,
            steps=steps,
            warnings=warnings,
            confirm_phrase=f"PURGE {len(pruned)}",
        )

    def execute(self, plan: DeletePlan) -> DeletePlan:
        """Rebuild and execute a purge plan, recording partial failures per step."""
        fresh = self.plan(plan.paths)
        if fresh.digest != plan.digest:
            raise StalePlanError(
                "the download tree changed since this plan was built; re-plan first"
            )

        steps = {step.kind: step for step in fresh.steps}
        delete_step = steps["rmtree"]
        planned_reclaimable: dict[str, int] = {}
        for rel in delete_step.targets:
            node = measure(self.root, self.root / rel)
            planned_reclaimable[rel] = node.size - node.linked_bytes
        deleted: set[str] = set()
        delete_failures: dict[str, str] = {}
        for rel in delete_step.targets:
            try:
                target = resolve_target(self.root, rel)
                if not target.is_dir():
                    raise ValueError("target is no longer a directory")
                shutil.rmtree(target)
                deleted.add(rel)
            except (OSError, ValueError) as exc:
                delete_failures[rel] = str(exc)[:200]
        delete_step.status = "failed" if delete_failures else "ok"
        delete_step.detail = json.dumps(delete_failures, sort_keys=True) if delete_failures else ""
        fresh.reclaimable_bytes = sum(planned_reclaimable[rel] for rel in deleted)

        clear_step = steps.get("slskd_clear")
        if clear_step is not None:
            clear_failures: dict[str, str] = {}
            cleared = 0
            for encoded in clear_step.targets:
                rel, username, transfer_id = json.loads(encoded)
                if rel not in deleted:
                    continue
                try:
                    if self._client is None:
                        raise SlskdError("slskd client is unavailable")
                    self._client.remove(username, transfer_id)
                    cleared += 1
                except SlskdError as exc:
                    clear_failures[encoded] = str(exc)[:200]
            clear_step.status = "failed" if clear_failures else "ok"
            detail = {"cleared": cleared, "failures": clear_failures}
            clear_step.detail = json.dumps(detail, sort_keys=True)

        return fresh

    def _snapshot(self, rels: list[str]) -> list[Path]:
        """Return every entry that ``rmtree`` would remove, without following symlinks."""
        entries: set[Path] = set()
        for rel in rels:
            target = self.root / rel
            entries.add(target)
            for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
                directory = Path(dirpath)
                entries.add(directory)
                for name in dirnames:
                    entries.add(directory / name)
                for name in filenames:
                    entries.add(directory / name)
        return sorted(entries, key=str)
