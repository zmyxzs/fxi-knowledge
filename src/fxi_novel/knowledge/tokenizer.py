"""The single Chinese tokenization path used by both FTS indexing and queries."""

import re
import unicodedata

import jieba

_REGISTERED_TERMS: set[str] = set()
_TERM_COMPONENTS: dict[str, tuple[str, ...]] = {}


def register_entity_terms(terms) -> int:
    """Teach jieba the project's entity names so OOV names stay one token.

    Indexing and querying share this path; call it before either side runs.
    Multi-character components are remembered so a partial query such as
    "风神" still matches "风神金刚腿" after the full name becomes one token.
    """
    added = 0
    for term in terms:
        if not isinstance(term, str):
            continue
        value = term.strip()
        if len(value) < 2 or value in _REGISTERED_TERMS:
            continue
        components = tuple(dict.fromkeys(
            token.strip() for token in jieba.cut(value, cut_all=False, HMM=False)
            if len(token.strip()) >= 2 and token.strip() != value
        ))
        jieba.add_word(value)
        _REGISTERED_TERMS.add(value)
        if components:
            _TERM_COMPONENTS[value] = components
        added += 1
    return added


def tokenize_chinese(text: str) -> list[str]:
    """Split text with jieba and discard whitespace-only/punctuation-only tokens."""
    terms: list[str] = []
    for token in jieba.cut(text, cut_all=False, HMM=False):
        normalized = token.strip().casefold()
        if not normalized:
            continue
        if all(
            character.isspace()
            or unicodedata.category(character)[0] in {"P", "S", "Z", "C"}
            for character in normalized
        ):
            continue
        terms.append(normalized)
    return terms


def fts_document(text: str) -> str:
    """Return the normalized terms stored in the FTS projection.

    Registered names also contribute their multi-character components, so a
    partial query such as "风神" still matches "风神金刚腿" without adding
    single characters (which would break exact phrase filters).
    """
    terms = tokenize_chinese(text)
    components = [
        component
        for term in terms
        for component in _TERM_COMPONENTS.get(term, ())
    ]
    return " ".join(dict.fromkeys([*terms, *components]))


def fts_documents(texts: list[str], *, batch_size: int = 128) -> list[str]:
    """Tokenize bounded document batches in one jieba pass per batch."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    results: list[str] = []
    for offset in range(0, len(texts), batch_size):
        batch = texts[offset : offset + batch_size]
        marker = "fxiuniquedocumentboundarytoken98731"
        while any(marker in text for text in batch):
            marker += "x"
        joined = f" {marker} ".join(batch)
        documents: list[list[str]] = []
        terms: list[str] = []
        for token in jieba.cut(joined, cut_all=False, HMM=False):
            normalized = token.strip().casefold()
            if normalized == marker:
                documents.append(terms)
                terms = []
            elif normalized and not all(
                character.isspace()
                or unicodedata.category(character)[0] in {"P", "S", "Z", "C"}
                for character in normalized
            ):
                terms.append(normalized)
        documents.append(terms)
        results.extend(" ".join(document) for document in documents)
    return results


def fts_query(text: str) -> str:
    """Build a literal OR query from the same terms used for document indexing.

    Registered names also expand to their multi-character components so a
    query still matches card indexes that were built before registration.
    """
    terms: list[str] = []
    for term in tokenize_chinese(text):
        terms.append(term)
        terms.extend(_TERM_COMPONENTS.get(term, ()))
    quoted = ['"' + term.replace('"', '""') + '"' for term in dict.fromkeys(terms)]
    return " OR ".join(quoted)


def fts_phrase_query(text: str) -> str:
    """Build one literal phrase from the same terms used for document indexing."""
    terms = tokenize_chinese(text)
    if not terms:
        return ""
    phrase = " ".join(term.replace('"', '""') for term in terms)
    return f'"{phrase}"'


def fts_character_query(text: str) -> str:
    """Character-level OR fallback for indexes built before entity registration."""
    characters = [
        character for character in text
        if "\u4e00" <= character <= "\u9fff"
    ]
    return " OR ".join(
        '"' + character.replace('"', '""') + '"'
        for character in dict.fromkeys(characters)
    )


def fts_phrase_group_query(text: str) -> str:
    """Preserve explicit phrase groups so common single-character tokens do not flood recall."""
    groups = [
        item for item in re.split(r"[\s,，。.!！?？;；、:：|/]+", text.strip())
        if item
    ]
    if not groups:
        return ""

    if len(groups) > 1:
        clauses = [fts_phrase_query(group) for group in groups]
    else:
        terms = tokenize_chinese(groups[0])
        clauses = [fts_phrase_query(groups[0])]
        clauses.extend(
            '"' + term.replace('"', '""') + '"'
            for term in terms
            if len(term) > 1
        )
    return " OR ".join(dict.fromkeys(clause for clause in clauses if clause))


def fts_query_requiring_phrases(
    text: str,
    phrases: list[str] | None,
    *,
    required_texts: list[str] | None = None,
    preserve_phrase_groups: bool = False,
) -> str:
    """Anchor lexical queries to named phrases and selected conjunctive terms."""
    query = fts_phrase_group_query(text) if preserve_phrase_groups else fts_query(text)
    if not query:
        return query

    constraints: list[str] = []
    required_phrases = list(dict.fromkeys(
        phrase_query
        for phrase in (phrases or [])
        if (phrase_query := fts_phrase_query(phrase))
    ))
    if required_phrases:
        constraints.append(f"({' OR '.join(required_phrases)})")

    required_terms = list(dict.fromkeys(
        term
        for required_text in (required_texts or [])
        for term in tokenize_chinese(required_text)
        if len(term) > 1
    ))
    if required_terms:
        constraints.append("(" + " AND ".join(
            '"' + term.replace('"', '""') + '"' for term in required_terms
        ) + ")")

    if not constraints:
        return query
    return f"({query}) AND " + " AND ".join(constraints)
