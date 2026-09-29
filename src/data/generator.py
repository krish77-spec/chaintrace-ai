"""Synthetic Bitcoin P2P + transaction data generator (spec section 6).

The generator is the foundation of the whole prototype: the four ML analyses can
only be judged if the data they run on actually contains something worth finding.
So this module deliberately *plants* six families of pattern and keeps a ground
truth manifest of every one of them:

1. peeling chains            - 3-6 chains, 4-8 hops, 1-in / 2-out, decreasing value
2. high-risk seed wallets    - 5-8 wallets, some of which start/end peeling chains
3. entity clusters           - repeated co-spending wallet groups
4. anomalous transactions    - extreme fees, huge amounts, unusual scripts, bursts,
                               extreme fan-in / fan-out
5. time-correlated traffic   - network records within +/-30-120 s of high-risk txs,
                               often reusing a small pool of "attacker" IPs
6. mixing-like structures    - CoinJoin signature: >=5 inputs, >=5 similar outputs
7. benign look-alikes        - innocent behaviour shaped like a crime (exchange sweep,
                               payroll run, batching, self-transfer, legitimately high fee).
                               These are the *false-positive control*: flagging one is a
                               mistake, and scripts/benchmark_detectors.py counts them.

Everything is seeded (`random.seed(42)`, `np.random.seed(42)`, `Faker.seed(42)`), so
two runs on the same arguments produce byte-identical CSVs.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from faker import Faker

from src import config
from src.utils.bitcoin import p2pkh_address, p2sh_address, p2tr_address, p2wpkh_address

SATOSHI = 100_000_000
# Address mix, roughly the shape of a modern chain sample: mostly segwit, a
# quarter legacy, a tenth taproot.
ADDRESS_MIX = [("p2pkh", 0.40), ("bech32", 0.35), ("p2sh", 0.15), ("taproot", 0.10)]

# Private / documentation IP ranges are used on purpose: nothing in the prototype
# ever leaves the machine, and using reserved ranges keeps the data obviously fake.
PUBLIC_IP_NETWORKS = [
    (1, 1, 1), (8, 8, 8), (14, 14, 14), (23, 23, 23), (31, 31, 31), (37, 37, 37),
    (45, 45, 45), (51, 51, 51), (62, 62, 62), (77, 77, 77), (85, 85, 85),
    (91, 91, 91), (103, 103, 103), (109, 109, 109), (115, 115, 115),
    (128, 128, 128), (141, 141, 141), (151, 151, 151), (162, 162, 162),
    (172, 172, 172), (185, 185, 185), (193, 193, 193), (204, 204, 204),
    (212, 212, 212), (223, 223, 223),
]

KNOWN_BAD_IP_POOL = [
    ("185.220.101.42", 9009), ("185.220.101.77", 9004), ("45.155.205.233", 8080),
    ("103.251.167.20", 9050), ("91.219.236.17", 4433), ("204.13.164.118", 9001),
    ("162.247.74.201", 9002), ("5.199.162.33", 9051),
]


class SyntheticDataGenerator:
    """Builds a realistic-looking, pattern-rich Bitcoin dataset from a fixed seed."""

    def __init__(
        self,
        n_transactions: int = config.DEFAULT_TX_COUNT,
        n_wallets: int = config.DEFAULT_WALLET_COUNT,
        n_network_records: int = config.DEFAULT_NETWORK_COUNT,
        seed: int = config.RANDOM_SEED,
        days: int = config.DEFAULT_DAYS_OF_ACTIVITY,
        start_time: Optional[datetime] = None,
    ) -> None:
        random.seed(seed)
        np.random.seed(seed)
        Faker.seed(seed)

        self.seed = seed
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.faker = Faker()
        self.n_transactions = n_transactions
        self.n_wallets = n_wallets
        self.n_network_records = n_network_records
        self.days = days
        self.start_time = start_time or config.DATASET_WINDOW_START
        self.end_time = self.start_time + timedelta(days=days)

        self.wallets: List[str] = []
        self.wallet_kind: Dict[str, str] = {}
        self.seed_wallets: List[str] = []
        self.txs: List[Dict[str, Any]] = []
        self.network_records: List[Dict[str, Any]] = []
        # wallets that carry value along a peeling chain - excluded from background
        # activity so the laundering trail stays readable (peel destinations are still
        # reused, which is the realistic part an investigator has to reason about)
        self.chain_carrier_wallets: set = set()
        # wallets of the laundering ring planted by _plant_clusters (see below)
        self.ring_wallets: List[str] = []
        self.ground_truth: Dict[str, Any] = {
            "seed": seed,
            "description": "Ground-truth manifest of every pattern planted by the generator.",
            "window_start": self.start_time.isoformat(),
            "window_end": self.end_time.isoformat(),
            "seed_illicit_wallets": [],
            "peeling_chains": [],
            "mixing_transactions": [],
            "anomalous_transactions": [],
            "entity_clusters": [],
            "correlated_network_records": [],
            "benign_transactions": [],
        }
        self._tx_counter = 0
        self._geo_enricher = None

    # ------------------------------------------------------------------ #
    # Low level helpers
    # ------------------------------------------------------------------ #
    def _geo_for_ip(self, ip: str) -> Dict[str, Any]:
        """Resolve one IP with the *same* offline mapping stage 2 uses.

        The dataset itself carries ``geo_country`` / ``geo_asn`` because SIH26146
        lists them among the minimum metadata fields.  Reusing the enrichment
        table (instead of inventing a second one) guarantees the shipped column
        and the enriched column can never disagree.
        """
        if self._geo_enricher is None:
            from src.data.enrich import GeoIPEnricher

            self._geo_enricher = GeoIPEnricher(persist_mock=False)
        return self._geo_enricher.lookup(ip)

    def _random_bytes(self, length: int) -> bytes:
        """Seeded random bytes (the hash160 / x-only key of a generated address)."""
        return bytes(self.rng.getrandbits(8) for _ in range(length))

    def new_address(self, kind: Optional[str] = None) -> str:
        """Create a *real* Bitcoin address: correct encoding, correct checksum.

        The payload is random (nothing derives from a key we do not have) but the
        encoding is the genuine article, so a Bitcoin library accepts every address in
        this dataset instead of rejecting our synthetic strings.
        """
        kind = kind or self.rng.choices(
            [name for name, _ in ADDRESS_MIX], weights=[weight for _, weight in ADDRESS_MIX]
        )[0]
        if kind == "p2pkh":
            return p2pkh_address(self._random_bytes(20))
        if kind == "p2sh":
            return p2sh_address(self._random_bytes(20))
        if kind == "taproot":
            return p2tr_address(self._random_bytes(32))
        return p2wpkh_address(self._random_bytes(20))

    def _new_txid(self, label: str = "") -> str:
        h = hashlib.sha256(f"{self.seed}:{self._tx_counter}:{label}".encode()).hexdigest()
        self._tx_counter += 1
        return h

    def _ts(self, lo: float = 0.0, hi: float = 1.0) -> datetime:
        offset = self.rng.uniform(lo, hi) * self.days * 86400
        return self.start_time + timedelta(seconds=offset)

    def _amount(self, low: float, high: float, skew: float = 1.0) -> float:
        value = float(self.np_rng.lognormal(mean=0.0, sigma=1.0, size=1)[0])
        value = low + (high - low) * min(value / 6.0, 1.0)
        return round(max(value * skew, 1e-6), 8)

    def _fee_for(self, total_in: float, fee_ratio: float) -> float:
        return round(max(total_in * fee_ratio, 0.00002), 8)

    def add_tx(
        self,
        timestamp: datetime,
        inputs: Sequence[str],
        input_amounts: Sequence[float],
        outputs: Sequence[str],
        output_amounts: Sequence[float],
        script_type: str = "P2WPKH",
        fee: Optional[float] = None,
        tag: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Append one transaction and return its txid."""
        total_in = round(float(sum(input_amounts)), 8)
        total_out = round(float(sum(output_amounts)), 8)
        if fee is None:
            fee = round(max(total_in - total_out, 0.00002), 8)
        txid = self._new_txid(tag or script_type)
        record = {
            "timestamp": timestamp,
            "txid": txid,
            "input_addresses": list(inputs),
            "output_addresses": list(outputs),
            "input_amounts": [round(float(x), 8) for x in input_amounts],
            "output_amounts": [round(float(x), 8) for x in output_amounts],
            "fee": round(float(fee), 8),
            "script_type": script_type,
        }
        if tag:
            record["_tag"] = tag
        if meta:
            record["_meta"] = meta
        self.txs.append(record)
        return txid

    # ------------------------------------------------------------------ #
    # Wallet universe
    # ------------------------------------------------------------------ #
    def _build_wallets(self) -> None:
        """Create the wallet pool: normal users, hubs, cash-out points and seeds."""
        n_seeds = self.rng.randint(config.SEED_WALLET_MIN, config.SEED_WALLET_MAX)
        n_hubs = max(4, int(self.n_wallets * 0.03))
        n_cashout = max(6, int(self.n_wallets * 0.05))

        for _ in range(n_hubs):
            addr = self.new_address("p2sh")
            self.wallets.append(addr)
            self.wallet_kind[addr] = "hub"          # exchange-like, many counterparties
        for _ in range(n_cashout):
            addr = self.new_address()
            self.wallets.append(addr)
            self.wallet_kind[addr] = "cashout"      # reused peel destination
        while len(self.seed_wallets) < n_seeds:
            addr = self.new_address()
            if addr in self.seed_wallets:
                continue
            self.seed_wallets.append(addr)
            self.wallets.append(addr)
            self.wallet_kind[addr] = "illicit_seed"
        while len(self.wallets) < self.n_wallets:
            addr = self.new_address()
            self.wallets.append(addr)
            self.wallet_kind[addr] = "user"
        self.wallet_kind = {w: self.wallet_kind[w] for w in self.wallets}
        self.ground_truth["seed_illicit_wallets"] = list(self.seed_wallets)

    def _user(self) -> str:
        pool = [w for w in self.wallets if w not in self.chain_carrier_wallets]
        return self.rng.choice(pool or self.wallets)

    def _hub(self) -> str:
        hubs = [w for w in self.wallets if self.wallet_kind[w] == "hub"]
        return self.rng.choice(hubs) if hubs else self._user()

    def _cashout(self) -> str:
        outs = [w for w in self.wallets if self.wallet_kind[w] == "cashout"]
        return self.rng.choice(outs) if outs else self._user()

    def _benign_wallet(self) -> str:
        """A wallet with no illicit involvement whatsoever.

        Used only by the false-positive control.  ``_user()`` may return a seed wallet,
        which would make a "benign" flow touch a sanctioned address - and then flagging
        it would be *correct*, so the benchmark would measure nothing.  A control group
        has to be genuinely clean.
        """
        blocked = set(self.seed_wallets) | self.chain_carrier_wallets | set(self.ring_wallets)
        pool = [
            wallet for wallet in self.wallets
            if wallet not in blocked and self.wallet_kind.get(wallet) != "illicit_seed"
        ]
        return self.rng.choice(pool or self.wallets)

    # ------------------------------------------------------------------ #
    # Pattern 1 + 2: peeling chains and seed wallets
    # ------------------------------------------------------------------ #
    def _plant_peeling_chains(self) -> int:
        n_chains = self.rng.randint(config.PEEL_CHAINS_MIN, config.PEEL_CHAINS_MAX)
        planted = 0
        for chain_idx in range(n_chains):
            hops = self.rng.randint(config.PEEL_CHAIN_HOPS_MIN, config.PEEL_CHAIN_HOPS_MAX)

            # half of the chains start at (or immediately after) a known-bad wallet
            if chain_idx < max(1, n_chains // 2):
                head = self.seed_wallets[chain_idx % len(self.seed_wallets)]
                starts_at_seed = True
            else:
                head = self.new_address()
                self.wallets.append(head)
                self.wallet_kind[head] = "user"
                starts_at_seed = False

            value = self._amount(35.0, 420.0)
            ts = self._ts(0.02, 0.55)   # hops advance forward in time from here
            chain_txids: List[str] = []
            chain_wallets: List[str] = [head]
            hops_meta: List[Dict[str, Any]] = []
            current = head
            ends_at_seed = False

            for hop in range(hops):
                # peeling chains move *forward* in time: each hop spends what the
                # previous hop delivered, so timestamps must be strictly increasing
                ts = ts + timedelta(minutes=self.rng.randint(6, 95), seconds=self.rng.randint(0, 59))
                next_wallet = self.new_address()
                self.wallets.append(next_wallet)
                self.wallet_kind[next_wallet] = "user"

                peel_amount = round(value * self.rng.uniform(0.06, 0.22), 8)
                continue_amount = round(value - peel_amount - self._fee_for(value, 0.0004), 8)
                if continue_amount <= 0:
                    break

                # chain 0 feeds the planted laundering ring, so entity clustering has
                # something concrete to connect to the peeling evidence
                if chain_idx == 0 and hop == 1 and self.ring_wallets:
                    peel_target = self.rng.choice(self.ring_wallets)
                # ~35% of peels go straight into a known-bad wallet, the rest cash out
                elif self.rng.random() < 0.35:
                    peel_target = self.rng.choice(self.seed_wallets)
                    ends_at_seed = True
                else:
                    peel_target = self._cashout()

                script_type = self.rng.choice(["P2PKH", "P2SH", "P2WPKH"])
                txid = self.add_tx(
                    timestamp=ts,
                    inputs=[current],
                    input_amounts=[round(value, 8)],
                    outputs=[next_wallet, peel_target],
                    output_amounts=[continue_amount, peel_amount],
                    script_type=script_type,
                    fee=self._fee_for(value, 0.0004),
                    tag=f"peel_chain_{chain_idx}_hop_{hop}",
                    meta={
                        "chain_id": f"PC-{chain_idx + 1:02d}",
                        "hop": hop,
                        "asymmetry": round(
                            continue_amount / max(continue_amount + peel_amount, 1e-9), 4
                        ),
                    },
                )
                chain_txids.append(txid)
                hops_meta.append(
                    {
                        "hop": hop,
                        "txid": txid,
                        "from_wallet": current,
                        "continue_wallet": next_wallet,
                        "peel_wallet": peel_target,
                        "continue_amount": continue_amount,
                        "peel_amount": peel_amount,
                        "timestamp": ts.isoformat(),
                    }
                )
                current = next_wallet
                chain_wallets.append(next_wallet)
                self.chain_carrier_wallets.add(next_wallet)
                value = continue_amount
                planted += 1
                if value < 0.05:
                    break

            self.ground_truth["peeling_chains"].append(
                {
                    "chain_id": f"PC-{chain_idx + 1:02d}",
                    "length": len(chain_txids),
                    "starts_at_seed": starts_at_seed,
                    "reaches_seed": ends_at_seed,
                    "txids": chain_txids,
                    "hops": hops_meta,
                }
            )
        return planted

    def _plant_seed_activity(self) -> None:
        """Give the seed wallets ordinary-looking traffic so risk can propagate."""
        for seed in self.seed_wallets:
            for _ in range(self.rng.randint(2, 5)):
                sender = self._user()
                amount = self._amount(0.4, 9.0)
                ts = self._ts(0.05, 0.95)
                self.add_tx(
                    timestamp=ts,
                    inputs=[sender],
                    input_amounts=[round(amount * 1.004, 8)],
                    outputs=[seed],
                    output_amounts=[amount],
                    script_type=self.rng.choice(config.SCRIPT_TYPES),
                    tag="seed_funding",
                    meta={"reason": "ordinary-looking payment into a known-bad wallet"},
                )
            # the seed also cashes out to a hub, like a real laundering hop
            amount = self._amount(2.0, 30.0)
            self.add_tx(
                timestamp=self._ts(0.3, 0.98),
                inputs=[seed],
                input_amounts=[round(amount * 1.002, 8)],
                outputs=[self._hub()],
                output_amounts=[amount],
                script_type="P2SH",
                tag="seed_cashout",
                meta={"reason": "seed wallet forwards value to an exchange-like hub"},
            )

    # ------------------------------------------------------------------ #
    # Pattern 3: entity clusters (repeated co-spending groups)
    # ------------------------------------------------------------------ #
    def _plant_clusters(self) -> None:
        n_groups = self.rng.randint(config.CLUSTER_GROUP_MIN, config.CLUSTER_GROUP_MAX)
        used: set = set()
        for g in range(n_groups):
            # the first group is a laundering ring: it shares ownership AND touches a
            # known-bad wallet, so clustering visibly changes the risk picture
            linked_to_seed = g == 0
            size = self.rng.randint(3, 8)
            members = []
            while len(members) < size:
                candidate = self.new_address()
                if candidate in used:
                    continue
                used.add(candidate)
                members.append(candidate)
                self.wallets.append(candidate)
                self.wallet_kind[candidate] = "user"

            for round_idx in range(self.rng.randint(2, 4)):
                n_in = self.rng.randint(2, min(5, len(members)))
                inputs = self.rng.sample(members, n_in)
                amounts = [self._amount(0.2, 6.5) for _ in inputs]
                total = sum(amounts)
                if linked_to_seed and round_idx == 0:
                    dest = self.rng.choice(self.seed_wallets)
                else:
                    dest = self._hub() if self.rng.random() < 0.5 else self._user()
                out_amount = round(total * self.rng.uniform(0.985, 0.998), 8)
                self.add_tx(
                    timestamp=self._ts(0.05, 0.95),
                    inputs=inputs,
                    input_amounts=amounts,
                    outputs=[dest],
                    output_amounts=[out_amount],
                    script_type=self.rng.choice(config.SCRIPT_TYPES),
                    tag=f"cluster_group_{g}_sweep_{round_idx}",
                    meta={"group": g, "members": members},
                )
            if linked_to_seed:
                # the ring is also paid by the seed it consolidates for
                amount = self._amount(1.0, 12.0)
                self.add_tx(
                    timestamp=self._ts(0.2, 0.9),
                    inputs=[self.rng.choice(self.seed_wallets)],
                    input_amounts=[round(amount * 1.003, 8)],
                    outputs=[members[0]],
                    output_amounts=[amount],
                    script_type="P2PKH",
                    tag="cluster_seed_link",
                    meta={"group": g, "members": members},
                )
            if linked_to_seed:
                self.ring_wallets = list(members)
            self.ground_truth["entity_clusters"].append(
                {
                    "group_id": f"EC-{g + 1:02d}",
                    "members": members,
                    "linked_to_seed_wallet": linked_to_seed,
                    "receives_peeled_value": linked_to_seed,
                    "note": (
                        "wallets that repeatedly spend together (common-input ownership); "
                        "the first group is a laundering ring that also touches a seed wallet"
                        if linked_to_seed else
                        "wallets that repeatedly spend together (common-input ownership)"
                    ),
                }
            )

    # ------------------------------------------------------------------ #
    # Pattern 4: anomalous transactions
    # ------------------------------------------------------------------ #
    def _plant_anomalies(self) -> None:
        n_anom = self.rng.randint(config.ANOMALY_TX_MIN, config.ANOMALY_TX_MAX)
        kinds = [
            "extreme_fee_ratio", "very_large_amount", "unusual_script",
            "rapid_successive_small", "extreme_fan_in", "extreme_fan_out",
        ]
        for i in range(n_anom):
            kind = kinds[i % len(kinds)]
            ts = self._ts(0.05, 0.95)
            txid = None
            detail: Dict[str, Any] = {"kind": kind}

            if kind == "extreme_fee_ratio":
                amount = self._amount(3.0, 40.0)
                fee_ratio = self.rng.uniform(0.18, 0.42)      # 18-42% of the value burned
                fee = round(amount * fee_ratio, 8)
                txid = self.add_tx(
                    ts, [self._user()], [round(amount + fee, 8)],
                    [self._user()], [amount], "P2PKH", fee=fee,
                    tag="anomaly_extreme_fee",
                    meta={"fee_ratio": round(fee_ratio, 4)},
                )
                detail["fee_ratio"] = round(fee_ratio, 4)

            elif kind == "very_large_amount":
                amount = self._amount(700.0, 2400.0)
                txid = self.add_tx(
                    ts, [self._user()], [round(amount * 1.001, 8)],
                    [self._user()], [amount], "P2WSH",
                    tag="anomaly_large_amount", meta={"amount": amount},
                )
                detail["amount"] = amount

            elif kind == "unusual_script":
                script = self.rng.choice(config.UNUSUAL_SCRIPT_TYPES)
                amount = self._amount(1.0, 12.0)
                txid = self.add_tx(
                    ts, [self._user()], [round(amount * 1.002, 8)],
                    [self._user(), self._user()],
                    [round(amount * 0.6, 8), round(amount * 0.39, 8)],
                    script, tag="anomaly_unusual_script", meta={"script_type": script},
                )
                detail["script_type"] = script

            elif kind == "rapid_successive_small":
                sender = self._user()
                burst = self.rng.randint(10, 18)
                detail["burst_size"] = burst
                txids = []
                for b in range(burst):
                    amount = round(self.rng.uniform(0.004, 0.02), 8)
                    txids.append(
                        self.add_tx(
                            ts + timedelta(seconds=b * self.rng.randint(2, 7)),
                            [sender],
                            [round(amount * 1.01 + 0.00005, 8)],
                            [self._user()],
                            [amount],
                            "P2WPKH",
                            tag="anomaly_burst",
                            meta={"burst_index": b},
                        )
                    )
                txid = txids[0]
                detail["txids"] = txids

            elif kind == "extreme_fan_in":
                n_in = self.rng.randint(12, 22)
                inputs = [self._user() for _ in range(n_in)]
                amounts = [self._amount(0.05, 1.5) for _ in range(n_in)]
                total = round(sum(amounts), 8)
                txid = self.add_tx(
                    ts, inputs, amounts, [self.rng.choice(self.seed_wallets)],
                    [round(total * 0.997, 8)], "P2PKH",
                    tag="anomaly_fan_in", meta={"n_inputs": n_in},
                )
                detail["n_inputs"] = n_in

            else:  # extreme_fan_out
                n_out = self.rng.randint(8, 15)
                amount = self._amount(40.0, 200.0)
                outs = [self._user() for _ in range(n_out)]
                split = round(amount / n_out, 8)
                txid = self.add_tx(
                    ts, [self._user()], [round(amount * 1.002, 8)], outs,
                    [split] * n_out, "P2SH",
                    tag="anomaly_fan_out", meta={"n_outputs": n_out},
                )
                detail["n_outputs"] = n_out

            if txid:
                detail["txid"] = txid
                detail["timestamp"] = ts.isoformat()
                self.ground_truth["anomalous_transactions"].append(detail)

    # ------------------------------------------------------------------ #
    # Pattern 6: mixing-like structures (CoinJoin signature)
    # ------------------------------------------------------------------ #
    def _plant_mixing(self) -> None:
        n_mix = self.rng.randint(config.MIXING_TX_MIN, config.MIXING_TX_MAX)
        for m in range(n_mix):
            n_in = self.rng.randint(config.COINJOIN_MIN_INPUTS, 8)
            n_out = self.rng.randint(config.COINJOIN_MIN_OUTPUTS, 9)
            inputs = [self._user() for _ in range(n_in)]
            amounts = [self._amount(0.35, 2.4) for _ in range(n_in)]
            total = sum(amounts)
            fee = round(total * self.rng.uniform(0.0008, 0.0016), 8)
            pool = total - fee
            outputs = [self._user() for _ in range(n_out)]
            # near-equal outputs are the CoinJoin signature, but the jitter must not
            # break value conservation: normalise the jittered weights back onto the
            # spendable pool so sum(outputs) + fee == sum(inputs) to the satoshi.
            weights = [self.rng.uniform(0.99, 1.01) for _ in range(n_out)]
            weight_sum = sum(weights)
            out_amounts = [round(pool * w / weight_sum, 8) for w in weights]
            drift = round(pool - sum(out_amounts), 8)
            out_amounts[-1] = round(out_amounts[-1] + drift, 8)
            ts = self._ts(0.1, 0.9)
            txid = self.add_tx(
                ts, inputs, amounts, outputs, out_amounts,
                self.rng.choice(["P2SH", "P2WSH"]),
                fee=fee, tag="mixing_coinjoin",
                meta={"n_inputs": n_in, "n_outputs": n_out},
            )
            self.ground_truth["mixing_transactions"].append(
                {
                    "txid": txid,
                    "n_inputs": n_in,
                    "n_outputs": n_out,
                    "timestamp": ts.isoformat(),
                    "note": "many unrelated inputs, many equal-sized outputs (CoinJoin-like)",
                }
            )

    # ------------------------------------------------------------------ #
    # Pattern 7: innocent behaviour that looks criminal
    # ------------------------------------------------------------------ #
    def _plant_benign_lookalikes(self) -> None:
        """Plant *innocent* transactions that trip the obvious heuristics.

        This is the false-positive control, and it is the honest half of the demo:
        every family below is something a real operator does every day, shaped like
        something a rule fires on.

        ==========================  ===================================================
        exchange_sweep              35-60 customer inputs -> 1 hot-wallet output
        payroll_fanout              1 funding input -> 20-30 near-equal payments
        batching_wallet             1 input -> 2 outputs split 90/10 (looks like a peel)
        self_transfer_chain         a wallet moving funds to its own next address
        high_fee_small_amount       a tiny payment carrying a 6-12% fee (Looks anomalous)
        ==========================  ===================================================

        None of them touch a seed wallet, a cash-out wallet or a planted ring, so a
        detector that flags one is producing a false positive.
        """
        plan = {
            "exchange_sweep": 2,
            "payroll_fanout": 2,
            "batching_wallet": 3,
            "self_transfer_chain": 2,
            "high_fee_small_amount": 2,
        }
        for kind, count in plan.items():
            for _ in range(count):
                ts = self._ts(0.05, 0.95)
                txids: List[str] = []
                note = ""

                if kind == "exchange_sweep":
                    n_in = self.rng.randint(35, 60)
                    amounts = [self._amount(0.05, 2.5) for _ in range(n_in)]
                    total = round(sum(amounts), 8)
                    fee = self._fee_for(total, 0.0006)
                    txids.append(
                        self.add_tx(
                            ts, [self._benign_wallet() for _ in range(n_in)], amounts,
                            [self._hub()], [round(total - fee, 8)], "P2SH", fee=fee,
                            tag="benign_exchange_sweep",
                            meta={"reason": "exchange consolidation"},
                        )
                    )
                    note = "exchange consolidation: many customer inputs, one hot-wallet output"

                elif kind == "payroll_fanout":
                    total = self._amount(20.0, 90.0)
                    n_out = self.rng.randint(20, 30)
                    fee = self._fee_for(total, 0.0005)
                    pool = round(total - fee, 8)
                    each = round(pool / n_out, 8)
                    outputs = [each] * (n_out - 1) + [round(pool - each * (n_out - 1), 8)]
                    txids.append(
                        self.add_tx(
                            ts, [self._hub()], [total],
                            [self._benign_wallet() for _ in range(n_out)], outputs, "P2WPKH", fee=fee,
                            tag="benign_payroll_fanout",
                            meta={"reason": "payout run"},
                        )
                    )
                    note = "payout run: one funding input, many equal outbound payments"

                elif kind == "batching_wallet":
                    total = self._amount(5.0, 40.0)
                    fee = self._fee_for(total, 0.0005)
                    pool = round(total - fee, 8)
                    big = round(pool * 0.9, 8)
                    small = round(pool - big, 8)
                    change = self.new_address()
                    self.wallets.append(change)
                    self.wallet_kind[change] = "user"
                    txids.append(
                        self.add_tx(
                            ts, [self._hub()], [total], [self._benign_wallet(), change], [big, small],
                            "P2WPKH", fee=fee, tag="benign_batching_wallet",
                            meta={"reason": "batched payment plus change"},
                        )
                    )
                    note = "batched payment with change: 90/10 split, so it *looks* like a peel hop"

                elif kind == "self_transfer_chain":
                    current = self.new_address()
                    self.wallets.append(current)
                    self.wallet_kind[current] = "user"
                    value = self._amount(1.0, 12.0)
                    for hop in range(3):
                        nxt = self.new_address()
                        self.wallets.append(nxt)
                        self.wallet_kind[nxt] = "user"
                        fee = self._fee_for(value, 0.0004)
                        ts = ts + timedelta(minutes=self.rng.randint(20, 300))
                        txids.append(
                            self.add_tx(
                                ts, [current], [value], [nxt], [round(value - fee, 8)],
                                "P2WPKH", fee=fee, tag="benign_self_transfer",
                                meta={"reason": "wallet housekeeping"},
                            )
                        )
                        current = nxt
                        value = round(value - fee, 8)
                    note = "wallet housekeeping: funds moved through the owner's own addresses"

                else:  # high_fee_small_amount
                    total = round(self.rng.uniform(0.0008, 0.004), 8)
                    ratio = self.rng.uniform(0.06, 0.12)
                    fee = round(total * ratio, 8)
                    txids.append(
                        self.add_tx(
                            ts, [self._benign_wallet()], [total], [self._benign_wallet()],
                            [round(total - fee, 8)], "P2PKH", fee=fee,
                            tag="benign_high_fee",
                            meta={"fee_ratio": round(ratio, 4)},
                        )
                    )
                    note = "small payment where the network fee is legitimately 6-12% of the value"

                self.ground_truth["benign_transactions"].append(
                    {"kind": kind, "txids": txids, "timestamp": ts.isoformat(), "note": note}
                )

    # ------------------------------------------------------------------ #
    # Background traffic
    # ------------------------------------------------------------------ #
    def _background_activity(self, remaining: int) -> None:
        for _ in range(max(remaining, 0)):
            n_in = self.rng.choices([1, 2, 3], weights=[0.68, 0.24, 0.08])[0]
            n_out = self.rng.choices([1, 2, 3, 4], weights=[0.42, 0.42, 0.12, 0.04])[0]
            inputs = [self._user() for _ in range(n_in)]
            amounts = [self._amount(0.01, 4.5) for _ in range(n_in)]
            total = sum(amounts)
            fee = self._fee_for(total, self.rng.uniform(0.0004, 0.0016))
            pool = round(total - fee, 8)
            outputs = [self._user() for _ in range(n_out)]
            if n_out == 1:
                out_amounts = [pool]
            else:
                cuts = sorted(self.rng.uniform(0.05, 0.95) for _ in range(n_out - 1))
                shares, prev = [], 0.0
                for cut in cuts:
                    shares.append(cut - prev)
                    prev = cut
                shares.append(1.0 - prev)
                out_amounts = [round(pool * s, 8) for s in shares]
            self.add_tx(
                timestamp=self._ts(0.0, 1.0),
                inputs=inputs,
                input_amounts=amounts,
                outputs=outputs,
                output_amounts=out_amounts,
                script_type=self.rng.choices(config.SCRIPT_TYPES, weights=[0.3, 0.2, 0.25, 0.15, 0.1])[0],
                fee=fee,
                tag="background",
            )

    # ------------------------------------------------------------------ #
    # Pattern 5: network metadata (noise + time-correlated traffic)
    # ------------------------------------------------------------------ #
    def _interesting_txs(self) -> List[Dict[str, Any]]:
        interesting = [
            tx for tx in self.txs
            if any(
                key in tx.get("_tag", "")
                for key in ("peel_chain", "mixing_coinjoin", "anomaly", "seed_", "cluster_group")
            )
        ]
        return interesting

    def _random_ip(self) -> str:
        base = self.rng.choice(PUBLIC_IP_NETWORKS)
        return f"{base[0]}.{base[1]}.{base[2]}.{self.rng.randint(2, 254)}"

    def _build_network_records(self) -> None:
        interesting = self._interesting_txs()
        self.rng.shuffle(interesting)

        # ~45% of the network records are correlated with a suspicious transaction
        n_correlated = min(int(self.n_network_records * 0.45), len(interesting) * 2)
        n_noise = max(self.n_network_records - n_correlated, 0)

        for tx in interesting[:n_correlated]:
            ts: datetime = tx["timestamp"]
            # suspicious traffic reuses a small pool of source IPs
            src_ip, src_port = self.rng.choice(KNOWN_BAD_IP_POOL)
            dst_ip = self._random_ip()
            with_txid = self.rng.random() < 0.55
            # records that carry the TXID may sit anywhere in the +/-30-120 s band;
            # records without a TXID must fall inside the 90 s correlation window
            max_offset = 120 if with_txid else config.TIME_WINDOW_SECONDS - 10
            offset = self.rng.uniform(30, max_offset) * self.rng.choice([-1, 1])
            record_ts = ts + timedelta(seconds=offset)
            src_geo = self._geo_for_ip(src_ip)
            record = {
                "timestamp": record_ts,
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "src_port": src_port,
                "dst_port": self.rng.choice([8333, 18333, 8332, 8334]),
                "txid": tx["txid"] if with_txid else "",
                "protocol": self.rng.choice(["BITCOIN_P2P", "BITCOIN_P2P", "TCP"]),
                "geo_country": src_geo.get("geo_country"),
                "geo_asn": src_geo.get("geo_asn"),
                "_correlated": True,
                "_offset_seconds": round(offset, 2),
            }
            self.network_records.append(record)
            self.ground_truth["correlated_network_records"].append(
                {
                    "txid": tx["txid"],
                    "src_ip": src_ip,
                    "offset_seconds": round(offset, 2),
                    "matched_by": "txid" if with_txid else "time_window",
                    "tag": tx.get("_tag"),
                }
            )

        for _ in range(n_noise):
            ts = self._ts(0.0, 1.0)
            src_ip = self._random_ip()
            src_geo = self._geo_for_ip(src_ip)
            self.network_records.append(
                {
                    "timestamp": ts,
                    "src_ip": src_ip,
                    "dst_ip": self._random_ip(),
                    "src_port": self.rng.randint(1024, 65535),
                    "dst_port": self.rng.choice([8333, 18333, 80, 443, 9050]),
                    "txid": "",
                    "protocol": self.rng.choice(["BITCOIN_P2P", "TCP", "TLS"]),
                    "geo_country": src_geo.get("geo_country"),
                    "geo_asn": src_geo.get("geo_asn"),
                    "_correlated": False,
                    "_offset_seconds": None,
                }
            )
        self.network_records.sort(key=lambda r: r["timestamp"])

    # ------------------------------------------------------------------ #
    # Assembly / serialisation
    # ------------------------------------------------------------------ #
    def generate(self) -> Dict[str, Any]:
        """Run the full generation process and return DataFrames + ground truth."""
        self._build_wallets()

        self._plant_clusters()          # first: defines the laundering ring the chains feed
        planted = self._plant_peeling_chains()
        self._plant_seed_activity()
        self._plant_anomalies()
        self._plant_mixing()
        self._plant_benign_lookalikes()  # innocent behaviour that *looks* criminal
        self._background_activity(self.n_transactions - len(self.txs))

        self._build_network_records()
        self.txs.sort(key=lambda t: t["timestamp"])

        tx_rows = []
        for tx in self.txs:
            tx_rows.append(
                {
                    "timestamp": tx["timestamp"].isoformat(),
                    "txid": tx["txid"],
                    "input_addresses": "|".join(tx["input_addresses"]),
                    "output_addresses": "|".join(tx["output_addresses"]),
                    "input_amounts": "|".join(f"{a:.8f}" for a in tx["input_amounts"]),
                    "output_amounts": "|".join(f"{a:.8f}" for a in tx["output_amounts"]),
                    "fee": tx["fee"],
                    "script_type": tx["script_type"],
                }
            )
        net_rows = [
            {
                "timestamp": r["timestamp"].isoformat(),
                "src_ip": r["src_ip"],
                "dst_ip": r["dst_ip"],
                "src_port": r["src_port"],
                "dst_port": r["dst_port"],
                "txid": r["txid"],
                "protocol": r["protocol"],
                "geo_country": r.get("geo_country"),
                "geo_asn": r.get("geo_asn"),
            }
            for r in self.network_records
        ]

        transactions_df = pd.DataFrame(tx_rows)
        network_df = pd.DataFrame(net_rows)
        bulk_df = self._build_bulk_frame(tx_rows, self.network_records)

        self.ground_truth["counts"] = {
            "transactions": len(transactions_df),
            "network_records": len(network_df),
            "bulk_records": len(bulk_df),
            "wallets": len(set(self.wallets)),
            "peeling_chain_txs": planted,
            "seed_wallets": len(self.seed_wallets),
            "mixing_transactions": len(self.ground_truth["mixing_transactions"]),
            "anomalous_transactions": len(self.ground_truth["anomalous_transactions"]),
            "benign_transactions": sum(
                len(item["txids"]) for item in self.ground_truth["benign_transactions"]
            ),
            "entity_clusters": len(self.ground_truth["entity_clusters"]),
            "correlated_network_records": len(self.ground_truth["correlated_network_records"]),
        }
        self.ground_truth["wallet_roles"] = {
            addr: kind for addr, kind in self.wallet_kind.items() if kind != "user"
        }
        return {
            "transactions": transactions_df,
            "network": network_df,
            "bulk": bulk_df,
            "seed_wallets": list(self.seed_wallets),
            "ground_truth": self.ground_truth,
        }

    # ------------------------------------------------------------------ #
    # Bulk (single-file) view of the same dataset
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_bulk_frame(
        tx_rows: List[Dict[str, Any]], network_records: List[Dict[str, Any]]
    ) -> pd.DataFrame:
        """One file carrying both layers, exactly as SIH26146 describes it.

        One row is one *observation*, and every row keeps its own timestamp: the
        blockchain rows carry the transaction time, the network rows carry the
        packet time.  When a network record already carries the TXID, the
        transaction's columns are copied onto that packet row - so the file shows
        the join on one line while the +/-90 s correlation window stays
        reconstructable.  Collapsing the two timestamps into one would look tidier
        and quietly destroy the correlation evidence.
        """
        tx_fields = [
            "txid", "input_addresses", "output_addresses",
            "input_amounts", "output_amounts", "fee", "script_type",
        ]
        net_fields = ["src_ip", "dst_ip", "src_port", "dst_port", "protocol",
                      "geo_country", "geo_asn"]
        columns = ["timestamp"] + tx_fields + net_fields

        rows: List[Dict[str, Any]] = []
        by_txid: Dict[str, Dict[str, Any]] = {}
        for tx in tx_rows:
            row = {col: tx.get(col) for col in columns}
            rows.append(row)
            by_txid[tx["txid"]] = row

        for record in network_records:
            txid = record.get("txid") or ""
            row = {col: None for col in columns}
            row["timestamp"] = record["timestamp"].isoformat()
            row["txid"] = txid
            for col in net_fields:
                row[col] = record.get(col)
            matched = by_txid.get(txid)
            if matched is not None:
                # joined observation: blockchain columns are copied onto the packet
                # row, the packet keeps its own timestamp
                for col in tx_fields:
                    row[col] = matched.get(col)
                row["txid"] = txid
            rows.append(row)
        frame = pd.DataFrame(rows, columns=columns)
        return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)

    def write(self, output_dir: Path | str) -> Dict[str, Path]:
        """Write transactions / network / bulk CSVs plus the seeds + ground truth."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle = self.generate()

        paths = {
            "transactions": output_dir / config.TRANSACTIONS_CSV,
            "network": output_dir / config.NETWORK_CSV,
            "bulk": output_dir / config.BULK_CSV,
            "seeds": output_dir / config.SEEDS_JSON,
            "ground_truth": output_dir / config.GROUND_TRUTH_JSON,
        }
        bundle["transactions"].to_csv(paths["transactions"], index=False)
        bundle["network"].to_csv(paths["network"], index=False)
        bundle["bulk"].to_csv(paths["bulk"], index=False)
        with open(paths["seeds"], "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "description": "Known-bad / sanctioned wallets used to seed risk propagation.",
                    "generated_by": "src/data/generator.py",
                    "seed": self.seed,
                    "wallets": bundle["seed_wallets"],
                },
                fh,
                indent=2,
            )
        with open(paths["ground_truth"], "w", encoding="utf-8") as fh:
            json.dump(bundle["ground_truth"], fh, indent=2)
        return paths


def generate_dataset(
    output_dir: Optional[Path | str] = None,
    n_transactions: int = config.DEFAULT_TX_COUNT,
    n_wallets: int = config.DEFAULT_WALLET_COUNT,
    n_network_records: int = config.DEFAULT_NETWORK_COUNT,
    seed: int = config.RANDOM_SEED,
) -> Dict[str, Path]:
    """Convenience wrapper used by scripts/generate_sample_data.py and the Docker build."""
    gen = SyntheticDataGenerator(
        n_transactions=n_transactions,
        n_wallets=n_wallets,
        n_network_records=n_network_records,
        seed=seed,
    )
    return gen.write(output_dir or config.SYNTHETIC_DIR)


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    written = generate_dataset()
    for name, path in written.items():
        print(f"{name:>12}: {path}")
