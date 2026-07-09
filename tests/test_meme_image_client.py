"""
tests/test_meme_image_client.py — unit tests for meme/image_client.py

Tests response parsing (pure) and the ImageClient.generate() method via
a mocked httpx transport.  No real network calls.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SAMPLE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100   # fake PNG header
_SAMPLE_B64 = base64.b64encode(_SAMPLE_PNG).decode("utf-8")
_SAMPLE_DATA_URL = f"data:image/png;base64,{_SAMPLE_B64}"


def _make_openai_response(content_blocks: list) -> dict:
    """Wrap content blocks in a standard OpenAI-style chat/completions response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content_blocks,
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1. _parse_image_from_response — pure function
# ---------------------------------------------------------------------------

class TestParseImageFromResponse:
    """_parse_image_from_response handles multiple response formats."""

    def _parse(self, data: dict) -> bytes:
        from meme.image_client import _parse_image_from_response
        return _parse_image_from_response(data)

    def test_image_url_block_with_data_url(self):
        """Standard OpenAI image_url block → correct bytes."""
        response = _make_openai_response([
            {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
        ])
        result = self._parse(response)
        assert result == _SAMPLE_PNG

    def test_image_block_with_data_url(self):
        """Alternative 'image' block format → correct bytes."""
        response = _make_openai_response([
            {"type": "image", "image": {"url": _SAMPLE_DATA_URL}}
        ])
        result = self._parse(response)
        assert result == _SAMPLE_PNG

    def test_string_content_data_url(self):
        """Content is a plain data URL string → correct bytes."""
        response = {
            "choices": [
                {"message": {"content": _SAMPLE_DATA_URL}}
            ]
        }
        result = self._parse(response)
        assert result == _SAMPLE_PNG

    def test_string_content_bare_base64(self):
        """Content is a bare base64 string (no data: prefix) → correct bytes."""
        response = {
            "choices": [
                {"message": {"content": _SAMPLE_B64}}
            ]
        }
        result = self._parse(response)
        assert result == _SAMPLE_PNG

    def test_no_choices_raises(self):
        from meme.image_client import _parse_image_from_response
        with pytest.raises(ValueError, match="No choices"):
            _parse_image_from_response({})

    def test_empty_choices_list_raises(self):
        from meme.image_client import _parse_image_from_response
        with pytest.raises(ValueError, match="No choices"):
            _parse_image_from_response({"choices": []})

    def test_no_content_raises(self):
        from meme.image_client import _parse_image_from_response
        response = {"choices": [{"message": {}}]}
        with pytest.raises(ValueError, match="No content"):
            _parse_image_from_response(response)

    def test_non_image_content_blocks_raises(self):
        """Content blocks with no image type → ValueError."""
        response = _make_openai_response([
            {"type": "text", "text": "Here is your image: ..."}
        ])
        with pytest.raises(ValueError, match="Could not extract"):
            self._parse(response)

    def test_mixed_blocks_extracts_image(self):
        """Image block present alongside a text block → extracts the image."""
        response = _make_openai_response([
            {"type": "text", "text": "Generated image:"},
            {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}},
        ])
        result = self._parse(response)
        assert result == _SAMPLE_PNG


# ---------------------------------------------------------------------------
# 2. ImageClient._ref_image_block — static helper
# ---------------------------------------------------------------------------

class TestRefImageBlock:
    def test_block_type_is_image_url(self):
        from meme.image_client import ImageClient
        block = ImageClient._ref_image_block(b"\x00\x01", "image/png")
        assert block["type"] == "image_url"
        assert "image_url" in block

    def test_block_contains_data_url(self):
        from meme.image_client import ImageClient
        raw = b"\x89PNG"
        block = ImageClient._ref_image_block(raw, "image/png")
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        decoded = base64.b64decode(url.split(",")[1])
        assert decoded == raw


# ---------------------------------------------------------------------------
# 3. ImageClient.generate() — mocked httpx transport
# ---------------------------------------------------------------------------

