import io
import json
import logging

import pytest

import dynatrace_client
import func
from conftest import FakeResponse, FakeSession
from filtering import CompartmentFilter, FilterStats, parse_ocid_list

PREPROD = "ocid1.compartment.oc1..preprod"
PREPROD_2 = "ocid1.compartment.oc1..preprod2"
DEV = "ocid1.compartment.oc1..dev"
QA = "ocid1.compartment.oc1..qa"


def metric(compartment, name="CpuUtilization", namespace="oci_computeagent"):
    return {
        "namespace": namespace, "resourceGroup": None, "compartmentId": compartment, "name": name,
        "dimensions": {"resourceId": "ocid1.instance.oc1..i", "region": "eu-frankfurt-1"},
        "datapoints": [{"timestamp": 1730000000000, "value": 5.0, "count": 1}],
    }


# ---------- parsing ----------

def test_parse_accepts_commas_semicolons_spaces_and_newlines():
    raw = f"{PREPROD}, {PREPROD_2};\n{DEV}\t{QA}"
    assert parse_ocid_list(raw, "X") == {PREPROD, PREPROD_2, DEV, QA}


@pytest.mark.parametrize("raw", [None, "", "   ", "<Compartment OCIDs>", " , ; "])
def test_parse_empty_and_placeholder_means_no_list(raw):
    assert parse_ocid_list(raw, "X") == frozenset()


def test_parse_warns_about_values_that_are_not_ocids(caplog):
    with caplog.at_level(logging.WARNING):
        result = parse_ocid_list(f"{PREPROD},my-compartment", "COMPARTMENT_ALLOWLIST")
    assert "my-compartment" in caplog.text and "COMPARTMENT_ALLOWLIST" in caplog.text
    assert PREPROD in result


# ---------- decisions ----------

def test_no_lists_means_filter_off():
    f = CompartmentFilter()
    assert not f.active
    assert f.allows(DEV) == (True, "")
    assert f.allows(None) == (True, "")


def test_allowlist_only_listed_compartments():
    f = CompartmentFilter(allow=frozenset({PREPROD}))
    assert f.allows(PREPROD) == (True, "")
    assert f.allows(DEV) == (False, "not in allowlist")


def test_denylist_blocks_listed_and_allows_the_rest():
    f = CompartmentFilter(deny=frozenset({DEV, QA}))
    assert f.allows(DEV) == (False, "denied")
    assert f.allows(PREPROD) == (True, "")


def test_deny_wins_over_allow():
    f = CompartmentFilter(allow=frozenset({PREPROD, DEV}), deny=frozenset({DEV}))
    assert f.allows(DEV) == (False, "denied")
    assert f.allows(PREPROD) == (True, "")


def test_missing_compartment_id():
    assert CompartmentFilter(allow=frozenset({PREPROD})).allows(None) == (False, "no compartmentId in payload")
    assert CompartmentFilter(deny=frozenset({DEV})).allows(None) == (True, "")


def test_matching_is_exact_not_prefix_or_case_folded():
    f = CompartmentFilter(allow=frozenset({PREPROD}))
    assert f.allows(PREPROD_2)[0] is False  # PREPROD is a prefix of PREPROD_2
    assert f.allows(PREPROD.upper())[0] is False


def test_from_env_logs_overlap(monkeypatch, caplog):
    monkeypatch.setenv("COMPARTMENT_ALLOWLIST", f"{PREPROD},{DEV}")
    monkeypatch.setenv("COMPARTMENT_DENYLIST", DEV)
    with caplog.at_level(logging.WARNING):
        f = CompartmentFilter.from_env()
    assert DEV in caplog.text and f.allows(DEV)[0] is False


def test_stats_summary_is_capped_and_counts_reasons():
    stats = FilterStats()
    for i in range(15):
        stats.add(f"ocid1.compartment.oc1..c{i}", "denied")
    stats.add(None, "no compartmentId in payload")
    summary = stats.summary(top=3)
    assert summary["skipped_items"] == 16
    assert summary["reasons"] == {"denied": 15, "no compartmentId in payload": 1}
    assert len(summary["compartments"]) == 3


# ---------- handler integration ----------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "dt0c01.SECRET")
    s = FakeSession([FakeResponse(202, {"linesOk": 1, "linesInvalid": 0})])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: s)
    return s


def call(payload):
    func.handler(None, io.BytesIO(json.dumps(payload).encode()))


def sent_compartments(session):
    return [l.split('oci.compartment_id="')[1].split('"')[0]
            for c in session.ingest_calls() for l in c["data"].decode().split("\n")]


def test_filter_is_off_by_default(env):
    call([metric(DEV), metric(PREPROD)])
    assert sorted(sent_compartments(env)) == sorted([DEV, PREPROD])


def test_allowlist_keeps_only_preprod(env, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_ALLOWLIST", PREPROD)
    call([metric(DEV), metric(PREPROD), metric(QA), metric(PREPROD)])
    assert sent_compartments(env) == [PREPROD, PREPROD]


def test_denylist_drops_non_prod(env, monkeypatch):
    monkeypatch.setenv("COMPARTMENT_DENYLIST", f"{DEV}\n{QA}")
    call([metric(DEV), metric(PREPROD), metric(QA)])
    assert sent_compartments(env) == [PREPROD]


def test_filter_applies_to_generic_namespaces_too(env, monkeypatch):
    monkeypatch.setenv("IMPORT_ALL_METRICS", "true")
    monkeypatch.setenv("COMPARTMENT_ALLOWLIST", PREPROD)
    call([metric(DEV, "PutMessagesLatency.Time", "oci_streaming"),
          metric(PREPROD, "PutMessagesLatency.Time", "oci_streaming")])
    assert sent_compartments(env) == [PREPROD]


def test_everything_filtered_sends_nothing(env, monkeypatch, caplog):
    monkeypatch.setenv("COMPARTMENT_ALLOWLIST", PREPROD)
    with caplog.at_level(logging.INFO):
        call([metric(DEV), metric(QA)])
    assert env.calls == []
    assert '"skipped_items": 2' in caplog.text and DEV in caplog.text


def test_summary_logged_once_per_invocation_not_per_metric(env, monkeypatch, caplog):
    monkeypatch.setenv("COMPARTMENT_DENYLIST", DEV)
    with caplog.at_level(logging.INFO):
        call([metric(DEV)] * 20 + [metric(PREPROD)])
    assert caplog.text.count("compartment filter:") == 1
    assert '"skipped_items": 20' in caplog.text


def test_filtered_items_do_not_count_as_invalid(env, monkeypatch, caplog):
    monkeypatch.setenv("COMPARTMENT_DENYLIST", DEV)
    with caplog.at_level(logging.INFO):
        call([metric(DEV), metric(PREPROD)])
    assert "invalid_items=0" in caplog.text


def test_malformed_item_still_isolated_when_filter_active(env, monkeypatch, caplog):
    monkeypatch.setenv("COMPARTMENT_ALLOWLIST", PREPROD)
    with caplog.at_level(logging.INFO):
        call(["not-an-object", metric(PREPROD)])
    assert sent_compartments(env) == [PREPROD]
    assert "invalid_items=1" in caplog.text
