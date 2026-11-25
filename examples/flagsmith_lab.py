"""Seed and edit the synthetic local demo's Flagsmith environment."""

from __future__ import annotations

import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

from flagsmith import Flagsmith

from flag_flight_recorder import FlightError
from flag_flight_recorder.model import (
    JSON,
    Event,
    Object,
    canonical,
    object_value,
    read_json,
    string_value,
)
from flag_flight_recorder.snapshot import (
    RecordingFlags,
    Snapshot,
    _NoRedirect,
    fetch_document,
    sdk_base,
)
from flag_flight_recorder.store import read_file, write_private

from .csv_export.shared import PROFILE, STREAM
from .shipping_quote.shared import EXPRESS, REGIONAL
from .stack import Stack

ORG = "Flag Flight Recorder Lab"
PROJECT = "Flag Flight Recorder"
ENVIRONMENT = "Replay Demonstration"
PILOT = "Pilot Plan"
MOUNTAIN = "Mountain Zone"
CONTROL = "synthetic-pilot-support-control"


class LocalAPI:
    def __init__(self, stack: Stack):
        self.stack = stack
        self.url = sdk_base(stack.url)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.token: str | None = None
        self.secrets: set[str] = set()

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[private]")
        return text

    def request(self, method: str, path: str, payload: JSON = None) -> tuple[int, JSON]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        url = self.stack.url + path if path == "/version/" else self.url + path
        request = urllib.request.Request(
            url, None if payload is None else canonical(payload), headers, method=method
        )
        try:
            with self.opener.open(request, timeout=12) as response:
                return response.status, read_json(response.read(), "Flagsmith response")
        except urllib.error.HTTPError as error:
            detail = self._redact(error.read().decode(errors="replace"))[:450]
            raise FlightError(
                f"Flagsmith {method} {self._redact(path)} returned HTTP {error.code}: {detail}"
            ) from None
        except OSError as error:
            raise FlightError(
                f"Could not reach the local Flagsmith management API: {error}"
            ) from error

    def object(self, method: str, path: str, payload: JSON = None) -> Object:
        return object_value(self.request(method, path, payload)[1], "Flagsmith response")

    def items(self, path: str, **filters: str | int) -> list[Object]:
        result: list[Object] = []
        for page in range(1, 101):
            parameters = urlencode({**filters, "page_size": 100, "page": page})
            _, response = self.request("GET", f"{path}?{parameters}")
            if isinstance(response, list):
                return [object_value(item, "Flagsmith row") for item in response]
            listing = object_value(response, "Flagsmith listing")
            rows = listing.get("results")
            if not isinstance(rows, list):
                raise FlightError("Flagsmith returned an invalid resource list.")
            result.extend(object_value(item, "Flagsmith row") for item in rows)
            if not listing.get("next"):
                return result
        raise FlightError("The demo expected a small, dedicated Flagsmith environment.")

    def ensure(
        self, path: str, payload: Object, field: str = "name", **filters: str | int
    ) -> Object:
        matches = [row for row in self.items(path, **filters) if row.get(field) == payload[field]]
        if len(matches) > 1:
            raise FlightError(f"Duplicate demo resources for {payload[field]!r}.")
        return matches[0] if matches else self.object("POST", path, payload)

    def authenticate(self) -> str:
        info = self.object("GET", "/version/")
        if info.get("is_saas") is not False:
            raise FlightError("The demo requires self-hosted Flagsmith.")
        has_users = object_value(info.get("self_hosted_data"), "Self-hosted details").get(
            "has_users"
        )
        account = self.stack.state / "admin.json"
        if not account.is_file():
            if has_users:
                raise FlightError(
                    "This server already has users. Use its matching local lab directory."
                )
            write_private(
                account,
                canonical(
                    {
                        "email": "flight-recorder@example.com",
                        "password": secrets.token_urlsafe(27),
                    }
                ),
            )
        credentials = object_value(
            read_json(read_file(account, "local demo account"), "Demo account"), "Demo account"
        )
        password = string_value(credentials.get("password"), "Local demo password")
        self.secrets.add(password)
        if has_users:
            response = self.object("POST", "/auth/login/", credentials)
        else:
            response = self.object(
                "POST",
                "/auth/users/",
                {
                    **credentials,
                    "re_password": password,
                    "first_name": "Flight Recorder",
                    "last_name": "Local Lab",
                    "superuser": True,
                },
            )
        self.token = string_value(response.get("key"), "Local auth token")
        self.secrets.add(self.token)
        return string_value(info.get("image_tag"), "Flagsmith release")


def resource_id(row: Object, label: str) -> int:
    value = row.get("id")
    if type(value) is not int:
        raise FlightError(f"Flagsmith did not return a numeric {label} ID.")
    return value


def resource(items: list[Object], field: str, target: JSON, label: str) -> Object | None:
    matches = [row for row in items if row.get(field) == target]
    if len(matches) > 1:
        raise FlightError(f"Flagsmith returned multiple rows for {label}.")
    return matches[0] if matches else None


