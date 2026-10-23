"""Overlap sharing preserves readable source text and exact citation authority."""
from copy import deepcopy
import json

import pytest

from fxi_novel.knowledge.query_assessment_projection import (
    expand_assessment_materials, project_assessment_materials, restore_assessment_row_evidence,
)
from test_query_assessment_projection import _payload


def overlapping():
    payload = _payload()
    text = ''.join(f'第{i}次：她说尚未确认，等他回来再问。🌙\n' for i in range(240))
    payload['evidence'] = [
        {'id': f'evidence-{i}', 'space': 'learning', 'work_id': None,
         'source_id': 'source', 'snapshot_id': 'snapshot', 'start_offset': start, 'end_offset': end,
         'role': 'supporting' if i != 2 else 'context', 'quote': text[start:end]}
        for i, (start, end) in enumerate(((0, 2400), (1300, 3600), (1600, 1700)), 1)
    ]
    payload['requirements'] = [{'id': f'req-{i}', 'available_evidence_ids': [f'evidence-{i+1}']}
                               for i in range(3)]
    return payload, text


def test_overlapping_and_contained_quotes_share_text_without_expanding_citation_permissions():
    original, text = overlapping()
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    assert len(projected['source_passages']) == 1
    segments = projected['source_passages']['passage-1']['segments']
    assert ''.join(row['text'] for row in segments) == text[:3600]
    for segment in segments:
        expected = [f'e{i}' for i, ref in enumerate(original['evidence'], 1)
                    if ref['start_offset'] <= segment['start_offset'] and ref['end_offset'] >= segment['end_offset']]
        assert set(segment['evidence_ids']) == set(expected)
    assert [row['available_evidence_ids'] for row in projected['requirements']] == [['e1'], ['e2'], ['e3']]
    assert projected['evidence'][1]['role'] == 'context'
    assert expand_assessment_materials(projected) == original == frozen
    assert len(json.dumps(projected, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))
    assert restore_assessment_row_evidence({'evidence_ids': ['e3']}, projected) == {'evidence_ids': ['evidence-3']}
    with pytest.raises(ValueError, match='unknown request-local evidence ID'):
        restore_assessment_row_evidence({'evidence_ids': ['passage-1']}, projected)


@pytest.mark.parametrize('field,value', [('snapshot_id','other'), ('source_id','other'),
                                        ('work_id','other'), ('space','writing'), ('branch_id','other')])
def test_different_source_identity_cannot_share_a_continuous_passage(field, value):
    original, _ = overlapping()
    original['evidence'] = original['evidence'][:2]
    original['evidence'][1][field] = value
    projected = project_assessment_materials(original)
    assert 'source_passages' not in projected
    assert expand_assessment_materials(projected) == original


def test_conflicting_overlap_remains_unmerged_without_repair_or_loss():
    original, _ = overlapping()
    original['evidence'][1]['quote'] = '错' + original['evidence'][1]['quote'][1:]
    projected = project_assessment_materials(original)
    assert 'source_passages' not in projected
    assert expand_assessment_materials(projected) == original


def test_disjoint_and_invalid_ranges_do_not_invent_missing_source_text():
    original, _ = overlapping()
    original['evidence'][1]['start_offset'] += 6000
    original['evidence'][1]['end_offset'] += 6000
    original['evidence'][2]['end_offset'] += 1
    projected = project_assessment_materials(original)
    assert 'source_passages' not in projected
    assert expand_assessment_materials(projected) == original


def test_small_overlaps_keep_simpler_inline_quotes():
    original, _ = overlapping()
    original['evidence'] = [
        {**original['evidence'][0], 'start_offset': 0, 'end_offset': 4, 'quote': '甲乙丙丁'},
        {**original['evidence'][1], 'start_offset': 2, 'end_offset': 6, 'quote': '丙丁戊己'},
    ]
    projected = project_assessment_materials(original)
    assert 'source_passages' not in projected
    assert expand_assessment_materials(projected) == original


def test_audit_rejects_a_passage_that_omits_part_of_an_evidence_range():
    original, _ = overlapping()
    projected = project_assessment_materials(original)
    projected['source_passages']['passage-1']['segments'][0]['evidence_ids'] = []
    with pytest.raises(ValueError, match='exact evidence interval'):
        expand_assessment_materials(projected)


def test_reordered_and_duplicate_evidence_restore_exactly():
    original, _ = overlapping()
    original['evidence'].append({**original['evidence'][0], 'id': 'evidence-4'})
    original['evidence'].reverse()
    assert expand_assessment_materials(project_assessment_materials(original)) == original


def test_source_sharing_preserves_exact_identity_and_citation_boundaries():
    original, _ = overlapping()
    for row in original['evidence']:
        row['content_hash'] = 'a' * 64
    original['evidence'].append({**original['evidence'][0], 'id': 'other-snapshot',
                                 'snapshot_id': 'different-snapshot'})
    original['evidence'].append({**original['evidence'][0], 'id': 'explicit-null',
                                 'branch_id': None})
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    refs = projected['evidence']
    assert refs[0]['source_ref'] == refs[1]['source_ref'] == refs[2]['source_ref']
    assert len({refs[i]['source_ref'] for i in (0, 3, 4)}) == 3
    assert expand_assessment_materials(projected) == original == frozen
    assert restore_assessment_row_evidence({'evidence_ids': ['e4']}, projected) == {
        'evidence_ids': ['other-snapshot']}
    broken = deepcopy(projected)
    broken['evidence'][0]['source_ref'] = 'missing'
    with pytest.raises(KeyError):
        expand_assessment_materials(broken)
    broken = deepcopy(projected)
    broken['evidence'][0]['source_id'] = 'override'
    with pytest.raises(ValueError, match='source identity'):
        expand_assessment_materials(broken)


