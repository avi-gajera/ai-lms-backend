"""Select the configured LLMProvider (one instance per process)."""

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMProvider


@lru_cache
def get_llm() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "fake":
        from app.llm.fake_provider import FakeLLMProvider

        return FakeLLMProvider()

    from app.llm.groq_provider import GroqProvider

    return GroqProvider(s)