@dataclass(frozen=True)
class FlagLab:
    api: LocalAPI
    version: str
    sdk_key: str
    environment_id: int
    environment_path: str
    feature_ids: dict[str, int]
    segment_ids: dict[str, int]

    def snapshot(self) -> Snapshot:
        return Snapshot(fetch_document(self.api.stack.url, self.sdk_key))

    def remote(self, event: Event) -> RecordingFlags:
        client = Flagsmith(
            environment_key=self.sdk_key, api_url=self.api.url + "/", request_timeout_seconds=6
        )
        return RecordingFlags(
            client.get_identity_flags(event.identity, event.traits, transient=True)
        )

    def binding(self, segment: str, flag: str) -> Object | None:
        return resource(
            self.api.items(
                "/features/feature-segments/",
                environment=self.environment_id,
                feature=self.feature_ids[flag],
            ),
            "segment",
            self.segment_ids[segment],
            f"{segment} / {flag} binding",
        )

    def state(self, segment: str, flag: str) -> Object:
        binding = self.binding(segment, flag)
        if binding is None:
            raise FlightError(f"Missing override for {segment} / {flag}.")
        items = self.api.items(
            "/features/featurestates/",
            environment=self.environment_id,
            feature=self.feature_ids[flag],
            feature_segment=resource_id(binding, "binding"),
        )
        if len(items) != 1:
            raise FlightError(f"Expected one feature state for {segment} / {flag}.")
        return items[0]

    def ensure_override(self, segment: str, flag: str) -> None:
        if self.binding(segment, flag) is None:
            self.api.object(
                "POST",
                f"{self.environment_path}features/{self.feature_ids[flag]}/create-segment-override/",
                {
                    "feature_segment": {"segment": self.segment_ids[segment]},
                    "enabled": True,
                    "feature_state_value": {"type": "unicode", "string_value": segment},
                },
            )
        self.set_override(segment, flag, True)

    def set_override(self, segment: str, flag: str, enabled: bool) -> Object:
        original = self.state(segment, flag)
        before = original.get("enabled")
        if type(before) is not bool:
            raise FlightError("Flagsmith did not return an enabled state.")
        response: Object = {
            "flag": flag,
            "segment": segment,
            "before": before,
            "after": enabled,
            "method": None,
            "http_status": None,
        }
        if before is enabled:
            return response
        status, updated = self.api.request(
            "PATCH",
            f"/features/featurestates/{resource_id(original, 'feature state')}/",
            {"environment": self.environment_id, "enabled": enabled},
        )
        if object_value(updated, "Flag change").get("enabled") is not enabled:
            raise FlightError("Flagsmith did not acknowledge the requested flag change.")
        if self.state(segment, flag).get("enabled") is not enabled:
            raise FlightError("Flagsmith did not persist the requested flag change.")
        response.update({"method": "PATCH", "http_status": status})
        return response


def provision(stack: Stack) -> FlagLab:
    api = LocalAPI(stack)
    server_version = api.authenticate()
    org = resource_id(api.ensure("/organisations/", {"name": ORG}), "organisation")
    project = resource_id(
        api.ensure(
            "/projects/",
            {"name": PROJECT, "organisation": org},
            organisation=org,
        ),
        "project",
    )
    environment = api.ensure(
        "/environments/",
        {"name": ENVIRONMENT, "project": project, "use_identity_overrides_in_local_eval": True},
        project=project,
    )
    client_key = string_value(environment.get("api_key"), "Flagsmith environment key")
    api.secrets.add(client_key)
    environment_path = f"/environments/{client_key}/"
    if environment.get("use_identity_overrides_in_local_eval") is not True:
        api.object("PATCH", environment_path, {"use_identity_overrides_in_local_eval": True})

    feature_ids: dict[str, int] = {}
    feature_path = f"/projects/{project}/features/"
    for name in (STREAM, REGIONAL, EXPRESS):
        feature_ids[name] = resource_id(
            api.ensure(
                feature_path,
                {
                    "name": name,
                    "type": "STANDARD",
                    "default_enabled": False,
                },
            ),
            "feature",
        )
    feature_ids[PROFILE] = resource_id(
        api.ensure(
            feature_path,
            {
                "name": PROFILE,
                "type": "MULTIVARIATE",
                "default_enabled": True,
                "initial_value": "detailed",
                "multivariate_options": [
                    {"type": "unicode", "string_value": item, "default_percentage_allocation": 50}
                    for item in ("summary", "detailed")
                ],
            },
        ),
        "feature",
    )

    segment_ids = {}
    for name, prop, value in ((PILOT, "plan", "pilot"), (MOUNTAIN, "zone", "mountain")):
        segment_ids[name] = resource_id(
            api.ensure(
                f"/projects/{project}/segments/",
                {
                    "name": name,
                    "project": project,
                    "rules": [
                        {
                            "type": "ALL",
                            "conditions": [{"operator": "EQUAL", "property": prop, "value": value}],
                        }
                    ],
                },
            ),
            "segment",
        )

    key = api.ensure(environment_path + "api-keys/", {"name": "Recorder SDK", "active": True})
    sdk_key = string_value(key.get("key"), "Flagsmith server-side key")
    api.secrets.add(sdk_key)
    lab = FlagLab(
        api,
        server_version,
        sdk_key,
        resource_id(environment, "environment"),
        environment_path,
        feature_ids,
        segment_ids,
    )
    for segment, flag in ((PILOT, STREAM), (MOUNTAIN, REGIONAL), (MOUNTAIN, EXPRESS)):
        lab.ensure_override(segment, flag)

    identities = environment_path + "identities/"
    control = resource_id(
        api.ensure(
            identities,
            {"identifier": CONTROL},
            field="identifier",
            q=CONTROL,
        ),
        "identity",
    )
    controls = f"{identities}{control}/featurestates/"
    if resource(api.items(controls), "feature", feature_ids[STREAM], "support override") is None:
        api.object(
            "POST",
            controls,
            {
                "feature": feature_ids[STREAM],
                "enabled": False,
                "feature_state_value": "support-control",
            },
        )
    return lab
