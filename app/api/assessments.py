"""/assessments — generate (once the completion threshold is met) and fetch assessments."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import LLMProvider, Settings, get_db, get_llm_dep, get_settings_dep
from app.core.exceptions import NotFound
from app.db.models import Assessment
from app.schemas.api import ERROR_RESPONSES, AssessmentCreate, AssessmentOut
from app.services.question_gen import generate_assessment

router = APIRouter(prefix="/assessments", tags=["assessments"], responses=ERROR_RESPONSES)


@router.post(
    "",
    response_model=AssessmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate an assessment for a watched video",
    description=(
        "Returns **409** unless the video is indexed and the learner's progress is at or above "
        "`COMPLETION_THRESHOLD`. Generates a mix of MCQ, true/false and short-answer questions "
        "grounded in the video's transcript. Correct answers are not included in the response.\n\n"
        "**Synchronous LLM call:** typically 20-40 s (longer while Groq rate-limits and we back "
        "off). Clients and any reverse proxy in front of the API need a read timeout of >= 120 s."
    ),
)
def create_assessment(
    body: AssessmentCreate,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_dep),
    settings: Settings = Depends(get_settings_dep),
) -> Assessment:
    return generate_assessment(
        db,
        llm,
        settings,
        video_id=body.video_id,
        learner_id=body.learner_id,
        num_questions=body.num_questions,
        mix=body.mix,
    )


@router.get("/{assessment_id}", response_model=AssessmentOut, summary="Fetch an assessment's questions")
def get_assessment(assessment_id: str, db: Session = Depends(get_db)) -> Assessment:
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        raise NotFound(f"Assessment {assessment_id} not found")
    return assessment
