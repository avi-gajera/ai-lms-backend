"""Video → retrievable knowledge pipeline (runs inside the Celery task).

    pending → processing → [transcribe] → transcribed → [chunk → label topics → embed/index] → indexed
                                       ↘ failed (on a permanent error, with the error message)

Each stage commits its result, and every write is an upsert keyed by (video_id, chunk_index), so a
job that is retried or re-delivered after a worker crash converges to the same state instead of
duplicating data. A retry after transcription reuses the stored segments (no second Whisper pass).
"""

import math
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import LLMError, NotFound
from app.core.logging import get_logger
from app.db.models import TranscriptChunk, Video, VideoStatus
from app.llm import prompts
from app.llm.base import LLMProvider
from app.llm.schemas import TopicLabels
from app.services import retrieval
from app.services.chunking import Chunk, Segment, chunk_segments
from app.services.transcription import Transcript, transcribe

logger = get_logger(__name__)

DEFAULT_TOPIC = "General"


def label_topics(
    llm: LLMProvider, title: str, chunks: list[Chunk], batch_size: int
) -> dict[int, str]:
    """Topic per chunk_index via the fast model; falls back to DEFAULT_TOPIC on any LLM failure."""
    topics: dict[int, str] = {}
    known: list[str] = []
    # ~one topic per 3 chunks, between 2 and 8: enough granularity for a useful topic-wise report,
    # few enough that each topic gets several questions.
    max_topics = max(2, min(8, math.ceil(len(chunks) / 3)))
    try:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            payload = [{"chunk_index": c.index, "text": " ".join(c.text.split()[:120])} for c in batch]
            system, user = prompts.topic_label(title, payload, known, max_topics)
            result = llm.structured(system, user, TopicLabels, tier="fast")
            for label in result.labels:
                name = " ".join(label.topic.split())[:120] or DEFAULT_TOPIC
                # Canonicalise case-variants of an existing topic ("recursion basics" == "Recursion Basics").
                name = next((k for k in known if k.lower() == name.lower()), name)
                topics[label.chunk_index] = name
                if name not in known:
                    known.append(name)
    except LLMError as exc:
        logger.warning("topic labelling failed; using default topic", extra={"error": exc.message})
        return {c.index: DEFAULT_TOPIC for c in chunks}
    labels = [topics.get(c.index, DEFAULT_TOPIC) for c in chunks]
    return dict(zip((c.index for c in chunks), cap_topics(labels, max_topics)))


def cap_topics(labels: list[str], max_topics: int) -> list[str]:
    """Deterministically enforce the topic budget the prompt asked for: while there are too many
    topics, fold the rarest one into the topic of its neighbouring chunk (transcripts are
    sequential, so a neighbour is the most likely parent subject)."""
    labels = list(labels)
    while len(set(labels)) > max(1, max_topics):
        counts = {t: labels.count(t) for t in dict.fromkeys(labels)}
        rarest = min(counts, key=lambda t: (counts[t], -labels.index(t)))
        i = labels.index(rarest)
        neighbours = [labels[j] for j in (i - 1, i + 1) if 0 <= j < len(labels) and labels[j] != rarest]
        target = max(neighbours, key=lambda t: counts[t]) if neighbours else next(t for t in counts if t != rarest)
        labels = [target if t == rarest else t for t in labels]
    return labels


def _upsert_chunks(db: Session, video: Video, chunks: list[Chunk], topics: dict[int, str]) -> list[TranscriptChunk]:
    existing = {c.chunk_index: c for c in db.query(TranscriptChunk).filter_by(video_id=video.id)}
    rows: list[TranscriptChunk] = []
    for c in chunks:
        row = existing.pop(c.index, None) or TranscriptChunk(video_id=video.id, chunk_index=c.index)
        row.text, row.start_time, row.end_time = c.text, c.start, c.end
        row.topic = topics[c.index]
        row.qdrant_point_id = retrieval.point_id(video.id, c.index)
        db.add(row)
        rows.append(row)
    for stale in existing.values():  # a previous run produced more chunks
        db.delete(stale)
    db.flush()
    return rows


def _set_status(db: Session, video: Video, status: VideoStatus, error: str | None = None) -> None:
    video.status = status
    video.error = error
    db.commit()


def run(
    db: Session,
    llm: LLMProvider,
    settings: Settings,
    video_id: str,
    transcriber: Callable[[str], Transcript] | None = None,
) -> Video:
    transcriber = transcriber or transcribe
    video = db.get(Video, video_id)
    if video is None:
        raise NotFound(f"Video {video_id} not found")
    t0 = time.perf_counter()
    _set_status(db, video, VideoStatus.PROCESSING)

    # 1. Transcribe (skipped if a previous attempt already stored the segments).
    if video.transcript_segments:
        segments = [Segment(**s) for s in video.transcript_segments]
        logger.info("reusing stored transcript", extra={"video_id": video_id})
    else:
        tr = transcriber(video.source_path)
        segments = tr.segments
        video.transcript_segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
        video.transcript_text = tr.text
        video.language = tr.language
        video.duration_s = tr.duration_s
        logger.info(
            "transcribed",
            extra={"video_id": video_id, "segments": len(segments), "language": tr.language,
                   "duration_s": round(tr.duration_s, 1), "elapsed_s": round(time.perf_counter() - t0, 1)},
        )
    _set_status(db, video, VideoStatus.TRANSCRIBED)
    if not segments:
        raise ValueError("No speech was detected in this video")

    # 2. Chunk + label topics.
    chunks = chunk_segments(segments, settings.chunk_target_words, settings.chunk_overlap_words)
    topics = label_topics(llm, video.title, chunks, settings.topic_label_batch_size)

    # 3. Persist chunks (upsert) and index vectors (idempotent point ids), then mark indexed.
    rows = _upsert_chunks(db, video, chunks, topics)
    retrieval.index_chunks(
        video.id,
        [
            retrieval.IndexedChunk(
                chunk_id=r.id, video_id=video.id, chunk_index=r.chunk_index, text=r.text,
                start_time=r.start_time, end_time=r.end_time, topic=r.topic,
            )
            for r in rows
        ],
    )
    _set_status(db, video, VideoStatus.INDEXED)
    logger.info(
        "video indexed",
        extra={"video_id": video_id, "chunks": len(rows), "topics": sorted(set(topics.values())),
               "elapsed_s": round(time.perf_counter() - t0, 1)},
    )
    return video


def mark_failed(db: Session, video_id: str, error: str) -> None:
    video = db.get(Video, video_id)
    if video is not None:
        _set_status(db, video, VideoStatus.FAILED, error[:2000])
