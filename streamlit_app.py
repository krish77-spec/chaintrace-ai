"""Hosted entry point at the filename hosts expect to find.

Streamlit Community Cloud's deploy form has a **Main file path** field whose default is
exactly ``streamlit_app.py`` at the root of the repository, so having this file here means
the form can be accepted as it comes - no path to look up, no branch to think about.

Everything it does lives in ``src/bootstrap.py``: build the artefacts if they are missing
(about three seconds on the bundled dataset), then run the console exactly as a Docker or
local run would.  The same logic is reachable as ``app/cloud_app.py`` for a host that
prefers the console's own directory; the two cannot drift because they share one function.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bootstrap import run_console  # noqa: E402  (import follows the sys.path fix)

run_console()
