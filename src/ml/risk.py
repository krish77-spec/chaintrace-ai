"""Stage 5 - Risk scoring (spec section 11.5).

Risk flows outward from known-bad ("seed") wallets.  Three mechanisms run in
parallel and are blended, because they answer different questions:

* **Personalized PageRank** (``networkx.pagerank`` with a ``personalization``
  vector over the seeds) - "how much of the seed's influence reaches this wallet
  through the transaction graph?"  Structural, and inherently damped by degree.
* **Hub-damped proximity decay** - "how close is this wallet to a known-bad
  wallet, and how hard was it to get here?"  A shortest-path search where passing
  *through* a high-degree wallet (an exchange-like hub with hundreds of
  counterparties) costs extra, because sharing a hub with a criminal is weak
  evidence while a direct payment chain is strong evidence.
* **Cluster inheritance** - wallets that co-spend (common-input ownership) are one
  real-world entity, so a risky member raises the whole cluster, discounted
  because the ownership inference is probabilistic.  Louvain communities are
  deliberately *not* used here: they are a soft grouping and would smear risk.

Seeds themselves are pinned at 1.0.  Transaction risk is derived from the
highest-risk wallet touching it, blended with the transaction's own anomaly,
peeling and mixing evidence.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import networkx as nx
import numpy as np

from src import config
from src.graph.builder import GraphBuilder, as_builder
from src.utils.helpers import clamp

logger = logging.getLogger(__name__)


def clamp_map(scores: Dict[str, float]) -> Dict[str, float]:
    return {key: round(clamp(score, 0.0, 1.0), 6) for key, score in scores.items()}


@dataclass
class RiskResult:
    wallet_risk: Dict[str, float] = field(default_factory=dict)
    tx_risk: Dict[str, float] = field(default_factory=dict)
    wallet_risk_pagerank: Dict[str, float] = field(default_factory=dict)
    wallet_risk_proximity: Dict[str, float] = field(default_factory=dict)
    hops_to_seed: Dict[str, int] = field(default_factory=dict)
    cluster_inherited: Dict[str, float] = field(default_factory=dict)
    seeds: List[str] = field(default_factory=list)
    method_used: str = "personalized_pagerank+hub_damped_decay"

    def summary(self) -> Dict[str, Any]:
        high = [w for w, score in self.wallet_risk.items() if score >= config.MIN_ALERT_RISK]
        return {
            "seeds": len(self.seeds),
            "wallets_scored": len(self.wallet_risk),
            "transactions_scored": len(self.tx_risk),
            "wallets_above_alert_threshold": len(high),
            "max_wallet_risk": round(max(self.wallet_risk.values()), 4) if self.wallet_risk else 0.0,
            "wallets_within_two_hops_of_seed": sum(
                1 for hops in self.hops_to_seed.values() if hops is not None and hops <= 2
            ),
            "wallets_raised_by_cluster_ownership": sum(
                1 for wallet, score in self.cluster_inherited.items()
                if score > self.wallet_risk_pagerank.get(wallet, 0.0)
            ),
            "method": self.method_used,
        }


def _personalized_pagerank(
    graph: nx.MultiDiGraph,
    seeds: Sequence[str],
    alpha: float = config.PAGERANK_ALPHA,
) -> Dict[str, float]:
    personalization = {seed: 1.0 / len(seeds) for seed in seeds}
    if not personalization:
        return {node: 0.0 for node in graph.nodes}
    try:
        ranking = nx.pagerank(graph, alpha=alpha, personalization=personalization, weight="weight")
    except Exception as exc:  # pragma: no cover - degenerate graph
        logger.warning("Personalized PageRank failed (%s); falling back to proximity-only risk", exc)
        return {node: 0.0 for node in graph.nodes}
    values = np.array(list(ranking.values()), dtype=float)
    top = values.max() if len(values) else 0.0
    if top <= 0:
        return {node: 0.0 for node in graph.nodes}
    return {str(node): float(score / top) for node, score in ranking.items()}


def _hub_penalty(graph: nx.Graph, node: str, hub_degree: int = config.HUB_DEGREE) -> float:
    """Extra cost for routing risk through a busy wallet (exchange/VPN/hub)."""
    degree = graph.degree(node)
    if degree <= hub_degree:
        return 0.0
    return math.log1p(degree / hub_degree)


def _hub_damped_decay(
    graph: nx.Graph,
    seeds: Sequence[str],
    max_hops: int = config.RISK_BFS_MAX_HOPS,
    decay: float = config.RISK_DECAY,
) -> tuple:
    """Multi-source weighted shortest path -> (proximity, hops).

    Edge cost = -log(decay) * (1 + hub_penalty(u) + hub_penalty(v)), so proximity
    stays ``decay ** hops`` along ordinary wallet chains and drops much faster when
    the path passes through a hub.
    """
    proximity: Dict[str, float] = {}
    hops: Dict[str, int] = {}
    if not seeds:
        return proximity, hops

    base_cost = -math.log(max(decay, 1e-6))
    weighted = nx.Graph()
    for src, dst, data in graph.edges(data=True):
        cost = base_cost * (
            1.0 + _hub_penalty(graph, src) + _hub_penalty(graph, dst)
        )
        weighted.add_edge(src, dst, cost=max(cost, 1e-9))
    for node in graph.nodes:
        if node not in weighted:
            weighted.add_node(node)

    sources = [seed for seed in seeds if seed in weighted]
    if not sources:
        return proximity, hops

    try:
        distances, paths = nx.multi_source_dijkstra(weighted, sources, weight="cost")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Weighted proximity search failed (%s); falling back to plain BFS", exc)
        distances, paths = {}, {}

    for node, distance in distances.items():
        hop_count = max(len(paths.get(node, [])) - 1, 0)
        if hop_count > max_hops:
            continue
        hops[node] = hop_count
        proximity[node] = float(math.exp(-distance))

    for seed in sources:
        hops[seed] = 0
        proximity[seed] = 1.0
    return proximity, hops


def propagate_cluster_risk(
    wallet_risk: Dict[str, float],
    cluster_labels: Dict[str, int],
    cluster_methods: Optional[Dict[str, str]] = None,
    discount: float = config.CLUSTER_RISK_DISCOUNT,
) -> Dict[str, float]:
    """Spread risk across wallets that share one owner (evidenced co-spending only)."""
    if not cluster_labels:
        return dict(wallet_risk)
    cluster_methods = cluster_methods or {}
    sizes: Dict[int, int] = {}
    for cluster_id in cluster_labels.values():
        sizes[cluster_id] = sizes.get(cluster_id, 0) + 1

    # "cluster collapse": transitive co-spend merging can fuse most of a dataset into
    # one pseudo-owner.  Inheriting risk inside such a blob would smear risk over the
    # whole graph, so collapsed clusters are excluded from inheritance (they are still
    # reported in the clustering summary so an analyst can inspect them).
    collapsed = {cid for cid, size in sizes.items() if size > config.CLUSTER_INHERIT_MAX_SIZE}
    if collapsed:
        logger.warning(
            "Cluster collapse guard: %d cluster(s) exceeded %d wallets and were excluded from risk inheritance",
            len(collapsed), config.CLUSTER_INHERIT_MAX_SIZE,
        )

    peak: Dict[int, float] = {}
    for wallet, cluster_id in cluster_labels.items():
        if cluster_id in collapsed:
            continue
        if cluster_methods and cluster_methods.get(wallet) != "common_input":
            continue
        peak[cluster_id] = max(peak.get(cluster_id, 0.0), float(wallet_risk.get(wallet, 0.0)))

    inherited = dict(wallet_risk)
    for wallet, cluster_id in cluster_labels.items():
        if cluster_id in collapsed:
            continue
        if cluster_methods and cluster_methods.get(wallet) != "common_input":
            continue
        floor = discount * peak.get(cluster_id, 0.0)
        if floor > inherited.get(wallet, 0.0):
            inherited[wallet] = round(clamp(floor), 6)
    return inherited


def propagate_risk(
    builder: GraphBuilder,
    seeds: Sequence[str],
    anomaly_scores: Optional[Dict[str, float]] = None,
    peel_scores: Optional[Dict[str, float]] = None,
    mixing_scores: Optional[Dict[str, float]] = None,
    cluster_labels: Optional[Dict[str, int]] = None,
    cluster_methods: Optional[Dict[str, str]] = None,
) -> RiskResult:
    """Run the full risk stage and return wallet + transaction risk scores."""
    builder = as_builder(builder)
    projection = builder.wallet_projection()
    undirected = builder.wallet_projection_undirected()
    wallet_nodes = [str(n) for n in projection.nodes]
    seeds = [seed for seed in seeds if seed in projection] or list(seeds)

    pagerank_scores = _personalized_pagerank(projection, seeds)
    proximity_scores, hop_map = _hub_damped_decay(undirected, seeds)

    result = RiskResult(seeds=list(seeds))
    for wallet in wallet_nodes:
        pr = pagerank_scores.get(wallet, 0.0)
        prox = proximity_scores.get(wallet, 0.0)
        blended = clamp(config.RISK_WEIGHT_PAGERANK * pr + config.RISK_WEIGHT_PROXIMITY * prox)
        if wallet in seeds:
            blended = config.SEED_RISK
        result.wallet_risk_pagerank[wallet] = round(pr, 6)
        result.wallet_risk_proximity[wallet] = round(prox, 6)
        result.wallet_risk[wallet] = round(blended, 6)
        if wallet in hop_map:
            result.hops_to_seed[wallet] = hop_map[wallet]

    # ---- inherit risk inside evidenced (common-input) clusters ---------- #
    if cluster_labels:
        before = dict(result.wallet_risk)
        result.wallet_risk = clamp_map(
            propagate_cluster_risk(result.wallet_risk, cluster_labels, cluster_methods)
        )
        result.cluster_inherited = {
            wallet: score for wallet, score in result.wallet_risk.items()
            if score > before.get(wallet, 0.0) + 1e-9
        }
        if result.cluster_inherited:
            logger.info(
                "Cluster ownership raised risk for %d wallets (%.0f%% inheritance discount)",
                len(result.cluster_inherited), (1 - config.CLUSTER_RISK_DISCOUNT) * 100,
            )

    # ---- propagate to transactions ------------------------------------ #
    anomaly_scores = anomaly_scores or {}
    peel_scores = peel_scores or {}
    mixing_scores = mixing_scores or {}

    for txid, info in builder.tx_index.items():
        wallets: List[str] = [str(a) for a in info.get("inputs", [])] + [
            str(a) for a in info.get("outputs", [])
        ]
        if not wallets and not builder.transactions.empty:
            row = builder.transactions[builder.transactions["txid"] == txid]
            if not row.empty:
                wallets = [str(a) for a in (row.iloc[0].get("inputs") or [])] + [
                    str(a) for a in (row.iloc[0].get("outputs") or [])
                ]
        if not wallets:
            wallets = [
                str(src) for src, _, edge in builder.graph.in_edges(txid, data=True)
                if edge.get("edge_type") == "in"
            ] + [
                str(dst) for _, dst, edge in builder.graph.out_edges(txid, data=True)
                if edge.get("edge_type") == "out"
            ]
        wallet_risk = max((result.wallet_risk.get(wallet, 0.0) for wallet in wallets), default=0.0)
        own_evidence = max(
            float(anomaly_scores.get(txid, 0.0)),
            float(peel_scores.get(txid, 0.0)),
            float(mixing_scores.get(txid, 0.0)),
        )
        result.tx_risk[txid] = round(
            clamp(config.TX_RISK_BLEND * wallet_risk + (1.0 - config.TX_RISK_BLEND) * own_evidence),
            6,
        )

    if cluster_labels:
        result.method_used = "personalized_pagerank+hub_damped_decay+cluster_inheritance"
    logger.info(
        "Risk propagation: %d wallets scored (%d above %.2f, %d within 2 hops of a seed), %d transactions scored",
        len(result.wallet_risk),
        sum(1 for score in result.wallet_risk.values() if score >= config.MIN_ALERT_RISK),
        config.MIN_ALERT_RISK,
        sum(1 for hops in result.hops_to_seed.values() if hops <= 2),
        len(result.tx_risk),
    )
    return result


def explain_risk_path(
    builder: GraphBuilder,
    wallet: str,
    seeds: Sequence[str],
    max_paths: int = 3,
) -> List[List[str]]:
    """Shortest evidence paths from the given wallet back to any seed wallet."""
    builder = as_builder(builder)
    undirected = builder.wallet_projection_undirected()
    if wallet not in undirected:
        return []
    paths: List[List[str]] = []
    for seed in seeds:
        if seed not in undirected or seed == wallet:
            continue
        try:
            path = nx.shortest_path(undirected, wallet, seed)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        paths.append(path)
        if len(paths) >= max_paths:
            break
    return paths
