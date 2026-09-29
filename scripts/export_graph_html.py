#!/usr/bin/env python
"""Export the entity/transaction graph as a standalone interactive HTML file.

    python scripts/export_graph_html.py                 # top 400 nodes by risk
    python scripts/export_graph_html.py --max-nodes 800 --open

The output (``data/artifacts/graph.html``) is a single self-contained file - pyvis
assets are inlined, so it opens in a browser with the network cable unplugged.  It
is the same rendering the dashboard offers as a download, and it is handy for a
live demo on a machine where the Streamlit server is not running.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.graph.builder import load_graph_payload  # noqa: E402
from src.utils.helpers import setup_logging  # noqa: E402

PALETTE = ["#4dabf7", "#f783ac", "#69db7c", "#ffd43b", "#b197fc", "#ffa94d", "#63e6be", "#ff8787"]


def risk_colour(score: float) -> str:
    if score >= 0.85:
        return "#e03131"
    if score >= 0.7:
        return "#ff922b"
    if score >= 0.5:
        return "#fcc419"
    if score >= 0.3:
        return "#94d82d"
    return "#2b8a3e"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the ChainTrace AI graph as interactive HTML")
    parser.add_argument("--graph", type=Path, default=config.GRAPH_PATH)
    parser.add_argument("--output", type=Path, default=config.ARTIFACT_DIR / "graph.html")
    parser.add_argument("--max-nodes", type=int, default=400)
    parser.add_argument("--open", action="store_true", help="open the file in a browser afterwards")
    args = parser.parse_args()

    setup_logging("WARNING")
    payload = load_graph_payload(args.graph)
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    if not nodes:
        print(f"No graph found at {args.graph} - run the pipeline first.")
        return 1

    degree: dict = {}
    for edge in edges:
        degree[edge["source"]] = degree.get(edge["source"], 0) + 1
        degree[edge["target"]] = degree.get(edge["target"], 0) + 1

    nodes.sort(
        key=lambda node: (float(node.get("risk_score") or 0.0), degree.get(node["id"], 0)),
        reverse=True,
    )
    keep = {node["id"] for node in nodes[: args.max_nodes]}
    drawn_nodes = [node for node in nodes if node["id"] in keep]
    drawn_edges = [edge for edge in edges if edge["source"] in keep and edge["target"] in keep]

    from pyvis.network import Network

    network = Network(
        height="900px", width="100%", directed=True, notebook=False,
        bgcolor="#0b1220", font_color="#e7ecf5", cdn_resources="in_line",
    )
    network.barnes_hut(gravity=-12000, spring_length=140)

    for node in drawn_nodes:
        score = float(node.get("risk_score") or 0.0)
        cluster = node.get("cluster_id")
        colour = risk_colour(score) if score > 0 else (
            PALETTE[int(cluster) % len(PALETTE)] if cluster is not None else "#495057"
        )
        node_type = node.get("node_type") or "wallet"
        shape = {"wallet": "dot", "txid": "square", "ip": "triangle"}.get(node_type, "dot")
        title = (
            f"<b>{node_type}</b><br>{node['id']}<br>"
            f"risk {score:.3f} · cluster {cluster if cluster is not None else '-'} · "
            f"degree {degree.get(node['id'], 0)}"
            + ("<br><b>SEED WALLET</b>" if node.get("is_seed") else "")
        )
        network.add_node(
            node["id"], label=str(node.get("label") or node["id"])[:22], title=title,
            color=colour, shape=shape, size=8 + 26 * score,
        )
    for edge in drawn_edges:
        network.add_edge(
            edge["source"], edge["target"],
            title=f"{edge.get('edge_type', '')} {edge.get('amount') or ''}",
            color="#2b3a55",
        )

    network.set_options(
        """
        {"physics": {"stabilization": {"iterations": 220}},
         "interaction": {"hover": true, "tooltipDelay": 90}}
        """
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    network.write_html(str(args.output), notebook=False, open_browser=False)
    print(f"wrote {args.output} ({len(drawn_nodes)} nodes, {len(drawn_edges)} edges)")
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
