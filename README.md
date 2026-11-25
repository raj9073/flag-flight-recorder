# Flag Flight Recorder

Reproduce a flag-dependent Python failure after the live configuration changes. Flag Flight Recorder binds the rules actually used by an operation to its inputs, then uses the official Flagsmith Python SDK to run that operation again with networking disabled.

The demo uses a real self-hosted Flagsmith instance. A pilot CSV export fails, a live targeting rule changes, and the export starts working. Historical replay brings back the original exception. One generated pytest test then fails on the buggy exporter and passes on the fixed version under the original settings. A separate shipping quote handler exercises the same Python integration.

![Evidence report comparing the original CSV failure, successful live retry, and offline reproduction](docs/preview.png)

## Run the demo

Use Linux with unprivileged user and network namespaces, Python 3.11–3.14, [uv](https://docs.astral.sh/uv/), and Docker or Podman with Compose. The report also opens as a standalone HTML file after generation.

```sh
uv sync --locked
uv run --locked python -m examples.stack start
uv run --locked python -m examples.demo
uv run --locked flight-recorder serve .local/demo/evidence.html
```

Open [the evidence viewer](http://127.0.0.1:13811). Switch between CSV export and shipping quote, then open **Regression test** to see the before-and-after results. **Capture details** shows the snapshot and input digests and the live SDK parity checks. **Download evidence** saves the structured report.

The stack publishes [Flagsmith](http://127.0.0.1:13810) on loopback. The demo generates its local sign-in in `.local/lab/admin.json` and sets up a dedicated synthetic project. Each run restores its starting overrides, performs both live edits, and prints the location of the private captures and exported tests. The synthetic overrides are disabled at the end.

Use `uv run --locked python -m examples.stack stop` to stop the services while retaining the database. When a port is in use, supply the same explicit `--port` and a new `--state` directory to both stack and demo commands.

## Explore a capture

From the private run directory printed by the demo, choose a capture ID and current snapshot digest from the evidence report. Set `FLIGHT_RECORDER_STORE` to that run's `store` directory. The examples are registered by `examples.registry:build_registry`.

```sh
uv run --locked flight-recorder replay CAPTURE_ID --registry examples.registry:build_registry
uv run --locked flight-recorder compare CAPTURE_ID --current CURRENT_SNAPSHOT_SHA \
  --registry examples.registry:build_registry --output .local/comparison.json
uv run --locked flight-recorder export-pytest CAPTURE_ID \
  --registry examples.registry:build_registry \
  --expect examples/csv_export/expected.csv --output .local/my-regression
uv run --locked pytest .local/my-regression/test_incident.py
```

The exported suite includes a snapshot, manifest, expected result and declared fixtures. Its default app version is `current`. For the demo registry, set `FLIGHT_RECORDER_APP_VERSION=buggy` or `fixed` to run the same generated test against either implementation. The expectation is supplied at export time and represents the intended application result.

Use `flight-recorder snapshot --url URL` with `FLAGSMITH_SERVER_KEY` set to a self-hosted server-side SDK key, or pass `--file` with an exported environment document. For scripted captures, run `flight-recorder capture OPERATION --event EVENT.json --snapshot SNAPSHOT_SHA --registry MODULE:FACTORY`. See `flight-recorder --help` for all arguments. Use `flight-recorder report` to render a comparison JSON file and optionally attach original and fixed regression results from `flight-recorder verify`.

## Add an operation

Register a synchronous Python handler and supply JSON-compatible inputs and any external data as explicit fixtures. For replay from the CLI, expose the registry through an importable factory.

```python
import os
from pathlib import Path

from flag_flight_recorder import Event, OperationContext, Recorder, Registry, Store


def render_label(context: OperationContext) -> str:
    return "priority" if context.flags.enabled("priority_label") else "standard"


def build_registry() -> Registry:
    registry = Registry()
    registry.register("render-label", handler=render_label)
    return registry


recorder = Recorder(Store(Path(".local/recorder")), build_registry())
recorder.snapshots.refresh(os.environ["FLAGSMITH_URL"], os.environ["FLAGSMITH_SERVER_KEY"])
result = recorder.run("render-label", Event("customer-1", {"tier": "pilot"}, {}, {}))
```

For this integration, create the boolean flag `priority_label` and set `FLAGSMITH_URL` to your self-hosted instance. The recorder pins an immutable environment document before calling the handler. Historical replay validates the application and evaluator fingerprints and checks that the outcome and evaluated flag reads match the capture. A comparison runs the same code and input under two snapshots. Regression verification intentionally selects the buggy or fixed application version and checks the expected output under the original snapshot.

Keep clocks, randomness and service responses in declared inputs when a handler depends on them. Register supporting application files with the optional `sources` argument to include them in the code fingerprint. Keep raw captures and server-side keys in local storage. The example data and public evidence are synthetic. [Design and verification](docs/architecture.md)

## Verify from source

```sh
uv run --locked ruff check src examples tests
uv run --locked ruff format --check src examples tests
uv run --locked mypy
uv run --locked pytest -q
uv build --no-sources
```

The separate browser check uses Chrome and the running viewer. It verifies desktop and mobile layouts, navigation, keyboard tabs, evidence download, the offline HTML file and browser errors.

```sh
uv run --locked --group browser python tests/browser_check.py \
  --report .local/demo/evidence.html
```

Built for [Toggle Wars Track 03](https://www.wemakedevs.org/hackathons/flagsmith#tracks). The native offline evaluation uses the [Flagsmith server-side SDK](https://docs.flagsmith.com/integrating-with-flagsmith/sdks/server-side#offline-mode).
