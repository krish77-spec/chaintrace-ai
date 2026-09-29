"""Tests for the seven X-factors (A-G in ``docs/13_X_FACTORS.md``).

Each test targets one feature and asserts the property that makes it worth having:

* **A** the ledger is a hash *chain*: an edit anywhere is detected, at the right entry;
* **B** the case file is a self-contained dossier carrying the alert's real evidence;
* **C** the adversarial benchmark scores both illicit recall and false positives;
* **D** the timeline helper orders nodes by their first transaction;
* **E** the infrastructure roll-up groups alerts by ASN and country;
* **F** every alert carries a counterfactual that is arithmetically consistent;
* **G** the generated addresses pass real Base58Check / bech32 validation.

The heavy fixture (one small pipeline run) is shared, and the whole ``data/`` tree is
redirected to a sandbox by ``conftest.py``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from src import config  # noqa: E402
from src.data.generator import generate_dataset  # noqa: E402
from src.graph.builder import load_graph_payload  # noqa: E402
from src.pipeline.runner import load_alerts, run_full_pipeline  # noqa: E402
from src.utils.bitcoin import (  # noqa: E402
    base58check_decode,
    bech32_decode,
    is_valid_address,
    p2pkh_address,
    p2wpkh_address,
)
from src.utils.casefile import build_case_file  # noqa: E402
from src.utils.hashing import (  # noqa: E402
    GENESIS_HASH,
    read_ledger,
    tamper_demo,
    verify_ledger_chain,
    write_ledger,
)
from src.utils.helpers import first_seen_by_edge, sortable_stamp  # noqa: E402
from src.utils.infrastructure import focus_list, rollup  # noqa: E402

# BIP-173 test vector: the P2WPKH address for hash160 751e76e8199196d454941c45d1b3a323f1433bd6
BIP173_ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
BIP173_PROGRAM = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """One small but complete pipeline run the X-factor tests can inspect."""
    base = tmp_path_factory.mktemp("xfactor")
    synthetic = base / "synthetic"
    artifacts = base / "artifacts"
    generate_dataset(
        output_dir=synthetic,
        n_transactions=400,
        n_wallets=180,
        n_network_records=120,
        seed=11,
    )
    run_full_pipeline(input_dir=synthetic, output_dir=artifacts)
    return {"base": base, "synthetic": synthetic, "artifacts": artifacts}


# --------------------------------------------------------------------------- #
# G - Bitcoin-correct encodings
# --------------------------------------------------------------------------- #
def test_generated_addresses_pass_bitcoin_checksums(workspace):
    import pandas as pd

    frame = pd.read_csv(workspace["synthetic"] / "transactions.csv")
    addresses = set()
    for column in ("input_addresses", "output_addresses"):
        for cell in frame[column].dropna():
            addresses.update(
                part.strip()
                for part in str(cell).replace("|", ",").split(",")
                if part.strip()
            )
    assert addresses, "the sample must contain addresses to validate"

    invalid = sorted(address for address in addresses if not is_valid_address(address))
    assert not invalid, f"{len(invalid)} addresses fail checksum validation, e.g. {invalid[:3]}"

    # the checksum is real maths, not a prefix: a single flipped character must fail
    sample = sorted(addresses)[0]
    broken = sample[:-1] + ("1" if sample[-1] != "1" else "2")
    assert not is_valid_address(broken)


def test_base58check_and_bech32_round_trip():
    hash160 = bytes.fromhex("010966776006953d5567439e5e39f86a0d273bee")
    address = p2pkh_address(hash160)
    version, payload = base58check_decode(address)
    assert version == 0x00 and payload == hash160

    hrp, witness_version, program, spec = bech32_decode(BIP173_ADDRESS)
    assert (hrp, witness_version, program, spec) == ("bc", 0, BIP173_PROGRAM, "bech32")
    assert p2wpkh_address(BIP173_PROGRAM) == BIP173_ADDRESS
    assert is_valid_address(BIP173_ADDRESS)


# --------------------------------------------------------------------------- #
# A - tamper-evident ledger
# --------------------------------------------------------------------------- #
def test_ledger_is_a_chain_and_detects_an_edited_entry(workspace, tmp_path):
    alerts = load_alerts(workspace["artifacts"] / "alerts.json")
    assert len(alerts) >= 4

    ledger_path = tmp_path / "ledger.jsonl"
    written = write_ledger(alerts, ledger_path)
    assert written == len(alerts)

    # the file starts with a header line describing the ledger itself
    entries = [entry for entry in read_ledger(ledger_path) if entry.get("alert_id")]
    assert len(entries) == len(alerts)
    assert entries[0]["prev_hash"] == GENESIS_HASH
    assert all(entry.get("entry_hash") for entry in entries)
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]

    report = verify_ledger_chain(ledger_path)
    assert report["ok"] and report["chained"] and report["first_break"] is None

    # edit one historical entry the way an insider would: make the alert look harmless
    middle = len(entries) // 2
    victim_id = entries[middle]["alert_id"]
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    edit_at = next(
        index
        for index, line in enumerate(lines)
        if line.strip() and json.loads(line).get("alert_id") == victim_id
    )
    record = json.loads(lines[edit_at])
    record["risk_score"] = 0.0
    lines[edit_at] = json.dumps(record)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    broken = verify_ledger_chain(ledger_path)
    assert not broken["ok"]
    # positions are 1-based over the sealed entries, so the edit is reported exactly
    assert broken["first_break"] == middle + 1
    assert any(item["position"] == middle + 1 for item in broken["breaks"])

    demo = tamper_demo(ledger_path)
    assert demo["detected"] is True
    assert demo["first_break"] is not None


# --------------------------------------------------------------------------- #
# B - one-click case file
# --------------------------------------------------------------------------- #
def test_case_file_is_self_contained_and_escapes_html(workspace):
    alerts = load_alerts(workspace["artifacts"] / "alerts.json")
    alert = dict(alerts[0])
    alert["reasons"] = list(alert.get("reasons") or []) + ["<script>alert(1)</script>"]

    case_file = build_case_file(alert, load_graph_payload())
    assert set(case_file) >= {"html", "markdown", "filename"}
    assert case_file["filename"].startswith(alert["alert_id"])

    html = case_file["html"]
    assert html.startswith("<!doctype html>")
    assert "<script>alert(1)</script>" not in html          # escaped, not executed
    assert "&lt;script&gt;" in html
    assert alert["evidence_hash"] in html
    assert "What would clear this entity" in html

    markdown = case_file["markdown"]
    assert alert["evidence_hash"] in markdown
    assert markdown.startswith("# ChainTrace AI")


# --------------------------------------------------------------------------- #
# C - adversarial precision benchmark
# --------------------------------------------------------------------------- #
def test_precision_benchmark_scores_illicit_and_benign(workspace):
    import benchmark_detectors

    report = benchmark_detectors.benchmark(workspace["synthetic"], workspace["artifacts"])
    population = report["population"]
    assert population["labelled_illicit"] > 0
    assert population["labelled_benign"] > 0

    for level in ("signal_level", "lead_level"):
        for system in ("rules", "chaintrace"):
            row = report[level][system]
            assert 0.0 <= row["recall_on_illicit"] <= 1.0
            assert 0.0 <= row["false_positive_rate"] <= 1.0
            assert 0 <= row["benign_flagged"] <= row["benign_total"]

    families = {item["family"]: item for item in report["per_benign_family"]}
    assert {"exchange_sweep", "payroll_fanout", "batching_wallet"}.issubset(families)
    assert all(item["planted"] > 0 for item in report["per_benign_family"])
    assert "innocent look-alikes" in report["verdict"]


# --------------------------------------------------------------------------- #
# D - timeline replay helper
# --------------------------------------------------------------------------- #
def test_first_seen_orders_nodes_by_their_first_transaction(workspace):
    payload = load_graph_payload()
    edges = payload["edges"]
    first_seen = first_seen_by_edge(edges)

    assert first_seen, "every node must have a first appearance"
    assert min(first_seen.values()) <= max(first_seen.values())

    # the node is never seen before one of its own edges (the edge stamp is normalised the
    # same way the helper normalises it - see the spelling test below for why)
    for edge in edges[:200]:
        stamp = edge.get("timestamp")
        if not stamp:
            continue
        assert first_seen[edge["source"]] <= sortable_stamp(stamp)
        assert first_seen[edge["target"]] <= sortable_stamp(stamp)

    # playing the window only ever adds to the picture
    ticks = sorted(set(first_seen.values()))
    seen_counts = [
        sum(1 for value in first_seen.values() if value <= tick) for tick in ticks
    ]
    assert seen_counts == sorted(seen_counts)
    assert seen_counts[0] >= 1
    assert seen_counts[-1] == len(first_seen)

    # ...and the ticks are in *calendar* order, which is only true if every stamp in the
    # payload is written in one spelling.  The graph export used to mix `str(pd.Timestamp)`
    # (space) with `.isoformat()` (T); 'T' sorts after every digit, so the sorted ticks put a
    # later space-separated instant *before* an earlier T-separated one and "step back one
    # event" moved the clock forward.
    stamps = [datetime.fromisoformat(value) for value in ticks]
    assert stamps == sorted(stamps)
    assert stamps[-1] == max(stamps)


def test_first_seen_treats_both_timestamp_spellings_as_one_instant():
    """A space-separated stamp and a 'T' stamp must be comparable as instants.

    This is the bug the seek bar exposed: the artefacts carried both spellings, and the
    replay's ticks and cutoffs are string comparisons, so the two orders disagreed.
    """
    mixed = [
        {"source": "late", "target": "tx", "timestamp": "2026-09-21 23:22:52.125681"},
        {"source": "early", "target": "tx", "timestamp": "2026-09-21T06:11:03.070041"},
        {"source": "first", "target": "tx", "timestamp": "2026-08-23T00:21:23.358190"},
    ]
    first_seen = first_seen_by_edge(mixed)

    assert first_seen["late"] == "2026-09-21T23:22:52.125681"
    assert first_seen["early"] == "2026-09-21T06:11:03.070041"
    ticks = sorted(set(first_seen.values()))
    assert ticks == [
        "2026-08-23T00:21:23.358190",
        "2026-09-21T06:11:03.070041",
        "2026-09-21T23:22:52.125681",
    ]
    # the cutoff the replay uses is a string `<=`: now it agrees with the calendar
    assert first_seen["early"] <= ticks[-1]
    assert not first_seen["late"] <= ticks[1]



# --------------------------------------------------------------------------- #
# E - infrastructure roll-up
# --------------------------------------------------------------------------- #
def test_infrastructure_rollup_groups_providers_and_countries(workspace):
    alerts = load_alerts(workspace["artifacts"] / "alerts.json")
    correlated = []
    if config.CORRELATED_PATH.exists():
        import pandas as pd

        correlated = pd.read_csv(config.CORRELATED_PATH).fillna("").to_dict(orient="records")

    report = rollup(alerts, correlated)
    assert report["headline"]
    assert report["totals"]["alerts"] == len(alerts)

    providers = report["providers"]
    if providers:
        counts = [row["alerts"] for row in providers]
        assert counts == sorted(counts, reverse=True)
        # every provider that is listed was actually touched by an alert
        assert all(row["alerts"] > 0 for row in providers)
        assert all(row["key"] for row in providers)

        focus = focus_list(report, limit=3)
        assert [row["rank"] for row in focus] == list(range(1, len(focus) + 1))
        assert all(row["action"] for row in focus)

    countries = report["countries"]
    assert [row["alerts"] for row in countries] == sorted(
        (row["alerts"] for row in countries), reverse=True
    )
    # a country that appears in the evidence is always reached through some provider
    assert all(row["providers"] > 0 for row in countries)


# --------------------------------------------------------------------------- #
# F - counterfactual explanations
# --------------------------------------------------------------------------- #
def test_every_alert_carries_a_consistent_counterfactual(workspace):
    alerts = load_alerts(workspace["artifacts"] / "alerts.json")
    assert alerts

    for alert in alerts:
        rows = alert.get("counterfactuals")
        assert isinstance(rows, list) and rows, f"{alert['alert_id']} has no counterfactual"
        summary = alert.get("counterfactual_summary") or ""
        assert summary.strip()

        for row in rows:
            assert row.get("label") and row.get("evidence")
            assert 0.0 <= float(row["score_without"]) <= 1.0
            # removing evidence can never make the alert stronger
            assert float(row["score_without"]) <= float(alert["risk_score"]) + 1e-9
            assert float(row["score_drop"]) >= -1e-9
            # "decisive" means exactly this: without it the entity is not an alert
            if row.get("decisive"):
                assert not row.get("still_flagged")
                assert float(row["score_without"]) < float(alert["risk_score"])

    # and at least one alert in the sample is decided by a single piece of evidence
    decisive = [
        alert for alert in alerts
        if any(row.get("decisive") for row in alert.get("counterfactuals") or [])
    ]
    assert decisive, "the sample should contain at least one decisive counterfactual"
