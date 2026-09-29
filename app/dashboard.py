"""ChainTrace AI - investigator dashboard (spec section 14).

Four screens, all offline:

1. **Overview & Ranked Alerts** - counters, filters, the ranked alert table, the full
   explanation for the selected alert (reasons + feature contributions) and the
   counterfactual: what evidence would have to fall away to clear the entity.
2. **Interactive Graph** - clickable wallet/transaction/IP map, coloured by risk or
   by ownership cluster.  Tapping a node puts it in **focus**: that node and its direct
   connections stay bright while the rest of the graph fades, tapping a lit neighbour
   walks the ladder one hop further, and the focused entity gets its own **timeline**
   (every transaction it took part in, in order, with amounts and counterparties).
   A seek bar (dragged by hand - there is no auto-play) unrolls the whole collection
   window hop by hop.
3. **Infrastructure** - the flagged activity rolled up by provider (ASN) and country,
   i.e. "which three networks should we talk to next?".
4. **Evidence & Integrity** - the complete evidence package for an alert, its
   SHA-256 hash, live re-verification, the hash-chained ledger (with a tamper demo)
   and the one-click case-file export.

The dashboard is a pure client of the artefacts written by the pipeline.  It tries
the FastAPI backend first (``CHAINTRACE_API_URL``) and silently falls back to
reading ``data/artifacts/`` from disk, so the demo works even with the API stopped.
"""

from __future__ import annotations

import io
import json
import math
import os
import random
import sys
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import __version__, config  # noqa: E402
from src.data.generator import generate_dataset  # noqa: E402
from src.graph.builder import load_graph_payload  # noqa: E402
from src.pipeline.runner import load_alerts, load_pipeline_summary, run_full_pipeline  # noqa: E402
from src.utils.casefile import build_case_file  # noqa: E402
from src.utils.focus import (  # noqa: E402
    EDGE_COLOURS,
    advance_path,
    flow_rows,
    focus_view,
    index_nodes,
    node_events,
    node_facts,
    path_summary,
    route_summary,
    short_id,
    timeline_stats,
)
from src.utils.hashing import (  # noqa: E402
    read_ledger,
    tamper_demo,
    verify_ledger,
    verify_ledger_chain,
)
from src.utils.helpers import first_seen_by_edge, format_btc  # noqa: E402
from src.utils.infrastructure import focus_list, rollup  # noqa: E402

RISK_COLOURS = ["#2b8a3e", "#94d82d", "#fcc419", "#ff922b", "#e03131"]
#: Legend wording for the three edge meanings, kept next to the colours in
#: :mod:`src.utils.focus` so the graph and the timeline tell the same story.
EDGE_LEGEND = [
    ("in", "wallet \u2192 transaction (funds it)"),
    ("out", "transaction \u2192 wallet (pays it)"),
    ("network", "IP \u2194 transaction (correlated)"),
]
#: How many lit hops the money-flow animation carries.  A focus neighbourhood is a couple
#: of dozen edges; the cap only exists so a pathological hub (a mixing transaction with
#: hundreds of legs) cannot turn the frame animation into a slideshow.
FLOW_EDGE_LIMIT = 90
#: Set ``CHAINTRACE_FOCUS_DEBUG=1`` to print, under the chart, exactly how each tap was
#: read (selection, last handled click, epoch).  Kept in the console on purpose: the click
#: path is the one part of the graph that depends on the browser, so when a tap looks
#: "lost" this line says whether the browser sent it at all.
FOCUS_DEBUG = bool(os.environ.get("CHAINTRACE_FOCUS_DEBUG"))

