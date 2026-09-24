"""Application settings, loaded from environment variables / `.env`.

Every tunable of the system lives here so behaviour is a config change, not a code change.
Defaults target *local development without any external services*:
SQLite file DB, embedded Qdrant (file mode) and an in-process background job runner.
Docker Compose overrides these to use Redis + Celery worker + a Qdrant server.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "AI-Powered LMS"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Relational DB (SQLite by default; Postgres = change the URL) ---
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'lms.db').as_posix()}"
    # Run `alembic upgrade head` when the API starts (convenient locally; Docker does it too).
    auto_migrate: bool = True

    # --- Background jobs ---
    # "local"  => jobs run on a background thread inside the API process (no Redis/worker needed).
    # "celery" => jobs are queued on Redis and executed by a separate Celery worker (Docker).
    task_backend: Literal["local", "celery"] = "local"
    redis_url: str = "redis://localhost:6379/0"
    task_max_retries: int = 3
    # Redis broker: an un-acked message (worker died mid-job) is re-delivered after this many
    # seconds. Must exceed the longest job, or a slow job is delivered twice (harmless — jobs are
    # idempotent — but wasteful). Kombu's default is 1 hour.
    celery_visibility_timeout: int = 1800

    # --- Vector DB ---
    # If qdrant_url is set, use a Qdrant server; otherwise use embedded file mode at qdrant_path
    # (":memory:" for tests). Embedded mode is single-process: fine for local eager mode only.
    qdrant_url: str | None = None
    qdrant_path: str = str(PROJECT_ROOT / "data" / "qdrant")
    qdrant_collection: str = "transcript_chunks"

    # --- LLM (Groq via LangChain, behind LLMProvider) ---
    llm_provider: Literal["groq", "fake"] = "groq"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_model_fast: str = "openai/gpt-oss-20b"
    llm_temperature: float = 0.2
    # gpt-oss / qwen are reasoning models: hidden reasoning counts against the tokens-per-minute
    # limit. "medium" for generation/grading quality, "low" for bulk labelling. Empty = don't send
    # the parameter (use for non-reasoning models such as Llama).
    llm_reasoning_effort: str = "medium"
    llm_reasoning_effort_fast: str = "low"
    llm_max_attempts: int = 6
    llm_timeout_s: float = 60.0

    # --- Transcription ---
    whisper_model_size: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # --- Embeddings ---
    embedding_provider: Literal["huggingface", "fake"] = "huggingface"
    # bge-small beat all-MiniLM-L6-v2 on our transcripts (MRR 0.89 vs 0.73) at the same size (D21).
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # Instruction prepended to *queries only* (BGE is trained with it); empty for models without one.
    embedding_query_instruction: str = "Represent this sentence for searching relevant passages: "

    # --- Video input ---
    video_storage_dir: str = str(PROJECT_ROOT / "data" / "videos")
    sample_videos_dir: str = str(PROJECT_ROOT / "sample_data" / "videos")
    max_upload_mb: int = 500
    allowed_video_extensions: list[str] = Field(
        default=[".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav"]
    )

    # --- Chunking / retrieval ---
    chunk_target_words: int = 220
    chunk_overlap_words: int = 40
    retrieval_top_k: int = 5
    topic_label_batch_size: int = 20

    # --- Assessment ---
    completion_threshold: float = 0.9
    default_num_questions: int = 8
    # Fractions per question type; normalised and rounded when an assessment is generated.
    default_question_mix: dict[str, float] = Field(
        default={"mcq": 0.4, "true_false": 0.2, "short_answer": 0.4}
    )
    question_gen_max_chunks: int = 8

    # --- Evaluation / report ---
    short_answer_pass_threshold: float = 0.6
    strength_threshold: float = 0.8
    weakness_threshold: float = 0.6


@lru_cache
def get_settings() -> Settings:
    return Settings()
