# Deploy OpenBricks

OpenBricks separates the browser workspace from notebook execution. The default deployment has two long-running containers. Optional profiles add object storage and a catalog server.

| Service | Role | Default RAM ceiling | Published port |
| --- | --- | --- | --- |
| `openbricks` | Management UI, accounts, workspace metadata, scheduler, connector configuration, and run routing | 1 GB | 3000 |
| `compute` | Authenticated execution API, Python notebook sessions, Spark driver, and one notebook queue | 4 GB, with a 1 GB Spark JVM heap | Internal Docker network only |
| `seaweedfs` (`storage` profile) | SeaweedFS 4.52, providing local S3-compatible object storage | 512 MB | 8333 on host loopback by default |
| `catalog` (`catalog` profile) | Unity Catalog OSS 0.7.0 metadata API | 1 GB, with a 512 MB Java heap | Internal Docker network only |

These values are ceilings rather than memory reserved at startup. Each service's RAM-plus-swap limit matches its RAM ceiling, so no additional swap is available. Set `OPENBRICKS_CONTROL_MEMORY_LIMIT`, `OPENBRICKS_MEMORY_LIMIT`, and `SPARK_DRIVER_MEMORY` in `.env` to adjust the management, compute, and Spark limits deliberately. The Spark heap shares the compute container's limit with Python workers, native JVM memory, libraries, and the compute API.

## Start the local deployment

From this directory, copy `.env.example` to `.env` if one does not exist. Preserve an existing `.env` and data volume when upgrading. Set a unique initial administrator password and a separate randomly generated compute token containing at least 32 characters.

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f openbricks compute
```

Open `http://localhost:3000`, or the same port on the host's reachable address. Put HTTPS in front of the UI for access outside a trusted local network and enable `OPENBRICKS_SECURE_COOKIE=true` when HTTPS is in use.

The management container sends notebook source to the chosen compute service over an authenticated API. Each compute service executes one notebook at a time; distinct compute services can execute notebooks concurrently. Python variables persist between individual cell runs within a notebook on that compute service while its session remains active. Changing a notebook's selected compute service changes where its in-memory session lives.

Compute automatically shuts down its idle interpreter after 900 seconds (15 minutes) with no active or queued work. This releases the Spark JVM and Python session memory. The next notebook run starts a fresh interpreter, so in-memory variables and temporary views do not survive idle shutdown; notebooks, installed wheels, files, and persistent tables remain in their volumes. `OPENBRICKS_IDLE_TIMEOUT_SECONDS` changes the idle duration, and `0` disables this behavior. A scheduled or interactive run prevents idle shutdown while it is active or queued.

The bundled management and compute containers mount the same `openbricks-data` volume at `/data`. They share local objects, wheel artifacts, installed Python packages, and the local Spark warehouse. Their metadata databases are separate: management owns `/data/openbricks.sqlite3`, while compute owns `/data/compute-state/openbricks.sqlite3`. Keep one management process and one bundled compute owner for this shared local warehouse.

## Add the optional local storage service

Set the following values in `.env`, using unique credentials:

```dotenv
COMPOSE_PROFILES=storage
OPENBRICKS_S3_ENDPOINT=http://seaweedfs:8333
OPENBRICKS_S3_ACCESS_KEY=your-chosen-access-key
OPENBRICKS_S3_SECRET_KEY=your-unique-secret-key
```

Then run `docker compose up -d`. SeaweedFS stores its data in the separate `seaweedfs-data` volume. Its published S3 endpoint defaults to `http://127.0.0.1:8333`; containers on the same Compose network use `http://seaweedfs:8333`.

The S3 connector can also point to an existing S3-compatible service on this host or elsewhere. No bundled storage service is required when using an existing endpoint. An endpoint must be reachable from the compute service that will run the notebook, not merely from the management server. Native Spark `s3a://` access uses the endpoint, region, and storage credentials passed to the compute container; named notebook connections receive their own saved configuration when a run is dispatched.

## Add the optional Unity Catalog service

Enable the `catalog` profile and point the compute configuration at the internal catalog endpoint:

```dotenv
COMPOSE_PROFILES=storage,catalog
OPENBRICKS_CATALOG_URL=http://catalog:8080
OPENBRICKS_CATALOG_NAMES=unity
```

