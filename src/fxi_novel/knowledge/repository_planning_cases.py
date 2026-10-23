"""Read-only pages over the existing current study FTS projection."""
from __future__ import annotations

import json

from .scope_selection import all_version_source_dependencies_filter, source_selection_filter
from .tokenizer import fts_query


def scoped_sources(repository, scopes, *, learning_source_ids, before_chapter, boundary_work_id=None, branch_id=None,
                   writing_source_pins=()):
    """Resolve only directly registered scopes; return metadata, never raw text."""
    sources = {}
    verified = {row["source_id"]: row for row in writing_source_pins}
    with repository.database.connection() as connection:
        target_origins = set()
        if boundary_work_id is not None:
            for row in connection.execute("SELECT document_metadata_json FROM source WHERE space='writing' AND work_id=? AND deleted_at IS NULL", (boundary_work_id,)):
                origin = json.loads(row[0] or "{}").get("origin_source_id")
                if origin:
                    target_origins.add(origin)
        for index, (space, work, ids, folders) in enumerate(scopes):
            parameters = [space, work]
            selection = source_selection_filter("s", ids, folders, parameters)
            exact = getattr(scopes, "exact_source_ids_by_scope", [None] * len(scopes))[index]
            if exact is not None:
                selection += " AND s.id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(exact))
            if space == "learning" and learning_source_ids is not None:
                selection += " AND s.id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(learning_source_ids))
            rows = connection.execute("""SELECT s.id,s.space,s.work_id,s.title,s.document_metadata_json,
                ss.id AS snapshot_id,ss.content_hash FROM source s
                JOIN source_current_snapshot current ON current.source_id=s.id
                JOIN source_snapshot ss ON ss.id=current.snapshot_id
                WHERE s.space=? AND s.work_id IS ? AND s.deleted_at IS NULL""" + selection, parameters).fetchall()
            for row in rows:
                value = dict(row)
                metadata = json.loads(value.pop("document_metadata_json") or "{}")
                extent = metadata.get("chapter_range")
                same_work = (boundary_work_id is not None and (value["work_id"] == boundary_work_id
                    or metadata.get("narrative_work_id") == boundary_work_id
                    or metadata.get("origin_source_id") in target_origins))
                source_branch = metadata.get("branch_id", (metadata.get("prose_projection") or {}).get("branch_id", branch_id))
                if branch_id is not None and same_work and source_branch != branch_id:
                    continue
                if before_chapter is not None and (boundary_work_id is None or same_work):
                    if value["space"] == "writing" and value["work_id"] == boundary_work_id and (
                        "prose_projection" in metadata or (
                            metadata.get("material_role") == "original_chapter_text" and "chapter_map" in metadata
                        )
                    ):
                        pin = verified.get(value["id"])
                        if (not pin or pin["snapshot_id"] != value["snapshot_id"]
                                or pin["content_hash"] != value["content_hash"]):
                            continue
                    # Unknown time does not grant access to a prediction input.
                    elif (not isinstance(extent, list) or len(extent) != 2
                            or any(type(part) is not int for part in extent)
                            or not 1 <= extent[0] <= extent[1] < before_chapter):
                        continue
                value["document_metadata"] = metadata
                sources[value["id"]] = value
    return sources


def explicit_case_scopes(repository, source_ids, *, space, work_id):
    """Author-selected learning sources need not be automatically registered.

    This trusted selector grants only the listed physical sources, never their
    folders, linked works, or a recursive learning library. Writing sources must
    belong to the current work; factual selection stays independently bounded.
    """
    groups = {}
    with repository.database.connection() as connection:
        rows = connection.execute("SELECT id,space,work_id FROM source WHERE deleted_at IS NULL AND id IN (SELECT value FROM json_each(?))", (json.dumps(source_ids),)).fetchall()
    for row in rows:
        if row["space"] != "learning" and (row["space"], row["work_id"]) != (space, work_id):
            raise ValueError("selected case source belongs to another writing work")
        groups.setdefault((row["space"], row["work_id"]), []).append(row["id"])
    return [(owner_space, owner_work, ids, None) for (owner_space, owner_work), ids in groups.items()]


CASE_STUDY_ANALYSIS_TYPES = ("planning", "style")


