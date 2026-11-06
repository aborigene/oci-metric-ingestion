"""Generic path: ingest namespaces that have no curated mapping, keeping ALL their OCI dimensions.

Key:        cloud.oci.<namespace without the "oci_" prefix>.<metric name>
Gauge:      min/max/sum/count per minute (SummaryStat)
Dimensions: every OCI dimension, normalised so names and values stay stable (a Smartscape/OpenPipeline
            configuration can rely on them), plus cloud.provider=oci and oci.service=<namespace>.
"""
import logging
import re
from typing import Any, Dict, List

from aggregation import create_minutely_buckets
from mint import MintMetric
from summary_stat import SummaryStat

MAX_VALUE_LENGTH = 255
MAX_KEY_LENGTH = 99  # keys are kept under 100 characters

# OCI dimension names with a fixed, curated-style target name.
DIMENSION_NAMES = {
    "resourceId": "oci.resource_id",
    "region": "oci.region",
    "compartmentId": "oci.compartment_id",
    "resourceGroup": "oci.resource_group",
    "resourceDisplayName": "oci.resource_display_name",
}

# Service names used by the curated mappings (and the extension's topology rules, which match metric keys
# by PREFIX, e.g. $prefix(cloud.oci.compute)). A generic key that starts with one of these is matched by
# the extension rules too.
_CURATED_PREFIXES = (
    "compute", "instance_pool", "load_balancer", "network_load_balancer", "vcn", "vpn", "block_volume",
    "filesystem", "object_store", "api_gateway", "function",
)

_warned_prefix_clash = set()

_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NON_KEY_CHARS = re.compile(r"[^a-z0-9_]+")
_NON_METRIC_CHARS = re.compile(r"[^A-Za-z0-9_.\-]")


def reset_state():
    """Forget which warnings were already logged (used by tests)."""
    _warned_prefix_clash.clear()


def service_name(namespace: str) -> str:
    """The namespace without its "oci_" prefix (only the prefix is removed)."""
    return namespace[len("oci_"):] if namespace.startswith("oci_") else namespace


def generic_metric_key(namespace: str, metric_name: str) -> str:
    service = service_name(namespace)
    key = f"cloud.oci.{service}.{_NON_METRIC_CHARS.sub('_', str(metric_name))}"
    if any(service.startswith(p) for p in _CURATED_PREFIXES) and namespace not in _warned_prefix_clash:
        _warned_prefix_clash.add(namespace)
        logging.getLogger().warning(
            f"Metric keys of namespace '{namespace}' (cloud.oci.{service}.*) start with the same prefix as a "
            f"curated service; the extension's topology and OpenPipeline rules that match by prefix will also match them"
        )
    return key


def snake_case(name: str) -> str:
    """availabilityDomain -> availability_domain, VnicId -> vnic_id, lb-name -> lb_name."""
    name = _ACRONYM_BOUNDARY.sub(r"\1_\2", str(name))
    name = _WORD_BOUNDARY.sub(r"\1_\2", name)
    name = _NON_KEY_CHARS.sub("_", name.lower()).strip("_")
    return re.sub(r"_+", "_", name)


def dimension_key(oci_name: str) -> str:
    key = DIMENSION_NAMES.get(oci_name)
    if key is None:
        key = f"oci.{snake_case(oci_name)}"
    return key[:MAX_KEY_LENGTH]


def clean_value(value: Any):
    """Return the dimension value as a string, or None if it must be omitted (null, empty, "None")."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "None":
        return None
    return text[:MAX_VALUE_LENGTH]


def normalise_dimensions(body: Dict) -> Dict[str, str]:
    """All OCI dimensions of a payload as stable Dynatrace dimensions."""
    service = service_name(body.get("namespace") or "")
    result: Dict[str, str] = {"cloud.provider": "oci", "oci.service": service}

    oci_dimensions = dict(body.get("dimensions") or {})
    # resourceGroup and compartmentId are top-level fields of the payload, not part of "dimensions".
    oci_dimensions["resourceGroup"] = body.get("resourceGroup")
    oci_dimensions["compartmentId"] = body.get("compartmentId")

    for name, raw in oci_dimensions.items():
        value = clean_value(raw)
        if value is None:
            continue
        key = dimension_key(name)
        if key in ("cloud.provider", "oci.service"):
            continue  # reserved: always the constants above
        if key in result:
            logging.getLogger().debug(f"Dimension '{name}' maps to '{key}', which is already set; keeping the first value")
            continue
        result[key] = value
    return result


def process_generic(body: Dict) -> List[str]:
    """MINT lines (min/max/sum/count per minute) for a metric of an unmapped namespace."""
    key = generic_metric_key(body.get("namespace") or "", body.get("name"))
    dimensions = normalise_dimensions(body)

    lines: List[str] = []
    for timestamp, values in sorted(create_minutely_buckets(body.get("datapoints")).items()):
        stat = SummaryStat(min(values), max(values), sum(values), len(values))
        lines.append(str(MintMetric(key, stat, dimensions, timestamp * 1000)))
    return lines
