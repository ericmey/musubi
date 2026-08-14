"""httpx-backed Ollama client satisfying the maturation sweep's Protocol.

The :class:`musubi.lifecycle.maturation.OllamaClient` Protocol has two
methods — ``score_importance`` and ``infer_topics`` — both of which
must return ``None`` when Ollama is unreachable so the sweep can fall
back to captured values (see
[[06-ingestion/maturation#Failure modes]]). This module provides the
real production implementation.

Design contract:

- Every call posts to ``{base_url}/api/chat`` with a Pydantic-derived
  JSON Schema as ``format`` and ``temperature: 0`` for determinism.
  Ollama's structured-output decoder (≥0.5.0) constrains generation to
  the schema, preventing missing-field validation failures that
  free-form ``format: "json"`` permits.
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
from musubi.llm.prompt_boundary import ChatMessage, build_untrusted_data_messages
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


# Cache JSON Schemas at import time — they're used on every Ollama call
# as the ``format`` value, which switches Ollama into structured-output
# mode and constrains decoding to required fields.
_IMPORTANCE_SCHEMA: dict[str, Any] = _ImportanceResponse.model_json_schema()
_TOPICS_SCHEMA: dict[str, Any] = _TopicResponse.model_json_schema()
_SYNTHESIS_SCHEMA: dict[str, Any] = _SynthesisResponse.model_json_schema()
_CONTRADICTION_SCHEMA: dict[str, Any] = _ContradictionResponse.model_json_schema()


def _load_prompt(name: str, version: str) -> str:
    """Load a frozen prompt file from ``musubi.llm.prompts.<name>``."""
    resource = files("musubi.llm.prompts").joinpath(name).joinpath(f"{version}.txt")
    return resource.read_text(encoding="utf-8")


class HttpxOllamaClient:
    """Production :class:`OllamaClient` backed by ``httpx.AsyncClient``.

    Speaks either wire protocol behind one surface (ADR 0043):

    - ``api="ollama"`` (default): native ``/api/chat`` with Ollama's
      ``format``-schema structured output — the original co-located
      qwen path. Unchanged behavior.
    - ``api="openai"``: ``/v1/chat/completions`` with
      ``response_format: json_schema (strict)`` — any OpenAI-compatible
      endpoint (LiteLLM, vLLM, llama.cpp server). This is how the
      lifecycle worker reaches the 35B house lane, whose structured
      output actually survives the synthesis schema; the co-located
      4B measured 0% on it (see the ADR for the full gradient).

    Everything above the transport — prompt loading, payload shaping,
    pydantic validation, debug dumps, the return-None-on-failure
    contract — is identical across both, so callers cannot tell which
    wire they are on. That is the point: model quality becomes a
    deployment decision, not a code path.

    Parameters
    ----------
    base_url:
        Base URL of the endpoint. For ``api="openai"``, with or without
        a trailing ``/v1`` (normalized).
    model:
        Model id as the endpoint knows it (``qwen3:4b`` for local
        Ollama; ``house/backup`` behind LiteLLM).
    timeout_s:
        Per-request timeout. Defaults to 120s — leave headroom for a
        cold batch on either lane.
    debug_dir:
        Optional directory to dump raw responses on parse/validation
        failure. Callers supply ``/var/lib/musubi/maturation-debug``
        in production.
    api:
        Wire protocol, ``"ollama"`` or ``"openai"``.
    api_key:
        Optional bearer for the endpoint (LiteLLM). Never logged.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        debug_dir: Path | None = None,
        api: str = "ollama",
        api_key: str | None = None,
    ) -> None:
        if api not in ("ollama", "openai"):
            raise ValueError(f"unsupported llm api {api!r} (expected 'ollama' or 'openai')")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._debug_dir = debug_dir
        self._api = api
        self._api_key = api_key
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

        payload = {
            "items": [
                {
                    "id": str(it.object_id),
                    "captured": it.captured_importance,
                    "content": _one_line(it.content),
                }
                for it in items
            ]
        }
        prompt_instructions = _load_prompt("importance", _IMPORTANCE_PROMPT_V)
        messages = build_untrusted_data_messages(prompt_instructions, payload)  # type: ignore[arg-type]

        raw = await self._chat(messages, kind="importance", schema=_IMPORTANCE_SCHEMA)
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

        payload = {
            "items": [
                {
                    "id": str(it.object_id),
                    "existing": it.existing_tags,
                    "content": _one_line(it.content),
                }
                for it in items
            ]
        }
        prompt_instructions = _load_prompt("topics", _TOPICS_PROMPT_V)
        messages = build_untrusted_data_messages(prompt_instructions, payload)  # type: ignore[arg-type]

        raw = await self._chat(messages, kind="topics", schema=_TOPICS_SCHEMA)
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
        payload = {
            "items": [
                {
                    "id": str(m.object_id),
                    "importance": m.importance,
                    "tags": m.tags,
                    "content": _one_line(m.content),
                }
                for m in cluster.memories
            ]
        }
        prompt_instructions = _load_prompt("synthesis", _SYNTHESIS_PROMPT_V)
        messages = build_untrusted_data_messages(prompt_instructions, payload)  # type: ignore[arg-type]
        raw = await self._chat(messages, kind="synthesis", schema=_SYNTHESIS_SCHEMA)
        if raw is None:
            return None
        try:
            parsed = _SynthesisResponse.model_validate_json(raw)
        except ValidationError as exc:
            self._write_debug("synthesis", raw, reason=str(exc))
            log.warning("ollama-synthesis-validate-failed err=%s", exc)
            return None
        # SynthesizedConcept enforces `synthesis_rationale: min_length=1`,
        # but small LLMs occasionally return an empty `rationale` field
        # (even though the schema asks for one). Substitute a generic-but-
        # valid fallback so a single weak response doesn't crash the
        # synthesis sweep for the entire identity family.
        rationale = parsed.rationale or (
            f"Auto-synthesized from {len(cluster.memories)} matured episodics "
            f"clustering on shared tags / dense similarity. Rationale not "
            f"provided by the LLM."
        )
        return SynthesisOutput(
            title=parsed.title,
            content=parsed.content,
            rationale=rationale,
            tags=list(parsed.tags),
            importance=parsed.importance,
            contradicts_notice=parsed.contradicts_notice,
        )

    async def check_contradiction(self, pair: ContradictionInput) -> ContradictionOutput | None:
        """Decide whether two concepts conflict logically."""
        payload = {
            "concept_a": {
                "title": pair.concept_a.title,
                "content": _one_line(pair.concept_a.content),
            },
            "concept_b": {
                "title": pair.concept_b.title,
                "content": _one_line(pair.concept_b.content),
            },
        }
        prompt_instructions = _load_prompt("contradiction", _CONTRADICTION_PROMPT_V)
        messages = build_untrusted_data_messages(prompt_instructions, payload)  # type: ignore[arg-type]
        raw = await self._chat(messages, kind="contradiction", schema=_CONTRADICTION_SCHEMA)
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

    async def _chat(
        self,
        messages: list[ChatMessage],
        *,
        kind: str,
        schema: dict[str, Any] | None = None,
    ) -> str | None:
        """POST one chat turn on the configured wire; return the assistant
        message content or None.

        ``api="ollama"``: ``/api/chat`` with the schema as the ``format``
        value (Ollama structured-output mode, ≥0.5.0), or free-form JSON
        mode when ``schema`` is None.

        ``api="openai"``: ``/v1/chat/completions`` with
        ``response_format: {type: json_schema, strict: true}``, or
        ``{type: json_object}`` when ``schema`` is None. If the backend
        only best-efforts the constraint, the existing validate-or-None
        contract downstream absorbs it (the caller skips and retries
        next sweep — made non-fatal in #684).
        """
        headers: dict[str, str] = {}
        if self._api == "openai":
            base = self._base_url
            url = (
                f"{base}/chat/completions"
                if base.endswith("/v1")
                else f"{base}/v1/chat/completions"
            )
            response_format: dict[str, Any] = (
                {
                    "type": "json_schema",
                    "json_schema": {"name": f"musubi_{kind}", "schema": schema, "strict": True},
                }
                if schema is not None
                else {"type": "json_object"}
            )
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "temperature": 0,
                "response_format": response_format,
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            url = f"{self._base_url}/api/chat"
            payload = {
                "model": self._model,
                "messages": messages,
                "stream": False,
                "format": schema if schema is not None else "json",
                "options": {"temperature": 0},
            }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
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

        content = (
            _extract_openai_content(body)
            if self._api == "openai"
            else _extract_message_content(body)
        )
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


def _extract_openai_content(body: Any) -> str | None:
    """Pull the assistant content out of an OpenAI-format chat completion."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


def _one_line(text: str, *, max_chars: int = 1500) -> str:
    """Collapse newlines + cap length so the prompt stays compact."""
    flat = " ".join(text.split())
    if len(flat) > max_chars:
        return flat[:max_chars] + " …"
    return flat


__all__ = ["HttpxOllamaClient"]
