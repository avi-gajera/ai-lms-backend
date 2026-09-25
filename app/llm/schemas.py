"""Output contracts for every LLM call.

These models are sent to the provider as strict JSON schemas, so they follow strict-mode rules:
every field is required (no defaults / Optionals — "empty" is an empty string or list) and no
extra properties. Semantic validation (e.g. "MCQ answer must be one of its options") happens
afterwards in the services.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Topic labelling ---------------------------------------------------------------------------


class ChunkTopic(_Strict):
    chunk_index: int
    topic: str = Field(description="Short topic name (2-5 words), reused across related chunks")


class TopicLabels(_Strict):
    labels: list[ChunkTopic]


# --- Question generation -----------------------------------------------------------------------
# One schema per question type, containing only the fields that type needs. Asking for one type
# per call (instead of one mixed list full of "empty when not applicable" fields) proved far more
# reliable: the model returns the requested count and does not drop fields.


class _QuestionBase(_Strict):
    prompt: str = Field(description="The question text shown to the learner")
    explanation: str = Field(description="Why the correct answer is correct, citing the context")
    topic: str = Field(description="The `topic` value of the source chunk this question is based on")
    difficulty: Literal["easy", "medium", "hard"]
    source_chunk_ids: list[str] = Field(description="Ids of the context chunks the answer relies on")


class MCQItem(_QuestionBase):
    options: list[str] = Field(description="Exactly 4 distinct answer options")
    correct_answer: str = Field(description="The exact text of the correct option")


class TrueFalseItem(_QuestionBase):
    prompt: str = Field(description="A statement the learner judges as true or false")
    is_true: bool = Field(description="Whether the statement is true according to the context")


class ShortAnswerItem(_QuestionBase):
    reference_answer: str = Field(description="A model answer (1-3 sentences) grounded in the context")
    rubric: str = Field(description="The key points a full-credit answer must contain")


class MCQSet(_Strict):
    questions: list[MCQItem]


class TrueFalseSet(_Strict):
    questions: list[TrueFalseItem]


class ShortAnswerSet(_Strict):
    questions: list[ShortAnswerItem]


QUESTION_SET_SCHEMAS: dict[str, type[_Strict]] = {
    "mcq": MCQSet,
    "true_false": TrueFalseSet,
    "short_answer": ShortAnswerSet,
}


class GeneratedQuestion(_Strict):
    """Internal, type-agnostic form every generated question is normalised into before validation."""

    type: Literal["mcq", "true_false", "short_answer"]
    prompt: str = Field(description="The question text shown to the learner")
    options: list[str] = Field(description="Exactly 4 options for mcq; empty list otherwise")
    correct_answer: str = Field(
        description="mcq: the exact text of the correct option; true_false: 'true' or 'false'; "
        "short_answer: empty string"
    )
    reference_answer: str = Field(description="A model answer grounded in the context (required for short_answer)")
    rubric: str = Field(description="Key points a full-credit short answer must contain; empty for mcq/true_false")
    explanation: str = Field(description="Why the correct answer is correct, citing the context")
    topic: str = Field(description="The topic of the source chunk(s) this question tests")
    difficulty: Literal["easy", "medium", "hard"]
    source_chunk_ids: list[str] = Field(description="Ids of the context chunks the answer relies on")


def normalise_question(qtype: str, item: _QuestionBase) -> GeneratedQuestion:
    common = dict(
        type=qtype,
        prompt=item.prompt,
        explanation=item.explanation,
        topic=item.topic,
        difficulty=item.difficulty,
        source_chunk_ids=item.source_chunk_ids,
    )
    if isinstance(item, MCQItem):
        return GeneratedQuestion(
            **common,
            options=item.options,
            correct_answer=item.correct_answer,
            reference_answer=item.correct_answer,
            rubric="",
        )
    if isinstance(item, TrueFalseItem):
        answer = "true" if item.is_true else "false"
        return GeneratedQuestion(**common, options=[], correct_answer=answer, reference_answer=answer, rubric="")
    assert isinstance(item, ShortAnswerItem)
    return GeneratedQuestion(
        **common, options=[], correct_answer="", reference_answer=item.reference_answer, rubric=item.rubric
    )


# --- Short-answer grading (LLM-as-judge) -------------------------------------------------------


class JudgedAnswer(_Strict):
    question_id: str
    score: float = Field(description="0.0 (wrong/irrelevant) to 1.0 (complete and accurate)")
    feedback: str = Field(description="2-3 sentences addressed to the learner")
    improvement_areas: list[str] = Field(description="Concrete concepts to revisit; empty if none")


class JudgedAnswers(_Strict):
    results: list[JudgedAnswer]


# --- Report narrative --------------------------------------------------------------------------


class ReportNarrative(_Strict):
    summary: str = Field(description="One paragraph (80-150 words) addressed to the learner")
    suggestions: list[str] = Field(description="3-5 specific, actionable study suggestions")
