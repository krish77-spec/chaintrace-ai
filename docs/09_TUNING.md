# Part 9 — Every tuning knob

All of these live in `src/config.py`. Nothing else in the codebase hard-codes a threshold, so this file is
the complete list of things you can change to make the system more sensitive, more precise, or faster.

Change one value, re-run `python scripts/run_full_pipeline.py`, and grade the result with
`python scripts/validate_detections.py`.

---

## 9.1 Dataset shape (the synthetic world)

| Constant | Default | Change it to |
|---|---|---|
| `DEFAULT_TX_COUNT` | 1200 | make a bigger or smaller haystack |
| `DEFAULT_WALLET_COUNT` | 450 | change density; fewer wallets → more co-spending → more cluster collisions |
| `DEFAULT_NETWORK_COUNT` | 320 | more or fewer packet records |
| `DEFAULT_DAYS_OF_ACTIVITY` | 30 | spread the transactions over a longer period |
| `PEEL_CHAINS_MIN/MAX` | 3 / 6 | how many peeling chains to plant |
| `PEEL_CHAIN_HOPS_MIN/MAX` | 4 / 8 | how long they are (the detector caps walks at `MAX_PEEL_HOPS`) |
| `SEED_WALLET_MIN/MAX` | 5 / 8 | how many known-bad anchors |
| `MIXING_TX_MIN/MAX` | 2 / 3 | how many CoinJoins to plant |
| `ANOMALY_TX_MIN/MAX` | 8 / 14 | how many anomalous transactions to plant |
| `CLUSTER_GROUP_MIN/MAX` | 3 / 6 | how many co-spending ownership groups to plant |
| `RANDOM_SEED` | 42 | **change this to test whether your detector actually generalises.** Everything is reproducible from it, including the ground truth. |

> The single most useful experiment in the whole project: run
> `python scripts/validate_detections.py` with `RANDOM_SEED` set to 1, 2 and 3. A detector that only works
> on seed 42 is a detector that memorised the dataset.

## 9.2 Correlation (stage 3)

| Constant | Default | Effect |
|---|---|---|
| `TIME_WINDOW_SECONDS` | 90 | widening it matches more (and weaker) records; narrowing it makes matches more precise. The generator plants window matches inside `TIME_WINDOW_SECONDS − 10`, so shrinking this below ~40 s starts losing planted matches. |
| `CORRELATION_EXACT_CONFIDENCE` | 1.0 | confidence for a TXID-exact match |
| `CORRELATION_CONFIDENCE_FLOOR` | 0.5 | confidence at the window edge |
| `CORRELATION_MIN_CONFIDENCE` | 0.30 | matches below this are discarded |

The ambiguity penalty (`1/sqrt(candidates)`) is not configurable on purpose: it is a statement of fact,
not a preference. Twenty transactions inside a 90-second window is genuinely ambiguous.

## 9.3 Clustering (stage 5b)

| Constant | Default | Effect | Watch out for |
|---|---|---|---|
| `COMMON_INPUT_MAX_INPUTS` | 5 | co-spends above this are skipped as merge hazards | **lowering it (e.g. 3) to 3 breaks up the chain-merge blob but also loses real ownership evidence** |
| `USE_LOUVAIN_REFINEMENT` | True | soft communities for wallets with no hard evidence | turning it off makes `cluster_method` all `common_input`/`singleton` |
| `LOUVAIN_RESOLUTION` | 1.0 | >1 → more, smaller communities; <1 → fewer, larger | it changes modularity and cluster counts |
| `CLUSTER_SIZE_WARN` | 6 | size at which a cluster is worth mentioning in a reason line | — |
| `CLUSTER_SIZE_REPORT_MAX` | 50 | beyond this a cluster is too broad to name in a reason | — |
| `CLUSTER_INHERIT_MAX_SIZE` | 30 | **the collapse guard**: components larger than this inherit no risk and earn no cluster evidence | lower it to be more conservative; it is the main defence against flooding |

## 9.4 Anomaly detection (stage 5c)

| Constant | Default | Effect |
|---|---|---|
| `ISOLATION_FOREST_CONTAMINATION` | 0.06 | the assumed fraction of outliers — the model's own prior |
| `ISOLATION_FOREST_ESTIMATORS` | 200 | more trees = more stable, slower |
| `USE_LOF_MODEL` | True | blend in local outlier detection |
| `LOF_NEIGHBORS` | 20 | neighbourhood size; needs `> LOF_NEIGHBORS + 2` wallets to run at all |
| `LOF_WEIGHT` | 0.35 | how much LOF counts in the blended score |
| `ANOMALY_FLAG_QUANTILE` | 0.94 | **absolute flag rate**: 0.94 always flags ~6% of whatever you feed it. Raise it for a quieter system, lower it for a noisier one. |

That last one is the most important behavioural note in this table: the anomaly stage is
**quantile-based**, so it always produces roughly the same *number* of flags regardless of how clean the
data is. It is a ranking tool, not a "is this file suspicious" tool.

## 9.5 Peeling and mixing (stage 5d)

