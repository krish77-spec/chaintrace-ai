#!/usr/bin/env python
"""Score the detectors against the generator's ground-truth manifest.

This is the honesty check that keeps the demo defensible: the synthetic dataset
knows exactly which transactions were planted as peeling hops, CoinJoins,
anomalies, clusters and seeds, so precision/recall can be measured rather than
asserted.

    python scripts/validate_detections.py
    python scripts/validate_detections.py --input data/synthetic --output data/artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.data import parsers  # noqa: E402
from src.ml import peeling as peeling_module  # noqa: E402
from src.pipeline.runner import load_alerts  # noqa: E402
from src.utils.helpers import setup_logging  # noqa: E402


def _ratio(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate detectors against planted ground truth")
    parser.add_argument("--input", type=Path, default=config.SYNTHETIC_DIR)
    parser.add_argument("--output", type=Path, default=config.ARTIFACT_DIR)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("WARNING" if args.quiet else "INFO")

    truth_path = args.input / config.GROUND_TRUTH_JSON
    if not truth_path.exists():
        print(f"No ground truth manifest at {truth_path} - run scripts/generate_sample_data.py first.")
        return 1
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    transactions, _network, _seeds = parsers.load_input_bundle(args.input)
    peel = peeling_module.detect_peeling_and_mixing(transactions, seeds=truth.get("seed_illicit_wallets", []))

    planted_chain_txs = {
        txid
        for chain in truth.get("peeling_chains", [])
        for txid in chain.get("txids", [])
    }
    detected_chain_txs = {txid for txid, flag in peel.tx_is_peel.items() if flag}

    planted_mixing = {item["txid"] for item in truth.get("mixing_transactions", [])}
    detected_mixing = {txid for txid, flag in peel.tx_is_mixing.items() if flag}

    planted_anomalies = set()
    for item in truth.get("anomalous_transactions", []):
        planted_anomalies.update(item.get("txids", [item["txid"]] if item.get("txid") else []))

    alerts = load_alerts(args.output / "alerts.json")
    alert_entities = {alert["entity"] for alert in alerts}
    planted_seeds = set(truth.get("seed_illicit_wallets", []))
    planted_cluster_wallets = {
        member for group in truth.get("entity_clusters", []) for member in group.get("members", [])
    }
    seeded_ring_wallets = {
        member
        for group in truth.get("entity_clusters", [])
        if group.get("linked_to_seed_wallet")
        for member in group.get("members", [])
    }

    report = {
        "peeling_chains": {
            "planted_chains": len(truth.get("peeling_chains", [])),
            "detected_chains": len(peel.chains),
            "planted_chain_transactions": len(planted_chain_txs),
            "detected_chain_transactions": len(detected_chain_txs),
            "true_positives": len(planted_chain_txs & detected_chain_txs),
            "recall": _ratio(len(planted_chain_txs & detected_chain_txs), len(planted_chain_txs)),
            "precision": _ratio(len(planted_chain_txs & detected_chain_txs), len(detected_chain_txs)),
        },
        "mixing": {
            "planted": len(planted_mixing),
            "detected": len(detected_mixing),
            "true_positives": len(planted_mixing & detected_mixing),
            "recall": _ratio(len(planted_mixing & detected_mixing), len(planted_mixing)),
            "precision": _ratio(len(planted_mixing & detected_mixing), len(detected_mixing)),
        },
        "alerts": {
            "total": len(alerts),
            "above_0_5": sum(1 for a in alerts if a["risk_score"] >= 0.5),
            "above_0_7": sum(1 for a in alerts if a["risk_score"] >= 0.7),
            "seeds_alerted": len(planted_seeds & alert_entities),
            "seeds_total": len(planted_seeds),
            "peeling_alerts": sum(1 for a in alerts if a.get("evidence", {}).get("peel_chain")),
            "mixing_alerts": sum(1 for a in alerts if a.get("evidence", {}).get("is_mixing")),
            "cluster_wallets_alerted": len(planted_cluster_wallets & alert_entities),
            "cluster_wallets_total": len(planted_cluster_wallets),
            "seeded_ring_members_alerted": len(seeded_ring_wallets & alert_entities),
            "seeded_ring_members_total": len(seeded_ring_wallets),
            "cluster_reasons_present": sum(
                1 for a in alerts if any("cluster of" in reason for reason in a.get("reasons", []))
            ),
            "mean_confidence": round(
                sum(a["confidence"] for a in alerts) / len(alerts), 4
            ) if alerts else 0.0,
        },
    }

    print("\nChainTrace AI - detection validation vs planted ground truth")
    print("=" * 66)
    for section, values in report.items():
        print(section.upper())
        for key, value in values.items():
            print(f"   {key:>28}: {value}")
        print("-" * 66)
    print(
        "\nNote: precision is measured against a *sample* of planted patterns, so a few "
        "unplanted-but-genuinely-suspicious transactions may count as false positives."
    )
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
