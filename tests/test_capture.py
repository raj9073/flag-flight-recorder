"""Capture integrity, input validation, and request-time snapshot consistency."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event as Signal

import pytest

from flag_flight_recorder import Event, FlightError, OperationContext, Recorder, Registry, Store
from flag_flight_recorder.model import canonical, read_json
from flag_flight_recorder.snapshot import Snapshot
from flag_flight_recorder.store import write_private


@pytest.mark.parametrize(
    "raw",
    [
        b'{"identity":"x","identity":"y","inputs":{}}',
        b'{"identity":"x","inputs":{"rate":NaN}}',
        b'{"identity":"x","inputs":',
        b"\xff",
    ],
)
def test_rejects_invalid_json(raw: bytes) -> None:
    with pytest.raises(FlightError):
        read_json(raw, "test input")


@pytest.mark.parametrize(
    "value,message",
    [
        ({"identity": "", "inputs": {}}, "Identity"),
        ({"identity": "x"}, "missing inputs"),
        ({"identity": "x", "inputs": {}, "typo": 3}, "unknown typo"),
        ({"identity": "x", "inputs": {}, "traits": {"tier": []}}, "Trait"),
        ({"identity": "x", "inputs": {}, "fixtures": {"../escape": {}}}, "fixture name"),
        ({"identity": "x", "inputs": {"amount": float("inf")}}, "valid JSON"),
    ],
)
def test_rejects_invalid_events(value, message: str) -> None:
    with pytest.raises(FlightError, match=message):
        Event.parse(value)


def test_public_boundary_rejects_non_string_keys() -> None:
    with pytest.raises(FlightError, match="object keys must be strings"):
        canonical({1: "unsupported"})


def test_snapshot_is_immutable(old: Snapshot) -> None:
    with pytest.raises(FrozenInstanceError):
        old.document = b"corrupt"


def test_invalid_environment_fails_explicitly() -> None:
    with pytest.raises(FlightError, match="environment document"):
        Snapshot(b'{"bad":"document"}')


def test_uninitialized_provider_fails(tmp_path: Path) -> None:
    recorder = Recorder(Store(tmp_path), Registry())
    with pytest.raises(FlightError, match="Load an environment"):
        recorder.record("unavailable", Event("pilot", {}, {}, {}))


def test_live_call_keeps_application_error_and_records_it(lab: Recorder, csv_event: Event) -> None:
    ids = []
    with pytest.raises(ValueError, match="not in fieldnames"):
        lab.run("csv-export", csv_event, "buggy", on_capture=lambda capture: ids.append(capture.id))
    assert len(ids) == 1
    result = lab.store.load_capture(ids[0])
    assert result.outcome["message"] == "dict contains fields not in fieldnames: 'customer'"
    assert result.event.digest == csv_event.digest


def test_storage_failure_preserves_original_application_result_and_error(
    lab: Recorder,
    csv_event: Event,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(*args):
        raise FlightError("Storage unavailable")

    monkeypatch.setattr(lab.store, "save_capture", fail)
    assert lab.run("csv-export", csv_event, "fixed").startswith("order_id,total_inr")
    with pytest.raises(ValueError, match="not in fieldnames"):
        lab.run("csv-export", csv_event, "buggy")
    assert "Storage unavailable" in caplog.text


def test_failing_callback_preserves_original_result(
    lab: Recorder,
    csv_event: Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failed_callback(capture):
        raise RuntimeError("callback failed")

    assert lab.run("csv-export", csv_event, on_capture=failed_callback).startswith(
        "order_id,total_inr"
    )
    assert "Capture callback failed" in caplog.text


def test_disrupted_write_leaves_no_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args):
        raise OSError(5, "simulated I/O failure")

    monkeypatch.setattr("flag_flight_recorder.store.os.link", interrupted)
    with pytest.raises(FlightError, match="Could not write record"):
        write_private(tmp_path / "capture.json", b"{}")
    assert not list(tmp_path.iterdir())


def test_content_addresses_reject_changed_bytes(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    write_private(path, b"original")
    write_private(path, b"original")
    with pytest.raises(FlightError, match="different contents"):
        write_private(path, b"different")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == b"original"


def test_legacy_snapshot_stays_selected_through_refresh(
    tmp_path: Path,
    old: Snapshot,
    current: Snapshot,
) -> None:
    selected = Signal()
    resume = Signal()

    def read_flags(context: OperationContext):
        first = context.flags.enabled("stream_report_rows")
        if context.identity == "before":
            selected.set()
            assert resume.wait(4)
        return [first, context.flags.enabled("stream_report_rows")]

    registry = Registry()
    registry.register("active-request", handler=read_flags)
    recorder = Recorder(Store(tmp_path), registry)
    recorder.snapshots.publish(old.document)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(recorder.record, "active-request", Event("before", {}, {}, {}))
        try:
            assert selected.wait(4)
            recorder.snapshots.publish(current.document)
            second = recorder.record("active-request", Event("after", {}, {}, {}))
        finally:
            resume.set()
        first = pending.result(timeout=4)

    assert first.snapshot_sha256 == old.digest
    assert first.outcome == {"status": "success", "result": [True, True]}
    assert second.snapshot_sha256 == current.digest
    assert second.outcome == {"status": "success", "result": [False, False]}


def test_duplicate_and_unregistered_operations_are_reported() -> None:
    def handler(context: OperationContext) -> str:
        return context.identity

    registry = Registry()
    registry.register("my-handler", handler=handler)
    with pytest.raises(FlightError, match="already registered"):
        registry.register("my-handler", handler=handler)
    with pytest.raises(FlightError, match="Unknown operation"):
        registry.get("absent", "current")
    with pytest.raises(FlightError, match="Invalid operation"):
        registry.register("../escaped", handler=handler)


def test_failed_refresh_keeps_last_document(tmp_path: Path, old: Snapshot) -> None:
    recorder = Recorder(Store(tmp_path), Registry())
    recorder.snapshots.publish(old.document)
    with pytest.raises(FlightError, match="environment document"):
        recorder.snapshots.publish(b"{}")
    assert recorder.snapshots.current().digest == old.digest
