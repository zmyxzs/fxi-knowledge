"""Shared meanings of persisted learning choices, independent of transport."""
from __future__ import annotations

import json

LEARNING_USES = frozenset({"planning", "prose", "prose_reference"})


def normalize_learning_libraries(libraries):
    if not isinstance(libraries, list) or len(libraries) > 20:
        raise ValueError("learning preferences require at most 20 libraries")
    result, seen = [], set()
    for row in libraries:
        if (not isinstance(row, dict) or not {"learning_work_id", "uses"} <= row.keys()
                or row.keys() - {"learning_work_id", "uses", "purposes"}):
            raise ValueError("learning preferences require learning_work_id, uses and optional purposes")
        identity, uses = row["learning_work_id"], row["uses"]
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("learning preference library identities must be unique")
        if not isinstance(uses, list) or not uses or any(not isinstance(use, str) or use not in LEARNING_USES for use in uses):
            raise ValueError("learning preference uses must be planning, prose or prose_reference")
        if "prose" in uses and "prose_reference" in uses:
            raise ValueError("one library cannot be both primary prose style and local prose reference")
        purposes = row.get("purposes", {})
        if (not isinstance(purposes, dict) or purposes.keys() - set(uses)
                or any(not isinstance(value, str) or len(value) > 2000 for value in purposes.values())):
            raise ValueError("learning reference purposes must be text keyed by selected use, at most 2000 characters each")
        purposes = {key: value.strip() for key, value in purposes.items() if value.strip()}
        result.append({"learning_work_id": identity, "uses": sorted(set(uses)),
                       **({"purposes": purposes} if purposes else {})})
        seen.add(identity)
    if sum("prose" in row["uses"] for row in result) > 1:
        raise ValueError("the prose style binding accepts one primary library; local references may use several")
    return result


def learning_preference_state(connection, scope_key, revision=None):
    row = connection.execute(
        "SELECT p.* FROM writing_learning_preference p "
        + ("JOIN writing_learning_preference_head h ON h.scope_key=p.scope_key AND h.revision=p.revision "
           "WHERE p.scope_key=?" if revision is None else "WHERE p.scope_key=? AND p.revision=?"),
        (scope_key,) if revision is None else (scope_key, revision),
    ).fetchone()
    return {"work_id": None if scope_key == "new_works" else scope_key,
            "revision": row["revision"] if row else 0,
            "libraries": json.loads(row["libraries_json"]) if row else [],
            "inherited_default_revision": row["inherited_default_revision"] if row else None}
