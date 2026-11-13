"""LLM access for Card Connect.

Every model call goes through the thebooleanjulian-llm gateway
(https://llm.thebooleanjulian.dev): the bot holds one service key and names a
use case; the gateway picks the provider and model and holds the provider
keys. Use cases are registered for the `card-connect` service in the gateway's
src/routes.js.
"""

import base64
import json
import os
import re

import httpx

GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "https://llm.thebooleanjulian.dev").rstrip("/")

# The gateway may walk a fallback chain of providers, each with its own timeout.
TIMEOUT_SECONDS = 150

USE_CASE_EXTRACT = "card-extract"
USE_CASE_FOLLOWUP = "card-followup"
USE_CASE_INTEL = "card-intel"

# Tests swap this for an httpx.MockTransport.
_transport = None


class LLMError(RuntimeError):
    """The gateway could not be reached or refused the request. Safe to log."""


async def _chat(use_case: str, payload: dict) -> str:
    key = os.environ.get("LLM_GATEWAY_KEY", "").strip()
    if not key:
        raise LLMError("LLM_GATEWAY_KEY is not set")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=_transport) as client:
            response = await client.post(
                f"{GATEWAY_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "X-TBJ-Use-Case": use_case,
                    "User-Agent": "card-connect",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise LLMError(f"gateway unreachable ({type(exc).__name__})") from exc

    if response.status_code != 200:
        try:
            error = response.json()["error"]
            detail = f"{error.get('code')}: {error.get('message')}"
        except Exception:
            detail = response.text[:200]
        raise LLMError(f"gateway returned {response.status_code} for {use_case} ({detail})")

    try:
        return (response.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError(f"gateway sent an unexpected reply for {use_case}") from exc


async def complete(use_case: str, system: str, user: str, max_tokens: int = 400, json_mode: bool = False) -> str:
    """Plain text-in, text-out chat completion."""
    payload = {
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return await _chat(use_case, payload)


async def vision(use_case: str, system: str, prompt: str, image_bytes: bytes, media_type: str, max_tokens: int = 1024) -> str:
    """One image plus a text prompt, for a use case that allows images."""
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    return await _chat(
        use_case,
        {
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    ],
                },
            ],
        },
    )


def parse_json(text: str) -> dict:
    """Parse a model's JSON reply, tolerating markdown fences and stray prose.

    Raises ValueError if no JSON object can be found.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data
