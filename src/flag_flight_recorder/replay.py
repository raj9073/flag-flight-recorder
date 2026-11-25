"""Historical replay and configuration comparison in an isolated worker."""

from __future__ import annotations

import contextlib
import errno
import importlib
import ipaddress
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .integration import Execution, Registry, evaluate
from .model import (
    Capture,
    FlightError,
    Object,
    canonical,
    object_value,
    read_json,
    runtime,
    string_value,
)
from .snapshot import Snapshot
from .store import Store


def load_registry(spec: str) -> Registry:
    module, separator, factory_name = spec.partition(":")
    if not separator or not module or not factory_name.isidentifier():
        raise FlightError("A registry must be named as an importable module:factory.")
    try:
        factory = getattr(importlib.import_module(module), factory_name)
        registry = factory()
    except Exception as error:
        raise FlightError(f"Could not load application registry {spec!r}: {error}") from error
    if not isinstance(registry, Registry):
        raise FlightError("The registry factory must return a Registry.")
    return registry


def verify_runtime(capture: Capture) -> None:
    observed = runtime()
    if observed != capture.evaluator:
        raise FlightError(
            f"Flagsmith evaluator fingerprint mismatch. Captured {capture.evaluator!r}, observed {observed!r}."
        )


def historical(registry: Registry, capture: Capture, snapshot: Snapshot) -> Execution:
    verify_runtime(capture)
    if snapshot.digest != capture.snapshot_sha256:
        raise FlightError(
            f"Historical environment digest mismatch. Captured {capture.snapshot_sha256}, observed {snapshot.digest}."
        )
    operation = registry.get(capture.operation, capture.app_version)
    fingerprint = operation.fingerprint()
    if fingerprint != capture.app_sha256:
        raise FlightError(
            f"Application code fingerprint mismatch. Captured {capture.app_sha256}, observed {fingerprint}."
        )
    result = evaluate(operation, snapshot, capture.event)
    if canonical(result.reads) != canonical(capture.reads):
        raise FlightError(
            f"Replay diverged from the captured flag reads. Changes: {flag_diff(capture.reads, result.reads)!r}"
        )
    if canonical(result.outcome) != canonical(capture.outcome):
        raise FlightError(
            f"Replay diverged from the captured application outcome. Captured {str(capture.outcome)[:360]}, observed {str(result.outcome)[:360]}."
        )
    return result


def execution_document(snapshot: Snapshot, execution: Execution) -> Object:
    return {
        "snapshot_sha256": snapshot.digest,
        "reads": execution.reads,
        "outcome": execution.outcome,
    }


def flag_diff(before: Object, after: Object) -> list[Object]:
    return [
        {"name": name, "before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if canonical(before.get(name)) != canonical(after.get(name))
    ]


def network_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/net")
    except OSError as error:
        raise FlightError("Offline execution needs Linux network namespaces.") from error


def verify_isolation(parent_namespace: str, probe: str | None) -> Object:
    child = network_namespace()
    interfaces = sorted(name for _, name in socket.if_nameindex())
    if child == parent_namespace or interfaces != ["lo"]:
        raise FlightError(
            "The replay worker needs a separate network namespace with only loopback."
        )
    evidence: Object = {"namespace": child, "interfaces": list(interfaces), "isolated": True}
    if probe is None:
        return evidence

    try:
        address = urlsplit(probe)
        host = address.hostname or ""
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        if address.scheme not in ("http", "https") or not is_loopback:
            raise ValueError("expected a loopback HTTP(S) URL")
        port = address.port or (443 if address.scheme == "https" else 80)
        numeric = "127.0.0.1" if host == "localhost" else host
    except ValueError as error:
        raise FlightError("The isolation probe expects a loopback HTTP(S) URL.") from error

    try:
        with socket.create_connection((numeric, port), timeout=1):
            raise FlightError("The live listener was reachable from the replay worker.")
    except OSError as error:
        if error.errno not in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH):
            raise FlightError(
                f"Could not verify isolation against the listener: {error}"
            ) from error
        evidence.update(
            {
                "probe_host": numeric,
                "probe_port": port,
                "live_listener_reachable": False,
                "connection_error": errno.errorcode[error.errno],
            }
        )
    return evidence


