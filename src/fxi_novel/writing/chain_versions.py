"""Task-local writing chains with content-addressed prompt snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping


SOURCE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VERSION = "v1"
VERSION_LABELS = {"v1": "版本1", "v2": "版本2"}
_ACTIVE: ContextVar[dict[str, Any] | None] = ContextVar("writing_chain", default=None)


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def chain_feature(name: str) -> bool:
    active = _ACTIVE.get()
    return bool(active and active["features"].get(name))


def restored_planning_stage(stage: str) -> bool:
    base = stage.removesuffix("_json_retry")
    return chain_feature("chapter_approach") and base in {
        "chapter_planning_approach", "chapter_planning_approach_repair",
    }


def current_chain_pin() -> dict[str, str] | None:
    active = _ACTIVE.get()
    return copy.deepcopy(active["pin"]) if active else None


@contextmanager
def chain_context(snapshot: dict[str, Any] | None):
    token = _ACTIVE.set(snapshot)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def selected_prompt(path: str | Path) -> str | None:
    active = _ACTIVE.get()
    if active is None:
        return None
    try:
        key = Path(path).resolve().relative_to(Path(active["prompt_root"])).as_posix()
    except ValueError:
        return None
    return active["prompts"].get(key)


class WritingChainVersions:
    def __init__(self, snapshot_directory: str | Path, *, source_root: str | Path = SOURCE_ROOT) -> None:
        self.directory = Path(snapshot_directory)
        self.source_root = Path(source_root).resolve()

    def catalog(self) -> dict[str, Any]:
        return {"default": DEFAULT_VERSION, "versions": [
            {"id": version, "label": label,
             "source_thread": "01a13fc6-12f9-7e72-bfbb-d77c87cdaf5f" if version == "v1"
             else "01a1379b-06a9-7b82-8f94-616138e74844"}
            for version, label in VERSION_LABELS.items()
        ]}

    def capture(self, version: str) -> dict[str, str]:
        if version not in VERSION_LABELS:
            raise ValueError("unsupported writing chain version")
        definition = json.loads((self.source_root / "config/writing_chains" / f"{version}.json").read_text(encoding="utf-8"))
        if definition.get("version") != version or definition.get("schema_version") != 1:
            raise ValueError("writing chain definition has an invalid identity")
        if (not isinstance(definition.get("features"), dict)
                or set(definition["features"]) != {
                    "chapter_approach", "single_chapter_unit", "legacy_editor_brief"}
                or any(type(value) is not bool for value in definition["features"].values())):
            raise ValueError("writing chain features must use the supported boolean flags")
        prompt_root = self.source_root / "prompts"
        method = (prompt_root / "shared/decision-method.md").read_text(encoding="utf-8").strip()
        prompts = {}
        for group in ("assistant", "writing"):
            for path in sorted((prompt_root / group).glob("*.md")):
                text = path.read_text(encoding="utf-8").strip()
                marker = "{{decision_method}}"
                if marker in text:
                    if text.count(marker) != 1 or not method or marker in method:
                        raise ValueError("invalid shared method in writing chain")
                    text = text.replace(marker, method)
                prompts[path.relative_to(prompt_root).as_posix()] = text
        for key, row in definition.get("prompt_overrides", {}).items():
            if key.startswith("/") or ".." in Path(key).parts or not key.endswith(".md"):
                raise ValueError("writing chain override has an invalid prompt path")
            text = row["text"]
            if not isinstance(text, str) or not text.strip() or hashlib.sha256(text.encode("utf-8")).hexdigest() != row["sha256"]:
                raise ValueError("writing chain restored prompt hash mismatch")
            prompts[key] = text
        snapshot = {"schema_version": 1, "version": version, "prompt_root": str(prompt_root),
                    "features": definition["features"], "prompts": prompts,
                    "definition_sha256": _digest(definition)}
        digest = _digest(snapshot)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{digest}.json"
        if path.exists():
            if _digest(json.loads(path.read_text(encoding="utf-8"))) != digest:
                raise ValueError("writing chain snapshot was modified")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                                 prefix=".chain-", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(_encoded(snapshot))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"version": version, "label": VERSION_LABELS[version], "snapshot_sha256": digest}

    def read(self, pin: Mapping[str, Any]) -> dict[str, Any]:
        if (set(pin) != {"version", "label", "snapshot_sha256"} or pin.get("version") not in VERSION_LABELS
                or pin.get("label") != VERSION_LABELS[pin["version"]]):
            raise ValueError("invalid writing chain pin")
        digest = pin["snapshot_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid writing chain snapshot hash")
        snapshot = json.loads((self.directory / f"{digest}.json").read_text(encoding="utf-8"))
        if _digest(snapshot) != digest or snapshot.get("version") != pin["version"]:
            raise ValueError("writing chain snapshot does not match the accepted task")
        return {**snapshot, "pin": dict(pin)}

    @contextmanager
    def activate(self, pin: Mapping[str, Any] | None):
        with chain_context(self.read(pin) if pin is not None else None):
            yield