def test_old_receipts_without_shared_sources_remain_readable():
    original, _ = overlapping()
    projected = project_assessment_materials(original)
    sources = projected.pop('evidence_sources', {})
    for row in [*projected['evidence'], *projected.get('source_passages', {}).values()]:
        if 'source_ref' in row:
            row.update(sources[row.pop('source_ref')])
    assert expand_assessment_materials(projected) == original


def copied_source_passages():
    original, text = overlapping()
    originals = deepcopy(original['evidence'])
    for index, row in enumerate(originals[:2], 4):
        original['evidence'].append({
            **row, 'id': f'evidence-{index}', 'space': 'writing', 'work_id': 'other-work',
            'branch_id': 'other-branch', 'source_id': 'base-source',
            'snapshot_id': 'base-snapshot', 'content_hash': 'different-hash',
            'start_offset': row['start_offset'] + 500,
            'end_offset': row['end_offset'] + 500,
            'role': 'context', 'purpose': 'author_reference',
        })
    original['requirements'].append({'id': 'base-only', 'available_evidence_ids': ['evidence-4']})
    return original, text


def test_exact_passage_text_shares_across_sources_with_distinct_coordinates_and_segments():
    original, text = copied_source_passages()
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    first, second = projected['source_passages'].values()
    assert first['text_ref'] == second['text_ref']
    assert projected['source_passage_texts'] == {first['text_ref']: text[:3600]}
    assert len(first['segments']) != len(second['segments'])
    assert first['source_ref'] != second['source_ref']
    assert first['segments'][0]['start_offset'] == 0
    assert second['segments'][0]['start_offset'] == 500
    assert all('text' not in row for passage in (first, second) for row in passage['segments'])
    assert projected['requirements'][-1]['available_evidence_ids'] == ['e4']
    assert projected['evidence'][3]['role'] == 'context'
    assert projected['evidence'][3]['purpose'] == 'author_reference'
    assert expand_assessment_materials(projected) == original == frozen
    assert len(json.dumps(projected, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))
    assert restore_assessment_row_evidence({'evidence_ids': ['e4']}, projected) == {
        'evidence_ids': ['evidence-4']}
    with pytest.raises(ValueError, match='unknown request-local evidence ID'):
        restore_assessment_row_evidence({'evidence_ids': [first['text_ref']]}, projected)


def test_one_changed_character_in_another_source_prevents_whole_passage_text_sharing():
    original, _ = copied_source_passages()
    # Change both overlapping quotes consistently, without choosing one source as truth.
    for row in original['evidence'][3:]:
        offset = 1500 - row['start_offset']
        if 0 <= offset < len(row['quote']):
            row['quote'] = row['quote'][:offset] + '异' + row['quote'][offset + 1:]
    projected = project_assessment_materials(original)
    assert len(projected['source_passages']) == 2
    assert 'source_passage_texts' not in projected
    assert expand_assessment_materials(projected) == original


@pytest.mark.parametrize('corruption', [
    'short_text', 'non_text', 'gap', 'inline_text', 'empty_segments', 'negative_offset', 'bool_offset',
])
def test_shared_passage_text_audit_rejects_invalid_coordinates_or_conflicting_text(corruption):
    original, _ = copied_source_passages()
    projected = project_assessment_materials(original)
    passage = projected['source_passages']['passage-2']
    identifier = passage['text_ref']
    if corruption == 'short_text':
        projected['source_passage_texts'][identifier] = 'short'
    elif corruption == 'non_text':
        projected['source_passage_texts'][identifier] = 1
    elif corruption == 'gap':
        passage['segments'][0]['end_offset'] -= 1
    elif corruption == 'inline_text':
        passage['segments'][0]['text'] = 'override'
    elif corruption == 'negative_offset':
        passage['segments'][0]['start_offset'] = -1
    elif corruption == 'bool_offset':
        passage['segments'][0]['start_offset'] = True
    else:
        passage['segments'] = []
    with pytest.raises(ValueError, match='shared source passage text'):
        expand_assessment_materials(projected)


def test_shared_passage_text_does_not_transfer_evidence_coverage_from_another_source():
    original, _ = copied_source_passages()
    projected = project_assessment_materials(original)
    passages = list(projected['source_passages'].values())
    passages[1]['segments'][0]['evidence_ids'] = passages[0]['segments'][0]['evidence_ids']
    with pytest.raises(ValueError, match='exact evidence interval'):
        expand_assessment_materials(projected)


def test_shared_passage_text_reference_must_be_present_in_this_request():
    original, _ = copied_source_passages()
    projected = project_assessment_materials(original)
    projected['source_passages']['passage-2']['text_ref'] = 'missing'
    with pytest.raises(KeyError):
        expand_assessment_materials(projected)
