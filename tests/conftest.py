"""Session-wide test isolation.

The pipeline writes some artefacts to the *output directory* it is given and others to
well-known paths under ``data/`` (the graph export, the trained models, the feature CSVs
and the evidence ledger).  The test suite runs the real pipeline, so without this fixture
running ``pytest`` overwrote the artefacts of the last real demo run — leaving the
dashboard comparing a fresh ``alerts.json`` against a ledger written from the *test*
dataset and reporting "⚠ ledger needs attention".

So every path under ``data/`` is redirected to a throwaway directory for the whole
session, and the original values are restored afterwards.  Anything that reads or writes
through ``config`` therefore stays inside the sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import config

# Every path config derives from DATA_DIR, and where it lives inside the sandbox.
SANDBOX_PATHS = {
    "RAW_DIR": "raw",
    "SYNTHETIC_DIR": "synthetic",
    "GEO_DIR": "geo",
    "MODEL_DIR": "models",
    "ARTIFACT_DIR": "artifacts",
    "LEDGER_PATH": "evidence_ledger.jsonl",
    "GRAPH_PATH": "artifacts/graph.json",
    "ALERTS_PATH": "artifacts/alerts.json",
    "SUMMARY_PATH": "artifacts/pipeline_summary.json",
    "WALLET_FEATURES_PATH": "artifacts/wallet_features.csv",
    "TX_FEATURES_PATH": "artifacts/tx_features.csv",
    "CORRELATED_PATH": "artifacts/correlated_records.csv",
    "EXPLAINER_META_PATH": "artifacts/explainer_meta.json",
    "GEOLITE2_CITY_DB": "geo/GeoLite2-City.mmdb",
    "GEOLITE2_ASN_DB": "geo/GeoLite2-ASN.mmdb",
    "MOCK_GEO_PATH": "geo/mock_geoip.json",
}


@pytest.fixture(scope="session", autouse=True)
def sandboxed_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the whole session against a temporary ``data/`` directory."""
    base = tmp_path_factory.mktemp("chaintrace-data")
    patch = pytest.MonkeyPatch()
    patch.setattr(config, "DATA_DIR", base, raising=False)
    for name, relative in SANDBOX_PATHS.items():
        patch.setattr(config, name, base / relative, raising=False)
    config.ensure_directories()
    try:
        yield base
    finally:
        patch.undo()
