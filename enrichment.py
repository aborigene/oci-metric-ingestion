"""Name enrichment: add oci.resource_display_name and oci.compartment_name to generic-path metrics.

The OCI Monitoring payload only carries OCIDs. Names are looked up with the OCI Search service (resource
principal), in batches, before the MINT lines are built, so a name is present from the first sighting.

Design constraints (the OCI APIs are throttled per tenancy, limits are not published, and 429 responses carry
no Retry-After):
  * one paginated search per (region, compartment, resource type) group, and ALL returned resources fill the
    memo, not only the requested ones; compartment names use one "query compartment resources" per region;
  * an in-process memo with TTL (+/-20% jitter), a shorter negative TTL and a size cap. It is disposable:
    nothing is persisted, a cold start just triggers lookups;
  * guardrails: a cap on calls and on seconds per invocation, one in-flight lookup per group, a circuit breaker
    (3 consecutive 429/5xx/timeouts -> skip lookups for 60s, doubling up to 600s) and a bounded SDK retry;
  * a lookup failure NEVER raises and NEVER blocks ingest: the OCID is used as the name and a structured
    warning with counters is logged.
"""
import json
import logging
import os
import random
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from generic import clean_value, normalise_dimensions
from registry import (
    COMPARTMENT_QUERY, OCID_PATTERN, RegistryEntry, load_registry,
)

DEFAULT_TTL_SECONDS = 900
DEFAULT_NEGATIVE_TTL_SECONDS = 120
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_MAX_CALLS = 5
DEFAULT_MAX_SECONDS = 10
TTL_JITTER = 0.2

BREAKER_FAILURE_THRESHOLD = 3
BREAKER_BASE_SECONDS = 60
BREAKER_MAX_SECONDS = 600

SDK_RETRY_MAX_ATTEMPTS = 3
SDK_RETRY_TOTAL_SECONDS = 10
SDK_RETRY_MAX_WAIT_SECONDS = 5
SDK_CONNECT_TIMEOUT = 5
SDK_READ_TIMEOUT = 8


def _env_number(name: str, default, cast=float, minimum=0):
    try:
        value = cast(os.environ.get(name, default))
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


