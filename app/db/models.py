"""SQLAlchemy ORM models.

Portable types only (String UUIDs, JSON, string enums validated by Pydantic) so the schema runs
unchanged on SQLite and Postgres.

    Video 1─* TranscriptChunk
    Video 1─* LearnerProgress
    Video 1─* Assessment 1─* Question
              Assessment 1─* Attempt 1─* Answer (*─1 Question)
                                 Attempt 1─1 Report
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class VideoStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    TRANSCRIBED = "transcribed"
    INDEXED = "indexed"
    FAILED = "failed"


class QuestionType(StrEnum):
    MCQ = "mcq"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255))
    source_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(20), default=VideoStatus.PENDING, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str | None] = mapped_column(String(16))
    transcript_text: Mapped[str | None] = mapped_column(Text)
    # Raw whisper segments [{start, end, text}] — kept so a retried job can skip re-transcription.
    transcript_segments: Mapped[list[Any] | None] = mapped_column()
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    chunks: Mapped[list["TranscriptChunk"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="TranscriptChunk.chunk_index"
    )


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"
    __table_args__ = (UniqueConstraint("video_id", "chunk_index", name="uq_chunk_video_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    topic: Mapped[str] = mapped_column(String(128), default="General")
    qdrant_point_id: Mapped[str] = mapped_column(String(36))

    video: Mapped[Video] = relationship(back_populates="chunks")


class LearnerProgress(Base):
    __tablename__ = "learner_progress"
    __table_args__ = (UniqueConstraint("learner_id", "video_id", name="uq_progress_learner_video"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    learner_id: Mapped[str] = mapped_column(String(128), index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    learner_id: Mapped[str] = mapped_column(String(128), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    questions: Mapped[list["Question"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", order_by="Question.position"
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[list[Any] | None] = mapped_column()
    # MCQ: the exact option text; true_false: "true" / "false"; short_answer: null.
    correct_answer: Mapped[str | None] = mapped_column(Text)
    reference_answer: Mapped[str | None] = mapped_column(Text)
    rubric: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(128))
    difficulty: Mapped[str] = mapped_column(String(16), default="medium")
    source_chunk_ids: Mapped[list[Any]] = mapped_column(default=list)

    assessment: Mapped[Assessment] = relationship(back_populates="questions")


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    learner_id: Mapped[str] = mapped_column(String(128), index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_score: Mapped[float] = mapped_column(Float, default=0.0)

    answers: Mapped[list["Answer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )
    report: Mapped["Report | None"] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", uselist=False
    )


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id", name="uq_answer_attempt_q"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    response: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    is_correct: Mapped[bool] = mapped_column(default=False)
    feedback: Mapped[str] = mapped_column(Text, default="")
    improvement_areas: Mapped[list[Any]] = mapped_column(default=list)
    evaluator: Mapped[str] = mapped_column(String(16))  # "rule" | "llm" | "none"

    attempt: Mapped[Attempt] = relationship(back_populates="answers")
    question: Mapped[Question] = relationship()


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), unique=True
    )
    overall: Mapped[dict[str, Any]] = mapped_column(default=dict)
    by_type: Mapped[dict[str, Any]] = mapped_column(default=dict)
    topic_breakdown: Mapped[list[Any]] = mapped_column(default=list)
    strengths: Mapped[list[Any]] = mapped_column(default=list)
    weaknesses: Mapped[list[Any]] = mapped_column(default=list)
    improvement_areas: Mapped[list[Any]] = mapped_column(default=list)
    suggestions: Mapped[list[Any]] = mapped_column(default=list)
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    summary_source: Mapped[str] = mapped_column(String(16), default="llm")  # "llm" | "template"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    attempt: Mapped[Attempt] = relationship(back_populates="report")
