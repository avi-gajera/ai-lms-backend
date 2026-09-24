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
The brief asks for real background processing. FastAPI's `BackgroundTasks` jobs are lost on a restart and can't be inspected. The Celery settings are `task_acks_late=True`, `task_reject_on_worker_lost=True` and `worker_prefetch_multiplier=1`. With these, a worker killed mid-job leaves the message unacknowledged, and it is delivered again. Jobs are idempotent (D9), so running one twice is safe. `GET /tasks/{id}` exposes Celery state, and `Video.status` is the lasting record that the domain relies on.

### D5 — Transcription with faster-whisper (CPU, int8, `base` model)
CTranslate2 runs several times faster than stock Whisper on CPU, and the int8 model uses little memory. All input is decoded and resampled to 16 kHz mono, using the FFmpeg libraries bundled through PyAV (D17), so any container or codec works. VAD filtering skips silence. **Trade-off:** `base` is less accurate than `large-v3`. The model size is a config value (`WHISPER_MODEL_SIZE`).

### D6 — Embeddings: a local 384-d sentence-transformers model
The first choice was `all-MiniLM-L6-v2`; measurement replaced it with `bge-small-en-v1.5` (see D21). There is no per-call cost, it works offline, and it's fast on CPU. It's wrapped as a LangChain `Embeddings`, so switching to an API embedding model is a factory change. Vectors are normalized and compared with cosine distance.

### D7 — LLM: Groq, called through LangChain, behind our own `LLMProvider`
- **Why Groq:** a free tier that's enough for an assessment, and very fast inference.
- **Models:** `openai/gpt-oss-120b` (default) for question generation, grading and summaries. `openai/gpt-oss-20b` (fast) for bulk topic labelling.
- **Structured output:** both models support Groq's **strict `json_schema`** mode. Every LLM output that feeds the pipeline is a Pydantic model sent as a strict schema. Groq checks the output against it and rejects output that doesn't match, and those rejections are retried (D19). Pydantic validation then catches format problems, and the service validation catches problems of meaning.
- **Rate limits (free tier):** 30 req/min, 1K req/day, 8K tokens/min, 200K tokens/day. Each prompt plus its output is kept under about 6K tokens: question generation uses at most about 8 chunks, and topic labelling works in batches. `tenacity` retries 429 errors (waiting as long as `retry-after` says), 5xx errors and invalid generations (D19).
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
After chunking, the fast model labels the chunks in batches and is asked to reuse a small set of topic names across the video. The budget is about one topic per 3 chunks, between 2 and 8, and a deterministic cap enforces it (D20). The topic is stored on each chunk and copied onto the questions made from it. This is what makes the topic-wise report a deterministic group-by. **Fallback:** if labelling fails, the topic is `"General"`. The pipeline doesn't fail because of it.

### D12 — Retrieval strategy
- **Method:** dense retrieval, always **scoped to one `video_id`**, top-k (default 5), cosine similarity.
- **Why no hybrid/BM25:** each search covers a single lecture transcript of a few dozen chunks. Dense search handles paraphrased learner questions well at this size, and BM25 would mostly help with rare exact terms. Hybrid search is the natural next step once searches span a larger corpus.
- **Question generation doesn't use similarity search.** It uses `representative_chunks()`, which spreads coverage across topics, because a quiz should test the whole video and not only what matches one query.

### D13 — Question generation
- **Structured calls grounded in the selected chunks,** whose ids are included in the prompt. There is one call per question type; see D18 for why a single mixed call was abandoned.
- **Prompt rules:** questions must be answerable only from the given context; they test understanding (why, how, apply) rather than recall; MCQ has 4 plausible options; each question carries a `topic` and `source_chunk_ids`.
- **Validation after generation:**
  - An MCQ's answer must be one of its options.
  - A true/false answer must be a boolean.
  - Each chunk id must exist.
  - Invalid questions are dropped, and one top-up call per type replaces them.
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

## D. Decisions made during implementation

### D16 — Pluggable job dispatch: in-process runner locally, Celery in Docker
- `TASK_BACKEND=local` runs jobs on one background thread inside the API process. There's no Redis and no worker, and a single process can safely own Qdrant's embedded file store. `POST /videos` still returns 202 immediately.
- `TASK_BACKEND=celery` queues jobs on Redis for a separate worker.
- Both modes run the same job function (`app/workers/jobs.py`).
- **Trade-off:** local job status is kept in memory, so `/tasks/{id}` forgets it after a restart. `Video.status` in the DB is the lasting record in both modes.

### D17 — Audio decoding through faster-whisper's bundled PyAV (no system ffmpeg)
faster-whisper decodes audio and resamples it to 16 kHz mono itself, using PyAV, which bundles the FFmpeg libraries. The app needs no ffmpeg binary. The sample fetch script uses the ffmpeg binary that ships with `imageio-ffmpeg` to merge YouTube's separate video and audio streams.

