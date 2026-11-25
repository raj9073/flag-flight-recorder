"""CSV export integration using the Flagsmith pilot rollout."""

from pathlib import Path

from flag_flight_recorder import Registry

from .buggy import export_report as original_report
from .fixed import export_report as fixed_report


def register(registry: Registry) -> None:
    shared = (Path(__file__).with_name("shared.py"),)
    registry.register("csv-export", handler=original_report, version="buggy", sources=shared)
    registry.register("csv-export", handler=fixed_report, version="fixed", sources=shared)
    registry.register("csv-export", handler=fixed_report, sources=shared)


def build_registry() -> Registry:
    registry = Registry()
    register(registry)
    return registry
