"""The capture format and strict JSON boundaries used by the recorder."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from typing import TypeAlias, cast

Scalar: TypeAlias = str | int | float | bool | None
JSON: TypeAlias = "Scalar | list[JSON] | dict[str, JSON]"
Object: TypeAlias = dict[str, JSON]
TRAIT_TYPES = (str, int, float, bool, type(None))
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CAPTURE_ID = re.compile(r"[0-9a-f]{24}\Z")


class FlightError(Exception):
    """The recorder could not provide valid evidence for the requested operation."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, JSON]]) -> Object:
    result: Object = {}
    for key, value in pairs:
        if key in result:
            raise FlightError(f"Duplicate JSON field {key!r}.")
        result[key] = value
    return result


def _invalid_number(value: str) -> JSON:
    raise FlightError(f"Unsupported JSON number {value}.")


def read_json(raw: bytes, label: str) -> JSON:
    try:
        return cast(
            JSON,
            json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_number),
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise FlightError(f"{label} is not valid JSON: {error}") from error


def canonical(value: JSON) -> bytes:
    try:
        _validate_json_types(value)
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise FlightError(f"The operation must supply valid JSON data: {error}") from error


def _validate_json_types(value: JSON) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("object keys must be strings")
            _validate_json_types(child)
    elif isinstance(value, list):
        for child in value:
            _validate_json_types(child)
    elif not isinstance(value, TRAIT_TYPES):
        raise TypeError(f"{type(value).__name__} is not a JSON type")


def object_value(value: JSON, label: str) -> Object:
    if not isinstance(value, dict):
        raise FlightError(f"{label} must be a JSON object.")
    return value


def string_value(value: JSON, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FlightError(f"{label} must be a nonempty string.")
    return value


def checked_id(value: JSON, pattern: re.Pattern[str], label: str) -> str:
    result = string_value(value, label)
    if not pattern.fullmatch(result):
        raise FlightError(f"Invalid {label}.")
    return result


def fields(data: Object, required: set[str], optional: set[str], label: str) -> None:
    missing = required - data.keys()
    unexpected = data.keys() - required - optional
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unexpected:
            details.append(f"unknown {', '.join(sorted(unexpected))}")
        raise FlightError(f"Invalid {label}: {'; '.join(details)}.")


def runtime() -> Object:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "flagsmith": version("flagsmith"),
        "engine": version("flagsmith-flag-engine"),
    }


@dataclass(frozen=True)
class Event:
    identity: str
    traits: dict[str, Scalar]
    inputs: JSON
    fixtures: Object

    @classmethod
    def parse(cls, value: JSON) -> Event:
        data = object_value(value, "Operation input")
        fields(data, {"identity", "inputs"}, {"traits", "fixtures"}, "operation input")
        identity = string_value(data["identity"], "Identity")
        candidate_traits = object_value(data.get("traits", {}), "Traits")
        traits: dict[str, Scalar] = {}
        for name, item in candidate_traits.items():
            string_value(name, "Trait name")
            if not isinstance(item, TRAIT_TYPES):
                raise FlightError(f"Trait {name!r} must be a string, number, boolean or null.")
            traits[name] = item
        fixtures = object_value(data.get("fixtures", {}), "Fixtures")
        for name in fixtures:
            checked_id(name, SLUG, "fixture name")
        event = cls(identity, traits, data["inputs"], fixtures)
        canonical(event.document())
        return event

    def document(self) -> Object:
        return {
            "identity": self.identity,
            "traits": dict(self.traits),
            "inputs": self.inputs,
            "fixtures": self.fixtures,
        }

    def copy(self) -> Event:
        return self.parse(read_json(canonical(self.document()), "Operation input"))

    @property
    def digest(self) -> str:
        return sha256(canonical(self.document()))


def validate_outcome(value: JSON) -> Object:
    data = object_value(value, "Outcome")
    status = data.get("status")
    if status == "success":
        fields(data, {"status", "result"}, set(), "outcome")
    elif status == "error":
        fields(data, {"status", "type", "message"}, set(), "outcome")
        string_value(data["type"], "Exception type")
        if not isinstance(data["message"], str):
            raise FlightError("Exception message must be a string.")
    else:
        raise FlightError("Outcome must describe a success or application error.")
    return data


@dataclass(frozen=True)
class Capture:
    id: str
    occurred_at: str
    operation: str
    app_version: str
    app_sha256: str
    evaluator: Object
    snapshot_sha256: str
    event_sha256: str
    event: Event
    reads: Object
    outcome: Object

    def document(self) -> Object:
        event = self.event.document()
        event["fixtures"] = {
            name: sha256(canonical(data)) for name, data in self.event.fixtures.items()
        }
        return {
            "schema": 1,
            "id": self.id,
            "occurred_at": self.occurred_at,
            "operation": self.operation,
            "app_version": self.app_version,
            "app_sha256": self.app_sha256,
            "evaluator": self.evaluator,
            "snapshot_sha256": self.snapshot_sha256,
            "event_sha256": self.event_sha256,
            "event": event,
            "reads": self.reads,
            "outcome": self.outcome,
        }

    @classmethod
    def parse(cls, value: JSON, fixtures: Object) -> Capture:
        data = object_value(value, "Capture")
        fields(
            data,
            {
                "schema",
                "id",
                "occurred_at",
                "operation",
                "app_version",
                "app_sha256",
                "evaluator",
                "snapshot_sha256",
                "event_sha256",
                "event",
                "reads",
                "outcome",
            },
            set(),
            "capture",
        )
        if type(data["schema"]) is not int or data["schema"] != 1:
            raise FlightError("Unsupported capture schema.")
        occurred_at = string_value(data["occurred_at"], "Capture timestamp")
        try:
            if datetime.fromisoformat(occurred_at).tzinfo is None:
                raise ValueError("timestamp requires a timezone")
        except ValueError as error:
            raise FlightError("Capture timestamp must be an ISO date with a timezone.") from error

        event = Event.parse({**object_value(data["event"], "Captured event"), "fixtures": fixtures})
        event_sha = checked_id(data["event_sha256"], SHA256, "operation input digest")
        if event.digest != event_sha:
            raise FlightError("Captured operation input digest mismatch.")
        evaluator = object_value(data["evaluator"], "Evaluator fingerprint")
        fields(evaluator, {"python", "implementation", "flagsmith", "engine"}, set(), "evaluator")
        for key, item in evaluator.items():
            string_value(item, f"Evaluator {key}")

        reads = object_value(data["reads"], "Flag reads")
        for name, read in reads.items():
            string_value(name, "Flag name")
            read = object_value(read, "Flag read")
            fields(read, set(), {"enabled", "value"}, "flag read")
            if not read or ("enabled" in read and type(read["enabled"]) is not bool):
                raise FlightError(f"Invalid flag read for {name!r}.")
        return cls(
            checked_id(data["id"], CAPTURE_ID, "capture ID"),
            occurred_at,
            checked_id(data["operation"], SLUG, "operation ID"),
            checked_id(data["app_version"], SLUG, "application version"),
            checked_id(data["app_sha256"], SHA256, "application digest"),
            evaluator,
            checked_id(data["snapshot_sha256"], SHA256, "snapshot digest"),
            event_sha,
            event,
            reads,
            validate_outcome(data["outcome"]),
        )
