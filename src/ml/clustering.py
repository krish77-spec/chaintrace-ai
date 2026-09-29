"""Stage 5 - Entity clustering (spec section 11.2).

Two layers, in the order the brief asks for:

1. **Common-input-ownership heuristic.**  Any addresses that appear together as
   inputs of the same transaction almost certainly share one owner, because
   spending multiple UTXOs in one transaction requires the private keys for all
   of them.  Implemented with a Union-Find over every multi-input transaction.
2. **Louvain community detection** on the wallet-to-wallet projection, used to
   group wallets that do not literally co-spend but behave like one entity
   (funding each other, shared timing patterns).  This is the "graph embedding
   lite" half of the brief - it stays pure NetworkX, no extra dependencies.

The final ``cluster_id`` prefers the (hard evidence) common-input group and falls
back to the (soft evidence) Louvain community, and every address carries a
``cluster_method`` string saying which one produced its label.  NetworkX's Louvain
is randomised, so ``seed`` keeps runs reproducible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandas as pd

from src import config
from src.graph.builder import GraphBuilder, _as_list, as_builder

logger = logging.getLogger(__name__)


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def find(self, item: str) -> str:
        parent = self.parent.setdefault(item, item)
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        rank_a, rank_b = self.rank.get(ra, 0), self.rank.get(rb, 0)
        if rank_a < rank_b:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if rank_a == rank_b:
            self.rank[ra] = rank_a + 1


@dataclass
class ClusterResult:
    labels: Dict[str, int] = field(default_factory=dict)
    sizes: Dict[int, int] = field(default_factory=dict)
    methods: Dict[str, str] = field(default_factory=dict)
    members: Dict[int, List[str]] = field(default_factory=dict)
    n_common_input_clusters: int = 0
    n_louvain_communities: int = 0
    modularity: Optional[float] = None
    stats: Optional[pd.DataFrame] = None

    @property
    def n_clusters(self) -> int:
        return len(self.sizes)

    def cluster_of(self, wallet: str) -> Optional[int]:
        return self.labels.get(wallet)

    def describe(self, cluster_id: int, limit: int = 12) -> List[str]:
        return self.members.get(cluster_id, [])[:limit]

    def summary(self) -> Dict[str, Any]:
        largest = max(self.sizes.values()) if self.sizes else 0
        multi = sum(1 for size in self.sizes.values() if size > 1)
        return {
            "clusters": self.n_clusters,
            "multi_wallet_clusters": multi,
            "largest_cluster_size": int(largest),
            "common_input_clusters": int(self.n_common_input_clusters),
            "louvain_communities": int(self.n_louvain_communities),
            "modularity": round(float(self.modularity), 4) if self.modularity is not None else None,
            "wallets_labelled": len(self.labels),
        }


def _transactions_from_graph(builder: GraphBuilder) -> pd.DataFrame:
    """Rebuild a minimal transactions frame from a graph (used when only the graph is passed)."""
    rows = []
    for txid, data in builder.graph.nodes(data=True):
        if data.get("node_type") != "txid":
            continue
        inputs = [str(src) for src, _, edge in builder.graph.in_edges(txid, data=True)
                  if edge.get("edge_type") == "in"]
        outputs = [str(dst) for _, dst, edge in builder.graph.out_edges(txid, data=True)
                   if edge.get("edge_type") == "out"]
        rows.append({"txid": txid, "inputs": inputs, "outputs": outputs,
                     "input_amounts": [], "output_amounts": [],
                     "timestamp": data.get("timestamp")})
    return pd.DataFrame(rows)


def _is_merge_hazard(row: pd.Series, n_inputs: int) -> bool:
    """Should this transaction be excluded from co-spend merging?

    The common-input heuristic is transitive, so a single exchange sweep or CoinJoin
    can fuse thousands of unrelated wallets into one "owner" (the classic
    chain-merge collision).  Two standard guards are applied:

    * very large consolidations (> COMMON_INPUT_MAX_INPUTS inputs) are excluded;
    * CoinJoin-shaped transactions (many inputs, many near-equal outputs) are
      excluded, because their inputs deliberately come from different owners.
    """
    if n_inputs > config.COMMON_INPUT_MAX_INPUTS:
        return True
    outputs = _as_list(row.get("outputs"))
    amounts = _as_list(row.get("output_amounts"))
    if len(outputs) >= config.COINJOIN_MIN_OUTPUTS and len(amounts) == len(outputs):
        try:
            values = [float(v) for v in amounts]
        except (TypeError, ValueError):
            return False
        mean = sum(values) / len(values) if values else 0.0
        if mean > 0:
            spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
            if spread / mean < config.COINJOIN_OUTPUT_CV:
                return True
    return False


def common_input_clusters(transactions: pd.DataFrame) -> Tuple[Dict[str, str], int]:
    """Union-Find over co-spent inputs -> {address: cluster_root}, n_clusters."""
    uf = UnionFind()
    skipped = 0
    for _, row in transactions.iterrows():
        inputs = [str(a) for a in _as_list(row.get("inputs"))]
        if len(inputs) < 2:
            continue
        if _is_merge_hazard(row, len(inputs)):
            skipped += 1
            continue
        first = inputs[0]
        for other in inputs[1:]:
            uf.union(first, other)
    if skipped:
        logger.info(
            "Common-input heuristic skipped %d merge-hazard transactions (sweeps/CoinJoins)",
            skipped,
        )
    roots = {address: uf.find(address) for address in list(uf.parent.keys())}
    distinct = len(set(roots.values()))
    return roots, distinct


def louvain_communities(
    builder: GraphBuilder,
    seed: int = config.RANDOM_SEED,
    resolution: float = config.LOUVAIN_RESOLUTION,
) -> Tuple[Dict[str, int], float, int]:
    """Community detection on the wallet projection -> {wallet: community}, modularity, n."""
    builder = as_builder(builder)
    undirected = nx.Graph(builder.wallet_projection_undirected())

    if undirected.number_of_nodes() == 0:
        return {}, 0.0, 0

    try:
        communities = nx.community.louvain_communities(
            undirected, weight="weight", resolution=resolution, seed=seed
        )
        modularity = nx.community.modularity(undirected, communities, weight="weight")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Louvain failed (%s) - falling back to connected components", exc)
        communities = list(nx.connected_components(undirected))
        modularity = float("nan")

    labels: Dict[str, int] = {}
    for index, members in enumerate(sorted(communities, key=len, reverse=True)):
        for node in members:
            labels[str(node)] = index
    return labels, modularity, len(communities)


def cluster_entities(
    builder: GraphBuilder,
    transactions: Optional[pd.DataFrame] = None,
    use_louvain: bool = config.USE_LOUVAIN_REFINEMENT,
    seed: int = config.RANDOM_SEED,
) -> ClusterResult:
    """Run the full clustering stage and return labels for every wallet."""
    builder = as_builder(builder)
    if transactions is None:
        transactions = (
            builder.transactions if not builder.transactions.empty else _transactions_from_graph(builder)
        )
    result = ClusterResult()

    roots, n_common = common_input_clusters(transactions)
    result.n_common_input_clusters = n_common

    louvain_labels: Dict[str, int] = {}
    if use_louvain:
        louvain_labels, modularity, n_communities = louvain_communities(builder, seed=seed)
        result.modularity = modularity if modularity == modularity else None  # NaN guard
        result.n_louvain_communities = n_communities

    wallets = [n for n, d in builder.graph.nodes(data=True) if d.get("node_type") == "wallet"]

    # assign ids: hard evidence (common input) first, then soft evidence (Louvain)
    root_to_id: Dict[str, int] = {}
    next_id = 0
    for wallet in wallets:
        root = roots.get(wallet)
        if root is None:
            continue
        if root not in root_to_id:
            root_to_id[root] = next_id
            next_id += 1
        result.labels[wallet] = root_to_id[root]
        result.methods[wallet] = "common_input"

    offset = next_id + 1000  # keep the two id spaces clearly separate
    for wallet in wallets:
        if wallet in result.labels:
            continue
        community = louvain_labels.get(wallet)
        if community is None:
            result.labels[wallet] = next_id + 500_000  # singleton bucket
            result.methods[wallet] = "singleton"
            continue
        result.labels[wallet] = offset + community
        result.methods[wallet] = "louvain"

    members: Dict[int, List[str]] = {}
    for wallet, cluster_id in result.labels.items():
        members.setdefault(cluster_id, []).append(wallet)
    result.members = members
    result.sizes = {cluster_id: len(wallets_in) for cluster_id, wallets_in in members.items()}

    rows = []
    for cluster_id, wallets_in in sorted(members.items(), key=lambda kv: len(kv[1]), reverse=True):
        methods = {result.methods[w] for w in wallets_in}
        rows.append(
            {
                "cluster_id": cluster_id,
                "size": len(wallets_in),
                "method": "common_input" if "common_input" in methods else sorted(methods)[0],
                "example_members": "|".join(wallets_in[:3]),
            }
        )
    result.stats = pd.DataFrame(rows)

    logger.info(
        "Clustering: %d entities (%d from common-input, %d Louvain communities, largest=%d)",
        result.n_clusters, result.n_common_input_clusters, result.n_louvain_communities,
        max(result.sizes.values()) if result.sizes else 0,
    )
    return result
