"""Registry that loads the two independent example integrations."""

from flag_flight_recorder import Registry

from . import csv_export, shipping_quote


def build_registry() -> Registry:
    registry = Registry()
    csv_export.register(registry)
    shipping_quote.register(registry)
    return registry
