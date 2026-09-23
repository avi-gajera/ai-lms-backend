"""Request / response models for the REST API (also the source of the OpenAPI docs)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QType = Literal["mcq", "true_false", "short_answer"]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- videos ------------------------------------------------------------------------------------


class VideoAccepted(BaseModel):
    video_id: str
    task_id: str
    status: str
    status_url: str
    task_url: str


class VideoOut(_Out):
    id: str
    title: str
    status: str
    error: str | None
    duration_s: float | None
    language: str | None
    chunk_count: int
    topics: list[str]
    created_at: datetime
    updated_at: datetime


class ChunkOut(_Out):
    id: str
    chunk_index: int
    start_time: float
    end_time: float
    topic: str
    text: str


class ProgressIn(BaseModel):
    learner_id: str = Field(min_length=1, max_length=128, examples=["learner-001"])
    progress: float = Field(ge=0, le=1, description="Fraction of the video watched (0-1)", examples=[0.95])


class ProgressOut(BaseModel):
    video_id: str
    learner_id: str
    progress: float
    completion_threshold: float
    assessment_unlocked: bool


class RetrieveIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000, examples=["What is the base case of a recursive function?"])
    top_k: int | None = Field(default=None, ge=1, le=20)


class RetrievedChunkOut(BaseModel):
    chunk_id: str
    chunk_index: int
    score: float
    start_time: float
    end_time: float
    timestamp: str
    topic: str
    text: str


class RetrieveOut(BaseModel):
    video_id: str
    query: str
    results: list[RetrievedChunkOut]


# --- assessments -------------------------------------------------------------------------------


class AssessmentCreate(BaseModel):
    video_id: str
    learner_id: str = Field(min_length=1, max_length=128, examples=["learner-001"])
    num_questions: int | None = Field(default=None, ge=1, le=20)
    mix: dict[QType, float] | None = Field(
        default=None,
        description="Relative share per question type, e.g. {'mcq': 2, 'true_false': 1, 'short_answer': 2}",
    )


class QuestionOut(_Out):
    """A question as shown to the learner — answers and rubric are deliberately not exposed."""

    id: str
    position: int
    type: QType
    prompt: str
    options: list[str] | None
    topic: str
    difficulty: str


class AssessmentOut(_Out):
    id: str
    video_id: str
    learner_id: str
    created_at: datetime
    questions: list[QuestionOut]


# --- attempts / evaluation ---------------------------------------------------------------------


class AnswerIn(BaseModel):
    question_id: str
    response: str | None = Field(
        default=None,
        description="mcq: option text, letter (A-D) or number (1-4); true_false: true/false; short_answer: free text",
    )


class AttemptCreate(BaseModel):
    assessment_id: str
    learner_id: str = Field(min_length=1, max_length=128)
    answers: list[AnswerIn]


class AnswerResult(BaseModel):
    question_id: str
    position: int
    type: QType
    topic: str
    prompt: str
    response: str | None
    score: float
    is_correct: bool
    feedback: str
    improvement_areas: list[str]
    evaluator: Literal["rule", "llm", "none"]
    correct_answer: str | None
    reference_answer: str | None


class AttemptOut(BaseModel):
    id: str
    assessment_id: str
    learner_id: str
    submitted_at: datetime
    total_score: float
    max_score: float
    percentage: float
    answers: list[AnswerResult]
    report_url: str


class ReportOut(_Out):
    id: str
    attempt_id: str
    created_at: datetime
    overall: dict[str, Any]
    by_type: dict[str, Any]
    topic_breakdown: list[dict[str, Any]]
    strengths: list[dict[str, Any]]
    weaknesses: list[dict[str, Any]]
    improvement_areas: list[str]
    suggestions: list[str]
    ai_summary: str
    summary_source: Literal["llm", "template"]


# --- tasks / health ----------------------------------------------------------------------------


class TaskOut(BaseModel):
    task_id: str
    state: str
    backend: str
    result: Any = None
    error: str | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, str]
    task_backend: str
    llm_provider: str
