# ChainTrace AI — The Complete Walkthrough Guide

This is the "read me like a human" guide. It explains what the prototype does, what every file is for,
how the pieces are wired together, what the clusters and the threats actually mean, and how to install
and run the dashboard. It is split into parts so you can read one thing at a time.

| Part | File | Read this when you want to know... |
|---|---|---|
| 1 | [docs/01_OVERVIEW.md](docs/01_OVERVIEW.md) | What the system is, the data-flow picture, and what every single file does |
| 2 | [docs/02_DATA.md](docs/02_DATA.md) | What the dataset looks like — every column, example rows, the ground-truth manifest |
| 3 | [docs/03_PIPELINE.md](docs/03_PIPELINE.md) | How everything connects — all 8 stages, inputs/outputs of each, the exact call chain |
| 4 | [docs/04_CLUSTERS.md](docs/04_CLUSTERS.md) | Entity clustering explained — common-input, Louvain, merge guards, collapse guard |
| 5 | [docs/05_THREATS.md](docs/05_THREATS.md) | The threat typologies, how each one is detected, and what the alert means |
| 6 | [docs/06_SCORING.md](docs/06_SCORING.md) | How evidence becomes a number: components, weights, severity ladder, actions |
| 7 | [docs/07_EVIDENCE.md](docs/07_EVIDENCE.md) | Evidence hashing, the append-only ledger, and what "verified" really proves |
| 8 | [docs/08_RUNBOOK.md](docs/08_RUNBOOK.md) | Install + run (Docker and local), the dashboard tour, reading the outputs |
| 9 | [docs/09_TUNING.md](docs/09_TUNING.md) | Every knob in `config.py`, what it changes, safe ranges |
| 10 | [docs/10_TROUBLESHOOTING.md](docs/10_TROUBLESHOOTING.md) | Errors you may hit, what they mean, how to fix them, plus a glossary |
| 11 | [docs/11_TUTORIAL.md](docs/11_TUTORIAL.md) | The button-by-button tutorial — every control, every number, illustrated, under-the-hood explanations |
| 12 | [docs/12_DATA_AUDIT.md](docs/12_DATA_AUDIT.md) | Whether the data actually matches SIH26146 — the 35 machine-checked requirements, the fixes, and what the checks cannot prove |
| 13 | [docs/13_X_FACTORS.md](docs/13_X_FACTORS.md) | The seven X-factors — all built: hash-chained ledger, case files, precision harness, timeline replay, infrastructure roll-up, counterfactuals, real Bitcoin encodings |
| 14 | [docs/14_DEPLOY.md](docs/14_DEPLOY.md) | How to publish it to GitHub and host a live copy (Hugging Face Space), and what a hosted copy does *not* prove |

If you only have five minutes: read the "60-second version" below, then Part 6 and Part 9.

---

## The 60-second version

ChainTrace AI is an **offline investigative console** for a very specific investigative problem:

> A blockchain gives you transactions. A network capture gives you packets. Neither one alone tells you
> *who is doing what*. ChainTrace AI takes both, lines them up in time, builds one graph out of them,
> runs four independent machine-learning analyses over that graph, and produces a ranked list of alerts
> where every alert is written in plain English and carries a SHA-256 evidence hash so it cannot be
> quietly edited afterwards.

Concretely, for the bundled sample dataset (all numbers below are from a real run of the demo):

- **1,200 transactions** and **308 network-metadata records** go in — the latter carrying `geo_country`/`geo_asn`.
- **141 network records (46%)** get correlated to a transaction — **64 by exact TXID**, **77 by a ±90 s
  time window**. Mean correlation confidence **0.78**.
- Those become a graph of **1,872 nodes / 4,220 edges** over **516 wallet addresses**.
- Four ML analyses run: **transaction anomaly detection** (IsolationForest + LOF), **entity clustering**
  (common-input ownership + Louvain communities), **peeling-chain / CoinJoin structure detection**, and
  **risk propagation** from 8 known-bad seed wallets.
- Output: **60 ranked alerts** (35 wallets, 25 transactions), **5 peeling chains (29 transactions)**, **3 CoinJoin
  transactions**, **32 entity clusters**, and an append-only evidence ledger with one SHA-256 line per alert.

Everything runs inside one Docker container. **No analysis feature touches the internet.**

---

## Vocabulary you need before Part 1

Five words are used constantly. Learn these and the rest of the guide reads easily.

**Wallet / address** — a Bitcoin address. In this prototype a wallet = one address. There is no key
management and no real chain access; addresses are just string identifiers.

**UTXO** — an "unspent transaction output". Bitcoin has no account balances; it has outputs, and a
transaction *consumes* some outputs (its **inputs**) and creates new ones (its **outputs**). This matters
because **to spend two outputs in one transaction you must control the keys for both** — that single fact
is the foundation of the entire clustering stage.

**Peeling chain** — a laundering pattern. Take 100 BTC, spend it as "92 BTC onwards + 8 BTC to me", then
repeat, peeling a little value off each hop. The big output keeps travelling forward in time; the small
outputs are the criminal's cut. Also called chain-hopping.

**CoinJoin** — a mixing transaction where many unrelated people contribute inputs and receive
near-equal-sized outputs, so an outside observer cannot tell which output belongs to whom. It is the
legitimate-privacy / illegitimate-laundering tool that deliberately breaks the common-input heuristic.

**Seed wallet** — an address an analyst already *knows* is bad (a sanction list, a ransomware payout
address, an exchange freeze list). Risk in this system is not absolute; it is **measured as exposure to
seeds**, then spread outward through the graph.

---

## Where to start depending on who you are

- **Demoing to judges / teammates:** Part 8 (Runbook) → start the stack → Part 8's dashboard tour.
- **Understanding the ML story:** Part 4 (clusters) + Part 5 (threats) + Part 6 (scoring).
- **Reviewing the code:** Part 1 (file map) → Part 3 (pipeline wiring) → Part 9 (tuning knobs).
- **Worried about realism / "is this fake?":** Part 2 explains the synthetic dataset and its
  ground-truth manifest, Part 5 names the real-world technique behind every planted pattern, and Part 12
  is the honest audit — including the four things it deliberately does *not* prove.
- **Deciding what to build next:** Part 13.
