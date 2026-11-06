import base64
import logging
from types import SimpleNamespace

import pytest

import dynatrace_client
import func
import vault
from conftest import REAL_READ_SECRET, FakeResponse, FakeSession, retry_settings
from delivery import DeliveryError, send_lines
from dynatrace_client import AuthError

SECRET_OCID = "ocid1.vaultsecret.oc1.eu-frankfurt-1.aaaa"


class Reader:
    def __init__(self, *values):
        self.values, self.calls = list(values), 0

    def __call__(self, ocid):
        self.calls += 1
        assert ocid == SECRET_OCID
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, Exception):
            raise value
        return value


NO_SLEEP = dict(sleep=lambda s: None, rand=lambda: 0.5)


def token_client(session, reader):
    return dynatrace_client.DynatraceClient("https://abc.live.dynatrace.com", session=session).using_api_token(
        None, secret_provider=vault.SecretProvider(SECRET_OCID, reader))


def test_secret_is_read_once_and_cached():
    reader = Reader("tok-1")
    provider = vault.SecretProvider(SECRET_OCID, reader)
    assert provider.get() == provider.get() == "tok-1" and reader.calls == 1


def test_api_token_comes_from_vault_and_is_reused_across_requests():
    session, reader = FakeSession([FakeResponse(202, {"linesOk": 1})]), Reader("tok-1")
    client = token_client(session, reader)
    send_lines(["a gauge,1 1", "b gauge,1 1"], client, max_lines=1, **NO_SLEEP)
    assert [c["headers"]["Authorization"] for c in session.ingest_calls()] == ["Api-Token tok-1"] * 2
    assert reader.calls == 1


def test_401_refreshes_the_secret_from_vault_and_retries_once():
    session = FakeSession([FakeResponse(401), FakeResponse(202, {"linesOk": 1})])
    reader = Reader("old-token", "rotated-token")
    stats = send_lines(["a gauge,1 1"], token_client(session, reader), **NO_SLEEP)
    assert stats.lines_sent == 1 and reader.calls == 2
    assert [c["headers"]["Authorization"] for c in session.ingest_calls()] == ["Api-Token old-token", "Api-Token rotated-token"]


def test_persistent_401_does_not_loop(caplog):
    session, reader = FakeSession([FakeResponse(401, text="denied")]), Reader("t1", "t2")
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["a gauge,1 1"], token_client(session, reader), **NO_SLEEP)
    assert len(session.ingest_calls()) == 2 and stats.lines_rejected == 1
    assert "t1" not in caplog.text and "t2" not in caplog.text


def test_vault_throttling_is_transient_and_makes_the_invocation_fail_for_redelivery():
    err = AuthError("vault 429", transient=True)
    with pytest.raises(DeliveryError):
        send_lines(["a gauge,1 1"], token_client(FakeSession(), Reader(err)), **NO_SLEEP)


def test_vault_permission_error_is_permanent_and_logged(caplog):
    err = AuthError("Could not read the secret from OCI Vault: ServiceError: NotAuthorizedOrNotFound", transient=False)
    session = FakeSession()
    with caplog.at_level(logging.ERROR):
        stats = send_lines(["a gauge,1 1"], token_client(session, Reader(err)), **NO_SLEEP)
    assert session.calls == [] and stats.lines_rejected == 1 and "NotAuthorizedOrNotFound" in caplog.text


def test_empty_secret_is_rejected():
    with pytest.raises(AuthError):
        vault.SecretProvider(SECRET_OCID, Reader("")).get()


