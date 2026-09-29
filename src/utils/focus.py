"""Focus mode - walk the graph one node at a time and read that node's own history.

The graph holds ~1,900 nodes, which is far too much to read at one intensity.  The
console therefore lets the investigator *tap a node*, and this module supplies the four
pieces of logic behind that gesture:

``advance_path``
    The **ladder**.  Focus is a path, not a single node: tapping a connected node makes
    it the new head and remembers the edge that got you there, so a walk through a
    peeling chain can be retraced hop by hop.  Tapping a node already on the path walks
    back to it instead of looping.

``focus_view``
    What stays lit.  Only the head node and its **direct** connections stay at full
    strength; the rest of the graph fades.  A direct edge is evidence ("these two
    touched"), a two-hop association is a hunch, and the console must not blur the two.

``node_events``
    The **timeline** of one entity: every transaction it took part in, in chronological
    order, with direction, amount, counterparty and the counterparty's own risk score.
    Edge direction follows stage 4: ``wallet -> txid`` is an "in" edge (that wallet
    *funded* the transaction), ``txid -> wallet`` is an "out" edge (the transaction
    *paid* that wallet), and ``ip -> txid`` is a correlation.

``node_facts``
    A readable key/value view of a node (the raw payload rendered as JSON is technically
    complete and practically useless).

Everything here is a pure function over the payload written by stage 4, so the rules are
unit-tested without Streamlit (see ``tests/test_focus.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.utils.helpers import format_btc

#: How many hops of breadcrumb to keep before dropping the oldest ones.
MAX_HOPS = 12
#: Colours the dashboard uses for the three edge meanings (kept here so the chart, the
#: legend and the timeline all speak the same visual language).
EDGE_COLOURS = {"in": "#4dabf7", "out": "#ffa94d", "network": "#b197fc"}
GRAPH_COLOURS = {"in": "#4dabf7", "out": "#ffa94d", "network": "#b197fc"}

#: Direction words per node type, keyed by the *focused entity's* point of view.  The
#: graph's edge types are named after the transaction ("in" = into the tx, "out" = out
#: of the tx), which is the opposite of what a wallet experiences: a wallet on the
#: source end of an "in" edge has just **spent**.  Getting this backwards is the classic
#: way to print a seed wallet's 114 BTC payout as a receipt, so it is pinned in one place.
DIRECTION_LABELS = {
    ("wallet", "in"): "received",
    ("wallet", "out"): "spent",
    ("wallet", "network"): "packet on",
    ("txid", "in"): "funded by",
    ("txid", "out"): "paid out to",
    ("txid", "network"): "packet from",
    ("ip", "in"): "funded by",
    ("ip", "out"): "paid out to",
    ("ip", "network"): "correlated with",
}

#: How the focused entity experiences each raw edge type.  Transactions see edges the
#: way they are named; wallets see them mirrored (money in = an "out" edge pointing at
#: the wallet); IPs only ever carry correlation edges.
FLOW_BY_TYPE = {
    "wallet": {"in": "out", "out": "in", "network": "network"},
    "txid": {"in": "in", "out": "out", "network": "network"},
    "ip": {"in": "in", "out": "out", "network": "network"},
}


def index_nodes(nodes: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map node id -> node payload."""
    return {str(node["id"]): node for node in nodes}


def parse_time(value: Any) -> Optional[datetime]:
    """Parse either timestamp spelling used by the artefacts ("T" or space separated)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def short_id(node_id: str, keep: int = 8) -> str:
    """``bc1q35srz6a27ps6m0l89jweznt83n2sqn2fllftgx`` -> ``bc1q35sr…ftgx``."""
    text = str(node_id)
    if len(text) <= keep * 2 + 1:
        return text
    return f"{text[:keep]}\u2026{text[-keep:]}"


def adjacency(edges: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Undirected adjacency: ``node -> {neighbour: the edge that connects them}``.

    The graph is directed, but "who is this address connected to" is not, so both
    directions are folded into one lookup (the first edge seen between a pair wins for
    the tie-break attributes; the pair itself is what matters here).
    """
    neighbours: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if not source or not target:
            continue
        neighbours.setdefault(source, {}).setdefault(target, edge)
        neighbours.setdefault(target, {}).setdefault(source, edge)
    return neighbours


