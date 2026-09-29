# Part 3 — How everything connects

This is the wiring diagram in words. Read it alongside `src/pipeline/runner.py`, which is the only file
that knows the order.

---

## 3.1 The entry points (who starts the pipeline)

There are exactly four ways the pipeline starts, and all four end up in the same function,
`src/pipeline/runner.py::run_full_pipeline`:

| Trigger | Path |
|---|---|
| Docker container start | `entrypoint.sh` → `python scripts/run_full_pipeline.py` |
| Manual run | `python scripts/run_full_pipeline.py` |
| API job | `POST /run` → `routes.py::run` → background thread → `_run_job` → `run_full_pipeline` |
| Dashboard button | sidebar **▶ Run Analysis** → `dashboard.py::_run_pipeline_now` → `run_full_pipeline` |

`run_full_pipeline(input_dir, output_dir, seeds_file, progress, write_artifacts)` is therefore the single
definition of "analysis". Everything else is packaging.

---

## 3.2 Stage by stage: input → call → output → next consumer

### Stage 1 — Ingest (`parsers`)

- **In:** a directory (`data/synthetic`, a `data/raw/upload_*` folder, or any directory you point it at)
  containing transactions and/or network files in CSV / JSON / JSONL / XML.
- **Call:** `parsers.load_input_bundle(input_dir)` → `(transactions_df, network_df, discovered_seeds)`
- **Inside:** every file is read (`read_records`), its columns are canonicalised (`TX_FIELDS`,
  `NETWORK_FIELDS` alias maps), list-ish fields are split (`split_list`), amounts are aligned to address
  counts (`split_amounts`), broken rows are dropped with a warning.
- **Also does:** `guess_kind()` decides whether an unlabelled file is transactions, network records or a
  **combined bulk export** (`mixed`) by looking for address columns vs `src_ip`/`dst_ip`; a mixed file is
  split row by row and the packet records are de-duplicated across the two views.
- **Out:** two normalised DataFrames + `unified_transactions.csv` / `unified_network.csv`
  (so you can see exactly what the parser understood).
- **Next consumer:** stage 2 (network), stage 3 (both).
- **Demo numbers:** 1,200 transactions, 308 network records.
- **Failure mode:** if no transactions could be ingested, the runner writes an `_empty_summary(...)` and
  returns — the dashboard still renders, it just has nothing to show.

### Stage 2 — Enrich (`enrich`)

- **In:** the network DataFrame.
- **Call:** `GeoIPEnricher()` then `enrich_network(network, enricher)`.
- **Inside:** local MaxMind `.mmdb` if present, else a deterministic hash lookup into `GEO_TABLE`
  (16 jurisdictions with ASN + organisation). Private ranges are flagged, never geolocated.
- **Out:** the same DataFrame plus `geo_country`, `geo_asn`, `asn_org`, `ip_version`, `src_is_private`,
  `cross_border`; plus an enrichment summary (countries seen, ASNs seen, cross-border count) and
  `data/geo/mock_geoip.json`.
- **Next consumer:** stage 3 (the geo columns travel with the correlation rows).
- **Demo numbers:** 308 records enriched (288 cross-border), 16 countries, 16 ASNs.

### Stage 3 — Correlate (`correlator`)

- **In:** normalised transactions + enriched network records.
- **Call:** `correlate(transactions, network)` → one row per matched pair.
- **Two matching paths, in priority order:**
  1. **`txid_exact`** — the record's TXID exists in the transaction table → confidence `1.0`.
  2. **`time_window`** — no TXID (or unknown TXID), so binary-search the sorted transaction timeline for
     candidates within `±90 s`; take the closest, apply linear confidence decay from 1.0 down to the
     `0.5` floor, then multiply by `1/sqrt(candidate_count)`.
- **Out:** rows carrying `timestamp, txid, time_delta_seconds, correlation_method,
  correlation_confidence, geo_country, geo_asn, asn_org, cross_border`, sorted by confidence.
- **Then:** `enrich_transactions_with_geo(transactions, correlated)` copies the *best* correlation per
  transaction onto the transaction rows (this is where `src_geo_is_offshore` comes from later), and
  `correlated_records.csv` is written.
- **Next consumer:** stage 4 (as graph edges/attributes) and stage 5a (as features).
- **Demo numbers:** 141 correlated pairs — 64 exact, 77 window — covering 131 unique transactions, mean
  confidence 0.78.

