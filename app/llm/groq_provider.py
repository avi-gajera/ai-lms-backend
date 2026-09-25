"""Groq implementation of LLMProvider, via LangChain's `ChatGroq`.

- Structured output uses Groq's strict `json_schema` mode (constrained decoding: the output is
  guaranteed to match the Pydantic schema). If a model rejects strict mode, we fall back once to
  tool calling for that schema and remember the choice.
- Transient failures (429 rate limit, 5xx, timeouts, connection errors) and unparseable outputs are
  retried with exponential backoff; auth/config errors fail fast. Anything that still fails is
  raised as `LLMError`, which the API maps to a 502/503.
- Every call logs model, latency and token usage.
"""

import time
from typing import Any

import groq
from langchain_groq import ChatGroq
from pydantic import BaseModel, ValidationError
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings
from app.core.exceptions import LLMError, LLMUnavailable
from app.core.logging import get_logger
from app.llm.base import LLMProvider, T, Tier
from app.llm.prompts import PROMPT_VERSION

logger = get_logger(__name__)


class _Retryable(Exception):
    """Internal marker: this attempt failed in a way worth retrying."""


_TRANSIENT = (
    groq.RateLimitError,
    groq.APITimeoutError,
    groq.APIConnectionError,
    groq.InternalServerError,
)

# Groq returns HTTP 400 when the *model's output* fails schema validation (json_validate_failed /
# tool_use_failed). That is a sampling failure worth retrying, unlike a genuinely bad request.
_GENERATION_FAILURES = (
    "failed_generation",
    "does not match the expected schema",
    "tool call validation failed",
    "json_validate_failed",
    "tool_use_failed",
)

_backoff = wait_exponential_jitter(initial=2, max=30)


def _wait_for_rate_limit(retry_state) -> float:
    """On a 429, wait as long as Groq's `retry-after` header says (tokens-per-minute windows
    refill on a known schedule); otherwise exponential backoff with jitter."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, groq.RateLimitError):
        try:
            return min(60.0, float(exc.response.headers.get("retry-after", "")) + 0.5)
        except (TypeError, ValueError):
            pass
    return _backoff(retry_state)


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, settings: Settings):
        if not settings.groq_api_key:
            raise LLMUnavailable("GROQ_API_KEY is not set; configure it or use LLM_PROVIDER=fake.")
        self.settings = settings
        self._models = {"default": settings.groq_model, "fast": settings.groq_model_fast}
        self._chat: dict[str, ChatGroq] = {}
        self._fallback_schemas: set[str] = set()  # schemas that needed function_calling

    def _llm(self, tier: Tier) -> ChatGroq:
        if tier not in self._chat:
            extra: dict[str, Any] = {}
            effort = self.settings.llm_reasoning_effort_fast if tier == "fast" else self.settings.llm_reasoning_effort
            if effort:
                extra["reasoning_effort"] = effort
            self._chat[tier] = ChatGroq(
                model=self._models[tier],
                api_key=self.settings.groq_api_key,
                temperature=self.settings.llm_temperature,
                timeout=self.settings.llm_timeout_s,
                max_retries=0,  # retries are ours (tenacity), so they are logged and bounded
                **extra,
            )
        return self._chat[tier]

    # --- public API ----------------------------------------------------------------------------

    def structured(self, system: str, user: str, schema: type[T], *, tier: Tier = "default") -> T:
        messages = [("system", system), ("human", user)]

        def attempt() -> T:
            method = "function_calling" if schema.__name__ in self._fallback_schemas else "json_schema"
            runnable = self._llm(tier).with_structured_output(
                schema, method=method, strict=method == "json_schema" or None, include_raw=True
            )
            try:
                out: dict[str, Any] = runnable.invoke(messages)
            except groq.BadRequestError as exc:
                text = str(exc).lower()
                if any(s in text for s in _GENERATION_FAILURES):
                    # The model produced output that failed Groq's schema validation: a sampling
                    # failure, not a bad request — another attempt usually succeeds.
                    raise _Retryable(f"invalid generation: {str(exc)[:300]}") from exc
                if method == "json_schema" and ("not supported" in text or "unsupported" in text):
                    logger.warning(
                        "json_schema not supported; falling back to tool calling",
                        extra={"schema": schema.__name__, "error": str(exc)[:300]},
                    )
                    self._fallback_schemas.add(schema.__name__)
                    raise _Retryable(str(exc)) from exc
                raise
            self._log_usage(out.get("raw"), tier, schema.__name__)
            parsed = out.get("parsed")
            if out.get("parsing_error") or parsed is None:
                raise _Retryable(f"unparseable output: {out.get('parsing_error')}")
            if isinstance(parsed, BaseModel) and not isinstance(parsed, schema):
                parsed = schema.model_validate(parsed.model_dump())
            elif isinstance(parsed, dict):
                parsed = schema.model_validate(parsed)
            return parsed  # type: ignore[return-value]

        return self._with_retries(attempt, what=schema.__name__)

    def text(self, system: str, user: str, *, tier: Tier = "default") -> str:
        def attempt() -> str:
            msg = self._llm(tier).invoke([("system", system), ("human", user)])
            self._log_usage(msg, tier, "text")
            return str(msg.content)

        return self._with_retries(attempt, what="text")

    # --- internals -----------------------------------------------------------------------------

    def _with_retries(self, fn, *, what: str):
        start = time.perf_counter()
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self.settings.llm_max_attempts),
                wait=_wait_for_rate_limit,
                retry=retry_if_exception_type((_Retryable, ValidationError, *_TRANSIENT)),
                reraise=False,
                before_sleep=lambda rs: logger.warning(
                    "llm call retrying",
                    extra={
                        "what": what,
                        "attempt": rs.attempt_number,
                        "error": str(rs.outcome.exception())[:300] if rs.outcome else None,
                    },
                ),
            ):
                with attempt:
                    return fn()
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise LLMError(
                f"LLM call failed after {self.settings.llm_max_attempts} attempts; please retry later.",
                details={"what": what, "error": str(last)[:500]},
            ) from last
        except groq.AuthenticationError as exc:
            raise LLMUnavailable("Groq rejected the API key (check GROQ_API_KEY).") from exc
        except (groq.APIStatusError, groq.APIError) as exc:
            raise LLMError(f"Groq API error: {str(exc)[:300]}", details={"what": what}) from exc
        finally:
            logger.debug("llm call finished", extra={"what": what, "elapsed_s": round(time.perf_counter() - start, 2)})

    def _log_usage(self, raw: Any, tier: Tier, what: str) -> None:
        usage = getattr(raw, "usage_metadata", None) or {}
        logger.info(
            "llm call",
            extra={
                "provider": self.name,
                "model": self._models[tier],
                "what": what,
                "prompt_version": PROMPT_VERSION,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
        )
