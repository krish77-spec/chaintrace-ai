"""The warm-up path a shell-less host depends on.

The Docker image and a local checkout warm themselves with shell commands.  Streamlit
Community Cloud - the host this project deploys to - runs one Python process and nothing
else, so ``src/bootstrap.py`` and ``app/cloud_app.py`` have to be able to turn a bare
checkout (dataset committed, artefacts absent) into a console with real findings in it.

``conftest.py`` redirects every ``data/`` path into a temp directory for the whole
session, so none of this touches the demo artefacts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import bootstrap, config  # noqa: E402

# `streamlit_app.py` at the root is the filename Streamlit Community Cloud offers as its
# default main file; `app/cloud_app.py` is the same thing beside the console.
HOSTED_ENTRYPOINTS = (ROOT / "streamlit_app.py", ROOT / "app" / "cloud_app.py")


def _clear_artefacts() -> None:
    for path in (config.ALERTS_PATH, config.GRAPH_PATH, config.SUMMARY_PATH):
        path.unlink(missing_ok=True)


def _offline_api(monkeypatch) -> None:
    """Keep the console from probing for a live API during a test run."""
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")


def test_a_bare_checkout_becomes_a_populated_console():
    _clear_artefacts()
    assert bootstrap.missing_artefacts(), "the artefacts should have been removed"

    said: list[str] = []
    assert bootstrap.ensure_artefacts(log=said.append) is True

    assert bootstrap.artefacts_present()
    assert said and any("pipeline" in line for line in said)
    assert json.loads(config.ALERTS_PATH.read_text()), "the console must have alerts to rank"
    assert config.LEDGER_PATH.exists(), "the evidence ledger is written during the run"


def test_warmup_costs_nothing_when_the_copy_is_already_warm():
    bootstrap.ensure_artefacts()  # build if an earlier test left it cold
    assert bootstrap.artefacts_present()
    assert bootstrap.ensure_artefacts() is False


def test_warmup_regenerates_the_dataset_when_it_is_missing():
    _clear_artefacts()
    (config.SYNTHETIC_DIR / config.TRANSACTIONS_CSV).unlink(missing_ok=True)
    assert bootstrap.dataset_present() is False

    bootstrap.ensure_artefacts()

    assert bootstrap.dataset_present() is True


@pytest.mark.parametrize("entrypoint", HOSTED_ENTRYPOINTS, ids=lambda path: path.name)
def test_hosted_entrypoints_render_the_whole_console(entrypoint, monkeypatch):
    """`streamlit run <entrypoint>` is all a host does - both of them must just work."""
    from streamlit.testing.v1 import AppTest

    _clear_artefacts()
    _offline_api(monkeypatch)

    app = AppTest.from_file(str(entrypoint), default_timeout=300).run()

    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.metric, "the counters must render on a cold, hosted copy"


def test_both_hosted_entrypoints_defer_to_one_warm_up():
    """Two entry points, one implementation - so they cannot drift apart."""
    for path in HOSTED_ENTRYPOINTS:
        assert "run_console" in path.read_text(encoding="utf-8"), path
