"""Stage 3 - Correlate network metadata with blockchain records (spec section 9).

Two strategies, both implemented and always reported side by side:

1. **Exact TXID match** - a network record carries the txid. Confidence 1.0.
2. **Time-window match** - no txid on the record, so it is joined to any
   transaction whose timestamp is within ``TIME_WINDOW_SECONDS`` (default 90 s).
   Confidence decays linearly with the time distance and is penalised for
   ambiguous matches (one record falling inside several transactions).

Output columns: ``correlation_method`` and ``correlation_confidence`` (0-1).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from src import config

logger = logging.getLogger(__name__)

METHOD_EXACT = "txid_exact"
METHOD_WINDOW = "time_window"
METHOD_NONE = "unmatched"


def _window_confidence(seconds: float, window: float, n_candidates: int) -> float:
    """Linear decay from 1.0 (same instant) to CORRELATION_CONFIDENCE_FLOOR at the edge."""
    distance = abs(float(seconds))
    if distance > window:
        return 0.0
    span = max(window, 1e-9)
    base = 1.0 - (1.0 - config.CORRELATION_CONFIDENCE_FLOOR) * (distance / span)
    ambiguity_penalty = 1.0 / math.sqrt(max(n_candidates, 1))
    return float(max(min(base * ambiguity_penalty, config.CORRELATION_EXACT_CONFIDENCE), 0.0))


def correlate(
    transactions: pd.DataFrame,
    network: pd.DataFrame,
    window_seconds: float = config.TIME_WINDOW_SECONDS,
) -> pd.DataFrame:
    """Return one row per correlated (network record, transaction) pair."""
    if transactions is None or transactions.empty or network is None or network.empty:
        logger.warning("Correlation skipped: need both transactions and network records")
        return pd.DataFrame(
            columns=[
                "timestamp", "txid", "src_ip", "dst_ip", "src_port", "dst_port",
                "protocol", "geo_country", "geo_asn", "correlation_method",
                "correlation_confidence", "time_delta_seconds",
            ]
        )

    tx = transactions.copy()
    net = network.copy()
    tx["timestamp"] = pd.to_datetime(tx["timestamp"])
    net["timestamp"] = pd.to_datetime(net["timestamp"])

    # sorted transaction timeline used by the window matcher
    tx_sorted = tx.sort_values("timestamp").reset_index(drop=True)
    tx_times = tx_sorted["timestamp"].values.astype("datetime64[ns]").astype("int64")

    matched_rows: List[Dict[str, Any]] = []
    exact_hits = 0
    window_hits = 0

    tx_by_id = {row["txid"]: row for _, row in tx.iterrows()}

    for _, record in net.iterrows():
        record_ts = int(pd.Timestamp(record["timestamp"]).value)
        base = {
            "network_timestamp": record["timestamp"],
            "src_ip": record.get("src_ip"),
            "dst_ip": record.get("dst_ip"),
            "src_port": record.get("src_port"),
            "dst_port": record.get("dst_port"),
            "protocol": record.get("protocol"),
            "geo_country": record.get("geo_country") or record.get("src_geo_country"),
            "geo_asn": record.get("geo_asn") or record.get("src_geo_asn"),
            "asn_org": record.get("asn_org") or record.get("src_asn_org"),
            "cross_border": record.get("cross_border"),
            "src_is_private": record.get("src_is_private"),
        }

        txid = str(record.get("txid") or "").strip()
        if txid and txid in tx_by_id:
            row = tx_by_id[txid]
            matched_rows.append(
                {
                    **base,
                    "timestamp": row["timestamp"],
                    "txid": txid,
                    "time_delta_seconds": (record["timestamp"] - row["timestamp"]).total_seconds(),
                    "correlation_method": METHOD_EXACT,
                    "correlation_confidence": config.CORRELATION_EXACT_CONFIDENCE,
                }
            )
            exact_hits += 1
            continue

        # ---- time-window fallback ------------------------------------- #
        lo = np.searchsorted(tx_times, record_ts - int(window_seconds * 1e9), side="left")
        hi = np.searchsorted(tx_times, record_ts + int(window_seconds * 1e9), side="right")
        candidates = tx_sorted.iloc[lo:hi]
        if candidates.empty:
            continue
        deltas = (candidates["timestamp"] - record["timestamp"]).dt.total_seconds()
        best_idx = deltas.abs().idxmin()
        best_delta = float(deltas.loc[best_idx])
        confidence = _window_confidence(best_delta, window_seconds, len(candidates))
        if confidence <= 0:
            continue
        row = candidates.loc[best_idx]
        matched_rows.append(
            {
                **base,
                "timestamp": row["timestamp"],
                "txid": row["txid"],
                "time_delta_seconds": best_delta,
                "correlation_method": METHOD_WINDOW,
                "correlation_confidence": round(confidence, 4),
                "window_candidates": int(len(candidates)),
            }
        )
        window_hits += 1

    correlated = pd.DataFrame(matched_rows)
    if correlated.empty:
        logger.warning("No network records could be correlated with transactions")
        return correlated

    correlated = correlated.sort_values(
        ["correlation_confidence", "timestamp"], ascending=[False, True]
    ).reset_index(drop=True)

    logger.info(
        "Correlated %d network records (%d exact TXID, %d time-window)",
        len(correlated), exact_hits, window_hits,
    )
    return correlated


def per_transaction_correlation(correlated: pd.DataFrame) -> pd.DataFrame:
    """Best correlation per txid - a compact table for features/alerts."""
    if correlated is None or correlated.empty:
        return pd.DataFrame(columns=["txid", "correlation_confidence", "correlation_method"])
    grouped = correlated.groupby("txid")
    best = grouped.agg(
        correlation_confidence=("correlation_confidence", "max"),
        correlation_method=("correlation_method", lambda values: sorted(set(values))[0]),
        network_records=("correlation_confidence", "size"),
        mean_time_delta_seconds=("time_delta_seconds", "mean"),
    ).reset_index()
    return best


def correlation_summary(correlated: pd.DataFrame) -> Dict[str, Any]:
    """Counters for the pipeline summary and the dashboard header."""
    if correlated is None or correlated.empty:
        return {
            "correlated_pairs": 0, "txid_exact": 0, "time_window": 0,
            "unique_transactions": 0, "mean_confidence": 0.0,
        }
    methods = correlated["correlation_method"].value_counts().to_dict()
    return {
        "correlated_pairs": int(len(correlated)),
        "txid_exact": int(methods.get(METHOD_EXACT, 0)),
        "time_window": int(methods.get(METHOD_WINDOW, 0)),
        "unique_transactions": int(correlated["txid"].nunique()),
        "mean_confidence": round(float(correlated["correlation_confidence"].mean()), 4),
    }
