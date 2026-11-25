"""Private, content-addressed storage with atomic publication."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .model import (
    CAPTURE_ID,
    SHA256,
    SLUG,
    Capture,
    FlightError,
    Object,
    canonical,
    checked_id,
    object_value,
    read_json,
    sha256,
)

if TYPE_CHECKING:
    from .snapshot import Snapshot


def read_file(path: Path, description: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise FlightError(f"Could not read {description} at {path}: {error.strerror}.") from error


def write_private(path: Path, payload: bytes, *, replace: bool = False) -> None:
    """Expose a fully written file, preserving an existing content-addressed entry."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as handle:
            temp = Path(handle.name)
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                if replace:
                    os.replace(temp, path)
                else:
                    try:
                        os.link(temp, path)
                    except FileExistsError:
                        if read_file(path, "existing record") != payload:
                            raise FlightError(
                                f"An existing record at {path} has different contents."
                            ) from None
            finally:
                temp.unlink(missing_ok=True)
    except OSError as error:
        raise FlightError(f"Could not write record at {path}: {error.strerror}.") from error


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save_snapshot(self, snapshot: Snapshot) -> str:
        write_private(self.root / "snapshots" / f"{snapshot.digest}.json", snapshot.document)
        return snapshot.digest

    def load_snapshot(self, digest: str) -> Snapshot:
        from .snapshot import Snapshot

        digest = checked_id(digest, SHA256, "snapshot digest")
        document = read_file(self.root / "snapshots" / f"{digest}.json", "environment snapshot")
        if sha256(document) != digest:
            raise FlightError("Environment snapshot digest mismatch.")
        return Snapshot(document)

    def save_capture(self, capture: Capture) -> None:
        capture_id = checked_id(capture.id, CAPTURE_ID, "capture ID")
        for value in capture.event.fixtures.values():
            data = canonical(value)
            write_private(self.root / "fixtures" / f"{sha256(data)}.json", data)
        write_private(self.root / "captures" / f"{capture_id}.json", canonical(capture.document()))

    def list_captures(self, operation: str | None, limit: int) -> list[Object]:
        if not 1 <= limit <= 1000:
            raise FlightError("Choose a capture limit between 1 and 1000.")
        rows = []
        for path in (self.root / "captures").glob("*.json"):
            if not CAPTURE_ID.fullmatch(path.stem):
                continue
            raw = object_value(read_json(read_file(path, "capture"), "Capture"), "Capture")
            if raw.get("id") != path.stem:
                raise FlightError("Capture ID does not match the stored record.")
            if operation is None or raw.get("operation") == operation:
                row = {
                    k: raw.get(k)
                    for k in ("id", "occurred_at", "operation", "app_version", "snapshot_sha256")
                }
                row["status"] = object_value(raw.get("outcome"), "Outcome").get("status")
                rows.append(row)
        return sorted(
            rows, key=lambda row: (str(row["occurred_at"]), str(row["id"])), reverse=True
        )[:limit]

    def load_capture(self, capture_id: str) -> Capture:
        capture_id = checked_id(capture_id, CAPTURE_ID, "capture ID")
        raw = read_file(self.root / "captures" / f"{capture_id}.json", "capture")
        data = object_value(read_json(raw, "Capture"), "Capture")
        if data.get("id") != capture_id:
            raise FlightError("Capture ID does not match the stored record.")
        return decode_capture(data, self.root / "fixtures")


def decode_capture(data: Object, fixture_dir: Path) -> Capture:
    event = object_value(data.get("event"), "Captured event")
    references = object_value(event.get("fixtures"), "Captured fixture references")
    fixtures: Object = {}
    for name, digest_value in references.items():
        checked_id(name, SLUG, "fixture name")
        digest = checked_id(digest_value, SHA256, "fixture digest")
        raw = read_file(fixture_dir / f"{digest}.json", f"fixture {name!r}")
        if sha256(raw) != digest:
            raise FlightError(f"Fixture digest mismatch for {name!r}.")
        fixtures[name] = read_json(raw, f"Fixture {name!r}")
    return Capture.parse(data, fixtures)
