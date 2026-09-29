# 12 · Dataset audit — does the data actually match SIH26146?

The prototype's credibility rests on one claim: *the synthetic dataset contains every
field the problem statement asks for, at the size it asks for, with the crime patterns
it asks for.* This part turns that claim into 32 machine-checked assertions, records
what failed before, and states plainly what the checks cannot prove.

Run it yourself (from the repository root):

```bash
python scripts/generate_sample_data.py     # rebuild the dataset from scratch
python scripts/audit_dataset.py            # 32 contract checks, exit 0 only if all pass
python scripts/audit_dataset.py --json data/artifacts/dataset_audit.json
```

The same checks are enforced by `tests/test_dataset_contract.py`, so the dataset cannot
drift away from the problem statement without the test suite going red.

---

## 1. What the problem statement actually requires

The SIH26146 problem statement (SIH 2026 · NTRO · Cryptocurrency) says, verbatim:

> **Dataset: Parameters & Synthetic Generation** — Participants will work with a
> synthetic dataset modelled on real Bitcoin P2P/transaction fields (no real seized or
> live-intercept data will be provided). Minimum fields: `timestamp`, `src_ip`,
> `dst_ip`, `src_port`, `dst_port`, `txid`, `input_addresses[]`, `output_addresses[]`,
> `input_amounts[]`, `output_amounts[]`, `geo_country/asn` (integrate open source
> downloadable Geo IP database).

and, in the objective:

> Ingest & parse a bulk metadata dataset (`timestamp`, `src/dst IP & port`, `TXID`,
> `input/output wallet addresses`, `amounts`, `fee`, `script type`).

Three things follow, and all three were previously only *partly* true of our data:

1. **One field list spanning both layers.** Network-layer identifiers and blockchain-layer
   amounts are listed together, in one sentence, in one dataset.
2. **`geo_country` / `geo_asn` is a dataset field**, not just a derived column.
3. **The dataset is described as *bulk*** — the corridor from one file to the graph has to
   work when the two layers arrive in the same table.

## 2. What was wrong

| # | Finding | Severity | Why it mattered |
|---|---|---|---|
| 1 | `geo_country` / `geo_asn` were **absent** from the shipped CSVs (added later by stage 2) | High | A judge comparing our file header against the minimum-field list sees two missing columns immediately. |
| 2 | There was **no single-file dataset**. Two CSVs only. | High | The stated ingest path is a bulk export; nothing in the repo proved we could ingest one. Feeding both layers in one table silently discarded the network half. |
| 3 | Supplied geo columns were **ignored** on ingest and then **overwritten** by the mock lookup | Medium | "Integrate an open source GeoIP database" implies respecting what the data already says. |
| 4 | **2 of 1,200** transactions did not balance: `sum(outputs) > sum(inputs) − fee` | Medium | The CoinJoin generator jittered each output ±1% and never rescaled to the spendable pool. An investigator's ledger that does not balance is a credibility problem. |
| 5 | Planted patterns sat at the **floor** of every required range: 3 chains, 5 seeds, 2 mixing transactions | Medium | Meets the letter of the spec, shows none of its intent. |
| 6 | The data window was hard-coded to **March 2026** | Low | Collection data that predates the problem statement by six months invites the question "is this real?" |
| 7 | A dead loop in `_build_network_records` (`for … : pass`) | Cosmetic | Looked like an unfinished thought. |

## 3. What the dataset is now

Regenerated from scratch — `RANDOM_SEED = 42`, one command, byte-reproducible.

**Collection window:** 2026-08-23 → 2026-09-21 (the 30 days ending on the demo build date).
Declared in `src/config.py` as `DATASET_WINDOW_START` / `DATASET_WINDOW_END` so it is
deterministic rather than clock-dependent.

| File | Rows | Columns |
|---|---|---|
| `data/synthetic/transactions.csv` | 1,200 | the 8 blockchain fields |
| `data/synthetic/network_metadata.csv` | 308 | 8 network fields **+ `geo_country`, `geo_asn`** |
| `data/synthetic/bulk_metadata.csv` | 1,508 | **all 15 fields in one table** — 1,200 transaction rows + 308 packet rows, of which **64 packet rows also carry their transaction's blockchain columns** on the same line |
| `data/synthetic/seed_illicit_wallets.json` | 8 wallets | known-bad seed list |
| `data/synthetic/planted_patterns.json` | manifest | every planted txid, chain, hop and anomaly — the ground truth the detectors are graded against |

**Size envelope** (spec: 800–1,500 transactions, 300–600 wallets, 200–400 network records):

```
transactions      1,200    ✓
wallets             516    ✓
network records     308    ✓
```

**Planted patterns** (all confirmed against the manifest, not asserted):

```
peeling chains          5   (spec 3–6)   — 29 chain transactions, hop counts 5/7/6/4/7, all 5 touch a seed
seed wallets            8   (spec 5–8)
mixing structures       3   (spec 2–3)   — ≥5 inputs, ≥5 near-equal outputs, distinct inputs
anomaly families       10   (spec 8–14)  — extreme fee, huge amount, unusual script, burst,
                                            fan-in, fan-out
entity clusters         6   (spec 3–6)   — repeated co-spend groups
network correlations  132   planted, ±30–120 s around the suspicious transactions
```

