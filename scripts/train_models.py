#!/usr/bin/env python
"""Train and persist the anomaly-detection models, offline.

    python scripts/train_models.py                 # on the bundled synthetic sample
    python scripts/train_models.py --input data/raw

This is the stage-5a path executed on its own (the spec requires every stage to be
runnable individually as well as through the pipeline).  It reuses the same
parsers / graph / feature modules, fits Isolation Forest (+ LOF) on the
transaction and wallet feature matrices, and saves ``.joblib`` bundles into
``data/models/`` so a demo run never has to retrain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.correlation.correlator import correlate  # noqa: E402
from src.data import enrich as enrich_module  # noqa: E402
from src.data import parsers  # noqa: E402
from src.graph.builder import GraphBuilder  # noqa: E402
from src.ml import anomaly as anomaly_module  # noqa: E402
from src.ml import features as features_module  # noqa: E402
from src.utils.helpers import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the ChainTrace AI anomaly models offline")
    parser.add_argument("--input", type=Path, default=config.SYNTHETIC_DIR)
    parser.add_argument("--seed-json", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("WARNING" if args.quiet else "INFO")
    config.ensure_directories()

    transactions, network, discovered_seeds = parsers.load_input_bundle(args.input)
    if transactions.empty:
        print(f"No transactions found in {args.input} - run scripts/generate_sample_data.py first.")
        return 1

    seeds: list = list(discovered_seeds)
    seed_path = args.seed_json or (args.input / config.SEEDS_JSON)
    if seed_path.exists():
        payload = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("wallets"):
            seeds = [str(w) for w in payload["wallets"]]

    if not network.empty:
        network = enrich_module.enrich_network(network, enrich_module.GeoIPEnricher())
    correlated = correlate(transactions, network)
    builder = GraphBuilder(transactions, correlated, seeds=seeds).build()
    engineer = features_module.FeatureEngineer(builder, correlated)

    tx_features = engineer.build_transaction_features()
    tx_columns = [c for c in features_module.TX_FEATURE_COLUMNS if c in tx_features.columns]
    tx_result = anomaly_module.detect_anomalies(
        tx_features, tx_columns, id_column="txid", name="transaction",
        save_path=config.MODEL_DIR / "transaction_anomaly_model.joblib",
    )

    wallet_features = engineer.build_wallet_features()
    wallet_columns = [c for c in features_module.WALLET_FEATURE_COLUMNS if c in wallet_features.columns]
    wallet_result = anomaly_module.detect_anomalies(
        wallet_features, wallet_columns, id_column="node_id", name="wallet",
        save_path=config.MODEL_DIR / "wallet_anomaly_model.joblib",
    )

    meta = {
        "transaction_model": {
            "features": tx_columns,
            "flagged": tx_result.summary()["flagged"],
            "scored": tx_result.summary()["scored"],
            "top_features": sorted(
                tx_result.feature_importance.items(), key=lambda kv: kv[1], reverse=True
            )[:8],
        },
        "wallet_model": {
            "features": wallet_columns,
            "flagged": wallet_result.summary()["flagged"],
            "scored": wallet_result.summary()["scored"],
            "top_features": sorted(
                wallet_result.feature_importance.items(), key=lambda kv: kv[1], reverse=True
            )[:8],
        },
        "contamination": config.ISOLATION_FOREST_CONTAMINATION,
        "lof_enabled": config.USE_LOF_MODEL,
    }
    (config.MODEL_DIR / "training_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("\nTrained ChainTrace AI anomaly models")
    print("=" * 58)
    print(f"transaction model : {tx_result.summary()}")
    print(f"wallet model      : {wallet_result.summary()}")
    print("-" * 58)
    print("strongest transaction features:")
    for name, weight in meta["transaction_model"]["top_features"]:
        print(f"   {name:>26}: {weight:.4f}")
    print("-" * 58)
    print(f"saved to {config.MODEL_DIR}")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
