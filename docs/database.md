# Database schema

- **Relational data:** SQLite (`data/lms.db`, WAL mode), managed with SQLAlchemy 2 and Alembic. The migrations are in `alembic/versions/`, and `alembic upgrade head` creates the schema. The API also runs the migrations automatically at startup.
- **Vectors:** stored in Qdrant, in the collection `transcript_chunks` (see the bottom of this page).

```mermaid
erDiagram
    videos ||--o{ transcript_chunks : "has"
    videos ||--o{ learner_progress : "watched by"
    videos ||--o{ assessments : "assessed by"
    assessments ||--o{ questions : "contains"
    assessments ||--o{ attempts : "attempted in"
    attempts ||--o{ answers : "contains"
    questions ||--o{ answers : "answered by"
    attempts ||--|| reports : "summarised by"

    videos {
        string id PK "uuid"
        string title
        string source_path
        string status "pending|processing|transcribed|indexed|failed"
        text error
        float duration_s
        string language
        text transcript_text
        json transcript_segments "whisper segments; lets retries skip re-transcription"
        string celery_task_id
        datetime created_at
        datetime updated_at
    }
    transcript_chunks {
        string id PK
        string video_id FK
        int chunk_index "UNIQUE(video_id, chunk_index)"
        text text
        float start_time
        float end_time
        string topic "LLM-labelled; drives topic-wise reports"
        string qdrant_point_id "uuid5(video_id:chunk_index)"
    }
    learner_progress {
        string id PK
        string learner_id "UNIQUE(learner_id, video_id)"
        string video_id FK
        float progress "0-1, never decreases"
        datetime updated_at
    }
    assessments {
        string id PK
        string video_id FK
        string learner_id
        json config "requested/generated mix, context chunk ids, rejected questions, prompt version"
        datetime created_at
    }
    questions {
        string id PK
        string assessment_id FK
        int position
        string type "mcq|true_false|short_answer"
        text prompt
        json options "mcq only"
        text correct_answer "mcq option text / true|false"
        text reference_answer
        text rubric "short_answer grading key points"
        text explanation
        string topic
        string difficulty
        json source_chunk_ids "grounding"
    }
    attempts {
        string id PK
        string assessment_id FK
        string learner_id
        datetime submitted_at
        float total_score
        float max_score
    }
    answers {
        string id PK
        string attempt_id FK "UNIQUE(attempt_id, question_id)"
        string question_id FK
        text response
        float score "0-1"
        bool is_correct
        text feedback
        json improvement_areas
        string evaluator "rule|llm|none"
    }
    reports {
        string id PK
        string attempt_id FK "unique"
        json overall
        json by_type
        json topic_breakdown
        json strengths
        json weaknesses
        json improvement_areas
        json suggestions
        text ai_summary
        string summary_source "llm|template"
        datetime created_at
    }
```

## Design notes

- **Portable types only.** IDs are UUID strings, and structured fields use `JSON`. The schema runs unchanged on PostgreSQL: set `DATABASE_URL=postgresql+psycopg://…` and install `psycopg`.
- **Idempotency keys.**
  - `UNIQUE(video_id, chunk_index)` lets re-processing a video upsert its chunks instead of duplicating them.
  - `UNIQUE(learner_id, video_id)` gives each learner exactly one progress row per video.
  - `UNIQUE(attempt_id, question_id)` allows one answer per question per attempt.
- **Cascades.** Deleting a video deletes its chunks, progress, assessments, attempts and reports.
- **Answers are not exposed.** `questions.correct_answer`, `reference_answer` and `rubric` are stored for grading. `GET /assessments/{id}` never returns them. `POST /attempts` reveals them only after grading.

## Vector store (Qdrant)

| Field | Value |
|---|---|
| Collection | `transcript_chunks`, one for all videos |
| Vector | 384-d, cosine distance, `BAAI/bge-small-en-v1.5` (normalized; queries carry the BGE instruction prefix) |
| Point id | `uuid5(namespace, "{video_id}:{chunk_index}")`, so the same chunk always maps to the same point |
| Payload | `page_content` (chunk text), plus `metadata.{video_id, chunk_id, chunk_index, start_time, end_time, topic}` |
| Payload indexes | `metadata.video_id` (keyword) and `metadata.chunk_index` (integer), on a Qdrant server |

Every search is filtered by `metadata.video_id`.
