"""Real isolated replay, comparison, and portable pytest verification."""

from dataclasses import replace
from pathlib import Path

import pytest

from examples.registry import build_registry
from flag_flight_recorder import Event, FlightError, OperationContext, Recorder, Registry
from flag_flight_recorder.model import canonical, object_value, read_json
from flag_flight_recorder.regression import assert_regression, export_pytest, run_regression
from flag_flight_recorder.replay import historical, isolated, network_namespace, verify_isolation
from flag_flight_recorder.snapshot import Snapshot

REGISTRY = "examples.registry:build_registry"
CSV = "order_id,total_inr\nORD-1001,1250.00\nORD-1002,2475.50\n"


def test_replay_comparison_reexecutes_both_historical_and_updated_settings(
    lab: Recorder,
    current: Snapshot,
    csv_event: Event,
) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    lab.store.save_snapshot(current)
    result = isolated(
        "compare",
        {
            "store": str(lab.store.root),
            "registry": REGISTRY,
            "capture": capture.id,
            "current": current.digest,
        },
    )
    assert result["recorded"] == result["historical"]
    assert object_value(result["current"], "Current execution")["outcome"] == {
        "status": "success",
        "result": CSV,
    }
    assert result["flag_changes"] == [
        {"name": "stream_report_rows", "before": {"enabled": True}, "after": {"enabled": False}}
    ]
    isolation = object_value(result["isolation"], "Isolation")
    assert isolation["interfaces"] == ["lo"] and isolation["namespace"] != network_namespace()


def test_direct_replay_requires_real_isolation() -> None:
    with pytest.raises(FlightError, match="separate network namespace"):
        verify_isolation(network_namespace(), None)


def test_historical_rejects_new_snapshot_and_version(
    lab: Recorder,
    old: Snapshot,
    current: Snapshot,
    csv_event: Event,
) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    with pytest.raises(FlightError, match="snapshot|environment digest"):
        historical(lab.registry, capture, current)
    with pytest.raises(FlightError, match="evaluator fingerprint"):
        historical(
            lab.registry,
            replace(capture, evaluator={**capture.evaluator, "flagsmith": "changed"}),
            old,
        )

    def replacement(context: OperationContext) -> str:
        return context.identity

    registry = Registry()
    registry.register("csv-export", handler=replacement, version="buggy")
    with pytest.raises(FlightError, match="code fingerprint"):
        historical(registry, capture, old)


def test_historical_rejects_divergent_reads_or_outcome(
    lab: Recorder,
    old: Snapshot,
    csv_event: Event,
) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    with pytest.raises(FlightError, match="captured flag reads"):
        historical(lab.registry, replace(capture, reads={}), old)
    with pytest.raises(FlightError, match="captured application outcome"):
        historical(lab.registry, replace(capture, outcome={"status": "success", "result": ""}), old)


def test_rejects_corrupted_snapshot(lab: Recorder, old: Snapshot) -> None:
    path = lab.store.root / "snapshots" / f"{old.digest}.json"
    path.write_bytes(b"{}")
    with pytest.raises(FlightError, match="snapshot digest mismatch"):
        lab.store.load_snapshot(old.digest)


@pytest.mark.parametrize("missing", [True, False])
def test_rejects_missing_or_corrupted_external_fixture(
    lab: Recorder,
    quote_event: Event,
    missing: bool,
) -> None:
    capture = lab.record("shipping-quote", quote_event, "buggy")
    (fixture,) = (lab.store.root / "fixtures").glob("*.json")
    if missing:
        fixture.unlink()
    else:
        fixture.write_bytes(b"{}")
    with pytest.raises(FlightError, match="fixture 'rates'|Fixture digest mismatch"):
        lab.store.load_capture(capture.id)


def test_rejects_modified_operation_input(lab: Recorder, csv_event: Event) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    manifest = lab.store.root / "captures" / f"{capture.id}.json"
    data = object_value(read_json(manifest.read_bytes(), "Capture"), "Capture")
    object_value(data["event"], "Event")["identity"] = "synthetic-different-identity"
    manifest.write_bytes(canonical(data))
    with pytest.raises(FlightError, match="input digest mismatch"):
        lab.store.load_capture(capture.id)


@pytest.mark.parametrize(
    "operation,event_fixture,expected",
    [
        ("csv-export", "csv_event", CSV),
        (
            "shipping-quote",
            "quote_event",
            {
                "status": "unavailable",
                "destination": "Ridge Valley",
                "service": "express",
            },
        ),
    ],
)
def test_exported_test_accepts_fix_under_identical_rules(
    lab: Recorder,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    operation,
    event_fixture,
    expected,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = lab.record(operation, request.getfixturevalue(event_fixture), "buggy")
    output = tmp_path / operation
    test = export_pytest(lab.store, capture.id, expected, output, REGISTRY, "fixed")
    assert test.name == "test_incident.py"
    before = run_regression(output, "buggy")
    after = run_regression(output, "fixed")
    assert before["passed"] is False and before["flags_match"] is True
    assert after["passed"] is True and after["flags_match"] is True
    assert before["event_sha256"] == after["event_sha256"] == capture.event_sha256
    assert before["snapshot_sha256"] == after["snapshot_sha256"] == capture.snapshot_sha256
    assert before["app_sha256"] != after["app_sha256"]

    from flag_flight_recorder.model import sha256

    spec_hash = sha256((output / "regression.json").read_bytes())
    monkeypatch.setenv("FLIGHT_RECORDER_APP_VERSION", "buggy")
    with pytest.raises(AssertionError, match=operation):
        assert_regression(output, spec_sha256=spec_hash)
    monkeypatch.setenv("FLIGHT_RECORDER_APP_VERSION", "fixed")
    assert_regression(output, spec_sha256=spec_hash)


def test_wrong_expectation_fails_even_when_handler_returns_success(
    lab: Recorder,
    csv_event: Event,
    tmp_path: Path,
) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    output = tmp_path / "bad-expectation"
    export_pytest(lab.store, capture.id, "incorrect result", output, REGISTRY, "fixed")
    result = run_regression(output)
    assert result["flags_match"] is True
    assert result["passed"] is False
    assert "differs from the expected result" in result["detail"]


def test_export_refuses_to_overwrite_and_detects_spec_edit(
    lab: Recorder,
    csv_event: Event,
    tmp_path: Path,
) -> None:
    from flag_flight_recorder.model import sha256

    capture = lab.record("csv-export", csv_event, "buggy")
    output = tmp_path / "export"
    export_pytest(lab.store, capture.id, CSV, output, REGISTRY, "fixed")
    with pytest.raises(FlightError, match="already exists"):
        export_pytest(lab.store, capture.id, CSV, output, REGISTRY, "fixed")
    spec = output / "regression.json"
    digest = sha256(spec.read_bytes())
    spec.write_bytes(spec.read_bytes() + b"\n")
    with pytest.raises(FlightError, match="specification digest mismatch"):
        assert_regression(output, spec_sha256=digest)


def test_cli_reports_unknown_registry() -> None:
    from flag_flight_recorder.replay import load_registry

    with pytest.raises(FlightError, match="Could not load"):
        load_registry("nonexistent_recording_app:build")
    assert build_registry().get("shipping-quote", "fixed").name == "shipping-quote"
