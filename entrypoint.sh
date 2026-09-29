#!/usr/bin/env bash
# ChainTrace AI container entrypoint.
#
# Starts the FastAPI backend (port 8000) and the Streamlit dashboard (port 8501)
# in one container, exactly as `docker compose up` expects.  No network access is
# required at runtime - everything was installed and trained during the image build.
set -euo pipefail

API_PORT="${CHAINTRACE_API_PORT:-8000}"
LOG_LEVEL="${CHAINTRACE_LOG_LEVEL:-INFO}"
RUN_ON_START="${CHAINTRACE_RUN_ON_START:-auto}"

# Which port the dashboard listens on.
#
#  * `docker compose up` (and a bare `docker run -p 8501:8501`) wants 8501.
#  * A Hugging Face Space proxies exactly one port and its `app_port` is 7860, and it
#    identifies the container with SPACE_ID.  Rather than making every host pass an
#    environment variable, follow the platform.
#
# An explicit CHAINTRACE_DASHBOARD_PORT always wins, so an existing deployment cannot
# be surprised by this block.
if [ -n "${CHAINTRACE_DASHBOARD_PORT:-}" ]; then
  DASHBOARD_PORT="${CHAINTRACE_DASHBOARD_PORT}"
elif [ -n "${SPACE_ID:-}" ]; then
  DASHBOARD_PORT="${CHAINTRACE_HF_PORT:-7860}"
else
  DASHBOARD_PORT=8501
fi

echo "=============================================================="
echo " ChainTrace AI - offline investigative prototype"
echo " API        : http://localhost:${API_PORT}  (docs at /docs)"
echo " Dashboard  : http://localhost:${DASHBOARD_PORT}"
if [ -n "${SPACE_ID:-}" ]; then
  echo " Platform   : Hugging Face Space ${SPACE_ID}"
  echo "              (only the dashboard port is proxied; the API is internal)"
fi
echo " Startup run: ${RUN_ON_START}"
echo "=============================================================="

cd /app

# 0. the runtime data tree must be writable.
#
# Some platforms - Hugging Face Spaces is the one we deploy to - run the container as
# UID 1000 even though the image was built as root.  Everything the dashboard *reads*
# is baked into the image at build time, so a read-only tree only breaks the things we
# write at runtime: the append-only evidence ledger, the on-start warm-up, and the file
# upload endpoint.  Instead of failing there, fall back to a writable copy of the tree.
for dir in raw synthetic geo models artifacts; do
  mkdir -p "data/${dir}" 2>/dev/null || true
done
if touch data/.write_probe 2>/dev/null; then
  rm -f data/.write_probe
else
  fallback="/tmp/chaintrace-data"
  echo "[entrypoint] data/ is read-only here; using a writable copy at ${fallback}"
  mkdir -p "${fallback}"
  cp -a data/. "${fallback}/" 2>/dev/null || true
  export CHAINTRACE_DATA_DIR="${fallback}"
fi

# 1. make sure sample data exists (regenerate if the volume was mounted empty)
if [ ! -f "data/synthetic/transactions.csv" ]; then
  echo "[entrypoint] generating synthetic sample dataset..."
  python scripts/generate_sample_data.py --quiet
fi

# 2. train/refresh models if any are missing (offline; uses the local dataset)
if ! ls data/models/*.joblib >/dev/null 2>&1; then
  echo "[entrypoint] training models (Isolation Forest, LOF)..."
  python scripts/train_models.py --quiet || echo "[entrypoint] training skipped"
fi

# 3. warm the artefacts so the dashboard is never empty on first paint
if [ "${RUN_ON_START}" != "never" ] && [ ! -f "data/artifacts/alerts.json" ]; then
  echo "[entrypoint] running the full pipeline once..."
  python scripts/run_full_pipeline.py --quiet || echo "[entrypoint] pipeline warm-up skipped"
fi

# 4. API in the background, dashboard in the foreground (its logs stay visible)
uvicorn src.api.main:app --host 0.0.0.0 --port "${API_PORT}" --log-level "$(echo "${LOG_LEVEL}" | tr '[:upper:]' '[:lower:]')" &
API_PID=$!

cleanup() {
  echo "[entrypoint] stopping API (pid ${API_PID})"
  kill "${API_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# wait for the API to answer before starting the UI, so the dashboard's first API call succeeds
for _ in $(seq 1 40); do
  if python - <<'PY' 2>/dev/null
import urllib.request, os, sys
port = os.environ.get("CHAINTRACE_API_PORT", "8000")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
except Exception:
    sys.exit(1)
PY
  then
    echo "[entrypoint] API is up"
    break
  fi
  sleep 0.5
done

echo "[entrypoint] starting dashboard on port ${DASHBOARD_PORT}"
exec streamlit run app/dashboard.py \
  --server.port "${DASHBOARD_PORT}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false
