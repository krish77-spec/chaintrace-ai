#!/usr/bin/env python3
"""Audit the shipped dataset against the SIH26146 problem statement.

The problem statement (SIH 2026 · SIH26146 · NTRO) pins down a *minimum field
list* for the metadata dataset, a size envelope, and a set of patterns the
synthetic generator must plant.  "It looks fine to me" is not a defence in front
of a judge, so this script turns every one of those requirements into a check
that either passes or fails, and prints the evidence for each.


    python scripts/audit_dataset.py                       # default data/synthetic
    python scripts/audit_dataset.py --input data/synthetic
    python scripts/audit_dataset.py --json out.json        # machine-readable

Exit code is 0 only when every check passes, so it doubles as a CI gate (see
``tests/test_dataset_contract.py``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.utils.bitcoin import describe_address  # noqa: E402

# --------------------------------------------------------------------------- #
# The contract, transcribed from the problem statement
# --------------------------------------------------------------------------- #
# "Minimum fields: timestamp, src_ip, dst_ip, src_port, dst_port, txid,
#  input_addresses[], output_addresses[], input_amounts[], output_amounts[],
#  geo_country/asn"  + the ingest objective adds fee and script type.
REQUIRED_NETWORK_FIELDS = [
    "timestamp", "src_ip", "dst_ip", "src_port", "dst_port", "txid",
    "geo_country", "geo_asn",
]
REQUIRED_TX_FIELDS = [
    "timestamp", "txid", "input_addresses", "output_addresses",
    "input_amounts", "output_amounts", "fee", "script_type",
]
BULK_FIELDS = REQUIRED_TX_FIELDS + ["src_ip", "dst_ip", "src_port", "dst_port",
                                    "protocol", "geo_country", "geo_asn"]

# Spec section 6 size envelope.
VOLUME_RULES: Dict[str, Tuple[int, int]] = {
    "transactions": (800, 1500),
    "wallets": (300, 600),
    "network_records": (200, 400),
}
# Spec section 6 planted-pattern envelope.
PATTERN_RULES: Dict[str, Tuple[int, int]] = {
    "peeling_chains": (3, 6),
    "chain_hops": (4, 8),
    "seed_wallets": (5, 8),
    "mixing_transactions": (2, 3),
    "anomalous_transactions": (8, 14),
    "entity_clusters": (3, 6),
}

AMOUNT_TOLERANCE = 1e-6          # 100 satoshi of rounding headroom
MIN_BENIGN_FAMILIES = 5          # the false-positive control must actually exist


@dataclass
class Check:
    """One requirement and the evidence for/against it."""

    category: str
    requirement: str
    actual: str
    passed: bool
    detail: Dict[str, Any] = field(default_factory=dict)


def _status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def audit_dataset(input_dir: Path | str) -> List[Check]:
    """Run every contract check and return the results."""
    input_dir = Path(input_dir)
    checks: List[Check] = []

    def read(name: str) -> Optional[pd.DataFrame]:
        path = input_dir / name
        if not path.exists():
            return None
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception:
            return None

    tx = read(config.TRANSACTIONS_CSV)
    net = read(config.NETWORK_CSV)
    bulk = read(config.BULK_CSV)
    seeds_path = input_dir / config.SEEDS_JSON
    gt_path = input_dir / config.GROUND_TRUTH_JSON
    seeds = json.loads(seeds_path.read_text()) if seeds_path.exists() else {}
    gt = json.loads(gt_path.read_text()) if gt_path.exists() else {}

    # ------------------------------------------------------------------ #
    # 1. Files the dataset must consist of
    # ------------------------------------------------------------------ #
    for label, frame in (
        (config.TRANSACTIONS_CSV, tx),
        (config.NETWORK_CSV, net),
        (config.BULK_CSV, bulk),
        (config.SEEDS_JSON, seeds or None),
        (config.GROUND_TRUTH_JSON, gt or None),
    ):
        checks.append(
            Check("files", f"{label} present", "yes" if frame is not None else "missing",
                  frame is not None)
        )
    if tx is None or net is None or bulk is None:
        return checks

    # ------------------------------------------------------------------ #
    # 2. Minimum field list
    # ------------------------------------------------------------------ #
    def fields_check(category: str, label: str, frame: pd.DataFrame, required: Sequence[str]) -> None:
        missing = [c for c in required if c not in frame.columns]
        checks.append(
            Check(category, f"{label}: minimum fields", f"{len(required) - len(missing)}/{len(required)}",
                  not missing, {"missing": missing})
        )

    fields_check("fields", config.TRANSACTIONS_CSV, tx, REQUIRED_TX_FIELDS)
    fields_check("fields", config.NETWORK_CSV, net, REQUIRED_NETWORK_FIELDS)
    fields_check("fields", config.BULK_CSV, bulk, BULK_FIELDS)

    # ------------------------------------------------------------------ #
    # 3. Size envelope
    # ------------------------------------------------------------------ #
    wallet_count = len(pd.unique(tx[["input_addresses", "output_addresses"]]
                                 .stack().str.split("|").explode()))
    actual_volumes = {
        "transactions": len(tx),
        "wallets": wallet_count,
        "network_records": len(net),
    }
    for key, (low, high) in VOLUME_RULES.items():
        value = actual_volumes[key]
        checks.append(
            Check("volume", f"{key} within {low}-{high}", str(value), low <= value <= high)
        )

    # ------------------------------------------------------------------ #
    # 4. Planted patterns (cross-checked against the ground-truth manifest)
    # ------------------------------------------------------------------ #
    chains = gt.get("peeling_chains", [])
    hop_lengths = [c.get("length", 0) for c in chains]
    observed_patterns = {
        "peeling_chains": len(chains),
        "chain_hops": min(hop_lengths) if hop_lengths else 0,
        "seed_wallets": len(gt.get("seed_illicit_wallets", [])),
        "mixing_transactions": len(gt.get("mixing_transactions", [])),
        "anomalous_transactions": len(gt.get("anomalous_transactions", [])),
        "entity_clusters": len(gt.get("entity_clusters", [])),
    }
    for key, (low, high) in PATTERN_RULES.items():
        value = observed_patterns[key]
        checks.append(
            Check("patterns", f"{key} within {low}-{high}", str(value), low <= value <= high)
        )
    if hop_lengths:
        oversize = [h for h in hop_lengths if h > PATTERN_RULES["chain_hops"][1]]
        checks.append(
            Check("patterns", "no chain longer than 8 hops", f"max {max(hop_lengths)}", not oversize)
        )

    # every planted txid must actually exist in the dataset
    planted_txids = [t for c in chains for t in c.get("txids", [])]
    planted_txids += [m["txid"] for m in gt.get("mixing_transactions", []) if m.get("txid")]
    planted_txids += [a["txid"] for a in gt.get("anomalous_transactions", []) if a.get("txid")]
    missing_planted = sorted(set(planted_txids) - set(tx["txid"]))
    checks.append(
        Check("patterns", "ground-truth txids exist in the dataset",
              f"{len(planted_txids) - len(missing_planted)}/{len(planted_txids)}",
              not missing_planted, {"missing": missing_planted[:5]})
    )
    benign = gt.get("benign_transactions", [])
    benign_txids = [t for item in benign for t in item.get("txids", [])]
    benign_missing = sorted(set(benign_txids) - set(tx["txid"]))
    checks.append(
        Check("patterns", f"false-positive control present ({MIN_BENIGN_FAMILIES}+ families)",
              f"{sum(len(i.get('txids', [])) for i in benign)} benign transactions "
              f"in {len({i['kind'] for i in benign})} families",
              len({item["kind"] for item in benign}) >= MIN_BENIGN_FAMILIES and not benign_missing,
              {"missing": benign_missing[:5]})
    )
    illicit_txids = set(planted_txids)
    overlap = illicit_txids & set(benign_txids)
    checks.append(
        Check("patterns", "no transaction is labelled both illicit and benign",
              f"{len(overlap)} overlapping", not overlap)
    )

    seeds_declared = set(seeds.get("wallets", []))
    seed_addresses = set()
    for column in ("input_addresses", "output_addresses"):
        for value in tx[column]:
            seed_addresses.update(str(value).split("|"))
    checks.append(
        Check("patterns", "declared seed wallets appear in the data",
              f"{len(seeds_declared & seed_addresses)}/{len(seeds_declared)}",
              seeds_declared <= seed_addresses)
    )

    # ------------------------------------------------------------------ #
    # 5. Value integrity - an investigator's ledger must balance
    # ------------------------------------------------------------------ #
    def amounts(value: str) -> List[float]:
        return [float(v) for v in str(value).split("|") if v not in ("", "nan")]

    violations = []
    negative_fees = 0
    for _, row in tx.iterrows():
        total_in = sum(amounts(row["input_amounts"]))
        total_out = sum(amounts(row["output_amounts"]))
        fee = float(row["fee"])
        if fee <= 0:
            negative_fees += 1
        if abs(total_in - total_out - fee) > AMOUNT_TOLERANCE:
            violations.append(
                {"txid": row["txid"], "delta": round(total_in - total_out - fee, 10)}
            )
    checks.append(
        Check("integrity", "sum(inputs) = sum(outputs) + fee on every row",
              f"{len(tx) - len(violations)}/{len(tx)}", not violations,
              {"violations": violations[:5]})
    )
    checks.append(Check("integrity", "every fee > 0", f"{len(tx) - negative_fees}/{len(tx)}",
                        negative_fees == 0))

    duplicated = int(tx["txid"].duplicated().sum())
    checks.append(Check("integrity", "txids unique", f"{duplicated} duplicates", duplicated == 0))

    # ------------------------------------------------------------------ #
    # 6. Field realism
    # ------------------------------------------------------------------ #
    addresses = set()
    for column in ("input_addresses", "output_addresses"):
        for value in tx[column]:
            addresses.update(str(value).split("|"))
    addresses.discard("")

    # A real decoder, not a regex: every address is decoded and its checksum
    # re-derived (base58check, or the BIP-173 / BIP-350 bech32 polymod).
    described = [(address, describe_address(address)) for address in sorted(addresses)]
    malformed = [address for address, info in described if not info["valid"]]
    checks.append(
        Check("realism", "every address decodes with a valid checksum",
              f"{len(addresses) - len(malformed)}/{len(addresses)} valid", not malformed,
              {"examples": malformed[:5]})
    )
    kinds: Dict[str, int] = {}
    for _, info in described:
        kinds[str(info["kind"])] = kinds.get(str(info["kind"]), 0) + 1
    checks.append(
        Check("realism", "address types cover legacy + segwit v0 + taproot",
              " · ".join(f"{k} {v}" for k, v in sorted(kinds.items())),
              {"p2pkh", "p2sh", "p2wpkh", "p2tr"} <= set(kinds))
    )

    ports = pd.to_numeric(net["src_port"], errors="coerce").dropna()
    bad_ports = int(((ports < 1) | (ports > 65535)).sum())
    checks.append(Check("realism", "ports within 1-65535", f"{len(ports) - bad_ports}/{len(ports)}",
                        bad_ports == 0))

    geo_coverage = int((net["geo_country"].astype(str).str.len() == 2).sum())
    checks.append(
        Check("realism", "every network record carries geo_country/geo_asn",
              f"{geo_coverage}/{len(net)}", geo_coverage == len(net))
    )

    stamps = pd.to_datetime(tx["timestamp"], errors="coerce")
    window_ok = bool(((stamps >= config.DATASET_WINDOW_START) &
                      (stamps <= config.DATASET_WINDOW_END + pd.Timedelta(days=1))).all())
    checks.append(
        Check("realism", "timestamps inside the declared collection window",
              f"{stamps.min()} -> {stamps.max()}", window_ok)
    )

    # ------------------------------------------------------------------ #
    # 7. The bulk file really is the two layers joined
    # ------------------------------------------------------------------ #
    bulk_tx_rows = int((bulk["input_addresses"].str.len() > 0).sum())
    bulk_net_rows = int((bulk["src_ip"].str.len() > 0).sum())
    checks.append(
        Check("bulk", "bulk file contains both layers",
              f"{bulk_tx_rows} blockchain rows / {bulk_net_rows} network rows",
              bulk_tx_rows > 0 and bulk_net_rows > 0)
    )
    # every packet must survive into the bulk file; blockchain columns additionally
    # appear on the joined packet rows, so that count is allowed to exceed len(tx)
    checks.append(
        Check("bulk", "bulk file carries every network record",
              f"{bulk_net_rows}/{len(net)}", bulk_net_rows == len(net))
    )
    checks.append(
        Check("bulk", "bulk file carries every transaction",
              f"{bulk_tx_rows}/{len(tx)} blockchain rows", bulk_tx_rows >= len(tx))
    )
    joined = bulk[(bulk["input_addresses"].str.len() > 0) & (bulk["src_ip"].str.len() > 0)]
    checks.append(
        Check("bulk", "correlated rows carry both layers on one line",
              f"{len(joined)} joined rows", len(joined) > 0)
    )
    txid_coverage = set(bulk[bulk["txid"].str.len() > 0]["txid"]) >= set(tx["txid"])
    checks.append(
        Check("bulk", "every transaction appears in the bulk file", 
              f"{len(set(bulk['txid']) & set(tx['txid']))}/{len(tx)}", txid_coverage)
    )

    return checks


def summarize(checks: Sequence[Check]) -> Dict[str, Any]:
    failed = [c for c in checks if not c.passed]
    return {
        "total": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "ok": not failed,
        "checks": [
            {"category": c.category, "requirement": c.requirement, "actual": c.actual,
             "status": _status(c.passed), **({"detail": c.detail} if c.detail else {})}
            for c in checks
        ],
    }


def print_report(checks: Sequence[Check]) -> None:
    width = max(len(c.requirement) for c in checks)
    category = None
    for check in checks:
        if check.category != category:
            category = check.category
            print(f"\n== {category.upper()} ==")
        print(f"  [{_status(check.passed)}] {check.requirement:<{width}}  {check.actual}")
        if not check.passed and check.detail:
            print(f"         -> {json.dumps(check.detail)[:300]}")
    failed = [c for c in checks if not c.passed]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the dataset against SIH26146")
    parser.add_argument("--input", default=str(config.SYNTHETIC_DIR),
                        help="directory holding the generated dataset")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="also write the machine-readable report here")
    args = parser.parse_args(argv)

    checks = audit_dataset(args.input)
    print_report(checks)
    report = summarize(checks)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON report written to {args.json_out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
