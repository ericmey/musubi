"""httpx-backed Ollama client satisfying the maturation sweep's Protocol.

The :class:`musubi.lifecycle.maturation.OllamaClient` Protocol has two
methods — ``score_importance`` and ``infer_topics`` — both of which
must return ``None`` when Ollama is unreachable so the sweep can fall
back to captured values (see
[[06-ingestion/maturation#Failure modes]]). This module provides the
real production implementation.

Design contract:

- Every call posts to ``{base_url}/api/chat`` with
  ``format: "json"`` and ``temperature: 0`` for determinism.
- A fresh :class:`httpx.AsyncClient` per call — matches the rest of the
  codebase (see :mod:`musubi.embedding.tei`) and keeps the client
  loop-agnostic so the lifecycle worker can re-enter
  ``asyncio.run`` on every sweep tick without dragging a pool across
  loops.
- Pydantic models validate the response shape. A validator failure
  returns ``None`` for that call — callers treat that the same as an
  outage (captured values win).
- On any exception (connect error, timeout, non-2xx, invalid JSON,
  validation failure), we log and return ``None``. The sweep
  re-runs next cron tick, so one-off failures are harmless.
- Optional ``debug_dir``: when set, every failed call writes the raw
  response bytes to ``debug_dir/<epoch>-<kind>.json`` — matches the
  maturation spec's "log raw response" requirement for parse errors.
"""

from __future__ import annotations

import json
import logging
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from musubi.lifecycle.maturation import OllamaImportance, OllamaTopic
from musubi.lifecycle.synthesis import (
    ContradictionInput,
    ContradictionOutput,
    SynthesisInput,
    SynthesisOutput,
)
from musubi.types.common import KSUID, validate_ksuid

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 120.0

_IMPORTANCE_PROMPT_V = "v1"
_TOPICS_PROMPT_V = "v1"
_SYNTHESIS_PROMPT_V = "v1"
_CONTRADICTION_PROMPT_V = "v1"


class _ImportanceItem(BaseModel):
    id: str
    importance: int = Field(ge=1, le=10)


class _ImportanceResponse(BaseModel):
    items: list[_ImportanceItem]


class _TopicItem(BaseModel):
    id: str
    topics: list[str] = Field(default_factory=list)


class _TopicResponse(BaseModel):
    items: list[_TopicItem]


class _SynthesisResponse(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    rationale: str = ""
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=10)
    contradicts_notice: str = ""


class _ContradictionResponse(BaseModel):
    verdict: str = Field(pattern=r"^(consistent|contradictory)$")
    reason: str = ""


def _load_prompt(name: str, version: str) -> str:
    """Load a frozen prompt file from ``musubi.llm.prompts.<name>``."""
    resource = files("musubi.llm.prompts").joinpath(name).joinpath(f"{version}.txt")
    return resource.read_text(encoding="utf-8")


