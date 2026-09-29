# Part 10 — Troubleshooting, FAQ and glossary

## 10.1 Errors you may actually hit

### `docker compose up --build` fails or hangs in the pip layer

Export a mirror, or just retry — the Dockerfile uses a BuildKit pip cache mount, so a completed download
is not repeated:

```bash
DOCKER_BUILDKIT=1 docker compose build --progress=plain
```

If the daemon is not running (`failed to connect to the docker API at unix:///Users/.../.docker/run/docker.sock`),
start Docker Desktop and wait for the whale to stop animating.

### The dashboard is up but empty / shows zeros

The artefacts are missing. Either run the analysis:

```bash
python scripts/run_full_pipeline.py
```

or press **▶ Run Analysis** in the sidebar, or start the container with
`CHAINTRACE_RUN_ON_START=always`.

### The dashboard shows "⚠ ledger needs attention"

`alerts.json` and `evidence_ledger.jsonl` came from different runs. This is exactly what used to happen
when the test suite rewrote the ledger from the *test* dataset while the demo's `alerts.json` stayed put
(fixed in `tests/conftest.py`, which sandboxes every `data/` path). If you see it, re-run the pipeline so
one run produces both files together:

```bash
python scripts/run_full_pipeline.py
curl -s localhost:8000/ledger/verify | python3 -m json.tool
```

### `AttributeError: 'MultiDiGraph' object has no attribute 'transactions'`

You passed a bare graph where a `GraphBuilder` was expected. `GraphBuilder(...).build()` returns the
builder (fluent), not the graph; the graph is at `.graph`. `FeatureEngineer`, `cluster_entities` and
`propagate_risk` accept either — `as_builder(...)` handles it — but `FeatureEngineer` needs the *raw
transaction rows*, so it must be built from a builder created with the transactions frame.

### `GET /graph/{node_id}` returns 500 or an empty body

It used to: the route called the `/graph` handler directly, so FastAPI's unresolved `Query(...)` object
leaked in as `limit`. Both graph forms now share one `_graph_response()` builder. If you see a 500 there
again, check that nobody reintroduced a direct handler-to-handler call.

### A click on the graph does nothing, or the dotted route line never appears

Three different things produce that one symptom, and they need different answers:

1. **The app never got the click.** Streamlit's plotly binding (1.38) drops single point-clicks
   sometimes: the browser reports no selection at all and the app cannot know a tap happened. Start the
   dashboard with `CHAINTRACE_FOCUS_DEBUG=1` and a **🐞 tap log** appears under the chart, one line per
   rerun — `sel=-` is this case. No focus logic can fix it, so use **Find a node**, the **Hop to a
   connected node** dropdown, or click a row of the **Direct connections** table: all three walk exactly
   the same ladder without chart hit-testing. A box or lasso drag always arrives too.
2. **You are running an older build.** `docker compose` bakes the code into the image (only `data/` is
   mounted), so a container started before your edits serves the old dashboard — a build from before
   focus mode exists shows a *Node inspector* JSON panel on a tap, no lit neighbourhood and no dotted
   route. Check with `docker exec chaintrace-ai grep -c "Who sent what to whom" /app/app/dashboard.py`
   (0 = stale) and rebuild: `docker compose up --build -d`. Running the app from the checkout
   (`.venv311/bin/streamlit run app/dashboard.py`) always uses your current code and is faster to
   iterate on.
3. **The route really is one node long.** The dotted amber line only appears from the **second** hop
   onwards (a single node has no route), and the head can legitimately have *0 direct connections* if
   every edge it had is filtered out by the replay cutoff or *Hide nodes below risk*. The header line
   states the count: `🔎 In focus: <id> - wallet, risk 0.312, 4 direct connection(s)`.

If the log shows `sel=<node> — handled=1` on a node you just clicked, that is the duplicate-click guard
working: the same selection on the same chart instance is only acted on once (otherwise the chart's
stored selection would re-focus the node on every later rerun). Click a *different* node, or press
**🔄 Reset the graph** to hand the chart a fresh identity, and it responds again.

