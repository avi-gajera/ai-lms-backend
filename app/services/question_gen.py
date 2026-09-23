"""Assessment generation: pick representative context → one grounded structured-output call per
question type (+ one top-up per type if needed) → validate → persist.

Context selection deliberately does NOT use similarity search: a quiz should cover the whole video,
so we take chunks round-robin across topics (evenly spaced within each topic) up to a token budget.
"""

import math
from collections import OrderedDict

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import Conflict, LLMError, NotFound
from app.core.logging import get_logger
from app.db.models import (
    Assessment,
    LearnerProgress,
    Question,
    QuestionType,
    TranscriptChunk,
    Video,
    VideoStatus,
)
from app.llm import prompts
from app.llm.base import LLMProvider
from app.llm.schemas import QUESTION_SET_SCHEMAS, GeneratedQuestion, normalise_question
from app.services.grading import normalize, parse_bool

logger = get_logger(__name__)

QTYPES = [QuestionType.MCQ.value, QuestionType.TRUE_FALSE.value, QuestionType.SHORT_ANSWER.value]


# --- pure helpers (unit-tested) ------------------------------------------------------------------


def question_counts(total: int, mix: dict[str, float]) -> dict[str, int]:
    """Split `total` questions across types by `mix` fractions (largest-remainder rounding)."""
    weights = {t: max(0.0, float(mix.get(t, 0))) for t in QTYPES}
    s = sum(weights.values())
    if total <= 0 or s == 0:
        raise ValueError("need a positive number of questions and a non-zero mix")
    raw = {t: total * w / s for t, w in weights.items()}
    counts = {t: math.floor(v) for t, v in raw.items()}
    for t in sorted(raw, key=lambda t: raw[t] - counts[t], reverse=True)[: total - sum(counts.values())]:
        counts[t] += 1
    return counts


def select_representative(chunks: list[TranscriptChunk], max_chunks: int) -> list[TranscriptChunk]:
    """Round-robin across topics (in order of first appearance), evenly spaced within each topic."""
    by_topic: "OrderedDict[str, list[TranscriptChunk]]" = OrderedDict()
    for c in sorted(chunks, key=lambda c: c.chunk_index):
        by_topic.setdefault(c.topic, []).append(c)

    # How many from each topic: share max_chunks round-robin.
    quota = {t: 0 for t in by_topic}
    remaining = min(max_chunks, len(chunks))
    while remaining:
        for t, members in by_topic.items():
            if remaining and quota[t] < len(members):
                quota[t] += 1
                remaining -= 1

    picked: list[TranscriptChunk] = []
    for t, members in by_topic.items():
        k = quota[t]
        if k:
            step = len(members) / k
            picked += [members[int(i * step + step / 2)] for i in range(k)]
    return sorted(picked, key=lambda c: c.chunk_index)


def validate_questions(
    generated: list[GeneratedQuestion], chunks_by_id: dict[str, TranscriptChunk]
) -> tuple[list[GeneratedQuestion], list[str]]:
    """Enforce the semantic rules strict JSON schemas cannot express. Returns (valid, rejections)."""
    valid, rejected = [], []
    known_topics = {c.topic for c in chunks_by_id.values()}
    seen_prompts: set[str] = set()

    for q in generated:
        src = [cid for cid in q.source_chunk_ids if cid in chunks_by_id]
        if not src:
            rejected.append(f"no valid source chunk: {q.prompt[:60]}")
            continue
        if normalize(q.prompt) in seen_prompts or not q.prompt.strip():
            rejected.append(f"empty/duplicate prompt: {q.prompt[:60]}")
            continue

        update: dict = {"source_chunk_ids": src}
        # Topic must be one the report can group by; fall back to the source chunk's topic.
        if q.topic not in known_topics:
            update["topic"] = chunks_by_id[src[0]].topic

        if q.type == "mcq":
            opts = [o.strip() for o in q.options if o.strip()]
            if len(opts) != 4 or len({normalize(o) for o in opts}) != 4:
                rejected.append(f"mcq needs 4 distinct options: {q.prompt[:60]}")
                continue
            match = [o for o in opts if normalize(o) == normalize(q.correct_answer)]
            if len(match) != 1:
                rejected.append(f"mcq answer not among options: {q.prompt[:60]}")
                continue
            update |= {"options": opts, "correct_answer": match[0]}
        elif q.type == "true_false":
            b = parse_bool(q.correct_answer)
            if b is None:
                rejected.append(f"true_false answer not boolean: {q.prompt[:60]}")
                continue
            update |= {"options": [], "correct_answer": "true" if b else "false"}
        else:  # short_answer
            if not q.reference_answer.strip():
                rejected.append(f"short_answer missing reference: {q.prompt[:60]}")
                continue
            update |= {"options": [], "correct_answer": ""}

        seen_prompts.add(normalize(q.prompt))
        valid.append(q.model_copy(update=update))
    return valid, rejected


