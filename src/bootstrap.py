"""Warm a copy of the project that has no shell step of its own.

Three hosts warm this project in three different ways, and only two of them have
somewhere to put a shell command:

* the **Docker image** does it during the build, and ``entrypoint.sh`` repeats the
  important part at container start;
* a **local checkout** is warmed by ``scripts/run_full_pipeline.py`` (Part 8);
* a host that only runs a Python process - **Streamlit Community Cloud** is the one
  this project deploys to (Part 14) - has neither, so without this module the console
  would come up empty on the first visit even though every stage works.

``ensure_artefacts`` closes that gap, and it needs nothing that is not already in the
repository: the synthetic dataset is byte-stable from ``RANDOM_SEED``, and the eight
pipeline stages fit their own models as they run - the ``.joblib`` files are a
by-product of a run, not a prerequisite for one.  On the default 1,200-transaction
dataset the whole thing takes about three seconds.

It is deliberately a no-op whenever the artefacts are already in place, so it is safe
to call from an entrypoint, from a test, or by hand.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Callable, List, Optional

from src import config
from src.data.generator import generate_dataset
from src.pipeline.runner import run_full_pipeline

__all__ = [
    "artefacts_present",
    "dataset_present",
    "ensure_artefacts",
    "missing_artefacts",
    "run_console",
]


def _required() -> "tuple[Path, Path, Path]":
    """The three files the console cannot render without."""
    return (config.ALERTS_PATH, config.GRAPH_PATH, config.SUMMARY_PATH)


def missing_artefacts() -> List[Path]:
    """Which of the artefacts the dashboard needs are absent right now."""
    return [path for path in _required() if not path.exists()]


def artefacts_present() -> bool:
    """True when the console has everything it reads."""
    return not missing_artefacts()


def dataset_present() -> bool:
    """True when the generated sample dataset is on disk."""
    return (config.SYNTHETIC_DIR / config.TRANSACTIONS_CSV).exists()


def ensure_artefacts(
    force: bool = False,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> bool:
    """Generate what is missing and run the pipeline once.

    Returns ``True`` when work was done and ``False`` when the copy was already warm,
    so a caller can decide whether to say anything.  ``log`` receives plain strings
    (never Streamlit calls) because the hosted entrypoint has to warm up *before* the
    dashboard gets to call ``st.set_page_config``, which must be the first Streamlit
    command in a script.
    """
    say = log or (lambda message: None)

    if not force and artefacts_present():
        return False

    if force or not dataset_present():
        say(f"generating the synthetic dataset into {config.SYNTHETIC_DIR} ...")
        generate_dataset(output_dir=config.SYNTHETIC_DIR)

    say("running the eight-stage pipeline (about three seconds) ...")
    summary = run_full_pipeline(
        input_dir=config.SYNTHETIC_DIR,
        output_dir=config.ARTIFACT_DIR,
        progress=progress,
    )

    counts = summary.get("counts", {})
    say(
        "warm-up complete: "
        f"{counts.get('transactions', 0)} transactions, "
        f"{counts.get('alerts', 0)} alerts, "
        f"{counts.get('peel_chains', 0)} peeling chains "
        f"in {summary.get('duration_seconds', 0):.1f}s"
    )
    return True


def run_console(entrypoint: Optional[Path] = None) -> None:
    """Warm this copy up, then run the dashboard script as the main program.

    This is what the hosted entrypoints are: ``streamlit_app.py`` at the repository root
    (the name Streamlit Community Cloud looks for by default) and ``app/cloud_app.py``
    (the same thing, one directory down).  They exist only because a hosting form wants
    a filename it recognises - Docker and a local checkout execute ``app/dashboard.py``
    directly - and both defer to this one implementation so they cannot drift apart.

    The warm-up prints rather than drawing, because ``st.set_page_config`` must be the
    first Streamlit command in the script and it lives inside the dashboard.
    """
    ensure_artefacts(log=lambda message: print(f"[chaintrace] {message}", flush=True))
    dashboard = entrypoint or (config.PROJECT_ROOT / "app" / "dashboard.py")
    runpy.run_path(str(dashboard), run_name="__main__")