`OPENBRICKS_CATALOG_NAMES` is a comma-separated list of catalog names to attach to Spark at kernel startup. The compute image contains Unity Catalog's Spark 4.0 / Scala 2.13 connector version 0.7.0 and Delta Spark 4.3.1. Notebook startup uses those local jars and does not download dependencies from Maven.

The default `OPENBRICKS_CATALOG_S3_BUCKETS=openbricks` configures storage credentials for the bundled bucket. Add other bucket names deliberately when registering their tables. External Parquet table queries through Unity Catalog and native Spark S3A have been checked against the bundled SeaweedFS deployment. Creating metadata does not copy data or make an unreachable storage location accessible.

Three-part names such as `unity.default.example` address catalog tables. The existing Hive-backed `spark_catalog` remains the default for unqualified table names. Tables stored on the local filesystem must use locations visible to both catalog and compute; the bundled deployment shares `/data` for that purpose. Remote compute needs shared object storage or separately provisioned access to the same table files.

The bundled catalog is an internal, trusted-team metadata service with authorization disabled in its development configuration. It does **not** provide enterprise role-based access control or enforce a security boundary between notebook users. Do not expose that catalog port publicly as configured. A separately secured catalog deployment can use `OPENBRICKS_CATALOG_TOKEN` for Spark's static-token authentication. Configure the catalog's own authorization and storage credentials according to that deployment's requirements.

## Deploy a compute service on another host

Build the compute image, or transfer the built image to a compatible Docker host:

```powershell
docker build --target compute -t openbricks-compute:local .
docker save openbricks-compute:local -o openbricks-compute-image.tar
```

On the destination host:

```bash
docker load -i openbricks-compute-image.tar
```

Copy `compose.compute.yaml` to that host. Create a local `.env` beside it containing a **new token for this compute service**, at least 32 characters long. For example, `openssl rand -hex 32` generates a random token on systems with OpenSSL. Keep it private; do not reuse the management administrator's password.

```dotenv
OPENBRICKS_COMPUTE_TOKEN=replace-with-your-unique-random-token
OPENBRICKS_MEMORY_LIMIT=4g
SPARK_DRIVER_MEMORY=1g
SPARK_MASTER=local[2]
OPENBRICKS_IDLE_TIMEOUT_SECONDS=900
```

Start the remote service:

```bash
docker compose -f compose.compute.yaml up -d
docker compose -f compose.compute.yaml ps
```

By default, that file publishes port 8001 only on `127.0.0.1`. Put an HTTPS reverse proxy on that host in front of `http://127.0.0.1:8001`, preserve the `Authorization` header, and use the resulting HTTPS URL when registering the service. The compute service requires its bearer token for status, execution, cancellation, and package operations; `/health` exposes only a minimal health response.

In **Compute clusters** in the management UI:

1. Add a cluster name, its compute-service URL, and its unique token.
2. Test the connection.
3. Choose that cluster for a notebook or scheduled job.

The URL identifies an **OpenBricks compute service**, rather than a raw Spark master. HTTP is supported only when explicitly allowed for a trusted local network. Keep TLS verification enabled for HTTPS. The built-in local cluster's endpoint and token are managed through the deployment configuration rather than edited in the UI.

### Data and packages on remote compute

A remote compute service has its own `compute-data` volume. Registering it does not copy the management server's filesystem or make its `blob://` local buckets shared. A local blob URI refers to that compute host's local storage.

Before dispatching a remote notebook, management synchronizes its installed wheel artifacts to that compute service. The agent installs them without fetching dependencies from a package registry. Upload dependency wheels explicitly. Raw Git repository trees and arbitrary files are **not** automatically replicated; package reusable project code into wheels and use an accessible S3 endpoint for shared datasets.

The same network rule applies to catalog and storage endpoints. Names such as `compute`, `seaweedfs`, and `catalog` are private names on the original Docker network and do not resolve automatically from another host. Configure remote agents with independently reachable catalog/S3 URLs, credentials, and routes. Do not assume that testing a connector from the management host proves reachability from a different compute host.

### Use an existing multi-node Spark standalone cluster

The default compute service runs Spark locally with two execution threads. It can instead act as the driver for an existing Spark standalone cluster by setting `SPARK_MASTER=spark://your-master:7077` in the remote compute deployment.

