"""httpx-backed client satisfying :class:`PromotionLLM`.

Contract differences from :class:`musubi.llm.HttpxOllamaClient`:

- **Failure modes raise** rather than return ``None``. The promotion
  sweep wraps each render in its own ``try/except`` and records a
  ``promotion_rejection`` with the error message — silent ``None`` at
  this layer would be consumed as "rendering succeeded with empty
  content" which is strictly worse than a loud failure.
- **Validation is in the Protocol's dataclass** (``PromotionRender``
  has its own ``model_validator`` that enforces H2 presence and
  rejects AI-disclaimer strings). On validation failure we re-raise
  :class:`pydantic.ValidationError` as a :class:`ValueError` so the
  sweep's rejection reason string reads cleanly.

A separate class (rather than another method on ``HttpxOllamaClient``)
keeps the two Protocol contracts — outage-returns-None for maturation
/ synthesis vs raise-on-failure for promotion — from leaking into the
same code path.
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from musubi.lifecycle.promotion import PromotionRender

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0  # Qwen on CPU fallback can take 90s+ for long renders
_PROMPT_VERSION = "v1"


class _PromotionResponse(BaseModel):
    """Wire-level shape from the LLM — upgraded to :class:`PromotionRender`
    before return so the sweep's Protocol contract carries the tighter
    validation (H2 required, disclaimer strings rejected).
    """

    body: str = Field(min_length=1)
    wikilinks: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)


def _load_prompt(name: str, version: str) -> str:
    resource = files("musubi.llm.prompts").joinpath(name).joinpath(f"{version}.txt")
    return resource.read_text(encoding="utf-8")


def _render_prompt(*, title: str, content: str, rationale: str, top_memories: list[str]) -> str:
    tpl = _load_prompt("promotion-render", _PROMPT_VERSION)
    memories_block = "\n".join(f"  - {m}" for m in top_memories) if top_memories else "  (none)"
    return (
        tpl.replace("{TITLE}", title)
        .replace("{CONTENT}", content)
        .replace("{RATIONALE}", rationale)
        .replace("{TOP_MEMORIES}", memories_block)
    )


def _extract_message_content(body: Any) -> str | None:
    """Duplicates :func:`musubi.llm.ollama._extract_message_content`
    deliberately — keeps the two clients' failure-mode code paths
    independent. If a third caller appears, promote to a shared home.
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


class HttpxPromotionClient:
    """Production satisfier of the ``PromotionLLM`` Protocol."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    async def render_curated_markdown(
        self,
        title: str,
        content: str,
        rationale: str,
        top_memories: list[str],
    ) -> PromotionRender:
        """Render a synthesized concept as curated markdown."""
        prompt = _render_prompt(
            title=title,
            content=content,
            rationale=rationale,
            top_memories=top_memories,
        )
        raw = await self._chat(prompt)
        try:
            wire = _PromotionResponse.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"promotion-render envelope invalid: {exc}") from exc

        try:
            return PromotionRender(
                body=wire.body,
                wikilinks=wire.wikilinks,
                sections=wire.sections,
            )
        except ValidationError as exc:
            raise ValueError(f"promotion-render body rejected: {exc}") from exc

    async def _chat(self, prompt: str) -> str:
        url = f"{self._base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"promotion-render envelope not JSON: {exc}") from exc

        content = _extract_message_content(body)
        if content is None:
            raise ValueError("promotion-render envelope missing assistant message content")
        return content


__all__ = ["HttpxPromotionClient"]
