# Part 2 — The dataset, decoded

The prototype ships with a synthetic dataset because real blockchain-plus-network captures are not
distributable. This part explains exactly what is in that data, column by column, and what the
ground-truth manifest contains. Once you know this, every number in the dashboard is interpretable.

All of it is also downloadable from the console itself: Screen 1 → **📦 Take the bundled sample
dataset with you** offers the three CSVs, the seed list and the ground-truth manifest (plus the offline
GeoIP table) individually or as one zip. Nothing has to be generated first — the dataset is committed
with the repository while the analysis built on top of it is not (the reasoning for that split is in
[Part 14 §0](14_DEPLOY.md)).

---

## 2.1 `data/synthetic/transactions.csv` — 1,200 rows

The blockchain side. One row = one transaction.

| Column | Type | Meaning | Example |
|---|---|---|---|
| `timestamp` | ISO-8601 string | when the transaction was seen | `2026-08-23T00:14:49.844347` |
| `txid` | 64-hex string | transaction id (the primary key everything joins on) | `58bf99682c5d0f7bf6a0075feb34aeeb48c8c65bb4dbe19d4517fbde860282b5` |
| `input_addresses` | `\|`-separated | addresses whose coins this transaction spends | `1PsSSueqi1XiZqAxSFFopEXFaescqWouWN` |
| `output_addresses` | `\|`-separated | addresses this transaction pays | `bc1qgum75n80...|1ijZF2eddXJmp...|1CUf1dYwoGnZ...` |
| `input_amounts` | `\|`-separated BTC | value of each input, position-matched to `input_addresses` | `0.49417240` |
| `output_amounts` | `\|`-separated BTC | value of each output, position-matched to `output_addresses` | `0.14691988|0.12059023|0.22601391` |
| `fee` | BTC float | `sum(inputs) − sum(outputs)`, i.e. the miner fee | `0.000648` |
| `script_type` | enum | output script family | `P2TR` |

`script_type` values: the mainstream five (`P2PKH`, `P2SH`, `P2WPKH`, `P2WSH`, `P2TR`) plus the
deliberately unusual three (`OP_RETURN`, `MULTISIG_BARE`, `NONSTANDARD`).

**Why the `|` separator and not nested JSON:** it survives a CSV round-trip with no quoting games, and
the parsers accept `|`, `,`, `;` and JSON-list forms on input — so files from other tools load too.

### Reading a row like an investigator

Row 0 above: one address spends `0.494 BTC` and pays three addresses (`0.1469`, `0.1206`, `0.2260 BTC`)
with a `0.000648 BTC` fee, using a Taproot output. Nothing suspicious — a 1-input, 3-output ordinary
payment. This is what most of the dataset looks like on purpose: 1,200 rows of mostly-normal traffic is
the haystack the planted patterns hide in.

---

## 2.2 `data/synthetic/network_metadata.csv` — 308 rows

The network side: enriched-flow-style records.

| Column | Meaning | Example |
|---|---|---|
| `timestamp` | when the packet record was observed | `2026-08-23T00:20:19.455752` |
| `src_ip` / `dst_ip` | source and destination IP | `223.223.223.90` → `212.212.212.207` |
| `src_port` / `dst_port` | source port, destination port | `65079` → `18333` |
| `txid` | transaction id **when the record carries one** (else blank) | *(blank in the row shown)* |
| `protocol` | `BITCOIN_P2P`, `TCP`, `TLS` | `BITCOIN_P2P` |
| `geo_country` / `geo_asn` | jurisdiction and network of the **source** IP, resolved offline | `DE` / `AS24940` (Hetzner) |

`geo_country`/`geo_asn` are part of the dataset because SIH26146 lists them among the minimum
metadata fields. They are written by the generator using the *same* offline table stage 2 uses, so a
column in the file and a column produced by enrichment can never disagree; uploads that already carry
their own geo values keep them and the lookup only fills gaps.

