"""Answer evaluation: rules for closed questions, one batched LLM-as-judge call for short answers.

Everything is graded in memory first and persisted in one transaction, so an LLM failure leaves
no half-graded attempt behind (the client simply retries).
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import LLMError, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.models import Answer, Assessment, Attempt, Question, QuestionType, TranscriptChunk
from app.llm import prompts
from app.llm.base import LLMProvider
from app.llm.schemas import JudgedAnswers
from app.services.grading import fmt_ts, grade_mcq, grade_true_false

logger = get_logger(__name__)

JUDGE_BATCH_SIZE = 5  # keeps each judge prompt well inside Groq's free-tier tokens/minute


@dataclass
class Graded:
    question: Question
    response: str | None
    score: float = 0.0
    is_correct: bool = False
    feedback: str = ""
    improvement_areas: list[str] = field(default_factory=list)
    evaluator: str = "rule"


def _where(q: Question, chunks: dict[str, TranscriptChunk]) -> str:
    src = [chunks[c] for c in q.source_chunk_ids if c in chunks]
    if not src:
        return ""
    return f" (see {fmt_ts(min(c.start_time for c in src))}-{fmt_ts(max(c.end_time for c in src))} in the video)"


def _quote(text: str | None) -> str:
    return f'"{(text or "").strip().rstrip(".")}"'


def grade_closed(q: Question, response: str | None, chunks: dict[str, TranscriptChunk]) -> Graded:
    g = Graded(question=q, response=response)
    explanation = f" {q.explanation}" if q.explanation else ""
    if q.type == QuestionType.MCQ:
        ok, chosen = grade_mcq(response, q.options or [], q.correct_answer or "")
        g.is_correct, g.score = ok, 1.0 if ok else 0.0
        if ok:
            g.feedback = f"Correct.{explanation}"
        else:
            picked = f"You chose {_quote(chosen)}. " if chosen else "Your response did not match any option. "
            g.feedback = f"Incorrect. {picked}The correct answer is {_quote(q.correct_answer)}.{explanation}{_where(q, chunks)}"
    else:
        ok, given = grade_true_false(response, q.correct_answer or "")
        g.is_correct, g.score = ok, 1.0 if ok else 0.0
        if ok:
            g.feedback = f"Correct — the statement is {q.correct_answer}.{explanation}"
        else:
            said = "" if given is None else f"You answered {str(given).lower()}. "
            g.feedback = f"Incorrect. {said}The statement is {q.correct_answer}.{explanation}{_where(q, chunks)}"
    if not ok:
        g.improvement_areas = [f"{q.topic}: revisit this concept{_where(q, chunks)}".strip()]
    return g


def judge_short_answers(
    llm: LLMProvider,
    items: list[Graded],
    chunks: dict[str, TranscriptChunk],
    pass_threshold: float,
) -> None:
    """Grade short answers in batches with the LLM judge; mutates `items` in place."""
    for start in range(0, len(items), JUDGE_BATCH_SIZE):
        batch = items[start : start + JUDGE_BATCH_SIZE]
        payload = [
            {
                "question_id": g.question.id,
                "question": g.question.prompt,
                "topic": g.question.topic,
                "reference_answer": g.question.reference_answer or "",
                "rubric": g.question.rubric or "",
                "source_context": " ".join(
                    " ".join(chunks[c].text.split()[:200]) for c in g.question.source_chunk_ids if c in chunks
                ),
                "response": g.response or "",
            }
            for g in batch
        ]
        system, user = prompts.judge(payload)
        result = llm.structured(system, user, JudgedAnswers)
        by_id = {r.question_id: r for r in result.results}
        missing = [g.question.id for g in batch if g.question.id not in by_id]
        if missing:
            raise LLMError("The grader did not return a result for every answer; please retry.", details=missing)
        for g in batch:
            r = by_id[g.question.id]
            g.evaluator = "llm"
            g.score = round(min(1.0, max(0.0, r.score)), 2)
            g.is_correct = g.score >= pass_threshold
            g.feedback = r.feedback.strip()
            g.improvement_areas = [a.strip() for a in r.improvement_areas if a.strip()]
            if not g.is_correct and not g.improvement_areas:
                g.improvement_areas = [f"{g.question.topic}: revisit this concept{_where(g.question, chunks)}"]


def evaluate_attempt(
    db: Session,
    llm: LLMProvider,
    settings: Settings,
    *,
    assessment_id: str,
    learner_id: str,
    responses: dict[str, str | None],
) -> Attempt:
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        raise NotFound(f"Assessment {assessment_id} not found")
    questions = {q.id: q for q in assessment.questions}
    unknown = [qid for qid in responses if qid not in questions]
    if unknown:
        raise ValidationFailed("Answers reference questions not in this assessment", details=unknown)

    chunk_ids = {cid for q in questions.values() for cid in q.source_chunk_ids}
    chunks = {c.id: c for c in db.query(TranscriptChunk).filter(TranscriptChunk.id.in_(chunk_ids))}

    graded: list[Graded] = []
    to_judge: list[Graded] = []
    for q in assessment.questions:
        resp = responses.get(q.id)
        if resp is None or not str(resp).strip():
            graded.append(Graded(
                question=q, response=None, evaluator="none",
                feedback="Not attempted." + (f" Reference answer: {q.reference_answer}" if q.reference_answer else ""),
                improvement_areas=[f"{q.topic}: question left unanswered{_where(q, chunks)}"],
            ))
        elif q.type == QuestionType.SHORT_ANSWER:
            g = Graded(question=q, response=str(resp), evaluator="llm")
            graded.append(g)
            to_judge.append(g)
        else:
            graded.append(grade_closed(q, str(resp), chunks))

    if to_judge:
        judge_short_answers(llm, to_judge, chunks, settings.short_answer_pass_threshold)

    attempt = Attempt(
        assessment_id=assessment.id,
        learner_id=learner_id,
        total_score=round(sum(g.score for g in graded), 2),
        max_score=float(len(graded)),
    )
    for g in graded:
        attempt.answers.append(Answer(
            question_id=g.question.id,
            response=g.response,
            score=g.score,
            is_correct=g.is_correct,
            feedback=g.feedback,
            improvement_areas=g.improvement_areas,
            evaluator=g.evaluator,
        ))
    db.add(attempt)
    db.flush()  # assign ids; the caller commits together with the report
    logger.info(
        "attempt evaluated",
        extra={"attempt_id": attempt.id, "score": attempt.total_score, "max": attempt.max_score,
               "llm_graded": len(to_judge)},
    )
    return attempt
