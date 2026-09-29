# Part 8 — Runbook: install it, run it, read it

Three ways to run ChainTrace AI. Pick one; they all end at the same place.

| Way | Best for | Needs | Time |
|---|---|---|---|
| **A. Docker** | the demo, judging, "it must just work" | Docker Desktop | first build ~10 min (network), later starts seconds |
| **B. Local Python** | development, reading code, editing | Python 3.11 | ~3 min install |
| **C. API only** | scripting, integration | Python 3.11 | seconds |

---

## A. Docker (the one-command demo)

```bash
cd chaintrace-ai
docker compose up --build
```

Then open:

- **Dashboard:** http://localhost:8501
- **API docs (Swagger):** http://localhost:8000/docs
- **Health probe:** http://localhost:8000/health

What happens during the build (this is why the demo is instant afterwards):

1. Python 3.11-slim base, then the pinned stack from `requirements.txt` (with a BuildKit pip cache mount,
   so an interrupted build resumes instead of re-downloading ~150 MB).
2. Inside the image: `python scripts/generate_sample_data.py` → `run_full_pipeline.py` →
   `train_models.py`. The image **ships with a completed analysis**.
3. At container start, `entrypoint.sh` creates directories, regenerates the sample or re-runs the pipeline
   **only if artefacts are missing**, then starts uvicorn (8000) and Streamlit (8501).

Useful variants:

```bash
# real KernelSHAP explanations on the top 12 alerts (installs shap; slower build)
INSTALL_SHAP=1 docker compose build && docker compose up

# always re-run the analysis at container start
CHAINTRACE_RUN_ON_START=always docker compose up

# tear down (keeps ./data because it is a bind mount)
docker compose down
```

**Offline claim:** after the build, unplug the network. Runtime does no outbound calls — synthetic data,
local models, a local GeoIP table, and `gatherUsageStats = false` in `.streamlit/config.toml`.

> **The image carries the code.** `src/`, `app/` and `scripts/` are copied at build time and only
> `./data` is a bind mount, so after editing the dashboard you must rebuild for the container to serve it:
> `docker compose up --build -d`. Otherwise localhost:8501 keeps serving the previous build — which is
> how "my fix does nothing" usually happens. To check what a running container actually has:
> `docker exec chaintrace-ai grep -c "Animate the money flow" /app/app/dashboard.py`. For fast iteration
> run the checkout instead: `streamlit run app/dashboard.py --server.port 8502`.

---

## B. Local Python (and installing Streamlit on purpose)

The stack needs **Python 3.11** (3.9 is too old — `networkx 3.3` and `streamlit 1.38` require 3.9+ but
the pinned `pandas 2.2`/`numpy 1.26` combination behaves badly on 3.9; the image uses 3.11).

```bash
cd chaintrace-ai

# 1. create the environment (3.11)
python3.11 -m venv .venv311          # or: conda create -p .venv311 python=3.11 -y
source .venv311/bin/activate         # Windows: .venv311\Scripts\activate

# 2. install everything, Streamlit included
pip install -r requirements.txt
# --- or install just the dashboard stack, if you only want to look at it ---
# pip install streamlit==1.38.0 pandas==2.2.2 numpy==1.26.4 plotly==5.24.0 \
#             networkx==3.3 requests==2.32.3

# 3. build the dataset, audit it, train the models, run the analysis
python scripts/generate_sample_data.py
python scripts/audit_dataset.py         # 32 checks against the SIH26146 brief
python scripts/train_models.py
python scripts/run_full_pipeline.py

# 4. start the dashboard
streamlit run app/dashboard.py
# → http://localhost:8501
```

Optional, in a second terminal — start the API too, so the dashboard's sidebar shows **API · live**
instead of **artefacts · filesystem**:

```bash
uvicorn src.api.main:app --port 8000
```

Check the installation before a demo:

```bash
python -m pytest -q                    # 60 tests, ~9 s
python scripts/audit_dataset.py        # 32 dataset-contract checks vs the SIH26146 brief
python scripts/validate_detections.py  # detector precision/recall vs planted ground truth
```

