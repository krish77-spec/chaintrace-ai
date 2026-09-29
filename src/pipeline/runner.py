"""Pipeline orchestrator (spec section 12).

``run_full_pipeline(input_dir, output_dir)`` runs the eight stages in order and
writes every artefact the API and the dashboard need:

    1  ingest        parse CSV/JSON/XML           -> unified DataFrames
    2  enrich        offline GeoIP                -> country/ASN per IP
    3  correlate     TXID exact + time window     -> correlated_records.csv
    4  graph         NetworkX MultiDiGraph        -> graph.json
    5  ml            clustering / anomaly /
                     peeling+mixing / risk        -> models/*.joblib, feature CSVs
    6  explain       reasons, SHAP-style
                     contributions, confidence    -> alerts.json
    7  dashboard     (consumes the artefacts)
    8  evidence      SHA-256 hashes + ledger      -> evidence_ledger.jsonl

The function is pure with respect to the filesystem: it reads the input
directory, and writes only inside ``output_dir`` / ``data/``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from src import config
from src.correlation.correlator import correlate, correlation_summary
from src.data import enrich as enrich_module
from src.data import parsers
from src.graph.builder import GraphBuilder
from src.ml import anomaly as anomaly_module
from src.ml import clustering as clustering_module
from src.ml import explain as explain_module
from src.ml import features as features_module
from src.ml import peeling as peeling_module
from src.ml import risk as risk_module
from src.utils.hashing import write_ledger
from src.utils.helpers import Timer, json_safe, setup_logging

logger = logging.getLogger(__name__)

SEED_FILE_NAMES = (config.SEEDS_JSON, "sanctioned_wallets.json", "known_bad_wallets.json")


@dataclass
class PipelineContext:
    """Everything a stage produces, kept in one place for the API and the UI."""

    input_dir: Path
    output_dir: Path
    transactions: pd.DataFrame = field(default_factory=pd.DataFrame)
    network: pd.DataFrame = field(default_factory=pd.DataFrame)
    correlated: pd.DataFrame = field(default_factory=pd.DataFrame)
    builder: Optional[GraphBuilder] = None
    clusters: Optional[clustering_module.ClusterResult] = None
    tx_features: pd.DataFrame = field(default_factory=pd.DataFrame)
    wallet_features: pd.DataFrame = field(default_factory=pd.DataFrame)
    tx_anomaly: Optional[anomaly_module.AnomalyResult] = None
    wallet_anomaly: Optional[anomaly_module.AnomalyResult] = None
    peeling: Optional[peeling_module.PeelResult] = None
    risk: Optional[risk_module.RiskResult] = None
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    seeds: List[str] = field(default_factory=list)
    stage_timings: Dict[str, float] = field(default_factory=dict)
    stages: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, stage: int, name: str, timer: Timer, detail: Dict[str, Any]) -> None:
        self.stage_timings[name] = timer.seconds
        self.stages.append(
            {
                "stage": stage,
                "name": name,
                "seconds": timer.seconds,
                "detail": json_safe(detail),
            }
        )


# --------------------------------------------------------------------------- #
# stage helpers
# --------------------------------------------------------------------------- #
def _load_seeds(input_dir: Path, seeds_file: Optional[Path | str] = None) -> List[str]:
    candidates: List[Path] = []
    if seeds_file:
        candidates.append(Path(seeds_file))
    candidates.extend(input_dir / name for name in SEED_FILE_NAMES)
    candidates.append(config.SYNTHETIC_DIR / config.SEEDS_JSON)
    for path in candidates:
        if path.exists():
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                wallets = payload.get("wallets") or payload.get("seed_illicit_wallets") or []
            elif isinstance(payload, list):
                wallets = payload
            else:
                wallets = []
            wallets = [str(w) for w in wallets if w]
            if wallets:
                logger.info("Loaded %d seed wallets from %s", len(wallets), path)
                return wallets
    logger.warning("No seed wallet file found - risk propagation will rely on modelled signals only")
    return []


def run_full_pipeline(
    input_dir: Optional[Path | str] = None,
    output_dir: Optional[Path | str] = None,
    seeds_file: Optional[Path | str] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
    write_artifacts: bool = True,
) -> Dict[str, Any]:
    """Run every stage end to end and return a JSON-serialisable summary."""
    setup_logging()
    input_dir = Path(input_dir or config.SYNTHETIC_DIR)
    output_dir = Path(output_dir or config.ARTIFACT_DIR)
    config.ensure_directories()
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = PipelineContext(input_dir=input_dir, output_dir=output_dir)
    started = datetime.now(timezone.utc)

    def notify(stage: int, name: str) -> None:
        if progress:
            try:
                progress(name, stage, 8)
            except Exception:  # pragma: no cover - UI callback must never break the run
                pass

    # ---------------- stage 1: ingest ---------------------------------- #
    notify(1, "ingest")
    with Timer("ingest") as timer:
        transactions, network, discovered_seeds = parsers.load_input_bundle(input_dir)
        ctx.transactions, ctx.network = transactions, network
        if write_artifacts and not transactions.empty:
            parsers.save_unified(transactions, network, output_dir)
    ctx.record(1, "ingest", timer, {"transactions": len(transactions), "network_records": len(network)})

    if transactions.empty:
        summary = _empty_summary(input_dir, output_dir, "No transactions could be ingested")
        _write_summary(summary, output_dir)
        return summary

    # ---------------- stage 2: enrich ---------------------------------- #
    notify(2, "enrich")
    with Timer("enrich") as timer:
        enricher = enrich_module.GeoIPEnricher()
        network = enrich_module.enrich_network(network, enricher) if not network.empty else network
        ctx.network = network
        enrichment_summary = enrich_module.build_enrichment_summary(network)
    ctx.record(2, "enrich", timer, enrichment_summary)

    # ---------------- stage 3: correlate -------------------------------- #
    notify(3, "correlate")
    with Timer("correlate") as timer:
        ctx.correlated = correlate(transactions, network)
        if not ctx.correlated.empty:
            transactions = enrich_module.enrich_transactions_with_geo(transactions, ctx.correlated)
            ctx.transactions = transactions
        correlation_stats = correlation_summary(ctx.correlated)
        if write_artifacts and not ctx.correlated.empty:
            ctx.correlated.to_csv(config.CORRELATED_PATH, index=False)
    ctx.record(3, "correlate", timer, correlation_stats)

    # ---------------- stage 4: graph ------------------------------------ #
    notify(4, "graph")
    ctx.seeds = _load_seeds(input_dir, seeds_file) or discovered_seeds
    with Timer("graph") as timer:
        builder = GraphBuilder(transactions, ctx.correlated, seeds=ctx.seeds)
        builder.build()
        ctx.builder = builder
        graph_stats = builder.get_stats()
        if write_artifacts:
            builder.save(config.GRAPH_PATH)
    ctx.record(4, "graph", timer, graph_stats)

    # ---------------- stage 5a: transaction features --------------------- #
    notify(5, "features")
    with Timer("features") as timer:
        engineer = features_module.FeatureEngineer(builder, ctx.correlated)
        ctx.tx_features = engineer.build_transaction_features()
    ctx.record(5, "features", timer, {"tx_features": len(ctx.tx_features)})

    # ---------------- stage 5b: clustering ------------------------------- #
    notify(5, "clustering")
    with Timer("clustering") as timer:
        ctx.clusters = clustering_module.cluster_entities(builder, transactions)
    ctx.record(5, "clustering", timer, ctx.clusters.summary())

    # ---------------- stage 5c: anomaly detection ------------------------ #
    notify(5, "anomaly")
    with Timer("anomaly") as timer:
        usable = [c for c in features_module.TX_FEATURE_COLUMNS if c in ctx.tx_features.columns]
        ctx.tx_anomaly = anomaly_module.detect_anomalies(
            ctx.tx_features, usable, id_column="txid", name="transaction",
            save_path=config.MODEL_DIR / "transaction_anomaly_model.joblib",
        )
        anomaly_detail = ctx.tx_anomaly.summary()
    ctx.record(5, "anomaly", timer, anomaly_detail)

    # ---------------- stage 5d: peeling / mixing ------------------------- #
    notify(5, "peeling")
    with Timer("peeling") as timer:
        ctx.peeling = peeling_module.detect_peeling_and_mixing(transactions, seeds=ctx.seeds)
    ctx.record(5, "peeling", timer, ctx.peeling.summary())

    # ---------------- stage 5e: risk propagation ------------------------- #
    notify(5, "risk")
    with Timer("risk") as timer:
        ctx.risk = risk_module.propagate_risk(
            builder,
            ctx.seeds,
            anomaly_scores=ctx.tx_anomaly.score_map(),
            peel_scores=ctx.peeling.tx_peel_score,
            mixing_scores=ctx.peeling.tx_mixing_score,
            cluster_labels=ctx.clusters.labels if ctx.clusters else None,
            cluster_methods=ctx.clusters.methods if ctx.clusters else None,
        )
        risk_detail = ctx.risk.summary()
    ctx.record(5, "risk", timer, risk_detail)

    # ---------------- stage 5f: wallet features + anomaly ---------------- #
    notify(5, "wallet_features")
    with Timer("wallet_features") as timer:
        ctx.wallet_features = engineer.build_wallet_features(
            cluster_labels=ctx.clusters.labels,
            cluster_sizes=ctx.clusters.sizes,
            risk=ctx.risk,
            peel=ctx.peeling,
        )
        updates = _node_attribute_updates(ctx)
        builder.apply_node_attributes(updates)
        usable_wallet = [c for c in features_module.WALLET_FEATURE_COLUMNS if c in ctx.wallet_features.columns]
        if len(ctx.wallet_features) > config.LOF_NEIGHBORS + 2:
            ctx.wallet_anomaly = anomaly_module.detect_anomalies(
                ctx.wallet_features, usable_wallet, id_column="node_id", name="wallet",
                save_path=config.MODEL_DIR / "wallet_anomaly_model.joblib",
            )
            for wallet, score in ctx.wallet_anomaly.score_map().items():
                updates.setdefault(wallet, {})["anomaly_score"] = score
            builder.apply_node_attributes(updates)
        if write_artifacts:
            engineer.save()
            # Stage 4 already saved a graph, but that copy predates every annotation
            # written by this stage (cluster_id, risk_score, peel/anomaly scores).
            # The API and the dashboard render THAT file, so without this second save
            # the graph tab would colour by nothing: risk 0.000 and cluster "-"
            # for all wallets, however good the analytics actually were.
            builder.save(config.GRAPH_PATH)
    ctx.record(5, "wallet_features", timer, {
        "wallet_features": len(ctx.wallet_features),
        "wallet_anomaly_flagged": ctx.wallet_anomaly.summary()["flagged"] if ctx.wallet_anomaly else 0,
    })

    # ---------------- stage 6: explain + rank ---------------------------- #
    notify(6, "explain")
    with Timer("explain") as timer:
        explainer = explain_module.AlertExplainer(
            builder=builder,
            tx_features=ctx.tx_features,
            wallet_features=ctx.wallet_features,
            anomaly=ctx.tx_anomaly,
            peeling=ctx.peeling,
            risk=ctx.risk,
            clusters=ctx.clusters,
            correlated=ctx.correlated,
            seeds=ctx.seeds,
            anomaly_detector=None,
            wallet_anomaly=ctx.wallet_anomaly,
        )
        bundle = explainer.build_alerts()
        ctx.alerts = bundle.alerts
        alert_summary = bundle.summary()
        alert_summary["explainer_used"] = bundle.explainer_used
    ctx.record(6, "explain", timer, alert_summary)

    # ---------------- stage 8: evidence lock ----------------------------- #
    notify(8, "evidence")
    with Timer("evidence") as timer:
        ledger_written = write_ledger(ctx.alerts) if write_artifacts else 0
        evidence_summary = {"ledger_entries": ledger_written, "alerts_hashed": len(ctx.alerts)}
    ctx.record(8, "evidence", timer, evidence_summary)

    # ---------------- artefacts ------------------------------------------ #
    if write_artifacts:
        _write_alerts(ctx.alerts, output_dir)
        if ctx.wallet_anomaly is not None:
            _write_explainer_meta(ctx)

    finished = datetime.now(timezone.utc)
    summary = {
        "generated_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "artifacts": {
            "graph": str(config.GRAPH_PATH),
            "alerts": str(output_dir / "alerts.json"),
            "ledger": str(config.LEDGER_PATH),
            "wallet_features": str(config.WALLET_FEATURES_PATH),
            "tx_features": str(config.TX_FEATURES_PATH),
            "correlated": str(config.CORRELATED_PATH),
        },
        "counts": {
            "transactions": int(len(ctx.transactions)),
            "network_records": int(len(ctx.network)),
            "correlated_pairs": int(len(ctx.correlated)),
            "wallets": int(graph_stats["wallets"]),
            "graph_nodes": int(graph_stats["nodes"]),
            "graph_edges": int(graph_stats["edges"]),
            "clusters": int(ctx.clusters.n_clusters) if ctx.clusters else 0,
            "peel_chains": len(ctx.peeling.chains) if ctx.peeling else 0,
            "mixing_transactions": int(sum(1 for v in ctx.peeling.tx_is_mixing.values() if v)) if ctx.peeling else 0,
            "anomalies_flagged": int(ctx.tx_anomaly.summary()["flagged"]) if ctx.tx_anomaly else 0,
            "alerts": len(ctx.alerts),
            "seed_wallets": len(ctx.seeds),
        },
        "stages": json_safe(ctx.stages),
        "alerts_preview": json_safe(
            [
                {
                    "alert_id": alert["alert_id"],
                    "entity": alert["entity"],
                    "entity_type": alert["entity_type"],
                    "risk_score": alert["risk_score"],
                    "confidence": alert["confidence"],
                    "top_reason": (alert["reasons"] or [""])[0],
                    "evidence_hash": alert["evidence_hash"],
                }
                for alert in ctx.alerts[:15]
            ]
        ),
        "status": "ok",
    }

    if write_artifacts:
        _write_summary(summary, output_dir)
    logger.info(
        "Pipeline finished in %.1fs: %d transactions, %d alerts, %d peel chains, %d mixing txs",
        summary["duration_seconds"], summary["counts"]["transactions"], summary["counts"]["alerts"],
        summary["counts"]["peel_chains"], summary["counts"]["mixing_transactions"],
    )
    return summary


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _node_attribute_updates(ctx: PipelineContext) -> Dict[str, Dict[str, Any]]:
    updates: Dict[str, Dict[str, Any]] = {}
    for wallet, cluster_id in (ctx.clusters.labels if ctx.clusters else {}).items():
        updates.setdefault(wallet, {})["cluster_id"] = cluster_id
    for wallet, score in (ctx.risk.wallet_risk if ctx.risk else {}).items():
        updates.setdefault(wallet, {})["risk_score"] = score
    for wallet in list(updates.keys()):
        updates[wallet]["is_seed"] = wallet in ctx.seeds
    for txid, score in (ctx.tx_anomaly.score_map() if ctx.tx_anomaly else {}).items():
        updates.setdefault(txid, {})["anomaly_score"] = score
    for txid, score in (ctx.peeling.tx_peel_score if ctx.peeling else {}).items():
        updates.setdefault(txid, {})["peel_score"] = score
    for txid, flag in (ctx.peeling.tx_is_mixing if ctx.peeling else {}).items():
        updates.setdefault(txid, {})["is_mixing"] = bool(flag)
    # Transactions carry `risk_score` too, not a differently-named twin.  Wallet nodes are
    # created with `risk_score` (see GraphBuilder._touch_wallet) and every reader -- the
    # dashboard's colouring, its node-risk filter and sort, and the standalone pyvis export
    # -- reads `node["risk_score"]` for *all* node types.  Writing `tx_risk_score` here meant
    # all 1,200 transaction nodes silently resolved to risk 0.000 in the UI while the alert
    # for that same txid reported risk 0.95.
    for txid, score in (ctx.risk.tx_risk if ctx.risk else {}).items():
        updates.setdefault(txid, {})["risk_score"] = score
    return updates


def _write_alerts(alerts: Sequence[Dict[str, Any]], output_dir: Path) -> Path:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(alerts),
        "alerts": json_safe(list(alerts)),
    }
    path = output_dir / "alerts.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_summary(summary: Dict[str, Any], output_dir: Path) -> Path:
    path = output_dir / "pipeline_summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def _write_explainer_meta(ctx: PipelineContext) -> None:
    meta = {
        "explainer": "importance_surrogate",
        "shap_installed": explain_module.shap_available(),
        "transaction_importance": ctx.tx_anomaly.feature_importance if ctx.tx_anomaly else {},
        "wallet_importance": ctx.wallet_anomaly.feature_importance if ctx.wallet_anomaly else {},
        "note": (
            "Feature contributions come from model importance multiplied by the entity's "
            "standardised deviation. Install shap==0.46.0 (see requirements-shap.txt) to "
            "switch the top transaction alerts to real KernelSHAP values."
        ),
    }
    config.EXPLAINER_META_PATH.write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")


def _empty_summary(input_dir: Path, output_dir: Path, reason: str) -> Dict[str, Any]:
    return {
        "status": "empty",
        "reason": reason,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"transactions": 0, "network_records": 0, "alerts": 0},
        "alerts_preview": [],
        "stages": [],
    }


def load_pipeline_summary(path: Optional[Path | str] = None) -> Dict[str, Any]:
    # Resolved at call time, not bound as a default: tests (and any caller that points
    # config at another workspace) patch ``config``, and an import-time default would
    # keep reading the original path while appearing to honour the patch.
    path = Path(path or config.SUMMARY_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_alerts(path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    path = Path(path or config.ALERTS_PATH)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, dict):
        return payload.get("alerts", [])
    if isinstance(payload, list):
        return payload
    return []
