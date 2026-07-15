import regex


def truncate_grapheme_safe(text: str, max_chars: int, suffix: str = "") -> str:
    r"""Truncate text safely at grapheme cluster boundaries.

    Python's native `text[:max_chars]` slices by unicode codepoints. This yields valid Unicode
    but can bisect extended graphemes (like family groups, ZWJ sequences, regional indicators,
    or combined diacritics) resulting in a semantically broken glyph.

    This uses the PCRE `\X` grapheme cluster matcher from the `regex` package to step through
    visual characters accurately, dropping the final split grapheme if it exceeds the `max_chars` budget.
    """
    if len(text) <= max_chars:
        return text

    # Subtract suffix length from budget
    suffix_len = len(suffix)
    budget = max_chars
    if max_chars > suffix_len:
        budget = max_chars - suffix_len

    result = []
    current_codepoint_len = 0

    for match in regex.finditer(r"\X", text):
        grapheme = match.group()
        grapheme_len = len(grapheme)
        if current_codepoint_len + grapheme_len > budget:
            break
        result.append(grapheme)
        current_codepoint_len += grapheme_len

    truncated_str = "".join(result)

    if max_chars <= suffix_len:
        return truncated_str

    return truncated_str + suffix
