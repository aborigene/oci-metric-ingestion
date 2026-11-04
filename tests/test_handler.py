import io
import json
import logging

import pytest

import dynatrace_client
import func
from conftest import FakeResponse, FakeSession
from delivery import DeliveryError

CPU = {
    "namespace": "oci_computeagent", "resourceGroup": None, "compartmentId": "ocid1.compartment.oc1..a",
    "name": "CpuUtilization",
    "dimensions": {"resourceId": "ocid1.instance.oc1..i", "region": "eu-frankfurt-1", "resourceDisplayName": "vm"},
    "datapoints": [{"timestamp": 1730000000000, "value": 5.0, "count": 1}],
}
BAD = "not-an-object"  # process_metrics raises AttributeError on it


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com/")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "dt0c01.SECRET")
    monkeypatch.setenv("LOG_LEVEL", "INFO")


@pytest.fixture
def session(monkeypatch):
    s = FakeSession([FakeResponse(202, {"linesOk": 1, "linesInvalid": 0})])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: s)
    monkeypatch.setattr("delivery.time.sleep", lambda s: None)
    return s


def call(payload):
    func.handler(None, io.BytesIO(json.dumps(payload).encode()))


def test_batch_is_sent_in_one_request(env, session):
    call([CPU, dict(CPU, name="DiskIopsRead"), dict(CPU, name="LoadAverage")])
    assert len(session.ingest_calls()) == 1
    assert session.calls[0]["data"].count(b"\n") == 2


def test_one_bad_item_does_not_drop_the_rest(env, session, caplog):
    with caplog.at_level(logging.INFO):
        call([CPU, BAD, dict(CPU, name="LoadAverage")])
    assert session.calls[0]["data"].count(b"\n") == 1  # both good items, in one request
    assert "Skipping metric that could not be processed" in caplog.text
    assert "invalid_items=1 lines=2" in caplog.text


def test_iso_timestamp_payload_is_ingested(env, session):
    item = json.loads(json.dumps(CPU))
    item["datapoints"][0]["timestamp"] = "2024-10-27T03:33:20Z"
    call(item)
    assert session.calls[0]["data"].decode().endswith(" 1729999980000")


def test_transient_failure_raises_so_connector_hub_retries(env, monkeypatch):
    s = FakeSession([FakeResponse(503)])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: s)
    monkeypatch.setattr("delivery.time.sleep", lambda x: None)
    with pytest.raises(DeliveryError):
        call(CPU)


def test_permanent_rejection_does_not_raise(env, monkeypatch):
    s = FakeSession([FakeResponse(400, text="nope")])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: s)
    call(CPU)  # must not raise


def test_unparseable_body_is_dropped_without_raising(env, session):
    func.handler(None, io.BytesIO(b"{not json"))
    assert session.calls == []


def test_dry_run_logs_and_does_not_send(env, session, monkeypatch, caplog):
    monkeypatch.setenv("DRY_RUN", "true")
    with caplog.at_level(logging.INFO):
        call(CPU)
    assert session.calls == []
    assert "DRY_RUN cloud.oci.compute.cpu.util" in caplog.text


def test_dry_run_needs_no_credentials(session, monkeypatch, caplog):
    monkeypatch.setenv("DRY_RUN", "true")
    with caplog.at_level(logging.INFO):
        call(CPU)
    assert "DRY_RUN" in caplog.text


def test_missing_configuration_is_logged_not_raised(session, caplog):
    with caplog.at_level(logging.ERROR):
        call(CPU)
    assert "Cannot send metrics: Missing configuration value" in caplog.text
    assert session.calls == []


def test_log_level_defaults_when_unset(env, session, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL")
    call(CPU)  # used to raise KeyError outside the try block


def test_oauth_mode_end_to_end_reuses_token(session, monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OAUTH_CLIENT_ID", "c")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "s")
    monkeypatch.setenv("OAUTH_ACCOUNT_URN", "u")
    session.script = [FakeResponse(200, {"access_token": "t", "expires_in": 300})] * 1
    original_post = session.post

    def post(url, **kw):
        if "sso" in url:
            session.calls.append({"url": url, **kw})
            return FakeResponse(200, {"access_token": "t", "expires_in": 300})
        session.script = [FakeResponse(202, {"linesOk": 1})]
        return original_post(url, **kw)

    session.post = post
    call(CPU)
    call(CPU)
    assert len([c for c in session.calls if "sso" in c["url"]]) == 1
    assert len(session.ingest_calls()) == 2


# ---------- proxy ----------

@pytest.mark.parametrize("value", [None, "", "<Proxy URL>", "  "])
def test_unset_or_placeholder_proxy_means_no_proxy(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("PROXY_URL", value)
    assert func.create_proxy_connection() is None


def test_proxy_without_scheme_is_ignored(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "proxy.example.com:3128")
    assert func.create_proxy_connection() is None


def test_proxy_with_credentials_is_url_encoded(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://proxy.example.com:3128")
    monkeypatch.setenv("PROXY_USERNAME", "us er")
    monkeypatch.setenv("PROXY_PASSWORD", "p@ss:word")
    assert func.create_proxy_connection() == "http://us%20er:p%40ss%3Aword@proxy.example.com:3128"


def test_placeholder_proxy_credentials_are_ignored(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://proxy.example.com:3128")
    monkeypatch.setenv("PROXY_USERNAME", "<Proxy Username>")
    monkeypatch.setenv("PROXY_PASSWORD", "<Proxy Password>")
    assert func.create_proxy_connection() == "http://proxy.example.com:3128"


def test_proxy_password_never_logged(env, session, monkeypatch, caplog):
    monkeypatch.setenv("PROXY_URL", "http://proxy.example.com:3128")
    monkeypatch.setenv("PROXY_USERNAME", "user")
    monkeypatch.setenv("PROXY_PASSWORD", "hunter2")
    with caplog.at_level(logging.DEBUG):
        call(CPU)
    assert "hunter2" not in caplog.text
    assert session.calls[0]["proxies"] == {"http": "http://user:hunter2@proxy.example.com:3128",
                                           "https": "http://user:hunter2@proxy.example.com:3128"}


def test_import_all_metrics_true_still_works_in_this_pr(env, session, monkeypatch):
    monkeypatch.setenv("IMPORT_ALL_METRICS", "true")
    call(CPU)
    assert b"cloud.oci.computeagent.CpuUtilization" in session.calls[0]["data"]
