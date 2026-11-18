# Purpose
See https://www.dynatrace.com/hub/detail/oracle-connector-hub-signals-ingest/

# What's new and changes
See [CHANGELOG](CHANGELOG.md). Subscribe to watch CHANGELOG to get notified about new releases, fixes and patches to this project. 


# Getting Started
**Note:** These actions must be done by a OCI tenancy administrator using the Oracle Cloud Shell or Oracle Code Editor.
Policies must also be configured in the OCI tenant to allow the Service Connector read metrics from the tenant. Users should do this using a group where the resource type is `serviceconnectors` before assigning the following policy: 
`Allow group <GROUP_NAME> to read metrics in tenancy`

## Setup the Application & OCI Function
1. Login to the OCI portal and navigate to **Applications** and click on 'Create Application'. 
2. Enter a name for the application and select a subnet for this application. Change the **Shape** to *GENERIC_ARM* then click on 'Create'
![alt text](images/image.png)
3. Once the application is created you will be rediected to the **Getting started** page. Launch the cloud shell and follow the instructions under **Setup fn CLI on Cloud Shell**.
![alt text](images/image-1.png)
4. In the cloud shell clone the OCI Function from this github repository and enter the directory.
![alt text](images/image-2.png)
5. Edit the `func.yaml` file using your preferred text editor (`vim` or `nano`) and set the value of `DYNATRACE_TENANT` to your Dynatrace tenant URL. If you're using new Dynatrace then the you may need to replace 'apps' with 'live' in the tenant URL.  
Ex: `https://<tenant id>.apps.dynatrace.com` should be `https://<tenant id>.live.dyntrace.com`
    - If you're using token based authentication, set the  value of  `DYNATRACE_API_KEY` to an API token that has the `metrics.ingest` scope.
    - If you're using an OAuth2 Client for authentication change the `AUTH_METHOD` to `oauth` and enter your client_id, client_secret and URN. Please see the "Configuring an OAuth2 Client" section for more details on the requirements.
    - Recommended: instead of writing the API token or the OAuth client secret in `func.yaml` (where anyone who can read the function configuration can see it), keep it in OCI Vault and set `DYNATRACE_SECRET_OCID`. See "Keeping the Dynatrace secret in OCI Vault" below.
![alt text](images/image-13.png)
    - Set the configuration option `IMPORT_ALL_METRICS` if you want to import metrics from a namespace that is not supported by the OCI extension. These metrics will not have metadata associated with them. See "Namespaces without a curated mapping" below.
6. Save and exit the text editor. Now deploy the function using the command `fn -v deploy --app <application name>`
![alt text](images/image-3.png)
If the deployment succeeded then you should see the image in your OCI container registry.
![alt text](images/image-4.png)

## Create an OCI Connector
1. Navigate to the **Connector Hub** and click on 'Create connector'.
![alt text](images/image-5.png)
2. Enter a name for the connector and select 'Monitoring' as the **Source** and 'Functions' as the **Target**
![alt text](images/image-6.png)
3. Select the compartments and namespaces you want to pull metrics from.
![alt text](images/image-7.png)
4. Scroll past **Configure task** and choose the compartment, application and function that you just created in the previous setps.
![alt text](images/image-8.png)
5. If you see a warning asking if you want to create a policy in a given compartment, you *must* click **Create**
![alt text](images/image-14.png)
6. Finally click 'Create'
![alt text](images/image-15.png)
7. Verify the metrics are getting ingested into Dynatrace.
![alt text](images/image-9.png)

## Configuring an OAuth2 Client
Using an OAuth2 client for the OCI functions will require you to add some additional policies to give the oauth token access to ingest metrics.