For this mode, set `SPARK_DRIVER_HOST` to an address reachable by the Spark workers and `SPARK_DRIVER_BIND_ADDRESS=0.0.0.0`. Expose and route the required driver/block-manager ports deliberately, and configure your Spark deployment's network and security settings. Loopback driver addresses only work for local mode. The supplied Compose file publishes the compute API port; it does not automatically publish Spark driver ports or create a Spark master and workers.

Workers must be able to access the same table/data locations and compatible Python dependencies. A Docker volume on one host is not distributed storage. Use shared object storage or another storage system accessible to every participating worker. This integration does not provision machines, autoscale Spark workers, or turn unrelated agents into a single Spark cluster automatically.

See the official [Spark standalone deployment guide](https://spark.apache.org/docs/4.0.1/spark-standalone.html) for master/worker provisioning, driver connectivity, and security.

## Trust, secrets, and backups

OpenBricks is for a trusted team. Notebook code and wheel installation run with the compute service account's permissions. Dispatch includes notebook source and only the notebook's explicitly selected connections, constrained by project assignments. An empty selection sends no named connections. Project roles and compute allowlists control application actions; they do not isolate arbitrary Python from shared files, environment settings, or the compute account's underlying privileges. Only register hosts you trust. Use HTTPS for remote services and limit network access to the intended management server and collaborators.

Saved connection credentials and remote compute tokens are encrypted at rest using `/data/secrets.key`, unless `OPENBRICKS_SECRET_KEY` is explicitly provided. Back up the metadata database **together with its key**; restoring one without the other prevents saved credentials from being decrypted. Treat both as sensitive backup material. Include local notebook/object data, wheel artifacts, the Spark warehouse, and separate SeaweedFS/catalog volumes in your backup plan.

The Operations screen can create streamed backups of the management volume, optionally encrypted with a passphrase. It waits for in-flight edits and requires idle compute before snapshotting. Restore validates the archive in staging, replaces local state, and restarts the managed container. External SQL Server databases, remote object storage, separate catalog/SeaweedFS volumes, `.env`, and independently configured compute hosts need their own backups. Keep an externally supplied `OPENBRICKS_SECRET_KEY` separately. See [Operations](docs/OPERATIONS.md) for limits and recovery steps.

## Versioned images and upgrades

The release workflow publishes separate management and compute images. After confirming a release's packages are available, use `docker compose -f compose.release.yaml pull` and `docker compose -f compose.release.yaml up -d` with the same `.env` and project name. Both Compose files use the same named data volume. Never run two management owners against it simultaneously. `compose.yaml` remains the source-build option. Image overrides are `OPENBRICKS_RUNTIME_IMAGE` and `OPENBRICKS_COMPUTE_IMAGE`.

Before an upgrade, back up local data and separate storage/catalog volumes. Stop new submissions and wait for active work to finish. The first start migrates existing records into the shared default project; legacy members retain editor access there. Review assignments before inviting more users. Use matching management/compute versions and retain old images needed by pinned environments. A code rollback alone does not undo metadata changes; use a validated pre-upgrade backup if restoring an older release. See [release process](docs/RELEASING.md).

An image export contains the runtime, not your data volumes. Before moving or replacing a host, back up the relevant volumes separately and restore their ownership so the service account can write them. Avoid `docker compose down -v` unless deleting those persistent volumes is intentional.

## Docker Desktop and WSL host memory

Docker Desktop's virtual-machine memory limit is separate from each container's limit. On Windows with the WSL backend, you can configure a host-wide ceiling and automatic cache reclamation in `%USERPROFILE%\.wslconfig`. For example, the following limits the entire WSL subsystem to 6 GB; choose a value that fits your machine and workloads:

```ini
[wsl2]
memory=6GB

[experimental]
autoMemoryReclaim=dropCache
```

The example's 6 GB ceiling is shared by Docker and all other WSL distributions. Container maximums are not reserved at startup, but a host ceiling below their combined demand can constrain concurrent work. Large builds, the optional services, or multiple busy compute services may require more host memory. Idle interpreter shutdown releases Spark's own memory; WSL reclamation returns unused filesystem cache after the subsystem becomes idle. Restarting WSL to apply its settings interrupts all running WSL distributions, so finish their work and restore required services afterward.

See [Docker's WSL guidance](https://docs.docker.com/desktop/features/wsl/) and [Microsoft's WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config).
