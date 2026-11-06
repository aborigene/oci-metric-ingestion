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
