from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "create_demo.py"
SPEC = importlib.util.spec_from_file_location("create_demo", SCRIPT)
assert SPEC and SPEC.loader
create_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(create_demo)


def test_create_demo_writes_only_synthetic_files(tmp_path):
    base = tmp_path / "demo"
    created = create_demo.create_demo(base)

    assert len(created) == 3
    assert all(path.is_relative_to(base) for path in created)
    assert (base / "config").is_dir()
    assert (base / "music").is_dir()
    assert (base / "video").is_dir()
    assert (base / "slskd-downloads").is_dir()
    assert all("Synthetic demo" in path.read_text() for path in created)


def test_create_demo_refuses_a_nonempty_directory(tmp_path):
    (tmp_path / "real-file").write_text("do not touch")
    with pytest.raises(RuntimeError, match="refusing non-empty"):
        create_demo.create_demo(tmp_path)
    assert (tmp_path / "real-file").read_text() == "do not touch"
