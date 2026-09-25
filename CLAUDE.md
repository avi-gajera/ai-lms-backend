# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@.claude/rules/aidlc.md

# AI-Powered LMS Backend

Backend-only Learning Management System: video → transcript → RAG index → assessment generation → answer evaluation → learning report. Full brief: `AI-Powered_LMS.md` (phases 0–9 in §7; local only, not in the public repo). User-facing docs: `README.md`; architecture diagram: `docs/architecture.md`; schema/ERD + vector layout: `docs/database.md`; every trade-off (D1–D27): `docs/decisions.md`; 10-minute demo script: `docs/walkthrough.md`.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI, sync `def` endpoints (run in threadpool) |
| Relational DB | **SQLite** via SQLAlchemy 2 + Alembic (`render_as_batch=True`), WAL mode + busy_timeout. Postgres = change `DATABASE_URL` only |
| Vector DB | Qdrant — server in Docker (`QDRANT_URL`), embedded file mode locally (`QDRANT_PATH=data/qdrant`), `:memory:` in tests; via `langchain-qdrant` |
| Background jobs | `TASK_BACKEND=local` (thread in the API process) or `celery` (Redis + worker, `acks_late`, `reject_on_worker_lost`) — D16 |
| Transcription | faster-whisper (`base`, CPU, int8, VAD); audio decoded via its bundled PyAV — **no system ffmpeg** (D17) |
| Embeddings | `BAAI/bge-small-en-v1.5` (384-d) via `langchain-huggingface`, chosen over MiniLM by measured MRR (D21). Queries get BGE's retrieval prefix |
| LLM | **Groq** (`openai/gpt-oss-120b` default, `openai/gpt-oss-20b` "fast" tier) via `langchain-groq` `with_structured_output(method="json_schema", strict=True)`, behind our own `LLMProvider` (`app/llm/base.py`) |

## Commands

