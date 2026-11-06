"""Compartment filtering: decide, from the compartment OCID in a metric payload, whether to ingest it.

Exact OCID matching only (no OCI API calls). Rules:
  * a compartment in COMPARTMENT_DENYLIST is always skipped (deny wins);
  * if COMPARTMENT_ALLOWLIST is set, only the compartments in it are ingested;
  * with neither list set the filter is off and every metric is ingested.
"""
import logging
import os
import re
from collections import Counter
from typing import Dict, FrozenSet, Optional, Tuple

_SEPARATORS = re.compile(r"[,;\s]+")
_PLACEHOLDER = re.compile(r"^<.*>$")


def parse_ocid_list(raw: Optional[str], setting: str) -> FrozenSet[str]:
    """Split a comma/semicolon/whitespace separated list of OCIDs. Empty values and "<placeholders>" are ignored."""
    text = (raw or "").strip()
    if _PLACEHOLDER.match(text):  # untouched func.yaml value such as "<Compartment OCIDs>"
        return frozenset()
    ocids = set()
    for token in _SEPARATORS.split(text):
        if not token:
            continue
        if not token.startswith("ocid1."):
            logging.getLogger().warning(f"{setting}: '{token}' does not look like an OCID and will never match")
        ocids.add(token)
    return frozenset(ocids)


class CompartmentFilter:
    def __init__(self, allow: FrozenSet[str] = frozenset(), deny: FrozenSet[str] = frozenset()):
        self.allow = allow
        self.deny = deny

    @classmethod
    def from_env(cls) -> "CompartmentFilter":
        allow = parse_ocid_list(os.environ.get("COMPARTMENT_ALLOWLIST"), "COMPARTMENT_ALLOWLIST")
        deny = parse_ocid_list(os.environ.get("COMPARTMENT_DENYLIST"), "COMPARTMENT_DENYLIST")
        both = allow & deny
        if both:
            logging.getLogger().warning(f"Compartments in both lists are denied: {sorted(both)}")
        return cls(allow, deny)

    @property
    def active(self) -> bool:
        return bool(self.allow or self.deny)

    def allows(self, compartment_id: Optional[str]) -> Tuple[bool, str]:
        """Return (ingest?, reason). The reason is "" when the metric is ingested."""
        if not self.active:
            return True, ""
        if compartment_id in self.deny:
            return False, "denied"
        if self.allow:
            if not compartment_id:
                return False, "no compartmentId in payload"
            if compartment_id not in self.allow:
                return False, "not in allowlist"
        return True, ""


class FilterStats:
    """Counts what was skipped so a single summary can be logged per invocation."""

    def __init__(self):
        self.skipped: Counter = Counter()  # compartment id -> skipped metrics
        self.reasons: Counter = Counter()

    def add(self, compartment_id: Optional[str], reason: str):
        self.skipped[compartment_id or "<none>"] += 1
        self.reasons[reason] += 1

    @property
    def total(self) -> int:
        return sum(self.skipped.values())

    def summary(self, top: int = 10) -> Dict:
        return {
            "skipped_items": self.total,
            "reasons": dict(self.reasons),
            "compartments": dict(self.skipped.most_common(top)),
        }