1. Visit your Dynatrace tenant's **Account Management** portal and navigate to **Identity & access management** -> **Policy Management**. Click on the add policy button.
![alt text](images/oauth/image.png)
2. Give the policy a name like **Metric Ingest** and paste the following policy statement into the large text box `ALLOW storage:metrics:write;` and click on **Save**
![alt text](images/oauth/image-1.png)
3. Now navigate to the **Service Users** page under **Identity & access management** and click on **Add service user**
![alt text](images/oauth/image-2.png)
4. Give the user a name and optionally a description then click on **Save**
![alt text](images/oauth/image-3.png)
5. Click on the 3 vertical dots on the right side of the page and select **View Service User**
![alt text](images/oauth/image-4.png)
6. Copy the **Service user email** that is displayed on this page, you will need it for step 9.
7. Click on the **+Permission** button and from the dropdown menu select the policy we created in step 2.
![alt text](images/oauth/image-5.png)
8. Under **Identity & access management** navigate to **OAuth clients** and click on **Create Client**
![alt text](images/oauth/image-6.png)
9. Paste the service user email address that you copied in setp 6 in the **Subject user email** text box then assign the user **Write metrics** (`storage:metrics:write`) permission.
![alt text](images/oauth/image-7.png)
![alt text](images/oauth/image-8.png)
10. Finally click on **Create Client** at the bottom of the page and copy the `client_id`, `client_secret` and `urn` for use with the OCI function.  

## Namespaces without a curated mapping
Namespaces that the Dynatrace extension knows (`oci_computeagent`, `oci_lbaas`, `oci_nlb`, `oci_vcn`, `oci_vpn`, `oci_blockstore`, `oci_instancepools`, `oci_filestorage`, `oci_apigateway`, `oci_faas`) always use their curated metric keys and dimensions. Any other namespace is handled as follows:

- `IMPORT_ALL_METRICS=false` (default): the namespace is skipped. This is logged once per namespace.
- `IMPORT_ALL_METRICS=true`: the namespace is ingested generically.

Generic metrics:
- **Key:** `cloud.oci.<namespace without "oci_">.<metric name>`, for example `cloud.oci.streaming.PutMessagesLatency.Time`. Characters other than letters, digits, `_`, `.` and `-` in the metric name become `_`.
- **Value:** one `min/max/sum/count` summary per minute.
- **Dimensions:** every OCI dimension, plus `cloud.provider=oci` and `oci.service=<namespace without "oci_">`.

| OCI | Dynatrace dimension |
|---|---|
| `resourceId` | `oci.resource_id` |
| `region` | `oci.region` |
| `compartmentId` | `oci.compartment_id` |
| `resourceGroup` | `oci.resource_group` |
| `resourceDisplayName` | `oci.resource_display_name` |
| any other `fooBar` | `oci.foo_bar` (camelCase becomes snake_case) |

Null, empty and `None` values are omitted. Values are cut at 255 characters, keys at 99. If two OCI dimensions map to the same name the first one wins.

**Dedup warning.** If a namespace is already collected by the Dynatrace extension (via the OCI API), do not also enable it here: both sources would write the same entities and series.

**Key prefix warning.** The extension's topology and OpenPipeline rules match metric keys by prefix (for example `cloud.oci.compute*`). A generic namespace whose name starts with the same prefix, such as `oci_computecontainerinstance`, is matched by the compute rules as well. The function logs a warning once per such namespace.

## Names for resources and compartments (name enrichment)
OCI metrics carry OCIDs only. For namespaces ingested through `IMPORT_ALL_METRICS`, the function adds two dimensions so the data is readable (and so a Smartscape node can be named):

| Dimension | Added for | Source |
|---|---|---|
| `oci.compartment_name` | every generic metric | OCI Search, `query compartment resources` |
| `oci.resource_display_name` | namespaces in the registry (`oci_streaming` out of the box) | OCI Search, one query per compartment and resource type |

- If the metric already has a `resourceDisplayName`, it is used and the lookup is skipped.
- The name is looked up **before** the metrics are sent, so it is present from the first metric.
- When a name cannot be resolved (missing permission, throttling, the circuit breaker is open) the **OCID is used as the name** and a warning with counters (`hits`, `negative_hits`, `misses`, `calls`, `failures`, `breaker`) is logged. Ingestion is never blocked. `negative_hits` are resources that were searched a moment ago and not found (not searched again for `NAME_CACHE_NEGATIVE_TTL_SECONDS`).

Every search also logs one `name lookup: region=… query=… returned=N wanted=M matched=K` line. If a search succeeds but does not return the resources that were asked for, it is a **warning**: OCI Search only returns what the caller may see, so this usually means a missing IAM policy, a wrong query or resource type, or a resource in another compartment or region.
- Names are dimensions. If a name changes (or is first the OCID and later resolved), the metric continues under a new series for that name. Query by `oci.resource_id` when you need continuity.
- Curated namespaces are not enriched, their output does not change.

