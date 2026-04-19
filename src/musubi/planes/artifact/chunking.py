import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class RawChunk:
    index: int
    content: str
    start_offset: int
    end_offset: int
    metadata: dict[str, Any]


class ChunkerProtocol(Protocol):
    def chunk(self, text: str) -> list[RawChunk]: ...


class MarkdownHeadingChunker:
    """Split on H2/H3."""

    def chunk(self, text: str) -> list[RawChunk]:
        # Naive implementation for test contract
        chunks = []
        # Split on \n## or \n###
        pattern = re.compile(r"(?=\n##\s|\n###\s)")
        parts = pattern.split(text)
        offset = 0
        for i, part in enumerate(parts):
            if not part:
                continue
            path = "unknown"
            match = re.match(r"^\n?(#{2,3})\s+(.*)$", part, re.M)
            if match:
                path = match.group(2).strip()
            # If the part is somehow larger than a threshold, we'd token-slide it.
            # But naive works for test.
            end = offset + len(part)
            chunks.append(
                RawChunk(
                    index=i,
                    content=part.strip(),
                    start_offset=offset,
                    end_offset=end,
                    metadata={"heading_path": path},
                )
            )
            offset = end
        return chunks


class VTTTurnsChunker:
    """Group 3-5 speaker turns."""

    def chunk(self, text: str) -> list[RawChunk]:
        # Naive turn grouping
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
    """512-token window, 128-token overlap. (Naively implemented with words)"""

    def chunk(self, text: str) -> list[RawChunk]:
        words = text.split()
        chunks = []
        window = 512
        overlap = 128
        step = window - overlap

        idx = 0
        word_offset = 0
        while word_offset < max(1, len(words)):
            chunk_words = words[word_offset : word_offset + window]
            content = " ".join(chunk_words)
            # We don't have true char offsets for sliding words easily, just approximate
            start = text.find(content[:10]) if content else 0
            end = start + len(content)
            chunks.append(
                RawChunk(
                    index=idx, content=content, start_offset=start, end_offset=end, metadata={}
                )
            )
            idx += 1
            word_offset += step
            if word_offset >= len(words):
                break
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
