"""Deterministic text chunking with source-relative Unicode offsets."""

from dataclasses import dataclass


CHUNKER_VERSION = "plain_text_v3_section_boundaries"
OFFSET_UNIT = "unicode_codepoint"
DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 120
_BOUNDARIES = ("\n\n", "\n", "。", "！", "？", "!", "?", "；", ";", "，", ",")


@dataclass(frozen=True)
class TextChunk:
    sequence: int
    start_offset: int
    end_offset: int
    text: str


def split_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[TextChunk]:
    """Split text deterministically, preferring nearby paragraph and sentence edges."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be between zero and max_chars")

    chunks: list[TextChunk] = []
    start = 0
    text_length = len(text)
    minimum_boundary = max(1, max_chars // 2)
    while start < text_length:
        target = min(start + max_chars, text_length)
        end = target
        if target < text_length:
            lower_bound = min(target, start + minimum_boundary)
            boundary_position = -1
            boundary_length = 0
            for boundary in _BOUNDARIES:
                candidate = text.rfind(boundary, lower_bound, target)
                if candidate > boundary_position:
                    boundary_position = candidate
                    boundary_length = len(boundary)
            if boundary_position >= 0:
                end = boundary_position + boundary_length

        if end <= start:
            end = target
        chunks.append(
            TextChunk(
                sequence=len(chunks),
                start_offset=start,
                end_offset=end,
                text=text[start:end],
            )
        )
        if end >= text_length:
            break
        start = max(start + 1, end - overlap)

    return chunks
