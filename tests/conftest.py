"""Small valid Flagsmith documents for local tests. No live account data is used."""

from pathlib import Path
from uuid import UUID

import pytest

from examples.registry import build_registry
from flag_flight_recorder import Event, Recorder, Store
from flag_flight_recorder.model import Object, canonical, read_json
from flag_flight_recorder.snapshot import Snapshot

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def environment(stream: bool = True, express: bool = True, *, profile: str = "summary") -> Snapshot:
    values = [
        ("stream_report_rows", stream, None),
        ("export_column_profile", True, profile),
        ("use_regional_rates", True, None),
        ("enable_express_quotes", express, None),
    ]
    states: list[Object] = []
    for index, (name, enabled, value) in enumerate(values, 1):
        states.append(
            {
                "feature": {"id": index, "name": name, "type": "STANDARD"},
                "enabled": enabled,
                "django_id": index,
                "feature_segment": None,
                "featurestate_uuid": str(UUID(int=index)),
                "feature_state_value": value,
                "multivariate_feature_state_values": [],
            }
        )
    return Snapshot(
        canonical(
            {
                "id": 1,
                "api_key": "synthetic-fixture-environment",
                "name": "Recorder fixture",
                "project": {
                    "id": 2,
                    "name": "Synthetic tests",
                    "organisation": {"id": 3, "name": "Synthetic organisation"},
                    "segments": [],
                },
                "feature_states": list(states),
                "identity_overrides": [],
            }
        )
    )


@pytest.fixture
def old() -> Snapshot:
    return environment()


@pytest.fixture
def current() -> Snapshot:
    return environment(stream=False)


@pytest.fixture
def csv_event() -> Event:
    return Event.parse(read_json((EXAMPLES / "csv_export/event.json").read_bytes(), "CSV sample"))


@pytest.fixture
def quote_event() -> Event:
    return Event.parse(
        read_json((EXAMPLES / "shipping_quote/event.json").read_bytes(), "Quote sample")
    )


@pytest.fixture
def lab(tmp_path: Path, old: Snapshot) -> Recorder:
    recorder = Recorder(Store(tmp_path / "store"), build_registry())
    recorder.snapshots.publish(old.document)
    return recorder
