import copy
import io
import json
import logging

import pytest

import dynatrace_client
import func
import generic
from conftest import FakeResponse, FakeSession
from fixtures import build_payloads
from golden_expected import EXPECTED
from generic import clean_value, dimension_key, generic_metric_key, normalise_dimensions, snake_case

REGION = "eu-frankfurt-1"
COMPARTMENT = "ocid1.compartment.oc1..aaaa"
STREAM_1 = "ocid1.stream.oc1.eu-frankfurt-1.aaaa"
STREAM_2 = "ocid1.stream.oc1.eu-frankfurt-1.bbbb"


def streaming(resource_id, **overrides):
    payload = {
        "namespace": "oci_streaming", "resourceGroup": None, "compartmentId": COMPARTMENT,
        "name": "PutMessagesLatency.Time",
        "dimensions": {"region": REGION, "resourceId": resource_id},
        "metadata": {"displayName": "Put Messages Latency", "unit": "ms"},
        "datapoints": [{"timestamp": 1730000000000, "value": 12.5, "count": 1}],
    }
    payload.update(overrides)
    return payload


def dims_of(line):
    head = line.split(" gauge,")[0]
    key, _, rest = head.partition(",")
    out = {}
    for part in rest.split('",'):
        k, _, v = part.partition('="')
        out[k] = v.rstrip('"')
    return key, out


@pytest.fixture
def generic_on(monkeypatch):
    monkeypatch.setenv("IMPORT_ALL_METRICS", "true")


# ---------- routing ----------

def test_unmapped_namespace_is_skipped_without_import_all(caplog):
    with caplog.at_level(logging.INFO):
        assert func.process_metrics(streaming(STREAM_1)) == []
        assert func.process_metrics(streaming(STREAM_2)) == []
        func.process_metrics(streaming(STREAM_1, namespace="oci_other"))
    assert caplog.text.count("Skipping namespace 'oci_streaming'") == 1  # once per namespace
    assert caplog.text.count("Skipping namespace 'oci_other'") == 1


def test_unmapped_namespace_uses_generic_path_with_import_all(generic_on):
    lines = func.process_metrics(streaming(STREAM_1))
    assert len(lines) == 1 and lines[0].startswith("cloud.oci.streaming.PutMessagesLatency.Time,")


@pytest.mark.parametrize("flag", ["true", "True", " TRUE "])
def test_import_all_is_case_insensitive(monkeypatch, flag):
    monkeypatch.setenv("IMPORT_ALL_METRICS", flag)
    assert func.process_metrics(streaming(STREAM_1))


@pytest.mark.parametrize("case", sorted(build_payloads()))
def test_curated_namespaces_ignore_import_all(case, generic_on):
    assert func.process_metrics(copy.deepcopy(build_payloads()[case])) == EXPECTED[case]


def test_unmapped_metric_of_curated_namespace_is_not_sent_generically(generic_on):
    payload = copy.deepcopy(build_payloads()["oci_computeagent/CpuUtilization"])
    payload["name"] = "SomethingNew"
    assert func.process_metrics(payload) == []


# ---------- series identity: the bug behind "two streams collapse into one" ----------

def test_two_streams_in_one_compartment_are_two_series(generic_on):
    one = func.process_metrics(streaming(STREAM_1))[0]
    two = func.process_metrics(streaming(STREAM_2))[0]
    assert one.split(" gauge,")[0] != two.split(" gauge,")[0]
    _, d1 = dims_of(one)
    _, d2 = dims_of(two)
    assert d1["oci.resource_id"] == STREAM_1 and d2["oci.resource_id"] == STREAM_2
    assert d1["oci.region"] == d2["oci.region"] == REGION
    assert d1["oci.compartment_id"] == COMPARTMENT


def test_streaming_line_exact(generic_on):
    assert func.process_metrics(streaming(STREAM_1)) == [
        'cloud.oci.streaming.PutMessagesLatency.Time,cloud.provider="oci",oci.service="streaming",'
        'oci.region="eu-frankfurt-1",oci.resource_id="ocid1.stream.oc1.eu-frankfurt-1.aaaa",'
        'oci.compartment_id="ocid1.compartment.oc1..aaaa" '
        'gauge,min=12.5,max=12.5,sum=12.5,count=1 1729999980000'
    ]


def test_null_resource_group_is_omitted_never_none(generic_on):
    line = func.process_metrics(streaming(STREAM_1))[0]
    assert "None" not in line and "oci.resource_group" not in line


def test_resource_group_kept_when_present(generic_on):
    line = func.process_metrics(streaming(STREAM_1, resourceGroup="rg-1"))[0]
    assert dims_of(line)[1]["oci.resource_group"] == "rg-1"


# ---------- aggregation (unchanged behaviour) ----------

def test_min_max_sum_count_per_minute(generic_on):
    payload = streaming(STREAM_1, datapoints=[
        {"timestamp": 1730000000000, "value": 1.5, "count": 1},
        {"timestamp": 1730000020000, "value": 4.0, "count": 1},
        {"timestamp": "2024-10-27T03:34:30Z", "value": 10, "count": 1},
    ])
    lines = func.process_metrics(payload)
    assert [l.split(" gauge,")[1] for l in lines] == [
        "min=1.5,max=4.0,sum=5.5,count=2 1729999980000",
        "min=10,max=10,sum=10,count=1 1730000040000",
    ]


def test_all_buckets_are_sent_not_only_the_last(generic_on):
    assert len(func.process_metrics(streaming(STREAM_1, datapoints=[
        {"timestamp": 1730000000000 + i * 60_000, "value": i} for i in range(5)]))) == 5


def test_no_datapoints_no_lines(generic_on):
    assert func.process_metrics(streaming(STREAM_1, datapoints=None)) == []
    assert func.process_metrics(streaming(STREAM_1, datapoints=[])) == []