| Setting | Default | Meaning |
|---|---|---|
| `ENRICH_NAMES` | `true` | `false` disables enrichment completely (the OCI SDK is not even loaded). |
| `ENRICHMENT_REGISTRY_JSON` | not set | Add or override namespaces, see below. |
| `NAME_CACHE_TTL_SECONDS` | `900` | How long a name is remembered (+/-20% jitter). |
| `NAME_CACHE_NEGATIVE_TTL_SECONDS` | `120` | How long "not found" or "lookup failed" is remembered. |
| `NAME_CACHE_MAX_ENTRIES` | `10000` | Memo size; the oldest entries are dropped first. |
| `MAX_LOOKUP_CALLS_PER_INVOCATION` | `5` | OCI Search calls (including result pages) per invocation. |
| `MAX_LOOKUP_SECONDS_PER_INVOCATION` | `10` | Time budget for lookups per invocation. |

How lookups stay cheap and safe (the OCI APIs are throttled per tenancy, the limits are not published and a 429 has no `Retry-After`):
- One paginated search per region, compartment and resource type, per invocation. **All** resources it returns are remembered, not only the ones asked for, so a second stream in the same compartment costs nothing. Compartment names use one search per region.
- The memo lives in the container's memory only. A cold start simply looks the names up again. Nothing is stored anywhere.
- Only one lookup per group runs at a time. After 3 consecutive 429 / 5xx / timeouts, lookups pause for 60 seconds, then 120, doubling up to 600. SDK calls retry at most 3 times within 10 seconds, with jittered exponential backoff.

**Registry.** `ENRICHMENT_REGISTRY_JSON` maps a namespace to `resource_type`, `id_dimension` (default `resourceId`), `name_dimension` (default `oci.resource_display_name`) and optionally `query`. A field overrides only that field, `null` removes a namespace, and a new namespace needs `resource_type`:
```
{"oci_queue": {"resource_type": "queue"}, "oci_streaming": {"name_dimension": "oci.stream_name"}}
```
The Search query text and resource type names are not verified against a live tenancy (see "Not verified"). Test one with `oci search resource structured-search --query-text "query stream resources where compartmentId = '<ocid>'"` and fix it through `query` / `resource_type` if needed, no code change is required.

### IAM policies
The function authenticates to OCI with its resource principal. Create a dynamic group for the function and policies, for example (adapt names and scopes):
```
Dynamic group:  ALL {resource.type = 'fnfunc', resource.compartment.id = '<function compartment OCID>'}

Allow dynamic-group <dynamic group> to inspect compartments in tenancy
Allow dynamic-group <dynamic group> to inspect streams in tenancy
```
According to the Oracle documentation ("Details for Search"), the Search service itself needs **no** policy: what a search returns depends on the `inspect` permission the caller has for each resource type (`STREAM_INSPECT` for `streams`, `COMPARTMENT_INSPECT` for `compartments`). Without it Search does **not** fail, it returns no results, and the names fall back to OCIDs. Add one `inspect <resource type> in tenancy` statement per namespace you add to the registry (for a service that does not support `inspect`, Oracle says the `read` verb is used instead).

The statements above follow that documentation but were still **not verified against a live tenancy**. Policies that use `in tenancy` must be created in the root compartment, and with Identity Domains the dynamic group is written `<domain name>/<group name>`. To find out what is missing, look for the log line `name lookup: … returned=0`: it means the search ran and found nothing.

### Permissions for more namespaces: two profiles
Name enrichment only works for resources the function's dynamic group may `inspect`. When you add namespaces to the registry, choose one of these profiles:

**Minimal (recommended default): one `inspect` statement per resource type you enrich.** The function can only list the metadata of what you named.
```
Allow dynamic-group <dynamic group> to inspect compartments in tenancy
Allow dynamic-group <dynamic group> to inspect streams in tenancy
Allow dynamic-group <dynamic group> to inspect <resource type> in tenancy      # one per added namespace
```
Use `in compartment <name>` instead of `in tenancy` to limit it further.

**Broad: one statement for every resource type, present and future.** No IAM change is needed when a namespace is added.
```
Allow dynamic-group <dynamic group> to inspect all-resources in tenancy
```
`all-resources` is the aggregate resource type of OCI IAM (see "Policy Syntax" in the Oracle documentation). The function only reads names, but the credential becomes wide: it can list the metadata of everything in the tenancy, which many security teams do not accept. Do not use `read all-resources`, it is far broader than needed. Oracle documents that some services do not recognise `inspect` (for example listing instances needs `read`). For those, Search returns nothing under `inspect`, and you must add a `read` statement for that resource type only.

