"""Prompt/config data must resolve in both checkout and installed layouts."""

from __future__ import annotations

from pathlib import Path

from fxi_novel import project_paths
from fxi_novel.prompt_loading import DECISION_METHOD_PATH
from fxi_novel.project_paths import project_root
from fxi_novel.writing.chain_versions import WritingChainVersions


def _populate(root: Path) -> None:
    (root / "prompts" / "shared").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "shared" / "decision-method.md").write_text("共享方法", encoding="utf-8")
    (root / "config" / "writing_chains").mkdir(parents=True, exist_ok=True)
    (root / "config" / "writing_chains" / "v1.json").write_text("{}", encoding="utf-8")


def test_checkout_root_holds_prompts_and_chain_definitions() -> None:
    root = project_root()

    assert (root / "prompts" / "shared" / "decision-method.md").is_file()
    assert (root / "config" / "writing_chains" / "v1.json").is_file()
    assert DECISION_METHOD_PATH.is_file()


def test_environment_override_wins_when_it_holds_the_data(tmp_path, monkeypatch) -> None:
    _populate(tmp_path)
    monkeypatch.setenv("FXI_NOVEL_ROOT", str(tmp_path))

    assert project_root() == tmp_path.resolve()


def test_environment_override_without_data_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FXI_NOVEL_ROOT", str(tmp_path))

    assert project_root() != tmp_path.resolve()
    assert (project_root() / "prompts" / "shared" / "decision-method.md").is_file()


def test_installed_layout_prefers_bundled_data(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FXI_NOVEL_ROOT", raising=False)
    package = tmp_path / "site-packages" / "fxi_novel"
    _populate(package / "_data")
    monkeypatch.setattr(project_paths, "__file__", str(package / "project_paths.py"))

    assert project_paths.project_root() == (package / "_data").resolve()


def test_chain_capture_reads_the_resolved_root(tmp_path) -> None:
    pin = WritingChainVersions(tmp_path / "snapshots").capture("v1")

    assert pin["version"] == "v1"
    assert pin["label"] == "版本1"
    assert len(pin["snapshot_sha256"]) == 64
    assert (tmp_path / "snapshots" / f"{pin['snapshot_sha256']}.json").is_file()
