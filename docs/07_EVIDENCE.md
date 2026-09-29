# Part 7 — Evidence and integrity

An investigative tool that cannot show its work is an opinion generator. This part explains the
mechanism that makes every ChainTrace AI alert auditable, and — just as importantly — what it does and
does not prove.

Code: `src/utils/hashing.py` (209 lines), used by `src/pipeline/runner.py` (stage 8) and
`src/api/routes.py` (`/ledger`, `/ledger/verify`), rendered by the dashboard's **Evidence & Integrity**
tab.

---

## 7.1 The alert pipeline

```
alert dict (entity, score, reasons, evidence, top_features)
        │
        ├─ build_evidence_package(alert)      drop volatile fields
        │        evidence_hash, hash_verified_at, generated_at
        │
        ├─ canonical_json(package)           sorted keys, deterministic separators
        │
        ├─ sha256_hex(...)                   64 hex characters
        │
        ├─ stamped["evidence_hash"] = ...    written back onto the alert
        │
        └─ build_ledger_entry(alert)         {alert_id, entity, hash, timestamp, risk_score}
                 │
                 └─ append to data/evidence_ledger.jsonl   (exactly one line, one handle)
```

Three details do all the work:

**Canonical serialisation.** Hashing `json.dumps(dict)` is a trap: dictionaries preserve insertion order,
floats format inconsistently, and `NaN`/`numpy` types are not JSON at all. `canonical_json` sorts keys and
uses fixed separators, and `_json_default` converts numpy/pandas/Timestamp values, so the same logical
package always produces the same bytes — on any machine, in any process.

**Volatile fields are excluded.** `generated_at` changes every run, and `evidence_hash` cannot be part of
what it hashes. Stripping both means re-running the same analysis over the same input produces the *same*
hash, which is what makes verification meaningful rather than decorative.

**One entry, one handle.** `write_ledger` truncates the file, writes one header line, then writes one line
per alert through a single open handle. An earlier version also called `append_ledger_entry` inside the
loop, which opened a *second* handle on the same file: two offset streams interleaving by accident,
producing duplicated or clobbered lines. It happened to look correct, which is the worst kind of bug.
There is now a regression test that asserts *exactly* one header plus N unique alert lines.

---

## 7.2 The ledger format

`data/evidence_ledger.jsonl` — newline-delimited JSON, one record per line:

```json
{"hash_algorithm": "SHA-256", "created_at": "2026-09-18T06:52:57.622451+00:00",
 "note": "Append-only evidence ledger. One line per alert.", "system": "ChainTrace AI",
 "type": "ledger_header"}
{"alert_id": "CT-W-1E60A69D", "entity": "bc1qxu2npz5rjkhmf06t2t9c0lfwasasqaj2yuknms",
 "hash": "c64ffa732a46e378d14c4f4e1711ab7c3186a4a2e9d16232a145c63b3ff94e3f",
 "risk_score": 0.95, "timestamp": "2026-09-18T06:52:57.624031+00:00"}
...
```

One run = one coherent ledger (a fresh run rewrites the file, because mixing datasets in one ledger is
meaningless). 60 alerts → 1 header + 60 data lines.

---

## 7.3 Verification, in three layers

**Layer 1 — the alert's own hash.** `verify_evidence_hash(alert)` recomputes SHA-256 over the alert's
evidence package and compares it with the stored `evidence_hash`. If someone edits a reason line, a score
or a feature contribution, the hash changes and the alert no longer verifies.

**Layer 2 — the ledger.** `verify_ledger(alerts)` compares the alerts file against the ledger and reports:

| Field | Meaning |
|---|---|
| `ledger_entries` | how many alert lines the ledger has |
| `alerts_checked` | how many alerts were hashed and compared |
| `verified` | alerts whose stored hash matches both the recomputation **and** the ledger |
| `mismatched` | alerts whose hash disagrees with the ledger or with the recomputation |
| `missing_from_ledger` | alerts with no ledger line — i.e. someone added an alert |
| `ok` | true only when everything matches |

**Layer 3 — the UI.** The dashboard's **Evidence & Integrity** tab shows those counts, a per-alert
*Recompute SHA-256* button, and the raw ledger lines. In the verified demo state it reads
**entries: 60, verified: 60, mismatched: 0, ✔ ledger consistent**.

You can also check it from outside the app:

```bash
curl -s http://localhost:8000/ledger/verify | python3 -m json.tool
```

---

## 7.4 What this proves — and what it does not

**It proves:** the alert content served by the app is byte-for-byte the content that was hashed and
recorded at stage 8. If a number in an alert changed after the pipeline ran, the dashboard will say so.
That is a real, useful property for a demo to a judge, an auditor or a court: the evidence package has a
verifiable fingerprint.

**It does not prove:**

- **That the analysis was correct.** A hash proves integrity, not truth. A wrong conclusion hashes
  perfectly.
- **That the ledger is complete.** A chain of hashes with no external anchor cannot detect the *deletion*
  of an entry, and this ledger has no previous-hash field linking entries together. Adding a
  `previous_hash` per line would turn it into a hash chain with tamper-evident ordering — a natural next
  step (see the note at the end of this Part).
- **That nobody with write access could rewrite everything.** Anyone who can run the pipeline can
  regenerate the whole ledger. Real tamper-evidence needs the head hash anchored somewhere the operator
  does not control (a signed release, a timestamp authority, another system's log).

So the honest claim is: **the ledger makes accidental or casual modification detectable, and gives every
alert a stable fingerprint to cite in a report.** It is not a notary, and the guide should not pretend
otherwise. Stating the boundary clearly is what makes the claim credible.

---

## 7.5 The dashboard is a client, not a source of truth (a bug worth knowing)

The Evidence tab originally reported **50 mismatched ledger entries** on a perfectly good run. The cause
was subtle and is worth recording, because it is a trap any two-layer app can fall into:

- `GET /alerts` returns **trimmed summaries** — deliberately, to keep the list payload small: no
  `evidence`, no `reasons`.
- The dashboard preferred the API, then re-verified SHA-256 over those *trimmed* dicts.
- Every hash therefore disagreed with the ledger, and the flagship integrity feature reported tampering
  when nothing was wrong.

The fix: verification always fetches **full alert packages** (`GET /alerts/{id}`) or reads the local
`alerts.json`, and the summary/list path is never used for hashing. Two lessons the team should keep:
never hash a projection of a record, and make sure the UI and the API agree about which representation
they are talking about.

Continue to **[Part 8 — Runbook and dashboard tour](08_RUNBOOK.md)**.