class HttpxOllamaClient:
    """Production :class:`OllamaClient` backed by ``httpx.AsyncClient``.

    Parameters
    ----------
    base_url:
        Base URL of the Ollama HTTP endpoint (e.g.
        ``http://ollama:11434``).
    model:
        Ollama-tagged model name (e.g. ``qwen3:4b``).
    timeout_s:
        Per-request timeout. Defaults to 120s — Qwen-4B on CPU fallback
        can take ~60s for a batch of 10; leave headroom.
    debug_dir:
        Optional directory to dump raw responses on parse/validation
        failure. Callers supply ``/var/lib/musubi/maturation-debug``
        in production.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        debug_dir: Path | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._debug_dir = debug_dir
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API — matches OllamaClient Protocol.
    # ------------------------------------------------------------------

    async def score_importance(self, items: list[OllamaImportance]) -> dict[KSUID, int] | None:
        """Return ``{object_id: importance}`` for items the LLM scored.

        Returns ``None`` on outage or total failure. A partial response
        (fewer IDs than input) returns only the IDs that came back —
        the sweep's caller handles the missing ones as fallbacks.
        """
        if not items:
            return {}

        rendered = "\n".join(
            f"- id={it.object_id} captured={it.captured_importance}\n  content: "
            f"{_one_line(it.content)}"
            for it in items
        )
        prompt = _load_prompt("importance", _IMPORTANCE_PROMPT_V).replace("{ITEMS}", rendered)

        raw = await self._chat(prompt, kind="importance")
        if raw is None:
            return None
        try:
            parsed = _ImportanceResponse.model_validate_json(raw)
        except ValidationError as exc:
            self._write_debug("importance", raw, reason=str(exc))
            log.warning("ollama-importance-validate-failed err=%s", exc)
            return None

        wanted = {str(it.object_id) for it in items}
        out: dict[KSUID, int] = {}
        for row in parsed.items:
            if row.id not in wanted:
                continue
            try:
                key = validate_ksuid(row.id)
            except ValueError:
                continue
            out[key] = row.importance
        return out

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[KSUID, list[str]] | None:
        """Return ``{object_id: [topic, ...]}`` for items the LLM classified."""
        if not items:
            return {}

        rendered = "\n".join(
            f"- id={it.object_id} existing={it.existing_tags}\n  content: {_one_line(it.content)}"
            for it in items
        )
        prompt = _load_prompt("topics", _TOPICS_PROMPT_V).replace("{ITEMS}", rendered)

        raw = await self._chat(prompt, kind="topics")
        if raw is None:
            return None
        try:
            parsed = _TopicResponse.model_validate_json(raw)
        except ValidationError as exc:
            self._write_debug("topics", raw, reason=str(exc))
            log.warning("ollama-topics-validate-failed err=%s", exc)
            return None

        wanted = {str(it.object_id) for it in items}
        out: dict[KSUID, list[str]] = {}
        for row in parsed.items:
            if row.id not in wanted:
                continue
            try:
                key = validate_ksuid(row.id)
            except ValueError:
                continue
            out[key] = list(row.topics)
        return out

    # ------------------------------------------------------------------
    # SynthesisOllamaClient Protocol
    # ------------------------------------------------------------------

    async def synthesize_cluster(self, cluster: SynthesisInput) -> SynthesisOutput | None:
        """Ask the LLM to condense a memory cluster into one concept.

        Returns ``None`` on outage or parse failure; the synthesis
        sweep's caller treats that as "skip this cluster, try next run".
        """
        if not cluster.memories:
            return None
        rendered = "\n".join(
            f"- id={m.object_id} importance={m.importance} tags={m.tags}\n"
            f"  content: {_one_line(m.content)}"
            for m in cluster.memories
        )
        prompt = _load_prompt("synthesis", _SYNTHESIS_PROMPT_V).replace("{ITEMS}", rendered)
        raw = await self._chat(prompt, kind="synthesis")
        if raw is None:
            return None
        try:
            parsed = _SynthesisResponse.model_validate_json(raw)
        except ValidationError as exc:
            self._write_debug("synthesis", raw, reason=str(exc))
            log.warning("ollama-synthesis-validate-failed err=%s", exc)
            return None
        return SynthesisOutput(
            title=parsed.title,
            content=parsed.content,
            rationale=parsed.rationale,
            tags=list(parsed.tags),
            importance=parsed.importance,
            contradicts_notice=parsed.contradicts_notice,
        )

    async def check_contradiction(self, pair: ContradictionInput) -> ContradictionOutput | None:
        """Decide whether two concepts conflict logically."""
        prompt = (
            _load_prompt("contradiction", _CONTRADICTION_PROMPT_V)
            .replace("{A_TITLE}", pair.concept_a.title)
            .replace("{A_CONTENT}", _one_line(pair.concept_a.content))
            .replace("{B_TITLE}", pair.concept_b.title)
            .replace("{B_CONTENT}", _one_line(pair.concept_b.content))
        )
        raw = await self._chat(prompt, kind="contradiction")
        if raw is None:
            return None
        try:
            parsed = _ContradictionResponse.model_validate_json(raw)
        except ValidationError as exc:
            self._write_debug("contradiction", raw, reason=str(exc))
            log.warning("ollama-contradiction-validate-failed err=%s", exc)
            return None
        return ContradictionOutput(verdict=parsed.verdict, reason=parsed.reason)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _chat(self, prompt: str, *, kind: str) -> str | None:
        """POST to /api/chat; return the assistant message content or None."""
        url = f"{self._base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning(
                "ollama-%s-network-error type=%s err=%s",
                kind,
                type(exc).__name__,
                exc,
            )
            return None
        if response.status_code >= 400:
            log.warning(
                "ollama-%s-http-error status=%d body=%s",
                kind,
                response.status_code,
                response.text[:500],
            )
            self._write_debug(kind, response.text, reason=f"http-{response.status_code}")
            return None
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            self._write_debug(kind, response.text, reason=f"envelope-not-json: {exc}")
            log.warning("ollama-%s-envelope-not-json err=%s", kind, exc)
            return None

        content = _extract_message_content(body)
        if not content:
            self._write_debug(kind, response.text, reason="empty-message-content")
            log.warning("ollama-%s-empty-content body=%s", kind, str(body)[:500])
            return None
        return content

    def _write_debug(self, kind: str, text: str, *, reason: str) -> None:
        if self._debug_dir is None:
            return
        path = self._debug_dir / f"{int(time.time())}-{kind}.json"
        try:
            path.write_text(
                json.dumps({"reason": reason, "raw": text}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # debug-dir disk-full etc — don't poison the run
            log.warning("ollama-debug-write-failed path=%s err=%s", path, exc)


def _extract_message_content(body: Any) -> str | None:
    """Pull the assistant message content out of an Ollama /api/chat body.

    Tolerant: accepts either the new ``{"message": {"content": ...}}``
    shape or the older ``{"response": ...}``.
    """
    if not isinstance(body, dict):
        return None
    msg = body.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
    resp = body.get("response")
    if isinstance(resp, str) and resp.strip():
        return resp
    return None


def _one_line(text: str, *, max_chars: int = 1500) -> str:
    """Collapse newlines + cap length so the prompt stays compact."""
    flat = " ".join(text.split())
    if len(flat) > max_chars:
        return flat[:max_chars] + " …"
    return flat


__all__ = ["HttpxOllamaClient"]
