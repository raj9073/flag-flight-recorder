"""Evaluate the exact self-hosted Flagsmith environment selected for an operation."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from flagsmith import Flagsmith
from flagsmith.models import Flags

from .model import (
    JSON,
    FlightError,
    Object,
    Scalar,
    canonical,
    object_value,
    read_json,
    sha256,
    string_value,
)

if TYPE_CHECKING:
    from .store import Store


class OfflineDocument:
    def __init__(self, data: bytes):
        self._data = data

    def get_environment(self) -> Any:
        return object_value(read_json(self._data, "Flagsmith document"), "Flagsmith document")


@dataclass(frozen=True)
class Snapshot:
    document: bytes = field(repr=False)
    digest: str = field(init=False)
    _client: Flagsmith = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        document = bytes(self.document)
        data = object_value(read_json(document, "Flagsmith document"), "Flagsmith document")
        if not isinstance(data.get("project"), dict) or not isinstance(
            data.get("feature_states"), list
        ):
            raise FlightError(
                "Expected a Flagsmith environment document with project and flag states."
            )
        try:
            client = Flagsmith(offline_mode=True, offline_handler=OfflineDocument(document))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise FlightError(
                f"Flagsmith could not load the environment document: {error}"
            ) from error

        object.__setattr__(self, "document", document)
        object.__setattr__(self, "digest", sha256(document))
        object.__setattr__(self, "_client", client)

    def flags(self, identity: str, traits: dict[str, Scalar]) -> RecordingFlags:
        try:
            values = self._client.get_identity_flags(identity, traits, transient=True)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise FlightError(
                f"Flagsmith could not evaluate the selected document: {error}"
            ) from error
        return RecordingFlags(values)


class RecordingFlags:
    def __init__(self, flags: Flags):
        self._flags = flags
        self._reads: dict[str, Object] = {}

    def enabled(self, name: str) -> bool:
        string_value(name, "Flag name")
        try:
            value = self._flags.is_feature_enabled(name)
        except Exception as error:
            raise FlightError(f"Flagsmith could not read enabled state for {name!r}.") from error
        self._reads.setdefault(name, {})["enabled"] = value
        return value

    def value(self, name: str) -> JSON:
        string_value(name, "Flag name")
        try:
            value = cast(JSON, self._flags.get_feature_value(name))
        except Exception as error:
            raise FlightError(f"Flagsmith could not read value for {name!r}.") from error
        canonical(value)
        self._reads.setdefault(name, {})["value"] = value
        return value

    def reasons(self, reads: Object) -> Object:
        """Reasons supplied by the official evaluator for the flags this operation used."""
        return {
            name: str(reason)
            for name in reads
            if (reason := getattr(self._flags.get_flag(name), "reason", None))
        }

    def resolved(self) -> dict[str, Object]:
        return {
            flag.feature_name: {"enabled": flag.enabled, "value": flag.value}
            for flag in self._flags.all_flags()
        }

    def observations(self) -> Object:
        return {name: dict(read) for name, read in self._reads.items()}


def effective_snapshot(flags: dict[str, Object]) -> Snapshot:
    """A synthetic Flagsmith document for an already-resolved set of effective values."""
    states: list[JSON] = []
    for index, (name, values) in enumerate(sorted(flags.items()), 1):
        states.append(
            {
                "feature": {"id": index, "name": name, "type": "STANDARD"},
                "enabled": values["enabled"],
                "feature_state_value": values["value"],
                "django_id": index,
                "featurestate_uuid": str(UUID(int=index)),
                "feature_segment": None,
                "multivariate_feature_state_values": [],
            }
        )
    return Snapshot(
        canonical(
            {
                "id": 1,
                "name": "Effective flags fixture",
                "api_key": "offline-synthetic-fixture",
                "project": {
                    "id": 1,
                    "name": "Local fixture",
                    "organisation": {"id": 1, "name": "Local"},
                    "segments": [],
                },
                "feature_states": states,
                "identity_overrides": [],
            }
        )
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def sdk_base(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise FlightError("Use a valid HTTP(S) Flagsmith base URL.") from error
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise FlightError("Use an HTTP(S) Flagsmith base URL without credentials or query fields.")
    base = url.rstrip("/")
    return base if base.endswith("/api/v1") else f"{base}/api/v1"


def fetch_document(base_url: str, key: str, timeout: float = 10.0) -> bytes:
    if not key.startswith("ser.") or any(character.isspace() for character in key):
        raise FlightError("A Flagsmith server-side environment key is required.")
    request = urllib.request.Request(
        sdk_base(base_url) + "/environment-document/",
        headers={"X-Environment-Key": key, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as error:
        raise FlightError(f"Flagsmith environment export returned HTTP {error.code}.") from error
    except OSError as error:
        raise FlightError(
            f"Could not connect to the configured Flagsmith instance: {error}"
        ) from error


class SnapshotProvider:
    """Publish validated snapshots while allowing active requests to retain their selection."""

    def __init__(self, store: Store):
        self.store = store
        self._selected: Snapshot | None = None
        self._refresh = Lock()

    def _publish(self, document: bytes) -> Snapshot:
        snapshot = Snapshot(document)
        self.store.save_snapshot(snapshot)
        self._selected = snapshot
        return snapshot

    def publish(self, document: bytes) -> Snapshot:
        with self._refresh:
            return self._publish(document)

    def refresh(self, base_url: str, key: str) -> Snapshot:
        with self._refresh:
            return self._publish(fetch_document(base_url, key))

    def current(self) -> Snapshot:
        selected = self._selected
        if selected is None:
            raise FlightError("Load an environment snapshot before running an operation.")
        return selected
