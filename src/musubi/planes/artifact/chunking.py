from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from tokenizers import Tokenizer

_BGE_M3_TOKENIZER = "BAAI/bge-m3"
_DEFAULT_WINDOW_TOKENS = 512
_DEFAULT_OVERLAP_TOKENS = 128
_SENTENCE_END_RE = re.compile(r"[.!?。？！][\"')\]]*$")


@dataclass
class RawChunk:
    index: int
    content: str
    start_offset: int
    end_offset: int
    metadata: dict[str, Any]


class ChunkerProtocol(Protocol):
    def chunk(self, text: str) -> list[RawChunk]: ...


@runtime_checkable
class TokenizerProtocol(Protocol):
    def encode(self, sequence: str) -> Any: ...


@dataclass(frozen=True)
class _TokenizedText:
    ids: list[int]
    offsets: list[tuple[int, int]]


@lru_cache(maxsize=1)
def _load_bge_m3_tokenizer() -> Tokenizer:
    """Load and cache the BGE-M3 tokenizer.

    `tokenizers` handles the HuggingFace cache on disk; this process-level
    cache keeps repeated chunking calls from paying construction cost.
    """
    return Tokenizer.from_pretrained(_BGE_M3_TOKENIZER)


def _tokenize(text: str, tokenizer: TokenizerProtocol | None = None) -> _TokenizedText:
    tok = tokenizer or _load_bge_m3_tokenizer()
    encoded = tok.encode(text)
    ids = list(encoded.ids)
    offsets = [tuple(offset) for offset in encoded.offsets]

    # Drop special tokens with empty offsets. Chunk sizes are measured over
    # text-bearing tokens only, and offsets then map directly back to content.
    filtered_ids: list[int] = []
    filtered_offsets: list[tuple[int, int]] = []
    for token_id, (start, end) in zip(ids, offsets, strict=True):
        if end <= start:
            continue
        filtered_ids.append(int(token_id))
        filtered_offsets.append((int(start), int(end)))
    return _TokenizedText(ids=filtered_ids, offsets=filtered_offsets)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _likely_within_window(text: str, window_tokens: int) -> bool:
    # BGE-M3 may split punctuation/subwords, so this is intentionally
    # conservative. It only avoids tokenizer loading for clearly small
    # markdown sections.
    return len(text.split()) <= window_tokens // 2 and len(text) <= window_tokens * 4


def _sentence_boundary_token_index(
    text: str,
    offsets: Sequence[tuple[int, int]],
    *,
    max_end_token: int,
    min_end_token: int,
) -> int | None:
    for token_index in range(max_end_token - 1, min_end_token - 1, -1):
        _, char_end = offsets[token_index]
        if _SENTENCE_END_RE.search(text[:char_end].rstrip()):
            return token_index + 1
    return None


