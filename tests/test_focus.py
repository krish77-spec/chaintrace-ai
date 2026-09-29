"""Focus mode: the ladder, the fade set and the per-entity timeline.

These are pure-function tests over a hand-built mini graph, so they pin the *rules* the
dashboard relies on:

* tapping a node appends it to the path, and tapping a breadcrumb walks back instead of
  looping;
* only the head node and its **direct** connections are lit (a two-hop neighbour must
  stay faded, or the fade stops meaning anything);
* a wallet's timeline says **spent** where the graph says "in" - the edge is named after
  the transaction, not the wallet, and getting that backwards prints a payout as a
  receipt.  The sums are checked against the node's own totals for the same reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.focus import (  # noqa: E402
    MAX_HOPS,
    advance_path,
    direct_neighbours,
    flow_rows,
    focus_view,
    index_nodes,
    node_events,
    node_facts,
    path_summary,
    route_hops,
    route_summary,
    short_id,
    timeline_stats,
)

NODES = [
    {"id": "W_SEED", "node_type": "wallet", "risk_score": 1.0, "is_seed": True, "tx_count": 1,
     "total_in": 0.0, "total_out": 1.0, "unique_counterparties": 1, "mean_amount": 1.0,
     "cluster_id": 0, "peel_score": 0.0, "anomaly_score": 0.9,
     "first_seen": "2026-09-01T00:00:00", "last_seen": "2026-09-01T00:00:00",
     "lifetime_hours": 0.0},
    {"id": "TX_A", "node_type": "txid", "risk_score": 0.8, "total_in": 1.0, "total_out": 0.9,
     "fee": 0.1, "num_inputs": 1, "num_outputs": 1, "script_type": "P2WPKH",
     "timestamp": "2026-09-01T00:00:00", "anomaly_score": 0.7},
    {"id": "W_MID", "node_type": "wallet", "risk_score": 0.4, "tx_count": 2, "total_in": 0.9,
     "total_out": 0.5, "unique_counterparties": 2, "mean_amount": 0.7, "cluster_id": 0,
     "last_seen": "2026-09-02T00:00:00", "first_seen": "2026-09-01T00:00:00",
     "lifetime_hours": 24.0, "is_mixing": True},
    {"id": "TX_B", "node_type": "txid", "risk_score": 0.3, "total_in": 0.5, "total_out": 0.4,
     "fee": 0.1, "num_inputs": 1, "num_outputs": 1, "script_type": "P2WSH",
     "timestamp": "2026-09-02T00:00:00", "anomaly_score": 0.2},
    {"id": "W_FAR", "node_type": "wallet", "risk_score": 0.1, "tx_count": 1, "total_in": 0.4,
     "total_out": 0.0, "unique_counterparties": 1, "mean_amount": 0.4, "cluster_id": None},
    {"id": "W_OTHER", "node_type": "wallet", "risk_score": 0.0, "tx_count": 0, "total_in": 0.0,
     "total_out": 0.0, "unique_counterparties": 0, "mean_amount": 0.0, "cluster_id": None},
    {"id": "1.2.3.4", "node_type": "ip", "risk_score": 0.0, "geo_country": "IN",
     "geo_asn": "AS9498", "asn_org": "Bharti Airtel Ltd", "cross_border": False},
]

EDGES = [
    {"source": "W_SEED", "target": "TX_A", "edge_type": "in", "amount": 1.0,
     "timestamp": "2026-09-01T00:00:00"},
    {"source": "TX_A", "target": "W_MID", "edge_type": "out", "amount": 0.9,
     "timestamp": "2026-09-01T00:00:00"},
    {"source": "W_MID", "target": "TX_B", "edge_type": "in", "amount": 0.5,
     "timestamp": "2026-09-02T00:00:00"},
    {"source": "TX_B", "target": "W_FAR", "edge_type": "out", "amount": 0.4,
     "timestamp": "2026-09-02T00:00:00"},
    {"source": "1.2.3.4", "target": "TX_B", "edge_type": "network",
     "timestamp": "2026-09-02T00:00:05", "correlation_method": "time_window",
     "correlation_confidence": 0.85},
]

BY_ID = index_nodes(NODES)


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
def test_advance_path_appends_each_hop_in_order():
    path = advance_path([], "W_SEED")
    path = advance_path(path, "TX_A")
    path = advance_path(path, "W_MID")
    assert path == ["W_SEED", "TX_A", "W_MID"]
    assert "W_SEED" in path_summary(path) or short_id("W_SEED") in path_summary(path)


def test_tapping_a_breadcrumb_walks_back_instead_of_looping():
    path = ["W_SEED", "TX_A", "W_MID", "TX_B"]
    assert advance_path(path, "TX_A") == ["W_SEED", "TX_A"]


def test_an_empty_click_leaves_the_path_alone():
    assert advance_path(["W_SEED"], None) == ["W_SEED"]
    assert advance_path(["W_SEED"], "") == ["W_SEED"]


def test_the_ladder_is_bounded():
    long_path = [f"n{index}" for index in range(MAX_HOPS + 5)]
    trimmed = advance_path(long_path, "new")
    assert len(trimmed) == MAX_HOPS
    assert trimmed[-1] == "new"


# --------------------------------------------------------------------------- #
# what stays lit
# --------------------------------------------------------------------------- #
def test_focus_lights_only_the_head_and_its_direct_neighbours():
    view = focus_view(["W_MID"], EDGES)
    assert view["head"] == "W_MID"
    assert set(view["neighbours"]) == {"TX_A", "TX_B"}
    # W_SEED is two hops away and must stay faded; so must W_FAR, one hop past TX_B
    assert "W_SEED" not in view["nodes"]
    assert "W_FAR" not in view["nodes"]
    assert len(view["edges"]) == 2


def test_the_route_walked_is_kept_as_breadcrumbs():
    view = focus_view(["W_SEED", "TX_A", "W_MID"], EDGES)
    assert tuple(sorted(("W_SEED", "TX_A"))) in view["ladder"]
    assert tuple(sorted(("TX_A", "W_MID"))) in view["ladder"]
    assert view["path"] == ["W_SEED", "TX_A", "W_MID"]


def test_network_correlations_are_direct_connections_too():
    assert direct_neighbours("1.2.3.4", EDGES) == ["TX_B"]
    view = focus_view(["1.2.3.4"], EDGES)
    assert view["nodes"] == {"1.2.3.4", "TX_B"}


def test_no_focus_lights_nothing():
    view = focus_view([], EDGES)
    assert view["head"] is None
    assert not view["nodes"] and not view["edges"]


# --------------------------------------------------------------------------- #
# the timeline
# --------------------------------------------------------------------------- #
def test_a_wallets_timeline_reads_spending_as_spent():
    """The graph calls the edge "in" (into the transaction); the wallet spent it."""
    events = node_events("W_SEED", BY_ID, EDGES)
    assert len(events) == 1
    assert events[0]["edge_type"] == "in"
    assert events[0]["kind"] == "out"
    assert events[0]["direction"] == "spent"
    assert events[0]["counterparty"] == "TX_A"


def test_timeline_totals_agree_with_the_nodes_own_lifetime_totals():
    for node_id in ("W_SEED", "W_MID", "W_FAR", "TX_A", "TX_B"):
        node = BY_ID[node_id]
        stats = timeline_stats(node, node_events(node_id, BY_ID, EDGES))
        if node["node_type"] == "wallet":
            assert stats["received"] == pytest.approx(node["total_in"])
            assert stats["sent"] == pytest.approx(node["total_out"])
        else:
            assert stats["received"] == pytest.approx(node["total_in"])
            assert stats["sent"] == pytest.approx(node["total_out"])


def test_a_transaction_timeline_lists_funders_before_payees_in_time_order():
    events = node_events("TX_B", BY_ID, EDGES)
    assert [event["direction"] for event in events] == ["funded by", "paid out to", "packet from"]
    assert [event["step"] for event in events] == [1, 2, 3]
    assert events[2]["kind"] == "network"
    assert events[2]["method"] == "time_window"


def test_events_carry_the_counterpartys_risk_and_flags():
    events = node_events("TX_A", BY_ID, EDGES)
    seed_event = next(event for event in events if event["counterparty"] == "W_SEED")
    assert seed_event["counterparty_risk"] == pytest.approx(1.0)
    assert seed_event["counterparty_is_seed"] is True


def test_timeline_stats_summarise_the_window():
    node = BY_ID["W_MID"]
    stats = timeline_stats(node, node_events("W_MID", BY_ID, EDGES))
    assert stats["events"] == 2
    assert stats["span_hours"] == pytest.approx(24.0)
    # "flagged" here means "counterparty risk >= 0.5": TX_B is 0.3, TX_A is 0.8
    assert stats["flagged_counterparties"] == 1
    assert stats["high_risk_counterparties"] == 0


def test_undated_edges_are_skipped_rather_than_crashing():
    edges = EDGES + [{"source": "W_SEED", "target": "W_OTHER", "edge_type": "in", "amount": 1.0}]
    assert len(node_events("W_SEED", BY_ID, edges)) == 1


# --------------------------------------------------------------------------- #
# facts + formatting
# --------------------------------------------------------------------------- #
def test_node_facts_lead_with_the_role_for_a_seed_wallet():
    facts = dict(node_facts(BY_ID["W_SEED"]))
    assert "SEED WALLET" in facts["Role"]
    assert facts["Sent"] == "1.0000 BTC"
    assert facts["Transactions"] == "1"


def test_node_facts_are_typed_per_node_kind():
    tx_facts = dict(node_facts(BY_ID["TX_A"]))
    assert tx_facts["Fee"] == "100.0000 mBTC"
    assert tx_facts["Inputs / outputs"] == "1 in \u00b7 1 out"
    ip_facts = dict(node_facts(BY_ID["1.2.3.4"]))
    assert ip_facts["Country"] == "IN"
    assert "Bharti Airtel" in ip_facts["ASN"]


def test_the_route_carries_the_money_each_hop_moved():
    """The dotted route line is a value trail: every hop shows what moved along it."""
    hops = route_hops(["W_SEED", "TX_A", "W_MID"], EDGES)
    assert [hop["label"] for hop in hops] == ["funded", "paid"]
    assert hops[0]["amount"] == pytest.approx(1.0)
    assert hops[1]["amount"] == pytest.approx(0.9)
    summary = route_summary(["W_SEED", "TX_A", "W_MID"], EDGES)
    assert "1.0000 BTC" in summary and "0.9000 BTC" in summary
    assert summary.startswith("W_SEED")


def test_a_network_hop_reports_the_match_method_instead_of_an_amount():
    hops = route_hops(["TX_B", "1.2.3.4"], EDGES)
    assert len(hops) == 1
    assert hops[0]["amount"] is None
    assert hops[0]["label"] == "matched packet"
    assert "time_window" in route_summary(["TX_B", "1.2.3.4"], EDGES)


def test_short_id_keeps_short_ids_intact():
    assert short_id("W_SEED") == "W_SEED"
    long_id = "bc1q35srz6a27ps6m0l89jweznt83n2sqn2fllftgx"
    assert short_id(long_id) == "bc1q35sr\u20262fllftgx"


# --------------------------------------------------------------------------- #
# who sent what to whom
# --------------------------------------------------------------------------- #
def test_a_wallet_row_is_written_sender_first_and_labelled_from_its_own_side():
    """W_MID received 0.9 from TX_A and spent 0.5 into TX_B - both rows must say so."""
    flow = flow_rows("W_MID", BY_ID, EDGES)
    received = [row for row in flow["rows"] if row["role"] == "received"]
    sent = [row for row in flow["rows"] if row["role"] == "sent"]
    assert len(received) == 1 and len(sent) == 1
    assert received[0]["from"] == "TX_A" and received[0]["to"] == "W_MID"
    assert received[0]["amount"] == pytest.approx(0.9)
    assert "paid this entity" in received[0]["meaning"]
    assert sent[0]["from"] == "W_MID" and sent[0]["to"] == "TX_B"
    assert sent[0]["amount"] == pytest.approx(0.5)
    assert flow["total_sent"] == pytest.approx(0.5)
    assert flow["total_received"] == pytest.approx(0.9)
    assert flow["resolution"] == "exact"
    # every row reads as an arrow, and the sender is always in the From column
    assert all(row["from_label"].split()[0] in {"wallet", "txid"} for row in flow["rows"])


def test_a_single_input_transaction_calls_its_mapping_exact():
    flow = flow_rows("TX_A", BY_ID, EDGES)
    assert flow["resolution"] == "exact"
    assert flow["senders"] == 1 and flow["receivers"] == 1
    funder = flow["rows"][0]
    assert funder["from"] == "W_SEED" and funder["to"] == "TX_A"
    assert funder["role"] == "funded by"
    assert flow["total_sent"] == pytest.approx(1.0)
    assert flow["total_received"] == pytest.approx(0.9)


def test_a_multi_input_transaction_is_reported_as_a_pool_not_an_invention():
    """Bitcoin links inputs and outputs only as a pool; a second funder must flip the label."""
    pooled = EDGES + [
        {"source": "W_OTHER", "target": "TX_A", "edge_type": "in", "amount": 0.25,
         "timestamp": "2026-09-01T00:00:00"}
    ]
    flow = flow_rows("TX_A", BY_ID, pooled)
    assert flow["resolution"] == "pooled"
    assert flow["senders"] == 2
    assert "pool" in flow["note"]
    assert {row["from"] for row in flow["rows"] if row["direction"] == "spend"} == {
        "W_SEED", "W_OTHER"
    }


def test_a_matched_packet_row_carries_no_amount():
    flow = flow_rows("1.2.3.4", BY_ID, EDGES)
    assert len(flow["rows"]) == 1
    assert flow["rows"][0]["amount"] is None
    assert flow["rows"][0]["role"] == "correlated"
    assert flow["rows"][0]["from"] == "1.2.3.4" and flow["rows"][0]["to"] == "TX_B"
    assert flow["total_sent"] == 0.0 and flow["total_received"] == 0.0