def study_page(repository, scopes, allowed_sources, query, *, offset, limit, matching_source_ids=None):
    """Filter complete formal lineage before LIMIT; use existing FTS for ranking."""
    expression = fts_query(query)
    if query and not expression:
        return []
    scope_parameters, scope_parts = [], []
    for space, work, _, _ in scopes:
        scope_parts.append("(i.space=? AND i.work_id IS ?)")
        scope_parameters.extend([space, work])
    if not scope_parts or not allowed_sources:
        return []
    parameters = [expression] if expression else []
    parameters.extend(scope_parameters)
    parameters.append(json.dumps(CASE_STUDY_ANALYSIS_TYPES))
    lineage = all_version_source_dependencies_filter("v.id", list(allowed_sources), parameters)
    matched = ""
    if matching_source_ids is not None:
        matched = """ AND EXISTS (SELECT 1 FROM json_each(json_extract(v.content_json,'$.source_refs')) ref
            WHERE json_extract(ref.value,'$.source_id') IN (SELECT value FROM json_each(?)))"""
        parameters.append(json.dumps(matching_source_ids))
    parameters.extend([limit, offset])
    with repository.database.connection() as connection:
        rows = connection.execute("""SELECT i.id AS item_id,v.id AS version_id,i.updated_at,
            """ + ("bm25(knowledge_item_fts)" if expression else "0") + """ AS lexical_rank
            FROM knowledge_item i JOIN knowledge_version v ON v.item_id=i.id
              AND v.version=(SELECT MAX(latest.version) FROM knowledge_version latest WHERE latest.item_id=i.id)
            """ + ("JOIN knowledge_item_fts ON knowledge_item_fts.item_id=i.id AND knowledge_item_fts.version_id=v.id" if expression else "") + """
            WHERE """ + ("knowledge_item_fts MATCH ? AND " if expression else "") + "(" + " OR ".join(scope_parts) + ")" + """
              AND i.kind='source_study' AND i.status IN ('draft','reviewed')
              AND json_extract(v.content_json,'$.analysis_type') IN (SELECT value FROM json_each(?))
              AND COALESCE(json_extract(v.content_json,'$.learning_role'),'') NOT IN ('planning_method_synthesis','craft_method_synthesis')
              AND json_type(v.content_json,'$.markdown')='text' AND (""" + lineage + " )" + matched + """
            ORDER BY lexical_rank,i.updated_at DESC,i.id LIMIT ? OFFSET ?""", parameters).fetchall()
    return [dict(row) for row in rows]


def dependency_items(repository, version_id):
    """The existing dependency graph, read only; embedded context is checked too."""
    with repository.database.connection() as connection:
        rows = connection.execute("""WITH RECURSIVE required(id) AS (
            SELECT ? UNION SELECT dep.knowledge_version_id FROM required
            JOIN knowledge_version version ON version.id=required.id
            JOIN knowledge_projection_dependency dep ON dep.consumer_kind='knowledge_item'
              AND dep.consumer_id=version.item_id
              AND dep.consumer_revision IN (version.id,CAST(version.version AS TEXT))
              AND dep.upstream_kind='knowledge_version')
            SELECT version.item_id,version.id AS version_id FROM required
            JOIN knowledge_version version ON version.id=required.id""", (version_id,)).fetchall()
    return [dict(row) for row in rows]


def is_prior_study_version(repository, item, pin):
    """Verify the refinement owner's historical self-pin, not a current dependency."""
    if (not isinstance(pin, dict) or pin.get("item_id") != item["id"]
            or not isinstance(pin.get("version_id"), str) or type(pin.get("version")) is not int):
        return False
    with repository.database.connection() as connection:
        row = connection.execute("SELECT item_id,version FROM knowledge_version WHERE id=?",
                                 (pin["version_id"],)).fetchone()
    return bool(row and row["item_id"] == item["id"] and row["version"] == pin["version"]
                and row["version"] < item["version"])


def study_watermark(repository, scopes):
    with repository.database.connection() as connection:
        return [tuple(connection.execute("""SELECT COUNT(*),MAX(v.rowid),MAX(i.updated_at)
            FROM knowledge_item i JOIN knowledge_version v ON v.item_id=i.id
            WHERE i.space=? AND i.work_id IS ? AND i.kind='source_study'""", (space, work)).fetchone())
                for space, work, _, _ in scopes]
