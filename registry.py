"""Which namespaces get their resource names resolved, and how. Data, not code.

Each entry maps an OCI namespace to:
  resource_type   OCI Search resource type used in the query (for example "stream")
  id_dimension    the OCI dimension holding the resource OCID                 (default "resourceId")
  name_dimension  the Dynatrace dimension that receives the name               (default "oci.resource_display_name")
  query           Search query template; placeholders {resource_type} and {compartment_id}
                  (default "query {resource_type} resources where compartmentId = '{compartment_id}'")

The shipped registry has one entry (oci_streaming). Add or change entries with the ENRICHMENT_REGISTRY_JSON
environment variable, for example:

  {"oci_streaming": {"query": "query stream resources where compartmentId = '{compartment_id}'"},
   "oci_queue":     {"resource_type": "queue"},
   "oci_other":     null}

A field given for an existing namespace overrides that field only; `null` removes the namespace; a new
namespace needs at least "resource_type". Invalid entries are logged and ignored.

NOTE: the default query text and the resource type names are UNVERIFIED against a live tenancy. Check them with
  oci search resource structured-search --query-text "query stream resources where compartmentId = '<ocid>'"
and correct them through "query" / "resource_type" here, no code change is needed.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Dict, Optional

DEFAULT_ID_DIMENSION = "resourceId"
DEFAULT_NAME_DIMENSION = "oci.resource_display_name"
DEFAULT_QUERY = "query {resource_type} resources where compartmentId = '{compartment_id}'"
COMPARTMENT_QUERY = "query compartment resources"  # UNVERIFIED, see the note above
COMPARTMENT_RESOURCE_TYPE = "compartment"

# The OCID of a compartment is placed inside a quoted query string, so it is validated first.
OCID_PATTERN = re.compile(r"^ocid1\.[A-Za-z0-9._-]+$")
_RESOURCE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_ALLOWED_FIELDS = {"resource_type", "id_dimension", "name_dimension", "query"}


@dataclass(frozen=True)
class RegistryEntry:
    resource_type: str
    id_dimension: str = DEFAULT_ID_DIMENSION
    name_dimension: str = DEFAULT_NAME_DIMENSION
    query: str = DEFAULT_QUERY

    def build_query(self, compartment_id: str) -> str:
        return self.query.format(resource_type=self.resource_type, compartment_id=compartment_id)


DEFAULT_REGISTRY: Dict[str, dict] = {
    "oci_streaming": {"resource_type": "stream"},
}


def _validate(namespace: str, entry: RegistryEntry) -> Optional[str]:
    """Return a problem description, or None if the entry is usable."""
    if not _RESOURCE_TYPE.match(entry.resource_type or ""):
        return f"resource_type {entry.resource_type!r} must be letters and digits"
    if not entry.id_dimension or not isinstance(entry.id_dimension, str):
        return "id_dimension must be a non-empty string"
    if not entry.name_dimension or not isinstance(entry.name_dimension, str) or len(entry.name_dimension) > 99:
        return "name_dimension must be a non-empty string of at most 99 characters"
    try:
        text = entry.build_query("ocid1.compartment.oc1..x")
    except (KeyError, IndexError, ValueError) as ex:
        return f"query has an unknown placeholder or bad braces ({ex!r}); allowed: {{resource_type}}, {{compartment_id}}"
    if "ocid1.compartment.oc1..x" not in text:
        return "query must contain {compartment_id}"
    return None


def load_registry(raw_json: Optional[str] = None) -> Dict[str, RegistryEntry]:
    """The shipped registry with ENRICHMENT_REGISTRY_JSON (or `raw_json`) applied. Never raises."""
    if raw_json is None:
        raw_json = os.environ.get("ENRICHMENT_REGISTRY_JSON")

    entries: Dict[str, RegistryEntry] = {ns: RegistryEntry(**fields) for ns, fields in DEFAULT_REGISTRY.items()}

    text = (raw_json or "").strip()
    if not text or (text.startswith("<") and text.endswith(">")):
        return entries
    try:
        overrides = json.loads(text)
        if not isinstance(overrides, dict):
            raise ValueError("expected a JSON object of namespace -> entry")
    except ValueError as ex:
        logging.getLogger().error(f"ENRICHMENT_REGISTRY_JSON ignored, it is not valid: {ex}")
        return entries

    for namespace, fields in overrides.items():
        if fields is None:
            entries.pop(namespace, None)
            continue
        if not isinstance(fields, dict) or set(fields) - _ALLOWED_FIELDS:
            logging.getLogger().error(
                f"ENRICHMENT_REGISTRY_JSON: entry '{namespace}' ignored, expected an object with fields {sorted(_ALLOWED_FIELDS)}"
            )
            continue
        if namespace in entries:
            candidate = replace(entries[namespace], **fields)
        elif "resource_type" in fields:
            candidate = RegistryEntry(**fields)
        else:
            logging.getLogger().error(f"ENRICHMENT_REGISTRY_JSON: new entry '{namespace}' needs a resource_type; ignored")
            continue
        problem = _validate(namespace, candidate)
        if problem:
            logging.getLogger().error(f"ENRICHMENT_REGISTRY_JSON: entry '{namespace}' ignored, {problem}")
            continue
        entries[namespace] = candidate
    return entries
