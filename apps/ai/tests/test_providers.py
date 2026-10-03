"""
The provider adapters (ADR-A28).

What is tested here is the seam, not the vendors: that each adapter normalises
one HTTP exchange into the same `Result`, and — more importantly — that it maps
failures onto the three exception types the runner classifies on. An adapter
that raised a bare `RequestException` would make every outage look permanent.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import requests
from django.test import SimpleTestCase

from apps.ai.providers.base import (
    ProviderFailure,
    ProviderRefused,
    ProviderTimeout,
    Request,
)
from apps.ai.providers.ollama import OllamaAdapter
from apps.ai.providers.openrouter import OpenRouterAdapter
from apps.common.errors import InvalidState
from config import ai as ai_config

REQUEST = Request(system="Be careful.", user="Review this.", json_schema={"type": "object"})


def _response(status: int, payload=None, text: str = ""):
    response = mock.Mock(status_code=status, text=text)
    response.json.return_value = payload if payload is not None else {}
    return response


class OpenRouterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = OpenRouterAdapter(base_url="https://api.test/v1", api_key="k")

    def complete(self, response):
        with mock.patch("requests.post", return_value=response) as post:
            result = self.adapter.complete(REQUEST, model="m", timeout=5)
        return result, post

    def test_a_good_response_is_normalised(self):
        result, _ = self.complete(
            _response(
                200,
                {
                    "id": "gen-1",
                    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )
        )

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.tokens_in, 11)
        self.assertEqual(result.tokens_out, 7)

    def test_the_schema_is_sent_when_one_is_given(self):
        _, post = self.complete(
            _response(200, {"choices": [{"message": {"content": "{}"}}], "usage": {}})
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_a_timeout_is_its_own_exception(self):
        with (
            mock.patch("requests.post", side_effect=requests.Timeout("slow")),
            self.assertRaises(ProviderTimeout),
        ):
            self.adapter.complete(REQUEST, model="m", timeout=1)

    def test_a_transport_error_is_a_failure(self):
        with (
            mock.patch("requests.post", side_effect=requests.ConnectionError("reset")),
            self.assertRaises(ProviderFailure),
        ):
            self.adapter.complete(REQUEST, model="m", timeout=1)

    def test_a_rate_limit_is_retryable(self):
        # 429 is transient by definition, so it must not be a refusal.
        with self.assertRaises(ProviderFailure):
            self.complete(_response(429, text="slow down"))

    def test_a_server_error_is_retryable(self):
        with self.assertRaises(ProviderFailure):
            self.complete(_response(503, text="unavailable"))

    def test_a_bad_request_is_not_retried(self):
        # Our request being wrong. Sending it again produces the same answer
        # more expensively.
        with self.assertRaises(ProviderRefused):
            self.complete(_response(400, text="bad model"))

    def test_a_content_filter_is_a_refusal(self):
        with self.assertRaises(ProviderRefused):
            self.complete(
                _response(
                    200,
                    {
                        "choices": [
                            {"message": {"content": ""}, "finish_reason": "content_filter"}
                        ],
                        "usage": {},
                    },
                )
            )

    def test_an_unreadable_body_is_a_failure(self):
        response = mock.Mock(status_code=200, text="not json")
        response.json.side_effect = ValueError("nope")
        with (
            mock.patch("requests.post", return_value=response),
            self.assertRaises(ProviderFailure),
        ):
            self.adapter.complete(REQUEST, model="m", timeout=5)


class OllamaTests(SimpleTestCase):
    def setUp(self):
        self.adapter = OllamaAdapter(base_url="http://localhost:11434")

    def test_a_good_response_is_normalised(self):
        response = _response(200, {"response": "hello", "prompt_eval_count": 5, "eval_count": 3})
        with mock.patch("requests.post", return_value=response):
            result = self.adapter.complete(REQUEST, model="llama", timeout=5)

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.tokens_in, 5)

    def test_the_schema_is_asked_for_in_the_prompt(self):
        # No native schema mode, so the instruction is the honest fallback.
        response = _response(200, {"response": "{}"})
        with mock.patch("requests.post", return_value=response) as post:
            self.adapter.complete(REQUEST, model="llama", timeout=5)

        payload = post.call_args.kwargs["json"]
        self.assertIn("JSON only", payload["prompt"])
        # The schema itself, so Ollama constrains decoding to it, rather than
        # "json" plus the schema pasted into the prompt as text.
        self.assertEqual(payload["format"], {"type": "object"})
        self.assertNotIn('"type": "object"', payload["prompt"])

    def test_a_timeout_is_its_own_exception(self):
        with (
            mock.patch("requests.post", side_effect=requests.Timeout("slow")),
            self.assertRaises(ProviderTimeout),
        ):
            self.adapter.complete(REQUEST, model="llama", timeout=1)

    def test_a_server_error_is_a_failure(self):
        with (
            mock.patch("requests.post", return_value=_response(500, text="boom")),
            self.assertRaises(ProviderFailure),
        ):
            self.adapter.complete(REQUEST, model="llama", timeout=5)


class ConfigTests(SimpleTestCase):
    """Routing configuration is code (ADR-A28), so it is testable like code."""

    def test_cost_is_computed_from_token_counts(self):
        spec = ai_config.ModelSpec(
            provider="x",
            name="y",
            input_cost_per_million=Decimal("1.00"),
            output_cost_per_million=Decimal("2.00"),
        )
        self.assertEqual(spec.cost_for(1_000_000, 0), Decimal("1.000000"))
        self.assertEqual(spec.cost_for(0, 500_000), Decimal("1.000000"))

    def test_every_task_names_models_that_exist(self):
        for key, spec in ai_config.TASKS.items():
            for name in spec.models:
                with self.subTest(task=key, model=name):
                    self.assertIn(name, ai_config.MODELS)

    def test_every_model_names_a_provider_that_exists(self):
        for name, spec in ai_config.MODELS.items():
            with self.subTest(model=name):
                self.assertIn(spec.provider, ai_config.PROVIDERS)

    def test_an_unconfigured_provider_is_skipped_rather_than_attempted(self):
        # Trying it produces a 401 that looks like an outage and wastes a retry
        # slot a working provider could have used.
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}, clear=False):
            chain = ai_config.chain_for("resume.analysis")
            self.assertTrue(all(provider.key != "openrouter" for _, _, provider in chain))

    def test_a_configured_provider_is_included(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-test"}, clear=False):
            chain = ai_config.chain_for("resume.analysis")
            self.assertTrue(any(provider.key == "openrouter" for _, _, provider in chain))

    def test_an_unknown_task_is_refused(self):
        with self.assertRaises(InvalidState):
            ai_config.task("no.such.task")

    def test_an_unknown_model_is_refused(self):
        with self.assertRaises(InvalidState):
            ai_config.model("gpt-9")
