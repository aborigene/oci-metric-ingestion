import json
import logging
import threading

import pytest

import enrichment
from enrichment import (
    CircuitBreaker, Enricher, EnrichmentConfig, NameMemo, NameResolver, ResourceWant, is_transient,
)
from registry import COMPARTMENT_QUERY, RegistryEntry, load_registry

REGION = "eu-frankfurt-1"
COMP_A = "ocid1.compartment.oc1..aaaa"
COMP_B = "ocid1.compartment.oc1..bbbb"
S1, S2, S3 = ("ocid1.stream.oc1.eu-frankfurt-1.s%d" % i for i in (1, 2, 3))
STREAM = RegistryEntry("stream")


def q_streams(comp):
    return f"query stream resources where compartmentId = '{comp}'"


class Item:
    def __init__(self, identifier, display_name):
        self.identifier, self.display_name = identifier, display_name


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSearcher:
    """data: {(region, query): [page1_items, page2_items, ...]}. `side_effects` is consumed one per call:
    an Exception is raised, None means "answer normally"."""

    def __init__(self, data=None, side_effects=None, clock=None, cost=0.0):
        self.data = data or {}
        self.side_effects = list(side_effects or [])
        self.calls = []
        self.clock, self.cost = clock, cost

    def search(self, region, query, page=None):
        self.calls.append((region, query, page))
        if self.clock:
            self.clock.t += self.cost
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if effect is not None:
                raise effect
        pages = self.data.get((region, query), [[]])
        index = int(page) if page else 0
        nxt = str(index + 1) if index + 1 < len(pages) else None
        return pages[index], nxt


def resolver(searcher, clock=None, rand=lambda: 0.5, **config):
    clock = clock or Clock()
    r = NameResolver(EnrichmentConfig(**config), searcher, clock=clock, rand=rand)
    r.begin_invocation()
    return r, clock


def want(ocid, comp=COMP_A, region=REGION, entry=STREAM):
    return ResourceWant(region, comp, entry, ocid)


STREAMS_A = {(REGION, q_streams(COMP_A)): [[Item(S1, "orders"), Item(S2, "payments"), Item(S3, "audit")]]}


# ---------- batching: one search per group fills the memo for everything ----------

def test_one_search_per_group_fills_memo_for_all_streams():
    searcher = FakeSearcher(STREAMS_A)
    r, _ = resolver(searcher)
    r.resolve([want(S1)], [])
    assert len(searcher.calls) == 1
    assert searcher.calls[0][:2] == (REGION, q_streams(COMP_A))
    # the other two streams were NOT requested but are now known
    assert (r.lookup(REGION, S1), r.lookup(REGION, S2), r.lookup(REGION, S3)) == ("orders", "payments", "audit")
    r.resolve([want(S2), want(S3)], [])
    assert len(searcher.calls) == 1  # served from the memo


def test_requests_for_one_group_are_deduplicated_into_one_call():
    searcher = FakeSearcher(STREAMS_A)
    r, _ = resolver(searcher)
    r.resolve([want(S1), want(S2), want(S1), want(S3)], [])
    assert len(searcher.calls) == 1


def test_groups_are_region_compartment_and_type():
    data = {
        (REGION, q_streams(COMP_A)): [[Item(S1, "a")]],
        (REGION, q_streams(COMP_B)): [[Item(S2, "b")]],
        ("us-ashburn-1", q_streams(COMP_A)): [[Item(S3, "c")]],
        (REGION, "query queue resources where compartmentId = '%s'" % COMP_A): [[Item("ocid1.queue..q", "q")]],
    }
    searcher = FakeSearcher(data)
    r, _ = resolver(searcher)
    r.resolve([want(S1), want(S2, COMP_B), want(S3, region="us-ashburn-1"),
               want("ocid1.queue..q", entry=RegistryEntry("queue"))], [])
    assert len(searcher.calls) == 4
    assert r.lookup("us-ashburn-1", S3) == "c" and r.lookup(REGION, S3) is None  # memo is per region


