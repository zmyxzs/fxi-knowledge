import json
from fxi_novel.knowledge.graph_parser_support import _evidence_units, _unique_quote_for_evidence_unit
from fxi_novel.knowledge.graph_parser_resume import build_parse_group_request


def test_equipment_properties_and_conditions_stay_with_their_subject():
    armor = '金狼战甲：黄金装备，防御+150，生命+150。攻击时附带50风属性伤害，一定概率反弹30%的伤害。'
    boots = '金狼战靴：黄金装备，防御+50，移动速度+5。'
    text = armor + '\n' + boots
    segment = {'ref': 'E1', 'text': text, 'start_offset': 0, 'end_offset': len(text), 'evidence_span_id': 'span'}
    units = _evidence_units(segment)
    assert [u['text'] for u in units] == [armor, boots]
    registry, _, request, input_hash = build_parse_group_request((segment,), branch_id=None)
    assert json.loads(request)['segments'][0]['evidence_units'] == [
        {'ref': 'X1', 'text': armor}, {'ref': 'X2', 'text': boots}]
    for ref, expected in [('X1', armor), ('X2', boots)]:
        s, start, end = registry[ref]
        assert _unique_quote_for_evidence_unit(s, start, end) == expected
    assert build_parse_group_request((segment,), branch_id=None)[3] == input_hash


def test_paragraphs_preserve_crlf_offsets_and_do_not_cross_target_boundary():
    text = '  村长说：“只有满足条件，才会生效；否则失败。”\r\n\r\n  下一段。 '
    units = _evidence_units({'text': text})
    assert len(units) == 2
    assert all(text[u['start']:u['end']] == u['text'] for u in units)
    assert units[0]['text'] == '村长说：“只有满足条件，才会生效；否则失败。”'
    assert units[1]['text'] == '下一段。'
    assert _evidence_units({'text': ''}) == []


def test_long_paragraphs_remain_bounded_and_lossless():
    text = '前半句，后半句。' * 400
    units = _evidence_units({'text': text})
    assert ''.join(u['text'] for u in units) == text
    assert all(len(u['text']) <= 1200 for u in units)
