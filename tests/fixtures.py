"""Deterministic OCI Monitoring payloads for every curated namespace.

One payload per (namespace, metric name[, filter variant]). Used by the golden
tests, which compare the MINT lines produced by the curated path against
``golden_expected.py`` (captured from the code before any change).
"""
from metric_mapping import namespace_map

BASE_TS = 1730000000000  # epoch ms, 2024-10-27T03:33:20Z
MINUTE = 60_000

# Dimensions to send per namespace (superset of the namespace's dimension_map keys).
_DIM_VALUES = {
    "availabilityDomain": "AD-1",
    "faultDomain": "FAULT-DOMAIN-1",
    "imageId": "ocid1.image.oc1..img",
    "instancePoolId": "ocid1.instancepool.oc1..pool",
    "region": "eu-frankfurt-1",
    "resourceDisplayName": "my-resource",
    "resourceName": "my-named-resource",
    "resourceId": "ocid1.resource.oc1.eu-frankfurt-1.aaaa",
    "lbComponent": "frontend",
    "lbHostId": "host-1",
    "lbName": "my-lb",
    "backendSetName": "bset",
    "attachmentId": "ocid1.attachment.oc1..att",
    "parentResourceId": "ocid1.vpn.oc1..vpn",
    "publicIp": "10.0.0.1",
    "DisplayName": "my-pool",
    "mountTargetId": "ocid1.mounttarget.oc1..mt",
    "throughput": "1",
    "size": "4k",
    "resourceTenantId": "ocid1.tenancy.oc1..ten",
}

# Dimensions needed to satisfy dimension_filter entries of specific metrics.
_FILTER_VARIANTS = {
    ("oci_filestorage", "MetadataRequestAverageLatency"): [
        {"resourceType": "filesystem", "operation": "ReadMetadata"},
        {"resourceType": "filesystem", "operation": "WriteMetadata"},
    ],
    ("oci_faas", "FunctionResponseCount"): [
        {"responseType": "Error"},
        {"responseType": "Throttled"},
        {"responseType": "Success"},
    ],
}


def _datapoints():
    # Two datapoints in the first minute, one in the second, one in the third.
    return [
        {"timestamp": BASE_TS, "value": 1.5, "count": 1},
        {"timestamp": BASE_TS + 20_000, "value": 4.0, "count": 1},
        {"timestamp": BASE_TS + MINUTE + 5_000, "value": 2.25, "count": 1},
        {"timestamp": BASE_TS + 2 * MINUTE, "value": 10, "count": 1},
    ]


def build_payloads():
    """Return {case_id: payload} for every curated namespace and metric."""
    payloads = {}
    for namespace, mapping in namespace_map.items():
        dim_keys = list(mapping.dimension_map.keys())
        for metric_name in mapping.metric_key_map:
            variants = _FILTER_VARIANTS.get((namespace, metric_name), [None])
            for i, variant in enumerate(variants):
                dims = {k: _DIM_VALUES.get(k, "x") for k in dim_keys
                        if k not in ("resourceGroup", "compartmentId")}
                if "resourceType" in dim_keys and not variant:
                    dims["resourceType"] = "filesystem"
                dims.update(variant or {})
                suffix = f"#{i}" if len(variants) > 1 else ""
                payloads[f"{namespace}/{metric_name}{suffix}"] = {
                    "namespace": namespace,
                    "resourceGroup": "rg-1",
                    "compartmentId": "ocid1.compartment.oc1..aaaa",
                    "name": metric_name,
                    "dimensions": dims,
                    "metadata": {"displayName": metric_name, "unit": "x"},
                    "datapoints": _datapoints(),
                }
        # One null-resourceGroup variant per namespace documents the existing "None" output.
        first = next(iter(mapping.metric_key_map))
        base = payloads[next(k for k in payloads if k.startswith(f"{namespace}/{first}"))]
        nullrg = dict(base, resourceGroup=None, dimensions=dict(base["dimensions"]))
        payloads[f"{namespace}/{first}@null-resource-group"] = nullrg
    return payloads
