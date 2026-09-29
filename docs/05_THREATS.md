# Part 5 — The threats, typology by typology

"Threat" here means a **money-laundering or illicit-finance technique that is visible in the shape of
transaction and network data**. Each section below answers the same five questions:

1. What does the criminal actually do?
2. What does it look like in the data?
3. How does ChainTrace AI detect it?
4. What does the alert say, and what should the analyst do?
5. How well does it work on the planted ground truth?

The demo numbers at the end of each section come from a real run.

---

## 5.1 Peeling chains (chain-hopping)

**What the criminal does.** Moves stolen or darknet funds through hundreds of hops, peeling a little off
at each one, so that no single address ever holds the whole amount and no single transfer looks large
enough to report. The "skin" peeled off at each hop is what actually gets spent; the "fruit" keeps
rolling forward.

**What it looks like in the data.** A transaction with **exactly one input and exactly two outputs**,
where the two outputs are **very unequal**: one big "continue" output and one small "peel" output. Then
the continue output is spent by the *next* hop, in the same shape, later in time.

**How it is detected** (`src/ml/peeling.py`):

| Rule | Constant | Effect |
|---|---|---|
| shape is 1-in / 2-out | — | anything else cannot be a hop |
| asymmetry `larger/(larger+smaller)` | `PEEL_ASYMMETRY_THRESHOLD = 0.75` | a 50/50 split is a normal payment, not a peel |
| the peeled output is not dust | `PEEL_PEEL_MIN_SHARE = 0.03` | avoids flagging transactions that just have a change address |
| walks must be ≥ 3 hops | `MIN_PEEL_CHAIN_LENGTH = 3` | one hop is a coincidence; three in a row is a technique |
| hop limit | `MAX_PEEL_HOPS = 8` | bounded walk length |

The detection itself is the interesting part:

1. Build a **spend index** — for every address, the timestamps and txids of the transactions that spend
   it, sorted in time.
2. Every candidate hop is tried as a **starting point** (not just the "obvious" first one). This matters:
   a seed or cash-out wallet can be spent by several unrelated transactions, so trusting a single head
   would let one dead end consume a real chain.
3. From each start, walk forward to the transaction that spends the *continue* output, choosing the
   candidate by **value continuity** — the one whose input amount is closest to what the previous hop
   forwarded — and only ever moving forward in time.
4. Collect every walk of ≥3 hops, sort them by length and value, and assign them to chains so that **no
   hop belongs to two chains**.
5. Score each chain: `0.40·length + 0.25·mean asymmetry + 0.20·log(value) + 0.15` if it starts at or feeds
   a seed. Per-transaction scores decay slightly with hop position (`1 − 0.04·hop`) — earlier hops are
   closer to the source and therefore more interesting.
6. Standalone peel-shaped transactions that did not form a chain still get a low score
   (`0.35 × asymmetry`, capped at 0.5) — partial evidence, clearly marked as such.

**What the analyst sees.** Reasons like *"Participates in peeling chain PC-01 of length 7 hops (hop 1)"*,
*"Peeling chain routes peeled value into a known illicit seed wallet"*, and *"Chain moved 787.5145 BTC
across 7 hops with mean asymmetry 0.87"*. The evidence package carries the full hop list with
`from_wallet`, `continue_wallet`, `peel_wallet`, both amounts and timestamps, so the chain can be
re-drawn by hand.

**Recommended action.** `Escalate - request full wallet history and freeze-linked counterparties` when the
chain touches a seed; monitor otherwise.

**Score on planted ground truth:**

```
planted_chains 3      detected_chains 3
planted_chain_transactions 21   detected 21   true positives 21
recall 1.00           precision 1.00
```

**Known honest limitation.** 158 standalone peel-shaped transactions were also found (ordinary payments
that happen to have a change output — `1-in / 2-out` with a small asymmetry). They are scored low
(≤0.5) and marked `is_peel = False`, but they are the reason the peeling family dominates the alert list
(47 of 60 alerts). A production tuning would raise `PEEL_PEEL_MIN_SHARE` or require a minimum value.

---

## 5.2 CoinJoin / mixing structures

**What the criminal does.** Uses a collaborative transaction (or a mixing service that mimics one) so
that outside observers cannot link inputs to outputs. Legitimate privacy tool, favourite laundering
layer: after a mix, the "tainted" coins are statistically indistinguishable from everyone else's.

**What it looks like in the data.** Many inputs from **unrelated** addresses, many outputs of **near-equal
size**, no single dominant output.

