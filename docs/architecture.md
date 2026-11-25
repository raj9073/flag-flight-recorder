# How Flag Flight Recorder works

## Request lifetime

1. A snapshot refresh fetches an environment document from self-hosted Flagsmith and validates it with the official Python SDK.
2. The recorder stores the exact bytes under their SHA-256 digest and publishes one immutable snapshot reference. Refreshes are serialized. An in-progress operation keeps the reference it selected.
3. The operation receives a flag reader evaluated for the exact captured identity and typed traits. The reader records enabled states and values as the handler uses them.
4. The recorder executes the registered app handler, captures its result or exception, and writes declared fixtures before publishing the capture record. The application-facing integration preserves the original result or exception if writing the capture fails and reports the storage error separately.

The snapshot digest identifies the stored bytes. Compare evaluated flag values when looking at separate exports. Flagsmith-generated metadata can change between exports even when the application sees the same flag values.

## Execution modes

| Mode | Code and inputs | Evidence |
| --- | --- | --- |
| Historical replay | Exact original snapshot, input, registered app version and compatible runtime | Recomputed flag reads and actual result or exception match the stored capture |
| Configuration comparison | Exact original app and input, with one historical and one selected current snapshot | Verified historical reproduction, current outcome and a typed difference of the flag reads |
| Regression verification | Original snapshot and input, with the explicitly selected app version | Observed flags still match the original and the app returns the supplied expected result |

Replay and regression run in a child process in a Linux network namespace. The worker checks that its namespace differs from the controller's and that loopback is its only interface. The demo also probes the live Flagsmith listener from the worker and confirms that the same listener is available from the host.

The application registry is selected by the caller. A capture identifies a registered operation and version. It does not carry executable application code. The replay compares the recorded source fingerprint and evaluator versions with the current runtime. Fixture hashes validate the files before the operation runs. The exported pytest test pins the generated regression specification and works with a local copy of the original environment document.

## Examples

The CSV summary export accepts two synthetic order rows. With streaming enabled, the buggy path sends a customer column that is outside the selected field list. The fixed streaming path projects the selected columns before writing a row. Both versions are runnable, and the historical test asserts the original flag conditions as well as the intended output.

The shipping quote operation receives a JSON rate-table fixture. An express quote in the Mountain Zone reaches a regional table with a standard rate only. The original handler raises a lookup error. The fixed handler returns a structured unavailable result. This example uses the same registration, replay, comparison and export interfaces as the CSV case.

## Evidence and boundaries

The demo changes flags through the management API on its dedicated loopback instance. The recorder itself fetches environment documents and delegates rule evaluation to the SDK. The generated browser report is read-only and self-contained. It shows observed outcomes, exact change results, actual pytest verdicts and isolation evidence. The CLI returns status 0 for successful reproduction or verification, status 1 for a regression assertion failure, and status 2 for capture validation, setup or execution errors.

The supported operation boundary is synchronous Python with JSON-compatible input and declared fixture data. Real capture documents, identities and values can be sensitive. Keep them in the private store. The demo and its public evidence use synthetic data and exclude raw environment documents and account credentials.
