"""LLMProvider — the only LLM seam the rest of the system knows about.

Services ask for either a *structured* result (a Pydantic model, schema-enforced by the provider)
or plain *text*. Which vendor / model / SDK sits behind it is a configuration choice
(`LLM_PROVIDER`), which is what keeps the LLM swappable and the test-suite offline.
"""

from abc import ABC, abstractmethod
from typing import Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Tier = Literal["default", "fast"]
"""`default` = strongest configured model (generation, grading, prose);
`fast` = cheaper model for bulk/low-stakes work (topic labelling)."""


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def structured(self, system: str, user: str, schema: type[T], *, tier: Tier = "default") -> T:
        """Return an instance of `schema`. Raises `LLMError` after exhausting retries."""

    @abstractmethod
    def text(self, system: str, user: str, *, tier: Tier = "default") -> str:
        """Return free-form text. Raises `LLMError` after exhausting retries."""