| Namespace | Search resource type | Permission needed to see it in Search | Status |
|---|---|---|---|
| `oci_streaming` | `stream` | `inspect streams` (`STREAM_INSPECT`) | permission documented by Oracle, query not yet verified in a tenancy |
| (compartment names) | `compartment` | `inspect compartments` (`COMPARTMENT_INSPECT`) | documented by Oracle, not yet verified in a tenancy |
| any other | — | — | **not verified**, do the check below |

To add a namespace:
1. Find the resource type and check the query with your own user, for example `oci search resource structured-search --query-text "query <type> resources where compartmentId = '<ocid>'"`.
2. Add it to `ENRICHMENT_REGISTRY_JSON` (see above) and grant `inspect` (or `read`) for that type to the dynamic group.
3. Look for the log line `name lookup: … returned=N wanted=M matched=K`. `returned=0` means the search ran and found nothing: almost always a missing permission or a wrong query.

The policy for reading the Dynatrace secret from OCI Vault is in the next section.

## Keeping the Dynatrace secret in OCI Vault
By default the API token (`DYNATRACE_API_KEY`) or OAuth client secret is stored in the function configuration in plain text. To avoid that:

1. In the OCI console create a secret in a Vault (**Identity & Security > Vault > Secrets**). Its content is the API token if `AUTH_METHOD=token`, or the OAuth client secret if `AUTH_METHOD=oauth` (`OAUTH_CLIENT_ID` and `OAUTH_ACCOUNT_URN` stay in `func.yaml`).
2. Copy the secret OCID and set `DYNATRACE_SECRET_OCID` in `func.yaml` (the line is commented out by default). When it is set, `DYNATRACE_API_KEY` / `OAUTH_CLIENT_SECRET` are not used.
3. Let the function read it, using the same dynamic group as above (this statement was **not verified against a live tenancy**):
```
Allow dynamic-group <dynamic group> to read secret-bundles in compartment <vault compartment> where target.secret.id = '<secret OCID>'
```

The value is read once, cached, and read again after a 401 (token) or a rejected client secret (OAuth), so rotating the secret needs no redeploy. It is never logged. If the secret cannot be read, a temporary OCI error (throttling, 5xx, timeout) fails the invocation so Connector Hub redelivers the batch, while a permanent one (for example `NotAuthorizedOrNotFound`, usually a missing policy) is logged as an error and the lines are dropped. In both cases the function logs show the cause.

## Memory and timeout
Name enrichment and Vault use the OCI SDK, so `func.yaml` now sets **512 MB** of memory (it was 256) and a **120 second** timeout. If you deploy with your own settings, keep at least these values.

## Choosing which compartments are ingested
Large tenancies often contain compartments (dev, QA, ...) that should not be sent to Dynatrace. From cheapest to most flexible:

1. **Scope the connector (recommended first step).** In Connector Hub a Monitoring source lists compartments and, per compartment, the namespaces to read. Metrics from compartments you do not list never reach the function. You can create several connectors that use the same function, for example one per environment. Whether a listed compartment includes its sub-compartments is not documented in the pages we checked, so list sub-compartments explicitly unless you have verified otherwise.
2. **Allow/deny lists in the function** (this setting). Useful when the connector has to read a compartment tree but only part of it is wanted, and as a safety net.

| Setting | Default | Meaning |
|---|---|---|
| `COMPARTMENT_ALLOWLIST` | not set | OCIDs separated by commas, semicolons or whitespace. When set, only metrics from these compartments are ingested. |
| `COMPARTMENT_DENYLIST` | not set | Metrics from these compartments are never ingested. Wins over the allowlist. |

- Matching is on the exact compartment OCID in the metric. Sub-compartments are not included automatically.
- With neither setting the function ingests everything, exactly as before. Filtering applies to every namespace.
- A metric without a compartment OCID is skipped when an allowlist is set.
- Each invocation logs one line such as `compartment filter: {"skipped_items": 12, ...}` listing the compartments that were skipped.
- All OCI Function configuration values together may not exceed 4 KB, which is roughly 30-40 OCIDs next to the other settings. Use a connector per environment for more.

