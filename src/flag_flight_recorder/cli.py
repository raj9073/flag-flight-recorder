"""Command line for capture, comparison, replay and pytest export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .diagnostics import preflight
from .integration import Recorder
from .model import JSON, Event, FlightError, Object, object_value, read_json
from .regression import export_pytest, load_expected, run_regression
from .replay import isolated, load_registry
from .report import render_report, report_data, serve_report, sharing_summary
from .snapshot import Snapshot, fetch_document
from .store import Store, read_file, write_private


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="flight-recorder",
        description="Capture, replay and verify flag-dependent Python operations with Flagsmith.",
    )
    root.add_argument(
        "--store",
        type=Path,
        default=Path(os.getenv("FLIGHT_RECORDER_STORE", ".local/recorder")),
        help="private recorder directory (default: .local/recorder)",
    )
    subs = root.add_subparsers(dest="command", required=True)

    listing = subs.add_parser("list", help="list recent captures without exposing their inputs")
    listing.add_argument("--operation")
    listing.add_argument("--limit", type=int, default=20)
    doctor = subs.add_parser("doctor", help="check replay and optional live demo prerequisites")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--port", type=int, default=13810)
    doctor.add_argument("--viewer-port", type=int, default=13811)
    share = subs.add_parser("share", help="export an anonymous summary of validated evidence")
    share.add_argument("evidence", type=Path)
    share.add_argument("--output", type=Path, required=True)
    snapshot = subs.add_parser("snapshot", help="store an environment document from Flagsmith")
    source = snapshot.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="self-hosted Flagsmith base URL")
    source.add_argument("--file", type=Path, help="an exported environment document")

    capture = subs.add_parser("capture", help="run a registered operation and record its outcome")
    capture.add_argument("operation")
    capture.add_argument("--event", type=Path, required=True)
    capture.add_argument("--snapshot", required=True)
    capture.add_argument("--registry", required=True, metavar="MODULE:FACTORY")
    capture.add_argument("--app-version", default="current")

    for command in ("replay", "compare", "explore"):
        replay = subs.add_parser(command, help=f"{command} a capture in a network-isolated worker")
        replay.add_argument("capture")
        replay.add_argument("--registry", required=True, metavar="MODULE:FACTORY")
        replay.add_argument(
            "--probe", help="optional live loopback HTTP(S) listener for isolation evidence"
        )
        replay.add_argument("--output", type=Path)
        if command == "explore":
            replay.add_argument(
                "--axes",
                type=Path,
                required=True,
                help="two effective flags and two values per flag",
            )
            replay.add_argument("--candidate-version", required=True)
        if command == "compare":
            replay.add_argument("--current", required=True, help="current snapshot digest")

    export = subs.add_parser(
        "export-pytest", help="create a pytest test using the historical rules"
    )
    export.add_argument("capture")
    export.add_argument("--registry", required=True, metavar="MODULE:FACTORY")
    export.add_argument("--expect", type=Path, required=True, help="expected .csv or .json result")
    export.add_argument(
        "--output", type=Path, required=True, help="new directory for the test and fixtures"
    )
    export.add_argument("--app-version", default="current")

    verify = subs.add_parser("verify", help="run a regression fixture and print its actual result")
    verify.add_argument("fixture", type=Path)
    verify.add_argument("--app-version")
    verify.add_argument("--probe", help="optional live loopback HTTP(S) listener")
    verify.add_argument("--output", type=Path)

    report = subs.add_parser("report", help="render a standalone evidence viewer")
    report.add_argument("evidence", type=Path)
    report.add_argument("--before", type=Path, help="optional original regression result")
    report.add_argument("--after", type=Path, help="optional fixed regression result")
    report.add_argument(
        "--exploration", type=Path, help="optional four-combination result for this capture"
    )
    report.add_argument("--output", type=Path, required=True)

    serve = subs.add_parser("serve", help="open the generated evidence viewer on loopback")
    serve.add_argument("report", type=Path)
    serve.add_argument("--port", type=int, default=13811)
    return root


def emit(result: JSON, output: Path | None = None) -> None:
    content = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if output is not None:
        write_private(output, content.encode("utf-8"), replace=True)
    print(content, end="")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Store(args.store)
    try:
        if args.command == "list":
            emit(list(store.list_captures(args.operation, args.limit)))
        elif args.command == "doctor":
            if any(not 1024 <= v <= 65535 for v in (args.port, args.viewer_port)):
                raise FlightError("Use ports between 1024 and 65535.")
            outcome = preflight(args.live, args.port, args.viewer_port)
            emit(outcome)
            return 0 if outcome["ready"] else 2
        elif args.command == "share":
            data = report_data(read_json(read_file(args.evidence, "evidence"), "Evidence"))
            emit(sharing_summary(data), args.output)
        elif args.command == "snapshot":
            if args.file:
                raw = read_file(args.file, "environment document")
            else:
                key = os.getenv("FLAGSMITH_SERVER_KEY", "")
                raw = fetch_document(args.url, key)
            snapshot = Snapshot(raw)
            emit({"snapshot_sha256": store.save_snapshot(snapshot)})
        elif args.command == "capture":
            registry = load_registry(args.registry)
            recorder = Recorder(store, registry)
            recorder.snapshots.publish(store.load_snapshot(args.snapshot).document)
            event = Event.parse(
                read_json(read_file(args.event, "operation event"), "Operation event")
            )
            capture = recorder.record(args.operation, event, args.app_version)
            emit(
                {
                    "capture_id": capture.id,
                    "snapshot_sha256": capture.snapshot_sha256,
                    "event_sha256": capture.event_sha256,
                    "reads": capture.reads,
                    "outcome": capture.outcome,
                }
            )
        elif args.command in ("replay", "compare", "explore"):
            options: Object = {}
            if args.command == "explore":
                options = {
                    "axes": read_json(read_file(args.axes, "axes"), "Flag axes"),
                    "candidate": args.candidate_version,
                }
            emit(
                isolated(
                    args.command,
                    {
                        **options,
                        "registry": args.registry,
                        "store": str(store.root),
                        "capture": args.capture,
                        "current": getattr(args, "current", None),
                    },
                    args.probe,
                ),
                args.output,
            )
        elif args.command == "export-pytest":
            test = export_pytest(
                store,
                args.capture,
                load_expected(args.expect),
                args.output,
                args.registry,
                args.app_version,
            )
            emit({"test": str(test), "capture_id": args.capture})
        elif args.command == "verify":
            evidence = run_regression(args.fixture, args.app_version, probe=args.probe)
            emit(evidence, args.output)
            return 0 if evidence["passed"] else 1
        elif args.command == "report":
            checks: Object | None = None
            if args.before or args.after:
                if not args.before or not args.after:
                    raise FlightError("Provide both the original and fixed regression results.")
                checks = {
                    label: object_value(read_json(read_file(path, label), label), label)
                    for label, path in (("before", args.before), ("after", args.after))
                }
            content = read_json(
                read_file(args.evidence, "execution evidence"), "Execution evidence"
            )
            matrix = (
                object_value(
                    read_json(read_file(args.exploration, "exploration"), "Exploration"),
                    "Exploration",
                )
                if args.exploration
                else None
            )
            write_private(args.output, render_report(content, checks, matrix), replace=True)
            emit({"report": str(args.output.resolve())})
        elif args.command == "serve":
            serve_report(args.report, args.port)
    except (FlightError, OSError) as error:
        print(f"Flight Recorder: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
