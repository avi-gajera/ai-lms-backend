"""/videos — register + process videos, report watch progress, ad-hoc retrieval."""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import Settings, get_db, get_settings_dep
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.models import LearnerProgress, TranscriptChunk, Video, VideoStatus
from app.schemas.api import (
    ERROR_RESPONSES,
    ChunkOut,
    ProgressIn,
    ProgressOut,
    RetrievedChunkOut,
    RetrieveIn,
    RetrieveOut,
    VideoAccepted,
    VideoOut,
)
from app.services import retrieval
from app.services.grading import fmt_ts
from app.workers.dispatch import enqueue_process_video

router = APIRouter(prefix="/videos", tags=["videos"], responses=ERROR_RESPONSES)
logger = get_logger(__name__)


def _get_video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise NotFound(f"Video {video_id} not found")
    return video


def _video_out(video: Video) -> VideoOut:
    topics = list(dict.fromkeys(c.topic for c in video.chunks))
    return VideoOut(
        id=video.id, title=video.title, status=video.status, error=video.error,
        duration_s=video.duration_s, language=video.language, chunk_count=len(video.chunks),
        topics=topics, created_at=video.created_at, updated_at=video.updated_at,
    )


def _resolve_sample(sample_path: str, settings: Settings) -> Path:
    """Only files inside SAMPLE_VIDEOS_DIR may be referenced (prevents path traversal)."""
    base = Path(settings.sample_videos_dir).resolve()
    candidate = (base / sample_path).resolve()
    if not candidate.is_relative_to(base):
        raise ValidationFailed("sample_path must be inside the sample videos directory")
    if not candidate.is_file():
        raise NotFound(f"Sample video '{sample_path}' not found in {settings.sample_videos_dir}")
    return candidate


def _check_extension(name: str, settings: Settings) -> str:
    ext = Path(name).suffix.lower()
    if ext not in settings.allowed_video_extensions:
        raise ValidationFailed(
            f"Unsupported file type '{ext}'", details={"allowed": settings.allowed_video_extensions}
        )
    return ext


def _save_upload(upload: UploadFile, settings: Settings) -> Path:
    ext = _check_extension(upload.filename or "", settings)
    dest_dir = Path(settings.video_storage_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4()}{ext}"
    limit, written = settings.max_upload_mb * 1024 * 1024, 0
    with dest.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise ValidationFailed(f"File exceeds the {settings.max_upload_mb} MB upload limit")
            out.write(chunk)
    return dest


@router.post(
    "",
    response_model=VideoAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Register a video and queue it for processing",
    description=(
        "Provide **either** a multipart `file` upload **or** `sample_path` (a file name inside "
        "`sample_data/videos/`). Processing (transcription → chunking → topic labelling → "
        "embedding) runs in the background; poll `status_url` or `task_url`. Bodies over "
        "`MAX_UPLOAD_MB` are rejected with **413** as they stream in."
    ),
)
def create_video(
    file: UploadFile | None = File(default=None),
    sample_path: str | None = Form(default=None),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> VideoAccepted:
    if bool(file and file.filename) == bool(sample_path):
        raise ValidationFailed("Provide exactly one of `file` or `sample_path`")

    if sample_path:
        path = _resolve_sample(sample_path, settings)
        _check_extension(path.name, settings)
        default_title = path.stem
    else:
        path = _save_upload(file, settings)  # type: ignore[arg-type]
        default_title = Path(file.filename).stem  # type: ignore[union-attr]

    video = Video(title=(title or default_title.replace("_", " ")).strip()[:255], source_path=str(path))
    db.add(video)
    db.commit()

    task_id = enqueue_process_video(video.id)
    video.celery_task_id = task_id
    db.commit()
    logger.info("video queued", extra={"video_id": video.id, "task_id": task_id})
    return VideoAccepted(
        video_id=video.id, task_id=task_id, status=video.status,
        status_url=f"/videos/{video.id}", task_url=f"/tasks/{task_id}",
    )


@router.get("", response_model=list[VideoOut], summary="List videos")
def list_videos(db: Session = Depends(get_db)) -> list[VideoOut]:
    return [_video_out(v) for v in db.query(Video).order_by(Video.created_at.desc())]


@router.get("/{video_id}", response_model=VideoOut, summary="Video status and metadata")
def get_video(video_id: str, db: Session = Depends(get_db)) -> VideoOut:
    return _video_out(_get_video(db, video_id))


@router.get("/{video_id}/chunks", response_model=list[ChunkOut], summary="Transcript chunks with topics and timestamps")
def get_chunks(video_id: str, db: Session = Depends(get_db)) -> list[TranscriptChunk]:
    return _get_video(db, video_id).chunks


@router.post("/{video_id}/progress", response_model=ProgressOut, summary="Report learner watch progress")
def report_progress(
    video_id: str,
    body: ProgressIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ProgressOut:
    """Simulates the signal a video player would send. Progress never decreases."""
    _get_video(db, video_id)
    row = db.query(LearnerProgress).filter_by(learner_id=body.learner_id, video_id=video_id).one_or_none()
    if row is None:
        row = LearnerProgress(learner_id=body.learner_id, video_id=video_id, progress=0.0)
        db.add(row)
    row.progress = max(row.progress, body.progress)
    db.commit()
    return ProgressOut(
        video_id=video_id, learner_id=body.learner_id, progress=row.progress,
        completion_threshold=settings.completion_threshold,
        assessment_unlocked=row.progress >= settings.completion_threshold,
    )


@router.post("/{video_id}/retrieve", response_model=RetrieveOut, summary="Semantic search within one video")
def retrieve(
    video_id: str,
    body: RetrieveIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RetrieveOut:
    video = _get_video(db, video_id)
    if video.status != VideoStatus.INDEXED:
        raise Conflict(f"Video is not indexed yet (status: {video.status})")
    hits = retrieval.search(video_id, body.query, body.top_k or settings.retrieval_top_k)
    return RetrieveOut(
        video_id=video_id,
        query=body.query,
        results=[
            RetrievedChunkOut(**h.__dict__, timestamp=f"{fmt_ts(h.start_time)}-{fmt_ts(h.end_time)}")
            for h in hits
        ],
    )
