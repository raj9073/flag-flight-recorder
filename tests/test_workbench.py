"""Real receipts, linked regression verdicts and controlled feature combinations."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import EXAMPLES, environment

from examples.order_service import make_request, order_service
from examples.registry import build_registry
from flag_flight_recorder import Event, FlightError, Recorder, Store
from flag_flight_recorder.cli import main
from flag_flight_recorder.exploration import parse_axes
from flag_flight_recorder.model import sha256
from flag_flight_recorder.regression import export_pytest, run_regression
from flag_flight_recorder.replay import isolated
from flag_flight_recorder.report import report_data

FACTORY = "examples.registry:build_registry"


@pytest.fixture(scope="module")
def incident(tmp_path_factory):
    folder = tmp_path_factory.mktemp("real-receipts")
    store = Store(folder / "store")
    sample = EXAMPLES / "csv_export"
    event = Event.parse(json.loads((sample / "event.json").read_bytes()))
    active = environment()
    with order_service(store, lambda: active.document) as url:
        original = make_request(url, "csv-export", event, store)
        active = environment(stream=False)
        retry = make_request(url, "csv-export", event, store)
    cid = original["capture_id"]
    args = {"store": str(store.root), "capture": cid, "registry": FACTORY}
    comparison = isolated("compare", {**args, "current": active.digest})
    axes = json.loads((sample / "axes.json").read_bytes())
    matrix = isolated("explore", {**args, "axes": axes, "candidate": "fixed"})
    suite = folder / "suite"
    expected = (sample / "expected.csv").read_text()
    wrapper = export_pytest(store, cid, expected, suite, FACTORY, "fixed")
    runs = {
        "before": run_regression(suite, "buggy"),
        "after": run_regression(suite, "fixed"),
        "test_sha256": sha256(wrapper.read_bytes()),
    }
    other = folder / "other-expectation"
    export_pytest(store, cid, expected.splitlines()[0], other, FACTORY, "fixed")
    case = {
        "id": "csv",
        "title": "CSV export",
        "comparison": comparison,
        "live_requests": {"original": original, "retry": retry},
        "regression": runs,
        "exploration": matrix,
    }
    return {"schema": 2, "cases": [case]}, run_regression(other, "fixed"), args


def test_public_claims_and_private_summary(incident):
    data, _, _ = incident
    valid = report_data(data)
    case = valid["cases"][0]
    assert case["regression"]["binding"] == "verified"
    assert "binding" not in data["cases"][0]["regression"]
    assert case["live_requests"]["original"]["http_status"] == 500
    assert case["live_requests"]["retry"]["http_status"] == 200
    assert case["comparison"]["occurred_at"] == case["live_requests"]["original"]["occurred_at"]
    assert set(case["comparison"]["reasons"]) == {"stream_report_rows", "export_column_profile"}
    cells = case["exploration"]["cells"]
    assert [
        cell["settings"] for cell in cells if cell["original"]["outcome"]["status"] == "error"
    ] == [[True, "summary"]]
    assert all(cell["candidate"]["outcome"]["status"] == "success" for cell in cells)
    assert len({cell["snapshot_sha256"] for cell in cells}) == 4
    assert valid["share_summary"] == {
        "format": "flight-recorder-summary-v1",
        "incidents": [
            {
                "changed_flags": 1,
                "replay_matched": True,
                "before_passed": False,
                "after_passed": True,
            }
        ],
    }


def test_two_real_exports_with_different_expected_output_cannot_prove_a_fix(incident):
    data, other, _ = incident
    case = data["cases"][0]
    assert not other["passed"] and case["regression"]["after"]["passed"]
    assert other["app_sha256"] == case["regression"]["after"]["app_sha256"]
    with pytest.raises(FlightError, match="different expectations"):
        report_data(case["comparison"], {"before": other, "after": case["regression"]["after"]})


@pytest.mark.parametrize(
    ("keys", "value", "message"),
    [
        (("regression", "after", "spec_sha256"), "a" * 64, "different test specifications"),
        (("regression", "test_sha256"), "a" * 64, "does not pin"),
        (("regression", "before", "passed"), True, "verdict disagrees"),
        (("regression", "after", "pytest_exit"), 1, "pytest exit"),
        (("regression", "after", "app_sha256"), "SAME_CODE", "Identical application"),
        (("live_requests", "retry", "http_status"), 500, "method or status"),
        (("live_requests", "original", "occurred_at"), "another time", "another capture"),
        (("exploration", "cells", 1, "settings"), [False, "summary"], "duplicate"),
        (
            ("exploration", "cells", 2, "original", "reads", "stream_report_rows", "enabled"),
            False,
            "contradicts",
        ),
        (
            ("exploration", "cells", 2, "original", "outcome"),
            {"status": "success", "result": "unexpected"},
            "historical evidence",
        ),
        (("exploration", "candidate_app_sha256"), "b" * 64, "differs from the regression"),
        (("comparison", "flag_changes"), [], "disagree"),
    ],
)
def test_report_rejects_inconsistent_evidence(incident, keys, value, message):
    data = deepcopy(incident[0])
    case = data["cases"][0]
    target = case
    for k in keys[:-1]:
        target = target[k]
    target[keys[-1]] = case["comparison"]["app_sha256"] if value == "SAME_CODE" else value
    with pytest.raises(FlightError, match=message):
        report_data(data)


def test_schema_one_and_comparisons_without_a_candidate(incident):
    data = deepcopy(incident[0])
    case = data["cases"][0]
    bare = report_data(case["comparison"])["cases"][0]
    assert bare["occurred_at"] == case["comparison"]["occurred_at"]
    assert bare["regression"] is None
    for key in ("before", "after"):
        del case["regression"][key]["spec_sha256"]
    del case["exploration"], case["live_requests"]
    data["schema"] = 1
    archive = report_data(data)
    assert archive["cases"][0]["regression"]["binding"] == "legacy"
    assert "after_passed" not in archive["share_summary"]["incidents"][0]
    data["schema"] = 2
    with pytest.raises(FlightError, match="specification digest"):
        report_data(data)


def test_exploration_rejects_axes_missing_the_captured_value(incident):
    _, _, args = incident
    axes = json.loads((EXAMPLES / "csv_export" / "axes.json").read_bytes())
    axes[1]["values"] = ["detailed", "other"]
    with pytest.raises(FlightError, match="Include the original"):
        isolated("explore", {**args, "axes": axes, "candidate": "fixed"})
    axes[0]["values"] = [0, 1]
    with pytest.raises(FlightError, match="boolean"):
        parse_axes(axes)


def test_error_only_capture_preserves_calls_and_lists_metadata(
    tmp_path: Path, old, csv_event, capsys
):
    recorder = Recorder(Store(tmp_path / "records"), build_registry(), errors_only=True)
    recorder.snapshots.publish(old.document)
    seen = []
    assert "ORD-1001" in recorder.run("csv-export", csv_event, "fixed", seen.append)
    assert recorder.store.list_captures(None, 10) == [] and seen == []
    with pytest.raises(ValueError, match="customer"):
        recorder.run("csv-export", csv_event, "buggy", seen.append)
    assert len(seen) == 1
    assert (
        main(
            [
                "--store",
                str(recorder.store.root),
                "list",
                "--operation",
                "csv-export",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["id"] == seen[0].id
    assert set(rows[0]) == {
        "id",
        "occurred_at",
        "operation",
        "app_version",
        "snapshot_sha256",
        "status",
    }
    assert recorder.record("csv-export", csv_event, "fixed").outcome["status"] == "success"
    assert len(recorder.store.list_captures(None, 10)) == 2
    assert recorder.store.list_captures("another", 1) == []
    with pytest.raises(FlightError, match="limit"):
        recorder.store.list_captures(None, 0)
    assert main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"]