Local (host venv works; `.venv` exists):

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload             # migrates the DB on startup; http://localhost:8000/docs
pytest                                    # full offline suite (~5 s)
pytest tests/unit/test_grading.py         # one file
pytest tests/api/test_flow.py -k threshold # one test by name
ruff check .                              # lint (config in pyproject.toml; `ruff check --fix .` for safe fixes)
ruff format .                             # format (line length 120); CI fails on unformatted code
pre-commit install                        # hooks: ruff lint+format, whitespace/EOF/yaml/toml checks
python scripts/fetch_sample_videos.py     # download the 3 sample lectures into sample_data/videos/
python scripts/demo.py                    # end-to-end demo against a running API → docs/sample-outputs/
python scripts/export_openapi.py          # regenerate docs/openapi.json after API changes
python scripts/make_demo_db.py            # regenerate data/demo.db
```

Docker (production-style: api, worker, redis, qdrant; `tools` = opt-in dev image):

```bash
docker compose up --build
docker compose run --rm --no-deps tools pytest
docker compose run --rm tools python scripts/demo.py
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic revision --autogenerate -m "msg"
sh scripts/lock_requirements.sh           # after editing requirements*.txt: regenerate the .lock pins (the image installs the locks)
```

Set `LLM_PROVIDER=fake` to run the whole pipeline without a Groq key. All settings live in `app/config.py`.

CI (`.github/workflows/ci.yml`) runs `ruff check .`, `ruff format --check .` and `pytest` on Python 3.12 with the locked deps; keep all green — run `ruff format .` before committing. Ruff is pinned in the workflow and in `.pre-commit-config.yaml` (keep the two in sync; not in the lock files, D26) — `pip install ruff pre-commit` locally. Don't run `scripts/lock_requirements.sh` casually: it re-resolves every runtime pin.

## Architecture

Layering: `app/api/` (HTTP only, no business logic) → `app/services/` (business logic) → `app/llm/` (LLM access) and `app/db/`. `app/workers/` holds thin job wrappers around services.

- **Ingestion (the only async path).** `POST /videos` takes exactly one of `url` (allowlisted host, validated in the request by `services/downloader.validate_url`), `file` or `sample_path`, creates `Video(pending)` and calls `app/workers/dispatch.enqueue_process_video`, which routes to the in-process `LocalRunner` or a Celery task depending on `TASK_BACKEND`. Both run `workers/jobs.process_video_job` → `services/video_pipeline`: [download audio with yt-dlp, `url` videos only, skipped once the file exists] → transcribe → chunk (~220 words / ~40 overlap, cut on Whisper segment boundaries so timestamps stay exact) → topic-label with the fast model (length-scaled topic budget, fallback "General") → embed → upsert. Idempotent: Qdrant ids are `uuid5(video_id:chunk_index)`, SQLite chunks upsert on `(video_id, chunk_index)`, stored segments let a retry skip transcription. Status: `pending → [downloading →] processing → transcribed → indexed | failed`.
- **Retrieval** (`services/retrieval.py`) is always filtered to a single `video_id`.
- **Assessment generation** (`POST /assessments`, synchronous, 20–40 s) is gated by watch progress ≥ `COMPLETION_THRESHOLD` (409 otherwise). Context comes from topic-balanced chunk selection, not similarity search. One structured-output call per question type, each with its own strict Pydantic schema (`app/llm/schemas.py`); outputs are validated (4 distinct MCQ options, answer among them, source chunks exist, dedupe), with one top-up call per short type (D13, D18). Answers/rubrics are never returned by `GET /assessments/{id}`.
- **Evaluation + report** (`POST /attempts`, synchronous): MCQ/TF graded by rules (`services/grading.py`); short answers by one batched LLM-judge call (correct at score ≥ 0.6). Report numbers are computed deterministically (`services/report.py`); only summary/suggestions prose comes from the LLM, with a template fallback (`summary_source: "template"`). A judge failure returns 502 and persists nothing.
- **LLM layer.** Services call `get_llm()` (`app/llm/factory.py`, cached) and use `structured(...)` / `text(...)` with `tier="default"|"fast"`. `GroqProvider` handles 429 `retry-after`, retries `json_validate_failed`, fails fast on auth (503), and raises retriable 502s (D19). Prompts are versioned in `app/llm/prompts.py`.
- **Errors.** Raise the hierarchy in `app/core/exceptions.py`; handlers render every error (incl. 422/404/413) as `{"error": {code, message, request_id, details}}`. `app/core/middleware.py` enforces `MAX_UPLOAD_MB` while streaming and sanitises `X-Request-ID`.
- **Tests** (`tests/conftest.py`) set env *before* importing the app: temp SQLite, in-memory Qdrant, `LLM_PROVIDER=fake`, `EMBEDDING_PROVIDER=fake` (hashing embeddings), `TASK_BACKEND=local`, and a monkeypatched fake transcriber whose "video" file just lists topic names. Link tests patch `video_pipeline.download_audio` (never hit the network). Each test drops/recreates tables and clears the `get_llm` cache; call `wait_jobs()` after enqueueing ingestion. New LLM-dependent behaviour needs matching output in `app/llm/fake_provider.py`.

## Rules for working in this repo

- **No frontend/UI** until the full backend and docs are done. Auth is out of scope (`learner_id` passed in; `app/api/deps.py` is where auth would plug in).
- **LangChain = integrations only** (ChatGroq, QdrantVectorStore, HuggingFaceEmbeddings, ChatPromptTemplate). No LCEL chains, no LangGraph, no agents. Only `app/llm/*`, `app/services/embeddings.py`, `app/services/retrieval.py` may import LangChain.
- Compute deterministically what can be computed (MCQ/TF grading, report aggregation); use the LLM only for generation, open-ended grading and prose.
- Groq free tier: 8K tokens/min — keep each prompt + output under ~6K tokens. The daily quota is the practical limit for demo runs (D24).
- **Keep this file current:** with every change you make, update the relevant parts of `CLAUDE.md` in the same change (commands, stack, architecture, rules, env vars). Never leave it describing old behaviour.
- Whenever you make a non-trivial design choice, **append it to `docs/decisions.md`** as the next `D<n>`.
- Schema changes need an Alembic migration (the API auto-migrates on startup when `AUTO_MIGRATE=true`).
- The Docker image is `python:3.12-slim`; in Docker, SQLite lives on the `lms-data` named volume (not a bind mount) because WAL locking across the api/worker containers needs a real Linux filesystem (D22).

## AI-DLC Workflow

AI-DLC v2.9.0 (awslabs/aidlc-workflows, Claude runtime) is installed in `.claude/` (agents, skills, stage protocols, knowledge, sensors, tools, hooks) and `aidlc/` (method tree + intent records). **These are local-only:** `.claude/`, `aidlc/`, `.mcp.aidlc-shipped.json` and `AI-Powered_LMS.md` are gitignored and were removed from the git history, so the public GitHub repo holds only the application — never `git add -f` them. The method rules are pulled into context by the `@.claude/rules/aidlc.md` import at the top of this file (→ `aidlc/spaces/default/memory/{org,team,project}.md` + phase rules). Edit the method there, never in `.claude/rules/`.

- Start or resume a workflow with `/aidlc <description>`; `/aidlc --doctor` validates setup; `/aidlc --status` shows progress. Artifacts go under `aidlc/spaces/default/intents/<record>/`; application code goes at the repo root.
- Stages stop at approval gates; keep **Inception short** for this project — requirements and design are already settled in `AI-Powered_LMS.md` and `docs/decisions.md`.
- **Local adaptation:** the framework's engine, hooks and statusline run via `bun .claude/tools/aidlc.ts`. The shipped settings (hooks, statusline, and an env block that forces AWS Bedrock) are kept inert in `.claude/settings.aidlc-shipped.json`, and the shipped MCP servers in `.mcp.aidlc-shipped.json`. To enable the full engine: install bun, merge the `hooks`/`statusLine`/`permissions` blocks into `.claude/settings.json` (omit the Bedrock `env` unless you use Bedrock), approve hooks via `/hooks`, and restart Claude Code. The complete shipped framework guide is in `.claude/aidlc-CLAUDE.shipped.md`.
