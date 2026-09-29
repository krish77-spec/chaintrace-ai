"""Stage 5 - Peeling-chain and mixing detection (spec section 11.4).

Peeling chains are the signature laundering move: take one large UTXO, spend it
into two outputs - a "continue" output that carries the bulk forward and a small
"peel" output that cashes out - then repeat, hop after hop, until the trail is
exhausted.

Detection is *rule-assisted and score-based* (the shape test is a heuristic, the
ranking is a learned/weighted score built from features), in three steps:

1. mark candidate peel hops: 1 input -> 2 outputs with asymmetry > 0.75 and a
   meaningful (non-dust) peel output;
2. walk forward along the large output, up to ``MAX_PEEL_HOPS``;
3. score each chain on length, mean asymmetry, total value moved and whether it
   touches a known-bad seed wallet.

CoinJoin-style mixing is detected separately: >= 5 inputs, >= 5 outputs whose
amounts are nearly identical (coefficient of variation < 0.15), with dissimilar
input sizes - the opposite shape of a normal consolidation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src import config
from src.graph.builder import _as_floats, _as_list

logger = logging.getLogger(__name__)


@dataclass
class PeelChain:
    chain_id: str
    txids: List[str]
    wallets: List[str]
    length: int
    mean_asymmetry: float
    total_value_moved: float
    starts_at_seed: bool
    reaches_seed: bool
    score: float
    hops: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "length": self.length,
            "mean_asymmetry": round(self.mean_asymmetry, 4),
            "total_value_moved": round(self.total_value_moved, 8),
            "starts_at_seed": self.starts_at_seed,
            "reaches_seed": self.reaches_seed,
            "score": round(self.score, 4),
            "txids": self.txids,
            "wallets": self.wallets,
            "hops": self.hops,
        }


@dataclass
class PeelResult:
    chains: List[PeelChain] = field(default_factory=list)
    tx_peel_score: Dict[str, float] = field(default_factory=dict)
    tx_chain_id: Dict[str, str] = field(default_factory=dict)
    tx_hop: Dict[str, int] = field(default_factory=dict)
    tx_chain_length: Dict[str, int] = field(default_factory=dict)
    tx_is_peel: Dict[str, bool] = field(default_factory=dict)
    wallet_peel_score: Dict[str, float] = field(default_factory=dict)
    wallet_chain_length: Dict[str, int] = field(default_factory=dict)
    tx_mixing_score: Dict[str, float] = field(default_factory=dict)
    tx_is_mixing: Dict[str, bool] = field(default_factory=dict)
    wallet_mixing_count: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        return {
            "peel_chains": len(self.chains),
            "peel_chain_transactions": int(sum(c.length for c in self.chains)),
            "chained_peel_hops": sum(1 for v in self.tx_is_peel.values() if v),
            "standalone_peel_like": sum(
                1 for txid, score in self.tx_peel_score.items()
                if score > 0 and txid not in self.tx_is_peel
            ),
            "longest_chain": max((c.length for c in self.chains), default=0),
            "chains_touching_seeds": sum(1 for c in self.chains if c.starts_at_seed or c.reaches_seed),
            "mixing_transactions": sum(1 for v in self.tx_is_mixing.values() if v),
            "max_peel_score": round(max(self.tx_peel_score.values()), 4) if self.tx_peel_score else 0.0,
        }


def _tx_records(transactions: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for _, row in transactions.iterrows():
        txid = str(row.get("txid") or "")
        if not txid:
            continue
        inputs = [str(a) for a in _as_list(row.get("inputs"))]
        outputs = [str(a) for a in _as_list(row.get("outputs"))]
        amounts = _as_floats(row.get("output_amounts"))
        amounts += [0.0] * (len(outputs) - len(amounts))
        records[txid] = {
            "txid": txid,
            "inputs": inputs,
            "outputs": outputs,
            "input_amounts": _as_floats(row.get("input_amounts")),
            "output_amounts": amounts[: len(outputs)],
            "total_out": float(sum(amounts[: len(outputs)])),
            "fee": float(row.get("fee") or 0.0),
            "timestamp": pd.to_datetime(row.get("timestamp")),
        }
    return records


def _build_spend_index(records: Dict[str, Dict[str, Any]]) -> Dict[str, List[Tuple[pd.Timestamp, str]]]:
    index: Dict[str, List[Tuple[pd.Timestamp, str]]] = {}
    for txid, record in records.items():
        for address in record["inputs"]:
            index.setdefault(address, []).append((record["timestamp"], txid))
    for address in index:
        index[address].sort(key=lambda pair: pair[0])
    return index


def _asymmetry(record: Dict[str, Any]) -> Tuple[float, str, str, float, float]:
    """Return (asymmetry, continue_addr, peel_addr, continue_amt, peel_amt)."""
    amounts = record["output_amounts"]
    outputs = record["outputs"]
    if len(amounts) != 2 or len(outputs) != 2:
        return 0.0, "", "", 0.0, 0.0
    if amounts[0] >= amounts[1]:
        larger, smaller, continue_addr, peel_addr = amounts[0], amounts[1], outputs[0], outputs[1]
    else:
        larger, smaller, continue_addr, peel_addr = amounts[1], amounts[0], outputs[1], outputs[0]
    total = larger + smaller
    if total <= 0:
        return 0.0, "", "", 0.0, 0.0
    return larger / total, continue_addr, peel_addr, larger, smaller


def _next_hop(
    current: Dict[str, Any],
    candidates: Dict[str, Dict[str, Any]],
    spend_index: Dict[str, List[Tuple[pd.Timestamp, str]]],
    seen: set,
) -> Optional[Dict[str, Any]]:
    """Pick the candidate that most plausibly continues this chain.

    Value continuity is the strongest signal an investigator has: the next hop's
    input amount should be almost exactly what the previous hop forwarded.  Among
    the candidate spenders of the "continue" output we therefore pick the closest
    value match (ties broken by earliest timestamp), and only accept hops that
    happen *after* the current one.

    A candidate is only eligible if the value it forwards is *plausible*: you cannot
    forward more than you received (within fee rounding), and forwarding a small
    fraction of it means the money was split elsewhere, which ends the trail.  This
    bound is what stops the walk from stringing together unrelated transactions that
    merely share a spender - the false-positive mode the precision benchmark caught.
    """
    best: Optional[Dict[str, Any]] = None
    best_delta = None
    continue_amount = float(current["continue_amount"])
    for timestamp, candidate_txid in spend_index.get(current["continue_addr"], []):
        if candidate_txid in seen or candidate_txid == current["txid"]:
            continue
        candidate = candidates.get(candidate_txid)
        if candidate is None or timestamp <= current["timestamp"]:
            continue
        input_amount = float(candidate["input_amounts"][0]) if candidate["input_amounts"] else 0.0
        if input_amount <= 0 or continue_amount <= 0:
            continue
        ratio = input_amount / continue_amount
        if ratio > config.PEEL_CONTINUE_MAX_RATIO or ratio < config.PEEL_CONTINUE_MIN_RATIO:
            continue  # value continuity broken -> this is not the next hop
        delta = abs(input_amount - continue_amount)
        if best is None or delta < best_delta:  # type: ignore[operator]
            best, best_delta = candidate, delta
    return best


def detect_peel_chains(
    transactions: pd.DataFrame,
    seeds: Optional[Sequence[str]] = None,
    max_hops: int = config.MAX_PEEL_HOPS,
    min_length: int = config.MIN_PEEL_CHAIN_LENGTH,
) -> PeelResult:
    seeds = set(seeds or [])
    result = PeelResult()
    records = _tx_records(transactions)
    if not records:
        return result
    spend_index = _build_spend_index(records)

    # ---- 1. candidate peel hops --------------------------------------- #
    candidates: Dict[str, Dict[str, Any]] = {}
    for txid, record in records.items():
        if len(record["inputs"]) != 1 or len(record["outputs"]) != 2:
            continue
        asymmetry, continue_addr, peel_addr, continue_amt, peel_amt = _asymmetry(record)
        if asymmetry <= config.PEEL_ASYMMETRY_THRESHOLD:
            continue
        if peel_amt <= 0 or (peel_amt / max(continue_amt + peel_amt, 1e-9)) < config.PEEL_PEEL_MIN_SHARE:
            continue
        candidates[txid] = {
            **record,
            "asymmetry": asymmetry,
            "continue_addr": continue_addr,
            "peel_addr": peel_addr,
            "continue_amount": continue_amt,
            "peel_amount": peel_amt,
        }

    # ---- 2. enumerate forward walks along the large output ------------- #
    # Every candidate hop is tried as a starting point; the best non-overlapping
    # walks win.  Doing it this way (instead of trusting a single "head") matters
    # because a seed or cash-out wallet can be spent by several unrelated
    # transactions, which would otherwise burn a real chain on a dead end.
    ordered = sorted(candidates.values(), key=lambda r: r["timestamp"])
    walks: List[List[Dict[str, Any]]] = []
    for record in ordered:
        walk: List[Dict[str, Any]] = []
        current = record
        seen = {record["txid"]}
        while current is not None and len(walk) < max_hops:
            walk.append(current)
            current = _next_hop(current, candidates, spend_index, seen)
            if current is not None:
                seen.add(current["txid"])
        if len(walk) >= min_length:
            walks.append(walk)

    # prefer long, high-value walks; a hop may only belong to one chain
    walks.sort(
        key=lambda walk: (len(walk), sum(step["continue_amount"] for step in walk)),
        reverse=True,
    )

    assigned: Dict[str, str] = {}
    chains: List[PeelChain] = []
    for walk in walks:
        if any(step["txid"] in assigned for step in walk):
            continue

        hops: List[Dict[str, Any]] = []
        chain_txids: List[str] = []
        wallets: List[str] = []
        for index, step in enumerate(walk):
            chain_txids.append(step["txid"])
            if not wallets:
                wallets.extend(step["inputs"])
            wallets.append(step["continue_addr"])
            wallets.append(step["peel_addr"])
            hops.append(
                {
                    "txid": step["txid"],
                    "hop": index,
                    "from_wallet": step["inputs"][0],
                    "continue_wallet": step["continue_addr"],
                    "peel_wallet": step["peel_addr"],
                    "continue_amount": round(step["continue_amount"], 8),
                    "peel_amount": round(step["peel_amount"], 8),
                    "asymmetry": round(step["asymmetry"], 4),
                    "timestamp": step["timestamp"].isoformat()
                    if hasattr(step["timestamp"], "isoformat") else str(step["timestamp"]),
                }
            )

        for txid in chain_txids:
            assigned[txid] = f"PC-{len(chains) + 1:02d}"

        mean_asymmetry = float(np.mean([hop["asymmetry"] for hop in hops]))
        total_moved = float(sum(hop["continue_amount"] for hop in hops))
        starts_at_seed = bool(hops and hops[0]["from_wallet"] in seeds)
        reaches_seed = any(
            hop["peel_wallet"] in seeds or hop["continue_wallet"] in seeds for hop in hops
        )

        length_score = min(len(chain_txids) / max(max_hops, 1), 1.0)
        asymmetry_score = float(np.clip((mean_asymmetry - 0.5) / 0.5, 0.0, 1.0))
        value_score = min(math.log1p(total_moved) / math.log1p(100.0), 1.0)
        seed_bonus = 0.15 if (starts_at_seed or reaches_seed) else 0.0
        score = float(
            np.clip(0.40 * length_score + 0.25 * asymmetry_score + 0.20 * value_score + seed_bonus, 0.0, 1.0)
        )

        chains.append(
            PeelChain(
                chain_id=f"PC-{len(chains) + 1:02d}",
                txids=chain_txids,
                wallets=sorted(set(wallets)),
                length=len(chain_txids),
                mean_asymmetry=mean_asymmetry,
                total_value_moved=total_moved,
                starts_at_seed=starts_at_seed,
                reaches_seed=reaches_seed,
                score=score,
                hops=hops,
            )
        )

    # ---- 3. attach scores to transactions and wallets ------------------ #
    result.chains = chains
    for chain in chains:
        for index, txid in enumerate(chain.txids):
            hop_decay = 1.0 - 0.04 * index          # earlier hops matter slightly more
            result.tx_peel_score[txid] = round(float(np.clip(chain.score * hop_decay, 0.0, 1.0)), 4)
            result.tx_chain_id[txid] = chain.chain_id
            result.tx_hop[txid] = index
            result.tx_chain_length[txid] = chain.length
            result.tx_is_peel[txid] = True
        for wallet in chain.wallets:
            result.wallet_peel_score[wallet] = max(result.wallet_peel_score.get(wallet, 0.0), chain.score)
            result.wallet_chain_length[wallet] = max(result.wallet_chain_length.get(wallet, 0), chain.length)

    # standalone peel-shaped transactions (partial evidence, lower score)
    for txid, record in candidates.items():
        if txid in result.tx_is_peel:
            continue
        score = float(np.clip(0.35 * record["asymmetry"], 0.0, 0.5))
        result.tx_peel_score[txid] = round(score, 4)
        result.tx_is_peel[txid] = False

    logger.info(
        "Peeling detection: %d chains (%d txs), %d standalone peel-like hops",
        len(chains), sum(chain.length for chain in chains),
        sum(1 for txid, flag in result.tx_is_peel.items() if not flag),
    )
    return result


def detect_mixing(
    transactions: pd.DataFrame,
    min_inputs: int = config.COINJOIN_MIN_INPUTS,
    min_outputs: int = config.COINJOIN_MIN_OUTPUTS,
    output_cv_threshold: float = config.COINJOIN_OUTPUT_CV,
) -> Tuple[Dict[str, float], Dict[str, bool], Dict[str, int]]:
    """CoinJoin-style shape test -> (tx scores, flags, per-wallet participation counts)."""
    scores: Dict[str, float] = {}
    flags: Dict[str, bool] = {}
    wallet_counts: Dict[str, int] = {}

    for _, row in transactions.iterrows():
        txid = str(row.get("txid") or "")
        inputs = [str(a) for a in _as_list(row.get("inputs"))]
        outputs = [str(a) for a in _as_list(row.get("outputs"))]
        amounts = _as_floats(row.get("output_amounts"))
        if len(inputs) < min_inputs or len(outputs) < min_outputs or len(amounts) != len(outputs):
            continue
        mean_out = float(np.mean(amounts))
        if mean_out <= 0:
            continue
        cv = float(np.std(amounts) / mean_out)
        if cv >= output_cv_threshold:
            continue

        if len(set(inputs)) < min_inputs:
            # repeated addresses mean one owner recycling coins, not a mix of participants
            continue

        uniformity = float(np.clip(1.0 - cv / max(output_cv_threshold, 1e-9), 0.0, 1.0))
        size_score = min(len(inputs) / 12.0, 1.0)
        score = float(np.clip(0.6 * uniformity + 0.4 * size_score, 0.0, 1.0))
        scores[txid] = round(score, 4)
        flags[txid] = True
        for wallet in set(inputs) | set(outputs):
            wallet_counts[wallet] = wallet_counts.get(wallet, 0) + 1

    return scores, flags, wallet_counts


def detect_peeling_and_mixing(
    transactions: pd.DataFrame,
    seeds: Optional[Sequence[str]] = None,
) -> PeelResult:
    """Convenience wrapper running both detectors and merging the results."""
    result = detect_peel_chains(transactions, seeds=seeds)
    scores, flags, wallet_counts = detect_mixing(transactions)
    result.tx_mixing_score = scores
    result.tx_is_mixing = flags
    result.wallet_mixing_count = wallet_counts
    logger.info("Mixing detection: %d CoinJoin-like transactions", len(scores))
    return result
