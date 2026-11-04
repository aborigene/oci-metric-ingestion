import logging

import pytest
import requests

import dynatrace_client
from conftest import FakeResponse, FakeSession
from delivery import DeliveryError, chunk_lines, send_lines
from dynatrace_client import AuthError, DynatraceClient

NO_SLEEP = dict(sleep=lambda s: None, rand=lambda: 0.5)


def api_client(session):
    return DynatraceClient("https://abc.live.dynatrace.com/", session=session).using_api_token("dt0c01.SECRET")


# ---------- chunking ----------

def test_chunk_by_line_count():
    lines = [f"m{i} gauge,1 1" for i in range(1201)]
    chunks = chunk_lines(lines, max_bytes=10**9, max_lines=500)
    assert [len(c) for c in chunks] == [500, 500, 201]
    assert [l for c in chunks for l in c] == lines


def test_chunk_byte_boundary_is_exact():
    line = "x" * 9  # 9 bytes
    # 3 lines joined by newlines = 9*3 + 2 = 29 bytes -> fits in 29, not in 28
    assert [len(c) for c in chunk_lines([line] * 3, max_bytes=29, max_lines=500)] == [3]
    assert [len(c) for c in chunk_lines([line] * 3, max_bytes=28, max_lines=500)] == [2, 1]
    for chunk in chunk_lines([line] * 10, max_bytes=28, max_lines=500):
        assert len("\n".join(chunk).encode()) <= 28


def test_chunk_counts_utf8_bytes_not_characters():
    line = "é" * 10  # 10 characters, 20 bytes
    assert [len(c) for c in chunk_lines([line, line], max_bytes=30, max_lines=500)] == [1, 1]


def test_oversized_single_line_gets_its_own_chunk():
    chunks = chunk_lines(["a", "b" * 100, "c"], max_bytes=10, max_lines=500)
    assert chunks == [["a"], ["b" * 100], ["c"]]


def test_empty_input_has_no_chunks():
    assert chunk_lines([], 10, 10) == []


# ---------- status handling ----------

def test_success_single_request_for_many_lines():
    session = FakeSession([FakeResponse(202, {"linesOk": 3, "linesInvalid": 0})])
    stats = send_lines(["a gauge,1 1", "b gauge,1 1", "c gauge,1 1"], api_client(session), **NO_SLEEP)
    assert len(session.ingest_calls()) == 1
    assert session.calls[0]["data"] == b"a gauge,1 1\nb gauge,1 1\nc gauge,1 1"
    assert (stats.lines_sent, stats.lines_rejected, stats.retries) == (3, 0, 0)


def test_429_then_success():
    session = FakeSession([FakeResponse(429), FakeResponse(202, {"linesOk": 1, "linesInvalid": 0})])
    sleeps = []
    stats = send_lines(["a gauge,1 1"], api_client(session), sleep=sleeps.append, rand=lambda: 0.5)
    assert len(session.ingest_calls()) == 2
    assert stats.retries == 1 and stats.lines_sent == 1
    assert sleeps == [1.0]  # base 1s * 2**0 * (0.5 + 0.5)


def test_backoff_grows_and_is_jittered():
    session = FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(202, {"linesOk": 1})])
    sleeps = []
    send_lines(["a gauge,1 1"], api_client(session), sleep=sleeps.append, rand=lambda: 0.0)
    assert sleeps == [0.5, 1.0]  # (1*2**0*0.5, 1*2**1*0.5)


def test_retry_after_header_is_honoured():
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(202, {"linesOk": 1})])
    sleeps = []
    send_lines(["a gauge,1 1"], api_client(session), sleep=sleeps.append, rand=lambda: 0.0)
    assert sleeps == [7.0]


def test_persistent_500_raises_only_after_all_chunks_attempted():
    session = FakeSession([FakeResponse(500)])
    lines = [f"m{i} gauge,1 1" for i in range(3)]
    with pytest.raises(DeliveryError):
        send_lines(lines, api_client(session), max_lines=1, **NO_SLEEP)
    # 3 chunks x 3 attempts each: every chunk was tried to the end before raising
    assert len(session.ingest_calls()) == 9
    assert {c["data"] for c in session.ingest_calls()} == {b"m0 gauge,1 1", b"m1 gauge,1 1", b"m2 gauge,1 1"}


