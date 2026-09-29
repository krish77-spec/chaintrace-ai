"""Pydantic schemas (spec section 13).

Deliberately permissive (``extra="allow"`` on the evidence models) so the API keeps
working when a new detector adds a field to the alert evidence without a schema
change - the prototype should not break because a feature was added.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    system: str = "ChainTrace AI"
    version: str
    offline_mode: bool = True
    artifacts_ready: bool
    alerts_loaded: int
    ledger_entries: int
    seed_wallets: int = 0
    models_present: List[str] = Field(default_factory=list)


class AlertSummary(BaseModel):
    alert_id: str
    entity: str
    entity_type: str
    risk_score: float
    confidence: float
    top_reason: str
    recommended_action: Optional[str] = None
    evidence_hash: Optional[str] = None
    components: Dict[str, float] = Field(default_factory=dict)


class AlertDetail(AlertSummary):
    reasons: List[str] = Field(default_factory=list)
    top_features: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    explainer: Optional[str] = None

    model_config = {"extra": "allow"}


class AlertListResponse(BaseModel):
    total: int
    returned: int
    min_risk: float
    alerts: List[AlertSummary]


class GraphNode(BaseModel):
    id: str
    label: Optional[str] = None
    node_type: Optional[str] = None
    risk_score: Optional[float] = None
    cluster_id: Optional[int] = None
    anomaly_score: Optional[float] = None
    peel_score: Optional[float] = None
    is_seed: Optional[bool] = None

    model_config = {"extra": "allow"}


class GraphEdge(BaseModel):
    source: str
    target: str
    edge_type: Optional[str] = None
    amount: Optional[float] = None
    correlation_confidence: Optional[float] = None

    model_config = {"extra": "allow"}


class GraphResponse(BaseModel):
    node_count: int
    edge_count: int
    truncated: bool = False
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    stats: Dict[str, Any] = Field(default_factory=dict)


class StageTiming(BaseModel):
    stage: int
    name: str
    seconds: float
    detail: Dict[str, Any] = Field(default_factory=dict)


class StatsResponse(BaseModel):
    system: str = "ChainTrace AI"
    generated_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    counts: Dict[str, Any] = Field(default_factory=dict)
    graph: Dict[str, Any] = Field(default_factory=dict)
    correlation: Dict[str, Any] = Field(default_factory=dict)
    clustering: Dict[str, Any] = Field(default_factory=dict)
    risk: Dict[str, Any] = Field(default_factory=dict)
    alerts: Dict[str, Any] = Field(default_factory=dict)
    stages: List[StageTiming] = Field(default_factory=list)
    top_alerts: List[AlertSummary] = Field(default_factory=list)


class JobStatus(BaseModel):
    job_id: str
    status: str                      # queued | running | finished | failed
    stage: Optional[str] = None
    stage_index: Optional[int] = None
    stages_total: int = 8
    progress: float = 0.0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    summary: Optional[Dict[str, Any]] = None


class RunRequest(BaseModel):
    input_dir: Optional[str] = None
    seeds_file: Optional[str] = None
    use_uploaded_files: bool = False


class UploadResponse(BaseModel):
    files_saved: List[Dict[str, Any]]
    upload_dir: str
    message: str


class LedgerVerification(BaseModel):
    ok: bool
    ledger_entries: int
    alerts_checked: int
    verified: int
    mismatched: int
    missing_from_ledger: int
    details: List[Dict[str, Any]] = Field(default_factory=list)
