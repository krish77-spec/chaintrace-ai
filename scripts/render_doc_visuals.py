#!/usr/bin/env python
"""Regenerate the images embedded in docs/11_TUTORIAL.md.

Everything is drawn with Pillow from the *current* artifacts in data/artifacts, so
the charts always show real numbers from the last pipeline run. Wireframes are
faithful mock-ups of the dashboard layout (not browser screenshots) - every
label matches the widget label in app/dashboard.py.

Usage:
    python scripts/render_doc_visuals.py
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "data" / "artifacts"
OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- palette ----
BG = "#0b1220"
PANEL = "#0e1526"
CARD = "#141d31"
CARD2 = "#182338"
LINE = "#2b3a55"
INK = "#f1f3f5"
MUTED = "#8a93a6"
BLUE = "#4dabf7"
RED = "#ff6b6b"
ORANGE = "#ffa94d"
YELLOW = "#ffd43b"
LIME = "#a9e34b"
GREEN = "#69db7c"
GRAY = "#adb5bd"
#: Streamlit's primary-button fill, so the wireframes advertise the highlighted control
PRIMARY_FILL = "#ff4b4b"

FONT_CACHE: dict = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if bold else []
    ) + [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size)
            FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    FONT_CACHE[key] = f
    return f


def rounded(d: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def text(d, xy, s, size=14, color=INK, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=color, anchor=anchor)


def tile(d, x, y, w, h, label, value, accent=BLUE):
    rounded(d, (x, y, x + w, y + h), 10, fill=CARD, outline=LINE)
    text(d, (x + 14, y + 10), label.upper(), 11, MUTED, bold=True)
    text(d, (x + 14, y + 32), value, 24, accent, bold=True)


def bar_h(d, x, y, w_max, frac, label, value_s, h=22, color=BLUE):
    text(d, (x, y + 2), label, 13, INK)
    bx = x + 250
    bw = int(w_max * frac)
    rounded(d, (bx, y, bx + w_max, y + h), 6, fill=CARD2)
    if bw > 4:
        rounded(d, (bx, y, bx + bw, y + h), 6, fill=color)
    text(d, (bx + w_max + 12, y + 2), value_s, 13, MUTED)


def load(name):
    return json.load(open(ART / name))


def summary():
    return load("pipeline_summary.json")


def alerts():
    data = load("alerts.json")
    return data if isinstance(data, list) else data.get("alerts", [])


def frame(w, h, title):
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    text(d, (24, 16), title, 20, INK, bold=True)
    return img, d


def _ensure_project_root_on_path() -> None:
    """Make ``src.*`` importable when this file is run as a script."""
    import sys
    from pathlib import Path as _Path

    root = str(_Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def chrome(d, w, active=0):
    """App title bar + tab row."""
    rounded(d, (12, 8, w - 12, 46), 8, fill=PANEL, outline=LINE)
    text(d, (30, 17), "🔗 ChainTrace AI", 15, INK, bold=True)
    text(d, (200, 21), "offline correlation · explainable, hash-stamped alerts", 12, MUTED)
    tabs = ["🎯 Ranked Alerts", "🕸 Interactive Graph", "🌐 Infrastructure",
            "🔒 Evidence & Integrity"]
    x = 24
    for i, label in enumerate(tabs):
        tw = int(d.textlength(label, font=font(13, True))) + 28
        box = (x, 54, x + tw, 84)
        if i == active:
            rounded(d, box, 8, fill=CARD, outline=BLUE, width=2)
        else:
            rounded(d, box, 8, fill=PANEL, outline=LINE)
        text(d, (x + 14, 61), label, 13, INK if i == active else MUTED, bold=i == active)
        x += tw + 10


# ---------------------------------------------------- 1. wireframe: tab 1 ----
def wireframe_tab1():
    s = summary()
    al = alerts()
    counts = s.get("counts", {})
    w, h = 1240, 1010
    img, d = frame(w, h, "Tab 1 — Ranked Alerts (counterfactual + case-file export)")
    chrome(d, w, active=0)

    # sidebar
    rounded(d, (12, 96, 292, h - 16), 10, fill=PANEL, outline=LINE)
    text(d, (28, 110), "SIDEBAR — controls", 12, MUTED, bold=True)
    sy = 140
    radio = ["Generate demo dataset", "Use bundled artifacts", "Upload your own"]
    text(d, (28, sy), "Input data", 12, INK, bold=True)
    sy += 22
    for i, r in enumerate(radio):
        dot_y = sy + 8
        d.ellipse((32, dot_y, 44, dot_y + 12), fill=BLUE if i == 1 else BG, outline=BLUE)
        text(d, (52, sy), r, 12, INK if i == 1 else MUTED)
        sy += 24
    sy += 8
    rounded(d, (28, sy, 276, sy + 34), 8, fill=BLUE)
    text(d, (28 + 138, sy + 9), "▶  Run Analysis", 13, "#0b1220", bold=True, anchor="ma")
    sy += 46
    rounded(d, (28, sy, 276, sy + 44), 8, fill=CARD, outline=LINE)
    text(d, (40, sy + 6), "Upload evidence files", 12, MUTED)
    text(d, (40, sy + 24), "CSV · JSON · XML  (drag or browse)", 11, MUTED)
    sy += 54
    rounded(d, (28, sy, 276, sy + 30), 8, fill=CARD2, outline=LINE)
    text(d, (28 + 124, sy + 8), "Store uploads", 12, INK, anchor="ma")
    sy += 46
    text(d, (28, sy), "Minimum risk score", 12, INK, bold=True)
    sy += 20
    d.line((34, sy + 6, 270, sy + 6), fill=LINE, width=3)
    knob = 34 + int((270 - 34) * 0.5)
    d.ellipse((knob - 8, sy - 2, knob + 8, sy + 14), fill=BLUE)
    text(d, (28, sy + 18), "0.50  (MIN_ALERT_RISK)", 11, MUTED)
    sy += 46
    text(d, (28, sy), "Entity type", 12, INK, bold=True)
    sy += 22
    for chip in ("wallet  ×", "txid  ×"):
        cw = int(d.textlength(chip, font=font(11))) + 16
        rounded(d, (28, sy, 28 + cw, sy + 22), 11, fill=CARD2, outline=LINE)
        text(d, (36, sy + 4), chip, 11, INK)
        sy += 26
    sy += 6
    text(d, (28, sy), "Search entity or reason", 12, INK, bold=True)
    sy += 22
    rounded(d, (28, sy, 276, sy + 28), 6, fill=CARD, outline=LINE)
    text(d, (38, sy + 6), "e.g. bc1q… or “peeling”", 11, MUTED)

    # metric tiles (2 rows x 5, real values)
    mx, my = 312, 96
    tw_, th, gap = 178, 78, 10
    tiles = [
        ("Records processed", f"{counts.get('transactions', 0):,}", INK),
        ("Network records", f"{counts.get('network_records', 0):,}", INK),
        ("Ranked alerts", str(len(al)), BLUE),
        ("Highest risk", f"{max((a['risk_score'] for a in al), default=0):.2f}", RED),
        ("Entities clustered", str(counts.get("clusters", 0)), INK),
        ("Peeling chains", str(counts.get("peel_chains", 0)), ORANGE),
        ("Mixing txs", str(counts.get("mixing_transactions", 0)), ORANGE),
        ("Anomalies flagged", str(counts.get("anomalies_flagged", 0)), YELLOW),
        ("Evidence hashes", str(len(al)), GREEN),
        ("Pipeline time", f"{s.get('duration_seconds', 0):.1f}s", INK),
    ]
    for i, (label, value, accent) in enumerate(tiles):
        x = mx + (i % 5) * (tw_ + gap)
        y = my + (i // 5) * (th + gap)
        tile(d, x, y, tw_, th, label, value, accent)
    ty = my + 2 * (th + gap) + 10

    # ranked table with 3 real rows
    rounded(d, (mx, ty, w - 16, ty + 190), 10, fill=CARD, outline=LINE)
    cols = [("Alert", 0), ("Entity", 150), ("Type", 330), ("Risk", 400),
            ("Conf.", 470), ("Top reason", 550), ("Action", 830)]
    text(d, (mx + 14, ty + 8), "RANKED ALERT TABLE — sorted by risk score", 12, MUTED, bold=True)
    hy = ty + 32
    for name, dx in cols:
        text(d, (mx + 14 + dx, hy), name, 12, MUTED, bold=True)
    d.line((mx + 8, hy + 20, w - 24, hy + 20), fill=LINE)
    ry = hy + 28
    for a in al[:3]:
        entity = a["entity"] if a["entity_type"] == "wallet" else a["entity"][:16] + "…"
        row = [
            a["alert_id"], entity, a["entity_type"], f"{a['risk_score']:.3f}",
            f"{a['confidence']:.2f}",
            ((a.get("reasons") or [""])[0])[:42] + "…",
            a.get("recommended_action", "")[:34],
        ]
        for (name, dx), val in zip(cols, row):
            color = RED if name == "Risk" and a["risk_score"] >= 0.85 else INK
            text(d, (mx + 14 + dx, ry), str(val), 12, color)
        d.line((mx + 8, ry + 22, w - 24, ry + 22), fill="#1b2740")
        ry += 26

    # alert detail card
    dy = ty + 206
    rounded(d, (mx, dy, w - 16, h - 16), 10, fill=PANEL, outline=LINE)
    a0 = al[0]
    text(d, (mx + 14, dy + 10), f"ALERT DETAIL — {a0['alert_id']}   (click a row above to open)",
         13, INK, bold=True)
    cy = dy + 40
    text(d, (mx + 14, cy), "Why this was flagged:", 12, MUTED, bold=True)
    cy += 22
    for reason in (a0.get("reasons") or [])[:4]:
        d.ellipse((mx + 18, cy + 6, mx + 24, cy + 12), fill=BLUE)
        text(d, (mx + 32, cy), reason[:88], 12, INK)
        cy += 22
    text(d, (mx + 14, cy + 6), "Per-detector bars:", 12, MUTED, bold=True)
    comps = a0.get("components", {})
    cx = mx + 160
    for key in ("peel", "mixing", "anomaly", "risk", "cluster", "correlation"):
        v = comps.get(key, 0.0)
        rounded(d, (cx, cy, cx + 96, cy + 20), 6, fill=CARD2)
        if v > 0:
            rounded(d, (cx, cy, cx + int(96 * v), cy + 20), 6,
                    fill=RED if v >= 0.85 else ORANGE)
        text(d, (cx + 2, cy + 3), f"{key} {v:.2f}", 10, INK)
        cx += 112
    text(d, (mx + 14, cy + 34),
         f"risk {a0['risk_score']:.3f} · confidence {a0['confidence']:.2f} · "
         f"action: {a0.get('recommended_action', '')}", 13, YELLOW, bold=True)

    # counterfactual block (X-factor F)
    qy = cy + 74
    text(d, (mx + 14, qy), "🔄 What would clear this entity?  (one row per piece of evidence, "
         "removed and re-scored)", 12, INK, bold=True)
    rows = (a0.get("counterfactuals") or [])
    summary_text = (a0.get("counterfactual_summary") or "")[:118]
    text(d, (mx + 14, qy + 20), summary_text + "…", 11, MUTED)
    hy2 = qy + 44
    heads = [("Remove this evidence", 0), ("Score would be", 520), ("Drop", 660),
             ("Still an alert?", 740), ("Decisive?", 880)]
    for name, dx in heads:
        text(d, (mx + 14 + dx, hy2), name, 11, MUTED, bold=True)
    d.line((mx + 8, hy2 + 18, w - 24, hy2 + 18), fill=LINE)
    ry2 = hy2 + 26
    for row in rows[:2]:
        vals = [
            str(row.get("label", ""))[:58],
            f"{float(row.get('score_without') or 0):.3f}",
            f"{float(row.get('score_drop') or 0):.3f}",
            "yes" if row.get("still_flagged") else "no",
            "yes" if row.get("decisive") else "no",
        ]
        for (name, dx), val in zip(heads, vals):
            text(d, (mx + 14 + dx, ry2), val, 11,
                 RED if (name == "Decisive?" and val == "yes") else INK)
        ry2 += 22
    if not rows:
        text(d, (mx + 14, ry2), "(no counterfactual rows in this artefact)", 11, MUTED)
    text(d, (mx + 14, ry2 + 8),
         "no row says “Decisive: yes” → the alert rests on corroborating evidence, "
         "not on a single fact.", 11, GREEN)

    # downloads row (X-factor B)
    by = ry2 + 36
    for i, label in enumerate(("⬇  Alerts CSV", "⬇  Case file (HTML)", "⬇  Case file (Markdown)")):
        bx = mx + 14 + i * 250
        rounded(d, (bx, by, bx + 230, by + 34), 8,
                fill=BLUE if i == 1 else CARD2, outline=LINE)
        text(d, (bx + 115, by + 9), label, 12, "#0b1220" if i == 1 else INK,
             bold=i == 1, anchor="ma")
    text(d, (mx + 14, by + 46),
         "the case file is one self-contained dossier: reasons · counterfactual · inline-SVG "
         "neighbourhood · network evidence · hashes.", 11, MUTED)

    img.save(OUT / "wireframe_tab1.png")
    print("wrote", OUT / "wireframe_tab1.png")


# ---------------------------------------------------- 2. wireframe: tab 2 ----
def wireframe_focus():
    """The focus view: one node lit, its direct connections lit, everything else faded."""
    w, h = 1240, 1160
    img, d = frame(w, h, "Focus mode — one entity, its direct connections, and its own timeline")
    chrome(d, w, active=1)

    rounded(d, (312, 98, w - 16, 148), 8, fill=CARD, outline=BLUE, width=2)
    text(d, (326, 106),
         "🔎 In focus:  342f31d2…fca73934  ·  txid  ·  risk 0.909  ·  3 direct connections",
         12, INK, bold=True)
    rounded(d, (806, 112, 950, 142), 8, fill=CARD2, outline=LINE)
    text(d, (878, 119), "⬅ Back one hop", 11, MUTED, anchor="ma")
    rounded(d, (962, 112, 1130, 142), 8, fill=PRIMARY_FILL, outline=BLUE)
    text(d, (1046, 119), "🔄 Reset the graph", 11, "#ffffff", anchor="ma")
    text(d, (326, 126), "Route walked:  bc1q35sr…2fllftgx  →  342f31d2…fca73934", 11, MUTED)

    canvas = (312, 164, w - 16, 560)
    rounded(d, canvas, 10, fill="#080e1a", outline=LINE)
    rng = random.Random(11)
    faded = []
    for _ in range(120):
        fx = rng.uniform(canvas[0] + 20, canvas[2] - 20)
        fy = rng.uniform(canvas[1] + 20, canvas[3] - 20)
        faded.append((fx, fy))
    head = ((canvas[0] + canvas[2]) / 2, (canvas[1] + canvas[3]) / 2 + 20)
    for fx, fy in faded:
        d.line((head[0], head[1], fx, fy), fill="#131d33", width=1)
    for fx, fy in faded:
        d.ellipse((fx - 5, fy - 5, fx + 5, fy + 5), fill="#39465f")

    # the direct connections of the focused transaction: two payees + the funding wallet
    neighbours = [
        (head[0] - 200, head[1] - 120, GREEN, "98.3023595 BTC → 15BqRqvx…yvhDFc1d"),
        (head[0] + 210, head[1] - 90, ORANGE, "16.5035034 BTC → 34B5sPbw…MMfU525Q"),
        (head[0] - 258, head[1] + 96, RED, "114.8518036 BTC ← bc1q35sr…2fllftgx (SEED)"),
    ]
    # edge colour follows the *direction of the money*: this transaction paid the two payees
    # (orange, txid -> wallet) and was funded by the seed wallet (blue, wallet -> txid).
    edge_colours = ["#ffa94d", "#ffa94d", "#4dabf7"]
    for (nx_, ny, _, _), colour in zip(neighbours, edge_colours):
        d.line((head[0], head[1], nx_, ny), fill=colour, width=3)

    def arrowhead(start, end, colour, at=0.72, size=9):
        """A small triangle on the edge, pointing the way the coin moved."""
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / length, dy / length
        tip = (start[0] + dx * at, start[1] + dy * at)
        back = (tip[0] - ux * size * 1.6, tip[1] - uy * size * 1.6)
        d.polygon(
            [tip,
             (back[0] - uy * size, back[1] + ux * size),
             (back[0] + uy * size, back[1] - ux * size)],
            fill=colour,
        )

    for (nx_, ny, _, _), colour in zip(neighbours[:2], edge_colours[:2]):
        arrowhead(head, (nx_, ny), colour)
    arrowhead(neighbours[2][:2], head, edge_colours[2])

    def blend(colour: str, other: str, how_much: float) -> str:
        """Mix two hex colours - PIL has no alpha on an RGB canvas, so a halo is a blend."""
        first = tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))
        second = tuple(int(other[index:index + 2], 16) for index in (1, 3, 5))
        mixed = tuple(round(a + (b - a) * how_much) for a, b in zip(first, second))
        return "#%02x%02x%02x" % mixed

    # The coin in flight, on the first payout edge: a comet tail along the line it has
    # already covered plus the coin itself, which is what "▶ Animate the money flow" draws
    # frame by frame in the real chart.
    start, end = head, neighbours[0][:2]
    tail_from = (start[0] + (end[0] - start[0]) * 0.34, start[1] + (end[1] - start[1]) * 0.34)
    coin = (start[0] + (end[0] - start[0]) * 0.56, start[1] + (end[1] - start[1]) * 0.56)
    d.line((tail_from[0], tail_from[1], coin[0], coin[1]), fill=edge_colours[0], width=6)
    d.ellipse((coin[0] - 11, coin[1] - 11, coin[0] + 11, coin[1] + 11),
              fill=edge_colours[0], outline="#f8f9fa", width=2)

    # The play control lives inside the chart in the real console, in the *bottom*-right
    # corner.  The top-right is where plotly draws its own zoom/pan/select toolbar, and the
    # pill used to sit up there - the two landed on exactly the same pixels (measured: the
    # pill x 1252-1411 y 936-969, the toolbar x 1140-1412 y 937-963).  The toolbar is drawn
    # here as well, so the picture shows *why* the pill moved rather than just where it is.
    toolbar = (canvas[2] - 148, canvas[1] + 8)
    for index in range(5):
        rounded(d, (toolbar[0] + index * 28, toolbar[1], toolbar[0] + index * 28 + 20,
                    toolbar[1] + 20), 4, fill="#16233a", outline="#2b3a55")
    text(d, (canvas[2] - 176, canvas[1] + 12), "plotly's own zoom / pan toolbar", 10,
         MUTED, anchor="ra")
    play = (canvas[2] - 232, canvas[3] - 68)
    rounded(d, (play[0], play[1], play[0] + 220, play[1] + 30), 8, fill=CARD2, outline=LINE)
    text(d, (play[0] + 110, play[1] + 8), "▶ Animate the money flow", 11, INK, anchor="ma")

    for nx_, ny, colour, label in neighbours:
        d.ellipse((nx_ - 29, ny - 29, nx_ + 29, ny + 29), fill=blend(colour, "#080e1a", 0.78))
        d.ellipse((nx_ - 18, ny - 18, nx_ + 18, ny + 18), fill=colour,
                  outline="#ffffff", width=3)
        text(d, (nx_ + 24, ny - 6), label, 11, MUTED)
    d.ellipse((head[0] - 41, head[1] - 41, head[0] + 41, head[1] + 41),
              fill=blend(RED, "#080e1a", 0.72))
    d.ellipse((head[0] - 30, head[1] - 30, head[0] + 30, head[1] + 30), fill=RED,
              outline="#ffffff", width=4)
    text(d, (head[0], head[1] - 46), "the node in focus", 11, INK, anchor="ma")
    text(d, (canvas[0] + 16, canvas[1] + 10),
         "dim circles = unrelated nodes, still clickable (clicking one moves the focus there)",
         11, MUTED)
    text(d, (canvas[0] + 16, canvas[3] - 26),
         "blue = wallet funded the transaction · orange = the transaction paid that wallet · "
         "arrowheads point the way the money moved · dotted amber = the route you walked · "
         "halo = the lit neighbourhood · the coin is mid-flight between sender and receiver",
         11, MUTED)

    # focus panel
    py = 578
    tiles = [("Risk score", "0.909", INK), ("Direct connections", "3", INK),
             ("Events", "3", INK), ("Value in", "114.8518 BTC", GREEN),
             ("Value out", "114.8059 BTC", ORANGE)]
    x = 312
    for label, value, accent in tiles:
        rounded(d, (x, py, x + 176, py + 62), 8, fill=CARD, outline=LINE)
        text(d, (x + 12, py + 8), label.upper(), 10, MUTED, bold=True)
        text(d, (x + 12, py + 26), value, 15, accent, bold=True)
        x += 186

    # money flow: who sent what to whom, sender in the left column, receiver in the next
    flow_y = py + 76
    rounded(d, (312, flow_y, w - 16, flow_y + 152), 8, fill=CARD, outline=LINE)
    text(d, (324, flow_y + 8), "💸 Who sent what to whom", 12, INK, bold=True)
    flow_headers = ["From (sent)", "To (received)", "Amount (BTC)", "What it means"]
    for index, head_text in enumerate(flow_headers):
        text(d, (324 + index * 232, flow_y + 32), head_text, 10, MUTED, bold=True)
    flow_rows = [
        ("wallet bc1q35sr…2fllftgx", "txid 342f31d2…fca73934", "114.8518036",
         "this entity received it from that wallet"),
        ("txid 342f31d2…fca73934", "wallet 15BqRqvx…yvhDFc1d", "98.3023595",
         "this entity sent it on to that wallet"),
        ("txid 342f31d2…fca73934", "wallet 34B5sPbw…MMfU525Q", "16.5035034",
         "this entity sent it on to that wallet"),
    ]
    for r, row in enumerate(flow_rows):
        for c, cell in enumerate(row):
            text(d, (324 + c * 232, flow_y + 54 + r * 19), cell, 10,
                 INK if c < 2 else MUTED)
    text(d, (324, flow_y + 118),
         "1 sender(s) → 2 receiver(s)  ·  114.8518 BTC sent in  ·  114.8059 BTC paid out  ·  "
         "difference 45.9407 mBTC is the miner fee.",
         10, MUTED)
    text(d, (324, flow_y + 134),
         "One sender, so this is exact: bc1q35sr…2fllftgx funded the transaction, which paid "
         "both receivers above.",
         10, MUTED)

    col_y = py + 76 + 172
    rounded(d, (312, col_y, 704, col_y + 178), 8, fill=CARD, outline=LINE)
    text(d, (324, col_y + 8), "What this entity is", 12, INK, bold=True)
    rows = [("Risk score", "0.909"), ("Timestamp", "2026-08-23 21:29:57"),
            ("Value in", "114.8518 BTC"), ("Value out", "114.8059 BTC"),
            ("Fee", "45.9407 mBTC"), ("Inputs / outputs", "1 in · 2 out"),
            ("Script type", "P2WPKH"), ("Anomaly score", "0.798")]
    for index, (key, value) in enumerate(rows):
        text(d, (324, col_y + 32 + index * 18), key, 11, MUTED)
        text(d, (508, col_y + 32 + index * 18), value, 11, INK)

    rounded(d, (716, col_y, w - 16, col_y + 178), 8, fill=CARD, outline=LINE)
    text(d, (728, col_y + 8), "Direct connections (3)  —  click a row to walk there",
         12, INK, bold=True)
    headers = ["When", "What happened", "Amount", "Counterparty", "Their risk", "Flag"]
    for index, head_text in enumerate(headers):
        text(d, (728 + index * 78, col_y + 34), head_text, 10, MUTED, bold=True)
    table = [
        ("2026-08-23 21:29", "paid out to", "98.3024", "15BqRqvx…", "0.62", ""),
        ("2026-08-23 21:29", "paid out to", "16.5035", "34B5sPbw…", "0.55", ""),
        ("2026-08-23 21:29", "funded by", "114.8518", "bc1q35sr…", "1.000", "SEED"),
    ]
    for r, row in enumerate(table):
        for c, cell in enumerate(row):
            colour = RED if cell == "SEED" else INK if c < 5 else MUTED
            text(d, (728 + c * 78, col_y + 56 + r * 20), cell, 10, colour)

    rail_y = col_y + 206
    rounded(d, (312, rail_y, w - 16, rail_y + 74), 8, fill=CARD, outline=LINE)
    text(d, (324, rail_y + 8), "🕒 Timeline of this entity", 12, INK, bold=True)
    d.line((330, rail_y + 46, 1180, rail_y + 46), fill="#1f2a44", width=2)
    for px, colour, size in ((380, "#4dabf7", 9), (382, "#ffa94d", 7),
                             (384, "#ffa94d", 6), (760, "#4dabf7", 6),
                             (980, "#b197fc", 5)):
        d.ellipse((px - size, rail_y + 46 - size, px + size, rail_y + 46 + size),
                  fill=colour, outline="#0b1220")
    text(d, (1188, rail_y + 40), "time →", 10, MUTED)
    text(d, (324, rail_y + 58),
         "one dot per connection · blue = money in · orange = money out · "
         "purple diamond = matched packet · dot size follows the amount",
         10, MUTED)

    img.save(OUT / "wireframe_focus.png")
    print("wrote", OUT / "wireframe_focus.png")


def wireframe_tab2():
    s = summary()
    detail = next((st for st in s.get("stages", []) if st.get("name") == "graph"), {})
    det = detail.get("detail", {})
    w, h = 1240, 960
    img, d = frame(w, h, "Tab 2 — Interactive Graph (with focus mode and the timeline replay)")
    chrome(d, w, active=1)

    tiles = [
        ("Nodes", str(det.get("nodes", 0)), INK),
        ("Edges", str(det.get("edges", 0)), INK),
        ("Wallets", str(det.get("wallets", 0)), BLUE),
        ("Seeds", str(det.get("seed_wallets", 0)), RED),
    ]
    x = 312
    for label, value, accent in tiles:
        tile(d, x, 96, 218, 70, label, value, accent)
        x += 230

    # controls row
    controls = [
        ("Colour nodes by", "Risk score  ▾"),
        ("Max nodes drawn", "250  ←→  50–1200"),
        ("Hide nodes below risk", "0.00  ←→"),
        ("Find a node (address or txid)", "paste an id, press ⏎…"),
    ]
    x = 312
    for label, value in controls:
        rounded(d, (x, 182, x + 218, 236), 8, fill=CARD, outline=LINE)
        text(d, (x + 12, 190), label, 11, MUTED, bold=True)
        text(d, (x + 12, 208), value, 12, INK)
        x += 230

    # focus row — always on screen, whether or not anything is in focus
    rounded(d, (312, 246, w - 16, 322), 8, fill=CARD, outline=BLUE, width=2)
    text(d, (326, 254),
         "🔎 In focus:  bc1q35sr…2fllftgx  ·  wallet  ·  risk 1.000  ·  12 direct connection(s)",
         12, INK, bold=True)
    text(d, (326, 274), "Route walked:  bc1q35sr…2fllftgx  →  342f31d2…fca73934", 11, MUTED)
    rounded(d, (806, 262, 950, 296), 8, fill=CARD2, outline=LINE)
    text(d, (878, 271), "⬅ Back one hop", 11, MUTED, anchor="ma")
    rounded(d, (962, 262, 1130, 296), 8, fill=PRIMARY_FILL, outline=BLUE)
    text(d, (1046, 271), "🔄 Reset the graph", 11, "#ffffff", anchor="ma")
    text(d, (326, 300),
         "the lit node and its direct connections stay bright, everything else fades · "
         "Reset is never disabled", 11, MUTED)

    # timeline replay row (X-factor D)
    rounded(d, (312, 330, w - 16, 386), 8, fill=CARD, outline=BLUE, width=2)
    text(d, (326, 338), "⏱ Timeline replay", 12, INK, bold=True)
    rounded(d, (450, 336, 470, 356), 10, fill=BLUE)
    rounded(d, (466, 339, 480, 353), 7, fill=BLUE)
    text(d, (492, 340), "Data known up to", 11, MUTED)
    d.line((600, 350, 860, 350), fill=LINE, width=3)
    knob2 = 600 + int(260 * 0.62)
    d.ellipse((knob2 - 8, 342, knob2 + 8, 358), fill=BLUE)
    text(d, (872, 340), "event 41 / 66  ·  2026-09-14 03:11", 11, MUTED)
    rounded(d, (1040, 334, 1160, 364), 8, fill=CARD2, outline=LINE)
    text(d, (1100, 341), "▶ Play the window", 11, INK, anchor="ma")
    text(d, (326, 362),
         "replay shows only what was known by then — the chain unrolls hop by hop and the "
         "alert counter climbs.", 11, MUTED)

    # canvas with nodes
    canvas = (312, 398, w - 16, h - 92)
    rounded(d, canvas, 10, fill="#080e1a", outline=LINE)
    rng = random.Random(7)
    nodes = []
    for _ in range(90):
        nx_ = rng.uniform(canvas[0] + 24, canvas[2] - 24)
        ny = rng.uniform(canvas[1] + 24, canvas[3] - 24)
        r = rng.random()
        if r < 0.08:
            nodes.append((nx_, ny, RED, 9, "seed / risk ≥ 0.85"))
        elif r < 0.25:
            nodes.append((nx_, ny, ORANGE, 7, "risk 0.7–0.85"))
        elif r < 0.5:
            nodes.append((nx_, ny, YELLOW, 6, "risk 0.5–0.7"))
        elif r < 0.8:
            nodes.append((nx_, ny, BLUE, 6, "wallet"))
        elif r < 0.95:
            nodes.append((nx_, ny, GRAY, 4, "transaction"))
        else:
            nodes.append((nx_, ny, "#c77f3f", 5, "IP / network node"))
    for i in range(0, len(nodes) - 1, 2):
        a, b = nodes[i], nodes[i + 1]
        d.line((a[0], a[1], b[0], b[1]), fill="#1b2740", width=1)
    for nx_, ny, color, size, _ in nodes:
        d.ellipse((nx_ - size, ny - size, nx_ + size, ny + size), fill=color,
                  outline="#0b1220")
    text(d, (canvas[0] + 16, canvas[1] + 10),
         "drag to pan · scroll to zoom · hover a node for risk, cluster, degree · "
         "click a node to focus it (the entity's timeline appears below)",
         12, MUTED)

    # legend
    ly = h - 80
    legend = [(RED, "risk ≥ 0.85"), (ORANGE, "0.70–0.85"), (YELLOW, "0.50–0.70"),
              (LIME, "0.30–0.50"), (GREEN, "< 0.30"), (BLUE, "wallet"), (GRAY, "txid"),
              ("#c77f3f", "ip")]
    lx = 312
    for color, label in legend:
        d.ellipse((lx, ly + 4, lx + 12, ly + 16), fill=color)
        text(d, (lx + 18, ly + 2), label, 11, MUTED)
        lx += int(d.textlength(label, font=font(11))) + 46
    text(d, (312, ly + 26),
         "“⬇ Download this graph as an interactive HTML file” saves the current view "
         "— it opens offline in any browser.", 12, MUTED)

    img.save(OUT / "wireframe_tab2.png")
    print("wrote", OUT / "wireframe_tab2.png")


# ------------------------------------------- 3. wireframe: tab 3 (infra) ----
def wireframe_tab3():
    """X-factor E — the ASN / country roll-up, rendered from the real artefacts."""
    _ensure_project_root_on_path()
    from src.utils.infrastructure import focus_list, rollup

    al = alerts()
    report = rollup(al)
    report["focus"] = focus_list(report)
    totals = report.get("totals", {})
    w, h = 1240, 700
    img, d = frame(w, h, "Tab 3 — Infrastructure roll-up: which networks to talk to next")
    chrome(d, w, active=2)

    tiles = [
        ("Alerts", str(totals.get("alerts", len(al))), INK),
        ("With network evidence", str(totals.get("alerts_with_network_evidence", 0)), BLUE),
        ("Distinct providers (ASN)", str(totals.get("distinct_asns", 0)), ORANGE),
        ("Distinct countries", str(totals.get("distinct_countries", 0)), INK),
    ]
    x = 24
    for label, value, accent in tiles:
        tile(d, x, 96, 292, 74, label, value, accent)
        x += 302

    rounded(d, (24, 184, w - 16, 240), 8, fill=CARD, outline=LINE)
    text(d, (38, 196), "ℹ", 14, BLUE, bold=True)
    text(d, (62, 198), (report.get("headline") or "")[:150], 12, INK)
    text(d, (62, 216),
         "generated from the roll-up itself, so the sentence and the tables can never disagree",
         11, MUTED)

    # providers table
    rounded(d, (24, 256, 700, 560), 10, fill=PANEL, outline=LINE)
    text(d, (40, 268), "Providers (ASN) behind flagged activity", 12, INK, bold=True)
    pcols = [("ASN", 0), ("Operator", 90), ("Alerts", 330), ("Max risk", 400),
             ("Countries", 480)]
    for name, dx in pcols:
        text(d, (40 + dx, 296), name, 11, MUTED, bold=True)
    d.line((32, 316, 690, 316), fill=LINE)
    y = 324
    for row in (report.get("providers") or [])[:9]:
        vals = [row["key"], str(row["label"])[:24], str(row["alerts"]),
                f"{float(row.get('max_risk') or 0):.2f}",
                ", ".join(row.get("countries") or [])[:14]]
        for (name, dx), val in zip(pcols, vals):
            text(d, (40 + dx, y), val, 12,
                 RED if name == "Max risk" and float(row.get("max_risk") or 0) >= 0.85 else INK)
        y += 24

    # countries table
    rounded(d, (716, 256, w - 16, 560), 10, fill=PANEL, outline=LINE)
    text(d, (732, 268), "Countries in the flagged traffic", 12, INK, bold=True)
    ccols = [("Country", 0), ("Alerts", 120), ("Providers", 200), ("Max risk", 300)]
    for name, dx in ccols:
        text(d, (732 + dx, 296), name, 11, MUTED, bold=True)
    d.line((724, 316, w - 24, 316), fill=LINE)
    y = 324
    for row in (report.get("countries") or [])[:9]:
        vals = [row["key"], str(row["alerts"]), str(row.get("providers", 0)),
                f"{float(row.get('max_risk') or 0):.2f}"]
        for (name, dx), val in zip(ccols, vals):
            text(d, (732 + dx, y), val, 12, INK)
        y += 24

    # next steps
    rounded(d, (24, 576, w - 16, 664), 10, fill=CARD, outline=LINE)
    text(d, (40, 586), "Suggested next steps", 12, INK, bold=True)
    fy = 608
    for row in (report.get("focus") or [])[:4]:
        text(d, (46, fy),
             f"{row['rank']}. {row['provider']} ({row['asn']}, {row['countries']}) — "
             f"{row['alerts']} linked alerts, max risk {float(row['max_risk']):.2f}. "
             f"{row['action']}.", 11, INK)
        fy += 18

    img.save(OUT / "wireframe_tab3.png")
    print("wrote", OUT / "wireframe_tab3.png")


# --------------------------------------- 4. wireframe: tab 4 (evidence) ----
def wireframe_tab4():
    al = alerts()
    a0 = al[0]
    w, h = 1240, 900
    img, d = frame(w, h, "Tab 4 — Evidence & Integrity: hash-chained ledger + case file")
    chrome(d, w, active=3)

    # alert selector
    rounded(d, (24, 96, w - 16, 134), 8, fill=CARD, outline=LINE)
    text(d, (38, 104), "Alert", 11, MUTED, bold=True)
    text(d, (38, 116),
         f"{a0['alert_id']} · {a0['entity_type']} · risk {a0['risk_score']:.2f}          ▾",
         13, INK)

    # left column
    lx, lwd = 24, 700
    rounded(d, (lx, 148, lx + lwd, 252), 10, fill=CARD, outline=LINE)
    text(d, (lx + 16, 160), "Entity", 11, MUTED, bold=True)
    text(d, (lx + 16, 176), a0["entity"][:60], 13, INK)
    text(d, (lx + 16, 200), f"Risk {a0['risk_score']:.3f} · confidence {a0['confidence']:.3f}",
         13, YELLOW, bold=True)
    text(d, (lx + 16, 224), "SHA-256", 11, MUTED, bold=True)
    text(d, (lx + 16, 238), str(a0.get("evidence_hash", ""))[:64] + "…", 12, GRAY)

    rounded(d, (lx, 264, lx + lwd, 388), 10, fill=PANEL, outline=LINE)
    text(d, (lx + 16, 276), "Reasons", 12, INK, bold=True)
    ry = 302
    for reason in (a0.get("reasons") or [])[:4]:
        d.ellipse((lx + 20, ry + 7, lx + 26, ry + 13), fill=BLUE)
        text(d, (lx + 34, ry), reason[:84], 12, INK)
        ry += 22

    rounded(d, (lx, 400, lx + lwd, 560), 10, fill=PANEL, outline=LINE)
    text(d, (lx + 16, 412), "Evidence package  (st.json — expandable)", 12, INK, bold=True)
    ev_keys = list((a0.get("evidence") or {}).keys())[:9]
    jy = 438
    for k in ev_keys:
        text(d, (lx + 24, jy), f'"{k}": …', 12, GRAY)
        jy += 22

    # right column
    rx = 744
    rounded(d, (rx, 148, w - 16, 196), 8, fill=BLUE)
    text(d, (rx + 150, 164), "🔍  Recompute SHA-256 and verify", 13, "#0b1220", bold=True, anchor="ma")
    rounded(d, (rx, 208, w - 16, 258), 8, fill="#12331f", outline=GREEN)
    text(d, (rx + 14, 218), "✔ Hash verified — the evidence has not been altered.",
         12, GREEN, bold=True)
    text(d, (rx + 14, 236), "stored: a3f9…  computed: a3f9…  (identical)", 11, GRAY)

    rounded(d, (rx, 270, w - 16, 396), 10, fill=CARD, outline=LINE)
    text(d, (rx + 16, 282), "Ledger status", 12, INK, bold=True)
    ly = 308
    for k, v in (("entries", "60"), ("verified", "60"), ("mismatched", "0"),
                 ("missing", "0")):
        text(d, (rx + 16, ly), k, 12, MUTED)
        text(d, (rx + 200, ly), v, 12, INK)
        ly += 24
    text(d, (rx + 16, ly + 4), "✔ ledger consistent", 13, GREEN, bold=True)

    for i, label in enumerate(("⬇ Download evidence JSON", "⬇ Download all alerts")):
        rounded(d, (rx, 408 + i * 46, w - 16, 408 + i * 46 + 36), 8,
                fill=CARD2, outline=LINE)
        text(d, (rx + 16, 408 + i * 46 + 9), label, 12, INK)

    # hash-chain verification (X-factor A) + case-file export (X-factor B)
    rounded(d, (24, 574, w - 16, 700), 10, fill=CARD, outline=LINE)
    text(d, (40, 586), "Hash chain — every entry seals the entry before it", 12, INK, bold=True)
    rounded(d, (40, 612, 340, 648), 8, fill=BLUE)
    text(d, (190, 621), "🔗  Verify the whole hash chain", 12, "#0b1220", bold=True, anchor="ma")
    rounded(d, (356, 612, 656, 648), 8, fill=CARD2, outline=LINE)
    text(d, (506, 621), "🕵  Simulate tampering", 12, INK, anchor="ma")
    rounded(d, (672, 612, 1010, 648), 8, fill="#12331f", outline=GREEN)
    text(d, (686, 620), "✔ 60 sealed entries verified — each links to the one before it.",
         11, GREEN, bold=True)
    text(d, (686, 634), "python -m src.utils.hashing --verify  (exit 1 if broken)", 10, GRAY)
    text(d, (40, 664), "⬇  Case file (HTML)   ⬇  Case file (Markdown)   "
         "⬇  Evidence JSON   ⬇  All alerts (JSON)", 12, INK)

    # ledger table
    rounded(d, (24, 712, w - 16, 868), 10, fill=PANEL, outline=LINE)
    text(d, (40, 724), "Append-only evidence ledger  (data/evidence_ledger.jsonl)",
         12, INK, bold=True)
    cols = ["seq", "alert_id", "sealed_at", "evidence_hash"]
    cx = 40
    widths = [60, 200, 260, 120]
    for name, wd in zip(cols, widths):
        text(d, (cx, 750), name, 12, MUTED, bold=True)
        cx += wd
    d.line((32, 770, w - 24, 770), fill=LINE)
    y = 778
    for i, seq in enumerate((1, 2, 3)):
        vals = [str(seq), str(al[i]["alert_id"]), "2026-09-19T10:02:…Z",
                str(al[i].get("evidence_hash", ""))[:24] + "…"]
        cx = 40
        for val, wd in zip(vals, widths):
            text(d, (cx, y), val, 12, GRAY)
            cx += wd
        y += 24
    text(d, (40, 850), "60 rows — one hash per alert, written once, never edited; the chain is what detects that.",
         11, MUTED)

    img.save(OUT / "wireframe_tab4.png")
    print("wrote", OUT / "wireframe_tab4.png")


# ------------------------------------------------- 5. stage timing chart ----
def stage_timings():
    s = summary()
    stages = s.get("stages", [])
    w, h = 1240, 120 + 34 * len(stages) + 60
    img, d = frame(w, h, "What the pipeline spends its time on — real timings of the last run")
    tmax = max(st["seconds"] for st in stages) or 1.0
    colours = {1: BLUE, 2: BLUE, 3: BLUE, 4: GREEN, 5: ORANGE, 6: YELLOW, 7: RED, 8: RED}
    y = 90
    for st in stages:
        color = colours.get(st["stage"], GRAY)
        bar_h(d, 24, y, 640, st["seconds"] / tmax,
              f"stage {st['stage']} · {st['name']}", f"{st['seconds']:.3f}s",
              h=24, color=color)
        y += 34
    text(d, (24, y + 10),
         f"total: {s.get('duration_seconds', 0):.1f}s  ·  stage 5 runs six detectors "
         "(the orange block) — everything else is plumbing.", 13, MUTED, bold=True)
    img.save(OUT / "stage_timings.png")
    print("wrote", OUT / "stage_timings.png")


# --------------------------------------------------- 6. score anatomy -------
def score_anatomy():
    al = alerts()
    a0 = al[0]
    comps = a0.get("components", {})
    weights = {"peel": 0.22, "mixing": 0.10, "anomaly": 0.20, "risk": 0.34,
               "cluster": 0.10, "correlation": 0.08}
    ladder = {"risk": 0.95, "peel": 0.85, "mixing": 0.75, "anomaly": 0.70}
    blended = sum(weights[k] * comps.get(k, 0.0) for k in weights) / sum(weights.values())
    ladder_term = max(ladder[k] * comps.get(k, 0.0) for k in ladder)
    final = max(blended, ladder_term)

    w, h = 1240, 560
    img, d = frame(w, h, f"Anatomy of one alert score — {a0['alert_id']} (real values)")
    text(d, (24, 54), "1 · six detectors each vote 0–1", 14, BLUE, bold=True)
    text(d, (460, 54), "2 · weighted blend", 14, BLUE, bold=True)
    text(d, (800, 54), "3 · severity ladder floor", 14, BLUE, bold=True)

    y = 92
    for k in ("peel", "mixing", "anomaly", "risk", "cluster", "correlation"):
        v = comps.get(k, 0.0)
        bar_h(d, 24, y, 220, v, f"{k}  (w {weights[k]:.2f})", f"{v:.2f}",
              h=20, color=RED if v >= 0.85 else ORANGE if v >= 0.4 else "#3b4a63")
        y += 34

    rounded(d, (460, 92, 760, 200), 10, fill=CARD, outline=LINE)
    text(d, (476, 104), "blended = Σ weight·component", 12, INK, bold=True)
    text(d, (476, 126), "            ÷ Σ weights (1.04!)", 12, MUTED)
    text(d, (476, 150), f"= {blended:.3f}", 20, YELLOW, bold=True)
    text(d, (476, 182), "the blend alone would undersell a strong signal", 11, MUTED)

    rounded(d, (800, 92, 1100, 200), 10, fill=CARD, outline=LINE)
    text(d, (816, 104), "ladder = max over:", 12, INK, bold=True)
    ly = 126
    for k, floor in ladder.items():
        text(d, (816, ly), f"{floor:.2f} × {k} = {ladder[k]*comps.get(k,0.0):.3f}",
             12, GRAY)
        ly += 20
    text(d, (816, ly + 2), f"= {ladder_term:.3f}", 16, YELLOW, bold=True)

    rounded(d, (24, 300, 560, 372), 10, fill=CARD2, outline=RED)
    text(d, (40, 312), f"FINAL = max(blend, ladder) = {final:.3f}   "
                       f"(stored as risk_score)", 14, RED, bold=True)
    rounded(d, (24, 384, 560, 456), 10, fill=CARD2, outline=GREEN)
    strength = sum(sorted([v for v in comps.values() if v > 0], reverse=True)[:3]) / 3
    agreement = min(sum(1 for v in comps.values() if v >= 0.40) / 3, 1.0)
    conf = max(0.60 * strength + 0.40 * agreement, 0.0)
    text(d, (40, 396), f"CONFIDENCE = 0.60·{strength:.2f} + 0.40·{agreement:.2f} "
                       f"= {conf:.2f}", 14, GREEN, bold=True)
    text(d, (40, 424), "strength = mean of top 3 components · agreement = how many "
                       "components ≥ 0.40", 11, MUTED)

    rounded(d, (580, 300, 1100, 456), 10, fill=PANEL, outline=LINE)
    text(d, (596, 312), "Where the score sends it (recommended_action):", 12, INK, bold=True)
    ay = 340
    for threshold, label, color in (
        ("seed wallet", "Escalate immediately — sanctioned/known-bad", RED),
        ("≥ 0.75 or ≤1 hop from seed", "Escalate — full wallet history", ORANGE),
        ("≥ 0.50 (MIN_ALERT_RISK)", "Investigate — enrich with KYC data", YELLOW),
        ("below 0.50", "Monitor — next review cycle", GRAY),
    ):
        d.ellipse((600, ay + 6, 612, ay + 18), fill=color)
        text(d, (622, ay), threshold, 12, INK, bold=True)
        text(d, (622, ay + 16), label, 11, MUTED)
        ay += 42

    img.save(OUT / "score_anatomy.png")
    print("wrote", OUT / "score_anatomy.png")


# ------------------------------------------------- 6. score distribution ----
def score_distribution():
    al = alerts()
    scores = [a["risk_score"] for a in al]
    bins = [0] * 10
    for s in scores:
        bins[min(int(s * 10), 9)] += 1
    w, h = 1240, 460
    img, d = frame(w, h, "The 60 alert scores of the last run (real data)")
    base = h - 90
    top = 90
    bw = 100
    gap = 8
    x0 = 80
    for i, n in enumerate(bins):
        bh = int((n / max(bins)) * (base - top))
        x = x0 + i * (bw + gap)
        color = [GREEN, LIME, YELLOW, YELLOW, ORANGE, ORANGE, RED, RED, RED, RED][i]
        rounded(d, (x, base - bh, x + bw, base), 6, fill=color)
        text(d, (x + bw / 2, base - bh - 22), str(n), 14, INK, bold=True, anchor="ma")
        text(d, (x + bw / 2, base + 8), f"{i/10:.1f}", 11, MUTED, anchor="ma")
    # MIN_ALERT_RISK line
    lx = x0 + int(5 * (bw + gap))
    d.line((lx, top - 10, lx, base), fill=INK, width=2)
    text(d, (lx + 8, top - 8), "MIN_ALERT_RISK = 0.50", 12, INK, bold=True)
    hi = sum(1 for s in scores if s >= 0.9)
    text(d, (80, h - 44),
         f"{len(scores)} alerts · {hi} of them ≥ 0.90 · none below 0.50 by construction "
         "(the explainer only keeps entities that clear the threshold).",
         13, MUTED, bold=True)
    img.save(OUT / "score_distribution.png")
    print("wrote", OUT / "score_distribution.png")


if __name__ == "__main__":
    wireframe_tab1()
    wireframe_tab2()
    wireframe_focus()
    wireframe_tab3()
    wireframe_tab4()
    stage_timings()
    score_anatomy()
    score_distribution()
    print("done →", OUT)
