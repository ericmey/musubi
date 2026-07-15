import regex


def truncate_grapheme_safe(text: str, max_chars: int) -> str:
    r"""Truncate text safely at grapheme cluster boundaries.

    Python's native `text[:max_chars]` slices by unicode codepoints. This yields valid Unicode
    but can bisect extended graphemes (like family groups, ZWJ sequences, regional indicators,
    or combined diacritics) resulting in a semantically broken glyph.

    This uses the PCRE `\X` grapheme cluster matcher from the `regex` package to step through
    visual characters accurately, dropping the final split grapheme if it exceeds the `max_chars` budget.
    """
    if len(text) <= max_chars:
        return text

    # We need to find the string up to max_chars CODEPOINTS that does not split a grapheme.
    # The requirement: "preserve the existing cap unit as a maximum CODEPOINT budget...
    # choose the last complete grapheme whose end <= budget (or <= max_chars-3 before appending '...')".

    budget = max_chars
    if max_chars > 3:
        budget = max_chars - 3

    result = []
    current_codepoint_len = 0

    # regex.finditer(r'\X', text) yields full grapheme clusters
    for match in regex.finditer(r"\X", text):
        grapheme = match.group()
        grapheme_len = len(grapheme)
        if current_codepoint_len + grapheme_len > budget:
            break
        result.append(grapheme)
        current_codepoint_len += grapheme_len

    truncated_str = "".join(result)

    if max_chars <= 3:
        return truncated_str

    return truncated_str + "..."