def test_compartments_one_call_per_region_and_fill_all():
    data = {(REGION, COMPARTMENT_QUERY): [[Item(COMP_A, "prod"), Item(COMP_B, "dev"), Item("ocid1.compartment..z", "z")]],
            ("us-ashburn-1", COMPARTMENT_QUERY): [[Item(COMP_A, "prod-us")]]}
    searcher = FakeSearcher(data)
    r, _ = resolver(searcher)
    r.resolve([], [(REGION, COMP_A), (REGION, COMP_B), ("us-ashburn-1", COMP_A)])
    assert sorted(c[0] for c in searcher.calls) == sorted(["us-ashburn-1", REGION])
    assert r.lookup(REGION, "ocid1.compartment..z") == "z"
    assert r.lookup("us-ashburn-1", COMP_A) == "prod-us"


def test_exact_queries():
    searcher = FakeSearcher()
    r, _ = resolver(searcher)
    r.resolve([want(S1)], [(REGION, COMP_A)])
    assert {c[1] for c in searcher.calls} == {
        "query stream resources where compartmentId = 'ocid1.compartment.oc1..aaaa'",
        "query compartment resources",
    }


def test_compartment_id_that_is_not_an_ocid_is_never_put_in_a_query():
    searcher = FakeSearcher()
    r, _ = resolver(searcher)
    r.resolve([want(S1, comp="x' || displayName = 'y")], [])
    assert searcher.calls == []


# ---------- memo: TTL, jitter, negative caching, size ----------

def test_ttl_expiry_with_injected_clock():
    clock = Clock()
    searcher = FakeSearcher(STREAMS_A)
    r, _ = resolver(searcher, clock, ttl_seconds=900)
    r.resolve([want(S1)], [])
    clock.t += 719  # < 900 * 0.8 (rand=0.5 -> exactly 900, so safely inside)
    assert r.lookup(REGION, S1) == "orders"
    clock.t += 200  # 919 > 900
    assert r.lookup(REGION, S1) is None
    r.begin_invocation()
    r.resolve([want(S1)], [])
    assert len(searcher.calls) == 2  # refreshed after expiry


@pytest.mark.parametrize("rand,expected", [(0.0, 720.0), (0.5, 900.0), (1.0, 1080.0)])
def test_ttl_jitter_is_plus_minus_20_percent(rand, expected):
    clock = Clock(0.0)
    memo = NameMemo(clock, lambda: rand, ttl=900, negative_ttl=120, max_entries=10)
    memo.put(("r", "a"), "name")
    clock.t = expected - 0.001
    assert memo.get(("r", "a")) == (True, "name")
    clock.t = expected
    assert memo.get(("r", "a")) == (False, None)


def test_negative_cache_prevents_repeat_lookups_then_expires():
    clock = Clock()
    searcher = FakeSearcher({(REGION, q_streams(COMP_A)): [[Item(S1, "orders")]]})
    r, _ = resolver(searcher, clock, negative_ttl_seconds=120)
    r.resolve([want("ocid1.stream..ghost")], [])
    assert len(searcher.calls) == 1
    r.begin_invocation()
    r.resolve([want("ocid1.stream..ghost")], [])
    assert len(searcher.calls) == 1  # negatively cached
    assert r.lookup(REGION, "ocid1.stream..ghost") is None
    clock.t += 121
    r.begin_invocation()
    r.resolve([want("ocid1.stream..ghost")], [])
    assert len(searcher.calls) == 2


def test_memo_evicts_oldest_beyond_max_entries():
    memo = NameMemo(Clock(), lambda: 0.5, ttl=900, negative_ttl=120, max_entries=3)
    for i in range(5):
        memo.put(("r", f"id{i}"), f"n{i}")
    assert len(memo) == 3
    assert memo.get(("r", "id0"))[0] is False and memo.get(("r", "id1"))[0] is False
    assert [memo.get(("r", f"id{i}"))[1] for i in (2, 3, 4)] == ["n2", "n3", "n4"]


def test_memo_refreshing_an_entry_makes_it_newest():
    memo = NameMemo(Clock(), lambda: 0.5, ttl=900, negative_ttl=120, max_entries=2)
    memo.put(("r", "a"), "a")
    memo.put(("r", "b"), "b")
    memo.put(("r", "a"), "a2")
    memo.put(("r", "c"), "c")
    assert memo.get(("r", "b"))[0] is False and memo.get(("r", "a"))[1] == "a2"


# ---------- guardrails ----------

def three_groups():
    data = {(REGION, q_streams(c)): [[Item(f"ocid1.stream..{i}", f"n{i}")]] for i, c in enumerate(
        ["ocid1.compartment..c0", "ocid1.compartment..c1", "ocid1.compartment..c2"])}
    wants = [want(f"ocid1.stream..{i}", comp=f"ocid1.compartment..c{i}") for i in range(3)]
    return data, wants


