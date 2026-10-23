"""Parse the learning-note evidence marker format consistently."""

from __future__ import annotations

import re


_EVIDENCE_MARKER = re.compile(r"【EVIDENCE:([^】]+)】")
_REPEATED_PREFIX = "EVIDENCE:"


def evidence_marker_ids(text: str) -> list[str]:
    """Return unique IDs from evidence markers, including grouped markers.

    The marker's initial ``EVIDENCE:`` is consumed by the outer expression.
    A model may separate grouped IDs with commas or semicolons and repeat the
    prefix, e.g. ``【EVIDENCE:id-a; EVIDENCE:id-b】``. Remove exactly that one repeated
    prefix; malformed or out-of-scope IDs remain unchanged for strict callers
    to reject against the current study item's evidence set.
    """

    result: list[str] = []
    seen: set[str] = set()
    for marker in _EVIDENCE_MARKER.findall(text):
        for raw_id in re.split(r"[,;]", marker):
            evidence_id = raw_id.strip()
            if evidence_id.startswith(_REPEATED_PREFIX):
                evidence_id = evidence_id[len(_REPEATED_PREFIX) :].strip()
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                result.append(evidence_id)
    return result
