"""Hosted entry point for platforms that only run a Python process.

    streamlit run app/cloud_app.py

Streamlit Community Cloud is the host this project is deployed to (Part 14).  It runs
one Python process from the repository with no build step of its own, so the console
would open on an empty data directory: the dataset is committed, but the artefacts are
deliberately not (they are regenerated in seconds and would silently drift from the
code).  This wrapper closes that gap - it builds the artefacts if they are missing and
then hands over to the console, unchanged, exactly as a Docker or local run would.

The Docker image and a local checkout keep using ``app/dashboard.py`` directly; they
warm themselves during the image build and via ``scripts/run_full_pipeline.py``.

Progress goes to stdout, never to Streamlit, because ``st.set_page_config`` has to be
the first Streamlit command in the script and it lives inside ``dashboard.py``.
Streamlit Cloud shows this output in the app log.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _log(message: str) -> None:
    print(f"[chaintrace] {message}", flush=True)


def main() -> None:
    from src.bootstrap import ensure_artefacts

    ensure_artefacts(log=_log)
    runpy.run_path(str(HERE / "dashboard.py"), run_name="__main__")


main()
