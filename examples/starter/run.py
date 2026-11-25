"""Record one request with a local fixture or a configured Flagsmith environment."""

import argparse
import os
from pathlib import Path

from flag_flight_recorder import Event, Recorder, Store
from flag_flight_recorder.snapshot import effective_snapshot


def main() -> None:
    from .registry import build_registry

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="use FLAGSMITH_URL and FLAGSMITH_SERVER_KEY"
    )
    parser.add_argument("--store", type=Path, default=Path(".local/recorder"))
    args = parser.parse_args()
    recorder = Recorder(Store(args.store), build_registry())
    if args.live:
        url, key = os.getenv("FLAGSMITH_URL"), os.getenv("FLAGSMITH_SERVER_KEY")
        if not url or not key:
            parser.error("--live needs both FLAGSMITH_URL and FLAGSMITH_SERVER_KEY")
        recorder.snapshots.refresh(url, key)
    else:
        fixture = effective_snapshot({"priority_label": {"enabled": True, "value": None}})
        recorder.snapshots.publish(fixture.document)
    capture = recorder.record("render-label", Event("example-customer", {}, {}, {}))
    print(capture.id)


if __name__ == "__main__":
    main()