### Stage 4 — Graph (`builder`)

- **In:** transactions + correlated records + the seed wallet list
  (`_load_seeds(input_dir, seeds_file)`, falling back to seeds discovered during ingest).
- **Call:** `GraphBuilder(transactions, correlated, seeds).build()` → returns the builder (fluent).
- **Inside:** each transaction becomes a `txid` node; each input address becomes a `wallet` node with a
  `wallet → txid` edge of `edge_type="in"`; each output becomes a `txid → wallet` edge of
  `edge_type="out"`; correlated IPs become `ip` evidence nodes attached to their transaction. Wallet
  nodes accumulate in/out totals, degree, counterparties, first/last seen, max fan-in/fan-out.
- **Also builds:** `tx_index` (fast `{txid: {...}}` lookup), the cached wallet→wallet projection, the
  undirected projection, and `graph.json` (node-link, capped at 6,000 nodes).
- **Next consumer:** *almost everything downstream reads the graph*, not the CSVs — features (needs
  `builder.transactions` too), clustering, risk, explain.
- **Demo numbers:** 1,872 nodes / 4,220 edges — 516 wallets, 1,200 transactions, 156 IP nodes; density
  0.0011, average degree 4.29, one weakly-connected component, 6,383.30 BTC total value moved.

### Stage 5 — the ML block (six sub-stages, deliberately in this order)

**5a. Transaction features** (`FeatureEngineer(builder, correlated).build_transaction_features()`)
→ 24 features for all 1,200 transactions. Needs the builder because it needs raw transaction rows, not
just topology. **Feeds:** 5c (model input), 6 (attribution).

**5b. Entity clustering** (`cluster_entities(builder, transactions)`)
→ labels + methods + sizes for all 516 wallets. **Feeds:** 5e (risk inheritance), 5f (cluster_size
features), 6 (cluster evidence and cluster reason lines). *Explained fully in Part 4.*

**5c. Anomaly detection** (`detect_anomalies(tx_features, TX_FEATURE_COLUMNS, id_column="txid")`)
→ IsolationForest + LOF blended scores, flags at the 94th percentile. **Feeds:** 5e (transaction risk
blend), 6 (the anomaly component and the "99th percentile" reason line + attribution).

**5d. Peeling & mixing** (`detect_peeling_and_mixing(transactions, seeds)`)
→ peel chains with per-hop detail, per-transaction peel/mixing scores, per-wallet peel scores and mixing
counts. **Feeds:** 5e, 5f, 6. *Explained fully in Part 5.*

**5e. Risk propagation** (`propagate_risk(builder, seeds, anomaly_scores, peel_scores, mixing_scores,
cluster_labels, cluster_methods)`)
→ wallet risk, transaction risk, hop counts, PageRank/proximity components, and which wallets were raised
by cluster ownership. **Feeds:** 5f, 6. *Explained fully in Part 6.*

**5f. Wallet features + wallet anomaly** (`build_wallet_features(cluster_labels, cluster_sizes, risk,
peel)` then a second `detect_anomalies(...)` over wallets)
→ 27 wallet features, a wallet-level outlier model, `wallet_features.csv` / `tx_features.csv`, and
`apply_node_attributes(...)` which writes `cluster_id`, `risk_score` and peel flags **back onto the graph
nodes** so the dashboard's graph colouring is data, not decoration.

> Note the dependency spine of stage 5: **features → clustering → anomaly → peeling → risk → wallet
> features**. Risk runs *after* the detectors because transaction risk blends detector evidence; wallet
> features run *after* risk because they include risk columns. Running them in another order would
> silently produce zeros, which is why the order is hard-coded in the runner and asserted by tests.

### Stage 6 — Explain and rank (`explain`)

- **In:** the builder, both feature frames, the anomaly results, the peel result, the risk result, the
  cluster result, the correlated records, and the seeds.
- **Call:** `AlertExplainer(...).build_alerts()` → an `AlertBundle` (alerts + summary of the run).
- **Inside:** gather candidates (wallets above threshold + transactions flagged by any detector), build
  wallet alerts and transaction alerts, compute `{peel, mixing, anomaly, risk, cluster, correlation}`
  components, blend them with the severity ladder, compute confidence, write the plain-English reasons,
  attach attribution (real SHAP if installed, otherwise a labelled surrogate), rank, and enforce
  category diversity (`ALERTS_PER_CATEGORY=2`).
