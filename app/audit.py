"""Append-only audit log.

With hard delete there is no trash directory to inspect, so this file is the only
record that a deletion happened and what it touched. Entries are only ever appended —
never edited, never truncated in place. It does rotate at 5 MB, but rotation renames
rather than deletes, and only the fifth-oldest generation is ever dropped.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from app.adapters.base import DeletePlan

log = logging.getLogger(__name__)


class AuditWriteError(RuntimeError):
    """The pre-delete audit intent could not be stored durably."""


def record_intent(path: Path, plan: DeletePlan, actor: str, confirmed: str) -> None:
    """Write what is *about* to be deleted, before the first destructive call.

    Without this pair, a process killed mid-execute leaves the files gone and the log
    empty, which is indistinguishable from a run that never happened. An `intent` with
    no matching `outcome` signals an interrupted deletion.
    """
    _append(
        path,
        {
            "event": "intent",
            "ts": datetime.now(UTC).isoformat(),
            "actor": actor,
            "library": plan.library,
            "plan_id": plan.id,
            "digest": plan.digest,
            "confirmed_with": confirmed,
            "paths": plan.paths,
            "file_count": plan.file_count,
            "total_bytes": plan.total_bytes,
            "steps": [{"kind": s.kind, "targets": s.targets} for s in plan.steps],
        },
        strict=True,
    )


# Rotate at 5 MB, keep 5 generations. The log is the only record a deletion happened, so
# rotation never deletes the newest history — it renames, and only the oldest generation
# is ever dropped. At roughly 1 KB per entry that keeps ~25000 deletions reachable, which
# on a single-user box is years.
MAX_BYTES = 5 * 1024 * 1024
KEEP = 5


def _rotate_if_needed(path: Path) -> None:
    """Roll `audit.log` to `audit.log.1` … `.5` once it passes MAX_BYTES."""
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        oldest = path.with_suffix(path.suffix + f".{KEEP}")
        if oldest.exists():
            oldest.unlink()
        for n in range(KEEP - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{n}")
            if src.exists():
                src.rename(path.with_suffix(path.suffix + f".{n + 1}"))
        path.rename(path.with_suffix(path.suffix + ".1"))
    except OSError:
        # Never let rotation block a deletion from being recorded.
        log.exception("audit rotation failed for %s", path)


def _append(path: Path, entry: dict, *, strict: bool = False) -> None:
    """Append and fsync one JSONL record.

    Intent writes are strict because no destructive action has started yet. Outcome
    writes remain best-effort because raising after deletion would misreport what
    already happened.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        log.exception("AUDIT WRITE FAILED — not recorded: %s", entry)
        if strict:
            raise AuditWriteError("audit intent could not be written; deletion aborted") from exc


def record(path: Path, plan: DeletePlan, actor: str, confirmed: str) -> None:
    """Append one JSONL entry describing an executed plan.

    Failing to write the audit log must not hide the fact that files were deleted, so a
    write error is logged loudly and swallowed — by the time this runs the deletion has
    already happened, and raising here would misreport it as failed.
    """
    entry = {
        "event": "outcome",
        "ts": datetime.now(UTC).isoformat(),
        "actor": actor,
        "library": plan.library,
        "plan_id": plan.id,
        "digest": plan.digest,
        "confirmed_with": confirmed,
        "paths": plan.paths,
        "file_count": plan.file_count,
        "total_bytes": plan.total_bytes,
        "reclaimable_bytes": plan.reclaimable_bytes,
        "steps": [
            {"kind": s.kind, "status": s.status, "detail": s.detail, "targets": s.targets}
            for s in plan.steps
        ],
    }
    _append(path, entry)


def tail(path: Path, limit: int = 50) -> list[dict]:
    """The most recent entries, newest first. Malformed lines are skipped, not fatal.

    Reads the previous generation as well, so history does not appear to vanish in the
    moments after a rotation.
    """
    entries: list[dict] = []
    for candidate in (path.with_suffix(path.suffix + ".1"), path):
        if not candidate.exists():
            continue
        with candidate.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("skipping malformed audit line in %s", candidate.name)
    return entries[-limit:][::-1]
