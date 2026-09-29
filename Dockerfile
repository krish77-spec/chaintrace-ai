# ChainTrace AI - offline container image
#
#   docker compose up --build
#
# The build stage does everything that needs a network: install dependencies, generate
# the synthetic dataset, train the ML models and warm one full pipeline run.  After the
# image is built the system never touches the network again (no CDN, no telemetry, no
# cloud model at runtime).
#
# Optional real SHAP values (pulls numpy>=2, kept out of the core image on purpose):
#   docker compose build --build-arg INSTALL_SHAP=1
#
# syntax=docker/dockerfile:1
FROM python:3.11-slim

ARG INSTALL_SHAP=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHAINTRACE_LOG_LEVEL=INFO \
    HOME=/tmp

WORKDIR /app

# Only the dependency manifests are copied first, so code edits do not invalidate the
# (slow) pip layer.  The pip download cache lives in a BuildKit cache mount rather than
# in the image: wheels are reused across rebuilds (so an interrupted or repeated build
# resumes instead of re-downloading ~150 MB) while the final image stays lean.
COPY requirements.txt requirements-shap.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    && if [ "$INSTALL_SHAP" = "1" ]; then pip install -r requirements-shap.txt; fi

# Application code
COPY src/ ./src/
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY .streamlit/ ./.streamlit/
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
    && mkdir -p data/raw data/synthetic data/geo data/models data/artifacts

# ---------------------------------------------------------------------------------------
# Offline warm-up: sample data, trained + persisted models, and one complete pipeline run
# so the dashboard has something to show the moment the container starts.
# ---------------------------------------------------------------------------------------
RUN python scripts/generate_sample_data.py --quiet \
    && python scripts/audit_dataset.py \
    && python scripts/train_models.py --quiet \
    && python scripts/run_full_pipeline.py --quiet

# The build above ran as root, but a Hugging Face Space runs the container as UID 1000.
# Everything the console *reads* is already baked in, yet the container still writes at
# runtime (the append-only evidence ledger, the on-start warm-up, uploaded files), so
# hand those directories over rather than switching the whole image to a non-root USER -
# `USER 1000` would break the ./data bind mount that `docker compose` uses on macOS,
# where the host directory belongs to the desktop user.
RUN chmod -R a+rwX data && chmod a+rwX /app

EXPOSE 8000 8501 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('CHAINTRACE_API_PORT','8000') + '/health', timeout=3)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