def _make_mock_transport(response_body: dict, status_code: int = 200):
    """Return an httpx.MockTransport that responds with *response_body*."""
    response_bytes = json.dumps(response_body).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            headers={"Content-Type": "application/json"},
            content=response_bytes,
        )

    return httpx.MockTransport(handler)


class TestImageClientGenerate:
    """ImageClient.generate() with a mocked httpx transport."""

    def _make_client(self, transport) -> object:
        from meme.image_client import ImageClient
        return ImageClient(
            base_url="https://openrouter.test/api",
            api_key="sk-test-key",
            model="test/image-model",
            transport=transport,
        )

    def test_generate_returns_correct_bytes(self):
        """Happy path: valid image in response → raw PNG bytes returned."""
        response = _make_openai_response([
            {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
        ])
        transport = _make_mock_transport(response)
        client = self._make_client(transport)

        result = client.generate("A bold fitness meme", [])
        assert result == _SAMPLE_PNG

    def test_reference_images_included_in_request(self):
        """Reference images are present as image_url blocks in the request body."""
        captured_body: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured_body.append(body)
            response = _make_openai_response([
                {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
            ])
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(response).encode(),
            )

        from meme.image_client import ImageClient

        client = ImageClient(
            base_url="https://openrouter.test/api",
            api_key="sk-test",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )

        ref = (b"\x89PNG" + b"\x00" * 10, "image/png")
        client.generate("Test prompt", [ref, ref])

        assert captured_body, "Handler was not called"
        messages = captured_body[0].get("messages", [])
        assert messages, "No messages in request"
        user_content = messages[0]["content"]

        image_blocks = [b for b in user_content if b.get("type") == "image_url"]
        assert len(image_blocks) == 2, "Expected 2 reference image blocks"

    def test_reference_images_capped_at_3(self):
        """No more than 3 reference images are sent regardless of how many are given."""
        captured_body: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(_make_openai_response([
                    {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
                ])).encode(),
            )

        from meme.image_client import ImageClient

        client = ImageClient(
            base_url="https://openrouter.test/api",
            api_key="sk-test",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )

        refs = [(b"\x89PNG" + b"\x00" * i, "image/png") for i in range(5)]
        client.generate("Test", refs)

        user_content = captured_body[0]["messages"][0]["content"]
        image_blocks = [b for b in user_content if b.get("type") == "image_url"]
        assert len(image_blocks) == 3, "Should be capped at 3 reference images"

    def test_model_and_modalities_in_request(self):
        """Request must include model name and modalities=['image','text']."""
        captured_body: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(_make_openai_response([
                    {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
                ])).encode(),
            )

        from meme.image_client import ImageClient

        client = ImageClient(
            base_url="https://openrouter.test/api",
            api_key="sk-test",
            model="openai/gpt-5.4-image-2",
            transport=httpx.MockTransport(handler),
        )

        client.generate("Test prompt", [])

        body = captured_body[0]
        assert body["model"] == "openai/gpt-5.4-image-2"
        assert "image" in body.get("modalities", [])
        assert "text" in body.get("modalities", [])

    def test_http_error_propagates(self):
        """Non-2xx response → httpx.HTTPStatusError raised."""
        transport = _make_mock_transport({"error": "bad request"}, status_code=400)
        client = self._make_client(transport)

        with pytest.raises(httpx.HTTPStatusError):
            client.generate("Test", [])

    def test_authorization_header_sent(self):
        """Bearer token is present in the Authorization header."""
        headers_seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            headers_seen.append(dict(request.headers))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(_make_openai_response([
                    {"type": "image_url", "image_url": {"url": _SAMPLE_DATA_URL}}
                ])).encode(),
            )

        from meme.image_client import ImageClient

        client = ImageClient(
            base_url="https://openrouter.test/api",
            api_key="sk-my-secret-key",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        client.generate("Prompt", [])

        auth = headers_seen[0].get("authorization", "")
        assert auth == "Bearer sk-my-secret-key"