**How it is detected** (`detect_mixing`):

| Rule | Constant |
|---|---|
| at least N inputs | `COINJOIN_MIN_INPUTS = 5` |
| at least N outputs | `COINJOIN_MIN_OUTPUTS = 5` |
| output sizes near-equal: coefficient of variation `< 0.15` | `COINJOIN_OUTPUT_CV = 0.15` |
| **inputs must be distinct addresses** | — (repeated inputs mean one owner recycling coins, not a mix) |

Score = `0.6 × output uniformity + 0.4 × min(inputs/12, 1)`.

**Why the "distinct inputs" check matters.** Without it, an exchange consolidation with equal-value
outputs scores as a CoinJoin. With it, the detector distinguishes *"many people mixed together"* from
*"one person reorganised their coins"*.

**What the analyst sees.** *"CoinJoin-like mixing structure detected (score 0.84: many inputs, near-equal
outputs)"*, plus the transaction's input/output counts. Mixing evidence also feeds the transaction risk
blend and it participates in the diversity guarantee, so the three CoinJoin alerts in a 60-alert list
are never crowded out by peeling hops.

**Recommended action.** Investigate — the counterparties of a mix are not automatically suspects, but the
transaction's timing correlation to network traffic is worth reviewing.

**Score on planted ground truth:** planted 2, detected 2, precision 1.00, recall 1.00 — and **no
unplanted transaction was flagged**, which matters because a CoinJoin false positive is expensive.

---

## 5.3 Seed-wallet exposure (proximity laundering)

**What the criminal does.** Nothing clever — this is about *you* knowing one bad address and needing to
find everything connected to it. Exchange deposit, ransomware payout, sanctioned address.

**How it is detected.** Risk flows outward from the seeds (Part 6), and an entity is reported when it is
either a seed itself, within `RISK_BFS_MAX_HOPS = 6` hops of one, or mathematically influential in the
seeded PageRank.

Hub damping is what makes this useful: passing *through* a wallet with >15 counterparties costs extra, so
"we both used the same exchange" does not read the same as "coins went directly from you to a sanction
address". In the demo, 371 wallets are within 2 hops of a seed — a number that would be meaningless
without hub damping and the decay.

**What the analyst sees.** *"3 hops from a known illicit seed wallet"*, *"2 hops from a known illicit seed
wallet (risk decayed by distance)"*, plus `risk_paths_to_seed` — the **actual address paths** back to the
seed, rendered in the evidence panel.

**Recommended action.** `Escalate immediately - sanctioned/known-bad wallet` for the seeds themselves;
escalate/investigate for everything else, scaled by score.

**Score:** 5 of 5 seed wallets alerted, in the top five positions.

---

## 5.4 Fan-in sweeps and laundering rings (the entity threat)

**What the criminal does.** Runs a "money mule" structure: several addresses under one owner, sweeping
them together regularly and out through a hub. The *entity* is the threat; the individual addresses look
small.

**How it is detected.** The whole of Part 4. In short: Union-Find over co-spending with merge guards
identifies the ownership group; a tainted (non-collapsed) group is treated as an exposed entity; a
collapsed one is reported but not trusted.

**What the analyst sees.** *"Belongs to a cluster of 11 wallets that share common-input ownership (likely
one real-world owner)"* followed by *"The ownership cluster contains a wallet that is already high-risk,
so the whole entity is treated as exposed"* — or the explicit collapse caveat when the heuristic failed.

**Recommended action.** Investigate with KYC/exchange data; a cluster is a lead about *who*, not proof of
*what*.

**Score on planted ground truth:** 25 alerts carry cluster reasoning; 30 alerts earn full cluster
evidence from non-collapsed clusters. **Honest limitation:** only 1 of 19 planted cluster wallets and 1 of
4 seeded-ring members reached the alert list, because both planted groups were absorbed into the
347-wallet collapsed component (§4.5). Clustering currently contributes explanation and ranking more than
it contributes promotion — a legitimate target for improvement.

---

## 5.5 Transaction anomalies (the six families)

These are not one technique; they are the classic "this transaction does not look like the others"
signals. `ANOMALY_FLAG_QUANTILE = 0.94` means the top ~6% of transactions by score are flagged.

