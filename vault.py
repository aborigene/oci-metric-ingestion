"""Optional: read the Dynatrace token / OAuth client secret from OCI Vault instead of func.yaml.

Set DYNATRACE_SECRET_OCID to the OCID of a secret in OCI Vault. The function reads it with its resource principal:
  AUTH_METHOD=token  -> the secret's content is the API token
  AUTH_METHOD=oauth  -> the secret's content is the OAuth client secret (client id and URN stay in func.yaml)

The value is cached for the life of the process and read again after the cache is invalidated (a 401 from the API
token, or the OAuth token endpoint rejecting the client secret). The value is never logged.
"""
import base64
import logging
import threading
from typing import Callable, Dict, Optional

from dynatrace_client import AuthError
from enrichment import bounded_retry_strategy, is_transient

_cache: Dict[str, str] = {}
_lock = threading.Lock()


def reset_state():
    _cache.clear()


def read_secret(secret_ocid: str) -> str:
    """Read the CURRENT version of a Vault secret. Raises AuthError (transient flag set) on any failure."""
    try:
        import oci  # noqa: PLC0415
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.secrets.SecretsClient({"region": signer.region}, signer=signer, timeout=(5, 8))
        bundle = client.get_secret_bundle(secret_ocid, retry_strategy=bounded_retry_strategy(oci)).data
        content = bundle.secret_bundle_content.content
        return base64.b64decode(content).decode("utf-8").strip()
    except Exception as ex:
        # Only the exception type and message of the SDK are reported, never the secret itself.
        raise AuthError(f"Could not read the secret from OCI Vault: {type(ex).__name__}: {str(ex)[:200]}",
                        transient=is_transient(ex)) from ex


class SecretProvider:
    def __init__(self, secret_ocid: str, reader: Optional[Callable[[str], str]] = None):
        self._ocid = secret_ocid
        self._reader = reader or read_secret

    def get(self) -> str:
        with _lock:
            if self._ocid not in _cache:
                value = self._reader(self._ocid)
                if not value:
                    raise AuthError("The Vault secret is empty", transient=False)
                _cache[self._ocid] = value
                logging.getLogger().info("Read the Dynatrace secret from OCI Vault")
            return _cache[self._ocid]

    def invalidate(self):
        with _lock:
            _cache.pop(self._ocid, None)
