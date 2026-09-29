# Part 1 — Overview, the data-flow picture, and every file explained

## 1.1 The problem in one diagram

```
                     INVESTIGATOR INPUT
        ┌────────────────────────────────────────────┐
        │ blockchain-ish records      network capture │
        │ transactions.csv            network_meta... │
        │ (txid, inputs, outputs,     (timestamp,     │
        │  amounts, fee, script)       src/dst IP,    │
        │                              ports, txid)   │
        └───────────────┬─────────────────────────────┘
                        │
                        ▼
   STAGE 1  INGEST      parse CSV / JSON / XML, normalise column names, dedupe
                        │
                        ▼
   STAGE 2  ENRICH      attach geography + ASN to each IP (offline table, no API calls)
                        │
                        ▼
   STAGE 3  CORRELATE   network record  ⟷  transaction
                        ├── exact TXID match            (confidence 1.00)
                        └── ±90 s time window match     (confidence 1.00 → 0.50, minus ambiguity penalty)
                        │
                        ▼
   STAGE 4  GRAPH       one NetworkX MultiDiGraph:
                        wallet ──in──▶ txid ──out──▶ wallet      (+ ip evidence nodes)
                        │
                        ▼
   STAGE 5  ML          ┌─ features      ~24 tx features, ~27 wallet features
                        ├─ clustering    common-input ownership (hard) + Louvain (soft)
                        ├─ anomaly       IsolationForest + LOF blend
                        ├─ peeling       peeling chains + CoinJoin shapes
                        └─ risk          Personalized PageRank + hub-damped proximity + cluster inheritance
                        │
                        ▼
   STAGE 6  EXPLAIN     rank, write plain-English reasons, name the top contributing features
                        │
                        ▼
   STAGE 7  SERVE       FastAPI (JSON)  +  Streamlit dashboard (human console)
                        │
                        ▼
   STAGE 8  EVIDENCE    canonical-JSON → SHA-256 per alert → append-only ledger file
```

Two things make this more than a script collection:

1. **Every alert is traceable.** It carries the detector scores that produced it, the feature
   contributions, the graph paths back to a seed wallet, the correlated network records, and the
   related transactions.
2. **Every alert is tamper-evident.** The evidence package is serialised canonically, hashed, and
   appended to a ledger; the dashboard can recompute and verify it in front of you.

---

## 1.2 Repository map

