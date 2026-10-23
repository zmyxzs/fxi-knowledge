"""Current-use projection for methods derived from a revisable planning study."""


def _reference_coverage(old: str, current: str, default_role: str) -> str:
    """Retain the original source identity and reading scope, allowing expansion."""
    old_role = f"COALESCE(NULLIF(json_extract({old}, '$.role'), ''), '{default_role}')"
    current_role = f"COALESCE(NULLIF(json_extract({current}, '$.role'), ''), '{default_role}')"
    old_kind = f"COALESCE(json_extract({old}, '$.selection_kind'), 'whole_source')"
    current_kind = f"COALESCE(json_extract({current}, '$.selection_kind'), 'whole_source')"
    return f"""
        json_extract({old}, '$.source_id') = json_extract({current}, '$.source_id')
        AND json_extract({old}, '$.snapshot_id') = json_extract({current}, '$.snapshot_id')
        AND json_extract({old}, '$.content_hash') = json_extract({current}, '$.content_hash')
        AND {old_role} = {current_role}
        AND (
            ({current_role} != 'carryover' AND {current_kind} = 'whole_source')
            OR (
                ({old_kind} = 'selected_evidence'
                 OR ({old_role} = 'carryover' AND json_array_length({old}, '$.evidence_ids') > 0))
                AND NOT EXISTS (
                    SELECT 1 FROM json_each({old}, '$.evidence_ids') AS old_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM json_each({current}, '$.evidence_ids') AS current_id
                        WHERE current_id.value = old_id.value
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM evidence_span AS original_span
                        JOIN source_snapshot AS original_snapshot ON original_snapshot.id = original_span.snapshot_id
                        JOIN json_each({current}, '$.sections') AS current_section
                          ON json_extract(current_section.value, '$.start_offset') <= original_span.start_offset
                         AND json_extract(current_section.value, '$.end_offset') >= original_span.end_offset
                        WHERE {current_kind} != 'selected_evidence'
                          AND original_span.id = old_id.value
                          AND original_snapshot.source_id = json_extract({old}, '$.source_id')
                          AND original_snapshot.id = json_extract({old}, '$.snapshot_id')
                    )
                )
            )
            OR (
                {old_kind} != 'selected_evidence'
                AND ({old_kind} != 'whole_source' OR {old_role} = 'carryover')
                AND {current_kind} != 'selected_evidence'
                AND json_array_length({old}, '$.sections') > 0
                AND NOT EXISTS (
                    SELECT 1 FROM json_each({old}, '$.sections') AS old_section
                    WHERE NOT EXISTS (
                        SELECT 1 FROM json_each({current}, '$.sections') AS current_section
                        WHERE json_extract(current_section.value, '$.start_offset')
                                  <= json_extract(old_section.value, '$.start_offset')
                          AND json_extract(current_section.value, '$.end_offset')
                                  >= json_extract(old_section.value, '$.end_offset')
                    )
                )
            )
        )
    """


def current_planning_study_basis_filter(item_alias: str, version_alias: str) -> str:
    """Exclude known replaced interpretations without hiding historical artifacts.

    A study version pin locates the original interpretation, not an instruction
    to reject every version change. Display edits preserve its Markdown and
    reading scope. Legacy records without a resolvable pin retain their existing
    source/evidence contract. The same predicate serves search and exact reads.
    """
    ref = "CASE WHEN study_ref.type = 'object' THEN study_ref.value ELSE '{}' END"
    lost_scope = []
    for path in ("source_refs", "carryover_refs"):
        old = "CASE WHEN old_ref.type = 'object' THEN old_ref.value ELSE '{}' END"
        current = "CASE WHEN current_ref.type = 'object' THEN current_ref.value ELSE '{}' END"
        lost_scope.append(f"""
            EXISTS (
                SELECT 1 FROM json_each(study_pin.content_json, '$.{path}') AS old_ref
                WHERE old_ref.type = 'object'
                  AND json_type({old}, '$.source_id') = 'text'
                  AND json_type({old}, '$.snapshot_id') = 'text'
                  AND json_type({old}, '$.content_hash') = 'text'
                  AND NOT EXISTS (
                      SELECT 1 FROM json_each(study_current.content_json, '$.{path}') AS current_ref
                      WHERE current_ref.type = 'object'
                        AND ({_reference_coverage(old, current, 'carryover' if path == 'carryover_refs' else 'study_source')})
                  )
            )
        """)
    return f"""
        (
            json_extract({version_alias}.content_json, '$.learning_role')
                IS NOT 'planning_method_synthesis'
            OR NOT EXISTS (
                SELECT 1
                FROM json_each({version_alias}.content_json, '$.derived_from_studies') AS study_ref
                JOIN knowledge_item AS study
                  ON study.id = json_extract({ref}, '$.item_id')
                 AND study.space = {item_alias}.space AND study.work_id IS {item_alias}.work_id
                 AND study.kind = 'source_study'
                JOIN knowledge_version AS study_pin
                  ON study_pin.item_id = study.id
                 AND study_pin.version = json_extract({ref}, '$.version')
                JOIN knowledge_version AS study_current
                  ON study_current.item_id = study.id
                 AND study_current.version = (
                     SELECT MAX(latest_study.version) FROM knowledge_version AS latest_study
                     WHERE latest_study.item_id = study.id
                 )
                WHERE json_type({ref}, '$.version') = 'integer'
                  AND json_extract({ref}, '$.version') > 0
                  AND (
                      study.status NOT IN ('draft', 'reviewed')
                      OR (json_type(study_pin.content_json, '$.markdown') = 'text'
                          AND json_extract(study_pin.content_json, '$.markdown')
                              IS NOT json_extract(study_current.content_json, '$.markdown'))
                      OR {' OR '.join(lost_scope)}
                  )
            )
        )
    """