### Nodes in the graph tab are all the same colour

They were, and the reason is worth knowing: `graph.json` was written in stage 4, *before* stage 5f
annotated the nodes with `cluster_id` / `risk_score`, so the file the API and dashboard render carried
`risk_score: 0.0` and `cluster_id: null` for every wallet. The runner now re-exports the graph after the
annotations, and a test asserts the exported payload carries them. If it regresses, that test fails.

### `pytest` changes my demo data

It does not any more, but if you add tests that bypass `config` (hard-coding `Path("data/...")`), they
will. Always go through `src.config`.

### Streamlit warns "install the Watchdog module"

Harmless. Optional file-watcher speed-up (`pip install watchdog`).

### I click a node on the graph and nothing becomes focused

Known, and worked around rather than hidden. The chart hands selections to the app through Streamlit's
plotly binding, and that binding is version-dependent: in the Streamlit build tested here (1.38) a
**box/lasso drag** and the controls below the chart always reach the app, but a single point-click can be
dropped before it arrives — the app never receives it, so nothing visible happens.

Two things were done about the part that *is* in our control:

1. **The click now lands on the node.** Edge traces used to be pickable, and a line vertex sits exactly on
the marker it joins, so "closest" picking could return the *edge* — which carries no node id, giving an
empty selection. Edges are now `hoverinfo="skip"` and a click reports the node with its id (verified in a
browser: `curve 6, customdata = <address>`).
2. **The same walk is reachable without chart hit-testing:** type the id into **Find a node**, pick from
**Hop to a connected node**, or **click a row of the Direct connections table**. All three produce exactly
the same focus, ladder and timeline.

If you want true click-anywhere-on-the-chart behaviour regardless of Streamlit version, that needs a small
custom component that listens for `plotly_click` in the page and sends the id back to Python. It is not
shipped because Streamlit's component library is normally pulled from a CDN, which would break the offline
promise; vendoring it is a deliberate next step, not an accident.

### I clicked a node and now I cannot get back / the focus keeps coming back

Fixed, and worth knowing because it was a genuine bug rather than a missing button. The chart keeps its
own selection in Streamlit's widget state. Clearing the focus from Python did **not** clear that
selection, so the very next rerun read the old click again and re-focused the node — it looked as though
nothing could be undone. Now every focus change made *outside* the chart goes through one helper
(`move_focus`), which (a) records the node being left as "already handled" so a live selection cannot be
re-adopted, and (b) moves the chart to a **new widget key** so it comes back with nothing selected
(verified in a browser: the old plotly DOM node is gone and the replacement starts with an empty
selection). The "already handled" block is scoped to the chart instance you are leaving, so clicking the
same node on the *new* chart works normally — the block is not a general "this node is banned" flag.

Three things are on screen in the focus row above the chart, always:

| Button | What it does |
|---|---|
| **⬅ Back one hop** | Step back one rung of the walk you made (greyed out with a single-hop or empty path) |
| **🔄 Reset the graph** | Clear the focus completely: nothing selected, whole graph at one intensity, **search box emptied**, chart given a fresh key. Never disabled — press it any time |
| **✖**-free alternative | Typing a *new* id into **Find a node** replaces the focus rather than adding to it |

If a click seems to stick, press **🔄 Reset the graph**: it clears the app's focus and the chart's
selection in one go.

### The graph reshuffles / jumps when I click

It should not, and if it does, the position cache is being lost. Node positions are stored per session
(`_stable_layout`): an existing node keeps its spot and a new one is dropped next to neighbours that are
already placed. A cache reset (a full page reload, or a new session) legitimately re-lays-out once.

### A wallet's timeline totals do not match the wallet's own totals