> **Note:** the repository contains a second, older `.venv` directory created on Python 3.9. It is inert
> and gitignored — use `.venv311`. If your IDE points at `.venv`, point it at `.venv311`.

---

## C. API only

```bash
uvicorn src.api.main:app --port 8000            # module: src/api/main.py, FastAPI app
```

The 12 endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | service banner + artefact state |
| GET | `/health` | liveness + artefact paths |
| GET | `/stats` | stage timings and counts from `pipeline_summary.json` |
| GET | `/alerts?min_risk=&limit=&entity_type=` | ranked alerts (**summaries**) |
| GET | `/alerts/{alert_id}` | one alert with its **full evidence package** |
| GET | `/graph` | whole graph (node-link payload) |
| GET | `/graph/{node_id}?depth=1..3` | neighbourhood subgraph around a node |
| GET | `/ledger?limit=` | raw ledger lines |
| GET | `/ledger/verify` | recompute hashes and compare against the ledger |
| POST | `/upload` | store CSV/JSON/JSONL/XML evidence files |
| POST | `/run` | start the pipeline as a background job |
| GET | `/status/{job_id}` | job progress (`stage 3/8`) and result |

---

## 8.1 Dashboard tour

The dashboard is one Streamlit page with a **sidebar** and **four tabs**. It prefers the live API and
falls back to the artefact files, so it never shows an empty screen just because the API is down.

### Sidebar (controls)

| Control | What it does |
|---|---|
| **status pill** | `API · live` or `artefacts · filesystem` — which source the data came from |
| **Input data** | `Bundled synthetic sample` · `Uploaded files (data/raw)` · `Regenerate synthetic sample first` |
| **▶ Run Analysis** | runs the full pipeline on the selected input, inside the app, and reports stages |
| **Upload evidence files** | accepts CSV/JSON/JSONL/XML; **Store uploads** writes them to `data/raw/upload_<timestamp>/` |
| **Minimum risk score** | slider filter over the alert list (default 0.50) |
| **Entity type** | filter to wallets, transactions, or both |
| **Search entity or reason** | free-text search across entity ids *and* reason text |

### Tab 1 — 🎯 Ranked Alerts

- **Ten metric tiles** across the top: totals, counts above thresholds, seed exposure, chains, mixes,
  clusters, mean confidence, correlation rate.
- A **ranked table** of every alert (60 in the demo) with risk, confidence, entity type, top reason.
- A **detail card** for the selected alert: reasons in plain English, the six score components as bars,
  the top contributing features with their values, the correlated network records, the related
  transactions, the risk paths back to a seed, and the recommended action.
- **CSV download** of the filtered alert list for a report.

### Tab 2 — 🕸 Interactive Graph

- **Colour nodes by**: *Risk score*, *Ownership cluster*, or *Node type*.
- **Max nodes drawn** slider and a **Hide nodes below risk** slider keep the picture readable.
- **Find a node** box: pasting an address or txid puts it in focus.
- Plotly-rendered network graph; hovering a node shows **risk, cluster id and degree**.
- **Focus mode** — clicking a node lights that node and its **direct** connections (behind a coloured
  halo) and fades the rest; **Hop to a connected node** (dropdown) or a row of the **Direct connections**
  table walks one hop further, **⬅ Back one hop** retraces the walk, and **🔄 Reset the graph** (always
  enabled) clears the focus, empties the search box and hands back a chart with nothing selected. The
  focused node's own **timeline** (connections over time, sized by amount) and its alert list appear
  below the chart.
- **Edge direction** — every lit edge is drawn with an arrowhead pointing the way the money moved:
  `wallet → txid` means that wallet spent in, `txid → wallet` means the transaction paid that wallet. And
  **▶ Animate the money flow** inside the chart plays a coin from each sender to each receiver, sized by
  the amount. It sits in the chart's **bottom-right** corner on purpose — the top-right is where plotly
  draws its own zoom/pan/select toolbar on hover, and the two used to land on the same pixels. Node
  *size*, in both cases, tracks degree (connections) — not amount.
