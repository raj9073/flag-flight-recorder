"""Command-line contract, report integrity and live request error handling."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from flag_flight_recorder import Event, FlightError, Recorder
from flag_flight_recorder.cli import main
from flag_flight_recorder.report import render_report, report_data
from flag_flight_recorder.snapshot import Snapshot, fetch_document, sdk_base


def test_command_line_capture_replay_compare_export_and_report(
    tmp_path: Path,
    old: Snapshot,
    current: Snapshot,
    csv_event: Event,
    capsys: pytest.CaptureFixture,
) -> None:
    base = ["--store", str(tmp_path / "store")]
    snapshots = []
    for index, snapshot in enumerate((old, current)):
        source = tmp_path / f"environment-{index}.json"
        source.write_bytes(snapshot.document)
        assert main([*base, "snapshot", "--file", str(source)]) == 0
        snapshots.append(json.loads(capsys.readouterr().out)["snapshot_sha256"])
    event = tmp_path / "event.json"
    event.write_text(json.dumps(csv_event.document()))
    registry = ["--registry", "examples.registry:build_registry"]
    assert (
        main(
            [
                *base,
                "capture",
                "csv-export",
                "--snapshot",
                snapshots[0],
                "--event",
                str(event),
                "--app-version",
                "buggy",
                *registry,
            ]
        )
        == 0
    )
    capture = json.loads(capsys.readouterr().out)
    assert capture["outcome"]["type"] == "ValueError"
    assert main([*base, "replay", capture["capture_id"], *registry]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["historical"]["outcome"] == capture["outcome"]

    comparison = tmp_path / "comparison.json"
    assert (
        main(
            [
                *base,
                "compare",
                capture["capture_id"],
                "--current",
                snapshots[1],
                "--output",
                str(comparison),
                *registry,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["current"]["outcome"]["status"] == "success"
    expected = tmp_path / "expected.csv"
    expected.write_text("order_id,total_inr\nORD-1001,1250.00\nORD-1002,2475.50\n")
    suite = tmp_path / "suite"
    assert (
        main(
            [
                *base,
                "export-pytest",
                capture["capture_id"],
                "--expect",
                str(expected),
                "--output",
                str(suite),
                "--app-version",
                "fixed",
                *registry,
            ]
        )
        == 0
    )
    assert Path(json.loads(capsys.readouterr().out)["test"]).is_file()
    prior, fixed = tmp_path / "before.json", tmp_path / "after.json"
    assert main(["verify", str(suite), "--app-version", "buggy", "--output", str(prior)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False
    assert main(["verify", str(suite), "--output", str(fixed)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    report = tmp_path / "report.html"
    assert (
        main(
            [
                "report",
                str(comparison),
                "--before",
                str(prior),
                "--after",
                str(fixed),
                "--output",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["report"] == str(report)
    assert "Execution trace" in report.read_text()
    assert main(["report", str(comparison), "--before", str(prior), "--output", str(report)]) == 2
    assert "Provide both" in capsys.readouterr().err
    assert main([*base, "replay", "../wrong", *registry]) == 2
    assert "Invalid capture ID" in capsys.readouterr().err


def test_report_escapes_embedded_application_results(lab: Recorder, csv_event: Event) -> None:
    capture = lab.record("csv-export", csv_event, "buggy")
    document = {
        "recorded": {
            "snapshot_sha256": capture.snapshot_sha256,
            "reads": {},
            "outcome": {"status": "success", "result": "</script><script>throw Error(1)</script>"},
        }
    }
    result = {
        **document,
        "historical": document["recorded"],
        "current": document["recorded"],
        "status": "reproduced",
        "capture_id": capture.id,
        "operation": "csv-export",
        "event_sha256": capture.event_sha256,
        "flag_changes": [],
        "isolation": {"isolated": True, "interfaces": ["lo"]},
    }
    html = render_report(result).decode()
    assert "</script><script>throw" not in html
    assert "\\u003c/script\\u003e" in html
    with pytest.raises(FlightError, match="network-isolated"):
        report_data({**result, "isolation": {}})
    with pytest.raises(FlightError, match="historical capture"):
        report_data(result, {"before": {"capture_id": "unrelated"}})


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1",
        "http://test:secret@localhost",
        "http://localhost?test=true",
        "http://[::1",
        "http://localhost:wat",
        "http://localhost:0",
    ],
)
def test_invalid_sdk_urls(url: str) -> None:
    with pytest.raises(FlightError, match="Flagsmith"):
        sdk_base(url)


def test_sdk_export_uses_right_path_and_blocks_redirected_secrets(old: Snapshot) -> None:
    requests: list[tuple[str, str | None]] = []
    routing = {"redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("X-Environment-Key")))
            if routing["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/unexpected")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(old.document)

        def log_message(self, *_args: object) -> None:
            return

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            assert fetch_document(url, "ser.synthetic") == old.document
            routing["redirect"] = True
            with pytest.raises(FlightError, match="HTTP 302"):
                fetch_document(url, "ser.synthetic")
            with pytest.raises(FlightError, match="server-side environment key"):
                fetch_document(url, "not-a-server-key")
        finally:
            server.shutdown()
            thread.join(3)
    assert requests == [("/api/v1/environment-document/", "ser.synthetic")] * 2