| Family | What it means in the real world | Planted signature |
|---|---|---|
| `extreme_fee_ratio` | fat-finger error, coercion, or fee-based signalling/encoding | fee 18–42% of the transferred value |
| `very_large_amount` | layering a large sum through a small wallet | 700–2,400 BTC in one hop |
| `unusual_script` | non-standard output construction — data smuggling, obfuscation | `OP_RETURN`, `MULTISIG_BARE`, `NONSTANDARD` |
| `rapid_successive_small` | structuring / smurfing to stay under reporting thresholds | 10–18 sends inside seconds |
| `extreme_fan_in` | consolidation immediately before a cash-out | 12–22 inputs into one output, often into a seed |
| `extreme_fan_out` | distribution / "spray and pray" — payout splitting or dusting | 8–15 near-equal outputs |

**How it is detected** (`src/ml/anomaly.py`): a `StandardScaler`, an `IsolationForest`
(`contamination=0.06`, `n_estimators=200`) and a `LocalOutlierFactor(n_neighbors=20)`, rank-normalised and
blended with `LOF_WEIGHT = 0.35`.

- **IsolationForest** catches *global* outliers: a fee ratio of 0.4 is absurd in absolute terms.
- **LOF** catches *local* outliers: a transaction that looks ordinary next to the whole population but is
  bizarre next to its own neighbours. Money laundering is usually local weirdness, which is why one model
  is not enough.

**What the analyst sees.** *"High anomaly score from the Isolation Forest model (99th percentile)"*, with
the top contributing features named (`log_total_input`, `mean_time_delta_seconds`,
`correlation_confidence`, `is_1in_2out`, `has_network_correlation` — from a real alert).

**Recommended action.** Investigate or monitor depending on corroboration — an anomaly alone is a lead,
never a conclusion.

**Score:** 72 of 1,200 transactions flagged (top 6%); 23 transaction alerts in the ranked list. 9 of the
14 planted anomaly transactions surface as alerts.

---

## 5.6 Network-layer exposure (the metadata threat)

**What the criminal does.** Everything technical leaves metadata: which IP talked to which node, when,
over which port. This is the layer that turns "an address moved coins" into "a device in Brazil was
talking to a node right before the transaction appeared".

**How it is detected** (stages 2–3): correlate the packet record to the transaction by **exact TXID**
(confidence 1.0) or by a **±90 s window** with distance-decayed confidence and an ambiguity penalty; then
attach offline GeoIP/ASN, flag private ranges and mark cross-border traffic. `src_geo_is_offshore`
(PA, SC, RU, CN, HK, TR, UA) becomes a transaction feature, and `distinct_source_countries` becomes a
wallet feature.

**What the analyst sees.** *"5 network record(s) correlated with this transaction from BR, SE (best
confidence 1.00 via txid_exact)"*, with every record listed including `time_delta_seconds` and the ASN
organisation.

**Recommended action.** Correlate with provider records; cross-border plus offshore hosting plus a seed
payment is the strongest non-financial signal in the dataset.

**Score:** 141 of 308 network records correlated (64 exact, 77 window), mean confidence 0.78; 132 were
planted, so a few noise records legitimately fall inside a 90-second window — which is precisely why every
match carries a confidence instead of a boolean.

---

## 5.7 How the threats combine into one ranked answer

Threats are not scored in isolation. Each contributes a component, and Part 6 explains the blend. The
practical consequence: an alert's reasons tell a *composite* story. A real alert from the demo:

```
CT-W-A72FDBCA   wallet bc1qsl7glxz370q7guvu6zuwfqtd6n5gcuh9jdsds4   risk 0.95   confidence 1.00
components: peel 0.88 · mixing 0.00 · anomaly 0.99 · risk 1.00 · cluster 0.00 · correlation 1.00

Wallet is on the known-illicit seed list used to bootstrap this analysis
Participates in peeling chain PC-01 of length 7 hops (hop 1)
Peeling chain starts at a known illicit seed wallet
Peeling chain routes peeled value into a known illicit seed wallet
Chain moved 787.5145 BTC across 7 hops with mean asymmetry 0.87
High anomaly score from the Isolation Forest model (99th percentile)
This is a seed wallet: risk is fully seeded by analyst input
Excluded from ownership-based reasoning: this address sits in a 347-wallet component, above the
30-wallet limit where the common-input heuristic is treated as a chain-merge collision
5 network record(s) correlated with this transaction from GB, IN, SC (best confidence 1.00 via txid_exact)

action: Escalate immediately - sanctioned/known-bad wallet
```

That is the design goal in one object: **financial structure + entity ownership + statistical anomaly +
network metadata + stated provenance**, each with its own confidence, all in language a non-ML
investigator can act on.

Continue to **[Part 6 — From evidence to a number](06_SCORING.md)**.
