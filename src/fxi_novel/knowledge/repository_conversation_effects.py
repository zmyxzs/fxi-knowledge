"""Exact, reversible knowledge writes owned by an assistant conversation branch."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable
from uuid import uuid4

from fxi_novel.storage.conversation_effects import (
    assert_conversation_write_allowed,
    conversation_write_origin,
    list_conversation_effects,
    mark_conversation_effect_retracted,
    record_conversation_effect,
    get_conversation_task_origin,
)

from .repository_support import _now
from .source_record_history import record_source_state
from .tokenizer import fts_documents
from .repository_conversation_dialogue_effects import dialogue_effect_state, dialogue_effect_target, restore_dialogue_effect
from .author_input_coverage import coverage_state, restore_coverage
from .learning_preferences import learning_preference_state


KNOWLEDGE_EFFECT_DOMAINS = (
    "knowledge_item", "knowledge_source", "knowledge_dialogue", "learning_work_registration",
    "work_learning_references", "work_base_reference",
    "author_input_coverage", "learning_preference",
)
_REFERENCE_TABLES = {
    "work_learning_references": (
        "writing_learning_source_reference", "writing_learning_scope_reference",
    ),
    "work_base_reference": (
        "writing_base_work_reference", "writing_base_source_reference",
        "writing_base_scope_reference",
    ),
}


@contextmanager
def conversation_task_write_scope(connection, *, task_kind, task_id):
    """A resumed durable task retains its original attribution, including none."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_task_origin'").fetchone():
        origin = get_conversation_task_origin(connection, task_kind=task_kind, task_id=task_id)
    else:
        origin = None
    with conversation_write_origin(origin):
        assert_conversation_write_allowed(connection)
        yield


def knowledge_effect_state(connection: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """Include immutable version identity and mutable publication/status metadata."""
    item = connection.execute("SELECT * FROM knowledge_item WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    version = connection.execute(
        "SELECT * FROM knowledge_version WHERE item_id = ? ORDER BY version DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    evidence = [] if version is None else [
        str(row["evidence_span_id"]) for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ? ORDER BY evidence_span_id", (version["id"],),
        ).fetchall()
    ]
    publications = []
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_query_reviewed_claim_publication'").fetchone():
        publications = [str(row["id"]) for row in connection.execute(
            "SELECT id FROM knowledge_query_reviewed_claim_publication WHERE candidate_item_id=? ORDER BY id", (item_id,),
        ).fetchall()]
    return {"item": dict(item), "version": dict(version) if version else None,
            "evidence_span_ids": evidence, "query_publication_ids": publications}