```
chaintrace-ai/
├── Dockerfile                  image definition (multi-stage-ish, pip-cached, offline warm-up)
├── docker-compose.yml          one service, two ports: 8000 API / 8501 dashboard
├── entrypoint.sh               container start script: ensure dirs → generate data → run pipeline → boot API + UI
├── requirements.txt            pinned core stack (API + dashboard + ML)
├── requirements-shap.txt       optional extra: real KernelSHAP explanations
├── .dockerignore / .gitignore  keep venvs, raw uploads, caches out of image and repo
├── .streamlit/config.toml      dashboard theme + disables usage stats (offline requirement)
├── README.md                   the team-facing write-up, split into six parts (one per person)
├── GUIDE.md                    this guide's index
├── docs/                       the guide's parts (you are in Part 1)
│
├── app/
│   └── dashboard.py            Streamlit console: ranked alerts, interactive graph, evidence tab
│
├── src/                        the pipeline itself (importable, tested)
│   ├── config.py               every constant and every path in one place
│   ├── data/
│   │   ├── generator.py        synthetic dataset with planted, ground-truth-labelled patterns
│   │   ├── parsers.py          CSV/JSON/XML → normalised DataFrames (tolerant, dedupes, drops junk rows)
│   │   └── enrich.py           offline GeoIP/ASN enrichment (local table, optional MaxMind .mmdb)
│   ├── correlation/
│   │   └── correlator.py       network record ⟷ transaction matching + confidence
│   ├── graph/
│   │   └── builder.py          the MultiDiGraph, wallet projection, subgraphs, node-link export
│   ├── ml/
│   │   ├── features.py         24 tx features + 27 wallet features (the model input)
│   │   ├── clustering.py       Union-Find common-input ownership + Louvain communities
│   │   ├── anomaly.py          IsolationForest + LOF, blended and rank-normalised
│   │   ├── peeling.py          peeling-chain walks + CoinJoin shape detector
│   │   ├── risk.py             PageRank + hub-damped proximity + cluster inheritance
│   │   └── explain.py          alert construction, reason lines, feature attribution, ranking
│   ├── pipeline/
│   │   └── runner.py           orchestrates stages 1→8, writes artefacts + summary
│   ├── api/
│   │   ├── main.py             FastAPI app factory, optional auto-run on boot, static file serving
│   │   ├── routes.py           the 12 endpoints + background job handling
│   │   └── schemas.py          Pydantic response models (the API contract)
│   └── utils/
│       ├── helpers.py          logging, Timer, clamp, json_safe, formatting
│       └── hashing.py          canonical JSON, SHA-256 evidence hashes, ledger read/write/verify
│
├── scripts/                    thin command-line front doors (no logic lives here)
│   ├── generate_sample_data.py build data/synthetic/* from the generator
│   ├── audit_dataset.py        the 32 machine-checked dataset requirements vs the SIH26146 brief
│   ├── run_full_pipeline.py    run stages 1→8 and print a summary
│   ├── train_models.py         fit + persist the two anomaly models to data/models/
│   ├── validate_detections.py  score the detectors against the ground-truth manifest
│   ├── benchmark_detectors.py  the rules baseline the ML has to beat (X-factor C)
│   ├── deploy_hf_space.sh      publish the current commit to a Hugging Face Space (Part 14)
│   └── export_graph_html.py    standalone offline pyvis page (no server required)
│
├── tests/                      65 tests, all hermetic (see conftest.py)
│   ├── conftest.py             sandboxes every data path into a tmp dir (tests can never touch the demo)
│   ├── test_pipeline.py        19 tests: generator, parsers, enrich, correlate, graph, ML, alerts, ledger, API
│   ├── test_dashboard.py       6 tests: the four screens, focus mode, the manual timeline seek,
│   │                               and the dataset downloads on Screen 1
│   ├── test_focus.py           23 tests: the focus ladder, fade, money-flow rows, timeline stats
│   ├── test_dataset_contract.py 4 tests: a freshly generated dataset still satisfies the SIH26146 brief
│   ├── test_xfactors.py        9 tests: the X-factors A–G (ledger, case files, benchmark, replay, infra)
│   └── test_bootstrap.py       4 tests: the shell-less warm-up and the hosted entrypoint
│
└── data/                       everything the pipeline reads and writes (dataset tracked, derived files ignored)
    ├── synthetic/              generated input: transactions.csv, network_metadata.csv, seeds, planted_patterns.json
    ├── raw/                    uploaded files land here, one folder per upload
    ├── geo/                    mock_geoip.json (the offline enrichment table cache)
    ├── models/                 trained .joblib models + training_metadata.json
    └── artifacts/              pipeline output consumed by the API and dashboard
    └── evidence_ledger.jsonl   the append-only SHA-256 ledger
```

---

## 1.3 Every file, explained

### Top-level packaging

| File | What it does | Why it exists |
|---|---|---|
| `Dockerfile` | Python 3.11-slim image. Installs the pinned stack, copies `src/ app/ scripts/ tests/`, runs `generate_sample_data.py` + `run_full_pipeline.py` + `train_models.py` at **build time** so the first container start is instant and the image is genuinely self-contained. | `docker compose up --build` has to produce a working demo with zero extra steps. Warming the data at build time makes the demo deterministic, not dependent on the machine. Uses a BuildKit `--mount=type=cache` for pip so interrupted builds resume instead of re-downloading ~150 MB. |
| `docker-compose.yml` | One service `chaintrace`. Maps `8000` (API) and `8501` (dashboard), mounts `./data`, sets `CHAINTRACE_RUN_ON_START=auto`, healthchecks `/health`. | The one-command demo. The `./data` mount is deliberate: artefacts survive container restarts, so a demo never starts from an empty console. |
| `entrypoint.sh` | Start-up sequence: create dirs → generate the sample if `data/synthetic` is empty → run the pipeline if `data/artifacts` is empty → start uvicorn → start Streamlit. | Makes "first run" and "restart" behave the same. Passes `bash -n`. |
| `requirements.txt` | numpy, pandas, scipy, scikit-learn, networkx, fastapi, uvicorn, streamlit, pyvis, requests, python-multipart, joblib, pytest. | Pinned versions; `python-multipart` is required by the upload endpoint and is easy to forget. |
| `requirements-shap.txt` | Just `shap`. | Deliberately **optional**: shap forces numpy≥2, which breaks the pinned scikit-learn stack. `INSTALL_SHAP=1` at build time switches the top alerts from an importance surrogate to real KernelSHAP. |
| `.streamlit/config.toml` | Theme colours + `gatherUsageStats = false`. | The offline claim would be a lie if Streamlit phoned home. |
| `README.md` | The write-up for the six-person team, one part per role. | Answers "who built what, and how do I verify *my* slice". |
| `GUIDE.md` + `docs/` | This guide. | Answers "explain all of it to me like I'm new". |