### D18 — One structured-output call per question type (found in testing)
The first design asked for all 8 questions, of mixed types, in one call, using a single schema. In that schema, fields that don't apply to a type (such as `rubric` for an MCQ) had to be empty strings. Tested against real Groq, this often returned only 1–2 questions or left fields out, which failed schema validation. The design is now:
- A dedicated schema per type (`MCQSet`, `TrueFalseSet`, `ShortAnswerSet`), each with only that type's fields. True/false uses a real `bool`.
- One call per type, which is told which questions already exist, so ideas aren't repeated across types.
- At most one top-up call per type for any questions that fail validation.

The cost is up to 3–6 calls instead of 1–2. In return the output is reliable, each call is smaller, and each prompt is simpler.

### D19 — LLM resilience against real free-tier behaviour
- **Reasoning tokens count against tokens-per-minute.** `gpt-oss` models are reasoning models. `reasoning_effort` is `medium` for generation and grading and `low` for topic labelling. Both are configurable.
- **429 rate limits.** We wait for the duration in Groq's `retry-after` header, capped at 60 s, rather than guessing with exponential backoff.
- **HTTP 400 "generated JSON does not match the schema".** Groq checks structured output against the schema and rejects output that doesn't match. This is a sampling failure, not a malformed request, so it is **retried**. Only an actual "json_schema not supported" error switches that schema to tool-calling mode.
- **Input shape matters.** The judge's input list was named `items`, and the model copied that name into its output (`results: {items: [...]}`). Renaming the input to `answers_to_grade` fixed it.
- All of this is covered by unit tests with a stubbed client (`tests/unit/test_groq_provider.py`).

### D20 — Topic granularity scales with video length
The topic budget is about one topic per 3 chunks, clamped to 2–8, and the prompt says sub-aspects of one subject belong to the same topic. Without this, a 6-chunk video came out as 6 one-chunk topics ("CPI Basics", "CPI Example", "CPI Calculation"), and a per-topic report with one question per topic isn't meaningful.

### D21 — Embedding model: `bge-small-en-v1.5` instead of `all-MiniLM-L6-v2` (measured)
- **What we saw:** a real query ("Where does the oxygen released during photosynthesis come from?") ranked the chunk containing the answer only 3rd with MiniLM, behind a generic 15-second closing chunk.
- **The test:** both models ran on the real transcripts of all three sample videos, with 9 hand-written queries, each paired with a keyword that marks the relevant chunk. The harness was a small throwaway script.

  | Model | Dim | MRR | hit@1 |
  |---|---|---|---|
  | all-MiniLM-L6-v2 | 384 | 0.73 | 6/9 |
  | **bge-small-en-v1.5** | 384 | **0.89** | **8/9** |

- **The change:** BGE is the default. The dimension is the same and CPU cost is about the same. Queries get BGE's retrieval instruction prefix, and passages are embedded as they are (`EMBEDDING_QUERY_INSTRUCTION`).
- **Chunker fix:** it no longer emits a tiny trailing chunk. A remainder under half the target size is folded into the last chunk.
- **Caveats:** the evaluation set is small and the keyword labels are only a proxy for relevance. It's enough to choose between two small models, not to benchmark them.

### D22 — Docker: SQLite on a named volume, shared by the API and worker
- **Change from the plan:** the plan put `./data` on a bind mount so the `.db` file would be visible on the host.
- **Why:** SQLite's WAL mode needs working POSIX locks and shared-memory mmap on the `-wal`/`-shm` files. Across Docker Desktop's Windows/macOS file sharing these are unreliable, and two containers (API and worker) write to the file.
- **What we did:** both containers mount the same named volume `lms-data`, which is a real Linux filesystem inside the Docker VM, so locking behaves normally.
- **Getting the file out:** `docker compose cp api:/app/data/lms.db ./data/lms.db`.
- **Unchanged:** the committed `data/demo.db` snapshot is still the easy way for a reviewer to look at real data. `sample_data/videos` (read-only) and `docs/sample-outputs` are bind-mounted, because they involve no concurrent writes.
- **Other Docker choices:**
  - One image serves both the API and the worker.
  - Torch is installed from the CPU-only wheel index, which makes the image several GB smaller.
  - The Whisper and embedding models are baked in at build time.
  - The container runs as a non-root user.
  - The worker waits for the API to be healthy, so migrations have run before it writes.
  - The Qdrant image is pinned to v1.19.1, matching `qdrant-client`.

### D23 — Crash recovery on Redis needs an explicit visibility timeout (found in testing)
- **The test:** SIGKILL the Celery worker in the middle of transcription (`docker compose kill worker`), then start it again.
- **What happened first:** with `acks_late` and `reject_on_worker_lost` alone, the job was **not** re-delivered, and the video stayed `processing`. With the Redis transport, a message that is never acknowledged is only restored after the broker's `visibility_timeout`, and Kombu's default for that is 1 hour.
- **The fix:** `CELERY_VISIBILITY_TIMEOUT` is now set explicitly, with a default of 1800 s. It has to be longer than the longest job; otherwise a job that is still running gets delivered a second time, which is harmless because jobs are idempotent (D9), but wasteful.
- **Verified with a 60 s timeout:**
  - The killed job was restored and finished (`indexed`, 7 chunks) about 3 minutes after the kill.
  - A job stuck by an earlier kill, before the fix, was also restored and finished.
  - Afterwards Redis's `unacked` hash was empty.