**The bulk file's shape.** One row is one *observation*, and each keeps its own clock:
blockchain rows carry the transaction time, packet rows carry the packet time. When a
packet already knows its TXID, the transaction's columns are copied onto that packet row,
so the file shows the join on one line — **64** such joined rows. Collapsing both
timestamps into one would have looked tidier and destroyed the ±90 s correlation evidence.

## 4. The 35 checks

```
== FILES ==        transactions.csv · network_metadata.csv · bulk_metadata.csv ·
                   seed_illicit_wallets.json · planted_patterns.json          5/5
== FIELDS ==       minimum field list present in all three data files           3/3
== VOLUME ==       transactions 800–1500 · wallets 300–600 · network 200–400    3/3
== PATTERNS ==     chains 3–6 · hops 4–8 · seeds 5–8 · mixing 2–3 ·
                   anomalies 8–14 · clusters 3–6 · no chain > 8 hops ·
                   every ground-truth txid present (46/46) · a benign
                   control set exists · no illicit/benign overlap ·
                   every declared seed present                           11/11
== INTEGRITY ==    sum(inputs) = sum(outputs) + fee on 1200/1200 rows ·
                   fees > 0 · txids unique                                  3/3
== REALISM ==      every address decodes with a valid checksum (516/516) ·
                   legacy + segwit v0 + taproot all present ·
                   ports 1–65535 (308/308) · geo on 308/308 records ·
                   timestamps inside the declared window                    5/5
== BULK ==         both layers present · every packet kept · every transaction
                   kept · joined rows carry both layers · txid coverage   5/5
```

`35/35 checks passed`. The failures that existed before this pass were: fields 2 missing,
patterns 3 of 9 at the floor, integrity 2 violations, and the whole BULK category (the
file did not exist).

## 5. Detection quality after the rebuild

Graded against the generator's manifest (`scripts/validate_detections.py`), not asserted:

| Detector | Planted | Detected | Precision | Recall |
|---|---|---|---|---|
| Peeling chains | 5 chains / 29 transactions | 5 / 29 | **1.00** | **1.00** |
| Mixing (CoinJoin-like) | 3 | 3 | **1.00** | **1.00** |
| Seed wallets alerted | 8 | 8 | — | 1.00 |
| Anomaly families flagged | 10 families | 72 transactions in the top 6% | — | — |

And the run-level numbers the rest of the documentation quotes:

```
1,200 transactions · 308 network records · 516 wallets · 141 correlated pairs
(64 exact-TXID, 77 time-window) · 1,872 graph nodes / 4,220 edges
32 clusters · 5 peel chains · 3 mixing · 60 ranked alerts (55 above 0.7)
ledger: 60 entries, 60 hashes verified · pipeline 2.6 s
```

## 6. What this audit does **not** prove

Being explicit about this is the difference between a demo and a claim:

- **Precision is measured against planted patterns, not against real criminal behaviour.**
  1.00 precision means "we did not invent a chain that wasn't planted". It does not mean
  1.00 precision on real chain data, where the same heuristics produce false positives
  (exchange sweeps, batching wallets). The CoinJoin detector's `distinct inputs` guard is
  a real attempt at that problem, not a solution to it.
- **The GeoIP table is deterministic mock data** unless a real `GeoLite2-*.mmdb` is dropped
  into `data/geo/`. The *integration* is real (MaxMind reader first, hash table as
  fallback); the *answers* in the shipped dataset are synthetic. Their job is to make the
  country/ASN evidence path testable offline.
- **Volume is prototype scale.** 1,200 transactions is a laboratory, not a mempool.
- **The accountant's check ≠ an accountant's audit.** Value conservation and address
  encoding are verified; script-level validity (signatures, scriptPubKey derivation) is modelled,
  because the data is synthetic.

## 7. Change log for this pass

| File | Change |
|---|---|
| `src/config.py` | dataset window (`DATASET_WINDOW_*`), `BULK_CSV`, `GEO_DATASET_COLUMNS`, pattern floors raised into the middle of the spec ranges |
| `src/data/generator.py` | geo columns on every network record (resolved through the same offline table stage 2 uses), bulk single-file writer, CoinJoin value-conservation fix, dead loop removed, window + counts in the manifest |
| `src/data/parsers.py` | `geo_country`/`geo_asn` in the network schema, geo aliases in the ingest mapping, `mixed` source detection, per-row split of bulk exports, network de-duplication across the two views |
| `src/data/enrich.py` | provided geo values win; the lookup only fills gaps |
| `scripts/audit_dataset.py` | **new** — the 32 checks, `--json` report, exit code as a CI gate |
| `tests/test_dataset_contract.py` | **new** — audit enforced in the suite, plus the bulk-split and no-double-count paths |

---

**Next:** *[Part 13 — X-factors](13_X_FACTORS.md)* lists the bolt-on capabilities that
would move this from "satisfies the brief" to "hard to copy" — each with the honest cost.
