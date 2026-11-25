# Record the Flag Flight Recorder demo

Open the interactive CSV incident in a desktop browser. Before recording, run the self-hosted example from the README once. Keep the terminal output and its private run path handy for a live replay. Use the viewer’s **Copy command** with the matching local report and store. The hosted example has different capture IDs. [Interactive incident](https://raj9073.github.io/flag-flight-recorder/) · [Local setup](../README.md#run-with-self-hosted-flagsmith)

| Time | Show | Explain in your own words |
| --- | --- | --- |
| 0:00–0:20 | Select **Code fix**. Point to the failed and passed pytest results, then the streaming flag, on in both runs. | “A customer’s export failed. Even after a flag change made the bug disappear, we could bring it back and prove the code fix.” |
| 0:20–0:50 | Select **Capture**, then **Live retry**. Show the HTTP 500 and 200 receipts for the same input. In Flagsmith, show the Pilot Plan rule for streaming. | The customer recovered when the targeting rule changed. The code was still the buggy version. The summary column profile stayed the same. |
| 0:50–1:20 | Select **Offline replay**. Show the matching CSV error, the loopback-only worker and the original snapshot. Run the copied command in the local terminal. | The official SDK evaluated the saved targeting document. The worker reproduced the historical request without reaching the live server. |
| 1:20–1:55 | Open **Try flag combinations**. Compare on/summary with on/detailed and off/summary. Open the observed flag reads in one cell. | Only one of these four combinations failed for the two test orders. The fixed exporter returned the selected columns in every cell. |
| 1:55–2:25 | Open **Inspect the regression test and code change**. Show the patch and runnable test, then switch to **Shipping quote**. | The export fix selected the right columns. The same recorder also captures a different handler with a regional-rate fixture. |

For the clearest terminal view, show the two pytest runs from the private directory printed by your fresh demo. `FLIGHT_RECORDER_APP_VERSION=buggy` produces a failing assertion with the original exception. `fixed` passes with the same test file.

```sh
export FLIGHT_RECORDER_STORE="$run_dir/store"
FLIGHT_RECORDER_APP_VERSION=buggy uv run --locked pytest -q "$run_dir/csv-export/test/test_incident.py"
FLIGHT_RECORDER_APP_VERSION=fixed uv run --locked pytest -q "$run_dir/csv-export/test/test_incident.py"
```

Set `run_dir` to the printed run directory before running those commands. The Flagsmith sign-in file, environment keys and private environment documents belong off camera.