def test_per_invocation_call_cap():
    data, wants = three_groups()
    searcher = FakeSearcher(data)
    r, _ = resolver(searcher, max_calls=2)
    r.resolve(wants, [])
    assert len(searcher.calls) == 2
    assert r.summary()["skipped_cap"] == 1
    # the group that was skipped is NOT negatively cached: it is resolved in the next invocation
    r.begin_invocation()
    r.resolve(wants, [])
    assert len(searcher.calls) == 3


def test_pages_count_against_the_call_cap_and_partial_results_are_kept():
    pages = [[Item(S1, "orders")], [Item(S2, "payments")], [Item(S3, "audit")]]
    searcher = FakeSearcher({(REGION, q_streams(COMP_A)): pages})
    r, _ = resolver(searcher, max_calls=2)
    r.resolve([want("ocid1.stream..unseen")], [])
    assert len(searcher.calls) == 2
    assert r.lookup(REGION, S1) == "orders" and r.lookup(REGION, S2) == "payments"
    # incomplete result: the unseen id must not be remembered as "does not exist"
    assert r.memo.get((REGION, "ocid1.stream..unseen")) == (False, None)


def test_pagination_is_followed_to_the_end():
    pages = [[Item(S1, "orders")], [Item(S2, "payments")], [Item(S3, "audit")]]
    searcher = FakeSearcher({(REGION, q_streams(COMP_A)): pages})
    r, _ = resolver(searcher, max_calls=5)
    r.resolve([want(S3)], [])
    assert [c[2] for c in searcher.calls] == [None, "1", "2"]
    assert r.lookup(REGION, S3) == "audit"


def test_per_invocation_seconds_cap():
    clock = Clock()
    data, wants = three_groups()
    searcher = FakeSearcher(data, clock=clock, cost=6.0)  # each call "takes" 6s
    r, _ = resolver(searcher, clock, max_seconds=10)
    r.resolve(wants, [])
    assert len(searcher.calls) == 2  # after 12s the third group is skipped
    assert r.summary()["skipped_cap"] == 1


def test_zero_call_cap_disables_lookups():
    searcher = FakeSearcher(STREAMS_A)
    r, _ = resolver(searcher, max_calls=0)
    r.resolve([want(S1)], [(REGION, COMP_A)])
    assert searcher.calls == []


def test_only_one_lookup_per_group_in_flight():
    searcher = FakeSearcher(STREAMS_A)
    r, _ = resolver(searcher)
    key = (REGION, COMP_A, "stream", STREAM.query)
    lock = r._group_lock(key)
    assert lock.acquire(blocking=False)
    try:
        r.resolve([want(S1)], [])  # must not wait for the lock
    finally:
        lock.release()
    assert searcher.calls == [] and r.summary()["skipped_busy"] == 1
    r.resolve([want(S1)], [])
    assert len(searcher.calls) == 1


def test_concurrent_resolves_do_one_search():
    barrier, release = threading.Event(), threading.Event()

    class Slow(FakeSearcher):
        def search(self, region, query, page=None):
            barrier.set()
            release.wait(5)
            return super().search(region, query, page)

    searcher = Slow(STREAMS_A)
    r, _ = resolver(searcher)
    t = threading.Thread(target=lambda: r.resolve([want(S1)], []))
    t.start()
    assert barrier.wait(5)
    r.resolve([want(S1)], [])  # second caller while the first is in flight
    release.set()
    t.join(5)
    assert len(searcher.calls) == 1


# ---------- circuit breaker ----------