- **Out:** 60 alerts (in the demo: 37 wallet + 23 transaction), `alerts.json`, plus
  `explainer_meta.json` recording *which* explainer actually ran.
- **Demo numbers:** highest risk 0.95, mean risk 0.85, mean confidence 0.96, 55 alerts ≥ 0.70, out of
  1,727 candidates considered.

### Stage 7 — Serve (`api` + `app`)

- The API reads `alerts.json`, `graph.json`, `pipeline_summary.json` and the ledger, and answers the 12
  endpoints. The dashboard reads the API (or the same files if the API is down).
- **Nothing in stage 7 recomputes analytics.** If a number looks wrong, it was produced in stages 1–6.

### Stage 8 — Evidence lock (`utils/hashing`)

- **In:** the ranked alerts.
- **Call:** `write_ledger(ctx.alerts)`.
- **Inside:** each alert's evidence package is serialised deterministically (`canonical_json`: sorted
  keys, no whitespace drift), hashed with SHA-256, stamped as `evidence_hash`, and appended as exactly
  one line to `data/evidence_ledger.jsonl` — one write handle, one line per alert.
- **Out:** a ledger file an auditor can re-verify. *Explained fully in Part 7.*

---

## 3.3 The full connection table

Who reads whom. If you are debugging, this table tells you which upstream stage to blame.

| Producer | Consumed by | As what |
|---|---|---|
| generator | parsers | `transactions.csv`, `network_metadata.csv`, `seed_illicit_wallets.json` |
| parsers | enrich, correlator, graph | normalised DataFrames |
| enrich | correlator | geo/ASN/cross-border columns on network rows |
| correlator | graph, features, explain | correlated pairs + best correlation per tx |
| graph builder | features, clustering, risk, explain, API, dashboard | `MultiDiGraph`, `tx_index`, wallet/undirected projections, `graph.json` |
| features (tx) | anomaly, explain | 24-column matrix |
| clustering | risk, features (wallet), explain | `labels`, `methods`, `sizes`, `members` |
| anomaly (tx) | risk, explain | `score_map()` (0–1 per txid) |
| peeling | risk, features (wallet), explain | `tx_peel_score`, `tx_is_mixing`, `wallet_peel_score`, `wallet_mixing_count`, chains |
| risk | features (wallet), explain, API (`/stats`) | `wallet_risk`, `tx_risk`, `hops_to_seed`, components |
| features (wallet) + wallet anomaly | explain | 27-column wallet matrix + wallet outlier scores |
| explain | hashing, API, dashboard | ranked alerts with evidence packages |
| hashing | API (`/ledger*`), dashboard | ledger entries + verification result |

Two structural consequences worth remembering:

1. **The graph is the hub.** Stages 5b–6 read the graph rather than the CSVs. If the graph is wrong,
   everything downstream is wrong in a correlated way — which is why `test_graph_structure_and_helpers`
   checks node and edge types explicitly.
2. **The seeds are an input, not a constant.** They arrive from `seed_illicit_wallets.json`, from a file
   discovered at ingest time, or from `POST /run`'s request body. Change the file, re-run, and the whole
   risk picture is recomputed. Clustering is unchanged by seeds — it does not know who is bad.

---

## 3.4 What happens on the very first `docker compose up --build`

1. **Build time.** The image installs the pinned stack; then, inside the image:
   `scripts/generate_sample_data.py` → `scripts/run_full_pipeline.py` → `scripts/train_models.py`.
   So the image ships with a generated dataset, a completed analysis and trained models. Build-time
   warm-up is why the demo starts in seconds instead of minutes.
2. **Container start.** `entrypoint.sh` creates directories, generates the sample **only if missing**,
   runs the pipeline **only if artefacts are missing**, starts uvicorn on `8000`, then Streamlit on
   `8501`. With `./data` bind-mounted, a restart skips straight to serving.
3. **First request.** `GET /health` reports the artefact paths and whether they exist; `GET /stats`
   reports the stage timings. The dashboard's sidebar pill reads **API · live**.

Continue to **[Part 4 — Clusters explained](04_CLUSTERS.md)**.
