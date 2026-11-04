import re
from datetime import datetime
from summary_stat import SummaryStat
from typing import Dict, Optional, Union

# Limits of the Metrics v2 ingest API (line protocol).
MAX_LINE_CHARS = 50_000
MAX_DIMENSIONS = 50

_INVALID_KEY_CHARS = re.compile(r"[^a-z0-9_.\-]")


def sanitize_dimension_key(key: str) -> str:
    """Lower-case a dimension key and replace characters the line protocol does not allow."""
    key = _INVALID_KEY_CHARS.sub("_", str(key).lower())
    if not key or not key[0].isalpha():
        key = f"dim_{key}"
    return key


def escape_dimension_value(value) -> str:
    """Escape a dimension value for use inside the double quotes of a MINT line."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


class MintMetric(str):
    def __new__(cls, key: str, value: Union[float, SummaryStat], dimensions: Dict[str, str] | None = None, time: Optional[int] = None):
        if dimensions is None:
            dimensions = {}
        if len(dimensions) > MAX_DIMENSIONS:
            raise ValueError(
                f"Metric '{key}' has {len(dimensions)} dimensions, the limit is {MAX_DIMENSIONS}"
            )
        dimensions_string = ",".join(
            [f'{sanitize_dimension_key(k)}="{escape_dimension_value(v)}"' for k, v in dimensions.items()]
        )
        timestamp = int(datetime.now().timestamp() * 1000)
        if time is not None:
            timestamp = time
        line = f"{key}{',' if dimensions_string else ''}{dimensions_string} gauge,{value} {timestamp}"
        if len(line) >= MAX_LINE_CHARS:
            raise ValueError(f"Metric '{key}' line is {len(line)} characters, the limit is {MAX_LINE_CHARS}")
        return super().__new__(cls, line)
