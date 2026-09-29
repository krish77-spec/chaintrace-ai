"""Stage 4 - Graph construction (spec section 10).

Builds a ``networkx.MultiDiGraph`` with three node types:

* ``wallet``  - an address
* ``txid``    - a transaction
* ``ip``      - a network endpoint, only created when correlation evidence exists

Edges:

* ``wallet -> txid``  ("in"):  amount spent
* ``txid  -> wallet`` ("out"): amount received
* ``ip -> txid``      ("net"): correlation confidence / method

Node attributes are deliberately MongoDB-ish (flat scalars) so the same structure
can be dumped to JSON and rendered by the dashboard without adaptation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import networkx as nx
import pandas as pd

from src import config

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [part for part in value.split("|") if part != ""]
    return [value]


def _as_floats(value: Any) -> List[float]:
    out: List[float] = []
    for part in _as_list(value):
        try:
            out.append(float(part))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


class GraphBuilder:
    """Turns correlated records into the entity/transaction graph."""

    def __init__(
        self,
        transactions: pd.DataFrame,
        correlated: Optional[pd.DataFrame] = None,
        seeds: Optional[Sequence[str]] = None,
    ) -> None:
        self.transactions = transactions if transactions is not None else pd.DataFrame()
        self.correlated = correlated if correlated is not None else pd.DataFrame()
        self.seeds = set(seeds or [])
        self.graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self.tx_index: Dict[str, Dict[str, Any]] = {}
        self._projection: Optional[nx.MultiDiGraph] = None
        self._undirected_projection: Optional[nx.Graph] = None

    # ------------------------------------------------------------------ #
    def build(self) -> "GraphBuilder":
        """Construct the graph and return ``self`` so calls can be chained.

        The constructed networkx graph is available as ``self.graph``; returning the
        builder (rather than the graph) keeps ``GraphBuilder(...).build()`` usable
        wherever a builder is expected, e.g. ``FeatureEngineer`` or ``cluster_entities``.
        """
        g = nx.MultiDiGraph()
        tx_first_seen: Dict[str, pd.Timestamp] = {}
        tx_last_seen: Dict[str, pd.Timestamp] = {}

        for _, row in self.transactions.iterrows():
            txid = str(row.get("txid") or "").strip()
            if not txid:
                continue
            timestamp = pd.to_datetime(row.get("timestamp"))
            inputs = [str(a) for a in _as_list(row.get("inputs"))]
            outputs = [str(a) for a in _as_list(row.get("outputs"))]
            input_amounts = _as_floats(row.get("input_amounts"))
            output_amounts = _as_floats(row.get("output_amounts"))
            input_amounts += [0.0] * (len(inputs) - len(input_amounts))
            output_amounts += [0.0] * (len(outputs) - len(output_amounts))

            total_in = float(sum(input_amounts[: len(inputs)]))
            total_out = float(sum(output_amounts[: len(outputs)]))
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else None
            self.tx_index[txid] = {
                "txid": txid,
                "timestamp": timestamp,
                "num_inputs": len(inputs),
                "num_outputs": len(outputs),
                "total_in": total_in,
                "total_out": total_out,
                "fee": float(row.get("fee") or 0.0),
                "script_type": str(row.get("script_type") or "UNKNOWN"),
                "io_ratio": len(inputs) / max(len(outputs), 1),
                "meta": meta or {},
            }

            g.add_node(
                txid,
                node_type="txid",
                label=txid[:14],
                timestamp=timestamp.isoformat() if timestamp is not None else None,
                total_in=total_in,
                total_out=total_out,
                fee=float(row.get("fee") or 0.0),
                num_inputs=len(inputs),
                num_outputs=len(outputs),
                script_type=str(row.get("script_type") or "UNKNOWN"),
            )
            tx_first_seen[txid] = timestamp
            tx_last_seen[txid] = timestamp

            for address, amount in zip(inputs, input_amounts):
                self._touch_wallet(g, address, timestamp, incoming=0.0, outgoing=amount)
                self._add_flow_edge(
                    g, address, txid, f"in:{txid}:{address}", "in", amount, timestamp
                )
            for address, amount in zip(outputs, output_amounts):
                self._touch_wallet(g, address, timestamp, incoming=amount, outgoing=0.0)
                self._add_flow_edge(
                    g, txid, address, f"out:{txid}:{address}", "out", amount, timestamp
                )

        self._add_ip_evidence(g)
        self._finalise_wallet_stats(g)
        self.graph = g
        logger.info(
            "Graph built: %d nodes, %d edges (%d wallets, %d transactions, %d ip nodes)",
            g.number_of_nodes(), g.number_of_edges(),
            sum(1 for _, d in g.nodes(data=True) if d.get("node_type") == "wallet"),
            sum(1 for _, d in g.nodes(data=True) if d.get("node_type") == "txid"),
            sum(1 for _, d in g.nodes(data=True) if d.get("node_type") == "ip"),
        )
        return self

    # ------------------------------------------------------------------ #
    @staticmethod
    def _add_flow_edge(
        g: nx.MultiDiGraph,
        source: str,
        target: str,
        key: str,
        edge_type: str,
        amount: float,
        timestamp: pd.Timestamp,
    ) -> None:
        """Add a fund/payout edge, **summing** when one address appears twice.

        A transaction can legitimately pay the same address in two output rows (and the odd
        malformed export consumes the same address twice).  The key is one edge per
        (transaction, address) pair, so a repeated pair must accumulate rather than
        overwrite: overwriting silently drops value, and it made a wallet's own totals
        disagree with the sum of its timeline (a discrepancy the focus view made visible).
        """
        value = float(amount or 0.0)
        if g.has_edge(source, target, key=key):
            data = g.get_edge_data(source, target, key=key)
            data["amount"] = float(data.get("amount") or 0.0) + value
            data["occurrences"] = int(data.get("occurrences") or 1) + 1
            return
        g.add_edge(
            source, target, key=key, edge_type=edge_type,
            amount=value, timestamp=timestamp, occurrences=1,
        )

    # ------------------------------------------------------------------ #
    def _touch_wallet(
        self,
        g: nx.MultiDiGraph,
        address: str,
        timestamp: pd.Timestamp,
        incoming: float,
        outgoing: float,
    ) -> None:
        data = g.nodes.get(address)
        if data is None:
            g.add_node(
                address,
                node_type="wallet",
                label=f"{address[:8]}...{address[-4:]}",
                total_in=0.0,
                total_out=0.0,
                tx_count=0,
                first_seen=timestamp.isoformat() if timestamp is not None else None,
                last_seen=timestamp.isoformat() if timestamp is not None else None,
                is_seed=address in self.seeds,
                cluster_id=None,
                risk_score=0.0,
                anomaly_score=0.0,
                peel_score=0.0,
                is_mixing=False,
                total_received=0.0,
                total_sent=0.0,
                unique_counterparties=0,
                mean_amount=0.0,
                std_amount=0.0,
                lifetime_hours=0.0,
            )
            data = g.nodes[address]
        data["total_in"] = float(data.get("total_in", 0.0)) + float(incoming)
        data["total_out"] = float(data.get("total_out", 0.0)) + float(outgoing)
        data["tx_count"] = int(data.get("tx_count", 0)) + 1
        if timestamp is not None:
            first = data.get("first_seen")
            last = data.get("last_seen")
            iso = timestamp.isoformat()
            if not first or iso < first:
                data["first_seen"] = iso
            if not last or iso > last:
                data["last_seen"] = iso
        # aliases the spec calls out explicitly
        data["total_received"] = float(data.get("total_in", 0.0))
        data["total_sent"] = float(data.get("total_out", 0.0))

    def _add_ip_evidence(self, g: nx.MultiDiGraph) -> None:
        if self.correlated is None or self.correlated.empty:
            return
        for _, row in self.correlated.iterrows():
            txid = str(row.get("txid") or "").strip()
            if not txid or txid not in g:
                continue
            confidence = float(row.get("correlation_confidence") or 0.0)
            method = str(row.get("correlation_method") or "unknown")
            for ip_col in ("src_ip", "dst_ip"):
                ip = row.get(ip_col)
                if not ip:
                    continue
                ip = str(ip)
                if ip not in g:
                    g.add_node(
                        ip,
                        node_type="ip",
                        label=ip,
                        geo_country=row.get("geo_country"),
                        geo_asn=row.get("geo_asn"),
                        asn_org=row.get("asn_org"),
                        cross_border=bool(row.get("cross_border")) if row.get("cross_border") is not None else None,
                        risk_score=0.0,
                    )
                g.add_edge(
                    ip, txid, key=f"net:{txid}:{ip}:{method}",
                    edge_type="network",
                    correlation_confidence=confidence,
                    correlation_method=method,
                    timestamp=row.get("network_timestamp").isoformat()
                    if hasattr(row.get("network_timestamp"), "isoformat") else None,
                )

    def _finalise_wallet_stats(self, g: nx.MultiDiGraph) -> None:
        for node, data in g.nodes(data=True):
            if data.get("node_type") != "wallet":
                continue
            amounts: List[float] = []
            counterparties: Set[str] = set()
            neighbours = list(g.successors(node)) + list(g.predecessors(node))
            for neighbour in neighbours:
                if g.nodes[neighbour].get("node_type") in ("txid", "ip"):
                    counterparties.add(neighbour)
                for _, _, edge in g.out_edges(node, data=True):
                    if edge.get("edge_type") in ("in", "out"):
                        amounts.append(float(edge.get("amount") or 0.0))
                for _, _, edge in g.in_edges(node, data=True):
                    if edge.get("edge_type") in ("in", "out"):
                        amounts.append(float(edge.get("amount") or 0.0))
                break  # amounts collected once, below
            amounts = []
            for _, _, edge in g.out_edges(node, data=True):
                amounts.append(float(edge.get("amount") or 0.0))
            for _, _, edge in g.in_edges(node, data=True):
                amounts.append(float(edge.get("amount") or 0.0))
            data["unique_counterparties"] = len(counterparties)
            data["mean_amount"] = float(sum(amounts) / len(amounts)) if amounts else 0.0
            if len(amounts) > 1:
                mean = data["mean_amount"]
                data["std_amount"] = float(
                    (sum((a - mean) ** 2 for a in amounts) / (len(amounts) - 1)) ** 0.5
                )
            else:
                data["std_amount"] = 0.0
            first, last = data.get("first_seen"), data.get("last_seen")
            if first and last:
                try:
                    data["lifetime_hours"] = round(
                        (pd.Timestamp(last) - pd.Timestamp(first)).total_seconds() / 3600.0, 4
                    )
                except Exception:
                    data["lifetime_hours"] = 0.0

    # ------------------------------------------------------------------ #
    # Feature helpers consumed by src/ml/features.py
    # ------------------------------------------------------------------ #
    def wallet_projection(self) -> nx.MultiDiGraph:
        """Wallet-only directed graph: edge wallet -> wallet with the txid that moved it.

        Derived from the transaction graph itself (not from the raw DataFrame), so a
        builder reconstructed around an existing graph still works.  Cached: risk
        propagation, clustering and evidence paths all need it, and rebuilding it per
        call was the single largest cost in the pipeline.
        """
        signature = (self.graph.number_of_nodes(), self.graph.number_of_edges())
        if self._projection is not None and self._projection.graph.get("built_for") == signature:
            return self._projection

        projected = nx.MultiDiGraph()
        for txid, data in self.graph.nodes(data=True):
            if data.get("node_type") != "txid":
                continue
            sources = [
                (src, edge.get("amount", 0.0))
                for src, _, edge in self.graph.in_edges(txid, data=True)
                if edge.get("edge_type") == "in"
            ]
            destinations = [
                (dst, edge.get("amount", 0.0))
                for _, dst, edge in self.graph.out_edges(txid, data=True)
                if edge.get("edge_type") == "out"
            ]
            if not sources or not destinations:
                continue
            total_in = sum(float(amount) for _, amount in sources) or 1.0
            for src, _ in sources:
                if src not in projected:
                    projected.add_node(src, node_type="wallet", is_seed=src in self.seeds)
            for dst, amount in destinations:
                if dst not in projected:
                    projected.add_node(dst, node_type="wallet", is_seed=dst in self.seeds)
                for src, _ in sources:
                    projected.add_edge(
                        src, dst, key=f"{txid}:{src}:{dst}",
                        txid=txid, amount=float(amount),
                        share_of_input=float(amount) / total_in,
                    )

        projected.graph["built_for"] = signature
        self._projection = projected
        self._undirected_projection = None
        return projected

    def wallet_projection_undirected(self) -> nx.Graph:
        """Simple-graph view of the wallet projection (cached, for BFS/Dijkstra)."""
        if self._undirected_projection is not None:
            return self._undirected_projection
        projected = self.wallet_projection()
        undirected = nx.Graph()
        for src, dst, data in projected.edges(data=True):
            weight = float(data.get("share_of_input") or 0.0) or 1e-6
            if undirected.has_edge(src, dst):
                undirected[src][dst]["weight"] += weight
            else:
                undirected.add_edge(src, dst, weight=weight)
        for node in projected.nodes:
            if node not in undirected:
                undirected.add_node(node)
        self._undirected_projection = undirected
        return undirected

    def rebuild_tx_index(self) -> Dict[str, Dict[str, Any]]:
        """Rebuild ``tx_index`` from the graph itself.

        Needed when a builder is reconstructed around an existing graph (loading a
        saved graph.json, or calling an ML module with a bare MultiDiGraph): the index
        is normally filled during :meth:`build`.
        """
        index: Dict[str, Dict[str, Any]] = {}
        for txid, data in self.graph.nodes(data=True):
            if data.get("node_type") != "txid":
                continue
            inputs = [
                str(src) for src, _, edge in self.graph.in_edges(txid, data=True)
                if edge.get("edge_type") == "in"
            ]
            outputs = [
                str(dst) for _, dst, edge in self.graph.out_edges(txid, data=True)
                if edge.get("edge_type") == "out"
            ]
            index[str(txid)] = {
                "txid": str(txid),
                "timestamp": pd.to_datetime(data.get("timestamp")),
                "num_inputs": int(data.get("num_inputs", len(inputs))),
                "num_outputs": int(data.get("num_outputs", len(outputs))),
                "total_in": float(data.get("total_in", 0.0) or 0.0),
                "total_out": float(data.get("total_out", 0.0) or 0.0),
                "fee": float(data.get("fee", 0.0) or 0.0),
                "script_type": str(data.get("script_type") or "UNKNOWN"),
                "io_ratio": len(inputs) / max(len(outputs), 1),
                "meta": {},
                "inputs": inputs,
                "outputs": outputs,
            }
        self.tx_index = index
        return index

    def apply_node_attributes(self, updates: Dict[str, Dict[str, Any]]) -> None:
        """Attach ML results (cluster_id, risk_score, ...) to existing nodes."""
        for node, attrs in updates.items():
            if node in self.graph:
                self.graph.nodes[node].update(attrs)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def get_subgraph(self, seed_nodes: Iterable[str], depth: int = config.GRAPH_SUBGRAPH_DEFAULT_DEPTH) -> nx.MultiDiGraph:
        nodes: Set[str] = {n for n in seed_nodes if n in self.graph}
        frontier = set(nodes)
        for _ in range(max(int(depth), 0)):
            next_frontier: Set[str] = set()
            for node in frontier:
                next_frontier.update(self.graph.successors(node))
                next_frontier.update(self.graph.predecessors(node))
            frontier = next_frontier - nodes
            nodes.update(frontier)
            if not frontier:
                break
        return self.graph.subgraph(nodes).copy()

    def get_neighbourhood(self, node: str, depth: int = 1) -> Dict[str, Any]:
        sub = self.get_subgraph([node], depth=depth)
        return to_node_link(self.graph, sub)

    def get_stats(self) -> Dict[str, Any]:
        g = self.graph
        wallets = [n for n, d in g.nodes(data=True) if d.get("node_type") == "wallet"]
        txs = [n for n, d in g.nodes(data=True) if d.get("node_type") == "txid"]
        ips = [n for n, d in g.nodes(data=True) if d.get("node_type") == "ip"]
        degrees = [d for _, d in g.degree()] or [0]
        try:
            components = nx.number_weakly_connected_components(g) if g.number_of_nodes() else 0
        except Exception:
            components = 0
        return {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "wallets": len(wallets),
            "transactions": len(txs),
            "ip_nodes": len(ips),
            "seed_wallets": sum(1 for n in wallets if g.nodes[n].get("is_seed")),
            "density": round(nx.density(g), 6) if g.number_of_nodes() > 1 else 0.0,
            "avg_degree": round(sum(degrees) / len(degrees), 4),
            "max_degree": int(max(degrees)),
            "weakly_connected_components": components,
            "total_value_moved": round(
                float(sum(d.get("total_out", 0.0) for n, d in g.nodes(data=True) if d.get("node_type") == "wallet")), 4
            ),
        }

    def save(self, path: Optional[Path | str] = None) -> Path:
        path = Path(path or config.GRAPH_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = to_node_link(self.graph)
        payload["stats"] = self.get_stats()
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


def to_node_link(
    graph: nx.MultiDiGraph,
    subgraph: Optional[nx.MultiDiGraph] = None,
    max_nodes: int = config.MAX_GRAPH_NODES_FOR_EXPORT,
) -> Dict[str, Any]:
    """Serialise a (sub)graph into {nodes, edges} JSON the dashboard can render."""
    target = subgraph if subgraph is not None else graph
    if target.number_of_nodes() > max_nodes:
        # keep the highest-degree slice so the browser stays responsive
        degrees = sorted(target.degree, key=lambda kv: kv[1], reverse=True)
        keep = {node for node, _ in degrees[:max_nodes]}
        target = target.subgraph(keep)
        truncated = True
    else:
        truncated = False

    nodes: List[Dict[str, Any]] = []
    for node, data in target.nodes(data=True):
        entry = {"id": str(node), "label": str(data.get("label") or node)}
        for key, value in data.items():
            if key == "label" or isinstance(value, (list, dict)):
                continue
            entry[key] = value.item() if hasattr(value, "item") else value
        nodes.append(entry)

    edges: List[Dict[str, Any]] = []
    for src, dst, key, data in target.edges(keys=True, data=True):
        entry = {"source": str(src), "target": str(dst), "key": str(key)}
        for name, value in data.items():
            if name == "timestamp" and value is not None:
                # One spelling for every timestamp in the payload.  ``str(pd.Timestamp)``
                # writes "2026-08-23 00:21:23.358190" (space) while ``.isoformat()`` writes
                # "2026-08-23T00:21:23.358190", and the two do **not** sort together as
                # strings - 'T' beats every digit, so a later instant written with a space
                # sorted before an earlier one written with a 'T'.  The timeline replay builds
                # its ticks and its cutoff comparisons out of these strings, so the mixture
                # made stepping back in time show a *later* clock.  ``sortable_stamp`` in
                # ``src.utils.helpers`` normalises the legacy spelling on read as well.
                entry[name] = value.isoformat() if hasattr(value, "isoformat") else str(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                entry[name] = value.item() if hasattr(value, "item") else value
        edges.append(entry)

    return {"nodes": nodes, "edges": edges, "truncated": truncated, "node_count": len(nodes), "edge_count": len(edges)}


def as_builder(candidate: Any) -> "GraphBuilder":
    """Accept either a :class:`GraphBuilder` or a bare ``networkx.MultiDiGraph``.

    The ML modules are all called with "the graph" in the specification, so they
    normalise their input here instead of forcing every caller to know which wrapper
    type to pass.
    """
    if isinstance(candidate, GraphBuilder):
        return candidate
    if isinstance(candidate, nx.MultiDiGraph) or isinstance(candidate, nx.DiGraph):
        builder = GraphBuilder(pd.DataFrame(), None)
        builder.graph = candidate
        builder.rebuild_tx_index()
        return builder
    raise TypeError(f"Expected a GraphBuilder or networkx graph, got {type(candidate)!r}")


def load_graph_payload(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Read a previously written graph.json (used by API/dashboard when idle).

    The default path is resolved at call time so that pointing ``config`` at another
    workspace (as the tests do) actually redirects the read.
    """
    path = Path(path or config.GRAPH_PATH)
    if not path.exists():
        return {"nodes": [], "edges": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - corrupt file
        logger.warning("Could not read %s: %s", path, exc)
        return {"nodes": [], "edges": [], "stats": {}}