Destination ports tell a story: `8333` (Bitcoin mainnet P2P), `18333` (testnet), `8332` (RPC — someone
talking to a node's control port), `8334` (alternate P2P), `9050` (**Tor**), plus `80`/`443`.

Record composition:

- **125 of them (42%) are deliberately correlated** with an *interesting* transaction (peeling hop,
  CoinJoin, anomaly, seed activity, cluster sweep). **64 of those carry the TXID** → `txid_exact` match at
  confidence `1.00`; the other 51 sit inside the ±90 s window → time-window match (a handful of noise
  records also land inside a window, which is exactly why every match carries a confidence).
- **The rest are noise**: no TXID, random addresses, random ports. These exist so the correlator is
  genuinely choosing, not just echoing.

### Reading a row like an investigator

`185.185.185.252 → 109.109.109.106:9050` over `BITCOIN_P2P` with **no TXID**. On its own: weak. But when
this 90-second window also contains a known-bad seed wallet funding transaction, the correlator reports
"candidate count = 1, delta = 49.6 s" and the alert explains exactly that. That is the whole point of the
time-window path: *the packet and the transaction are two halves of one event*, and neither file says so
by itself.

---

## 2.3 `data/synthetic/seed_illicit_wallets.json` — the risk bootstrap

```json
{
  "description": "Known-bad / sanctioned wallets used to seed risk propagation.",
  "generated_by": "src/data/generator.py",
  "seed": 42,
  "wallets": ["bc1qxu2npz5rjkhmf06t2t9c0lfwasasqaj2yuknms", "..."]
}
```

Eight wallet addresses. In a real deployment this file is filled from sanction lists, exchange freeze
lists and prior case work. Risk in ChainTrace AI is **relative to this file**: change the seeds and the
entire risk landscape changes. Nothing else in the system hard-codes who is bad.

---

## 2.3b `data/synthetic/bulk_metadata.csv` — the one-file view

SIH26146 describes ingesting a *bulk* metadata dataset whose minimum fields span both layers, so the
dataset ships that shape as well as the split files. 1,508 rows, 15 columns:

```
timestamp, txid, input_addresses, output_addresses, input_amounts, output_amounts, fee,
script_type, src_ip, dst_ip, src_port, dst_port, protocol, geo_country, geo_asn
```

- **1,200 transaction rows** — blockchain columns filled, network columns blank.
- **308 packet rows** — network columns filled; **64 of them also carry their transaction's blockchain
  columns**, because that packet carried the TXID. Those are the correlated rows: the join is visible on
  one line.
- **One row is one observation, and every row keeps its own clock.** Blockchain rows carry the
transaction time; packet rows carry the packet time. Collapsing the two timestamps into one would have
  looked tidier and destroyed the ±90 s correlation evidence.

Drop this single file into `data/raw/` (or upload it) and stage 1 recognises it as `mixed`, splits it back
into transactions and network records, and de-duplicates against the split files if both are present —
see §2.6 and Part 12.

---

## 2.4 `data/synthetic/planted_patterns.json` — the answer key

This is what makes the prototype *measurable* rather than merely *demonstrable*. The pipeline never reads
it; `scripts/validate_detections.py` and the test suite do.

Top-level structure:

| Key | Contents |
|---|---|
| `seed` | `42` — the RNG seed, so the whole dataset is reproducible |
| `seed_illicit_wallets` | the 5 seed addresses |
| `peeling_chains` | 5 chains, each with `chain_id`, `length`, `starts_at_seed`, `reaches_seed`, the `txids`, and per-hop detail (`from_wallet`, `continue_wallet`, `peel_wallet`, `continue_amount`, `peel_amount`, `timestamp`) |
| `mixing_transactions` | the CoinJoin transactions with `n_inputs`, `n_outputs`, timestamp, and a note |
| `anomalous_transactions` | 10 anomalies, each labelled with the anomaly `kind` and the concrete value that makes it anomalous (`fee_ratio`, `amount`, `script_type`, `burst_size`, `n_inputs`, `n_outputs`) |
| `entity_clusters` | the planted ownership groups: members, whether the group is linked to a seed, whether it receives peeled value, and a human note |
| `correlated_network_records` | every network record that *should* correlate, with `matched_by: "txid"` or `"time_window"` and the `offset_seconds` |
| `counts` | the summary of the above |
| `wallet_roles` | the 43 non-`user` wallets: `hub`, `cashout`, `illicit_seed` |

Real values from the shipped sample:

```
transactions              1200
network_records            308
bulk_records              1508      (1200 transaction rows + 308 packet rows)
wallets                    516
peeling_chain_txs           29      (chains of length 5, 7, 6, 4, 7 → 5 chains)
seed_wallets                 8
mixing_transactions          3
anomalous_transactions      10
entity_clusters              6
correlated_network_records 125
```

**Collection window.** `2026-08-23 → 2026-09-22` — the 30 days ending on the demo build date,
declared as `DATASET_WINDOW_START` / `DATASET_WINDOW_END` in `src/config.py` so the dataset is
deterministic instead of clock-dependent.

Anomaly families planted, in order: `extreme_fee_ratio`, `very_large_amount`, `unusual_script`,
`rapid_successive_small`, `extreme_fan_in`, `extreme_fan_out`, cycling.

---

## 2.5 The wallet-role model

The generator builds a small economy rather than random noise:

| Role | Share | Behaviour | Why it exists |
|---|---|---|---|
| `hub` | ~3% (≈13) | many counterparties — stands in for an exchange, a payment processor, a VPN exit | exercise hub damping in risk propagation; show why "shared an exchange" is weak evidence |
| `cashout` | ~5% (≈22) | reused as the destination of peeled value | gives peeling chains a realistic off-ramp that repeats |
| `illicit_seed` | 8 | known bad; receives funds, forwards to hubs, and is the head or tail of some peel chains | the anchor for all risk |
| `user` | the rest | ordinary traffic | the haystack |

The first planted ownership group is special: it is a **laundering ring** — it shares ownership
(repeated co-spending), it receives peeled value from a chain, and it is also paid by a seed wallet.
That is deliberate: it means entity clustering has a concrete, checkable consequence. The ring is what
turns "one risky wallet" into "a fourteen-wallet entity that is exposed".

---

## 2.6 Provenance: how the data is *supposed* to be replaced

Nothing in stages 2–8 requires the generator. The contract for real data is just:

1. A file the parsers can read (CSV/JSON/JSONL/XML) containing transactions with at least
   `txid`, `timestamp`, `input_addresses`/`inputs`, `output_addresses`/`outputs`, and amounts.
2. A file containing network records with `timestamp`, `src_ip`, `dst_ip` and (optionally) `txid` —
   optionally carrying `geo_country`/`geo_asn` of its own, in which case those values win.
3. A list of known-bad wallets.
4. **Or** a single combined file with both layers (classified as `mixed` and split row by row), which is
   how SIH26146 describes the dataset. `scripts/audit_dataset.py` checks either shape against the
   problem statement's minimum-field list — see Part 12.

Drop those into `data/raw/<your-folder>/` (or use `POST /upload`), point the pipeline at that directory,
and every stage runs unchanged. The only thing you lose is the scorecard, because there is no
`planted_patterns.json` for real data — which is exactly why the synthetic dataset exists.

Continue to **[Part 3 — How everything connects](03_PIPELINE.md)**.
