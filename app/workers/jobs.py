"""Job bodies — plain functions shared by the Celery task and the local runner.

Keeping the work here (not inside the Celery decorator) means the same code path runs in both
execution modes and is directly callable from tests.
"""

import uuid

from app.config import get_settings
from app.core.logging import get_logger, request_id_var
from app.db.session import SessionLocal
from app.llm.factory import get_llm
from app.services import video_pipeline

logger = get_logger(__name__)


def process_video_job(video_id: str, task_id: str | None = None) -> dict:
    token = request_id_var.set(task_id or str(uuid.uuid4()))
    db = SessionLocal()
    try:
        logger.info("job started", extra={"job": "process_video", "video_id": video_id})
        video = video_pipeline.run(db, get_llm(), get_settings(), video_id)
        return {"video_id": video.id, "status": video.status, "chunks": len(video.chunks)}
    finally:
        db.close()
        request_id_var.reset(token)


def mark_video_failed(video_id: str, error: str) -> None:
    db = SessionLocal()
    try:
        video_pipeline.mark_failed(db, video_id, error)
    finally:
        db.close()
