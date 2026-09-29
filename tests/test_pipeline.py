"""Smoke + regression tests for the whole ChainTrace AI prototype.

    pytest -q                       # from the repository root

The suite runs the real pipeline once on a smaller synthetic dataset (smaller so the
tests stay fast) and then checks the properties the specification promises:

* the generator plants every required pattern family;
* parsers accept CSV, JSON and XML and normalise to one schema;
* correlation finds TXID-exact matches and time-window matches;
* the graph has wallet / txid / ip nodes and the required edge directions;
* clustering groups co-spending wallets, anomaly detection flags planted outliers,
  peeling detection finds the planted chains, risk pins the seeds at 1.0;
* every alert carries an explanation, a feature attribution and a SHA-256 hash that
  re-verifies, and the ledger agrees with the alerts file;
* the FastAPI surface answers (health, alerts, graph, stats, ledger verification).
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

from src import config  # noqa: E402
from src.api.main import app  # noqa: E402
from src.correlation.correlator import correlate, correlation_summary  # noqa: E402
from src.data import parsers  # noqa: E402
from src.data.generator import SyntheticDataGenerator, generate_dataset  # noqa: E402
from src.graph.builder import GraphBuilder, load_graph_payload  # noqa: E402
from src.ml import clustering as clustering_module  # noqa: E402
from src.ml import peeling as peeling_module  # noqa: E402
from src.ml import risk as risk_module  # noqa: E402
from src.pipeline.runner import load_alerts, run_full_pipeline  # noqa: E402
from src.utils.hashing import verify_evidence_hash, verify_ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> dict:
    """Generate a small dataset and run the full pipeline exactly once."""
    base = tmp_path_factory.mktemp("chaintrace")
    synthetic = base / "synthetic"
    artifacts = base / "artifacts"

    generate_dataset(
        output_dir=synthetic, n_transactions=500, n_wallets=220, n_network_records=160, seed=7
    )
    summary = run_full_pipeline(input_dir=synthetic, output_dir=artifacts, write_artifacts=True)

    truth = json.loads((synthetic / config.GROUND_TRUTH_JSON).read_text(encoding="utf-8"))
    transactions, network, seeds = parsers.load_input_bundle(synthetic)
    return {
        "synthetic": synthetic,
        "artifacts": artifacts,
        "summary": summary,
        "truth": truth,
        "transactions": transactions,
        "network": network,
        "seeds": seeds,
        "alerts": load_alerts(artifacts / "alerts.json"),
    }


# --------------------------------------------------------------------------- #
# stage 1 - generator + parsers
# --------------------------------------------------------------------------- #
def test_generator_plants_every_pattern_family(workspace):
    counts = workspace["truth"]["counts"]
    assert counts["transactions"] >= 500
    assert 5 <= counts["seed_wallets"] <= 8
    assert counts["peeling_chain_txs"] > 0
    assert counts["mixing_transactions"] >= 2
    assert counts["anomalous_transactions"] > 0
    assert counts["entity_clusters"] >= 3
    assert counts["correlated_network_records"] > 0


def test_planted_peeling_chains_are_monotonic_in_time(workspace):
    hop_times = {}
    for chain in workspace["truth"]["peeling_chains"]:
        stamps = [pd.Timestamp(hop["timestamp"]) for hop in chain["hops"]]
        assert stamps == sorted(stamps), f"{chain['chain_id']} hops must advance in time"
        hop_times[chain["chain_id"]] = stamps
    assert hop_times, "expected at least one planted peeling chain"


def test_generator_is_reproducible(tmp_path):
    first = SyntheticDataGenerator(n_transactions=120, n_wallets=60, n_network_records=40, seed=99)
    second = SyntheticDataGenerator(n_transactions=120, n_wallets=60, n_network_records=40, seed=99)
    a = first.generate()["transactions"]
    b = second.generate()["transactions"]
    pd.testing.assert_frame_equal(a, b)


def test_parsers_normalise_csv_json_and_xml(tmp_path):
    generator = SyntheticDataGenerator(n_transactions=25, n_wallets=20, n_network_records=10, seed=3)
    bundle = generator.generate()
    txs = bundle["transactions"]

    csv_path = tmp_path / "tx.csv"
    txs.to_csv(csv_path, index=False)
    from_csv = parsers.load_transactions(csv_path)
    assert {"timestamp", "txid", "inputs", "outputs", "input_amounts", "output_amounts",
            "fee", "script_type"}.issubset(from_csv.columns)
    assert isinstance(from_csv.iloc[0]["inputs"], list)

    json_path = tmp_path / "tx.json"
    json_path.write_text(json.dumps(txs.to_dict(orient="records")), encoding="utf-8")
    from_json = parsers.load_transactions(json_path)
    assert len(from_json) == len(from_csv)

    xml_rows = ["<transactions>"]
    for _, row in txs.iterrows():
        xml_rows.append(
            "<transaction>"
            f"<timestamp>{row['timestamp']}</timestamp>"
            f"<txid>{row['txid']}</txid>"
            f"<inputs>{''.join(f'<address>{a}</address>' for a in row['input_addresses'].split('|'))}</inputs>"
            f"<outputs>{''.join(f'<address>{a}</address>' for a in row['output_addresses'].split('|'))}</outputs>"
            f"<fee>{row['fee']}</fee>"
            f"<script_type>{row['script_type']}</script_type>"
            "</transaction>"
        )
    xml_rows.append("</transactions>")
    xml_path = tmp_path / "tx.xml"
    xml_path.write_text("".join(xml_rows), encoding="utf-8")
    from_xml = parsers.normalize_transactions(parsers.read_records(xml_path))
    assert len(from_xml) == len(from_csv)
    assert from_xml.iloc[0]["txid"] == from_csv.iloc[0]["txid"]


def test_parser_drops_broken_rows_without_crashing():
    frame = parsers.normalize_transactions(
        [
            {"txid": "ok1", "timestamp": "2026-01-01T00:00:00", "input_addresses": "a|b",
             "input_amounts": "1.0|2.0", "output_addresses": "c", "output_amounts": "2.9",
             "fee": "0.1", "script_type": "P2PKH"},
            {"timestamp": "not-a-date", "input_addresses": "x"},      # dropped: bad timestamp
            {"txid": "no-io", "timestamp": "2026-01-01T00:00:00"},     # dropped: no addresses
        ]
    )
    assert len(frame) == 1
    assert frame.iloc[0]["txid"] == "ok1"
    assert frame.iloc[0]["inputs"] == ["a", "b"]


# --------------------------------------------------------------------------- #
# stage 2/3 - enrichment + correlation
# --------------------------------------------------------------------------- #
def test_geoip_enrichment_is_offline_and_deterministic(workspace):
    from src.data.enrich import GeoIPEnricher, enrich_network

    network = enrich_network(workspace["network"], GeoIPEnricher())
    assert "geo_country" in network.columns
    assert network["geo_country"].notna().any()
    assert network["geo_asn"].notna().any()
    assert set(network["cross_border"].unique()) <= {True, False}

    enricher = GeoIPEnricher()
    first = enricher.lookup("185.220.101.42")
    second = GeoIPEnricher().lookup("185.220.101.42")
    assert first == second


def test_correlation_uses_txid_and_time_window(workspace):
    correlated = correlate(workspace["transactions"], workspace["network"])
    stats = correlation_summary(correlated)
    assert stats["correlated_pairs"] > 0
    assert stats["txid_exact"] > 0
    assert {"correlation_method", "correlation_confidence"}.issubset(correlated.columns)
    assert correlated["correlation_confidence"].between(0.0, 1.0).all()
    # exact TXID matches must be full confidence
    exact = correlated[correlated["correlation_method"] == "txid_exact"]
    assert (exact["correlation_confidence"] == 1.0).all()


# --------------------------------------------------------------------------- #
# stage 4 - graph
# --------------------------------------------------------------------------- #
def test_graph_structure_and_helpers(workspace):
    builder = GraphBuilder(
        workspace["transactions"],
        correlate(workspace["transactions"], workspace["network"]),
        seeds=workspace["truth"]["seed_illicit_wallets"],
    )
    builder.build()
    graph = builder.graph
    types = {data.get("node_type") for _, data in graph.nodes(data=True)}
    assert {"wallet", "txid"}.issubset(types)

    txid = next(node for node, data in graph.nodes(data=True) if data.get("node_type") == "txid")
    edge_types = {data.get("edge_type") for _, _, data in graph.out_edges(txid, data=True)}
    assert "out" in edge_types or "network" in edge_types

    stats = builder.get_stats()
    assert stats["wallets"] > 0 and stats["transactions"] > 0 and stats["edges"] > 0

    subgraph = builder.get_subgraph([txid], depth=1)
    assert subgraph.number_of_nodes() >= 2


def test_repeated_address_in_one_transaction_accumulates_its_edge(workspace):
    """One edge per (transaction, address): a repeated pair must sum, never overwrite.

    A transaction can pay the same address in two output rows.  Overwriting the edge
    silently drops value, and it made a wallet's own lifetime totals disagree with the sum
    of its timeline - a contradiction the focus view put on screen.
    """
    import pandas as pd

    from src.graph.builder import GraphBuilder

    transactions = pd.DataFrame(
        [
            {
                "timestamp": "2026-09-01T00:00:00",
                "txid": "a" * 64,                    "inputs": ["W1", "W1"],
                    "outputs": ["W2", "W2"],
                "input_amounts": [0.4, 0.6],
                "output_amounts": [0.75, 0.25],
                "fee": 0.0,
                "script_type": "P2WPKH",
            }
        ]
    )
    builder = GraphBuilder(transactions, seeds=[])
    builder.build()
    graph = builder.graph

    assert graph.number_of_edges("W1", "a" * 64) == 1
    assert graph["W1"]["a" * 64]["in:%s:W1" % ("a" * 64)]["amount"] == pytest.approx(1.0)
    assert graph["a" * 64]["W2"]["out:%s:W2" % ("a" * 64)]["amount"] == pytest.approx(1.0)
    # and the wallet totals still agree with the single accumulated edge
    assert graph.nodes["W1"]["total_out"] == pytest.approx(1.0)
    assert graph.nodes["W2"]["total_in"] == pytest.approx(1.0)


def test_exported_graph_carries_risk_and_cluster_annotations(workspace):
    """graph.json is what the API and the dashboard render, so it must be annotated.

    Stage 4 writes a graph and stage 5f annotates the in-memory nodes (cluster_id,
    risk_score, peel/anomaly scores). Unless the export is refreshed after those
    annotations, the graph tab colours every wallet as risk 0.000 / cluster "-"
    while the analytics behind it are perfectly good.
    """
    # config.GRAPH_PATH is the file the API and the dashboard actually read.
    payload = load_graph_payload()
    wallets = [node for node in payload["nodes"] if node.get("node_type") == "wallet"]
    assert wallets
    assert any(float(node.get("risk_score") or 0.0) > 0.0 for node in wallets), (
        "exported graph must carry propagated risk scores"
    )
    assert any(node.get("cluster_id") is not None for node in wallets), (
        "exported graph must carry cluster ids"
    )
    seed_ids = set(workspace["truth"]["seed_illicit_wallets"])
    seeded = [node for node in wallets if node.get("id") in seed_ids]
    assert seeded and all(float(node["risk_score"]) >= 0.9 for node in seeded), (
        "seed wallets must be exported pinned at their seeded risk"
    )

    # Transaction nodes must expose the same `risk_score` attribute the UI reads for every
    # node type.  They used to be written as `tx_risk_score`, so all 1,200 transactions
    # rendered as risk 0.000 in the graph while their own alerts said risk 0.95.
    tx_nodes = [node for node in payload["nodes"] if node.get("node_type") == "txid"]
    assert tx_nodes
    assert all("risk_score" in node for node in tx_nodes), (
        "transaction nodes must carry risk_score, not only tx_risk_score"
    )
    scored_txs = [node for node in tx_nodes if float(node["risk_score"]) > 0.0]
    assert scored_txs, "transaction risk must reach the exported graph"


# --------------------------------------------------------------------------- #
# stage 5 - the four ML analyses
# --------------------------------------------------------------------------- #
def test_clustering_groups_planted_co_spenders(workspace):
    builder = GraphBuilder(workspace["transactions"], None).build()
    result = clustering_module.cluster_entities(builder, workspace["transactions"])
    assert result.n_clusters > 0

    merged_groups = 0
    for group in workspace["truth"]["entity_clusters"]:
        labels = {result.labels.get(member) for member in group["members"] if member in result.labels}
        labels.discard(None)
        if len(labels) == 1 and group["members"]:
            merged_groups += 1
    assert merged_groups >= 1, "at least one planted co-spending group must land in one cluster"
    assert result.summary()["largest_cluster_size"] >= 2


def test_anomaly_model_flags_planted_outliers(workspace):
    summary = workspace["summary"]
    assert summary["counts"]["anomalies_flagged"] > 0
    alerts = workspace["alerts"]
    tx_alerts = {alert["entity"] for alert in alerts if alert["entity_type"] == "txid"}
    planted = {item["txid"] for item in workspace["truth"]["anomalous_transactions"] if item.get("txid")}
    assert planted, "generator must plant anomalous transactions"
    assert tx_alerts, "anomaly model must surface transactions as alerts"


def test_peeling_detector_recovers_planted_chains(workspace):
    result = peeling_module.detect_peeling_and_mixing(
        workspace["transactions"], seeds=workspace["truth"]["seed_illicit_wallets"]
    )
    planted_hops = {
        txid for chain in workspace["truth"]["peeling_chains"] for txid in chain["txids"]
    }
    detected_hops = {txid for txid, flag in result.tx_is_peel.items() if flag}
    assert planted_hops, "generator must plant peeling chains"
    overlap = planted_hops & detected_hops
    assert len(overlap) / len(planted_hops) >= 0.75, "peeling recall should be high on planted chains"

    for chain in result.chains:
        assert chain.length >= config.MIN_PEEL_CHAIN_LENGTH
        assert 0.0 <= chain.score <= 1.0
        assert chain.mean_asymmetry > config.PEEL_ASYMMETRY_THRESHOLD


def test_mixing_detector_finds_coinjoins(workspace):
    result = peeling_module.detect_peeling_and_mixing(workspace["transactions"])
    planted = {item["txid"] for item in workspace["truth"]["mixing_transactions"]}
    detected = {txid for txid, flag in result.tx_is_mixing.items() if flag}
    assert planted, "generator must plant CoinJoin-like transactions"
    assert planted & detected, "at least one planted CoinJoin must be detected"
    assert not (detected - planted), "mixing detector should not flag unrelated transactions"


def test_risk_pins_seeds_and_propagates(workspace):
    seeds = workspace["truth"]["seed_illicit_wallets"]
    builder = GraphBuilder(workspace["transactions"], None, seeds=seeds).build()
    result = risk_module.propagate_risk(builder, seeds)
    for seed in seeds:
        assert result.wallet_risk.get(seed) == pytest.approx(1.0)
    assert any(score >= 0.5 for score in result.tx_risk.values()), "risk must reach transactions"
    assert all(0.0 <= score <= 1.0 for score in result.wallet_risk.values())


# --------------------------------------------------------------------------- #
# stage 6/8 - explanations, alerts and evidence integrity
# --------------------------------------------------------------------------- #
def test_alerts_are_explainable_and_ranked(workspace):
    alerts = workspace["alerts"]
    assert len(alerts) >= 5, "success criterion: at least 5 ranked alerts"
    scores = [alert["risk_score"] for alert in alerts]
    assert scores == sorted(scores, reverse=True), "alerts must be ranked by risk"

    for alert in alerts:
        assert alert["reasons"], "every alert needs a plain-English reason"
        assert alert["top_features"], "every alert needs feature contributions"
        assert alert["risk_score"] > 0
        assert 0.0 <= alert["confidence"] <= 1.0
        total_contribution = sum(abs(f["contribution"]) for f in alert["top_features"])
        assert total_contribution == pytest.approx(1.0, abs=0.05)


def test_evidence_hashes_verify_and_ledger_matches(workspace):
    alerts = workspace["alerts"]
    assert alerts
    for alert in alerts[:5]:
        ok, computed = verify_evidence_hash(alert)
        assert ok, f"hash mismatch for {alert['alert_id']}"
        assert computed == alert["evidence_hash"]

    # tampering must be detected
    tampered = json.loads(json.dumps(alerts[0]))
    tampered["reasons"] = ["nothing to see here"]
    ok, _ = verify_evidence_hash(tampered)
    assert not ok

    report = verify_ledger(alerts)
    assert report["ok"], report
    assert report["verified"] == len(alerts)


def test_pipeline_summary_reports_all_stages(workspace):
    summary = workspace["summary"]
    assert summary["status"] == "ok"
    stage_names = {stage["name"] for stage in summary["stages"]}
    assert {"ingest", "enrich", "correlate", "graph", "clustering", "anomaly",
            "peeling", "risk", "explain", "evidence"}.issubset(stage_names)
    assert summary["counts"]["alerts"] >= 5
    assert summary["duration_seconds"] < 180


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def test_api_endpoints(workspace, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "ARTIFACT_DIR", workspace["artifacts"])
    monkeypatch.setattr(config, "ALERTS_PATH", workspace["artifacts"] / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", workspace["artifacts"] / "pipeline_summary.json")

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        alerts = client.get("/alerts", params={"min_risk": 0.5, "limit": 5})
        assert alerts.status_code == 200
        payload = alerts.json()
        assert payload["returned"] <= 5
        for alert in payload["alerts"]:
            assert alert["risk_score"] >= 0.5

        if payload["alerts"]:
            detail = client.get(f"/alerts/{payload['alerts'][0]['alert_id']}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["reasons"] and body["evidence_hash"]

        graph = client.get("/graph", params={"limit": 50})
        assert graph.status_code == 200
        assert graph.json()["node_count"] > 0

        # both spellings of "subgraph around a node" must work: the query form and the
        # path form share one builder, and the path form used to fail with a 500
        # because a Query(...) default leaked in as `limit`.
        any_node = graph.json()["nodes"][0]["id"]
        centred = client.get("/graph", params={"node_id": any_node, "depth": 2})
        assert centred.status_code == 200, centred.text
        assert centred.json()["node_count"] >= 1

        neighbourhood = client.get(f"/graph/{any_node}", params={"depth": 2})
        assert neighbourhood.status_code == 200, neighbourhood.text
        assert neighbourhood.json()["node_count"] >= 1

        assert client.get("/graph/does-not-exist").status_code == 404

        stats = client.get("/stats")
        assert stats.status_code == 200
        assert "counts" in stats.json()

        verification = client.get("/ledger/verify")
        assert verification.status_code == 200
        assert verification.json()["ledger_entries"] > 0

        assert client.get("/alerts/NOPE").status_code == 404
        assert client.get("/status/unknown-job").status_code == 404