# --------------------------------------------------------------------------- #
# styling
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="ChainTrace AI - Bitcoin investigation console",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #0b1220; }
      section[data-testid="stSidebar"] { background: #0e1729; border-right: 1px solid #1f2a44; }
      h1, h2, h3, h4 { color: #e7ecf5 !important; letter-spacing: .2px; }
      .ct-sub { color: #8fa3bf; font-size: .86rem; margin-top: -0.6rem; }
      .ct-card {
          background: #111c33; border: 1px solid #1f2a44; border-radius: 10px;
          padding: 14px 16px; margin-bottom: 10px;
      }
      .ct-pill {
          display: inline-block; padding: 2px 9px; border-radius: 999px;
          font-size: .72rem; font-weight: 600; letter-spacing: .3px;
      }
      .ct-safe { background: #12351f; color: #69db7c; }
      .ct-warn { background: #3d2a06; color: #ffd43b; }
      .ct-bad  { background: #3b0d0d; color: #ff8787; }
      code { color: #74c0fc; }
      div[data-testid="stMetricValue"] { color: #e7ecf5; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# data access (API first, local artefacts as fallback)
# --------------------------------------------------------------------------- #
def _api_get(path: str, timeout: float = config.API_TIMEOUT_SECONDS) -> Optional[Any]:
    try:
        import requests

        response = requests.get(f"{config.API_URL}{path}", timeout=timeout)
        if response.status_code == 200:
            return response.json()
    except Exception:
        return None
    return None


def api_alive() -> bool:
    payload = _api_get("/health", timeout=1.5)
    return bool(payload and payload.get("status") == "ok")


@st.cache_data(ttl=5, show_spinner=False)
def _artifacts_mtime() -> float:
    stamps = [
        path.stat().st_mtime
        for path in (config.ALERTS_PATH, config.SUMMARY_PATH, config.GRAPH_PATH)
        if path.exists()
    ]
    return max(stamps) if stamps else 0.0


def load_bundle() -> Dict[str, Any]:
    """Alerts + summary + graph, preferring the API when it answers."""
    api_stats = _api_get("/stats")
    api_alerts = _api_get("/alerts?limit=500")
    if api_stats and api_alerts:
        return {
            "source": "api",
            "stats": api_stats,
            "alerts": api_alerts.get("alerts", []),
            "graph": _api_get("/graph") or load_graph_payload(),
            "summary": load_pipeline_summary(),
            "detailed": False,
        }
    alerts = load_alerts()
    return {
        "source": "files",
        "stats": None,
        "alerts": alerts,
        "graph": load_graph_payload(),
        "summary": load_pipeline_summary(),
        "detailed": True,
    }


# --------------------------------------------------------------------------- #
# the bundled sample dataset (offered for download, never rebuilt for it)
# --------------------------------------------------------------------------- #
# The dataset ships with the repository, so a deployed copy has it from the first
# second - the analysis is what gets built on top of it.  These entries are the files a
# reviewer would otherwise have to run the generator to see, and the ground-truth
# manifest is the one that makes the measured numbers on the other tabs checkable.
DATASET_FILES: Sequence[Sequence[str]] = (
    (
        "transactions.csv",
        "text/csv",
        "Transaction layer: txid, inputs, outputs, value, fee, block height, timestamp.",
    ),
    (
        "network_metadata.csv",
        "text/csv",
        "Network layer: src/dst IP and port, protocol, bytes, TXID, country and ASN.",
    ),
    (
        "bulk_metadata.csv",
        "text/csv",
        "Both layers in one file - the single-file format SIH26146 literally describes.",
    ),
    (
        "seed_illicit_wallets.json",
        "application/json",
        "The known-bad seed wallets that risk propagation starts from.",
    ),
    (
        "planted_patterns.json",
        "application/json",
        "Ground-truth manifest: which patterns were planted where. Feeds the benchmark, "
        "never the pipeline.",
    ),
)


def dataset_files() -> List[Dict[str, Any]]:
    """The bundled dataset files that are actually on disk, with a row count for CSVs."""
    found: List[Dict[str, Any]] = []
    for name, mime, description in DATASET_FILES:
        path = config.SYNTHETIC_DIR / name
        if not path.exists():
            continue
        rows: Optional[int] = None
        if name.endswith(".csv"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    rows = max(sum(1 for _ in handle) - 1, 0)
            except OSError:
                rows = None
        found.append({"name": name, "path": path, "mime": mime, "description": description, "rows": rows})
    return found


def dataset_zip() -> bytes:
    """One archive: the dataset plus the offline GeoIP table it is enriched from."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in dataset_files():
            archive.write(entry["path"], arcname=f"chaintrace_sample/{entry['name']}")
        if config.MOCK_GEO_PATH.exists():
            archive.write(config.MOCK_GEO_PATH, arcname="chaintrace_sample/mock_geoip.json")
    return buffer.getvalue()


def _synthetic_fingerprint() -> float:
    """Newest mtime in the dataset directory - the cache key for the downloads."""
    stamps = [path.stat().st_mtime for path in config.SYNTHETIC_DIR.glob("*") if path.is_file()]
    return max(stamps) if stamps else 0.0


@st.cache_data(show_spinner=False)
def _dataset_bytes(path: str, mtime: float) -> bytes:
    """File contents, cached - the mtime argument *is* the cache key."""
    return Path(path).read_bytes()


@st.cache_data(show_spinner=False)
def _dataset_zip(fingerprint: float) -> bytes:
    """The whole archive, cached the same way."""
    return dataset_zip()


def risk_class(score: float) -> str:
    if score >= 0.75:
        return "ct-bad"
    if score >= 0.5:
        return "ct-warn"
    return "ct-safe"


def risk_colour(score: float) -> str:
    if score >= 0.85:
        return RISK_COLOURS[4]
    if score >= 0.7:
        return RISK_COLOURS[3]
    if score >= 0.5:
        return RISK_COLOURS[2]
    if score >= 0.3:
        return RISK_COLOURS[1]
    return RISK_COLOURS[0]


def _interactive_html(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    risk_by_id: Dict[str, float],
) -> str:
    """Build a standalone pyvis HTML file (offline: assets are inlined)."""
    try:
        from pyvis.network import Network

        network = Network(height="700px", width="100%", directed=True, notebook=False, cdn_resources="in_line")
        for node in nodes:
            score = float(node.get("risk_score") or 0.0)
            network.add_node(
                node["id"],
                label=str(node.get("label") or node["id"]),
                title=f"{node.get('node_type')} · risk {score:.3f}",
                color=risk_colour(score),
                size=8 + 18 * score,
            )
        for edge in edges:
            network.add_edge(edge["source"], edge["target"], title=edge.get("edge_type", ""))
        return network.generate_html()
    except Exception as exc:  # pragma: no cover - pyvis optional in some installs
        return f"<html><body><p>pyvis unavailable: {exc}</p></body></html>"


#: How many snapshots the timeline replay scrubs through.  They become plotly *frames* with a
#: plotly *slider* in the chart itself, so dragging is a client-side animation - no rerun, no
#: reload (see :func:`_replay_figure`).  More frames = finer scrubbing but a bigger figure,
#: because every frame re-sends where the nodes are and which edges exist; this is the balance
#: point (~1.2 days per step over the shipped 30-day window).
REPLAY_FRAMES = 24


def _node_colour(node: Dict[str, Any], colour_mode: str) -> str:
    """Marker colour for one node: by risk band, ownership cluster, or node type."""
    if colour_mode.startswith("Risk"):
        return risk_colour(float(node.get("risk_score") or 0.0))
    if colour_mode.startswith("Ownership"):
        cluster_id = node.get("cluster_id")
        if cluster_id is None:
            return "#495057"
        palette = [
            "#4dabf7", "#f783ac", "#69db7c", "#ffd43b", "#b197fc",
            "#ffa94d", "#63e6be", "#ff8787", "#74c0fc", "#e599f7",
        ]
        return palette[int(cluster_id) % len(palette)]
    return {
        "wallet": "#4dabf7", "txid": "#adb5bd", "ip": "#ffa94d",
    }.get(node.get("node_type") or "wallet", "#adb5bd")


def _hover_for(node: Dict[str, Any], faded: bool = False, clickable: bool = True) -> str:
    """The tooltip for one node.

    ``clickable`` is switched off for the timeline replay: there the picture is a time
    scrubber and a tap does not move the focus, so the tooltip must not promise that it does.
    """
    lines = [
        f"<b>{node.get('node_type', 'node')}</b>  risk {float(node.get('risk_score') or 0.0):.3f}",
        str(node["id"]),
    ]
    node_type = node.get("node_type")
    if node_type == "txid":
        lines.append(
            f"{float(node.get('total_in') or 0):.4f} BTC in \u00b7 "
            f"{float(node.get('total_out') or 0):.4f} BTC out"
        )
    elif node_type == "wallet":
        lines.append(
            f"{int(node.get('tx_count') or 0)} transactions \u00b7 "
            f"cluster {node.get('cluster_id', '-')}"
        )
    else:
        lines.append(f"{node.get('geo_country', '?')} \u00b7 {node.get('geo_asn', '?')}")
    if node.get("is_seed"):
        lines.append("<b>SEED WALLET (known bad)</b>")
    if clickable:
        lines.append(
            "<i>faded - not a direct connection, click to focus here</i>"
            if faded
            else "<i>click to focus</i>"
        )
    return "<br>".join(lines)


def _network_figure(
    drawn: List[Dict[str, Any]],
    drawn_edges: List[Dict[str, Any]],
    positions: Dict[str, Any],
    degree: Dict[str, int],
    colour_mode: str,
    focus: Optional[Dict[str, Any]] = None,
) -> Any:
    """Build the plotly figure for one set of nodes/edges.

    Without ``focus`` the whole frame is drawn at one intensity.  With a focus (the ladder
    set from :func:`focus_view`) only the head node and its **direct** connections stay
    bright: the route walked so far is drawn in amber, everything unrelated drops to a
    faint grey.  Nodes are faded, never removed - a click anywhere still moves the focus,
    so the investigator can walk the graph one hop at a time.

    Every lit hop in a focused view additionally carries the money-flow animation: a coin
    leaves the sender and rides along the edge to the receiver, with a comet tail tracing
    the line it followed (see the flow block below).  It is built only when there is
    something lit to animate, so the whole-graph view stays a single static picture.
    """
    import plotly.graph_objects as go

    # both of these are also used by the replay figure, so they live at module level
    def node_colour(node: Dict[str, Any]) -> str:
        return _node_colour(node, colour_mode)

    head_id = str(focus["head"]) if focus and focus.get("head") else None
    lit_nodes = set(focus["nodes"]) if head_id else set()
    lit_edges = set(focus["edges"]) if head_id else set()
    ladder_edges = set(focus["ladder"]) if head_id else set()
    # filled in only in the focused branch: the whole-graph view has nothing to animate
    flow_frames: List[Any] = []

    def pair(edge: Dict[str, Any]) -> tuple:
        return tuple(sorted((str(edge.get("source")), str(edge.get("target")))))

    def edge_group(edge: Dict[str, Any]) -> str:
        """Classify an edge for the fade: the route, a direct connection, or background."""
        if head_id is None:
            return "all"
        if pair(edge) in ladder_edges:
            return "ladder"
        return "focus" if pair(edge) in lit_edges else "dim"

    def edge_segments(selector) -> tuple:
        xs: List[Optional[float]] = []
        ys: List[Optional[float]] = []
        for edge in drawn_edges:
            if not selector(edge):
                continue
            x0, y0 = positions.get(edge["source"], (0.0, 0.0))
            x1, y1 = positions.get(edge["target"], (0.0, 0.0))
            xs += [x0, x1, None]
            ys += [y0, y1, None]
        return xs, ys

    def hover_for(node: Dict[str, Any], faded: bool = False) -> str:
        return _hover_for(node, faded=faded)

    def node_scatter(
        subset: List[Dict[str, Any]],
        sizes: List[float],
        colours: Any,
        *,
        ring: Optional[Any] = None,
        opacity: float = 1.0,
        text: Optional[List[str]] = None,
        name: Optional[str] = None,
    ) -> Any:
        return go.Scatter(
            x=[positions.get(node["id"], (0.0, 0.0))[0] for node in subset],
            y=[positions.get(node["id"], (0.0, 0.0))[1] for node in subset],
            mode="markers",
            marker=dict(
                size=sizes,
                color=colours,
                opacity=opacity,
                line=ring or dict(width=1, color="#0b1220"),
            ),
            text=text if text is not None else [hover_for(node) for node in subset],
            hoverinfo="text",
            customdata=[node["id"] for node in subset],
            name=name or "",
            showlegend=name is not None,
        )

    # Edges are drawn with hoverinfo="skip" on purpose: a line vertex sits exactly on the
    # marker it joins, and plotly's "closest" picking then returns the *edge* - which
    # carries no customdata - so a tap on a node would resolve to nothing.  Skipping the
    # edges in hit-testing hands every click to the node under the cursor.
    figure = go.Figure()
    if head_id is None:
        edge_x, edge_y = edge_segments(lambda edge: True)
        figure.add_trace(
            go.Scatter(
                x=edge_x, y=edge_y, mode="lines", hoverinfo="skip",
                line=dict(width=0.6, color="#2b3a55"), showlegend=False,
            )
        )
        figure.add_trace(
            node_scatter(
                drawn,
                [10 + min(degree.get(node["id"], 0), 40) for node in drawn],
                [node_colour(node) for node in drawn],
            )
        )
    else:
        # background first, then the evidence, then the nodes, head last so it sits on top
        edge_x, edge_y = edge_segments(lambda edge: edge_group(edge) == "dim")
        figure.add_trace(
            go.Scatter(
                x=edge_x, y=edge_y, mode="lines", hoverinfo="skip",
                line=dict(width=0.5, color="#16233a"), showlegend=False,
            )
        )
        for kind, label in EDGE_LEGEND:
            edge_x, edge_y = edge_segments(
                lambda edge, want=kind: edge_group(edge) == "focus"
                and edge.get("edge_type") == want
            )
            figure.add_trace(
                go.Scatter(
                    x=edge_x, y=edge_y, mode="lines", hoverinfo="skip",
                    line=dict(width=2.6, color=EDGE_COLOURS[kind]),
                    name=label, showlegend=bool(edge_x),
                )
            )
        edge_x, edge_y = edge_segments(lambda edge: edge_group(edge) == "ladder")
        figure.add_trace(
            go.Scatter(
                x=edge_x, y=edge_y, mode="lines", hoverinfo="skip",
                line=dict(width=2.8, color="#fcc419", dash="dot"),
                name="your route (breadcrumbs)", showlegend=bool(edge_x),
            )
        )

        # ------------------------------------------------------------------ #
        # the money-flow animation: a coin per hop, from the sender to the receiver
        # ------------------------------------------------------------------ #
        # A line says "these two touched"; the thing an investigator has to read off it
        # is *which way the value went*.  The arrowheads already state the direction;
        # this makes it something the eye can follow - every lit hop gets a coin that
        # leaves the sender, rides along the edge and lands on the receiver, dragging a
        # comet tail that traces the line behind it.  Plotly has no moving-line
        # primitive, so the movement is a frame animation played by the "▶ Animate the
        # money flow" button in the chart's own bottom-right corner (see the layout block
        # below for why that corner and not the top-right).  Only lit hops are animated:
        # motion on three hundred faded edges would be decoration, not evidence.
        flow_colours = {"ladder": "#fcc419", **EDGE_COLOURS}
        flow_buckets = ["ladder", "in", "out", "network"]
        flow_edges: List[Dict[str, Any]] = []
        for edge in drawn_edges:
            if edge_group(edge) not in ("focus", "ladder"):
                continue
            if edge.get("amount") is None and edge.get("edge_type") != "network":
                continue
            start = positions.get(edge["source"])
            end = positions.get(edge["target"])
            if not start or not end:
                continue
            flow_edges.append(
                {
                    "edge": edge,
                    "start": start,
                    "end": end,
                    "bucket": (
                        "ladder" if edge_group(edge) == "ladder"
                        else str(edge.get("edge_type") or "in")
                    ),
                }
            )
            if len(flow_edges) >= FLOW_EDGE_LIMIT:
                break

        biggest_flow = max(
            (float(item["edge"].get("amount") or 0.0) for item in flow_edges), default=0.0
        )

        def flow_coin_size(item: Dict[str, Any]) -> float:
            """Coins grow with the amount (square-root, so one whale does not hide the rest)."""
            amount = item["edge"].get("amount")
            if amount is None or biggest_flow <= 0:
                return 7.5
            return 7.0 + 7.0 * math.sqrt(float(amount) / biggest_flow)

        def flow_tail(progress: float, bucket: str) -> tuple:
            """The stretch each hop has just covered, drawn as a comet tail.

            The tail spans ``progress - 0.28`` to ``progress`` of the way from sender to
            receiver, in the colour that hop's money direction already uses, so the pulse
            reads as travel along an existing line rather than as a new kind of edge.
            """
            xs: List[Optional[float]] = []
            ys: List[Optional[float]] = []
            for item in flow_edges:
                if item["bucket"] != bucket:
                    continue
                (x0, y0), (x1, y1) = item["start"], item["end"]
                start_at = max(progress - 0.28, 0.0)
                xs += [x0 + (x1 - x0) * start_at, x0 + (x1 - x0) * progress, None]
                ys += [y0 + (y1 - y0) * start_at, y0 + (y1 - y0) * progress, None]
            return xs, ys

        def flow_heads(progress: float) -> tuple:
            """Where every coin is at ``progress`` of its hop."""
            xs = [item["start"][0] + (item["end"][0] - item["start"][0]) * progress
                  for item in flow_edges]
            ys = [item["start"][1] + (item["end"][1] - item["start"][1]) * progress
                  for item in flow_edges]
            return xs, ys

        flow_trace_indices: List[int] = []
        flow_frames: List[Any] = []
        if flow_edges:
            for bucket in flow_buckets:
                tail_x, tail_y = flow_tail(0.0, bucket)
                figure.add_trace(
                    go.Scatter(
                        x=tail_x, y=tail_y, mode="lines", hoverinfo="skip",
                        line=dict(width=3.6, color=flow_colours[bucket]),
                        showlegend=False,
                    )
                )
                flow_trace_indices.append(len(figure.data) - 1)
            figure.add_trace(
                go.Scatter(
                    x=[], y=[], mode="markers", hoverinfo="skip",
                    marker=dict(
                        size=[flow_coin_size(item) for item in flow_edges],
                        color=[flow_colours[item["bucket"]] for item in flow_edges],
                        line=dict(width=1.2, color="#f8f9fa"),
                    ),
                    showlegend=False,
                )
            )
            flow_trace_indices.append(len(figure.data) - 1)

            steps = 12
            for step in range(1, steps + 1):
                progress = step / steps
                head_x, head_y = flow_heads(progress)
                flow_frames.append(
                    go.Frame(
                        name=str(step),
                        traces=flow_trace_indices,
                        data=[
                            *[
                                go.Scatter(
                                    x=flow_tail(progress, bucket)[0],
                                    y=flow_tail(progress, bucket)[1],
                                    mode="lines", hoverinfo="skip",
                                    line=dict(width=3.6, color=flow_colours[bucket]),
                                    showlegend=False,
                                )
                                for bucket in flow_buckets
                            ],
                            go.Scatter(
                                x=head_x, y=head_y, mode="markers", hoverinfo="skip",
                                marker=dict(
                                    size=[flow_coin_size(item) for item in flow_edges],
                                    color=[flow_colours[item["bucket"]] for item in flow_edges],
                                    line=dict(width=1.2, color="#f8f9fa"),
                                ),
                                showlegend=False,
                            ),
                        ],
                    )
                )

        # Direction of the money, drawn on the edge itself.  A line says "these two touched"
        # and says nothing about who paid whom, so every lit edge (route + direct
        # connections) gets one arrowhead pointing the way the coin moved: wallet -> txid
        # for a spend, txid -> wallet for a payout, ip -> txid for a matched packet.  Only
        # lit edges get arrows - a few hundred arrowheads on the faded background would be
        # noise, not evidence.
        for edge in drawn_edges:
            group = edge_group(edge)
            if group not in ("focus", "ladder") or edge.get("amount") is None \
                    and edge.get("edge_type") != "network":
                continue
            start = positions.get(edge["source"])
            end = positions.get(edge["target"])
            if not start or not end:
                continue
            tip = (
                start[0] + (end[0] - start[0]) * 0.74,
                start[1] + (end[1] - start[1]) * 0.74,
            )
            tail = (
                start[0] + (end[0] - start[0]) * 0.52,
                start[1] + (end[1] - start[1]) * 0.52,
            )
            if tip == tail:
                continue
            figure.add_annotation(
                x=tip[0], y=tip[1], ax=tail[0], ay=tail[1],
                xref="x", yref="y", axref="x", ayref="y",
                text="", showarrow=True, arrowhead=3, arrowsize=1.6,
                arrowwidth=2.0,
                arrowcolor="#fcc419" if group == "ladder"
                else EDGE_COLOURS.get(str(edge.get("edge_type")), "#adb5bd"),
                opacity=0.95,
            )

        path_ids = set(focus["path"])
        background = [node for node in drawn if node["id"] not in lit_nodes]
        neighbours = [
            node for node in drawn if node["id"] in lit_nodes and node["id"] not in path_ids
        ]
        breadcrumbs = [
            node for node in drawn if node["id"] in path_ids and node["id"] != head_id
        ]
        head_node = next((node for node in drawn if node["id"] == head_id), None)
        size_of = lambda node: 16 + 0.5 * min(degree.get(node["id"], 0), 40)  # noqa: E731

        if background:
            figure.add_trace(
                node_scatter(
                    background,
                    [4 + 0.12 * min(degree.get(node["id"], 0), 40) for node in background],
                    "#2f3c52",
                    ring=dict(width=0.5, color="#16233a"),
                    opacity=0.32,
                    text=[hover_for(node, faded=True) for node in background],
                    name="faded - not a direct connection",
                )
            )
        if neighbours:
            # A soft halo under every lit neighbour.  One slightly brighter dot among
            # three hundred grey ones is easy to miss; a halo makes the lit neighbourhood
            # read as a *cluster* the moment the tap lands - which is the whole point of
            # focusing, and the thing that was hard to see before.
            figure.add_trace(
                node_scatter(
                    neighbours,
                    [size_of(node) + 11 for node in neighbours],
                    [node_colour(node) for node in neighbours],
                    ring=dict(width=0, color="rgba(0,0,0,0)"),
                    opacity=0.20,
                    text=[hover_for(node) for node in neighbours],
                )
            )
            figure.add_trace(
                node_scatter(
                    neighbours,
                    [size_of(node) for node in neighbours],
                    [node_colour(node) for node in neighbours],
                    ring=dict(width=2.2, color="#f8f9fa"),
                    name="direct connection",
                )
            )
        if breadcrumbs:
            figure.add_trace(
                node_scatter(
                    breadcrumbs,
                    [size_of(node) + 13 for node in breadcrumbs],
                    "#fcc419",
                    ring=dict(width=0, color="rgba(0,0,0,0)"),
                    opacity=0.18,
                    text=[hover_for(node) for node in breadcrumbs],
                )
            )
            figure.add_trace(
                node_scatter(
                    breadcrumbs,
                    [size_of(node) - 4 for node in breadcrumbs],
                    [node_colour(node) for node in breadcrumbs],
                    ring=dict(width=3.0, color="#fcc419"),
                )
            )
        if head_node:
            figure.add_trace(
                node_scatter(
                    [head_node],
                    [size_of(head_node) + 12],
                    [node_colour(head_node)],
                    ring=dict(width=3.5, color="#ffffff"),
                )
            )
    if flow_frames:
        # The play control lives inside the chart itself, so the movement can be replayed
        # without leaving the picture.  It is a one-shot play rather than a loop: an
        # investigator who wants to look again presses it again.
        #
        # Where it sits is deliberate.  It used to hang above the plot at ``y=1.14``, which
        # is exactly where plotly draws its own modebar (zoom / pan / box-select / reset) as
        # soon as the cursor is over the chart: measured in a browser, the pill occupied
        # x 1252-1411, y 936-969 while the modebar occupied x 1140-1412, y 937-963 - the same
        # pixels, so two sets of controls fought over one corner of the picture.  The
        # bottom-right corner is free in every view of this chart: the modebar is top-right,
        # the legend is top-left, and the axes are invisible (no labels to collide with).
        figure.frames = flow_frames
        figure.update_layout(
            updatemenus=[
                dict(
                    type="buttons",
                    direction="left",
                    showactive=False,
                    x=0.995,
                    xanchor="right",
                    y=0.02,
                    yanchor="bottom",
                    pad=dict(t=0, b=2, r=2),
                    bgcolor="rgba(11,18,32,0.92)",
                    bordercolor="#3b4a66",
                    font=dict(color="#e7ecf5", size=12),
                    buttons=[
                        dict(
                            label="▶ Animate the money flow",
                            method="animate",
                            args=[
                                None,
                                dict(
                                    frame=dict(duration=320, redraw=True),
                                    transition=dict(duration=180),
                                    fromcurrent=True,
                                    mode="immediate",
                                ),
                            ],
                        )
                    ],
                )
            ]
        )

    figure.update_layout(
        height=620,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="#0b1220",
        plot_bgcolor="#0b1220",
        font=dict(color="#adb5bd"),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        dragmode="pan",
        # A tap has to land on a target it can actually hit, so both of these are stated here
        # instead of left to a frontend default.  `clickmode` makes a click a *point selection*
        # (which is what Streamlit's plotly binding forwards to the app); `hoverdistance` is the
        # click/hover radius in pixels, and plotly's default of 20 is **smaller than the dots
        # the console draws**: node radius is 10-30 px on the whole graph and up to 32 px in
        # focus, so a click on the outer half of a big dot resolved to nothing at all.  That was
        # reproduced in a browser: the same 50 px dot focused at its centre (482, 472) and did
        # nothing 28 px lower, which is the most likely reason a real click "does nothing".
        # 32 px covers the largest marker with a margin, and stays under the ~44 px average
        # spacing between nodes, so it widens the target without stealing the neighbour's dot.
        clickmode="event+select",
        hoverdistance=32,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.0,
            x=0.0,
            font=dict(color="#8fa3bf", size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
    )
    return figure


def _replay_snapshots(
    ticks: Sequence[str],
    base_nodes: Sequence[Dict[str, Any]],
    base_edges: Sequence[Dict[str, Any]],
    first_seen: Dict[str, str],
    alerts: Sequence[Dict[str, Any]],
    count: int = REPLAY_FRAMES,
) -> List[Dict[str, Any]]:
    """The moments the in-chart timeline slider scrubs through.

    ``base_nodes`` is the *final* picture's node set (the same node budget and the same
    layout), so every snapshot is a subset of it: a node keeps the spot it lands on and is
    simply not drawn before its first transaction.  That is what makes the replay read as the
    graph *growing* instead of reshuffling under the cursor.
    """
    if not ticks:
        return []
    count = max(min(count, len(ticks)), 1)
    picks = (
        [len(ticks) - 1] if count == 1
        else sorted({round(index * (len(ticks) - 1) / (count - 1)) for index in range(count)})
    )

    snapshots: List[Dict[str, Any]] = []
    for pick in picks:
        cutoff = ticks[pick]
        visible = {
            str(node["id"]) for node in base_nodes
            if first_seen.get(str(node["id"]))
            and first_seen[str(node["id"])] <= cutoff
        }
        if not visible:  # an empty frame is not a picture
            continue
        # Counted over the *whole* graph, not just the nodes the "Max nodes drawn" budget kept:
        # the interesting claim is "the alert set grows as the window fills in", and a node cap
        # must not be able to flatten it.
        known_alerts = sum(
            1 for alert in alerts
            if first_seen.get(str(alert.get("entity")))
            and first_seen[str(alert.get("entity"))] <= cutoff
        )
        snapshots.append(
            {
                "cutoff": cutoff,
                # "08-23": short enough to print one under the slider without the labels
                # colliding with each other; the readout inside the picture has the exact time
                "label": str(cutoff)[5:10],
                "visible": visible,
                "readout": (
                    f"<b>data known up to {str(cutoff)[:19].replace('T', ' ')}</b>"
                    f"   \u00b7   {len(visible)} of {len(base_nodes)} nodes"
                    f"   \u00b7   {known_alerts} of {len(alerts)} alerts exist by then"
                ),
            }
        )
    return snapshots


def _replay_segments(
    edges: Sequence[Dict[str, Any]],
    positions: Dict[str, Any],
    visible: Any,
    kind: str,
) -> tuple:
    """Line segments for every edge of one direction that both endpoints exist for."""
    xs: List[Optional[float]] = []
    ys: List[Optional[float]] = []
    for edge in edges:
        if str(edge.get("edge_type")) != kind:
            continue
        source, target = str(edge["source"]), str(edge["target"])
        if source not in visible or target not in visible:
            continue
        x0, y0 = positions.get(source, (0.0, 0.0))
        x1, y1 = positions.get(target, (0.0, 0.0))
        xs += [round(float(x0), 4), round(float(x1), 4), None]
        ys += [round(float(y0), 4), round(float(y1), 4), None]
    return xs, ys


def _replay_readout(text: str) -> Dict[str, Any]:
    """The card in the corner of the replay that names the moment on screen."""
    return dict(
        text=text,
        x=0.012, xanchor="left", y=0.015, yanchor="bottom",
        xref="paper", yref="paper", showarrow=False,
        font=dict(color="#e7ecf5", size=12),
        bgcolor="rgba(11,18,32,0.85)",
        bordercolor="#2b3a55", borderwidth=1, borderpad=5,
    )


def _replay_figure(
    snapshots: Sequence[Dict[str, Any]],
    base_nodes: Sequence[Dict[str, Any]],
    base_edges: Sequence[Dict[str, Any]],
    positions: Dict[str, Any],
    degree: Dict[str, int],
    colour_mode: str,
) -> Any:
    """The timeline replay as **one self-contained figure**: frames plus a slider, in the chart.

    Dragging a Streamlit slider reruns the script, and a rerun re-renders the whole chart - with
    a couple of hundred nodes that reads as the page reloading, which is the one thing scrubbing
    a timeline must not do.  So the snapshots are plotly **frames** and the control is a plotly
    **slider** inside the figure: both live in the browser, so dragging is a client-side
    animation with no rerun, no websocket round trip and nothing else on the page touched.

    A frame carries only what changed:

    * where the nodes are - nodes that have not happened yet are parked *outside* the fixed axis
      range, so they are neither drawn nor hoverable (an invisible-but-hoverable dot is worse
      than no dot at all),
    * the line segments of the edges that exist by then, in the same three colours the rest of
      the tab uses for "who paid whom",
    * the readout card, so the exact moment and the alert count travel with the slider.

    That keeps the whole replay to a few hundred kilobytes transferred once, instead of roughly a
    megabyte re-sent on every step of a drag.
    """
    import plotly.graph_objects as go

    ids = [str(node["id"]) for node in base_nodes]
    xs = [round(float(positions.get(node_id, (0.0, 0.0))[0]), 4) for node_id in ids]
    ys = [round(float(positions.get(node_id, (0.0, 0.0))[1]), 4) for node_id in ids]
    if not ids or not snapshots:
        return go.Figure()

    sizes = [10 + min(degree.get(node_id, 0), 40) for node_id in ids]
    colours = [_node_colour(node, colour_mode) for node in base_nodes]
    # no "click to focus" hint: this chart is a time scrubber, the tap does not move the focus
    texts = [_hover_for(node, clickable=False) for node in base_nodes]

    span = max((max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0)
    #: Where a node that has not happened yet is parked.  Outside the range below on purpose:
    #: plotly does not draw or hit-test what is off the axis, so the node simply is not there.
    parked_x = max(xs) + span * 4.0
    parked_y = max(ys) + span * 4.0

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=xs, y=ys, mode="markers",
            marker=dict(size=sizes, color=colours, line=dict(width=1, color="#0b1220")),
            text=texts, hoverinfo="text", customdata=ids, showlegend=False,
        )
    )
    last_visible = snapshots[-1]["visible"]
    trace_of: Dict[str, int] = {}
    for kind, label in EDGE_LEGEND:
        line_x, line_y = _replay_segments(base_edges, positions, last_visible, kind)
        trace_of[kind] = len(figure.data)
        figure.add_trace(
            go.Scatter(
                x=line_x, y=line_y, mode="lines", hoverinfo="skip",
                line=dict(width=1.6, color=EDGE_COLOURS[kind]), name=label, showlegend=True,
            )
        )
    ordered = ["node"] + [kind for kind, _label in EDGE_LEGEND]

    frames: List[Any] = []
    for index, snapshot in enumerate(snapshots):
        visible = snapshot["visible"]
        data: List[Dict[str, Any]] = [
            {
                "x": [x if ids[position] in visible else parked_x for position, x in enumerate(xs)],
                "y": [y if ids[position] in visible else parked_y for position, y in enumerate(ys)],
            }
        ]
        for kind, _label in EDGE_LEGEND:
            line_x, line_y = _replay_segments(base_edges, positions, visible, kind)
            data.append({"x": line_x, "y": line_y})
        frames.append(
            {
                "name": f"at{index}",
                "data": data,
                "traces": [
                    0 if slot == "node" else trace_of[slot] for slot in ordered
                ],
                # inside the picture, so it changes with the frame and still costs no rerun
                "layout": {"annotations": [_replay_readout(snapshot["readout"])]},
            }
        )
    figure.frames = frames

    steps = [
        dict(
            method="animate",
            # a label under every step would collide with its neighbour (24 of them across a
            # ~1000 px chart); every fourth is enough to read the scale, and the card in the
            # corner always names the exact moment
            label=snapshots[index]["label"] if index % 4 == 0 else "",
            args=[
                [frame["name"]],
                dict(
                    mode="immediate",
                    frame=dict(duration=0, redraw=True),
                    transition=dict(duration=0),
                ),
            ],
        )
        for index, frame in enumerate(frames)
    ]

    figure.update_layout(
        sliders=[
            dict(
                active=len(steps) - 1,
                x=0.0, xanchor="left", len=1.0,
                y=0.0, yanchor="top",
                pad=dict(t=10, b=4, l=0, r=0),
                bgcolor="rgba(11,18,32,0.9)",
                bordercolor="#2b3a55",
                borderwidth=1,
                tickcolor="#2b3a55",
                font=dict(color="#8fa3bf", size=11),
                currentvalue=dict(visible=False),
                steps=steps,
            )
        ],
        height=620,
        # bottom margin reserved for the slider strip, so it never sits on the picture
        margin=dict(l=0, r=0, t=10, b=64),
        paper_bgcolor="#0b1220",
        plot_bgcolor="#0b1220",
        font=dict(color="#adb5bd"),
        # Fixed ranges: the frame that parks not-yet-existing nodes does not get to move the
        # view, and a drag on the slider does not either.  Panning and zooming still work.
        xaxis=dict(
            visible=False, fixedrange=False,
            range=[min(xs) - span * 0.06, max(xs) + span * 0.06],
        ),
        yaxis=dict(
            visible=False, fixedrange=False,
            range=[min(ys) - span * 0.06, max(ys) + span * 0.06],
        ),
        dragmode="pan",
        # tighter than the main chart on purpose: this one is scrubbed, not picked apart
        hoverdistance=12,
        annotations=[_replay_readout(snapshots[-1]["readout"])],
        legend=dict(
            orientation="h", yanchor="bottom", y=1.0, x=0.0,
            font=dict(color="#8fa3bf", size=11), bgcolor="rgba(0,0,0,0)",
        ),
    )
    return figure


def _selected_node_id(event: Any, risk_by_id: Optional[Dict[str, float]] = None) -> Optional[str]:
    """Pull the chosen node out of a plotly selection event (Streamlit >= 1.35).

    A tap gives one point; a box or lasso drag gives a bag of them, in which case the
    highest-risk node in the bag is the interesting one to open.  Points that carry no
    ``customdata`` (a click that landed on an edge vertex, before edges were excluded from
    hit-testing) are ignored rather than guessed at.
    """
    try:
        points = (
            event["selection"]["points"]
            if isinstance(event, dict)
            else event.selection.points
        )
    except Exception:
        return None

    candidates: List[str] = []
    for raw in points or []:
        value = raw.get("customdata") if isinstance(raw, dict) else getattr(raw, "customdata", None)
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        if value:
            candidates.append(str(value))
    if not candidates:
        return None
    if risk_by_id:
        return max(candidates, key=lambda node_id: risk_by_id.get(node_id, 0.0))
    return candidates[0]


def _stable_layout(
    frame_nodes: List[Dict[str, Any]], frame_links: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Spring layout whose nodes keep the spot they already had.

    Re-running a spring layout on a different node set moves *everything*, so walking a
    chain hop by hop would make the whole graph jump under the cursor.  Positions are
    therefore remembered for the session: a node that is already placed stays pinned and a
    node appearing for the first time is dropped next to the neighbours that are already
    on screen, then relaxed.
    """
    import networkx as nx

    cache: Dict[str, Any] = st.session_state.setdefault("graph_positions", {})
    layout_graph = nx.Graph()
    layout_graph.add_nodes_from(node["id"] for node in frame_nodes)
    layout_graph.add_edges_from((edge["source"], edge["target"]) for edge in frame_links)
    if not layout_graph:
        return {}

    def scatter_around(node_id: str, spread: float) -> tuple:
        """Deterministic jitter - the same node always lands in the same place."""
        rng = random.Random(zlib.crc32(str(node_id).encode()))
        return (rng.uniform(-spread, spread), rng.uniform(-spread, spread))

    initial: Dict[str, tuple] = {}
    fresh: List[str] = []
    for node_id in layout_graph.nodes:
        if node_id in cache:
            initial[node_id] = cache[node_id]
            continue
        placed = [
            cache[neighbour]
            for neighbour in layout_graph.neighbors(node_id)
            if neighbour in cache
        ]
        if placed:
            centre_x = sum(float(point[0]) for point in placed) / len(placed)
            centre_y = sum(float(point[1]) for point in placed) / len(placed)
            offset_x, offset_y = scatter_around(node_id, 0.35)
            initial[node_id] = (centre_x + offset_x, centre_y + offset_y)
        else:
            initial[node_id] = scatter_around(node_id, 2.0)
        fresh.append(node_id)

    if fresh:
        pinned = [node_id for node_id in layout_graph.nodes if node_id not in fresh]
        positions = nx.spring_layout(
            layout_graph,
            pos=initial,
            fixed=pinned or None,
            k=0.6,
            iterations=30,
            seed=config.RANDOM_SEED,
        )
    else:
        positions = initial
    cache.update({key: (float(value[0]), float(value[1])) for key, value in positions.items()})
    return cache


def _timeline_offsets(events: Sequence[Dict[str, Any]]) -> List[float]:
    """Stagger markers that share an instant so a fan-in reads as several events."""
    seen: Dict[str, int] = {}
    offsets: List[float] = []
    for event in events:
        key = str(event["time"])
        slot = seen.get(key, 0)
        seen[key] = slot + 1
        offsets.append(0.0 if slot == 0 else ((slot % 3) - 1) * 0.18)
    return offsets


def _timeline_figure(events: Sequence[Dict[str, Any]]) -> Any:
    """One marker per event on a time rail - the focused entity's own history."""
    import plotly.graph_objects as go

    whens = [event["when"] for event in events]
    amounts = [float(event["amount"] or 0.0) for event in events]
    biggest = max(amounts) if amounts else 0.0

    def hover_text(event: Dict[str, Any]) -> str:
        if event["amount"] is None:
            headline = f"<b>{event['direction']}</b>"
        else:
            headline = f"<b>{event['direction']}</b>  {event['amount']:.4f} BTC"
        peer = f"{event['counterparty_type']} {short_id(event['counterparty'])}"
        if event.get("method"):
            peer += f" \u00b7 matched by {event['method']}"
        stamp = str(event["time"])[:19].replace("T", " ")
        return (
            f"{headline}<br>{peer} \u00b7 risk {event['counterparty_risk']:.3f}"
            f"<br>{stamp}<br>event {event['step']} of {len(events)}"
        )

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[min(whens), max(whens)], y=[0.0, 0.0], mode="lines",
            line=dict(color="#1f2a44", width=2), hoverinfo="none", showlegend=False,
        )
    )
    figure.add_trace(
        go.Scatter(
            x=whens,
            y=_timeline_offsets(events),
            mode="markers",
            marker=dict(
                size=[
                    9 + 21 * (math.log1p(amount) / math.log1p(biggest)) if biggest > 0 else 11
                    for amount in amounts
                ],
                color=[EDGE_COLOURS.get(event["kind"], "#8fa3bf") for event in events],
                symbol=["diamond" if event["kind"] == "network" else "circle" for event in events],
                line=dict(width=1, color="#0b1220"),
            ),
            text=[hover_text(event) for event in events],
            hoverinfo="text",
            showlegend=False,
        )
    )
    figure.update_layout(
        height=180,
        margin=dict(l=0, r=0, t=8, b=0),
        paper_bgcolor="#0b1220",
        plot_bgcolor="#0b1220",
        font=dict(color="#8fa3bf", size=11),
        yaxis=dict(visible=False),
        xaxis=dict(showgrid=True, gridcolor="#1a2438", zeroline=False),
    )
    return figure


def _run_pipeline_now(input_dir: Path, label: str) -> None:
    """Run the pipeline in-process with a live stage ticker (offline, no API needed)."""
    with st.status(f"{label}: running the eight-stage pipeline...", expanded=True) as status:
        progress_bar = st.progress(0.0)
        stage_log: List[str] = []

        def progress(stage_name: str, index: int, total: int) -> None:
            stage_log.append(f"stage {index}/{total}: {stage_name}")
            progress_bar.progress(min(index / max(total, 1), 1.0))
            status.update(label=f"{label}: stage {index}/{total} - {stage_name}")

        summary = run_full_pipeline(input_dir=input_dir, output_dir=config.ARTIFACT_DIR, progress=progress)
        progress_bar.progress(1.0)
        counts = summary.get("counts", {})
        status.update(
            label=(
                f"{label} complete in {summary.get('duration_seconds', 0):.1f}s - "
                f"{counts.get('transactions', 0)} transactions, {counts.get('alerts', 0)} alerts"
            ),
            state="complete",
        )
    st.cache_data.clear()
    st.rerun()


# --------------------------------------------------------------------------- #
# sidebar
# --------------------------------------------------------------------------- #
bundle = load_bundle()
alerts: List[Dict[str, Any]] = bundle["alerts"]
summary = bundle["summary"]
graph_payload = bundle["graph"]
stats = summary.get("counts", {})
api_online = bundle["source"] == "api"

# ``/alerts`` returns trimmed summaries (no evidence, no reasons, no counterfactuals), so
# the full packages are read once from the artefacts for the whole page.
local_alerts: List[Dict[str, Any]] = load_alerts() or alerts
local_by_id: Dict[str, Any] = {entry.get("alert_id"): entry for entry in local_alerts}
chain_report = verify_ledger_chain()

with st.sidebar:
    st.markdown(f"### 🔗 ChainTrace AI\n<span class='ct-sub'>v{__version__} · offline prototype</span>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(
        f"<span class='ct-pill {'ct-safe' if api_online else 'ct-warn'}'>"
        f"{'API · live' if api_online else 'artefacts · filesystem'}</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"API: {config.API_URL}")

    st.markdown("#### Run analysis")
    source = st.radio(
        "Input data",
        ["Bundled synthetic sample", "Uploaded files (data/raw)", "Regenerate synthetic sample first"],
        index=0,
        label_visibility="collapsed",
    )
    if st.button("▶ Run Analysis", type="primary", use_container_width=True):
        if source.startswith("Regenerate"):
            generate_dataset(output_dir=config.SYNTHETIC_DIR)
            _run_pipeline_now(config.SYNTHETIC_DIR, "Regenerated sample analysis")
        elif source.startswith("Uploaded"):
            uploads = sorted(
                (path for path in config.RAW_DIR.glob("upload_*") if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
            )
            if not uploads:
                st.error("No uploaded files found. Use the uploader below first.")
            else:
                _run_pipeline_now(uploads[-1], "Uploaded-file analysis")
        else:
            _run_pipeline_now(config.SYNTHETIC_DIR, "Sample analysis")

    st.markdown("#### Upload evidence files")
    uploaded = st.file_uploader(
        "CSV / JSON / XML (transactions or network metadata)",
        type=["csv", "json", "jsonl", "xml"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )
    if uploaded and st.button("Store uploads", use_container_width=True):
        target_dir = config.RAW_DIR / f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        target_dir.mkdir(parents=True, exist_ok=True)
        for handle in uploaded:
            (target_dir / handle.name).write_bytes(handle.getvalue())
        st.success(f"Stored {len(uploaded)} file(s) in {target_dir.relative_to(PROJECT_ROOT)}")

    st.markdown("---")
    st.markdown("#### Filters")
    min_risk = st.slider("Minimum risk score", 0.0, 1.0, config.MIN_ALERT_RISK, 0.05)
    entity_filter = st.multiselect("Entity type", ["wallet", "txid"], default=["wallet", "txid"])
    search = st.text_input("Search entity or reason", "")

    st.markdown("---")
    st.caption(
        "Every alert carries a plain-English explanation and a SHA-256 evidence hash. "
        "Nothing in this console talks to the internet."
    )

filtered = [
    alert for alert in alerts
    if alert["risk_score"] >= min_risk
    and (not entity_filter or alert["entity_type"] in entity_filter)
    and (
        not search
        or search.lower() in alert["entity"].lower()
        or any(search.lower() in reason.lower() for reason in alert.get("reasons", []))
    )
]

# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
st.markdown("# ChainTrace AI")
st.markdown(
    "<div class='ct-sub'>Offline correlation of Bitcoin blockchain records with network metadata · "
    "four ML analyses · explainable, hash-stamped alerts</div>",
    unsafe_allow_html=True,
)
st.write("")

overview_tab, graph_tab, infra_tab, evidence_tab = st.tabs(
    [
        "🎯 Ranked Alerts",
        "🕸 Interactive Graph",
        "🌐 Infrastructure",
        "🔒 Evidence & Integrity",
    ]
)

# --------------------------------------------------------------------------- #
# tab 1 - ranked alerts
# --------------------------------------------------------------------------- #
with overview_tab:
    counts = bundle["stats"]["counts"] if bundle["stats"] else stats
    alert_stats = (
        bundle["stats"]["alerts"] if bundle["stats"]
        else {
            "total": len(alerts),
            "above_0_5": sum(1 for a in alerts if a["risk_score"] >= 0.5),
            "above_0_7": sum(1 for a in alerts if a["risk_score"] >= 0.7),
        }
    )
    highest = max((a["risk_score"] for a in alerts), default=0.0)

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Records processed", f"{counts.get('transactions', 0):,}")
    col2.metric("Network records", f"{counts.get('network_records', 0):,}")
    col3.metric("Ranked alerts", f"{len(alerts)}")
    col4.metric("Highest risk", f"{highest:.2f}")
    col5.metric("Entities clustered", f"{counts.get('clusters', 0)}")

    col6, col7, col8, col9, col10 = st.columns(5)
    col6.metric("Peeling chains", counts.get("peel_chains", 0))
    col7.metric("Mixing txs", counts.get("mixing_transactions", 0))
    col8.metric("Anomalies flagged", counts.get("anomalies_flagged", 0))
    col9.metric("Evidence hashes", len(alerts))
    col10.metric("Pipeline time", f"{summary.get('duration_seconds', 0):.1f}s")

    with st.expander("📦 Take the bundled sample dataset with you", expanded=False):
        st.caption(
            f"The exact dataset this page analysed - deterministic from seed {config.RANDOM_SEED}, "
            f"covering {config.DATASET_WINDOW_START:%d %b %Y} → {config.DATASET_WINDOW_END:%d %b %Y}. "
            "Every number on these four tabs reproduces from these files with "
            "`python scripts/run_full_pipeline.py`. It is synthetic throughout: no real address, "
            "wallet or IP belongs to anybody."
        )
        dataset_entries = dataset_files()
        if not dataset_entries:
            st.info(
                "The dataset is generated on first run. Use **Run analysis** in the sidebar with "
                "*Regenerate synthetic sample first* and it will appear here."
            )
        else:
            fingerprint = _synthetic_fingerprint()
            for entry in dataset_entries:
                text_column, button_column = st.columns([4, 1])
                with text_column:
                    rows = f" · {entry['rows']:,} rows" if entry["rows"] is not None else ""
                    st.markdown(f"**{entry['name']}**{rows}")
                    st.caption(entry["description"])
                with button_column:
                    st.download_button(
                        "⬇ Download",
                        data=_dataset_bytes(str(entry["path"]), entry["path"].stat().st_mtime),
                        file_name=entry["name"],
                        mime=entry["mime"],
                        key=f"dl_dataset_{entry['name']}",
                        use_container_width=True,
                    )
            st.download_button(
                "⬇ Everything as one zip — the dataset plus the offline GeoIP table",
                data=_dataset_zip(fingerprint),
                file_name="chaintrace_sample_dataset.zip",
                mime="application/zip",
                key="dl_dataset_zip",
                use_container_width=True,
            )

    st.markdown("---")
    if not filtered:
        st.info("No alerts match the current filters. Lower the minimum risk score or run the analysis.")
    else:
        table = pd.DataFrame(
            [
                {
                    "Alert": alert["alert_id"],
                    "Entity": alert["entity"] if alert["entity_type"] == "wallet" else f"{alert['entity'][:16]}…",
                    "Type": alert["entity_type"],
                    "Risk": round(alert["risk_score"], 3),
                    "Confidence": round(alert["confidence"], 3),
                    "Top reason": (alert.get("reasons") or [""])[0],
                    "Action": alert.get("recommended_action", ""),
                }
                for alert in filtered
            ]
        )
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            height=430,
            column_config={
                "Risk": st.column_config.ProgressColumn(
                    "Risk", min_value=0.0, max_value=1.0, format="%.3f"
                ),
                "Confidence": st.column_config.NumberColumn("Confidence", format="%.3f"),
            },
        )

        st.markdown("### Alert detail")
        labels = [
            f"{alert['alert_id']} · {alert['entity_type']} · risk {alert['risk_score']:.2f} · {alert['entity'][:26]}"
            for alert in filtered
        ]
        selected_label = st.selectbox("Select an alert to open the evidence", labels, index=0)
        selected = filtered[labels.index(selected_label)]
        # ``/alerts`` returns trimmed summaries; the detail card needs the full package
        # (reasons, features, counterfactuals).  Swap in the local full alert.
        selected = local_by_id.get(selected.get("alert_id"), selected)

        left, right = st.columns([3, 2])
        with left:
            st.markdown(
                f"<div class='ct-card'><span class='ct-pill {risk_class(selected['risk_score'])}'>"
                f"risk {selected['risk_score']:.2f}</span> "
                f"<span class='ct-pill ct-warn'>confidence {selected['confidence']:.2f}</span><br><br>"
                f"<b>{selected['entity_type'].upper()}</b><br><code>{selected['entity']}</code><br><br>"
                f"<b>Recommended action:</b> {selected.get('recommended_action', '-')}</div>",
                unsafe_allow_html=True,
            )
            st.markdown("**Why this was flagged**")
            for reason in selected.get("reasons", []):
                st.markdown(f"- {reason}")

            explanation_method = (selected.get("evidence") or {}).get("explanation_method", "")
            if explanation_method:
                st.caption(f"Explanation method: {explanation_method}")

        with right:
            components = selected.get("components") or {}
            if components:
                st.markdown("**Evidence strength by detector**")
                component_frame = pd.DataFrame(
                    {"detector": list(components.keys()), "score": list(components.values())}
                ).set_index("detector")
                st.bar_chart(component_frame, height=230)

            features = selected.get("top_features") or []
            if features:
                st.markdown("**Feature contributions**")
                feature_frame = pd.DataFrame(features).set_index("feature")
                if "contribution" in feature_frame.columns:
                    st.bar_chart(feature_frame[["contribution"]], height=230)
                st.dataframe(
                    pd.DataFrame(features), hide_index=True, use_container_width=True,
                    column_config={"value": st.column_config.NumberColumn("value", format="%.6g")},
                )

        # ---------------------------------------------------------------- #
        # counterfactual: what would clear this entity?
        # ---------------------------------------------------------------- #
        counterfactuals = selected.get("counterfactuals") or []
        st.markdown("### 🔄 What would clear this entity?")
        if not counterfactuals:
            st.caption(
                "Counterfactuals appear after an analysis run: each one removes a single piece "
                "of evidence and re-scores the alert."
            )
        else:
            st.caption(
                selected.get("counterfactual_summary")
                or "Each row removes one piece of evidence and asks: would this still be an alert?"
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Remove this evidence": row.get("label", row.get("evidence")),
                            "Score would be": round(float(row.get("score_without") or 0.0), 3),
                            "Drop": round(float(row.get("score_drop") or 0.0), 3),
                            "Still an alert?": "yes" if row.get("still_flagged") else "no",
                            "Decisive?": "yes" if row.get("decisive") else "no",
                        }
                        for row in counterfactuals
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
            decisive = [row for row in counterfactuals if row.get("decisive")]
            if decisive:
                st.warning(
                    "To clear this entity, disprove: "
                    + "; ".join(str(row.get("label", row.get("evidence"))) for row in decisive)
                )
            else:
                st.info(
                    "No single piece of evidence decides this alert - two or more independent "
                    "signals have to fall away before the score drops below the threshold."
                )

        with st.expander("Pipeline stage timings"):
            stage_frame = pd.DataFrame(summary.get("stages", []))
            if not stage_frame.empty:
                st.dataframe(
                    stage_frame[["stage", "name", "seconds"]],
                    hide_index=True, use_container_width=True,
                )
            st.caption(
                "Stage owner mapping: 1-3 Data (Person E), 4 Data & Graph (Person E), "
                "5-6 AI/ML (Persons A & B), 7 Dashboard (Person C), 8 Integration (Person F)."
            )

        case_file = build_case_file(selected, graph_payload, chain_report)
        dl_csv, dl_html, dl_md = st.columns(3)
        with dl_csv:
            st.download_button(
                "⬇ Alerts CSV",
                data=table.to_csv(index=False).encode("utf-8"),
                file_name="chaintrace_alerts.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_alerts_csv",
            )
        with dl_html:
            st.download_button(
                "⬇ Case file (HTML)",
                data=case_file["html"],
                file_name=f"{case_file['filename']}.html",
                mime="text/html",
                use_container_width=True,
                key="dl_case_file_html_alerts",
                help="One self-contained dossier for the selected alert: why it was flagged, "
                     "what would clear it, the graph picture, the network evidence and the hashes.",
            )
        with dl_md:
            st.download_button(
                "⬇ Case file (Markdown)",
                data=case_file["markdown"],
                file_name=f"{case_file['filename']}.md",
                mime="text/markdown",
                use_container_width=True,
                key="dl_case_file_md_alerts",
            )

# --------------------------------------------------------------------------- #
# tab 2 - interactive graph
# --------------------------------------------------------------------------- #
with graph_tab:
    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    graph_stats = graph_payload.get("stats", {})

    if not nodes:
        st.info("No graph artefact yet - run the analysis from the sidebar.")
    else:
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("Nodes", graph_stats.get("nodes", len(nodes)))
        g2.metric("Edges", graph_stats.get("edges", len(edges)))
        g3.metric("Wallets", graph_stats.get("wallets", 0))
        g4.metric("Seeds", graph_stats.get("seed_wallets", 0))

        nodes_by_id = index_nodes(nodes)
        risk_by_id = {
            node_id: float(node.get("risk_score") or 0.0) for node_id, node in nodes_by_id.items()
        }
        degree: Dict[str, int] = {}
        for edge in edges:
            degree[edge["source"]] = degree.get(edge["source"], 0) + 1
            degree[edge["target"]] = degree.get(edge["target"], 0) + 1

        controls = st.columns([2, 2, 2, 2])
        with controls[0]:
            colour_mode = st.selectbox("Colour nodes by", ["Risk score", "Ownership cluster", "Node type"])
        with controls[1]:
            max_nodes = st.slider("Max nodes drawn", 50, 1200, 250, 50)
        with controls[2]:
            min_node_risk = st.slider("Hide nodes below risk", 0.0, 1.0, 0.0, 0.05)
        with controls[3]:
            node_search = st.text_input("Find a node (address or txid)", "", key="node_search_box")

        # ------------------------------------------------------------------ #
        # focus mode: the ladder the investigator walks with clicks
        # ------------------------------------------------------------------ #
        # In replay mode the chart is a time scrubber and its figure carries no ``on_select``,
        # so inviting a tap here would advertise something the picture cannot do.  The replay's
        # own caption says what to do instead.
        if not st.session_state.get("timeline_replay"):
            st.caption(
                "**Click any node to focus it.**  That node and its **direct** connections stay lit "
                "behind a coloured halo while everything else fades - a connection you can point at "
                "is evidence, a two-hop hunch is not.  Click a lit neighbour to walk one hop further "
                "and the route you walked is drawn as a dotted amber line with the amounts on it, "
                "then **🔄 Reset the graph** clears it all.  If a click ever looks lost, click again "
                "or hop from the list below: *Hop to a connected node*, the *Direct connections* "
                "table and *Find a node* always work, whatever the chart does."
            )
        focus_path = [
            str(node_id) for node_id in st.session_state.get("graph_focus", [])
            if str(node_id) in nodes_by_id
        ]
        st.session_state["graph_focus"] = focus_path
        focus = focus_view(focus_path, edges) if focus_path else None
        head_id = focus_path[-1] if focus_path else None

        # ------------------------------------------------------------------ #
        # Moving the focus is always done through the two helpers below, never by
        # hand: the chart keeps its own selection in Streamlit's widget state, so
        # changing the focus in Python without discarding that selection lets the
        # *old* click be re-read on the very next rerun.  That is what made
        # "clear focus" look broken - the node came straight back.
        # ------------------------------------------------------------------ #
        def move_focus(target: str, *, fresh_chart: bool = True) -> None:
            """Walk the ladder to ``target``, and (unless a chart click did it) drop the
            chart's stale selection by moving it to a brand-new widget key."""
            st.session_state["graph_focus"] = advance_path(
                st.session_state.get("graph_focus", []), target
            )
            st.session_state["graph_last_click"] = target
            st.session_state["graph_last_click_epoch"] = int(
                st.session_state.get("graph_epoch", 0)
            )
            if fresh_chart:
                st.session_state["graph_epoch"] = int(st.session_state.get("graph_epoch", 0)) + 1

        def hop_to_selected_node() -> None:
            """Selectbox callback: jump the focus to the node picked in the list."""
            chosen = st.session_state.get("focus_hop") or ""
            if not chosen:
                return
            move_focus(chosen)
            st.session_state["focus_hop"] = ""

        def back_one_hop() -> None:
            """Step one rung back down the ladder."""
            path = list(st.session_state.get("graph_focus", []))
            if len(path) < 2:
                return
            # Remember the node we are leaving as "already handled", otherwise its still-live
            # chart selection would put it straight back on the path.  The epoch is bumped too,
            # so this block applies to the chart we are walking away from - clicking the same
            # node on the *new* chart is an honest click and focuses it again.
            st.session_state["graph_last_click"] = path[-1]
            st.session_state["graph_focus"] = path[:-1]
            st.session_state["graph_epoch"] = int(st.session_state.get("graph_epoch", 0)) + 1

        def reset_focus() -> None:
            """Clear the focus completely and hand back a graph with nothing selected."""
            path = list(st.session_state.get("graph_focus", []))
            st.session_state["graph_focus"] = []
            st.session_state["graph_last_click"] = path[-1] if path else None
            st.session_state["graph_epoch"] = int(st.session_state.get("graph_epoch", 0)) + 1
            st.session_state["node_search_box"] = ""
            st.session_state.pop("focus_row_seen", None)

        # When each node first appears - the timeline replay is built on this.
        first_seen = first_seen_by_edge(edges)
        ticks = sorted(set(first_seen.values()))

        # The replay is a *mode*, not a second control to keep in sync with the picture.
        # Switching it on hands the chart over to its own timeline slider (`_replay_figure`):
        # the snapshots are frames inside the figure, so dragging is a client-side animation -
        # no rerun, no chart re-render, nothing else on the page touched.  The first cut of this
        # screen had a Streamlit slider plus an auto-play loop; every step of a drag reran the
        # script and re-rendered the whole chart, which on a few hundred nodes read as the page
        # reloading, and the loop tore through 29 days in three seconds.
        replay = st.toggle(
            "⏱ Timeline replay",
            value=False,
            # read by the focus caption above, which must not invite a tap the replay
            # figure cannot honour; an explicit key is what makes that read possible
            key="timeline_replay",
            help=(
                "Scrub the collection window by hand, in place: the snapshots are frames inside "
                "the chart with a slider underneath them, so dragging it never reloads the page - "
                "the graph grows from the first record to the last as you drag.  The replay always "
                "draws the whole window: switch it off to walk one entity's neighbourhood."
            ),
        )

        nav = st.columns([4, 1.4, 1.4])
        with nav[0]:
            if replay:
                st.markdown(
                    "**⏱ Replay mode** - the picture below is the whole collection window over "
                    "time."
                    + (
                        f"  The focus on `{head_id}` is paused until you switch the toggle off."
                        if head_id else ""
                    )
                )
            elif head_id:
                head_node = nodes_by_id[head_id]
                st.markdown(
                    f"**🔎 In focus:** `{head_id}` - {head_node.get('node_type')}, risk "
                    f"{risk_by_id.get(head_id, 0.0):.3f}, {len(focus['neighbours'])} direct "
                    "connection(s)"
                )
                if len(focus_path) > 1:
                    # the dotted amber line on the chart, with the money that moved on it: the
                    # route is a value trail, not just a list of ids you happened to click
                    st.caption(f"Route walked (money trail): {route_summary(focus_path, edges)}")
            else:
                st.markdown("**🔎 Nothing in focus** - the whole graph is drawn at one intensity.")
        with nav[1]:
            st.button(
                "⬅ Back one hop",
                key="btn_focus_back",
                disabled=len(focus_path) < 2,
                on_click=back_one_hop,
                help="Step back one rung of the walk you made.",
                use_container_width=True,
            )
        with nav[2]:
            # Deliberately never disabled: pressing it with nothing in focus still clears any
            # selection the chart is holding on to, which is exactly what an investigator wants
            # when a click appears to have "stuck".
            st.button(
                "🔄 Reset the graph",
                key="btn_focus_clear",
                type="primary",
                on_click=reset_focus,
                help=(
                    "Clear the focus entirely: nothing selected, the whole graph at one "
                    "intensity, search box emptied. Safe to press at any time."
                ),
                use_container_width=True,
            )

        if focus_path:
            def hop_label(node_id: str) -> str:
                if not node_id:
                    return "→ hop to a connected node"
                candidate = nodes_by_id.get(node_id, {})
                tag = ""
                if candidate.get("is_seed"):
                    tag = " · SEED"
                elif candidate.get("is_mixing"):
                    tag = " · mixing"
                return (
                    f"{candidate.get('node_type', '?')} · {short_id(node_id)} · "
                    f"risk {risk_by_id.get(node_id, 0.0):.3f}{tag}"
                )

            st.selectbox(
                "Hop to a connected node",
                [""] + sorted(
                    focus["neighbours"],
                    key=lambda node_id: (-risk_by_id.get(node_id, 0.0), node_id),
                ),
                format_func=hop_label,
                key="focus_hop",
                on_change=hop_to_selected_node,
                help="The same walk as clicking a lit node - for people who prefer a list.",
            )

        focus_ids = set(focus["nodes"]) if focus else set()

        def window(
            focus_ids: Any = frozenset()
        ) -> tuple:
            """Pick the nodes/edges to draw and lay them out.

            The timeline replay does **not** come through here any more: it is scrubbed inside
            the chart (:func:`_replay_figure`), so what this returns is always the whole window.
            """
            # Once something is in focus, a search term has done its job - it chose the
            # focus - so it must stop narrowing the frame.  Otherwise the faded context
            # that gives the focus its meaning would not be drawn at all.
            query = "" if focus_ids else node_search
            candidates = [
                node for node in nodes
                if float(node.get("risk_score") or 0.0) >= min_node_risk
                and (not query or query.lower() in str(node["id"]).lower())
            ]
            candidates.sort(
                key=lambda node: (float(node.get("risk_score") or 0.0), degree.get(node["id"], 0)),
                reverse=True,
            )
            keep = {node["id"] for node in candidates[:max_nodes]}

            # always keep the neighbourhood of the searched node
            if query:
                frontier = {node["id"] for node in nodes if query.lower() in str(node["id"]).lower()}
                for _ in range(2):
                    next_frontier: set = set()
                    for edge in edges:
                        if edge["source"] in frontier:
                            next_frontier.add(edge["target"])
                        if edge["target"] in frontier:
                            next_frontier.add(edge["source"])
                    frontier |= next_frontier
                keep |= frontier

            # The focused node and its direct connections are always drawn, whatever the
            # node budget or the risk filter say - a fade needs something to light up.
            if focus_ids:
                keep |= {node_id for node_id in focus_ids}

            frame_nodes = [node for node in nodes if node["id"] in keep]
            frame_ids = {node["id"] for node in frame_nodes}
            frame_links = [edge for edge in edges if edge["source"] in frame_ids and edge["target"] in frame_ids]
            # The alerts that are actually on screen - a caption reads this, and it is the
            # honest number: the node budget can leave a flagged entity out of the picture.
            frame_alerts = [alert for alert in alerts if alert["entity"] in frame_ids]
            if not frame_nodes:
                return [], [], {}, []

            return frame_nodes, frame_links, _stable_layout(frame_nodes, frame_links), frame_alerts

        drawn, drawn_edges, positions, visible_alerts = window(focus_ids)
        if not drawn:
            st.warning("No nodes match the filters.")
        elif replay and ticks:
            # ---------------------------------------------------------- #
            # replay mode: the timeline is scrubbed *inside* the picture
            # ---------------------------------------------------------- #
            snapshots = _replay_snapshots(ticks, drawn, drawn_edges, first_seen, alerts)
            if not snapshots:
                st.warning("Nothing has a timestamp to replay yet.")
            else:
                st.caption(
                    "**Drag the timeline slider under the picture** - left is the first record of "
                    "the window, right is everything as finally collected.  The snapshots are "
                    "frames inside the chart, so scrubbing happens in place: no rerun, no reload, "
                    "and the graph grows under your cursor.  The card in the corner names the "
                    f"moment.  {len(snapshots)} steps over {len(ticks):,} dated events; the "
                    "replay always draws the whole window, so switch the toggle off to walk one "
                    "entity's neighbourhood again."
                )
                st.plotly_chart(
                    _replay_figure(
                        snapshots, drawn, drawn_edges, positions, degree, colour_mode
                    ),
                    use_container_width=True,
                    # no ``on_select``: in replay mode the picture is a time scrubber, and a
                    # tap that silently did nothing would be worse than no tap at all
                    key="ct_graph_replay",
                )
        else:
            # "scroll to zoom" would be a lie here: Streamlit leaves the wheel to the page on 2D
            # charts (`scrollZoom` is only on for 3d/geo/mapbox), so zoom lives in the toolbar.
            st.caption(
                f"**{len(drawn):,} of {len(nodes):,} nodes drawn** · {len(visible_alerts)} of "
                f"{len(alerts)} alerts are on screen · drag to pan · the toolbar (top-right) "
                "zooms and a double-click resets the view · **click any node to focus it** (a "
                "faded node moves the focus there too) · seeds are the known-bad wallets risk "
                "propagates from."
            )
            st.caption(
                "Coloured lines are direct connections and their **arrowheads point the way "
                "the money moved**: blue *wallet → transaction* (that wallet spent in), orange "
                "*transaction → wallet* (the transaction paid that wallet), purple *IP → "
                "transaction* (a matched packet, no coin).  Press **▶ Animate the money flow** "
                "(bottom-right of the picture, clear of the zoom/pan toolbar) and a coin rides "
                "every lit hop from the sender to the receiver, sized by the amount so the big "
                "transfer is obvious."
            )
            event = st.plotly_chart(
                _network_figure(drawn, drawn_edges, positions, degree, colour_mode, focus),
                use_container_width=True,
                # the key carries an epoch so resetting/back-stepping can hand back a chart
                # with nothing selected (see move_focus)
                key=f"ct_graph_{int(st.session_state.get('graph_epoch', 0))}",
                on_select="rerun",
                # points = tap a node, box/lasso = circle a group and open its riskiest
                # member.  All three are offered because Streamlit's plotly selection is
                # version-dependent; the list and table below always work.
                selection_mode=("points", "box", "lasso"),
            )

            # -------------------------------------------------------------- #
            # tap to focus: the click becomes the next rung of the ladder
            # -------------------------------------------------------------- #
            chosen = _selected_node_id(event, risk_by_id)
            if not chosen and node_search:
                match = next(
                    (node for node in nodes if node_search.lower() in str(node["id"]).lower()),
                    None,
                )
                chosen = match["id"] if match else None
            # A selection only counts if it is new, or if it comes from a chart instance we have
            # not acted on yet (see the epoch bookkeeping in move_focus / back_one_hop).
            already_handled = (
                chosen == st.session_state.get("graph_last_click")
                and st.session_state.get("graph_last_click_epoch")
                == st.session_state.get("graph_epoch", 0)
            )
            if FOCUS_DEBUG:
                # One line per rerun, last eight kept: `sel=-` means the browser sent no
                # point selection at all (the tap never reached Streamlit), while
                # `handled=True` on a *different* node id would mean the focus maths went
                # wrong.  This is the difference between "the user missed the dot" and
                # "the click was dropped", which no single screenshot can tell you.
                last_click = st.session_state.get("graph_last_click")
                log = st.session_state.setdefault("graph_debug", [])
                log.append(
                    f"sel={short_id(chosen) if chosen else '-'}"
                    f" last={short_id(last_click) if last_click else '-'}"
                    f" epoch={st.session_state.get('graph_epoch', 0)}"
                    f" last_epoch={st.session_state.get('graph_last_click_epoch', 0)}"
                    f" handled={int(bool(already_handled))}"
                    f" path={len(st.session_state.get('graph_focus', []))}"
                )
                del log[:-8]
                st.caption("🐞 tap log · " + "  ·  ".join(log))
            if chosen and not already_handled:
                move_focus(chosen, fresh_chart=False)
                st.rerun()

            # -------------------------------------------------------------- #
            # focus panel: what this entity is, who it touched, and when
            # -------------------------------------------------------------- #
            focus_path = [
                str(node_id) for node_id in st.session_state.get("graph_focus", [])
                if str(node_id) in nodes_by_id
            ]
            head_id = focus_path[-1] if focus_path else None
            if head_id:
                head_node = nodes_by_id[head_id]
                view = focus_view(focus_path, edges)
                events = node_events(head_id, nodes_by_id, edges)
                figures = timeline_stats(head_node, events)
                risk = risk_by_id.get(head_id, 0.0)
                st.markdown("---")
                st.markdown(
                    f"#### 🔎 Focus · `{head_id}` "
                    f"<span class='ct-pill {risk_class(risk)}'>{head_node.get('node_type')}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(
                    "Everything below describes **this one entity** and nothing else: what it is, "
                    "its direct connections, and its own timeline."
                )

                tiles: List[tuple] = [
                    ("Risk score", f"{risk:.3f}"),
                    ("Direct connections", f"{len(view['neighbours'])}"),
                    ("Events", f"{figures['events']}"),
                ]
                if head_node.get("node_type") == "wallet":
                    tiles += [
                        ("Received", format_btc(figures["received"])),
                        ("Spent", format_btc(figures["sent"])),
                    ]
                elif head_node.get("node_type") == "txid":
                    tiles += [
                        ("Value in", format_btc(head_node.get("total_in"))),
                        ("Value out", format_btc(head_node.get("total_out"))),
                    ]
                else:
                    tiles += [
                        ("Country", str(head_node.get("geo_country") or "-")),
                        ("Correlated events", f"{figures['network_events']}"),
                    ]
                tiles += [
                    (
                        "Active window",
                        f"{figures['span_hours']:.1f} h" if figures["span_hours"] else "-",
                    ),
                    ("Counterparties ≥ 0.5 risk", f"{figures['flagged_counterparties']}"),
                ]
                for column, (label, value) in zip(st.columns(len(tiles)), tiles):
                    column.metric(label, value)

                # ---------------------------------------------------------- #
                # money flow: who sent what to whom
                # ---------------------------------------------------------- #
                flow = flow_rows(head_id, nodes_by_id, edges)
                st.markdown("**💸 Who sent what to whom**")
                if not flow["rows"]:
                    st.info("No money movements recorded for this entity in the frame.")
                else:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "From (sent)": row["from_label"],
                                    "To (received)": row["to_label"],
                                    "Amount (BTC)": (
                                        round(row["amount"], 8)
                                        if row["amount"] is not None else None
                                    ),
                                    "What it means": row["meaning"],
                                    "When": str(row["time"] or "")[:19].replace("T", " "),
                                }
                                for row in flow["rows"]
                            ]
                        ),
                        hide_index=True, use_container_width=True,
                        height=min(320, 40 + 35 * len(flow["rows"])),
                    )
                    if head_node.get("node_type") == "txid":
                        # value in and value out are not equal and never should be: the gap is
                        # the miner fee.  Saying so stops the difference reading as a bug.
                        gap = flow["total_sent"] - flow["total_received"]
                        fee_note = (
                            f" · difference {format_btc(gap)} is the miner fee"
                            if gap > 0.00000001 else ""
                        )
                        st.caption(
                            f"{flow['senders']} sender(s) → {flow['receivers']} receiver(s) · "
                            f"{format_btc(flow['total_sent'])} sent in · "
                            f"{format_btc(flow['total_received'])} paid out{fee_note}."
                        )
                        if flow["resolution"] == "exact" and flow["rows"]:
                            sender = next(
                                (row for row in flow["rows"] if row["direction"] == "spend"), None
                            )
                            payees = [row for row in flow["rows"] if row["direction"] == "payout"]
                            if sender and payees:
                                payments = ", ".join(
                                    f"`{short_id(row['to'])}` got "
                                    f"{format_btc(row['amount'])}" for row in payees
                                )
                                st.caption(
                                    f"One sender, so this is exact: `{short_id(sender['from'])}` "
                                    f"sent {format_btc(sender['amount'])} in, and the "
                                    f"transaction paid out — {payments}."
                                )
                    st.caption(flow["note"])

                left, right = st.columns([1, 1])
                with left:
                    st.markdown("**What this entity is**")
                    st.dataframe(
                        pd.DataFrame(node_facts(head_node), columns=["Attribute", "Value"]),
                        hide_index=True, use_container_width=True,
                    )
                    related = [
                        alert for alert in alerts
                        if alert["entity"] == head_id
                        or head_id in json.dumps(alert.get("evidence", {}))
                    ]
                    if related:
                        st.markdown("**Alerts that mention it**")
                        for alert in related[:5]:
                            st.markdown(
                                f"- `{alert['alert_id']}` · risk {alert['risk_score']:.2f} · "
                                f"{(alert.get('reasons') or [''])[0]}"
                            )
                with right:
                    st.markdown(f"**Direct connections ({len(events)})**")
                    if not events:
                        st.info("This node has no dated connections in the frame.")
                    else:
                        # Clicking a row walks to that counterparty - the same hop as tapping
                        # the node, and the one route that never depends on chart hit-testing.
                        connection_table = st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "Step": event["step"],
                                        "When": str(event["time"])[:19].replace("T", " "),
                                        "What happened": event["direction"],
                                        "Amount (BTC)": (
                                            round(event["amount"], 8)
                                            if event["amount"] is not None else None
                                        ),
                                        "Counterparty": short_id(event["counterparty"]),
                                        "Type": event["counterparty_type"],
                                        "Their risk": round(event["counterparty_risk"], 3),
                                        "Flag": (
                                            "SEED" if event["counterparty_is_seed"]
                                            else ("mixing" if event["counterparty_flagged"] else "")
                                        ),
                                    }
                                    for event in events
                                ]
                            ),
                            hide_index=True, use_container_width=True,
                            height=min(430, 40 + 35 * len(events)),
                            on_select="rerun", selection_mode="single-row",
                            key="focus_connections",
                        )
                        st.caption("Click a row to walk to that counterparty.")
                        try:
                            rows = list(connection_table.selection.rows)
                        except Exception:
                            rows = []
                        if rows:
                            row_key = (head_id, int(rows[0]))
                            if row_key != st.session_state.get("focus_row_seen"):
                                st.session_state["focus_row_seen"] = row_key
                                move_focus(events[int(rows[0])]["counterparty"])
                                st.rerun()

                if events:
                    st.markdown("**🕒 Timeline of this entity**")
                    st.caption(
                        "One dot per connection on a time rail: blue = money received, orange = "
                        "money spent or paid out, purple diamond = a matched network packet. Dot "
                        "size follows the amount, so the big move is the obvious one."
                    )
                    st.plotly_chart(
                        _timeline_figure(events),
                        use_container_width=True,
                        key="ct_focus_timeline",
                    )
                st.caption(
                    "Press **⬅ Back one hop** to step back down the ladder you walked, or "
                    "**🔄 Reset the graph** (above the chart) to clear the focus completely and "
                    "see the whole graph at one intensity again."
                )

            export_nodes, export_edges = drawn, drawn_edges
            export_name = "chaintrace_graph.html"
            export_label = "⬇ Download this graph as an interactive HTML file"
            if head_id:
                view = focus_view(st.session_state.get("graph_focus", []), edges)
                export_nodes = [node for node in nodes if node["id"] in view["nodes"]]
                export_edges = [
                    edge for edge in edges
                    if tuple(sorted((edge["source"], edge["target"]))) in view["edges"]
                ]
                export_name = f"chaintrace_focus_{head_id[:12]}.html"
                export_label = "⬇ Download the focused neighbourhood as an interactive HTML file"
            st.download_button(
                export_label,
                data=_interactive_html(export_nodes, export_edges, risk_by_id),
                file_name=export_name,
                mime="text/html",
                use_container_width=False,
                key="dl_graph_html",
            )

