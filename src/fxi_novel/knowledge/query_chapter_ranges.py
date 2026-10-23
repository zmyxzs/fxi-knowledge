"""Exact grammar for explicit chapter windows; no event or story-time inference."""

from __future__ import annotations

import re
import unicodedata

_NUMBER = r"[0-9零〇一二三四五六七八九十百千万两]+"
_DIGITS = {character: index for index, character in enumerate("零一二三四五六七八九")}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_TITLE = re.compile(rf"^(?:[0-9]+[ ._-]+)?第(?P<number>{_NUMBER})章")
_PREFIX = re.compile(rf"^前(?P<last>{_NUMBER})(?:正式)?章(?:中|内|以内)?$")
_RANGE = re.compile(rf"^第?(?P<first>{_NUMBER})章(?:至|到|[-–—~～])第?(?P<last>{_NUMBER})章(?:内|之间)?$")
_SHORT_RANGE = re.compile(rf"^第?(?P<first>{_NUMBER})(?:至|到|[-–—~～])第?(?P<last>{_NUMBER})章(?:内|之间)?$")
_SINGLE = re.compile(rf"^第(?P<number>{_NUMBER})章(?:内)?$")
_CUTOFF = re.compile(rf"^(?:截止|截至)第?(?P<number>{_NUMBER})章$")
_CHAPTER_END = re.compile(rf"^第(?P<number>{_NUMBER})章结束时$")


def _chinese_number(number: int) -> str:
    if number >= 10000:
        upper, lower = divmod(number, 10000)
        return _chinese_number(upper) + "万" + (
            ("零" if lower < 1000 else "") + _chinese_number(lower) if lower else ""
        )
    result, gap = "", False
    for unit, label in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, number = divmod(number, unit)
        if digit:
            result += ("零" if gap else "") + "零一二三四五六七八九"[digit] + label
            gap = False
        elif result and number:
            gap = True
    return result.removeprefix("一") if result.startswith("一十") else result


def chapter_number(text: str) -> int | None:
    """Normalize only unambiguous numeric labels, including full-width digits."""
    value = unicodedata.normalize("NFKC", text).replace("〇", "零").replace("两", "二")
    if re.fullmatch(r"[0-9]+", value):
        number = int(value) if len(value) <= 7 else 0
    elif value and all(character in _DIGITS for character in value):
        number = int("".join(str(_DIGITS[character]) for character in value)) if len(value) <= 6 else 0
    elif value and all(character in _DIGITS or character in _UNITS for character in value):
        total, section, digit = 0, 0, 0
        for character in value:
            if character in _DIGITS:
                digit = _DIGITS[character]
            elif character == "万":
                total += (section + digit) * 10000
                section, digit = 0, 0
            else:
                section += (digit or 1) * _UNITS[character]
                digit = 0
        number = total + section + digit
        canonical = _chinese_number(number) if 1 <= number <= 100_000 else ""
        if value != canonical and not (value.startswith("一十") and value[1:] == canonical):
            return None
    else:
        return None
    return number if 1 <= number <= 100_000 else None


def chapter_number_from_title(title: str) -> int | None:
    match = _TITLE.match(unicodedata.normalize("NFKC", title).strip())
    return chapter_number(match["number"]) if match else None


def explicit_chapter_window(expression: str, relation: str) -> tuple[int, int] | None:
    """Read a closed chapter-source window; ambiguous phases remain unresolved."""
    text = unicodedata.normalize("NFKC", expression).strip()
    if relation not in {"during", "at", "as_of", "before"}:
        return None
    match = _PREFIX.fullmatch(text)
    # Existing planner receipts classify “前N章中” as before. Its literal
    # prefix still specifies chapters 1..N; “第N章之前” remains unresolved.
    if match and relation in {"during", "as_of", "before"}:
        first, last = 1, chapter_number(match["last"])
    elif relation == "during" and (match := (_RANGE.fullmatch(text) or _SHORT_RANGE.fullmatch(text))):
        first, last = chapter_number(match["first"]), chapter_number(match["last"])
    elif relation == "as_of" and (match := (
            _CUTOFF.fullmatch(text) or _SINGLE.fullmatch(text) or _CHAPTER_END.fullmatch(text))):
        first, last = 1, chapter_number(match["number"])
    elif relation in {"at", "during"} and (match := _SINGLE.fullmatch(text)):
        first = last = chapter_number(match["number"])
    else:
        return None
    return (first, last) if first is not None and last is not None and first <= last else None


__all__ = ["chapter_number_from_title", "explicit_chapter_window"]