def test_failed_chunk_does_not_stop_later_chunks_from_being_sent():
    session = FakeSession([FakeResponse(500)] * 3 + [FakeResponse(202, {"linesOk": 1})])
    with pytest.raises(DeliveryError):
        send_lines(["m0 gauge,1 1", "m1 gauge,1 1"], api_client(session), max_lines=1, **NO_SLEEP)
    assert session.ingest_calls()[-1]["data"] == b"m1 gauge,1 1"


def test_timeout_is_transient():
    session = FakeSession([requests.Timeout("slow")])
    with pytest.raises(DeliveryError):
        send_lines(["a gauge,1 1"], api_client(session), **NO_SLEEP)
    assert len(session.ingest_calls()) == 3


def test_connection_error_is_transient_then_recovers():
    session = FakeSession([requests.ConnectionError("down"), FakeResponse(202, {"linesOk": 1})])
    stats = send_lines(["a gauge,1 1"], api_client(session), **NO_SLEEP)
    assert stats.lines_sent == 1 and stats.retries == 1


def test_400_logs_status_and_body_and_continues(caplog):
    body = {"error": {"code": 400, "message": "metric key invalid"}, "linesOk": 0, "linesInvalid": 1}
    session = FakeSession([FakeResponse(400, body, text="BAD LINE BODY"), FakeResponse(202, {"linesOk": 1})])
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["bad", "good gauge,1 1"], api_client(session), max_lines=1, **NO_SLEEP)  # no raise
    assert len(session.ingest_calls()) == 2  # not retried, next chunk still sent
    assert stats.lines_rejected == 1 and stats.lines_sent == 1 and stats.retries == 0
    assert "HTTP 400" in caplog.text and "BAD LINE BODY" in caplog.text
    assert "dt0c01.SECRET" not in caplog.text


def test_partial_acceptance_is_counted_and_logged(caplog):
    session = FakeSession([FakeResponse(202, {"linesOk": 2, "linesInvalid": 1, "error": {"message": "x"}})])
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["a", "b", "c"], api_client(session), **NO_SLEEP)
    assert (stats.lines_sent, stats.lines_rejected) == (2, 1)
    assert "rejected 1 lines" in caplog.text


def test_retry_budget_stops_retrying_early():
    session = FakeSession([FakeResponse(503)])
    now = [0.0]
    with pytest.raises(DeliveryError):
        send_lines(["a gauge,1 1"], api_client(session), retry_budget_seconds=0.5,
                   clock=lambda: now[0], sleep=lambda s: None, rand=lambda: 0.5)
    assert len(session.ingest_calls()) == 1  # the first backoff (1s) would already exceed the 0.5s budget


def test_retry_budget_is_shared_across_chunks():
    session = FakeSession([FakeResponse(503)])
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(DeliveryError):
        send_lines(["a gauge,1 1", "b gauge,1 1"], api_client(session), max_lines=1, retry_budget_seconds=2.5,
                   clock=lambda: now[0], sleep=sleep, rand=lambda: 0.5)
    # chunk 1: tries at t=0, 1 (sleep 1), then 2nd backoff 2s would end at t=3 > 2.5 -> gives up after 2 tries
    # chunk 2: t=1, next backoff 1s -> t=2 ok, then 2s -> t=4 > 2.5 -> 2 tries
    assert len(session.ingest_calls()) == 4


def test_unexpected_request_exception_drops_chunk_without_raising():
    session = FakeSession([requests.exceptions.InvalidURL("bad")])
    stats = send_lines(["a gauge,1 1"], api_client(session), **NO_SLEEP)
    assert stats.lines_rejected == 1


def test_authorization_header_and_content_type_for_api_token():
    session = FakeSession()
    send_lines(["a gauge,1 1"], api_client(session), **NO_SLEEP)
    call = session.ingest_calls()[0]
    assert call["url"] == "https://abc.live.dynatrace.com/api/v2/metrics/ingest"
    assert call["headers"]["Authorization"] == "Api-Token dt0c01.SECRET"
    assert call["headers"]["Content-Type"].startswith("text/plain")
    assert call["timeout"]


# ---------- OAuth ----------

TOKEN_OK = {"access_token": "tok-1", "expires_in": 300}


def oauth_client(session, now):
    return DynatraceClient("https://abc.live.dynatrace.com", session=session).using_oauth(
        "client", "s3cret", "urn:dtaccount:x", clock=lambda: now[0])


def token_calls(session):
    return [c for c in session.calls if "sso" in c["url"]]


