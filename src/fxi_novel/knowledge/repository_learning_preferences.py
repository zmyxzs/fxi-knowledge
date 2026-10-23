"""Persist learning choices; all source registration stays with the reference owner."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .repository_support import _now
from .repository_conversation_effects import assert_conversation_write_allowed
from .learning_preferences import learning_preference_state, normalize_learning_libraries
from fxi_novel.storage.conversation_effects import current_conversation_origin, record_conversation_effect


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class KnowledgeLearningPreferenceRepositoryMixin:
    def _learning_preference(self, connection, work_id=None, revision=None):
        return learning_preference_state(connection, work_id or "new_works", revision)

    def get_learning_preference(self, work_id=None, *, revision=None):
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, "writing", work_id)
            return self._learning_preference(connection, work_id, revision)

    def _normalize_learning_libraries(self, connection, libraries):
        normalized = normalize_learning_libraries(libraries)
        for row in normalized:
            self._ensure_project_exists(connection, "learning", row["learning_work_id"])
        return normalized

    def _save_learning_preference(self, connection, work_id, libraries, *, expected_revision,
                                  inherited_default_revision=None):
        current = self._learning_preference(connection, work_id)
        if type(expected_revision) is not int or current["revision"] != expected_revision:
            raise ValueError("learning preference changed; reread its current revision")
        revision = connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM writing_learning_preference WHERE scope_key=?",
            (work_id or "new_works",),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO writing_learning_preference"
            "(scope_key,work_id,revision,libraries_json,inherited_default_revision,created_at) VALUES(?,?,?,?,?,?)",
            (work_id or "new_works", work_id, revision,
             json.dumps(libraries, ensure_ascii=False), inherited_default_revision, _now()),
        )
        connection.execute("INSERT INTO writing_learning_preference_head(scope_key,revision) VALUES(?,?) "
                           "ON CONFLICT(scope_key) DO UPDATE SET revision=excluded.revision",
                           (work_id or "new_works", revision))
        return self._learning_preference(connection, work_id)

    def replace_learning_preference(self, *, libraries, expected_revision, work_id=None):
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            if work_id is not None:
                self._ensure_project_exists(connection, "writing", work_id)
            selected = self._normalize_learning_libraries(connection, libraries)
            old = self._learning_preference(connection, work_id)
            origin = current_conversation_origin()
            effect_id = (_hash(["learning_preference", origin["conversation_id"], origin["message_id"], work_id])
                         if origin else None)
            if effect_id:
                effect = connection.execute("SELECT * FROM conversation_effect WHERE id=?", (effect_id,)).fetchone()
                if effect:
                    after = json.loads(effect["after_json"])
                    if effect["status"] != "active" or after != old or after["libraries"] != selected:
                        raise ValueError("learning preference changed after this message's saved operation")
                    return old
            if old["revision"] != expected_revision:
                raise ValueError("learning preference changed; reread its current revision")
            if work_id is not None:
                references = self._learning_reference_selection(connection, work_id)
                replaced_ids = {row["learning_work_id"] for row in old["libraries"]}
                scopes = [{key: row[key] for key in ("learning_work_id", "folder_id")}
                          for row in references["scopes"] if row["learning_work_id"] not in replaced_ids]
                scopes.extend({"learning_work_id": row["learning_work_id"], "folder_id": None} for row in selected)
                self.replace_work_learning_references(work_id, references["source_ids"], scopes,
                                                       _connection=connection)
            saved = self._save_learning_preference(connection, work_id, selected, expected_revision=expected_revision)
            record_conversation_effect(connection, domain="learning_preference", target_id=work_id or "new_works",
                                       before=old, after=saved, effect_id=effect_id)
            return saved

    def _inherit_work_learning_defaults(self, connection, work_id):
        defaults = self._learning_preference(connection)
        if not defaults["revision"]:
            return
        # An archived learning library must not make creating a book impossible.
        active = [row for row in defaults["libraries"] if connection.execute(
            "SELECT 1 FROM knowledge_project WHERE id=? AND space='learning' AND status='active'",
            (row["learning_work_id"],)).fetchone()]
        self.replace_work_learning_references(work_id, [], [
            {"learning_work_id": row["learning_work_id"], "folder_id": None} for row in active
        ], _connection=connection)
        self._save_learning_preference(connection, work_id, defaults["libraries"], expected_revision=0,
                                        inherited_default_revision=defaults["revision"])

    def work_learning_preference_context(self, work_id: str, *, use: str) -> dict[str, Any] | None:
        if use not in {"planning", "prose"}:
            raise ValueError("learning preference use must be planning or prose")
        with self.database.read_snapshot(), self.database.connection() as connection:
            preference = self._learning_preference(connection, work_id)
            if not preference["revision"]:
                return None
            registered = self._learning_reference_selection(connection, work_id, include_source_ids=True)
            allowed_ids = set(registered["effective_source_ids"])
            libraries = []
            source_ids = set()
            for selection in preference["libraries"]:
                if use not in selection["uses"] and not (use == "prose" and "prose_reference" in selection["uses"]):
                    continue
                library = connection.execute(
                    "SELECT title FROM knowledge_project WHERE id=? AND space='learning' AND status='active'",
                    (selection["learning_work_id"],)).fetchone()
                if library is None:
                    continue
                resolved = self.resolve_learning_work_scope_in_connection(connection, selection["learning_work_id"])
                selected_ids = set(resolved["source_ids"]) & allowed_ids
                if not selected_ids:
                    continue
                purposes = {key: value for key, value in selection.get("purposes", {}).items()
                            if key == use or (use == "prose" and key == "prose_reference")}
                libraries.append({"learning_work_id": selection["learning_work_id"], "title": library["title"],
                                  "uses": selection["uses"], "source_ids": sorted(selected_ids),
                                  **({"purposes": purposes} if purposes else {})})
                source_ids.update(selected_ids)
            context = {"kind": "work_learning_preference", "work_id": work_id,
                       "revision": preference["revision"], "use": use,
                       "libraries": libraries, "source_ids": sorted(source_ids),
                       "registered_selection": {key: registered[key] for key in ("source_ids", "scopes")}}
            context["preference_hash"] = _hash(context)
            return context
