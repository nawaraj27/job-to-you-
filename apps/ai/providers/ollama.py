"""
Ollama — local models (SRS §5.11).

Costs nothing, sends nothing off the machine, and is therefore the provider a
developer gets by default and the last link in most fallback chains. It has no
JSON-schema mode, so the schema is asked for by instruction and the runner's
validation does the rest.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import EmbeddingResult, ProviderFailure, ProviderTimeout, Request, Result

logger = logging.getLogger(__name__)


class OllamaAdapter:
    key = "ollama"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        import os

        self.base_url = base_url.rstrip("/")
        # How long Ollama keeps the model in memory after a call. Its default is
        # five minutes, after which the next candidate pays for loading 4.7 GB
        # from disk. Interviews arrive in bursts with gaps longer than that.
        self.keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
        # Ollama's own default context is 4,096 tokens, and a prompt longer
        # than that is truncated from the front without an error. A four-page
        # CV plus the instructions and the answer is past it, so the model was
        # reviewing the second half of the CV (docs/benchmarks/
        # CV-Analysis-Timing.md). One fixed size, not one per request: a change
        # of `num_ctx` makes Ollama reload the model, which costs more than it
        # saves.
        self.num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

    def complete(self, request: Request, *, model: str, timeout: int) -> Result:
        prompt = request.user
        if request.json_schema is not None:
            # Ollama constrains decoding to the schema itself (structured
            # outputs). The schema used to be pasted into the prompt as text:
            # 50-150 extra input tokens per call on a CPU that reads about 30 a
            # second, placed after the variable part of the prompt, so it could
            # never be served from the prompt cache. Measured on the follow-up
            # prompt: 448 -> 395 input tokens, same answer. The runner still
            # validates what comes back.
            prompt = f"{prompt}\n\nAnswer with JSON only."

        payload: dict[str, Any] = {
            "model": model,
            "system": request.system,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
                "num_ctx": self.num_ctx,
            },
        }
        if request.json_schema is not None:
            payload["format"] = request.json_schema

        body = self._post("/api/generate", payload, timeout=timeout)

        return Result(
            text=body.get("response") or "",
            tokens_in=int(body.get("prompt_eval_count") or 0),
            tokens_out=int(body.get("eval_count") or 0),
            raw={"done_reason": body.get("done_reason")},
        )

    def embed(self, text: str, *, model: str, timeout: int) -> EmbeddingResult:
        """One vector, locally (P13).

        The reason this exists: embedding every job and profile is the one AI
        workload whose volume is proportional to the corpus rather than to what
        people click, and paying per token for it is the cost mistake §9.6
        warns about. A local embedding model on the same machine costs nothing
        and needs no GPU worth the name.

        Ollama reports no token counts for embeddings, so `tokens_in` is zero
        and the run's cost is genuinely zero — not zero because nobody counted.
        """
        body = self._post("/api/embeddings", {"model": model, "prompt": text}, timeout=timeout)

        try:
            vector = [float(value) for value in body["embedding"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderFailure(f"Unreadable embedding response: {exc}") from exc

        if not vector:
            raise ProviderFailure("The provider returned an empty embedding.")

        return EmbeddingResult(vector=vector, tokens_in=0, raw={"model": model})

    # --- transport --------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """POST, classify the outcome, and return the decoded body.

        Ollama has no rate limit and no refusal envelope: it is either up or it
        is not, so every error here is a `ProviderFailure` — retryable, which
        for a local process usually means "it is still loading the model".
        """
        import requests

        try:
            response = requests.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
        except requests.Timeout as exc:
            raise ProviderTimeout(str(exc)) from exc
        except requests.RequestException as exc:
            raise ProviderFailure(str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderFailure(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderFailure(f"Unreadable response: {exc}") from exc
        return body
