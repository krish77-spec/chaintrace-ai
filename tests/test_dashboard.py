"""Dashboard smoke test.

Uses Streamlit's own ``AppTest`` harness to execute ``app/dashboard.py`` headlessly
and assert that the three investigator screens build without exceptions and contain
real data.  This catches the failure mode that is easiest to miss by eye: a syntax
or import error in the dashboard only shows up when someone opens the page.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.pipeline.runner import run_full_pipeline  # noqa: E402
from src.utils.hashing import verify_ledger_chain  # noqa: E402

DASHBOARD = ROOT / "app" / "dashboard.py"


@pytest.fixture(scope="module")
def artefacts(tmp_path_factory) -> Path:
    """One small pipeline run whose artefacts the dashboard will read."""
    base = tmp_path_factory.mktemp("dashboard")
    synthetic = base / "synthetic"
    artifacts = base / "artifacts"
    from src.data.generator import generate_dataset

    generate_dataset(
        output_dir=synthetic, n_transactions=400, n_wallets=180, n_network_records=120, seed=11
    )
    run_full_pipeline(input_dir=synthetic, output_dir=artifacts)
    # the graph export goes to the workspace path (config.GRAPH_PATH), the rest to the
    # output directory - copy it across so the graph tab has something real to draw.
    shutil.copyfile(config.GRAPH_PATH, artifacts / "graph.json")
    return artifacts


def test_dashboard_builds_without_exceptions(artefacts, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "ARTIFACT_DIR", artefacts)
    monkeypatch.setattr(config, "ALERTS_PATH", artefacts / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", artefacts / "pipeline_summary.json")
    monkeypatch.setattr(config, "GRAPH_PATH", artefacts / "graph.json")
    # keep the dashboard from probing for a live API during the test
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")

    app = AppTest.from_file(str(DASHBOARD), default_timeout=300).run()

    assert not app.exception, [str(e.value) for e in app.exception]
    assert [tab.label for tab in app.tabs] == [
        "🎯 Ranked Alerts",
        "🕸 Interactive Graph",
        "🌐 Infrastructure",
        "🔒 Evidence & Integrity",
    ]

    labels = {metric.label for metric in app.metric}
    assert {"Records processed", "Ranked alerts", "Highest risk"}.issubset(labels)
    # the infrastructure roll-up (E) and its "what next" counters render too
    assert {"With network evidence", "Distinct providers (ASN)", "Distinct countries"}.issubset(labels)

    alert_table = app.dataframe[0].value
    assert len(alert_table) > 0
    assert list(alert_table.columns)[:4] == ["Alert", "Entity", "Type", "Risk"]
    assert alert_table["Risk"].is_monotonic_decreasing

    assert any(box.label == "Select an alert to open the evidence" for box in app.selectbox)
    assert not app.error, [error.value for error in app.error]


def _network_spec(app) -> dict:
    """The interactive graph's serialized figure - the only chart 620 px tall.

    Reading the claims about the graph out of the figure is the only honest way to test
    them: "the picture shows less of the window" is a statement about how many nodes were
    drawn, which no screenshot assertion can pin down.
    """
    specs = [json.loads(element.proto.spec) for element in app.get("plotly_chart")]
    network = [spec for spec in specs if (spec.get("layout") or {}).get("height") == 620]
    assert len(network) == 1, "exactly one interactive graph must be on the page"
    return network[0]


def _drawn_nodes(spec) -> list:
    """The node ids the figure actually plotted (the marker trace carries them as data)."""
    nodes: list = []
    for trace in spec.get("data") or []:
        if trace.get("mode") == "markers" and trace.get("customdata"):
            nodes += [str(value) for value in trace["customdata"]]
    return nodes


def test_dashboard_timeline_replay_scrubs_inside_the_chart(artefacts, monkeypatch):
    """The replay is scrubbed *inside* the figure, so a drag never reruns the script.

    Two earlier versions were wrong in ways worth pinning down.  The first auto-played the
    window (~24 charts in a row, 100 ms apart): unreadable and impossible to stop.  The second
    used a Streamlit slider, and every step of a drag was a rerun that re-rendered the whole
    chart - on a few hundred nodes that reads as the page reloading.  What has to hold now:

    * no Streamlit seek widget and no play button at all,
    * the snapshots are plotly *frames* and the control is a plotly *slider* in the figure's
      own layout, so the browser animates locally with no server round trip,
    * the replay starts on the last snapshot (the whole window) and the first snapshot really
      does draw less of it,
    * the slider sits in the bottom margin, clear of the legend in the top strip.
    """
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "ARTIFACT_DIR", artefacts)
    monkeypatch.setattr(config, "ALERTS_PATH", artefacts / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", artefacts / "pipeline_summary.json")
    monkeypatch.setattr(config, "GRAPH_PATH", artefacts / "graph.json")
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")

    app = AppTest.from_file(str(DASHBOARD), default_timeout=300).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert len(app.toggle) == 1

    # nothing on the page can move the window on its own, and nothing moves it by rerunning
    keys = {getattr(button, "key", None) for button in app.button}
    assert not {"btn_replay_play", "btn_seek_back", "btn_seek_forward"} & keys
    assert not any("Play the window" in (button.label or "") for button in app.button)
    assert "replay_seek" not in {slider.key for slider in app.slider}

    app.toggle[0].set_value(True).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert not app.error, [error.value for error in app.error]

    spec = _network_spec(app)  # still one 620 px chart, now the replay figure
    frames = spec.get("frames") or []
    sliders = spec["layout"].get("sliders") or []
    assert len(frames) >= 8, "the window must be scrubbed in a useful number of steps"
    assert len(sliders) == 1, "the replay carries exactly one timeline slider"

    slider = sliders[0]
    steps = slider["steps"]
    assert len(steps) == len(frames)
    assert slider["active"] == len(frames) - 1, "it starts at the end of the window"
    assert slider["currentvalue"]["visible"] is False
    # the slider strip lives in the bottom margin and the legend in the top one, so the two
    # cannot overlap (this tab has already shipped one pair of controls on the same pixels)
    assert slider["yanchor"] == "top" and slider["y"] <= 0
    assert spec["layout"]["legend"]["y"] == 1.0
    assert spec["layout"]["margin"]["b"] > 0, "the slider needs its own strip under the plot"

    # every step animates to a frame that exists, in place and instantly: an animation would
    # queue behind the drag, a duration would make it feel sluggish, and redraw makes the
    # lines follow the nodes that just appeared
    names = {frame["name"] for frame in frames}
    for step in steps:
        assert step["method"] == "animate"
        assert step["args"][0][0] in names
        animation = step["args"][1]
        assert animation["mode"] == "immediate"
        assert animation["frame"]["duration"] == 0
        assert animation["frame"]["redraw"] is True
        assert animation["transition"]["duration"] == 0

    # a frame carries the node positions and the three direction-coloured edge traces, plus the
    # readout card: four traces, and the card names the moment of *that* frame
    ranges = spec["layout"]["xaxis"]["range"]
    for index, frame in enumerate(frames):
        assert frame["traces"] == [0, 1, 2, 3]
        assert len(frame["data"]) == 4
        node_x = frame["data"][0]["x"]
        assert len(node_x) == len(frames[-1]["data"][0]["x"]), "stable node order, frame to frame"
        readout = frame["layout"]["annotations"][0]["text"]
        assert "data known up to" in readout and "alerts exist by then" in readout
        for axis_range, values in ((ranges, node_x), (spec["layout"]["yaxis"]["range"], frame["data"][0]["y"])):
            inside = [value for value in values if axis_range[0] <= value <= axis_range[1]]
            assert inside, f"frame {index} must draw something"

    def drawn(frame) -> int:
        """Nodes the frame actually puts inside the axis range."""
        x_range, y_range = ranges, spec["layout"]["yaxis"]["range"]
        return sum(
            1
            for x, y in zip(frame["data"][0]["x"], frame["data"][0]["y"])
            if x_range[0] <= x <= x_range[1] and y_range[0] <= y <= y_range[1]
        )

    assert drawn(frames[-1]) > 1
    assert 0 < drawn(frames[0]) < drawn(frames[-1]), (
        "the first moment of the window cannot already draw the whole month"
    )
    assert [drawn(frame) for frame in frames] == sorted(drawn(frame) for frame in frames), (
        "the graph only ever grows while the window is scrubbed forward"
    )

    # the base figure is the last frame (the whole window), so the chart opens on everything
    base_x = [value for trace in spec["data"] for value in (trace.get("x") or [])]
    assert len(base_x) >= len(frames[-1]["data"][0]["x"])

    # switching the replay off hands the normal focus-mode chart straight back
    app.toggle[0].set_value(False).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    plain = _network_spec(app)
    assert not plain.get("frames"), "the normal chart is a static picture again"
    assert not (plain["layout"].get("sliders") or [])
    assert not app.error, [error.value for error in app.error]


def test_dashboard_verify_and_tamper_buttons_report_the_chain(artefacts, monkeypatch):
    """The integrity buttons must tell the truth: verify passes, tampering is caught."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "ARTIFACT_DIR", artefacts)
    monkeypatch.setattr(config, "ALERTS_PATH", artefacts / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", artefacts / "pipeline_summary.json")
    monkeypatch.setattr(config, "GRAPH_PATH", artefacts / "graph.json")
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")

    app = AppTest.from_file(str(DASHBOARD), default_timeout=300).run()

    app.button(key="btn_chain_verify").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert any("verified" in box.value for box in app.success)

    app.button(key="btn_tamper_demo").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert any("Tampering detected" in box.value for box in app.error)

    # the demo edits a copy in memory - the real ledger must still verify afterwards
    assert verify_ledger_chain()["ok"]


def test_dashboard_focus_mode_walks_the_ladder(artefacts, monkeypatch):
    """Focus mode: tap a node, hop to a neighbour, walk back, clear.

    The click handler is the one part of the graph tab that cannot be driven from a plain
    unit test, so the focus path is set directly and then walked through the two controls
    the investigator actually uses (the hop dropdown and the back button).
    """
    from streamlit.testing.v1 import AppTest

    from src.utils.focus import direct_neighbours, index_nodes

    monkeypatch.setattr(config, "ARTIFACT_DIR", artefacts)
    monkeypatch.setattr(config, "ALERTS_PATH", artefacts / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", artefacts / "pipeline_summary.json")
    monkeypatch.setattr(config, "GRAPH_PATH", artefacts / "graph.json")
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")

    payload = json.loads((artefacts / "graph.json").read_text(encoding="utf-8"))
    degree: dict = {}
    for edge in payload["edges"]:
        for key in (edge["source"], edge["target"]):
            degree[key] = degree.get(key, 0) + 1
    seeds = [node["id"] for node in payload["nodes"] if node.get("is_seed")]
    assert seeds, "the sample must plant seed wallets for this walk to be meaningful"
    start = max(seeds, key=lambda node_id: degree.get(node_id, 0))
    neighbours = direct_neighbours(start, payload["edges"])
    assert neighbours, "the seed must have direct connections"
    second = max(neighbours, key=lambda node_id: degree.get(node_id, 0))

    app = AppTest.from_file(str(DASHBOARD), default_timeout=300).run()
    unfocused = [
        json.loads(element.proto.spec) for element in app.get("plotly_chart")
    ]
    # nothing is focused yet: the whole graph is drawn at one intensity, with no route line
    # and no money-flow animation to play
    assert not any(
        trace.get("name") == "your route (breadcrumbs)" for spec in unfocused
        for trace in spec.get("data") or []
    )
    assert not any(spec.get("frames") for spec in unfocused)

    app.session_state["graph_focus"] = [start]
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]

    # the focus header names the entity, and the panel counts its direct connections
    assert any(start in element.value for element in app.markdown)
    labels = {metric.label for metric in app.metric}
    assert {"Direct connections", "Events", "Risk score"}.issubset(labels)

    # every movement is printed sender -> receiver, so "who sent this, who received it" is
    # answered by the row itself instead of a direction word to decode
    assert any("Who sent what to whom" in element.value for element in app.markdown)
    flow_tables = [
        element.value for element in app.dataframe
        if list(element.value.columns)[:2] == ["From (sent)", "To (received)"]
    ]
    assert flow_tables, "the focus panel must show who sent what to whom"
    flow_rows_shown = flow_tables[0].to_dict("records")
    assert flow_rows_shown, "a seed wallet with connections must show at least one movement"
    assert all(row["From (sent)"].split()[0] in {"wallet", "txid", "ip"}
               for row in flow_rows_shown)
    # a wallet's rows always talk about that wallet, so at least one end must be it (the table
    # prints shortened ids, so compare the prefix the table keeps)
    assert any(start[:8] in json.dumps(row) for row in flow_rows_shown)

    # hopping to a neighbour appends it to the ladder
    app.selectbox(key="focus_hop").set_value(second).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.session_state["graph_focus"] == [start, second]

    # ------------------------------------------------------------------ #
    # the picture itself: lit neighbourhood, dotted route, animated money flow
    # ------------------------------------------------------------------ #
    # The chart is asserted from its serialized figure, because "the focused node and its
    # neighbours stay lit" is a claim about traces - and the bug users report ("only the node
    # I tapped lights up, the dotted route never appears") is exactly a trace-level mistake.
    spec = next(
        json.loads(element.proto.spec) for element in app.get("plotly_chart")
        if any(
            trace.get("name") == "direct connection"
            for trace in (json.loads(element.proto.spec).get("data") or [])
        )
    )
    traces = {trace.get("name"): trace for trace in spec["data"] if trace.get("name")}

    lit = traces["direct connection"]
    assert len(lit["x"]) >= 1, "the head's direct connections must stay lit"
    faded = traces["faded - not a direct connection"]
    assert faded["marker"]["opacity"] < lit["marker"]["opacity"], "unrelated nodes must fade"

    route = traces["your route (breadcrumbs)"]
    assert route["line"]["dash"] == "dot"
    assert len([value for value in route["x"] if value is not None]) == 2, (
        "the walk just made must be drawn as one dotted hop"
    )

    # every lit hop carries an arrowhead (the legend explains them) and a coin in the
    # money-flow animation: the play button is inside the chart, and the last frame parks
    # every coin on a node, so the movement really is sender -> receiver.
    assert len(spec["layout"].get("annotations") or []) >= 1
    frames = spec.get("frames") or []
    assert len(frames) == 12
    assert frames[0]["traces"] == frames[-1]["traces"]
    play_control = (spec["layout"].get("updatemenus") or [{}])[0]
    buttons = play_control.get("buttons") or []
    assert buttons and "Animate" in buttons[0]["label"]
    # ...and it sits in the bottom-right corner of the plot area, not above it.  Plotly draws
    # its own zoom / pan / box-select toolbar in the top-right; measured in a browser, the old
    # y=1.14 pill (x 1252-1411, y 936-969) and that toolbar (x 1140-1412, y 937-963) occupied
    # the same pixels, so the two sets of controls fought over one corner.
    assert play_control["yanchor"] == "bottom" and play_control["y"] < 0.1
    assert play_control["xanchor"] == "right" and play_control["x"] > 0.9

    coins = frames[-1]["data"][-1]
    assert len(coins["x"]) == len(coins["marker"]["size"]) >= 1
    assert set(coins["marker"]["color"]) <= {"#4dabf7", "#ffa94d", "#b197fc", "#fcc419"}
    lit_xy = {
        (round(float(x), 6), round(float(y), 6))
        for trace in spec["data"]
        if trace.get("mode") == "markers" and trace.get("name") in ("direct connection", "")
        for x, y in zip(trace.get("x") or [], trace.get("y") or [])
    }
    assert lit_xy, "the focused frame must draw the head and its neighbours"
    assert all(
        (round(float(x), 6), round(float(y), 6)) in lit_xy for x, y in zip(coins["x"], coins["y"])
    ), "a finished coin must be sitting on a node, never in empty space"

    # and walking back removes the rung again, remembering the node we left so its still-live
    # chart selection cannot put it straight back
    app.button(key="btn_focus_back").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.session_state["graph_focus"] == [start]
    assert app.session_state["graph_last_click"] == second
    # ...but only for the chart instance we walked away from: the epoch moved on, so clicking
    # that same node on the new chart is an honest click and must focus it again.
    assert app.session_state["graph_last_click_epoch"] != app.session_state["graph_epoch"]

    # Reset the graph: nothing focused, the search box emptied, and the chart handed a new key
    # so it comes back with nothing selected
    app.text_input(key="node_search_box").set_value("bc1q").run()
    epoch_before = app.session_state["graph_epoch"]
    app.button(key="btn_focus_clear").click().run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.session_state["graph_focus"] == []
    assert app.session_state["node_search_box"] == ""
    assert app.session_state["graph_epoch"] == epoch_before + 1
    assert any(
        "Nothing in focus" in element.value for element in app.markdown
    ), "reset must hand back an unfocused graph"
    assert not app.error, [error.value for error in app.error]


def test_dashboard_tolerates_missing_artefacts(monkeypatch, tmp_path):
    """An empty data directory must show guidance, not a traceback."""
    from streamlit.testing.v1 import AppTest

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(config, "ARTIFACT_DIR", empty)
    monkeypatch.setattr(config, "ALERTS_PATH", empty / "alerts.json")
    monkeypatch.setattr(config, "SUMMARY_PATH", empty / "pipeline_summary.json")
    monkeypatch.setattr(config, "GRAPH_PATH", empty / "graph.json")
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:9/closed")

    app = AppTest.from_file(str(DASHBOARD), default_timeout=300).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.metric  # counters still render, showing zeros
