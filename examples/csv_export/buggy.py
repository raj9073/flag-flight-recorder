"""The original version of the CSV operation, retained for historical replay."""

from flag_flight_recorder import OperationContext

from .shared import project_row, report_settings, write_csv


def export_report(context: OperationContext) -> str:
    rows, streaming, columns = report_settings(context)
    if streaming:
        # The original streaming path supplied columns outside the selected profile.
        return write_csv(rows, columns)
    return write_csv([project_row(row, columns) for row in rows], columns)
