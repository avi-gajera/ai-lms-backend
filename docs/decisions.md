# Decision Log

Assumptions and engineering trade-offs, recorded as they were made. Newest entries are appended at the bottom.

---

## A. Assumptions

| # | Assumption | Consequence |
|---|---|---|
| A1 | There is no frontend, so there is no real watch-progress signal. | `POST /videos/{id}/progress` stands in for the signal a player would send. An assessment can only be generated once `progress >= COMPLETION_THRESHOLD` (default 0.9). |
| A2 | No auth or multi-tenancy is specified. | `learner_id` is a plain string in the request body. The API is built so that an auth dependency could supply `learner_id` later without changing routes or services. |
| A3 | "Multiple videos" means the pipeline isn't hardcoded to one file. | Everything is keyed by `video_id`. Qdrant search is always filtered by `video_id`. The worker runs with concurrency > 1. |
| A4 | Sample content is 3–5 short CC-licensed educational videos on different topics. | They are fetched ahead of time by `scripts/fetch_sample_videos.py` (yt-dlp). The API never downloads YouTube URLs itself. |
| A5 | Input is an uploaded file or a path under `sample_data/videos/`. | Path traversal outside that folder is rejected. Upload size and file extension are limited in config. |

## B. Stack decisions

### D1 — FastAPI with sync endpoints
FastAPI generates the OpenAPI/Swagger docs, which covers the API documentation deliverable. Endpoints are sync `def` because FastAPI runs them in a threadpool. This keeps async/await out of both the services and Celery. **Trade-off:** lower maximum throughput per process in exchange for simpler code, which is fine at this scale.

