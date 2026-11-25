"""Fixture-backed shipping rate selection."""

from flag_flight_recorder import OperationContext
from flag_flight_recorder.model import JSON, Object, object_value, string_value

REGIONAL = "use_regional_rates"
EXPRESS = "enable_express_quotes"


def selection(context: OperationContext) -> tuple[str, str, Object]:
    inputs = object_value(context.inputs, "Shipping quote input")
    destination = string_value(inputs.get("destination"), "Shipping destination")
    service = string_value(inputs.get("service"), "Delivery service")
    if service not in ("standard", "express"):
        raise ValueError("Delivery service must be standard or express.")
    regional = context.flags.enabled(REGIONAL)
    express = context.flags.enabled(EXPRESS)
    table = object_value(context.fixtures.get("rates"), "Shipping rate fixture")
    rates = object_value(table.get("regional" if regional else "default"), "Shipping rate profile")
    effective_service = "standard" if service == "express" and not express else service
    available = rates.get(destination, {})
    return destination, effective_service, object_value(available, "Destination rates")


def quoted(destination: str, service: str, amount: JSON) -> Object:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise ValueError("Shipping rates must be nonnegative integer minor units.")
    return {
        "status": "quoted",
        "destination": destination,
        "service": service,
        "amount_minor": amount,
    }