| Constant | Default | Effect |
|---|---|---|
| `PEEL_ASYMMETRY_THRESHOLD` | 0.75 | raise → fewer, cleaner chains; lower → more hops including mild peels (the demo finds chains at ~0.87 asymmetry) |
| `PEEL_PEEL_MIN_SHARE` | 0.03 | raise to ~0.08 to suppress the 158 standalone change-address false positives |
| `MAX_PEEL_HOPS` | 8 | hard cap on chain length; also normalises the length score |
| `MIN_PEEL_CHAIN_LENGTH` | 3 | lower to 2 → many more, much weaker chains |
| `COINJOIN_MIN_INPUTS` / `MIN_OUTPUTS` | 5 / 5 | smaller mixes are not detected |
| `COINJOIN_OUTPUT_CV` | 0.15 | raise to catch sloppier mixes (at the cost of false positives on payouts) |
| `COINJOIN_INPUT_CV` | 0.60 | inputs must be mixed-size, unlike a normal sweep |

## 9.6 Risk propagation (stage 5e)

| Constant | Default | Effect |
|---|---|---|
| `RISK_DECAY` | 0.7 | risk retained per hop; `0.7^6 ≈ 0.12` at the hop limit. Raise for a more suspicious system. |
| `RISK_BFS_MAX_HOPS` | 6 | beyond this, no proximity risk at all |
| `HUB_DEGREE` | 15 | wallets above this degree damp risk passing through them. **This is the knob that decides how much "we used the same exchange" counts.** In the demo, 371 wallets sit within 2 hops of a seed — most of that reach exists because ordinary wallets are not hubs. |
| `SEED_RISK` | 1.0 | the risk of a seed wallet itself |
| `RISK_WEIGHT_PAGERANK` / `RISK_WEIGHT_PROXIMITY` | 0.6 / 0.4 | structural influence vs path distance |
| `PAGERANK_ALPHA` | 0.85 | standard damping |
| `TX_RISK_BLEND` | 0.55 | transaction risk = 55% counterparty risk + 45% its own detector evidence |
| `CLUSTER_RISK_DISCOUNT` | 0.85 | how much of a cluster's peak risk is inherited by its members (the 15% discount for inference uncertainty) |

## 9.7 Alerts and explanation (stage 6)

| Constant | Default | Effect |
|---|---|---|
| `MIN_ALERT_RISK` | 0.50 | the alert threshold |
| `MAX_ALERTS` | 60 | list cap (the demo hits it exactly) |
| `ALERTS_PER_CATEGORY` | 2 | reserved slots per detector family (the diversity guarantee) |
| `ALERT_CONFIDENCE_FLOOR` | 0.35 | minimum reported confidence |
| `SCORE_WEIGHTS` | risk 0.34, peel 0.22, anomaly 0.20, cluster 0.10, mixing 0.10, correlation 0.08 | rebalance what "serious" means |
| `SEVERITY_LADDER` | risk 0.95, peel 0.85, mixing 0.75, anomaly 0.70 | how far a single strong signal can lift an alert |
| `TOP_FEATURES_PER_ALERT` | 5 | attribution panel size |
| `SHAP_MAX_ALERTS` / `SHAP_BACKGROUND_SIZE` / `SHAP_MAX_EVALS` | 12 / 60 / 300 | real SHAP budget — SHAP is slow, so it is capped to the top alerts |

## 9.8 Serving and performance

| Constant / env var | Default | Effect |
|---|---|---|
| `API_PORT` (`CHAINTRACE_API_PORT`) | 8000 | API port |
| `DASHBOARD_PORT` (`CHAINTRACE_DASHBOARD_PORT`) | 8501 | dashboard port |
| `API_URL` (`CHAINTRACE_API_URL`) | `http://127.0.0.1:8000` | what the dashboard calls |
| `API_TIMEOUT_SECONDS` | 2.5 | after this the dashboard falls back to files |
| `RUN_PIPELINE_ON_START` (`CHAINTRACE_RUN_ON_START`) | `auto` | `auto` = only if artefacts are missing; `always` = every start; `never` = serve only |
| `CHAINTRACE_DATA_DIR` | `./data` | relocate all inputs/outputs in one go (this is how the tests sandbox themselves) |
| `MAX_GRAPH_NODES_FOR_EXPORT` | 6000 | cap on `graph.json` size; above it the export is marked `truncated` |
| `GRAPH_SUBGRAPH_DEFAULT_DEPTH` | 2 | default depth for neighbourhood queries |

## 9.9 A recipe for the most common complaints

| Symptom | Turn these |
|---|---|
| "Too many alerts, all peeling hops" | `PEEL_PEEL_MIN_SHARE` ↑ (0.08), `MIN_PEEL_CHAIN_LENGTH` ↑ (4), `MAX_ALERTS` ↓ |
| "The only CoinJoin got buried" | already handled by `ALERTS_PER_CATEGORY`; raise it to 3 |
| "Everything looks risky" | `RISK_DECAY` ↓ (0.6), `HUB_DEGREE` ↓ (10), `CLUSTER_INHERIT_MAX_SIZE` ↓ (20) |
| "Nothing looks risky" | `RISK_BFS_MAX_HOPS` ↑ (8), `RISK_DECAY` ↑ (0.8), `MIN_ALERT_RISK` ↓ (0.4) |
| "The graph tab is a hairball" | `MAX_GRAPH_NODES_FOR_EXPORT` ↓, or filter in the tab by node risk |
| "Cluster ids are meaningless because everything is one blob" | `COMMON_INPUT_MAX_INPUTS` ↓ (3–4), and re-run; expect to lose some true groupings |
| "Anomalies are always 6% of my data" | expected — `ANOMALY_FLAG_QUANTILE` is a quantile, not a probability |

Continue to **[Part 10 — Troubleshooting and glossary](10_TROUBLESHOOTING.md)**.