- **⏱ Timeline replay** (toggle) hands the picture to a **timeline slider under the chart**. The
  snapshots are frames inside the plotly figure, so dragging the slider (or clicking the strip) scrubs the
  collection window **in place**: no rerun, no chart re-render, no page reload — the graph grows and
  shrinks under the cursor and the card in the corner names the moment, the node count and the alert
  count. It is deliberately manual: the first version auto-played the window in ~3 seconds (unreadable),
  the second drove it from a Streamlit slider (every drag step re-rendered the chart).
- **If a click seems to do nothing** — start the dashboard with `CHAINTRACE_FOCUS_DEBUG=1`: the
  **🐞 tap log** under the chart says whether the browser ever sent a selection (`sel=-` = it did not, and
  it will do the same for any focus logic). `Find a node`, the hop dropdown and the connections table
  always work. Part 10 has the full checklist.
- **Download** the current view as HTML (just the focus neighbourhood when one is active).

This tab is the visual proof that the pipeline and the ML are connected: node colour is `risk_score`
straight out of the risk stage, and cluster colour is `cluster_id` straight out of the clustering stage,
both written into `graph.json` at the end of the run. (They were *not* — the export happened before the
annotations were applied, so every node rendered as risk 0.000 / cluster "-". Fixed, with a regression
test.)

Three more things this tab has taught us, all fixed with regression tests:

- The replay used to be driven from a **Streamlit slider**, and every step of a drag was a rerun that
  re-rendered the whole chart — on a few hundred nodes that is a visible page reload. It now scrubs inside
  the figure (frames + a plotly slider), and the test asserts no seek widget exists in the page at all.
- The caption said **"scroll to zoom"**. Streamlit only turns scroll-zoom on for 3d/geo/mapbox charts, so
  the wheel over this 2D graph scrolls the page; the caption and the tutorial now point at the toolbar
  (and at double-click to reset).
- The payload carried **two spellings of the same timestamp**: `str(pd.Timestamp)` writes
  `2026-08-23 00:21:23.358190` (space) while `.isoformat()` writes `2026-08-23T00:21:23.358190` (T).
  String comparison is how the seek bar orders its ticks *and* applies its `<=` cutoff, and `'T'` sorts
  after every digit — so "step back one event" could move the clock **forward**, and the end of the bar
  held a mid-month instant. `src/utils/helpers.sortable_stamp()` normalises both spellings on read,
  `src/graph/builder.py` now writes one spelling, and `tests/test_xfactors.py` pins both.
- The auto-play replay loop rendered ~24 charts in a row at 100 ms each and could not be stopped. It is
  gone: the seek bar is manual by design (`tests/test_dashboard.py` asserts no play button exists).

### Tab 3 — 🔒 Evidence & Integrity

- Ledger summary: **entries / verified / mismatched**, with a pass/fail banner.
- Per-alert **Recompute SHA-256** button — watch it verify live.
- Raw ledger lines so an auditor can see the format.

Verified demo state: **entries: 60 · verified: 60 · mismatched: 0 · ✔ ledger consistent**.

---

## 8.2 Reading the outputs

Everything lands under `data/`:

| File | What it is | Read it when |
|---|---|---|
| `synthetic/transactions.csv`, `network_metadata.csv` | the input | you want to see raw data |
| `synthetic/planted_patterns.json` | the ground truth / answer key | you want to grade the detectors |
| `artifacts/pipeline_summary.json` | per-stage timings, counts, top-15 alert preview | you want one file that describes the run |
| `artifacts/alerts.json` | `{generated_at, count, alerts:[...]}` — the full ranked list with evidence | you want the findings |
| `artifacts/graph.json` | node-link payload (nodes, edges, stats) with `risk_score`/`cluster_id` per wallet | you are building a visualisation |
| `artifacts/wallet_features.csv`, `tx_features.csv` | the 27 / 24 feature matrices | you want to audit any number in an alert |
| `artifacts/correlated_records.csv` | every (network record, transaction) match with confidence | you are checking stage 3 |
| `artifacts/unified_*.csv` | exactly what the parser understood | an upload looks wrong |
| `artifacts/explainer_meta.json` | which explainer ran + model importances | someone asks "is this real SHAP?" |
| `evidence_ledger.jsonl` | the append-only hash ledger | you need integrity evidence |
| `models/*.joblib` + `training_metadata.json` | trained anomaly models | you want to reuse the models |

