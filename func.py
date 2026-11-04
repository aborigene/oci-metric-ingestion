import io
import os
import json
import logging
import re
from typing import Dict, List, Optional
from aggregation import create_minutely_buckets
from delivery import DeliveryError, send_lines
from dynatrace_client import DynatraceClient
from mint import MintMetric
from summary_stat import SummaryStat
from metric_mapping import namespace_map
from urllib.parse import quote


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() == "true"


def process_metrics(body: Dict) -> List[str]:
    """Turn one OCI Monitoring payload into MINT lines. Nothing is sent from here."""
    logging.getLogger().debug(f"process_metrics: {body}")

    namespace = body.get("namespace")
    metric_name = body.get("name")

    oci_dimensions: Dict[str, str] = dict(body.get("dimensions") or {})

    # Include the resourceGroup and compartmentId in oci_dimensions so they can be mapped using the dimension_mapping
    oci_dimensions["resourceGroup"] = body.get("resourceGroup")
    oci_dimensions["compartmentId"] = body.get("compartmentId")

    datapoints = body.get("datapoints")

    import_all_metrics = _env_bool("IMPORT_ALL_METRICS")
    logging.getLogger().debug(f"import_all_metrics: {import_all_metrics}")

    lines: List[str] = []
    if import_all_metrics:
        key_namespace = namespace.replace("oci_", "")
        key = f"cloud.oci.{key_namespace}.{metric_name}"

        buckets = create_minutely_buckets(datapoints)
        aggregated = {}
        for timestamp, values in sorted(buckets.items()):
            aggregated[timestamp] = SummaryStat(
                min(values), max(values), sum(values), len(values)
            )

        for timestamp, summary_stat in aggregated.items():
            mint_metric = MintMetric(
                key,
                summary_stat,
                {
                    "oci.resource_group": oci_dimensions.get("resourceGroup"),
                    "oci.compartment_id": oci_dimensions.get("compartmentId"),
                },
                timestamp * 1000,
            )
            logging.getLogger().debug(f"mint_metric: {mint_metric}")
            lines.append(str(mint_metric))
    else:
        metric_map = namespace_map.get(namespace)
        if metric_map is None:
            logging.getLogger().error(
                f"Could not find a metric mapping for namespace '{namespace}'"
            )
            return lines
        if value_or_none := metric_map.value_from_oci_metric_name(
            metric_name, oci_dimensions, datapoints
        ):
            dynatrace_metric_key, results = value_or_none
            dimensions = metric_map.dimensions(oci_dimensions)

            for result in results:
                lines.append(str(MintMetric(
                    dynatrace_metric_key,
                    result.value,
                    dimensions,
                    result.timestamp * 1000,
                )))
            # Historical behaviour: only the most recent minute of a payload is sent for curated
            # namespaces. CURATED_SEND_ALL_BUCKETS=true sends every minute (see CHANGES.md).
            if not _env_bool("CURATED_SEND_ALL_BUCKETS"):
                lines = lines[-1:]
            for line in lines:
                logging.getLogger().debug(f"process_metrics: Mint Metric: {line}")
        else:
            logging.getLogger().debug(
                f"Could not find a mapping for metric '{metric_name}' in namespace '{namespace}'"
            )
    return lines


def build_client() -> DynatraceClient:
    """Create the Dynatrace client from the function configuration. Raises ValueError if it is incomplete."""
    try:
        tenant_url = os.environ["DYNATRACE_TENANT"]
        auth_method = os.environ["AUTH_METHOD"]
    except KeyError as ex:
        raise ValueError(f"Missing configuration value {ex}") from None

    proxy_url = create_proxy_connection()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    client = DynatraceClient(tenant_url, proxies=proxies)
    try:
        if auth_method == "oauth":
            client.using_oauth(
                os.environ["OAUTH_CLIENT_ID"],
                os.environ["OAUTH_CLIENT_SECRET"],
                os.environ["OAUTH_ACCOUNT_URN"],
            )
        elif auth_method == "token":
            client.using_api_token(os.environ["DYNATRACE_API_KEY"])
        else:
            raise ValueError(f"Invalid authentication method '{auth_method}'. Expected either 'oauth' or 'token'")
    except KeyError as ex:
        raise ValueError(f"Missing configuration value {ex} for AUTH_METHOD '{auth_method}'") from None
    return client


_PLACEHOLDER = re.compile(r"^<.*>$")


def _env_value(name: str) -> Optional[str]:
    """An environment value, or None when it is empty or still the "<placeholder>" from func.yaml."""
    value = (os.environ.get(name) or "").strip()
    return None if not value or _PLACEHOLDER.match(value) else value


def create_proxy_connection() -> Optional[str]:
    """Return the proxy URL including credentials, or None when no usable PROXY_URL is configured."""
    proxy_address = _env_value("PROXY_URL")
    proxy_username = _env_value("PROXY_USERNAME")
    proxy_password = _env_value("PROXY_PASSWORD")

    # An empty value or the untouched func.yaml placeholder ("<Proxy URL>") means "no proxy".
    if proxy_address is None:
        return None
    if "://" not in proxy_address:
        logging.getLogger().error("PROXY_URL is not of the form <protocol>://<host>:<port>; ignoring it")
        return None

    protocol, address = proxy_address.split("://", 1)
    proxy_url = f"{protocol}://"
    proxy_url += _create_user_pass_url(proxy_username, proxy_password)
    proxy_url += f"{address}"
    return proxy_url


def _create_user_pass_url(proxy_username: str, proxy_password: str) -> str:
    user_pass_url = None
    if proxy_username:
        proxy_username = quote(proxy_username, safe="")
        user_pass_url = proxy_username
        if proxy_password:
            proxy_password = quote(proxy_password, safe="")
            user_pass_url += f":{proxy_password}"
        user_pass_url += "@"
    return user_pass_url if user_pass_url else ""


def handler(ctx, data: io.BytesIO = None):
    logging.getLogger().setLevel(str(os.environ.get("LOG_LEVEL", "INFO")).upper())

    try:
        raw = data.getvalue()
        logging.getLogger().debug(raw)
        body = json.loads(raw)
    except (Exception, ValueError) as ex:
        # A payload that cannot be parsed will never parse: do not make Connector Hub redeliver it.
        logging.getLogger().error(f"Could not parse the request body: {ex}")
        return

    # Batch of CloudEvents format, or a single CloudEvent
    items = body if isinstance(body, list) else [body]

    lines: List[str] = []
    invalid_items = 0
    for item in items:
        try:
            lines.extend(process_metrics(item))
        except Exception as ex:  # one bad metric must not drop the rest of the batch
            invalid_items += 1
            logging.getLogger().error(f"Skipping metric that could not be processed: {type(ex).__name__}: {ex}")
    logging.getLogger().info(f"parsed: items={len(items)} invalid_items={invalid_items} lines={len(lines)}")

    if not lines:
        return

    if _env_bool("DRY_RUN"):
        for line in lines:
            logging.getLogger().info(f"DRY_RUN {line}")
        return

    try:
        client = build_client()
    except ValueError as ex:
        logging.getLogger().error(f"Cannot send metrics: {ex}")
        return

    # Raises DeliveryError when a chunk still failed with a transient error (429/5xx/timeout) after the
    # retries, so the invocation fails and Connector Hub redelivers the batch.
    send_lines(lines, client)
