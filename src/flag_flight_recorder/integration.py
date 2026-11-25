"""Registry and capture API for developer-owned Python handlers."""

from __future__ import annotations

import inspect
import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .model import (
    JSON,
    SLUG,
    Capture,
    Event,
    FlightError,
    Object,
    canonical,
    checked_id,
    runtime,
    sha256,
)
from .snapshot import RecordingFlags, Snapshot, SnapshotProvider
from .store import Store, read_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationContext:
    identity: str
    inputs: JSON
    fixtures: Object
    flags: RecordingFlags


Handler = Callable[[OperationContext], JSON]


@dataclass(frozen=True)
class Operation:
    name: str
    version: str
    handler: Handler
    sources: tuple[Path, ...]

    def fingerprint(self) -> str:
        digests = [sha256(read_file(source, "application source")) for source in self.sources]
        return sha256(
            canonical(
                {
                    "handler": f"{self.handler.__module__}.{self.handler.__qualname__}",
                    "sources": list(digests),
                }
            )
        )


class Registry:
    """Explicitly register trusted application code by operation and version."""

    def __init__(self) -> None:
        self._operations: dict[tuple[str, str], Operation] = {}

    def register(
        self,
        name: str,
        *,
        handler: Handler,
        version: str = "current",
        sources: Iterable[Path] = (),
    ) -> None:
        name = checked_id(name, SLUG, "operation ID")
        version = checked_id(version, SLUG, "application version")
        key = (name, version)
        if key in self._operations:
            raise FlightError(f"Operation {name!r} version {version!r} is already registered.")
        source = inspect.getsourcefile(handler)
        if not source:
            raise FlightError("Register a Python handler with a readable source file.")
        files = tuple(
            dict.fromkeys([Path(source).resolve(), *(path.resolve() for path in sources)])
        )
        self._operations[key] = Operation(name, version, handler, files)

    def get(self, name: str, version: str) -> Operation:
        try:
            return self._operations[(name, version)]
        except KeyError:
            raise FlightError(
                f"Unknown operation {name!r} at application version {version!r}."
            ) from None


@dataclass(frozen=True)
class Execution:
    reads: Object
    outcome: Object
    exception: Exception | None


def evaluate(operation: Operation, snapshot: Snapshot, event: Event) -> Execution:
    event = event.copy()
    flags = snapshot.flags(event.identity, event.traits)
    context = OperationContext(event.identity, event.inputs, event.fixtures, flags)
    try:
        result = operation.handler(context)
    except FlightError:
        raise
    except Exception as error:
        return Execution(
            flags.observations(),
            {"status": "error", "type": type(error).__name__, "message": str(error)},
            error,
        )
    canonical(result)
    return Execution(flags.observations(), {"status": "success", "result": result}, None)


class Recorder:
    def __init__(self, store: Store, registry: Registry, *, errors_only: bool = False):
        self.errors_only = errors_only
        self.store = store
        self.registry = registry
        self.snapshots = SnapshotProvider(store)

    def _record(self, name: str, event: Event, version: str) -> tuple[Capture, Execution]:
        snapshot = self.snapshots.current()
        operation = self.registry.get(name, version)
        event = event.copy()
        app_sha = operation.fingerprint()
        occurred_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        execution = evaluate(operation, snapshot, event)
        capture = Capture(
            secrets.token_hex(12),
            occurred_at,
            name,
            version,
            app_sha,
            runtime(),
            snapshot.digest,
            event.digest,
            event,
            execution.reads,
            execution.outcome,
        )
        return capture, execution

    def record(self, name: str, event: Event, version: str = "current") -> Capture:
        capture, _ = self._record(name, event, version)
        self.store.save_capture(capture)
        return capture

    def run(
        self,
        name: str,
        event: Event,
        version: str = "current",
        on_capture: Callable[[Capture], None] | None = None,
    ) -> JSON:
        capture, execution = self._record(name, event, version)
        if self.errors_only and execution.exception is None:
            return execution.outcome["result"]
        try:
            self.store.save_capture(capture)
        except FlightError as error:
            logger.error("Operation %s finished, but capture could not be saved: %s", name, error)
        else:
            if on_capture is not None:
                try:
                    on_capture(capture)
                except Exception:
                    logger.exception("Capture callback failed after operation %s finished.", name)

        if execution.exception is not None:
            raise execution.exception
        return execution.outcome["result"]