# ---------- key ----------

def test_key_strips_only_the_prefix():
    assert generic_metric_key("oci_streaming", "X") == "cloud.oci.streaming.X"
    assert generic_metric_key("oci_foo_oci_bar", "X") == "cloud.oci.foo_oci_bar.X"
    assert generic_metric_key("custom_ns", "X") == "cloud.oci.custom_ns.X"


def test_key_replaces_characters_the_api_rejects():
    assert generic_metric_key("oci_streaming", "Put Messages [ms]/s") == "cloud.oci.streaming.Put_Messages__ms__s"


@pytest.mark.parametrize("namespace", ["oci_computecontainerinstance", "oci_vcnip", "oci_functionsx"])
def test_warns_once_when_key_prefix_collides_with_curated_service(namespace, caplog):
    with caplog.at_level(logging.WARNING):
        generic_metric_key(namespace, "A")
        generic_metric_key(namespace, "B")
    assert caplog.text.count("same prefix as a curated service") == 1


def test_no_warning_for_streaming(caplog):
    with caplog.at_level(logging.WARNING):
        generic_metric_key("oci_streaming", "A")
    assert caplog.text == ""


# ---------- dimension normalisation ----------

@pytest.mark.parametrize("name,expected", [
    ("resourceId", "oci.resource_id"),
    ("region", "oci.region"),
    ("compartmentId", "oci.compartment_id"),
    ("resourceGroup", "oci.resource_group"),
    ("resourceDisplayName", "oci.resource_display_name"),
    ("availabilityDomain", "oci.availability_domain"),
    ("resourceTenantId", "oci.resource_tenant_id"),
    ("VnicId", "oci.vnic_id"),
    ("lbHTTPName", "oci.lb_http_name"),
    ("streamName", "oci.stream_name"),
    ("already_snake", "oci.already_snake"),
    ("with-dash", "oci.with_dash"),
])
def test_dimension_names(name, expected):
    assert dimension_key(name) == expected


def test_snake_case_is_stable_and_idempotent():
    for name in ["availabilityDomain", "VnicId", "lbHTTPName", "a--b", "X"]:
        assert snake_case(snake_case(name)) == snake_case(name)


def test_every_oci_dimension_is_kept():
    body = streaming(STREAM_1)
    body["dimensions"].update({"availabilityDomain": "AD-1", "streamPoolId": "ocid1.streampool..x", "partition": "3"})
    dims = normalise_dimensions(body)
    assert dims["oci.availability_domain"] == "AD-1"
    assert dims["oci.stream_pool_id"] == "ocid1.streampool..x"
    assert dims["oci.partition"] == "3"
    assert dims["cloud.provider"] == "oci" and dims["oci.service"] == "streaming"


@pytest.mark.parametrize("value", [None, "", "   ", "None"])
def test_empty_values_are_omitted(value):
    body = streaming(STREAM_1)
    body["dimensions"]["partition"] = value
    assert "oci.partition" not in normalise_dimensions(body)


def test_non_string_values_are_stringified():
    body = streaming(STREAM_1)
    body["dimensions"].update({"partition": 3, "flag": False})
    dims = normalise_dimensions(body)
    assert dims["oci.partition"] == "3" and dims["oci.flag"] == "False"


def test_values_truncated_to_255_and_keys_under_100():
    body = streaming(STREAM_1)
    body["dimensions"]["longValue"] = "x" * 400
    body["dimensions"]["k" * 300] = "v"
    dims = normalise_dimensions(body)
    assert len(dims["oci.long_value"]) == 255
    assert all(len(k) < 100 for k in dims)


def test_constants_cannot_be_overridden_by_oci_dimensions():
    body = streaming(STREAM_1)
    body["dimensions"]["cloud.provider"] = "aws"
    body["dimensions"]["oci.service"] = "other"
    dims = normalise_dimensions(body)
    assert dims["cloud.provider"] == "oci" and dims["oci.service"] == "streaming"


def test_top_level_fields_win_over_same_named_dimension():
    body = streaming(STREAM_1)
    body["dimensions"]["compartmentId"] = "ocid1.compartment.oc1..other"
    assert normalise_dimensions(body)["oci.compartment_id"] == COMPARTMENT


def test_existing_display_name_is_carried_through():
    body = streaming(STREAM_1)
    body["dimensions"]["resourceDisplayName"] = "orders-stream"
    assert normalise_dimensions(body)["oci.resource_display_name"] == "orders-stream"


def test_null_dimensions_object_is_tolerated(generic_on):
    assert func.process_metrics(streaming(STREAM_1, dimensions=None))


def test_input_payload_is_not_mutated(generic_on):
    payload = streaming(STREAM_1)
    before = copy.deepcopy(payload)
    func.process_metrics(payload)
    assert payload == before


def test_dimension_values_with_quotes_are_escaped(generic_on):
    payload = streaming(STREAM_1)
    payload["dimensions"]["streamName"] = 'my "stream" \\ one'
    line = func.process_metrics(payload)[0]
    assert 'oci.stream_name="my \\"stream\\" \\\\ one"' in line
    assert "\n" not in line


def test_too_many_dimensions_drops_that_metric_only(generic_on, monkeypatch, caplog):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "t")
    session = FakeSession([FakeResponse(202, {"linesOk": 1})])
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: session)
    wide = streaming(STREAM_1)
    wide["dimensions"].update({f"d{i}": "v" for i in range(60)})
    with caplog.at_level(logging.ERROR):
        func.handler(None, io.BytesIO(json.dumps([wide, streaming(STREAM_2)]).encode()))
    assert "dimensions, the limit is 50" in caplog.text
    assert len(session.calls) == 1 and session.calls[0]["data"].count(b"\n") == 0
