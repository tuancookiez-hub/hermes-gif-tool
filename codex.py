"""The image-generation call behind ``gif_create``: a sprite sheet from the Codex image endpoint.

The request goes through ``credentialed_provider_request``: Hermes attaches the user's ChatGPT/Codex
sign-in and the plugin never sees the token. The plugin declares ``requires_auth: [openai-codex]``.

What the backend does and does not honour (measured 2026-11-05, Hermes 0.22.7):
- ``model`` is ignored (any id, real or made up, returns the same result) and no response field names
  the model, so GIF Tool sends none and reports "the image model your plan serves".
- ``size`` is ignored too (asked 1024x1024, got 1536x1024 / 1254x1254), so slicing never assumes a size.
- ``output_format: gif`` is refused ("GIF output is not enabled"), hence the sprite-sheet approach in ``gif_create``.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

ENDPOINT = "https://chatgpt.com/backend-api/codex/images/generations"
QUALITIES = ("low", "medium", "high")
TIMEOUT_S = 240.0
PLUGIN_VERSION = "1.0.0"


class CodexImageError(Exception):
    """A failure the user can act on; ``str()`` is the message to show."""


def _headers() -> dict[str, str]:
    # Identify as this plugin only; Hermes adds the sign-in and account headers itself.
    return {
        "User-Agent": f"gif-tool/{PLUGIN_VERSION} (Hermes Agent plugin)",
        "x-codex-image-turn-id": str(uuid.uuid4()),
    }


def _error_detail(body: bytes) -> str:
    import json

    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error") or data
            if isinstance(detail, dict):
                detail = detail.get("message") or detail
            text = str(detail)
    except ValueError:
        pass
    return text[:300] or "no details"


def generate_sheet(prompt: str, quality: str = "medium") -> tuple[bytes, dict[str, Any]]:
    """POST one generation; return ``(png_bytes, meta)``. Raises :class:`CodexImageError`."""
    from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request

    if quality not in QUALITIES:
        raise CodexImageError(f"quality must be one of {', '.join(QUALITIES)}")
    body = {"prompt": prompt, "n": 1, "quality": quality, "size": "1024x1024"}
    started = time.monotonic()
    try:
        resp = credentialed_provider_request(
            "openai-codex", "POST", ENDPOINT, json=body, headers=_headers(), timeout=TIMEOUT_S)
    except ProviderNotSignedIn as exc:
        raise CodexImageError(str(exc)) from None
    except PermissionError as exc:
        raise CodexImageError(f"Hermes refused the Codex request: {exc}") from None
    except Exception as exc:  # noqa: BLE001 -- httpx errors; name the class, never echo the request
        raise CodexImageError(f"Could not reach the Codex image service ({type(exc).__name__}). "
                              "Check your connection and try again.") from None
    seconds = round(time.monotonic() - started, 1)
    if resp.status == 401:
        raise CodexImageError("The Codex sign-in was rejected. Sign in to OpenAI Codex again in Hermes "
                              "(Providers in Desktop, or `hermes auth add openai-codex`).")
    if resp.status == 429:
        raise CodexImageError("Your ChatGPT plan's image limit is used up for now. Try again later. "
                              f"({_error_detail(resp.body)})")
    if resp.status >= 400:
        raise CodexImageError(f"The Codex image service returned HTTP {resp.status}: {_error_detail(resp.body)}")
    try:
        payload = resp.json()
        item = (payload.get("data") or [])[0]
        raw = base64.b64decode(item["b64_json"])
    except Exception:  # noqa: BLE001
        raise CodexImageError("The Codex image service returned no image.") from None
    usage = payload.get("usage") or {}
    meta = {
        "seconds": seconds,
        "quality_requested": quality,
        "quality_reported": payload.get("quality"),
        "size_reported": payload.get("size"),
        "image_tokens": (usage.get("output_tokens_details") or {}).get("image_tokens"),
        "request_id": resp.headers.get("x-codex-imagegen-request-id"),
    }
    return raw, meta
