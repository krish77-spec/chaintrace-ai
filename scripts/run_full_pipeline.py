#!/usr/bin/env python
"""Run the full eight-stage pipeline from the command line.

    # on the bundled synthetic sample
    python scripts/run_full_pipeline.py

    # on your own files (CSV / JSON / XML, any mix)
    python scripts/run_full_pipeline.py --input data/raw --output data/artifacts

    # deterministic demo: regenerate the sample, then analyse it
    python scripts/run_full_pipeline.py --regenerate-sample
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.data.generator import generate_dataset  # noqa: E402
from src.pipeline.runner import run_full_pipeline  # noqa: E402
from src.utils.helpers import humanise_seconds, setup_logging  # noqa: E402


def _print_summary(summary: dict) -> None:
    print("\nChainTrace AI - pipeline summary")
    print("=" * 62)
    if summary.get("status") != "ok":
        print(f"status: {summary.get('status')} - {summary.get('reason', '')}")
        return
    print(f"duration            : {humanise_seconds(summary['duration_seconds'])}")
    print(f"input directory     : {summary['input_dir']}")
    print(f"output directory    : {summary['output_dir']}")
    print("-" * 62)
    for key, value in summary["counts"].items():
        print(f"{key:>22}: {value}")
    print("-" * 62)
    print("stage timings")
    for stage in summary.get("stages", []):
        print(f"  {stage['stage']}. {stage['name']:<18} {stage['seconds']:>7.2f}s")
    print("-" * 62)
    print("top ranked alerts")
    for alert in summary.get("alerts_preview", [])[:10]:
        print(
            f"  {alert['alert_id']}  risk={alert['risk_score']:.2f}  conf={alert['confidence']:.2f}  "
            f"{alert['entity_type']:<6} {alert['entity'][:26]:<26} {alert['top_reason'][:64]}"
        )
    print("=" * 62)
    print(f"\nArtefacts written to {summary['output_dir']} and {config.LEDGER_PATH}")
    print("Start the dashboard with:  streamlit run app/dashboard.py\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ChainTrace AI pipeline end to end")
    parser.add_argument("--input", type=Path, default=config.SYNTHETIC_DIR, help="directory with CSV/JSON/XML inputs")
    parser.add_argument("--output", type=Path, default=config.ARTIFACT_DIR, help="directory for artefacts")
    parser.add_argument("--seeds", type=Path, default=None, help="seed wallet JSON (illicit wallets)")
    parser.add_argument("--regenerate-sample", action="store_true", help="regenerate synthetic data first")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("WARNING" if args.quiet else "INFO")
    config.ensure_directories()

    if args.regenerate_sample:
        paths = generate_dataset(output_dir=config.SYNTHETIC_DIR)
        print(f"Regenerated sample data in {config.SYNTHETIC_DIR} ({len(paths)} files)")

    summary = run_full_pipeline(input_dir=args.input, output_dir=args.output, seeds_file=args.seeds)
    _print_summary(summary)
    return 0 if summary.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
