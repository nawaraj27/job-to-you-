"""
OpenRouter (SRS §5.11).

One HTTP call, one normalised result. Everything OpenRouter-specific — the
chat-completions envelope, where usage counts live, which status codes mean
"try again" — stops here.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    EmbeddingResult,
    ProviderFailure,
    ProviderRefused,
    ProviderTimeout,
    Request,
    Result,
)

logger = logging.getLogger(__name__)

#: Status codes worth another attempt. 429 is included because a rate limit is
#: transient by definition; any other 4xx is our request being wrong, and
#: sending it again produces the same answer more expensively.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenRouterAdapter:
    key = "openrouter"

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def complete(self, request: Request, *, model: str, timeout: int) -> Result:

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "schema": request.json_schema,
                    "strict": False,
                },
            }

        body = self._post("/chat/completions", payload, timeout=timeout)

        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise ProviderFailure(f"Unreadable response: {exc}") from exc

        if choice.get("finish_reason") == "content_filter":
            raise ProviderRefused("The model declined to answer.")

        usage = body.get("usage") or {}
        return Result(
            text=text,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            raw={"id": body.get("id"), "finish_reason": choice.get("finish_reason")},
        )

    def embed(self, text: str, *, model: str, timeout: int) -> EmbeddingResult:
        """One vector for one string (P13).

        Same transport, same error classification, different endpoint. Sharing
        `_post` is the point: an embedding outage and a completion outage are
        the same outage, and two copies of the status-code table would
        eventually disagree about whether 429 is retryable.
        """
        body = self._post("/embeddings", {"model": model, "input": text}, timeout=timeout)

        try:
            vector = [float(value) for value in body["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFailure(f"Unreadable embedding response: {exc}") from exc

        if not vector:
            # An empty vector is not a vector. Passing it on would store a row
            # that matches everything equally, which reads as a working feature.
            raise ProviderFailure("The provider returned an empty embedding.")

        usage = body.get("usage") or {}
        return EmbeddingResult(
            vector=vector,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            raw={"model": body.get("model")},
        )

    # --- transport --------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """POST, classify the outcome, and return the decoded body.

        The three exceptions this raises are the whole of what the runner
        classifies on, so every endpoint on this provider must go through here.
        """
        import requests

        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise ProviderTimeout(str(exc)) from exc
        except requests.RequestException as exc:
            raise ProviderFailure(str(exc)) from exc

        if response.status_code in RETRYABLE_STATUS:
            raise ProviderFailure(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise ProviderRefused(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderFailure(f"Unreadable response: {exc}") from exc
        return body
