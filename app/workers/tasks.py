"""Celery task wrappers around `app.workers.jobs`."""

from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.core.logging import get_logger, setup_worker_logging
from app.workers.celery_app import celery_app
from app.workers.jobs import mark_video_failed, process_video_job

logger = get_logger(__name__)
setup_worker_logging()

# Infrastructure hiccups worth retrying. Everything else (bad media file, no speech, bug) is
# permanent: the video is marked failed immediately with the error message.
TRANSIENT = (OperationalError, ResponseHandlingException, UnexpectedResponse, ConnectionError, TimeoutError)


@celery_app.task(bind=True, name="process_video")
def process_video(self, video_id: str) -> dict:
    max_retries = get_settings().task_max_retries
    try:
        return process_video_job(video_id, task_id=self.request.id)
    except TRANSIENT as exc:
        if self.request.retries < max_retries:
            countdown = 5 * 2**self.request.retries
            logger.warning(
                "transient failure; retrying",
                extra={"video_id": video_id, "retry": self.request.retries + 1, "in_s": countdown, "error": str(exc)[:300]},
            )
            raise self.retry(exc=exc, countdown=countdown, max_retries=max_retries)
        mark_video_failed(video_id, f"{type(exc).__name__}: {exc}")
        raise
    except Exception as exc:
        logger.exception("process_video failed", extra={"video_id": video_id})
        mark_video_failed(video_id, f"{type(exc).__name__}: {exc}")
        raise
