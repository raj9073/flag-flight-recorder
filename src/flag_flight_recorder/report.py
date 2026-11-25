"""Generate a portable evidence viewer and serve it on loopback."""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .model import (
    JSON,
    SHA256,
    FlightError,
    Object,
    canonical,
    checked_id,
    object_value,
    string_value,
    validate_outcome,
)
from .store import read_file


def case_document(comparison: Object, regression: Object | None = None) -> Object:
    operation = string_value(comparison.get("operation"), "Operation")
    return {
        "id": operation,
        "title": operation.replace("-", " ").capitalize(),
        "description": "Historical reproduction and configuration comparison.",
        "comparison": comparison,
        "regression": regression,
        "sdk_checks": [],
    }


def _validate_comparison(case: Object) -> Object:
    comparison = object_value(case.get("comparison"), "Configuration comparison")
    if comparison.get("status") != "reproduced":
        raise FlightError("The report requires a verified historical reproduction.")
    string_value(comparison.get("capture_id"), "Capture ID")
    string_value(comparison.get("operation"), "Operation")
    checked_id(comparison.get("event_sha256"), SHA256, "operation input digest")
    stages = (
        ("recorded", "historical", "current")
        if "recorded" in comparison
        else ("historical", "current")
    )
    for key in stages:
        stage = object_value(comparison.get(key), f"{key.capitalize()} execution")
        checked_id(stage.get("snapshot_sha256"), SHA256, "snapshot digest")
        object_value(stage.get("reads"), "Flag reads")
        validate_outcome(stage.get("outcome"))
    if "recorded" in comparison and canonical(comparison["recorded"]) != canonical(
        comparison["historical"]
    ):
        raise FlightError("The replay report does not match the recorded execution.")

    changes = comparison.get("flag_changes")
    if not isinstance(changes, list):
        raise FlightError("The report requires the evaluated flag change list.")
    for value in changes:
        change = object_value(value, "Flag change")
        string_value(change.get("name"), "Flag name")
        for end in ("before", "after"):
            if change.get(end) is not None:
                object_value(change[end], "Flag observation")
    isolation = object_value(comparison.get("isolation"), "Replay isolation")
    if isolation.get("isolated") is not True or isolation.get("interfaces") != ["lo"]:
        raise FlightError("The report requires evidence of a network-isolated replay.")
    return comparison


def _validate_regression(comparison: Object, value: JSON) -> None:
    if value is None:
        return
    tests = object_value(value, "Regression evidence")
    historical = object_value(comparison["historical"], "Historical execution")
    for label in ("before", "after"):
        run = object_value(tests.get(label), f"{label.capitalize()} regression result")
        if (
            run.get("capture_id") != comparison["capture_id"]
            or run.get("event_sha256") != comparison["event_sha256"]
            or run.get("snapshot_sha256") != historical["snapshot_sha256"]
            or type(run.get("passed")) is not bool
            or type(run.get("flags_match")) is not bool
        ):
            raise FlightError(
                "Regression evidence must belong to the comparison's historical capture."
            )
        validate_outcome(run.get("outcome"))
        string_value(run.get("detail"), "Regression detail")
        string_value(run.get("app_version"), "Regression application version")
    if tests.get("test_sha256") is not None:
        checked_id(tests["test_sha256"], SHA256, "exported test digest")


def report_data(data: JSON, tests: Object | None = None) -> Object:
    raw = object_value(data, "Evidence")
    if "cases" not in raw:
        raw = {
            "schema": 1,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "cases": [case_document(raw, tests)],
        }
    elif tests is not None:
        raise FlightError("Attach individual regression results to a single comparison file.")

    cases = raw.get("cases")
    if (
        type(raw.get("schema")) is not int
        or raw["schema"] != 1
        or not isinstance(cases, list)
        or not cases
    ):
        raise FlightError("Expected version 1 evidence containing one or more comparisons.")
    ids = set()
    for item in cases:
        case = object_value(item, "Incident")
        identity = string_value(case.get("id"), "Incident ID")
        if identity in ids:
            raise FlightError("Incident IDs must be unique in the report.")
        ids.add(identity)
        string_value(case.get("title"), "Incident title")
        _validate_regression(_validate_comparison(case), case.get("regression"))
    return raw


def render_report(data: JSON, tests: Object | None = None) -> bytes:
    raw = report_data(data, tests)
    assets = files("flag_flight_recorder").joinpath("assets")
    template = assets.joinpath("report.html").read_text(encoding="utf-8")
    encoded = (
        canonical(raw)
        .decode()
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return (
        template.replace(
            "/* REPORT_STYLE */", assets.joinpath("report.css").read_text(encoding="utf-8")
        )
        .replace("/* REPORT_SCRIPT */", assets.joinpath("report.js").read_text(encoding="utf-8"))
        .replace("<!-- REPORT_DATA -->", encoded)
        .encode("utf-8")
    )


def serve_report(path: Path, port: int) -> None:
    if not 1024 <= port <= 65535:
        raise FlightError("Choose a report port between 1024 and 65535.")
    read_file(path, "evidence report")

    class ReportHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                html = path.read_bytes()
            except OSError:
                self.send_error(503, "Evidence report is unavailable.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        with ThreadingHTTPServer(("127.0.0.1", port), ReportHandler) as server:
            print(f"Evidence report at http://127.0.0.1:{port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return
    except OSError as error:
        raise FlightError(f"Could not serve the evidence report: {error}") from error
