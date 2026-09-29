#!/usr/bin/env python
"""Generate the synthetic sample dataset (stage 1 input).

    python scripts/generate_sample_data.py --transactions 1200 --wallets 450 --network 320

Writes network_metadata.csv, transactions.csv, seed_illicit_wallets.json and
planted_patterns.json (the ground-truth manifest) into ``data/synthetic/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.data.generator import generate_dataset  # noqa: E402
from src.utils.helpers import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the ChainTrace AI synthetic Bitcoin dataset")
    parser.add_argument("--transactions", type=int, default=config.DEFAULT_TX_COUNT)
    parser.add_argument("--wallets", type=int, default=config.DEFAULT_WALLET_COUNT)
    parser.add_argument("--network", type=int, default=config.DEFAULT_NETWORK_COUNT)
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    parser.add_argument("--output-dir", type=Path, default=config.SYNTHETIC_DIR)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("WARNING" if args.quiet else "INFO")
    config.ensure_directories()

    paths = generate_dataset(
        output_dir=args.output_dir,
        n_transactions=args.transactions,
        n_wallets=args.wallets,
        n_network_records=args.network,
        seed=args.seed,
    )
    manifest = json.loads(Path(paths["ground_truth"]).read_text(encoding="utf-8"))
    counts = manifest.get("counts", {})

    print("\nGenerated synthetic dataset")
    print("-" * 46)
    for name, path in paths.items():
        print(f"{name:>14}: {path}")
    print("-" * 46)
    for key, value in counts.items():
        print(f"{key:>28}: {value}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
