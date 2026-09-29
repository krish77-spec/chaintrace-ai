# 13 · The seven X-factors — all built

Part 12 fixed the data. This part is about the features that make ChainTrace AI *hard to copy*:
the things a reviewer remembers a week later.

This file used to be a menu of ideas with costs. **Every item on it is now implemented, wired into the
dashboard, and covered by tests** (`tests/test_xfactors.py`). Below, each one gets the same five answers:

* **What it is** — in plain language, no jargon.
* **What shipped** — the actual code, in one line.
* **Where you see it** — the button, tab or command.
* **Why it lands** — the question it answers that a normal dashboard cannot.
* **What it still does not prove** — the honest limit.

All seven run inside the same offline container: no new services, no API keys, no internet.

---

## A. Tamper-evident evidence ledger (a real hash chain)

**What it is.** A sealed record of every alert, where each entry also stores the fingerprint of the entry
before it. Change anything in the past and the chain visibly breaks — and points at the row that broke.

**What shipped.** `src/utils/hashing.py`: `build_ledger_entry()` writes `prev_hash` + `entry_hash` per
entry; `verify_ledger_chain()` recomputes every link and returns the **position of the first break**;
`tamper_demo()` edits a *copy in memory* to prove detection works without touching the real file.

**Where you see it.**

* Dashboard → **🔒 Evidence & Integrity** → **🔗 Verify the whole hash chain** →
  *"60 sealed entries verified - each one links to the entry before it."*
* Same tab → **🕵 Simulate tampering** →
  *"Tampering detected at entry 31: entry contents changed after sealing."* (Nothing is written to disk;
  press verify again and the chain is intact.)
* Terminal: `python -m src.utils.hashing --verify` (exit `1` on a break, usable as a CI gate) and
  `python -m src.utils.hashing --tamper-demo`.

**Why it lands.** "Evidence integrity" was an explicit objective, and the difference between hashing
things and *chaining* them is the difference between a checksum and an audit trail. It is also the single
most demo-able thing in the project: tamper live, watch the verifier catch it and name the row.

**What it does not prove.** That the record was not edited afterwards — not that the analysis inside it
was right. It is tamper-evidence, not truth.

---

## B. One-click case file

**What it is.** A dossier for an alert, as a file — the thing an investigator actually attaches to a case.

**What shipped.** `src/utils/casefile.py` → `build_case_file(alert, graph, ledger_report)` returns
`{"html", "markdown", "filename"}`. The HTML is **fully self-contained**: no CDN, no external CSS, no
image requests — the alert's neighbourhood is drawn as **inline SVG** by `neighbourhood_svg()`.

**Where you see it.** **⬇ Case file (HTML)** / **⬇ Case file (Markdown)** in Tab 1 (⚡ Ranked Alerts) and
Tab 4 (🔒 Evidence & Integrity). Each file contains:

| Section | Contents |
|---|---|
| Summary | alert id, entity, risk, confidence, recommended action, timestamp |
| Why this was flagged | the full reasons list, in detector order |
| What would clear this entity | the counterfactual summary + the ablation table |
| Neighbourhood | inline-SVG picture of the alert's surroundings |
| Financials / cluster / peel chain | totals, counterparties, first/last seen, cluster method and size, chain id, hop, value moved, asymmetry |
| Network evidence | every correlated packet: country, ASN, operator, match method, confidence, Δ time |
| Related transactions | the transactions the evidence points at, with role and amount |
| Integrity | the SHA-256 and the ledger's chain status, plus the exact re-verification commands |

**Why it lands.** The dashboard is a tool; a case file is a deliverable. It also gives the demo an ending
that matches how the work really ends: *here is the lead, here is the package, here is the tamper-proof
hash.*

**What it does not prove.** A dossier is a template. It is only as strong as the evidence inside it, and
it inherits every limitation of that evidence.

---

## C. Adversarial precision harness ("innocent look-alikes")

**What it is.** The unfair test. We plant ordinary, legitimate behaviour that *looks* criminal and then
measure how much of it the detectors wrongly promote.