They must match, and a test asserts it. Both are sums over the wallet's edges. This was a real bug for four
wallets: when one transaction consumed or paid the **same address twice**, the graph's single
(transaction, address) edge was overwritten instead of accumulated, so the wallet's totals counted value the
graph had quietly dropped. `GraphBuilder._add_flow_edge` now sums such duplicates (`occurrences` records how
many rows went into an edge), and the mismatch is impossible by construction.

### The analysis is slow / the machine gets warm

The heavy parts are the anomaly models (IsolationForest + LOF over every transaction and every wallet) and
graph construction. On a laptop, the whole pipeline is ~7–9 s for 1,200 transactions. Scaling to 10,000+
transactions wants the wallet projection cached (it already is) and a trimmed feature set.

---

## 10.2 FAQ

**Is the dataset real?**
No. It is synthetic and generated from a seed (`src/data/generator.py`), with a ground-truth manifest so
detectors can be *measured* rather than praised. The *patterns* it plants are real techniques — peeling
chains, CoinJoins, fan-in sweeps, structuring — explained in Part 5.

**How would I point it at real data?**
Put transaction files and network files in a folder (or use `POST /upload` / the sidebar uploader),
make sure the columns are recognisable (see the alias maps in `src/data/parsers.py`, or Part 2), and run
the pipeline on that folder. You lose the scorecard, because real data has no `planted_patterns.json`.

**Does it need internet?**
No. The only network access is the Docker *build* (to install packages). At runtime: synthetic data, local
models, a local GeoIP table, and Streamlit's usage stats disabled.

**Is the ML supervised?**
No. There are no labels anywhere in the pipeline. Anomaly detection is unsupervised (IsolationForest +
LOF); clustering is unsupervised (Union-Find + Louvain); risk is **semi-supervised** — it uses the analyst's
seed list as the only supervisory signal, which is exactly how a real investigation starts.

**Why not just use a graph neural network?**
Because the deliverable has to run offline, start in seconds, and be explainable to a non-ML investigator.
A GNN would add a training pipeline, a GPU dependency and no explanation. The design instead uses
structure (peeling), shape (CoinJoin), statistics (anomaly) and graph diffusion (risk), each of which can
be reasoned about by hand. `LOUVAIN_RESOLUTION` and the features are the extension points if a team wants
to add embeddings later.

**Why is `shap` optional?**
`shap` forces `numpy>=2`, which conflicts with the pinned scikit-learn/pandas stack. The default explainer
is an explicitly-labelled permutation-importance surrogate, and `INSTALL_SHAP=1` switches the top alerts to
real KernelSHAP. The `explainer` field on every alert always says which one produced the numbers.

**Why 60 alerts exactly?**
`MAX_ALERTS = 60` and the demo produces more than 60 candidates (1,727 were considered). The list is
capped, with reserved slots per detector family so a rare family is never crowded out.

**Why do 158 "peel-like" transactions exist that are not chains?**
Because `1-in / 2-out` with a dominant output is also just… a payment with change. They are scored low
(≤0.5), flagged `is_peel = False`, and kept separate from real chains. Raise `PEEL_PEEL_MIN_SHARE` to
tighten it.

**Something looks wrong in the numbers. Where do I look?**
`data/artifacts/pipeline_summary.json` → the `stages` array names every stage, its timing and its detail
(including the clustering and risk summaries). Then `wallet_features.csv` / `tx_features.csv` for the exact
feature values behind any alert.

---

## 10.3 Glossary

**Alert** — one ranked finding about one entity (wallet or transaction), with reasons, components,
attribution and a hash. `CT-W-...` = wallet, `CT-T-...` = transaction.

**Address / wallet** — in this prototype, one Bitcoin address. No key management, no chain access.

**Anomaly score** — blended IsolationForest + LOF rank, 0–1, reported as a percentile in reasons.

**Chain-merge collision** — the failure mode of the common-input heuristic where transitive co-spending
fuses many unrelated owners into one component. Guarded by `CLUSTER_INHERIT_MAX_SIZE`.

