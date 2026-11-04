import pytest

from aggregation import create_minutely_buckets, to_epoch_ms
from mint import MAX_DIMENSIONS, MintMetric, escape_dimension_value, sanitize_dimension_key
from summary_stat import SummaryStat

MS = 1730000000000  # 2024-10-27T03:33:20Z


@pytest.mark.parametrize("value", [
    MS, float(MS), str(MS),
    "2024-10-27T03:33:20Z", "2024-10-27T03:33:20+00:00", "2024-10-27T03:33:20", "2024-10-27T05:33:20+02:00",
])
def test_timestamp_formats_agree(value):
    assert to_epoch_ms(value) == pytest.approx(MS)


def test_iso_with_fraction():
    assert to_epoch_ms("2024-10-27T03:33:20.250Z") == pytest.approx(MS + 250)


@pytest.mark.parametrize("bad", ["yesterday", None, True, [], {}])
def test_timestamp_rejects_garbage(bad):
    with pytest.raises(ValueError):
        to_epoch_ms(bad)


def test_buckets_are_epoch_seconds_and_accept_iso():
    buckets = create_minutely_buckets([
        {"timestamp": MS, "value": 1},
        {"timestamp": "2024-10-27T03:33:50Z", "value": 3},   # same minute, ISO
        {"timestamp": MS + 60_000, "value": 5},
    ])
    assert dict(buckets) == {1729999980: [1, 3], 1730000040: [5]}


def test_invalid_datapoints_are_skipped_not_fatal():
    buckets = create_minutely_buckets([
        {"timestamp": MS, "value": 1},
        {"timestamp": "garbage", "value": 2},
        {"timestamp": MS},
        {"timestamp": MS, "value": None},
        {"timestamp": MS, "value": float("nan")},
    ])
    assert dict(buckets) == {1729999980: [1]}


def test_summary_stat_line_format():
    line = MintMetric("cloud.oci.x.y", SummaryStat(1.0, 4.0, 5.0, 2), {"a": "b"}, 1730000000000)
    assert line == 'cloud.oci.x.y,a="b" gauge,min=1.0,max=4.0,sum=5.0,count=2 1730000000000'


def test_gauge_without_dimensions():
    assert MintMetric("k", 1.5, None, 5) == "k gauge,1.5 5"


def test_escaping_quotes_backslashes_newlines():
    assert escape_dimension_value('my "stream"') == 'my \\"stream\\"'
    assert escape_dimension_value("a\\b") == "a\\\\b"
    assert escape_dimension_value("a\nb\rc") == "a b c"
    line = MintMetric("k", 1, {"name": 'say "hi" \\ there'}, 1)
    assert line == 'k,name="say \\"hi\\" \\\\ there" gauge,1 1'


def test_dimension_value_with_quote_cannot_break_out_of_the_line():
    line = MintMetric("k", 1, {"a": '", evil="1'}, 1)
    assert line.count("\n") == 0
    assert line == 'k,a="\\", evil=\\"1" gauge,1 1'


@pytest.mark.parametrize("raw,clean", [
    ("oci.Resource_Id", "oci.resource_id"),
    ("my key", "my_key"),
    ("a/b", "a_b"),
    ("9lives", "dim_9lives"),
    ("", "dim_"),
])
def test_dimension_key_sanitising(raw, clean):
    assert sanitize_dimension_key(raw) == clean


def test_too_many_dimensions_rejected():
    dims = {f"d{i}": "v" for i in range(MAX_DIMENSIONS + 1)}
    with pytest.raises(ValueError):
        MintMetric("k", 1, dims, 1)
    MintMetric("k", 1, {f"d{i}": "v" for i in range(MAX_DIMENSIONS)}, 1)


def test_oversized_line_rejected():
    with pytest.raises(ValueError):
        MintMetric("k", 1, {"a": "x" * 50_000}, 1)
