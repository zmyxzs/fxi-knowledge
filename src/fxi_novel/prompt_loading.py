"""Expand the shared decision method only in prompts that explicitly opt in.

This loads new requests. Frozen messages belong to their original execution
and must not be passed through this loader during replay.
"""
from pathlib import Path

from .project_paths import project_root


DECISION_METHOD_MARKER = "{{decision_method}}"
DECISION_METHOD_PATH = project_root() / "prompts" / "shared" / "decision-method.md"


def load_prompt(path: str | Path, *, decision_method_path: str | Path | None = None) -> str:
    from .writing.chain_versions import selected_prompt
    frozen = selected_prompt(path)
    if frozen is not None:
        return frozen
    text = Path(path).read_text(encoding="utf-8").strip()
    if DECISION_METHOD_MARKER not in text:
        return text
    if text.count(DECISION_METHOD_MARKER) != 1:
        raise ValueError("a prompt must include the decision method exactly once")
    method = Path(decision_method_path or DECISION_METHOD_PATH).read_text(encoding="utf-8").strip()
    if not method or DECISION_METHOD_MARKER in method:
        raise ValueError("the shared decision method must be nonempty and cannot include itself")
    return text.replace(DECISION_METHOD_MARKER, method)
