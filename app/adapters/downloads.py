"""slskd leftovers: completed download directories eligible for manual cleanup.

This is a separate root from the music library with its own path jail. It deliberately
does not share `MusicAdapter`: the semantics differ (no Lidarr ownership, age matters,
`failed_imports` is special) and conflating them would put the download tree one bug
away from the library's delete path.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.scan import measure, resolve_root, resolve_target
from app.slskd import SlskdClient, SlskdError

log = logging.getLogger(__name__)

# slskd's own bookkeeping directory. Deleting it is a slskd problem, not a disk-space one.
PROTECTED = frozenset({"failed_imports"})


@dataclass
class Leftover:
    name: str
    path: str
    size: int
    file_count: int
    age_days: float
    protected: bool = False
    transfer_ids: list[tuple[str, str]] = field(default_factory=list)  # (username, id)


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
        """slskd's remote directory names mapped to (username, transfer id) pairs.

        Remote paths are Windows-style (`@@user\\Music\\Artist\\Album`), so matching is
        done on the last path segment — the album folder name — which is what slskd uses
        for the local directory too.
        """
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
        """Every directory in the download tree, newest last, with its slskd records."""
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

    def purge(self, names: list[str]) -> dict[str, str]:
        """Remove named leftover directories and their slskd transfer records.

        Protected names are refused rather than skipped quietly — a caller asking to
        delete `failed_imports` has misunderstood something and should be told.
        """
        by_directory = self._transfers_by_directory()
        outcome: dict[str, str] = {}
        for name in names:
            # Resolve BEFORE checking protection. Checking the raw string let
            # `./failed_imports`, `failed_imports/`, `failed_imports/.` and
            # `sub/../failed_imports` all through, because resolve_target normalises them
            # but the string comparison never saw the normalised form.
            try:
                target = resolve_target(self.root, name)
            except ValueError as exc:
                outcome[name] = f"refused: {exc}"
                continue
            relative_parts = target.relative_to(self.root).parts
            if PROTECTED.intersection(relative_parts):
                # Covers both the directory itself and anything inside it.
                outcome[name] = "refused: protected"
                continue
            try:
                shutil.rmtree(target)
            except OSError as exc:
                outcome[name] = f"failed: {exc}"
                continue
            cleared = 0
            # Look transfers up by the resolved directory name, not the raw input, so
            # `./Album` clears the same records `Album` would.
            for username, transfer_id in by_directory.get(target.name, []):
                try:
                    self._client.remove(username, transfer_id)  # type: ignore[union-attr]
                    cleared += 1
                except (SlskdError, AttributeError) as exc:
                    log.warning("slskd record %s/%s not cleared: %s", username, transfer_id, exc)
            outcome[name] = f"deleted (slskd records cleared: {cleared})"
        return outcome
