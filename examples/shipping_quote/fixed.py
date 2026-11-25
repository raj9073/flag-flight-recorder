"""Quote handler that reports a missing service explicitly."""

from flag_flight_recorder import OperationContext
from flag_flight_recorder.model import Object

from .shared import quoted, selection


def quote(context: OperationContext) -> Object:
    destination, service, available = selection(context)
    if service not in available:
        return {"status": "unavailable", "destination": destination, "service": service}
    return quoted(destination, service, available[service])
