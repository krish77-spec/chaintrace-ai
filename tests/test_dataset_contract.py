"""The dataset contract, enforced as a test.

SIH26146 pins down a minimum field list, a size envelope and a set of patterns the
synthetic data must contain.  ``scripts/audit_dataset.py`` checks all of that by hand;
these tests make the same checks fail the build, so the shipped dataset cannot drift
away from the problem statement without somebody noticing.

Two things are covered:

* a freshly generated dataset at the specification's own default sizes satisfies
  every requirement in the audit;
* a single combined "bulk" export (both layers in one file, the format the problem
  statement literally describes) is split back into transactions *and* network
  records instead of losing half its columns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src import config  # noqa: E402
from src.data import parsers  # noqa: E402
from src.data.generator import SyntheticDataGenerator  # noqa: E402

import audit_dataset  # noqa: E402  (scripts/audit_dataset.py)


@pytest.fixture(scope="module")
def spec_sized_dataset(tmp_path_factory) -> Path:
    """A dataset generated at the specification's default sizes, in a temp dir."""
    output = tmp_path_factory.mktemp("spec-dataset")
    generator = SyntheticDataGenerator()
    generator.write(output)
    return output


@pytest.fixture(scope="module")
def audit_results(spec_sized_dataset: Path) -> list:
    return audit_dataset.audit_dataset(spec_sized_dataset)


def test_generated_dataset_satisfies_every_requirement(audit_results) -> None:
    report = audit_dataset.summarize(audit_results)
    failures = [f'{c["requirement"]} (actual: {c["actual"]})'
                for c in report["checks"] if c["status"] == "FAIL"]
    assert report["ok"], "dataset does not match SIH26146:\n  " + "\n  ".join(failures)
    # guard against the audit silently shrinking to nothing
    assert report["total"] >= 25


def test_every_planted_pattern_is_in_the_value_conserving_ledger(spec_sized_dataset: Path) -> None:
    """Half the demo's credibility rests on the amounts adding up."""
    transactions = pd.read_csv(spec_sized_dataset / config.TRANSACTIONS_CSV, dtype=str)
    truth = json.loads((spec_sized_dataset / config.GROUND_TRUTH_JSON).read_text())

    def amounts(value: str) -> float:
        return sum(float(v) for v in str(value).split("|") if v)

    for _, row in transactions.iterrows():
        delta = amounts(row["input_amounts"]) - amounts(row["output_amounts"]) - float(row["fee"])
        assert abs(delta) < 1e-6, f"{row['txid']} does not balance (delta {delta})"

    by_txid = set(transactions["txid"])
    planted = [t for chain in truth["peeling_chains"] for t in chain["txids"]]
    assert planted and set(planted) <= by_txid


def test_combined_bulk_export_is_split_into_both_layers(tmp_path: Path) -> None:
    """One file, both layers - the ingest path must not throw either away."""
    generator = SyntheticDataGenerator(
        n_transactions=240, n_wallets=120, n_network_records=90, seed=11
    )
    bundle = generator.generate()
    bulk_path = tmp_path / config.BULK_CSV
    bundle["bulk"].to_csv(bulk_path, index=False)

    records = parsers.read_records(bulk_path)
    assert parsers.guess_kind(records) == "mixed"

    transactions, network, _ = parsers.load_input_bundle(tmp_path)
    assert len(transactions) == len(bundle["transactions"])
    assert len(network) == len(bundle["network"])

    # the network half must keep its geo columns, and the joined rows must be there
    assert {"geo_country", "geo_asn"} <= set(network.columns)
    assert network["geo_country"].notna().all()
    joined = network["txid"].astype(str).str.len() > 0
    assert joined.any(), "no network record kept its TXID through the bulk split"

    # blockchain columns survived too
    assert {"inputs", "outputs", "fee", "script_type"} <= set(transactions.columns)


def test_split_files_and_bulk_file_agree(tmp_path: Path) -> None:
    """A directory holding both views must not double-count the network layer."""
    generator = SyntheticDataGenerator(
        n_transactions=200, n_wallets=100, n_network_records=80, seed=5
    )
    bundle = generator.generate()
    generator2 = SyntheticDataGenerator(
        n_transactions=200, n_wallets=100, n_network_records=80, seed=5
    )
    bundle2 = generator2.generate()
    bundle2["transactions"].to_csv(tmp_path / config.TRANSACTIONS_CSV, index=False)
    bundle2["network"].to_csv(tmp_path / config.NETWORK_CSV, index=False)
    bundle["bulk"].to_csv(tmp_path / config.BULK_CSV, index=False)

    transactions, network, _ = parsers.load_input_bundle(tmp_path)
    assert len(transactions) == len(bundle["transactions"])
    assert len(network) == len(bundle["network"])
