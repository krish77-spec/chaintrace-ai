"""Evidence integrity (spec section 15).

Every alert's evidence package is serialised as *canonical* JSON (sorted keys,
fixed separators, no NaN) and hashed with SHA-256.  The hash is stamped on the
alert and appended to ``data/evidence_ledger.jsonl``.

The ledger is a **hash chain**, not just a list of hashes.  Each line carries
``prev_hash`` (the ``entry_hash`` of the line before it) and its own
``entry_hash`` over the whole entry, so:

* editing an alert's evidence breaks that alert's own hash;
* deleting a line breaks the link for every line after it;
* reordering or inserting a line breaks the chain.

Verification therefore answers two separate questions - *does each alert still
match its own hash?* and *is the ledger itself intact?* - and reports the first
entry where the chain breaks.  ``python -m src.utils.hashing --verify`` runs the
same check from the command line.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import config

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    """Make numpy / pandas / datetime objects JSON-safe without changing meaning."""
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


def canonical_json(payload: Any) -> str:
    """Deterministic JSON string: sorted keys, compact separators, NaN -> null."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
        allow_nan=False,
    )


def sha256_hex(payload: Any) -> str:
    """SHA-256 of a dict/list (canonical JSON) or of raw bytes/str."""
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path | str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_package(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Strip volatile fields so the hash covers only the investigative substance."""
    volatile = {"evidence_hash", "hash_verified_at", "generated_at"}
    return {k: v for k, v in alert.items() if k not in volatile}


def stamp_evidence_hash(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the alert with its SHA-256 evidence hash filled in."""
    package = build_evidence_package(alert)
    stamped = dict(alert)
    stamped["evidence_hash"] = sha256_hex(package)
    return stamped


def verify_evidence_hash(alert: Dict[str, Any]) -> Tuple[bool, str]:
    """Recompute the hash of an alert in place and compare with the stored value."""
    expected = alert.get("evidence_hash")
    computed = sha256_hex(build_evidence_package(alert))
    return (computed == expected, computed)


GENESIS_HASH = "0" * 64


def build_ledger_entry(
    alert: Dict[str, Any],
    previous_hash: str = GENESIS_HASH,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the one-line ledger record for an alert (no I/O).

    ``entry_hash`` seals the entry *together with* the previous entry's hash, which is
    what turns a list of hashes into a tamper-evident chain.
    """
    entry = {
        "alert_id": alert.get("alert_id"),
        "entity": alert.get("entity"),
        "hash": alert.get("evidence_hash"),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "risk_score": alert.get("risk_score"),
        "prev_hash": previous_hash,
    }
    entry["entry_hash"] = sha256_hex({k: v for k, v in entry.items()})
    return entry


def verify_ledger_chain(ledger_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Recompute every ``entry_hash`` and check that each link points at its parent.

    Returns::

        {"entries": n, "ok": True|False, "breaks": [...], "first_break": int|None,
         "chained": True|False}

    A single deliberately corrupted line is reported with its 1-based position, which
    is what makes the tamper demo in the dashboard a one-round demonstration instead
    of a hand-wave.
    """
    entries = [e for e in read_ledger(ledger_path) if e.get("alert_id")]
    report: Dict[str, Any] = {
        "entries": len(entries),
        "chained": bool(entries) and all("entry_hash" in e for e in entries),
        "ok": True,
        "breaks": [],
        "first_break": None,
    }
    if not entries:
        return report
    if not report["chained"]:
        report["ok"] = False
        report["breaks"].append({"position": 1, "reason": "ledger written before hash chaining"})
        report["first_break"] = 1
        return report

    previous = GENESIS_HASH
    for position, entry in enumerate(entries, start=1):
        if entry.get("prev_hash") != previous:
            report["breaks"].append(
                {
                    "position": position,
                    "alert_id": entry.get("alert_id"),
                    "reason": "broken link: prev_hash does not match the previous entry",
                    "expected_prev": previous,
                    "found_prev": entry.get("prev_hash"),
                }
            )
        body = {k: v for k, v in entry.items() if k != "entry_hash"}
        if sha256_hex(body) != entry.get("entry_hash"):
            report["breaks"].append(
                {
                    "position": position,
                    "alert_id": entry.get("alert_id"),
                    "reason": "this entry was edited: entry_hash does not match its contents",
                }
            )
        previous = entry.get("entry_hash")

    report["ok"] = not report["breaks"]
    report["first_break"] = report["breaks"][0]["position"] if report["breaks"] else None
    return report


def tamper_demo(ledger_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Alter one line *in memory* and show what verification does about it.

    Used by the dashboard's "simulate tampering" button.  It never writes to disk, so
    the demo cannot damage the real ledger: it copies the entries, edits the middle
    one, and verifies the copy.
    """
    entries = [e for e in read_ledger(ledger_path) if e.get("alert_id")]
    if len(entries) < 3:
        return {"ok": False, "reason": "not enough ledger entries to demo tampering"}
    index = len(entries) // 2
    original = entries[index]
    entries[index] = dict(original)
    entries[index]["risk_score"] = 0.0          # the classic edit: make the alert look harmless
    report = _verify_chain_entries(entries)
    return {
        "ok": report["ok"],
        "tampered_position": index + 1,
        "tampered_alert_id": original.get("alert_id"),
        "changed": f"risk_score {original.get('risk_score')} -> 0.0",
        "detected": not report["ok"],
        "first_break": report["first_break"],
        "reason": report["breaks"][0]["reason"] if report["breaks"] else "tampering NOT detected",
    }


def _verify_chain_entries(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Chain-check an in-memory list of entries (shared by verify + tamper demo)."""
    report: Dict[str, Any] = {"ok": True, "breaks": [], "first_break": None}
    previous = GENESIS_HASH
    for position, entry in enumerate(entries, start=1):
        if entry.get("prev_hash") != previous:
            report["breaks"].append({"position": position, "reason": "broken link"})
        body = {k: v for k, v in entry.items() if k != "entry_hash"}
        if sha256_hex(body) != entry.get("entry_hash"):
            report["breaks"].append(
                {"position": position, "reason": "entry contents changed after sealing"}
            )
        previous = entry.get("entry_hash")
    report["ok"] = not report["breaks"]
    report["first_break"] = report["breaks"][0]["position"] if report["breaks"] else None
    return report


def append_ledger_entry(
    alert: Dict[str, Any],
    ledger_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Append one {alert_id, hash, timestamp} line to the append-only ledger."""
    ledger_path = Path(ledger_path or config.LEDGER_PATH)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = build_ledger_entry(alert)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=_json_default) + "\n")
    return entry


def append_ledger_line(
    alert: Dict[str, Any],
    ledger_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Append one alert to an existing ledger, extending the hash chain.

    Used when a *new* analysis adds alerts without discarding the historical ledger.
    """
    ledger_path = Path(ledger_path or config.LEDGER_PATH)
    existing = [e for e in read_ledger(ledger_path) if e.get("alert_id")]
    previous = existing[-1]["entry_hash"] if existing else GENESIS_HASH
    entry = build_ledger_entry(alert, previous_hash=previous)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=_json_default) + "\n")
    return entry


def write_ledger(alerts: List[Dict[str, Any]], ledger_path: Optional[Path | str] = None) -> int:
    """Rewrite the ledger for a fresh pipeline run (one run = one coherent ledger).

    Each entry is written exactly once through a single file handle.  Writing it twice
    (once via ``append_ledger_entry`` and once here) left the two handles to interleave
    at their own offsets, which silently duplicated or clobbered ledger lines.
    """
    ledger_path = Path(ledger_path or config.LEDGER_PATH)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    previous = GENESIS_HASH
    with open(ledger_path, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "ledger_header",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "system": "ChainTrace AI",
                    "hash_algorithm": "SHA-256",
                    "chaining": "each entry seals the previous entry_hash (tamper-evident chain)",
                    "note": "Append-only evidence ledger. One line per alert.",
                },
                sort_keys=True,
            )
            + "\n"
        )
        for alert in alerts:
            entry = build_ledger_entry(alert, previous_hash=previous)
            previous = entry["entry_hash"]
            handle.write(json.dumps(entry, sort_keys=True, default=_json_default) + "\n")
            written += 1
    return written