class MarkdownHeadingChunker:
    """Split on H2/H3, token-splitting oversize sections."""

    def __init__(
        self,
        *,
        tokenizer: TokenizerProtocol | None = None,
        window_tokens: int = _DEFAULT_WINDOW_TOKENS,
        overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        self._tokenizer = tokenizer
        self._window_tokens = window_tokens
        self._overlap_tokens = overlap_tokens

    def chunk(self, text: str) -> list[RawChunk]:
        chunks: list[RawChunk] = []

        for section_start, section_end, heading_path in _markdown_sections(text):
            section_text = text[section_start:section_end]
            tokenizer = self._tokenizer
            token_count: int | None = None
            if tokenizer is not None:
                token_count = len(_tokenize(section_text, tokenizer).ids)

            if (
                token_count is not None and token_count <= self._window_tokens
            ) or (
                token_count is None and _likely_within_window(section_text, self._window_tokens)
            ):
                start, end = _trim_span(text, section_start, section_end)
                if start == end:
                    continue
                chunks.append(
                    RawChunk(
                        index=len(chunks),
                        content=text[start:end],
                        start_offset=start,
                        end_offset=end,
                        metadata={
                            "heading_path": heading_path,
                            "token_count": token_count,
                        },
                    )
                )
                continue

            tokenizer = tokenizer or _load_bge_m3_tokenizer()
            splitter = TokenSlidingChunker(
                tokenizer=tokenizer,
                window_tokens=self._window_tokens,
                overlap_tokens=self._overlap_tokens,
                prefer_sentence_boundary=True,
            )
            for subchunk in splitter.chunk(section_text):
                chunks.append(
                    RawChunk(
                        index=len(chunks),
                        content=subchunk.content,
                        start_offset=section_start + subchunk.start_offset,
                        end_offset=section_start + subchunk.end_offset,
                        metadata={
                            **subchunk.metadata,
                            "heading_path": heading_path,
                            "split_from_oversize_section": True,
                        },
                    )
                )
        return chunks


def _markdown_sections(text: str) -> list[tuple[int, int, str]]:
    headings = list(re.finditer(r"(?m)^#{2,3}\s+(.+)$", text))
    sections: list[tuple[int, int, str]] = []

    if not headings:
        return [(0, len(text), "unknown")] if text else []

    first = headings[0]
    if text[: first.start()].strip():
        sections.append((0, first.start(), "unknown"))

    for idx, heading in enumerate(headings):
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        sections.append((heading.start(), end, heading.group(1).strip()))
    return sections


class VTTTurnsChunker:
    """Group 3-5 speaker turns."""

    def chunk(self, text: str) -> list[RawChunk]:
        chunks = []
        turns = text.split("\n\n")
        offset = 0
        for i, turn in enumerate(turns):
            if not turn.strip():
                continue
            end = offset + len(turn)
            chunks.append(
                RawChunk(
                    index=i,
                    content=turn.strip(),
                    start_offset=offset,
                    end_offset=end,
                    metadata={"speakers": ["Unknown"]},
                )
            )
            offset = end + 2
        return chunks


class TokenSlidingChunker:
    """BGE-M3 token windows with a 512-token window and 128-token overlap."""

    def __init__(
        self,
        *,
        tokenizer: TokenizerProtocol | None = None,
        window_tokens: int = _DEFAULT_WINDOW_TOKENS,
        overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
        prefer_sentence_boundary: bool = False,
    ) -> None:
        if window_tokens <= 0:
            raise ValueError("window_tokens must be positive")
        if overlap_tokens < 0 or overlap_tokens >= window_tokens:
            raise ValueError("overlap_tokens must be >= 0 and less than window_tokens")
        self._tokenizer = tokenizer
        self._window_tokens = window_tokens
        self._overlap_tokens = overlap_tokens
        self._prefer_sentence_boundary = prefer_sentence_boundary

    def chunk(self, text: str) -> list[RawChunk]:
        if not text:
            return []

        tokenized = _tokenize(text, self._tokenizer)
        if not tokenized.ids:
            start, end = _trim_span(text, 0, len(text))
            return [
                RawChunk(
                    index=0,
                    content=text[start:end],
                    start_offset=start,
                    end_offset=end,
                    metadata={"token_count": 0, "token_start": 0, "token_end": 0},
                )
            ]

        chunks: list[RawChunk] = []
        token_start = 0
        while token_start < len(tokenized.ids):
            token_end = min(token_start + self._window_tokens, len(tokenized.ids))
            if self._prefer_sentence_boundary and token_end < len(tokenized.ids):
                boundary = _sentence_boundary_token_index(
                    text,
                    tokenized.offsets,
                    max_end_token=token_end,
                    min_end_token=min(token_start + self._overlap_tokens + 1, token_end),
                )
                if boundary is not None:
                    token_end = boundary

            char_start = tokenized.offsets[token_start][0]
            char_end = tokenized.offsets[token_end - 1][1]
            char_start, char_end = _trim_span(text, char_start, char_end)
            chunks.append(
                RawChunk(
                    index=len(chunks),
                    content=text[char_start:char_end],
                    start_offset=char_start,
                    end_offset=char_end,
                    metadata={
                        "token_start": token_start,
                        "token_end": token_end,
                        "token_count": token_end - token_start,
                    },
                )
            )

            if token_end >= len(tokenized.ids):
                break
            token_start = max(token_end - self._overlap_tokens, token_start + 1)
        return chunks


class JsonChunker:
    """One chunk per top-level array element."""

    def chunk(self, text: str) -> list[RawChunk]:
        import json

        chunks = []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                for i, item in enumerate(data):
                    content = json.dumps(item)
                    chunks.append(
                        RawChunk(
                            index=i,
                            content=content,
                            start_offset=0,
                            end_offset=len(content),
                            metadata={"json_path": f"[{i}]"},
                        )
                    )
            else:
                chunks.append(
                    RawChunk(
                        index=0, content=text, start_offset=0, end_offset=len(text), metadata={}
                    )
                )
        except json.JSONDecodeError:
            chunks.append(
                RawChunk(index=0, content=text, start_offset=0, end_offset=len(text), metadata={})
            )
        return chunks


def get_chunker(name: str) -> ChunkerProtocol:
    if name == "markdown-headings-v1":
        return MarkdownHeadingChunker()
    if name == "vtt-turns-v1":
        return VTTTurnsChunker()
    if name == "token-sliding-v1":
        return TokenSlidingChunker()
    if name == "json-v1":
        return JsonChunker()
    return TokenSlidingChunker()
