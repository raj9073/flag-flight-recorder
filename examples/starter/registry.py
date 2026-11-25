"""Importable handlers. Loading this module requires no storage or network access."""

from flag_flight_recorder import OperationContext, Registry


def render_label(context: OperationContext) -> str:
    return "priority" if context.flags.enabled("priority_label") else "standard"


def build_registry() -> Registry:
    registry = Registry()
    registry.register("render-label", handler=render_label)
    return registry
