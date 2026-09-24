"""Celery application (used when TASK_BACKEND=celery, e.g. in Docker).

Reliability settings:
- task_acks_late + task_reject_on_worker_lost: the message is acknowledged only after the task
  finishes, so if the worker dies mid-job the broker re-delivers it (jobs are idempotent) — on
  Redis, once the visibility timeout expires.
- worker_prefetch_multiplier=1: a worker holds one long job at a time instead of hoarding several.
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "lms",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=60 * 60 * 24 * 7,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    # With the Redis transport, acks_late only helps if un-acked messages are eventually restored:
    # that happens after the visibility timeout (see Settings.celery_visibility_timeout).
    broker_transport_options={"visibility_timeout": settings.celery_visibility_timeout},
    result_backend_transport_options={"visibility_timeout": settings.celery_visibility_timeout},
)