### `src/config.py` — the single source of truth (182 lines)

Every constant and every path lives here, so no module hard-codes a number or a location.

- **Paths**: `DATA_DIR` (overridable with the `CHAINTRACE_DATA_DIR` env var), then `RAW_DIR`,
  `SYNTHETIC_DIR`, `GEO_DIR`, `MODEL_DIR`, `ARTIFACT_DIR`, `LEDGER_PATH`, `GRAPH_PATH`, `ALERTS_PATH`,
  `SUMMARY_PATH`, and the per-artefact feature CSVs.
- **Dataset shape**: `DEFAULT_TX_COUNT=1200`, `DEFAULT_WALLET_COUNT=450`, `DEFAULT_NETWORK_COUNT=320`,
  plus the planted-pattern counts (3–6 peel chains, 5–8 seeds, 2–3 CoinJoins, 8–14 anomalies, 3–6 clusters).
- **Correlation**: `TIME_WINDOW_SECONDS=90`, `CORRELATION_EXACT_CONFIDENCE=1.0`,
  `CORRELATION_CONFIDENCE_FLOOR=0.5`.
- **Clustering**: `COMMON_INPUT_MAX_INPUTS=5`, `USE_LOUVAIN_REFINEMENT=True`, `LOUVAIN_RESOLUTION=1.0`,
  `CLUSTER_SIZE_WARN=6`, `CLUSTER_SIZE_REPORT_MAX=50`.
- **Risk**: `RISK_DECAY=0.7`, `RISK_BFS_MAX_HOPS=6`, `HUB_DEGREE=15`, `RISK_WEIGHT_PAGERANK=0.6`,
  `RISK_WEIGHT_PROXIMITY=0.4`, `CLUSTER_RISK_DISCOUNT=0.85`, `CLUSTER_INHERIT_MAX_SIZE=30`.
- **Alerts**: `MIN_ALERT_RISK=0.50`, `MAX_ALERTS=60`, `ALERTS_PER_CATEGORY=2`, `SCORE_WEIGHTS`,
  `SEVERITY_LADDER`.
- **API/UI**: ports, `API_URL`, `API_TIMEOUT_SECONDS`, `RUN_PIPELINE_ON_START` (`auto|always|never`).
- `ensure_directories()` creates every directory idempotently.

**This is the file to open first when explaining the system to someone.**

### `src/data/generator.py` — the synthetic dataset (787 lines)

Builds an investigable world on purpose, and writes down what it planted.

- `SyntheticDataGenerator` seeds one `random.Random(42)` → the whole dataset is reproducible.
- Wallet pool has **roles**: `user`, `hub` (exchange-like, many counterparties), `cashout` (reused peel
  destination), `illicit_seed`.
- Planted pattern families, each recorded in `ground_truth`:
  `_plant_peeling_chains` (chains whose hops move strictly forward in time, value continuity preserved,
  ~35% of peels land in a seed wallet), `_plant_seed_activity`, `_plant_clusters` (repeated co-spending
  groups; group 0 is a laundering ring that also touches a seed), `_plant_anomalies` (six families:
  extreme fee ratio, very large amount, unusual script, rapid burst, extreme fan-in, extreme fan-out),
  `_plant_mixing` (CoinJoin-shaped transactions).
