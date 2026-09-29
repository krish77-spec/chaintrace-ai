"""REST endpoints (spec section 13).

    GET  /health                 liveness + artefact inventory
    POST /upload                 accept CSV/JSON/XML files (network + transactions)
    POST /run                    run the full pipeline as a background job
    GET  /status/{job_id}        progress of that job
    GET  /alerts                 ranked alert list (min_risk, limit, entity_type)
    GET  /alerts/{alert_id}      full evidence package for one alert
    GET  /graph                  nodes + edges (optionally around a seed node)
    GET  /graph/{node_id}        neighbourhood of one node
    GET  /stats                  summary counters for the dashboard header
    GET  /ledger                 the append-only evidence ledger
    GET  /ledger/verify          re-hash every alert and compare against the ledger

Job state is an in-memory dict - explicitly allowed for this prototype, no Redis.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from src import __version__, config
from src.api import schemas
from src.graph.builder import load_graph_payload
from src.pipeline.runner import load_alerts, load_pipeline_summary, run_full_pipeline
from src.utils.hashing import read_ledger, verify_ledger, verify_ledger_chain
from src.utils.infrastructure import focus_list, rollup

logger = logging.getLogger(__name__)
router = APIRouter()

# --------------------------------------------------------------------------- #
# in-memory job registry
# --------------------------------------------------------------------------- #
JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "stage": None,
            "stage_index": 0,
            "stages_total": 8,
            "progress": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "error": None,
            "summary": None,
        }
    return job_id


def _update_job(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _run_job(job_id: str, input_dir: Path, seeds_file: Optional[Path]) -> None:
    _update_job(job_id, status="running")
    started = datetime.now(timezone.utc)

    def progress(stage_name: str, index: int, total: int) -> None:
        _update_job(
            job_id, stage=stage_name, stage_index=index,
            progress=round(min(index / max(total, 1), 1.0), 3),
        )

    try:
        summary = run_full_pipeline(
            input_dir=input_dir, output_dir=config.ARTIFACT_DIR,
            seeds_file=seeds_file, progress=progress,
        )
        _update_job(
            job_id,
            status="finished" if summary.get("status") == "ok" else "failed",
            stage="done",
            progress=1.0,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 3),
            summary=summary,
            error=None if summary.get("status") == "ok" else summary.get("reason"),
        )
    except Exception as exc:  # pragma: no cover - surfaced through the API
        logger.exception("Pipeline job %s failed", job_id)
        _update_job(
            job_id, status="failed", error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 3),
        )


# --------------------------------------------------------------------------- #
# system
# --------------------------------------------------------------------------- #
@router.get("/health", response_model=schemas.HealthResponse, tags=["system"])
def health() -> schemas.HealthResponse:
    alerts = load_alerts()
    ledger = [entry for entry in read_ledger() if entry.get("alert_id")]
    models = sorted(path.name for path in config.MODEL_DIR.glob("*.joblib"))
    return schemas.HealthResponse(
        status="ok",
        version=__version__,
        artifacts_ready=bool(alerts),
        alerts_loaded=len(alerts),
        ledger_entries=len(ledger),
        seed_wallets=len(_load_seed_wallets()),
        models_present=models,
    )


def _load_seed_wallets() -> List[str]:
    path = config.SYNTHETIC_DIR / config.SEEDS_JSON
    if not path.exists():
        return []
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        return [str(w) for w in payload.get("wallets", [])]
    except Exception:
        return []


@router.get("/stats", response_model=schemas.StatsResponse, tags=["system"])
def stats() -> schemas.StatsResponse:
    summary = load_pipeline_summary()
    alerts = load_alerts()
    graph = load_graph_payload()
    graph_stats = graph.get("stats", {}) or {}

    top = [
        schemas.AlertSummary(
            alert_id=alert["alert_id"], entity=alert["entity"], entity_type=alert["entity_type"],
            risk_score=alert["risk_score"], confidence=alert["confidence"],
            top_reason=(alert.get("reasons") or [""])[0],
            recommended_action=alert.get("recommended_action"),
            evidence_hash=alert.get("evidence_hash"),
            components=alert.get("components", {}),
        )
        for alert in alerts[: min(len(alerts), 10)]
    ]

    stages = [
        schemas.StageTiming(
            stage=stage.get("stage", 0), name=stage.get("name", ""),
            seconds=stage.get("seconds", 0.0), detail=stage.get("detail", {}),
        )
        for stage in summary.get("stages", [])
    ]

    alerts_summary = {
        "total": len(alerts),
        "above_0_5": sum(1 for a in alerts if a["risk_score"] >= 0.5),
        "above_0_7": sum(1 for a in alerts if a["risk_score"] >= 0.7),
        "wallet_alerts": sum(1 for a in alerts if a["entity_type"] == "wallet"),
        "transaction_alerts": sum(1 for a in alerts if a["entity_type"] == "txid"),
        "peeling_alerts": sum(1 for a in alerts if (a.get("evidence") or {}).get("peel_chain")),
        "mixing_alerts": sum(1 for a in alerts if (a.get("evidence") or {}).get("is_mixing")),
        "cluster_alerts": sum(
            1 for a in alerts if ((a.get("evidence") or {}).get("cluster") or {}).get("tainted")
        ),
    }

    return schemas.StatsResponse(
        generated_at=summary.get("generated_at"),
        duration_seconds=summary.get("duration_seconds"),
        counts=summary.get("counts", {}),
        graph=graph_stats,
        correlation={
            "correlated_pairs": summary.get("counts", {}).get("correlated_pairs", 0),
            "stage": next((s["detail"] for s in summary.get("stages", []) if s["name"] == "correlate"), {}),
        },
        clustering={
            "stage": next((s["detail"] for s in summary.get("stages", []) if s["name"] == "clustering"), {}),
        },
        risk={
            "stage": next((s["detail"] for s in summary.get("stages", []) if s["name"] == "risk"), {}),
        },
        alerts=alerts_summary,
        stages=stages,
        top_alerts=top,
    )


# --------------------------------------------------------------------------- #
# upload / run
# --------------------------------------------------------------------------- #
@router.post("/upload", response_model=schemas.UploadResponse, tags=["pipeline"])
async def upload(files: List[UploadFile] = File(...)) -> schemas.UploadResponse:
    config.ensure_directories()
    upload_dir = config.RAW_DIR / f"upload_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved: List[Dict[str, Any]] = []
    allowed = (".csv", ".json", ".jsonl", ".ndjson", ".xml")
    for upload_file in files:
        suffix = Path(upload_file.filename or "").suffix.lower()
        if suffix not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {upload_file.filename}")
        target = upload_dir / Path(upload_file.filename or "upload").name
        with target.open("wb") as handle:
            shutil.copyfileobj(upload_file.file, handle)
        saved.append({"name": target.name, "path": str(target), "bytes": target.stat().st_size})

    if not saved:
        raise HTTPException(status_code=400, detail="No files received")
    logger.info("Stored %d uploaded file(s) in %s", len(saved), upload_dir)
    return schemas.UploadResponse(
        files_saved=saved,
        upload_dir=str(upload_dir),
        message="Files stored. POST /run with use_uploaded_files=true (or input_dir) to analyse them.",
    )


@router.post("/run", response_model=schemas.JobStatus, tags=["pipeline"])
def run(background: BackgroundTasks, request: Optional[schemas.RunRequest] = None) -> schemas.JobStatus:
    request = request or schemas.RunRequest()
    if request.input_dir:
        input_dir = Path(request.input_dir)
    elif request.use_uploaded_files:
        uploads = sorted(
            (path for path in config.RAW_DIR.glob("upload_*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        )
        if not uploads:
            raise HTTPException(status_code=400, detail="No uploaded files found - POST /upload first")
        input_dir = uploads[-1]
    else:
        input_dir = config.SYNTHETIC_DIR

    if not input_dir.exists():
        raise HTTPException(status_code=400, detail=f"Input directory does not exist: {input_dir}")

    seeds_file = Path(request.seeds_file) if request.seeds_file else None
    job_id = _new_job()
    background.add_task(_run_job, job_id, input_dir, seeds_file)
    logger.info("Queued pipeline job %s on %s", job_id, input_dir)
    return schemas.JobStatus(**JOBS[job_id])


@router.get("/status/{job_id}", response_model=schemas.JobStatus, tags=["pipeline"])
def status(job_id: str) -> schemas.JobStatus:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return schemas.JobStatus(**job)


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #
def _alert_summary(alert: Dict[str, Any]) -> schemas.AlertSummary:
    return schemas.AlertSummary(
        alert_id=alert["alert_id"],
        entity=alert["entity"],
        entity_type=alert["entity_type"],
        risk_score=alert["risk_score"],
        confidence=alert["confidence"],
        top_reason=(alert.get("reasons") or [""])[0],
        recommended_action=alert.get("recommended_action"),
        evidence_hash=alert.get("evidence_hash"),
        components=alert.get("components", {}),
    )


@router.get("/alerts", response_model=schemas.AlertListResponse, tags=["alerts"])
def alerts(
    min_risk: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=500),
    entity_type: Optional[str] = Query(None, description="wallet | txid"),
) -> schemas.AlertListResponse:
    all_alerts = load_alerts()
    filtered = [a for a in all_alerts if a["risk_score"] >= min_risk]
    if entity_type:
        filtered = [a for a in filtered if a["entity_type"] == entity_type]
    filtered.sort(key=lambda a: (a["risk_score"], a["confidence"]), reverse=True)
    return schemas.AlertListResponse(
        total=len(filtered),
        returned=len(filtered[:limit]),
        min_risk=min_risk,
        alerts=[_alert_summary(a) for a in filtered[:limit]],
    )


@router.get("/alerts/{alert_id}", response_model=schemas.AlertDetail, tags=["alerts"])
def alert_detail(alert_id: str) -> schemas.AlertDetail:
    for alert in load_alerts():
        if alert["alert_id"] == alert_id:
            return schemas.AlertDetail(**_alert_summary(alert).model_dump(), **{
                "reasons": alert.get("reasons", []),
                "top_features": alert.get("top_features", []),
                "evidence": alert.get("evidence", {}),
                "explainer": alert.get("explainer"),
            })
    raise HTTPException(status_code=404, detail=f"Unknown alert {alert_id}")


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
def _graph_response(
    node_id: Optional[str],
    depth: int,
    limit: int,
) -> schemas.GraphResponse:
    """Shared graph/neighbourhood payload builder.

    Kept separate from the route functions so ``/graph/{node_id}`` can reuse it with
    already-resolved values: calling a FastAPI route function directly would pass its
    ``Query(...)`` default objects through as real arguments.
    """
    payload = load_graph_payload()
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])

    if node_id:
        node_ids = {str(node["id"]) for node in nodes}
        if node_id not in node_ids:
            raise HTTPException(status_code=404, detail=f"Unknown node {node_id}")
        adjacency: Dict[str, set] = {}
        for edge in edges:
            adjacency.setdefault(edge["source"], set()).add(edge["target"])
            adjacency.setdefault(edge["target"], set()).add(edge["source"])
        keep = {node_id}
        frontier = {node_id}
        for _ in range(depth):
            next_frontier: set = set()
            for node in frontier:
                next_frontier |= adjacency.get(node, set())
            frontier = next_frontier - keep
            keep |= frontier
        nodes = [node for node in nodes if node["id"] in keep]
        edges = [edge for edge in edges if edge["source"] in keep and edge["target"] in keep]

    nodes = nodes[:limit]
    node_ids = {node["id"] for node in nodes}
    edges = [edge for edge in edges if edge["source"] in node_ids and edge["target"] in node_ids]

    return schemas.GraphResponse(
        node_count=len(nodes),
        edge_count=len(edges),
        truncated=bool(payload.get("truncated")) or len(nodes) >= limit,
        nodes=[schemas.GraphNode(**node) for node in nodes],
        edges=[schemas.GraphEdge(**edge) for edge in edges],
        stats=payload.get("stats", {}),
    )


@router.get("/graph", response_model=schemas.GraphResponse, tags=["graph"])
def graph(
    node_id: Optional[str] = Query(None, description="centre the subgraph on this node"),
    depth: int = Query(config.GRAPH_SUBGRAPH_DEFAULT_DEPTH, ge=1, le=4),
    limit: int = Query(config.MAX_GRAPH_NODES_FOR_EXPORT, ge=10, le=20000),
) -> schemas.GraphResponse:
    return _graph_response(node_id=node_id, depth=depth, limit=limit)


@router.get("/graph/{node_id}", response_model=schemas.GraphResponse, tags=["graph"])
def graph_neighbourhood(node_id: str, depth: int = Query(1, ge=1, le=3)) -> schemas.GraphResponse:
    return _graph_response(
        node_id=node_id,
        depth=depth,
        limit=config.MAX_GRAPH_NODES_FOR_EXPORT,
    )


# --------------------------------------------------------------------------- #
# evidence ledger
# --------------------------------------------------------------------------- #
@router.get("/ledger", tags=["evidence"])
def ledger(limit: int = Query(500, ge=1, le=5000)) -> JSONResponse:
    entries = read_ledger()
    return JSONResponse({"entries": entries[:limit], "total": len(entries)})


@router.get("/ledger/verify", response_model=schemas.LedgerVerification, tags=["evidence"])
def ledger_verify() -> schemas.LedgerVerification:
    report = verify_ledger(load_alerts())
    return schemas.LedgerVerification(**report)


@router.get("/ledger/chain", tags=["evidence"])
def ledger_chain() -> JSONResponse:
    """Verify the hash chain itself (linkage + per-entry integrity)."""
    return JSONResponse(verify_ledger_chain())


# --------------------------------------------------------------------------- #
# infrastructure roll-up
# --------------------------------------------------------------------------- #
@router.get("/infrastructure", tags=["analysis"])
def infrastructure() -> JSONResponse:
    """Group the flagged entities by the ASN/country behind their traffic."""
    correlated: List[Dict[str, Any]] = []
    if config.CORRELATED_PATH.exists():
        try:
            correlated = pd.read_csv(config.CORRELATED_PATH).fillna("").to_dict(orient="records")
        except Exception:  # pragma: no cover - defensive
            correlated = []
    report = rollup(load_alerts(), correlated)
    report["focus"] = focus_list(report)
    return JSONResponse(report)
