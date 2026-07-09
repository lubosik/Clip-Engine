"""
meme/image_client.py — image generation via OpenAI-compatible chat/completions.

Posts to {LLM_BASE_URL}/v1/chat/completions with modalities=["image","text"].
Reference images are included as image_url content blocks so the model can
use them for visual style guidance.

Designed for unit testability: pass a custom httpx transport to ImageClient
to mock network calls without patching httpx globally.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Media-type map for reference images sent as data URLs in the request
_EXT_TO_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _media_type_for_ext(ext: str) -> str:
    return _EXT_TO_MIME.get(ext.lower().lstrip("."), "image/jpeg")


def _base_url_for_image_gen() -> str:
    """Resolve the API base URL for image generation."""
    from core.settings import get_settings

    s = get_settings()
    base = s.llm_base_url
    if base is None and s.llm_api_key and s.llm_api_key.startswith("sk-or-"):
        base = "https://openrouter.ai/api"
    if base is None:
        base = "https://api.openai.com"
    return base.rstrip("/")


def _parse_image_from_response(data: dict) -> bytes:
    """
    Extract raw image bytes from an OpenAI-compatible chat/completions response.

    Handles multiple content block formats defensively:
      - content is a list with type='image_url' blocks (OpenAI / OpenRouter)
      - content is a list with type='image' blocks
      - content is a bare base64 string or data URL

    Raises:
        ValueError: if no image can be extracted.
    """
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        raise ValueError(
            f"No choices in image generation response: {str(data)[:300]}"
        )

    message = choices[0].get("message", {})
    content = message.get("content")

    if content is None:
        raise ValueError(
            f"No content in response message: {str(message)[:300]}"
        )

    # --- Case 1: content is a plain string (bare base64 or data URL) ---
    if isinstance(content, str):
        raw = content.strip()
        if raw.startswith("data:"):
            # data:image/png;base64,<data>
            if "," in raw:
                raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw)
        except Exception as exc:
            raise ValueError(
                f"Content string is not valid base64: {str(exc)}"
            ) from exc

    # --- Case 2: content is a list of blocks ---
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "")

            # OpenAI-style: {"type": "image_url", "image_url": {"url": "data:..."}}
            if block_type == "image_url":
                url = block.get("image_url", {})
                if isinstance(url, dict):
                    url = url.get("url", "")
                if not isinstance(url, str):
                    continue
                if url.startswith("data:") and "," in url:
                    url = url.split(",", 1)[1]
                try:
                    return base64.b64decode(url)
                except Exception:
                    continue

            # Some providers: {"type": "image", "image": {"url": "data:..."}}
            if block_type == "image":
                img_field = block.get("image", {})
                if isinstance(img_field, dict):
                    url = img_field.get("url", "")
                elif isinstance(img_field, str):
                    url = img_field
                else:
                    url = ""
                if url.startswith("data:") and "," in url:
                    url = url.split(",", 1)[1]
                if url:
                    try:
                        return base64.b64decode(url)
                    except Exception:
                        continue

    raise ValueError(
        f"Could not extract image from response. "
        f"choices[0].message.content={str(content)[:200]}"
    )


class ImageClient:
    """
    HTTP client for image generation via chat/completions.

    Args:
        base_url:   API base URL (no trailing slash).  Endpoint used:
                    {base_url}/v1/chat/completions.
        api_key:    Bearer token for authorization.
        model:      Model identifier (e.g. 'openai/gpt-5.4-image-2').
        transport:  Optional httpx transport for unit testing.
                    Pass httpx.MockTransport(handler) to intercept requests.
        timeout:    Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: Any = None,
        timeout: float = 120.0,
    ) -> None:
        self._endpoint = base_url.rstrip("/") + "/v1/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport

    # ------------------------------------------------------------------
    # Reference image helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ref_image_block(image_bytes: bytes, media_type: str = "image/png") -> dict:
        """Build an image_url content block from raw bytes (data URL)."""
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        }

    # ------------------------------------------------------------------
    # Public generate API
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        reference_images: list[tuple[bytes, str]],
    ) -> bytes:
        """
        Generate an image from *prompt* using up to 3 reference images for
        visual style guidance.

        Args:
            prompt:           Text description of the image to create.
            reference_images: List of (raw_bytes, media_type) pairs.
                              Passed as image_url content blocks.

        Returns:
            Raw PNG/JPEG bytes of the generated image.

        Raises:
            httpx.HTTPStatusError: on non-2xx HTTP response.
            ValueError:            if the response contains no parseable image.
        """
        # Build content blocks: reference images first, then text prompt
        content: list[dict] = []
        for raw_bytes, media_type in reference_images[:3]:  # cap at 3
            content.append(self._ref_image_block(raw_bytes, media_type))
        content.append({"type": "text", "text": prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": content}],
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        client_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        log.info(
            "Requesting image generation",
            extra={
                "model": self._model,
                "ref_images": len(reference_images),
                "prompt_preview": prompt[:80],
            },
        )

        with httpx.Client(**client_kwargs) as http:
            response = http.post(self._endpoint, json=payload)

        response.raise_for_status()
        data = response.json()

        log.debug("Image generation response keys=%s", list(data.keys()))
        image_bytes = _parse_image_from_response(data)
        log.info(
            "Image generation complete",
            extra={"model": self._model, "bytes": len(image_bytes)},
        )
        return image_bytes


def build_image_client(model: str) -> ImageClient:
    """
    Convenience constructor: builds ImageClient from settings.

    Raises RuntimeError if LLM_API_KEY or LLM_MODEL are missing.
    """
    from core.settings import get_settings

    s = get_settings()
    api_key, _ = s.require_llm()
    base_url = _base_url_for_image_gen()
    return ImageClient(base_url=base_url, api_key=api_key, model=model)