### D2 — SQLite (not PostgreSQL) as the relational store
- **Why:** the reviewer can open the database file directly (`data/demo.db`), no DB server is needed, and there's one less service in Docker.
- **Risk:** the API and the Celery worker both write to it. **Mitigation:** WAL journal mode (readers don't block the writer), `busy_timeout=30000`, short transactions, and `worker_prefetch_multiplier=1` with concurrency 2. At this write volume (a handful of rows per request) that is plenty.
- **Portability:** the models use only portable types: `String(36)` UUIDs, `sa.JSON`, and `String` enums that Pydantic validates. Alembic uses `render_as_batch=True`. **Switching to Postgres is a single change to `DATABASE_URL`** plus adding `psycopg`.
- **When to switch:** real concurrent users, several API replicas, or workers on more than one host. SQLite has one writer at a time and works on a single host only.

### D3 — Qdrant as the vector DB (its own service)
Qdrant is a real external vector database with payload filtering, so every search can be restricted to one `video_id`. Its embedded file mode was rejected because only one process can open the file at a time, and here both the API (retrieval) and the worker (indexing) need it. Tests use `QdrantClient(":memory:")`, so they don't need a server.

### D4 — Celery + Redis for background work
The brief asks for real background processing. FastAPI's `BackgroundTasks` jobs are lost on a restart and can't be inspected. The Celery settings are `task_acks_late=True`, `task_reject_on_worker_lost=True` and `worker_prefetch_multiplier=1`. With these, a worker killed mid-job leaves the message unacknowledged, and it is delivered again. Jobs are idempotent (D8), so running one twice is safe. `GET /tasks/{id}` exposes Celery state, and `Video.status` is the lasting record that the domain relies on.

### D5 — Transcription with faster-whisper (CPU, int8, `base` model)
CTranslate2 runs several times faster than stock Whisper on CPU, and the int8 model uses little memory. All input goes through ffmpeg first (16 kHz mono WAV), so any container or codec works. VAD filtering skips silence. **Trade-off:** `base` is less accurate than `large-v3`. The model size is a config value (`WHISPER_MODEL_SIZE`).

### D6 — Embeddings: local `all-MiniLM-L6-v2` (384-d)
There is no per-call cost, it works offline, and it's fast on CPU. It's wrapped as a LangChain `Embeddings`, so switching to an API embedding model is a factory change. Vectors are normalized and compared with cosine distance.

### D7 — LLM: Groq, called through LangChain, behind our own `LLMProvider`
- **Why Groq:** a free tier that's enough for an assessment, and very fast inference.
- **Models:** `openai/gpt-oss-120b` (default) for question generation, grading and summaries. `openai/gpt-oss-20b` (fast) for bulk topic labelling.
- **Structured output:** both models support Groq's **strict `json_schema`** mode, where decoding is constrained to the schema. Every LLM output that feeds the pipeline is a Pydantic model, so parsing can't fail on malformed JSON. A further Pydantic validation step and one retry cover problems that are about meaning rather than format.
- **Rate limits (free tier):** 30 req/min, 1K req/day, 8K tokens/min, 200K tokens/day. Each prompt plus its output is kept under about 6K tokens: question generation uses at most about 8 chunks, and topic labelling works in batches. `tenacity` retries 429 and 5xx responses with exponential backoff.
- **Abstraction:** services call `LLMProvider.structured(...)` and `LLMProvider.text(...)`. Only `app/llm/groq_provider.py` knows about Groq and LangChain. `FakeLLMProvider` makes the whole test suite deterministic and offline.

### D8 — LangChain for integrations only
This updates the original brief, which said "no LangChain." We use `ChatGroq` (structured output), `QdrantVectorStore`, `HuggingFaceEmbeddings` and `ChatPromptTemplate`. These are maintained integrations that replace glue code we would otherwise write ourselves. **We deliberately don't use LCEL chains, LangGraph or agents.** The pipeline is a straight line (ingest → index → generate → evaluate → report), so a graph or agent would add indirection without adding anything useful. Plain service functions are easier to read, test and explain.

### D9 — Idempotent, restart-safe ingestion
- Qdrant point id = `uuid5(NAMESPACE, f"{video_id}:{chunk_index}")`.
- DB chunks are upserted on `UNIQUE(video_id, chunk_index)`.
- Chunks and points left over from an earlier run with more chunks are deleted.
- **Result:** re-processing a video, or having a message delivered again after a crash, produces identical state with no duplicates.

## C. AI system design

### D10 — Chunking
Chunks are windows of about 220 words with about 40 words of overlap, and their edges fall on **Whisper segment boundaries**. That keeps every chunk's `start_time` and `end_time` exact, so reports can say where in the video to rewatch. LangChain's text splitters would lose the timestamps. **Trade-off:** chunks vary a little in size.

### D11 — Topic labelling
After chunking, the fast model labels the chunks in batches and is asked to reuse a small set of 3–8 topic names across the video. The topic is stored on each chunk and copied onto the questions made from it. This is what makes the topic-wise report a deterministic group-by. **Fallback:** if labelling fails, the topic is `"General"`. The pipeline doesn't fail because of it.

### D12 — Retrieval strategy
- **Method:** dense retrieval, always **scoped to one `video_id`**, top-k (default 5), cosine similarity.
- **Why no hybrid/BM25:** each search covers a single lecture transcript of a few dozen chunks. Dense search handles paraphrased learner questions well at this size, and BM25 would mostly help with rare exact terms. Hybrid search is the natural next step once searches span a larger corpus.
- **Question generation doesn't use similarity search.** It uses `representative_chunks()`, which spreads coverage across topics, because a quiz should test the whole video and not only what matches one query.

### D13 — Question generation
- **One structured call per assessment,** grounded in the selected chunks, whose ids are included in the prompt.
- **Prompt rules:** questions must be answerable only from the given context; they test understanding (why, how, apply) rather than recall; MCQ has 4 plausible options; each question carries a `topic` and `source_chunk_ids`.
- **Validation after generation:**
  - An MCQ's answer must be one of its options.
  - A true/false answer must be a boolean.
  - Each chunk id must exist.
  - Invalid questions are dropped, and one top-up call replaces them.
- **Synchronous:** it runs inside the request, taking a few seconds. It's documented as a choice; the same Celery pattern could be used if it got slower.

### D14 — Evaluation: rules where possible, LLM-as-judge where necessary
- **MCQ and true/false:** normalized exact matching, accepting an option letter or option text, ignoring case and whitespace. It's deterministic, free and can't hallucinate. Feedback comes from a template and includes the correct answer and the source timestamp.
- **Short answer:** one **batched** LLM-as-judge call per attempt, grounded in the question, reference answer, rubric and source chunk text.
  - The judge returns a `score` in [0,1], `feedback` and `improvement_areas`.
  - **Rubric:** 1.0 = complete and accurate; 0.5–0.9 = partly correct, with some missing or imprecise ideas; 0 = wrong or irrelevant.
  - `is_correct = score >= 0.6`.
  - Unanswered questions score 0 without an LLM call.

### D15 — Learning report
- **Computed deterministically:** overall %, per-type and per-topic accuracy and average score, strengths (topic accuracy ≥ 80%), weaknesses (< 60%), and deduplicated improvement areas.
- **One LLM call** writes the summary paragraph and suggestions, which point to video timestamps for weak topics.
- **Fallback:** if the LLM call fails, a template summary is used. The report is always created.
- This follows the same rule as everywhere else: compute what can be computed, and generate only the prose.
