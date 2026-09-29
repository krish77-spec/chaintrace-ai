"""FastAPI application (spec section 13).

    uvicorn src.api.main:app --host 0.0.0.0 --port 8000

Startup behaviour (``CHAINTRACE_RUN_ON_START``):

* ``auto``  (default) - if no alert artefact exists yet, run the pipeline once in a
  background thread so the API is immediately usable out of the box;
* ``always`` - always (re)run the pipeline on start;
* ``never``  - never touch the data; just serve whatever is on disk.

Everything is offline: no telemetry, no CDN assets, no outbound calls.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import __version__, config
from src.api.routes import router
from src.pipeline.runner import load_alerts, run_full_pipeline
from src.utils.helpers import setup_logging

logger = logging.getLogger(__name__)

_BOOT_LOCK = threading.Lock()


def _maybe_run_pipeline() -> None:
    if config.RUN_PIPELINE_ON_START == "never":
        return
    if config.RUN_PIPELINE_ON_START != "always" and config.ALERTS_PATH.exists() and load_alerts():
        logger.info("Artefacts already present - skipping startup pipeline run")
        return
    with _BOOT_LOCK:
        try:
            logger.info("Running startup pipeline on %s", config.SYNTHETIC_DIR)
            run_full_pipeline(input_dir=config.SYNTHETIC_DIR, output_dir=config.ARTIFACT_DIR)
        except Exception:  # pragma: no cover - startup must never crash the API
            logger.exception("Startup pipeline run failed - the API will still serve /health")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    config.ensure_directories()
    threading.Thread(target=_maybe_run_pipeline, name="chaintrace-boot", daemon=True).start()
    logger.info("ChainTrace AI API v%s ready (offline mode)", __version__)
    yield
    logger.info("ChainTrace AI API shutting down")


app = FastAPI(
    title="ChainTrace AI",
    description=(
        "Offline Bitcoin transaction/network correlation and ML triage prototype. "
        "Eight stages: ingest, enrich, correlate, graph, four ML analyses, explainability, "
        "evidence hashing."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # local, single-user prototype
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", tags=["system"])
def root() -> Dict[str, Any]:
    return {
        "system": "ChainTrace AI",
        "version": __version__,
        "offline": True,
        "docs": "/docs",
        "dashboard": f"http://localhost:{config.DASHBOARD_PORT}",
        "endpoints": [
            "GET /health", "GET /stats", "POST /upload", "POST /run",
            "GET /status/{job_id}", "GET /alerts", "GET /alerts/{alert_id}",
            "GET /graph", "GET /graph/{node_id}", "GET /ledger", "GET /ledger/verify",
        ],
    }