- `_background_activity` fills the remaining transaction budget with ordinary-looking traffic, so the
  detectors must find needles in hay.
- `_build_network_records` creates 308 records: ~42% deliberately correlated with an *interesting*
  transaction (59% of those carry the TXID, the rest fall inside the 90 s window), the remainder is noise.
  Every record carries `geo_country`/`geo_asn`, resolved through the same offline table stage 2 uses.
- `write()` emits `transactions.csv`, `network_metadata.csv`, the combined `bulk_metadata.csv`,
  `seed_illicit_wallets.json` and `planted_patterns.json` (the answer key).

**Why it exists:** SIH-type problems ship no dataset, and a detector you cannot score is a detector you
cannot defend. The generator gives the project a measurable ground truth.

### `src/data/parsers.py` — tolerant input normalisation (417 lines)

- Accepts **CSV, JSON, JSONL/NDJSON and XML** (`read_records`, `_read_xml`).
- `guess_kind()` labels each file `transactions`, `network` or **`mixed`** — a single bulk export holding
  both layers — and a mixed file is split row by row (the same packet arriving in two files is
  de-duplicated) instead of losing half its columns.
- `split_list` understands `a|b`, `a,b`, `["a","b"]`, `['a','b']`, and scalars — so an uploaded file does
  not have to match our column layout.
- `split_amounts` pads/truncates amount lists to match address counts (a very common broken-data case).
- `parse_timestamp` handles ISO strings, epoch seconds and epoch milliseconds.
- `normalize_transactions` / `normalize_network` map aliases (`from_addresses`, `source_ip`,
  `hash`, `transaction_id`, `tx_id`, `proto`, ...) onto the canonical columns
  `timestamp, txid, inputs, outputs, input_amounts, output_amounts, fee, script_type, src_ip, dst_ip, src_port, dst_port`.
- Rows that cannot be salvaged are **dropped with a warning, never crash the run**.

### `src/data/enrich.py` — offline geography (258 lines)

- `GEO_TABLE` is a deterministic table of 16 hosting/VPN jurisdictions (NL, DE, US, SG, RU, CN, SE, PA,
  SC, IN, GB, BR, UA, TR, HK, CH) with ASN + organisation.
- `GeoIPEnricher` uses a local MaxMind `.mmdb` **if present**, otherwise falls back to a deterministic
  hash-based lookup into that table — same IP always resolves to the same country/ASN, forever.
- Private ranges (`10/8`, `172.16/12`, `192.168/16`, `127/8`) are flagged, not geolocated.
- `enrich_network` adds `geo_country`, `geo_asn`, `asn_org`, `ip_version`, `src_is_private`, and a
  `cross_border` flag (source and destination country differ).
- `persist_mock_mapping` writes `data/geo/mock_geoip.json` so the mapping is inspectable and cacheable.

**Why offline:** no API keys, no rate limits, no internet at demo time — and results stay reproducible.

### `src/correlation/correlator.py` — joining two worlds (176 lines)

- `correlate(transactions, network)` returns one row per (network record, transaction) pair.
- **Path 1 — `txid_exact`**: the record carries a TXID that exists in the transaction table →
  confidence `1.0`.
- **Path 2 — `time_window`**: no TXID, so look for transactions within `±90 s` using a sorted timeline
  (binary search, not a nested loop) and take the closest one. Confidence decays linearly from `1.0` at
  the same instant to `CORRELATION_CONFIDENCE_FLOOR=0.5` at the window edge, then is multiplied by an
  ambiguity penalty `1/sqrt(number_of_candidates)` — if twenty transactions sit inside the window, the
  match is honestly reported as weak.
- `per_transaction_correlation` keeps the best match per TXID (that is what the feature engineer and the
  alerts use). `correlation_summary` produces the stage-3 numbers for the summary file.

### `src/graph/builder.py` — the single graph everything else reads (504 lines)

- `GraphBuilder(transactions, correlated, seeds).build()` produces a `networkx.MultiDiGraph` and
  returns `self` so calls chain (`FeatureEngineer(GraphBuilder(...).build())` works).