# --------------------------------------------------------------------------- #
# tab 3 - infrastructure roll-up (E)
# --------------------------------------------------------------------------- #
with infra_tab:
    st.markdown("### 🌐 Infrastructure - where should the investigation go next?")
    st.caption(
        "Every alert that carries network evidence is grouped by the provider (ASN) and country "
        "behind the traffic. This is the \"what next\" view: the wallets move, the infrastructure "
        "stays put."
    )
    if not alerts:
        st.info("No alerts yet - run the analysis from the sidebar.")
    else:
        infra = _api_get("/infrastructure")
        if not infra:
            correlated_rows: List[Dict[str, Any]] = []
            if config.CORRELATED_PATH.exists():
                try:
                    correlated_rows = (
                        pd.read_csv(config.CORRELATED_PATH).fillna("").to_dict(orient="records")
                    )
                except Exception:
                    correlated_rows = []
            infra = rollup(local_alerts, correlated_rows)
            infra["focus"] = focus_list(infra)

        totals = infra.get("totals", {})
        i1, i2, i3, i4 = st.columns(4)
        i1.metric("Alerts", totals.get("alerts", len(alerts)))
        i2.metric("With network evidence", totals.get("alerts_with_network_evidence", 0))
        i3.metric("Distinct providers (ASN)", totals.get("distinct_asns", 0))
        i4.metric("Distinct countries", totals.get("distinct_countries", 0))
        st.info(infra.get("headline", ""))

        provider_rows = infra.get("providers", [])[:12]
        country_rows = infra.get("countries", [])[:12]
        if provider_rows or country_rows:
            left, right = st.columns(2)
            with left:
                st.markdown("**Providers (ASN) behind flagged activity**")
                if provider_rows:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "ASN": row["key"],
                                    "Operator": row["label"],
                                    "Alerts": row["alerts"],
                                    "Max risk": round(float(row.get("max_risk") or 0.0), 3),
                                    "Countries": ", ".join(row.get("countries") or []) or "-",
                                }
                                for row in provider_rows
                            ]
                        ),
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.caption(
                        "No alert carries network evidence in this run - correlate the packet "
                        "records first."
                    )
            with right:
                st.markdown("**Countries in the flagged traffic**")
                if country_rows:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Country": row["key"],
                                    "Alerts": row["alerts"],
                                    "Providers": row.get("providers", 0),
                                    "Max risk": round(float(row.get("max_risk") or 0.0), 3),
                                }
                                for row in country_rows
                            ]
                        ),
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.caption("No country data in this run.")

        focus_rows = infra.get("focus") or []
        if focus_rows:
            st.markdown("**Suggested next steps**")
            for row in focus_rows:
                st.markdown(
                    f"{row['rank']}. **{row['provider']}** (`{row['asn']}`, {row['countries']}) - "
                    f"{row['alerts']} linked alerts, max risk {float(row['max_risk']):.2f}. "
                    f"{row['action']}."
                )
        st.download_button(
            "⬇ Download this roll-up (JSON)",
            data=json.dumps(infra, indent=2, default=str),
            file_name="chaintrace_infrastructure.json",
            mime="application/json",
            use_container_width=False,
            key="dl_infrastructure_json",
        )

