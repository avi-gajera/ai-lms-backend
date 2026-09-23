"""/tasks — poll background job status."""

from fastapi import APIRouter

from app.schemas.api import TaskOut
from app.workers.dispatch import get_task_status

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskOut, summary="Background job status")
def get_task(task_id: str) -> TaskOut:
    """States: PENDING, STARTED, RETRY, SUCCESS, FAILURE (UNKNOWN for an id this process never saw
    in local mode). The video's own `status` field is the durable record."""
    s = get_task_status(task_id)
    return TaskOut(task_id=s.task_id, state=s.state, backend=s.backend, result=s.result, error=s.error)
