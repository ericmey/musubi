"""LLM clients used by lifecycle sweeps.

Houses the httpx-backed :class:`HttpxOllamaClient` that satisfies the
:class:`musubi.lifecycle.maturation.OllamaClient` Protocol. Prompt
templates live under ``prompts/<name>/v<N>.txt`` — a prompt change is a
new file, never an edit (see ``docs/Musubi/06-ingestion/CLAUDE.md``).
"""

from musubi.llm.ollama import HttpxOllamaClient

__all__ = ["HttpxOllamaClient"]