# --------------------------------------------------------------------------- #
# tab 4 - evidence & integrity
# --------------------------------------------------------------------------- #
with evidence_tab:
    st.markdown("### Evidence package & integrity")
    if not alerts:
        st.info("No alerts yet - run the analysis from the sidebar.")
    else:
        # ``/alerts`` returns trimmed summaries (enough for the ranked table but without
        # ``evidence`` / ``reasons``).  Hashing and the evidence package need the complete
        # alert, so the full local packages are used - the dashboard and the API share one
        # data directory inside the container.
        full_alerts = local_alerts

        labels = [
            f"{alert['alert_id']} · {alert['entity_type']} · risk {alert['risk_score']:.2f}"
            for alert in alerts
        ]
        choice = st.selectbox("Alert", labels, index=0, key="evidence_alert")
        selected_summary = alerts[labels.index(choice)]
        alert = local_by_id.get(selected_summary.get("alert_id"), selected_summary)

        left, right = st.columns([3, 2])
        with left:
            st.markdown(
                f"<div class='ct-card'><b>Entity</b><br><code>{alert['entity']}</code><br><br>"
                f"<b>Risk {alert['risk_score']:.3f}</b> · confidence {alert['confidence']:.3f}<br>"
                f"<b>SHA-256</b><br><code>{alert.get('evidence_hash')}</code></div>",
                unsafe_allow_html=True,
            )
            st.markdown("**Reasons**")
            for reason in alert.get("reasons", []):
                st.markdown(f"- {reason}")

            st.markdown("**Evidence package**")
            st.json(alert.get("evidence", {}))

        with right:
            st.markdown("**Integrity checks**")
            if st.button("🔍 Recompute SHA-256 and verify", type="primary", use_container_width=True):
                from src.utils.hashing import verify_evidence_hash

                ok, computed = verify_evidence_hash(alert)
                if ok:
                    st.success("Hash verified - the evidence has not been altered.")
                else:
                    st.error("Hash mismatch - the evidence package changed after it was sealed.")
                st.code(f"stored   : {alert.get('evidence_hash')}\ncomputed : {computed}", language="text")

            ledger_report = verify_ledger(full_alerts or None)
            st.markdown(
                f"<div class='ct-card'><b>Ledger status</b><br>"
                f"entries: {ledger_report['ledger_entries']}<br>"
                f"verified: {ledger_report['verified']}<br>"
                f"mismatched: {ledger_report['mismatched']}<br>"
                f"missing: {ledger_report['missing_from_ledger']}<br>"
                f"<b>{'✔ ledger consistent' if ledger_report['ok'] else '⚠ ledger needs attention'}</b></div>",
                unsafe_allow_html=True,
            )

            if st.button("🔗 Verify the whole hash chain", use_container_width=True, key="btn_chain_verify"):
                chain_now = verify_ledger_chain()
                if chain_now.get("ok"):
                    st.success(
                        f"{chain_now.get('entries', 0)} sealed entries verified - each one links "
                        "to the entry before it, so no history was altered or removed."
                    )
                else:
                    st.error(
                        f"Chain broken at entry {chain_now.get('first_break')}: "
                        + "; ".join(str(item.get("reason")) for item in chain_now.get("breaks", [])[:3])
                    )
            if st.button("🕵 Simulate tampering", use_container_width=True, key="btn_tamper_demo"):
                demo = tamper_demo()
                if demo.get("detected"):
                    st.error(
                        f"Tampering detected at entry {demo.get('first_break')}: {demo.get('reason')}"
                    )
                    st.caption(
                        f"The demo edited entry {demo.get('tampered_position')} "
                        f"({demo.get('tampered_alert_id')}) in memory only: {demo.get('changed')}. "
                        "Nothing on disk was touched - press verify again to confirm."
                    )
                else:
                    st.warning(f"Tampering was not detected: {demo.get('reason')}")
            st.caption(
                "From a terminal: `python -m src.utils.hashing --verify` (exit code 1 on a break) "
                "and `python -m src.utils.hashing --tamper-demo`."
            )

            case_file = build_case_file(alert, graph_payload, ledger_report)
            st.download_button(
                "⬇ Case file (HTML)",
                data=case_file["html"],
                file_name=f"{case_file['filename']}.html",
                mime="text/html",
                use_container_width=True,
                key="dl_case_file_html_evidence",
                help="One self-contained dossier for this alert - open it in any browser.",
            )
            st.download_button(
                "⬇ Case file (Markdown)",
                data=case_file["markdown"],
                file_name=f"{case_file['filename']}.md",
                mime="text/markdown",
                use_container_width=True,
                key="dl_case_file_md_evidence",
            )
            st.download_button(
                "⬇ Evidence JSON",
                data=json.dumps(alert, indent=2, sort_keys=True, default=str),
                file_name=f"{alert['alert_id']}_evidence.json",
                mime="application/json",
                use_container_width=True,
                key="dl_evidence_json",
            )
            st.download_button(
                "⬇ All alerts (JSON)",
                data=json.dumps({"alerts": full_alerts}, indent=2, default=str),
                file_name="chaintrace_alerts.json",
                mime="application/json",
                use_container_width=True,
                key="dl_all_alerts",
            )

    st.markdown("---")
    st.markdown("#### Append-only evidence ledger")
    st.caption(
        f"Hash chain: {'✔ intact' if chain_report.get('ok') else '⚠ broken'} · "
        f"{chain_report.get('entries', 0)} sealed entries · "
        "every entry stores the hash of the entry before it, so an edit breaks the chain."
    )
    entries = [entry for entry in read_ledger() if entry.get("alert_id")]
    if entries:
        st.dataframe(pd.DataFrame(entries), hide_index=True, use_container_width=True, height=260)
    else:
        st.caption("Ledger is empty - run the analysis to seal evidence hashes.")

    st.markdown("---")
    st.caption(
        "ChainTrace AI is a prototype: synthetic data only, no live Bitcoin node, no cloud model, "
        "no internet access at runtime. See README.md for the full technical write-up."
    )