- **Node types**: `wallet`, `txid`, `ip`. **Edge types**: `in` (wallet → txid), `out` (txid → wallet),
  and IP-evidence edges. Each edge carries `amount` and `timestamp`; each wallet node accumulates
  in/out totals, degree, counterparty counts, first/last seen, max fan-in/fan-out.
- `tx_index` is the fast lookup `{txid: {inputs, outputs, amounts, fee, ...}}`; `rebuild_tx_index()`
  reconstructs it from the graph alone when only a graph was passed in.
- `wallet_projection()` collapses the graph to wallet→wallet edges (cached — this is what made the run
  go from 45 s to ~7 s), and `wallet_projection_undirected()` gives the undirected view used by
  clustering and risk.
- `get_subgraph(seed_nodes, depth)` / `get_neighbourhood(node, depth)` power the API's `/graph/{node_id}`.
- `to_node_link(...)` and `load_graph_payload(...)` write/read the browser-ready `graph.json`
  (capped at `MAX_GRAPH_NODES_FOR_EXPORT=6000` so the payload stays small enough to render).
- `as_builder(candidate)` accepts either a `GraphBuilder` **or** a bare graph, which is what lets the ML
  functions be called both in the pipeline and in isolation (a trap the tests caught early).

### `src/ml/features.py` — the model input (361 lines)

- **24 transaction features**: `num_inputs, num_outputs, total_input, total_output, fee, fee_ratio,
  amount_ratio, out_amount_cv, in_amount_cv, io_ratio, is_1in_2out, is_1in_1out, is_consolidation,
  is_fan_out, dust_outputs, asymmetry, log_total_input, script_type_encoded,
  has_network_correlation, correlation_confidence, n_network_records, mean_time_delta_seconds,
  src_geo_is_offshore, is_coinjoin_shape`.
- **27 wallet features**: degree and transaction counts, received/sent/net flow, unique counterparties,
  amount statistics, lifetime and throughput (`tx_per_hour`), `burstiness` (max transactions inside one
  60-second window), `max_fan_in/fan_out`, `fraction_of_peel_like_txs`, `peel_chain_length`,
  `mixing_participations`, cluster size/membership, `pagerank`, `risk_pagerank`, `risk_proximity`,
  `hops_to_seed`, `n_correlated_ips`, `distinct_source_countries`, `is_seed`.
- `OFFSHORE_COUNTRIES = {PA, SC, RU, CN, HK, TR, UA}` — the "same host, suspicious jurisdiction" angle.
- `save()` writes `wallet_features.csv` and `tx_features.csv` so every number in an alert can be audited
  in a spreadsheet.
- It **raises a clear error** if handed a bare graph with no transaction rows, instead of failing deep
  inside with an `AttributeError`.

### `src/ml/clustering.py` — entity resolution (264 lines)

Covered in depth in **Part 4**. In one line: Union-Find over co-spent inputs (hard ownership evidence,
with merge guards) + Louvain communities on the wallet projection (soft behavioural grouping), with the
hard label winning and `cluster_method` recording which one produced each label.

### `src/ml/anomaly.py` — unsupervised outlier detection (268 lines)

- `AnomalyDetector(scaler=True)` fits a `StandardScaler`, an `IsolationForest`
  (`contamination=0.06`, `n_estimators=200`) and, if `USE_LOF_MODEL`, a `LocalOutlierFactor(n_neighbors=20)`.
- Scores are rank-normalised and blended (`LOF_WEIGHT=0.35`), then flags are set at the
  `ANOMALY_FLAG_QUANTILE=0.94` quantile — the top ~6% of transactions.
- `feature_importance()` measures each feature's contribution by **permutation** (shuffling a column and
  watching the score move), which is model-agnostic and needs no extra dependency.
- `save()` / `load()` persist to `data/models/*.joblib`; `scripts/train_models.py` does this at build time.

**Why two models:** IsolationForest is good at global outliers (an absurd fee), LOF is good at local
outliers (a transaction that is normal-looking next to the global population but bizarre next to its
neighbours). Blending them catches both kinds of "weird".

