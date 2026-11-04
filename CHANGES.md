# Changes in `fix/delivery-and-auth`

Each change, why it was made, and how it was verified. The curated (mapped) namespaces keep their exact output;
`tests/golden_expected.py` was captured from the code *before* this branch and `tests/test_golden_curated.py`
asserts it on every namespace and metric (110 cases).

| Change | Motivation | Where |
|---|---|---|
| One request per batch (chunked by 500 lines / 900,000 bytes) over one `requests.Session` | Before: one HTTP POST, and one new client, per datapoint. A batch of 100 metrics meant 100+ sequential HTTPS calls inside one function invocation. | `delivery.py`, `dynatrace_client.py` |
| Status codes are checked, transient failures retried, then the invocation raises | Before: every exception was swallowed and the status was never read, so Connector Hub always saw success and a Dynatrace outage silently lost data. | `delivery.py`, `func.py` `handler` |
| Other 4xx: log status and body, continue | Retrying a rejected line cannot succeed and would block later batches. | `delivery.py` |
| Partial acceptance (`linesInvalid`) is counted and logged | A 202 can still carry rejected lines. | `delivery.py` |
| OAuth token cached at module level, refreshed 60s before expiry | Before: a token request per datapoint, and `is_expired()` was inverted. | `dynatrace_client.py` |
| 401 refreshes the token once | A cached token can be revoked before expiry. | `delivery.py` |
| Timestamps accept epoch ms and ISO-8601 | Before: a string raised `TypeError` and the whole batch was dropped. | `aggregation.py` |
| Invalid datapoints are skipped, one bad metric no longer aborts the batch | Same cause. | `aggregation.py`, `func.py` `handler` |
| Escape `"` and `\` in dimension values, sanitise keys, enforce 50 dims / 50,000 chars per line | Before: a quote in a value produced an invalid line. | `mint.py` |
| Placeholder `PROXY_URL` is "no proxy"; proxy URL no longer logged | Before: the shipped placeholder stopped all ingestion; the proxy password was logged at INFO. | `func.py` |
| `LOG_LEVEL` / `IMPORT_ALL_METRICS` default when unset; payloads logged at DEBUG | Before: `KeyError` (outside the try for `LOG_LEVEL`). | `func.py` |
| `DRY_RUN` | Lets users validate the output without credentials. | `func.py` |
| `CURATED_SEND_ALL_BUCKETS` (default `false`) | Before: for curated namespaces only the last minute bucket was sent (the push call is outside the `for` loop). Fixing it changes the number of lines, so it is opt-in. | `func.py` |
| `func.yaml`: version 0.1.0, timeout 120s, new settings documented | Retries need more than the default timeout. | `func.yaml` |

## Known defects deliberately left unchanged (curated output)

- `oci.resource_group="None"` is emitted for a null `resourceGroup` (10 golden cases show it).
- Only the most recent minute of a payload is sent unless `CURATED_SEND_ALL_BUCKETS=true`.
- `oci_filestorage` `MetadataRequestAverageLatency`: the dimension filter matches on any key, so `operation=WriteMetadata` is reported as `metadata.read.latency` (golden case `#1`).
- With `IMPORT_ALL_METRICS=true` every namespace, including mapped ones, still goes through the generic path (fixed in a later PR).
