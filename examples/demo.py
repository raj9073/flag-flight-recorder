"""Run the two live Flagsmith incidents, their offline replays, and their exported tests."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from flag_flight_recorder import Event, FlightError, Store
from flag_flight_recorder.model import Object, canonical, object_value, read_json, sha256
from flag_flight_recorder.regression import export_pytest, load_expected
from flag_flight_recorder.replay import isolated
from flag_flight_recorder.report import render_report
from flag_flight_recorder.snapshot import RecordingFlags, Snapshot
from flag_flight_recorder.store import read_file, write_private

from .csv_export.shared import PROFILE, STREAM
from .flagsmith_lab import CONTROL, MOUNTAIN, PILOT, FlagLab, provision
from .order_service import make_request, order_service
from .portable import sample_artifacts
from .registry import build_registry
from .shipping_quote.shared import EXPRESS, REGIONAL
from .stack import Stack, settings

DIRECTORY = Path(__file__).parent
REGISTRY = "examples.registry:build_registry"


@dataclass(frozen=True)
class Scenario:
    operation: str
    label: str
    description: str
    assets: str
    segment: str
    changed_flag: str
    flags: tuple[str, ...]
    value_flags: tuple[str, ...]
    expected_error: str
    expected_message: str
    expected_file: str
    fix: str

    def event(self) -> Event:
        path = DIRECTORY / self.assets / "event.json"
        return Event.parse(read_json(read_file(path, "example event"), "Example event"))

    def expected(self) -> Path:
        return DIRECTORY / self.assets / self.expected_file

    def observations(self, flags: RecordingFlags) -> Object:
        for name in self.flags:
            flags.enabled(name)
        for name in self.value_flags:
            flags.value(name)
        return flags.observations()


CSV = Scenario(
    "csv-export",
    "CSV export",
    "The pilot streaming path writes a customer column into a summary export.",
    "csv_export",
    PILOT,
    STREAM,
    (STREAM, PROFILE),
    (PROFILE,),
    "ValueError",
    "dict contains fields not in fieldnames: 'customer'",
    "expected.csv",
    "Project each streamed row onto the selected report columns before passing it to the CSV writer.",
)
QUOTE = Scenario(
    "shipping-quote",
    "Shipping quote",
    "A regional express quote reaches a destination with a standard rate only.",
    "shipping_quote",
    MOUNTAIN,
    EXPRESS,
    (REGIONAL, EXPRESS),
    (),
    "KeyError",
    "'express'",
    "expected.json",
    "Return an unavailable quote when the requested service is absent from the destination rate table.",
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FlightError(message)


def flag_state(reads: Object, name: str) -> Object:
    return object_value(reads.get(name), f"Flag {name!r}")


def wait_for_flag(
    lab: FlagLab, event: Event, case: Scenario, enabled: bool
) -> tuple[Snapshot, Object]:
    deadline = time.monotonic() + 30
    while True:
        native = lab.snapshot()
        offline = case.observations(native.flags(event.identity, event.traits))
        online = case.observations(lab.remote(event))
        if offline == online and flag_state(online, case.changed_flag).get("enabled") is enabled:
            return native, online
        if time.monotonic() >= deadline:
            raise FlightError(
                f"Flagsmith's API and environment document did not agree for {case.label}."
            )
        time.sleep(1)


def csv_inputs(lab: FlagLab) -> tuple[Event, list[Object]]:
    sample = CSV.event()
    buckets: dict[str, Event] = {}
    for number in range(64):
        event = replace(sample, identity=f"synthetic-pilot-{number:02d}")
        reads = CSV.observations(lab.remote(event))
        profile = flag_state(reads, PROFILE).get("value")
        if isinstance(profile, str):
            buckets.setdefault(profile, event)
        if {"summary", "detailed"} <= buckets.keys():
            break
    require(
        {"summary", "detailed"} <= buckets.keys(), "Both Flagsmith CSV variants must be observed."
    )
    summary = buckets["summary"]
    checks = [
        ("Environment default", replace(summary, traits={"plan": "standard"}), False, "summary"),
        ("Pilot segment", summary, True, "summary"),
        ("Identity override", replace(summary, identity=CONTROL), False, None),
        ("Weighted variant", buckets["detailed"], True, "detailed"),
    ]
    evidence: list[Object] = []
    for label, event, streaming, expected_variant in checks:
        _, values = wait_for_flag(lab, event, CSV, streaming)
        variant = flag_state(values, PROFILE).get("value")
        require(
            variant == expected_variant or expected_variant is None,
            f"{label} did not select the expected CSV variant.",
        )
        evidence.append(
            {
                "label": label,
                "identity": event.identity,
                "streaming": streaming,
                "variant": variant,
                "parity": "live API and pinned document agree",
            }
        )
    return summary, evidence


def pytest_result(test: Path, version: str, probe: str, folder: Path) -> Object:
    receipt_file = folder / f"verification-{version}.json"
    env = {
        **os.environ,
        "FLIGHT_RECORDER_APP_VERSION": version,
        "FLIGHT_RECORDER_RESULT": str(receipt_file.resolve()),
        "FLIGHT_RECORDER_PROBE": probe,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    try:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test)],
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FlightError(f"The exported {version} test timed out.") from error
    log = process.stdout + process.stderr
    write_private(folder / f"pytest-{version}.txt", log)
    receipt = object_value(
        read_json(read_file(receipt_file, "pytest verification receipt"), "pytest receipt"),
        "pytest receipt",
    )
    require(
        process.returncode == (0 if receipt["passed"] else 1),
        f"pytest and its worker receipt disagree. See {folder}.",
    )
    receipt["pytest_exit"] = process.returncode
    return receipt


def run_case(
    lab: FlagLab,
    store: Store,
    case: Scenario,
    event: Event,
    directory: Path,
    controls: list[Object],
    service: str,
) -> Object:
    _, remote_reads = wait_for_flag(lab, event, case, True)
    first_request = make_request(service, case.operation, event, store)
    capture = store.load_capture(str(first_request["capture_id"]))
    old = store.load_snapshot(capture.snapshot_sha256)
    require(capture.reads == remote_reads, f"{case.label} capture differed from live Flagsmith.")
    require(
        capture.outcome
        == {
            "status": "error",
            "type": case.expected_error,
            "message": case.expected_message,
        },
        f"{case.label} did not produce the original application exception.",
    )
    print(f"{case.label}: original {case.expected_error}: {case.expected_message}", flush=True)

    transition = lab.set_override(case.segment, case.changed_flag, False)
    transition["occurred_at"] = now()
    require(
        transition["method"] == "PATCH" and transition["http_status"] == 200,
        f"{case.label} expected a real Flagsmith rule update.",
    )
    _, updated_reads = wait_for_flag(lab, event, case, False)
    retry = make_request(service, case.operation, event, store)
    new = store.load_snapshot(str(retry["snapshot_sha256"]))
    lab.api.stack.wait(timeout=5)
    comparison = isolated(
        "compare",
        {
            "registry": REGISTRY,
            "store": str(store.root),
            "capture": capture.id,
            "current": new.digest,
        },
        probe=lab.api.stack.url,
    )
    lab.api.stack.wait(timeout=5)
    current = object_value(comparison.get("current"), "Current run")
    current_outcome = object_value(current.get("outcome"), "Current outcome")
    require(
        first_request["http_status"] == 500
        and retry["http_status"] == 200
        and current_outcome["status"] == "success"
        and current["reads"] == updated_reads == retry["reads"]
        and canonical(retry["outcome"]) == canonical(current_outcome),
        f"{case.label} retry did not succeed under the updated live rules.",
    )
    changes = comparison.get("flag_changes")
    require(
        isinstance(changes, list)
        and len(changes) == 1
        and object_value(changes[0], "Changed flag").get("name") == case.changed_flag,
        f"{case.label} did not isolate the intended flag change.",
    )
    print(
        f"{case.label}: live rule changed, retry succeeded, and offline replay reproduced the crash.",
        flush=True,
    )

    test = export_pytest(
        store, capture.id, load_expected(case.expected()), directory / "test", REGISTRY, "fixed"
    )
    test_sha = sha256(read_file(test, "exported pytest"))
    red = pytest_result(test, "buggy", lab.api.stack.url, directory)
    green = pytest_result(test, "fixed", lab.api.stack.url, directory)
    require(
        red["outcome"] == capture.outcome
        and test_sha == sha256(read_file(test, "exported pytest")),
        "The failing pytest must reproduce the original error with the same test.",
    )
    require(
        red["passed"] is False
        and green["passed"] is True
        and red["flags_match"] is True
        and green["flags_match"] is True
        and red["snapshot_sha256"] == green["snapshot_sha256"] == old.digest
        and red["event_sha256"] == green["event_sha256"] == capture.event_sha256
        and red["app_sha256"] != green["app_sha256"],
        f"{case.label} regression runs did not isolate the code fix.",
    )
    print(
        f"{case.label}: same exported pytest failed on buggy code and passed on fixed code.",
        flush=True,
    )

    # The public evidence includes synthetic execution results, never environment documents or keys.
    axes = read_json((DIRECTORY / case.assets / "axes.json").read_bytes(), "Flag axes")
    matrix = isolated(
        "explore",
        {
            "registry": REGISTRY,
            "store": str(store.root),
            "capture": capture.id,
            "axes": axes,
            "candidate": "fixed",
        },
        probe=lab.api.stack.url,
    )
    return {
        "id": case.operation,
        "title": case.label,
        "headline": "A pilot’s CSV export failed."
        if case.operation == "csv-export"
        else "Express quoting failed in Ridge Valley.",
        "description": case.description,
        "fix": case.fix,
        "occurred_at": capture.occurred_at,
        "transition": transition,
        "comparison": comparison,
        "live_requests": {"original": first_request, "retry": retry},
        "exploration": matrix,
        "artifacts": sample_artifacts(
            capture,
            old,
            build_registry(),
            REGISTRY,
            DIRECTORY / case.assets,
            load_expected(case.expected()),
            directory,
        ),
        "regression": {"before": red, "after": green, "test_sha256": test_sha},
        "sdk_checks": list(controls),
    }


def run_demo(stack: Stack, output: Path) -> Path:
    stack.verify_state()
    stack.wait()
    with (stack.state / "demo.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FlightError("Another demo is already using this lab.") from None
        lab = provision(stack)
        run = Path(tempfile.mkdtemp(prefix="run-", dir=stack.state))
        store = Store(run / "store")
        event, controls = csv_inputs(lab)
        with order_service(store, lambda: lab.snapshot().document) as service:
            first = run_case(lab, store, CSV, event, run / "csv-export", controls, service)
            quote_event = QUOTE.event()
            _, quote_values = wait_for_flag(lab, quote_event, QUOTE, True)
            require(
                flag_state(quote_values, REGIONAL).get("enabled") is True,
                "Shipping quote requires the regional rate table.",
            )
            second = run_case(
                lab,
                store,
                QUOTE,
                quote_event,
                run / "shipping-quote",
                [
                    {
                        "label": "Mountain zone segment",
                        "identity": quote_event.identity,
                        "parity": "live API and pinned document agree",
                    }
                ],
                service,
            )
        evidence: Object = {
            "schema": 2,
            "sample": "synthetic-orders",
            "generated_at": now(),
            "flagsmith_release": lab.version,
            "sdk": "6.5.0",
            "cases": [first, second],
        }
        report = render_report(evidence)
        write_private(output, canonical(evidence), replace=True)
        write_private(output.with_suffix(".html"), report, replace=True)
        print(
            f"Evidence: {output.resolve()}\nReport: {output.with_suffix('.html').resolve()}"
            f"\nPrivate captures and exported tests: {run.resolve()}",
            flush=True,
        )
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    settings(parser)
    parser.add_argument("--output", type=Path, default=Path(".local/demo/evidence.json"))
    args = parser.parse_args()
    try:
        run_demo(Stack(args.port, args.state), args.output)
    except (FlightError, OSError) as error:
        print(f"Flight Recorder demo: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