### `src/ml/peeling.py` — structure detection (375 lines)

Covered in depth in **Part 5**:

- `detect_peel_chains`: candidate hop = exactly 1 input, exactly 2 outputs, asymmetry
  `larger/(larger+smaller) > 0.75`, and the peeled output is not dust (`>3%` share). Then it enumerates
  **all** forward walks from every candidate, keeps only walks of ≥3 hops, sorts by length and value,
  and assigns walks to chains so that no hop belongs to two chains. The next hop is chosen by **value
  continuity** — the spend whose input amount is closest to what the previous hop forwarded, with
  timestamps strictly increasing.
- Chain score = `0.40·length + 0.25·asymmetry + 0.20·value + 0.15` seed bonus (if it starts at or
  routes into a seed). Earlier hops score slightly higher than later hops (`1 - 0.04·hop`).
- `detect_mixing`: CoinJoin shape test — ≥5 inputs, ≥5 outputs, output coefficient of variation
  `< 0.15` (near-equal outputs), and inputs must be distinct addresses (otherwise it is one owner
  recycling coins, not a mix). Score blends output uniformity (0.6) with input count (0.4).

### `src/ml/risk.py` — how badness spreads (316 lines)

Covered in depth in **Part 6**:

- `_personalized_pagerank`: `nx.pagerank` with a personalization vector over the seeds, normalised so
  the highest wallet is 1.0. Structural influence through the transaction graph.
- `_hub_damped_decay`: multi-source Dijkstra over the wallet projection where each edge costs
  `-log(decay)·(1 + hub_penalty(u) + hub_penalty(v))`, and `hub_penalty = log1p(degree/HUB_DEGREE)` for
  wallets above 15 counterparties. So proximity is `decay^hops` along ordinary chains and decays much
  faster when the path runs through an exchange-like hub — sharing a hub with a criminal is weak
  evidence; a direct payment chain is strong evidence.
- Wallet risk = `0.6·pagerank + 0.4·proximity`, seeds pinned at `1.0`.
- `propagate_cluster_risk`: wallets that share a **common-input** owner inherit `0.85 × the cluster's
  peak risk` — but only inside non-collapsed, evidenced clusters (Part 4).
- Transaction risk = `0.55 × highest touching wallet risk + 0.45 × own detector evidence`.
- `explain_risk_path` returns actual shortest paths from a wallet back to a seed, which is what the
  alert's `risk_paths_to_seed` field shows.

### `src/ml/explain.py` — turning scores into an analyst's alert (830 lines)

- `AlertExplainer` gathers every candidate entity (wallets above threshold, transactions flagged by any
  detector), scores them, writes the reasons, attaches attribution, and ranks.
- `_reason_lines` is the plain-English layer: seed membership, peeling-chain membership including chain
  id, length, hop number and whether it starts/reaches a seed, CoinJoin score, anomaly percentile, hops
  to seed, cluster size and cluster-taint, correlated network records with countries and best
  confidence, high fee ratio, burst behaviour.
- `_score_components` produces `{peel, mixing, anomaly, risk, cluster, correlation}`; `_composite`
  blends them with `SCORE_WEIGHTS` and then applies a **severity ladder** so one corroborated strong
  signal cannot be diluted by the absence of the others.
- `_confidence` reports how much independent corroboration sits behind the alert.
- `_recommended_action` maps score/hops/seed status to an action ladder (monitor → investigate →
  escalate → escalate immediately).
- `_property_contributions` names the top contributing features per alert, using real **KernelSHAP**
  when `shap` is installed, otherwise a clearly-labelled importance surrogate. The field `explainer`
  records which one was used, so nothing is misrepresented as SHAP that is not SHAP.
- `_ensure_diversity` guarantees each detector family (`SECURITY`, `ANOMALY`, `MIXING`, `LOOP`) keeps at
  least `ALERTS_PER_CATEGORY` slots, so a single dominant family cannot crowd the others out of the
  ranked list — an investigator must not lose the only CoinJoin alert to fifty peeling hops.
- `_attach_shap` upgrades the top `SHAP_MAX_ALERTS=12` alerts when SHAP is available.

