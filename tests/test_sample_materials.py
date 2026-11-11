"""Material sampling helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "sample_materials", ROOT / "scripts" / "sample_materials.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_discover_and_select(tmp_path) -> None:
    module = _module()
    raw = tmp_path / "某书" / "raw"
    raw.mkdir(parents=True)
    for name in ("ch001.md", "ch002.md", "ch003.md"):
        (raw / name).write_text("正文", encoding="utf-8")
    books = module._discover(tmp_path)
    assert [book for book, _ in books] == ["某书"]
    selected = module._select(books[0][1], 2)
    assert [path.name for path in selected] == ["ch001.md", "ch002.md"]
    assert module._chapter_number(Path("0007.md")) == 7
