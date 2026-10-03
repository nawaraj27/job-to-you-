"""
A deterministic adapter, for tests and the evaluation harness.

Not a mock scattered through test files. A real adapter implementing the real
protocol, registered like any other provider, because the thing under test is
the *pipeline* — caching, retry classification, quota accounting, schema
validation, `AIRun` lineage — and every one of those is provider-agnostic by
construction. Testing it against a patched `requests` would be testing the
patch.

It is also what makes the eval harness runnable in CI with no key, no network
and no cost: a golden corpus needs a model that answers the same way twice, and
no real model does.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from typing import Any

from apps.common.models import EMBEDDING_DIMENSIONS

from .base import (
    EmbeddingResult,
    ProviderFailure,
    ProviderRefused,
    ProviderTimeout,
    Request,
    Result,
)


class FakeAdapter:
    """Answers from a queue. Empty queue means a canned success."""

    key = "fake"

    #: Class-level so a test can arrange answers before the runner builds the
    #: instance — the runner constructs adapters itself, which is the behaviour
    #: under test and not something to work around.
    responses: deque[Any] = deque()
    calls: list[dict[str, Any]] = []

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = base_url

    @classmethod
    def reset(cls) -> None:
        cls.responses = deque()
        cls.calls = []

    @classmethod
    def queue(cls, *answers: Any) -> None:
        """Arrange what the next calls return.

        An answer is a string (returned as the model's text), an exception
        instance (raised), or a dict (serialised to JSON, which is what a task
        with an output schema expects).
        """
        for answer in answers:
            cls.responses.append(answer)

    def complete(self, request: Request, *, model: str, timeout: int) -> Result:
        type(self).calls.append(
            {"model": model, "system": request.system, "user": request.user, "timeout": timeout}
        )

        answer = self.responses.popleft() if self.responses else {"text": "ok"}

        if isinstance(answer, ProviderTimeout | ProviderFailure | ProviderRefused):
            raise answer
        if isinstance(answer, BaseException):
            raise answer

        text = answer if isinstance(answer, str) else json.dumps(answer)
        # Token counts proportional to length: a test asserting that cost is
        # attributed needs a cost that is not zero, and a constant would hide
        # an arithmetic mistake.
        return Result(
            text=text,
            tokens_in=max(len(request.user) // 4, 1),
            tokens_out=max(len(text) // 4, 1),
            raw={"fake": True},
        )

    def embed(self, text: str, *, model: str, timeout: int) -> EmbeddingResult:
        """A deterministic vector for one string (P13).

        Derived from a hash of the text, so the same text always embeds to the
        same vector and two different texts almost never collide. That is enough
        to test everything the platform actually does with a vector — store it,
        filter by model, order by distance, notice when it is missing — without
        a model, a GPU or a network.

        It is **not** semantic: "Python developer" and "Django engineer" are as
        far apart here as any two strings. No test may assert that similar text
        embeds nearby, because this adapter cannot make that true and a test
        that seemed to pass would be asserting a hash collision.
        """
        type(self).calls.append({"model": model, "embed": text, "timeout": timeout})

        answer = self.responses.popleft() if self.responses else None
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, list):
            return EmbeddingResult(vector=[float(value) for value in answer], tokens_in=1)

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat the digest to fill the width, then map bytes onto [-1, 1].
        raw = (digest * ((EMBEDDING_DIMENSIONS // len(digest)) + 1))[:EMBEDDING_DIMENSIONS]
        vector = [(byte - 127.5) / 127.5 for byte in raw]

        length = math.sqrt(sum(value * value for value in vector)) or 1.0
        return EmbeddingResult(
            vector=[value / length for value in vector],
            tokens_in=max(len(text) // 4, 1),
            raw={"fake": True},
        )
