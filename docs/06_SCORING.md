# Part 6 — From evidence to a number

The previous two parts produced evidence. This part explains how that evidence becomes a single ranked
score, a confidence, and an action — and why the whole thing is designed to *not* trust one signal.

Everything here lives in `src/ml/explain.py` (`AlertExplainer`) and `src/ml/risk.py`.

---

## 6.1 The six components

Every alert — wallet or transaction — is scored from the same six components, each in `[0, 1]`:

| Component | Where it comes from | Meaning |
|---|---|---|
| `risk` | `risk.propagate_risk` | proximity/PageRank exposure to a known-bad seed wallet |
| `peel` | `peeling.detect_peel_chains` | membership in a peeling chain (decayed by hop position) |
| `mixing` | `peeling.detect_mixing` | CoinJoin-like structure score |
| `anomaly` | `anomaly.detect_anomalies` | blended IsolationForest + LOF percentile |
| `cluster` | `clustering.cluster_entities` | ownership-group evidence (0 if the component collapsed) |
| `correlation` | `correlator.correlate` | best network-record correlation confidence |

Real components from a demo alert: `{peel: 0.9215, mixing: 0.0, anomaly: 0.9878, risk: 1.0, cluster: 1.0,
correlation: 1.0}`.

## 6.2 The weighted blend

`SCORE_WEIGHTS` in `config.py`:

| Component | Weight | Why this weight |
|---|---|---|
| `risk` | **0.34** | exposure to a known-bad wallet is the strongest available evidence |
| `peel` | **0.22** | structural evidence of a real laundering technique |
| `anomaly` | **0.20** | statistical, useful but weak alone — lots of legitimate weirdness exists |
| `cluster` | **0.10** | probabilistic ownership inference |
| `mixing` | **0.10** | a mix is not itself a crime; it is context |
| `correlation` | **0.08** | metadata supports a case, rarely makes one |

```
blended = Σ(weight × component) / Σ(weight)
```

## 6.3 The severity ladder (why the blend is not enough)

A pure weighted average has a serious flaw: a wallet on a sanction list with no other evidence blends to
about **0.34** — it would rank *below* a merely weird transaction. So the score is floored by a
**severity ladder**: if one corroborated signal is very strong, the alert cannot fall below the matching
rung.

```
score = max(blended, ladder)
ladder = max( 0.95 × risk, 0.85 × peel, 0.75 × mixing, 0.70 × anomaly )
SEVERITY_LADDER = {"risk": 0.95, "peel": 0.85, "mixing": 0.75, "anomaly": 0.70}
```

Consequences you can see in the demo: all five seed wallets sit at exactly **0.95** (the `risk` rung),
peeling hops at **0.85**, and the single strongest anomaly at **0.70**. This is a deliberate, documented
policy choice — not a bug in the averaging.

## 6.4 Confidence is a different number from risk

`risk_score` answers "how bad does this look?". `confidence` answers "how much independent evidence
agrees?". They are separate because an investigator needs both: a 0.9-risk alert with 0.35 confidence
says *"something is off here but I have one weak signal"*, whereas 0.9/0.97 says *"four detectors agree"*.

```python
top3       = mean of the three strongest components
independent = count of components ≥ 0.40
agreement  = clamp(independent / 3, 0, 1)
confidence = clamp(0.60 × top3 + 0.40 × agreement, ALERT_CONFIDENCE_FLOOR, 1.0)
```

Demo: mean confidence **0.96** across 60 alerts. A seed wallet with everything firing sits at **1.00**; a
lone anomalous transaction with one corroborating correlation sits around **0.60**.

## 6.5 Recommended action

| Condition | Action |
|---|---|
| in the seed list | `Escalate immediately - sanctioned/known-bad wallet` |
| score ≥ 0.75 **or** ≤ 1 hop from a seed | `Escalate - request full wallet history and freeze-linked counterparties` |
| score ≥ `MIN_ALERT_RISK` (0.50) | `Investigate - enrich with exchange/KYC data` |
| otherwise | `Monitor - queue for the next review cycle` |

Demo distribution: 5 escalate-immediately, 35 escalate, 20 investigate. No 0.50–0.70 score is presented
as a conclusion.

## 6.6 Feature attribution: which numbers caused this

Each alert carries `top_features` — the features that moved the decision most, with contribution and value:

```json
[{"feature": "risk_pagerank",         "contribution": 0.3647, "value": 0.93574},
 {"feature": "peel_chain_length",     "contribution": 0.2290, "value": 8.0},
 {"feature": "out_degree",            "contribution": 0.1642, "value": 10.0},
 {"feature": "n_txs",                 "contribution": 0.1276, "value": 13.0},
 {"feature": "pagerank",              "contribution": 0.1146, "value": 0.002534}]
```

Contributions are normalised to sum to 1.0 (the test suite asserts this), so the panel reads as a
percentage breakdown.

Two explainers exist and the alert **says which one produced the numbers** in its `explainer` field:

- `importance_surrogate` (default) — the model's permutation importance multiplied by the entity's
  standardised deviation on that feature. Cheap, deterministic, clearly labelled as a surrogate.
- `kernelshap` — real `shap` KernelSHAP contributions, used automatically for the top
  `SHAP_MAX_ALERTS = 12` alerts **when `shap` is installed**.

This matters for honesty: the field never claims SHAP when SHAP was not run. `explainer_meta.json` records
the same fact for the whole run, alongside the model importances.

## 6.7 Ranking and the diversity guarantee

Ranking is by `risk_score` descending, then confidence. But a strict sort has a failure mode: in a
60-alert list, a family that produces many members (here, peeling hops) can push out the *only* example of
a rarer family (here, the 3 CoinJoins). An investigator must not lose that evidence.

So `_ensure_diversity` reserves at least `ALERTS_PER_CATEGORY = 2` slots per detector family —
`SECURITY` (seed/risk), `ANOMALY`, `MIXING`, `LOOP` (peeling/mixing structure) — before filling the
remainder by score. The output is still sorted by score; it is just not allowed to be *only* one kind of
evidence.

Result in the demo: 60 alerts covering 35 wallets and 25 transactions, 42 peeling-related, 3 CoinJoin, 8
seeds, 35 with cluster reasoning, with the mean confidence at 0.96.

## 6.8 Thresholds and what they mean for the demo

| Constant | Value | Effect |
|---|---|---|
| `MIN_ALERT_RISK` | 0.50 | nothing below this is called an alert |
| `MAX_ALERTS` | 60 | the list is capped (the demo hits the cap exactly) |
| `ALERTS_PER_CATEGORY` | 2 | reserved slots per detector family |
| `ANOMALY_FLAG_QUANTILE` | 0.94 | top ~6% of transactions flagged as anomalous |
| `ALERT_CONFIDENCE_FLOOR` | 0.35 | confidence never reported below this |

Demo totals: **1,727 candidates considered → 60 ranked alerts**, 60 above 0.5, 55 above 0.7.

## 6.9 The one-paragraph summary

Risk is **relative** (exposure to seeds, damped by distance and by hub degree); detector scores are
**structural** (peeling, mixing) and **statistical** (anomaly); clustering is **ownership inference**;
correlation is **metadata**. They are blended with weights that reflect how much each deserves to be
trusted, floored by a severity ladder so one very strong signal is never diluted into invisibility,
reported with an agreement-based confidence, explained by named feature contributions, and capped by a
diversity rule so a rare but important finding cannot be crowded out.

Continue to **[Part 7 — Evidence and integrity](07_EVIDENCE.md)**.
