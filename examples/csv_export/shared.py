"""Inputs and rendering used by both versions of the CSV exporter."""

import csv
import io
from collections.abc import Iterable

from flag_flight_recorder import OperationContext
from flag_flight_recorder.model import Object, object_value

STREAM = "stream_report_rows"
PROFILE = "export_column_profile"
COLUMNS = {
    "summary": ("order_id", "total_inr"),
    "detailed": ("order_id", "customer", "total_inr"),
}


def report_settings(context: OperationContext) -> tuple[list[Object], bool, tuple[str, ...]]:
    if not context.flags.enabled(PROFILE):
        raise ValueError("CSV column profile is disabled.")
    profile = context.flags.value(PROFILE)
    if not isinstance(profile, str) or profile not in COLUMNS:
        raise ValueError("Invalid CSV column profile.")
    streaming = context.flags.enabled(STREAM)
    rows = object_value(context.inputs, "CSV input").get("rows")
    if not isinstance(rows, list):
        raise ValueError("CSV input requires a rows list.")
    return [object_value(row, "CSV row") for row in rows], streaming, COLUMNS[profile]


def project_row(row: Object, columns: tuple[str, ...]) -> Object:
    return {column: row[column] for column in columns}


def write_csv(rows: Iterable[Object], columns: tuple[str, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