### The alert object (one wallet alert, abridged from the demo)

```json
{
  "alert_id": "CT-W-646B8580",
  "entity": "bc1qgxznuzhg9j56tvgh32s7j48ayfweh9eqs8ahsc",
  "entity_type": "wallet",
  "risk_score": 0.95,
  "confidence": 1.0,
  "explainer": "importance_surrogate",
  "recommended_action": "Escalate immediately - sanctioned/known-bad wallet",
  "components": {"peel": 0.7742, "mixing": 0.0, "anomaly": 0.9886,
                 "risk": 1.0, "cluster": 1.0, "correlation": 1.0},
  "reasons": ["Wallet is on the known-illicit seed list ...", "..."],
  "top_features": [{"feature": "risk_pagerank", "contribution": 0.3436, "value": 1.0}, "..."],
  "evidence": {
    "is_seed_wallet": true, "hops_to_seed": 0,
    "pagerank_risk": 1.0, "proximity_risk": 1.0,
    "risk_paths_to_seed": [["bc1qgxzn...", "1hJhT...", "bc1q8a8..."]],
    "cluster": {"cluster_id": 1017, "size": 30, "method": "louvain", "tainted": true},
    "peel_chain": {"chain_id": "PC-01", "chain_length": 7, "hop": 1, "reaches_seed": true,
                   "total_value_moved": 787.5145, "mean_asymmetry": 0.8680},
    "financials": {"total_received": 15.3542, "total_sent": 8.6377, "n_transactions": 12,
                   "first_seen": "2026-08-23T21:24:44.905381", "last_seen": "2026-09-19T06:57:19.314595"},
    "network_evidence": [{"src_ip": "103.251.167.20", "geo_country": "SC",
                          "correlation_method": "txid_exact", "correlation_confidence": 1.0}],
    "related_transactions": ["...up to 10 rows with role, amounts, peel score..."]
  },
  "evidence_hash": "c90d7664e54096960ac988573a442b622daa0da108a32812f601b3769a19d68c"
}
```

A transaction alert has the same envelope with a transaction-shaped `evidence` block
(`num_inputs`, `num_outputs`, `total_in`, `total_out`, `fee_ratio`, `anomaly_score`, `is_peel_hop`,
`is_mixing`, `correlation`, `addresses`).

### The summary file

`pipeline_summary.json` is the single best file to open first. It contains `duration_seconds`, `counts`
(transactions, wallets, clusters, chains, alerts…), the `artifacts` paths, a `stages` array with the
timing and detail of every stage, and `alerts_preview` with the top 15 alerts.

Real values from the demo: 1,200 transactions · 308 network records · 141 correlated pairs · 516 wallets ·
1,872 graph nodes · 4,220 edges · 32 clusters · 5 peel chains · 3 mixing transactions · 72 anomalies
flagged · 60 alerts · 8 seeds · **2.6 s pipeline** (plus ~4 s to generate the data and train the models).

---

## 8.3 Other useful commands

```bash
python scripts/export_graph_html.py
#   → writes a standalone pyvis HTML page you can open with no server at all
#     (useful when the laptop running the demo has no browser access to localhost)

python -m pytest -q
#   60 tests in ~9 s, all sandboxed away from ./data

python scripts/audit_dataset.py
#   32 checks: minimum fields, size envelope, planted patterns, value integrity,
#   address/port/geo realism, bulk-file integrity

python scripts/validate_detections.py
#   precision/recall vs planted_patterns.json
```

Continue to **[Part 9 — Tuning knobs](09_TUNING.md)**.
