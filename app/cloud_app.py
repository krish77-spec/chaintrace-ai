"""Hosted entry point, kept next to the console it serves.

    streamlit run app/cloud_app.py

Streamlit Community Cloud is the host this project is deployed to (Part 14).  It runs one
Python process with no build step of its own, so the console would open on an empty data
directory: the dataset is committed, but the artefacts are deliberately not, because they
are rebuilt in seconds and would otherwise drift from the code.  This entry point closes
that gap by warming the copy up and then handing over to the console, unchanged.

The work itself is in ``src/bootstrap.py``, so the root ``streamlit_app.py`` - the filename
Streamlit Community Cloud offers by default - and this file do exactly the same thing.
Docker and a local checkout keep running ``app/dashboard.py`` directly; they warm
themselves during the image build and via ``scripts/run_full_pipeline.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bootstrap import run_console  # noqa: E402  (import follows the sys.path fix)

run_console()
