# How to back up and restore

Charmed Valkey can stream a point-in-time RDB backup of your dataset to object
storage, list the backups it has taken, and restore the whole cluster from any of
them. Both S3-compatible object storage and Azure Blob storage are supported.

Backups are driven by three charm actions — `create-backup`, `list-backups`, and
`restore` — and by a related storage-integrator charm that holds the storage
credentials. You relate **either** the [`s3-integrator`](https://charmhub.io/s3-integrator)
**or** the [`azure-storage-integrator`](https://charmhub.io/azure-storage-integrator),
never both at once: the two backends are mutually exclusive, and relating both leaves
Charmed Valkey blocked until you remove one.

## Relate an object-storage integrator

Choose one of the two options below depending on where you want to store backups.
In both cases the credentials live in the integrator charm — Charmed Valkey reads
them over the relation and never stores them in plain text.

### Option A — S3-compatible storage

Deploy the S3 integrator:

```shell
juju deploy s3-integrator --channel 2/edge
```

Store the access key and secret key in a [Juju secret](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/secret/)
and grant it to the integrator:

```shell
juju add-secret s3-creds access-key=<ACCESS_KEY> secret-key=<SECRET_KEY>
juju grant-secret s3-creds s3-integrator
```

Point the integrator at the secret and configure the bucket:

```shell
juju config s3-integrator \
  credentials=secret:<SECRET_ID> \
  bucket=<BUCKET> \
  endpoint=<ENDPOINT_URL> \
  region=<REGION> \
  path=<PATH_PREFIX>
```

If your endpoint uses a private or self-signed certificate, base64-encode its CA
chain and pass it as `tls-ca-chain` so the integrator can verify the endpoint over
HTTPS:

```shell
juju config s3-integrator tls-ca-chain="$(base64 -w0 ca-chain.pem)"
```

Finally, integrate Charmed Valkey with the S3 integrator on the `s3-credentials`
endpoint:

```shell
juju integrate valkey:s3-credentials s3-integrator
```

### Option B — Azure Blob storage

Deploy the Azure storage integrator:

```shell
juju deploy azure-storage-integrator --channel 1/edge
```

Store the storage-account key in a Juju secret (the content key must be
`secret-key`) and grant it to the integrator:

```shell
juju add-secret azure-creds secret-key=<STORAGE_ACCOUNT_KEY>
juju grant-secret azure-creds azure-storage-integrator
```

Point the integrator at the secret and configure the container:

```shell
juju config azure-storage-integrator \
  credentials=secret:<SECRET_ID> \
  container=<CONTAINER> \
  storage-account=<STORAGE_ACCOUNT> \
  connection-protocol=https \
  path=<PATH_PREFIX>
```

Finally, integrate Charmed Valkey with the Azure storage integrator on the
`azure-credentials` endpoint:

```shell
juju integrate valkey:azure-credentials azure-storage-integrator
```

```{note}
See [Azure limitations](#azure-limitations) below before choosing an endpoint or
connection protocol for Azure.
```

## Create a backup

Once an integrator is related and `active`, take a backup. `create-backup` can run
on **any** unit — leader or follower — and streams that unit's dataset straight to
object storage:

```shell
juju run valkey/leader create-backup
```

On success the action returns the identifier of the new backup:

```text
backup-id: 2026-07-20T12:30:00Z
```

## List backups

List the backups currently in the configured bucket or container, newest first:

```shell
juju run valkey/leader list-backups
```

By default the list is rendered as a table. Pass `output=json` for machine-readable
output:

```shell
juju run valkey/leader list-backups output=json
```

`list-backups` is read-only and safe to run while another backup is still uploading.

## Restore a backup

Restoring replaces the dataset on **all** units with the contents of the chosen
backup, so it must run on the **leader** unit. Pass the `backup-id` exactly as it
appears in `list-backups`:

```shell
juju run valkey/leader restore backup-id=2026-07-20T12:30:00Z
```

```{caution}
`restore` overwrites the current dataset on every unit. Take a fresh backup first
if you might need the current data again.
```

The action confirms that the restore was initiated:

```text
restore: initiated for 2026-07-20T12:30:00Z
```

Charmed Valkey then coordinates the restore across the cluster and returns to
`active` once every unit has loaded the restored dataset.

## Troubleshooting

Two application statuses cover the configuration mistakes:

- `Missing or invalid backup storage credentials` — an integrator is related but
  Charmed Valkey has nothing usable stored. The most common cause is an unset
  `path`: the integrators default it to empty, and Charmed Valkey requires it so
  that `list-backups` can never enumerate a whole bucket or container. Check the
  integrator's config and `juju debug-log` on the leader.
- `More than one backup storage integrator related; relate exactly one` — both `s3-integrator` and
  `azure-storage-integrator` are related. Remove one; the survivor is picked up
  automatically, without waiting for any further event.

## Azure limitations

The Azure Blob backend supports the common case — public Azure Blob storage over
HTTPS, and plain-HTTP emulators such as [Azurite](https://github.com/Azure/Azurite)
— but it does **not** have full parity with the S3 backend:

- **No private or custom-CA HTTPS endpoint.** The `azure_storage` relation carries
  no CA-chain field, so Charmed Valkey can only verify endpoints signed by a public
  CA already in the system trust store. An Azure-compatible endpoint (for example an
  Azure Stack deployment) fronted by a private or self-signed certificate cannot be
  trusted over HTTPS. Only public Azure over HTTPS and plain-HTTP emulators work.
  If you need a custom CA, use the S3 backend instead, which accepts a
  `tls-ca-chain`.
- **`abfs` and `abfss` connection protocols are unsupported.** These designate
  ADLS-Gen2 (hierarchical-namespace) endpoints served by a different API than Blob
  storage. Charmed Valkey rejects them up front rather than mis-talking the Blob
  API to a data-lake endpoint. Use a Blob connection protocol — `https` or `wasbs`
  (HTTPS), or `http` or `wasb` (HTTP) — instead.
