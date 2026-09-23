"""Deterministic, offline LLMProvider.

Used by the test-suite and for running the whole pipeline without an API key
(`LLM_PROVIDER=fake`). It reads the same ```json block the real model sees and builds a
schema-valid, content-derived answer — so every code path downstream of the LLM is exercised.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.llm.base import LLMProvider, T, Tier
from app.llm.schemas import (
    ChunkTopic,
    JudgedAnswer,
    JudgedAnswers,
    MCQItem,
    MCQSet,
    ReportNarrative,
    ShortAnswerItem,
    ShortAnswerSet,
    TopicLabels,
    TrueFalseItem,
    TrueFalseSet,
)

_JSON_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_WORD = re.compile(r"[a-z0-9]+")


def _payload(user: str) -> dict[str, Any]:
    m = _JSON_BLOCK.search(user)
    return json.loads(m.group(1)) if m else {}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 3}


def _sentence(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0]
    return first[:200]


class FakeLLMProvider(LLMProvider):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (schema/kind, tier) — handy for test assertions
        self._handlers: dict[type[BaseModel], Callable[[dict], BaseModel]] = {
            TopicLabels: self._topics,
            MCQSet: self._mcq,
            TrueFalseSet: self._true_false,
            ShortAnswerSet: self._short,
            JudgedAnswers: self._judge,
            ReportNarrative: self._narrative,
        }

    def structured(self, system: str, user: str, schema: type[T], *, tier: Tier = "default") -> T:
        self.calls.append((schema.__name__, tier))
        return self._handlers[schema](_payload(user))  # type: ignore[return-value]

    def text(self, system: str, user: str, *, tier: Tier = "default") -> str:
        self.calls.append(("text", tier))
        return "This is a deterministic response from the fake LLM provider."

    # --- handlers ------------------------------------------------------------------------------

    @staticmethod
    def _topics(data: dict) -> TopicLabels:
        # Two consecutive chunks per topic → a handful of topics per video, deterministically.
        return TopicLabels(
            labels=[
                ChunkTopic(chunk_index=c["chunk_index"], topic=f"Topic {c['chunk_index'] // 2 + 1}")
                for c in data.get("chunks", [])
            ]
        )

    # Question sets: one item per requested question, cycling through the context chunks and
    # skipping chunk offsets already used (via `already_asked`) so prompts stay unique.

    @staticmethod
    def _items(data: dict):
        chunks = data.get("chunks", [])
        offset = len(data.get("already_asked", []))
        for i in range(data.get("count", 0)):
            k = offset + i
            c = chunks[k % len(chunks)]
            fact = _sentence(c["text"])
            yield k, c, fact, dict(
                explanation=f"The video explains: {fact}", topic=c["topic"],
                difficulty="medium", source_chunk_ids=[c["id"]],
            )

    def _mcq(self, data: dict) -> MCQSet:
        return MCQSet(questions=[
            MCQItem(prompt=f"Which statement best reflects the video's point about {c['topic']} (Q{k})?",
                    options=[fact, "None of the ideas in the video", "The opposite claim", "An unrelated claim"],
                    correct_answer=fact, **common)
            for k, c, fact, common in self._items(data)
        ])

    def _true_false(self, data: dict) -> TrueFalseSet:
        return TrueFalseSet(questions=[
            TrueFalseItem(prompt=f"(Q{k}) {fact}", is_true=True, **common)
            for k, c, fact, common in self._items(data)
        ])

    def _short(self, data: dict) -> ShortAnswerSet:
        return ShortAnswerSet(questions=[
            ShortAnswerItem(prompt=f"In your own words, explain the key idea about {c['topic']} (Q{k}).",
                            reference_answer=fact, rubric=f"Mentions: {fact}", **common)
            for k, c, fact, common in self._items(data)
        ])

    @staticmethod
    def _judge(data: dict) -> JudgedAnswers:
        results = []
        for item in data.get("answers_to_grade", []):
            ref, resp = _words(item.get("reference_answer", "")), _words(item.get("response", ""))
            score = round(len(ref & resp) / len(ref), 2) if ref else 0.0
            results.append(JudgedAnswer(
                question_id=item["question_id"],
                score=score,
                feedback=(
                    "Good answer — it covers the key idea." if score >= 0.6
                    else "Your answer misses part of the key idea from the video."
                ),
                improvement_areas=[] if score >= 0.6 else [f"Review: {item.get('topic', 'this topic')}"],
            ))
        return JudgedAnswers(results=results)

    @staticmethod
    def _narrative(data: dict) -> ReportNarrative:
        overall = data.get("overall", {})
        weak = [w["topic"] for w in data.get("weaknesses", [])]
        strong = [s["topic"] for s in data.get("strengths", [])]
        return ReportNarrative(
            summary=(
                f"You scored {overall.get('percentage', 0)}% on this assessment. "
                + (f"You showed solid understanding of {', '.join(strong)}. " if strong else "")
                + (f"Spend more time on {', '.join(weak)}." if weak else "Keep up the good work.")
            ),
            suggestions=[f"Rewatch the section on {t} and summarise it in your own words." for t in weak]
            or ["Try explaining the main ideas of the video to someone else."],
        )
