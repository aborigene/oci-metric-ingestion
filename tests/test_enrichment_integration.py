"""End to end: handler -> enrichment -> MINT lines -> delivery, with a fake OCI Search and a fake HTTP session."""
import io
import json
import logging

import pytest

import dynatrace_client
import enrichment
import func
from conftest import REAL_OCI_SEARCH, FakeResponse, FakeSession, retry_settings
from enrichment import Enricher, EnrichmentConfig, NameResolver
from registry import COMPARTMENT_QUERY, load_registry
from test_enrichment import COMP_A, REGION, S1, S2, FakeSearcher, Item, q_streams, streaming

SEARCH_DATA = {
    (REGION, q_streams(COMP_A)): [[Item(S1, "orders-stream"), Item(S2, "payments-stream")]],
    (REGION, COMPARTMENT_QUERY): [[Item(COMP_A, "Prod-Streaming")]],
}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "dt0c01.SECRET")
    monkeypatch.setenv("IMPORT_ALL_METRICS", "true")
    session = FakeSession([FakeResponse(202, {"linesOk": 2, "linesInvalid": 0})])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: session)

    def install(searcher, **config):
        resolver = NameResolver(EnrichmentConfig(**config), searcher)
        enrichment._enricher, enrichment._initialised = Enricher(load_registry(), resolver), True
        return resolver

    return session, install


def call(*bodies):
    func.handler(None, io.BytesIO(json.dumps(list(bodies)).encode()))


def lines_sent(session):
    return [l for c in session.ingest_calls() for l in c["data"].decode().split("\n")]


def parse(line):
    head = line.split(" gauge,")[0]
    _, _, rest = head.partition(",")
    return {p.partition('="')[0]: p.partition('="')[2].rstrip('"') for p in rest.split('",')}


def test_two_streams_same_compartment_two_series_with_names(setup):
    session, install = setup
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    call(streaming(S1), streaming(S2))

    lines = lines_sent(session)
    assert len(lines) == 2 and len(set(l.split(" gauge,")[0] for l in lines)) == 2
    d = [parse(l) for l in lines]
    assert [x["oci.resource_id"] for x in d] == [S1, S2]
    assert [x["oci.resource_display_name"] for x in d] == ["orders-stream", "payments-stream"]
    assert all(x["oci.compartment_name"] == "Prod-Streaming" for x in d)
    assert all(x["oci.region"] == REGION and x["oci.compartment_id"] == COMP_A for x in d)
    assert all(x["cloud.provider"] == "oci" and x["oci.service"] == "streaming" for x in d)
    assert not any("None" in l for l in lines)
    # ONE stream search and ONE compartment search for the whole batch
    assert len(searcher.calls) == 2


def test_names_are_resolved_before_the_first_request_is_sent(setup):
    session, install = setup
    order = []
    searcher = FakeSearcher(SEARCH_DATA)
    original = searcher.search
    searcher.search = lambda *a, **k: (order.append("search"), original(*a, **k))[1]
    install(searcher)
    original_post = session.post
    session.post = lambda *a, **k: (order.append("send"), original_post(*a, **k))[1]
    call(streaming(S1))
    assert order == ["search", "search", "send"]
    assert "orders-stream" in lines_sent(session)[0]  # present on the very first sighting


def test_second_invocation_is_served_from_the_memo(setup):
    session, install = setup
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    call(streaming(S1))
    n = len(searcher.calls)
    call(streaming(S2))  # S2 was filled by the first search although it was never requested
    assert len(searcher.calls) == n
    assert "payments-stream" in lines_sent(session)[1]


def test_lookup_failure_falls_back_to_the_ocid_and_still_sends(setup, caplog):
    session, install = setup
    install(FakeSearcher(side_effects=[Exception("HTTP 500")] * 5))
    with caplog.at_level(logging.WARNING):
        call(streaming(S1))
    d = parse(lines_sent(session)[0])
    assert d["oci.resource_display_name"] == S1 and d["oci.compartment_name"] == COMP_A
    assert "name enrichment degraded" in caplog.text


def test_open_breaker_does_not_block_ingest(setup):
    session, install = setup
    resolver = install(FakeSearcher(SEARCH_DATA))
    for _ in range(3):
        resolver.breaker.record_failure()
    call(streaming(S1))
    assert len(lines_sent(session)) == 1
    assert parse(lines_sent(session)[0])["oci.resource_display_name"] == S1


