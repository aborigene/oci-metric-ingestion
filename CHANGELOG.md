# Change Log

## Unreleased

### Fixed in this version:

- Metrics are now sent in batches (one request per up to 500 lines / 900 KB) instead of one request per datapoint, using a single HTTP session
- HTTP status codes are checked. 429, 5xx and timeouts are retried with backoff; if they still fail the function now raises so Connector Hub redelivers the batch. Other 4xx responses are logged with their body
- OAuth tokens are cached and refreshed 60 seconds before they expire (the expiry check was inverted, so a token was requested for every datapoint)
- ISO-8601 timestamps are accepted as well as epoch milliseconds, and an invalid datapoint or metric no longer drops the rest of the batch
- Dimension values are escaped for the MINT line protocol and dimension keys are sanitised
- An unedited `PROXY_URL` placeholder in `func.yaml` is treated as "no proxy" instead of stopping all ingestion, and proxy credentials are no longer written to the logs
- `LOG_LEVEL` and `IMPORT_ALL_METRICS` default to `INFO` and `False` when not set
- Payloads are logged only at `DEBUG` level

### Improved in this version:

- New `COMPARTMENT_ALLOWLIST` / `COMPARTMENT_DENYLIST` settings to ingest only the compartments you want. Off by default
- New `DRY_RUN`, `MAX_PAYLOAD_BYTES`, `MAX_LINES_PER_REQUEST`, `RETRY_BUDGET_SECONDS` and `CURATED_SEND_ALL_BUCKETS` settings
- The function timeout is now 120 seconds
- Added a pytest suite (`pip install -r requirements-dev.txt && pytest`)

---

## Version 2025.08.04 (Aug 4th)

### Fixed in this version:

- Fix a bug where metrics were attempting to be mapped to their Dynatrace counterpart when `IMPORT_ALL_METRICS` was set to True

---

## Version 2025.06.17 (June 17th)

### Improved in this version:

- Added support for routing API calls through a proxy

---

## Version 2025.04.15 (Apr 15th)

### Improved in this version:

- Added support for authenticaton with OAuth2

---
