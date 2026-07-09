"""
meme/_client.py — shared LLM helpers for the meme engine.

Provides call_text() and call_vision() backed by the anthropic SDK.
Mirrors the routing logic from core/llm.py (sk-or- → OpenRouter) but
is kept inside meme/ so core/llm.py is never modified.

Both functions retry once on empty/unparseable responses.
"""

from __future__ import annotations

import base64
import json
import logging
import re

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client construction (shared)
# ---------------------------------------------------------------------------

def _make_client():
    """Build and return an (anthropic.Anthropic, model_str) tuple from settings."""
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "anthropic SDK is required for the meme engine. "
            "Install with: pip install anthropic"
        ) from exc

    from core.settings import get_settings

    s = get_settings()
    api_key, model = s.require_llm()

    base_url = s.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"

    if base_url:
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        log.debug("Meme LLM via base_url=%s model=%s", base_url, model)
    else:
        client = anthropic.Anthropic(api_key=api_key)
        log.debug("Meme LLM via api.anthropic.com model=%s", model)

    return client, model


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> dict | None:
    """
    Extract the first balanced JSON object from *text*.

    Handles nested braces and strings correctly.  Returns None if no
    valid JSON object is found.
    """
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None

        depth = 0
        in_str = False
        esc = False

        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        pass
                    # Candidate wasn't valid JSON; advance past it
                    pos = i + 1
                    break
        else:
            # Reached end of string without closing the brace
            return None


# ---------------------------------------------------------------------------
# Public LLM call wrappers
# ---------------------------------------------------------------------------

def call_text(prompt: str, *, max_tokens: int = 1024) -> str:
    """
    Make a text-only LLM call and return the raw response string.

    Retries once if the response is empty (mirrors core/llm.py).
    Raises RuntimeError if LLM_API_KEY / LLM_MODEL are missing.
    """
    client, model = _make_client()

    def _call() -> str:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text if msg.content else ""

    resp = _call()
    log.debug("Meme text LLM response length=%d", len(resp))

    if not resp.strip():
        log.warning("Empty meme text LLM response; retrying once")
        resp = _call()

    return resp


def call_vision(
    prompt: str,
    images: list[tuple[bytes, str]],
    *,
    max_tokens: int = 2048,
) -> str:
    """
    Make a vision LLM call using anthropic-SDK image content blocks.

    Args:
        prompt: Text prompt appended after the image blocks.
        images: List of (raw_bytes, media_type) pairs.
                media_type should be 'image/png', 'image/jpeg', etc.
        max_tokens: Token budget for the response.

    Returns:
        Model text response string.

    Retries once on empty response.
    """
    client, model = _make_client()

    content: list[dict] = []
    for raw_bytes, media_type in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(raw_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": prompt})

    def _call() -> str:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        return msg.content[0].text if msg.content else ""

    resp = _call()
    log.debug(
        "Meme vision LLM response length=%d images=%d",
        len(resp),
        len(images),
    )

    if not resp.strip():
        log.warning("Empty meme vision LLM response; retrying once")
        resp = _call()

    return resp
