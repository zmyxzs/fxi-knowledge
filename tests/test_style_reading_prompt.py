"""Stage examples expand once without becoming source evidence."""
import json
from pathlib import Path
import re

import pytest

from fxi_novel.learning.service_style_reading import load_style_reading_prompt
from fxi_novel.learning.service_style import _resolve_style_excerpt_selections
from fxi_novel.learning.style_fingerprint import DnaFingerprint, StyleSynthesisResponse


STAGE_EXAMPLES = [
    ("{{style_reading_example}}", "style-reading-example.md"),
    ("{{style_synthesis_example}}", "style-synthesis-example.md"),
]


def test_legacy_prompt_does_not_require_example_file(tmp_path):
    path = tmp_path / "legacy.md"
    path.write_text(" Historical instructions.\n", encoding="utf-8")
    assert load_style_reading_prompt(path) == "Historical instructions."


@pytest.mark.parametrize("marker, filename", STAGE_EXAMPLES)
def test_explicit_reference_requires_example(tmp_path, marker, filename):
    path = tmp_path / "prompt.md"
    path.write_text(marker, encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_style_reading_prompt(path)


@pytest.mark.parametrize("marker, filename", STAGE_EXAMPLES)
@pytest.mark.parametrize("example, count", [("", 1), ("Example", 2)])
def test_empty_or_repeated_example_is_not_silently_delivered(tmp_path, marker, filename, example, count):
    path = tmp_path / "prompt.md"
    path.write_text(" ".join([marker] * count), encoding="utf-8")
    (tmp_path / filename).write_text(example, encoding="utf-8")
    with pytest.raises(ValueError):
        load_style_reading_prompt(path)


def test_one_prompt_cannot_accidentally_expand_both_stage_examples(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text(" ".join(marker for marker, _ in STAGE_EXAMPLES), encoding="utf-8")
    with pytest.raises(ValueError, match="one stage example"):
        load_style_reading_prompt(path)


def synthesis_example():
    path = Path(__file__).resolve().parents[1] / "prompts/learning/style-synthesis-example.md"
    text = path.read_text(encoding="utf-8")
    body = text.split("```json\n", 1)[1].split("```", 1)[0]
    samples = {}
    for sample_id, block in re.findall(r"^示范样本(\w+)：\n((?:\[p\d+\] .+\n)+)", text, re.MULTILINE):
        rows = re.findall(r"^\[p(\d+)\] (.+)$", block, re.MULTILINE)
        assert [int(number) for number, _ in rows] == list(range(1, len(rows) + 1))
        assert sample_id not in samples
        samples[sample_id] = [paragraph for _, paragraph in rows]
    return StyleSynthesisResponse.model_validate(json.loads(body)), samples


def test_synthesis_example_obeys_the_actual_output_contract():
    example, samples = synthesis_example()
    assert samples
    assert example.operations
    for operation in example.operations:
        assert operation.avoid_when == ""
        for support in operation.examples:
            selection = support.source_selection
            assert 1 <= selection.start_paragraph <= selection.end_paragraph <= len(samples[selection.sample_id])


def test_synthesis_sentence_forms_pair_skeleton_quote_and_condition():
    example, samples = synthesis_example()
    entries = re.findall(r"^\d+\. 【.+】骨架：.+$", example.summary, re.MULTILINE)
    assert entries
    paragraphs = [paragraph for rows in samples.values() for paragraph in rows]
    for entry in entries:
        match = re.fullmatch(r"\d+\. 【.+】骨架：(.+?)原句：‘(.+?)’条件：(.+)", entry)
        assert match is not None, entry
        skeleton, quote, condition = match.groups()
        assert "[" in skeleton and "]" in skeleton
        assert condition.strip()
        assert any(quote in paragraph for paragraph in paragraphs), quote


def test_synthesis_anchor_directory_matches_the_resolved_complete_quotes():
    example, samples = synthesis_example()
    directory = re.findall(r"【([^】]+)】(\w+) p(\d+)-p(\d+)。", example.summary)
    assert directory == [(anchor.title, anchor.source_selection.sample_id,
                          str(anchor.source_selection.start_paragraph), str(anchor.source_selection.end_paragraph))
                         for anchor in example.golden_voice_slices]
    assert all(anchor.quote == "" for anchor in example.golden_voice_slices)
    fingerprint = DnaFingerprint.model_validate(example.model_dump())
    recovery = _resolve_style_excerpt_selections(fingerprint, [
        {"sample_id": sample_id, "text": "\n".join(rows)} for sample_id, rows in samples.items()
    ])
    assert recovery == []
    assert len(fingerprint.golden_voice_slices) == len(directory)
    for anchor in fingerprint.golden_voice_slices:
        selection = anchor.source_selection
        assert anchor.quote == "\n".join(samples[selection.sample_id][
            selection.start_paragraph - 1:selection.end_paragraph])