def source_effect_state(connection: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    source = connection.execute("SELECT * FROM source WHERE id=?", (source_id,)).fetchone()
    if source is None:
        return None
    snapshot = connection.execute(
        "SELECT snapshot_id FROM source_current_snapshot WHERE source_id=?", (source_id,),
    ).fetchone()
    return {"source": dict(source), "snapshot_id": snapshot["snapshot_id"] if snapshot else None}


def refresh_source_current_fts(connection, source_id):
    """Rebuild only the restored source's current lexical projection."""
    connection.execute(
        "DELETE FROM chunk_fts WHERE chunk_id IN (SELECT chunk.id FROM chunk "
        "JOIN source_snapshot AS snapshot ON snapshot.id=chunk.snapshot_id WHERE snapshot.source_id=?)", (source_id,),
    )
    state = source_effect_state(connection, source_id)
    if state is None or state["source"]["deleted_at"] is not None or not state["snapshot_id"]:
        return
    chunks = connection.execute(
        "SELECT id,text FROM chunk WHERE snapshot_id=? AND chunker_version="
        "(SELECT chunker_version FROM chunk WHERE snapshot_id=? GROUP BY chunker_version "
        "ORDER BY MAX(created_at) DESC,chunker_version DESC LIMIT 1) ORDER BY sequence",
        (state["snapshot_id"], state["snapshot_id"]),
    ).fetchall()
    connection.executemany(
        "INSERT INTO chunk_fts(chunk_id,tokenized_text) VALUES (?,?)",
        [(row["id"], text) for row, text in zip(chunks, fts_documents([row["text"] for row in chunks]), strict=True)],
    )


def resolve_source_compensation_state(connection, source_id, state):
    return KnowledgeConversationEffectRepositoryMixin._resolve_compensation_state(connection, "knowledge_source", source_id, state)


def learning_work_effect_state(connection, work_id):
    project = connection.execute("SELECT * FROM knowledge_project WHERE id=?", (work_id,)).fetchone()
    if project is None:
        return None
    folders = [dict(row) for row in connection.execute(
        "SELECT * FROM knowledge_learning_work_folder WHERE work_id=? ORDER BY folder_id", (work_id,),
    ).fetchall()]
    return {"project": dict(project), "folders": folders}


def _effect_state(connection, domain, target_id):
    if domain == "learning_preference":
        return learning_preference_state(connection, target_id)
    if domain == "author_input_coverage":
        return coverage_state(connection, target_id)
    if domain == "knowledge_item":
        return knowledge_effect_state(connection, target_id)
    if domain == "knowledge_source":
        return source_effect_state(connection, target_id)
    if domain == "learning_work_registration":
        return learning_work_effect_state(connection, target_id)
    if domain == "knowledge_dialogue":
        return dialogue_effect_state(connection, target_id)
    return reference_effect_state(connection, domain, target_id)


def reference_effect_state(
    connection: sqlite3.Connection, domain: str, work_id: str,
) -> dict[str, Any]:
    """Capture configured exact sources/scopes, without expanding dynamic selections."""
    return {
        table: sorted(
            [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE work_id = ?", (work_id,),
            ).fetchall()],
            key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False),
        )
        for table in _REFERENCE_TABLES[domain]
    }


def record_knowledge_effect(
    connection: sqlite3.Connection, item_id: str, before: dict[str, Any] | None,
) -> None:
    after = knowledge_effect_state(connection, item_id)
    if after is not None and before != after:
        record_conversation_effect(
            connection, domain="knowledge_item", target_id=item_id,
            before=before, after=after,
        )


