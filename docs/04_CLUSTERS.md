# Part 4 — Clusters, explained properly

This is the part most people get stuck on, so it is written slowly. Everything here comes from
`src/ml/clustering.py` and `src/ml/risk.py`, with the real numbers from the bundled demo run.

---

## 4.1 Why clustering exists at all

A blockchain sees **addresses**, not people. One person routinely controls fifty addresses: a fresh one
per payment, a change address per transaction, a cold wallet, a hot wallet. If your analysis treats every
address as a separate entity, you get two expensive mistakes:

1. **False comfort.** A criminal's coins move `hot wallet → fresh address → fresh address → cash out`.
   Each fresh address looks innocent in isolation, so an address-level alert list stays quiet.
2. **Alert fatigue.** You raise ten alerts for one person and call it ten threats.

**Entity resolution** — deciding "these addresses are probably the same owner" — fixes both. It is the
difference between "this address is suspicious" and "this *entity* is a laundering operation".

The idea that makes it possible: **to spend two outputs in one transaction you must sign with the keys
for both.** So if address A and address B appear together as inputs of the same transaction, they very
probably share an owner. That is the **common-input-ownership heuristic**, and it is the workhorse of
every commercial blockchain-analytics product.

It is also *only a heuristic*, and the interesting engineering is in how it fails.

---

## 4.2 Layer 1 — common-input ownership (hard evidence)

### How it works here

`common_input_clusters(transactions)` runs **Union-Find** (disjoint-set with path compression and union
by rank) over every transaction with two or more inputs: all inputs of that transaction are unioned into
one set. Transitivity does the rest — if A spends with B, and B later spends with C, then A, B and C end
up in one component.

Output: `{address: component_root}` plus the number of distinct components.

### Why Union-Find and not a graph traversal

Because it is the right shape for the problem: near-linear time, no recursion depth limits, and it
answers "which set is this in" in effectively constant time even as we union thousands of pairs. A
flood-fill over the graph would give the same answer more slowly and would need rebuilding per query.

### The failure mode that matters most: chain-merge collision

The heuristic is transitive, and that is a loaded gun:

```
miner pools out each block  →  300 unrelated addresses co-spend in one sweep
                            →  union-find fuses 300 unrelated "owners" into one component
```

The same thing happens with exchange withdrawals, custodial wallets, and (by design) CoinJoins. Once a
component fuses, every later co-spend of *any* member drags in everything that member ever touched. This
is called a **chain-merge collision**, and in the demo data it is not hypothetical — see §4.5.

### The two guards implemented

`_is_merge_hazard(row, n_inputs)` excludes a transaction from merging when:

1. **`n_inputs > COMMON_INPUT_MAX_INPUTS` (5).** Large consolidations are exchange/miner/pool behaviour,
   not two addresses of one person paying together.
2. **CoinJoin shape.** If the transaction has ≥5 outputs (≥`COINJOIN_MIN_OUTPUTS`) whose coefficient of
   variation is below `COINJOIN_OUTPUT_CV` (0.15) — i.e. many near-equal outputs — its inputs are
   deliberately from different owners, which is the exact opposite of ownership evidence.

In the demo run, 4 transactions were skipped by these guards (logged, never silent).

---

## 4.3 Layer 2 — Louvain communities (soft evidence)

Common-input only groups addresses that *literally co-spend*. A real criminal entity often has addresses
that never appear in the same transaction, but that fund each other, receive from each other, or move in
lockstep.

`louvain_communities(builder)` runs **Louvain community detection** (`networkx.community.louvain_communities`)
on the **wallet-to-wallet projection**: the graph collapsed so that an edge exists between wallet A and
wallet B if they ever appear in the same transaction, weighted by how much value and how often they
interact. Louvain maximises modularity — roughly, "find groups with many internal edges and few external
ones" — and it is the standard, dependency-free way to do this in NetworkX.

- `LOUVAIN_RESOLUTION = 1.0` (default resolution; higher → more, smaller communities).
- `seed = RANDOM_SEED` makes the randomised algorithm reproducible.
- It is wrapped in a try/except that falls back to plain connected components, because a degenerate graph
  must not kill the run.
- Modularity is recorded (`0.3177` in the demo) — low modularity means "there is not much real community
  structure here", which is itself useful information.

### Why Louvain is treated as *soft* evidence

Two addresses that co-spend really do share keys. Two addresses in the same Louvain community might just
be two customers of the same exchange. So Louvain labels are **reported and used for explanation**, but
they are **never used to spread risk** (§4.6). Mixing the two confidences together is how analytics tools
generate nonsense like "everyone who used this exchange is a criminal".

---

## 4.4 How the two layers are combined

`cluster_entities(builder, transactions)` produces one label per wallet, preferring hard evidence:

| Step | Who gets a label | `cluster_method` | id space |
|---|---|---|---|
| 1 | every wallet in a common-input component | `common_input` | `0, 1, 2, ...` |
| 2 | remaining wallets, grouped by their Louvain community | `louvain` | `offset + community`, where `offset = next_id + 1000` |
| 3 | remaining wallets with no community | `singleton` | `next_id + 500_000` |

The two id spaces are kept deliberately far apart so that **the id itself tells you which evidence
produced the label**: `id < 1000` = hard common-input ownership; `id ≈ 1000+n` = soft Louvain community;
`id ≥ 500000` = singleton.

Every wallet also carries `cluster_method`, and the result object carries `members`, `sizes`,
`n_common_input_clusters`, `n_louvain_communities` and `modularity`, all of which land in
`pipeline_summary.json` under stage 5 `clustering`.