# --- service -----------------------------------------------------------------------------------


def _chunk_payload(chunks: list[TranscriptChunk], words: int = 220) -> list[dict]:
    return [
        {"id": c.id, "topic": c.topic, "text": " ".join(c.text.split()[:words])} for c in chunks
    ]


def generate_assessment(
    db: Session,
    llm: LLMProvider,
    settings: Settings,
    *,
    video_id: str,
    learner_id: str,
    num_questions: int | None = None,
    mix: dict[str, float] | None = None,
) -> Assessment:
    video = db.get(Video, video_id)
    if video is None:
        raise NotFound(f"Video {video_id} not found")
    if video.status != VideoStatus.INDEXED:
        raise Conflict(f"Video is not ready for assessment (status: {video.status})")

    progress = (
        db.query(LearnerProgress).filter_by(learner_id=learner_id, video_id=video_id).one_or_none()
    )
    watched = progress.progress if progress else 0.0
    if watched < settings.completion_threshold:
        raise Conflict(
            f"Learner has watched {watched:.0%} of this video; an assessment unlocks at "
            f"{settings.completion_threshold:.0%}. Report progress via POST /videos/{video_id}/progress.",
            details={"progress": watched, "threshold": settings.completion_threshold},
        )

    n = num_questions or settings.default_num_questions
    counts = question_counts(n, mix or settings.default_question_mix)
    chunks = db.query(TranscriptChunk).filter_by(video_id=video_id).all()
    context = select_representative(chunks, settings.question_gen_max_chunks)
    by_id = {c.id: c for c in context}

    payload = _chunk_payload(context)
    kept: list[GeneratedQuestion] = []
    rejected: list[str] = []
    topups: dict[str, int] = {}
    missing: dict[str, int] = {}

    # One focused call per question type (+ at most one top-up per type). Each call sees the
    # prompts already written so ideas are not repeated across types.
    for qtype, wanted in counts.items():
        have: list[GeneratedQuestion] = []
        for attempt in range(2):
            need = wanted - len(have)
            if need <= 0:
                break
            if attempt:
                topups[qtype] = need
                logger.info("question top-up", extra={"type": qtype, "missing": need})
            system, user = prompts.question_gen(
                video.title, payload, qtype, need, [q.prompt for q in kept + have]
            )
            result = llm.structured(system, user, QUESTION_SET_SCHEMAS[qtype])
            candidates = [normalise_question(qtype, item) for item in result.questions]
            valid, rej = validate_questions(candidates, by_id)
            rejected += rej
            seen = {normalize(q.prompt) for q in kept + have}
            have += [q for q in valid if normalize(q.prompt) not in seen][:need]
        if len(have) < wanted:
            missing[qtype] = wanted - len(have)
        kept += have

    if not kept:
        raise LLMError("The LLM did not produce any valid questions; please retry.", details=rejected)

    # Keep type order stable for the learner: mcq → true_false → short_answer.
    kept.sort(key=lambda q: QTYPES.index(q.type))
    assessment = Assessment(
        video_id=video_id,
        learner_id=learner_id,
        config={
            "requested": counts,
            "generated": {t: sum(q.type == t for q in kept) for t in QTYPES},
            "context_chunk_ids": [c.id for c in context],
            "rejected": rejected,
            "topup_requested": topups,
            "missing": missing,
            "prompt_version": prompts.PROMPT_VERSION,
        },
    )
    for i, q in enumerate(kept):
        assessment.questions.append(
            Question(
                position=i,
                type=q.type,
                prompt=q.prompt,
                options=q.options or None,
                correct_answer=q.correct_answer or None,
                reference_answer=q.reference_answer or None,
                rubric=q.rubric or None,
                explanation=q.explanation or None,
                topic=q.topic,
                difficulty=q.difficulty,
                source_chunk_ids=q.source_chunk_ids,
            )
        )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    logger.info(
        "assessment generated",
        extra={"assessment_id": assessment.id, "questions": len(kept), "missing": missing},
    )
    return assessment
