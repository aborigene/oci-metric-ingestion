"""The curated (mapped) path must produce exactly the lines captured before the refactor."""
import copy

import pytest

import func
from fixtures import build_payloads
from golden_expected import EXPECTED
from metric_mapping import namespace_map

PAYLOADS = build_payloads()


def test_fixture_set_matches_golden_set():
    assert set(PAYLOADS) == set(EXPECTED)


def test_every_namespace_and_metric_is_covered():
    for namespace, mapping in namespace_map.items():
        for metric_name in mapping.metric_key_map:
            assert any(k.startswith(f"{namespace}/{metric_name}") for k in EXPECTED), (namespace, metric_name)


@pytest.mark.parametrize("case", sorted(PAYLOADS))
def test_curated_output_is_unchanged(case, monkeypatch):
    monkeypatch.setenv("IMPORT_ALL_METRICS", "False")
    lines = func.process_metrics(copy.deepcopy(PAYLOADS[case]))
    assert lines == EXPECTED[case]


def test_import_all_unset_still_uses_curated_path():
    # IMPORT_ALL_METRICS used to be mandatory (KeyError when missing); unset now means false.
    case = "oci_computeagent/CpuUtilization"
    assert func.process_metrics(copy.deepcopy(PAYLOADS[case])) == EXPECTED[case]


def test_known_curated_defects_are_preserved():
    # Documented in CHANGES.md; changing them is a deliberate, separate decision.
    assert any('oci.resource_group="None"' in v[0] for v in EXPECTED.values() if v)
    lines = EXPECTED["oci_computeagent/CpuUtilization"]
    assert len(lines) == 1  # four datapoints over three minutes, only the last minute is sent


def test_curated_send_all_buckets_flag(monkeypatch):
    monkeypatch.setenv("CURATED_SEND_ALL_BUCKETS", "true")
    lines = func.process_metrics(copy.deepcopy(PAYLOADS["oci_computeagent/CpuUtilization"]))
    assert len(lines) == 3
    assert lines[-1] == EXPECTED["oci_computeagent/CpuUtilization"][0]
    assert [l.rsplit(" ", 1)[1] for l in lines] == ["1729999980000", "1730000040000", "1730000100000"]
