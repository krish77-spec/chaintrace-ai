"""Stage 1 - Ingest.

Accepts CSV, JSON or XML (spec section 7) and normalises everything into **one**
internal schema so that no other module ever has to care about file formats:

    timestamps / amounts / lists in a fixed order, columns:
        timestamp, txid, inputs, outputs, input_amounts, output_amounts,
        fee, script_type, src_ip, dst_ip, src_port, dst_port, geo_country, geo_asn

Network metadata is normalised in parallel:
        timestamp, src_ip, dst_ip, src_port, dst_port, txid, protocol

Bad rows are dropped with a warning instead of crashing the pipeline.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

TX_COLUMNS = [
    "timestamp", "txid", "inputs", "outputs", "input_amounts", "output_amounts",
    "fee", "script_type", "src_ip", "dst_ip", "src_port", "dst_port",
    "geo_country", "geo_asn",
]
NETWORK_COLUMNS = [
    "timestamp", "src_ip", "dst_ip", "src_port", "dst_port", "txid", "protocol",
    "geo_country", "geo_asn",
]

TX_LIST_FIELDS = {
    "input_addresses": "inputs",
    "inputs": "inputs",
    "from_addresses": "inputs",
    "output_addresses": "outputs",
    "outputs": "outputs",
    "to_addresses": "outputs",
}
AMOUNT_FIELDS = {
    "input_amounts": "input_amounts",
    "input_values": "input_amounts",
    "output_amounts": "output_amounts",
    "output_values": "output_amounts",
}
NETWORK_FIELDS = {
    "timestamp": "timestamp", "time": "timestamp", "datetime": "timestamp",
    "src_ip": "src_ip", "source_ip": "src_ip", "srcip": "src_ip",
    "dst_ip": "dst_ip", "destination_ip": "dst_ip", "dstip": "dst_ip",
    "src_port": "src_port", "source_port": "src_port",
    "dst_port": "dst_port", "destination_port": "dst_port",
    "txid": "txid", "tx_id": "txid", "hash": "txid", "transaction_id": "txid",
    "protocol": "protocol", "proto": "protocol",
}
# SIH26146 lists geo_country/asn among the *dataset's* minimum fields, so a file
# may already carry them.  They are honoured when present and only computed by the
# enrichment stage when they are not.
GEO_FIELDS = {
    "geo_country": "geo_country", "country": "geo_country",
    "country_code": "geo_country", "src_geo_country": "geo_country",
    "geo_asn": "geo_asn", "asn": "geo_asn", "src_geo_asn": "geo_asn",
    "asn_org": "asn_org", "geo_country_name": "geo_country_name",
}
NETWORK_FIELDS.update(GEO_FIELDS)
TX_FIELDS = {
    "timestamp": "timestamp", "time": "timestamp", "datetime": "timestamp",
    "txid": "txid", "tx_id": "txid", "hash": "txid", "transaction_id": "txid",
    "fee": "fee", "tx_fee": "fee",
    "script_type": "script_type", "script": "script_type", "type": "script_type",
    **GEO_FIELDS,
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "none", "null", "[]"):
        return True
    return False


def split_list(value: Any) -> List[str]:
    """Split 'a|b', 'a,b', '["a","b"]', ['a','b'] or a scalar into a clean list."""
    if _is_missing(value):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if not _is_missing(v)]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if not _is_missing(v)]
        except Exception:  # pragma: no cover - defensive
            pass
    for sep in ("|", ";", ","):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def split_amounts(value: Any, expected: Optional[int] = None) -> List[float]:
    """Parse an amount list and pad/truncate it to ``expected`` when known."""
    parts = split_list(value)
    amounts: List[float] = []
    for part in parts:
        try:
            amounts.append(float(part))
        except (TypeError, ValueError):
            amounts.append(0.0)
    if expected is not None:
        if len(amounts) < expected:
            amounts.extend([0.0] * (expected - len(amounts)))
        amounts = amounts[:expected]
    return amounts


def parse_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        try:
            # seconds vs milliseconds heuristic
            seconds = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
            return pd.Timestamp.fromtimestamp(seconds)
        except Exception:
            return None
    try:
        return pd.to_datetime(str(value), utc=False, errors="coerce")
    except Exception:
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _canonical_key(key: str) -> str:
    return str(key).strip().lower().replace(" ", "_")


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def read_records(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Read a CSV / JSON / XML file into a list of flat dictionaries."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        return frame.to_dict(orient="records")
    if suffix in (".json", ".jsonl", ".ndjson"):
        raw = path.read_text(encoding="utf-8")
        if suffix in (".jsonl", ".ndjson"):
            return [json.loads(line) for line in raw.splitlines() if line.strip()]
        payload = json.loads(raw)
        if isinstance(payload, dict):
            for key in ("records", "data", "transactions", "network", "network_metadata", "items"):
                if key in payload and isinstance(payload[key], list):
                    return payload[key]
            # single record dict
            return [payload]
        if isinstance(payload, list):
            return payload
        raise ValueError(f"Unsupported JSON structure in {path}")
    if suffix == ".xml":
        return _read_xml(path)
    raise ValueError(f"Unsupported file type: {path}")


def _read_xml(path: Path) -> List[Dict[str, Any]]:
    tree = ET.parse(path)
    root = tree.getroot()
    records: List[Dict[str, Any]] = []
    # either <root><record>..</record></root> or <transactions><transaction>..</transaction>
    candidates = list(root)
    if len(candidates) == 1 and len(list(candidates[0])) > 1:
        candidates = list(candidates[0])
    for node in candidates:
        record: Dict[str, Any] = {}
        for child in node:
            if len(list(child)) > 0:  # nested lists: <inputs><address>..</address></inputs>
                record[child.tag] = [c.text for c in child]
            else:
                record[child.tag] = child.text
        if not record:
            record = {k: v for k, v in node.attrib.items()}
        record["_tag"] = node.tag
        if record.get("txid") is None and node.attrib.get("txid"):
            record["txid"] = node.attrib["txid"]
        records.append(record)
    return records


# --------------------------------------------------------------------------- #
# normalisers
# --------------------------------------------------------------------------- #
def normalize_transactions(records: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    dropped = 0
    for raw in records:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row: Dict[str, Any] = {col: None for col in TX_COLUMNS}
        row["inputs"], row["outputs"] = [], []
        row["input_amounts"], row["output_amounts"] = [], []

        for key, value in raw.items():
            key_l = _canonical_key(key)
            if key_l in TX_LIST_FIELDS:
                row[TX_LIST_FIELDS[key_l]] = split_list(value)
            elif key_l in AMOUNT_FIELDS:
                row[AMOUNT_FIELDS[key_l]] = split_amounts(value)
            elif key_l in TX_FIELDS:
                row[TX_FIELDS[key_l]] = value
            elif key_l in NETWORK_FIELDS:
                row[NETWORK_FIELDS[key_l]] = value

        row["timestamp"] = parse_timestamp(row["timestamp"])
        row["txid"] = str(row["txid"]).strip() if not _is_missing(row["txid"]) else None
        row["script_type"] = (
            str(row["script_type"]).strip().upper() if not _is_missing(row["script_type"]) else "UNKNOWN"
        )
        n_in, n_out = len(row["inputs"]), len(row["outputs"])
        row["input_amounts"] = split_amounts(row["input_amounts"], n_in)
        row["output_amounts"] = split_amounts(row["output_amounts"], n_out)
        row["fee"] = _to_float(row["fee"], 0.0)
        if row["fee"] <= 0.0:
            total_in = sum(row["input_amounts"])
            total_out = sum(row["output_amounts"])
            if total_in > 0:
                row["fee"] = round(max(total_in - total_out, 0.0), 8)
        row["src_port"] = _to_int(row["src_port"])
        row["dst_port"] = _to_int(row["dst_port"])
        for geo_col in ("src_ip", "dst_ip", "geo_country", "geo_asn"):
            if _is_missing(row[geo_col]):
                row[geo_col] = None

        if row["timestamp"] is None or not row["txid"]:
            dropped += 1
            continue
        if not row["inputs"] and not row["outputs"]:
            dropped += 1
            continue
        rows.append(row)

    if dropped:
        logger.warning("Dropped %d malformed transaction rows during ingest", dropped)
    frame = pd.DataFrame(rows, columns=TX_COLUMNS)
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def normalize_network(records: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    dropped = 0
    for raw in records:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row: Dict[str, Any] = {col: None for col in NETWORK_COLUMNS}
        for key, value in raw.items():
            key_l = _canonical_key(key)
            if key_l in NETWORK_FIELDS:
                row[NETWORK_FIELDS[key_l]] = value
        row["timestamp"] = parse_timestamp(row["timestamp"])
        row["txid"] = str(row["txid"]).strip() if not _is_missing(row["txid"]) else ""
        row["protocol"] = str(row["protocol"]).strip().upper() if not _is_missing(row["protocol"]) else "TCP"
        row["src_port"] = _to_int(row["src_port"])
        row["dst_port"] = _to_int(row["dst_port"])
        for text_col in ("src_ip", "dst_ip", "geo_country", "geo_asn"):
            if _is_missing(row[text_col]):
                row[text_col] = None
        if row["timestamp"] is None or (not row["src_ip"] and not row["dst_ip"]):
            dropped += 1
            continue
        rows.append(row)

    if dropped:
        logger.warning("Dropped %d malformed network rows during ingest", dropped)
    frame = pd.DataFrame(rows, columns=NETWORK_COLUMNS)
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


# --------------------------------------------------------------------------- #
# source-type detection / directory ingest
# --------------------------------------------------------------------------- #
def guess_kind(records: Sequence[Dict[str, Any]]) -> str:
    """Return 'network', 'transactions' or 'unknown' for a set of raw records."""
    keys = set()
    for record in records[:50]:
        if isinstance(record, dict):
            keys.update(_canonical_key(k) for k in record.keys())
    tx_signals = {"input_addresses", "inputs", "output_addresses", "outputs", "input_amounts", "fee"}
    net_signals = {"src_ip", "dst_ip", "src_port", "dst_port", "protocol"}
    tx_score = len(keys & tx_signals)
    net_score = len(keys & net_signals)
    # A single "bulk" export (the format SIH26146 describes) has both layers in
    # one table.  Classifying it as one or the other would silently throw away
    # half the evidence, so it gets its own kind and is split row by row.
    if tx_score >= 2 and net_score >= 2:
        return "mixed"
    if tx_score > net_score:
        return "transactions"
    if net_score > tx_score:
        return "network"
    return "unknown"


def split_mixed_records(
    records: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a bulk/combined export into (transaction rows, network rows).

    Blockchain fields win the row when both layers are present, because that row
    is a *joined* observation; its network fields are copied onto the transaction
    and the IP/port data is also emitted as a network record so correlation and
    the network graph keep working.
    """
    tx_rows: List[Dict[str, Any]] = []
    net_rows: List[Dict[str, Any]] = []
    tx_value_fields = {"input_addresses", "inputs", "output_addresses", "outputs",
                       "input_amounts", "output_amounts"}
    net_value_fields = {"src_ip", "dst_ip", "src_port", "dst_port"}
    for record in records:
        if not isinstance(record, dict):
            continue
        # Decide per *row*, not per column: a bulk export has one wide schema, and
        # most rows populate only one of the two layers.
        populated = {
            _canonical_key(k): v for k, v in record.items() if not _is_missing(v)
        }
        if populated.keys() & tx_value_fields:
            tx_rows.append(record)
        if populated.keys() & net_value_fields:
            net_rows.append(record)
    return tx_rows, net_rows