def read_ledger(ledger_path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    ledger_path = Path(ledger_path or config.LEDGER_PATH)
    if not ledger_path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def verify_ledger(
    alerts: Optional[List[Dict[str, Any]]] = None,
    ledger_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Compare the ledger against the current alerts file."""
    entries = [e for e in read_ledger(ledger_path) if e.get("alert_id")]
    ledger_by_id = {e["alert_id"]: e.get("hash") for e in entries}
    chain = verify_ledger_chain(ledger_path)
    report: Dict[str, Any] = {
        "ledger_entries": len(entries),
        "verified": 0,
        "mismatched": 0,
        "missing_from_ledger": 0,
        "alerts_checked": 0,
        "chain_ok": chain["ok"],
        "chain_length": chain["entries"],
        "chain_first_break": chain["first_break"],
        "chain_breaks": chain["breaks"],
        "ok": True,
        "details": [],
    }
    if not chain["ok"]:
        report["ok"] = False
    if alerts is None:
        return report

    report["alerts_checked"] = len(alerts)
    for alert in alerts:
        alert_id = alert.get("alert_id")
        ok, computed = verify_evidence_hash(alert)
        ledger_hash = ledger_by_id.get(alert_id)
        if ledger_hash is None:
            report["missing_from_ledger"] += 1
            report["ok"] = False
            report["details"].append({"alert_id": alert_id, "status": "not_in_ledger"})
            continue
        if ok and ledger_hash == computed:
            report["verified"] += 1
        else:
            report["mismatched"] += 1
            report["ok"] = False
            report["details"].append(
                {
                    "alert_id": alert_id,
                    "status": "hash_mismatch",
                    "stored": alert.get("evidence_hash"),
                    "computed": computed,
                    "ledger": ledger_hash,
                }
            )
    return report


def _main(argv: Optional[List[str]] = None) -> int:
    """``python -m src.utils.hashing --verify`` - verify the ledger chain on disk."""
    import argparse

    parser = argparse.ArgumentParser(description="Verify the ChainTrace AI evidence ledger")
    parser.add_argument("--verify", action="store_true", help="verify the hash chain")
    parser.add_argument("--ledger", default=str(config.LEDGER_PATH))
    parser.add_argument("--tamper-demo", action="store_true",
                        help="show that a one-value edit is detected (nothing is written)")
    args = parser.parse_args(argv)

    if args.tamper_demo:
        print(json.dumps(tamper_demo(args.ledger), indent=2))
        return 0

    chain = verify_ledger_chain(args.ledger)
    print(f"ledger      : {args.ledger}")
    print(f"entries     : {chain['entries']}")
    print(f"chained     : {chain['chained']}")
    print(f"chain intact: {'YES' if chain['ok'] else 'NO'}")
    for item in chain["breaks"]:
        print(f"  break at entry {item['position']}: {item['reason']}")
    return 0 if chain["ok"] else 1


def new_alert_id(entity: str, entity_type: str, index: int = 0) -> str:
    """Stable, readable alert identifier (CT-<type>-<short hash>)."""
    digest = hashlib.sha256(f"{entity_type}:{entity}:{index}".encode()).hexdigest()[:8].upper()
    prefix = {"wallet": "W", "txid": "T", "cluster": "C", "ip": "I"}.get(entity_type, "X")
    return f"CT-{prefix}-{digest}"


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
