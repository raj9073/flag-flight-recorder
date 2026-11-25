"""Produce a distributable synthetic pytest bundle without copying a live environment."""

from __future__ import annotations

import base64
import difflib
import io
import zipfile
from importlib.metadata import version
from pathlib import Path

from flag_flight_recorder import Recorder, Store
from flag_flight_recorder.integration import Registry
from flag_flight_recorder.model import JSON, Capture, FlightError, Object, runtime, sha256
from flag_flight_recorder.regression import export_pytest, run_regression
from flag_flight_recorder.snapshot import Snapshot, effective_snapshot


def sample_artifacts(
    original: Capture,
    snapshot: Snapshot,
    registry: Registry,
    factory: str,
    example: Path,
    expected: JSON,
    directory: Path,
) -> Object:
    flags = snapshot.flags(original.event.identity, original.event.traits).resolved()
    projection = effective_snapshot({name: flags[name] for name in original.reads})
    store = Store(directory / "fixture-store")
    recorder = Recorder(store, registry)
    recorder.snapshots.publish(projection.document)
    sample = recorder.record(original.operation, original.event, original.app_version)
    if sample.outcome != original.outcome or sample.reads != original.reads:
        raise FlightError("The portable fixture did not reproduce the demonstrated incident.")
    bundle = directory / "portable"
    export_pytest(store, sample.id, expected, bundle, factory, "fixed")
    red = run_regression(bundle, "buggy")
    green = run_regression(bundle, "fixed")
    if red["passed"] or not green["passed"] or red["spec_sha256"] != green["spec_sha256"]:
        raise FlightError("The separate portable fixture did not pass fix verification.")
    release = version("flag-flight-recorder")
    wheel = f"flag_flight_recorder-{release}-py3-none-any.whl"
    instructions = (
        f"Flag Flight Recorder {release} · {original.operation}\n\n"
        "This synthetic fixture contains the effective flag values for this example. "
        "Its capture and snapshot digests belong to the fixture.\n\n"
        "From the directory containing test_incident.py, use uv on Linux with "
        "user and network namespaces:\n\n"
        f"uv venv --python {runtime()['python']} .venv\n"
        ". .venv/bin/activate\n"
        f"uv pip install pytest https://github.com/raj9073/flag-flight-recorder/releases/download/v{release}/{wheel}\n"
        "FLIGHT_RECORDER_APP_VERSION=buggy python -m pytest -q test_incident.py\n"
        "FLIGHT_RECORDER_APP_VERSION=fixed python -m pytest -q test_incident.py\n"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", instructions)
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    data = stream.getvalue()
    diff = "".join(
        difflib.unified_diff(
            (example / "buggy.py").read_text().splitlines(True),
            (example / "fixed.py").read_text().splitlines(True),
            fromfile=f"{original.operation}/buggy.py",
            tofile=f"{original.operation}/fixed.py",
        )
    )
    return {
        "filename": f"{original.operation}-synthetic-pytest.zip",
        "zip_base64": base64.b64encode(data).decode("ascii"),
        "zip_sha256": sha256(data),
        "fixture_snapshot": projection.digest,
        "fixture_spec": green["spec_sha256"],
        "patch": diff,
        "runtime": runtime(),
    }
