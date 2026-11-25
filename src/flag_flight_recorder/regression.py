"""Export and run a portable pytest regression case with historical rules."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .integration import Registry, evaluate
from .model import (
    JSON,
    SHA256,
    FlightError,
    Object,
    canonical,
    checked_id,
    fields,
    object_value,
    read_json,
    sha256,
    string_value,
)
from .replay import isolated, verify_runtime
from .snapshot import Snapshot
from .store import Store, decode_capture, read_file, write_private


def load_expected(path: Path) -> JSON:
    content = read_file(path, "expected result")
    if path.suffix.lower() == ".json":
        return read_json(content, "Expected result")
    if path.suffix.lower() == ".csv":
        try:
            return content.decode("utf-8")
        except UnicodeError as error:
            raise FlightError("The expected CSV must be UTF-8.") from error
    raise FlightError("Provide the intended application result in a .csv or .json file.")


def export_pytest(
    store: Store,
    capture_id: str,
    expected: JSON,
    output: Path,
    registry: str,
    version: str,
) -> Path:
    capture = store.load_capture(capture_id)
    snapshot = store.load_snapshot(capture.snapshot_sha256)
    output = output.resolve()
    if output.exists():
        raise FlightError(f"Regression directory already exists at {output}.")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".regression-", dir=output.parent))
    except OSError as error:
        raise FlightError(
            f"Could not create the regression directory: {error.strerror}."
        ) from error
    try:
        capture_bytes = canonical(capture.document())
        write_private(temporary / "capture.json", capture_bytes)
        write_private(temporary / "environment.json", snapshot.document)
        for content in capture.event.fixtures.values():
            payload = canonical(content)
            write_private(temporary / "fixtures" / f"{sha256(payload)}.json", payload)
        spec: Object = {
            "schema": 1,
            "capture_sha256": sha256(capture_bytes),
            "expected": expected,
            "registry": registry,
            "app_version": version,
        }
        spec_bytes = canonical(spec)
        write_private(temporary / "regression.json", spec_bytes)
        test_source = (
            '"""Verify the expected result against the original Flagsmith environment."""\n'
            "from pathlib import Path\n\n"
            "from flag_flight_recorder.regression import assert_regression\n\n\n"
            "def test_flag_dependent_operation() -> None:\n"
            f"    assert_regression(Path(__file__).parent, spec_sha256={sha256(spec_bytes)!r})\n"
        )
        write_private(temporary / "test_incident.py", test_source.encode("utf-8"))
        try:
            temporary.rename(output)
        except OSError as error:
            raise FlightError(
                f"Could not publish the regression directory: {error.strerror}."
            ) from error
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return output / "test_incident.py"


def _spec(folder: Path, digest: str | None = None) -> tuple[Object, str]:
    raw = read_file(folder / "regression.json", "regression specification")
    actual_digest = sha256(raw)
    if digest is not None and actual_digest != digest:
        raise FlightError("Regression specification digest mismatch.")
    spec = object_value(read_json(raw, "Regression specification"), "Regression specification")
    fields(
        spec,
        {"schema", "capture_sha256", "expected", "registry", "app_version"},
        set(),
        "regression",
    )
    if type(spec["schema"]) is not int or spec["schema"] != 1:
        raise FlightError("Unsupported regression specification schema.")
    return spec, actual_digest


def evaluate_bundle(folder: Path, registry: Registry, version: str, spec_sha256: str) -> Object:
    spec, _ = _spec(folder, spec_sha256)
    captured = read_file(folder / "capture.json", "regression capture")
    if sha256(captured) != checked_id(spec["capture_sha256"], SHA256, "capture digest"):
        raise FlightError("Regression capture digest mismatch.")
    data = object_value(read_json(captured, "Regression capture"), "Regression capture")
    capture = decode_capture(data, folder / "fixtures")
    snapshot = Snapshot(read_file(folder / "environment.json", "regression snapshot"))
    if snapshot.digest != capture.snapshot_sha256:
        raise FlightError("Regression snapshot digest mismatch.")
    verify_runtime(capture)
    operation = registry.get(capture.operation, version)
    execution = evaluate(operation, snapshot, capture.event)
    expected = spec["expected"]
    flags_match = canonical(execution.reads) == canonical(capture.reads)
    status = execution.outcome["status"]
    outcome_matches = status == "success" and canonical(execution.outcome["result"]) == canonical(
        expected
    )
    if not flags_match:
        detail = "Evaluated flag reads differ from the captured conditions."
    elif status == "error":
        detail = f"{execution.outcome['type']}: {execution.outcome['message']}"
    elif not outcome_matches:
        detail = "Application output differs from the expected result."
    else:
        detail = "Historical flags and expected application result match."
    return {
        "passed": flags_match and outcome_matches,
        "detail": detail,
        "capture_id": capture.id,
        "operation": capture.operation,
        "app_version": version,
        "app_sha256": operation.fingerprint(),
        "snapshot_sha256": snapshot.digest,
        "event_sha256": capture.event_sha256,
        "flags_match": flags_match,
        "reads": execution.reads,
        "outcome": execution.outcome,
        "expected": expected,
    }


def run_regression(
    folder: Path,
    version: str | None = None,
    *,
    spec_sha256: str | None = None,
    probe: str | None = None,
) -> Object:
    spec, digest = _spec(folder, spec_sha256)
    registry = string_value(spec["registry"], "Application registry")
    selected = version or string_value(spec["app_version"], "Application version")
    return isolated(
        "regression",
        {
            "registry": registry,
            "folder": str(folder.resolve()),
            "version": selected,
            "spec_sha256": digest,
        },
        probe,
    )


def assert_regression(folder: Path, *, spec_sha256: str) -> None:
    result = run_regression(
        folder,
        os.environ.get("FLIGHT_RECORDER_APP_VERSION"),
        spec_sha256=spec_sha256,
        probe=os.environ.get("FLIGHT_RECORDER_PROBE"),
    )
    if not result["passed"]:
        raise AssertionError(f"{result['operation']} ({result['app_version']}): {result['detail']}")
