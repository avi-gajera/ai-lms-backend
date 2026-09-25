# 10-minute walkthrough

A script for explaining the system out loud, in order. Each step says what to show and what to say. Decision numbers (D*n*) point to [decisions.md](decisions.md).

**Before you start:** the stack is running (`docker compose up`, or `uvicorn app.main:app` locally) and at least one sample video has been processed, because ingestion takes 1–2 minutes per video. The recorded results of a full run are in [`sample-outputs/`](sample-outputs/), so every step can also be shown from files if Groq is rate-limited.

---

## 1. The problem and the shape of the answer (1 min)

**Show:** the architecture diagram in the [README](../README.md#architecture).

**Say:**
- A video becomes knowledge you can search, and that knowledge is used to assess the learner: *ingest → index → generate → evaluate → report*.
- The only slow step, ingestion, runs in the background. Everything else is an ordinary request.
- **The guiding rule:** compute what can be computed, and use the LLM only for generation, open-ended grading and prose (D14, D15).

## 2. Ingestion (1.5 min)

**Show:** `POST /videos` in Swagger (`/docs`) with `sample_path=inflation_khan.mp4`. It returns **202** and a `task_id`. Then `GET /videos/{id}` until the status is `indexed`, and `GET /videos/{id}/chunks`.

**Say:**
- faster-whisper transcribes on the CPU, with no system ffmpeg (D5, D17).
- Chunks are cut on Whisper segment boundaries, so every chunk has exact timestamps. That is what lets the report say "rewatch 01:15–02:31" (D10).
- The fast model labels topics, within a budget that scales with the video's length (D11, D20).
- **Idempotent:** Qdrant ids are `uuid5(video_id:chunk_index)`. A retry or a crash re-run never creates duplicates (D9). Crash recovery was tested by killing the worker in the middle of a job (D23).

## 3. Retrieval (1 min)

**Show:** `POST /videos/{id}/retrieve` with "Why does inflation reduce the purchasing power of money?" ([`02_retrieval.json`](sample-outputs/inflation_khan/02_retrieval.json)).

**Say:**
- Dense search, **always filtered to one `video_id`** (D12).
- The embedding model was chosen by measurement: bge-small scored MRR 0.89 against 0.73 for MiniLM on the real transcripts (D21).
- There is no BM25/hybrid search yet, on purpose: one lecture is a few dozen chunks. Hybrid search is the next step for search across many videos.

## 4. The progress gate and question generation (2 min)

**Show:** `POST /assessments` before any progress was reported → **409** ([`03_assessment_locked_409.json`](sample-outputs/inflation_khan/03_assessment_locked_409.json)). Then `POST /videos/{id}/progress` with 0.95, and `POST /assessments` again ([`04_assessment.json`](sample-outputs/inflation_khan/04_assessment.json)).

**Say:**
- The context comes from **topic-balanced selection**, not similarity search, so the quiz covers the whole video.
- **One strict-schema call per question type.** The single mixed call failed against the real Groq API, so it was replaced (D18).
- Every question is validated after generation: 4 distinct MCQ options, the answer among them, and existing source chunks. Bad questions are dropped and one top-up call replaces them (D13).
- Correct answers and rubrics are never returned by the API.

## 5. Evaluation (1.5 min)

**Show:** `POST /attempts` ([`05_attempt_evaluation.json`](sample-outputs/inflation_khan/05_attempt_evaluation.json)).

**Say:**
- MCQ and true/false are graded **by rules**. That is free and deterministic, and it accepts "B", "2" or the option text.
- Short answers get **one batched LLM-as-judge call**, grounded in the reference answer, the rubric and the source transcript. An answer counts as correct at a score of 0.6 or more (D14).
- If the judge fails, the API returns a clean 502 and **nothing is saved half-graded**.

## 6. The learning report (1 min)

**Show:** [`06_report.md`](sample-outputs/inflation_khan/06_report.md).

**Say:**
- Every number (overall %, per topic, per type, strengths ≥ 80 %, weaknesses < 60 %, rewatch timestamps) is **computed**.
- Only the summary and the suggestions come from the LLM. If that call fails, a template is used, so a report is always produced (D15).

## 7. Engineering quality (1 min)

**Show:** `pytest` (70 offline tests in about 5 s), `ruff check .`, and the [CI workflow](../.github/workflows/ci.yml).

**Say:**
- The `LLMProvider` interface has a Groq implementation and a fake one, so the whole suite runs offline (D7).
- Real Groq behaviour is handled: waiting for the time in `retry-after`, retrying schema rejections, failing fast on auth errors (D19), and the daily quota (D24).
- Production hardening: upload limits enforced while the body streams in, one error envelope everywhere, locked dependencies, and internal services left unpublished (D25).

## 8. Trade-offs and what comes next (1 min)

**Say:**
- **SQLite** makes the database a single file with no server, but allows one writer and one host. Postgres is a change to `DATABASE_URL` (D2).
- **Synchronous LLM endpoints** (20–40 s) keep the flow simple. The fix, if timeouts become a problem, is the existing 202-and-poll job pattern (D25).
- **Redis as the broker** means crash recovery waits for the visibility timeout. RabbitMQ would re-queue immediately (D23).
- **Next:** authentication (it plugs into `app/api/deps.py`), Postgres, hybrid search, and moving generation and grading into background jobs.