def test_breaker_opens_after_three_consecutive_failures_and_recovers():
    clock = Clock()
    data, wants = three_groups()
    extra = want("ocid1.stream..x", comp="ocid1.compartment..c9")
    searcher = FakeSearcher(data, side_effects=[HttpError(429)] * 3 + [HttpError(503), None], clock=None)
    r, _ = resolver(searcher, clock, max_calls=20)

    r.resolve(wants, [])  # three failing groups
    assert r.summary()["failures"] == 3 and r.summary()["breaker"].startswith("open(60s")
    calls_before = len(searcher.calls)

    r.begin_invocation()
    r.resolve([extra], [])  # breaker open: no call
    assert len(searcher.calls) == calls_before and r.summary()["skipped_breaker"] == 1

    clock.t += 61  # half-open: one probe
    r.begin_invocation()
    r.resolve([want("ocid1.stream..y", comp="ocid1.compartment..c8")], [])
    assert len(searcher.calls) == calls_before + 1  # the probe fails (503) -> re-opens for 120s
    assert r.summary()["breaker"].startswith("open(120s")

    clock.t += 121
    r.begin_invocation()
    r.resolve([want(S1)], [])  # probe succeeds
    assert r.breaker.state == "closed"
    # after recovering the delay starts from 60s again
    for _ in range(3):
        r.breaker.record_failure()
    assert r.breaker.state.startswith("open(60s")


def test_breaker_delay_doubles_up_to_600s():
    clock = Clock()
    b = CircuitBreaker(clock)
    delays = []
    for _ in range(3):
        b.record_failure()
    for _ in range(6):
        delays.append(round(b._open_until - clock.t))
        clock.t = b._open_until
        b.record_failure()  # failed probe
    assert delays == [60, 120, 240, 480, 600, 600]


def test_non_transient_errors_do_not_open_the_breaker():
    data, wants = three_groups()
    searcher = FakeSearcher(data, side_effects=[HttpError(403)] * 3)
    r, _ = resolver(searcher, max_calls=20)
    r.resolve(wants, [])
    assert r.summary()["failures"] == 3 and r.breaker.state == "closed"


def test_a_success_resets_the_failure_count():
    b = CircuitBreaker(Clock())
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.allow()


@pytest.mark.parametrize("ex,expected", [
    (HttpError(429), True), (HttpError(500), True), (HttpError(503), True), (HttpError(404), False),
    (HttpError(403), False), (HttpError(401), False), (TimeoutError(), True), (ConnectionError(), True),
    (ValueError("x"), False),
])
def test_transient_classification(ex, expected):
    assert is_transient(ex) is expected


def test_transient_by_exception_name():
    class ConnectTimeout(Exception):
        pass
    assert is_transient(ConnectTimeout())


# ---------- failure never raises and falls back to the OCID ----------

def test_lookup_failure_never_raises_and_falls_back_to_ocid(caplog):
    searcher = FakeSearcher(side_effects=[HttpError(500)] * 5)
    r, _ = resolver(searcher)
    enricher = Enricher(load_registry(), r)
    body = streaming(S1)
    with caplog.at_level(logging.WARNING):
        enricher.prepare([body])  # must not raise
    dims = enricher.apply(body, enrichment.normalise_dimensions(body))
    assert dims["oci.resource_display_name"] == S1
    assert dims["oci.compartment_name"] == COMP_A
    warning = [rec.message for rec in caplog.records if "name enrichment degraded" in rec.message][0]
    counters = json.loads(warning.split(": ", 1)[1])
    assert {"hits", "misses", "calls", "failures", "breaker"} <= set(counters)
    assert counters["failures"] >= 1


def test_unexpected_exception_inside_resolver_is_contained(caplog):
    class Boom:
        def search(self, *a, **k):
            raise RuntimeError("boom")

    r, _ = resolver(Boom())
    r.resolve([want(S1)], [(REGION, COMP_A)])  # does not raise
    assert r.summary()["failures"] >= 1


def test_a_bug_in_resolve_is_contained(monkeypatch, caplog):
    r, _ = resolver(FakeSearcher())
    monkeypatch.setattr(r, "_resolve", lambda *a: 1 / 0)
    with caplog.at_level(logging.WARNING):
        r.resolve([want(S1)], [])
    assert "unexpected error" in caplog.text and r.summary()["errors"] == 1


# ---------- the enricher ----------

def streaming(resource_id, **dims):
    body = {
        "namespace": "oci_streaming", "resourceGroup": None, "compartmentId": COMP_A, "name": "PutMessagesLatency.Time",
        "dimensions": {"region": REGION, "resourceId": resource_id},
        "datapoints": [{"timestamp": 1730000000000, "value": 12.5, "count": 1}],
    }
    body["dimensions"].update(dims)
    return body


def make_enricher(searcher, registry=None, **config):
    r, clock = resolver(searcher, **config)
    return Enricher(registry or load_registry(), r), r, clock


