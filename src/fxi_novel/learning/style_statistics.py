"""Deterministic surface counts for the exact text delivered to distillation."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


def surface_statistics(text: str) -> dict[str, Any]:
    paragraphs = [line for line in text.splitlines() if line.strip()]
    def length(value: str) -> int:
        return sum(not char.isspace() for char in value)
    paragraph_lengths = [length(line) for line in paragraphs]
    sentence_lengths = []
    for paragraph in paragraphs:
        start = 0
        for match in re.finditer(r'''[。！？!?]+[”’」』"']*|$''', paragraph):
            size = length(paragraph[start:match.end()])
            if size:
                sentence_lengths.append(size)
            start = match.end()

    def distribution(values: list[int], ranges: list[tuple[str, int, int | None]]) -> dict[str, Any]:
        counts = {label: sum(value >= low and (high is None or value <= high) for value in values)
                  for label, low, high in ranges}
        return {"count": len(values), "maximum": max(values, default=0), "counts": counts,
                "percentages": {label: round(count * 100 / len(values), 2) if values else 0
                                for label, count in counts.items()}}

    characters = length(text)
    counts = Counter(text)
    punctuation = {char: counts[char] for char in "，。！？：；、…—!?.,:;"}
    return {
        "counting_basis": {
            "length": "unicode_codepoints_excluding_whitespace_including_punctuation",
            "paragraph": "nonempty_line_including_headings_interfaces_and_asides",
            "sentence_segment": "split_each_line_after_。！？!?_with_closing_quotes;_include_unterminated_tail",
            "quotation_presence": "quotation_mark_presence_only_not_dialogue_classification",
        },
        "characters": characters,
        "paragraphs": distribution(paragraph_lengths, [
            ("1-10", 1, 10), ("11-20", 11, 20), ("21-40", 21, 40), ("41-70", 41, 70), ("71+", 71, None),
        ]),
        "sentence_segments": distribution(sentence_lengths, [
            ("1-10", 1, 10), ("11-30", 11, 30), ("31+", 31, None),
        ]),
        "punctuation_counts": punctuation,
        "punctuation_per_3000_characters": {
            char: round(count * 3000 / characters, 2) if characters else 0 for char, count in punctuation.items()
        },
        "paragraphs_with_quotation_marks": sum(any(char in line for char in '“”「」『』"') for line in paragraphs),
    }
