import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import dynatrace_client  # noqa: E402
import enrichment  # noqa: E402
import func  # noqa: E402
import vault  # noqa: E402
import generic  # noqa: E402

REAL_OCI_SEARCH = enrichment.OciSearcher.search  # kept for the offline SDK smoke test
REAL_READ_SECRET = vault.read_secret


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate every test: no inherited configuration, fresh session and token cache."""
    for name in list(os.environ):
        if name in {
            "IMPORT_ALL_METRICS", "CURATED_SEND_ALL_BUCKETS", "DRY_RUN", "LOG_LEVEL", "DYNATRACE_TENANT",
            "AUTH_METHOD", "DYNATRACE_API_KEY", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_ACCOUNT_URN",
            "PROXY_URL", "PROXY_USERNAME", "PROXY_PASSWORD", "MAX_PAYLOAD_BYTES", "MAX_LINES_PER_REQUEST",
            "RETRY_BUDGET_SECONDS", "COMPARTMENT_ALLOWLIST", "COMPARTMENT_DENYLIST",
            "ENRICH_NAMES", "ENRICHMENT_REGISTRY_JSON", "NAME_CACHE_TTL_SECONDS", "NAME_CACHE_NEGATIVE_TTL_SECONDS",
            "NAME_CACHE_MAX_ENTRIES", "MAX_LOOKUP_CALLS_PER_INVOCATION", "MAX_LOOKUP_SECONDS_PER_INVOCATION",
            "DYNATRACE_SECRET_OCID",
        }:
            monkeypatch.delenv(name)
    # Name enrichment is off unless a test turns it on with a fake searcher; the real one must never run.
    monkeypatch.setenv("ENRICH_NAMES", "false")

    def no_real_oci(*args, **kwargs):
        raise AssertionError("a test reached the real OCI searcher")

    monkeypatch.setattr(enrichment.OciSearcher, "search", no_real_oci)
    monkeypatch.setattr(vault, "read_secret", no_real_oci)
    enrichment.reset_state()
    vault.reset_state()
    dynatrace_client.reset_state()
    generic.reset_state()
    func._logged_namespaces.clear()
    yield
    dynatrace_client.reset_state()
    enrichment.reset_state()


class FakeResponse:
    def __init__(self, status_code=202, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else ("" if body is None else str(body))

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Stands in for requests.Session. `script` is a list of FakeResponse/Exception, consumed in order;
    the last entry repeats. Every call is recorded in `.calls` as (url, data, headers)."""

    def __init__(self, script=None):
        self.script = list(script) if script else [FakeResponse(202)]
        self.calls = []

    def post(self, url, data=None, headers=None, proxies=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}, "proxies": proxies, "timeout": timeout})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def ingest_calls(self):
        return [c for c in self.calls if c["url"].endswith("/api/v2/metrics/ingest")]


@pytest.fixture
def fake_session():
    return FakeSession


def retry_settings(strategy):
    """Read the effective limits out of an OCI SDK retry strategy object."""
    settings = {"backoff": type(strategy).__name__, "max_wait": strategy.max_wait_between_calls_seconds,
                "base": strategy.base_sleep_time_seconds, "factor": strategy.exponent_growth_factor}
    for checker in strategy.checkers.checkers:
        name = type(checker).__name__
        if name == "LimitBasedRetryChecker":
            settings["max_attempts"] = checker.max_attempts
        elif name == "TotalTimeExceededRetryChecker":
            settings["total_seconds"] = checker.time_limit_seconds
        elif name == "TimeoutConnectionAndServiceErrorRetryChecker":
            settings["status_config"] = checker.service_error_retry_config
            settings["any_5xx"] = checker.retry_any_5xx
    return settings
