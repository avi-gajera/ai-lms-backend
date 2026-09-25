"""Background job dispatch, independent of the execution backend.

TASK_BACKEND=celery → enqueue on Redis; a separate Celery worker runs it (Docker / production).
TASK_BACKEND=local  → run on a background thread in this process (local dev: no Redis, and a single
                      process can safely own the embedded Qdrant store). Status lives in memory,
                      so it does not survive a restart — `Video.status` in the DB always does.

Either way the API returns immediately with a task id that `GET /tasks/{id}` can poll.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TaskStatus:
    task_id: str
    state: str  # PENDING | STARTED | SUCCESS | FAILURE | RETRY | UNKNOWN
    result: Any = None
    error: str | None = None
    backend: str = "local"


@dataclass
class _LocalTask:
    state: str = "PENDING"
    result: Any = None
    error: str | None = None
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


class LocalRunner:
    """One worker thread: sequential jobs keep SQLite writes and Whisper CPU usage predictable."""

    def __init__(self, max_workers: int = 1):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lms-job")
        self._tasks: dict[str, _LocalTask] = {}
        self._lock = threading.Lock()

    def submit(self, fn, *args) -> str:
        task_id = str(uuid.uuid4())
        with self._lock:
            self._tasks[task_id] = _LocalTask()
        self._pool.submit(self._run, task_id, fn, *args)
        return task_id

    def _run(self, task_id: str, fn, *args) -> None:
        from app.workers.jobs import mark_video_failed

        self._tasks[task_id].state = "STARTED"
        try:
            self._tasks[task_id].result = fn(*args, task_id=task_id)
            self._tasks[task_id].state = "SUCCESS"
        except Exception as exc:
            logger.exception("local job failed", extra={"task_id": task_id})
            self._tasks[task_id].state = "FAILURE"
            self._tasks[task_id].error = f"{type(exc).__name__}: {exc}"
            if args:
                mark_video_failed(args[0], self._tasks[task_id].error)

    def status(self, task_id: str) -> TaskStatus:
        t = self._tasks.get(task_id)
        if t is None:
            return TaskStatus(task_id=task_id, state="UNKNOWN")
        return TaskStatus(task_id=task_id, state=t.state, result=t.result, error=t.error)

    def wait_all(self, timeout: float | None = None) -> None:
        """Test helper: block until all submitted jobs are done."""
        self._pool.shutdown(wait=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lms-job")


_local_runner: LocalRunner | None = None


def local_runner() -> LocalRunner:
    global _local_runner
    if _local_runner is None:
        _local_runner = LocalRunner()
    return _local_runner


def enqueue_process_video(video_id: str) -> str:
    if get_settings().task_backend == "celery":
        from app.workers.tasks import process_video

        return process_video.delay(video_id).id

    from app.workers.jobs import process_video_job

    return local_runner().submit(process_video_job, video_id)


def get_task_status(task_id: str) -> TaskStatus:
    if get_settings().task_backend == "celery":
        from celery.result import AsyncResult

        from app.workers.celery_app import celery_app

        r = AsyncResult(task_id, app=celery_app)
        error = str(r.result) if r.state in ("FAILURE", "RETRY") else None
        result = r.result if r.state == "SUCCESS" else None
        return TaskStatus(task_id=task_id, state=r.state, result=result, error=error, backend="celery")

    return local_runner().status(task_id)