## Delivery behaviour and settings
The function collects every metric in a Connector Hub batch and posts them to Dynatrace in as few requests as possible.

| Setting | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `false` | `true` logs the MINT lines instead of sending them. No Dynatrace credentials are needed. |
| `MAX_PAYLOAD_BYTES` | `900000` | Maximum size of one request (the API limit is 1 MB). |
| `MAX_LINES_PER_REQUEST` | `500` | Maximum number of lines in one request. |
| `RETRY_BUDGET_SECONDS` | `45` | Total time spent retrying 429 / 5xx / timeouts per invocation. |
| `CURATED_SEND_ALL_BUCKETS` | `false` | Curated namespaces send only the latest minute of each payload (historical behaviour). `true` sends all minutes. |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every payload and MINT line. |

Errors are handled as follows. Connector Hub delivers at-least-once and does not send later batches until the function succeeds, but Dynatrace drops metrics older than one hour, so retries are bounded:
- 429, 5xx and timeouts are retried (up to 3 attempts per request, within `RETRY_BUDGET_SECONDS`). Every request of the batch is attempted. If any still failed, the function raises at the end so Connector Hub redelivers the batch.
- Other 4xx responses (for example a rejected line or a wrong token) are logged with the status and response body, and are not retried.

The function timeout is set to 120 seconds in `func.yaml`.

## Advanced: deploying without `fn deploy` (restricted networks)
`fn deploy` builds the image where you run it, which needs access to Docker Hub (the `fnproject` base images), PyPI and the registry. If your network blocks that and the build fails, build the image yourself and create the function in the console. Nothing in the function code changes; only how the image gets to OCI and where the configuration lives.

> The Dockerfile below is the one that was used and worked in a restricted corporate network (Brazil, `GENERIC_ARM`); only the customer-specific proxy values were replaced by placeholders. Your base-image mirror, proxy and certificate setup may differ.

Do **not** name the file `Dockerfile` inside this folder: `fn deploy` would then use it instead of `func.yaml`. Name it `Dockerfile.manual` and keep it in this folder (next to `func.py` and `requirements.txt`), because the build copies the whole folder.

### 1. Dockerfile.manual
```dockerfile
# Dockerfile.manual
FROM fnproject/python:3.11-dev AS build-stage

# Only if the build must go through a proxy. These variables exist in the build stage only;
# the final image starts from a clean base and does not contain them.
# ENV http_proxy=http://<proxy host>:<port>
# ENV https_proxy=http://<proxy host>:<port>

# Only if a TLS-intercepting proxy breaks certificate checks for PyPI. This disables
# the check for these hosts, so prefer installing your corporate CA certificate instead.
# Set both hosts in one command: a second `pip config set global.trusted-host` replaces the first.
# RUN pip config set global.trusted-host "pypi.org files.pythonhosted.org"

WORKDIR /function

COPY requirements.txt .

RUN pip install \
    --target /python \
    --no-cache-dir \
    -r requirements.txt

COPY . .

FROM fnproject/python:3.11

WORKDIR /function

COPY --from=build-stage /python /python
COPY --from=build-stage /function /function

ENV PYTHONPATH=/function:/python

ENTRYPOINT ["/python/bin/fdk", "/function/func.py", "handler"]
```
- If Docker Hub is blocked, replace both `fnproject/python` references with your internal mirror of the same images (`3.11-dev` for the build stage, `3.11` for the final one).
- The final stage must keep `PYTHONPATH` and the `ENTRYPOINT`: the `fdk` runtime is installed into `/python` by `requirements.txt`.
- Add a `.dockerignore` with `tests/`, `images/`, `.git/` and `*.md` for a smaller image.