**What shipped.** The generator plants five benign families (`exchange_sweep`, `payroll_fanout`,
`batching_wallet`, `self_transfer_chain`, `high_fee_small_amount`) and labels them in the manifest, so the
detectors can be graded **and** penalised. `scripts/benchmark_detectors.py` scores two systems —
a rules-only threshold baseline and the shipped stack — at two levels (did a detector fire? did it reach
the ranked list?), and writes `data/artifacts/detection_benchmark.json`.

**Where you see it.** `python scripts/benchmark_detectors.py`. Current output on the default dataset
(78 labelled illicit, 15 labelled benign, 1,107 unlabelled):

| Level | System | Recall (illicit) | Look-alikes flagged | FPR on benign |
|---|---|---|---|---|
| signal | rules-only | 0.41 | 8 | 0.53 |
| signal | **ChainTrace AI** | **0.65** | 9 | 0.60 |
| lead | rules-only | 0.41 | 8 | 0.53 |
| lead | **ChainTrace AI** | 0.28 | **0** | **0.00** |

**Why it lands.** The first question a serious reviewer asks is *"what is your false positive rate?"* —
and the honest answer used to be "we only measure recall against planted crimes". Now there is a number,
a baseline to compare against, and a clear statement of where we still lose (signal-level false positives
are comparable to the baseline; the model's advantage is that the *ranked list* stays clean).

**What it does not prove.** Precision against *planted* benign families. Real-world false positives depend
on real-world behaviour we cannot invent, and the benign families are ours — which is exactly why the
harness also prints each family separately instead of one aggregate number.

---

## D. Timeline replay (scrubbed inside the chart)

**What it is.** A timeline slider that scrubs the 30-day collection window by hand, so the graph shows
only what was known at that moment — the analyst drags it forward and back instead of watching a replay.

**What shipped.** `first_seen_by_edge()` in `src/utils/helpers.py` (each node is visible from its first
transaction), and a replay mode on the graph tab whose snapshots are **plotly frames** with a **plotly
slider in the figure itself**: dragging is a client-side animation, so there is no rerun, no chart
re-render and no page reload, and the readout card in the corner carries the exact moment plus the node
and alert counts. It replaces two earlier designs — an auto-play loop that walked 29 days in three
seconds, and a Streamlit slider whose every drag step was a rerun that redrew the whole chart (which on a
couple of hundred nodes reads as the page reloading).

**Where you see it.** Tab 2 (🕸 Interactive Graph) → **⏱ Timeline replay**, then drag the **timeline
slider under the picture** and watch a peeling chain unroll hop by hop while the risk colours spread
outward from the seeds.

**Why it lands.** Link analysis is normally static; this shows the *mechanism* — risk propagation really is
a per-hop process — and it gives the pitch its one genuinely visual moment.

**What it does not prove.** It is a view over timestamps, not a reconstruction of balances. "Visible from
its first transaction" is our definition of existence in the picture, and the caption says so.

---

## E. Infrastructure roll-up (the "who do we call?" view)

**What it is.** Grouping every flagged wallet and correlated packet by the **hosting provider (ASN)** and
**country** behind the traffic, because wallets are cheap to abandon and infrastructure is not.

**What shipped.** `src/utils/infrastructure.py` → `rollup()` (headline + provider table + country table +
totals) and `focus_list()` (a ranked "next steps" list); exposed as `GET /infrastructure` and rendered in
the dashboard. An alert is counted **once per distinct ASN**, so one provider cannot be inflated by
counting three of its addresses three times.

**Where you see it.** Tab 3 (🌐 Infrastructure): four tiles (alerts · with network evidence · distinct
providers · distinct countries), the generated headline, both tables, the next-steps list and a JSON
export. Current demo output:

```
7 hosting networks appear behind 60 alerts. The top three (Bharti Airtel Ltd,
Island Datacom Ltd, British Telecommunications PLC) carry 44 linked alerts
between them - start the provider conversation there.
```

**Why it lands.** It answers *"so what should the agency do next?"* — not "these wallets are bad" but
"these three providers are the common infrastructure, start there" — and it is the payoff of the
`geo_country` / `geo_asn` fields the brief asks for.

**What it does not prove.** The attribution. The names and countries come from the offline GeoIP step,
which uses a deterministic mock table unless a real `GeoLite2-*.mmdb` (or DB-IP Lite) is placed in
`data/geo/`. The grouping logic is real; the database is yours to supply.

---

## F. Counterfactual explanations ("what would clear this wallet?")

**What it is.** Next to every alert, the smallest change to the evidence that would take it off the list.

**What shipped.** `explain.py` re-scores each alert with one evidence component ablated at a time and
attaches `counterfactuals` + `counterfactual_summary` **before** the evidence hash is computed — so the
counterfactual is part of the sealed record and cannot be edited either.

**Where you see it.** Tab 1 → **🔄 What would clear this entity?** (and in the case file). Each row shows
the component removed, the score without it, the drop, whether it would still be an alert, and whether the
removal is **decisive**:

```
Remove this evidence                      Score would be   Drop   Still an alert?   Decisive?
risk inherited from the known-bad seeds        0.684       0.266        yes            no
```

Real alerts in this dataset are mostly *non-decisive* — and the tab says why: *"No single piece of
evidence decides this alert - two or more independent signals have to fall away."*

**Why it lands.** Explainability usually tells you why the score is high. A counterfactual tells an analyst
**what to verify to close the case**, which is the question that actually costs investigator time.

**What it does not prove.** A counterfactual is arithmetic on the model's own evidence, not a statement
about the world. "Remove the seed inheritance and it drops to 0.68" locates the weight of the case; it is
not an acquittal.

---

## G. Bitcoin-correct encodings

**What it is.** Addresses that pass a real Bitcoin address validator, because they are built with the real
checksums, not random strings with a plausible prefix.

**What shipped.** `src/utils/bitcoin.py` implements Base58Check and bech32/bech32m from the specs, and the
generator emits `p2pkh` / `p2sh` / `p2wpkh` / `p2tr` addresses accordingly. The dataset audit verifies
every one of them independently.

**Where you see it.** `data/synthetic/transactions.csv` (any address), or the audit check
*"every address decodes with a valid checksum — 516/516"*. Current mix: p2pkh 211 · p2sh 75 · p2tr 51 ·
p2wpkh 179. A single flipped character fails validation (asserted in `tests/test_xfactors.py`, alongside
the BIP-173 reference vector).

**Why it lands.** It closes the "this is fake data" line of attack with a one-line proof: hand the CSV to
any Bitcoin library and every address validates.

**What it does not prove.** Valid encoding ≠ real chain. These addresses are synthetic; they are correctly
*formatted* Bitcoin addresses, not observed ones.

---

## What I still would not add

* **A real GNN / embedding stack.** The brief makes it optional; it adds install risk and a training story
  that cannot be defended in a five-minute demo. Louvain + common-input already carries clustering.
* **A live Bitcoin node or any online enrichment.** It breaks the offline promise the whole submission is
  built around.
* **An LLM at runtime.** The narrative is generated by templates, so there is no network call, no API key,
  and no hallucination risk inside an evidence file.
* **Authentication / multi-user state.** Out of scope for a prototype and it distracts from detection.

---

## How to verify all of it in two minutes

```bash
cd chaintrace-ai
python scripts/audit_dataset.py          # 35/35 — the data contract (SIH26146), incl. address checksums
python scripts/benchmark_detectors.py    # C — precision vs innocent look-alikes, next to a baseline
python -m src.utils.hashing --verify     # A — the hash chain, exit 0 only if intact
python -m src.utils.hashing --tamper-demo# A — proves the check catches an edit
pytest -q                                # 67 tests, including tests/test_xfactors.py (A–G)
docker compose up --build                # then open http://localhost:8501
```

In the dashboard: Tab 1 (counterfactual + case file), Tab 2 (replay), Tab 3 (infrastructure),
Tab 4 (verify chain, simulate tampering). Part 11 is the button-by-button tutorial for all four.