class KnowledgeConversationEffectRepositoryMixin:
    """Compensate exact tracked effects while preserving all historical evidence."""

    @staticmethod
    def _conversation_effect_plan(connection, *, conversation_id, superseded_message_refs, work_id, effect_ids):
        effects = list_conversation_effects(
            connection, conversation_id=conversation_id,
            superseded_message_refs=superseded_message_refs,
            domains=KNOWLEDGE_EFFECT_DOMAINS, include_retracted=True,
        )
        missing_ids = set()
        if effect_ids is not None:
            selected_ids = set(effect_ids)
            effects = [effect for effect in effects if effect["id"] in selected_ids]
            missing_ids = selected_ids - {effect["id"] for effect in effects}
        active = [effect for effect in effects if effect["status"] == "active"]
        plans: dict[tuple[str, str], dict[str, Any]] = {}
        conflicts = [{"effect_id": identity, "reason": "conversation_effect_missing"}
                     for identity in sorted(missing_ids)]
        for effect in reversed(active):
            domain, target_id = effect["domain"], effect["target_id"]
            key = (domain, target_id)
            if work_id is not None and effect.get("work_id") != work_id:
                conflicts.append({"domain": domain, "target_id": target_id, "effect_id": effect["id"],
                                  "reason": "conversation_effect_scope_mismatch"})
                continue
            if key not in plans:
                actual = _effect_state(connection, domain, target_id)
                plans[key] = {"domain": domain, "target_id": target_id, "current": actual,
                              "restore": actual, "effect_ids": [], "revoked_version_ids": [],
                              "revoked_snapshot_ids": []}
            plan = plans[key]
            if plan["restore"] != effect["after"]:
                plan["restore"] = KnowledgeConversationEffectRepositoryMixin._resolve_compensation_state(
                    connection, domain, target_id, plan["restore"],
                )
            if plan["restore"] != effect["after"]:
                conflicts.append({"domain": domain, "target_id": target_id, "effect_id": effect["id"],
                                  "reason": "later_independent_change",
                                  "message": "该内容在对话操作后又被修改，未覆盖后续修改。"})
                continue
            if domain == "knowledge_item":
                version = effect["after"].get("version") or {}
                if version.get("id"):
                    plan["revoked_version_ids"].append(version["id"])
            elif domain == "knowledge_source" and effect["after"].get("snapshot_id"):
                plan["revoked_snapshot_ids"].append(effect["after"]["snapshot_id"])
            plan["restore"] = effect["before"]
            plan["effect_ids"].append(effect["id"])
        # A source cannot be hidden underneath independently retained knowledge.
        # Historical spans remain intact; this check concerns the active consumer.
        for plan in plans.values():
            if plan["domain"] == "learning_work_registration" and plan["restore"] is None:
                for row in connection.execute(
                    "SELECT id FROM source WHERE work_id=? AND deleted_at IS NULL", (plan["target_id"],),
                ).fetchall():
                    source_plan = plans.get(("knowledge_source", str(row["id"])))
                    if source_plan is None or (source_plan["restore"] is not None and source_plan["restore"]["source"]["deleted_at"] is None):
                        conflicts.append({"domain": plan["domain"], "target_id": plan["target_id"],
                                          "reason": "learning_work_has_independent_sources", "source_id": str(row["id"])})
                reference_rows = connection.execute(
                    "SELECT work_id,'work_learning_references' AS domain FROM writing_learning_scope_reference WHERE learning_work_id=? "
                    "UNION SELECT work_id,'work_base_reference' AS domain FROM writing_base_work_reference WHERE base_space='learning' AND base_work_id=?",
                    (plan["target_id"], plan["target_id"]),
                ).fetchall()
                for row in reference_rows:
                    reference_plan = plans.get((row["domain"], row["work_id"]))
                    retained = reference_plan["restore"] if reference_plan else reference_effect_state(connection, row["domain"], row["work_id"])
                    table = "writing_learning_scope_reference" if row["domain"] == "work_learning_references" else "writing_base_work_reference"
                    field = "learning_work_id" if row["domain"] == "work_learning_references" else "base_work_id"
                    if any(reference[field] == plan["target_id"] for reference in (retained or {}).get(table, [])):
                        conflicts.append({"domain": plan["domain"], "target_id": plan["target_id"],
                                          "reason": "learning_work_has_independent_references", "referencing_work_id": row["work_id"]})
            if plan["domain"] != "knowledge_source" or plan["current"] is None:
                continue
            snapshot_id = plan["current"]["snapshot_id"]
            if plan["restore"] is not None and plan["restore"]["snapshot_id"] == snapshot_id:
                continue
            for row in connection.execute(
                "SELECT DISTINCT receipt_id,generation FROM knowledge_dialogue_annotation_set "
                "WHERE snapshot_id=? AND status IN ('candidate','published')", (snapshot_id,),
            ).fetchall():
                target = dialogue_effect_target(row["receipt_id"], int(row["generation"]))
                dialogue_plan = plans.get(("knowledge_dialogue", target))
                retained = dialogue_plan["restore"] if dialogue_plan else dialogue_effect_state(connection, target)
                if any(entry["set"]["status"] in {"candidate", "published"} and entry["set"]["snapshot_id"] == snapshot_id
                       for entry in (retained or {}).get("sets", [])):
                    conflicts.append({"domain": plan["domain"], "target_id": plan["target_id"],
                                      "reason": "source_has_independent_dialogue", "dialogue_target_id": target})
            dependent_ids = [str(row["id"]) for row in connection.execute(
                "SELECT DISTINCT item.id FROM knowledge_item AS item "
                "JOIN knowledge_version AS version ON version.item_id=item.id "
                "JOIN knowledge_version_evidence AS link ON link.knowledge_version_id=version.id "
                "JOIN evidence_span AS evidence ON evidence.id=link.evidence_span_id "
                "WHERE item.status NOT IN ('deleted','rejected','superseded') AND evidence.snapshot_id=? "
                "AND (version.id=item.published_graph_version_id OR version.version="
                "(SELECT MAX(latest.version) FROM knowledge_version AS latest WHERE latest.item_id=item.id))",
                (snapshot_id,),
            ).fetchall()]
            for item_id in dependent_ids:
                knowledge_plan = plans.get(("knowledge_item", item_id))
                restored = knowledge_plan["restore"] if knowledge_plan is not None else knowledge_effect_state(connection, item_id)
                if restored is None or restored["item"]["status"] in {"deleted", "rejected", "superseded"}:
                    continue
                evidence_ids = restored["evidence_span_ids"]
                still_depends = bool(evidence_ids) and connection.execute(
                    "SELECT 1 FROM evidence_span WHERE snapshot_id=? AND id IN (" +
                    ",".join("?" for _ in evidence_ids) + ") LIMIT 1", (snapshot_id, *evidence_ids),
                ).fetchone() is not None
                if still_depends:
                    conflicts.append({"domain": plan["domain"], "target_id": plan["target_id"],
                                      "reason": "source_has_independent_knowledge", "knowledge_item_id": item_id})
            if plan["restore"] is None:
                reference_rows = connection.execute(
                    "SELECT work_id,'work_learning_references' AS domain FROM writing_learning_source_reference WHERE source_id=? "
                    "UNION SELECT work_id,'work_base_reference' AS domain FROM writing_base_source_reference WHERE source_id=?",
                    (plan["target_id"], plan["target_id"]),
                ).fetchall()
                for row in reference_rows:
                    reference_plan = plans.get((row["domain"], row["work_id"]))
                    retained = reference_plan["restore"] if reference_plan else reference_effect_state(connection, row["domain"], row["work_id"])
                    table = "writing_learning_source_reference" if row["domain"] == "work_learning_references" else "writing_base_source_reference"
                    if any(reference["source_id"] == plan["target_id"] for reference in (retained or {}).get(table, [])):
                        conflicts.append({"domain": plan["domain"], "target_id": plan["target_id"],
                                          "reason": "source_has_independent_references", "referencing_work_id": row["work_id"]})
        result = {
            "status": "conflict" if conflicts else "ready", "ready": not conflicts,
            "retracted": 0, "already_retracted": len(effects) - len(active),
            "effect_count": len(active), "item_count": len(plans), "conflicts": conflicts,
            "effect_ids": [effect["id"] for effect in effects],
            "items": [KnowledgeConversationEffectRepositoryMixin._effect_description(plan) for plan in plans.values()],
        }
        return result, list(plans.values())

    @staticmethod
    def _effect_description(plan):
        current, prior, domain = plan["current"], plan["restore"], plan["domain"]
        title = plan["target_id"]
        if current is not None and domain == "knowledge_item":
            content = json.loads(current["version"]["content_json"]) if current.get("version") else {}
            title = str(content.get("title") or content.get("entity_name") or title) if isinstance(content, dict) else title
        elif current is not None and domain in {"knowledge_source", "learning_work_registration"}:
            title = current["source" if domain == "knowledge_source" else "project"]["title"]
        elif domain == "learning_preference":
            title = "新作品默认参考设置" if plan["target_id"] == "new_works" else "作品参考设置"
        return {
            "domain": domain, "target_id": plan["target_id"], "title": title,
            "action": "deactivate" if prior is None else "restore",
            "effect_ids": plan["effect_ids"],
            "before_version": current["version"]["id"] if domain == "knowledge_item" and current else None,
            "after_version": prior["version"]["id"] if domain == "knowledge_item" and prior else None,
        }

    @staticmethod
    def _resolve_compensation_state(connection, domain, target_id, state):
        """Follow only exact saved compensation heads, never content equivalence."""
        rows = connection.execute(
            "SELECT retraction_json FROM conversation_effect "
            "WHERE domain=? AND target_id=? AND status='retracted' ORDER BY rowid DESC",
            (domain, target_id),
        ).fetchall()
        mappings = [json.loads(row["retraction_json"]) for row in rows]
        if domain == "knowledge_source":
            source_rows = connection.execute(
                "SELECT result_json FROM knowledge_source_idempotency_receipt WHERE source_id=?", (target_id,),
            ).fetchall()
            mappings.extend(
                entry for entry in [json.loads(row["result_json"]).get("_retraction", {}) for row in source_rows]
                if entry.get("status") == "retracted"
            )
        for _step in range(len(mappings)):
            replacement = next((entry for entry in mappings
                                if entry.get("restored_state") == state
                                and "logical_before" in entry), None)
            if replacement is None or replacement["logical_before"] == state:
                break
            state = replacement["logical_before"]
        return state

    def preflight_retract_conversation_effects(
        self, *, conversation_id: str, superseded_message_refs: Iterable[dict[str, Any]],
        work_id: str | None = None, operation_id: str | None = None,
        effect_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        with self.database.connection() as connection:
            result, _plans = self._conversation_effect_plan(
                connection, conversation_id=conversation_id,
                superseded_message_refs=superseded_message_refs, work_id=work_id,
                effect_ids=effect_ids,
            )
        return result

    def retract_conversation_effects(
        self, *, conversation_id: str, superseded_message_refs: Iterable[dict[str, Any]],
        work_id: str | None = None, operation_id: str | None = None,
        effect_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or uuid4().hex
        with conversation_write_origin(None), self.database.transaction() as connection:
            result, plans = self._conversation_effect_plan(
                connection, conversation_id=conversation_id,
                superseded_message_refs=superseded_message_refs, work_id=work_id,
                effect_ids=effect_ids,
            )
            if not result["ready"]:
                return result
            invalidated_version_ids = []
            descriptions = []
            for plan in plans:
                if plan["domain"] == "knowledge_item":
                    self._restore_conversation_knowledge_item(connection, plan)
                    invalidated_version_ids.extend(plan["revoked_version_ids"])
                elif plan["domain"] == "knowledge_source":
                    self._restore_conversation_source(connection, plan)
                    retained_snapshot = plan["restore"]["snapshot_id"] if plan["restore"] else None
                    for snapshot_id in set(plan["revoked_snapshot_ids"]) - {retained_snapshot}:
                        invalidated_version_ids.extend(str(row["knowledge_version_id"]) for row in connection.execute(
                            "SELECT DISTINCT link.knowledge_version_id FROM knowledge_version_evidence AS link "
                            "JOIN evidence_span AS evidence ON evidence.id=link.evidence_span_id WHERE evidence.snapshot_id=?",
                            (snapshot_id,),
                        ).fetchall())
                elif plan["domain"] == "learning_work_registration":
                    self._restore_conversation_learning_work(connection, plan)
                elif plan["domain"] == "knowledge_dialogue":
                    restore_dialogue_effect(connection, plan)
                elif plan["domain"] == "author_input_coverage":
                    restore_coverage(connection, plan)
                elif plan["domain"] == "learning_preference":
                    connection.execute("DELETE FROM writing_learning_preference_head WHERE scope_key=?",
                                       (plan["target_id"],))
                    prior = plan["restore"]
                    if prior and prior["revision"]:
                        connection.execute("INSERT INTO writing_learning_preference_head(scope_key,revision) VALUES(?,?)",
                                           (plan["target_id"], prior["revision"]))
                else:
                    self._restore_conversation_references(connection, plan)
                restored = _effect_state(connection, plan["domain"], plan["target_id"])
                description = self._effect_description(plan)
                if plan["domain"] == "knowledge_item":
                    description["after_version"] = restored["version"]["id"]
                descriptions.append(description)
                for effect_id in plan["effect_ids"]:
                    mark_conversation_effect_retracted(
                        connection, effect_id, operation_id=operation_id,
                        result={"restored_state": restored, "logical_before": plan["restore"]},
                    )
            result["invalidation"] = self._invalidate_dependency_versions_in_connection(
                connection, knowledge_version_ids=invalidated_version_ids,
                reason="assistant_branch_retracted",
            )
            result.update(status="completed", retracted=result["effect_count"], operation_id=operation_id)
            result["items"] = descriptions
        return result

    @staticmethod
    def _restore_conversation_learning_work(connection, plan):
        prior, work_id = plan["restore"], plan["target_id"]
        connection.execute("DELETE FROM knowledge_learning_work_folder WHERE work_id=?", (work_id,))
        if prior is None:
            connection.execute("UPDATE knowledge_project SET status='deleted',updated_at=? WHERE id=?", (_now(), work_id))
        else:
            project = prior["project"]
            connection.execute(
                "UPDATE knowledge_project SET title=?,description=?,status=?,updated_at=? WHERE id=?",
                (project["title"], project["description"], project["status"], _now(), work_id),
            )
            connection.executemany(
                "INSERT INTO knowledge_learning_work_folder(work_id,folder_id,created_at) VALUES (?,?,?)",
                [(row["work_id"], row["folder_id"], row["created_at"]) for row in prior["folders"]],
            )

    def _restore_conversation_source(self, connection, plan):
        prior, source_id = plan["restore"], plan["target_id"]
        record_source_state(connection, source_id)
        if prior is None:
            connection.execute("UPDATE source SET deleted_at=? WHERE id=?", (_now(), source_id))
        else:
            fields = ("title", "source_uri", "identity_key", "deleted_at", "folder_id", "document_metadata_json")
            connection.execute(
                "UPDATE source SET " + ",".join(field + "=?" for field in fields) + " WHERE id=?",
                (*[prior["source"][field] for field in fields], source_id),
            )
            if prior["snapshot_id"] is None:
                connection.execute("DELETE FROM source_current_snapshot WHERE source_id=?", (source_id,))
            else:
                connection.execute(
                    "INSERT INTO source_current_snapshot(source_id,snapshot_id) VALUES (?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                    (source_id, prior["snapshot_id"]),
                )
        self._sync_knowledge_source_title_search_grams(connection, source_id)
        refresh_source_current_fts(connection, source_id)
        record_source_state(connection, source_id)

    def _restore_conversation_knowledge_item(self, connection, plan):
        current, prior, item_id = plan["current"], plan["restore"], plan["target_id"]
        # New items receive a terminal version; restored items receive a fresh
        # compensation version. Neither historical versions nor shared spans are deleted.
        source = prior if prior is not None else current
        version = source["version"]
        if version is None:
            raise ValueError("tracked knowledge item has no restorable version")
        status = source["item"]["status"] if prior is not None else "deleted"
        version_id, now = uuid4().hex, _now()
        next_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_version WHERE item_id = ?", (item_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) VALUES (?,?,?,?,?,?)",
            (version_id, item_id, next_version, version["content_json"], status, now),
        )
        connection.executemany(
            "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES (?,?)",
            [(version_id, evidence_id) for evidence_id in source["evidence_span_ids"]],
        )
        published_id = source["item"].get("published_graph_version_id") if prior is not None else None
        connection.execute(
            "UPDATE knowledge_item SET status=?,published_graph_version_id=?,updated_at=? WHERE id=?",
            (status, published_id, now, item_id),
        )
        kind = source["item"]["kind"]
        content = json.loads(version["content_json"])
        if kind == "source_study":
            self._index_knowledge_item(connection, item_id, version_id, content)
        elif kind == "knowledge_graph_candidate":
            self._index_knowledge_graph_candidate(connection, item_id, version_id, content)
            if published_id and published_id != version_id:
                published = connection.execute(
                    "SELECT content_json FROM knowledge_version WHERE id=? AND item_id=?", (published_id, item_id),
                ).fetchone()
                if published is None:
                    raise ValueError("tracked published knowledge version is missing")
                self._index_knowledge_graph_candidate(connection, item_id, published_id, json.loads(published["content_json"]))
        else:
            self._sync_knowledge_card_candidate(connection, item_id, version_id, f"item:{item_id}")

    @staticmethod
    def _restore_conversation_references(connection, plan):
        tables = _REFERENCE_TABLES[plan["domain"]]
        for table in reversed(tables):
            connection.execute(f"DELETE FROM {table} WHERE work_id=?", (plan["target_id"],))
        for table in tables:
            for row in (plan["restore"] or {}).get(table, []):
                columns = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                connection.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})", tuple(row.values()))