@dataclass
class EnrichmentConfig:
    enabled: bool = True
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_calls: int = DEFAULT_MAX_CALLS
    max_seconds: float = DEFAULT_MAX_SECONDS

    @classmethod
    def from_env(cls) -> "EnrichmentConfig":
        return cls(
            enabled=os.environ.get("ENRICH_NAMES", "true").strip().lower() != "false",
            ttl_seconds=_env_number("NAME_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS, float, 1),
            negative_ttl_seconds=_env_number("NAME_CACHE_NEGATIVE_TTL_SECONDS", DEFAULT_NEGATIVE_TTL_SECONDS, float, 0),
            max_entries=_env_number("NAME_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES, int, 1),
            max_calls=_env_number("MAX_LOOKUP_CALLS_PER_INVOCATION", DEFAULT_MAX_CALLS, int, 0),
            max_seconds=_env_number("MAX_LOOKUP_SECONDS_PER_INVOCATION", DEFAULT_MAX_SECONDS, float, 0),
        )


# ---------------------------------------------------------------------------------------------- memo

class NameMemo:
    """(region, ocid) -> (name or None, expires_at). None is a negative entry. Oldest entries are evicted first."""

    def __init__(self, clock, rand, ttl: float, negative_ttl: float, max_entries: int):
        self._clock, self._rand = clock, rand
        self._ttl, self._negative_ttl, self._max = ttl, negative_ttl, max_entries
        self._data: "OrderedDict[Tuple[str, str], Tuple[Optional[str], float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _expiry(self, base: float) -> float:
        return self._clock() + base * (1 + TTL_JITTER * (2 * self._rand() - 1))

    def get(self, key) -> Tuple[bool, Optional[str]]:
        """(found, name). An expired entry counts as not found and is dropped."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            if self._clock() >= entry[1]:
                del self._data[key]
                return False, None
            return True, entry[0]

    def put(self, key, name: Optional[str]):
        expires = self._expiry(self._ttl if name is not None else self._negative_ttl)
        with self._lock:
            self._data[key] = (name, expires)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)


# ------------------------------------------------------------------------------------- circuit breaker

class CircuitBreaker:
    """Opens after BREAKER_FAILURE_THRESHOLD consecutive transient failures and stays open for a delay that
    doubles on every re-opening (60s, 120s, ... capped at 600s). One success closes it and resets the delay."""

    def __init__(self, clock):
        self._clock = clock
        self._consecutive = 0
        self._delay = BREAKER_BASE_SECONDS
        self._open_until: Optional[float] = None

    def allow(self) -> bool:
        return self._open_until is None or self._clock() >= self._open_until

    def record_success(self):
        self._consecutive = 0
        self._delay = BREAKER_BASE_SECONDS
        self._open_until = None

    def record_failure(self):
        self._consecutive += 1
        if self._consecutive >= BREAKER_FAILURE_THRESHOLD:
            self._open_until = self._clock() + self._delay
            self._delay = min(self._delay * 2, BREAKER_MAX_SECONDS)

    @property
    def state(self) -> str:
        if self.allow():
            return "closed" if self._consecutive < BREAKER_FAILURE_THRESHOLD else "half-open"
        return f"open({self._open_until - self._clock():.0f}s)"


_TRANSIENT_NAMES = {
    "RequestException", "ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError", "CircuitBreakerError",
}


def is_transient(ex: Exception) -> bool:
    """429, 5xx and timeouts/connection errors count towards the circuit breaker; everything else does not."""
    status = getattr(ex, "status", None)  # oci.exceptions.ServiceError
    if isinstance(status, int):
        return status == 429 or status >= 500
    return isinstance(ex, (TimeoutError, ConnectionError)) or type(ex).__name__ in _TRANSIENT_NAMES


# ------------------------------------------------------------------------------------------- resolver

class ResourceWant(NamedTuple):
    region: str
    compartment_id: str
    entry: RegistryEntry
    ocid: str


class NameResolver:
    """`searcher.search(region, query, page) -> (items, next_page)`; items expose .identifier and .display_name."""

    def __init__(self, config: EnrichmentConfig, searcher, clock=time.monotonic, rand=random.random):
        self.config = config
        self.searcher = searcher
        self._clock = clock
        self.memo = NameMemo(clock, rand, config.ttl_seconds, config.negative_ttl_seconds, config.max_entries)
        self.breaker = CircuitBreaker(clock)
        self._group_locks: Dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.counters: Counter = Counter()
        self._started = 0.0
        self._calls = 0
        self._last_error = ""

    # -- per invocation
    def begin_invocation(self):
        self.counters = Counter()
        self._calls = 0
        self._started = self._clock()

    def lookup(self, region: Optional[str], ocid: Optional[str]) -> Optional[str]:
        """The memoised name, or None when unknown, expired or negatively cached."""
        if not region or not ocid:
            return None
        return self.memo.get((region, ocid))[1]

    def resolve(self, resources: Iterable[ResourceWant], compartments: Iterable[Tuple[str, str]]):
        """Look up every name that is not in the memo. Never raises."""
        try:
            self._resolve(resources, compartments)
        except Exception as ex:  # a bug here must never block ingest
            self.counters["errors"] += 1
            logging.getLogger().warning(f"name enrichment skipped after an unexpected error: {type(ex).__name__}: {ex}")

    def _resolve(self, resources, compartments):
        resource_groups: Dict[tuple, Tuple[str, str, set]] = {}
        for want in resources:
            if not OCID_PATTERN.match(want.compartment_id or ""):
                continue
            key = (want.region, want.compartment_id, want.entry.resource_type, want.entry.query)
            query = want.entry.build_query(want.compartment_id)
            resource_groups.setdefault(key, (want.region, query, set()))[2].add(want.ocid)
        compartment_groups: Dict[tuple, Tuple[str, str, set]] = {}
        for region, ocid in compartments:
            compartment_groups.setdefault(("compartment", region), (region, COMPARTMENT_QUERY, set()))[2].add(ocid)

        for key, (region, query, ocids) in list(compartment_groups.items()) + list(resource_groups.items()):
            missing = set()
            for ocid in ocids:
                if self.memo.get((region, ocid))[0]:
                    self.counters["hits"] += 1
                else:
                    self.counters["misses"] += 1
                    missing.add(ocid)
            if missing:
                self._run_group(key, region, query, missing)

    def _group_lock(self, key) -> threading.Lock:
        with self._locks_guard:
            return self._group_locks.setdefault(key, threading.Lock())

    def _budget_left(self) -> bool:
        return self._calls < self.config.max_calls and (self._clock() - self._started) < self.config.max_seconds

    def _run_group(self, key, region, query, wanted: set):
        lock = self._group_lock(key)
        if not lock.acquire(blocking=False):  # another lookup for this group is already in flight
            self.counters["skipped_busy"] += 1
            return
        try:
            if not self.breaker.allow():
                self.counters["skipped_breaker"] += 1
                return
            if not self._budget_left():
                self.counters["skipped_cap"] += 1
                return
            found, complete, failed = self._search_all(region, query)
            for ocid, name in found.items():  # ALL returned resources, not only the requested ones
                self.memo.put((region, ocid), name)
            if complete or failed:
                # Not found in a complete result, or the lookup failed: remember that briefly so every
                # invocation does not retry. An incomplete result (budget ran out) is not conclusive.
                for ocid in wanted - set(found):
                    self.memo.put((region, ocid), None)
        finally:
            lock.release()

    def _search_all(self, region, query) -> Tuple[Dict[str, str], bool, bool]:
        """Returns (identifier -> name, complete, failed). Every page is one call against the budget."""
        found: Dict[str, str] = {}
        page = None
        while True:
            if not self._budget_left():
                self.counters["skipped_cap"] += 1
                return found, False, False
            self._calls += 1
            self.counters["calls"] += 1
            try:
                items, page = self.searcher.search(region, query, page)
            except Exception as ex:
                self.counters["failures"] += 1
                transient = is_transient(ex)
                if transient:
                    self.breaker.record_failure()
                self._last_error = f"{type(ex).__name__}: {ex}"[:300]
                logging.getLogger().debug(f"name lookup failed ({'transient' if transient else 'permanent'}): {self._last_error}")
                return found, False, True
            self.breaker.record_success()
            for item in items or []:
                identifier = getattr(item, "identifier", None)
                name = getattr(item, "display_name", None)
                if identifier and name:
                    found[identifier] = name
            if not page:
                return found, True, False

    # -- reporting
    def summary(self) -> Dict:
        data = {k: int(v) for k, v in self.counters.items()}
        for k in ("hits", "misses", "calls", "failures"):
            data.setdefault(k, 0)
        data["breaker"] = self.breaker.state
        data["memo_entries"] = len(self.memo)
        if self.counters.get("failures"):
            data["last_error"] = self._last_error
        return data

    def log_summary(self):
        data = self.summary()
        degraded = any(data.get(k) for k in ("failures", "skipped_breaker", "skipped_cap", "skipped_busy", "errors"))
        if degraded:
            logging.getLogger().warning(f"name enrichment degraded, using OCIDs as names where unresolved: {json.dumps(data)}")
        elif data["calls"] or data["misses"]:
            logging.getLogger().info(f"name enrichment: {json.dumps(data)}")


# --------------------------------------------------------------------------------------------- enricher

class Enricher:
    """Collects the lookups of one invocation (prepare), then adds the names to the dimensions (apply)."""

    def __init__(self, registry: Dict[str, RegistryEntry], resolver: NameResolver):
        self.registry = registry
        self.resolver = resolver

    @staticmethod
    def _context(body: Dict):
        dims = normalise_dimensions(body)
        return dims, dims.get("oci.region"), dims.get("oci.compartment_id")

    def _resource_id(self, body: Dict, entry: RegistryEntry) -> Optional[str]:
        value = (body.get("dimensions") or {}).get(entry.id_dimension)
        return str(value) if value else None

    def prepare(self, bodies: Iterable[Dict]):
        """Resolve every missing name for these (generic-path) payloads BEFORE any line is built."""
        self.resolver.begin_invocation()
        resources: List[ResourceWant] = []
        compartments: List[Tuple[str, str]] = []
        for body in bodies:
            dims, region, compartment = self._context(body)
            if not region:
                continue  # the region picks the client; without it nothing can be looked up
            if compartment:
                compartments.append((region, compartment))
            entry = self.registry.get(body.get("namespace"))
            if entry and compartment and entry.name_dimension not in dims:
                ocid = self._resource_id(body, entry)
                if ocid:
                    resources.append(ResourceWant(region, compartment, entry, ocid))
        self.resolver.resolve(resources, compartments)
        self.resolver.log_summary()

    def apply(self, body: Dict, dims: Dict[str, str]) -> Dict[str, str]:
        """Add oci.compartment_name and, for registered namespaces, the resource name. OCID when unresolved."""
        out = dict(dims)
        region, compartment = dims.get("oci.region"), dims.get("oci.compartment_id")
        if compartment:
            name = clean_value(self.resolver.lookup(region, compartment)) or clean_value(compartment)
            if name:
                out["oci.compartment_name"] = name
        entry = self.registry.get(body.get("namespace"))
        if entry and entry.name_dimension not in dims:
            ocid = self._resource_id(body, entry)
            if ocid:
                name = clean_value(self.resolver.lookup(region, ocid)) or clean_value(ocid)
                if name:
                    out[entry.name_dimension] = name
        return out


# ---------------------------------------------------------------------------------------------- OCI SDK

def bounded_retry_strategy(oci):
    """SDK retries for every OCI call of this function: at most 3 attempts and 10s in total, exponential backoff
    with full jitter, on 429 and 5xx (plus timeouts and connection errors, which the SDK always retries)."""
    return oci.retry.RetryStrategyBuilder(
        max_attempts_check=True, max_attempts=SDK_RETRY_MAX_ATTEMPTS,
        total_elapsed_time_check=True, total_elapsed_time_seconds=SDK_RETRY_TOTAL_SECONDS,
        service_error_check=True, service_error_retry_config={429: []}, service_error_retry_on_any_5xx=True,
        retry_max_wait_between_calls_seconds=SDK_RETRY_MAX_WAIT_SECONDS,
        backoff_type=oci.retry.BACKOFF_FULL_JITTER_VALUE,
    ).get_retry_strategy()


class OciSearcher:
    """Calls the OCI Search service with the function's resource principal. The `oci` SDK is imported lazily so
    the function runs (without names) even where it is missing, and ENRICH_NAMES=false never loads it."""

    def __init__(self):
        self._signer = None
        self._clients: Dict[str, object] = {}
        self._retry = None
        self._lock = threading.Lock()

    def _client(self, region: str):
        with self._lock:
            if region not in self._clients:
                import oci  # noqa: PLC0415
                if self._signer is None:
                    self._signer = oci.auth.signers.get_resource_principals_signer()
                if self._retry is None:
                    self._retry = bounded_retry_strategy(oci)
                self._clients[region] = oci.resource_search.ResourceSearchClient(
                    {"region": region}, signer=self._signer, timeout=(SDK_CONNECT_TIMEOUT, SDK_READ_TIMEOUT),
                )
            return self._clients[region]

    def search(self, region: str, query: str, page: Optional[str] = None):
        import oci  # noqa: PLC0415
        client = self._client(region)
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query, matching_context_type="NONE",
        )
        kwargs = {"retry_strategy": self._retry}
        if page:
            kwargs["page"] = page
        response = client.search_resources(details, **kwargs)
        return list(response.data.items or []), response.next_page


# ------------------------------------------------------------------------------------------ process-wide

_enricher: Optional[Enricher] = None
_initialised = False
_init_lock = threading.Lock()


def get_enricher() -> Optional[Enricher]:
    """The process-wide enricher (its memo survives between invocations of a warm container), or None when
    ENRICH_NAMES=false."""
    global _enricher, _initialised
    with _init_lock:
        if not _initialised:
            config = EnrichmentConfig.from_env()
            if config.enabled:
                _enricher = Enricher(load_registry(), NameResolver(config, OciSearcher()))
            _initialised = True
        return _enricher


def reset_state():
    global _enricher, _initialised
    with _init_lock:
        _enricher, _initialised = None, False
