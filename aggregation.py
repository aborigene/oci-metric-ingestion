import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class AggregateResult:
    timestamp: int
    value: int

def to_epoch_ms(timestamp: Any) -> float:
    """Convert an OCI datapoint timestamp to epoch milliseconds.

    Accepts epoch milliseconds (int/float, or a numeric string) and ISO-8601 strings
    ("2024-10-27T03:33:20Z", "2024-10-27T03:33:20.123+00:00"). A string without a UTC
    offset is read as UTC. Raises ValueError for anything else.
    """
    if isinstance(timestamp, bool):
        raise ValueError(f"Unsupported timestamp: {timestamp!r}")
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    if isinstance(timestamp, str):
        text = timestamp.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text)  # handles a trailing "Z" on Python 3.11+
        except ValueError:
            raise ValueError(f"Unsupported timestamp: {timestamp!r}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000
    raise ValueError(f"Unsupported timestamp: {timestamp!r}")


def create_minutely_buckets(datapoints: List[Dict]) -> Dict[float, list]:
    """Group datapoint values by the epoch SECONDS of the start of their minute."""
    buckets = defaultdict(list)

    for point in datapoints or []:
        try:
            ts_ms = to_epoch_ms(point["timestamp"])
            value = point["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"non-finite or non-numeric value {value!r}")
        except (KeyError, ValueError, TypeError) as ex:
            logging.getLogger().warning(f"Skipping invalid datapoint {point!r}: {ex}")
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
        minute_bucket = int(dt.replace(second=0, microsecond=0).timestamp())
        buckets[minute_bucket].append(value)
    return buckets

def aggregate_max(datapoints: List[Dict]) -> List[AggregateResult]:
    buckets = create_minutely_buckets(datapoints)

    aggregated = []
    for timestamp, values in sorted(buckets.items()):
        aggregated.append(AggregateResult(timestamp, max(values)))
    return aggregated

def aggregate_min(datapoints: List[Dict]) -> List[AggregateResult]:
    buckets = create_minutely_buckets(datapoints)

    aggregated = []
    for timestamp, values in sorted(buckets.items()):
        aggregated.append(AggregateResult(timestamp, min(values)))
    return aggregated

def aggregate_sum(datapoints: List[Dict]) -> List[AggregateResult]:
    buckets = create_minutely_buckets(datapoints)

    aggregated = []
    for timestamp, values in sorted(buckets.items()):
        aggregated.append(AggregateResult(timestamp, sum(values)))
    return aggregated

def aggregate_mean(datapoints: List[Dict]) -> List[AggregateResult]:
    buckets = create_minutely_buckets(datapoints)

    aggregated = []
    for timestamp, values in sorted(buckets.items()):
        aggregated.append(AggregateResult(timestamp, sum(values) / len(values)))
    return aggregated
 
