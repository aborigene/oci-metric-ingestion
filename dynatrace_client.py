import logging
import threading
import time
from typing import Callable, Dict, Optional

import requests

METRIC_INGEST_ENDPOINT = "/api/v2/metrics/ingest"
OAUTH_TOKEN_URL = "https://sso.dynatrace.com/sso/oauth2/token"
OAUTH_SCOPE = "storage:metrics:write"

# A cached token is refreshed this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15


class AuthError(Exception):
    """Raised when no access token can be obtained. `transient` tells the caller whether a retry can help."""

    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


# One process-wide session (connection reuse across invocations of a warm container).
_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


# OAuth tokens are cached at module level so a warm container does not ask for a new one per call.
# Keyed by (client_id, urn); the value is (access_token, expires_at_epoch_seconds).
_token_cache: Dict[tuple, tuple] = {}
_token_lock = threading.Lock()


def reset_state():
    """Forget the shared session and cached tokens (used by tests)."""
    global _session
    _session = None
    _token_cache.clear()


class OAuthAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: Optional[str],
        urn: str,
        session: Optional[requests.Session] = None,
        proxies: Optional[Dict[str, str]] = None,
        clock: Callable[[], float] = time.time,
        secret_provider=None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._secret_provider = secret_provider  # reads the client secret from OCI Vault when set
        self._urn = urn
        self._session = session or get_session()
        self._proxies = proxies
        self._clock = clock
        self._cache_key = (client_id, urn)

    def _is_fresh(self, entry) -> bool:
        return entry is not None and self._clock() < entry[1] - TOKEN_REFRESH_MARGIN_SECONDS

    def authorization_header(self) -> str:
        with _token_lock:
            entry = _token_cache.get(self._cache_key)
            if not self._is_fresh(entry):
                entry = self._request_token()
                _token_cache[self._cache_key] = entry
        return f"Bearer {entry[0]}"

    def invalidate(self):
        """Drop the cached token, for example after a 401."""
        with _token_lock:
            _token_cache.pop(self._cache_key, None)

    def _request_token(self) -> tuple:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._secret_provider.get() if self._secret_provider else self._client_secret,
            "resource": self._urn,
            "scope": OAUTH_SCOPE,
        }
        try:
            response = self._session.post(
                OAUTH_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxies=self._proxies,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as ex:
            raise AuthError(f"OAuth token request failed: {type(ex).__name__}", transient=True) from ex
        if response.status_code != 200:
            if self._secret_provider and response.status_code in (400, 401):
                self._secret_provider.invalidate()  # the secret may have been rotated: read it again next time
            transient = response.status_code == 429 or response.status_code >= 500
            # The body of a failed token request does not contain secrets; the request body (which does) is never logged.
            raise AuthError(
                f"OAuth token request returned HTTP {response.status_code}: {response.text[:500]}",
                transient=transient,
            )
        payload = response.json()
        return payload["access_token"], self._clock() + float(payload.get("expires_in", 300))


class ApiTokenAuth:
    def __init__(self, api_token: Optional[str], secret_provider=None):
        self._api_token = api_token
        self._secret_provider = secret_provider  # reads the token from OCI Vault when set

    def authorization_header(self) -> str:
        token = self._secret_provider.get() if self._secret_provider else self._api_token
        return f"Api-Token {token}"

    def invalidate(self):
        if self._secret_provider:
            self._secret_provider.invalidate()  # a 401 may mean the token was rotated: read it again


class DynatraceClient:
    """Posts MINT payloads to the Metrics v2 ingest endpoint of one tenant."""

    def __init__(self, tenant: str, session: Optional[requests.Session] = None, proxies: Optional[Dict[str, str]] = None):
        self._tenant = tenant.rstrip("/")
        self._session = session or get_session()
        self._proxies = proxies
        self._auth = None

    def using_oauth(self, client_id: str, client_secret: Optional[str], urn: str,
                    clock: Callable[[], float] = time.time, secret_provider=None):
        self._auth = OAuthAuth(client_id, client_secret, urn, self._session, self._proxies, clock, secret_provider)
        return self

    def using_api_token(self, api_token: Optional[str], secret_provider=None):
        self._auth = ApiTokenAuth(api_token, secret_provider)
        return self

    def invalidate_token(self):
        self._auth.invalidate()

    def post_lines(self, payload: str) -> requests.Response:
        """Send one payload. Raises AuthError or requests.RequestException; never inspects the status code."""
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Authorization": self._auth.authorization_header(),
        }
        return self._session.post(
            f"{self._tenant}{METRIC_INGEST_ENDPOINT}",
            data=payload.encode("utf-8"),
            headers=headers,
            proxies=self._proxies,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
