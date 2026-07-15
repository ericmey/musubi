import pytest

from musubi.retrieve.grapheme_truncation import truncate_grapheme_safe


def test_truncation_bypasses_short_text() -> None:
    text = "Hello world"
    # Budget easily fits
    assert truncate_grapheme_safe(text, max_chars=50) == "Hello world"


def test_truncation_cuts_at_grapheme_boundaries_safely() -> None:
    text = "This is a long sentence."
    # Budget 10. `10 - 3 = 7` chars before padding.
    # "This is" is 7 chars.
    assert truncate_grapheme_safe(text, max_chars=10) == "This is..."


def test_truncation_respects_max_chars_lte_3() -> None:
    text = "Hello"
    # No room for padding, just exact slice up to boundary
    assert truncate_grapheme_safe(text, max_chars=3) == "Hel"


def test_truncation_prevents_emoji_zwj_bisection() -> None:
    # Family emoji: Man (👨), ZWJ, Woman (👩), ZWJ, Girl (👧), ZWJ, Boy (👦)
    # The whole emoji is a single grapheme cluster but 7 codepoints.
    text = "Here: 👨‍👩‍👧‍👦"  # len = 6 + 7 = 13 codepoints

    # If we truncate at 9, native `text[:9]` would slice mid-emoji.
    # Our budget is 9. The pad takes 3 -> 6.
    # "Here: " is 6. The next grapheme is 7 codepoints. 6 + 7 = 13 > 6.
    # It must drop the emoji entirely to stay under budget.
    assert truncate_grapheme_safe(text, max_chars=9) == "Here: ..."


def test_truncation_preserves_single_emoji() -> None:
    text = "Hello 🚀 world"  # len = 6 + 1 + 6 = 13.
    # budget = 12. Pad = 3 -> 9.
    # "Hello " (6) + "🚀" (1) + " " (1) + "w" (1) = 9.
    assert truncate_grapheme_safe(text, max_chars=12) == "Hello 🚀 w..."


def test_truncation_prevents_combined_diacritic_bisection() -> None:
    text = "Café"  # len = 5
    assert truncate_grapheme_safe(text, max_chars=4) == "C..."


def test_truncation_prevents_regional_indicator_bisection() -> None:
    # US Flag: 🇺 + 🇸 (2 codepoints)
    text = "Flag 🇺🇸"  # len = 5 + 2 = 7
    # Budget = 6. Pad = 3 -> 3.
    # "F" (1), "l" (1), "a" (1)
    assert truncate_grapheme_safe(text, max_chars=6) == "Fla..."


def test_truncation_preserves_internal_whitespace() -> None:
    text = "A   B   C"
    # budget = 8. pad = 3 -> 5.
    # "A" (1) + " " (1) + " " (1) + " " (1) + "B" (1) = 5.
    assert truncate_grapheme_safe(text, max_chars=8) == "A   B..."


def test_truncation_preserves_trailing_whitespace_if_within_budget() -> None:
    text = "A   B   C "
    # To drop C but keep trailing whitespace: budget = 9. pad = 3 -> 6.
    # "A   B " is 6.
    assert truncate_grapheme_safe(text, max_chars=9) == "A   B ..."


def test_truncation_prevents_skin_tone_modifier_bisection() -> None:
    # Wave (👋) + Medium Skin Tone (🏽) = 👋🏽 (2 codepoints)
    text = "Hi 👋🏽!"  # len = 3 + 2 + 1 = 6 codepoints
    # Budget = 5. Pad = 3 -> 2.
    # "H" (1), "i" (1) -> 2. Next is " ".
    assert truncate_grapheme_safe(text, max_chars=5) == "Hi..."


@pytest.mark.asyncio
async def test_fast_retrieval_uses_grapheme_truncation_for_long_content() -> None:
    from musubi.retrieve.fast import _snippet

    text = "A" * 195 + "👨‍👩‍👧‍👦"
    payload = {"content": text}
    snippet, trunc, length = _snippet(payload)
    assert trunc is True
    assert length == 202
    assert not snippet.endswith("👨")
    assert snippet.endswith("...")


@pytest.mark.asyncio
async def test_recent_retrieval_uses_grapheme_truncation_for_long_content() -> None:
    from musubi.retrieve.recent import _snippet

    text = "A" * 195 + "👨‍👩‍👧‍👦"
    payload = {"content": text}
    _snippet_val, trunc, length = _snippet(payload, max_chars=200)
    assert trunc is True
    assert length == 202


@pytest.mark.asyncio
async def test_orchestration_uses_grapheme_truncation_for_long_content() -> None:
    from musubi.retrieve.orchestration import _snippet

    text = "A" * 195 + "👨‍👩‍👧‍👦"
    payload = {"content": text}
    _snippet_val, trunc, length = _snippet(payload, max_chars=200)
    assert trunc is True
    assert length == 202


@pytest.mark.asyncio
async def test_context_pack_uses_grapheme_truncation_for_long_content() -> None:
    from musubi.retrieve.context_pack import ContextCandidate, ContextPackQuery, build_context_pack
    from musubi.types.common import generate_ksuid

    text = "hello " + "A" * 112 + "👨‍👩‍👧‍👦"

    cand = ContextCandidate(
        object_id=str(generate_ksuid()),
        namespace="test",
        plane="test",
        content=text,
        state="matured",
    )

    pack = build_context_pack(
        [cand], ContextPackQuery(query_text="hello", max_items=1, max_chars=120)
    )
    item = next((i for group in pack.groups for i in group.items), None)
    assert item is not None
    assert item.content_truncated is True
    assert item.content_length == 125
    assert item.content == "hello " + "A" * 111 + "..."
