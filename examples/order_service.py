"""Loopback application with real order-export and shipping-quote HTTP endpoints."""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flag_flight_recorder import Event, FlightError, Recorder, Store
from flag_flight_recorder.model import Object, canonical, read_json
from flag_flight_recorder.snapshot import fetch_document

from .registry import build_registry

ROUTES = {"csv-export": "/exports", "shipping-quote": "/quotes"}


def create_server(
    store: Store, document: Callable[[], bytes], port: int, version: str
) -> ThreadingHTTPServer:
    registry = build_registry()
    routes = {path: name for name, path in ROUTES.items()}

    class Orders(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            operation = routes.get(self.path)
            if operation is None:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("Use a JSON event of at most one MiB.")
                event = Event.parse(read_json(self.rfile.read(length), "Request body"))
            except (ValueError, FlightError) as error:
                self.send_error(400, str(error))
                return
            try:
                recorder = Recorder(store, registry)
                recorder.snapshots.publish(document())
                capture = recorder.record(operation, event, version)
            except (FlightError, OSError):
                self.send_error(503, "Recording or flag evaluation is unavailable.")
                return
            status = 500 if capture.outcome["status"] == "error" else 200
            content_type = "application/json"
            if status == 200 and operation == "csv-export":
                body = str(capture.outcome["result"]).encode()
                content_type = "text/csv; charset=utf-8"
            else:
                value = capture.outcome if status == 500 else capture.outcome["result"]
                body = canonical(value)
            self.send_response(status)
            self.send_header("X-Capture-ID", capture.id)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Orders)


@contextmanager
def order_service(store: Store, document: Callable[[], bytes]) -> Iterator[str]:
    with create_server(store, document, 0, "buggy") as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def make_request(base: str, operation: str, event: Event, store: Store) -> Object:
    path = ROUTES[operation]
    request = urllib.request.Request(
        base + path, data=canonical(event.document()), headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.status
        capture_id = response.headers.get("X-Capture-ID")
        if not capture_id:
            raise FlightError(f"Order service returned HTTP {status} without a capture receipt.")
        body = response.read()
        payload = (
            body.decode() if response.headers.get_content_type() == "text/csv" else json.loads(body)
        )
    actual = payload if status == 500 else {"status": "success", "result": payload}
    capture = store.load_capture(capture_id)
    if (
        status not in (200, 500)
        or capture.event_sha256 != event.digest
        or canonical(actual) != canonical(capture.outcome)
    ):
        raise FlightError("The HTTP response does not match the captured operation.")
    return {
        "method": "POST",
        "path": path,
        "http_status": status,
        "capture_id": capture.id,
        "occurred_at": capture.occurred_at,
        "event_sha256": event.digest,
        "app_sha256": capture.app_sha256,
        "snapshot_sha256": capture.snapshot_sha256,
        "reads": capture.reads,
        "outcome": actual,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=13812)
    parser.add_argument("--store", type=Path, default=Path(".local/orders"))
    parser.add_argument("--version", choices=("buggy", "fixed"), default="buggy")
    args = parser.parse_args()
    url, key = os.environ.get("FLAGSMITH_URL"), os.environ.get("FLAGSMITH_SERVER_KEY")
    if not url or not key:
        parser.error("Set FLAGSMITH_URL and FLAGSMITH_SERVER_KEY to the demo environment.")
    with create_server(
        Store(args.store), lambda: fetch_document(url, key), args.port, args.version
    ) as server:
        print(f"Order service at http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
