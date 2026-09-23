"""GroqProvider retry/classification logic, with the network layer stubbed out."""

import groq
import httpx
import pytest

from app.config import Settings
from app.core.exceptions import LLMError, LLMUnavailable
from app.llm import groq_provider
from app.llm.groq_provider import GroqProvider
from app.llm.schemas import ReportNarrative

_REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _err(cls, status: int, message: str, headers: dict | None = None):
    resp = httpx.Response(status, request=_REQ, headers=headers or {}, json={"error": {"message": message}})
    return cls(message, response=resp, body={"error": {"message": message}})


class _Runnable:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def invoke(self, _messages):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Chat:
    def __init__(self, runnable):
        self.runnable = runnable
        self.methods = []

    def with_structured_output(self, schema, *, method, strict, include_raw):
        self.methods.append(method)
        return self.runnable


GOOD = {"raw": None, "parsed": ReportNarrative(summary="ok", suggestions=["a"]), "parsing_error": None}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(groq_provider, "_backoff", lambda rs: 0)  # no real sleeping
    p = GroqProvider(Settings(groq_api_key="test", llm_max_attempts=4))
    return p


def _wire(provider, monkeypatch, outcomes):
    chat = _Chat(_Runnable(outcomes))
    monkeypatch.setattr(provider, "_llm", lambda tier: chat)
    return chat


def test_requires_api_key():
    with pytest.raises(LLMUnavailable):
        GroqProvider(Settings(groq_api_key=None))


def test_success_uses_strict_json_schema(provider, monkeypatch):
    chat = _wire(provider, monkeypatch, [GOOD])
    assert provider.structured("s", "u", ReportNarrative).summary == "ok"
    assert chat.methods == ["json_schema"]


def test_invalid_generation_is_retried(provider, monkeypatch):
    bad = _err(groq.BadRequestError, 400, "Generated JSON does not match the expected schema. failed_generation")
    chat = _wire(provider, monkeypatch, [bad, bad, GOOD])
    assert provider.structured("s", "u", ReportNarrative).summary == "ok"
    assert chat.runnable.calls == 3
    assert set(chat.methods) == {"json_schema"}  # a sampling failure must not trigger the fallback


def test_rate_limit_is_retried(provider, monkeypatch):
    limited = _err(groq.RateLimitError, 429, "Rate limit reached", headers={"retry-after": "0"})
    chat = _wire(provider, monkeypatch, [limited, GOOD])
    assert provider.structured("s", "u", ReportNarrative).summary == "ok"
    assert chat.runnable.calls == 2


def test_rate_limit_wait_uses_retry_after_header():
    def state(exc):
        return type("RS", (), {"outcome": type("O", (), {"exception": lambda self: exc})()})()

    limited = _err(groq.RateLimitError, 429, "Rate limit reached", headers={"retry-after": "7"})
    assert groq_provider._wait_for_rate_limit(state(limited)) == 7.5
    capped = _err(groq.RateLimitError, 429, "Rate limit reached", headers={"retry-after": "600"})
    assert groq_provider._wait_for_rate_limit(state(capped)) == 60.0


def test_unparseable_output_is_retried_then_raises(provider, monkeypatch):
    bad = {"raw": None, "parsed": None, "parsing_error": ValueError("nope")}
    chat = _wire(provider, monkeypatch, [bad])
    with pytest.raises(LLMError) as exc:
        provider.structured("s", "u", ReportNarrative)
    assert chat.runnable.calls == 4
    assert exc.value.details["what"] == "ReportNarrative"


def test_auth_error_fails_fast(provider, monkeypatch):
    chat = _wire(provider, monkeypatch, [_err(groq.AuthenticationError, 401, "invalid api key")])
    with pytest.raises(LLMUnavailable):
        provider.structured("s", "u", ReportNarrative)
    assert chat.runnable.calls == 1


def test_unsupported_json_schema_falls_back_to_tool_calling(provider, monkeypatch):
    unsupported = _err(groq.BadRequestError, 400, "response_format json_schema is not supported for this model")
    chat = _wire(provider, monkeypatch, [unsupported, GOOD])
    provider.structured("s", "u", ReportNarrative)
    assert chat.methods == ["json_schema", "function_calling"]