def direct_neighbours(node_id: Optional[str], edges: Sequence[Dict[str, Any]]) -> List[str]:
    """Every node joined to ``node_id`` by one edge - the only nodes focus mode lights."""
    if not node_id:
        return []
    return sorted(adjacency(edges).get(node_id, {}))


def advance_path(
    path: Sequence[str], clicked_id: Optional[str], max_hops: int = MAX_HOPS
) -> List[str]:
    """Walk the ladder: append the tapped node, or snap back to it if already visited."""
    current = [str(node_id) for node_id in path]
    if not clicked_id:
        return current
    clicked = str(clicked_id)
    if clicked in current:
        # Tapping a breadcrumb is "go back there", not "visit it again".
        return current[: current.index(clicked) + 1]
    return (current + [clicked])[-max_hops:]


def focus_view(
    path: Sequence[str], edges: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Describe what the graph should light up for this focus path.

    Returns ``head`` (the node in focus), ``path``, ``neighbours`` (direct connections of
    the head), ``nodes`` / ``edges`` (frozenset-ish sets of pairs to highlight) and
    ``ladder`` (the pairs the investigator actually walked, drawn as breadcrumbs).
    """
    path = [str(node_id) for node_id in path]
    head = path[-1] if path else None
    if head is None:
        return {
            "head": None,
            "path": [],
            "neighbours": [],
            "nodes": set(),
            "edges": set(),
            "ladder": set(),
        }

    neighbours = direct_neighbours(head, edges)
    ladder = {tuple(sorted((path[index], path[index + 1]))) for index in range(len(path) - 1)}
    highlight_edges = set()
    for edge in edges:
        pair = tuple(sorted((str(edge.get("source")), str(edge.get("target")))))
        if head in pair or pair in ladder:
            highlight_edges.add(pair)
    return {
        "head": head,
        "path": path,
        "neighbours": neighbours,
        "nodes": set(path) | set(neighbours),
        "edges": highlight_edges,
        "ladder": ladder,
    }


def path_summary(path: Sequence[str]) -> str:
    """The breadcrumb line: ``bc1q35sr…ftgx → 0ef15554…f182 → 3HwiV3Br…oKNk``."""
    return " \u2192 ".join(short_id(node_id) for node_id in path)


def route_hops(
    path: Sequence[str], edges: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The money trail along a walked route: one entry per hop, with the amount on that edge.

    The dotted amber line on the chart shows *where the investigator walked*; this is the same
    walk with the numbers attached, so the route reads as value moving from one entity to the
    next rather than as a list of ids.  A hop that is a network correlation carries the
    correlation method instead of an amount (no money moved, a packet did).
    """
    by_pair: Dict[tuple, Dict[str, Any]] = {}
    for edge in edges:
        pair = tuple(sorted((str(edge.get("source")), str(edge.get("target")))))
        by_pair.setdefault(pair, edge)

    hops: List[Dict[str, Any]] = []
    for index in range(len(path) - 1):
        start, end = str(path[index]), str(path[index + 1])
        edge = by_pair.get(tuple(sorted((start, end))), {})
        edge_type = str(edge.get("edge_type") or "")
        hops.append(
            {
                "from": start,
                "to": end,
                "amount": float(edge["amount"]) if edge.get("amount") is not None else None,
                "edge_type": edge_type,
                "label": {
                    "in": "funded",
                    "out": "paid",
                    "network": "matched packet",
                }.get(edge_type, "linked"),
                "method": edge.get("correlation_method"),
            }
        )
    return hops


def _trail_formatter(amounts: Sequence[float]) -> Any:
    """One unit for a whole money trail, chosen from its largest hop.

    ``format_btc`` picks a unit per value, which is right for a single figure and wrong for a
    trail: 1.0 and 0.9 next to each other would print as "1.0000 BTC" and "900.0000 mBTC" and
    the reader has to do arithmetic to see which hop was bigger.  A trail gets one unit, so
    the hops are directly comparable - which is the whole point of tracking the money.
    """
    biggest = max((abs(float(a)) for a in amounts if a), default=0.0)
    for unit, divisor, digits in (("BTC", 1.0, 4), ("mBTC", 1e-3, 2), ("bits", 1e-6, 1), ("sat", 1e-8, 0)):
        if biggest >= divisor:
            return lambda value, d=divisor, u=unit, p=digits: f"{value / d:,.{p}f} {u}"
    return lambda value: f"{value:.8f} BTC"


def route_summary(path: Sequence[str], edges: Sequence[Dict[str, Any]]) -> str:
    """The route with the money on it: ``A -114.8518 BTC→ TX -98.3024 BTC→ B``."""
    if len(path) < 2:
        return ""
    hops = route_hops(path, edges)
    amounts = [hop["amount"] or 0.0 for hop in hops]
    if not any(amounts):
        amounts = [0.0]
    show = _trail_formatter(amounts)
    parts = [short_id(str(path[0]))]
    for hop in hops:
        if hop["amount"] is None:
            carried = hop["method"] or hop["label"]
        else:
            carried = show(hop["amount"])
        parts.append(f"-{carried}\u2192 {short_id(hop['to'])}")
    return "   ".join(parts)


def node_events(
    node_id: str,
    nodes_by_id: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Every dated event involving ``node_id``, oldest first.

    One event per edge, described from the focused entity's point of view: a wallet
    "received" when a transaction paid it and "spent" when it funded one; a transaction
    is "funded by" wallets and "paid out to" wallets; an IP is "correlated with" a
    transaction.  Each event carries the counterparty's own risk score so the timeline
    itself shows which hop was the dangerous one.
    """
    node = nodes_by_id.get(node_id, {})
    node_type = str(node.get("node_type") or "wallet")
    events: List[Dict[str, Any]] = []
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if node_id not in (source, target):
            continue
        when = parse_time(edge.get("timestamp"))
        if when is None:
            continue
        peer_id = target if source == node_id else source
        peer = nodes_by_id.get(peer_id, {})
        edge_type = str(edge.get("edge_type") or "")
        flow = FLOW_BY_TYPE.get(node_type, {}).get(edge_type, edge_type or "linked")
        events.append(
            {
                "time": str(edge.get("timestamp")),
                "when": when,
                "kind": flow,
                "direction": DIRECTION_LABELS.get((node_type, flow), flow or "linked"),
                "amount": float(edge["amount"]) if edge.get("amount") is not None else None,
                "counterparty": peer_id,
                "counterparty_type": str(peer.get("node_type") or "?"),
                "counterparty_is_seed": bool(peer.get("is_seed")),
                "counterparty_flagged": bool(peer.get("is_mixing")),
                "counterparty_risk": float(peer.get("risk_score") or 0.0),
                "method": edge.get("correlation_method"),
                "confidence": edge.get("correlation_confidence"),
                "edge_type": edge_type,
            }
        )
    events.sort(key=lambda event: event["when"])
    for position, event in enumerate(events):
        event["step"] = position + 1
    return events


def flow_rows(
    node_id: str,
    nodes_by_id: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Who sent what to whom, for one entity: one row per coin movement, sender first.

    Each row is written as ``from -> to`` so the answer to "who sent this, who received it" is
    the row itself, not a direction word the reader has to decode.  Amounts are the amounts on
    the edge, and the sign is the graph's: ``wallet -> txid`` means that wallet **spent** into
    the transaction, ``txid -> wallet`` means the transaction **paid** that wallet.

    The honest limit is Bitcoin's own: a transaction with several inputs links them only as a
    *pool*, so for those we show the pool and say so (``resolution``) rather than invent a
    sender-to-receiver matching.  With a single input the mapping really is exact, and we say
    that too.
    """
    node = nodes_by_id.get(node_id, {})
    node_type = str(node.get("node_type") or "wallet")
    rows: List[Dict[str, Any]] = []

    def side(identifier: str) -> str:
        return f"{nodes_by_id.get(identifier, {}).get('node_type', '?')} {short_id(identifier)}"

    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if node_id not in (source, target):
            continue
        edge_type = str(edge.get("edge_type") or "")
        amount = float(edge["amount"]) if edge.get("amount") is not None else None

        if edge_type == "network":
            peer = target if source == node_id else source
            rows.append(
                {
                    "from": source,
                    "to": target,
                    "from_label": side(source),
                    "to_label": side(target),
                    "amount": None,
                    "role": "correlated",
                    "meaning": (
                        f"network packet matched to this transaction ({edge.get('correlation_method')})"
                        if peer == target
                        else "network packet matched to this transaction"
                    ),
                    "direction": "network",
                    "time": edge.get("timestamp"),
                }
            )
            continue

        # wallet -> txid : the wallet spent into the transaction
        # txid -> wallet : the transaction paid the wallet
        wallet_sent = edge_type == "in"
        spender_is_focus = wallet_sent and source == node_id
        receiver_is_focus = (not wallet_sent) and target == node_id
        if spender_is_focus and node_type == "wallet":
            role, meaning = "sent", "this entity sent it into the transaction"
        elif receiver_is_focus and node_type == "wallet":
            role, meaning = "received", "the transaction paid this entity"
        elif node_type == "txid" and wallet_sent:
            role, meaning = "funded by", "this entity received it from that wallet"
        elif node_type == "txid":
            role, meaning = "paid out to", "this entity sent it on to that wallet"
        else:
            role, meaning = "moved", "coin movement"
        rows.append(
            {
                "from": source,
                "to": target,
                "from_label": side(source),
                "to_label": side(target),
                "amount": amount,
                "role": role,
                "meaning": meaning,
                "direction": "spend" if wallet_sent else "payout",
                "time": edge.get("timestamp"),
            }
        )

    rows.sort(key=lambda row: (str(row.get("time") or ""), -(row.get("amount") or 0.0)))

    senders = [row for row in rows if row["direction"] == "spend"]
    receivers = [row for row in rows if row["direction"] == "payout"]
    resolution = "not-applicable"
    note = ""
    if node_type == "txid":
        if len(senders) <= 1:
            resolution = "exact"
            note = (
                "One sender, so the sender -> receiver mapping is exact: the single wallet "
                "listed below funded every output."
                if senders
                else "No funding wallet in the graph for this transaction."
            )
        else:
            resolution = "pooled"
            note = (
                f"{len(senders)} senders for {len(receivers)} receiver(s). Bitcoin links inputs "
                "and outputs only as a pool, so this transaction is shown as a pool: the total "
                "in equals the total out plus the fee, but no honest tool can say which sender "
                "paid which receiver without the UTXO-level matching that this prototype does "
                "not model."
            )
    elif node_type == "wallet":
        resolution = "exact"
        note = (
            "A wallet's own movements are exact: each row below is money that left this "
            "entity (sent) or arrived at it (received)."
        )
    return {
        "rows": rows,
        "senders": len(senders),
        "receivers": len(receivers),
        "total_sent": sum(row["amount"] or 0.0 for row in rows if row["direction"] == "spend"),
        "total_received": sum(
            row["amount"] or 0.0 for row in rows if row["direction"] == "payout"
        ),
        "resolution": resolution,
        "note": note,
    }


def timeline_stats(node: Dict[str, Any], events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Headline numbers for one entity's timeline (what the metric tiles show)."""
    received = sum(event["amount"] or 0.0 for event in events if event["kind"] == "in")
    sent = sum(event["amount"] or 0.0 for event in events if event["kind"] == "out")
    network = [event for event in events if event["kind"] == "network"]
    whens = [event["when"] for event in events]
    first, last = (min(whens), max(whens)) if whens else (None, None)
    risky = [event for event in events if event["counterparty_risk"] >= 0.5]
    return {
        "events": len(events),
        "received": received,
        "sent": sent,
        "network_events": len(network),
        "first": first,
        "last": last,
        "span_hours": ((last - first).total_seconds() / 3600.0) if first and last else 0.0,
        "flagged_counterparties": len(risky),
        "high_risk_counterparties": sum(
            1 for event in events if event["counterparty_risk"] >= 0.85
        ),
        "node_type": str(node.get("node_type") or "wallet"),
    }


def node_facts(node: Dict[str, Any], risk_threshold: float = 0.85) -> List[Tuple[str, str]]:
    """Readable view of a node payload: ``[("Risk score", "0.997"), ...]``.

    Ordered most-useful-first and formatted per type (amounts as BTC, scores to three
    decimals) so an investigator reads "seed wallet, risk 1.000, 12 transactions" instead
    of a wall of JSON.
    """
    node_type = str(node.get("node_type") or "wallet")
    facts: List[Tuple[str, str]] = []

    def amount(value: Any) -> Optional[str]:
        if value is None:
            return None
        return format_btc(float(value))

    if node_type == "wallet":
        if node.get("is_seed"):
            facts.append(("Role", "SEED WALLET - known-bad, risk propagates from here"))
        elif node.get("is_mixing"):
            facts.append(("Role", "Mixing wallet (CoinJoin participant)"))
        facts.append(("Risk score", f"{float(node.get('risk_score') or 0.0):.3f}"))
        if node.get("cluster_id") is not None:
            facts.append(("Ownership cluster", f"cluster {node.get('cluster_id')}"))
        for label, key in (
            ("Transactions", "tx_count"),
            ("Received", "total_in"),
            ("Sent", "total_out"),
            ("Unique counterparties", "unique_counterparties"),
        ):
            value = node.get(key)
            if value is None:
                continue
            facts.append((label, amount(value) if "total" in key else f"{int(value)}"))
        if node.get("mean_amount") is not None:
            facts.append(("Mean transfer", amount(node.get("mean_amount"))))
        if node.get("first_seen"):
            facts.append(("First seen", str(node["first_seen"])[:19].replace("T", " ")))
        if node.get("last_seen"):
            facts.append(("Last seen", str(node["last_seen"])[:19].replace("T", " ")))
        if node.get("lifetime_hours") is not None:
            facts.append(("Wallet lifetime", f"{float(node['lifetime_hours']):,.1f} h"))
        facts.append(("Peeling score", f"{float(node.get('peel_score') or 0.0):.3f}"))
        facts.append(("Anomaly score", f"{float(node.get('anomaly_score') or 0.0):.3f}"))
    elif node_type == "txid":
        facts.append(("Risk score", f"{float(node.get('risk_score') or 0.0):.3f}"))
        if node.get("timestamp"):
            facts.append(("Timestamp", str(node["timestamp"])[:19].replace("T", " ")))
        facts.append(("Value in", amount(node.get("total_in")) or "-"))
        facts.append(("Value out", amount(node.get("total_out")) or "-"))
        facts.append(("Fee", amount(node.get("fee")) or "-"))
        facts.append(("Inputs / outputs", f"{node.get('num_inputs', '?')} in · {node.get('num_outputs', '?')} out"))
        if node.get("script_type"):
            facts.append(("Script type", str(node["script_type"])))
        facts.append(("Anomaly score", f"{float(node.get('anomaly_score') or 0.0):.3f}"))
    else:  # ip
        facts.append(("Risk score", f"{float(node.get('risk_score') or 0.0):.3f}"))
        if node.get("geo_country"):
            facts.append(("Country", str(node["geo_country"])))
        if node.get("geo_asn"):
            facts.append(("ASN", f"{node['geo_asn']}" + (f" · {node['asn_org']}" if node.get("asn_org") else "")))
        if node.get("cross_border") is not None:
            facts.append(("Cross-border", "yes" if node["cross_border"] else "no"))

    if node_type == "wallet" and float(node.get("risk_score") or 0.0) >= risk_threshold:
        facts.append(("Reading", "high-risk entity: above the 0.85 alert floor"))
    return facts
