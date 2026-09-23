"""Test configuration: fully offline and isolated.

Environment is set *before* the app is imported: temp SQLite file, in-memory Qdrant, fake LLM,
hashing embeddings, local job runner, and a fake transcriber (no Whisper model download).
"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="lms-tests-"))
os.environ.update(
    DATABASE_URL=f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    QDRANT_URL="",
    QDRANT_PATH=":memory:",
    LLM_PROVIDER="fake",
    EMBEDDING_PROVIDER="fake",
    TASK_BACKEND="local",
    AUTO_MIGRATE="false",
    VIDEO_STORAGE_DIR=str(_TMP / "videos"),
    SAMPLE_VIDEOS_DIR=str(_TMP / "samples"),
    CHUNK_TARGET_WORDS="40",
    CHUNK_OVERLAP_WORDS="8",
    LOG_JSON="false",
    LOG_LEVEL="WARNING",
)
os.environ.pop("GROQ_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services import transcription, video_pipeline  # noqa: E402
from app.services.chunking import Segment  # noqa: E402
from app.services.transcription import Transcript  # noqa: E402

TOPIC_TEXT = {
    "recursion": (
        "A recursive function is a function that calls itself to solve a smaller instance of the same problem. "
        "Every recursive function needs a base case that stops the recursion, otherwise it recurses forever "
        "and the program crashes with a stack overflow. "
    ),
    "photosynthesis": (
        "Photosynthesis is the process plants use to convert light energy into chemical energy. "
        "Chlorophyll in the chloroplasts absorbs sunlight, and the plant combines carbon dioxide and water "
        "to produce glucose, releasing oxygen as a by-product. "
    ),
    "inflation": (
        "Inflation is a general rise in prices across an economy over time, which reduces the purchasing "
        "power of money. Central banks raise interest rates to cool demand when inflation runs too high. "
    ),
}


def fake_transcript(path: str) -> Transcript:
    """Deterministic 'transcription': the file content names which topics the video covers."""
    topics = Path(path).read_text(encoding="utf-8").split(",")
    segments, t = [], 0.0
    for topic in topics:
        for sentence in TOPIC_TEXT[topic.strip()].split(". "):
            if sentence.strip():
                segments.append(Segment(start=t, end=t + 6.0, text=sentence.strip() + "."))
                t += 6.0
    return Transcript(segments=segments, language="en", duration_s=t)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app.services import retrieval

    retrieval.reset_client()
    monkeypatch.setattr(transcription, "transcribe", fake_transcript)
    monkeypatch.setattr(video_pipeline, "transcribe", fake_transcript)
    from app.llm.factory import get_llm

    get_llm.cache_clear()
    yield


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_video():
    """Create a sample 'video' whose fake transcript covers the given topics."""
    from app.config import get_settings

    def _make(name: str, topics: list[str]) -> str:
        d = Path(get_settings().sample_videos_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(",".join(topics), encoding="utf-8")
        return name

    return _make


def wait_jobs():
    from app.workers.dispatch import local_runner

    local_runner().wait_all()
