"""CSV export with the selected columns applied to streamed rows."""

from flag_flight_recorder import OperationContext

from .shared import project_row, report_settings, write_csv


def export_report(context: OperationContext) -> str:
    rows, streaming, columns = report_settings(context)
    projected = (project_row(row, columns) for row in rows)
    return write_csv(projected if streaming else list(projected), columns)