- **Trade-off:** recovery after a crash is only as fast as the visibility timeout allows. With RabbitMQ, a dropped connection re-queues the message immediately, so it would be the better broker if fast recovery mattered.

### D24 — Groq free-tier daily quota is the practical limit for demos
- **What happened:** a full development day of runs used up the 200K tokens/day allowance on `gpt-oss-120b`. The next `POST /attempts` returned a clean **502** with Groq's reason ("tokens per day… try again in 7m56s"), because the wait exceeded the 60 s retry cap. Nothing was saved in a half-graded state, and the same request succeeded once the rolling window had moved on.
- **Cost of one full demo run:** roughly 25K tokens across 3 videos (topic labelling on the 20b model; question generation, grading and summaries on the 120b model). That is comfortably inside the free tier for a reviewer.
- **If the quota runs out:** switch `GROQ_MODEL` to `openai/gpt-oss-20b`, which has a separate quota, or use `LLM_PROVIDER=fake` to run offline.

### D25 — Production hardening from the pre-release review
A production review of the Docker stack (all 59 tests green, full flow verified against Groq) found no functional bugs, but seven gaps. All are fixed and covered by 11 new tests (`tests/api/test_http.py`), and re-verified live on the stack.
- **Upload limit enforced while streaming.** Starlette parses a multipart form, spooling the file to a temp file, *before* the endpoint runs, so the size check in `POST /videos` fired only after a 5 GB upload was already on disk. `BodySizeLimitMiddleware` (`app/core/middleware.py`, pure ASGI) now rejects a declared `Content-Length` over `MAX_UPLOAD_MB` + 1 MB (multipart headroom) before reading anything. It also counts a chunked body as it arrives and cuts it off at the limit, replacing whatever the app answered to the aborted read with a 413. The endpoint keeps its exact per-file check. It is registered inside the request-context middleware, so a 413 still gets a request id and an access-log line. *Verified live:* a 600 MB declared upload got a 413 in 17 ms with 0 bytes sent. A 700 MB chunked upload was cut off at the limit, and afterwards `/tmp` was empty.
  - *Alternative rejected:* relying only on the reverse proxy's `client_max_body_size`. It's still recommended, but the app should not depend on the deployment to protect its own disk.
- **Qdrant not published on the host.** Port 6333 was bound on all interfaces with no API key, so anything on the network could read or delete vectors. Only the api and worker need it, over the compose network. *Alternative rejected:* a Qdrant API key while keeping the port. That's more secrets to manage for a port nothing outside the stack uses.
- **Pinned dependencies.** Every entry in `requirements.txt` was `>=`, so two builds a week apart could differ. The loose files stay human-edited. `scripts/lock_requirements.sh` resolves them inside `python:3.12-slim` into `requirements.lock` (runtime) and `requirements-dev.lock` (dev-only extras, constrained by the runtime lock), and the Dockerfile installs from the locks. CPU torch is installed alone from the PyTorch index with `--no-deps`, so every other package comes from PyPI at its pinned version (no mixing of indexes). *Alternatives rejected:* pip-tools or uv, which add a tool to the toolchain for what `pip freeze` in the target image already gives exactly; and hash-pinning, a reasonable next step that was out of proportion here.
- **Dev tooling out of the runtime image.** `pytest`, `yt-dlp` and `imageio-ffmpeg` were shipped to production. The Dockerfile now has a shared `base` stage, a `dev` target (adds `requirements-dev.lock`) and a `runtime` target, which is last so a plain `docker build` produces the lean image. The runtime image is about 150 MB smaller. Tests and the demo run in a `tools` compose service (profile `tools`, dev image, same volumes and env; the demo reaches the API via `LMS_BASE_URL=http://api:8000`), so `docker compose up` never starts it. `httpx` and `requests` stay in the runtime lock because runtime libraries depend on them.
- **One error envelope everywhere.** FastAPI's `RequestValidationError` (422) and framework `HTTPException`s (unknown route 404, 405, the parse errors) returned `{"detail": ...}`, contradicting the documented envelope. Handlers now map them to `{"error": {code, message, request_id, details}}`, with pydantic's per-field errors under `details` (minus their doc URLs). The routers declare `ErrorOut` for 422, so the OpenAPI spec documents the real shape instead of `HTTPValidationError`.
- **`X-Request-ID` sanitised.** A client-supplied id is echoed in a header and written to every log line, and one of 5,000 characters was accepted. Now only `[A-Za-z0-9._:-]{1,128}` is trusted; anything else gets a fresh uuid4.
- **Synchronous LLM endpoints documented, not changed.** `POST /assessments` (20–40 s) and `POST /attempts` (5–20 s) stay synchronous, as D1 intends and the brief's flow expects. Their OpenAPI descriptions and the README's *Production notes* now state the latency and require read timeouts of at least 120 s at the proxy and client. *Alternative deferred:* moving them onto the existing job pattern (202 + poll), which is the fix if the timeouts become a real constraint.
- **Still out of scope:** authentication (assumption A-series), TLS termination, and running on multiple hosts (needs Postgres, D2).

