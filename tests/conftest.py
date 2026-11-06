import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import dynatrace_client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate every test: no inherited configuration, fresh session and token cache."""
    for name in list(os.environ):
        if name in {
            "IMPORT_ALL_METRICS", "CURATED_SEND_ALL_BUCKETS", "DRY_RUN", "LOG_LEVEL", "DYNATRACE_TENANT",
            "AUTH_METHOD", "DYNATRACE_API_KEY", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_ACCOUNT_URN",
            "PROXY_URL", "PROXY_USERNAME", "PROXY_PASSWORD", "MAX_PAYLOAD_BYTES", "MAX_LINES_PER_REQUEST",
            "RETRY_BUDGET_SECONDS", "COMPARTMENT_ALLOWLIST", "COMPARTMENT_DENYLIST",
        }:
            monkeypatch.delenv(name)
    dynatrace_client.reset_state()
    yield
    dynatrace_client.reset_state()


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
