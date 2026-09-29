"""Case-file export - turn one alert into a document an investigator can file.

The dashboard is a tool; a **case file is a deliverable**.  This module renders a
single alert as a self-contained HTML page (and the same content as Markdown) holding
everything the analyst would otherwise assemble by hand:

* the ranked reasons in plain English;
* the counterfactual ("what would clear this entity"), which is where verification
  effort should go first;
* the evidence package - financials, ownership cluster, peeling chain, risk paths;
* the network evidence with country/ASN, and how each packet was matched;
* a picture of the entity's neighbourhood, drawn as inline SVG so the file needs no
  server, no internet and no external asset;
* the SHA-256 evidence hash and the ledger-chain status, plus the exact command that
  re-verifies them.

No dependency is used that the prototype does not already ship, and nothing is fetched
at render time: open the file offline and it is complete.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

RISK_COLOURS: Tuple[Tuple[float, str], ...] = (
    (0.85, "#ff6b6b"),
    (0.70, "#ff922b"),
    (0.50, "#ffd43b"),
    (0.30, "#a9e34b"),
    (0.00, "#4dabf7"),
)


def risk_colour(score: float) -> str:
    for threshold, colour in RISK_COLOURS:
        if score >= threshold:
            return colour
    return "#868e96"


# --------------------------------------------------------------------------- #
# the picture
# --------------------------------------------------------------------------- #
def neighbourhood_svg(
    entity: str,
    graph_payload: Optional[Dict[str, Any]],
    width: int = 780,
    height: int = 420,
    max_neighbours: int = 16,
) -> str:
    """Draw the entity, its neighbours and the connecting edges as inline SVG.

    Deliberately simple: the entity in the middle, neighbours on a ring, node colour by
    risk score.  Simple is the point - this picture ends up inside a document, so it has
    to be readable in black and white on someone's desk.
    """
    if not graph_payload:
        return ""
    nodes = {node["id"]: node for node in graph_payload.get("nodes", [])}
    edges = graph_payload.get("edges", [])
    if entity not in nodes:
        return ""

    neighbours: List[Tuple[str, str, float]] = []
    seen: set = set()
    for edge in edges:
        other = None
        if edge["source"] == entity:
            other = edge["target"]
        elif edge["target"] == entity:
            other = edge["source"]
        if other and other not in seen:
            seen.add(other)
            neighbours.append((other, str(edge.get("edge_type", "")), float(edge.get("amount") or 0.0)))
    neighbour_ids = [item[0] for item in neighbours[:max_neighbours]]

    import math

    cx, cy = width / 2, height / 2
    radius = min(width, height) / 2 - 60
    positions: Dict[str, Tuple[float, float]] = {entity: (cx, cy)}
    for index, node_id in enumerate(neighbour_ids):
        angle = (2 * math.pi * index) / max(len(neighbour_ids), 1) - math.pi / 2
        positions[node_id] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Neighbourhood of {html.escape(entity)}">',
        f'<rect width="{width}" height="{height}" fill="#0b1220"/>',
    ]
    for node_id, edge_type, amount in neighbours:
        if node_id not in positions:
            continue
        x0, y0 = positions[entity]
        x1, y1 = positions[node_id]
        dash = "" if edge_type == "out" else ' stroke-dasharray="4 3"'
        parts.append(
            f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
            f'stroke="#2b3a55" stroke-width="1.2"{dash}/>'
        )

    for node_id, (x, y) in positions.items():
        node = nodes.get(node_id, {})
        score = float(node.get("risk_score") or 0.0)
        radius_node = 13 if node_id == entity else 8
        fill = risk_colour(score)
        label = "seed" if node.get("is_seed") else str(node.get("node_type") or "wallet")
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius_node}" fill="{fill}" '
            f'stroke="#0b1220" stroke-width="1.5"/>'
        )
        short = node_id[:14] + "…" if len(node_id) > 15 else node_id
        parts.append(
            f'<text x="{x:.1f}" y="{y - radius_node - 5:.1f}" fill="#adb5bd" '
            f'font-family="monospace" font-size="9" text-anchor="middle">'
            f'{html.escape(short)}</text>'
        )
        if node_id == entity:
            parts.append(
                f'<text x="{x:.1f}" y="{y + radius_node + 14:.1f}" fill="#f1f3f5" '
                f'font-family="sans-serif" font-size="11" text-anchor="middle">'
                f'{html.escape(label)} · risk {score:.2f}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# the document
# --------------------------------------------------------------------------- #
def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "<p><em>none</em></p>"
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "_none_"
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def _network_rows(alert: Dict[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for record in (alert.get("evidence") or {}).get("network_evidence", []) or []:
        rows.append([
            record.get("src_ip") or "-",
            record.get("geo_country") or "-",
            record.get("geo_asn") or "-",
            record.get("asn_org") or "-",
            record.get("correlation_method") or "-",
            f"{float(record.get('correlation_confidence') or 0.0):.2f}",
            f"{float(record.get('time_delta_seconds') or 0.0):+.1f}s",
        ])
    return rows


def _counterfactual_rows(alert: Dict[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for item in alert.get("counterfactuals") or []:
        rows.append([
            item.get("label", item.get("evidence")),
            f"{float(item.get('score_without') or 0.0):.2f}",
            f"{float(item.get('score_drop') or 0.0):.2f}",
            "YES - decisive" if item.get("decisive") else "no",
        ])
    return rows


def build_case_file(
    alert: Dict[str, Any],
    graph_payload: Optional[Dict[str, Any]] = None,
    ledger_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Render one alert as ``{"html": ..., "markdown": ..., "filename": ...}``."""
    evidence = alert.get("evidence") or {}
    financials = evidence.get("financials") or {}
    cluster = evidence.get("cluster") or {}
    peel = evidence.get("peel_chain") or {}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hash_value = alert.get("evidence_hash") or "-"
    chain_ok = (ledger_report or {}).get("chain_ok")
    chain_text = (
        "hash chain intact" if chain_ok else
        "chain NOT verified" if chain_ok is False else "chain not checked"
    )
    svg = neighbourhood_svg(str(alert.get("entity")), graph_payload)

    summary_rows = [
        ["Alert", alert.get("alert_id")],
        ["Entity", alert.get("entity")],
        ["Entity type", alert.get("entity_type")],
        ["Risk score", f"{float(alert.get('risk_score') or 0.0):.3f}"],
        ["Confidence", f"{float(alert.get('confidence') or 0.0):.3f}"],
        ["Recommended action", alert.get("recommended_action", "-")],
        ["Generated", generated],
    ]
    financial_rows = [
        ["Total received (BTC)", f"{float(financials.get('total_received') or 0.0):.8f}"],
        ["Total sent (BTC)", f"{float(financials.get('total_sent') or 0.0):.8f}"],
        ["Transactions", financials.get("n_transactions", "-")],
        ["Unique counterparties", financials.get("unique_counterparties", "-")],
        ["First seen", financials.get("first_seen", "-")],
        ["Last seen", financials.get("last_seen", "-")],
    ]
    cluster_rows = [
        ["Cluster id", cluster.get("cluster_id", "-")],
        ["Size (wallets)", cluster.get("size", "-")],
        ["Method", cluster.get("method", "-")],
        ["Contains a high-risk wallet", cluster.get("tainted", "-")],
        ["Example members", ", ".join((cluster.get("example_members") or [])[:5]) or "-"],
    ]
    peel_rows = [
        ["Chain id", peel.get("chain_id", "-")],
        ["Chain length (hops)", peel.get("chain_length", "-")],
        ["Position in chain", peel.get("hop", "-")],
        ["Starts at a seed", peel.get("starts_at_seed", "-")],
        ["Routes value to a seed", peel.get("reaches_seed", "-")],
        ["Total value moved (BTC)", f"{float(peel.get('total_value_moved') or 0.0):.4f}"],
        ["Mean asymmetry", f"{float(peel.get('mean_asymmetry') or 0.0):.4f}"],
    ]
    related = (evidence.get("related_transactions") or [])[:10]
    related_rows = [
        [
            str(item.get("txid", ""))[:20] + "…",
            item.get("role", "-"),
            f"{float(item.get('amount') or 0.0):.6f}",
            item.get("timestamp", "-"),
        ]
        for item in related
    ]
    counterfactual_rows = _counterfactual_rows(alert)
    network_rows = _network_rows(alert)
    reasons = list(alert.get("reasons") or [])

    # ---------------------------------------------------------------- HTML --
    style = """
    body{background:#0b1220;color:#e9ecef;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0;padding:32px;line-height:1.5}
    h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;color:#ffd43b;
         text-transform:uppercase;letter-spacing:.06em}
    code{background:#111a2b;padding:2px 5px;border-radius:4px;font-size:12px}
    table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
    th,td{border:1px solid #22304a;padding:6px 9px;text-align:left}
    th{background:#131c2e;color:#adb5bd;font-weight:600}
    .sub{color:#adb5bd;font-size:13px}
    .pill{display:inline-block;background:#131c2e;border:1px solid #22304a;border-radius:999px;
          padding:2px 10px;font-size:12px;margin-right:6px}
    .ok{color:#69db7c}.warn{color:#ffd43b}
    ol{margin:6px 0 0 18px;padding:0}li{margin:3px 0}
    .cf{background:#111a2b;border-left:3px solid #ffd43b;padding:10px 14px;font-size:13px}
    footer{margin-top:28px;color:#868e96;font-size:12px;border-top:1px solid #22304a;padding-top:12px}
    """
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>ChainTrace case file · {html.escape(str(alert.get('alert_id')))}</title>",
        f"<style>{style}</style></head><body>",
        "<h1>ChainTrace AI — case file</h1>",
        f"<div class='sub'>{html.escape(str(alert.get('alert_id')))} · generated {generated} · "
        "offline prototype, synthetic data</div>",
        "<p>",
        f"<span class='pill'>risk {float(alert.get('risk_score') or 0.0):.2f}</span>",
        f"<span class='pill'>confidence {float(alert.get('confidence') or 0.0):.2f}</span>",
        f"<span class='pill'>{html.escape(str(alert.get('entity_type')))}</span>",
        "</p>",
        f"<p><code>{html.escape(str(alert.get('entity')))}</code></p>",
        "<h2>Summary</h2>", _table(["Field", "Value"], summary_rows),
        "<h2>Why this was flagged</h2>",
        "<ol>" + "".join(f"<li>{html.escape(str(reason))}</li>" for reason in reasons) + "</ol>",
        "<h2>What would clear this entity (counterfactual)</h2>",
        f"<div class='cf'>{html.escape(str(alert.get('counterfactual_summary') or '-'))}</div>",
        _table(["Evidence removed", "Score without it", "Score drop", "Would clear?"], counterfactual_rows),
        "<h2>Neighbourhood</h2>", svg or "<p><em>graph artefact not available</em></p>",
        "<h2>Financials</h2>", _table(["Metric", "Value"], financial_rows),
        "<h2>Ownership cluster</h2>", _table(["Field", "Value"], cluster_rows),
        "<h2>Peeling chain</h2>", _table(["Field", "Value"], peel_rows),
        "<h2>Network evidence (correlated packets)</h2>",
        _table(["Source IP", "Country", "ASN", "Operator", "Matched by", "Confidence", "Δ time"],
               network_rows),
        "<h2>Related transactions</h2>",
        _table(["TXID", "Role", "Amount (BTC)", "Timestamp"], related_rows),
        "<h2>Integrity</h2>",
        f"<p>Evidence SHA-256<br><code>{html.escape(hash_value)}</code></p>",
        f"<p>Ledger: <span class='{'ok' if chain_ok else 'warn'}'>{chain_text}</span> "
        f"({(ledger_report or {}).get('chain_length', 0)} sealed entries)</p>",
        "<p class='sub'>Re-verify with:<br>"
        "<code>python -m src.utils.hashing --verify</code><br>"
        "<code>python -m src.utils.hashing --tamper-demo</code></p>",
        "<footer>ChainTrace AI is a prototype: synthetic data only, no live Bitcoin node, "
        "no outbound network access. An alert is an investigative lead, not a finding of guilt.</footer>",
        "</body></html>",
    ]

    # ------------------------------------------------------------ Markdown --
    md: List[str] = [
        f"# ChainTrace AI — case file: `{alert.get('alert_id')}`", "",
        f"*Generated {generated} · offline prototype, synthetic data*", "",
        f"- **Entity**: `{alert.get('entity')}` ({alert.get('entity_type')})",
        f"- **Risk**: {float(alert.get('risk_score') or 0.0):.3f} · "
        f"**Confidence**: {float(alert.get('confidence') or 0.0):.3f}",
        f"- **Recommended action**: {alert.get('recommended_action', '-')}", "",
        "## Why this was flagged", "",
        *[f"1. {reason}" for reason in reasons], "",
        "## What would clear this entity (counterfactual)", "",
        f"> {alert.get('counterfactual_summary') or '-'}", "",
        _md_table(["Evidence removed", "Score without it", "Score drop", "Would clear?"],
                  counterfactual_rows), "",
        "## Financials", "", _md_table(["Metric", "Value"], financial_rows), "",
        "## Ownership cluster", "", _md_table(["Field", "Value"], cluster_rows), "",
        "## Peeling chain", "", _md_table(["Field", "Value"], peel_rows), "",
        "## Network evidence (correlated packets)", "",
        _md_table(["Source IP", "Country", "ASN", "Operator", "Matched by", "Confidence", "Δ time"],
                  network_rows), "",
        "## Related transactions", "",
        _md_table(["TXID", "Role", "Amount (BTC)", "Timestamp"], related_rows), "",
        "## Integrity", "",
        f"- Evidence SHA-256: `{hash_value}`",
        f"- Ledger: {chain_text} ({(ledger_report or {}).get('chain_length', 0)} sealed entries)",
        "- Re-verify: `python -m src.utils.hashing --verify`", "",
        "_An alert is an investigative lead, not a finding of guilt._",
    ]

    return {
        "html": "".join(parts),
        "markdown": "\n".join(md),
        "filename": f"{alert.get('alert_id', 'alert')}_case_file",
        "json": json.dumps(alert, indent=2, sort_keys=True, default=str),
    }