### `src/pipeline/runner.py` — the conductor (436 lines)

`run_full_pipeline(input_dir, output_dir, seeds_file, progress, seed)` executes stages 1→8 in order,
timing each one into a `PipelineContext` and writing:

- `artifacts/graph.json` (node-link payload for the browser)
- `artifacts/alerts.json` (`{generated_at, count, alerts:[...]}`)
- `artifacts/pipeline_summary.json` (per-stage timings + counts + status)
- `artifacts/wallet_features.csv`, `tx_features.csv`, `correlated_records.csv`, `unified_*.csv`
- `artifacts/explainer_meta.json` (which explainer ran, how many alerts got real SHAP)
- `evidence_ledger.jsonl` (one SHA-256 line per alert)

It accepts a `progress` callback, which is how `POST /run` reports `stage 3/8` to the API. It also
returns an empty-but-valid summary (`_empty_summary`) rather than crashing if there is no input data,
so the dashboard can always render something.

### `src/api/` — the JSON surface

- `main.py` (94 lines): builds the FastAPI app, adds CORS, mounts the router, serves a small landing
  payload at `/`, and — depending on `CHAINTRACE_RUN_ON_START` (`auto` runs only when artefacts are
  missing) — can warm the pipeline on boot.
- `routes.py` (394 lines): **12 endpoints**.
  `GET /health`, `GET /stats`, `GET /alerts` (filters: `min_risk`, `limit`, `entity_type`),
  `GET /alerts/{alert_id}` (full evidence package), `GET /graph`, `GET /graph/{node_id}` (subgraph,
  `depth` 1–3), `GET /ledger`, `GET /ledger/verify`, `POST /upload` (CSV/JSON/JSONL/XML),
  `POST /run`, `GET /status/{job_id}`.
  A shared `_graph_response()` builder serves both graph forms — the earlier bug where the two paths
  returned different results is exactly why it exists.
- `schemas.py` (141 lines): the Pydantic contract — `HealthResponse`, `AlertSummary`, `AlertDetail`,
  `AlertListResponse`, `GraphNode/Edge/Response`, `StageTiming`, `StatsResponse`, `JobStatus`,
  `RunRequest`, `UploadResponse`, `LedgerVerification`. `/alerts` returns **summaries** (no `evidence`
  field) while `/alerts/{id}` returns the full package — deliberate payload control, and the reason the
  dashboard must fetch full packages before verifying hashes.

### `src/utils/`

- `helpers.py` (129 lines): `setup_logging`, `Timer` (context manager used by the stage timings),
  `clamp`, `safe_div`, `json_safe` (numpy/pandas/NaN → JSON types), `format_btc`, `utc_now_iso`,
  `chunked`, `top_n`, `suppress_stderr`, `humanise_seconds`.
- `hashing.py` (209 lines): `canonical_json` (sorted keys, no whitespace variance),
  `sha256_hex`, `hash_file`, `build_evidence_package`, `stamp_evidence_hash`, `verify_evidence_hash`,
  `build_ledger_entry`, `append_ledger_entry`, `write_ledger`, `read_ledger`, `verify_ledger`,
  `new_alert_id`. Deterministic and single-handle per entry — an earlier version wrote each entry twice
  through two file handles.

### `app/dashboard.py` — the console (717 lines)

Streamlit app with four tabs and a control sidebar. Detailed tour in **Part 8**. Key design point:
`load_bundle()` prefers the **live API**, and falls back to reading the artefact files directly, so the
dashboard works whether or not the API is up (`source: "api"` vs `"filesystem"` in the sidebar pill).

### `scripts/`

| Script | Command | What it does |
|---|---|---|
| `generate_sample_data.py` | `python scripts/generate_sample_data.py` | Writes `data/synthetic/*` (transactions, network, seeds, `planted_patterns.json`) |
| `run_full_pipeline.py` | `python scripts/run_full_pipeline.py` | Runs stages 1→8, prints per-stage timings and counts |
| `train_models.py` | `python scripts/train_models.py` | Fits + saves the transaction and wallet anomaly models |
| `validate_detections.py` | `python scripts/validate_detections.py` | Scores detectors against `planted_patterns.json` (precision/recall per typology) |
| `export_graph_html.py` | `python scripts/export_graph_html.py` | Writes a standalone pyvis HTML page — a graph artefact that needs no server |

