"""Stage 5 - Feature engineering (spec section 11.1).

Produces two matrices that every model downstream consumes:

* ``tx_features``      - one row per transaction
* ``wallet_features``  - one row per wallet address

Both are plain pandas DataFrames with a fixed column order (``TX_FEATURE_COLUMNS`` /
``WALLET_FEATURE_COLUMNS``) so that SHAP explanations, the anomaly model and the
dashboard all agree on what "feature 7" means.

Feature building is *incremental*: the base matrices are built first (needed by the
anomaly model), then clustering, peeling and risk results are attached with the
``attach_*`` helpers.  This lets the pipeline run the stages in the order the spec
prescribes while still ending up with one complete feature table.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import networkx as nx
import numpy as np
import pandas as pd

from src import config
from src.graph.builder import GraphBuilder, as_builder, _as_floats, _as_list

logger = logging.getLogger(__name__)

SCRIPT_TYPES = ["P2PKH", "P2SH", "P2WPKH", "P2WSH", "P2TR", "OP_RETURN", "MULTISIG_BARE", "NONSTANDARD", "UNKNOWN"]

TX_FEATURE_COLUMNS = [
    "num_inputs", "num_outputs", "total_input", "total_output", "fee", "fee_ratio",
    "amount_ratio", "out_amount_cv", "in_amount_cv", "io_ratio", "is_1in_2out",
    "is_1in_1out", "is_consolidation", "is_fan_out", "dust_outputs", "asymmetry",
    "log_total_input", "script_type_encoded", "has_network_correlation",
    "correlation_confidence", "n_network_records", "mean_time_delta_seconds",
    "src_geo_is_offshore", "is_coinjoin_shape",
]

WALLET_FEATURE_COLUMNS = [
    "in_degree", "out_degree", "n_txs", "total_received", "total_sent",
    "net_flow", "unique_counterparties", "avg_tx_amount", "std_tx_amount",
    "max_tx_amount", "lifetime_hours", "tx_per_hour", "burstiness",
    "max_fan_in", "max_fan_out", "fraction_of_peel_like_txs", "peel_chain_length",
    "mixing_participations", "cluster_size", "cluster_member_count",
    "pagerank", "risk_pagerank", "risk_proximity", "hops_to_seed",
    "n_correlated_ips", "distinct_source_countries", "is_seed",
]

OFFSHORE_COUNTRIES = {"PA", "SC", "RU", "CN", "HK", "TR", "UA"}

# Aliases produced by the graph builder; kept so models/explainers can name features
# the way the specification does (`unique_counterparties`, `avg_tx_amount`, ...).
WALLET_ALIASES = {
    "total_received": "total_in",
    "total_sent": "total_out",
    "avg_tx_amount": "mean_amount",
    "std_tx_amount": "std_amount",
}


class FeatureEngineer:
    """Builds the wallet and transaction feature matrices."""

    def __init__(
        self,
        builder: GraphBuilder,
        correlated: Optional[pd.DataFrame] = None,
    ) -> None:
        # Feature engineering needs the raw transaction rows, not just the graph, so a
        # bare networkx graph is rejected with a clear message instead of an
        # ``AttributeError`` deep inside the constructor.
        builder = as_builder(builder)
        if builder.transactions.empty:
            raise ValueError(
                "FeatureEngineer requires a GraphBuilder built from transactions "
                "(the graph alone has no transaction rows). Pass "
                "GraphBuilder(transactions, correlated).build()."
            )
        self.builder = builder
        self.graph = builder.graph
        self.transactions = builder.transactions
        self.correlated = correlated if correlated is not None else pd.DataFrame()
        self.tx_features: Optional[pd.DataFrame] = None
        self.wallet_features: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------ #
    # transaction-level
    # ------------------------------------------------------------------ #
    def build_transaction_features(self) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []
        correlated_agg = self._correlation_aggregate()

        for txid, info in self.builder.tx_index.items():
            row = self.transactions[self.transactions["txid"] == txid]
            if row.empty:
                continue
            record = row.iloc[0]
            inputs = [str(a) for a in _as_list(record.get("inputs"))]
            outputs = [str(a) for a in _as_list(record.get("outputs"))]
            in_amounts = _as_floats(record.get("input_amounts"))
            out_amounts = _as_floats(record.get("output_amounts"))
            total_in = float(sum(in_amounts)) or 1e-9
            total_out = float(sum(out_amounts)) or 1e-9
            fee = float(record.get("fee") or 0.0)
            largest_out = max(out_amounts) if out_amounts else 0.0
            second_out = sorted(out_amounts)[-2] if len(out_amounts) > 1 else 0.0

            corr = correlated_agg.get(txid, {})
            script = str(record.get("script_type") or "UNKNOWN")
            out_cv = float(np.std(out_amounts) / np.mean(out_amounts)) if out_amounts and np.mean(out_amounts) else 0.0
            in_cv = float(np.std(in_amounts) / np.mean(in_amounts)) if in_amounts and np.mean(in_amounts) else 0.0
            n_dust = sum(1 for amount in out_amounts if 0 < amount < 0.001)

            rows.append(
                {
                    "txid": txid,
                    "node_id": txid,
                    "num_inputs": len(inputs),
                    "num_outputs": len(outputs),
                    "total_input": total_in,
                    "total_output": total_out,
                    "fee": fee,
                    "fee_ratio": fee / total_in,
                    "amount_ratio": largest_out / total_out,
                    "out_amount_cv": out_cv,
                    "in_amount_cv": in_cv,
                    "io_ratio": len(inputs) / max(len(outputs), 1),
                    "is_1in_2out": int(len(inputs) == 1 and len(outputs) == 2),
                    "is_1in_1out": int(len(inputs) == 1 and len(outputs) == 1),
                    "is_consolidation": int(len(inputs) >= 8 and len(outputs) <= 2),
                    "is_fan_out": int(len(inputs) <= 2 and len(outputs) >= 8),
                    "dust_outputs": n_dust,
                    "asymmetry": (largest_out + second_out)
                    and largest_out / max(largest_out + second_out, 1e-9) or 0.0,
                    "log_total_input": float(np.log1p(total_in)),
                    "script_type_encoded": SCRIPT_TYPES.index(script) if script in SCRIPT_TYPES else len(SCRIPT_TYPES) - 1,
                    "has_network_correlation": int(bool(corr)),
                    "correlation_confidence": float(corr.get("correlation_confidence", 0.0)),
                    "n_network_records": int(corr.get("n_network_records", 0)),
                    "mean_time_delta_seconds": float(corr.get("mean_time_delta_seconds", 0.0) or 0.0),
                    "src_geo_is_offshore": int(str(corr.get("geo_country") or "") in OFFSHORE_COUNTRIES),
                    "is_coinjoin_shape": int(
                        len(inputs) >= config.COINJOIN_MIN_INPUTS
                        and len(outputs) >= config.COINJOIN_MIN_OUTPUTS
                        and out_cv < config.COINJOIN_OUTPUT_CV
                    ),
                }
            )

        frame = pd.DataFrame(rows)
        if frame.empty:
            frame = pd.DataFrame(columns=["txid", "node_id"] + TX_FEATURE_COLUMNS)
        self.tx_features = frame
        logger.info("Transaction feature matrix: %d rows x %d features", len(frame), len(TX_FEATURE_COLUMNS))
        return frame

    def _correlation_aggregate(self) -> Dict[str, Dict[str, Any]]:
        if self.correlated is None or self.correlated.empty or "txid" not in self.correlated.columns:
            return {}
        agg: Dict[str, Dict[str, Any]] = {}
        for txid, group in self.correlated.groupby("txid"):
            agg[str(txid)] = {
                "correlation_confidence": float(group["correlation_confidence"].max()),
                "n_network_records": int(len(group)),
                "mean_time_delta_seconds": float(group["time_delta_seconds"].abs().mean()),
                "geo_country": next((c for c in group.get("geo_country", []) if c), None),
            }
        return agg

    # ------------------------------------------------------------------ #
    # wallet-level
    # ------------------------------------------------------------------ #
    def build_wallet_features(
        self,
        cluster_labels: Optional[Dict[str, int]] = None,
        cluster_sizes: Optional[Dict[int, int]] = None,
        risk: Optional[Any] = None,
        peel: Optional[Any] = None,
    ) -> pd.DataFrame:
        cluster_labels = cluster_labels or {}
        cluster_sizes = cluster_sizes or {}
        wallet_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") == "wallet"]

        # PageRank on the wallet<->tx bipartite graph (structure feature, not risk)
        try:
            pagerank = nx.pagerank(self.graph, alpha=config.PAGERANK_ALPHA)
        except Exception:  # pragma: no cover - degenerate graph
            pagerank = {node: 0.0 for node in self.graph.nodes}

        correlated_ips = self._wallet_correlated_ips()
        spend_index = self._spend_index()

        rows: List[Dict[str, Any]] = []
        for wallet in wallet_nodes:
            data = self.graph.nodes[wallet]
            in_degree = self.graph.out_degree(wallet)   # wallet -> tx edges (spent in)
            out_degree = self.graph.in_degree(wallet)   # tx -> wallet edges (received)
            n_txs = int(data.get("tx_count", 0))
            first, last = data.get("first_seen"), data.get("last_seen")
            lifetime_hours = float(data.get("lifetime_hours", 0.0) or 0.0)
            fan_in, fan_out = self._wallet_fan(wallet)
            peel_like_fraction = self._peel_like_fraction(wallet, spend_index)

            info = correlated_ips.get(wallet, {})
            pagerank_risk = 0.0
            proximity = 0.0
            hops = None
            if risk is not None:
                pagerank_risk = float(getattr(risk, "wallet_risk_pagerank", {}).get(wallet, 0.0))
                proximity = float(getattr(risk, "wallet_risk_proximity", {}).get(wallet, 0.0))
                hops = getattr(risk, "hops_to_seed", {}).get(wallet)
            peel_chain_length = 0
            if peel is not None:
                peel_chain_length = int(getattr(peel, "wallet_chain_length", {}).get(wallet, 0))

            cluster_id = cluster_labels.get(wallet)
            rows.append(
                {
                    "node_id": wallet,
                    "wallet": wallet,
                    "in_degree": in_degree,
                    "out_degree": out_degree,
                    "n_txs": n_txs,
                    "total_received": float(data.get("total_in", 0.0)),
                    "total_sent": float(data.get("total_out", 0.0)),
                    "net_flow": float(data.get("total_in", 0.0)) - float(data.get("total_out", 0.0)),
                    "unique_counterparties": int(data.get("unique_counterparties", 0)),
                    "avg_tx_amount": float(data.get("mean_amount", 0.0) or 0.0),
                    "std_tx_amount": float(data.get("std_amount", 0.0) or 0.0),
                    "max_tx_amount": float(data.get("max_tx_amount", max(data.get("total_in", 0.0), data.get("total_out", 0.0)))),
                    "lifetime_hours": lifetime_hours,
                    "tx_per_hour": (n_txs / lifetime_hours) if lifetime_hours > 0 else float(n_txs),
                    "burstiness": self._burstiness(wallet),
                    "max_fan_in": fan_in,
                    "max_fan_out": fan_out,
                    "fraction_of_peel_like_txs": peel_like_fraction,
                    "peel_chain_length": peel_chain_length,
                    "mixing_participations": int(getattr(peel, "wallet_mixing_count", {}).get(wallet, 0)) if peel else 0,
                    "cluster_size": int(cluster_sizes.get(cluster_id, 1)) if cluster_id is not None else 1,
                    "cluster_member_count": int(cluster_sizes.get(cluster_id, 1)) if cluster_id is not None else 1,
                    "pagerank": float(pagerank.get(wallet, 0.0)),
                    "risk_pagerank": pagerank_risk,
                    "risk_proximity": proximity,
                    "hops_to_seed": float(hops) if hops is not None else -1.0,
                    "n_correlated_ips": int(info.get("n_ips", 0)),
                    "distinct_source_countries": int(info.get("n_countries", 0)),
                    "is_seed": int(bool(data.get("is_seed"))),
                    "first_seen": first,
                    "last_seen": last,
                }
            )

        frame = pd.DataFrame(rows)
        if frame.empty:
            frame = pd.DataFrame(columns=["node_id", "wallet"] + WALLET_FEATURE_COLUMNS)
        self.wallet_features = frame
        logger.info("Wallet feature matrix: %d rows x %d features", len(frame), len(WALLET_FEATURE_COLUMNS))
        return frame

    # ------------------------------------------------------------------ #
    def _spend_index(self) -> Dict[str, List[str]]:
        """address -> list of txids where it is an input (i.e. it spent)."""
        index: Dict[str, List[str]] = {}
        for txid, info in self.builder.tx_index.items():
            row = self.transactions[self.transactions["txid"] == txid]
            if row.empty:
                continue
            for address in _as_list(row.iloc[0].get("inputs")):
                index.setdefault(str(address), []).append(txid)
        return index

    def _wallet_fan(self, wallet: str) -> tuple:
        max_in = max_out = 0
        for neighbour in self.graph.successors(wallet):
            if self.graph.nodes[neighbour].get("node_type") == "txid":
                max_in = max(max_in, int(self.graph.nodes[neighbour].get("num_inputs", 0)))
        for neighbour in self.graph.predecessors(wallet):
            if self.graph.nodes[neighbour].get("node_type") == "txid":
                max_out = max(max_out, int(self.graph.nodes[neighbour].get("num_outputs", 0)))
        return max_in, max_out

    def _burstiness(self, wallet: str) -> int:
        """Largest number of transactions this wallet touches inside any 60 s window."""
        timestamps: List[pd.Timestamp] = []
        for txid in list(self.graph.successors(wallet)) + list(self.graph.predecessors(wallet)):
            node = self.graph.nodes.get(txid, {})
            if node.get("node_type") != "txid" or not node.get("timestamp"):
                continue
            timestamps.append(pd.Timestamp(node["timestamp"]))
        if len(timestamps) < 2:
            return len(timestamps)
        timestamps.sort()
        best = 1
        left = 0
        for right in range(len(timestamps)):
            while (timestamps[right] - timestamps[left]).total_seconds() > 60:
                left += 1
            best = max(best, right - left + 1)
        return best

    def _peel_like_fraction(self, wallet: str, spend_index: Dict[str, List[str]]) -> float:
        """Share of this wallet's spends shaped like a peel hop (1-in / 2-out, asymmetric)."""
        spends = spend_index.get(wallet, [])
        if not spends:
            return 0.0
        peel_like = 0
        for txid in spends:
            info = self.builder.tx_index.get(txid)
            if not info:
                continue
            if info["num_inputs"] == 1 and info["num_outputs"] == 2:
                row = self.transactions[self.transactions["txid"] == txid]
                if row.empty:
                    continue
                amounts = _as_floats(row.iloc[0].get("output_amounts"))
                if len(amounts) == 2 and sum(amounts) > 0:
                    larger, smaller = max(amounts), min(amounts)
                    if larger / (larger + smaller) > config.PEEL_ASYMMETRY_THRESHOLD:
                        peel_like += 1
        return peel_like / len(spends)

    def _wallet_correlated_ips(self) -> Dict[str, Dict[str, Any]]:
        """wallet -> {n_ips, n_countries} from correlated network evidence."""
        result: Dict[str, Dict[str, Any]] = {}
        if self.correlated is None or self.correlated.empty:
            return result
        wallet_txs: Dict[str, List[str]] = {}
        for wallet, spend_txs in self._spend_index().items():
            wallet_txs.setdefault(wallet, []).extend(spend_txs)
        for txid, group in self.correlated.groupby("txid"):
            ips = {str(ip) for ip in list(group.get("src_ip", [])) + list(group.get("dst_ip", [])) if ip}
            countries = {c for c in group.get("geo_country", []) if c}
            for wallet, txids in wallet_txs.items():
                if str(txid) in txids:
                    entry = result.setdefault(wallet, {"n_ips": 0, "n_countries": 0, "_ips": set(), "_countries": set()})
                    entry["_ips"].update(ips)
                    entry["_countries"].update(countries)
        for entry in result.values():
            entry["n_ips"] = len(entry.pop("_ips"))
            entry["n_countries"] = len(entry.pop("_countries"))
        return result

    # ------------------------------------------------------------------ #
    def save(self, wallet_path=None, tx_path=None) -> Dict[str, str]:
        from pathlib import Path

        wallet_path = Path(wallet_path or config.WALLET_FEATURES_PATH)
        tx_path = Path(tx_path or config.TX_FEATURES_PATH)
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        written = {}
        if self.tx_features is not None:
            self.tx_features.to_csv(tx_path, index=False)
            written["tx_features"] = str(tx_path)
        if self.wallet_features is not None:
            self.wallet_features.to_csv(wallet_path, index=False)
            written["wallet_features"] = str(wallet_path)
        return written
