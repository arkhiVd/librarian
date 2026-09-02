#!/usr/bin/env python3
"""Create a disposable library containing synthetic book files."""

from __future__ import annotations

import argparse
from pathlib import Path

FILES = {
    "books/Example Book by Example Author/Example Author - Example Book.epub": (
        "Synthetic demo file. It is not a real ebook.\n"
    ),
    "books/Second Example Book/Second Example Book.epub": (
        "Synthetic demo file. It is not a real ebook.\n"
    ),
    "books/.originals/Example Author - Example Book.rar": (
        "Synthetic demo archive. It is plain text, not a real RAR file.\n"
    ),
}


def create_demo(base: Path) -> list[Path]:
    """Create empty mount directories and synthetic books under ``base``.

    Refuse a non-empty base so this helper can never overwrite or mix with a real
    library by accident.
    """
    base = base.resolve()
    if base.exists() and any(base.iterdir()):
        raise RuntimeError(f"refusing non-empty demo directory: {base}")

    for directory in ("config", "music", "video", "books", "slskd-downloads"):
        (base / directory).mkdir(parents=True, exist_ok=True)

    created = []
    for relative, content in FILES.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path("data"),
        help="demo mount directory, which must be empty (default: ./data)",
    )
    args = parser.parse_args()
    try:
        created = create_demo(args.base)
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"created {len(created)} synthetic files under {args.base.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
