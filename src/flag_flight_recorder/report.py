"""Generate a portable evidence viewer and serve it on loopback."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from .exploration import parse_axes
from .model import (
    JSON,
    SHA256,
    FlightError,
    Object,
    canonical,
    checked_id,
    object_value,
    sha256,
    string_value,
    validate_outcome,
)
from .regression import generated_test
from .replay import flag_diff
from .store import read_file


def case_document(comparison: Object, regression: Object | None = None) -> Object:
    operation = string_value(comparison.get("operation"), "Operation")
    return {
        "id": operation,
        "occurred_at": comparison.get("occurred_at"),
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
    historical = object_value(comparison["historical"], "Original")
    current = object_value(comparison["current"], "Updated")
    expected_changes = flag_diff(
        object_value(historical["reads"], "Original reads"),
        object_value(current["reads"], "Current reads"),
    )
    if canonical(changes) != canonical(list(expected_changes)):
        raise FlightError("Flag changes disagree with the recorded reads.")
    return comparison


def _validate_regression(comparison: Object, value: JSON, *, legacy: bool) -> None:
    if value is None:
        return
    tests = object_value(value, "Regression evidence")
    historical = object_value(comparison["historical"], "Historical execution")
    runs = []
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
        outcome = validate_outcome(run.get("outcome"))
        string_value(run.get("detail"), "Regression detail")
        string_value(run.get("app_version"), "Regression application version")
        checked_id(run.get("app_sha256"), SHA256, "application digest")
        reads = object_value(run.get("reads"), "Regression reads")
        if run.get("operation") != comparison["operation"] or "expected" not in run:
            raise FlightError("Regression operation or expectation is missing or mismatched.")
        matched = canonical(reads) == canonical(historical["reads"])
        passed = (
            matched
            and outcome["status"] == "success"
            and canonical(outcome["result"]) == canonical(run["expected"])
        )
        if run["flags_match"] is not matched or run["passed"] is not passed:
            raise FlightError("Regression verdict disagrees with its reads or expected output.")
        exit_code = run.get("pytest_exit")
        if exit_code is not None and (
            type(exit_code) is not int or exit_code != (0 if passed else 1)
        ):
            raise FlightError("pytest exit status disagrees with its verification result.")
        runs.append(run)
    before, after = runs
    if canonical(before["expected"]) != canonical(after["expected"]):
        raise FlightError("Regression results use different expectations.")
    if legacy and all("spec_sha256" not in run for run in runs):
        tests["binding"] = "legacy"
        return
    for run in runs:
        checked_id(run.get("spec_sha256"), SHA256, "regression specification digest")
    if before["spec_sha256"] != after["spec_sha256"]:
        raise FlightError("Regression results use different test specifications.")
    if (
        before["app_sha256"] != comparison.get("app_sha256")
        or canonical(before["outcome"]) != canonical(historical["outcome"])
        or not before["flags_match"]
    ):
        raise FlightError("The original regression run does not match the captured application.")
    if before["app_sha256"] == after["app_sha256"] and (
        canonical(before["reads"]) != canonical(after["reads"])
        or canonical(before["outcome"]) != canonical(after["outcome"])
    ):
        raise FlightError("Identical application code produced different regression evidence.")
    tests["binding"] = "verified"
    if tests.get("test_sha256") is not None:
        digest = checked_id(tests["test_sha256"], SHA256, "exported test digest")
        if digest != sha256(generated_test(str(before["spec_sha256"]))):
            raise FlightError("The exported pytest does not pin the verified specification.")


def _validate_requests(comparison: Object, value: JSON) -> None:
    requests = object_value(value, "Live application requests")
    receipts = []
    for key, stage_name in (("original", "recorded"), ("retry", "current")):
        run = object_value(requests.get(key), "HTTP receipt")
        stage = object_value(comparison.get(stage_name), "Corresponding execution")
        if any(run.get(k) != comparison.get(k) for k in ("event_sha256", "app_sha256")) or any(
            canonical(run.get(k)) != canonical(stage.get(k))
            for k in ("snapshot_sha256", "outcome", "reads")
        ):
            raise FlightError("HTTP receipt does not match the application's recorded execution.")
        cid = string_value(run.get("capture_id"), "HTTP capture ID")
        string_value(run.get("occurred_at"), "HTTP capture time")
        if key == "original" and (
            cid != comparison["capture_id"]
            or (
                comparison.get("occurred_at") is not None
                and run["occurred_at"] != comparison["occurred_at"]
            )
        ):
            raise FlightError("The original HTTP receipt identifies another capture.")
        outcome = validate_outcome(run["outcome"])
        if run.get("method") != "POST" or run.get("http_status") != (
            500 if outcome["status"] == "error" else 200
        ):
            raise FlightError("HTTP method or status disagrees with the application receipt.")
        string_value(run.get("path"), "HTTP path")
        receipts.append(run)
    first, retry = receipts
    if first["path"] != retry["path"] or first["capture_id"] == retry["capture_id"]:
        raise FlightError("Expected two distinct requests to the same application endpoint.")


def _validate_exploration(c: Object, value: JSON, regression: JSON) -> None:
    matrix = object_value(value, "Flag exploration")
    history = object_value(c["historical"], "Historical run")
    if (
        matrix.get("kind") != "effective-flag-combinations"
        or matrix.get("capture_id") != c["capture_id"]
        or matrix.get("event_sha256") != c["event_sha256"]
        or matrix.get("historical_snapshot") != history["snapshot_sha256"]
        or matrix.get("original_app_sha256") != c.get("app_sha256")
        or matrix.get("original_version") != c.get("app_version")
    ):
        raise FlightError("Flag exploration belongs to a different capture or application.")
    checked_id(matrix.get("candidate_app_sha256"), SHA256, "candidate application digest")
    for k in ("original_version", "candidate_version"):
        string_value(matrix.get(k), k)
    isolation = object_value(matrix.get("isolation"), "Exploration isolation")
    if isolation.get("isolated") is not True or isolation.get("interfaces") != ["lo"]:
        raise FlightError("Flag exploration needs isolated executions.")
    axes = parse_axes(matrix.get("axes"))
    choices = [axis["values"] for axis in axes]
    assert isinstance(choices[0], list) and isinstance(choices[1], list)
    wanted = {canonical(list(values)) for values in product(choices[0], choices[1])}
    baseline = matrix.get("baseline_settings")
    if not isinstance(baseline, list) or canonical(baseline) not in wanted:
        raise FlightError("Flag exploration must identify the original effective settings.")
    candidate_run = (
        object_value(regression["after"], "Candidate") if isinstance(regression, dict) else None
    )
    if candidate_run and candidate_run["app_version"] == matrix["candidate_version"]:
        if candidate_run["app_sha256"] != matrix["candidate_app_sha256"]:
            raise FlightError("The explored candidate differs from the regression application.")
    else:
        candidate_run = None
    cells = matrix.get("cells")
    if not isinstance(cells, list) or len(cells) != 4:
        raise FlightError("Expected four flag combinations.")
    for item in cells:
        cell = object_value(item, "Combination")
        settings = cell.get("settings")
        if not isinstance(settings, list) or canonical(settings) not in wanted:
            raise FlightError("Unexpected or duplicate flag combination.")
        wanted.remove(canonical(settings))
        checked_id(cell.get("snapshot_sha256"), SHA256, "scenario snapshot")
        for key in ("original", "candidate"):
            run = object_value(cell.get(key), "Exploration run")
            validate_outcome(run.get("outcome"))
            reads = object_value(run.get("reads"), "Observed flags")
            for axis, setting in zip(axes, settings, strict=True):
                field = str(axis["field"])
                read = object_value(reads.get(str(axis["flag"]), {}), "Observed flag")
                if field in read and canonical(read[field]) != canonical(setting):
                    raise FlightError("Observed flag contradicts the selected combination.")
            if canonical(settings) == canonical(baseline):
                reference = history if key == "original" else candidate_run
                if reference and any(
                    canonical(run.get(k)) != canonical(reference[k]) for k in ("outcome", "reads")
                ):
                    raise FlightError(
                        "The original combination disagrees with the historical evidence."
                    )


def sharing_summary(raw: Object) -> Object:
    """A fixed allowlist with no identities, labels, digests, values, output or error text."""
    cases = raw["cases"]
    assert isinstance(cases, list)
    items: list[JSON] = []
    for case_data in cases:
        case = object_value(case_data, "Incident")
        c = object_value(case["comparison"], "Comparison")
        changes = c["flag_changes"]
        assert isinstance(changes, list)
        runs = case.get("regression")
        item: Object = {"replay_matched": True, "changed_flags": len(changes)}
        if isinstance(runs, dict) and runs.get("binding") == "verified":
            for key in ("before", "after"):
                item[key + "_passed"] = object_value(runs[key], "Verification")["passed"]
        items.append(item)
    return {"format": "flight-recorder-summary-v1", "incidents": items}


def report_data(
    data: JSON, tests: Object | None = None, exploration: Object | None = None
) -> Object:
    raw = deepcopy(object_value(data, "Evidence"))
    legacy = "cases" in raw and raw.get("schema") == 1
    if "cases" not in raw:
        raw = {
            "schema": 2,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "cases": [
                {
                    **case_document(raw, deepcopy(tests)),
                    **({"exploration": exploration} if exploration is not None else {}),
                }
            ],
        }
    elif tests is not None or exploration is not None:
        raise FlightError("Attach individual regression results to a single comparison file.")

    cases = raw.get("cases")
    if (
        type(raw.get("schema")) is not int
        or raw["schema"] not in (1, 2)
        or not isinstance(cases, list)
        or not cases
    ):
        raise FlightError("Expected version 1 or 2 evidence containing one or more comparisons.")
    ids = set()
    for item in cases:
        case = object_value(item, "Incident")
        identity = string_value(case.get("id"), "Incident ID")
        if identity in ids:
            raise FlightError("Incident IDs must be unique in the report.")
        ids.add(identity)
        string_value(case.get("title"), "Incident title")
        comparison = _validate_comparison(case)
        _validate_regression(comparison, case.get("regression"), legacy=legacy)
        if "live_requests" in case:
            _validate_requests(comparison, case["live_requests"])
        if "exploration" in case:
            _validate_exploration(comparison, case["exploration"], case.get("regression"))
        if "artifacts" in case:
            artifact = object_value(case["artifacts"], "Portable fixture")
            try:
                archive = base64.b64decode(
                    string_value(artifact.get("zip_base64"), "Fixture contents"), validate=True
                )
            except (ValueError, binascii.Error) as error:
                raise FlightError("Invalid portable fixture encoding.") from error
            if sha256(archive) != artifact.get("zip_sha256"):
                raise FlightError("Portable fixture digest mismatch.")
            string_value(artifact.get("filename"), "Fixture filename")
            string_value(artifact.get("patch"), "Example code change")
            object_value(artifact.get("runtime"), "Fixture runtime")

    raw["share_summary"] = sharing_summary(raw)
    return raw


def render_report(
    data: JSON, tests: Object | None = None, exploration: Object | None = None
) -> bytes:
    raw = report_data(data, tests, exploration)
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
