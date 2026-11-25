"""Independent shipping quote integration."""

from pathlib import Path

from flag_flight_recorder import Registry

from .buggy import quote as original_quote
from .fixed import quote as fixed_quote


def register(registry: Registry) -> None:
    shared = (Path(__file__).with_name("shared.py"),)
    registry.register("shipping-quote", handler=original_quote, version="buggy", sources=shared)
    registry.register("shipping-quote", handler=fixed_quote, version="fixed", sources=shared)
    registry.register("shipping-quote", handler=fixed_quote, sources=shared)


def build_registry() -> Registry:
    registry = Registry()
    register(registry)
    return registry
