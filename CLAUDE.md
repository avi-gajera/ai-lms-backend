@.claude/rules/aidlc.md

# AI-Powered LMS Backend

Backend-only Learning Management System: video → transcript → RAG index → assessment generation → answer evaluation → learning report. Full brief: `AI-Powered_LMS.md`. Implementation plan: phases 0–9 in that brief (§7), adapted as described below.

## Stack (and why — see `docs/decisions.md` for the full log)

| Layer | Choice |
|---|---|
| API | FastAPI, sync `def` endpoints (run in threadpool) |
| Relational DB | **SQLite** via SQLAlchemy 2 + Alembic (`render_as_batch=True`), WAL mode + busy_timeout. Postgres = change `DATABASE_URL` only |
| Vector DB | Qdrant (own Docker service), via `langchain-qdrant` |
| Background jobs | Celery + Redis (`acks_late`, `reject_on_worker_lost`) |
| Transcription | faster-whisper (CPU, int8) + ffmpeg normalisation |
| Embeddings | `all-MiniLM-L6-v2` via `langchain-huggingface` |
| LLM | **Groq** (`openai/gpt-oss-120b` default, `openai/gpt-oss-20b` fast) via `langchain-groq` `with_structured_output(method="json_schema")`, behind our own `LLMProvider` interface (`app/llm/base.py`) |

## Rules for working in this repo

- **No frontend/UI** until the full backend and docs are done. Auth is out of scope (learner_id passed in).
- **LangChain = integrations only** (ChatGroq, QdrantVectorStore, HuggingFaceEmbeddings, ChatPromptTemplate). No LCEL chains, no LangGraph, no agents. Service code must not import LangChain directly except in `app/llm/*`, `app/services/embeddings.py`, `app/services/retrieval.py`.
- Compute deterministically what can be computed (MCQ/TF grading, report aggregation); use the LLM only for generation, open-ended grading and prose.
- Groq free tier: 8K tokens/min — keep each prompt + output under ~6K tokens.
- Whenever you make a non-trivial design choice, **append it to `docs/decisions.md`**.
- Everything runs in Docker (`python:3.12-slim`); the host Python is 3.14 and lacks wheels for some deps.

## Commands

```bash
docker compose up --build                 # api, worker, redis, qdrant
docker compose run --rm api pytest        # tests (offline: fake LLM / embeddings / in-memory Qdrant)
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic revision --autogenerate -m "msg"
python scripts/demo.py                    # end-to-end demo → docs/sample-outputs/
```

## AI-DLC Workflow

AI-DLC v2.9.0 (awslabs/aidlc-workflows, Claude runtime) is installed in `.claude/` (agents, skills, stage protocols, knowledge, sensors, tools, hooks) and `aidlc/` (method tree + intent records). The method rules are pulled into context by the `@.claude/rules/aidlc.md` import at the top of this file (→ `aidlc/spaces/default/memory/{org,team,project}.md` + phase rules). Edit the method there, never in `.claude/rules/`.

- Start or resume a workflow with `/aidlc <description>`; `/aidlc --doctor` validates setup; `/aidlc --status` shows progress. Artifacts go under `aidlc/spaces/default/intents/<record>/`; application code goes at the repo root.
- Stages stop at approval gates; keep **Inception short** for this project — requirements and design are already settled in `AI-Powered_LMS.md` and `docs/decisions.md`.
- **Local adaptation:** the framework's engine, hooks and statusline run via `bun .claude/tools/aidlc.ts`. The shipped settings (hooks, statusline, and an env block that forces AWS Bedrock) are kept inert in `.claude/settings.aidlc-shipped.json`, and the shipped MCP servers in `.mcp.aidlc-shipped.json`. To enable the full engine: install bun, merge the `hooks`/`statusLine`/`permissions` blocks into `.claude/settings.json` (omit the Bedrock `env` unless you use Bedrock), approve hooks via `/hooks`, and restart Claude Code. The complete shipped framework guide is in `.claude/aidlc-CLAUDE.shipped.md`.
