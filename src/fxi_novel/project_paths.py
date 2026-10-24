"""Locate the data root that holds ``prompts/`` and ``config/``.

The knowledge base reads prompt files and writing-chain definitions by path.
Those files live next to the package in a source checkout, but inside the wheel
they are bundled as package data (``fxi_novel/_data``). Both layouts are
supported without assuming a fixed depth below ``site-packages``.
"""

from __future__ import annotations

import os
from pathlib import Path


BUNDLED_DATA_DIRECTORY = "_data"


def _contains_project_data(root: Path) -> bool:
    return (
        (root / "prompts" / "shared" / "decision-method.md").is_file()
        and (root / "config" / "writing_chains" / "v1.json").is_file()
    )


def project_root() -> Path:
    """Return the directory that contains ``prompts/`` and ``config/``.

    Precedence:

    1. ``FXI_NOVEL_ROOT`` when it points at a directory holding the data.
    2. ``fxi_novel/_data`` shipped inside the wheel.
    3. The source checkout (editable install or plain ``PYTHONPATH``), found by
       walking up from this module.

    When none of them matches, the bundled path is returned so the eventual
    ``FileNotFoundError`` names the location the caller should populate.
    """
    configured = os.environ.get("FXI_NOVEL_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
        if _contains_project_data(root):
            return root

    package = Path(__file__).resolve().parent
    bundled = package / BUNDLED_DATA_DIRECTORY
    if _contains_project_data(bundled):
        return bundled
    for candidate in package.parents:
        if _contains_project_data(candidate):
            return candidate
    return bundled