def names(enricher, body):
    return enricher.apply(body, enrichment.normalise_dimensions(body))


def test_names_added_for_registered_namespace():
    searcher = FakeSearcher({**STREAMS_A, (REGION, COMPARTMENT_QUERY): [[Item(COMP_A, "ProdStreams")]]})
    enricher, _, _ = make_enricher(searcher)
    bodies = [streaming(S1), streaming(S2)]
    enricher.prepare(bodies)
    assert len(searcher.calls) == 2  # ONE stream search for both streams + ONE compartment search
    d1, d2 = (names(enricher, b) for b in bodies)
    assert d1["oci.resource_display_name"] == "orders" and d2["oci.resource_display_name"] == "payments"
    assert d1["oci.compartment_name"] == d2["oci.compartment_name"] == "ProdStreams"


def test_existing_resource_display_name_skips_the_lookup():
    searcher = FakeSearcher({(REGION, COMPARTMENT_QUERY): [[Item(COMP_A, "ProdStreams")]]})
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1, resourceDisplayName="from-the-payload")
    enricher.prepare([body])
    assert [c[1] for c in searcher.calls] == [COMPARTMENT_QUERY]  # no stream search
    dims = names(enricher, body)
    assert dims["oci.resource_display_name"] == "from-the-payload"
    assert dims["oci.compartment_name"] == "ProdStreams"


def test_namespace_without_registry_entry_gets_no_resource_lookup_and_no_error():
    searcher = FakeSearcher({(REGION, COMPARTMENT_QUERY): [[Item(COMP_A, "ProdStreams")]]})
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1)
    body["namespace"] = "oci_unregistered"
    enricher.prepare([body])
    assert [c[1] for c in searcher.calls] == [COMPARTMENT_QUERY]
    dims = names(enricher, body)
    assert "oci.resource_display_name" not in dims and dims["oci.compartment_name"] == "ProdStreams"


def test_missing_region_means_no_lookup_and_ocid_fallback():
    searcher = FakeSearcher()
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1)
    del body["dimensions"]["region"]
    enricher.prepare([body])
    assert searcher.calls == []
    dims = names(enricher, body)
    assert dims["oci.resource_display_name"] == S1 and dims["oci.compartment_name"] == COMP_A


def test_missing_resource_id_means_no_resource_name():
    enricher, _, _ = make_enricher(FakeSearcher())
    body = streaming(S1)
    del body["dimensions"]["resourceId"]
    enricher.prepare([body])
    assert "oci.resource_display_name" not in names(enricher, body)


def test_name_longer_than_255_is_truncated():
    long_name = "n" * 400
    searcher = FakeSearcher({(REGION, q_streams(COMP_A)): [[Item(S1, long_name)]]})
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1)
    enricher.prepare([body])
    assert names(enricher, body)["oci.resource_display_name"] == "n" * 255


def test_a_name_that_is_the_string_none_falls_back_to_the_ocid():
    searcher = FakeSearcher({(REGION, q_streams(COMP_A)): [[Item(S1, "None")]]})
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1)
    enricher.prepare([body])
    assert names(enricher, body)["oci.resource_display_name"] == S1


def test_custom_registry_entry_dimension_names():
    registry = load_registry(json.dumps({"oci_queue": {"resource_type": "queue", "id_dimension": "queueId",
                                                       "name_dimension": "oci.queue_name"}}))
    q = "query queue resources where compartmentId = '%s'" % COMP_A
    searcher = FakeSearcher({(REGION, q): [[Item("ocid1.queue..q1", "jobs")]]})
    enricher, _, _ = make_enricher(searcher, registry)
    body = {"namespace": "oci_queue", "compartmentId": COMP_A, "name": "X", "resourceGroup": None,
            "dimensions": {"region": REGION, "queueId": "ocid1.queue..q1"}, "datapoints": []}
    enricher.prepare([body])
    assert names(enricher, body)["oci.queue_name"] == "jobs"


def test_lookup_uses_the_region_of_the_metric():
    data = {("us-ashburn-1", q_streams(COMP_A)): [[Item(S1, "us-orders")]]}
    searcher = FakeSearcher(data)
    enricher, _, _ = make_enricher(searcher)
    body = streaming(S1, region="us-ashburn-1")
    enricher.prepare([body])
    assert {c[0] for c in searcher.calls} == {"us-ashburn-1"}
    assert names(enricher, body)["oci.resource_display_name"] == "us-orders"


