"""Stage 6 - Explainability and ranked alerts (spec section 11.6).

For every flagged wallet or transaction this module produces the alert object the
dashboard renders and the investigator hands to a supervisor:

    {
      "alert_id": "CT-W-1A2B3C4D",
      "entity": "bc1q...",
      "entity_type": "wallet",
      "risk_score": 0.87,
      "confidence": 0.81,
      "reasons": [
        "Participates in peeling chain PC-01 of length 6 hops",
        "2 hops from known illicit seed wallet",
        "High anomaly score (IsolationForest, 99th percentile)",
        "Belongs to cluster of 14 wallets that share common-input ownership"
      ],
      "top_features": [{"feature": "pagerank_risk", "contribution": 0.28, "value": 0.62}, ...],
      "evidence": {...},
      "evidence_hash": "sha256..."
    }

Feature attribution prefers **real SHAP values** (KernelSHAP over the trained
Isolation Forest).  SHAP is deliberately optional in this prototype: it is not in
``requirements.txt`` because it forces numpy>=2 and breaks the pinned core stack,
so when ``shap`` is absent the module falls back to a clearly-labelled
**importance surrogate** - global model importance multiplied by how far this
entity's feature value deviates from the population - and stamps
``"explainer": "importance_surrogate"`` on the alert so nobody is misled about
which method produced the numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src import config
from src.graph.builder import GraphBuilder, _as_list
from src.ml.anomaly import AnomalyResult
from src.ml.clustering import ClusterResult
from src.ml.peeling import PeelResult
from src.ml.risk import RiskResult, explain_risk_path
from src.utils.hashing import new_alert_id, stamp_evidence_hash as _stamp
from src.utils.helpers import clamp, safe_div

logger = logging.getLogger(__name__)


def utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def shap_available() -> bool:
    try:
        import shap as _shap  # noqa: F401

        return _shap is not None
    except Exception:
        return False


@dataclass
class AlertBundle:
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    explainer_used: str = "importance_surrogate"
    shap_explained: int = 0
    candidates_considered: int = 0

    def summary(self) -> Dict[str, Any]:
        if not self.alerts:
            return {"alerts": 0, "explainer": self.explainer_used}
        scores = [a["risk_score"] for a in self.alerts]
        return {
            "alerts": len(self.alerts),
            "wallet_alerts": sum(1 for a in self.alerts if a["entity_type"] == "wallet"),
            "transaction_alerts": sum(1 for a in self.alerts if a["entity_type"] == "txid"),
            "highest_risk": round(max(scores), 4),
            "mean_risk": round(float(np.mean(scores)), 4),
            "mean_confidence": round(float(np.mean([a["confidence"] for a in self.alerts])), 4),
            "alerts_above_0_7": sum(1 for score in scores if score >= 0.7),
            "explainer": self.explainer_used,
            "shap_explained_alerts": self.shap_explained,
            "candidates_considered": self.candidates_considered,
        }


class AlertExplainer:
    """Turns ML outputs into ranked, explainable, hash-stamped alerts."""

    def __init__(
        self,
        builder: GraphBuilder,
        tx_features: pd.DataFrame,
        wallet_features: pd.DataFrame,
        anomaly: Optional[AnomalyResult] = None,
        peeling: Optional[PeelResult] = None,
        risk: Optional[RiskResult] = None,
        clusters: Optional[ClusterResult] = None,
        correlated: Optional[pd.DataFrame] = None,
        seeds: Optional[Sequence[str]] = None,
        anomaly_model: Any = None,
        anomaly_detector: Any = None,
        wallet_anomaly: Optional[AnomalyResult] = None,
    ) -> None:
        self.builder = builder
        self.tx_features = tx_features
        self.wallet_features = wallet_features
        self.anomaly = anomaly
        self.wallet_anomaly = wallet_anomaly
        self.peeling = peeling or PeelResult()
        self.risk = risk or RiskResult()
        self.clusters = clusters
        self.correlated = correlated if correlated is not None else pd.DataFrame()
        self.seeds = list(seeds or [])
        self.anomaly_detector = anomaly_detector
        self._tx_feature_index = (
            tx_features.set_index("txid") if not tx_features.empty and "txid" in tx_features.columns else pd.DataFrame()
        )
        self._wallet_feature_index = (
            wallet_features.set_index("wallet")
            if not wallet_features.empty and "wallet" in wallet_features.columns
            else pd.DataFrame()
        )
        self._shap_cache: Dict[str, Dict[str, float]] = {}
        # pre-computed per-column statistics: attribution runs once per candidate and
        # recomputing pandas means/stds each time dominated the whole pipeline runtime
        self._tx_stats = self._column_stats(self._tx_feature_index)
        self._wallet_stats = self._column_stats(self._wallet_feature_index)
        if self.anomaly is not None:
            self.tx_importance = dict(self.anomaly.feature_importance)
        else:
            self.tx_importance = {}
        self.wallet_importance = (
            dict(self.wallet_anomaly.feature_importance) if self.wallet_anomaly is not None else {}
        )
        self._tx_correlated = self._group_correlated()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _group_correlated(self) -> Dict[str, List[Dict[str, Any]]]:
        if self.correlated is None or self.correlated.empty or "txid" not in self.correlated.columns:
            return {}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for txid, group in self.correlated.groupby("txid"):
            records = []
            for _, row in group.head(5).iterrows():
                records.append(
                    {
                        "src_ip": row.get("src_ip"),
                        "dst_ip": row.get("dst_ip"),
                        "dst_port": row.get("dst_port"),
                        "geo_country": row.get("geo_country"),
                        "geo_asn": row.get("geo_asn"),
                        "asn_org": row.get("asn_org"),
                        "time_delta_seconds": round(float(row.get("time_delta_seconds") or 0.0), 2),
                        "correlation_method": row.get("correlation_method"),
                        "correlation_confidence": round(float(row.get("correlation_confidence") or 0.0), 4),
                    }
                )
            groups[str(txid)] = records
        return groups

    def _tx_feature_row(self, txid: str) -> Dict[str, Any]:
        if self._tx_feature_index.empty or txid not in self._tx_feature_index.index:
            return {}
        return self._tx_feature_index.loc[txid].to_dict()

    def _wallet_feature_row(self, wallet: str) -> Dict[str, Any]:
        if self._wallet_feature_index.empty or wallet not in self._wallet_feature_index.index:
            return {}
        return self._wallet_feature_index.loc[wallet].to_dict()

    def _anomaly_score(self, txid: str) -> float:
        if self.anomaly is None:
            return 0.0
        return float(self.anomaly.score_map().get(txid, 0.0))

    def _wallet_anomaly_score(self, wallet: str) -> float:
        if self.wallet_anomaly is None:
            return 0.0
        return float(self.wallet_anomaly.score_map().get(wallet, 0.0))

    # ------------------------------------------------------------------ #
    # feature attribution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _column_stats(index: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
        stats: Dict[str, Tuple[float, float]] = {}
        if index is None or index.empty:
            return stats
        for column in index.columns:
            values = pd.to_numeric(index[column], errors="coerce")
            if values.notna().sum() == 0:
                continue
            std = float(values.std(ddof=0)) or 1.0
            stats[column] = (float(values.mean()), std if std > 0 else 1.0)
        return stats

    def _surrogate_contributions(
        self,
        feature_row: Dict[str, Any],
        importance: Dict[str, float],
        stats: Optional[Dict[str, Tuple[float, float]]] = None,
        top_k: int = config.TOP_FEATURES_PER_ALERT,
    ) -> List[Dict[str, Any]]:
        """importance x |standardised deviation from the population|."""
        if not importance or not feature_row:
            return []
        stats = stats if stats is not None else self._tx_stats
        contributions: List[Tuple[str, float, float]] = []
        for feature, weight in importance.items():
            if feature not in feature_row or feature not in stats:
                continue
            try:
                numeric = float(feature_row[feature])
            except (TypeError, ValueError):
                continue
            mean, std = stats[feature]
            deviation = abs(numeric - mean) / std
            contributions.append((feature, float(weight) * deviation, numeric))

        ranked = sorted(contributions, key=lambda item: item[1], reverse=True)[:top_k]
        # renormalise over the features actually reported, so the panel reads as
        # "100% of the attributed evidence" rather than a truncated share
        total = sum(item[1] for item in ranked)
        if total <= 0:
            return []
        return [
            {"feature": name, "contribution": round(score / total, 4), "value": round(value, 6)}
            for name, score, value in ranked
        ]

    def _shap_contributions(
        self, feature_row: Dict[str, Any], top_k: int = config.TOP_FEATURES_PER_ALERT
    ) -> Optional[List[Dict[str, Any]]]:
        """KernelSHAP over the trained Isolation Forest, when shap is installed."""
        if not shap_available() or self.anomaly_detector is None or self._tx_feature_index.empty:
            return None
        try:
            import shap

            detector = self.anomaly_detector
            features = detector.feature_columns
            values = [float(feature_row.get(name, 0.0) or 0.0) for name in features]
            background = self._tx_feature_index[features].fillna(0.0).astype(float)
            if len(background) > config.SHAP_BACKGROUND_SIZE:
                background = background.sample(config.SHAP_BACKGROUND_SIZE, random_state=config.RANDOM_SEED)

            def predict(matrix: np.ndarray) -> np.ndarray:
                return detector.model.score_samples(detector.scaler.transform(matrix))

            explainer = shap.KernelExplainer(predict, background.values)
            shap_values = explainer.shap_values(np.array([values]), nsamples=config.SHAP_MAX_EVALS)
            vector = np.asarray(shap_values)
            if vector.ndim > 1:
                vector = vector.reshape(-1)[: len(features)]
            ranked = sorted(zip(features, vector.tolist()), key=lambda item: abs(item[1]), reverse=True)[:top_k]
            total = sum(abs(value) for _, value in ranked) or 1.0
            return [
                {
                    "feature": name,
                    "contribution": round(value / total, 4),
                    "value": round(float(feature_row.get(name, 0.0) or 0.0), 6),
                }
                for name, value in ranked
            ]
        except Exception as exc:  # pragma: no cover - depends on optional shap install
            logger.warning("SHAP attribution failed (%s); using importance surrogate", exc)
            return None

    # ------------------------------------------------------------------ #
    # reasons
    # ------------------------------------------------------------------ #
    def _reason_lines(
        self,
        entity: str,
        entity_type: str,
        wallet_row: Dict[str, Any],
        chain_info: Optional[Dict[str, Any]],
        hops: Optional[int],
        anomaly_score: float,
        mixing_score: float,
        network_records: List[Dict[str, Any]],
        cluster_size: int,
        is_seed: bool,
    ) -> List[str]:
        reasons: List[str] = []
        if is_seed:
            reasons.append("Wallet is on the known-illicit seed list used to bootstrap this analysis")

        if chain_info:
            reasons.append(
                f"Participates in peeling chain {chain_info['chain_id']} of length "
                f"{chain_info['chain_length']} hops (hop {chain_info['hop'] + 1})"
            )
            if chain_info.get("starts_at_seed"):
                reasons.append("Peeling chain starts at a known illicit seed wallet")
            if chain_info.get("reaches_seed"):
                reasons.append("Peeling chain routes peeled value into a known illicit seed wallet")
            reasons.append(
                f"Chain moved {chain_info['total_value_moved']:.4f} BTC across "
                f"{chain_info['chain_length']} hops with mean asymmetry {chain_info['mean_asymmetry']:.2f}"
            )
        elif float(wallet_row.get("peel_chain_length") or 0) > 0:
            reasons.append(
                f"Belongs to a peeling chain of length {int(wallet_row['peel_chain_length'])} hops"
            )
        elif wallet_row.get("fraction_of_peel_like_txs"):
            fraction = float(wallet_row["fraction_of_peel_like_txs"])
            if fraction >= 0.5:
                reasons.append(
                    f"{fraction:.0%} of this wallet's spends are peel-shaped (1 input, 2 asymmetric outputs)"
                )

        if mixing_score > 0.5:
            reasons.append(
                f"CoinJoin-like mixing structure detected (score {mixing_score:.2f}: many inputs, near-equal outputs)"
            )

        if anomaly_score > 0:
            percentile = int(min(round(anomaly_score * 100), 100))
            reasons.append(
                f"{'High' if anomaly_score >= 0.9 else 'Elevated'} anomaly score from the Isolation Forest "
                f"model ({percentile}th percentile)"
            )

        if hops is not None:
            if hops == 0:
                reasons.append("This is a seed wallet: risk is fully seeded by analyst input")
            elif hops <= 2:
                reasons.append(f"{hops} hop{'s' if hops > 1 else ''} from a known illicit seed wallet")
            elif hops <= config.RISK_BFS_MAX_HOPS:
                reasons.append(f"{hops} hops from a known illicit seed wallet (risk decayed by distance)")

        if config.CLUSTER_SIZE_WARN <= cluster_size <= config.CLUSTER_SIZE_REPORT_MAX:
            reasons.append(
                f"Belongs to a cluster of {cluster_size} wallets that share common-input ownership "
                "(likely one real-world owner)"
            )
            if self._cluster_tainted(self.clusters.cluster_of(entity) if self.clusters else None):
                reasons.append(
                    "The ownership cluster contains a wallet that is already high-risk, so the whole "
                    "entity is treated as exposed"
                )
        elif entity_type == "wallet" and cluster_size > config.CLUSTER_SIZE_REPORT_MAX:
            # Say the failure out loud: an analyst who sees no cluster evidence on a wallet
            # that is plainly grouped with hundreds of others deserves to know why.
            reasons.append(
                f"Excluded from ownership-based reasoning: this address sits in a "
                f"{cluster_size}-wallet component, above the {config.CLUSTER_INHERIT_MAX_SIZE}-wallet "
                "limit where the common-input heuristic is treated as a chain-merge collision"
            )

        if network_records:
            best = max(network_records, key=lambda r: r.get("correlation_confidence") or 0)
            countries = sorted({r.get("geo_country") for r in network_records if r.get("geo_country")})
            country_text = f" from {', '.join(countries)}" if countries else ""
            reasons.append(
                f"{len(network_records)} network record(s) correlated with this transaction{country_text} "
                f"(best confidence {best.get('correlation_confidence', 0):.2f} via {best.get('correlation_method', 'n/a')})"
            )

        fee_ratio = float(wallet_row.get("fee_ratio") or 0.0)
        if fee_ratio > 0.1:
            reasons.append(f"Fee-to-input ratio is unusually high ({fee_ratio:.1%} of the value spent)")

        burstiness = float(wallet_row.get("burstiness") or 0)
        if burstiness >= 8:
            reasons.append(f"Burst behaviour: {int(burstiness)} transactions inside a single 60-second window")

        if entity_type == "wallet" and not reasons:
            reasons.append("Flagged by the composite risk score; no single dominant pattern")
        return reasons

    # ------------------------------------------------------------------ #
    # alert builders
    # ------------------------------------------------------------------ #
    def _score_components(
        self,
        risk_score: float,
        anomaly_score: float,
        peel_score: float,
        mixing_score: float,
        correlation_confidence: float,
        cluster_size: int,
        cluster_tainted: bool = False,
    ) -> Dict[str, float]:
        # a huge Louvain community is a weak signal on its own, so cap its contribution;
        # a *tainted* cluster (one member already above the risk threshold, or a seed)
        # promotes the whole ownership group - that is the payoff of entity clustering.
        # A *collapsed* component is the exception: once the transitive common-input
        # merge fuses hundreds of unrelated wallets, "the entity is exposed" stops being
        # an investigative claim, so the component earns no evidence at all.  risk.py
        # already refuses to inherit risk through these; both layers must agree or an
        # alert can claim exposure that the risk model deliberately rejected.
        collapsed = cluster_size > config.CLUSTER_INHERIT_MAX_SIZE
        if collapsed:
            cluster_evidence = 0.0
        elif cluster_tainted:
            cluster_evidence = 1.0
        else:
            cluster_evidence = (
                clamp(safe_div(cluster_size, 4.0))
                if 1 < cluster_size <= config.CLUSTER_SIZE_REPORT_MAX
                else 0.0
            )
        components = {
            "peel": clamp(peel_score),
            "mixing": clamp(mixing_score),
            "anomaly": clamp(anomaly_score),
            "risk": clamp(risk_score),
            "cluster": cluster_evidence,
            "correlation": clamp(correlation_confidence),
        }
        return components

    @staticmethod
    def _composite(components: Dict[str, float]) -> float:
        """Weighted evidence blend, floored by the strongest single piece of evidence.

        The blend alone would dilute a known-bad seed wallet to ~0.34 just because no
        other detector fired on it, which is misleading.  So the score is raised to a
        severity ladder value when one *corroborated* signal is very strong:

            wallet risk 1.0  -> at least 0.95   (known-bad, direct analyst input)
            peel score  0.8  -> at least 0.68   (multi-hop laundering structure)
            mixing      0.8  -> at least 0.60   (CoinJoin-shaped)
            anomaly     0.99 -> at least 0.69   (statistically extreme, single signal)
        """
        blended = sum(config.SCORE_WEIGHTS[key] * value for key, value in components.items())
        total_weight = sum(config.SCORE_WEIGHTS.values())
        blended = blended / total_weight
        ladder = max(
            config.SEVERITY_LADDER["risk"] * components.get("risk", 0.0),
            config.SEVERITY_LADDER["peel"] * components.get("peel", 0.0),
            config.SEVERITY_LADDER["mixing"] * components.get("mixing", 0.0),
            config.SEVERITY_LADDER["anomaly"] * components.get("anomaly", 0.0),
        )
        return clamp(max(blended, ladder))

    @staticmethod
    def _confidence(components: Dict[str, float]) -> float:
        """Agreement-based confidence: strong, independent signals that agree -> high."""
        signals = [value for key, value in components.items() if value > 0.0]
        if not signals:
            return 0.0
        top = sorted(signals, reverse=True)[:3]
        strength = float(np.mean(top))
        independent = sum(1 for value in components.values() if value >= 0.40)
        agreement = clamp(independent / 3.0, 0.0, 1.0)
        return clamp(0.60 * strength + 0.40 * agreement, config.ALERT_CONFIDENCE_FLOOR, 1.0)

    # Plain-English names for the six evidence components, so a counterfactual reads
    # like a sentence instead of a dictionary key.
    EVIDENCE_LABELS = {
        "peel": "the peeling-chain evidence",
        "mixing": "the CoinJoin / mixing evidence",
        "anomaly": "the statistical anomaly score",
        "risk": "risk inherited from the known-bad seed wallets",
        "cluster": "the ownership-cluster evidence",
        "correlation": "the network-correlation evidence",
    }

    def _annotate_assessment(self, alert: Dict[str, Any]) -> None:
        """Separate "statistically unusual" from "shaped like money laundering".

        The Isolation Forest rates *oddness*, not guilt - a large exchange sweep and a
        payout run are both genuinely unusual, and the benchmark in
        ``scripts/benchmark_detectors.py`` shows the model flags them.  Pretending the
        two are the same thing is how an alert list loses an analyst's trust, so an
        alert whose only strong signal is the anomaly score is labelled as such, in the
        alert itself: an untyped outlier to check, not a laundering lead to escalate.
        """
        components = alert.get("components") or {}
        evidence = alert.get("evidence") or {}
        typed = max(
            float(components.get("peel", 0.0)),
            float(components.get("mixing", 0.0)),
            float(components.get("risk", 0.0)),
            float(components.get("cluster", 0.0)),
        )
        anomaly = float(components.get("anomaly", 0.0))
        untyped = (
            anomaly >= 0.5
            and typed < 0.30
            and not evidence.get("is_seed_wallet")
        )
        if not untyped:
            alert["assessment"] = "corroborated"
            alert["assessment_note"] = (
                "A laundering-shaped pattern (peeling chain, CoinJoin, seed exposure or "
                "tainted ownership cluster) corroborates the statistical signal."
            )
            return
        alert["assessment"] = "unusual-but-untyped"
        anomaly_reason = (
            "Statistically unusual, but no laundering pattern is corroborated yet: this "
            "alert rests on the anomaly score alone. Innocent structures - an exchange "
            "consolidation, a payout run, a batched payment - have this shape too, so "
            "confirm the flow's business purpose before escalating."
        )
        alert["reasons"] = [anomaly_reason] + list(alert.get("reasons") or [])
        alert["assessment_note"] = anomaly_reason
        alert["recommended_action"] = (
            "Review - statistical outlier: confirm whether an exchange or payout "
            "structure explains this flow before escalating"
        )

    def _counterfactuals(
        self, components: Dict[str, float], score: float
    ) -> List[Dict[str, Any]]:
        """Ask the scoring function what would *clear* this entity.

        SHAP answers "why is the score high?".  This answers the question the analyst
        actually has next: **which single piece of evidence is holding this alert up?**
        Each component is removed on its own and the alert is re-scored with the same
        weights and severity ladder.

        ``still_flagged == False`` is the interesting case: without that one signal the
        entity would drop off the list, so that is where verification effort belongs.
        """
        if not components:
            return []
        rows: List[Dict[str, Any]] = []
        for key, value in sorted(components.items(), key=lambda item: -item[1]):
            if value <= 0.0:
                continue
            ablated = dict(components)
            ablated[key] = 0.0
            without = self._composite(ablated)
            drop = score - without
            if drop < 0.02:
                continue
            still_flagged = without >= config.MIN_ALERT_RISK
            rows.append(
                {
                    "evidence": key,
                    "label": self.EVIDENCE_LABELS.get(key, key),
                    "score_without": round(without, 4),
                    "score_drop": round(drop, 4),
                    "still_flagged": still_flagged,
                    "decisive": not still_flagged,
                }
            )
        rows = rows[:3]
        decisive = [row for row in rows if row["decisive"]]
        if decisive:
            top = min(decisive, key=lambda row: row["score_without"])
            summary = (
                f"Removing {top['label']} alone would drop this entity to "
                f"{top['score_without']:.2f} and take it off the alert list - "
                f"that is the evidence to verify first."
            )
        elif rows:
            summary = (
                f"No single signal clears this entity: the strongest removal ({rows[0]['label']}) "
                f"still leaves it at {rows[0]['score_without']:.2f}. The alert rests on "
                f"corroborating evidence, which is a stronger case."
            )
        else:
            summary = "Every component contributes little on its own - the alert is a weak aggregate."
        return rows, summary

    @staticmethod
    def _recommended_action(score: float, components: Dict[str, float], hops: Optional[int], is_seed: bool) -> str:
        if is_seed:
            return "Escalate immediately - sanctioned/known-bad wallet"
        if score >= 0.75 or (hops is not None and hops <= 1):
            return "Escalate - request full wallet history and freeze-linked counterparties"
        if score >= config.MIN_ALERT_RISK:
            return "Investigate - enrich with exchange/KYC data"
        return "Monitor - queue for the next review cycle"

    def _cluster_tainted(self, cluster_id: Optional[int]) -> bool:
        """True when any member of this ownership cluster is already high risk or a seed."""
        if cluster_id is None or self.clusters is None:
            return False
        for member in self.clusters.members.get(cluster_id, []):
            if member in self.seeds:
                return True
            if float(self.risk.wallet_risk.get(member, 0.0)) >= config.MIN_ALERT_RISK:
                return True
            if float(self.peeling.wallet_peel_score.get(member, 0.0)) >= 0.5:
                return True
        return False

    def _build_wallet_alert(self, wallet: str) -> Optional[Dict[str, Any]]:
        row = self._wallet_feature_row(wallet)
        if not row:
            return None
        is_seed = wallet in self.seeds or bool(row.get("is_seed"))
        risk_score = float(self.risk.wallet_risk.get(wallet, 0.0))
        anomaly_score = self._wallet_anomaly_score(wallet)
        peel_score = float(self.peeling.wallet_peel_score.get(wallet, 0.0))
        mixing_count = int(self.peeling.wallet_mixing_count.get(wallet, 0))
        mixing_score = clamp(mixing_count / 3.0)
        hops = self.risk.hops_to_seed.get(wallet)
        cluster_id = self.clusters.cluster_of(wallet) if self.clusters else None
        cluster_size = int(self.clusters.sizes.get(cluster_id, 1)) if (self.clusters and cluster_id is not None) else 1

        chain = next(
            (c for c in self.peeling.chains if wallet in c.wallets),
            None,
        )
        chain_info = None
        if chain is not None:
            chain_info = {
                "chain_id": chain.chain_id,
                "chain_length": chain.length,
                "hop": next(
                    (hop["hop"] for hop in chain.hops if wallet in (hop["from_wallet"], hop["continue_wallet"])),
                    0,
                ),
                "starts_at_seed": chain.starts_at_seed,
                "reaches_seed": chain.reaches_seed,
                "total_value_moved": chain.total_value_moved,
                "mean_asymmetry": chain.mean_asymmetry,
            }

        network_records: List[Dict[str, Any]] = []
        for txid in list(self.builder.graph.successors(wallet)) + list(self.builder.graph.predecessors(wallet)):
            if self.builder.graph.nodes.get(txid, {}).get("node_type") == "txid":
                network_records.extend(self._tx_correlated.get(txid, []))
        network_records = network_records[:5]
        correlation_confidence = max((r.get("correlation_confidence", 0.0) for r in network_records), default=0.0)

        cluster_tainted = self._cluster_tainted(cluster_id)
        components = self._score_components(
            risk_score, anomaly_score, peel_score, mixing_score, correlation_confidence,
            cluster_size, cluster_tainted=cluster_tainted,
        )
        score = self._composite(components)
        if not is_seed and score < config.MIN_ALERT_RISK and peel_score < 0.5:
            return None

        reasons = self._reason_lines(
            wallet, "wallet", row, chain_info, hops, anomaly_score, mixing_score,
            network_records, cluster_size, is_seed,
        )
        top_features = self._surrogate_contributions(
            row, self.wallet_importance, stats=self._wallet_stats
        ) or self._property_contributions("wallet", row)

        alert = {
            "alert_id": new_alert_id(wallet, "wallet"),
            "entity": wallet,
            "entity_type": "wallet",
            "risk_score": round(score, 4),
            "confidence": round(self._confidence(components), 4),
            "reasons": reasons,
            "top_features": top_features,
            "explainer": "importance_surrogate",
            "components": {key: round(value, 4) for key, value in components.items()},
            "recommended_action": self._recommended_action(score, components, hops, is_seed),
            "generated_at": utc_iso(),
            "evidence": {
                "wallet": wallet,
                "is_seed_wallet": is_seed,
                "hops_to_seed": hops,
                "pagerank_risk": round(float(self.risk.wallet_risk_pagerank.get(wallet, 0.0)), 4),
                "proximity_risk": round(float(self.risk.wallet_risk_proximity.get(wallet, 0.0)), 4),
                "risk_paths_to_seed": explain_risk_path(self.builder, wallet, self.seeds, max_paths=2),
                "cluster": {
                    "cluster_id": cluster_id,
                    "size": cluster_size,
                    "method": self.clusters.methods.get(wallet) if self.clusters else None,
                    "tainted": cluster_tainted,
                    "example_members": self.clusters.describe(cluster_id) if (self.clusters and cluster_id is not None) else [],
                },
                "peel_chain": chain_info,
                "financials": {
                    "total_received": round(float(row.get("total_received", 0.0)), 8),
                    "total_sent": round(float(row.get("total_sent", 0.0)), 8),
                    "n_transactions": int(row.get("n_txs", 0)),
                    "unique_counterparties": int(row.get("unique_counterparties", 0)),
                    "first_seen": row.get("first_seen"),
                    "last_seen": row.get("last_seen"),
                    "max_fan_in": int(row.get("max_fan_in", 0)),
                    "max_fan_out": int(row.get("max_fan_out", 0)),
                },
                "network_evidence": network_records,
                "related_transactions": self._related_transactions(wallet),
            },
        }
        return _stamp(alert)

    def _related_transactions(self, wallet: str, limit: int = 10) -> List[Dict[str, Any]]:
        related: List[Dict[str, Any]] = []
        neighbours = list(self.builder.graph.successors(wallet)) + list(self.builder.graph.predecessors(wallet))
        for txid in neighbours:
            info = self.builder.tx_index.get(txid)
            if not info:
                continue
            related.append(
                {
                    "txid": txid,
                    "timestamp": info["timestamp"].isoformat() if hasattr(info["timestamp"], "isoformat") else None,
                    "num_inputs": info["num_inputs"],
                    "num_outputs": info["num_outputs"],
                    "total_in": round(info["total_in"], 8),
                    "fee": round(info["fee"], 8),
                    "script_type": info["script_type"],
                    "role": "spent_from" if txid in self.builder.graph.successors(wallet) else "received_into",
                    "peel_score": round(float(self.peeling.tx_peel_score.get(txid, 0.0)), 4),
                    "anomaly_score": round(self._anomaly_score(txid), 4),
                }
            )
        related.sort(key=lambda item: (item["peel_score"], item["anomaly_score"]), reverse=True)
        return related[:limit]

    def _build_tx_alert(self, txid: str) -> Optional[Dict[str, Any]]:
        row = self._tx_feature_row(txid)
        info = self.builder.tx_index.get(txid)
        if not row or info is None:
            return None
        peel_score = float(self.peeling.tx_peel_score.get(txid, 0.0))
        mixing_score = float(self.peeling.tx_mixing_score.get(txid, 0.0))
        anomaly_score = self._anomaly_score(txid)
        risk_score = float(self.risk.tx_risk.get(txid, 0.0))
        network_records = self._tx_correlated.get(txid, [])
        correlation_confidence = max((r.get("correlation_confidence", 0.0) for r in network_records), default=0.0)

        is_peel = bool(self.peeling.tx_is_peel.get(txid))
        is_mixing = bool(self.peeling.tx_is_mixing.get(txid))
        anomaly_flag = bool(self.anomaly.flag_map().get(txid, False)) if self.anomaly else False
        if not (is_peel or is_mixing or anomaly_flag):
            return None

        tx_row = self.builder.transactions[self.builder.transactions["txid"] == txid]
        tx_inputs = [str(a) for a in _as_list(tx_row.iloc[0].get("inputs"))] if not tx_row.empty else []
        tx_outputs = [str(a) for a in _as_list(tx_row.iloc[0].get("outputs"))] if not tx_row.empty else []

        wallet_max_risk = 0.0
        for address in tx_inputs + tx_outputs:
            wallet_max_risk = max(wallet_max_risk, float(self.risk.wallet_risk.get(address, 0.0)))

        hops: Optional[int] = None
        hops_candidates = [self.risk.hops_to_seed.get(address) for address in tx_inputs + tx_outputs]
        hops_candidates = [h for h in hops_candidates if h is not None]
        if hops_candidates:
            hops = int(min(hops_candidates))

        components = self._score_components(
            max(risk_score, wallet_max_risk), anomaly_score, peel_score, mixing_score,
            correlation_confidence, 1,
        )
        score = self._composite(components)

        chain_id = self.peeling.tx_chain_id.get(txid)
        chain = next((c for c in self.peeling.chains if c.chain_id == chain_id), None)
        chain_info = None
        if chain is not None:
            chain_info = {
                "chain_id": chain.chain_id,
                "chain_length": chain.length,
                "hop": self.peeling.tx_hop.get(txid, 0),
                "starts_at_seed": chain.starts_at_seed,
                "reaches_seed": chain.reaches_seed,
                "total_value_moved": chain.total_value_moved,
                "mean_asymmetry": chain.mean_asymmetry,
            }

        reasons = self._reason_lines(
            txid, "txid", row, chain_info, hops, anomaly_score, mixing_score, network_records, 1, False
        )
        if mixing_score > 0.5:
            reasons.append(
                f"Structure matches a CoinJoin mix: {int(row.get('num_inputs', 0))} inputs, "
                f"{int(row.get('num_outputs', 0))} near-equal outputs (output CoV {float(row.get('out_amount_cv', 0.0)):.3f})"
            )
        if float(row.get("is_consolidation") or 0) and int(row.get("num_inputs", 0)) >= 12:
            reasons.append(
                f"Extreme fan-in: {int(row.get('num_inputs', 0))} inputs swept into {int(row.get('num_outputs', 0))} output(s)"
            )
        if float(row.get("is_fan_out") or 0):
            reasons.append(
                f"Extreme fan-out: one input split into {int(row.get('num_outputs', 0))} outputs"
            )

        top_features = self._surrogate_contributions(row, self.tx_importance)
        alert = {
            "alert_id": new_alert_id(txid, "txid"),
            "entity": txid,
            "entity_type": "txid",
            "risk_score": round(score, 4),
            "confidence": round(self._confidence(components), 4),
            "reasons": reasons,
            "top_features": top_features,
            "explainer": "importance_surrogate",
            "components": {key: round(value, 4) for key, value in components.items()},
            "recommended_action": self._recommended_action(score, components, hops, False),
            "generated_at": utc_iso(),
            "evidence": {
                "txid": txid,
                "timestamp": info["timestamp"].isoformat() if hasattr(info["timestamp"], "isoformat") else None,
                "num_inputs": info["num_inputs"],
                "num_outputs": info["num_outputs"],
                "total_in": round(info["total_in"], 8),
                "total_out": round(info["total_out"], 8),
                "fee": round(info["fee"], 8),
                "fee_ratio": round(float(row.get("fee_ratio", 0.0)), 6),
                "script_type": info["script_type"],
                "peel_chain": chain_info,
                "is_peel_hop": is_peel,
                "is_mixing": is_mixing,
                "anomaly_score": round(anomaly_score, 4),
                "anomaly_flagged": anomaly_flag,
                "wallet_risk_of_counterparties": round(wallet_max_risk, 4),
                "hops_to_seed": hops,
                "correlation": {
                    "confidence": round(correlation_confidence, 4),
                    "method": network_records[0]["correlation_method"] if network_records else None,
                    "records": network_records,
                },
                "addresses": {"inputs": tx_inputs[:12], "outputs": tx_outputs[:12]},
            },
        }
        return _stamp(alert)

    def _property_contributions(self, kind: str, row: Dict[str, Any], top_k: int = config.TOP_FEATURES_PER_ALERT) -> List[Dict[str, Any]]:
        """Fallback attribution when no anomaly model exists: normalised raw evidence."""
        if kind == "wallet":
            candidates = {
                "peel_chain_length": float(row.get("peel_chain_length", 0) or 0),
                "risk_proximity": float(row.get("risk_proximity", 0) or 0),
                "cluster_size": float(row.get("cluster_size", 0) or 0),
                "fraction_of_peel_like_txs": float(row.get("fraction_of_peel_like_txs", 0) or 0),
                "burstiness": float(row.get("burstiness", 0) or 0),
            }
        else:
            candidates = {
                "fee_ratio": float(row.get("fee_ratio", 0) or 0),
                "amount_ratio": float(row.get("amount_ratio", 0) or 0),
                "num_inputs": float(row.get("num_inputs", 0) or 0),
                "num_outputs": float(row.get("num_outputs", 0) or 0),
                "correlation_confidence": float(row.get("correlation_confidence", 0) or 0),
            }
        total = sum(abs(value) for value in candidates.values())
        if total <= 0:
            return []
        return [
            {"feature": name, "contribution": round(abs(value) / total, 4), "value": round(value, 6)}
            for name, value in sorted(candidates.items(), key=lambda kv: abs(kv[1]), reverse=True)[:top_k]
        ]

    # ------------------------------------------------------------------ #
    def build_alerts(self) -> AlertBundle:
        alerts: List[Dict[str, Any]] = []
        considered = 0

        for wallet in self.wallet_features.get("wallet", pd.Series(dtype=str)).tolist():
            considered += 1
            alert = self._build_wallet_alert(str(wallet))
            if alert is not None:
                alerts.append(alert)

        for txid in self.tx_features.get("txid", pd.Series(dtype=str)).tolist():
            considered += 1
            alert = self._build_tx_alert(str(txid))
            if alert is not None:
                alerts.append(alert)

        alerts.sort(key=lambda a: (a["risk_score"], a["confidence"]), reverse=True)
        alerts = self._ensure_diversity(alerts)[: config.MAX_ALERTS]

        # Counterfactuals are attached *after* ranking and before the evidence hash is
        # re-stamped, so the sealed package covers them too.
        for index, alert in enumerate(alerts):
            self._annotate_assessment(alert)
            rows, summary = self._counterfactuals(alert.get("components") or {}, alert["risk_score"])
            alert["counterfactuals"] = rows
            alert["counterfactual_summary"] = summary
            alerts[index] = _stamp(alert)

        bundle = AlertBundle(alerts=alerts, candidates_considered=considered)
        bundle.explainer_used = self._attach_shap(alerts)
        logger.info(
            "Alerts: %d ranked (of %d candidates), explainer=%s, top risk=%.3f",
            len(alerts), considered, bundle.explainer_used,
            alerts[0]["risk_score"] if alerts else 0.0,
        )
        return bundle

    @staticmethod
    def _category(alert: Dict[str, Any]) -> Optional[str]:
        """Which detector family does this alert mainly represent?"""
        evidence = alert.get("evidence") or {}
        if evidence.get("is_mixing"):
            return "mixing"
        if evidence.get("peel_chain"):
            return "peel"
        cluster = evidence.get("cluster") or {}
        if cluster.get("tainted"):
            return "cluster"
        if alert.get("entity_type") == "wallet" and evidence.get("is_seed_wallet"):
            return "risk"
        if evidence.get("anomaly_flagged") or evidence.get("anomaly_score"):
            return "anomaly"
        components = alert.get("components") or {}
        if components:
            return max(components.items(), key=lambda kv: kv[1])[0]
        return None

    def _ensure_diversity(
        self, alerts: List[Dict[str, Any]], per_category: int = config.ALERTS_PER_CATEGORY
    ) -> List[Dict[str, Any]]:
        """Guarantee that each detector family is represented in the ranked list.

        Ranking purely on score lets a dozen peeling hops bury the single CoinJoin or
        the only cluster finding.  Slots are reserved for the best finding of every
        family; everything else is still ranked by risk, highest first.
        """
        if per_category <= 0:
            return alerts[: config.MAX_ALERTS]
        categories = ("peel", "mixing", "anomaly", "risk", "cluster")
        reserve = per_category * len(categories)
        primary = list(alerts[: max(config.MAX_ALERTS - reserve, 1)])
        primary_ids = {alert["alert_id"] for alert in primary}

        guaranteed: List[Dict[str, Any]] = []
        for category in categories:
            matches = [
                alert for alert in alerts
                if self._category(alert) == category and alert["alert_id"] not in primary_ids
            ]
            guaranteed.extend(matches[:per_category])

        remaining = config.MAX_ALERTS - len(guaranteed)
        final = guaranteed + [alert for alert in alerts if alert["alert_id"] not in {
            item["alert_id"] for item in guaranteed
        }][: max(remaining, 0)]
        final.sort(key=lambda a: (a["risk_score"], a["confidence"]), reverse=True)
        return final

    def _attach_shap(self, alerts: List[Dict[str, Any]]) -> str:
        """Upgrade the top transaction alerts to real SHAP values when available."""
        if not shap_available():
            return "importance_surrogate"
        upgraded = 0
        for index, alert in enumerate(alerts):
            if alert["entity_type"] != "txid":
                continue
            if upgraded >= config.SHAP_MAX_ALERTS:
                break
            row = self._tx_feature_row(alert["entity"])
            values = self._shap_contributions(row)
            if not values:
                continue
            alert["top_features"] = values
            alert["explainer"] = "shap_kernel"
            alert["evidence"]["explanation_method"] = "shap_kernel (KernelSHAP over IsolationForest)"
            # re-seal this alert in place - indexing by the upgrade counter would have
            # overwritten unrelated alerts as soon as SHAP was installed
            alerts[index] = _stamp(alert)
            upgraded += 1
        for alert in alerts:
            alert.setdefault("evidence", {})
            alert["evidence"].setdefault("explanation_method", "importance_surrogate (model importance x deviation)")
        return "shap_kernel" if upgraded else "importance_surrogate"


def build_alerts(**kwargs: Any) -> AlertBundle:
    """Convenience wrapper used by the pipeline."""
    return AlertExplainer(**kwargs).build_alerts()
