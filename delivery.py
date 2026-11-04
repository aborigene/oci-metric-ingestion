"""Batched, retried delivery of MINT lines to the Dynatrace Metrics v2 ingest API.

Error policy (Connector Hub delivers at-least-once and blocks later batches until a call succeeds):
  * 2xx                      -> done.
  * 429, 5xx, timeouts       -> retried with jittered exponential backoff, bounded by an attempt
                                limit and a time budget; if still failing the chunk counts as failed.
  * any other 4xx            -> the status and response body are logged, the chunk is dropped and
                                delivery continues (retrying cannot help).
  * every chunk is attempted; only after all of them, a failure of a transient kind raises
    DeliveryError so that the invocation fails and the batch is redelivered.
"""
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests

from dynatrace_client import AuthError, DynatraceClient

DEFAULT_MAX_PAYLOAD_BYTES = 900_000  # the API limit is 1 MB
DEFAULT_MAX_LINES_PER_REQUEST = 500
DEFAULT_RETRY_BUDGET_SECONDS = 45
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 10.0
BODY_LOG_LIMIT = 2000


class DeliveryError(Exception):
    """Some chunks could not be delivered because of a transient failure (429, 5xx, timeout)."""


@dataclass
class DeliveryStats:
    lines_in: int = 0  # lines handed to delivery
    lines_sent: int = 0  # lines accepted by Dynatrace
    lines_rejected: int = 0  # lines dropped by Dynatrace (HTTP 4xx or linesInvalid) or by a permanent auth failure
    retries: int = 0  # extra attempts made after a transient failure
    chunks: int = 0
    chunks_failed: int = 0  # chunks still failing with a transient error after the retries
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"lines_in={self.lines_in} lines_sent={self.lines_sent} lines_rejected={self.lines_rejected} "
            f"retries={self.retries} chunks={self.chunks} chunks_failed={self.chunks_failed}"
        )


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def chunk_lines(lines: List[str], max_bytes: int, max_lines: int) -> List[List[str]]:
    """Split lines into chunks of at most `max_lines` lines and `max_bytes` bytes (UTF-8, newline-joined).

    A single line larger than `max_bytes` is returned alone in its own chunk; the API will reject it
    and the rejection is logged, it never blocks the other lines.
    """
    chunks: List[List[str]] = []
    current: List[str] = []
    current_bytes = 0
    for line in lines:
        size = len(line.encode("utf-8")) + (1 if current else 0)  # +1 for the joining newline
        if current and (len(current) >= max_lines or current_bytes + size > max_bytes):
            chunks.append(current)
            current, current_bytes = [], 0
            size = len(line.encode("utf-8"))
        current.append(line)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    value = response.headers.get("Retry-After") if response.headers else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _log_rejection(chunk_size: int, status: int, body: str):
    logging.getLogger().error(
        f"Dynatrace rejected {chunk_size} lines with HTTP {status}: {body[:BODY_LOG_LIMIT]}"
    )


def send_lines(
    lines: List[str],
    client: DynatraceClient,
    max_bytes: Optional[int] = None,
    max_lines: Optional[int] = None,
    retry_budget_seconds: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    rand: Optional[Callable[[], float]] = None,
) -> DeliveryStats:
    """Send all lines in chunks; raise DeliveryError at the end if a chunk failed transiently.

    `clock`, `sleep` and `rand` can be injected (tests); they default to time.monotonic, time.sleep and random.random.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    rand = rand or random.random
    max_bytes = max_bytes or _env_int("MAX_PAYLOAD_BYTES", DEFAULT_MAX_PAYLOAD_BYTES)
    max_lines = max_lines or _env_int("MAX_LINES_PER_REQUEST", DEFAULT_MAX_LINES_PER_REQUEST)
    if retry_budget_seconds is None:
        retry_budget_seconds = _env_int("RETRY_BUDGET_SECONDS", DEFAULT_RETRY_BUDGET_SECONDS)

    stats = DeliveryStats(lines_in=len(lines))
    deadline = clock() + retry_budget_seconds

    for chunk in chunk_lines(lines, max_bytes, max_lines):
        stats.chunks += 1
        if not _send_chunk(chunk, client, stats, deadline, clock, sleep, rand):
            stats.chunks_failed += 1

    logging.getLogger().info(f"delivery: {stats.summary()}")
    if stats.chunks_failed:
        raise DeliveryError(
            f"{stats.chunks_failed} of {stats.chunks} chunks failed with a transient error "
            f"({'; '.join(stats.errors[-3:])})"
        )
    return stats


def _send_chunk(chunk, client, stats, deadline, clock, sleep, rand) -> bool:
    """Deliver one chunk. Returns False only for a transient failure that exhausted its retries."""
    payload = "\n".join(chunk)
    attempt = 0
    refreshed_after_401 = False
    while True:
        attempt += 1
        retry_after = None
        try:
            response = client.post_lines(payload)
        except AuthError as ex:
            if not ex.transient:
                logging.getLogger().error(f"Cannot authenticate, dropping {len(chunk)} lines: {ex}")
                stats.lines_rejected += len(chunk)
                return True
            reason = str(ex)
        except (requests.Timeout, requests.ConnectionError) as ex:
            reason = f"{type(ex).__name__}"
        except requests.RequestException as ex:
            logging.getLogger().error(f"Request failed, dropping {len(chunk)} lines: {type(ex).__name__}: {ex}")
            stats.lines_rejected += len(chunk)
            return True
        else:
            status = response.status_code
            if 200 <= status < 300:
                _count_accepted(response, chunk, stats)
                return True
            if status == 401 and not refreshed_after_401:
                # The cached OAuth token may have been revoked or expired early: get a fresh one and retry once.
                refreshed_after_401 = True
                client.invalidate_token()
                attempt -= 1
                continue
            if status == 429 or status >= 500:
                reason = f"HTTP {status}"
                retry_after = _retry_after_seconds(response)
            else:
                _log_rejection(len(chunk), status, response.text)
                stats.lines_rejected += len(chunk)
                return True

        # Transient failure: back off or give up.
        stats.errors.append(reason)
        if attempt >= MAX_ATTEMPTS:
            logging.getLogger().error(f"Giving up on {len(chunk)} lines after {attempt} attempts: {reason}")
            return False
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)) * (0.5 + rand())
        if retry_after is not None:
            delay = max(delay, retry_after)
        if clock() + delay > deadline:
            logging.getLogger().error(f"Retry budget exhausted, giving up on {len(chunk)} lines: {reason}")
            return False
        logging.getLogger().warning(f"{reason}; retrying in {delay:.1f}s (attempt {attempt + 1} of {MAX_ATTEMPTS})")
        stats.retries += 1
        sleep(delay)


def _count_accepted(response, chunk, stats):
    """The API answers 202 with {"linesOk": n, "linesInvalid": m, "error": {...}} - account for partial rejection."""
    lines_ok, lines_invalid = len(chunk), 0
    try:
        body = response.json()
        if isinstance(body, dict) and "linesOk" in body:
            lines_ok = int(body.get("linesOk", 0))
            lines_invalid = int(body.get("linesInvalid", 0))
    except (ValueError, TypeError):
        pass
    stats.lines_sent += lines_ok
    stats.lines_rejected += lines_invalid
    if lines_invalid:
        logging.getLogger().error(
            f"Dynatrace accepted {lines_ok} and rejected {lines_invalid} lines: "
            f"{json.dumps(body, default=str)[:BODY_LOG_LIMIT]}"
        )