def test_oauth_client_secret_from_vault_and_reread_when_the_token_endpoint_rejects_it():
    class OAuthSession(FakeSession):
        def __init__(self):
            super().__init__([FakeResponse(202, {"linesOk": 1})])
            self.token_bodies, self.responses = [], [FakeResponse(401, text="invalid_client"),
                                                      FakeResponse(200, {"access_token": "t", "expires_in": 300})]

        def post(self, url, **kw):
            if "sso" in url:
                self.token_bodies.append(kw["data"]["client_secret"])
                return self.responses.pop(0)
            return super().post(url, **kw)

    session, reader = OAuthSession(), Reader("old-secret", "new-secret")
    client = dynatrace_client.DynatraceClient("https://abc.live.dynatrace.com", session=session).using_oauth(
        "client-id", None, "urn:x", secret_provider=vault.SecretProvider(SECRET_OCID, reader))
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)       # rejected: permanent, secret invalidated
    send_lines(["a gauge,1 1"], client, **NO_SLEEP)       # next invocation re-reads the secret
    assert session.token_bodies == ["old-secret", "new-secret"] and reader.calls == 2


# ---------- wiring through build_client ----------

@pytest.fixture
def vault_env(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("DYNATRACE_SECRET_OCID", SECRET_OCID)
    monkeypatch.setattr(vault, "read_secret", Reader("vault-token"))


def test_build_client_token_mode_needs_no_api_key_when_vault_is_set(vault_env, monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "<API Key with the 'metrics.ingest' scope>")  # untouched placeholder
    session = FakeSession()
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: session)
    func.build_client().post_lines("a gauge,1 1")
    assert session.calls[0]["headers"]["Authorization"] == "Api-Token vault-token"


def test_build_client_without_secret_ocid_uses_func_yaml_config(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "from-config")
    session = FakeSession()
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: session)
    func.build_client().post_lines("a gauge,1 1")
    assert session.calls[0]["headers"]["Authorization"] == "Api-Token from-config"


def test_build_client_oauth_mode_still_needs_client_id_and_urn(vault_env, monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    with pytest.raises(ValueError, match="OAUTH_CLIENT_ID"):
        func.build_client()


def test_placeholder_secret_ocid_is_ignored(monkeypatch):
    monkeypatch.setenv("DYNATRACE_TENANT", "https://abc.live.dynatrace.com")
    monkeypatch.setenv("AUTH_METHOD", "token")
    monkeypatch.setenv("DYNATRACE_API_KEY", "from-config")
    monkeypatch.setenv("DYNATRACE_SECRET_OCID", "<Vault secret OCID>")
    session = FakeSession()
    monkeypatch.setattr(dynatrace_client, "get_session", lambda: session)
    func.build_client().post_lines("a gauge,1 1")
    assert session.calls[0]["headers"]["Authorization"] == "Api-Token from-config"


# ---------- the real reader, offline ----------

def test_read_secret_decodes_base64_with_the_resource_principal(monkeypatch):
    oci = pytest.importorskip("oci")
    from cryptography.hazmat.primitives.asymmetric import rsa

    signer = oci.auth.signers.SecurityTokenSigner("fake.token", rsa.generate_private_key(65537, 2048))
    signer.region = "eu-frankfurt-1"
    monkeypatch.setattr(oci.auth.signers, "get_resource_principals_signer", lambda: signer)
    seen = {}

    def fake_get(self, secret_id, **kwargs):
        seen.update(endpoint=self.base_client.endpoint, secret_id=secret_id, retry=kwargs.get("retry_strategy"))
        content = base64.b64encode(b"  my-token\n").decode()
        return SimpleNamespace(data=SimpleNamespace(secret_bundle_content=SimpleNamespace(content=content)))

    monkeypatch.setattr(oci.secrets.SecretsClient, "get_secret_bundle", fake_get)

    assert REAL_READ_SECRET(SECRET_OCID) == "my-token"
    assert seen["endpoint"].startswith("https://secrets.vaults.eu-frankfurt-1.oci.oraclecloud.com")
    assert seen["secret_id"] == SECRET_OCID and retry_settings(seen["retry"])["max_attempts"] == 3


def test_read_secret_failure_becomes_an_auth_error_without_leaking(monkeypatch):
    oci = pytest.importorskip("oci")

    def boom():
        raise RuntimeError("not running in a function")

    monkeypatch.setattr(oci.auth.signers, "get_resource_principals_signer", boom)
    with pytest.raises(AuthError) as info:
        REAL_READ_SECRET(SECRET_OCID)
    assert info.value.transient is False and "not running in a function" in str(info.value)
