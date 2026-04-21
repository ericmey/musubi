"""httpx-backed client satisfying :class:`ReflectionLLM`.

Contract: returns ``None`` on any outage so the reflection sweep can
substitute the documented skip notice rather than failing the whole
digest. Matches the maturation/synthesis pattern — see
:mod:`musubi.llm.ollama` for the sibling clients.

Why a separate file (rather than another method on
:class:`HttpxOllamaClient`): the reflection Protocol has a simpler
surface (no pydantic-validated response — the LLM output *is* the
markdown) and keeping the two classes independent lets each evolve
without forcing the other's callers to re-verify.
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0
_PROMPT_VERSION = "v1"


def _load_prompt(name: str, version: str) -> str:
    resource = files("musubi.llm.prompts").joinpath(name).joinpath(f"{version}.txt")
    return resource.read_text(encoding="utf-8")


def _render_prompt(items: list[dict[str, object]]) -> str:
    tpl = _load_prompt("reflection", _PROMPT_VERSION)
    if not items:
        rendered = "  (no items)"
    else:
        rendered = "\n".join(
            f"- id={it.get('id')} topic={it.get('topic', '')}\n  summary: {it.get('summary', '')}"
            for it in items
        )
    return tpl.replace("{ITEMS}", rendered)


def _extract_message_content(body: Any) -> str | None:
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


class HttpxReflectionClient:
    """Production satisfier of the ``ReflectionLLM`` Protocol."""

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

    async def summarize_patterns(self, items: list[dict[str, object]]) -> str | None:
        """Ask the LLM to summarise the day's episodics into themes.

        Returns ``None`` on any outage — callers (see
        :func:`musubi.lifecycle.reflection.run_reflection_sweep`) treat
        ``None`` as "substitute the skip notice" rather than failing
        the whole reflection run.

        Empty ``items`` short-circuits to ``""`` without an HTTP call
        (avoids a pointless round trip and matches the "no dominant
        theme" case the prompt documents).
        """
        if not items:
            return ""

        prompt = _render_prompt(items)
        url = f"{self._base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning(
                "reflection-network-error type=%s err=%s",
                type(exc).__name__,
                exc,
            )
            return None
        if response.status_code >= 400:
            log.warning(
                "reflection-http-error status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            return None
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            log.warning("reflection-envelope-not-json err=%s", exc)
            return None

        content = _extract_message_content(body)
        if content is None:
            log.warning("reflection-empty-content body=%s", str(body)[:200])
            return None
        return content


__all__ = ["HttpxReflectionClient"]