Rules of thumb:
- A member of a **common-input** cluster can be treated as "same owner, probably" — with a size warning.
- A member of a **Louvain** cluster should be read as "these addresses interact a lot" — a lead, not a
  conclusion.

---

## 4.5 The collapse guard — and what actually happened in the demo

### The guard

After labelling, `propagate_cluster_risk` recomputes cluster sizes and checks:

```python
collapsed = {cid for cid, size in sizes.items() if size > config.CLUSTER_INHERIT_MAX_SIZE}  # 30
```

Any component above 30 wallets is treated as a **collapsed** component: it is excluded from risk
inheritance, and a `WARNING` is logged naming the count. It is still reported in the clustering summary,
and the dashboard still exposes it, because *an analyst should see the collision*, not have it hidden.

The alert layer is aligned with this (see Part 6): a collapsed component contributes **0.0** cluster
evidence, and the alert says so in plain English rather than quietly omitting it.

### What really happened in this dataset

Real output from the shipped demo:

```
Common-input heuristic skipped 7 merge-hazard transactions (sweeps/CoinJoins)
Clustering: 32 entities (17 from common-input, 15 Louvain communities, largest=347)
Cluster collapse guard: 1 cluster(s) exceeded 30 wallets and were excluded from risk inheritance
Cluster ownership raised risk for 15 wallets (15% inheritance discount)
```

And the actual export (`graph.json`), which the dashboard renders:

| size | cluster id | method | wallets with risk ≥ 0.5 | seeds inside |
|---|---|---|---|---|
| **347** | 0 | common_input | 4 | 4 |
| 30 | 1017 | louvain | 1 | 1 |
| 10 | 1020 | louvain | 1 | 0 |
| 10 | 1021 | louvain | 0 | 0 |
| 9 | 1019 / 1022 / 1023 | louvain | 0 | 0 |
| 8 | 1025 | louvain | 0 | 0 |
| ... | ... | ... | ... | ... |

Read that table carefully, because it is the honest story of the whole stage:

1. **The hard heuristic collided — badly.** 347 of the 516 wallets ended up in one common-input
   component. With 1,200 mostly-normal transactions over 450 human wallets, transitive co-spending
   chained almost the whole dataset together. *This is what happens in real data too*; it is exactly why
   commercial tools license human review alongside the heuristic.
2. **The guard did its job.** Because 347 > 30, that component earned **no** risk inheritance and **no**
   cluster evidence. Fifteen wallets *did* get inherited risk — from the genuine, small common-input
   components (the planted laundering ring and its neighbours among them).
3. **The soft layer still adds value.** The Louvain communities (size 2–30) are the groupings an
   investigator can actually act on, and three of them contain a seed or a high-risk wallet — those are
   the alerts that carry the "cluster contains a high-risk wallet, so the whole entity is treated as
   exposed" reason line.
4. **Nothing over-claims.** Nine alerts now carry an explicit statement that their address sits in a
   347-wallet component that the heuristic could not resolve, and that it is therefore excluded from
   ownership-based reasoning. An analyst reading it knows both the fact and the limitation.

If you want to see the collision shrink, raise `COMMON_INPUT_MAX_INPUTS` down (e.g. to 3) or lower
`CLUSTER_INHERIT_MAX_SIZE` in `config.py` and re-run — Part 9 explains the trade-offs. There is no
setting that makes transitive co-spending perfect; there is only a setting that makes it *honest*.

---

## 4.6 How clusters change the answer

Clusters are not decoration. They are consumed in four places:

| Consumer | What it does with clusters |
|---|---|
| `risk.propagate_cluster_risk` | inside **non-collapsed common-input** clusters, every member inherits `0.85 × cluster peak risk` (the 15% discount acknowledges that ownership is inferred, not observed) |
| `features.build_wallet_features` | adds `cluster_size` and `cluster_member_count` as model features — "this wallet's owner runs N addresses" is a real behavioural signal |
| `explain._score_components` | the `cluster` component: full `1.0` when a non-collapsed cluster is tainted (contains a seed or a wallet already over the alert threshold), proportional to size up to 50 otherwise, `0.0` for collapsed components |
| `explain._reason_lines` | the plain-English cluster lines, including the collapse caveat |

### A concrete before/after

Take the planted **laundering ring** (`entity_clusters[0]`, 3 wallets): it repeatedly co-spends, it
receives peeled value from chain `PC-01`, and a seed wallet pays into it. Before clustering, each of its
four addresses is just a low-signal wallet. With clustering:

- the ring is recognised as one ownership group;
- because the component the heuristic finally produced was the collapsed 347-wallet blob, inheritance was
  refused and the wallets stayed at risk 0.15–0.25 — **a deliberate non-claim**;
- the thirteen affected alerts say why, so the analyst can see the heuristic failure instead of an
  unexplained silence.

Meanwhile a wallet that co-spends inside a **clean 4-wallet component** with a risky member does get
promoted to `cluster: 1.0` — that is what "the payoff of entity clustering" means, and it is visible in
the demo as the small-size clusters in the table above.

---

## 4.7 The mental model to take away

```
common-input ownership   →  hard evidence   →  can spread risk   →  blocked above 30 wallets (collapse)
Louvain community        →  soft evidence   →  explains, ranks   →  never spreads risk
singleton                →  no evidence     →  identified as such
```

Three sentences that capture the design:

1. **Group addresses when you have cryptographic-ish evidence, and say which evidence it was.**
2. **The more you infer, the less you let it propagate.**
3. **When the inference fails, report the failure as loudly as you would report a detection.**

Continue to **[Part 5 — The threats](05_THREATS.md)**.