**Cluster** — a set of addresses inferred to share one owner. `common_input` = hard evidence;
`louvain` = soft behavioural evidence; `singleton` = no grouping.

**CoinJoin** — a transaction with many unrelated inputs and many near-equal outputs, used for privacy and
for laundering. Detected by shape, never by name.

**Common-input heuristic** — "addresses that appear together as inputs share an owner, because spending
them requires their keys". The foundation of entity resolution; transitive, therefore dangerous.

**Component (of a score)** — one of `risk`, `peel`, `mixing`, `anomaly`, `cluster`, `correlation`.

**Confidence** — agreement-based number (0–1) describing how much independent evidence supports an alert;
separate from the risk score.

**Correlated record** — a network record matched to a transaction, by exact TXID (`txid_exact`) or by a
±90 s window (`time_window`), with a confidence.

**Evidence hash** — SHA-256 over the alert's canonical evidence package. Detects modification.

**Fan-in / fan-out** — many inputs into one transaction (consolidation) / one transaction paying many
outputs (distribution, dusting, payouts).

**Ground truth / planted pattern** — a pattern the generator deliberately created and recorded in
`planted_patterns.json`, used to measure detection precision and recall.

**Hub** — a wallet with many counterparties (exchange-like). Above `HUB_DEGREE`, risk passing through it
is damped, because sharing a hub is weak evidence.

**Ledger** — `data/evidence_ledger.jsonl`, one line per alert, header + entries. Compared against
`alerts.json` by `verify_ledger`.

**Louvain** — modularity-maximising community detection on the wallet projection. Soft evidence only.

**Peeling chain** — sequential hops, each spending the large "continue" output and peeling a small amount
off. Detected by shape + value continuity + forward time.

**Personalized PageRank** — PageRank with the seeds as the personalization vector; measures how much
seed influence reaches a wallet structurally.

**Risk propagation** — spreading risk outward from seeds via PageRank and hub-damped proximity, then
inheriting inside non-collapsed common-input clusters.

**Seed wallet** — an address the analyst already knows is bad. The only supervised input in the system.

**Severity ladder** — `max(blended, 0.95·risk, 0.85·peel, 0.75·mixing, 0.70·anomaly)`; stops dilution of
one very strong signal.

**SHAP / KernelSHAP** — game-theoretic feature attribution. Optional dependency; the surrogate is clearly
labelled when SHAP is absent.

**Singleton** — a wallet that is in no cluster.

**Script type** — `P2PKH`, `P2SH`, `P2WPKH`, `P2WSH`, `P2TR` (standard) and `OP_RETURN`,
`MULTISIG_BARE`, `NONSTANDARD` (unusual — a real signal).

**UTXO** — unspent transaction output. Bitcoin's unit of value; transactions consume UTXOs as inputs and
create new ones as outputs.

**Taint / exposure** — how much of a wallet's activity traces back to a seed. Never reported as a fact
about guilt.

---

## 10.4 Where to go next

Five improvements that follow naturally from the current design, in rough order of value:

1. **Make the ledger a hash chain.** Add `previous_hash` to each ledger line so deletion and reordering
   become detectable, and anchor the head hash somewhere outside the container.
2. **Grade the detectors in CI.** Turn `validate_detections.py` into a test with a precision/recall floor,
   so tuning cannot silently regress detection quality.
3. **Attack the 347-wallet collision.** Make clustering evidence-aware per merge (e.g. weight merges by
   the number of distinct transactions supporting them, or require repeated co-spending above a size
   threshold) so the planted ownership groups surface as entities instead of dissolving into the blob.
4. **Real embedding layer.** Use the existing 51 features as input to a node2vec/graph-embedding model to
   create additional clusters — the `cluster_method` field already supports a third source.
5. **Uploaded-file workflow hardening.** A schema preview and a per-file record-acceptance report before
   a run, so an analyst can see what the parser understood *before* trusting the analysis.

Back to **[the guide index](../GUIDE.md)**.
