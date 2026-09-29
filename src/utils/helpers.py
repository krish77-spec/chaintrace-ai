"""Small shared helpers: logging setup, timing, safe maths and formatting."""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence

import numpy as np

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"


def setup_logging(level: Optional[str] = None) -> None:
    """Configure root logging once (idempotent) for scripts, API and dashboard."""
    resolved = (level or os.environ.get("CHAINTRACE_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))
    for noisy in ("urllib3", "matplotlib", "faker", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Timer:
    """Context manager that records elapsed seconds (used for stage timings)."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.seconds = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.seconds = round(time.perf_counter() - self._start, 3)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Timer {self.label} {self.seconds}s>"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return low
    return float(max(low, min(high, value)))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    try:
        if denominator in (0, None) or (isinstance(denominator, float) and math.isnan(denominator)):
            return default
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def json_safe(value: Any) -> Any:
    """Recursively convert numpy/pandas types into JSON-serialisable primitives."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return round(float(value), 8)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:  # pragma: no cover - defensive
            return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def format_btc(amount: Optional[float]) -> str:
    if amount is None:
        return "-"
    for unit, divisor in (("BTC", 1.0), ("mBTC", 1e-3), ("bits", 1e-6), ("sat", 1e-8)):
        if abs(amount) >= divisor:
            return f"{amount / divisor:,.4f} {unit}"
    return f"{amount:.8f} BTC"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), max(size, 1)):
        yield items[start: start + size]


def top_n(mapping: Dict[str, float], n: int = 10) -> List[tuple]:
    return sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)[:n]


@contextlib.contextmanager
def suppress_stderr() -> Iterator[None]:
    """Silence noisy third-party import-time output (used around optional imports)."""
    with open(os.devnull, "w") as devnull:
        old = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = old


def sortable_stamp(value: Any) -> str:
    """One comparable spelling for an artefact timestamp.

    The graph payload is written through pandas, so a timestamp can arrive either as
    ISO-8601 with a ``T`` (``2026-08-23T00:21:23.358190``, how the generator writes the raw
    CSV) or as ``str(pd.Timestamp)`` with a space (``2026-08-23 00:21:23.358190``).  Those
    two spellings are **not** interchangeable when compared as strings: ``'T'`` sorts after
    every digit, so a *later* instant written with a space sorted *before* an earlier one
    written with a ``T``.  The timeline replay compared them exactly that way, which is why
    stepping one event back could make the clock jump forward.

    Both spellings are normalised to the ``T`` form here: it is a valid ISO-8601 instant and
    it sorts chronologically, so callers can keep comparing timestamps as strings.
    """
    return str(value).replace(" ", "T", 1)


def first_seen_by_edge(edges: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """When each node first appears, read off the timestamps of its edges.

    Every wallet, transaction and IP node is visible from its first transaction, so a
    timeline replay built on this shows the network exactly as it grew (used by the
    dashboard scrubber).

    Returned stamps are normalised with :func:`sortable_stamp`, so string comparison is
    chronological order - which is what the replay's ``<=`` cutoff relies on.
    """
    first: Dict[str, str] = {}
    for edge in edges:
        timestamp = edge.get("timestamp")
        if not timestamp:
            continue
        timestamp = sortable_stamp(timestamp)
        for key in (edge.get("source"), edge.get("target")):
            if not key:
                continue
            if key not in first or timestamp < first[key]:
                first[key] = timestamp
    return first


def humanise_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} min {rest:.0f} s"
