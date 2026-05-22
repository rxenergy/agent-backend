FROM python:3.11-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Heavy ML deps (torch / sentence-transformers / transformers) are installed via
# the `[embeddings]` extra when BACKEND_EMBEDDINGS=1. Default builds stay small;
# RETRIEVER_BACKEND=opensearch requires this flag at build time.
ARG BACKEND_EMBEDDINGS=1
ENV BACKEND_EMBEDDINGS=${BACKEND_EMBEDDINGS}

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    if [ "${BACKEND_EMBEDDINGS}" = "1" ]; then \
      pip install --prefix=/install ".[embeddings]"; \
    else \
      pip install --prefix=/install "."; \
    fi

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:$PATH \
    HF_HOME=/var/cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10001 app

COPY --from=deps /install /usr/local

WORKDIR /app
COPY app ./app
RUN mkdir -p /var/lib/agent/events /var/cache/huggingface /app/tools /app/variants && \
    chown -R app:app /var/lib/agent /var/cache/huggingface /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
