# Multi-stage build. The default (last) stage `runtime` is the production image, used for both the
# API and the Celery worker (different commands in docker-compose.yml). The `dev` stage adds the
# test / demo tooling on top of the same layers:  docker build --target dev .
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/opt/models \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Exact versions from requirements.lock (regenerate with scripts/lock_requirements.sh).
# CPU-only torch comes from the PyTorch index (the default wheel bundles CUDA and is several GB
# larger); --no-deps so its dependencies are resolved from PyPI at the locked versions below.
COPY requirements.lock .
RUN pip install --no-deps --index-url https://download.pytorch.org/whl/cpu "$(grep '^torch==' requirements.lock)" \
 && pip install -r requirements.lock

# Bake the models into the image so containers start (and work offline) without downloading.
ARG WHISPER_MODEL_SIZE=base
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL_SIZE}', device='cpu', compute_type='int8')" \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}', device='cpu')"

RUN useradd --create-home --uid 1000 lms \
 && mkdir -p /app/data \
 && chown -R lms:lms /app /opt/models


# --- dev: runtime + pytest and the demo / sample-fetch script dependencies ----------------------
FROM base AS dev
COPY requirements-dev.lock .
RUN pip install -r requirements-dev.lock
COPY --chown=lms:lms . .
USER lms
CMD ["pytest"]


# --- runtime (default): no test or tooling packages ---------------------------------------------
FROM base AS runtime
COPY --chown=lms:lms . .
USER lms
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