def load_transactions(path: Union[str, Path]) -> pd.DataFrame:
    return normalize_transactions(read_records(path))


def load_network(path: Union[str, Path]) -> pd.DataFrame:
    return normalize_network(read_records(path))


def load_input_bundle(
    input_dir: Union[str, Path],
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """Load every supported file in ``input_dir`` into (transactions, network, seeds).

    Files are classified by their columns, not their names, so an investigator can
    drop in whatever the field team exported.
    """
    input_dir = Path(input_dir)
    tx_frames: List[pd.DataFrame] = []
    net_frames: List[pd.DataFrame] = []
    seeds: List[str] = []
    seen = False

    skip_names = {
        config.GROUND_TRUTH_JSON, "manifest.json", "planted_patterns.json",
        "pipeline_summary.json", "alerts.json", "graph.json", config.SEEDS_JSON,
    }
    for path in sorted(input_dir.rglob("*")):
        if path.is_dir() or path.name.startswith(".") or path.name in skip_names:
            continue
        suffix = path.suffix.lower()
        if suffix in (".json", ".jsonl", ".ndjson"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict) and "wallets" in payload and "seed" in payload and "counts" not in payload:
                seeds.extend(str(w) for w in payload["wallets"])
                seen = True
                continue
        if suffix not in (".csv", ".json", ".jsonl", ".ndjson", ".xml"):
            continue
        try:
            records = read_records(path)
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue
        if not records:
            continue
        seen = True
        kind = guess_kind(records)
        if kind == "transactions":
            tx_frames.append(normalize_transactions(records))
        elif kind == "network":
            net_frames.append(normalize_network(records))
        elif kind == "mixed":
            tx_part, net_part = split_mixed_records(records)
            logger.info(
                "%s looks like a combined bulk export - splitting it into %d transaction "
                "and %d network records", path, len(tx_part), len(net_part),
            )
            if tx_part:
                tx_frames.append(normalize_transactions(tx_part))
            if net_part:
                net_frames.append(normalize_network(net_part))
        else:
            logger.warning("Skipping %s: could not tell transactions from network metadata", path)

    if not seen:
        logger.warning("No ingestable files found in %s", input_dir)

    transactions = (
        pd.concat(tx_frames, ignore_index=True).drop_duplicates(subset=["txid"]).reset_index(drop=True)
        if tx_frames
        else pd.DataFrame(columns=TX_COLUMNS)
    )
    network = (
        pd.concat(net_frames, ignore_index=True)
        # The same network observation can arrive twice - once inside a combined
        # bulk export and once in the split metadata file.  Collapse those so the
        # correlation stage does not count the same packet twice.
        .drop_duplicates(
            subset=["timestamp", "src_ip", "dst_ip", "src_port", "dst_port", "txid"]
        )
        .reset_index(drop=True)
        if net_frames
        else pd.DataFrame(columns=NETWORK_COLUMNS)
    )
    return transactions, network, sorted(set(seeds))


def save_unified(
    transactions: pd.DataFrame,
    network: pd.DataFrame,
    output_dir: Union[str, Path],
) -> Tuple[Path, Path]:
    """Persist the unified tables (stage-1 hand-off artefacts)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tx_path = output_dir / "unified_transactions.csv"
    net_path = output_dir / "unified_network.csv"
    tx_out = transactions.copy()
    net_out = network.copy()
    for frame in (tx_out, net_out):
        if not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    for frame, columns in ((tx_out, ("inputs", "outputs", "input_amounts", "output_amounts")),
                           (net_out, ())):
        for column in columns:
            if column in frame.columns:
                frame[column] = frame[column].apply(
                    lambda values: "|".join(str(v) for v in values) if isinstance(values, list) else values
                )
    tx_out.to_csv(tx_path, index=False)
    net_out.to_csv(net_path, index=False)
    return tx_path, net_path