### `tests/`

- `conftest.py` (56 lines): redirects **every** `data/` path (synthetic, artefacts, ledger, models, geo)
  into a pytest `tmp_path`. Before this existed, running `pytest` rewrote the demo's ledger from the
  test dataset and left the dashboard reporting "ledger needs attention". Tests are now hermetic: `data/`
  is byte-identical after a full test run.
- `test_pipeline.py` (379 lines): 19 tests — every planted pattern family is present and monotonic in
  time, generation is reproducible, parsers handle CSV/JSON/XML and drop broken rows, enrichment is
  offline and deterministic, correlation finds both TXID and window matches, graph helpers work,
  clustering recovers planted co-spenders, the anomaly model flags planted outliers, peeling recovers
  the planted chains, mixing finds the CoinJoins, risk pins seeds and propagates, alerts are explainable
  and ranked, evidence hashes verify and the ledger matches, the summary reports all stages, and the API
  endpoints (including both graph forms) respond correctly.
- `test_dashboard.py`: the four screens build without exceptions, the focus mode walks the ladder and
  the money-flow table reports the right counterparties, the serialized graph figure really carries the
  halo, the lit-vs-faded traces, the arrowhead annotations, the 12 animation frames and a manual
  timeline slider (and no auto-play button), and the whole app survives *missing* artefacts.
- `test_focus.py` (23 tests): the focus ladder, `advance_path`, the faded background, `flow_rows`,
  `route_hops` / `route_summary` and the timeline statistics — the pure functions behind Tab 2.
- `test_dataset_contract.py` (4 tests): a dataset generated from scratch at the specification's own
  default sizes still satisfies every requirement in `audit_dataset.py`, and a combined "bulk" export
  splits back into both layers without losing half its columns.
- `test_xfactors.py` (9 tests): the X-factors A–G — ledger integrity and tamper detection, case-file
  export, the rules baseline, replay frames, the infrastructure roll-up, counterfactuals, and the real
  Base58Check address encoding.
- `test_bootstrap.py` (4 tests): the warm-up a host with no shell step depends on — a bare checkout
  becomes a populated console, a warm copy costs nothing, a deleted dataset is regenerated, and
  `streamlit run app/cloud_app.py` renders the whole console from cold.

### `data/` — inputs, models, outputs

| Path | Produced by | Consumed by |
|---|---|---|
| `data/synthetic/transactions.csv` | generator | parsers (stage 1) |
| `data/synthetic/network_metadata.csv` | generator | parsers (stage 1) |
| `data/synthetic/seed_illicit_wallets.json` | generator | risk seeds (stage 5d) |
| `data/synthetic/planted_patterns.json` | generator | `validate_detections.py`, tests (never by the pipeline) |
| `data/raw/upload_<timestamp>/` | `POST /upload` or the dashboard | stage 1 when you run an uploaded-file analysis |
| `data/geo/mock_geoip.json` | enricher | inspectable cache of the offline lookup |
| `data/models/*.joblib` | `train_models.py` | stage 5 anomaly scoring (optional) |
| `data/artifacts/*` | pipeline | API + dashboard |
| `data/evidence_ledger.jsonl` | stage 8 | `/ledger`, `/ledger/verify`, dashboard Evidence tab |

---

## 1.4 The three "rules" that shape the whole design

1. **Never silently guess.** Every inference carries a confidence and a method name
   (`correlation_confidence` + `correlation_method`, `cluster_method`, `explainer`). If a number is
   inferred rather than observed, it says so.
2. **Never let one weak signal punch above its weight.** Hub damping, the ambiguity penalty on window
   matches, the cluster-discount factor, the merge guards and the cluster-collapse guard are all the same
   principle: bad evidence must not travel far.
3. **Never lose the audit trail.** Raw input → normalised row → graph edge → feature value → component
   score → reason line → ledger hash. Any alert can be walked backwards all the way to the CSV.

Continue to **[Part 2 — The dataset decoded](02_DATA.md)**.
