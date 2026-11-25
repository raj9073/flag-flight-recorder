"""Four controlled interventions on effective flag values, isolated from live targeting."""

from __future__ import annotations

from itertools import product

from .integration import Registry, evaluate
from .model import JSON, Capture, FlightError, Object, canonical, object_value, string_value
from .snapshot import Snapshot, effective_snapshot


def parse_axes(value: JSON) -> list[Object]:
    if not isinstance(value, list) or len(value) != 2:
        raise FlightError("Choose exactly two flag axes.")
    axes: list[Object] = []
    for item in value:
        axis = object_value(item, "Flag axis")
        name = string_value(axis.get("flag"), "Flag name")
        field = axis.get("field")
        values = axis.get("values")
        if field not in ("enabled", "value") or not isinstance(values, list) or len(values) != 2:
            raise FlightError("Each axis needs enabled or value and two distinct choices.")
        if canonical(values[0]) == canonical(values[1]) or any(
            isinstance(v, (list, dict)) for v in values
        ):
            raise FlightError("Choose two distinct scalar values for each axis.")
        if field == "enabled" and not all(type(v) is bool for v in values):
            raise FlightError("An enabled axis must use boolean choices.")
        axes.append({"flag": name, "field": field, "values": values})
    if axes[0]["flag"] == axes[1]["flag"]:
        raise FlightError("Choose two different flags.")
    return axes


def explore(
    registry: Registry,
    capture: Capture,
    snapshot: Snapshot,
    axes_value: JSON,
    candidate: str,
    isolation: Object,
) -> Object:
    axes = parse_axes(axes_value)
    baseline = snapshot.flags(capture.event.identity, capture.event.traits).resolved()
    operations = [registry.get(capture.operation, v) for v in (capture.app_version, candidate)]
    original = evaluate(operations[0], effective_snapshot(baseline), capture.event)
    if canonical(original.reads) != canonical(capture.reads) or canonical(
        original.outcome
    ) != canonical(capture.outcome):
        raise FlightError("Resolved flags do not reproduce the captured baseline.")
    choices: list[list[JSON]] = []
    baseline_settings: list[JSON] = []
    for axis in axes:
        name = str(axis["flag"])
        field = str(axis["field"])
        if name not in baseline:
            raise FlightError(f"Flag {name!r} was not present in the captured environment.")
        values = axis["values"]
        assert isinstance(values, list)
        if not any(canonical(baseline[name][field]) == canonical(v) for v in values):
            raise FlightError(f"Include the original {field} setting for {name!r}.")
        choices.append(values)
        baseline_settings.append(baseline[name][field])
    cells: list[JSON] = []
    for settings in product(*choices):
        resolved = {name: dict(fields) for name, fields in baseline.items()}
        for axis, setting in zip(axes, settings, strict=True):
            resolved[str(axis["flag"])][str(axis["field"])] = setting
        scenario = effective_snapshot(resolved)
        runs: list[JSON] = []
        for operation in operations:
            result = evaluate(operation, scenario, capture.event)
            runs.append({"reads": result.reads, "outcome": result.outcome})
        cells.append(
            {
                "settings": list(settings),
                "snapshot_sha256": scenario.digest,
                "original": runs[0],
                "candidate": runs[1],
            }
        )
    return {
        "kind": "effective-flag-combinations",
        "capture_id": capture.id,
        "event_sha256": capture.event_sha256,
        "historical_snapshot": snapshot.digest,
        "original_version": capture.app_version,
        "candidate_version": candidate,
        "original_app_sha256": capture.app_sha256,
        "candidate_app_sha256": operations[1].fingerprint(),
        "axes": list(axes),
        "baseline_settings": baseline_settings,
        "cells": cells,
        "isolation": isolation,
    }