def isolated(mode: str, arguments: Object, probe: str | None = None) -> Object:
    request: Object = {
        **arguments,
        "mode": mode,
        "parent_namespace": network_namespace(),
        "probe": probe,
    }
    command = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--",
        sys.executable,
        "-m",
        "flag_flight_recorder.replay",
    ]
    try:
        process = subprocess.run(
            command, input=canonical(request), capture_output=True, check=False, timeout=45
        )
    except FileNotFoundError as error:
        raise FlightError("Install unshare to run the offline worker.") from error
    except subprocess.TimeoutExpired as error:
        raise FlightError("Offline worker exceeded its 45-second execution limit.") from error

    if not process.stdout:
        message = process.stderr.decode(errors="replace").strip()
        raise FlightError(f"Could not run the isolated worker: {message or process.returncode}.")
    result = object_value(read_json(process.stdout, "Worker result"), "Worker result")
    if process.returncode:
        message = string_value(result.get("error"), "Worker error")
        raise FlightError(message)
    return result


def _dispatch(request: Object) -> Object:
    probe_value = request.get("probe")
    probe = None if probe_value is None else string_value(probe_value, "Isolation probe")
    isolation = verify_isolation(
        string_value(request.get("parent_namespace"), "Parent namespace"), probe
    )
    registry = load_registry(string_value(request.get("registry"), "Application registry"))
    mode = request.get("mode")
    if mode == "regression":
        from .regression import evaluate_bundle

        folder = Path(string_value(request.get("folder"), "Regression fixture directory"))
        result = evaluate_bundle(
            folder,
            registry,
            string_value(request.get("version"), "Application version"),
            string_value(request.get("spec_sha256"), "Regression specification digest"),
        )
        return {**result, "isolation": isolation}

    store = Store(Path(string_value(request.get("store"), "Recorder store")))
    capture = store.load_capture(string_value(request.get("capture"), "Capture ID"))
    original = store.load_snapshot(capture.snapshot_sha256)
    replayed = historical(registry, capture, original)
    report: Object = {
        "status": "reproduced",
        "occurred_at": capture.occurred_at,
        "runtime": capture.evaluator,
        "registry": string_value(request.get("registry"), "Registry"),
        "reasons": original.flags(capture.event.identity, capture.event.traits).reasons(
            capture.reads
        ),
        "capture_id": capture.id,
        "operation": capture.operation,
        "app_version": capture.app_version,
        "app_sha256": capture.app_sha256,
        "event_sha256": capture.event_sha256,
        "identity": capture.event.identity,
        "traits": dict(capture.event.traits),
        "recorded": {
            "snapshot_sha256": capture.snapshot_sha256,
            "reads": capture.reads,
            "outcome": capture.outcome,
        },
        "historical": execution_document(original, replayed),
        "isolation": isolation,
    }
    if mode == "explore":
        from .exploration import explore

        return explore(
            registry,
            capture,
            original,
            request["axes"],
            string_value(request.get("candidate"), "Candidate version"),
            isolation,
        )
    if mode == "replay":
        return report
    if mode != "compare":
        raise FlightError("Unknown offline execution mode.")

    current = store.load_snapshot(string_value(request.get("current"), "Current snapshot"))
    updated = evaluate(registry.get(capture.operation, capture.app_version), current, capture.event)
    report.update(
        {
            "current": execution_document(current, updated),
            "flag_changes": list(flag_diff(replayed.reads, updated.reads)),
            "outcome_changed": canonical(replayed.outcome) != canonical(updated.outcome),
        }
    )
    return report


def main() -> int:
    try:
        request = object_value(
            read_json(sys.stdin.buffer.read(), "Worker request"), "Worker request"
        )
        with contextlib.redirect_stdout(sys.stderr):
            evidence = _dispatch(request)
        print(canonical(evidence).decode())
        return 0
    except FlightError as error:
        print(canonical({"error": str(error)}).decode())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
