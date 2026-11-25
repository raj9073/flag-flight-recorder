# How Flag Flight Recorder works

## What belongs to one request

A snapshot refresh fetches the Flagsmith environment document, validates it with the official SDK and writes the exact bytes under their SHA-256 digest. Refreshes are serialized. An operation retains its selected snapshot even if another request publishes a newer one. The SDK evaluates the captured identity and typed traits against that document. The application-facing flag reader records the enabled states and values the handler actually reads.

The capture contains the handler version, source fingerprint, runtime fingerprint, request input, declared fixture references, observed flags and result or exception. The store writes fixtures before publishing a capture. `Recorder.run()` preserves the application result or exception when persisting a capture fails. With `errors_only=True`, successful calls skip persistence and the capture callback. `Recorder.record()` also records a successful operation when explicitly requested.

## Four questions the tool can answer

| Question | What is held fixed | What establishes the answer |
| --- | --- | --- |
| Can we reproduce the incident? | Original input, fixtures, identity, traits, snapshot, code and evaluator | A fresh SDK evaluation and application invocation match both the recorded flag reads and outcome. |
| What happened after the rule edit? | Same registered application and request | The comparison executes the updated document offline. In the demo, a second real HTTP request separately produces a wire response, a distinct capture ID and matching reads. |
| Does the fix satisfy the expected behavior? | Original request, snapshot, expected output and exported pytest specification | Each run identifies its actual code and specification. The before run must match the captured code and outcome. A fix is shown only when a different code fingerprint turns the same failing expectation green. |
| Which of these flag combinations reproduces the failure? | All other effective flags, input, fixtures and identity | The explorer creates four synthetic documents from two declared axes and executes both original and candidate versions. Selecting a cell shows the two returned values or exceptions and the flags actually read. |

For the explorer, each axis names a flag, a field (`enabled` or `value`) and two distinct choices. Both axes must include the original effective settings. The worker first checks those effective settings reproduce the recorded baseline. The four cells are hypothetical interventions on the resolved values for this identity. Their results establish behavior for these particular inputs. Use ordinary historical replay to evaluate the original targeting rules. [CSV axes](../examples/csv_export/axes.json)

## What gets checked

- The worker runs in a separate Linux network namespace and verifies that only loopback exists. The demo probes the running Flagsmith listener from that worker and checks it remains available from the host.
- Historical replay requires an exact match on CPython implementation and patch version, Flagsmith SDK version, flag engine version and registered source fingerprint. A mismatch reports captured and observed details. Fixture digests are verified before execution.
- Full comparisons show the SDK’s recorded-snapshot evaluation reasons when available. The CSV example checks live API and captured-document parity for a default, a segment, an identity override, and a weighted variant. An SDK reason describes the flag evaluation for that identity.
- Regression verification checks every captured flag read as well as the expected result supplied by the developer. The generated pytest pins the digest of the specification, which pins the capture and expectation. Changing the expected output or mixing two test specifications invalidates a paired report. Archived v0.1 reports label their pair of test identities as unverified.
- The demo attaches receipts written by the worker inside the actual pytest runs, plus each process exit code. Report validation cross-checks those results, the two HTTP responses and the combination baseline. Digests support consistency checks. They do not authenticate the author of an arbitrary evidence file.

## What the application supplies

Register synchronous Python operations with JSON-compatible inputs and outputs. A capture identifies a handler in a registry module installed on the replay machine. The registry factory must be safe to import offline. Refresh a live document during application startup. Register influencing helper files with the optional `sources` argument. Pin additional dependencies in your own lockfile. If time, randomness or an external service affects the handler, pass its observed value as an explicit input or fixture. Replays execute the registered code, so use a trusted registry.

The examples use two order rows for CSV export and a JSON shipping rate table for a quote. CSV streaming with the summary columns exposed an extra customer field. Regional express quoting reached a rate table with only a standard service. Both fixes are exercised under their original feature flags. [CSV handler](../examples/csv_export/fixed.py) · [Shipping handler](../examples/shipping_quote/fixed.py)

## Private evidence and sharing

Keep a live capture store, environment documents and generic regression exports private. The full evidence viewer can include identities, traits, returned application data, exception text and flag names. The anonymous summary uses a fixed list of fields: the number of changed flags, whether replay matched, and linked regression verdicts when present. It omits identifiers and application data.

The published example uses synthetic order data. Its downloadable pytest archives contain separate synthetic snapshots with just the effective example flags. Each archive is verified on the original and fixed code before release. Its capture, snapshot and specification have their own digests. The original self-hosted environment stays in the private lab.

The CLI exits 0 for a reproduced incident or passing verification, 1 for a failed regression assertion, and 2 for a setup, validation or execution error. `flight-recorder doctor --live` checks local namespace support, Compose and port availability.
