"""/attempts — submit answers (evaluated immediately) and fetch the learning report."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import LLMProvider, Settings, get_db, get_llm_dep, get_settings_dep
from app.core.exceptions import NotFound, ValidationFailed
from app.db.models import Assessment, Attempt, Report
from app.schemas.api import AnswerResult, AttemptCreate, AttemptOut, ReportOut
from app.services.evaluation import evaluate_attempt
from app.services.report import build_report

router = APIRouter(prefix="/attempts", tags=["attempts"])


def _attempt_out(attempt: Attempt) -> AttemptOut:
    answers = sorted(attempt.answers, key=lambda a: a.question.position)
    return AttemptOut(
        id=attempt.id,
        assessment_id=attempt.assessment_id,
        learner_id=attempt.learner_id,
        submitted_at=attempt.submitted_at,
        total_score=attempt.total_score,
        max_score=attempt.max_score,
        percentage=round(100 * attempt.total_score / attempt.max_score, 1) if attempt.max_score else 0.0,
        answers=[
            AnswerResult(
                question_id=a.question_id, position=a.question.position, type=a.question.type,
                topic=a.question.topic, prompt=a.question.prompt, response=a.response, score=a.score,
                is_correct=a.is_correct, feedback=a.feedback, improvement_areas=a.improvement_areas,
                evaluator=a.evaluator, correct_answer=a.question.correct_answer,
                reference_answer=a.question.reference_answer,
            )
            for a in answers
        ],
        report_url=f"/attempts/{attempt.id}/report",
    )


@router.post(
    "",
    response_model=AttemptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit answers; returns per-question evaluation and generates the report",
    description=(
        "MCQ and true/false are graded deterministically; short answers are graded by an "
        "LLM judge grounded in the reference answer, rubric and source transcript. Questions "
        "without a response score 0. The learning report is generated in the same transaction."
    ),
)
def submit_attempt(
    body: AttemptCreate,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_dep),
    settings: Settings = Depends(get_settings_dep),
) -> AttemptOut:
    assessment = db.get(Assessment, body.assessment_id)
    if assessment is None:
        raise NotFound(f"Assessment {body.assessment_id} not found")
    if assessment.learner_id != body.learner_id:
        raise ValidationFailed("This assessment was generated for a different learner")
    ids = [a.question_id for a in body.answers]
    if len(ids) != len(set(ids)):
        raise ValidationFailed("Each question may be answered at most once per attempt")

    try:
        attempt = evaluate_attempt(
            db, llm, settings,
            assessment_id=body.assessment_id, learner_id=body.learner_id,
            responses={a.question_id: a.response for a in body.answers},
        )
        build_report(db, llm, settings, attempt)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(attempt)
    return _attempt_out(attempt)


@router.get("/{attempt_id}", response_model=AttemptOut, summary="Fetch an evaluated attempt")
def get_attempt(attempt_id: str, db: Session = Depends(get_db)) -> AttemptOut:
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise NotFound(f"Attempt {attempt_id} not found")
    return _attempt_out(attempt)


@router.get("/{attempt_id}/report", response_model=ReportOut, summary="Fetch the learning report for an attempt")
def get_report(attempt_id: str, db: Session = Depends(get_db)) -> Report:
    report = db.query(Report).filter_by(attempt_id=attempt_id).one_or_none()
    if report is None:
        raise NotFound(f"No report for attempt {attempt_id}")
    return report