def test_resource_display_name_in_the_payload_wins(setup):
    session, install = setup
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    call(streaming(S1, resourceDisplayName="name-from-oci-payload"))
    assert parse(lines_sent(session)[0])["oci.resource_display_name"] == "name-from-oci-payload"
    assert all(c[1] == COMPARTMENT_QUERY for c in searcher.calls)


def test_enrichment_applies_to_the_generic_path_only(setup):
    session, install = setup
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    cpu = {"namespace": "oci_computeagent", "resourceGroup": None, "compartmentId": COMP_A, "name": "CpuUtilization",
           "dimensions": {"resourceId": "ocid1.instance..i", "region": REGION, "resourceDisplayName": "vm"},
           "datapoints": [{"timestamp": 1730000000000, "value": 5.0}]}
    call(cpu)
    line = lines_sent(session)[0]
    assert "oci.compartment_name" not in line and searcher.calls == []


def test_enrich_names_false_sends_unenriched_and_never_searches(setup, monkeypatch):
    session, _ = setup
    monkeypatch.setenv("ENRICH_NAMES", "false")  # conftest makes any real search raise
    enrichment.reset_state()
    call(streaming(S1))
    d = parse(lines_sent(session)[0])
    assert "oci.resource_display_name" not in d and "oci.compartment_name" not in d
    assert d["oci.resource_id"] == S1


def test_enrichment_is_not_prepared_when_import_all_is_off(setup, monkeypatch):
    session, install = setup
    monkeypatch.setenv("IMPORT_ALL_METRICS", "false")
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    call(streaming(S1))
    assert searcher.calls == [] and session.calls == []


def test_a_crash_in_enrichment_never_blocks_ingest(setup, monkeypatch):
    session, install = setup
    install(FakeSearcher(SEARCH_DATA))
    monkeypatch.setattr(enrichment.Enricher, "prepare", lambda self, bodies: 1 / 0)
    call(streaming(S1))
    assert len(lines_sent(session)) == 1


def test_filtered_out_compartments_are_not_looked_up(setup, monkeypatch):
    session, install = setup
    searcher = FakeSearcher(SEARCH_DATA)
    install(searcher)
    monkeypatch.setenv("COMPARTMENT_DENYLIST", COMP_A)
    call(streaming(S1))
    assert searcher.calls == [] and session.calls == []


# ---------- the real SDK, offline: signer, region/endpoint, retry strategy, query object, pagination ----------

def test_real_sdk_usage_offline(monkeypatch):
    oci = pytest.importorskip("oci")
    from cryptography.hazmat.primitives.asymmetric import rsa

    signer = oci.auth.signers.SecurityTokenSigner("fake.token", rsa.generate_private_key(65537, 2048))
    monkeypatch.setattr(oci.auth.signers, "get_resource_principals_signer", lambda: signer)
    monkeypatch.setattr(enrichment.OciSearcher, "search", REAL_OCI_SEARCH)

    seen = {}

    class Resp:
        def __init__(self, items, nxt):
            self.data = type("C", (), {"items": items})()
            self.next_page = nxt

    def fake_search(self, details, **kwargs):
        seen["endpoint"] = self.base_client.endpoint
        seen["details"] = details
        seen["kwargs"] = kwargs
        return Resp([oci.resource_search.models.ResourceSummary(identifier=S1, display_name="orders-stream")], "tok2")

    monkeypatch.setattr(oci.resource_search.ResourceSearchClient, "search_resources", fake_search)

    items, nxt = enrichment.OciSearcher().search("uk-gov-london-1", "query stream resources where compartmentId = 'x'", "tok1")
    assert seen["endpoint"].startswith("https://query.uk-gov-london-1.oci.oraclegovcloud.uk")
    assert seen["details"].type == "Structured" and seen["details"].query.startswith("query stream resources")
    assert seen["kwargs"]["page"] == "tok1"
    strategy = seen["kwargs"]["retry_strategy"]
    assert retry_settings(strategy)["max_attempts"] == 3 and retry_settings(strategy)["total_seconds"] == 10
    assert (items[0].identifier, items[0].display_name, nxt) == (S1, "orders-stream", "tok2")


def test_bounded_retry_strategy_settings():
    oci = pytest.importorskip("oci")
    assert retry_settings(enrichment.bounded_retry_strategy(oci)) == {
        "backoff": "ExponentialBackoffWithFullJitterRetryStrategy", "max_wait": 5, "base": 1, "factor": 2,
        "max_attempts": 3, "total_seconds": 10,
        # retries 429 (any text code) and every 5xx; 409 IncorrectState/LockConflict are NOT retried
        "status_config": {429: []}, "any_5xx": True,
    }