class OAuthSession(FakeSession):
    def __init__(self, tokens, ingest=None):
        super().__init__(ingest or [FakeResponse(202, {"linesOk": 1})])
        self.tokens = list(tokens)

    def post(self, url, **kwargs):
        if "sso" in url:
            self.calls.append({"url": url, **kwargs, "headers": kwargs.get("headers") or {}})
            item = self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]
            if isinstance(item, Exception):
                raise item
            return item
        return super().post(url, **kwargs)


def test_oauth_token_reused_across_chunks_and_invocations():
    now = [1000.0]
    session = OAuthSession([FakeResponse(200, TOKEN_OK)])
    lines = [f"m{i} gauge,1 1" for i in range(4)]
    send_lines(lines, oauth_client(session, now), max_lines=1, **NO_SLEEP)      # 4 requests
    send_lines(lines, oauth_client(session, now), max_lines=2, **NO_SLEEP)      # a "new invocation", new client object
    assert len(session.ingest_calls()) == 6
    assert len(token_calls(session)) == 1
    assert all(c["headers"]["Authorization"] == "Bearer tok-1" for c in session.ingest_calls())


def test_oauth_token_refreshed_60s_before_expiry_not_after():
    now = [1000.0]
    session = OAuthSession([FakeResponse(200, TOKEN_OK), FakeResponse(200, {"access_token": "tok-2", "expires_in": 300})])
    client = oauth_client(session, now)
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)
    now[0] = 1000 + 300 - 61           # 61s left: still fresh (the old code had this inverted)
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)
    assert len(token_calls(session)) == 1
    now[0] = 1000 + 300 - 60           # 60s left: refresh
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)
    assert len(token_calls(session)) == 2
    assert session.ingest_calls()[-1]["headers"]["Authorization"] == "Bearer tok-2"


def test_oauth_401_refreshes_token_and_retries_once_without_backoff():
    now = [1000.0]
    session = OAuthSession(
        [FakeResponse(200, TOKEN_OK), FakeResponse(200, {"access_token": "tok-2", "expires_in": 300})],
        ingest=[FakeResponse(401), FakeResponse(202, {"linesOk": 1})],
    )
    sleeps = []
    stats = send_lines(["a gauge,1 1"], oauth_client(session, now), sleep=sleeps.append, rand=lambda: 0.5)
    assert stats.lines_sent == 1 and sleeps == []
    assert [c["headers"]["Authorization"] for c in session.ingest_calls()] == ["Bearer tok-1", "Bearer tok-2"]


def test_oauth_persistent_401_is_logged_and_dropped_not_looped(caplog):
    now = [1000.0]
    session = OAuthSession([FakeResponse(200, TOKEN_OK)], ingest=[FakeResponse(401, text="denied")])
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["a gauge,1 1"], oauth_client(session, now), **NO_SLEEP)
    assert len(session.ingest_calls()) == 2
    assert stats.lines_rejected == 1
    assert "HTTP 401" in caplog.text and "tok-1" not in caplog.text


def test_oauth_token_endpoint_5xx_is_transient():
    now = [1000.0]
    session = OAuthSession([FakeResponse(503, text="down")])
    with pytest.raises(DeliveryError):
        send_lines(["a gauge,1 1"], oauth_client(session, now), **NO_SLEEP)
    assert session.ingest_calls() == []


def test_oauth_bad_credentials_is_permanent_and_never_sends_bearer_none(caplog):
    now = [1000.0]
    session = OAuthSession([FakeResponse(400, text="invalid_client")])
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["a gauge,1 1"], oauth_client(session, now), **NO_SLEEP)
    assert session.ingest_calls() == []
    assert stats.lines_rejected == 1 and "invalid_client" in caplog.text
    assert "s3cret" not in caplog.text


def test_oauth_token_request_goes_through_proxies_and_has_a_timeout():
    now = [1000.0]
    session = OAuthSession([FakeResponse(200, TOKEN_OK)])
    client = DynatraceClient("https://abc.live.dynatrace.com", session=session,
                             proxies={"https": "http://p:3128"}).using_oauth("c", "s", "u", clock=lambda: now[0])
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)
    assert token_calls(session)[0]["proxies"] == {"https": "http://p:3128"}
    assert token_calls(session)[0]["timeout"]


def test_auth_error_type():
    assert AuthError("x", transient=True).transient is True