### 2. Build, tag and push with Podman
The application shape in this guide is `GENERIC_ARM`, so build for `linux/arm64` (on an x86 machine this needs QEMU emulation; Apple silicon builds it natively). If your application uses `GENERIC_X86`, use `linux/amd64`.
```bash
# Region key of the OCIR endpoint: gru = Sao Paulo, iad = Ashburn, fra = Frankfurt (see the Oracle documentation for others)
export REGION_KEY=gru
export TENANCY_NAMESPACE=<tenancy namespace>      # Tenancy details page, "Object storage namespace"
export REPO=<repository name>
export VERSION=0.2.0

# run from this folder; the final dot is the build context
podman build --platform linux/arm64 -f Dockerfile.manual -t oci-metric-ingestion:$VERSION .

# user: <tenancy namespace>/<username>  (with a non-default Identity Domain: <tenancy namespace>/<domain>/<username>)
# password: an OCI auth token, not the console password
podman login ${REGION_KEY}.ocir.io

podman tag  oci-metric-ingestion:$VERSION ${REGION_KEY}.ocir.io/${TENANCY_NAMESPACE}/${REPO}/oci-metric-ingestion:$VERSION
podman push ${REGION_KEY}.ocir.io/${TENANCY_NAMESPACE}/${REPO}/oci-metric-ingestion:$VERSION
```
Use a new tag (or the version number) for every build. Re-pushing the same tag may not make the function pick up the new image.

### 3. Create the function in the console
1. **Developer Services > Functions > Applications**, open the application (shape `GENERIC_ARM`, as in "Setup the Application").
2. **Functions > Create function**, and choose the option that uses an existing image from the registry (the wording changes between console versions). Select the image and tag you pushed.
3. Set **Memory 512 MB** and **Timeout 120 seconds**. These are the values in `func.yaml`; the OCI SDK needs the memory.
4. If the function fails to start with an image pull error, the service may lack permission to read the repository. Oracle documents a policy for the Functions service to read repositories; check its current wording ("Creating Policies to Control Access to Network and Function-Related Resources") and make sure the repository is in a compartment the policy covers. This was not verified here.

### 4. Set the configuration in the console
With `fn deploy` the settings come from `func.yaml`. When you create the function by hand, `func.yaml` is **not read at all**: open the function, **Configuration**, and add each key and value. Remember:
- Every value is plain text: write `true` / `false` and numbers as they are, without quotes, in the console field.
- Add only the keys you need; every other key has a default. Do **not** add the `<…>` placeholders from `func.yaml`.
- All values together are limited to 4 KB.

| Key | Needed | Value |
|---|---|---|
| `DYNATRACE_TENANT` | yes | Tenant URL (see "Setup the Application", step 5) |
| `AUTH_METHOD` | yes | `token` or `oauth` |
| `DYNATRACE_API_KEY` | `token`, unless Vault is used | API token with `metrics.ingest` |
| `OAUTH_CLIENT_ID`, `OAUTH_ACCOUNT_URN` | `oauth` | from the OAuth client |
| `OAUTH_CLIENT_SECRET` | `oauth`, unless Vault is used | the client secret |
| `DYNATRACE_SECRET_OCID` | recommended | OCID of the Vault secret; replaces the two secret keys above (see "Keeping the Dynatrace secret in OCI Vault") |
| `IMPORT_ALL_METRICS` | for streaming | `true` |
| `ENRICH_NAMES` | optional | `true` (default) or `false` |
| `COMPARTMENT_ALLOWLIST`, `COMPARTMENT_DENYLIST` | optional | OCIDs, see "Choosing which compartments are ingested" |
| `ENRICHMENT_REGISTRY_JSON` | optional | JSON, see "Names for resources and compartments" |
| `PROXY_URL`, `PROXY_USERNAME`, `PROXY_PASSWORD` | optional | only if a proxy is required |
| `DRY_RUN` | for the first test | `true` logs the lines instead of sending them; remove it or set `false` afterwards |
| `LOG_LEVEL` | optional | `INFO` (default) or `DEBUG` |

The delivery and cache settings (`MAX_PAYLOAD_BYTES`, `RETRY_BUDGET_SECONDS`, `NAME_CACHE_*`, `MAX_LOOKUP_*`, …) are optional; see `func.yaml` for their meaning and defaults.

Anyone with access to the function in the console can read these values. Use `DYNATRACE_SECRET_OCID` for the token or client secret.

### 5. Updating the function later
Build and push a new tag, then change the image of the function in the console (**Edit**), and keep the configuration. The Connector Hub connector does not need to change.

## Running the tests
```
pip install -r requirements-dev.txt
pytest
```
The tests do not make network calls.

## Debugging 
If you are running into issues getting the connector to work, go to the application and enable **Function Invocation Logs**.
![alt text](images/image-12.png)
Any errors will be logged here as well as some information about when the function has been run.
![alt text](images/image-11.png)
