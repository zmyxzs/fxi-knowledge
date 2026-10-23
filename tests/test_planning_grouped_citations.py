"""Separator compatibility preserves strict observation/source membership."""
import pytest

from fxi_novel.learning.citations import evidence_marker_ids
from fxi_novel.learning.selection import _extract_observations


@pytest.mark.parametrize("separator", [",", ";"])
def test_grouped_markers_reach_the_same_observation_owner(separator):
    note = f"## 原例\n实际过程【EVIDENCE:id-a{separator} EVIDENCE:id-b】\n回应【EVIDENCE:id-a】"
    assert evidence_marker_ids(note) == ["id-a", "id-b"]
    rows = _extract_observations(note, {"id-a": {}, "id-b": {}}, analysis_type="planning")
    assert rows[0]["evidence_ids"] == ["id-a", "id-b"]
    assert rows[0]["text"] == note.split("\n", 1)[1]


@pytest.mark.parametrize("unknown", ["outside-id", "EVIDENCE:EVIDENCE:id-b"])
def test_grouped_marker_does_not_hide_unknown_or_malformed_identity(unknown):
    note = f"## 原例\n实际过程【EVIDENCE:id-a; {unknown}】"
    with pytest.raises(ValueError, match="outside its study item"):
        _extract_observations(note, {"id-a": {}, "id-b": {}}, analysis_type="planning")
