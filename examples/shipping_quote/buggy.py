"""Original quote handler with a sparse rate-table lookup."""

from flag_flight_recorder import OperationContext
from flag_flight_recorder.model import Object

from .shared import quoted, selection


def quote(context: OperationContext) -> Object:
    destination, service, available = selection(context)
    return quoted(destination, service, available[service])
