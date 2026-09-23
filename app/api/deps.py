"""Shared FastAPI dependencies.

`learner_id` is passed in request bodies (no auth is specified by the brief). To add auth later,
a `current_learner` dependency would replace it here without touching routes' business logic.
"""

from app.config import Settings, get_settings
from app.db.session import get_db
from app.llm.base import LLMProvider
from app.llm.factory import get_llm

__all__ = ["get_db", "get_settings_dep", "get_llm_dep", "Settings", "LLMProvider"]


def get_settings_dep() -> Settings:
    return get_settings()


def get_llm_dep() -> LLMProvider:
    return get_llm()
