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

---

# Changes in `feat/compartment-filter`

Builds on `fix/delivery-and-auth`. With neither setting configured nothing changes (the golden tests still pass).

| Change | Motivation | Where |
|---|---|---|
| `COMPARTMENT_ALLOWLIST` / `COMPARTMENT_DENYLIST` (exact OCID match, deny wins) | A customer with very large non-production compartments wants to leave them out of Dynatrace. Filtering runs before any aggregation, so a skipped metric costs almost nothing, and it applies to curated and generic namespaces. | `filtering.py`, `func.py` `handler` |
| One summary log line per invocation with skipped counts per compartment | Makes it visible what is being excluded without a log line per metric. | `filtering.py`, `func.py` |
| Placeholders such as `<Compartment OCIDs>` mean "not set"; values that are not OCIDs are warned about | An unedited `func.yaml` or a typo must not silently drop everything. | `filtering.py` |

Not in this PR: sub-compartment inheritance and tag-based selection. Both need the compartment tree and tags from the OCI Search service, which arrive with the name-enrichment lookup (see the plan in the PR description).

---

# Changes in `feat/generic-path-dimensions`

Builds on `feat/compartment-filter`. The curated path is untouched: the golden tests pass with and without `IMPORT_ALL_METRICS`.

| Change | Motivation | Where |
|---|---|---|
| Routing: mapped namespace always curated; unmapped is generic only with `IMPORT_ALL_METRICS=true`, else skipped and logged once | Before, `IMPORT_ALL_METRICS=true` sent every namespace down the generic path and mapped namespaces lost their curated keys and dimensions. | `func.py` |
| Generic path keeps all OCI dimensions: `resourceId`, `region`, `compartmentId`, `resourceGroup`, `resourceDisplayName` with fixed names, anything else as `oci.<snake_case>`; adds `cloud.provider` and `oci.service` | Before, only compartment and resource group were sent, so several streams in one compartment became one series. A Smartscape node needs `oci.resource_id` and stable names. | `generic.py` |
| Null, empty and `"None"` values omitted; values cut at 255 chars, keys at 99 | `"None"` must never reach Dynatrace. | `generic.py` |
| camelCase dimension names become snake_case | `MintMetric` lower-cases keys, so `availabilityDomain` would have become `oci.availabilitydomain` while the curated path uses `oci.availability_domain`. | `generic.py` |
| Only the `oci_` prefix is stripped from the namespace | `str.replace` removed every occurrence. | `generic.py` |
| Metric-name characters the API rejects become `_` | A metric name with a space or bracket made the whole line invalid. | `generic.py` |
| Warning (once per namespace) when a generic key starts with a curated service name | The extension's topology and pipeline rules match metric keys by prefix, so for example `oci_computecontainerinstance` would be treated as compute. The key format is kept as specified; the warning makes the clash visible. | `generic.py` |

## Generic aggregation: checked, no bug

`min/max/sum/count` per minute is unchanged. `count` is the number of datapoints in that minute, not the OCI `count` field (which is ignored). Every minute of a payload is sent (unlike the curated path, see `CURATED_SEND_ALL_BUCKETS`).

## Behaviour change to be aware of

With `IMPORT_ALL_METRICS=true`, users who relied on mapped namespaces appearing as `cloud.oci.<namespace>.*` (for example `cloud.oci.computeagent.CpuUtilization`) will no longer receive those series; they receive the curated keys instead.

---

# Changes in `feat/name-enrichment`

Builds on `feat/generic-path-dimensions`. Curated output is untouched (golden tests pass); enrichment only touches the generic path.

| Change | Motivation | Where |
|---|---|---|
| `oci.compartment_name` and `oci.resource_display_name` added to generic metrics | A Smartscape node needs human-readable names, and the OCI payload only has OCIDs. | `enrichment.py`, `generic.py` |
| `registry.py`: namespace -> resource type, id/name dimensions, query template; `ENRICHMENT_REGISTRY_JSON` | Adding a namespace should be data, not code. The query is part of the data because its exact syntax could not be verified. | `registry.py` |
| One paginated search per (region, compartment, resource type); everything returned fills the memo | The OCI APIs are throttled per tenancy; N streams in one compartment must cost one call. | `enrichment.py` |
| Names resolved for the whole batch before any line is built | The name must be on the first sighting, not on a later metric. | `func.py` `handler` |
| In-process memo with TTL (+/-20% jitter), negative TTL and a size cap; nothing persisted | A disposable cache avoids a stateful dependency; jitter avoids synchronised refresh. | `NameMemo` |
| Guardrails: call and time caps per invocation, one lookup per group in flight, circuit breaker 60s doubling to 600s, bounded SDK retry | Limits are unpublished and 429s carry no `Retry-After`. | `NameResolver`, `CircuitBreaker`, `bounded_retry_strategy` |
| A lookup failure never raises and falls back to the OCID with a structured warning | Enrichment must never block ingestion. | `Enricher`, `func.py` |
| Failed lookups are also negatively cached (`NAME_CACHE_NEGATIVE_TTL_SECONDS`) | Avoids retrying a permission error on every invocation. | `NameResolver._run_group` |
| An incomplete result (call cap reached mid-pagination) is **not** negatively cached | "Not in a partial result" does not mean "does not exist". | `NameResolver._run_group` |
| The compartment OCID is validated before it is put into a query | The OCID is interpolated into a quoted query string. | `registry.OCID_PATTERN` |
| `ENRICH_NAMES=false` loads nothing, searches nothing | Switch for tenants without the IAM policies. | `get_enricher` |
| Optional Vault secret (`DYNATRACE_SECRET_OCID`), cached, re-read after a 401 or a rejected OAuth secret | Keeps the token out of `func.yaml`. | `vault.py`, `dynatrace_client.py` |
| `func.yaml`: version 0.2.0, memory 512 MB, all settings documented; `oci` added to `requirements.txt` | The SDK needs more memory. | `func.yaml`, `requirements.txt` |

## Verified against the installed SDK (oci 2.190.0)

Client and region handling (`ResourceSearchClient({"region": r}, signer=...)` gives the regional endpoint, including the gov realm), the resource principal signer classes, `ResourceSummary` fields (`identifier`, `display_name`, `compartment_id`, `lifecycle_state`, `defined_tags`, `freeform_tags`), pagination (`response.next_page`), `RetryStrategyBuilder` options and the effective limits it produces, `SecretsClient.get_secret_bundle` and the base64 content path. Tests cover these offline.

## Not verified (could not be checked without a live tenancy or documentation)

- That `stream` is a resource type indexed by Search, and the exact `where compartmentId = '...'` clause; likewise `query compartment resources` (including whether it returns the tenancy root and is complete from any region).
- That the metric's `compartmentId` is the compartment of the resource that emitted it (the lookup assumes so).
- The IAM policy statements and the dynamic group rule in the README.
- Search index freshness (how long a new or renamed resource takes to show up).
- That the OCI SDK installs and runs on the `GENERIC_ARM` shape and the `fnproject/python:3.11` image.

## Trade-offs

- Names are dimensions, so a rename, or a first metric that fell back to the OCID, creates a second series.
- Compartment lookups run per region as specified; compartments are tenancy-wide, so one region would probably do. Optimise once the Search behaviour is confirmed.