# ---------- config and registry ----------

def test_config_defaults(monkeypatch):
    monkeypatch.delenv("ENRICH_NAMES")  # the test suite turns it off by default
    c = EnrichmentConfig.from_env()
    assert (c.enabled, c.ttl_seconds, c.negative_ttl_seconds, c.max_entries, c.max_calls, c.max_seconds) == (
        True, 900, 120, 10000, 5, 10)


def test_config_from_env(monkeypatch):
    for k, v in {"ENRICH_NAMES": "false", "NAME_CACHE_TTL_SECONDS": "60", "NAME_CACHE_NEGATIVE_TTL_SECONDS": "5",
                 "NAME_CACHE_MAX_ENTRIES": "7", "MAX_LOOKUP_CALLS_PER_INVOCATION": "2",
                 "MAX_LOOKUP_SECONDS_PER_INVOCATION": "3"}.items():
        monkeypatch.setenv(k, v)
    c = EnrichmentConfig.from_env()
    assert (c.enabled, c.ttl_seconds, c.negative_ttl_seconds, c.max_entries, c.max_calls, c.max_seconds) == (
        False, 60, 5, 7, 2, 3)


def test_config_ignores_garbage(monkeypatch):
    monkeypatch.setenv("NAME_CACHE_TTL_SECONDS", "soon")
    monkeypatch.setenv("MAX_LOOKUP_CALLS_PER_INVOCATION", "-3")
    c = EnrichmentConfig.from_env()
    assert c.ttl_seconds == 900 and c.max_calls == 5


def test_default_registry_has_only_streaming():
    registry = load_registry()
    assert list(registry) == ["oci_streaming"]
    assert registry["oci_streaming"] == RegistryEntry("stream", "resourceId", "oci.resource_display_name")


def test_registry_override_and_extend():
    registry = load_registry(json.dumps({
        "oci_streaming": {"name_dimension": "oci.stream_name"},
        "oci_queue": {"resource_type": "queue"},
    }))
    assert registry["oci_streaming"].resource_type == "stream" and registry["oci_streaming"].name_dimension == "oci.stream_name"
    assert registry["oci_queue"] == RegistryEntry("queue")


def test_registry_null_removes_an_entry():
    assert load_registry('{"oci_streaming": null}') == {}


@pytest.mark.parametrize("raw", [
    "not json", "[1, 2]", '{"oci_x": "string"}', '{"oci_x": {"nothing": 1}}', '{"oci_x": {"resource_type": "bad type!"}}',
    '{"oci_x": {"resource_type": "x", "query": "query x resources"}}',
    '{"oci_x": {"resource_type": "x", "query": "{unknown} {compartment_id}"}}',
    '{"oci_x": {"resource_type": "x", "name_dimension": ""}}',
])
def test_invalid_registry_entries_are_ignored_not_fatal(raw, caplog):
    with caplog.at_level(logging.ERROR):
        registry = load_registry(raw)
    assert "oci_x" not in registry and "oci_streaming" in registry
    assert "ENRICHMENT_REGISTRY_JSON" in caplog.text


@pytest.mark.parametrize("raw", [None, "", "  ", "<Registry JSON>"])
def test_empty_registry_override_is_default(raw):
    assert list(load_registry(raw)) == ["oci_streaming"]


def test_registry_env_variable(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_REGISTRY_JSON", '{"oci_queue": {"resource_type": "queue"}}')
    assert set(load_registry()) == {"oci_streaming", "oci_queue"}


def test_query_template_can_be_overridden():
    registry = load_registry(json.dumps({"oci_streaming": {"query": "query Stream resources where compartmentId = '{compartment_id}'"}}))
    assert registry["oci_streaming"].build_query(COMP_A) == "query Stream resources where compartmentId = '%s'" % COMP_A


# ---------- process-wide enricher ----------

def test_enrich_names_false_disables_everything(monkeypatch):
    monkeypatch.setenv("ENRICH_NAMES", "false")
    enrichment.reset_state()
    assert enrichment.get_enricher() is None


def test_enricher_is_created_once_and_keeps_its_memo(monkeypatch):
    monkeypatch.setenv("ENRICH_NAMES", "true")
    enrichment.reset_state()
    first = enrichment.get_enricher()
    assert first is not None and enrichment.get_enricher() is first
